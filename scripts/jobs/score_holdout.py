#!/usr/bin/env python3
"""One-time hold-out scoring of an approved calibration.

  python3 scripts/jobs/score_holdout.py <job-id> --approval <ode_calibration record id>

Scores the held-out measurements (use=holdout) with the parameters the researcher accepted at their best
values and every other fitted parameter at its start (nominal) value, and writes holdout_score.json beside
the job's calibration_proposal.json. It is written once and never overwritten: a second look at the
hold-out would no longer be a hold-out.

  * the approval must be an `ode_calibration` stage_approval bound to the proposal's path and sha256;
    its `outputs` (the accepted parameters) must be a nonempty subset of the proposal's parameter names;
  * a proposal `start` of null means the nominal value is unknown: a non-accepted parameter with a null
    start refuses the scoring (the bundle's model defaults are not read; no value is ever assumed to be 0);
  * `runner(root, job_id, params_file) -> float` is injectable. The default runs
    `launch.py local validate <job-id> --use holdout --params <file> --out <file>` and reads its score.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import tempfile
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.data import approvals  # noqa: E402
from scripts.data.common import project_root, sha256_file, strict_json  # noqa: E402
from scripts.jobs import launch  # noqa: E402


def _number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def default_runner(root: Path, job_id: str, params_file: Path) -> float:
    out = Path(params_file).with_name("holdout-report.json")
    code = launch.validate(root, job_id, str(params_file), "holdout", str(out))
    if code != 0:
        raise ValueError(f"launch.py local validate --use holdout failed (exit {code})")
    score = strict_json(out.read_bytes()).get("score")
    if not _number(score):
        raise ValueError("the hold-out score is not a finite number")
    return float(score)


def score_holdout(root, job_id: str, approval_id: str, *, runner=None) -> Path:
    root = Path(root)
    runner = runner or default_runner
    job_dir = launch.find_job(root, job_id)
    target = job_dir / "holdout_score.json"
    if target.exists():
        raise ValueError(f"{target.relative_to(root).as_posix()} exists; the hold-out is scored once and "
                         "never overwritten")
    proposal_path = job_dir / "calibration_proposal.json"
    if not proposal_path.is_file():
        raise ValueError(f"no calibration_proposal.json for job {job_id}; import the results first")
    rel = proposal_path.relative_to(root).as_posix()
    digest = sha256_file(proposal_path)
    record = approvals.require_calibration_approval(root, approval_id, rel, digest)
    proposal = strict_json(proposal_path.read_bytes())
    rows = {r["name"]: r for r in proposal["parameters"]}
    accepted = record.get("outputs")
    if (not isinstance(accepted, list) or not accepted or len(accepted) != len(set(accepted))
            or not all(isinstance(a, str) and a in rows for a in accepted)):
        raise ValueError(f"approval {approval_id} outputs must be a nonempty list of the proposal's parameter "
                         f"names ({', '.join(rows)})")
    if record.get("session_id") != proposal["model"]["ode_session"]:
        raise ValueError(f"approval {approval_id} session_id is not the proposal's ODE session")
    params, source = {}, {}
    for name, row in rows.items():
        if name in accepted:
            params[name], source[name] = row["best"], "accepted best"
        elif _number(row.get("start")):
            params[name], source[name] = row["start"], "start"
        else:
            raise ValueError(f"parameter {name} is not accepted and its start value is unknown (null); the "
                             "hold-out would need a value the data do not give: accept it too, or "
                             "re-run the calibration with the model's nominal values recorded")
    tmp = Path(tempfile.mkdtemp(prefix=".holdout-", dir=job_dir))  # not /tmp: a small RAM tmpfs
    try:
        params_file = tmp / "holdout-params.json"
        params_file.write_text(json.dumps(params, indent=2) + "\n", encoding="utf-8")
        score = runner(root, job_id, params_file)
    finally:
        for child in tmp.iterdir():
            child.unlink()
        tmp.rmdir()
    if not _number(score):
        raise ValueError("the hold-out score is not a finite number")
    result = {"kind": "holdout_score", "schema_version": 1, "job_id": job_id,
              "proposal": {"path": rel, "sha256": digest}, "approval": approval_id,
              "accepted": list(accepted), "parameters": params, "parameter_source": source,
              "use": "holdout", "score": float(score),
              "note": "Scored once on the held-out measurements; accepted parameters at their best values, "
                      "all others at their start values."}
    data = (json.dumps(result, indent=2, allow_nan=False) + "\n").encode()
    try:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        raise ValueError(f"{target.relative_to(root).as_posix()} exists; the hold-out is scored once") from None
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("job_id")
    parser.add_argument("--approval", required=True, help="the ode_calibration stage_approval record id")
    args = parser.parse_args(argv)
    root = project_root().resolve()
    try:
        path = score_holdout(root, args.job_id, args.approval)
    except (ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"holdout score: {path.relative_to(root).as_posix()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
