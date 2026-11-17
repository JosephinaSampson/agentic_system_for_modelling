"""Dispatch lineage of a multicellular-configurator session: which approved file its run was dispatched with.

Every configurator run is a new PhysiCell session (record_session_id is only a lineage anchor), so a capture is
bound to the stage-A design or the stage-B projection it was built from by its invocation record
runs/multicellular-configurator/<session>/invocation/<id>/task.txt. The rule (which recorded brief is lineage) is
stated once, in docs/data-to-physicell-architecture.md under "Dispatch lineage"; dispatched_with reads the records
and _verified checks one brief against durable state only, never the live registry. Stdlib only.
"""
from __future__ import annotations

from pathlib import Path

from scripts import gate, state
from scripts.data import approvals
from scripts.data.common import reject_symlinks, strict_json
from scripts.physicell import physicell_design as pd
from scripts.physicell import settings_xml as sx

PREFIX = "DATA_INPUT "
FORMS = gate.PHYSICELL_FORMS  # the multicellular-configurator DATA_INPUT forms the dispatch hook verifies
REQUIRE = {"design": approvals.require_physicell_design_approval,
           "projection": approvals.require_physicell_projection_approval}


def _verified(root: Path, key: str, rel: str, sha256: str, first_line: str, value: dict, design: dict) -> None:
    """ValueError unless the recorded brief is one the dispatch hook could have passed (see the module docstring)."""
    stage_gate = gate.stage_gate_form(first_line)
    gate.design_upstream(design, stage_gate)
    if "standalone_decision_id" in stage_gate:
        gate.standalone(root, "multicellular", stage_gate["standalone_decision_id"], stage_gate["outputs"])
    else:
        # the outputs of the approval each design handoff was verified against (durable: DECISIONS.md is
        # append-only); never the live gate.check, so a later supersession does not invalidate a past build
        recs = state.records((root / "DECISIONS.md").read_text(encoding="utf-8"))
        for item in design["intracellular"]:
            approval = pd.handoff_approval(recs, item["maboss"])
            if approval["outputs"] != stage_gate["outputs"]:
                raise ValueError(f"STAGE_GATE outputs {stage_gate['outputs']} are not the outputs {approval['outputs']} "
                                 f"of approval {approval.get('id')} of the design's MaBoSS handoff "
                                 f"{item['maboss']['handoff']}")
    record = REQUIRE[key](root, value["approval"], rel, sha256)  # runs validate_record
    if value["record_session_id"] != record.get("session_id"):
        raise ValueError(f"its record_session_id {value['record_session_id']!r} is not the session_id "
                         f"{record.get('session_id')!r} of approval {value['approval']}")


def dispatched_with(root: Path, session: str, key: str, rel: str, sha256: str, invocation_id: str | None = None, *,
                    design: dict) -> str:
    """The rel path of the first runs/multicellular-configurator/<session>/invocation/*/task.txt (only
    <invocation_id>/task.txt when given) whose line 2 is a well-formed `key` (design or projection) DATA_INPUT line
    naming exactly `rel` and `sha256`, whose line 1 is a well-formed STAGE_GATE bound to `design`, whose approval is
    a real approval of that stage binding exactly `rel` and `sha256` and whose record_session_id is the approval's
    session_id (the module docstring); ValueError if none, naming each record refused and why (no task.txt; line 2
    not a DATA_INPUT line, not strict JSON or not the `key` form; another file or sha256; a refused STAGE_GATE,
    binding, approval or record_session_id). `design` is the
    checked design: the file `rel` itself for key "design", the projection's design for key "projection", as the
    caller's check loaded it. `session` and `invocation_id` must be identifiers."""
    if key not in FORMS:
        raise ValueError(f"lineage key must be one of {', '.join(FORMS)}, not {key!r}")
    try:
        state.identifier(session)
        if invocation_id is not None:
            state.identifier(invocation_id)
    except ValueError as exc:
        raise ValueError(f"lineage: session {session!r} / invocation {invocation_id!r}: {exc}") from None
    root = Path(root)
    folder_rel = f"runs/{sx.SPECIALIST}/{session}/invocation"
    folder = root / folder_rel
    reject_symlinks(folder, root)
    pattern = f"{invocation_id}/task.txt" if invocation_id is not None else "*/task.txt"
    if invocation_id is not None:
        folder_rel = f"{folder_rel}/{invocation_id}"
        reject_symlinks(folder / invocation_id, root)
    malformed, other_file, other_sha, unverified = [], [], [], []
    tasks = sorted(folder.glob(pattern)) if folder.is_dir() else []
    for task in tasks:
        reject_symlinks(task, root)
        task_rel = task.relative_to(root).as_posix()
        lines = task.read_text(encoding="utf-8", errors="replace").splitlines()
        if len(lines) < 2 or not lines[1].startswith(PREFIX):
            malformed.append(f"{task_rel} (line 2 is not a DATA_INPUT line)")
            continue
        try:
            value = strict_json(lines[1][len(PREFIX):].encode("utf-8"))
        except ValueError as exc:
            malformed.append(f"{task_rel} (line 2 is not a strict JSON DATA_INPUT object: {exc})")
            continue
        if not isinstance(value, dict) or set(value) != FORMS[key]:
            malformed.append(f"{task_rel} (line 2 is not the {key} DATA_INPUT form "
                             f"{{{', '.join(sorted(FORMS[key]))}}})")
            continue
        if value.get(key) != rel:
            other_file.append(f"{task_rel} ({key} {value.get(key)!r})")
            continue
        if value.get("sha256") != sha256:
            other_sha.append(f"{task_rel} (sha256 {value.get('sha256')})")
            continue
        try:
            _verified(root, key, rel, sha256, lines[0], value, design)
        except (ValueError, OSError, TypeError) as exc:
            unverified.append(f"{task_rel} ({exc})")
            continue
        return task_rel
    where = f"{folder_rel}/task.txt" if invocation_id is not None else f"{folder_rel}/*/task.txt"
    if not tasks:
        hint = f": {where} does not exist" if invocation_id is not None else f": there is no {where}"
    else:
        hint = ""
    if malformed:
        hint += f"; records whose line 2 is not a {key} DATA_INPUT line: {'; '.join(malformed)}"
    if other_file:
        hint += f"; records naming another {key}: {', '.join(other_file)}"
    if other_sha:
        hint += f"; records naming it with another sha256: {', '.join(other_sha)}"
    if unverified:
        hint += ("; records naming it that no verified dispatch could have produced (STAGE_GATE and its binding to "
                 f"the design, approval, record_session_id): {'; '.join(unverified)}")
    raise ValueError(f"lineage: no invocation record {where} has as its line 2 the DATA_INPUT line naming {key} "
                     f"{rel} with sha256 {sha256}{hint}")
