#!/usr/bin/env python3
"""Launch a built calibration job (local, ssh and slurm modes).

  launch.py local start <job-id> [--parallel K]   unpack, then run env/bootstrap.sh and launch/local.sh detached
  launch.py local status <job-id>                 progress (JSON)
  launch.py local fetch <job-id>                  copy results.tar.gz to inputs/calibration-results/<job-id>/
  launch.py local validate <job-id> [--params FILE [--partial]] [--nominal] [--use fit|holdout|all]
                           [--out FILE]           bootstrap if needed, then run.py validate (foreground);
                                                  --use holdout|all needs an ode_calibration approval of the
                                                  job's calibration_proposal.json
  launch.py print <job-id> --host NAME            the numbered scp/ssh commands to run by hand (runs nothing)
  launch.py ssh submit <job-id> --host NAME --authorization "<quote>"
                                                  mkdir, scp the bundle, set up, start (steps 1-4 of `print`); logs first
  launch.py ssh status|fetch|cancel <job-id> --host NAME
  launch.py slurm submit <job-id> [--resubmit [--force]]
                                                  on a SLURM login node: setup, then sbatch the array
                                                  (prints the commands when sbatch is not on PATH); --resubmit
                                                  needs every earlier array ended per sacct, --force proceeds
                                                  when sacct cannot tell (after checking squeue)

ssh mode: NAME is a host of <XDG_CONFIG_HOME or ~/.config>/agentic-modelling/hosts.json,
{"hosts": {NAME: {"ssh": alias, "remote_dir": str, "max_cores": int}}} (strict JSON, unknown keys refused).
`print` also accepts a host that is not configured (NAME = ssh alias, remote_dir ~/agentic-jobs); the
automatic `ssh` commands need a configured host. ssh/scp always run with -o BatchMode=yes (keys, never
passwords). `ssh submit` needs the researcher's authorization quote and appends
{"time","host","ssh_alias","bundle_sha256","authorization"} to <job folder>/dispatch.jsonl BEFORE running
anything. Only the bundle leaves this machine.

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
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.data.common import project_root, reject_symlinks, sha256_file, strict_json, utc_now  # noqa: E402
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
    if parallel is not None and parallel < 1:
        raise ValueError("--parallel must be >= 1")
    _, settings = _job_and_settings(root, job_id)
    target = unpack(root, job_id)
    if _pid(target):
        raise ValueError(f"job {job_id} is already running (pid {_pid(target)})")
    # local.sh applies the same rule; passing the effective value keeps the launch log honest
    extra = f" --parallel {runtime.effective_parallel(settings, parallel, os.cpu_count())}" if parallel else ""
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
    reject_symlinks(dest_dir, root)  # before mkdir: never create folders through a symlinked inputs/ path
    dest_dir.mkdir(parents=True, exist_ok=True)
    reject_symlinks(dest_dir, root)
    try:
        with dest.open("xb") as handle:
            handle.write(data)
    except FileExistsError:
        raise ValueError(f"{dest.relative_to(root).as_posix()} exists; fetch never overwrites") from None
    return dest


def require_runtime_version(job_dir: Path) -> None:
    """ValueError unless the job's job.json carries runtime_version >= run.RUNTIME_VERSION (a job built by an older
    runtime computes or accepts something else: e.g. its run.py has no validate --partial/--nominal)."""
    try:
        job = strict_json((Path(job_dir) / "job.json").read_bytes())
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        raise ValueError(f"cannot read {Path(job_dir).name}/job.json: {exc}") from exc
    version = job.get("runtime_version") if isinstance(job, dict) else None
    if type(version) is not int or version < runtime.RUNTIME_VERSION:
        raise ValueError(f"job {Path(job_dir).name} was built by an older runtime (runtime_version {version!r}, this "
                         f"framework needs {runtime.RUNTIME_VERSION}); rebuild this job with scripts/jobs/build.py "
                         "(a new job id) and run it again")


def require_holdout_approval(root: Path, job_id: str) -> str:
    """The id of an ode_calibration approval of this job's calibration_proposal.json (approvals.
    require_calibration_approval passes for it); ValueError otherwise. Hold-out predictions are shown only after
    the calibration is approved (score_holdout.py scores the hold-out once)."""
    from scripts import state
    from scripts.data import approvals
    root = Path(root)
    job_dir = find_job(root, job_id)
    proposal = job_dir / "calibration_proposal.json"
    rel = proposal.relative_to(root).as_posix()
    rule = ("hold-out predictions (--use holdout|all) are shown only after the researcher approves the calibration "
            "with an ode_calibration stage_approval of this job's calibration_proposal.json; then score the hold-out "
            "once with scripts/jobs/score_holdout.py")
    if not proposal.is_file() or proposal.is_symlink():
        raise ValueError(f"{rel} does not exist: {rule}")
    digest = sha256_file(proposal)
    decisions = root / "DECISIONS.md"
    text = decisions.read_text(encoding="utf-8") if decisions.is_file() else ""
    problems = []
    for record in state.records(text):
        artifact = record.get("artifact")
        if record.get("kind") != "stage_approval" or record.get("stage") != "ode_calibration" \
                or not isinstance(artifact, dict) or artifact.get("path") != rel:
            continue
        try:
            approvals.require_calibration_approval(root, record["id"], rel, digest)
            return record["id"]
        except (ValueError, OSError, KeyError, TypeError) as exc:
            problems.append(f"{record['id']}: {exc}")
    raise ValueError(f"no valid ode_calibration approval of {rel}" + (f" ({'; '.join(problems)})" if problems else "")
                     + f"; {rule}")


def validate(root: Path, job_id: str, params: str | None, use: str, out: str | None, *, partial: bool = False,
             nominal: bool = False) -> int:
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
    command += ["--partial"] * partial + ["--nominal"] * nominal
    return subprocess.run(command, cwd=target).returncode


# ---------------------------------------------------------------- ssh and slurm

HOST_KEYS = {"ssh", "remote_dir", "max_cores"}
SSH_ALIAS = re.compile(r"\A[A-Za-z0-9_][A-Za-z0-9_.@-]*\Z")
REMOTE_DIR = re.compile(r"\A[A-Za-z0-9_~/][A-Za-z0-9_./~-]*\Z")
DEFAULT_REMOTE_DIR = "~/agentic-jobs"
SSH_BASE = ["-o", "BatchMode=yes"]
NOTICE = ("Note: only the bundle (the model, the measurement rows used by the mapping and the parameter bounds) "
          "leaves this machine; no other inputs/ content is sent.")


def hosts_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "agentic-modelling" / "hosts.json"


def _check_host(name: str, entry) -> dict:
    if not isinstance(entry, dict):
        raise ValueError(f"hosts.json: host {name!r} must be an object")
    extra = set(entry) - HOST_KEYS
    if extra:
        raise ValueError(f"hosts.json: host {name!r} has unknown keys {sorted(extra)} (allowed: {sorted(HOST_KEYS)})")
    missing = HOST_KEYS - set(entry)
    if missing:
        raise ValueError(f"hosts.json: host {name!r} lacks {sorted(missing)}")
    if not isinstance(entry["ssh"], str) or not SSH_ALIAS.fullmatch(entry["ssh"]):
        raise ValueError(f"hosts.json: host {name!r} ssh must be an ssh alias or user@host (letters, digits, _ . @ -)")
    if not isinstance(entry["remote_dir"], str) or not REMOTE_DIR.fullmatch(entry["remote_dir"]):
        raise ValueError(f"hosts.json: host {name!r} remote_dir must be a plain path (letters, digits, _ . / ~ -)")
    cores = entry["max_cores"]
    if isinstance(cores, bool) or not isinstance(cores, int) or cores < 1:
        raise ValueError(f"hosts.json: host {name!r} max_cores must be a positive integer")
    return dict(entry)


def load_hosts() -> dict:
    path = hosts_path()
    if not path.exists():
        return {}
    data = strict_json(path.read_bytes())
    if not isinstance(data, dict) or set(data) != {"hosts"} or not isinstance(data["hosts"], dict):
        raise ValueError(f"{path}: expected {{\"hosts\": {{NAME: {{ssh, remote_dir, max_cores}}}}}} and nothing else")
    return {name: _check_host(name, entry) for name, entry in data["hosts"].items()}


def cores_parallel(settings: dict, max_cores: int) -> int:
    """K restarts at a time on a configured host: run.effective_parallel with its max_cores, i.e.
    min(parallel, max_cores // workers) for workers >= 1 and 1 for workers -1 (every core)."""
    return runtime.effective_parallel(settings, max_cores=int(max_cores))


def resolve_host(name: str, settings: dict, configured_only: bool) -> dict:
    hosts = load_hosts()
    if name in hosts:
        entry = hosts[name]
        return {"name": name, "ssh": entry["ssh"], "remote_dir": entry["remote_dir"],
                "parallel": cores_parallel(settings, entry["max_cores"])}
    if configured_only:
        known = ", ".join(sorted(hosts)) or "none"
        raise ValueError(f"host {name!r} is not in {hosts_path()} (configured: {known}); "
                         f"add it there or use `launch.py print` and run the commands yourself")
    if not SSH_ALIAS.fullmatch(name):
        raise ValueError(f"host {name!r} is not a valid ssh alias")
    return {"name": name, "ssh": name, "remote_dir": DEFAULT_REMOTE_DIR,
            "parallel": runtime.effective_parallel(settings)}


def _shown(argv: list[str]) -> str:
    """The argv as a shell line: quoted, except that a plain ~ stays readable (the remote shell expands it)."""
    return " ".join(a if re.fullmatch(r"[A-Za-z0-9_@%+=:,./~-]+", a) else shlex.quote(a) for a in argv)


def ssh_steps(job_id: str, bundle: Path, host: dict) -> dict:
    """The commands of the ssh flow as argv lists, keyed by step name."""
    rd, alias = host["remote_dir"], host["ssh"]
    remote = f"cd {rd}/{job_id} && ./launch/remote.sh"
    return {
        "mkdir": ["ssh", *SSH_BASE, alias, f"mkdir -p {rd}"],
        "upload": ["scp", *SSH_BASE, str(bundle), f"{alias}:{rd}/{job_id}.tar.gz"],
        "setup": ["ssh", *SSH_BASE, alias, f"cd {rd} && tar xzf {job_id}.tar.gz && cd {job_id} && ./launch/remote.sh setup"],
        "start": ["ssh", *SSH_BASE, alias, f"{remote} start --parallel {host['parallel']}"],
        "status": ["ssh", *SSH_BASE, alias, f"{remote} status"],
        "collect": ["ssh", *SSH_BASE, alias, f"{remote} collect"],
        "cancel": ["ssh", *SSH_BASE, alias, f"{remote} cancel"],
        "fetch": ["scp", *SSH_BASE, f"{alias}:{rd}/{job_id}/results.tar.gz",
                  f"inputs/{RESULTS[1]}/{job_id}/"],
    }


def _job_and_settings(root: Path, job_id: str) -> tuple[Path, dict]:
    job_dir = find_job(root, job_id)
    return job_dir, strict_json((job_dir / "job.json").read_bytes())["settings"]


def print_commands(root: Path, job_id: str, host_name: str) -> list[str]:
    job_dir, settings = _job_and_settings(root, job_id)
    host = resolve_host(host_name, settings, configured_only=False)
    steps = ssh_steps(job_id, (job_dir / "bundle.tar.gz").resolve(), host)
    order = ("mkdir", "upload", "setup", "start", "status", "collect", "fetch")
    lines = [f"{i}. {_shown(steps[name])}" for i, name in enumerate(order, 1)]
    # scp needs the local target folder (run from the repository root)
    lines[-1] = f"{len(order)}. mkdir -p inputs/{RESULTS[1]}/{job_id} && {_shown(steps['fetch'])}"
    return [*lines, "", NOTICE]


def _bundle_sha(job_dir: Path) -> str:
    build = strict_json((job_dir / "build.json").read_bytes())
    digest = sha256_file(job_dir / "bundle.tar.gz")
    if digest != build.get("bundle_sha256"):
        raise ValueError(f"{job_dir / 'bundle.tar.gz'} does not match build.json bundle_sha256")
    return digest


def log_dispatch(job_dir: Path, host: dict, bundle_sha256: str, authorization: str) -> None:
    entry = {"time": utc_now(), "host": host["name"], "ssh_alias": host["ssh"], "bundle_sha256": bundle_sha256,
             "authorization": authorization}
    path = job_dir / "dispatch.jsonl"
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError(f"{path} must be a regular file")
    created = not path.exists()
    with path.open("a", encoding="utf-8") as log:
        log.write(json.dumps(entry, sort_keys=True) + "\n")
        log.flush()
        os.fsync(log.fileno())
    if created:  # make the new directory entry durable too
        fd = os.open(job_dir, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _run(argv: list[str]) -> int:
    print(f"$ {_shown(argv)}", flush=True)
    return subprocess.run(argv, stdin=subprocess.DEVNULL).returncode


def ssh_submit(root: Path, job_id: str, host_name: str, authorization: str) -> int:
    if not isinstance(authorization, str) or not authorization.strip():
        raise ValueError("ssh submit needs --authorization \"<the researcher's words authorizing this host>\"")
    job_dir, settings = _job_and_settings(root, job_id)
    host = resolve_host(host_name, settings, configured_only=True)
    digest = _bundle_sha(job_dir)
    log_dispatch(job_dir, host, digest, authorization.strip())  # before anything leaves this machine
    print(NOTICE, flush=True)
    steps = ssh_steps(job_id, (job_dir / "bundle.tar.gz").resolve(), host)
    for name in ("mkdir", "upload", "setup", "start"):
        code = _run(steps[name])
        if code != 0:
            print(f"error: step '{name}' failed (exit {code}); later steps were not run", file=sys.stderr)
            return 1
    return 0


def ssh_simple(root: Path, job_id: str, host_name: str, command: str) -> int:
    job_dir, settings = _job_and_settings(root, job_id)
    host = resolve_host(host_name, settings, configured_only=True)
    return _run(ssh_steps(job_id, (job_dir / "bundle.tar.gz").resolve(), host)[command])


def ssh_fetch(root: Path, job_id: str, host_name: str) -> Path:
    """scp results.tar.gz from the host and place it in inputs/calibration-results/<job-id>/ (never overwrites)."""
    root = Path(root)
    job_dir, settings = _job_and_settings(root, job_id)
    host = resolve_host(host_name, settings, configured_only=True)
    dest_dir = root.joinpath(*RESULTS, job_id)
    dest = dest_dir / "results.tar.gz"
    reject_symlinks(dest_dir, root)
    if dest.exists():
        raise ValueError(f"{dest.relative_to(root).as_posix()} exists; fetch never overwrites")
    argv = ssh_steps(job_id, job_dir / "bundle.tar.gz", host)["fetch"]
    base = cache_root()
    base.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=f".fetch-{job_id}-", dir=base))
    try:
        got = tmp / "results.tar.gz"
        code = _run([*argv[:-1], str(got)])
        if code != 0 or not got.is_file():
            raise ValueError(f"scp failed (exit {code}); nothing was fetched")
        data = got.read_bytes()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    try:
        results = _results_json(data)
    except (tarfile.TarError, EOFError, OSError) as exc:
        raise ValueError(f"results.tar.gz is not readable: {exc}") from exc
    if results.get("job_sha256") != sha256_file(job_dir / "job.json"):
        raise ValueError("results.json job_sha256 does not match this job's job.json")
    dest_dir.mkdir(parents=True, exist_ok=True)
    reject_symlinks(dest_dir, root)
    try:
        with dest.open("xb") as handle:
            handle.write(data)
    except FileExistsError:
        raise ValueError(f"{dest.relative_to(root).as_posix()} exists; fetch never overwrites") from None
    return dest


def _umask() -> int:
    mask = os.umask(0)
    os.umask(mask)
    return mask


def slurm_setup(target: Path) -> None:
    subprocess.run(["bash", "launch/remote.sh", "setup"], cwd=target, check=True)


def slurm_submit(root: Path, job_id: str, resubmit: bool = False, force: bool = False) -> int:
    """sbatch the job's array. --resubmit runs the unfinished restarts again once every earlier array has ended
    per sacct; `force` (with resubmit) proceeds when sacct cannot tell (no accounting on the cluster), after the
    researcher has checked with squeue that no earlier array is still queued or running. An array sacct reports
    as active is never resubmitted."""
    if force and not resubmit:
        raise ValueError("--force only applies to --resubmit (when sacct cannot tell whether the earlier array ended)")
    job_dir, settings = _job_and_settings(root, job_id)
    sbatch = ["sbatch", f"--array=1-{settings['restarts']}", f"--time={settings['slurm']['time']}",
              f"--cpus-per-task={settings['slurm']['cpus_per_task']}", "launch/slurm.sbatch"]
    if not shutil.which("sbatch"):
        print("sbatch is not on PATH; on the SLURM login node, after unpacking the bundle "
              f"(tar xzf {job_id}.tar.gz), run:")
        print(f"cd {job_id}")
        print("bash launch/remote.sh setup")
        print("mkdir -p out state")
        print(_shown(sbatch))
        print("echo <array job id printed by sbatch> > state/slurm_job   # lets `remote.sh status|cancel` follow it")
        return 0
    target = unpack(root, job_id)
    record = target / "state" / "slurm_job"
    earlier = record.read_text(encoding="utf-8").split() if record.exists() else []
    if record.exists() and not resubmit:
        raise ValueError(f"job {job_id} was already submitted (array job {earlier[-1] if earlier else 'unknown'}); "
                         "cancel it with `bash launch/remote.sh cancel`, or pass --resubmit to run the "
                         "unfinished restarts again")
    for array in earlier:  # a resubmit while an earlier array runs would start the same restarts twice
        active = runtime.slurm_active(array)
        if active is True:
            raise ValueError(f"array job {array} is still active per sacct; wait for it or cancel it "
                             "(`bash launch/remote.sh cancel`) before --resubmit")
        if active is None and not force:
            raise ValueError(f"sacct cannot tell whether array job {array} has ended (no accounting, or sacct failed); "
                             f"check with `squeue -j {array}` that nothing of it is queued or running (cancel it with "
                             "`bash launch/remote.sh cancel` otherwise), then pass --resubmit --force")
        if active is None:
            print(f"warning: --force: sacct cannot tell whether array job {array} has ended; resubmitting on the "
                  "researcher's word that squeue shows none of it", file=sys.stderr)
    slurm_setup(target)
    (target / "out").mkdir(exist_ok=True)  # sbatch does not create the --output folder
    (target / "state").mkdir(exist_ok=True)
    done = subprocess.run(sbatch, cwd=target, capture_output=True, text=True)
    sys.stdout.write(done.stdout)
    sys.stderr.write(done.stderr)
    if done.returncode != 0:
        raise ValueError(f"sbatch failed (exit {done.returncode})")
    match = re.search(r"(\d+)", done.stdout)
    if not match:
        raise ValueError(f"could not read the array job id from sbatch output {done.stdout!r}; "
                         f"write it to {record} by hand")
    # one id per line (cancel stops every one); written atomically (temp file + rename)
    text = "".join(f"{array}\n" for array in [*earlier, match.group(1)])
    fd, tmp = tempfile.mkstemp(prefix=".slurm_job.", dir=record.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(tmp, 0o666 & ~_umask())  # mkstemp makes 0600; collaborators read the record (status, cancel)
        os.replace(tmp, record)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    print(f"recorded array job {match.group(1)} in {record}")
    return 0


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
    v.add_argument("--partial", action="store_true", help="--params gives some fitted names; the rest keep the "
                   "model defaults")
    v.add_argument("--nominal", action="store_true", help="also report the model defaults of the fitted names")
    p = modes.add_parser("print")
    p.add_argument("job_id")
    p.add_argument("--host", required=True, metavar="NAME")
    ssh = modes.add_parser("ssh").add_subparsers(dest="command", required=True)
    for name in ("submit", "status", "fetch", "cancel"):
        c = ssh.add_parser(name)
        c.add_argument("job_id")
        c.add_argument("--host", required=True, metavar="NAME")
        if name == "submit":
            c.add_argument("--authorization", default="", metavar="QUOTE")
    slurm = modes.add_parser("slurm").add_subparsers(dest="command", required=True)
    sub = slurm.add_parser("submit")
    sub.add_argument("job_id")
    sub.add_argument("--resubmit", action="store_true",
                     help="run the unfinished restarts again once every earlier array has ended per sacct")
    sub.add_argument("--force", action="store_true",
                     help="with --resubmit: proceed when sacct cannot tell (check squeue first)")
    args = parser.parse_args(argv)
    root = project_root().resolve()
    try:
        if args.mode == "print":
            print("\n".join(print_commands(root, args.job_id, args.host)))
        elif args.mode == "ssh":
            if args.command == "submit":
                return ssh_submit(root, args.job_id, args.host, args.authorization)
            if args.command == "fetch":
                print(f"fetched: {ssh_fetch(root, args.job_id, args.host).relative_to(root).as_posix()}")
            else:
                return ssh_simple(root, args.job_id, args.host, args.command)
        elif args.mode == "slurm":
            return slurm_submit(root, args.job_id, args.resubmit, args.force)
        elif args.command == "start":
            print(json.dumps(start(root, args.job_id, args.parallel), indent=2))
        elif args.command == "status":
            print(json.dumps(status(root, args.job_id), indent=2))
        elif args.command == "fetch":
            print(f"fetched: {fetch(root, args.job_id).relative_to(root).as_posix()}")
        else:
            if args.use != "fit":  # the hold-out stays unseen until the calibration is approved
                require_holdout_approval(root, args.job_id)
            return validate(root, args.job_id, args.params, args.use, args.out, partial=args.partial,
                            nominal=args.nominal)
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
