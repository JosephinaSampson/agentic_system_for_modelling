"""Pinned Python environments for the data-analyst specialist."""
from __future__ import annotations

import hashlib
import json
import platform
import re
import subprocess
from pathlib import Path

DATA_ENV = Path(".venv-data")
CP_ENV = Path(".venv-cellprofiler")
DATA_PYTHON_VERSION = "3.12"
LOCK = Path("setup/data-requirements.lock")
CP_REQUIREMENTS = Path("setup/data-requirements-cellprofiler.txt")
IMPORT_PROBE = "import pydeseq2, gseapy, anndata, decoupler, sklearn, statsmodels, pint, uncertainties, gget, seaborn, matplotlib, pypdf, openpyxl"
# Run with the data env's own interpreter: its Python major.minor and installed distributions.
METADATA_PROBE = (
    "import json, platform, sys, importlib.metadata as m\n"
    "d = {}\n"
    "for x in m.distributions():\n"
    "    n = x.metadata['Name']\n"
    "    if n: d.setdefault(n, []).append(x.version)\n"
    "print(json.dumps({'python': '%d.%d' % sys.version_info[:2], "
    "'architecture': platform.machine(), 'distributions': d}))"
)
ARCHITECTURE_PROBE = "import platform; print(platform.machine())"
PIN = re.compile(r"([A-Za-z0-9][A-Za-z0-9._-]*)==(\S+)")


def data_python(root: Path) -> Path:
    return Path(root)/DATA_ENV/"bin/python"


def lock_sha256(root: Path) -> str:
    return hashlib.sha256((Path(root)/LOCK).read_bytes()).hexdigest()


def _cellprofiler_python(root: Path) -> str:
    first = (Path(root)/CP_REQUIREMENTS).read_text(encoding="utf-8").splitlines()[0]
    match = re.fullmatch(r"#\s*python=(\d+\.\d+)", first.strip())
    if not match:
        raise ValueError(f"{CP_REQUIREMENTS} line 1 must be '# python=3.X'")
    return match.group(1)


def normalize(name: str) -> str:
    """PEP 503 project-name normalisation."""
    return re.sub(r"[-_.]+", "-", name).lower()


def normalize_architecture(name: str) -> str:
    aliases = {"aarch64": "arm64", "amd64": "x86_64"}
    lowered = name.strip().lower()
    return aliases.get(lowered, lowered)


def interpreter_architecture(python: Path) -> str:
    result = subprocess.run([str(python), "-c", ARCHITECTURE_PROBE], check=True,
                            capture_output=True, text=True)
    return normalize_architecture(result.stdout)


def lock_pins(root: Path) -> dict[str, str]:
    pins = {}
    for number, raw in enumerate((Path(root)/LOCK).read_text(encoding="utf-8").splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        match = PIN.fullmatch(line)
        if match is None:
            raise ValueError(f"{LOCK}:{number}: unsupported lock line (expected name==version): {raw.strip()}")
        pins[normalize(match.group(1))] = match.group(2)
    return pins


def lock_errors(root: Path, python: Path) -> list[str]:
    """Compare the installed data env with DATA_PYTHON_VERSION and every lock pin."""
    pins = lock_pins(root)
    proc = subprocess.run([str(python), "-E", "-s", "-c", METADATA_PROBE], check=True,
                          capture_output=True, text=True)
    data = json.loads(proc.stdout)
    installed: dict[str, list[str]] = {}
    for name, versions in data["distributions"].items():
        installed.setdefault(normalize(name), []).extend(versions)
    errors = []
    if data["python"] != DATA_PYTHON_VERSION:
        errors.append(f"Data environment Python is {data['python']}, expected {DATA_PYTHON_VERSION}")
    actual_architecture = normalize_architecture(data.get("architecture", ""))
    expected_architecture = normalize_architecture(platform.machine())
    if not actual_architecture or actual_architecture != expected_architecture:
        errors.append(f"Data environment architecture is {actual_architecture or 'unknown'}, "
                      f"expected {expected_architecture}")
    for name, version in sorted(pins.items()):
        found = installed.get(name)
        if not found:
            errors.append(f"Data environment is missing locked package {name}=={version}")
        elif found != [version]:
            errors.append(f"Data environment has {name} {', '.join(found)}, lock requires {version}")
    return errors


def ensure(root: Path, uv: str, *, check: bool, with_cellprofiler: bool) -> dict:
    root = Path(root)
    python = data_python(root)
    result = {"passed": False, "errors": [], "python": None, "lock_sha256": lock_sha256(root), "cellprofiler": None}
    try:
        if not check:
            if not python.exists():  # uv venv refuses an existing environment
                subprocess.run([uv, "venv", str(root/DATA_ENV), "--python", DATA_PYTHON_VERSION,
                                "--managed-python"], check=True)
        expected_architecture = normalize_architecture(platform.machine())
        actual_architecture = interpreter_architecture(python)
        if actual_architecture != expected_architecture:
            raise ValueError(f"Data environment architecture is {actual_architecture or 'unknown'}, "
                             f"expected {expected_architecture}; use a native Python environment")
        if not check:
            subprocess.run([uv, "pip", "sync", "--python", str(python), str(root/LOCK)], check=True)
        subprocess.run([str(python), "-E", "-s", "-c", IMPORT_PROBE], check=True)
        result["errors"].extend(lock_errors(root, python))
        result["python"] = str(python)
        if with_cellprofiler:
            cp = root/CP_ENV
            if not check:
                if not (cp/"bin/python").exists():
                    subprocess.run([uv, "venv", str(cp), "--python", _cellprofiler_python(root),
                                    "--managed-python"], check=True)
            architecture = interpreter_architecture(cp/"bin/python")
            if architecture != expected_architecture:
                raise ValueError(f"CellProfiler environment architecture is {architecture or 'unknown'}, "
                                 f"expected {expected_architecture}; use a native Python environment")
            if not check:
                subprocess.run([uv, "pip", "install", "--python", str(cp/"bin/python"), "-r",
                                str(root/CP_REQUIREMENTS), "--build-constraints",
                                str(root/CP_REQUIREMENTS)], check=True)
            subprocess.run([str(cp/"bin/cellprofiler"), "--version"], check=True)
            result["cellprofiler"] = str(cp/"bin/cellprofiler")
    except (subprocess.CalledProcessError, OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        result["errors"].append(f"Data environment failed: {error}")
        return result
    result["passed"] = not result["errors"]
    return result
