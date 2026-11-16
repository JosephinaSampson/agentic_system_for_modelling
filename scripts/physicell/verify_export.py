#!/usr/bin/env python3
"""Verify a stage-B PhysiCell export against its approved projection and the stage-A capture.

After the multicellular-configurator applied a physicell_projection.json and its export was captured, the new
capture (`after`) is compared with the projection's capture (`before`, the stage-A configuration). Stdlib only.

Refusals (ValueError; CLI exit 2): the projection fails physicell_projection.load_and_check; either capture fails
settings_xml.capture; the after-capture is in the before-capture's session; no invocation record
runs/multicellular-configurator/<after session>/invocation/*/task.txt has, as its line 2, the DATA_INPUT line naming
exactly this projection path and its sha256; the output file already exists.

Expected changes (model values from physicell_projection.model_value; floats within relative 1e-9, ints exact):
  setting           the resolved element of the after XML holds the model value.
  physiboss_input   exactly one cell_definition[@name=<cell>]/phenotype/intracellular/mapping/input with
                    physicell_name=<signal>, intracellular_name=<node>; its settings children are exactly action
                    (text), threshold, smoothing (int).
  physiboss_output  likewise mapping/output with physicell_name=<behavior>, intracellular_name=<node>; settings
                    exactly action, value, base_value, smoothing (int) and steepness (int), absent when the
                    entry's steepness is null.
  rule              the after rules CSV (no header, CBHG v3 columns cell_type, signal, direction, behavior,
                    saturation_value, half_max, hill_power, apply_to_dead) holds exactly the rule entries as a
                    multiset, apply_to_dead 0; any other row is unexpected.
cell_rules is compared by content, not position: exactly one enabled ruleset when rule entries exist, none
otherwise; the disabled rulesets are equal as a multiset of (filename, folder). Everything else: with the cell_rules
subtrees and the entries' mapping elements removed from both trees and each setting's element text taken from the
after tree, settings_xml.diff must be empty. Dependencies other than the rules CSV are byte-identical (package path
and sha256 from capture.json). Any other difference is listed in unexpected_changes and the verification fails.

Output (write-new, one file per attempt): runs/orchestrator/<p>/physicell_verification.<after session>.<after
capture>.json beside the projection: {kind: "physicell_verification", schema_version: 1, projection: {path,
sha256}, before: capture, after: capture, lineage: {invocation}, entries: [{entry_id, expected, found, ok}],
unexpected_changes: [...], passed}. CLI exit 0 passed, 1 not passed, 2 refused.
"""
from __future__ import annotations

import argparse
import copy
import csv
import hashlib
import io
import json
import math
import re
import sys
from collections import Counter
from pathlib import Path, PurePosixPath

if __package__ in (None, ""):  # run as a script: make the repo importable
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.data.common import project_root, reject_symlinks, strict_json  # noqa: E402
from scripts.physicell import physicell_design as pd  # noqa: E402
from scripts.physicell import physicell_projection as pp  # noqa: E402
from scripts.physicell import settings_xml as sx  # noqa: E402

REL_TOL = 1e-9
CSV_COLUMNS = ("cell_type", "signal", "direction", "behavior", "saturation_value", "half_max", "hill_power",
               "apply_to_dead")
INTEGER = re.compile(r"[+-]?[0-9]+")
LINKS = {"physiboss_input": ("input", "signal"), "physiboss_output": ("output", "behavior")}


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _close(found, expected: float) -> bool:
    try:
        value = float(str(found).strip())
    except ValueError:
        return False
    return math.isfinite(value) and math.isclose(value, expected, rel_tol=REL_TOL, abs_tol=0.0)


def _exact_int(found, expected: int) -> bool:
    text = str(found).strip()
    return bool(INTEGER.fullmatch(text)) and int(text) == expected


# ---------------------------------------------------------------- bindings

def _bind(root: Path, rel: str) -> tuple[dict, object, dict]:
    """(capture record with path and capture.json sha256, parsed package XML, capture.json manifest)."""
    bound = sx.capture(root, rel)
    manifest_path = root / rel / "capture.json"
    reject_symlinks(manifest_path, root)
    raw = manifest_path.read_bytes()
    xml_raw = (root / bound["xml_path"]).read_bytes()
    if _sha(xml_raw) != bound["xml_sha256"]:
        raise ValueError(f"capture {rel}: its settings XML changed while being read")
    return {"path": rel, "sha256": _sha(raw), **bound}, sx.parse_bytes(xml_raw), strict_json(raw)


def _lineage(root: Path, after_session: str, projection_rel: str, digest: str) -> str:
    """The rel path of the first invocation task.txt whose line 2 is this projection's DATA_INPUT line."""
    folder_rel = f"runs/{sx.SPECIALIST}/{after_session}/invocation"
    folder = root / folder_rel
    reject_symlinks(folder, root)
    other_sha = []
    for task in sorted(folder.glob("*/task.txt")) if folder.is_dir() else []:
        reject_symlinks(task, root)
        lines = task.read_text(encoding="utf-8", errors="replace").splitlines()
        if len(lines) < 2 or not lines[1].startswith("DATA_INPUT "):
            continue
        try:
            value = strict_json(lines[1][len("DATA_INPUT "):].encode("utf-8"))
        except ValueError:
            continue
        if not isinstance(value, dict) or value.get("projection") != projection_rel:
            continue
        rel = task.relative_to(root).as_posix()
        if value.get("sha256") == digest:
            return rel
        other_sha.append(f"{rel} (sha256 {value.get('sha256')})")
    hint = f"; records naming it with another sha256: {', '.join(other_sha)}" if other_sha else ""
    raise ValueError(f"lineage: no invocation record {folder_rel}/*/task.txt has as its line 2 the DATA_INPUT line "
                     f"naming projection {projection_rel} with sha256 {digest}{hint}")


# ---------------------------------------------------------------- entries

def _links(xml, entry: dict) -> list:
    """Every mapping element of the entry's cell definition that matches the entry's link."""
    tag, key = LINKS[entry["kind"]]
    found = []
    for definition in xml.findall("cell_definitions/cell_definition"):
        if definition.get("name") != entry["cell_definition"]:
            continue
        for link in definition.findall(f"phenotype/intracellular/mapping/{tag}"):
            if link.get("physicell_name") == entry[key] and link.get("intracellular_name") == entry["node"]:
                found.append(link)
    return found


def _link_check(xml, entry: dict) -> dict:
    if entry["kind"] == "physiboss_input":
        expected = {"action": entry["action"], "threshold": pp.model_value(entry["threshold"]),
                    "smoothing": entry["smoothing"]}
        floats = ("threshold",)
    else:
        expected = {"action": entry["action"], "value": pp.model_value(entry["value"]),
                    "base_value": pp.model_value(entry["base_value"]), "smoothing": entry["smoothing"]}
        if entry["steepness"] is not None:
            expected["steepness"] = entry["steepness"]
        floats = ("value", "base_value")
    links = _links(xml, entry)
    if len(links) != 1:
        found = [_settings(link) for link in links] or None
        return {"expected": expected, "found": found, "ok": False}
    found = _settings(links[0])
    ok = isinstance(found, dict) and set(found) == set(expected) and found["action"].strip() == expected["action"] \
        and all(_close(found[k], expected[k]) for k in floats) \
        and all(_exact_int(found[k], expected[k]) for k in expected if k not in floats and k != "action")
    return {"expected": expected, "found": found, "ok": bool(ok)}


def _settings(link):
    settings = link.findall("settings")
    if len(settings) != 1:
        return None
    children = list(settings[0])
    if len({c.tag for c in children}) != len(children) or any(len(c) for c in children):
        return {"children": [c.tag for c in children]}
    return {c.tag: (c.text or "").strip() for c in children}


def _setting_check(xml, entry: dict) -> dict:
    expected = pp.model_value(entry)
    try:
        element = sx.resolve(xml, entry["path"])
    except ValueError:
        return {"expected": expected, "found": None, "ok": False}
    text = (element.text or "").strip()
    return {"expected": expected, "found": text, "ok": not len(element) and _close(text, expected)}


def _rule_expected(entry: dict) -> dict:
    return {"cell_type": entry["cell_definition"], "signal": entry["signal"], "direction": entry["direction"],
            "behavior": entry["behavior"], "saturation_value": pp.model_value(entry["saturation_value"]),
            "half_max": pp.model_value(entry["half_max"]), "hill_power": pp.model_value(entry["hill_power"]),
            "apply_to_dead": 0}


def _row_matches(row: list, expected: dict) -> bool:
    if len(row) != len(CSV_COLUMNS) or row[:4] != [expected[k] for k in CSV_COLUMNS[:4]]:
        return False
    return all(_close(row[i], expected[CSV_COLUMNS[i]]) for i in (4, 5, 6)) and _exact_int(row[7], 0)


def _csv_rows(raw: bytes | None) -> list:
    if raw is None:
        return []
    text = raw.decode("utf-8")
    return [[field.strip() for field in row] for row in csv.reader(io.StringIO(text)) if any(f.strip() for f in row)]


def _rules_check(rules: list, rows: list) -> tuple[dict, list]:
    """({entry_id: result}, leftover rows): exact matches first, then a failed entry claims a row with its key."""
    expected = {e["entry_id"]: _rule_expected(e) for e in rules}
    left = list(rows)
    matched = {}
    for entry_id, want in expected.items():
        row = next((r for r in left if _row_matches(r, want)), None)
        if row is not None:
            left.remove(row)
            matched[entry_id] = {"expected": want, "found": row, "ok": True}
    for entry_id, want in expected.items():
        if entry_id in matched:
            continue
        key = [want[k] for k in CSV_COLUMNS[:4]]
        row = next((r for r in left if r[:4] == key), None)
        if row is not None:
            left.remove(row)
        matched[entry_id] = {"expected": want, "found": row, "ok": False}
    return matched, left


# ---------------------------------------------------------------- everything else

def _rulesets(xml) -> tuple[list, list]:
    """(enabled, disabled) rulesets as (filename, folder) pairs."""
    parents = {child: parent for parent in xml.iter() for child in parent}
    enabled, disabled = [], []
    for ruleset in xml.findall("cell_rules/rulesets/ruleset"):
        pair = ((ruleset.findtext("filename") or "").strip(), (ruleset.findtext("folder") or "").strip())
        (enabled if sx._enabled(ruleset, parents) else disabled).append(pair)
    return enabled, disabled


def _cell_rules_changes(before, after, has_rules: bool) -> list:
    changes = []
    before_enabled, before_disabled = _rulesets(before)
    after_enabled, after_disabled = _rulesets(after)
    want = 1 if has_rules else 0
    if len(after_enabled) != want:
        changes.append({"kind": "cell_rules", "path": "cell_rules/rulesets/ruleset (enabled)",
                        "before": [list(p) for p in before_enabled], "after": [list(p) for p in after_enabled],
                        "reason": f"{want} enabled ruleset(s) expected "
                                  f"({'rule entries exist' if has_rules else 'no rule entries'})"})
    if Counter(before_disabled) != Counter(after_disabled):
        changes.append({"kind": "cell_rules", "path": "cell_rules/rulesets/ruleset (disabled)",
                        "before": sorted(list(p) for p in before_disabled),
                        "after": sorted(list(p) for p in after_disabled),
                        "reason": "the disabled rulesets differ"})
    for tree in (before, after):  # cell_rules is compared above, by content only
        for element in tree.findall("cell_rules"):
            tree.remove(element)
    return changes


def _remove_links(tree, entries: list, before_tree) -> None:
    for entry in entries:
        if entry["kind"] not in LINKS:
            continue
        for link in _links(tree, entry):
            for mapping in tree.iter("mapping"):
                if link in list(mapping):
                    mapping.remove(link)
    if before_tree is None:
        return
    # A mapping block the entries created (the before-capture had none on that cell) goes with them.
    for definition in tree.findall("cell_definitions/cell_definition"):
        name = definition.get("name")
        had = [d for d in before_tree.findall("cell_definitions/cell_definition") if d.get("name") == name
               and d.findall("phenotype/intracellular/mapping")]
        for intracellular in definition.findall("phenotype/intracellular"):
            for mapping in intracellular.findall("mapping"):
                if not had and not len(mapping) and not (mapping.text or "").strip():
                    intracellular.remove(mapping)


def _dependency_changes(before_manifest: dict, after_manifest: dict, rules_name: str | None) -> list:
    def deps(manifest, skip=None):
        return {d["path"]: d["sha256"] for d in manifest["dependencies"]
                if PurePosixPath(d["path"]).name != skip}
    before, after = deps(before_manifest), deps(after_manifest, rules_name)
    return [{"kind": "dependency", "path": path, "before": before.get(path), "after": after.get(path)}
            for path in sorted(set(before) | set(after)) if before.get(path) != after.get(path)]


# ---------------------------------------------------------------- verify

def verify(root: Path, projection_rel: str, new_capture_rel: str) -> Path:
    """Write runs/orchestrator/<p>/physicell_verification.<after session>.<after capture>.json; ValueError on a
    refusal (projection check errors, lineage, an existing output)."""
    root = Path(root)
    data, digest, errors = pp.load_and_check(root, projection_rel)
    if errors:
        raise ValueError(f"projection {projection_rel} fails its check: " + "; ".join(errors))
    before, before_xml, before_manifest = _bind(root, data["capture"]["path"])
    after, after_xml, after_manifest = _bind(root, new_capture_rel)
    if before["sha256"] != data["capture"]["sha256"]:
        raise ValueError(f"capture {before['path']} changed since the projection check")
    if after["session"] == before["session"]:
        raise ValueError(f"lineage: the new capture {new_capture_rel} is in session {after['session']}, the "
                         f"projection's capture session; stage B runs in a new session")
    invocation = _lineage(root, after["session"], projection_rel, digest)
    purpose = PurePosixPath(projection_rel).parts[2]
    target = root / "runs" / "orchestrator" / purpose / \
        f"physicell_verification.{after['session']}.{after['capture']}.json"
    if target.exists() or target.is_symlink():
        raise ValueError(f"{target.relative_to(root).as_posix()} exists; a verification is never overwritten")

    entries = data["entries"]
    rules_raw = None
    if after["rules_path"] is not None:
        rules_file = root / after["rules_path"]
        reject_symlinks(rules_file, root)
        rules_raw = rules_file.read_bytes()
        if _sha(rules_raw) != after["rules_sha256"]:
            raise ValueError(f"rules CSV {after['rules_path']} changed while being read")
    rule_results, extra_rows = _rules_check([e for e in entries if e["kind"] == "rule"], _csv_rows(rules_raw))
    results = []
    for entry in entries:
        if entry["kind"] == "setting":
            result = _setting_check(after_xml, entry)
        elif entry["kind"] == "rule":
            result = rule_results[entry["entry_id"]]
        else:
            result = _link_check(after_xml, entry)
        results.append({"entry_id": entry["entry_id"], **result})

    unexpected = [{"kind": "rules_csv", "path": after["rules_path"], "before": None, "after": row}
                  for row in extra_rows]
    old, new = copy.deepcopy(before_xml), copy.deepcopy(after_xml)
    unexpected += _cell_rules_changes(old, new, any(e["kind"] == "rule" for e in entries))
    _remove_links(new, entries, old)
    _remove_links(old, entries, None)
    for entry in entries:
        if entry["kind"] != "setting":
            continue
        try:
            sx.resolve(old, entry["path"]).text = sx.resolve(new, entry["path"]).text
        except ValueError:
            pass  # the entry already fails; the diff shows the missing element
    unexpected += [{"kind": "xml", **change} for change in sx.diff(old, new)]
    rules_name = PurePosixPath(after["rules_path"]).name if after["rules_path"] else None
    unexpected += _dependency_changes(before_manifest, after_manifest, rules_name)

    report = {"kind": "physicell_verification", "schema_version": 1,
              "projection": {"path": projection_rel, "sha256": digest}, "before": before, "after": after,
              "lineage": {"invocation": invocation}, "entries": results, "unexpected_changes": unexpected,
              "passed": all(r["ok"] for r in results) and not unexpected}
    try:
        pd.write_new(root, target, json.dumps(report, indent=2) + "\n")
    except FileExistsError as exc:
        raise ValueError(f"{target.relative_to(root).as_posix()} exists; a verification is never overwritten") \
            from exc
    return target


def _why(report: dict) -> list[str]:
    lines = [f"entry {r['entry_id']}: expected {json.dumps(r['expected'])}, found {json.dumps(r['found'])}"
             for r in report["entries"] if not r["ok"]]
    for change in report["unexpected_changes"]:
        reason = f" ({change['reason']})" if "reason" in change else ""
        lines.append(f"unexpected {change['kind']} change at {change['path']}: before {json.dumps(change['before'])}, "
                     f"after {json.dumps(change['after'])}{reason}")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("projection", help="runs/orchestrator/<purpose>/physicell_projection.json")
    parser.add_argument("capture", help=f"the stage-B capture runs/{sx.SPECIALIST}/<session>/configs/<capture>")
    args = parser.parse_args(argv)
    root = project_root().resolve()
    try:
        path = verify(root, args.projection, args.capture)
    except (ValueError, OSError, UnicodeDecodeError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    report = json.loads(path.read_text(encoding="utf-8"))
    rel = path.relative_to(root).as_posix()
    if report["passed"]:
        print(f"physicell verification passed: {rel}")
        return 0
    for line in _why(report):
        print(line, file=sys.stderr)
    print(f"physicell verification FAILED: {rel}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
