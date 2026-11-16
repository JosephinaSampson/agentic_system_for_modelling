"""Dispatch lineage of a multicellular-configurator session: which approved file its run was dispatched with.

Every configurator run is a new PhysiCell session (record_session_id is only a lineage anchor), so a capture is
bound to the stage-A design or the stage-B projection it was built from by its invocation record:
runs/multicellular-configurator/<session>/invocation/<id>/task.txt keeps the dispatched brief, whose line 2 is the
DATA_INPUT line the dispatch hook verified. Stdlib only.
"""
from __future__ import annotations

from pathlib import Path

from scripts.data.common import reject_symlinks, strict_json
from scripts.physicell import settings_xml as sx

PREFIX = "DATA_INPUT "
FORMS = {"design": {"design", "sha256", "approval", "record_session_id"},
         "projection": {"projection", "sha256", "approval", "record_session_id"}}


def dispatched_with(root: Path, session: str, key: str, rel: str, sha256: str) -> str:
    """The rel path of the first runs/multicellular-configurator/<session>/invocation/*/task.txt whose line 2 is a
    well-formed `key` (design or projection) DATA_INPUT line naming exactly `rel` and `sha256`; ValueError if none."""
    if key not in FORMS:
        raise ValueError(f"lineage key must be one of {', '.join(FORMS)}, not {key!r}")
    root = Path(root)
    folder_rel = f"runs/{sx.SPECIALIST}/{session}/invocation"
    folder = root / folder_rel
    reject_symlinks(folder, root)
    other_sha = []
    for task in sorted(folder.glob("*/task.txt")) if folder.is_dir() else []:
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
        if value.get("sha256") == sha256:
            return task_rel
        other_sha.append(f"{task_rel} (sha256 {value.get('sha256')})")
    hint = f"; records naming it with another sha256: {', '.join(other_sha)}" if other_sha else ""
    raise ValueError(f"lineage: no invocation record {folder_rel}/*/task.txt has as its line 2 the DATA_INPUT line "
                     f"naming {key} {rel} with sha256 {sha256}{hint}")
