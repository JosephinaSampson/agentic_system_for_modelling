"""Resolve researcher approvals recorded in DECISIONS.md for data sessions, mappings and seeds files.

`scripts.state` is imported lazily: state.py imports this module, and it must also
keep working when run as a script.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from scripts.data.common import RUNS_ROOT, bound_bytes, json_object


def _state():
    from scripts import state
    return state


def load_record(root: Path, record_id: str) -> dict:
    state = _state()
    root = Path(root)
    text = (root / "DECISIONS.md").read_text(encoding="utf-8")
    for record in state.records(text):
        if record["id"] == record_id:
            return record
    raise ValueError("decision_id not found in DECISIONS.md governance records")


def _stage_approval(record: dict, record_id: str, stage: str) -> None:
    if record.get("kind") != "stage_approval":
        raise ValueError(f"decision {record_id} must be a stage_approval, not {record.get('kind')!r}")
    if record.get("stage") != stage:
        raise ValueError(f"decision {record_id} must approve stage {stage}, not {record.get('stage')!r}")


def _read_json(path: Path, rel: str):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise ValueError(f"cannot read {rel}: {exc}") from exc


def require_data_approval(root: Path, record_id: str, session_id: str) -> dict:
    state = _state()
    root = Path(root)
    record = load_record(root, record_id)
    _stage_approval(record, record_id, "data")
    if record.get("session_id") != session_id:
        raise ValueError(f"decision {record_id} approves session {record.get('session_id')!r}, not {session_id}")
    handoff_rel = f"{(RUNS_ROOT / session_id).as_posix()}/handoff.json"
    approved = record.get("artifact")
    if not isinstance(approved, dict) or approved.get("path") != handoff_rel:
        raise ValueError(f"decision {record_id} artifact must be {handoff_rel}")
    if state.digest(state.artifact(root, handoff_rel)) != approved.get("sha256"):
        raise ValueError(f"{handoff_rel} changed since approval {record_id} (sha256 mismatch)")
    return record


def handoff_artifacts(root: Path, session_id: str) -> set:
    """The (path, sha256) pairs listed by the session's handoff.json."""
    state = _state()
    handoff_rel = f"{(RUNS_ROOT / session_id).as_posix()}/handoff.json"
    handoff = _read_json(state.artifact(Path(root), handoff_rel), handoff_rel)
    artifacts = handoff.get("artifacts") if isinstance(handoff, dict) else None
    if not isinstance(artifacts, list) or handoff.get("session_id") != session_id:
        raise ValueError(f"{handoff_rel} is not a handoff of session {session_id}")
    return {(a.get("path"), a.get("sha256")) for a in artifacts if isinstance(a, dict)}


def require_in_handoff(root: Path, session_id: str, path: str, sha256: str, listed: set | None = None) -> set:
    listed = handoff_artifacts(root, session_id) if listed is None else listed
    if (path, sha256) not in listed:
        handoff_rel = f"{(RUNS_ROOT / session_id).as_posix()}/handoff.json"
        raise ValueError(f"{path} (sha256 {sha256[:12]}) is not an artifact of the approved handoff {handoff_rel}")
    return listed


def proposal_index(root: Path, session_id: str, listed: set) -> tuple[str, list]:
    state = _state()
    index_rel = f"{(RUNS_ROOT / session_id).as_posix()}/proposals/index.json"
    index_path = state.artifact(Path(root), index_rel)
    if (index_rel, state.digest(index_path)) not in listed:
        raise ValueError(f"{index_rel} differs from the approved handoff")
    index = _read_json(index_path, index_rel)
    return index_rel, index if isinstance(index, list) else []


def proposal_entry_for_path(root: Path, session_id: str, path: str, listed: set) -> dict:
    base = (RUNS_ROOT / session_id).as_posix()
    index_rel, index = proposal_index(root, session_id, listed)
    rel = path[len(base) + 1:]
    found = [p for p in index if isinstance(p, dict) and p.get("path") == rel]
    if len(found) != 1 or not isinstance(found[0].get("id"), str):
        raise ValueError(f"{path} is not exactly one proposal in {index_rel}")
    return found[0]


def require_proposal_in_outputs(record: dict, proposal_id: str) -> None:
    outputs = record.get("outputs")
    if not isinstance(outputs, list) or proposal_id not in outputs:
        raise ValueError(f"proposal {proposal_id} is not in the approved outputs of decision {record.get('id')}")


def require_proposal_approved(root: Path, record: dict, session_id: str, proposal_id: str) -> dict:
    state = _state()
    root = Path(root)
    base = (RUNS_ROOT / session_id).as_posix()
    # Never trust the caller's record: re-resolve it from DECISIONS.md and re-check it.
    record = require_data_approval(root, record.get("id"), session_id)
    require_proposal_in_outputs(record, proposal_id)
    listed = handoff_artifacts(root, session_id)
    index_rel, index = proposal_index(root, session_id, listed)
    found = [p for p in index if isinstance(p, dict) and p.get("id") == proposal_id]
    if len(found) != 1 or not isinstance(found[0].get("path"), str):
        raise ValueError(f"proposal {proposal_id} is not exactly one proposal in {index_rel}")
    entry = dict(found[0])
    rel = f"{base}/{entry['path']}"
    digest = state.digest(state.artifact(root, rel))
    require_in_handoff(root, session_id, rel, digest, listed)
    entry["path"] = rel
    entry["sha256"] = digest
    return entry


def _artifact_approval(root: Path, record_id: str, stage: str, rel: str, sha256: str) -> tuple[dict, bytes]:
    """(the `stage` stage_approval of exactly this artifact path and sha256, the artifact's bytes): read once, and
    those bytes hash to sha256 (unchanged on disk), so a caller can use them without reading the file again."""
    state = _state()
    root = Path(root)
    record = load_record(root, record_id)
    _stage_approval(record, record_id, stage)
    approved = record.get("artifact")
    if not isinstance(approved, dict) or approved.get("path") != rel:
        raise ValueError(f"decision {record_id} artifact must be {rel}")
    if approved.get("sha256") != sha256:
        raise ValueError(f"decision {record_id} approves a different sha256 than {sha256}")
    return record, bound_bytes(state.artifact(root, rel), sha256, rel, f"approval {record_id}")


def _require_artifact_approval(root: Path, record_id: str, stage: str, rel: str, sha256: str) -> dict:
    """The `stage` stage_approval of exactly this artifact path and sha256, unchanged on disk."""
    return _artifact_approval(root, record_id, stage, rel, sha256)[0]


def require_mapping_approval(root: Path, record_id: str, mapping_rel: str, sha256: str) -> dict:
    return _require_artifact_approval(root, record_id, "data_mapping", mapping_rel, sha256)


def require_seeds_approval(root: Path, record_id: str, seeds_rel: str, sha256: str) -> dict:
    """The data_seeds stage_approval of exactly this seeds.json path and sha256."""
    return _require_artifact_approval(root, record_id, "data_seeds", seeds_rel, sha256)


def _ode_mapping_outputs(rel: str, raw: bytes) -> tuple[str | None, set]:
    """(ODE session, measurement ids used by rules + fitted names) of an ode_mapping.json, tolerant of shape, from
    the bytes _artifact_approval read and hashed (the file is not read again)."""
    try:
        data = json.loads(raw.decode("utf-8"))
    except ValueError as exc:
        raise ValueError(f"cannot read {rel}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{rel} must be a JSON object")
    model = (data.get("inputs") or {}).get("model") if isinstance(data.get("inputs"), dict) else None
    session = model.get("ode_session") if isinstance(model, dict) else None
    names = set()
    for rule in data.get("rules") if isinstance(data.get("rules"), list) else ():
        ids = rule.get("measurements") if isinstance(rule, dict) else None
        if isinstance(ids, list):
            names.update(i for i in ids if isinstance(i, str))
    for item in data.get("fit") if isinstance(data.get("fit"), list) else ():
        if isinstance(item, dict) and isinstance(item.get("name"), str):
            names.add(item["name"])
    return session, names


def require_ode_mapping_approval(root: Path, record_id: str, rel: str, sha256: str) -> dict:
    """The ode_mapping stage_approval of exactly this ode_mapping.json path and sha256.

    The record must pass `state.validate_record` (researcher authorization, outputs); session_id is the ODE
    session of the mapping and outputs are exactly the measurement ids the rules use plus the fitted names.

    This binds the approval to the mapping bytes; it does not validate the mapping's content. Run
    `scripts/ode/ode_mapping.py check` and have it pass before the researcher approves; `scripts/jobs/build.py`
    runs the full check again before it builds a job (and job.json binds that mapping's sha256). The
    ode-modeler gate does not re-run the check: it requires the job.json beside the calibration proposal to
    name this same mapping path, sha256 and approval, and calls this function.
    """
    record, raw = _artifact_approval(root, record_id, "ode_mapping", rel, sha256)
    _state().validate_record(Path(root), record)
    session, expected = _ode_mapping_outputs(rel, raw)
    if record.get("session_id") != session:
        raise ValueError(f"decision {record_id} approves session {record.get('session_id')!r}, "
                         f"but the mapping binds ODE session {session!r}")
    outputs = record.get("outputs")
    if not isinstance(outputs, list) or len(outputs) != len(set(outputs)) or set(outputs) != expected:
        raise ValueError(f"decision {record_id} outputs must equal the measurement ids used by rules plus "
                         f"the fitted parameter names: {sorted(expected)}")
    return record


def require_calibration_approval(root: Path, record_id: str, rel: str, sha256: str) -> tuple[dict, dict]:
    """(record, proposal): the ode_calibration stage_approval of exactly this calibration_proposal.json.

    Every check of a calibration approval, shared by the ode-modeler gate and score_holdout:
      * a stage_approval of stage ode_calibration whose artifact is exactly `rel` at `sha256`, and the file
        still has that sha256;
      * the record passes `state.validate_record` (researcher authorization, unique nonempty outputs);
      * the proposal (parsed from the same bytes whose sha256 was checked) is a calibration_proposal with an
        ODE session and parameter names;
      * outputs (the accepted parameters) are a nonempty list of unique proposal parameter names;
      * session_id is the proposal's ODE session;
      * the proposal belongs to its job (require_proposal_job).
    """
    from scripts.data.common import strict_json
    state = _state()
    root = Path(root)
    record = load_record(root, record_id)
    _stage_approval(record, record_id, "ode_calibration")
    approved = record.get("artifact")
    if not isinstance(approved, dict) or approved.get("path") != rel:
        raise ValueError(f"decision {record_id} artifact must be {rel}")
    if approved.get("sha256") != sha256:
        raise ValueError(f"decision {record_id} approves a different sha256 than {sha256}")
    state.validate_record(root, record)
    # one read: the hash and the parsed proposal agree
    raw = bound_bytes(state.artifact(root, rel), sha256, rel, f"approval {record_id}")
    try:
        proposal = strict_json(raw)
    except (ValueError, RecursionError) as exc:
        raise ValueError(f"cannot read {rel}: {exc}") from None
    if not isinstance(proposal, dict) or proposal.get("kind") != "calibration_proposal":
        raise ValueError(f"{rel} is not a calibration_proposal")
    model = proposal.get("model")
    session = model.get("ode_session") if isinstance(model, dict) else None
    rows = proposal.get("parameters")
    names = [r.get("name") for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []
    if not isinstance(session, str) or not session or not names \
            or not all(isinstance(n, str) and n for n in names) or len(names) != len(set(names)):
        raise ValueError(f"{rel} names no ODE session or no (unique) parameter names")
    outputs = record.get("outputs")
    if (not isinstance(outputs, list) or not outputs or len(outputs) != len(set(outputs))
            or not all(isinstance(o, str) for o in outputs) or not set(outputs) <= set(names)):
        raise ValueError(f"calibration approval {record_id} outputs must be a nonempty list of unique parameter "
                         f"names of the proposal ({', '.join(sorted(names))}); got {outputs!r}")
    if record.get("session_id") != session:
        raise ValueError(f"calibration approval {record_id} has session_id {record.get('session_id')!r}; "
                         f"it must be the proposal's ODE session {session}")
    require_proposal_job(root, rel, proposal)
    return record, proposal


def require_proposal_job(root: Path, rel: str, proposal: dict) -> dict:
    """The job.json beside a calibration proposal, after checking that the proposal belongs to that job.

      * the proposal's mapping {path, sha256, approval} is an approved ode_mapping
        (require_ode_mapping_approval, incl. validate_record) of the proposal's ODE session;
      * the job.json beside it (a regular file, no symlink) has sha256 == proposal.job_sha256;
      * job.json job_id == proposal job_id == the folder name, and job.json mapping == proposal mapping.
    """
    from scripts.data.common import strict_json
    root = Path(root)
    session = proposal["model"]["ode_session"]
    ref = proposal.get("mapping")
    if not isinstance(ref, dict) or set(ref) != {"path", "sha256", "approval"} \
            or not all(isinstance(ref[k], str) and ref[k] for k in ref):
        raise ValueError(f"{rel} has no mapping {{path, sha256, approval}}")
    try:
        mapping_record = require_ode_mapping_approval(root, ref["approval"], ref["path"], ref["sha256"])
    except ValueError as exc:
        raise ValueError(f"{rel}: its mapping {ref['path']} (approval {ref['approval']}) is not an approved "
                         f"ode_mapping: {exc}") from None
    if mapping_record.get("session_id") != session:
        raise ValueError(f"{rel}: mapping approval {ref['approval']} is for ODE session "
                         f"{mapping_record.get('session_id')!r}, not the proposal's {session}")
    folder = (root / rel).parent
    job_json = folder / "job.json"
    job_raw = job_json.read_bytes() if job_json.is_file() and not job_json.is_symlink() else None
    if job_raw is None or hashlib.sha256(job_raw).hexdigest() != proposal.get("job_sha256"):
        raise ValueError(f"{rel}: job_sha256 does not match the job.json beside it")
    try:
        job = strict_json(job_raw)
    except (ValueError, RecursionError) as exc:
        raise ValueError(f"cannot read {folder.name}/job.json: {exc}") from None
    if not isinstance(job, dict):
        raise ValueError(f"{folder.name}/job.json must be a JSON object")
    if job.get("job_id") != folder.name or proposal.get("job_id") != folder.name:
        raise ValueError(f"{rel}: the job_id of the proposal ({proposal.get('job_id')!r}) and of its job.json "
                         f"({job.get('job_id')!r}) must both be its job folder name {folder.name}")
    if job.get("mapping") != ref:
        raise ValueError(f"{rel}: its mapping {ref} is not the mapping of the job.json beside it "
                         f"({job.get('mapping')!r})")
    return job


def _physicell_design_facts(rel: str, data: dict) -> tuple[str | None, list]:
    """(session anchor, cell definition and substrate names) of a physicell_design.json, tolerant of shape: the
    basis capture's session, else the purpose <p> of runs/orchestrator/<p>/... (a lineage anchor)."""
    basis = data.get("basis")
    capture = basis.get("capture") if isinstance(basis, dict) else None
    parts = capture.split("/") if isinstance(capture, str) else []
    anchor = rel.split("/") if isinstance(rel, str) else []
    purpose = anchor[2] if len(anchor) == 4 and anchor[:2] == ["runs", "orchestrator"] else None
    session = parts[2] if len(parts) == 5 else (purpose if basis is None else None)
    names = []
    for key in ("cell_definitions", "substrates"):
        for item in data.get(key) if isinstance(data.get(key), list) else ():
            if isinstance(item, dict) and isinstance(item.get("name"), str):
                names.append(item["name"])
    return session, names


def _physicell_projection_facts(rel: str, data: dict) -> tuple[str | None, list]:
    """(PhysiCell session of the projection's capture, entry ids) of a physicell_projection.json, tolerant of
    shape."""
    capture = data.get("capture")
    path = capture.get("path") if isinstance(capture, dict) else None
    parts = path.split("/") if isinstance(path, str) else []
    session = parts[2] if len(parts) == 5 else None
    entries = data.get("entries") if isinstance(data.get("entries"), list) else []
    ids = [e["entry_id"] for e in entries if isinstance(e, dict) and isinstance(e.get("entry_id"), str)]
    return session, ids


def _require_physicell_approval(root: Path, record_id: str, rel: str, sha256: str, stage: str, facts_fn,
                                what: tuple[str, str]) -> dict:
    """The `stage` stage_approval of exactly this path and sha256, validated (state.validate_record), whose
    session_id is the file's session and whose outputs are exactly its names. The file is read and hashed once
    (_artifact_approval): those bytes bind the approval and `facts_fn(rel, data)` takes (session, names) from them.
    `what` words the refusals: (how the file's session is named, what the outputs must equal)."""
    session_words, outputs_words = what
    record, raw = _artifact_approval(root, record_id, stage, rel, sha256)
    _state().validate_record(Path(root), record)
    session, names = facts_fn(rel, json_object(raw, rel))
    if record.get("session_id") != session:
        raise ValueError(f"decision {record_id} approves session {record.get('session_id')!r}, "
                         f"but {session_words} {session!r}")
    outputs = record.get("outputs")
    if not isinstance(outputs, list) or len(outputs) != len(set(outputs)) or set(outputs) != set(names) \
            or len(names) != len(set(names)):
        raise ValueError(f"decision {record_id} outputs must equal {outputs_words}: {sorted(set(names))}")
    return record


def require_physicell_design_approval(root: Path, record_id: str, rel: str, sha256: str) -> dict:
    """The physicell_design stage_approval of exactly this physicell_design.json path and sha256.

    The record must pass `state.validate_record`; session_id is the PhysiCell session of the design's basis
    capture (for a new configuration, the design's purpose <p>: a lineage anchor) and outputs are exactly the cell
    definition and substrate names. This binds the approval to the bytes; run
    `scripts/physicell/physicell_design.py check` and have it pass before the researcher approves.
    """
    return _require_physicell_approval(
        root, record_id, rel, sha256, "physicell_design", _physicell_design_facts,
        ("the design's session anchor is", "the cell definition and substrate names of the design"))


def require_physicell_projection_approval(root: Path, record_id: str, rel: str, sha256: str) -> dict:
    """The physicell_projection stage_approval of exactly this physicell_projection.json path and sha256.

    The record must pass `state.validate_record`; session_id is the PhysiCell session of the projection's capture
    and outputs are exactly the entry ids. This binds the approval to the bytes; run
    `scripts/physicell/physicell_projection.py check` and have it pass before the researcher approves.
    """
    return _require_physicell_approval(
        root, record_id, rel, sha256, "physicell_projection", _physicell_projection_facts,
        ("the projection's capture is of PhysiCell session", "the entry ids of the projection"))
