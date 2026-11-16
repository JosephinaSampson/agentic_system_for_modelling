"""Dispatch lineage of a multicellular-configurator session: which approved file its run was dispatched with.

Every configurator run is a new PhysiCell session (record_session_id is only a lineage anchor), so a capture is
bound to the stage-A design or the stage-B projection it was built from by its invocation record:
runs/multicellular-configurator/<session>/invocation/<id>/task.txt keeps the dispatched brief, whose line 2 is the
DATA_INPUT line the dispatch hook verified. A recorded brief is lineage only when it is one the hook could have
passed (the Codex runtime, for one, never runs check_data_input): line 1 is a well-formed STAGE_GATE, line 2's
approval is a real approval of the matching stage binding exactly this path and sha256
(approvals.require_physicell_design_approval / require_physicell_projection_approval), and its record_session_id is
that approval's session_id. Stdlib only.
"""
from __future__ import annotations

from pathlib import Path

from scripts import gate, state
from scripts.data import approvals
from scripts.data.common import reject_symlinks, strict_json
from scripts.physicell import settings_xml as sx

PREFIX = "DATA_INPUT "
FORMS = {"design": {"design", "sha256", "approval", "record_session_id"},
         "projection": {"projection", "sha256", "approval", "record_session_id"}}
REQUIRE = {"design": approvals.require_physicell_design_approval,
           "projection": approvals.require_physicell_projection_approval}


def _verified(root: Path, key: str, rel: str, sha256: str, first_line: str, value: dict) -> None:
    """ValueError unless the recorded brief is one the dispatch hook could have passed (see the module docstring)."""
    gate.stage_gate_form(first_line)
    record = REQUIRE[key](root, value["approval"], rel, sha256)  # runs validate_record
    if value["record_session_id"] != record.get("session_id"):
        raise ValueError(f"its record_session_id {value['record_session_id']!r} is not the session_id "
                         f"{record.get('session_id')!r} of approval {value['approval']}")


def dispatched_with(root: Path, session: str, key: str, rel: str, sha256: str, invocation_id: str | None = None) -> str:
    """The rel path of the first runs/multicellular-configurator/<session>/invocation/*/task.txt (only
    <invocation_id>/task.txt when given) whose line 2 is a well-formed `key` (design or projection) DATA_INPUT line
    naming exactly `rel` and `sha256`, whose line 1 is a well-formed STAGE_GATE, whose approval is a real approval of
    that stage binding exactly `rel` and `sha256` and whose record_session_id is the approval's session_id;
    ValueError if none (naming the records refused and why). `session` and `invocation_id` must be identifiers."""
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
    other_sha, unverified = [], []
    for task in sorted(folder.glob(pattern)) if folder.is_dir() else []:
        reject_symlinks(task, root)
        lines = task.read_text(encoding="utf-8", errors="replace").splitlines()
        if len(lines) < 2 or not lines[1].startswith(PREFIX):
            continue
        try:
            value = strict_json(lines[1][len(PREFIX):].encode("utf-8"))
        except ValueError:
            continue
        if not isinstance(value, dict) or set(value) != FORMS[key] or value.get(key) != rel:
            continue
        task_rel = task.relative_to(root).as_posix()
        if value.get("sha256") != sha256:
            other_sha.append(f"{task_rel} (sha256 {value.get('sha256')})")
            continue
        try:
            _verified(root, key, rel, sha256, lines[0], value)
        except (ValueError, OSError, TypeError) as exc:
            unverified.append(f"{task_rel} ({exc})")
            continue
        return task_rel
    hint = f"; records naming it with another sha256: {', '.join(other_sha)}" if other_sha else ""
    if unverified:
        hint += ("; records naming it that no verified dispatch could have produced (STAGE_GATE, approval, "
                 f"record_session_id): {'; '.join(unverified)}")
    where = f"{folder_rel}/task.txt" if invocation_id is not None else f"{folder_rel}/*/task.txt"
    raise ValueError(f"lineage: no invocation record {where} has as its line 2 the DATA_INPUT line naming {key} "
                     f"{rel} with sha256 {sha256}{hint}")
