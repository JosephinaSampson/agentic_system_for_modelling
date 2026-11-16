#!/usr/bin/env python3
"""Verified import of a returned calibration results archive, and the calibration proposal.

  python3 scripts/jobs/import_results.py <job-id>

Reads inputs/calibration-results/<job-id>/results.tar.gz (never modified) and writes, beside the job's
job.json under runs/orchestrator/<p>/jobs/<job-id>/: calibration_proposal.json, report.md and figures/.
Nothing reaches the model: the proposal is what the researcher reviews.

Verification (every failure is a ValueError and nothing is written):
  * the archive holds only regular files with safe names (no absolute paths, `..`, links, devices or
    duplicates); it is extracted into a temp folder inside the job folder, never /tmp;
  * every member except results.json is listed in results.json `files` with the same sha256, and every listed
    file is present (an unlisted member or a missing file is refused);
  * results.json `job_sha256` equals the sha256 of the local job.json;
  * env_report.json: Python 3.12.x and biomass 0.14.0;
  * each restart seed equals the job's settings seed for that restart; each ok restart's fitted values are
    inside the mapping bounds; at least one restart is ok;
  * the best restart's score is reproduced (`reproduce`) within settings `reproduce_rtol` (relative).

Proposal conventions:
  * results_sha256 is the sha256 of results.tar.gz itself;
  * good restarts = ok restarts with score <= best * (1 + good_fit_margin);
  * poorly_constrained = log10(good_max / good_min) > POORLY_CONSTRAINED_LOG10_SPAN;
  * `start` is the model's nominal value, computed locally by the reproduce step from the trusted cache copy
    of the bundle (`run.py validate` at nominal values); the archive's selftest/validate.json comes from the
    remote machine and is never used for it (hashes only prove it is the file the remote side listed).
    null when the reproduce step gives no finite nominal value for a parameter (unknown, never 0);
  * `partial` is true when results.json says so or fewer than settings.restarts restarts are ok (a missing
    or failed restart); failed restarts are listed with their status and excluded from the good restarts.

`reproduce(root, job_id, params_file) -> (score, nominal)` is injectable. The default runs
`launch.py local validate <job-id> --params <file> --nominal --out <file>` once (bootstrapping the cache
environment when missing: this needs internet and builds ~/.cache/agentic-jobs/<job-id>/, minutes, even for
ssh/slurm jobs) and reads the score of the report (use=fit, the objective the restarts minimised) and its
`nominal.params` (the model defaults of the fitted names, from the same process) as `nominal`.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import shutil
import statistics
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.data.common import finite_number, project_root, reject_symlinks, sha256_file, strict_json  # noqa: E402
from scripts.jobs import launch  # noqa: E402
from scripts.jobs.runtime.compare import PENALTY  # noqa: E402

POORLY_CONSTRAINED_LOG10_SPAN = 1.0
PIN_PYTHON = "3.12."
PIN_BIOMASS = "0.14.0"
MAX_MEMBER_BYTES = 512 * 1024 * 1024
MAX_TOTAL_BYTES = 2 * 1024 ** 3
FIGURE = re.compile(r"\Afigures/[A-Za-z0-9._-]+\.png\Z")
BOUND_RTOL = 1e-9  # gene2val's 10**(...) can land a rounding error beyond a bound
SHA256 = re.compile(r"\A[0-9a-f]{64}\Z")
HEURISTICS = ("Good restarts are the successful restarts whose score is within good_fit_margin of the best. "
              "A parameter is flagged poorly_constrained when its good-restart values span more than one "
              "decade (log10(good_max/good_min) > 1.0). These are heuristics: they flag, they do not prove, "
              "identifiability. Nothing here changes the model until the researcher approves it.")


def _write_new(path: Path, data: bytes) -> None:
    """Create path with data; identical existing bytes are fine, different ones are never overwritten."""
    if path.exists() or path.is_symlink():
        if path.is_file() and not path.is_symlink() and path.read_bytes() == data:
            return
        raise ValueError(f"{path.name} already exists with different content; it is never overwritten")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        handle.write(data)


def _safe_name(name: str) -> bool:
    parts = PurePosixPath(name).parts
    return bool(parts) and not name.startswith("/") and "\\" not in name and not any(
        p in ("", ".", "..") for p in name.split("/"))


def extract(archive: Path, dest: Path) -> dict[str, Path]:
    """Extract only safe regular files into dest; {member name: path}."""
    try:
        tf = tarfile.open(archive, "r:gz")
    except (tarfile.TarError, EOFError, OSError) as exc:
        raise ValueError(f"results.tar.gz is not a readable gzip tar: {exc}") from exc
    out: dict[str, Path] = {}
    with tf:
        try:
            members = tf.getmembers()
        except (tarfile.TarError, EOFError, OSError) as exc:
            raise ValueError(f"results.tar.gz is not readable: {exc}") from exc
        total = 0
        for m in members:
            if not _safe_name(m.name):
                raise ValueError(f"results.tar.gz member {m.name!r} has an unsafe path")
            if not m.isreg():
                raise ValueError(f"results.tar.gz member {m.name!r} is not a regular file "
                                 "(links, devices and directories are refused)")
            if m.size > MAX_MEMBER_BYTES:
                raise ValueError(f"results.tar.gz member {m.name!r} is larger than {MAX_MEMBER_BYTES} bytes")
            if m.name in out:
                raise ValueError(f"results.tar.gz has a duplicate member {m.name!r}")
            if m.name.startswith("figures/") and not FIGURE.fullmatch(m.name):
                raise ValueError(f"results.tar.gz member {m.name!r}: only figures/<name>.png files "
                                 "([A-Za-z0-9._-] names) are accepted under figures/")
            total += m.size
            if total > MAX_TOTAL_BYTES:
                raise ValueError(f"results.tar.gz is larger than {MAX_TOTAL_BYTES} bytes uncompressed")
            target = dest / m.name
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("xb") as handle:
                    shutil.copyfileobj(tf.extractfile(m), handle)
            except (FileExistsError, NotADirectoryError, IsADirectoryError) as exc:
                raise ValueError(f"results.tar.gz member {m.name!r} collides with another member: {exc}") from exc
            out[m.name] = target
    return out


def verify_members(members: dict[str, Path], results: dict) -> None:
    files = results.get("files")
    if (not isinstance(files, dict)
            or not all(isinstance(k, str) and isinstance(v, str) and SHA256.fullmatch(v) for k, v in files.items())):
        raise ValueError("results.json files must map file names to sha256")
    unlisted = sorted(set(members) - set(files) - {"results.json"})
    if unlisted:
        raise ValueError(f"results.tar.gz member {unlisted[0]!r} is not listed in results.json files")
    for name in sorted(files):
        if name not in members:
            raise ValueError(f"results.json lists {name!r} but it is missing from results.tar.gz")
        if sha256_file(members[name]) != files[name]:
            raise ValueError(f"{name} does not match results.json files (sha256 differs)")


def check_env(members: dict[str, Path]) -> None:
    if "env_report.json" not in members:
        raise ValueError("results.tar.gz has no env_report.json")
    report = strict_json(members["env_report.json"].read_bytes())
    version = (report.get("python") or {}).get("version") if isinstance(report, dict) else None
    if not isinstance(version, str) or not version.startswith(PIN_PYTHON):
        raise ValueError(f"env_report: Python {version!r} is not the pinned Python 3.12.x")
    packages = report.get("packages") if isinstance(report.get("packages"), dict) else {}
    if packages.get("biomass") != PIN_BIOMASS:
        raise ValueError(f"env_report: biomass {packages.get('biomass')!r} is not the pinned biomass {PIN_BIOMASS}")


def check_restarts(results: dict, settings: dict, fit: list[dict]) -> list[dict]:
    restarts = results.get("restarts")
    if not isinstance(restarts, list) or not restarts:
        raise ValueError("results.json has no restarts")
    seen = set()
    names = [f["name"] for f in fit]
    for r in restarts:
        n = r.get("restart") if isinstance(r, dict) else None
        if type(n) is not int or not 1 <= n <= settings["restarts"] or n in seen:
            raise ValueError(f"results.json restart {n!r} is invalid or repeated (settings has "
                             f"{settings['restarts']} restarts)")
        seen.add(n)
        expected = settings["seeds"][n - 1]
        if type(r.get("seed")) is not int:
            raise ValueError(f"restart {n}: seed {r.get('seed')!r} must be an integer")
        wall = r.get("wall_time_s")
        if wall is not None and not (finite_number(wall) and wall >= 0):
            raise ValueError(f"restart {n}: wall_time_s {wall!r} must be a finite number >= 0 or null")
        score = r.get("score")
        if score is not None and not finite_number(score):
            raise ValueError(f"restart {n}: score {score!r} must be a finite number or null")
        if r.get("seed") != expected:
            raise ValueError(f"seed {r.get('seed')!r} of restart {n} differs from the settings seed {expected}")
        if r.get("status") not in ("ok", "failed"):
            raise ValueError(f"restart {n} has status {r.get('status')!r} (expected ok or failed)")
        if r["status"] != "ok":
            continue
        if not finite_number(r.get("score")) or r["score"] < 0:
            raise ValueError(f"restart {n} is ok but its score {r.get('score')!r} is not a finite number >= 0")
        params = r.get("params")
        if not isinstance(params, dict) or set(params) != set(names):
            raise ValueError(f"restart {n} params must be exactly the fitted names {names}")
        for f in fit:
            value = params[f["name"]]
            lo, hi = f["lower"], f["upper"]
            if not finite_number(value) or not lo * (1 - BOUND_RTOL) <= value <= hi * (1 + BOUND_RTOL):
                raise ValueError(f"restart {n}: fitted {f['name']} = {value!r} is outside its bounds [{lo}, {hi}]")
    return sorted(restarts, key=lambda r: r["restart"])


def cross_check(members: dict[str, Path], restarts: list[dict]) -> None:
    """Each restarts/restart-N/result.json must agree with its results.json entry."""
    for r in restarts:
        name = f"restarts/restart-{r['restart']}/result.json"
        if name not in members:
            raise ValueError(f"{name} is missing from results.tar.gz (results.json lists restart {r['restart']})")
        try:
            own = strict_json(members[name].read_bytes())
        except ValueError as exc:
            raise ValueError(f"{name} is not valid JSON: {exc}") from exc
        for key in ("restart", "seed", "status", "score", "params"):
            if not isinstance(own, dict) or own.get(key) != r.get(key):
                raise ValueError(f"{name} {key} differs from the results.json entry of restart {r['restart']}")


def default_reproduce(root: Path, job_id: str, params_file: Path) -> tuple[float, dict]:
    """(score of `launch.py local validate <job-id> --params <file> --nominal` (use=fit), the local nominal values).

    One validate run (one bootstrap check, one process, one model load) gives both; they come from the
    locally unpacked, sha256-verified copy of the bundle, never from the archive.
    """
    out = Path(params_file).with_name("reproduced.json")
    code = launch.validate(root, job_id, str(params_file), "fit", str(out), nominal=True)
    if code != 0:
        raise ValueError(f"launch.py local validate failed (exit {code}); cannot reproduce the best score")
    report = strict_json(out.read_bytes())
    if not isinstance(report, dict) or report.get("params_source") != "given":
        raise ValueError("the local validate report is not a score of the given (best) params")
    score = report.get("score")
    if not finite_number(score):
        raise ValueError("the reproduced score is not a finite number")
    nominal = report.get("nominal")
    if not isinstance(nominal, dict) or not isinstance(nominal.get("params"), dict):
        raise ValueError("the local validate report has no nominal params")
    return float(score), nominal["params"]


def _stats(fit: list[dict], restarts: list[dict], good: list[int], best: dict, start: dict) -> list[dict]:
    rows = []
    good_set = set(good)
    for f in fit:
        values = [r["params"][f["name"]] for r in restarts if r["restart"] in good_set]
        lo, hi = min(values), max(values)
        rows.append({"name": f["name"], "kind": f["kind"], "unit": f["unit"], "start": start.get(f["name"]),
                     "lower": f["lower"], "upper": f["upper"], "best": best["params"][f["name"]],
                     "good_min": lo, "good_median": statistics.median(values), "good_max": hi,
                     "poorly_constrained": math.log10(hi / lo) > POORLY_CONSTRAINED_LOG10_SPAN})
    return rows


def _start_values(nominal, names: list[str]) -> dict:
    """The locally computed nominal values of the fitted names (finite numbers only; others stay unknown)."""
    if not isinstance(nominal, dict):
        raise ValueError("the reproduce step must return the local nominal values as an object")
    return {n: float(nominal[n]) for n in names if finite_number(nominal.get(n))}


def _g(value) -> str:
    return "n/a" if value is None else f"{value:.6g}"


def _cell(value) -> str:
    """Text for a markdown table cell: no pipes, backticks, angle brackets or line breaks."""
    text = " ".join(str(value).split())
    for ch in "|`<>[]!":
        text = text.replace(ch, "\\" + ch)
    return text


def report_md(proposal: dict) -> str:
    lines = [f"# Calibration proposal for {_cell(proposal['job_id'])}", "",
             f"Model: ODE session {_cell(proposal['model']['ode_session'])}, "
             f"revision {_cell(proposal['model']['revision'])}  ",
             f"Mapping: {_cell(proposal['mapping']['path'])} (approval {_cell(proposal['mapping']['approval'])})  ",
             f"Best restart: {proposal['best_restart']}; good restarts: "
             f"{', '.join(map(str, proposal['good_restarts']))}", ""]
    if proposal["partial"]:
        lines += ["**PARTIAL results:** not every restart returned a successful result.", ""]
    lines += ["## Restarts", "", "| restart | seed | score | status | wall time (s) |", "|---|---|---|---|---|"]
    for r in proposal["restarts"]:
        lines.append(f"| {r['restart']} | {r['seed']} | {_g(r['score'])} | {_cell(r['status'])} | {_g(r['wall_time_s'])} |")
    lines += ["", "## Parameters", "",
              "| name | kind | unit | start | lower | upper | best | good min | good median | good max "
              "| poorly constrained |", "|---|---|---|---|---|---|---|---|---|---|---|"]
    for p in proposal["parameters"]:
        flag = "YES" if p["poorly_constrained"] else "no"
        lines.append(f"| {_cell(p['name'])} | {_cell(p['kind'])} | {_cell(p['unit'])} | {_g(p['start'])} | {_g(p['lower'])} | "
                     f"{_g(p['upper'])} | {_g(p['best'])} | {_g(p['good_min'])} | {_g(p['good_median'])} | "
                     f"{_g(p['good_max'])} | {flag} |")
    lines += ["", proposal["heuristics_note"], ""]
    if proposal["figures"]:
        lines += ["## Figures", ""] + [f"![{Path(f).stem}]({f})" for f in proposal["figures"]] + [""]
    lines += ["Review this proposal; no value reaches the model until you approve it.", ""]
    return "\n".join(lines)


def import_results(root, job_id: str, *, reproduce=None) -> Path:
    """Verify results.tar.gz of the job and write the proposal; returns calibration_proposal.json."""
    root = Path(root)
    reproduce = reproduce or default_reproduce
    job_dir = launch.find_job(root, job_id)
    job_bytes = (job_dir / "job.json").read_bytes()
    job = strict_json(job_bytes)
    archive = root.joinpath(*launch.RESULTS, job_id, "results.tar.gz")
    if not archive.is_file():
        raise ValueError(f"no results.tar.gz at {archive.relative_to(root).as_posix()}; "
                         f"fetch the results first (launch.py ... fetch {job_id})")
    reject_symlinks(archive, root)
    mapping_path = root / job["mapping"]["path"]
    if sha256_file(mapping_path) != job["mapping"]["sha256"]:
        raise ValueError(f"{job['mapping']['path']} changed since the job was built (sha256 mismatch)")
    mapping = strict_json(mapping_path.read_bytes())
    settings, fit = job["settings"], mapping["fit"]
    tmp = Path(tempfile.mkdtemp(prefix=".import-", dir=job_dir))  # not /tmp: a small RAM tmpfs
    try:
        members = extract(archive, tmp / "x")
        if "results.json" not in members:
            raise ValueError("results.tar.gz has no results.json")
        results = strict_json(members["results.json"].read_bytes())
        if not isinstance(results, dict):
            raise ValueError("results.json must be a JSON object")
        job_sha = sha256_file(job_dir / "job.json")
        if results.get("job_sha256") != job_sha:
            raise ValueError("results.json job_sha256 does not match this job's job.json")
        verify_members(members, results)
        check_env(members)
        restarts = check_restarts(results, settings, fit)
        cross_check(members, restarts)
        ok = [r for r in restarts if r["status"] == "ok"]
        if not ok:
            raise ValueError("zero successful restarts: nothing to import "
                             f"({len(restarts)} restart(s), all failed)")
        best = min(ok, key=lambda r: (r["score"], r["restart"]))
        if best["score"] >= PENALTY:
            raise ValueError(f"the best score {best['score']!r} is the penalty value ({PENALTY:g}): "
                             "no restart found a meaningful fit")
        params_file = tmp / "best-params.json"
        params_file.write_text(json.dumps(best["params"], indent=2) + "\n", encoding="utf-8")
        outcome = reproduce(root, job_id, params_file)
        if not isinstance(outcome, tuple) or len(outcome) != 2:
            raise ValueError("the reproduce step must return (score, nominal values)")
        reproduced, nominal = outcome
        if not finite_number(reproduced):
            raise ValueError("the reproduced score is not a finite number")
        rtol = settings["reproduce_rtol"]
        scale = abs(best["score"]) or 1.0
        if abs(reproduced - best["score"]) > rtol * scale:
            raise ValueError(f"reproduced score {reproduced!r} differs from the reported best {best['score']!r} "
                             f"by more than reproduce_rtol {rtol} (relative)")
        limit = best["score"] * (1 + settings["good_fit_margin"])
        good = [r["restart"] for r in ok if r["score"] <= limit]
        good_rows = [r for r in ok if r["restart"] in good]
        model = mapping["inputs"]["model"]
        figures = sorted(n for n in members if n.startswith("figures/"))
        proposal = {
            "kind": "calibration_proposal", "schema_version": 1, "job_id": job_id, "job_sha256": job_sha,
            "results_sha256": sha256_file(archive),
            "mapping": dict(job["mapping"]),
            "model": {"ode_session": model["ode_session"], "revision": model["revision"],
                      "bundle_sha256": job["export_sha256"]},
            "restarts": [{k: r.get(k) for k in ("restart", "seed", "score", "status", "wall_time_s")}
                         for r in restarts],
            "partial": bool(results.get("partial")) or len(ok) < settings["restarts"],
            "best_restart": best["restart"], "good_restarts": good,
            "parameters": _stats(fit, good_rows, good, best, _start_values(nominal, [f["name"] for f in fit])),
            "heuristics_note": HEURISTICS, "figures": figures,
        }
        text = json.dumps(proposal, indent=2, allow_nan=False) + "\n"
        outputs = {job_dir / "calibration_proposal.json": text.encode(),
                   job_dir / "report.md": report_md(proposal).encode()}
        outputs.update({job_dir / n: members[n].read_bytes() for n in figures})
        for path in outputs:  # check every target before writing any
            if path.exists() and not (path.is_file() and path.read_bytes() == outputs[path]):
                raise ValueError(f"{path.relative_to(job_dir).as_posix()} already exists with different content; "
                                 "it is never overwritten")
        for path, data in outputs.items():
            _write_new(path, data)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return job_dir / "calibration_proposal.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("job_id")
    args = parser.parse_args(argv)
    root = project_root().resolve()
    try:
        path = import_results(root, args.job_id, reproduce=default_reproduce)
    except (ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as exc:
        print(f"error: the local reproduce step failed: {exc} (bootstrapping the job environment in the cache "
              "needs internet; see launch.log / env_report.json in the cache job folder); nothing was written",
              file=sys.stderr)
        return 1
    print(f"proposal: {path.relative_to(root).as_posix()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
