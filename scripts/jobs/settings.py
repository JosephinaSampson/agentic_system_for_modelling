"""Calibration settings: the researcher-approved knobs of a calibration job (stdlib only).

Exact keys (missing or extra keys are refused):
  restarts           int >= 1, the number of independent BioMASS-style `x_id` runs
  seeds              list of `restarts` unique ints in [0, 2**32), one per restart (restart N uses seeds[N-1])
  optimizer_options  exactly the BioMASS 0.14.0 differential-evolution defaults, written out:
                     strategy "best1bin", maxiter 50, popsize 3, tol 1e-4, mutation 0.1,
                     recombination 0.5, polish false (values may be changed, keys may not)
  workers            int >= 1 or -1: scipy `workers` inside one restart (BioMASS default 1); -1 = every core,
                     under SLURM the task's allocation (slurm.cpus_per_task, read from SLURM_CPUS_PER_TASK)
  parallel           int >= 1: restarts run at the same time by local.sh / remote.sh start, capped by
                     run.effective_parallel: min(parallel, cores // workers), 1 for workers -1
  slurm              {"time": "HH:MM:SS", "cpus_per_task": int >= 1}
  good_fit_margin    float > 0: restarts within this fraction of the best score count as good fits (import)
  reproduce_rtol     float > 0: relative tolerance of the import's score reproduction (default 1e-3)

`workers` is the BioMASS `optimizer_options["workers"]`; it is kept at the top level so a single
value governs it, and the runner passes it to differential_evolution.

When few parameters are fitted, raise `popsize` and/or `maxiter`: the population is popsize x n
members (at least 5), so with the BioMASS defaults 2 fitted parameters give a population of 6 that
collapses and stops early at a poor score (seen live: SSE 0.4 after 10 generations where 1.6e-6 was
attainable). More restarts alone do not fix this.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from scripts.data.common import strict_json

KEYS = ("restarts", "seeds", "optimizer_options", "workers", "parallel", "slurm", "good_fit_margin",
        "reproduce_rtol")
BIOMASS_DEFAULTS = {"strategy": "best1bin", "maxiter": 50, "popsize": 3, "tol": 1e-4, "mutation": 0.1,
                    "recombination": 0.5, "polish": False}
STRATEGIES = ("best1bin", "best1exp", "rand1bin", "rand1exp", "rand2bin", "rand2exp", "randtobest1bin",
              "randtobest1exp", "currenttobest1bin", "currenttobest1exp", "best2exp", "best2bin")
SLURM_TIME = re.compile(r"\A\d{2,}:[0-5]\d:[0-5]\d\Z")


def _int(value) -> bool:
    return type(value) is int


def _number(value) -> bool:
    return type(value) in (int, float)


def _check_optimizer(opts) -> None:
    if not isinstance(opts, dict) or set(opts) != set(BIOMASS_DEFAULTS):
        raise ValueError(f"optimizer_options must have exactly the keys {', '.join(BIOMASS_DEFAULTS)}")
    if opts["strategy"] not in STRATEGIES:
        raise ValueError(f"optimizer_options.strategy must be one of {', '.join(STRATEGIES)}")
    for key in ("maxiter", "popsize"):
        if not _int(opts[key]) or opts[key] < 1:
            raise ValueError(f"optimizer_options.{key} must be an int >= 1")
    if not _number(opts["tol"]) or opts["tol"] < 0:
        raise ValueError("optimizer_options.tol must be a number >= 0")
    mutation = opts["mutation"]
    pair = isinstance(mutation, list) and len(mutation) == 2 and all(_number(m) for m in mutation)
    if not ((_number(mutation) and 0 <= mutation < 2) or (pair and 0 <= mutation[0] <= mutation[1] < 2)):
        raise ValueError("optimizer_options.mutation must be a number in [0, 2) or a pair [min, max] in [0, 2)")
    if not _number(opts["recombination"]) or not 0 <= opts["recombination"] <= 1:
        raise ValueError("optimizer_options.recombination must be a number in [0, 1]")
    if type(opts["polish"]) is not bool:
        raise ValueError("optimizer_options.polish must be true or false (BioMASS recommends false)")


def check(data) -> dict:
    """The settings dict when valid; ValueError naming the first problem otherwise."""
    if not isinstance(data, dict):
        raise ValueError("settings must be a JSON object")
    missing = [k for k in KEYS if k not in data]
    if missing:
        raise ValueError(f"settings miss the keys: {', '.join(missing)}")
    extra = sorted(set(data) - set(KEYS))
    if extra:
        raise ValueError(f"settings have extra keys: {', '.join(extra)}")
    restarts = data["restarts"]
    if not _int(restarts) or restarts < 1:
        raise ValueError("restarts must be an int >= 1")
    seeds = data["seeds"]
    if (not isinstance(seeds, list) or len(seeds) != restarts
            or not all(_int(s) and 0 <= s < 2 ** 32 for s in seeds) or len(set(seeds)) != len(seeds)):
        raise ValueError("seeds must be a list of `restarts` unique ints in [0, 2**32)")
    _check_optimizer(data["optimizer_options"])
    if not _int(data["workers"]) or not (data["workers"] >= 1 or data["workers"] == -1):
        raise ValueError("workers must be an int >= 1 or -1")
    if not _int(data["parallel"]) or data["parallel"] < 1:
        raise ValueError("parallel must be an int >= 1")
    slurm = data["slurm"]
    if (not isinstance(slurm, dict) or set(slurm) != {"time", "cpus_per_task"}
            or not isinstance(slurm["time"], str) or not SLURM_TIME.fullmatch(slurm["time"])
            or not _int(slurm["cpus_per_task"]) or slurm["cpus_per_task"] < 1):
        raise ValueError('slurm must be {"time": "HH:MM:SS", "cpus_per_task": int >= 1}')
    for key in ("good_fit_margin", "reproduce_rtol"):
        if not _number(data[key]) or not data[key] > 0:
            raise ValueError(f"{key} must be a number > 0")
    return data


def load(path) -> dict:
    """Read and check a settings file (duplicate keys and NaN/Infinity refused)."""
    return check(strict_json(Path(path).read_bytes()))


def canonical(data: dict) -> bytes:
    return json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")


def digest(data: dict) -> str:
    """sha256 of the canonical JSON of the settings (whitespace and key order do not matter)."""
    return hashlib.sha256(canonical(data)).hexdigest()
