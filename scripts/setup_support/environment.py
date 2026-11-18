"""Dedicated environment installation; never modify the caller's environment."""
from __future__ import annotations

import json
import os
import platform
import re
import stat
import tempfile
from pathlib import Path

from . import backend_sources as source_support
from .detect import run
from .state import SetupError, atomic_write


def install(prefix: Path, manager: str, manager_path: str, manifest: dict,
            source: str | None = None, *, backend_sources: list[dict] | None = None) -> None:
    if prefix.is_symlink():
        raise SetupError(f"Refusing symlink environment: {prefix}")
    marker = prefix.parent / ("." + prefix.name + ".biomodelling-setup.json")
    if prefix.exists() and not marker.is_file():
        raise SetupError(f"Environment already exists and is not setup-managed: {prefix}; choose a new --env-prefix")
    desired = {"package": manifest["package"], "version": manifest["version"],
               "manager": manager, "source": source,
               "dependency_pins": manifest.get("dependency_pins", {})}
    if backend_sources:
        if source:
            raise SetupError("Cannot mix backend sources and package source")
        source_support.validate_versions(backend_sources, manifest)
        source_support.validate_sources(backend_sources)
        desired["backend_sources"] = backend_sources
    if marker.is_file() and json.loads(marker.read_text()) != desired:
        raise SetupError("Environment specification changed; choose a new --env-prefix to upgrade")
    prefix.parent.mkdir(parents=True, exist_ok=True)
    if not marker.exists():
        atomic_write(marker, json.dumps(desired).encode())
    if not (prefix/"bin/python").is_file():
        if manager == "conda":
            run([manager_path, "create", "--yes", "--prefix", str(prefix),
                 "--override-channels", "--channel", "conda-forge",
                 "python=" + manifest["python_environment"], "pip"], timeout=1800)
        else:
            run([manager_path, "-m", "venv", str(prefix)], timeout=180)
    python = str(prefix/"bin/python")
    requirement = source or f'{manifest["package"]}=={manifest["version"]}'
    dependencies = [f"{name}=={version}" for name, version in sorted(manifest.get("dependency_pins", {}).items())]
    # A completed environment is reused; verification checks for drift.
    if not (prefix/".setup-installed").exists():
        if backend_sources:
            with source_support.snapshots(backend_sources) as paths:
                source_names = {entry["name"] for entry in backend_sources}
                source_dependencies = [
                    f"{name}=={version}"
                    for name, version in sorted(manifest.get("dependency_pins", {}).items())
                    if name not in source_names
                ]
                run([python, "-m", "pip", "install", "--index-url", manifest["index_url"],
                     *source_dependencies, *[str(paths[e["name"]]) for e in backend_sources]], timeout=1800)
                source_support.write_receipt(prefix, backend_sources, paths)
        else:
            run([python, "-m", "pip", "install", "--index-url", manifest["index_url"], *dependencies, requirement], timeout=1800)
        run([python, "-m", "pip", "check"], timeout=60)
        frozen = run([python, "-m", "pip", "freeze"], timeout=60).stdout
        atomic_write(prefix/"resolved-requirements.txt", frozen.encode())
        atomic_write(prefix/".setup-installed", b"installed\n")


SYSTEM_PATH = ("/usr/local/sbin", "/usr/local/bin", "/usr/sbin", "/usr/bin", "/sbin", "/bin")


def system_path(system: str | None = None) -> tuple[str, ...]:
    """Stable system paths, including Homebrew on Apple Silicon."""
    system = system or platform.system()
    homebrew = ("/opt/homebrew/sbin", "/opt/homebrew/bin") if system == "Darwin" else ()
    return (*homebrew, *SYSTEM_PATH)


def transport_path(prefix: Path | None, *, graphviz_path: str | None = None,
                   uvx: str | None = None) -> str:
    """Keep explicit dependency directories without inheriting transient helpers."""
    external = [str(Path(graphviz_path).expanduser().absolute().parent)] if graphviz_path else []
    # uvx delegates to the uv binary installed beside it.
    first = [str(Path(uvx).parent)] if uvx else [str(prefix / "bin")]
    return os.pathsep.join(dict.fromkeys([*first, *external, *system_path()]))


def plugin_requirement(manifest: dict) -> str:
    return f'{manifest["package"]}=={manifest["version"]}'


def plugin_arguments(manifest: dict, server: str) -> list[str]:
    """The marketplace plugin's own launch arguments for one server."""
    return ["--from", plugin_requirement(manifest), f"mcp-{server}-server"]


def cache_root() -> Path:
    """$XDG_CACHE_HOME when absolute (relative values are invalid per XDG), else ~/.cache."""
    configured = os.environ.get("XDG_CACHE_HOME", "")
    return Path(configured) if os.path.isabs(configured) else Path.home()/".cache"


def server_tmpdir(server: str) -> Path:
    """Disk-backed temporary directory for one modelling server.

    Servers write large engine scratch files (MaBoSS ``maboss-run-*`` directories)
    through ``tempfile``; the system /tmp may be a small RAM-backed tmpfs.
    """
    return cache_root()/"mcp-biomodelling"/"tmp"/server


_TMPDIR = re.compile(r'"?TMPDIR"?\s*[:=]\s*("(?:[^"\\]|\\.)*")')


def rendered_tmpdirs(outputs: dict[Path, str]) -> dict[Path, str]:
    """The TMPDIR each rendered configuration file assigns (YAML or TOML string)."""
    found = {}
    for path, text in outputs.items():
        match = _TMPDIR.search(text)
        if match:
            found[path] = json.loads(match[1])
    return found


def ensure_private_tmpdir(path: Path) -> bool:
    """Create ``<cache>/mcp-biomodelling/tmp/<server>`` as a private (0700) real directory.

    Refuses a symlinked ``mcp-biomodelling``, ``tmp`` or server directory. Returns
    True when the server directory had to be created.
    """
    path = Path(path)
    created = False
    for candidate in (path.parent.parent, path.parent, path):
        if candidate.is_symlink():
            raise SetupError(f"Refusing symlink server TMPDIR component: {candidate}")
        if not candidate.exists():
            candidate.mkdir(parents=True, mode=0o700)
            os.chmod(candidate, 0o700)
            created = candidate == path
        elif not candidate.is_dir():
            raise SetupError(f"Server TMPDIR component is not a directory: {candidate}")
    os.chmod(path, 0o700)
    return created


def ensure_server_tmpdirs(outputs: dict[Path, str]) -> list[str]:
    """Create every rendered TMPDIR as a private (0700) real directory."""
    created = []
    for directory in sorted(set(rendered_tmpdirs(outputs).values())):
        ensure_private_tmpdir(Path(directory))
        created.append(directory)
    return created


def server_tmpdir_file_problems(outputs: dict[Path, str]) -> dict[Path, str]:
    """Check mode: per rendered file, a missing or mismatched TMPDIR in its current text."""
    problems = {}
    for path, expected in rendered_tmpdirs(outputs).items():
        try:
            current = rendered_tmpdirs({path: path.read_text(encoding="utf-8")}).get(path)
        except OSError:
            current = None
        if current is None:
            problems[path] = f"TMPDIR missing; expected {json.dumps(expected)}"
        elif current != expected:
            problems[path] = f"TMPDIR mismatch: found {json.dumps(current)}, expected {json.dumps(expected)}"
    return problems


def server_tmpdir_dir_problems(outputs: dict[Path, str]) -> list[str]:
    """Check mode: every rendered TMPDIR exists as a private (0700) real directory."""
    errors = []
    for directory in sorted(set(rendered_tmpdirs(outputs).values())):
        path = Path(directory)
        if path.is_symlink() or not path.is_dir():
            errors.append(f"TMPDIR directory missing: {directory}; rerun setup")
        elif stat.S_IMODE(path.stat().st_mode) != 0o700:
            errors.append(f"TMPDIR directory {directory} is not private (mode 0700); rerun setup")
    return errors


def server_transport(server: str, *, prefix: Path | None = None, plugin: dict | None = None,
                     graphviz_path: str | None = None) -> tuple[str, list[str], dict[str, str]]:
    """Command, arguments and environment for one isolated specialist transport."""
    tmpdir = str(server_tmpdir(server))
    if plugin:
        return (plugin["uvx"], plugin_arguments(plugin["manifest"], server),
                {"PATH": transport_path(None, graphviz_path=graphviz_path, uvx=plugin["uvx"]),
                 "TMPDIR": tmpdir})
    return (str(prefix/"bin"/f"mcp-{server}-server"), [],
            {"CONDA_PREFIX": str(prefix), "PATH": transport_path(prefix, graphviz_path=graphviz_path),
             "TMPDIR": tmpdir})


def runtime_env(prefix: Path, *, graphviz_path: str | None = None) -> dict:
    inherited = {k: v for k, v in os.environ.items() if k not in {"PYTHONPATH", "PYTHONHOME"}}
    return {**inherited, "PATH": transport_path(prefix, graphviz_path=graphviz_path),
            "CONDA_PREFIX": str(prefix), "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1"}


def _version_program(manifest: dict) -> str:
    program = "import importlib, importlib.metadata as m, json, sys; "
    program += f"[importlib.import_module(n) for n in {manifest['imports']!r}]; "
    program += f"dependencies = {{name:m.version(name) for name in {list(manifest.get('dependency_pins', {}))!r}}}; "
    program += f"print(json.dumps({{'version':m.version({manifest['package']!r}), 'python':list(sys.version_info[:3]), 'dependencies':dependencies}}))"
    return program


def plugin_env(uvx: str, *, graphviz_path: str | None = None) -> dict:
    inherited = {k: v for k, v in os.environ.items() if k not in {"PYTHONPATH", "PYTHONHOME", "CONDA_PREFIX"}}
    return {**inherited, "PATH": transport_path(None, graphviz_path=graphviz_path, uvx=uvx),
            "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1"}


def verify_plugin(uvx: str, manifest: dict, *, graphviz_path: str | None = None) -> dict:
    """Resolve the plugin launch and report what it actually installs.

    The plugin pins only the server package, so its other dependencies follow
    that package's ranges. Drift from the tested pins is reported, not fatal.
    """
    errors, warnings, versions = [], [], {}
    command = [uvx, "--from", plugin_requirement(manifest), "python", "-I", "-B", "-c", _version_program(manifest)]
    try:
        with tempfile.TemporaryDirectory(prefix="biomodelling-imports-") as temporary:
            versions = json.loads(run(command, cwd=Path(temporary), env={**plugin_env(uvx, graphviz_path=graphviz_path),
                "NUMBA_CACHE_DIR": str(Path(temporary)/"numba-cache")}, timeout=1800).stdout)
        if tuple(versions["python"]) < (3, 11):
            errors.append("The plugin launch resolved a Python older than 3.11")
        if versions["version"] != manifest["version"]:
            errors.append(f"Expected {manifest['package']} {manifest['version']}, found {versions['version']}")
        for name, expected in manifest.get("dependency_pins", {}).items():
            actual = versions.get("dependencies", {}).get(name)
            if actual != expected:
                warnings.append(f"Plugin launch resolved {name} {actual or 'missing metadata'}; tested pin is {expected}")
    except (SetupError, ValueError, KeyError, TypeError) as error:
        errors.append(str(error))
    return {"passed": not errors, "errors": errors, "warnings": warnings, "versions": versions,
            "launch": [uvx, *plugin_arguments(manifest, "<server>")]}


def verify_environment(prefix: Path, manifest: dict, *, graphviz_path: str | None = None) -> dict:
    errors = []
    python = prefix/"bin/python"
    versions = {}
    if not python.is_file():
        return {"passed": False, "errors": [f"Missing environment Python: {python}"]}
    program = _version_program(manifest)
    try:
        with tempfile.TemporaryDirectory(prefix="biomodelling-imports-") as temporary:
            versions = json.loads(run([str(python), "-I", "-B", "-c", program], cwd=Path(temporary), env={**runtime_env(prefix, graphviz_path=graphviz_path), "NUMBA_CACHE_DIR": str(Path(temporary)/"numba-cache")}, timeout=120).stdout)
        if tuple(versions["python"]) < (3, 11):
            errors.append("The modelling environment Python must be 3.11 or newer")
        if versions["version"] != manifest["version"]:
            errors.append(f"Expected {manifest['package']} {manifest['version']}, found {versions['version']}")
        for name, expected in manifest.get("dependency_pins", {}).items():
            actual = versions.get("dependencies", {}).get(name)
            if actual != expected:
                errors.append(f"Expected {name} {expected}, found {actual or 'missing metadata'}")
        run([str(python), "-m", "pip", "check"], env=runtime_env(prefix, graphviz_path=graphviz_path), timeout=60)
    except (SetupError, ValueError, KeyError, TypeError) as error:
        errors.append(str(error))
    for name in manifest["executables"]:
        file = prefix/"bin"/name
        if not file.is_file() or not os.access(file, os.X_OK):
            errors.append(f"Missing executable: {file}")
    return {"passed": not errors, "errors": errors, "versions": versions}
