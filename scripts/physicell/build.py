#!/usr/bin/env python3
"""Download a pinned PhysiCell release and compile its `template` or `template_BM` project.

  build.py --template template|template_BM [--version 1.14.2] [--dest DIR] [--verification FILE] [--jobs N]

Steps, in order (the toolchain and the verification are checked before anything is downloaded):
  toolchain  make, tar, curl (or git), python3 (template_BM only) and a compiler that builds a tiny OpenMP
             program: $PHYSICELL_CPP, then g++, then g++-NN. Apple clang alone is refused with the Homebrew fix.
  download   https://github.com/MathCancer/PhysiCell/archive/refs/tags/<version>.tar.gz extracted to
             <dest>/PhysiCell-<version>-<template>/ (default dest ~/PhysiCell); an existing folder is refused,
             nothing is overwritten. The archive sha256 is recorded (trust on first use).
  template   `make <template>` in the extracted folder (template_BM fetches MaBoSS: needs internet).
  settings   optional, --verification runs/orchestrator/<p>/physicell_verification.*.json: it must be a
             physicell_verification, schema_version 1, passed true, and its `after` capture must re-bind byte for
             byte (settings_xml.capture). The packaged PhysiCell_settings.xml and every package/config
             dependency are copied into the project's config/; a file that would be replaced is first renamed
             <name>.template-backup (an existing backup is refused).
  compile    `make -j N` (default os.cpu_count()); `project` must exist and its sha256 is recorded.

Report: runs/orchestrator/physicell-build/<UTC>-<template>/report.{json,md}. On a failed step the report is still
written (status "failed", the step and the last 40 output lines), the folder is kept and the exit status is 1.
The documented run command `./project config/PhysiCell_settings.xml` is written in the report and never executed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.data.common import project_root, reject_symlinks, sha256_file, strict_json, utc_now  # noqa: E402
from scripts.physicell import settings_xml as sx  # noqa: E402

DEFAULT_VERSION = "1.14.2"
TEMPLATES = ("template", "template_BM")
URL = "https://github.com/MathCancer/PhysiCell/archive/refs/tags/{version}.tar.gz"
RUN_COMMAND = "./project config/PhysiCell_settings.xml"
VERSION_RE = re.compile(r"[0-9A-Za-z][0-9A-Za-z._-]*\Z")
OPENMP_SOURCE = ("#include <omp.h>\n#include <cstdio>\n"
                 "int main() { int n = 0;\n#pragma omp parallel\n{\n#pragma omp atomic\nn++;\n}\n"
                 "std::printf(\"%d\\n\", n); return 0; }\n")
TAIL_LINES = 40
VERIFICATION_DIR = ("runs", "orchestrator")


class StepFailed(Exception):
    def __init__(self, step: str, message: str, tail: str = ""):
        super().__init__(message)
        self.step, self.tail = step, tail


def _which(name: str, env: dict) -> str | None:
    return shutil.which(name, path=env.get("PATH", ""))


def _tail(text: str, lines: int = TAIL_LINES) -> str:
    return "\n".join((text or "").splitlines()[-lines:])


def _run(argv: list[str], env: dict, cwd: Path | None = None, timeout: int | None = None):
    return subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)


def _output(done) -> str:
    return (done.stdout or "") + (done.stderr or "")


# ---------------------------------------------------------------- toolchain

def _candidates(env: dict) -> list[str]:
    names = [env["PHYSICELL_CPP"]] if env.get("PHYSICELL_CPP") else []
    names.append("g++")
    versioned = set()
    for folder in env.get("PATH", "").split(os.pathsep):
        try:
            versioned.update(p.name for p in Path(folder or ".").glob("g++-[0-9]*") if re.fullmatch(r"g\+\+-\d+", p.name))
        except OSError:
            continue
    names += sorted(versioned, key=lambda n: -int(n.split("-")[1]))
    return list(dict.fromkeys(names))


def _compiler(env: dict) -> tuple[str, str]:
    apple = False
    with tempfile.TemporaryDirectory(prefix="physicell-omp-") as tmp:
        source, binary = Path(tmp) / "omp.cpp", Path(tmp) / "omp"
        source.write_text(OPENMP_SOURCE, encoding="utf-8")
        for name in _candidates(env):
            path = _which(name, env)
            if path is None:
                continue
            try:
                version = _output(_run([path, "--version"], env, timeout=60))
                if "Apple clang" in version:
                    apple = True
                    continue
                built = _run([path, "-fopenmp", str(source), "-o", str(binary)], env, timeout=120)
            except (OSError, subprocess.SubprocessError):
                continue
            if built.returncode == 0:
                return name, (version.splitlines() or [""])[0].strip()
    if apple:
        raise ValueError("only Apple clang (no OpenMP) was found: run `brew install gcc` and "
                         "`export PHYSICELL_CPP=g++-NN` (NN = the installed gcc major version)")
    raise ValueError("no compiler builds an OpenMP program: install g++ (e.g. `apt install g++`) or set PHYSICELL_CPP")


def toolchain(env: dict, template: str = "template") -> dict:
    """{compiler, compiler_version, openmp, make, fetch, python3, tar}; ValueError with a one-line remedy."""
    found = {}
    for tool in ("make", "tar"):
        found[tool] = _which(tool, env)
        if found[tool] is None:
            raise ValueError(f"{tool} not found on PATH: install it (macOS: xcode-select --install; "
                             "Debian/Ubuntu: apt install build-essential)")
    curl, git = _which("curl", env), _which("git", env)
    if curl is None and git is None:
        raise ValueError("neither curl nor git found on PATH: install curl")
    python3 = _which("python3", env)
    if template == "template_BM" and python3 is None:
        raise ValueError("python3 not found on PATH: template_BM needs it to fetch MaBoSS")
    compiler, version = _compiler(env)
    return {"compiler": compiler, "compiler_version": version, "openmp": True, "make": found["make"],
            "tar": found["tar"], "fetch": "curl" if curl else "git", "python3": python3}


# ---------------------------------------------------------------- verified settings

def _verification(root: Path, given: str) -> tuple[str, dict, bytes]:
    """(repo-relative path, the verification, its bytes) for a passed physicell_verification; ValueError else."""
    path = Path(given)
    path = path if path.is_absolute() else root / path
    try:
        rel = path.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"verification {given} is outside the repository") from exc
    rel_text = rel.as_posix()
    if rel.parts[:2] != VERIFICATION_DIR or len(rel.parts) != 4 or ".." in rel.parts \
            or not re.fullmatch(r"physicell_verification\..+\.json", rel.parts[3]):
        raise ValueError("verification must be runs/orchestrator/<purpose>/physicell_verification.<session>.<capture>.json")
    reject_symlinks(path, root)
    if not path.is_file():
        raise ValueError(f"verification {rel_text} does not exist: the captured configuration is unverified")
    raw = path.read_bytes()
    data = strict_json(raw)
    if not isinstance(data, dict) or data.get("kind") != "physicell_verification" \
            or data.get("schema_version") != 1:
        raise ValueError(f"{rel_text} is not a physicell_verification with schema_version 1")
    if data.get("passed") is not True:
        raise ValueError(f"{rel_text} did not pass: only a verified capture may be copied into a project")
    after = data.get("after")
    if not isinstance(after, dict):
        raise ValueError(f"{rel_text} records no after capture")
    session, capture = after.get("session"), after.get("capture")
    if not isinstance(session, str) or not isinstance(capture, str):
        raise ValueError(f"{rel_text} records a malformed after capture")
    bound = sx.capture(root, f"runs/{sx.SPECIALIST}/{session}/configs/{capture}")
    if bound != after:
        raise ValueError(f"the captured configuration changed since {rel_text} verified it "
                         "(the after capture no longer matches the recorded binding)")
    return rel_text, data, raw


def _package_files(root: Path, after: dict) -> dict[str, bytes]:
    """The XML and every dependency of the verified capture, re-verified byte by byte: {name: bytes}."""
    parents = PurePosixPath(after["xml_path"]).parents  # <capture>/package/config, <capture>/package, <capture>
    folder = root / parents[0]
    manifest = strict_json((root / parents[2] / "capture.json").read_bytes())
    wanted = {sx.XML_NAME: after["xml_sha256"]}
    for dep in manifest["dependencies"]:
        wanted.setdefault(PurePosixPath(dep["path"]).name, dep["sha256"])
    files = {}
    for name, digest in wanted.items():
        path = folder / name
        reject_symlinks(path, root)
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != digest:
            raise ValueError(f"{name} changed while being copied (sha256 mismatch)")
        files[name] = raw
    return files


def copy_settings(root: Path, project: Path, verification_rel: str, verification: dict) -> list[dict]:
    files = _package_files(root, verification["after"])
    config = project / "config"
    config.mkdir(exist_ok=True)
    plan = []
    for name in sorted(files):
        target, backup = config / name, config / f"{name}.template-backup"
        if target.is_symlink() or backup.is_symlink():
            raise ValueError(f"config/{name} or its backup is a symlink")
        if backup.exists():
            raise ValueError(f"config/{backup.name} already exists: it is never overwritten")
        plan.append((name, target, backup))
    copied = []
    for name, target, backup in plan:
        backed_up = target.exists()
        if backed_up:
            target.rename(backup)
        target.write_bytes(files[name])
        copied.append({"name": name, "sha256": sha256_file(target),
                       "backup": f"config/{backup.name}" if backed_up else None})
    return copied


# ---------------------------------------------------------------- download and build

def download(tool: dict, env: dict, version: str, template: str, dest: Path) -> tuple[Path, str | None]:
    final = dest / f"PhysiCell-{version}-{template}"
    if final.exists() or final.is_symlink():
        raise ValueError(f"{final} already exists: it is never overwritten (choose another --dest)")
    dest.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".physicell-download-", dir=dest))
    try:
        digest = None
        archive = stage / "PhysiCell.tar.gz"
        if tool["fetch"] == "curl":
            argv = ["curl", "-fsSL", "--retry", "3", "-o", str(archive), URL.format(version=version)]
            done = _run(argv, env)
            if done.returncode != 0 or not archive.is_file():
                raise StepFailed("download", f"curl failed ({done.returncode})", _tail(_output(done)))
            digest = sha256_file(archive)
            done = _run(["tar", "-xzf", str(archive), "-C", str(stage)], env)
            if done.returncode != 0:
                raise StepFailed("download", f"tar failed ({done.returncode})", _tail(_output(done)))
        else:
            argv = ["git", "clone", "--depth", "1", "--branch", version,
                    "https://github.com/MathCancer/PhysiCell.git", str(stage / f"PhysiCell-{version}")]
            done = _run(argv, env)
            if done.returncode != 0:
                raise StepFailed("download", f"git clone failed ({done.returncode})", _tail(_output(done)))
        top = stage / f"PhysiCell-{version}"
        if not top.is_dir() or top.is_symlink():
            raise StepFailed("download", f"the archive has no top folder PhysiCell-{version}/")
        top.rename(final)
        return final, digest
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def _make(step: str, argv: list[str], project: Path, env: dict, steps: list) -> str:
    began = time.monotonic()
    done = _run(argv, env, cwd=project)
    out = _output(done)
    steps.append({"step": step, "argv": argv, "returncode": done.returncode,
                  "seconds": round(time.monotonic() - began, 1), "tail": _tail(out)})
    if done.returncode != 0:
        raise StepFailed(step, f"`{' '.join(argv)}` exited {done.returncode}", _tail(out))
    return out


def _markdown(report: dict) -> str:
    tool = report.get("toolchain") or {}
    lines = [f"# PhysiCell build: {report['template']} {report['version']} ({report['status']})", "",
             f"- started: {report['started_at']}", f"- duration: {report['duration_seconds']} s",
             f"- folder: {report.get('project_dir')}", f"- archive: {report['url']}",
             f"- archive sha256: {report.get('archive_sha256')}",
             f"- compiler: {tool.get('compiler')} ({tool.get('compiler_version')})",
             f"- project sha256: {report.get('project_sha256')}",
             f"- run command (not executed): `{report['run_command']}` from the project folder"]
    if report.get("failed_step"):
        lines += ["", f"## Failed step: {report['failed_step']}", "", report.get("error", ""), "", "```",
                  report.get("failure_tail", ""), "```"]
    settings = report.get("settings")
    if settings:
        lines += ["", "## Settings copy", "", f"- verification: {settings['verification']} (sha256 {settings['verification_sha256']})"]
        lines += [f"- {c['name']} sha256 {c['sha256']}" + (f" (backup {c['backup']})" if c["backup"] else "")
                  for c in settings["files"]]
    for step in report["steps"]:
        lines += ["", f"## {step['step']} (exit {step['returncode']}, {step['seconds']} s)", "", "```", step["tail"], "```"]
    return "\n".join(lines) + "\n"


def _write_report(root: Path, report: dict, stamp: str) -> Path:
    parent = root / "runs" / "orchestrator" / "physicell-build"
    reject_symlinks(parent, root)
    folder = parent / f"{stamp}-{report['template']}"
    reject_symlinks(folder, root)
    folder.mkdir(parents=True, exist_ok=False)
    (folder / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    (folder / "report.md").write_text(_markdown(report), encoding="utf-8")
    return folder


def build(root: Path, template: str, version: str = DEFAULT_VERSION, dest: str | Path = "~/PhysiCell",
          verification: str | None = None, jobs: int | None = None, env: dict | None = None) -> tuple[Path, dict]:
    """Returns (report folder, report). ValueError for a refusal before anything was downloaded."""
    root = Path(root).resolve()
    env = dict(os.environ if env is None else env)
    if template not in TEMPLATES:
        raise ValueError(f"template must be one of {', '.join(TEMPLATES)}")
    if not VERSION_RE.fullmatch(version):
        raise ValueError(f"unsafe --version {version!r}")
    jobs = jobs or os.cpu_count() or 1
    if jobs < 1:
        raise ValueError("--jobs must be at least 1")
    started, began = datetime.now(timezone.utc), time.monotonic()
    tool = toolchain(env, template)
    verified = _verification(root, verification) if verification else None
    dest = Path(dest).expanduser().resolve()
    final = dest / f"PhysiCell-{version}-{template}"
    if final.exists() or final.is_symlink():
        raise ValueError(f"{final} already exists: it is never overwritten (choose another --dest)")
    report = {"kind": "physicell_build", "schema_version": 1, "status": "ok", "template": template,
              "version": version, "url": URL.format(version=version), "started_at": utc_now(),
              "toolchain": tool, "jobs": jobs, "project_dir": str(final), "archive_sha256": None,
              "project_sha256": None, "steps": [], "run_command": RUN_COMMAND, "settings": None}
    make_env = dict(env, PHYSICELL_CPP=tool["compiler"])
    try:
        project, report["archive_sha256"] = download(tool, env, version, template, dest)
        _make("make " + template, ["make", template], project, make_env, report["steps"])
        if verified:
            try:
                files = copy_settings(root, project, verified[0], verified[1])
            except (ValueError, OSError) as exc:
                raise StepFailed("settings", str(exc)) from exc
            report["settings"] = {"verification": verified[0],
                                  "verification_sha256": hashlib.sha256(verified[2]).hexdigest(), "files": files}
        _make("make", ["make", "-j", str(jobs)], project, make_env, report["steps"])
        binary = project / "project"
        if not binary.is_file():
            raise StepFailed("make", "make succeeded but produced no `project` executable", report["steps"][-1]["tail"])
        report["project_sha256"] = sha256_file(binary)
    except StepFailed as exc:
        report.update(status="failed", failed_step=exc.step, error=str(exc), failure_tail=exc.tail)
    report["duration_seconds"] = round(time.monotonic() - began, 1)
    return _write_report(root, report, started.strftime("%Y%m%dT%H%M%SZ")), report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--template", required=True, choices=TEMPLATES)
    parser.add_argument("--version", default=DEFAULT_VERSION)
    parser.add_argument("--dest", default="~/PhysiCell")
    parser.add_argument("--verification", help="runs/orchestrator/<p>/physicell_verification.*.json (passed)")
    parser.add_argument("--jobs", type=int, default=None)
    args = parser.parse_args(argv)
    root = project_root().resolve()
    try:
        folder, report = build(root, args.template, args.version, args.dest, args.verification, args.jobs)
    except (ValueError, OSError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    rel = (folder / "report.json").relative_to(root).as_posix()
    if report["status"] != "ok":
        print(f"physicell build FAILED at step {report['failed_step']}: {report['error']}; report {rel}",
              file=sys.stderr)
        return 1
    print(f"physicell build ok: {report['project_dir']} project_sha256={report['project_sha256']} report {rel}")
    print(f"run (not executed): cd {report['project_dir']} && {RUN_COMMAND}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
