#!/usr/bin/env python3
"""Checked, atomic edits of the human-readable scientific sources of truth.

JSON inputs are files, never interpolated shell fragments. Governance records live
in DECISIONS.md itself; they record researcher authorization, not grant it.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import unicodedata
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
if __package__ in (None, ""):
    # Run as `python3 scripts/state.py`: make the `scripts` package importable.
    sys.path.insert(0, str(ROOT))
REGISTRY = ["Specialist", "Session ID", "Stage", "Status", "Derived from", "Handoff", "Artifacts"]
DECISIONS = ["Date", "Decision", "Alternatives considered", "Rationale", "Evidence", "Approved by"]
VALIDATION = ["Criterion", "Method", "Required evidence", "Status", "Result"]
DATA_DICTIONARY = ["Name", "Type", "Biological meaning", "Unit", "Source", "Version", "Notes"]
DATA_PRODUCT_KEYS = {"name", "type", "meaning", "unit", "identifiers", "source_session", "path", "sha256", "release", "decision_id", "notes"}
RECORD = re.compile(r"<!-- governance-record -->\s*```json\n(.*?)\n```", re.S)
ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
ROLES = {"network-curator": "topology", "boolean-dynamics-modeler": "boolean-dynamics", "multicellular-configurator": "multicellular", "ode-modeler": "biomass_ode", "data-analyst": "data"}

GOVERNANCE_STAGES = set(ROLES.values()) | {"data_mapping", "data_seeds", "ode_mapping", "ode_calibration",
                                           "physicell_design", "physicell_projection"}


def now():
    return datetime.now(timezone.utc).isoformat()


def identifier(value):
    if not isinstance(value, str) or not ID.fullmatch(value) or value in {".", ".."}:
        raise ValueError("invalid complete identifier")
    return value


def artifact(root, value):
    if not isinstance(value, str) or not value or Path(value).is_absolute():
        raise ValueError("artifact must be a repository-relative path")
    path = root / value
    if ".." in Path(value).parts or any(p.is_symlink() for p in [path, *path.parents] if p != root.parent):
        raise ValueError("artifact traversal/symlink is forbidden")
    path.resolve().relative_to(root.resolve())
    if not path.is_file():
        raise ValueError(f"missing artifact: {value}")
    return path


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def checked_artifact(root, item):
    if not isinstance(item, dict) or set(item) != {"path", "sha256"}:
        raise ValueError("artifact reference requires path and sha256")
    path = artifact(root, item["path"])
    if digest(path) != item["sha256"]:
        raise ValueError(f"artifact hash mismatch: {item['path']}")
    return path


REVIEWERS = ("scientific-reviewer", "reproducibility-auditor")


def checked_review(root, role, ref, reviewed):
    """A stage_review's report reference for `role`: the report.md of a recorded run of that reviewer.

    Any existing file used to satisfy a review reference, so a review could be recorded without a reviewer
    having run. The report must now be `runs/<role>/<agent id>/report.md`, written by the invocation recorder
    (scripts/capture/record_invocation.py) from the reviewer's own final message, with the invocation.json
    beside it naming that role and the sha256 of this report and of the recorded brief; and the brief must
    name the sha256 of `reviewed`, the artifact the record says was reviewed."""
    path = checked_artifact(root, ref)
    parts = Path(ref["path"]).parts
    if len(parts) != 4 or parts[:2] != ("runs", role) or parts[3] != "report.md" or Path(ref["path"]).as_posix() != ref["path"]:
        raise ValueError(f"{role} report must be runs/{role}/<agent id>/report.md, as written by the invocation "
                         "recorder (scripts/capture/recover.py after the reviewer finishes)")
    identifier(parts[2])
    task = artifact(root, f"runs/{role}/{parts[2]}/task.txt")
    try:
        meta = json.loads(artifact(root, f"runs/{role}/{parts[2]}/invocation.json").read_text(encoding="utf-8"))
    except ValueError as exc:
        raise ValueError(f"{role} report has no readable invocation record: {exc}") from exc
    if not isinstance(meta, dict) or (meta.get("record_type"), meta.get("specialist"), meta.get("invocation_id")) != ("review", role, parts[2]):
        raise ValueError(f"{role} report is not a recorded run of that reviewer")
    if meta.get("report_sha256") != ref["sha256"] or meta.get("task_sha256") != digest(task):
        raise ValueError(f"{role} report or brief differs from its invocation record")
    if reviewed["sha256"] not in task.read_text(encoding="utf-8"):
        raise ValueError(f"the recorded {role} brief does not name the reviewed artifact sha256 "
                         f"{reviewed['sha256']}; give each reviewer the artifact path and sha256")
    return path


# A stage_delta carries a full review over to an edge-only edit of the reviewed network. After this many
# changed edges since the last full stage_review, the reviewers must read the network again.
DELTA_REVIEW_LIMIT = 5
PASSING = {"approved", "approved_with_limitations"}
DELTA_KEYS = {"id", "kind", "session_id", "stage", "parent", "artifact", "changes", "rules", "approved_by",
              "authorization"}
OPPOSITE = {"activation": "inhibition", "inhibition": "activation"}


def _delta_evidence(root, change):
    """One change's support: its own literature report for this edge, or a recorded data approval."""
    if ("evidence" in change) == ("decision_id" in change):
        raise ValueError("each delta change cites exactly one of evidence (its edge report) or decision_id "
                         "(a recorded researcher approval of stage data)")
    if "decision_id" in change:
        found = [r for r in records((root / "DECISIONS.md").read_text(encoding="utf-8"))
                 if r.get("id") == change["decision_id"]]
        if len(found) != 1 or (found[0].get("kind"), found[0].get("stage")) != ("stage_approval", "data"):
            raise ValueError(f"delta decision_id {change['decision_id']} is not a recorded stage_approval of stage data")
        validate_record(root, found[0])
        return
    from scripts.evidence_contract import report_identity, validate_footer
    report = checked_artifact(root, change["evidence"])
    _, source, target, _, _ = report_identity(root.resolve(), report.resolve())
    if (source, target) != (change["source"], change["target"]):
        raise ValueError(f"evidence report {change['evidence']['path']} is not about "
                         f"{change['source']} -> {change['target']}")
    footer = validate_footer(report.read_text(encoding="utf-8"), source, target)
    if change["change"] == "removed":
        return  # any verdict may motivate a removal; the researcher approves it
    if footer["verdict"] not in {"supported", "context-dependent"} or footer["interaction_type"] == OPPOSITE[change["sign"]]:
        raise ValueError(f"evidence report {change['evidence']['path']} ({footer['verdict']}, "
                         f"{footer['interaction_type']}) does not support {change['sign']} of {target} by {source}; "
                         "a full stage review is required")


def checked_delta(root, item):
    """A stage_delta: the researcher approves the exact, recomputed edge difference between a reviewed
    NeKo-to-MaBoSS export (`parent`) and a new export (`artifact`) of the same session, so the earlier
    review carries over without new reviewer runs. `changes` and `rules` must equal what
    scripts/network_delta.py computes from the two BNET files now; each change cites its own support."""
    if set(item) - {"recorded_at"} != DELTA_KEYS:
        raise ValueError("stage_delta requires exactly: " + ", ".join(sorted(DELTA_KEYS)))
    if item["stage"] != "topology":
        raise ValueError("stage_delta applies to the topology stage only")
    if item.get("approved_by") != "researcher" or not item.get("authorization"):
        raise ValueError("stage_delta needs the researcher's explicit authorization of the listed changes")
    from scripts.network_delta import manifest_delta
    computed = manifest_delta(root, item["parent"], item["artifact"], item["session_id"])
    changes = item["changes"]
    if not isinstance(changes, list) or any(not isinstance(c, dict) for c in changes):
        raise ValueError("stage_delta changes must be a list of objects")
    stated = [{k: c.get(k) for k in ("change", "source", "target", "sign")} for c in changes]
    if stated != computed["changes"] or item["rules"] != computed["rules"]:
        raise ValueError("stage_delta changes or rules differ from the computed difference of the two exports; "
                         "run scripts/network_delta.py and record its output")
    for change in changes:
        if set(change) - {"evidence", "decision_id"} != {"change", "source", "target", "sign"}:
            raise ValueError("unknown stage_delta change field")
        _delta_evidence(root, change)
    return computed


def review_basis(root, recs, record):
    """The full stage_review that `record` (a stage_review or stage_delta in `recs`) rests on, and the number
    of delta-approved changes since it. Each delta's parent must itself be covered by an earlier passing
    review or delta, the latest review of a parent must not block, and the total stays within
    DELTA_REVIEW_LIMIT."""
    total, current = 0, record
    index = next((i for i, r in enumerate(recs) if r is record), len(recs))
    while current.get("kind") == "stage_delta":
        total += len(current["changes"])
        if total > DELTA_REVIEW_LIMIT:
            raise ValueError(f"{total} edges changed since the last full review (limit {DELTA_REVIEW_LIMIT}); "
                             "a full stage review is required")
        covers = [(i, r) for i, r in enumerate(recs)
                  if r.get("kind") in {"stage_review", "stage_delta"} and r.get("stage") == "topology"
                  and r.get("session_id") == current["session_id"] and r.get("artifact") == current["parent"]]
        if covers and covers[-1][1]["kind"] == "stage_review" and covers[-1][1].get("outcome") not in PASSING:
            raise ValueError("the latest review of the delta's parent export blocks it")
        earlier = [(i, r) for i, r in covers if i < index]
        if not earlier:
            raise ValueError("the delta's parent export has no earlier recorded review or approved delta")
        index, current = earlier[-1]
        validate_record(root, current)
    if current.get("outcome") not in PASSING:
        raise ValueError("the review this export rests on does not approve it")
    return current, total


# Characters a Markdown table cell cannot hold: the delimiter, and every control, line- or
# paragraph-separator character (str.splitlines breaks on \x0b, \x0c, \x1c-\x1e, \x85, U+2028, U+2029).
UNSAFE_CELL_CATEGORIES = {"Cc", "Zl", "Zp"}


def unsafe_cell_char(value):
    """The first character of `value` that a table cell cannot hold, or None."""
    return next((c for c in value if c == "|" or unicodedata.category(c) in UNSAFE_CELL_CATEGORIES), None)


def cell(value):
    value = str(value)
    bad = unsafe_cell_char(value)
    if bad is not None:
        raise ValueError(f"table cell contains a delimiter, newline or control character ({bad!r})")
    return value


def table(text, headers):
    lines = text.splitlines()
    matches = [i for i, line in enumerate(lines) if [x.strip() for x in line.strip().strip("|").split("|")] == headers]
    if len(matches) != 1:
        raise ValueError("expected exactly one recognized state table")
    start = matches[0] + 2
    if start > len(lines) or not re.fullmatch(r"[| :\-]+", lines[start - 1]):
        raise ValueError("invalid table separator")
    end = start
    rows = []
    while end < len(lines) and lines[end].startswith("|"):
        values = [x.strip().strip("`") for x in lines[end].strip().strip("|").split("|")]
        if len(values) != len(headers):
            raise ValueError("malformed state row")
        if values[0] != "_(none yet)_":
            rows.append(dict(zip(headers, values)))
        end += 1
    return lines, start, end, rows


def replace_table(text, headers, rows):
    lines, start, end, _ = table(text, headers)
    lines[start:end] = ["| " + " | ".join(cell(row[h]) for h in headers) + " |" for row in rows]
    return "\n".join(lines) + "\n"


def records(text):
    result = [json.loads(m.group(1)) for m in RECORD.finditer(text)]
    if text.count("<!-- governance-record -->") != len(result):
        raise ValueError("malformed governance record")
    if any(not isinstance(r, dict) for r in result):
        raise ValueError("governance records must be JSON objects")
    ids = [identifier(r.get("id")) for r in result]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate governance record ID")
    return result


@contextlib.contextmanager
def locked(root):
    # Locks belong to machine-local infrastructure, not scientific state.
    import fcntl
    folder = root / ".setup"
    folder.mkdir(exist_ok=True)
    with (folder / "state.lock").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def atomic(path, text):
    if path.is_symlink():
        raise ValueError("refusing symlinked state file")
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".state-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


def validate_record(root, item):
    if not isinstance(item, dict):
        raise ValueError("governance record must be an object")
    identifier(item.get("id"))
    kind = item.get("kind")
    if kind not in {"stage_approval", "stage_review", "stage_delta", "conditional_approval", "standalone_approval"}:
        raise ValueError("unknown governance record kind")
    if kind != "standalone_approval":
        identifier(item.get("session_id"))
    if item.get("stage") not in GOVERNANCE_STAGES:
        raise ValueError("unknown scientific stage")
    if kind == "stage_review":
        if item.get("outcome") not in {"approved", "approved_with_limitations", "revision_required", "blocked"}:
            raise ValueError("invalid review outcome")
        checked_artifact(root, item.get("artifact"))
        for role in REVIEWERS:
            checked_review(root, role, item.get(role), item["artifact"])
    elif kind == "stage_delta":
        checked_delta(root, item)
    elif kind in {"stage_approval", "standalone_approval"}:
        if item.get("approved_by") != "researcher" or not item.get("authorization"):
            raise ValueError("explicit researcher authorization quotation/reference required")
        if not isinstance(item.get("outputs"), list) or not item["outputs"] or any(not isinstance(x, str) or not x for x in item["outputs"]) or len(item["outputs"]) != len(set(item["outputs"])):
            raise ValueError("explicit unique nonempty declared outputs required")
        checked_artifact(root, item.get("artifact"))
    else:
        if item.get("approved_by") != "researcher" or not item.get("authorization"):
            raise ValueError("conditional approval needs explicit researcher authorization")
        from scripts.evidence_contract import report_identity, validate_footer
        report = checked_artifact(root, item.get("evidence"))
        session, source, target, _, _ = report_identity(root.resolve(), report.resolve())
        edge = item.get("edge")
        if not isinstance(edge, list) or len(edge) != 3 or any(not isinstance(x, str) or not x for x in edge):
            raise ValueError("conditional approval requires its exact literal [source, effect, target] edge")
        from scripts.codex.write_literature_report import validate_report
        validate_report(report.read_text(), source, target, item["session_id"])
        footer = validate_footer(report.read_text(), source, target, item["session_id"])
        if session != item["session_id"] or footer["edge"] != edge:
            raise ValueError("conditional approval edge/session disagrees with evidence identity")
        conditions = item.get("conditions")
        if not isinstance(conditions, dict) or not conditions or set(conditions) - {"verdict", "confidence", "interaction_type"}:
            raise ValueError("conditions must name supported evidence footer fields")
        if any(not isinstance(v, list) or not v or any(not isinstance(x, str) for x in v) for v in conditions.values()):
            raise ValueError("condition values must be nonempty string allowlists")


def data_product_approval(root, item):
    """Require decision_id to be the researcher's stage_approval of this data session's
    current handoff.json, and the product to be an approved artifact of that handoff."""
    from scripts.data import approvals
    from scripts.data.common import RUNS_ROOT
    session = item["source_session"]
    record = approvals.require_data_approval(root, item["decision_id"], session)
    listed = approvals.require_in_handoff(root, session, item["path"], item["sha256"])
    base = (RUNS_ROOT / session).as_posix()
    if item["path"].startswith(f"{base}/proposals/"):
        entry = approvals.proposal_entry_for_path(root, session, item["path"], listed)
        approvals.require_proposal_approved(root, record, session, entry["id"])


def update(root, operation, item):
    if not isinstance(item, dict):
        raise ValueError("state update must be a JSON object")
    root = root.resolve()
    with locked(root):
        name = "DECISIONS.md" if operation in {"decision", "record"} else "DATA_DICTIONARY.md" if operation == "data-product" else "VALIDATION_PLAN.md" if operation in {"validation", "validation-declare"} else "CURRENT_STATE.md"
        path = root / name
        text = path.read_text(encoding="utf-8")
        if operation == "decision":
            if set(item) != set(DECISIONS) - {"Date"}:
                raise ValueError("decision requires all decision-table fields except generated Date")
            rows = table(text, DECISIONS)[3]
            rows.append({"Date": now(), **item})
            text = replace_table(text, DECISIONS, rows)
        elif operation == "record":
            validate_record(root, item)
            if item["id"] in {r["id"] for r in records(text)}:
                raise ValueError("governance record ID already exists")
            if item["kind"] == "stage_delta":
                # refuse an unreviewed parent, or a delta past the limit, when it is recorded
                review_basis(root, [*records(text), item], item)
            item = {**item, "recorded_at": now()}
            text = text.rstrip() + "\n\n<!-- governance-record -->\n```json\n" + json.dumps(item, indent=2) + "\n```\n"
        elif operation == "registry-add":
            if set(item) != set(REGISTRY):
                raise ValueError("registry fields do not match schema")
            identifier(item["Session ID"])
            if ROLES.get(item["Specialist"]) != item["Stage"] or item["Status"] != "active" or item["Handoff"] not in {"pending", "approved", "exported"}:
                raise ValueError("invalid role, stage, status or handoff")
            if item["Derived from"] != "—":
                identifier(item["Derived from"])
            rows = table(text, REGISTRY)[3]
            if any(r["Session ID"] == item["Session ID"] for r in rows):
                raise ValueError("session already registered")
            rows.append(item)
            text = replace_table(text, REGISTRY, rows)
        elif operation in {"registry-supersede", "registry-handoff"}:
            rows = table(text, REGISTRY)[3]
            matches = [r for r in rows if r["Session ID"] == item.get("session_id")]
            if len(matches) != 1:
                raise ValueError("expected exactly one existing session")
            row = matches[0]
            if row["Status"] != "active":
                raise ValueError("only an active registry row may change")
            if operation == "registry-supersede":
                row["Status"] = "superseded"
            else:
                allowed = {"pending": "approved", "approved": "exported"}
                if allowed.get(row["Handoff"]) != item.get("handoff"):
                    raise ValueError("handoff transitions must be pending -> approved -> exported")
                if item["handoff"] == "exported":
                    checked_artifact(root, item.get("artifact"))
                row["Handoff"] = item["handoff"]
            text = replace_table(text, REGISTRY, rows)
        elif operation == "validation-declare":
            if set(item) != {"Criterion", "Method", "Required evidence"}:
                raise ValueError("criterion declaration requires Criterion, Method and Required evidence")
            identifier(item["Criterion"])
            if any(not isinstance(value, str) or not value.strip() for value in item.values()):
                raise ValueError("criterion declaration fields must be nonempty strings")
            rows = table(text, VALIDATION)[3]
            if any(row["Criterion"] == item["Criterion"] for row in rows):
                raise ValueError("criterion is already declared")
            rows.append({**item, "Status": "pending", "Result": "—"})
            text = replace_table(text, VALIDATION, rows)
        elif operation == "validation":
            if item.get("schema_version") != 1 or item.get("kind") != "artifact_validation":
                raise ValueError("only supported validator output may update validation status")
            checked_artifact(root, item.get("criteria"))
            rows = table(text, VALIDATION)[3]
            seen = set()
            for result in item.get("results", []):
                key = result.get("criterion_id")
                if key in seen or result.get("status") not in {"passed", "failed", "incomplete"}:
                    raise ValueError("duplicate criterion or invalid validation status")
                seen.add(key)
                matches = [r for r in rows if r["Criterion"] == key]
                if len(matches) != 1:
                    raise ValueError(f"expected exactly one declared criterion: {key}")
                for source in result.get("inputs", []):
                    checked_artifact(root, source)
                matches[0]["Status"] = result["status"]
                matches[0]["Result"] = cell(json.dumps({"metrics": result.get("metrics", {}), "criteria_sha256": item["criteria"]["sha256"], "inputs": result.get("inputs", [])}, sort_keys=True))
            if not seen:
                raise ValueError("validator output has no results")
            text = replace_table(text, VALIDATION, rows)
        elif operation == "data-product":
            if set(item) != DATA_PRODUCT_KEYS or any(not isinstance(v, str) for v in item.values()):
                raise ValueError("data-product requires exactly: " + ", ".join(sorted(DATA_PRODUCT_KEYS)))
            from scripts.data.common import SESSION_RE
            if SESSION_RE.fullmatch(item["source_session"]) is None:
                raise ValueError("source_session is not a data session ID")
            if not item["name"].strip() or not item["release"].strip():
                raise ValueError("data product needs a name and release")
            checked_artifact(root, {"path": item["path"], "sha256": item["sha256"]})
            data_product_approval(root, item)
            rows = table(text, DATA_DICTIONARY)[3]
            if any(r["Name"] == item["name"] for r in rows):
                raise ValueError("data product name already registered")
            rows.append({"Name": item["name"], "Type": item["type"], "Biological meaning": item["meaning"], "Unit": item["unit"],
                         "Source": f"`{item['source_session']}` `{item['path']}` sha256 `{item['sha256'][:12]}`",
                         "Version": item["release"],
                         "Notes": f"identifiers: {item['identifiers']}; decision {item['decision_id']}; {item['notes']}"})
            text = replace_table(text, DATA_DICTIONARY, rows)
        elif operation == "checkpoint":
            commit = item.get("commit", "")
            if not re.fullmatch(r"[a-f0-9]{40}", commit) or not item.get("summary"):
                raise ValueError("checkpoint needs a full content commit ID and summary")
            text = text.rstrip() + f"\n\n- Checkpoint {now()}: `{commit}` — {cell(item['summary'])}\n"
        else:
            raise ValueError("unknown state operation")
        atomic(path, text)
    return {"path": name, "sha256": digest(path), "operation": operation}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["decision", "record", "registry-add", "registry-supersede", "registry-handoff", "validation-declare", "validation", "checkpoint", "data-product"])
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args()
    try:
        print(json.dumps(update(args.root, args.operation, json.loads(args.input.read_text())), indent=2))
        return 0
    except (ValueError, OSError, KeyError, TypeError) as exc:
        parser.exit(2, f"State update refused: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
