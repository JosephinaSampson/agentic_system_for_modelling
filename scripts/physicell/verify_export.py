#!/usr/bin/env python3
"""Verify a stage-B PhysiCell export against its approved projection and the stage-A capture.

After the multicellular-configurator applied a physicell_projection.json and its export was captured, the new
capture (`after`) is compared with the projection's capture (`before`, the stage-A configuration). Stdlib only.

Refusals (ValueError; CLI exit 2): the projection fails physicell_projection.load_and_check; either capture fails
settings_xml.capture; the after-capture is in the before-capture's session; no invocation record
runs/multicellular-configurator/<after session>/invocation/*/task.txt has, as its line 2, the DATA_INPUT line naming
exactly this projection path and its sha256; the output file already exists.

Expected changes (model values from physicell_projection.model_value; floats are ASCII decimals, no NaN, infinity
or "_", within relative 1e-9; ints exact):
  setting           the resolved element of the after XML holds the model value.
  physiboss_input   exactly one cell_definition[@name=<cell>]/phenotype/intracellular/mapping/input with
                    physicell_name=<signal>, intracellular_name=<node>; its settings children are exactly action
                    (text), threshold, smoothing (int).
  physiboss_output  likewise mapping/output with physicell_name=<behavior>, intracellular_name=<node>; settings
                    exactly action, value, base_value, smoothing (int) and steepness (int), absent when the
                    entry's steepness is null.
                    A link has exactly the attributes physicell_name and intracellular_name and one child, settings;
                    neither settings nor its children carry attributes; a mapping the entries created (the
                    before-capture has none on that cell) carries none either.
  rule              the after rules CSV (no header, CBHG v3 columns cell_type, signal, direction, behavior,
                    saturation_value, half_max, hill_power, apply_to_dead) holds exactly the rule entries as a
                    multiset, apply_to_dead 0; any other row is unexpected.
cell_rules/rulesets/ruleset elements are compared by content, not position: exactly one enabled ruleset when rule
entries exist, none otherwise, protocol CBHG, version 3.0, format csv, with only folder ./config and filename = the
after rules CSV; the disabled rulesets are equal as a multiset of their full content. Everything else: with the
ruleset elements (only them) and the entries' mapping elements removed from both trees and each setting's element
text taken from the after tree, settings_xml.diff must be empty. Dependencies other than the rules CSV are
byte-identical (package path and sha256 from the capture.json settings_xml.capture verified). Any other difference
is listed in unexpected_changes and the verification fails.

Output (write-new, one file per attempt): runs/orchestrator/<p>/physicell_verification.<after session>.<after
capture>.json beside the projection: {kind: "physicell_verification", schema_version: 1, projection: {path,
sha256}, before, after (the settings_xml.capture bindings, verbatim), lineage: {invocation}, entries: [{entry_id,
expected, found, ok, problem (when not ok)}], unexpected_changes: [{kind, path, before, after, reason?}], passed}.
CLI exit 0 passed, 1 not passed, 2 refused. compute() builds the report without writing it; build.py re-runs it
on the file's own projection and after capture and requires the same result (ruling T9-d).
"""
from __future__ import annotations

import argparse
import copy
import csv
import hashlib
import io
import json
import re
import sys
from collections import Counter
from pathlib import Path, PurePosixPath

if __package__ in (None, ""):  # run as a script: make the repo importable
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.data.common import project_root, reject_symlinks  # noqa: E402
from scripts.physicell import lineage  # noqa: E402
from scripts.physicell import physicell_design as pd  # noqa: E402
from scripts.physicell import physicell_projection as pp  # noqa: E402
from scripts.physicell import settings_xml as sx  # noqa: E402

REL_TOL = 1e-9  # floats: settings_xml.number (strict ASCII decimal) and settings_xml.close
CSV_COLUMNS = ("cell_type", "signal", "direction", "behavior", "saturation_value", "half_max", "hill_power",
               "apply_to_dead")
INTEGER = re.compile(r"[+-]?[0-9]+")
LINK_ATTRIBUTES = {"physicell_name", "intracellular_name"}
ENABLED_RULESET = {"protocol": "CBHG", "version": "3.0", "format": "csv"}
RULES_FOLDER = "./config"
LINKS = {"physiboss_input": ("input", "signal"), "physiboss_output": ("output", "behavior")}


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _exact_int(found, expected: int) -> bool:
    text = str(found).strip()
    return bool(INTEGER.fullmatch(text)) and int(text) == expected


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


def _link_check(xml, entry: dict, created_mapping: bool) -> dict:
    """The entry's link in the after XML; `created_mapping`: the before-capture had no mapping on that cell."""
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
    tag, key = LINKS[entry["kind"]]
    what = f"{tag} link {entry[key]!r} / {entry['node']!r} on {entry['cell_definition']}"
    links = _links(xml, entry)
    if len(links) != 1:
        found = [_settings(link) for link in links] or None
        return _result(expected, found, [f"{len(links)} matching {what} elements, expected exactly 1"])
    link = links[0]
    found = _settings(link)
    problems = _link_structure(xml, link, created_mapping)
    if not isinstance(found, dict) or not all(isinstance(v, str) for v in found.values()):
        # fail closed: values that cannot be compared (no single settings, duplicate children) never pass
        problems.append("the settings values cannot be compared")
    else:
        if set(found) != set(expected):
            problems.append(f"settings children {sorted(found)} are not exactly {sorted(expected)}")
        else:
            for k, want in expected.items():
                if k == "action":
                    good = found[k] == want
                elif k in floats:
                    good = sx.close(sx.number(str(found[k])), want, REL_TOL)
                else:
                    good = _exact_int(found[k], want)
                if not good:
                    problems.append(f"{k}: found {found[k]!r}, expected {want!r}")
    return _result(expected, found, [f"{what}: {p}" for p in problems])


def _result(expected, found, problems: list) -> dict:
    result = {"expected": expected, "found": found, "ok": not problems}
    if problems:
        result["problem"] = "; ".join(problems)
    return result


def _link_structure(xml, link, created_mapping: bool) -> list:
    """Ruling T6-e: exactly {physicell_name, intracellular_name}, one settings child, no attributes below it; a
    mapping the entries created carries no attributes."""
    problems = []
    if set(link.attrib) != LINK_ATTRIBUTES:
        problems.append(f"attributes {sorted(link.attrib)} are not exactly {sorted(LINK_ATTRIBUTES)}")
    tags = [child.tag for child in link]
    if tags != ["settings"]:
        problems.append(f"children {tags} are not exactly one settings")
    if (link.text or "").strip():
        problems.append(f"text {link.text.strip()!r}")
    for settings in link.findall("settings"):
        if settings.attrib:
            problems.append(f"settings carries attributes {dict(settings.attrib)}")
        if (settings.text or "").strip():
            problems.append(f"settings text {settings.text.strip()!r}")
        duplicates = sorted(tag for tag, count in Counter(c.tag for c in settings).items() if count > 1)
        if duplicates:
            problems.append(f"settings has duplicate children {duplicates}")
        for child in settings:
            if child.attrib:
                problems.append(f"<{child.tag}> carries attributes {dict(child.attrib)}")
            if len(child):
                problems.append(f"<{child.tag}> has children {[c.tag for c in child]}")
    if created_mapping:
        parents = {child: parent for parent in xml.iter() for child in parent}
        mapping = parents.get(link)
        if mapping is not None and mapping.attrib:
            problems.append(f"the mapping element the entries created carries attributes {dict(mapping.attrib)}")
    return problems


def _settings(link):
    settings = link.findall("settings")
    if len(settings) != 1:
        return None
    children = list(settings[0])
    if len({c.tag for c in children}) != len(children):
        return [[c.tag, (c.text or "").strip()] for c in children]  # duplicate tags: a list, never compared
    return {c.tag: (c.text or "").strip() for c in children}


def _setting_check(xml, entry: dict) -> dict:
    expected = pp.model_value(entry)
    try:
        element = sx.resolve(xml, entry["path"])
    except ValueError as exc:
        return _result(expected, None, [str(exc)])
    text = (element.text or "").strip()
    if len(element):
        return _result(expected, text, [f"{entry['path']} is no longer a leaf element"])
    if not sx.close(sx.number(text), expected, REL_TOL):
        return _result(expected, text, [f"{entry['path']} holds {text!r}, expected {expected!r}"])
    return _result(expected, text, [])


def _rule_expected(entry: dict) -> dict:
    return {"cell_type": entry["cell_definition"], "signal": entry["signal"], "direction": entry["direction"],
            "behavior": entry["behavior"], "saturation_value": pp.model_value(entry["saturation_value"]),
            "half_max": pp.model_value(entry["half_max"]), "hill_power": pp.model_value(entry["hill_power"]),
            "apply_to_dead": 0}


def _row_matches(row: list, expected: dict) -> bool:
    if len(row) != len(CSV_COLUMNS) or row[:4] != [expected[k] for k in CSV_COLUMNS[:4]]:
        return False
    return all(sx.close(sx.number(str(row[i])), expected[CSV_COLUMNS[i]], REL_TOL) for i in (4, 5, 6)) and _exact_int(row[7], 0)


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
            matched[entry_id] = _result(want, row, [])
    for entry_id, want in expected.items():
        if entry_id in matched:
            continue
        key = [want[k] for k in CSV_COLUMNS[:4]]
        row = next((r for r in left if r[:4] == key), None)
        if row is not None:
            left.remove(row)
        problem = (f"rules CSV row {row} does not hold the model values (within relative {REL_TOL}) with "
                   "apply_to_dead 0" if row is not None else f"no rules CSV row for {key}")
        matched[entry_id] = _result(want, row, [problem])
    return matched, left


# ---------------------------------------------------------------- everything else

def _describe(element) -> dict:
    """The full content of an element: tag, attributes, stripped text and children (sorted, so order is ignored)."""
    children = sorted((_describe(child) for child in element), key=lambda d: json.dumps(d, sort_keys=True))
    return {"tag": element.tag, "attributes": dict(sorted(element.attrib.items())),
            "text": (element.text or "").strip(), "children": children}


def _rulesets(xml) -> tuple[list, list]:
    """(enabled, disabled) cell_rules/rulesets/ruleset elements."""
    parents = {child: parent for parent in xml.iter() for child in parent}
    enabled, disabled = [], []
    for ruleset in xml.findall("cell_rules/rulesets/ruleset"):
        (enabled if sx._enabled(ruleset, parents) else disabled).append(ruleset)
    return enabled, disabled


def _enabled_problems(ruleset, rules_name: str | None) -> list:
    """Ruling T6-e: protocol CBHG, version 3.0, format csv, enabled at most "true"; children exactly folder
    ./config and filename = the after capture's rules CSV, without attributes or children."""
    problems = []
    extra = sorted(set(ruleset.attrib) - set(ENABLED_RULESET) - {"enabled"})
    if extra:
        problems.append(f"attributes {extra} beyond {sorted(ENABLED_RULESET)} and enabled")
    for key, want in ENABLED_RULESET.items():
        if ruleset.get(key) != want:
            problems.append(f"{key}={ruleset.get(key)!r}, expected {want!r}")
    if ruleset.get("enabled", "true").lower() != "true":
        problems.append(f"enabled={ruleset.get('enabled')!r}, expected 'true'")
    if (ruleset.text or "").strip():
        problems.append(f"text {ruleset.text.strip()!r}")
    tags = sorted(child.tag for child in ruleset)
    if tags != ["filename", "folder"]:
        problems.append(f"children {tags} are not exactly folder and filename")
    for child in ruleset:
        if child.attrib or len(child):
            problems.append(f"<{child.tag}> carries attributes or children")
    for tag, want in (("folder", RULES_FOLDER), ("filename", rules_name)):
        found = [(child.text or "").strip() for child in ruleset.findall(tag)]
        if found != [want]:
            problems.append(f"{tag} {found}, expected {want!r}")
    return problems


def _cell_rules_changes(before, after, has_rules: bool, rules_name: str | None) -> list:
    """Compare the rulesets by content, then remove the ruleset elements (only them) from both trees."""
    changes = []
    before_enabled, before_disabled = _rulesets(before)
    after_enabled, after_disabled = _rulesets(after)
    want = 1 if has_rules else 0
    path = "cell_rules/rulesets/ruleset"
    if len(after_enabled) != want:
        changes.append({"kind": "cell_rules", "path": f"{path} (enabled)",
                        "before": [_describe(r) for r in before_enabled],
                        "after": [_describe(r) for r in after_enabled],
                        "reason": f"{want} enabled ruleset(s) expected "
                                  f"({'rule entries exist' if has_rules else 'no rule entries'})"})
    elif after_enabled:
        problems = _enabled_problems(after_enabled[0], rules_name)
        if problems:
            changes.append({"kind": "cell_rules", "path": f"{path} (enabled)", "before": None,
                            "after": _describe(after_enabled[0]), "reason": "; ".join(problems)})

    def key(ruleset):
        return json.dumps(_describe(ruleset), sort_keys=True)
    if Counter(map(key, before_disabled)) != Counter(map(key, after_disabled)):
        changes.append({"kind": "cell_rules", "path": f"{path} (disabled)",
                        "before": sorted((_describe(r) for r in before_disabled), key=json.dumps),
                        "after": sorted((_describe(r) for r in after_disabled), key=json.dumps),
                        "reason": "the disabled rulesets differ in content"})
    for tree in (before, after):  # the rest of cell_rules goes through settings_xml.diff
        for rulesets in tree.findall("cell_rules/rulesets"):
            for ruleset in rulesets.findall("ruleset"):
                rulesets.remove(ruleset)
    return changes


def _had_mapping(before_tree, cell: str) -> bool:
    return any(d.findall("phenotype/intracellular/mapping")
               for d in before_tree.findall("cell_definitions/cell_definition") if d.get("name") == cell)


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
    # A mapping block the entries created (the before-capture had none on that cell) goes with them when it is
    # bare: no attributes, text or other children (anything else stays for settings_xml.diff).
    for definition in tree.findall("cell_definitions/cell_definition"):
        if _had_mapping(before_tree, definition.get("name")):
            continue
        for intracellular in definition.findall("phenotype/intracellular"):
            for mapping in intracellular.findall("mapping"):
                if not len(mapping) and not mapping.attrib and not (mapping.text or "").strip():
                    intracellular.remove(mapping)


def _dependency_changes(before_manifest: dict, after_manifest: dict, rules_name: str | None) -> list:
    def deps(manifest, skip=None):
        return {d["path"]: d["sha256"] for d in manifest["dependencies"]
                if PurePosixPath(d["path"]).name != skip}
    before, after = deps(before_manifest), deps(after_manifest, rules_name)
    return [{"kind": "dependency", "path": path, "before": before.get(path), "after": after.get(path)}
            for path in sorted(set(before) | set(after)) if before.get(path) != after.get(path)]


# ---------------------------------------------------------------- verify

def target(root: Path, projection_rel: str, after: dict) -> Path:
    """runs/orchestrator/<p>/physicell_verification.<after session>.<after capture>.json beside the projection."""
    purpose = PurePosixPath(projection_rel).parts[2]
    return Path(root) / "runs" / "orchestrator" / purpose / \
        f"physicell_verification.{after['session']}.{after['capture']}.json"


def verify(root: Path, projection_rel: str, new_capture_rel: str) -> Path:
    """compute, then write runs/orchestrator/<p>/physicell_verification.<after session>.<after capture>.json
    (write-new); ValueError on a refusal (projection check errors, lineage, an existing output)."""
    root = Path(root)
    report = compute(root, projection_rel, new_capture_rel)
    path = target(root, projection_rel, report["after"])
    if path.exists() or path.is_symlink():
        raise ValueError(f"{path.relative_to(root).as_posix()} exists; a verification is never overwritten")
    try:
        pd.write_new(root, path, json.dumps(report, indent=2) + "\n")
    except FileExistsError as exc:
        raise ValueError(f"{path.relative_to(root).as_posix()} exists; a verification is never overwritten") \
            from exc
    return path


def compute(root: Path, projection_rel: str, new_capture_rel: str, after_binding: tuple | None = None) -> dict:
    """The verification report of the stage-B capture against the projection (pure: writes nothing). It re-runs the
    projection check and the lineage check; ValueError on a refusal. build.py recomputes it (ruling T9-d).

    Each capture is bound once: the stage-A capture (`before`) and the design's basis by the projection check, whose
    binding and tree are used here; the new capture by sx.capture_tree here, or `after_binding`, the caller's
    (binding, tree) of exactly new_capture_rel from sx.capture_tree (build.py passes the one it just verified)."""
    root = Path(root)
    checked = pp.load_checked(root, projection_rel)
    if checked.errors:
        raise ValueError(f"projection {projection_rel} fails its check: " + "; ".join(checked.errors))
    data, digest, design = checked.data, checked.digest, checked.design
    # capture_section bound it once and compared its manifest_sha256 with capture.sha256; the trees are deep-copied
    # below before any change
    before, before_xml = checked.capture
    after, after_xml = sx.capture_tree(root, new_capture_rel) if after_binding is None else after_binding
    if after["session"] == before["session"]:
        raise ValueError(f"lineage: the new capture {new_capture_rel} is in session {after['session']}, the "
                         f"projection's capture session; stage B runs in a new session")
    invocation = lineage.dispatched_with(root, after["session"], "projection", projection_rel, digest,
                                         design=design)

    entries = data["entries"]
    rules_raw = None
    rules_name = PurePosixPath(after["rules_path"]).name if after["rules_path"] else None
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
            result = _link_check(after_xml, entry, not _had_mapping(before_xml, entry["cell_definition"]))
        results.append({"entry_id": entry["entry_id"], **result})

    unexpected = [{"kind": "rules_csv", "path": after["rules_path"], "before": None, "after": row}
                  for row in extra_rows]
    old, new = copy.deepcopy(before_xml), copy.deepcopy(after_xml)
    unexpected += _cell_rules_changes(old, new, any(e["kind"] == "rule" for e in entries), rules_name)
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
    unexpected += _dependency_changes(before["manifest"], after["manifest"], rules_name)

    return {"kind": "physicell_verification", "schema_version": 1,
            "projection": {"path": projection_rel, "sha256": digest}, "before": before, "after": after,
            "lineage": {"invocation": invocation}, "entries": results, "unexpected_changes": unexpected,
            "passed": all(r["ok"] for r in results) and not unexpected}


def _why(report: dict) -> list[str]:
    lines = [f"entry {r['entry_id']}: {r.get('problem', 'failed')}; expected {json.dumps(r['expected'])}, "
             f"found {json.dumps(r['found'])}" for r in report["entries"] if not r["ok"]]
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
