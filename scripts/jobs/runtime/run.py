#!/usr/bin/env python3
"""Run a BioMASS calibration job from inside its bundle folder.

  python run.py selftest
  python run.py validate [--params FILE [--partial]] [--nominal] [--use fit|holdout|all] [--out FILE]
  python run.py calibrate --restart N [--takeover]
  python run.py collect
  python run.py status
  python run.py parallel [--requested K]   restarts to run at a time here (effective_parallel, os.cpu_count())
  python run.py env-report --uv-version V --uv-platform T --uv-sha256 S --selftest-exit CODE

Layout (the bundle root is this file's folder): job.json, model/<package>/ (the exported BioMASS Text2Model
package at its export path, model/generated_model/ for BioMASS 2.5.x exports), calibration/{compare.py,
spec.json}, env/, launch/. Outputs go to out/ and results.tar.gz.

How a parameter vector is scored (no biomass.optimize, no generated data files):
  * the model package (the single folder under model/ holding ode.py and name2idx/, see find_package) is
    imported by its own name with its parent folder on sys.path (its modules import each other relatively);
  * every simulated condition starts from the model defaults (`param_values()`, `initial_values()`),
    then the fitted values (fit kind parameter -> x, initial -> y0), then the condition's absolute
    overrides from compare.condition_overrides, then its `scales` (multiplying the current value);
  * the model's own Observable is driven with conditions=["__mapped__"] (so none of its named
    condition branches fire), t = the exact model-unit times of spec["times_model"][condition]
    (sorted floats including 0.0; biomass 0.14.0 solve_ode passes t to solve_ivp as t_span/t_eval,
    so float times are used as is; a single-point grid [0.0] is padded with 1.0, which is then
    dropped) and simulations = an empty (n_obs, 1, n_t) array; simulate(x, y0) returning False or
    raising is a solver failure (that condition is then missing, so compare gives PENALTY);
  * only the simulations the scored rows need are run, each only up to the times those rows need
    (compare.needed(spec, use)): calibration never simulates a hold-out-only condition, and a hold-out time
    never extends the integration of a condition shared with fit rows;
  * predictions and the objective come from calibration/compare.py (unweighted SSE over use=fit rows,
    PENALTY 1e12 on any failure).

Hold-out rows stay unseen until the hold-out is scored: `collect` writes predictions, simulations and figures
for use=fit rows (and fit-needed times) only, and `validate --use U` reports only the rows of use U.

A restart folder is deleted (and the restart run again) only when its owner (owner.json: host, pid, process start
identity, and the SLURM job/task/restart count/procid when run under SLURM) is gone (_owner_state: this pid, a
dead or reused pid, an ended SLURM task, or this task's own requeue), or with `calibrate --takeover` when it cannot
be checked from here. Every claimant holds out/.claim-restart-N/ from judging to placing the folder (cmd_calibrate). An interrupted restart (Ctrl-C, SIGTERM, SystemExit)
writes no result.json, so a rerun starts it again from its seed; only real errors and the selftest cap write
a `failed` result. Restarts run K at a time with K = effective_parallel (workers -1: 1; otherwise
min(parallel, cores // workers), locally cores = os.cpu_count()).

Calibration follows BioMASS 0.14.0: scipy differential_evolution in gene space [0, 1]^n with the
optimizer_options of job.json, gene -> value = 10 ** (log10(lb) + g * (log10(ub) - log10(lb))),
seed = the restart's seed, and an initial population drawn like biomass InitialPopulation (uniform
random genes, redrawn until the objective is finite and < 1e12) of popsize * n members (at least 5,
the minimum scipy accepts), from numpy default_rng(seed).

Only the standard library is imported at module level; numpy, scipy, biomass and matplotlib are
imported where they are needed, so the repository can test the pure functions.
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import hashlib
import importlib
import importlib.util
import io
import json
import math
import os
import platform
import shutil
import signal
import sys
import socket
import tarfile
import tempfile
import time
import traceback
import warnings
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = "out"
SELFTEST_CAP_S = 300
INIT_THRESHOLD = 1e12
INIT_MAX_DRAWS = 10000  # per population member
MIN_POPULATION = 5  # scipy differential_evolution refuses an init array with fewer members
USES = ("fit", "holdout", "all")
# The bundle's runtime/compare version, stamped into job.json as "runtime_version" by build.py. Bump it whenever
# run.py or compare.py change what a bundle computes or accepts; import_results.py and score_holdout.py refuse a
# job with an older or missing stamp ("rebuild this job"). 2: validate --partial/--nominal, fit-scale
# max_normalized, divisor rows by use, "trivial" rows.
RUNTIME_VERSION = 2


# ---------------------------------------------------------------- pure helpers (stdlib)

def model_times(spec: dict, condition: str) -> list[float]:
    """The exact model-unit times to simulate for a condition: sorted floats, always including 0.0."""
    return sorted({0.0, *(float(t) for t in spec["times_model"][condition])})


def gene2val(fit: list[dict], genes) -> dict:
    """BioMASS gene2val: {fit name: 10 ** (log10(lb) + g * (log10(ub) - log10(lb)))}."""
    return {f["name"]: 10 ** (f["log10_lower"] + float(g) * (f["log10_upper"] - f["log10_lower"]))
            for f, g in zip(fit, genes)}


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: Path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path: Path, data) -> None:
    """Atomic write; a unique temp file per writer, so concurrent restarts never collide."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, indent=2, allow_nan=False) + "\n"
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _finite_or_none(value):
    return value if isinstance(value, (int, float)) and math.isfinite(value) else None


def find_package(root: Path = ROOT) -> Path:
    """The single Text2Model package folder under root/model (a folder with ode.py and name2idx/, the
    folder model/ itself included); ValueError on zero or several."""
    base = Path(root) / "model"
    found = []
    for dirpath, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        if "ode.py" in files and "name2idx" in dirs:
            found.append(Path(dirpath))
    if not found:
        raise ValueError("no Text2Model package under model/ (a folder with ode.py and name2idx/)")
    if len(found) > 1:
        raise ValueError(f"several Text2Model packages under model/: {', '.join(sorted(map(str, found)))}")
    return found[0]


def load_compare(root: Path = ROOT):
    """calibration/compare.py of the bundle (or the sibling compare.py in the framework repository)."""
    for path in (root / "calibration" / "compare.py", root / "compare.py"):
        if path.is_file():
            spec = importlib.util.spec_from_file_location("compare", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
    raise FileNotFoundError("calibration/compare.py is missing")


SLURM_ENDED = {"COMPLETED", "FAILED", "CANCELLED", "TIMEOUT", "OUT_OF_MEMORY", "NODE_FAIL", "PREEMPTED",
               "BOOT_FAIL", "DEADLINE"}


def _slurm_ids() -> tuple[str, str] | None:
    """(array job id, task id) of this process when it runs as a SLURM array task (else the plain job id, "")."""
    job = os.environ.get("SLURM_ARRAY_JOB_ID") or os.environ.get("SLURM_JOB_ID")
    if not job:
        return None
    return job, os.environ.get("SLURM_ARRAY_TASK_ID", "") if os.environ.get("SLURM_ARRAY_JOB_ID") else ""


def slurm_active(job: str, task: str = "") -> bool | None:
    """True/False when sacct reports the job (array task) as still active/ended; None when it cannot tell."""
    import re
    import subprocess
    target = f"{job}_{task}" if task else job
    if not shutil.which("sacct"):
        return None
    try:
        done = subprocess.run(["sacct", "-j", target, "--format=JobID,State", "--noheader", "-P", "-X"],
                              capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    states = []
    for line in done.stdout.splitlines() if done.returncode == 0 else []:
        jid, _, state = line.partition("|")
        if re.fullmatch(re.escape(job) + (r"_" + re.escape(task) if task else r"(_.*)?"), jid.strip()):
            states.append(state.split()[0].upper() if state.split() else "")
    if not states:
        return None
    return any(state not in SLURM_ENDED for state in states)


def _boot_id() -> str | None:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def _process_start(pid: int) -> tuple[str | None, float] | None:
    """(identity or None, start time in epoch seconds) of a live process, or None when it cannot be read.

    Linux: /proc/<pid>/stat field 22 (start, in clock ticks since boot); identity = "linux:<boot id>:<ticks>"
    (exact, immune to wall-clock jumps; None without a boot id: btime moves with the wall clock), time = /proc/stat
    btime + ticks / CLK_TCK. Elsewhere: `ps -o lstart= -p PID`, identity "ps:<lstart>". Identities are compared
    only within one scheme (_owner_state)."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        ticks = int(stat[stat.rindex(")") + 2:].split()[19])
        btime = next(int(line.split()[1]) for line in Path("/proc/stat").read_text(encoding="utf-8").splitlines()
                     if line.startswith("btime "))
        boot = _boot_id()
        return (f"linux:{boot}:{ticks}" if boot else None), btime + ticks / os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, IndexError, StopIteration):
        pass
    import subprocess
    try:
        done = subprocess.run(["ps", "-o", "lstart=", "-p", str(int(pid))], capture_output=True, text=True,
                              timeout=10, env={**os.environ, "LC_ALL": "C"})
        text = " ".join(done.stdout.split())
        if done.returncode == 0 and text:
            return f"ps:{text}", time.mktime(time.strptime(text, "%a %b %d %H:%M:%S %Y"))
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return None


def _scheme(identity) -> str | None:
    return identity.split(":", 1)[0] if isinstance(identity, str) and ":" in identity else None


PID_START_SLACK_S = 2.0  # started_at is truncated to seconds and btime is whole seconds


def _epoch(text) -> float | None:
    try:
        stamp = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None
    return stamp.timestamp() if stamp.tzinfo is not None else None


def _own_requeue(owner: dict) -> bool:
    """This process is SLURM's requeue of the owner's own task: the same job and the same non-empty array task
    (a non-array job: the same SLURM_PROCID), with SLURM_RESTART_COUNT >= 1 and above the owner's recorded count.
    Another node of the same allocation (`srun -N2`, a shared `salloc`) has the same job id but is not a requeue."""
    ids = _slurm_ids()
    if ids is None or ids[0] != owner.get("slurm_job"):
        return False
    count = os.environ.get("SLURM_RESTART_COUNT", "")
    if not count.isdigit() or int(count) < 1:
        return False
    recorded = owner.get("slurm_restart")
    if isinstance(recorded, str) and recorded.isdigit() and int(recorded) >= int(count):
        return False
    task = ids[1]
    if task:
        return owner.get("slurm_task") == task
    procid = os.environ.get("SLURM_PROCID", "")
    return owner.get("slurm_task", "") == "" and bool(procid) and owner.get("slurm_procid") == procid


def _owner_state(folder: Path, claiming: bool = False) -> str:
    """"alive", "stale" or "unknown" for a restart folder without result.json, from its owner.json.

    `claiming`: cmd_calibrate, before it takes the folder over. Same host: stale when the pid is gone, reused (its
    process started after owner.json was written: the recorded process_start identity of the same scheme differs
    (identities of different schemes are not compared: alive), or, for an older
    owner.json without it, its start time is later than started_at) or, when claiming, this very process (it has
    not created the folder yet); otherwise alive. Another host (a SLURM compute node: it cannot be checked from
    here): unknown, unless claiming and the owner recorded its SLURM job: this task's own requeue (_own_requeue)
    is stale, otherwise sacct decides (ended: stale, active: alive, cannot tell: unknown)."""
    try:
        owner = read_json(folder / "owner.json")
        host, pid = owner["host"], int(owner["pid"])
    except (OSError, ValueError, KeyError, TypeError):
        return "stale"
    if host != socket.gethostname():
        job, task = owner.get("slurm_job"), owner.get("slurm_task", "")
        if not claiming or not isinstance(job, str) or not job:
            return "unknown"
        if _own_requeue(owner):
            return "stale"  # this task's own earlier attempt (a requeue): not a live owner
        active = slurm_active(job, task if isinstance(task, str) else "")
        return "unknown" if active is None else ("alive" if active else "stale")
    if claiming and pid == os.getpid():
        return "stale"
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return "stale"
    except PermissionError:
        pass  # a process of another user: exists
    except (OverflowError, ValueError):
        return "stale"
    current = _process_start(pid)
    if current is not None:
        recorded = owner.get("process_start")
        if isinstance(recorded, str) and recorded:
            if current[0] is not None and _scheme(recorded) == _scheme(current[0]):
                return "alive" if recorded == current[0] else "stale"  # another process now has that pid
            return "alive"  # identities of different schemes (or none now) prove nothing
        started = _epoch(owner.get("started_at"))
        if started is not None and current[1] > started + PID_START_SLACK_S:
            return "stale"  # the process with that pid started after the owner wrote owner.json
    return "alive"


def _owner_alive(folder: Path, claiming: bool = False) -> bool:
    """A restart folder without result.json is running unless its owner is known to be gone (_owner_state)."""
    return _owner_state(folder, claiming) != "stale"


def status(root: Path = ROOT, write: bool = True) -> dict:
    """Progress of the restarts from out/restart-N/ (stdlib; also used by launch.py).

    done = result.json written (ok or failed); running = no result yet and its owner process is alive
    (or on another host); stale = no result and no live owner (killed or crashed: rerun it); pending = not started.
    """
    job = read_json(root / "job.json")
    total = job["settings"]["restarts"]
    ok, failed, running, stale, pending = [], [], [], [], []
    for n in range(1, total + 1):
        folder = root / OUT / f"restart-{n}"
        result = folder / "result.json"
        if result.is_file():
            try:
                state = read_json(result).get("status")
            except ValueError:
                state = "failed"
            (ok if state == "ok" else failed).append(n)
        elif folder.is_dir():
            (running if _owner_alive(folder) else stale).append(n)
        else:
            pending.append(n)
    info = {"job_id": job["job_id"], "updated_at": now(), "restarts": total, "ok": len(ok), "failed": len(failed),
            "done": sorted(ok + failed), "running": running, "stale": stale, "pending": pending,
            "results": (root / "results.tar.gz").is_file()}
    if write and (root / OUT).is_dir():
        write_json(root / OUT / "status.json", info)
    return info


# ---------------------------------------------------------------- model and objective

_LOADED: dict = {}  # (bundle root, spec.json mtime_ns, size) -> loaded Problem state, once per process (pool
# workers unpickle per chunk); a rebuilt spec.json at the same root is loaded again


def clear_cache() -> None:
    """Forget every loaded Problem state of this process."""
    _LOADED.clear()


class Problem:
    """The calibration problem of a bundle: simulate, predict and score fitted values.

    Picklable (scipy workers != 1): only the bundle root is pickled; the loaded state (spec, compare, model,
    Observable) is cached per process in _LOADED, so a worker loads it once, not once per pool chunk.
    """

    def __init__(self, root: Path = ROOT):
        self.root = Path(root)
        self._ready = False

    def __getstate__(self):
        return {"root": str(self.root)}

    def __setstate__(self, data):
        self.__init__(Path(data["root"]))

    def _load(self) -> None:
        if self._ready:
            return
        info = (self.root / "calibration" / "spec.json").stat()
        key = (str(self.root), info.st_mtime_ns, info.st_size)
        state = _LOADED.get(key)
        if state is None:
            for stale in [k for k in _LOADED if k[0] == key[0]]:
                del _LOADED[stale]
            state = _LOADED[key] = self._load_state()
        self.__dict__.update(state)
        self._ready = True

    def _load_state(self) -> dict:
        import numpy as np
        self.np = np
        self.spec = read_json(self.root / "calibration" / "spec.json")
        self.compare = load_compare(self.root)
        package = find_package(self.root)
        if str(package.parent) not in sys.path:
            sys.path.insert(0, str(package.parent))
        importlib.import_module(package.name)
        ode = importlib.import_module(f"{package.name}.ode")
        names = importlib.import_module(f"{package.name}.name2idx")
        observable = importlib.import_module(f"{package.name}.observable")
        self.p_index = {name: i for i, name in enumerate(names.C.NAMES)}
        self.s_index = {name: i for i, name in enumerate(names.V.NAMES)}
        self.x_default = list(ode.param_values())
        self.y_default = list(ode.initial_values())
        self.obs = observable.Observable()
        self.obs_names = list(self.obs.obs_names)
        self.fit = self.spec["fit"]
        for f in self.fit:
            index = self.p_index if f["kind"] == "parameter" else self.s_index
            if f["name"] not in index:
                raise ValueError(f"fitted {f['kind']} {f['name']!r} is not in the model")
        self.names = list(self.spec["times_model"])
        self.needed = {use: self.compare.needed(self.spec, use) for use in USES}
        return {key: getattr(self, key) for key in ("np", "spec", "compare", "p_index", "s_index", "x_default",
                                                    "y_default", "obs", "obs_names", "fit", "names", "needed")}

    # -- values
    def nominal(self) -> dict:
        self._load()
        return {f["name"]: float(self.x_default[self.p_index[f["name"]]] if f["kind"] == "parameter"
                                 else self.y_default[self.s_index[f["name"]]]) for f in self.fit}

    def to_values(self, genes) -> dict:
        self._load()
        return gene2val(self.fit, genes)

    # -- simulation
    def simulate(self, name: str, fitted: dict, times=None) -> dict | None:
        """{observable: {time: value}} for a condition at `times` (default: every time of spec["times_model"]),
        or None on a solver failure. biomass integrates over (times[0], times[-1]), so evaluate passes only the
        times its rows need: a later hold-out time never extends (or fails) a fit simulation."""
        self._load()
        np = self.np
        overrides = self.compare.condition_overrides(self.spec, name, fitted)
        x, y0 = list(self.x_default), list(self.y_default)
        for f in self.fit:
            if f["kind"] == "parameter":
                x[self.p_index[f["name"]]] = fitted[f["name"]]
            else:
                y0[self.s_index[f["name"]]] = fitted[f["name"]]
        for key, value in overrides["parameters"].items():
            x[self.p_index[key]] = value
        for key, value in overrides["initials"].items():
            y0[self.s_index[key]] = value
        for key, factor in overrides["scales"].items():
            x[self.p_index[key]] *= factor
        if not all(math.isfinite(v) for v in (*x, *y0)):
            return None
        times = sorted({0.0, *(float(t) for t in times)}) if times is not None else model_times(self.spec, name)
        grid = times if len(times) > 1 else [times[0], times[0] + 1.0]
        obs = self.obs
        obs.conditions = ["__mapped__"]
        obs.t = list(grid)
        obs.simulations = np.empty((len(self.obs_names), 1, len(grid)))
        obs.simulations.fill(np.nan)
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                ok = obs.simulate(list(x), list(y0))
        except Exception:  # noqa: BLE001 - any failure inside the model is a solver failure
            return None
        if ok is False:
            return None
        return {obs_name: {t: float(obs.simulations[i, 0, j]) for j, t in enumerate(times)}
                for i, obs_name in enumerate(self.obs_names)}

    def simulate_all(self, fitted: dict, times: dict | None = None) -> dict:
        """Simulate every condition of `times` ({name: model times}; default every spec condition and time)."""
        sims = {}
        for name in self.names if times is None else [n for n in self.names if n in times]:
            result = self.simulate(name, fitted) if times is None else self.simulate(name, fitted, times[name])
            if result is not None:
                sims[name] = result
        return sims

    def evaluate(self, fitted: dict, use: str = "fit") -> tuple[float, dict, dict]:
        """(score, predictions, sims); use "all" sums the fit and hold-out scores (PENALTY if either is)."""
        self._load()
        needed = getattr(self, "needed", None) or {u: self.compare.needed(self.spec, u) for u in USES}
        sims = self.simulate_all(fitted, needed[use])  # the conditions AND times of this use only
        predictions = self.compare.predict(self.spec, sims)
        if use == "all":
            scores = [self.compare.objective(self.spec, predictions, use=u) for u in ("fit", "holdout")]
            score = self.compare.PENALTY if self.compare.PENALTY in scores else sum(scores)
        else:
            score = self.compare.objective(self.spec, predictions, use=use)
        return score, predictions, sims

    def __call__(self, genes) -> float:
        try:
            return float(self.evaluate(self.to_values(genes))[0])
        except Exception:  # noqa: BLE001 - the objective never raises
            return 1e12


def initial_population(problem: Problem, n_members: int, rng, deadline: float | None = None):
    """biomass InitialPopulation: random genes redrawn until the objective is finite and < 1e12."""
    import numpy as np
    problem._load()
    n = len(problem.fit)
    population = []
    for member in range(n_members):
        for _ in range(INIT_MAX_DRAWS):
            if deadline is not None and time.monotonic() > deadline:
                raise TimeoutError("time cap reached while drawing the initial population")
            genes = rng.random(n)
            value = problem(genes)
            if math.isfinite(value) and value < INIT_THRESHOLD:
                population.append(genes)
                break
        else:
            raise RuntimeError(f"no solvable initial member {member + 1} after {INIT_MAX_DRAWS} draws")
    return np.array(population)


# ---------------------------------------------------------------- calibrate

class _Log:
    def __init__(self, path: Path):
        self.handle = path.open("a", encoding="utf-8")

    def __call__(self, message: str) -> None:
        self.handle.write(f"{now()} {message}\n")
        self.handle.flush()

    def close(self) -> None:
        self.handle.close()


def run_restart(root: Path, restart: int, seed: int, options: dict, workers: int, out_dir: Path,
                cap_s: float | None = None) -> dict:
    """One differential-evolution run; writes out_dir/{result.json, trace.csv, log.txt}."""
    out_dir.mkdir(parents=True, exist_ok=True)
    log = _Log(out_dir / "log.txt")
    start = time.monotonic()
    deadline = start + cap_s if cap_s else None
    result = {"restart": restart, "seed": seed, "score": None, "status": "failed", "params": {},
              "wall_time_s": 0.0}
    record = True  # False for an interrupt: no result.json, so the restart is rerun from its seed
    trace_path = out_dir / "trace.csv"
    try:
        import numpy as np
        from scipy.optimize import differential_evolution
        problem = Problem(root)
        problem._load()
        n = len(problem.fit)
        log(f"restart {restart} seed {seed} options {json.dumps(options, sort_keys=True)} workers {workers}")
        size = max(MIN_POPULATION, options["popsize"] * n)
        init = initial_population(problem, size, np.random.default_rng(seed), deadline)
        log(f"initial population: {size} solvable members")
        with trace_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["generation", "best_score", "elapsed_s"])
            generation = [0]

            def callback(intermediate_result):
                generation[0] += 1
                elapsed = time.monotonic() - start
                writer.writerow([generation[0], repr(float(intermediate_result.fun)), f"{elapsed:.3f}"])
                handle.flush()
                log(f"generation {generation[0]}: best {float(intermediate_result.fun)!r}")
                return deadline is not None and time.monotonic() > deadline

            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                res = differential_evolution(
                    problem, [(0.0, 1.0)] * n, strategy=options["strategy"], maxiter=options["maxiter"],
                    popsize=options["popsize"], tol=options["tol"], mutation=options["mutation"],
                    recombination=options["recombination"], polish=options["polish"], seed=seed, init=init,
                    workers=workers, updating="immediate" if workers == 1 else "deferred", callback=callback)
        if deadline is not None and time.monotonic() > deadline:
            raise TimeoutError(f"time cap of {cap_s} s reached")
        params = problem.to_values(res.x)
        score = float(problem.evaluate(params)[0])
        result.update(params=params, score=_finite_or_none(score),
                      status="ok" if math.isfinite(score) and score < problem.compare.PENALTY else "failed")
        log(f"finished: {res.message} nit {res.nit} nfev {res.nfev}; score {score!r} status {result['status']}")
    except Exception as exc:  # noqa: BLE001 - recorded as a failed restart
        log(f"FAILED: {type(exc).__name__}: {exc}\n{traceback.format_exc()}")
    except BaseException as exc:  # the selftest cap (recorded as failed) or an interrupt (not recorded)
        record = isinstance(exc, SelftestTimeout)
        log(f"STOPPED: {type(exc).__name__}: {exc}" if record else
            f"interrupted ({type(exc).__name__}): no result.json; a rerun starts this restart again")
        raise
    finally:
        result["wall_time_s"] = round(time.monotonic() - start, 3)
        if not trace_path.exists():
            trace_path.write_text("generation,best_score,elapsed_s\n", encoding="utf-8")
        if record:
            write_json(out_dir / "result.json", result)
        log.close()
    return result


def _status_best_effort(root: Path) -> None:
    """status.json is informative only: a failed write must never cost a restart."""
    try:
        status(root)
    except (OSError, ValueError) as exc:
        print(f"warning: status.json not updated: {exc}", file=sys.stderr)


def effective_workers(workers: int) -> int:
    """scipy `workers` for one restart: -1 means every core, which under SLURM is the task's allocation
    (SLURM_CPUS_PER_TASK, i.e. settings slurm.cpus_per_task), not every core of the node."""
    if workers == -1:
        cpus = os.environ.get("SLURM_CPUS_PER_TASK", "")
        if cpus.isdigit() and int(cpus) >= 1:
            return int(cpus)
    return workers


def effective_parallel(settings: dict, requested: int | None = None, max_cores: int | None = None) -> int:
    """Restarts to run at a time, K, so restarts never oversubscribe the cores (one rule for every launcher):
    with workers = settings workers and P = `requested` or settings parallel,
      workers -1 (every core)          K = 1
      max_cores known, workers >= 1    K = min(P, max_cores // workers), at least 1
      max_cores unknown                K = P when workers is 1, else 1
    max_cores: a configured host's max_cores, os.cpu_count() locally, None for a printed unconfigured host."""
    k = max(1, int(requested) if requested else int(settings["parallel"]))
    if int(settings["workers"]) == -1:  # the raw setting: under SLURM effective_workers would turn it into a count
        return 1
    workers = int(settings["workers"])
    if max_cores is None:
        return k if workers == 1 else 1
    return max(1, min(k, int(max_cores) // max(1, workers)))


def _refuse_running(out_dir: Path, restart: int, state: str = "alive") -> int:
    try:
        owner = read_json(out_dir / "owner.json")
    except (OSError, ValueError):
        owner = {}
    owner = owner if isinstance(owner, dict) else {}
    slurm = f", SLURM job {owner['slurm_job']} task {owner.get('slurm_task') or '-'}" if owner.get("slurm_job") else ""
    why = ("its owner cannot be checked from here (another host without SLURM ids, or sacct cannot tell)"
           if state == "unknown" else "it is never deleted or started twice")
    print(f"error: restart {restart} is running: owner host {owner.get('host', '?')} pid {owner.get('pid', '?')} "
          f"(started {owner.get('started_at', '?')}{slurm}); {why}. If that process is confirmed gone (squeue, or "
          f"ps on that host), run `python run.py calibrate --restart {restart} --takeover` in the bundle folder (it "
          f"takes over only an owner that cannot be checked), or remove out/restart-{restart} by hand and run "
          "again.", file=sys.stderr)
    return 3


def _back_off(restart: int, why: str) -> int:
    print(f"error: restart {restart}: {why}; not started here.", file=sys.stderr)
    return 3


def _owner_bytes(folder: Path) -> bytes | None:
    try:
        return (folder / "owner.json").read_bytes()
    except OSError:
        return None


def _owner_record() -> dict:
    """owner.json of this process: host, pid, start time, process start identity and SLURM ids."""
    owner = {"host": socket.gethostname(), "pid": os.getpid(), "started_at": now()}
    identity = _process_start(os.getpid())
    if identity is not None and identity[0] is not None:
        owner["process_start"] = identity[0]
    slurm = _slurm_ids()
    if slurm:
        owner.update(slurm_job=slurm[0], slurm_task=slurm[1], slurm_restart=os.environ.get("SLURM_RESTART_COUNT")
                     or "0", slurm_procid=os.environ.get("SLURM_PROCID", ""))
    return owner


def _place(out: Path, prefix: str, target: Path, owner: dict) -> bool:
    """Create `target` with owner.json already inside (temp dir + rename, atomic and exclusive: the rename fails
    when target exists); False when it exists."""
    tmp = Path(tempfile.mkdtemp(prefix=prefix, dir=out))
    try:
        write_json(tmp / "owner.json", owner)
        try:
            os.rename(tmp, target)
            return True
        except OSError:
            if target.exists():
                return False
            raise
    finally:
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)


def _remove(out: Path, folder: Path, restart: int) -> bool:
    """Move `folder` to a unique trash name, then delete it; False when it is already gone."""
    trash = out / f".trash-restart-{restart}.{os.getpid()}.{os.urandom(4).hex()}"
    try:
        os.rename(folder, trash)
    except FileNotFoundError:
        return False
    shutil.rmtree(trash, ignore_errors=True)
    return True


def _claim_path(out: Path, restart: int) -> Path:
    return out / f".claim-restart-{restart}"


def _break_token(out: Path, restart: int, judged: bytes | None) -> Path:
    """The exclusive token for breaking the claim whose owner.json bytes are `judged` (one breaker per claim)."""
    return out / f".claim-restart-{restart}.break-{hashlib.sha256(judged or b'').hexdigest()[:16]}"


def _acquire_claim(out: Path, restart: int, owner: dict, takeover: bool) -> Path | str:
    """The per-restart claim out/.claim-restart-N/ (owner.json inside), held from judging the restart folder to
    placing it, so at most one claimant judges and changes out/restart-N at a time (atomic directory rename, which
    works on NFS). Returns the claim, or the reason to back off.

    A claim whose owner is stale (_owner_state, the restart folder's rules; or cannot be checked, with
    `takeover`) is broken by the one process that creates its break token .claim-restart-N.break-<hash of the
    claim's owner.json>: it re-reads those bytes in place (only a token holder removes a claim, and the token is
    unique to these bytes, so they cannot change meanwhile), moves the claim away and retries. A break token
    left by a dead breaker is reported, never removed automatically."""
    claim = _claim_path(out, restart)
    for _attempt in range(3):
        if _place(out, f".claiming-restart-{restart}.", claim, owner):
            return claim
        judged = _owner_bytes(claim)
        state = _owner_state(claim, claiming=True)
        if state == "alive" or (state == "unknown" and not takeover):
            try:
                holder = json.loads(judged.decode("utf-8")) if judged else {}
            except ValueError:
                holder = {}
            holder = holder if isinstance(holder, dict) else {}
            return (f"another process is starting it (out/{claim.name} held by host {holder.get('host', '?')} pid "
                    f"{holder.get('pid', '?')}); if that process is confirmed gone, remove out/{claim.name} by hand")
        token = _break_token(out, restart, judged)
        if not _place(out, f".breaking-restart-{restart}.", token, owner):
            return (f"another process is clearing the stale claim out/{claim.name} (out/{token.name}); if no process "
                    f"is starting restart {restart}, remove out/{token.name} by hand")
        try:
            if _owner_bytes(claim) == judged:
                _remove(out, claim, restart)
        finally:
            shutil.rmtree(token, ignore_errors=True)
    return f"out/{claim.name} keeps changing"


def cmd_calibrate(root: Path, restart: int, takeover: bool = False) -> int:
    """Run one restart in out/restart-N/, created with owner.json inside (temp dir + rename).

    Every claimant first takes the per-restart claim (_acquire_claim) and holds it until out/restart-N is placed,
    so judging and replacing the folder is one exclusive step. An existing folder without result.json is replaced
    only when its owner is stale (or, with `takeover`, cannot be checked): it is renamed to a unique trash name and
    deleted, and the new folder renamed into place. A claimant that cannot take the claim backs off with exit 3."""
    job = read_json(root / "job.json")
    settings = job["settings"]
    if not 1 <= restart <= settings["restarts"]:
        print(f"error: --restart must be in 1..{settings['restarts']}", file=sys.stderr)
        return 2
    out = root / OUT
    out_dir = out / f"restart-{restart}"
    if (out_dir / "result.json").exists():
        print(f"restart {restart} already done (out/restart-{restart}/result.json); not rerun")
        return 0
    out.mkdir(parents=True, exist_ok=True)
    owner = _owner_record()
    claim = _acquire_claim(out, restart, owner, takeover)
    if isinstance(claim, str):
        return _back_off(restart, claim)
    try:
        if (out_dir / "result.json").exists():  # finished while this process waited for the claim
            print(f"restart {restart} already done (out/restart-{restart}/result.json); not rerun")
            return 0
        if out_dir.exists():
            state = _owner_state(out_dir, claiming=True)
            if state == "alive" or (state == "unknown" and not takeover):
                return _refuse_running(out_dir, restart, state)
            _remove(out, out_dir, restart)  # an interrupted (stale) attempt; start the restart again from its seed
        if not _place(out, f".restart-{restart}.", out_dir, owner):
            return _back_off(restart, "the folder appeared while this process held the claim")
    finally:
        shutil.rmtree(claim, ignore_errors=True)
    _status_best_effort(root)
    result = run_restart(root, restart, settings["seeds"][restart - 1], settings["optimizer_options"],
                         effective_workers(settings["workers"]), out_dir)
    _status_best_effort(root)
    print(f"restart {restart}: {result['status']} score={result['score']} wall_time_s={result['wall_time_s']}")
    return 0 if result["status"] == "ok" else 1


# ---------------------------------------------------------------- validate

def _read_params(path: Path, fit: list[dict], partial: bool = False) -> dict:
    """The fitted values of a --params file: exactly the fitted names, or with `partial` a nonempty subset
    (the others keep the model defaults and are never read from the file)."""
    data = read_json(path)
    if isinstance(data, dict) and isinstance(data.get("params"), dict):
        data = data["params"]  # a restart's result.json
    names = [f["name"] for f in fit]
    given = set(data) if isinstance(data, dict) else None
    if given is None or (not given or not given <= set(names) if partial else given != set(names)):
        raise ValueError(f"--params must give {'a nonempty subset of' if partial else 'exactly'} the fitted names "
                         f"{names}")
    for name, value in data.items():
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            raise ValueError(f"--params {name} must be a positive finite number")
    return {name: float(data[name]) for name in names if name in data}


def prediction_rows(spec: dict, predictions: dict, use: str = "all") -> dict:
    """The rows of `use` ("all": every row) with their data, target and prediction."""
    rows = {}
    for mid, m in sorted(spec["measurements"].items()):
        if use != "all" and m["use"] != use:
            continue
        rows[mid] = {"rule_id": m["rule_id"], "type": m["type"], "observable": m["observable"],
                     "condition": m["condition"], "time": m["time"], "dose": m["dose"], "use": m["use"],
                     "role": m["role"], "trivial": bool(m.get("trivial")), "value": m["value"],
                     "target": _finite_or_none(m["target"]), "prediction": _finite_or_none(predictions.get(mid))}
    return rows


def validate(root: Path, params: dict | None, use: str, partial: bool = False, with_nominal: bool = False,
             problem: Problem | None = None) -> dict:
    """Score `params` (None: the model defaults) on the rows of `use`, with one loaded Problem.

    partial: `params` may name only some fitted values; the others keep the model defaults (`params` is the
    effective vector, `params_given` the names given). with_nominal: also report the model defaults of the
    fitted names (report["nominal"] = {"params": ...}), read from the loaded model, never simulated.
    """
    problem = problem or Problem(root)
    nominal = problem.nominal() if params is None or partial or with_nominal else None
    fitted = nominal if params is None else ({**nominal, **params} if partial else params)
    score, predictions, sims = problem.evaluate(fitted, use)
    report = {"use": use, "params_source": "nominal" if params is None else ("partial" if partial else "given"),
              "params": fitted, "score": float(score), "penalty": score >= problem.compare.PENALTY,
              "predictions": prediction_rows(problem.spec, predictions, use)}
    if partial:
        report["params_given"] = sorted(params or {})
    if with_nominal:
        report["nominal"] = {"params": nominal}
    return report


def cmd_validate(root: Path, params_file: str | None, use: str, out: str | None, partial: bool = False,
                 with_nominal: bool = False) -> int:
    problem = Problem(root)
    problem._load()
    params = _read_params(Path(params_file), problem.fit, partial) if params_file else None
    report = validate(root, params, use, partial, with_nominal, problem=problem)
    text = json.dumps(report, indent=2, allow_nan=False) + "\n"
    if out:
        Path(out).write_text(text, encoding="utf-8")
        print(f"validate: score={report['score']!r} (use={use}) written to {out}")
    else:
        sys.stdout.write(text)
    return 0


# ---------------------------------------------------------------- selftest

class SelftestTimeout(BaseException):
    """The selftest cap. A BaseException, so the objective's `except Exception` cannot swallow it."""


class _Cap:
    """Raise SelftestTimeout after `seconds` (SIGALRM, POSIX)."""

    def __init__(self, seconds: int):
        self.seconds = seconds

    def _fire(self, *_):
        raise SelftestTimeout(f"selftest exceeded its {self.seconds} s cap")

    def __enter__(self):
        if hasattr(signal, "SIGALRM"):
            self.previous = signal.signal(signal.SIGALRM, self._fire)
            signal.alarm(self.seconds)
        return self

    def __exit__(self, *exc):
        if hasattr(signal, "SIGALRM"):
            signal.alarm(0)
            signal.signal(signal.SIGALRM, self.previous)
        return False


def cmd_selftest(root: Path) -> int:
    """One restart with maxiter=1 and popsize=1, then validate at nominal values, under a 300 s cap."""
    job = read_json(root / "job.json")
    settings = job["settings"]
    out_dir = root / OUT / "selftest"
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)
    options = dict(settings["optimizer_options"], maxiter=1, popsize=1)
    report = {"started_at": now(), "cap_s": SELFTEST_CAP_S, "status": "failed"}
    start = time.monotonic()
    try:
        with _Cap(SELFTEST_CAP_S):
            result = run_restart(root, 0, settings["seeds"][0], options, 1, out_dir,  # checks the stack: 1 worker
                                 cap_s=SELFTEST_CAP_S)
            report["restart"] = result
            check = validate(root, None, "fit")
            write_json(out_dir / "validate.json", check)
            report["validate_nominal_score"] = check["score"]
        report["status"] = "ok" if result["status"] == "ok" else "failed"
    except (Exception, SelftestTimeout) as exc:  # noqa: BLE001 - run_restart's finally wrote result.json
        report["error"] = f"{type(exc).__name__}: {exc}"
    report["wall_time_s"] = round(time.monotonic() - start, 3)
    write_json(out_dir / "selftest.json", report)
    print(f"selftest: {report['status']} ({report['wall_time_s']} s)"
          + (f"; {report['error']}" if "error" in report else ""))
    return 0 if report["status"] == "ok" else 1


# ---------------------------------------------------------------- env report

def cmd_env_report(root: Path, args) -> int:
    from importlib import metadata
    selftest_path = root / OUT / "selftest" / "selftest.json"
    selftest = read_json(selftest_path) if selftest_path.is_file() else None
    packages = {}
    for dist in metadata.distributions():
        name = dist.metadata["Name"]
        if name:
            packages[name.lower()] = dist.version
    report = {
        "status": "ok" if args.selftest_exit == 0 and selftest and selftest.get("status") == "ok" else "failed",
        "created_at": now(), "job_id": read_json(root / "job.json")["job_id"],
        "job_sha256": sha256_file(root / "job.json"),
        "lock_sha256": sha256_file(root / "env" / "requirements.lock"),
        "uv": {"version": args.uv_version, "platform": args.uv_platform, "sha256": args.uv_sha256},
        "python": {"version": platform.python_version(), "implementation": platform.python_implementation(),
                   "executable": sys.executable},
        "host": {"system": platform.system(), "release": platform.release(), "machine": platform.machine(),
                 "platform": platform.platform(), "cpu_count": os.cpu_count()},
        "packages": dict(sorted(packages.items())),
        "selftest_exit": args.selftest_exit, "selftest": selftest,
    }
    write_json(root / "env_report.json", report)
    print(f"env_report.json: {report['status']}")
    return 0 if report["status"] == "ok" else 1


# ---------------------------------------------------------------- collect

def _figures(folder: Path, rows: dict, results: list[dict], spec: dict, traces: dict) -> list[str]:
    os.environ.setdefault("MPLBACKEND", "Agg")
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    folder.mkdir(parents=True, exist_ok=True)
    made = []
    # data vs fit: one panel per (rule, observable, condition); x = time, or dose for dose_response
    groups: dict = {}
    for mid, row in rows.items():
        if row["role"] == "reference":
            continue
        groups.setdefault((row["rule_id"], row["observable"], row["condition"]), []).append(row)
    if groups:
        cols = min(3, len(groups))
        nrows = math.ceil(len(groups) / cols)
        fig, axes = plt.subplots(nrows, cols, figsize=(4.2 * cols, 3.2 * nrows), squeeze=False)
        for ax, (key, items) in zip(axes.flat, sorted(groups.items())):
            dose = items[0]["type"] == "dose_response"
            items = sorted(items, key=lambda r: (r["dose"] if dose else r["time"]) or 0.0)
            xs = [(r["dose"] if dose else r["time"]) for r in items]
            for use, face in (("fit", "black"), ("holdout", "none")):
                pts = [(x, r["target"]) for x, r in zip(xs, items) if r["use"] == use and r["target"] is not None]
                if pts:
                    ax.scatter(*zip(*pts), facecolors=face, edgecolors="black", label=f"data ({use})", zorder=3)
            pred = [(x, r["prediction"]) for x, r in zip(xs, items) if r["prediction"] is not None]
            if pred:
                ax.plot(*zip(*pred), "x-", color="tab:red", label="best fit")
            ax.set_title(f"{key[0]}: {key[1]} | {key[2]}", fontsize=9)
            ax.set_xlabel(f"dose ({spec.get('dose_model_unit') or 'model unit'})" if dose
                          else f"time ({spec['time_model_unit']})")
            ax.legend(fontsize=7)
        for ax in list(axes.flat)[len(groups):]:
            ax.axis("off")
        fig.tight_layout()
        fig.savefig(folder / "data_vs_fit.png", dpi=120)
        plt.close(fig)
        made.append("data_vs_fit.png")
    ok = [r for r in results if r["status"] == "ok"]
    if ok:
        names = [f["name"] for f in spec["fit"]]
        best = min(ok, key=lambda r: r["score"])
        fig, ax = plt.subplots(figsize=(max(4.0, 1.1 * len(names) + 2), 3.6))
        for i, f in enumerate(spec["fit"]):
            values = [math.log10(r["params"][f["name"]]) for r in ok]
            offsets = [i + 0.15 * ((k % 5) - 2) / 2 for k in range(len(values))]
            ax.scatter(offsets, values, color="tab:blue", alpha=0.6, s=16)
            ax.scatter([i], [math.log10(best["params"][f["name"]])], color="tab:red", marker="*", s=80, zorder=3)
            ax.hlines([f["log10_lower"], f["log10_upper"]], i - 0.3, i + 0.3, colors="grey", linestyles="dashed")
        ax.set_xticks(range(len(names)), names, rotation=45, ha="right")
        ax.set_ylabel("log10 value (dashed: bounds, star: best)")
        ax.set_title(f"fitted parameters over {len(ok)} successful restarts", fontsize=9)
        fig.tight_layout()
        fig.savefig(folder / "parameters.png", dpi=120)
        plt.close(fig)
        made.append("parameters.png")
    if any(traces.values()):
        fig, ax = plt.subplots(figsize=(5.5, 3.6))
        for restart, points in sorted(traces.items()):
            if points:
                ax.plot([p[0] for p in points], [p[1] for p in points], label=f"restart {restart}", lw=1)
        ax.set_yscale("log")
        ax.set_xlabel("generation")
        ax.set_ylabel("best objective (SSE)")
        if len(traces) <= 12:
            ax.legend(fontsize=7)
        fig.tight_layout()
        fig.savefig(folder / "traces.png", dpi=120)
        plt.close(fig)
        made.append("traces.png")
    return made


def _trace(path: Path) -> list[tuple[int, float]]:
    points = []
    with contextlib.suppress(OSError, ValueError, KeyError):
        with path.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                value = float(row["best_score"])
                if math.isfinite(value) and value > 0:
                    points.append((int(row["generation"]), value))
    return points


def cmd_collect(root: Path) -> int:
    job = read_json(root / "job.json")
    total = job["settings"]["restarts"]
    stage = root / OUT / "collect"
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)
    log = _Log(stage / "collect_log.txt")
    results, traces = [], {}
    for n in range(1, total + 1):
        folder = root / OUT / f"restart-{n}"
        if not (folder / "result.json").is_file():
            continue
        results.append(read_json(folder / "result.json"))
        target = stage / "restarts" / f"restart-{n}"
        target.mkdir(parents=True)
        for name in ("result.json", "trace.csv", "log.txt"):
            if (folder / name).is_file():
                shutil.copyfile(folder / name, target / name)
        traces[n] = _trace(folder / "trace.csv")
    for name in ("selftest.json", "validate.json", "log.txt", "result.json", "trace.csv"):
        if (root / OUT / "selftest" / name).is_file():
            (stage / "selftest").mkdir(exist_ok=True)
            shutil.copyfile(root / OUT / "selftest" / name, stage / "selftest" / name)
    for name in ("env_report.json", "launch.log"):
        if (root / name).is_file():
            shutil.copyfile(root / name, stage / name)
    ok = [r for r in results if r.get("status") == "ok" and r.get("score") is not None]
    rows = {}
    problem = None
    if ok:
        best = min(ok, key=lambda r: (r["score"], r["restart"]))
        try:
            problem = Problem(root)
            # use=fit only: hold-out rows (and hold-out-only times) stay unseen until score_holdout
            score, predictions, sims = problem.evaluate(best["params"], "fit")
            rows = prediction_rows(problem.spec, predictions, "fit")
            write_json(stage / "predictions.json", {"best_restart": best["restart"], "params": best["params"],
                                                    "score_fit": _finite_or_none(score), "rows": rows})
            write_json(stage / "simulations.json", {
                "best_restart": best["restart"], "time_model_unit": problem.spec["time_model_unit"],
                "conditions": {name: {obs: [[t, _finite_or_none(v)] for t, v in values.items()]
                                      for obs, values in by_obs.items()} for name, by_obs in sims.items()}})
            log(f"best restart {best['restart']} score {score!r}")
        except Exception as exc:  # noqa: BLE001
            log(f"best-fit predictions failed: {type(exc).__name__}: {exc}")
    else:
        log("no successful restart: no best-fit predictions")
    try:
        spec = problem.spec if problem is not None else read_json(root / "calibration" / "spec.json")
        made = _figures(stage / "figures", rows, results, spec, traces)
        log(f"figures: {', '.join(made) or 'none'}")
    except Exception as exc:  # noqa: BLE001 - figures are a convenience
        log(f"figures failed: {type(exc).__name__}: {exc}")
    log.close()
    files = {p.relative_to(stage).as_posix(): sha256_file(p) for p in sorted(stage.rglob("*")) if p.is_file()}
    summary = {"job_sha256": sha256_file(root / "job.json"), "partial": len(results) < total,
               "restarts": sorted(results, key=lambda r: r["restart"]), "files": files}
    write_json(stage / "results.json", summary)
    tmp = root / "results.tar.gz.tmp"
    with tarfile.open(tmp, "w:gz") as tf:
        for path in sorted(stage.rglob("*")):
            if path.is_file():
                tf.add(path, arcname=path.relative_to(stage).as_posix(), recursive=False)
    os.replace(tmp, root / "results.tar.gz")
    status(root)
    n_ok = len(ok)
    print(f"results.tar.gz written: {len(results)}/{total} restarts ({n_ok} ok)"
          + (" - PARTIAL" if summary["partial"] else ""))
    return 0


# ---------------------------------------------------------------- CLI

def main(argv: list[str] | None = None) -> int:
    os.environ.setdefault("MPLBACKEND", "Agg")
    os.environ.setdefault("MPLCONFIGDIR", str(ROOT / "env" / "mplconfig"))  # keep matplotlib's cache in the job
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("selftest")
    v = sub.add_parser("validate")
    v.add_argument("--params", metavar="FILE", help="JSON {fit name: value} or a restart result.json")
    v.add_argument("--use", choices=USES, default="fit")
    v.add_argument("--out", metavar="FILE")
    v.add_argument("--partial", action="store_true",
                   help="--params may give a subset of the fitted names; the others keep the model defaults")
    v.add_argument("--nominal", action="store_true", help="also report the model defaults of the fitted names")
    c = sub.add_parser("calibrate")
    c.add_argument("--restart", type=int, required=True)
    c.add_argument("--takeover", action="store_true",
                   help="take over a restart folder whose owner cannot be checked from here (another host without "
                        "SLURM ids, or sacct cannot tell), after confirming that process is gone")
    sub.add_parser("collect")
    sub.add_parser("status")
    k = sub.add_parser("parallel")
    k.add_argument("--requested", type=int)
    e = sub.add_parser("env-report")
    e.add_argument("--uv-version", required=True)
    e.add_argument("--uv-platform", required=True)
    e.add_argument("--uv-sha256", required=True)
    e.add_argument("--selftest-exit", type=int, required=True)
    args = parser.parse_args(argv)
    if args.command == "selftest":
        return cmd_selftest(ROOT)
    if args.command == "validate":
        if args.partial and not args.params:
            parser.error("validate --partial needs --params")
        return cmd_validate(ROOT, args.params, args.use, args.out, args.partial, args.nominal)
    if args.command == "calibrate":
        return cmd_calibrate(ROOT, args.restart, takeover=args.takeover)
    if args.command == "collect":
        return cmd_collect(ROOT)
    if args.command == "status":
        print(json.dumps(status(ROOT), indent=2))
        return 0
    if args.command == "parallel":
        if args.requested is not None and args.requested < 1:
            print("error: --requested must be >= 1", file=sys.stderr)
            return 2
        print(effective_parallel(read_json(ROOT / "job.json")["settings"], args.requested, os.cpu_count()))
        return 0
    return cmd_env_report(ROOT, args)


if __name__ == "__main__":
    sys.exit(main())
