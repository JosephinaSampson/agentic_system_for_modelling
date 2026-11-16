#!/usr/bin/env python3
"""Record the live PhysiCell signal/behavior dictionary from a multicellular-configurator invocation.

Reads runs/multicellular-configurator/<session>/invocation/<id>/tool_calls.json and writes
physicell_dictionary.json next to the design (write-new, never overwrites):
  {kind: "physicell_dictionary", schema_version: 1, session, invocation, capture: {path, sha256 of capture.json},
   signals: [..], behaviors: {cell_definition: [..]}, source_sha256 (of tool_calls.json)}

Server order is kept (nothing sorted); exact repeats are dropped. Only successful list calls count, and only
those after the last successful structure-changing call of the session. Per-cell behaviors come only from calls
whose cell_type equals that cell definition. Calls naming another session are ignored; calls without a session id
belong to the invocation's session. Stdlib only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path, PurePosixPath

if __package__ in (None, ""):  # run as a script: make the repo importable
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.capture.exports import _structured  # noqa: E402
from scripts.data.common import project_root, reject_symlinks, strict_json  # noqa: E402
from scripts.physicell import physicell_design as pd  # noqa: E402
from scripts.physicell import settings_xml as sx  # noqa: E402

PREFIX = "mcp__physicell-agent__"
SIGNALS, BEHAVIORS = "list_all_available_signals", "list_all_available_behaviors"
STRUCTURE = ("add_single_cell_type", "add_single_substrate", "import_maboss_handoff",
             "load_xml_configuration", "add_physiboss_model")
ITEM = re.compile(r"^- \*\*(.+?)\*\*: ?(.*)$")
HEADING = re.compile(r"^###\s+(.+?)\s*$")


def _invocation(rel) -> tuple[str, str]:
    """(session, tool_calls.json path); a folder path is accepted and completed."""
    parts = PurePosixPath(rel).parts if isinstance(rel, str) and rel and "\\" not in rel else ()
    if len(parts) == 5:
        parts = parts + ("tool_calls.json",)
    if len(parts) != 6 or parts[:2] != ("runs", sx.SPECIALIST) or parts[3] != "invocation" \
            or parts[5] != "tool_calls.json" or ".." in parts or "." in parts:
        raise ValueError(f"invocation must be runs/{sx.SPECIALIST}/<session>/invocation/<id>/tool_calls.json, "
                         f"not {rel!r}")
    return parts[2], "/".join(parts)


def _texts(content) -> list[str]:
    if isinstance(content, str):
        return [content]
    if isinstance(content, list):
        return [b["text"] for b in content if isinstance(b, dict) and b.get("type") == "text"
                and isinstance(b.get("text"), str)]
    return []


def _payload_error(value, depth=0) -> bool:
    if depth > 4:
        return False
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, RecursionError):
            return False
    if isinstance(value, list):
        return any(_payload_error(b.get("text"), depth + 1) for b in value
                   if isinstance(b, dict) and b.get("type") == "text")
    if isinstance(value, dict):
        return value.get("isError") is True or any(
            _payload_error(value.get(k), depth + 1) for k in ("structuredContent", "structured_content"))
    return False


def _parse_markdown(content, kind_key: str) -> list[dict]:
    items, kind = [], None
    for text in _texts(content):
        for line in text.splitlines():
            heading = HEADING.match(line)
            if heading:
                kind = heading.group(1).lower()
                continue
            match = ITEM.match(line)
            if match:
                items.append({"name": match.group(1), kind_key: kind, "description": match.group(2)})
    return items


def _parse(content, tool: str) -> list[dict]:
    key, kind_key = ("signals", "signal_type") if tool == SIGNALS else ("behaviors", "behavior_type")
    payload = _structured(content)
    found = payload.get(key) if isinstance(payload, dict) else None
    items = found if isinstance(found, list) and all(isinstance(i, dict) and i.get("name") for i in found) \
        else _parse_markdown(content, kind_key)
    unique = []
    for item in items:
        if item not in unique:
            unique.append(item)
    return unique


def _calls(blocks: list, session: str) -> list[dict]:
    """Successful calls of this session, in result order: {index, tool, input, content}."""
    uses = {b["id"]: b for b in blocks if isinstance(b, dict) and b.get("type") == "tool_use" and "id" in b}
    out = []
    for index, block in enumerate(blocks):
        if not isinstance(block, dict) or block.get("type") != "tool_result":
            continue
        use = uses.get(block.get("tool_use_id"))
        name = use.get("name") if use else None
        if not isinstance(name, str) or not name.startswith(PREFIX):
            continue
        content = block.get("content")
        if block.get("is_error") is True or _payload_error(content):
            continue
        given = use.get("input") if isinstance(use.get("input"), dict) else {}
        payload = _structured(content)
        named = given.get("session_id") or (payload.get("session_id") if isinstance(payload, dict) else None)
        if named is not None and named != session:
            continue
        out.append({"index": index, "tool": name[len(PREFIX):], "input": given, "content": content})
    return out


def collect(blocks: list, session: str, cells: list[str]) -> tuple[list, dict]:
    calls = _calls(blocks, session)
    last_change = max((c["index"] for c in calls if c["tool"] in STRUCTURE), default=-1)
    live = [c for c in calls if c["index"] > last_change]
    errors, signals, behaviors = [], None, {}
    mine = [c for c in live if c["tool"] == SIGNALS]
    if mine:
        signals = _parse(mine[-1]["content"], SIGNALS)
    else:
        errors.append("no successful list_all_available_signals after the last structure-changing call")
    for cell in cells:
        found = [c for c in live if c["tool"] == BEHAVIORS and c["input"].get("cell_type") == cell]
        if found:
            behaviors[cell] = _parse(found[-1]["content"], BEHAVIORS)
        else:
            errors.append(f"no successful list_all_available_behaviors(cell_type={cell}) after the last "
                          "structure-changing call")
    if signals == []:
        errors.append("the signals list is empty or unreadable")
    errors += [f"the behaviors list of {c} is empty or unreadable" for c, v in behaviors.items() if not v]
    if errors:
        errors = [e if "after the last structure-changing" in e else e + " (lists must come after the last "
                  "structure-changing call)" for e in errors]
    return errors, {"signals": signals, "behaviors": behaviors}


def capture_dictionary(root: Path, invocation_rel: str, capture_rel: str, design_rel: str) -> Path:
    root = Path(root)
    session, invocation = _invocation(invocation_rel)
    bound = sx.capture(root, capture_rel)
    if bound["session"] != session:
        raise ValueError(f"invocation session {session} is not the capture's session {bound['session']}")
    design, _, problems = pd.load_and_check(root, design_rel)
    if problems:
        raise ValueError("the design is not valid: " + "; ".join(problems))
    if design["session"] != session:
        raise ValueError(f"design session {design['session']} is not the invocation session {session}")
    manifest = root / capture_rel / "capture.json"
    reject_symlinks(manifest, root)
    manifest_sha = hashlib.sha256(manifest.read_bytes()).hexdigest()
    path = root / invocation
    reject_symlinks(path, root)
    if not path.is_file():
        raise ValueError(f"invocation record missing: {invocation}")
    raw = path.read_bytes()
    blocks = strict_json(raw)
    if not isinstance(blocks, list):
        raise ValueError("tool_calls.json must be a list of content blocks")
    errors, found = collect(blocks, session, [c["name"] for c in design["cell_definitions"]])
    if errors:
        raise ValueError("dictionary not recorded: " + "; ".join(errors))
    out = {"kind": "physicell_dictionary", "schema_version": 1, "session": session, "invocation": invocation,
           "capture": {"path": capture_rel, "sha256": manifest_sha}, "signals": found["signals"],
           "behaviors": found["behaviors"], "source_sha256": hashlib.sha256(raw).hexdigest()}
    target = (root / design_rel).with_name("physicell_dictionary.json")
    try:
        pd._write_new(root, target, json.dumps(out, indent=2) + "\n")
    except FileExistsError as exc:
        raise ValueError(f"{target.relative_to(root).as_posix()} exists; the dictionary is never overwritten") from exc
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("invocation")
    parser.add_argument("capture")
    parser.add_argument("design")
    args = parser.parse_args(argv)
    root = project_root().resolve()
    try:
        print(capture_dictionary(root, args.invocation, args.capture, args.design).relative_to(root).as_posix())
    except (ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
