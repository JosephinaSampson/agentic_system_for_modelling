#!/usr/bin/env python3
"""One-time hold-out scoring of an approved calibration.

  python3 scripts/jobs/score_holdout.py <job-id> --approval <ode_calibration record id>

Scores the held-out measurements (use=holdout) with the parameters the researcher accepted at their best
values; no other value is passed, so every other parameter keeps the bundled model's default. Writes
holdout_score.json beside the job's calibration_proposal.json, once and never overwritten: a second look at
the hold-out would no longer be a hold-out.

  * the approval is checked by approvals.require_calibration_approval (the same checks as the ode-modeler
    gate): an `ode_calibration` stage_approval bound to the proposal's path and sha256, researcher-authorized
    (validate_record), whose `outputs` (the accepted parameters) are a nonempty subset of the proposal's
    parameter names and whose session_id is the proposal's ODE session;
  * `runner(root, job_id, params_file) -> {"score", "n_rows", "failed", "params"}` is injectable. The default
    runs `launch.py local validate <job-id> --use holdout --params <file> --partial --out <file>`: n_rows = the
    hold-out rows with a finite prediction that are not 1 by construction ("trivial"), failed = the hold-out
    rows (other than contrast references, which get none, and trivial rows) without one, params = the effective
    values (accepted + model defaults);
  * holdout_score.json appears only complete (temp file, fsync, os.link, which fails if it exists): a failed
    write leaves none, so the hold-out can still be scored once (the job folder's filesystem must support hard
    links); a job without the current runtime_version in job.json is refused ("rebuild this job");
  * nothing is written when no row received a prediction (an objective over no rows is 0.0), when any
    hold-out prediction failed (solver failure, NaN) or when the score is the penalty value: a failure is
    never recorded as the one-time hold-out score, so the hold-out can still be scored once it is fixed.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.data import approvals  # noqa: E402
from scripts.data.common import finite_number, project_root, sha256_file, strict_json  # noqa: E402
from scripts.jobs import launch  # noqa: E402
from scripts.jobs.runtime.compare import PENALTY  # noqa: E402


def predicted_rows(report: dict, use: str = "holdout") -> tuple[int, list[str]]:
    """(rows with a finite prediction, ids of rows without one) of a `run.py validate` report, for this use;
    contrast reference rows never get a prediction, and rows 1 by construction ("trivial": their residual does not
    depend on the parameters) carry no information: both are in neither."""
    rows = report.get("predictions") if isinstance(report, dict) else None
    if not isinstance(rows, dict):
        raise ValueError("the validate report has no predictions")
    scored = {mid: row for mid, row in rows.items()
              if isinstance(row, dict) and row.get("use") == use and row.get("role") != "reference"
              and row.get("trivial") is not True}
    failed = sorted(mid for mid, row in scored.items() if not finite_number(row.get("prediction")))
    return len(scored) - len(failed), failed


def default_runner(root: Path, job_id: str, params_file: Path) -> dict:
    out = Path(params_file).with_name("holdout-report.json")
    code = launch.validate(root, job_id, str(params_file), "holdout", str(out), partial=True)
    if code != 0:
        raise ValueError(f"launch.py local validate --use holdout failed (exit {code})")
    report = strict_json(out.read_bytes())
    score = report.get("score") if isinstance(report, dict) else None
    if not finite_number(score):
        raise ValueError("the hold-out score is not a finite number")
    n_rows, failed = predicted_rows(report)
    return {"score": float(score), "n_rows": n_rows, "failed": failed, "params": report.get("params")}


def score_holdout(root, job_id: str, approval_id: str, *, runner=None) -> Path:
    root = Path(root)
    runner = runner or default_runner
    job_dir = launch.find_job(root, job_id)
    launch.require_runtime_version(job_dir)
    target = job_dir / "holdout_score.json"
    if target.exists():
        raise ValueError(f"{target.relative_to(root).as_posix()} exists; the hold-out is scored once and "
                         "never overwritten")
    proposal_path = job_dir / "calibration_proposal.json"
    if not proposal_path.is_file():
        raise ValueError(f"no calibration_proposal.json for job {job_id}; import the results first")
    rel = proposal_path.relative_to(root).as_posix()
    digest = sha256_file(proposal_path)  # the approval check re-reads and parses exactly these bytes
    record, proposal = approvals.require_calibration_approval(root, approval_id, rel, digest)
    accepted = record["outputs"]
    rows = {r["name"]: r for r in proposal["parameters"]}
    params = {}
    for name in accepted:
        if not finite_number(rows[name].get("best")):
            raise ValueError(f"accepted parameter {name} has no finite best value in the proposal")
        params[name] = rows[name]["best"]
    tmp = Path(tempfile.mkdtemp(prefix=".holdout-", dir=job_dir))  # not /tmp: a small RAM tmpfs
    try:
        params_file = tmp / "holdout-params.json"
        params_file.write_text(json.dumps(params, indent=2) + "\n", encoding="utf-8")
        outcome = runner(root, job_id, params_file)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if not isinstance(outcome, dict):
        raise ValueError("the hold-out runner must return {score, n_rows, failed, params}")
    score, n_rows, failed = outcome.get("score"), outcome.get("n_rows"), outcome.get("failed")
    if not finite_number(score):
        raise ValueError("the hold-out score is not a finite number")
    if not isinstance(failed, list) or not all(isinstance(f, str) for f in failed):
        raise ValueError("the hold-out runner must list the failed hold-out rows")
    if failed:
        raise ValueError(f"{len(failed)} hold-out prediction(s) failed (solver failure or non-finite value): "
                         f"{', '.join(failed)}; nothing was written and the hold-out has not been scored")
    if score >= PENALTY:
        raise ValueError(f"the hold-out score {score!r} is the penalty value ({PENALTY:g}): a failed simulation, "
                         "not a hold-out score; nothing was written")
    if type(n_rows) is not int or n_rows < 1:
        raise ValueError(f"no informative hold-out row received a prediction (n_rows {n_rows!r}): the mapping has no "
                         "scored use=holdout measurement that is not 1 by construction; nothing was written")
    effective = outcome.get("params")
    result = {"kind": "holdout_score", "schema_version": 1, "job_id": job_id,
              "proposal": {"path": rel, "sha256": digest}, "approval": approval_id,
              "accepted": list(accepted), "parameters": params,
              "effective_parameters": effective if isinstance(effective, dict) else None,
              "use": "holdout", "score": float(score), "n_rows": n_rows,
              "note": "Scored once on the held-out measurements; accepted parameters at their best values, every "
                      "other parameter at the bundled model default (effective_parameters)."}
    data = (json.dumps(result, indent=2, allow_nan=False) + "\n").encode()
    _publish_once(root, target, data)
    return target


def _publish_once(root: Path, target: Path, data: bytes) -> None:
    """Write `data` to `target` only complete and only once: a temp file in the same folder is written and
    fsynced, then hard-linked to `target` (os.link fails if it exists), then unlinked. A failed write (disk full,
    I/O error, a kill) leaves no holdout_score.json, so the hold-out can still be scored once."""
    fd, tmp = tempfile.mkstemp(prefix=".holdout_score.", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        mask = os.umask(0)
        os.umask(mask)
        os.chmod(tmp, 0o666 & ~mask)  # mkstemp makes 0600
        try:
            os.link(tmp, target)
        except FileExistsError:
            raise ValueError(f"{target.relative_to(root).as_posix()} exists; the hold-out is scored once") from None
        except OSError as exc:
            raise ValueError(f"cannot create {target.relative_to(root).as_posix()}: the job folder's filesystem must "
                             f"support hard links for the one-time hold-out score ({exc}); nothing was written, so the "
                             "hold-out is still unscored: move the repository to such a filesystem") from None
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("job_id")
    parser.add_argument("--approval", required=True, help="the ode_calibration stage_approval record id")
    args = parser.parse_args(argv)
    root = project_root().resolve()
    try:
        path = score_holdout(root, args.job_id, args.approval)
    except (ValueError, OSError, KeyError, TypeError, subprocess.CalledProcessError) as exc:
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(f"holdout score: {path.relative_to(root).as_posix()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
