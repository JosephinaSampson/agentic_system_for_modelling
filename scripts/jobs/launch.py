#!/usr/bin/env python3
"""Launch a built calibration job (local mode).

  launch.py local start <job-id> [--parallel K]   unpack, then run env/bootstrap.sh and launch/local.sh detached
  launch.py local status <job-id>                 progress (JSON)
  launch.py local fetch <job-id>                  copy results.tar.gz to inputs/calibration-results/<job-id>/
  launch.py local validate <job-id> [--params FILE] [--use fit|holdout|all] [--out FILE]
                                                  bootstrap if needed, then run.py validate (foreground)

The job is runs/orchestrator/<p>/jobs/<job-id>/ (bundle.tar.gz, job.json, build.json). It is unpacked
to <XDG_CACHE_HOME or ~/.cache>/agentic-jobs/<job-id>/ after the bundle sha256 is checked against
build.json; every unpacked file is checked against job.json `files`. `fetch` only brings files into
inputs/, never overwrites, and checks that results.json echoes the job.json sha256.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.data.common import project_root, reject_symlinks, sha256_file, strict_json  # noqa: E402
from scripts.jobs.runtime import run as runtime  # noqa: E402

JOB_ID = re.compile(r"\Ajob-[0-9a-f]{12}\Z")
RESULTS = ("inputs", "calibration-results")


def _job_id(job_id: str) -> str:
    if not isinstance(job_id, str) or not JOB_ID.fullmatch(job_id):
        raise ValueError(f"invalid job id {job_id!r} (expected job-<12 hex>)")
    return job_id


def find_job(root: Path, job_id: str) -> Path:
    """runs/orchestrator/<p>/jobs/<job-id>/ (exactly one)."""
    _job_id(job_id)
    root = Path(root)
    found = sorted((root / "runs" / "orchestrator").glob(f"*/jobs/{job_id}"))
    if len(found) != 1:
        raise ValueError(f"no job {job_id} under runs/orchestrator/*/jobs/" if not found
                         else f"job {job_id} is ambiguous: {[str(p) for p in found]}")
    job_dir = found[0]
    reject_symlinks(job_dir / "bundle.tar.gz", root)
    reject_symlinks(job_dir / "job.json", root)
    return job_dir


def cache_root() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "agentic-jobs"


def _verify_unpacked(target: Path, job_bytes: bytes) -> None:
    if (target / "job.json").read_bytes() != job_bytes:
        raise ValueError(f"{target}/job.json differs from the built job")
    job = json.loads(job_bytes)
    for rel, digest in job["files"].items():
        path = target / rel
        if path.is_symlink() or not path.is_file() or sha256_file(path) != digest:
            raise ValueError(f"unpacked {rel} does not match job.json (sha256); remove {target} to unpack again")


def unpack(root: Path, job_id: str) -> Path:
    """The verified cache job folder, unpacking the bundle if it is not there yet."""
    job_dir = find_job(root, job_id)
    job_bytes = (job_dir / "job.json").read_bytes()
    build = strict_json((job_dir / "build.json").read_bytes())
    bundle = job_dir / "bundle.tar.gz"
    if sha256_file(bundle) != build.get("bundle_sha256"):
        raise ValueError(f"{bundle} does not match build.json bundle_sha256")
    base = cache_root()
    target = base / job_id
    if target.exists():
        _verify_unpacked(target, job_bytes)
        return target
    base.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=f".{job_id}-", dir=base))
    try:
        with tarfile.open(bundle, "r:gz") as tf:
            members = tf.getmembers()
            for m in members:
                parts = PurePosixPath(m.name).parts
                if (not m.isfile() or m.name.startswith("/") or ".." in parts or len(parts) < 2
                        or parts[0] != job_id):
                    raise ValueError(f"bundle member {m.name!r} is not a regular file under {job_id}/")
            if hasattr(tarfile, "data_filter"):
                tf.extractall(tmp, members=members, filter="data")
            else:  # Python < 3.12: members were checked above
                tf.extractall(tmp, members=members)
        for m in members:  # keep the bundle's modes (the data filter may drop exec bits on some versions)
            os.chmod(tmp / m.name, m.mode & 0o755)
        _verify_unpacked(tmp / job_id, job_bytes)
        os.rename(tmp / job_id, target)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return target


def _pid(target: Path) -> int | None:
    try:
        pid = int((target / "launch.pid").read_text().strip())
        os.kill(pid, 0)
        return pid
    except (OSError, ValueError):
        return None


def start(root: Path, job_id: str, parallel: int | None = None) -> dict:
    target = unpack(root, job_id)
    if _pid(target):
        raise ValueError(f"job {job_id} is already running (pid {_pid(target)})")
    if parallel is not None and parallel < 1:
        raise ValueError("--parallel must be >= 1")
    extra = f" --parallel {int(parallel)}" if parallel else ""
    command = f"bash env/bootstrap.sh && exec bash launch/local.sh{extra}"
    with (target / "launch.log").open("ab") as log:
        proc = subprocess.Popen(["bash", "-c", command], cwd=target, stdin=subprocess.DEVNULL, stdout=log,
                                stderr=subprocess.STDOUT, start_new_session=True)
    (target / "launch.pid").write_text(f"{proc.pid}\n")
    return {"job_id": job_id, "cache_dir": str(target), "pid": proc.pid, "log": str(target / "launch.log")}


def status(root: Path, job_id: str) -> dict:
    find_job(root, job_id)
    target = cache_root() / job_id
    if not target.is_dir():
        return {"job_id": job_id, "cache_dir": str(target), "unpacked": False, "launcher_running": False}
    info = runtime.status(target, write=False)
    env = target / "env_report.json"
    try:
        env_status = json.loads(env.read_text(encoding="utf-8")).get("status") if env.is_file() else None
    except ValueError:
        env_status = "unreadable"
    info.update(cache_dir=str(target), unpacked=True, launcher_running=_pid(target) is not None,
                environment=env_status)
    return info


def _environment_ok(target: Path) -> bool:
    try:
        return json.loads((target / "env_report.json").read_text(encoding="utf-8")).get("status") == "ok"
    except (OSError, ValueError, AttributeError):
        return False


def _results_json(data: bytes) -> dict:
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
        try:
            member = tf.getmember("results.json")
        except KeyError:
            raise ValueError("results.tar.gz has no results.json") from None
        handle = tf.extractfile(member)
        if handle is None:
            raise ValueError("results.json in results.tar.gz is not a file")
        return strict_json(handle.read())


def fetch(root: Path, job_id: str) -> Path:
    """Copy the cache job's results.tar.gz to inputs/calibration-results/<job-id>/ (never overwrites)."""
    root = Path(root)
    job_dir = find_job(root, job_id)
    source = cache_root() / job_id / "results.tar.gz"
    if not source.is_file():
        raise ValueError(f"no results.tar.gz in {source.parent} yet (see `launch.py local status {job_id}`)")
    data = source.read_bytes()
    try:
        results = _results_json(data)
    except (tarfile.TarError, EOFError, OSError) as exc:
        raise ValueError(f"results.tar.gz is not readable: {exc}") from exc
    if results.get("job_sha256") != sha256_file(job_dir / "job.json"):
        raise ValueError("results.json job_sha256 does not match this job's job.json")
    dest_dir = root.joinpath(*RESULTS, job_id)
    dest = dest_dir / "results.tar.gz"
    dest_dir.mkdir(parents=True, exist_ok=True)
    reject_symlinks(dest_dir, root)
    try:
        with dest.open("xb") as handle:
            handle.write(data)
    except FileExistsError:
        raise ValueError(f"{dest.relative_to(root).as_posix()} exists; fetch never overwrites") from None
    return dest


def validate(root: Path, job_id: str, params: str | None, use: str, out: str | None) -> int:
    target = unpack(root, job_id)
    if _pid(target) and not _environment_ok(target):
        # a `start` is bootstrapping: a second bootstrap would rebuild .venv under it
        raise ValueError(f"job {job_id} is running (pid {_pid(target)}) and its environment is not ready yet; "
                         "retry when `launch.py local status` shows environment ok")
    subprocess.run(["bash", "env/bootstrap.sh"], cwd=target, check=True)
    command = [str(target / ".venv" / "bin" / "python"), "run.py", "validate", "--use", use]
    if params:
        command += ["--params", str(Path(params).resolve())]
    if out:
        command += ["--out", str(Path(out).resolve())]
    return subprocess.run(command, cwd=target).returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    modes = parser.add_subparsers(dest="mode", required=True)
    local = modes.add_parser("local").add_subparsers(dest="command", required=True)
    s = local.add_parser("start")
    s.add_argument("job_id")
    s.add_argument("--parallel", type=int)
    local.add_parser("status").add_argument("job_id")
    local.add_parser("fetch").add_argument("job_id")
    v = local.add_parser("validate")
    v.add_argument("job_id")
    v.add_argument("--params", metavar="FILE")
    v.add_argument("--use", choices=runtime.USES, default="fit")
    v.add_argument("--out", metavar="FILE")
    args = parser.parse_args(argv)
    root = project_root().resolve()
    try:
        if args.command == "start":
            print(json.dumps(start(root, args.job_id, args.parallel), indent=2))
        elif args.command == "status":
            print(json.dumps(status(root, args.job_id), indent=2))
        elif args.command == "fetch":
            print(f"fetched: {fetch(root, args.job_id).relative_to(root).as_posix()}")
        else:
            return validate(root, args.job_id, args.params, args.use, args.out)
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
