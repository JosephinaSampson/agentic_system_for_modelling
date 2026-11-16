#!/usr/bin/env python3
"""Build a deterministic, portable BioMASS calibration job from an approved ode_mapping.

  build.py --mapping runs/orchestrator/<p>/ode_mapping.json --approval <record id> --settings <settings.json>

writes runs/orchestrator/<p>/jobs/<job-id>/{bundle.tar.gz, job.json, build.json} and prints the job dir.

The mapping must pass `ode_mapping.py check` and carry an `ode_mapping` stage_approval of exactly its
bytes. Job id = "job-" + the first 12 hex digits of sha256 over (mapping sha256, approval id, settings
sha256, export sha256, framework commit), one per line. The framework commit is the HEAD of this
checkout, suffixed "-dirty" when scripts/jobs/, scripts/ode/ or setup/biomass-calibration.lock have
uncommitted changes. The settings sha256 is the sha256 of their
canonical JSON (scripts/jobs/settings.py `digest`).

bundle.tar.gz holds one directory `<job-id>/` with:
  job.json                {"kind": "biomass-calibration", "job_id", "mapping": {path, sha256, approval},
                           "export_sha256", "settings", "framework_commit", "runtime_version" (run.RUNTIME_VERSION),
                           "files": {relpath: sha256}}
                          (`files` lists every other bundle file; the same bytes are written beside the bundle)
  model/<package>/...     the .py files of the export's Text2Model package, at their export path (BioMASS
                          2.5.x: model/generated_model/...), byte for byte; nothing else of the revision folder
  calibration/compare.py  scripts/jobs/runtime/compare.py verbatim
  calibration/spec.json   compare.resolve(mapping, rows): only the measurement rows the rules use
  run.py, README.txt, env/requirements.lock, env/bootstrap.sh, launch/{local.sh,remote.sh,slurm.sbatch}

The archive has no timestamps or owners (mtime 0, uid/gid 0, empty names, sorted entries, gzip mtime 0
and no file name), so the same inputs give the same bytes. build.json, outside the bundle, records the
build time, the host and the bundle sha256. Rebuilding an existing job returns it untouched when the
bundle is identical and refuses otherwise. The tar bytes are fully deterministic, but the gzip stream
depends on the build host's zlib: rebuilding on another machine gives the same job id and the same
file hashes in job.json, yet possibly different bundle.tar.gz bytes, which the rebuild then refuses
("exists with a different bundle"). Keep and ship the bundle that was built first.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
import platform
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts import state  # noqa: E402
from scripts.data import approvals  # noqa: E402
from scripts.data.common import project_root, reject_symlinks, sha256_file, utc_now  # noqa: E402
from scripts.jobs import settings as job_settings  # noqa: E402
from scripts.jobs.runtime import compare  # noqa: E402
from scripts.jobs.runtime.run import RUNTIME_VERSION  # noqa: E402,F401 - stamped into job.json
from scripts.ode import measurements as table, model_zip, ode_mapping  # noqa: E402

FRAMEWORK = Path(__file__).resolve().parents[2]
RUNTIME = Path(__file__).resolve().parent / "runtime"
LOCK = FRAMEWORK / "setup" / "biomass-calibration.lock"
KIND = "biomass-calibration"
# bundle path -> framework source
RUNTIME_FILES = {
    "run.py": RUNTIME / "run.py",
    "README.txt": RUNTIME / "README.txt",
    "calibration/compare.py": RUNTIME / "compare.py",
    "env/requirements.lock": LOCK,
    "env/bootstrap.sh": RUNTIME / "bootstrap.sh",
    "launch/local.sh": RUNTIME / "local.sh",
    "launch/remote.sh": RUNTIME / "remote.sh",
    "launch/slurm.sbatch": RUNTIME / "slurm.sbatch",
}
EXECUTABLE = {"run.py", "env/bootstrap.sh", "launch/local.sh", "launch/remote.sh", "launch/slurm.sbatch"}
# framework paths whose uncommitted changes mark the framework commit "-dirty" (ruling S3-ode)
DIRTY_PATHS = ("scripts/jobs", "scripts/ode", "setup/biomass-calibration.lock")
REQUIRED_MODEL = model_zip.PACKAGE_FILES  # relative to the package folder (model_zip.find_package)


def framework_commit(repo: Path = FRAMEWORK) -> str:
    """HEAD of the framework checkout, + "-dirty" when the bundled runtime paths have uncommitted changes
    (tracked or untracked, per `git status --porcelain`); "unknown" without git."""
    try:
        head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True,
                              text=True, timeout=30, check=True).stdout.strip()
        changes = subprocess.run(["git", "-C", str(repo), "status", "--porcelain", "--", *DIRTY_PATHS],
                                 capture_output=True, text=True, timeout=30, check=True).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return f"{head}-dirty" if changes else head


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def model_files(export: bytes) -> dict[str, bytes]:
    """The .py files of the export's Text2Model package (model_zip.find_package), at their zip paths;
    ValueError on any unsafe member, on zero or several packages, or on a missing package file."""
    files: dict[str, bytes] = {}
    try:
        zf = zipfile.ZipFile(io.BytesIO(export))
    except zipfile.BadZipFile as exc:
        raise ValueError(f"export is not a zip file: {exc}") from exc
    with zf:
        for info in zf.infolist():  # every member must be safe, even the ones not bundled
            name = info.filename
            parts = PurePosixPath(name).parts
            if (name.startswith("/") or "\\" in name or ".." in parts or "." in name.split("/")
                    or any(p == "" for p in name.rstrip("/").split("/"))):
                raise ValueError(f"export zip member {name!r} has an unsafe path")
        package = model_zip.find_package(zf.namelist())
        for info in zf.infolist():
            name = info.filename
            parts = PurePosixPath(name).parts
            if not name.startswith(package) or info.is_dir():
                continue
            if stat.S_ISLNK(info.external_attr >> 16):
                raise ValueError(f"export zip member {name!r} is a symlink")
            if "__pycache__" in parts or not name.endswith(".py"):
                continue
            if info.file_size > model_zip.MAX_MEMBER_BYTES:
                raise ValueError(f"export zip member {name!r} is larger than {model_zip.MAX_MEMBER_BYTES} bytes")
            files[name] = zf.read(info)
    missing = [package + rel for rel in REQUIRED_MODEL if package + rel not in files]
    if missing:
        raise ValueError(f"export zip lacks the model package files: {', '.join(missing)}")
    return files


def _rows(root: Path, mapping: dict) -> list[dict]:
    path, expected = ode_mapping.measurement_table(root, mapping["inputs"]["measurements"])
    before = sha256_file(path)
    rows = table.read(path)
    if not before == sha256_file(path) == expected:
        raise ValueError("the measurement table changed since the mapping was checked (sha256 mismatch)")
    return rows


def _job_dir_parent(root: Path, mapping_rel: str) -> Path:
    purpose = PurePosixPath(mapping_rel).parts[2]
    return root / "runs" / "orchestrator" / purpose / "jobs"


def job_id(mapping_sha: str, approval_id: str, settings_sha: str, export_sha: str, commit: str) -> str:
    return "job-" + _sha("\n".join([mapping_sha, approval_id, settings_sha, export_sha, commit]).encode())[:12]


def tar_gz(prefix: str, files: dict[str, bytes]) -> bytes:
    """Deterministic .tar.gz of files under `prefix/`: sorted, no timestamps, owners or file name."""
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tf:
        for rel in sorted(files):
            info = tarfile.TarInfo(f"{prefix}/{rel}")
            info.size = len(files[rel])
            info.mtime = 0
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            info.mode = 0o755 if rel in EXECUTABLE else 0o644
            tf.addfile(info, io.BytesIO(files[rel]))
    out = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=out, mtime=0, compresslevel=9) as gz:
        gz.write(raw.getvalue())
    return out.getvalue()


def assemble(root: Path, mapping_rel: str, approval_id: str, settings_rel: str) -> tuple[str, bytes, bytes]:
    """(job id, job.json bytes, bundle bytes) after every check; nothing is written."""
    root = Path(root)
    mapping, mapping_sha, errors = ode_mapping.load_and_check(root, mapping_rel)
    if errors:
        raise ValueError("the mapping does not pass ode_mapping check:\n  " + "\n  ".join(errors))
    approvals.require_ode_mapping_approval(root, approval_id, mapping_rel, mapping_sha)
    # canonical key order, so settings that differ only in key order or layout give identical bundles
    settings = json.loads(job_settings.canonical(job_settings.load(state.artifact(root, settings_rel))))
    model = mapping["inputs"]["model"]
    export = state.artifact(root, model["bundle"]).read_bytes()
    export_sha = _sha(export)
    if export_sha != model["sha256"]:
        raise ValueError(f"{model['bundle']} changed since the mapping was checked (sha256 mismatch)")
    files = model_files(export)
    spec = compare.resolve(mapping, _rows(root, mapping))
    files["calibration/spec.json"] = (json.dumps(spec, indent=2, sort_keys=True, allow_nan=False) + "\n").encode()
    for rel, source in RUNTIME_FILES.items():
        files[rel] = source.read_bytes()
    commit = framework_commit()
    jid = job_id(mapping_sha, approval_id, job_settings.digest(settings), export_sha, commit)
    job = {"kind": KIND, "job_id": jid,
           "mapping": {"path": mapping_rel, "sha256": mapping_sha, "approval": approval_id},
           "export_sha256": export_sha, "settings": settings, "framework_commit": commit,
           "runtime_version": RUNTIME_VERSION, "files": {rel: _sha(files[rel]) for rel in sorted(files)}}
    job_bytes = (json.dumps(job, indent=2) + "\n").encode()
    files["job.json"] = job_bytes
    return jid, job_bytes, tar_gz(jid, files)


def build(root: Path, mapping_rel: str, approval_id: str, settings_rel: str) -> Path:
    """Build (or find, when identical) the job; returns runs/orchestrator/<p>/jobs/<job-id>/."""
    root = Path(root)
    jid, job_bytes, bundle = assemble(root, mapping_rel, approval_id, settings_rel)
    parent = _job_dir_parent(root, mapping_rel)
    job_dir = parent / jid
    reject_symlinks(parent.parent, root)
    if job_dir.exists() or job_dir.is_symlink():
        reject_symlinks(job_dir, root)
        existing = job_dir / "bundle.tar.gz"
        if existing.is_file() and not existing.is_symlink() and sha256_file(existing) == _sha(bundle):
            return job_dir
        raise ValueError(f"{job_dir.relative_to(root).as_posix()} exists with a different bundle; "
                         "it is never overwritten")
    parent.mkdir(parents=True, exist_ok=True)
    reject_symlinks(parent, root)
    tmp = Path(tempfile.mkdtemp(prefix=f".{jid}-", dir=parent))
    try:
        (tmp / "bundle.tar.gz").write_bytes(bundle)
        (tmp / "job.json").write_bytes(job_bytes)
        info = {"job_id": jid, "bundle_sha256": _sha(bundle), "job_sha256": _sha(job_bytes),
                "built_at": utc_now(), "host": platform.node(), "builder_python": platform.python_version()}
        (tmp / "build.json").write_text(json.dumps(info, indent=2) + "\n", encoding="utf-8")
        os.chmod(tmp, 0o755)
        os.rename(tmp, job_dir)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return job_dir


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mapping", required=True)
    parser.add_argument("--approval", required=True, help="ode_mapping stage_approval record id")
    parser.add_argument("--settings", required=True, help="calibration settings JSON (repository-relative)")
    args = parser.parse_args(argv)
    root = project_root().resolve()
    try:
        job_dir = build(root, args.mapping, args.approval, args.settings)
    except (ValueError, OSError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    rel = job_dir.relative_to(root).as_posix()
    print(f"calibration job built: {rel} job_id={job_dir.name} "
          f"bundle_sha256={sha256_file(job_dir / 'bundle.tar.gz')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
