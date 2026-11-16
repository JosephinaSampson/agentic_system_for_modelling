#!/usr/bin/env python3
"""Record the live PhysiCell signal/behavior dictionary from a multicellular-configurator invocation.

Reads runs/multicellular-configurator/<session>/invocation/<id>/tool_calls.json and writes
physicell_dictionary.json next to the design (write-new, never overwrites):
  {kind: "physicell_dictionary", schema_version: 1, session, invocation, capture: {path, sha256 of capture.json},
   design: {path, sha256 of the checked design bytes}, signals: [..], behaviors: {cell_definition: [..]},
   source_sha256 (of tool_calls.json)}

Server order is kept (nothing sorted); exact repeats are dropped. Only successful list calls count, and only
those whose tool_use comes after the tool_result of the last successful structure-changing call of the session.
A structure-changing tool_use without a result refuses the capture; one whose result is an error is ignored (the
server rejected it, so the configuration is unchanged). Items are {name, type} from either result form. Per-cell behaviors come only from calls
whose cell_type equals that cell definition. Calls naming another session are ignored; calls without a session id
belong to the invocation's session. The invocation session must be the capture's session and must have been
dispatched with this design: the task.txt in the same invocation folder as the tool_calls.json has the design's
DATA_INPUT line, path and sha256, as its line 2 (lineage.dispatched_with). Before anything is written the capture must conform to the design
(conformance, ruling T9-b): every mismatch is named. Stdlib only.
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
from scripts.physicell import lineage  # noqa: E402
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


def _item(name, kind, where: str) -> dict:
    if not isinstance(name, str) or not name.strip() or not isinstance(kind, str) or not kind.strip():
        raise ValueError(f"malformed {where} item (name and type must be non-empty text): {name!r}, {kind!r}")
    return {"name": name, "type": kind}


def _parse_markdown(content, tool: str) -> list[dict]:
    items, kind = [], None
    for text in _texts(content):
        for line in text.splitlines():
            heading = HEADING.match(line)
            if heading:
                kind = heading.group(1).lower()
                continue
            match = ITEM.match(line)
            if match:
                if kind is None:
                    raise ValueError(f"{tool}: markdown item {match.group(1)!r} has no ### TYPE heading")
                items.append(_item(match.group(1), kind, tool))
    return items


def _parse(content, tool: str) -> list[dict]:
    key, kind_key = ("signals", "signal_type") if tool == SIGNALS else ("behaviors", "behavior_type")
    payload = _structured(content)
    if isinstance(payload, dict) and key in payload:
        found = payload[key]
        if not isinstance(found, list) or not all(isinstance(i, dict) for i in found):
            raise ValueError(f"{tool}: malformed structured payload ({key} is not a list of objects)")
        items = [_item(i.get("name"), i.get(kind_key), tool) for i in found]
    else:
        items = _parse_markdown(content, tool)
    unique = []
    for item in items:
        if item not in unique:
            unique.append(item)
    return unique


def _calls(blocks: list, session: str) -> list[dict]:
    """Successful calls of this session: {use, result, tool, input, content} (block positions).

    A structure-changing tool_use of this session with no tool_result refuses the capture. A call whose result is an
    error is dropped: the server rejected it, so the configuration is unchanged."""
    results = {b.get("tool_use_id"): (n, b) for n, b in enumerate(blocks)
               if isinstance(b, dict) and b.get("type") == "tool_result"}
    out = []
    for position, use in enumerate(blocks):
        if not isinstance(use, dict) or use.get("type") != "tool_use":
            continue
        name = use.get("name")
        if not isinstance(name, str) or not name.startswith(PREFIX):
            continue
        tool = name[len(PREFIX):]
        given = use.get("input") if isinstance(use.get("input"), dict) else {}
        if use.get("id") not in results:
            if tool in STRUCTURE and given.get("session_id") in (None, session):
                raise ValueError(f"structure-changing call {tool} (id {use.get('id')}) has no result; "
                                 "the configuration state is unknown")
            continue
        index, block = results[use["id"]]
        content = block.get("content")
        if block.get("is_error") is True or _payload_error(content):
            continue
        payload = _structured(content)
        named = given.get("session_id") or (payload.get("session_id") if isinstance(payload, dict) else None)
        if named is not None and named != session:
            continue
        out.append({"use": position, "result": index, "tool": tool, "input": given, "content": content})
    return out


def collect(blocks: list, session: str, cells: list[str]) -> tuple[list, dict]:
    calls = _calls(blocks, session)
    boundary = max((c["result"] for c in calls if c["tool"] in STRUCTURE), default=-1)
    live = [c for c in calls if c["use"] > boundary]
    after = "after the last structure-changing call"
    errors, signals, behaviors = [], None, {}
    mine = [c for c in live if c["tool"] == SIGNALS]
    if mine:
        signals = _parse(mine[-1]["content"], SIGNALS)
        if not signals:
            errors.append("the signals list is empty or unreadable")
    else:
        errors.append(f"no successful list_all_available_signals {after}")
    for cell in cells:
        found = [c for c in live if c["tool"] == BEHAVIORS and c["input"].get("cell_type") == cell]
        if not found:
            errors.append(f"no successful list_all_available_behaviors(cell_type={cell}) {after}")
            continue
        behaviors[cell] = _parse(found[-1]["content"], BEHAVIORS)
        if not behaviors[cell]:
            errors.append(f"the behaviors list of {cell} is empty or unreadable")
    return errors, {"signals": signals, "behaviors": behaviors}


# ---------------------------------------------------------------- stage-A conformance (ruling T9-b)

def _num(label: str, element, expected: float, problems: list, unit: str | None = None) -> None:
    """`element` holds `expected` (rel 1e-9) and, when `unit` is given, carries units="<unit>" exactly."""
    if element is None:
        problems.append(f"{label}: missing in the capture")
        return
    text = (element.text or "").strip()
    if not sx.close(sx.number(text), expected):
        problems.append(f"{label}: the capture holds {text!r}, the design {expected!r}")
    if unit is not None and element.get("units") != unit:
        problems.append(f"{label}: the capture's units {element.get('units')!r} are not the design's {unit!r}")


def _text(label: str, element, expected: str, problems: list) -> None:
    found = None if element is None else (element.text or "").strip()
    if found != expected:
        problems.append(f"{label}: the capture holds {found!r}, the design {expected!r}")


def _domain(design: dict, tree, basis_tree, problems: list) -> None:
    """The domain, dx/dy/dz, use_2D, overall max_time and its units equal the design's (a basis: the basis's)."""
    if design["basis"] is not None:
        if basis_tree is None:
            return
        for path in ("domain", "overall/max_time", "overall/space_units", "overall/time_units"):
            mine, theirs = tree.find(path), basis_tree.find(path)
            if mine is None or theirs is None or sx.diff(_wrap(mine), _wrap(theirs)):
                problems.append(f"{path}: differs from the basis capture's")
        return
    d = design["domain"]
    extents = {"x": d["domain_x"]["value"], "y": d["domain_y"]["value"],
               "z": d["dx"]["value"] if d["use_2d"] else d["domain_z"]["value"]}
    for axis, extent in extents.items():
        _num(f"domain {axis}_min", tree.find(f"domain/{axis}_min"), -extent / 2, problems)
        _num(f"domain {axis}_max", tree.find(f"domain/{axis}_max"), extent / 2, problems)
        _num(f"domain d{axis}", tree.find(f"domain/d{axis}"), d["dx"]["value"], problems)
    _text("domain use_2D", tree.find("domain/use_2D"), "true" if d["use_2d"] else "false", problems)
    _text("overall space_units", tree.find("overall/space_units"), d["domain_x"]["unit"], problems)
    _text("overall time_units", tree.find("overall/time_units"), d["max_time"]["unit"], problems)
    _num("overall max_time", tree.find("overall/max_time"), d["max_time"]["value"], problems, d["max_time"]["unit"])


def _wrap(element):
    holder = sx.ET.Element(sx.ROOT_TAG)
    holder.append(element)
    return holder


def _library_default(facts: dict) -> bool:
    """The untouched library default substrate server 2.5.2 inserts with create_simulation_domain (probe:
    physicell_config's _add_default_substrate_if_needed): label dimensionless, diffusion 100000 micron^2/min, decay
    10 1/min, initial 0 mmHg, Dirichlet disabled at 0 mmHg, no boundary enabled."""
    def holds(key, number, unit):
        fact = facts[key]
        return fact is not None and sx.close(sx.number(fact["text"]), number) and fact["units"] == unit

    return facts["count"] == 1 and facts["units"] == "dimensionless" \
        and holds("diffusion_coefficient", 100000.0, pd.DIFFUSION_UNIT) and holds("decay_rate", 10.0, pd.DECAY_UNIT) \
        and holds("initial_condition", 0.0, "mmHg") and holds("dirichlet", 0.0, "mmHg") \
        and (facts["dirichlet"]["enabled"] or "").lower() == "false" \
        and not any((b["enabled"] or "").lower() == "true" for b in facts["boundaries"])


def _substrates(design: dict, tree, problems: list) -> None:
    facts = sx.substrate_facts(tree)
    declared = {s["name"]: s for s in design["substrates"]}
    for name, found in facts.items():
        if found["count"] > 1:
            problems.append(f"substrate {name}: {found['count']} variables of that name in the capture")
    extra = [n for n in facts if n not in declared]
    if design["basis"] is None and pd.LIBRARY_SUBSTRATE in extra and _library_default(facts[pd.LIBRARY_SUBSTRATE]):
        extra.remove(pd.LIBRARY_SUBSTRATE)  # the server's own, untouched (no tool removes or changes it)
    if extra:
        problems.append(f"substrates {extra} are in the capture but not in the design")
    missing = [n for n in declared if n not in facts]
    if missing:
        problems.append(f"substrates {missing} are in the design but not in the capture")
    for name, item in declared.items():
        found = facts.get(name)
        if found is None:
            continue
        label = f"substrate {name}"
        if found["units"] != item["units"]:
            problems.append(f"{label}: units label {found['units']!r} is not the design's {item['units']!r}")
        for key in pd.SUBSTRATE_VALUES:
            _fact(f"{label} {key}", found[key], item[key]["value"], item[key]["unit"], problems)
        dirichlet, fact = item["dirichlet"], found["dirichlet"]
        if fact is None:
            problems.append(f"{label} Dirichlet_boundary_condition: missing in the capture")
            continue
        if (fact["enabled"] or "").lower() != ("true" if dirichlet["enabled"] else "false"):
            problems.append(f"{label} Dirichlet enabled: the capture holds {fact['enabled']!r}, the design "
                            f"{dirichlet['enabled']!r}")
        # server 2.5.2 fills an omitted dirichlet_value with initial_condition
        value = dirichlet["value"] or item["initial_condition"]
        _fact(f"{label} Dirichlet value", fact, value["value"], value["unit"], problems)
        if item["from"] == pd.BASIS_FROM:
            continue  # per-boundary options of a basis substrate stay as captured
        for boundary in found["boundaries"]:
            on = (boundary["enabled"] or "").lower() == "true"
            if on and not dirichlet["enabled"]:
                problems.append(f"{label} Dirichlet boundary {boundary['ID']}: enabled, the design's Dirichlet "
                                "condition is disabled")
            elif on and not sx.close(sx.number(boundary["text"]), value["value"]):
                problems.append(f"{label} Dirichlet boundary {boundary['ID']}: the capture holds "
                                f"{boundary['text']!r}, the design {value['value']!r}")


def _fact(label: str, fact: dict | None, value: float, unit: str, problems: list) -> None:
    if fact is None:
        problems.append(f"{label}: missing in the capture")
        return
    if not sx.close(sx.number(fact["text"]), value):
        problems.append(f"{label}: the capture holds {fact['text']!r}, the design {value!r}")
    if fact["units"] != unit:
        problems.append(f"{label}: the capture's units {fact['units']!r} are not the design's {unit!r}")


def _cells(design: dict, tree, problems: list) -> None:
    names = sx.cell_definitions(tree)
    declared = [c["name"] for c in design["cell_definitions"]]
    duplicates = sorted({n for n in names if names.count(n) > 1})
    if duplicates:
        problems.append(f"cell definitions {duplicates} appear more than once in the capture")
    extra, missing = [n for n in names if n not in declared], [n for n in declared if n not in names]
    if extra:
        problems.append(f"cell definitions {extra} are in the capture but not in the design")
    if missing:
        problems.append(f"cell definitions {missing} are in the design but not in the capture")
    names_by_key = {m["key"]: m["name"] for m in design["cycle_models"] or ()}
    carriers = {item["cell_definition"] for item in design["intracellular"]}
    for definition in tree.findall("cell_definitions/cell_definition"):
        name = definition.get("name")
        item = next((c for c in design["cell_definitions"] if c["name"] == name), None)
        if item is not None and name not in duplicates:
            want = item["cycle_model"] if item["from"] == pd.BASIS_FROM else names_by_key.get(item["cycle_model"])
            cycles = definition.findall("phenotype/cycle")
            found = cycles[0].get("name") if len(cycles) == 1 else None
            if found != want:
                problems.append(f"cell definition {name}: cycle {found!r} is not the design's {want!r} "
                                f"(cycle_model {item['cycle_model']!r})")
        models = definition.findall("phenotype/intracellular")
        if name in carriers:
            if len(models) != 1 or models[0].get("type") != "maboss":
                problems.append(f"cell definition {name}: the design's MaBoSS model is not its one intracellular "
                                f"model (found {[m.get('type') for m in models]})")
        elif models:
            problems.append(f"cell definition {name}: carries an intracellular model the design does not declare")
        for tag in ("input", "output"):
            links = definition.findall(f"phenotype/intracellular/mapping/{tag}")
            if links:
                # import_maboss_handoff (server 2.5.2) attaches the model with an empty mapping: no links
                problems.append(f"cell definition {name}: {len(links)} PhysiBoSS {tag} link(s); import_maboss_handoff "
                                "writes none and stage A adds none")


def conformance(root: Path, design: dict, capture_rel: str) -> list[str]:
    """Every difference between a checked design and its stage-A capture (ruling T9-b); [] when it conforms.

    The cell definitions (plus nothing else), their cycles, the substrates (the untouched library default substrate
    of a new configuration aside) with their label, values, units and Dirichlet settings, the domain and max_time
    (with a basis: the basis capture's), the MaBoSS models on exactly the design's carriers, no PhysiBoSS link and no
    enabled rules ruleset."""
    root = Path(root)
    bound = sx.capture(root, capture_rel)
    raw = (root / bound["xml_path"]).read_bytes()
    if hashlib.sha256(raw).hexdigest() != bound["xml_sha256"]:
        raise ValueError(f"capture {capture_rel}: its settings XML changed while being read")
    tree = sx.parse_bytes(raw)
    basis_tree = None
    if design["basis"] is not None:
        basis = sx.capture(root, design["basis"]["capture"])
        basis_tree = sx.load(root / basis["xml_path"])
    problems: list[str] = []
    _cells(design, tree, problems)
    _substrates(design, tree, problems)
    _domain(design, tree, basis_tree, problems)
    parents = {child: parent for parent in tree.iter() for child in parent}
    enabled = [r for r in tree.findall("cell_rules/rulesets/ruleset") if sx._enabled(r, parents)]
    if enabled or bound["rules_path"] is not None:
        problems.append(f"the capture has {len(enabled)} enabled rules ruleset(s); stage A adds no rule")
    return problems


def capture_dictionary(root: Path, invocation_rel: str, capture_rel: str, design_rel: str) -> Path:
    root = Path(root)
    session, invocation = _invocation(invocation_rel)
    bound = sx.capture(root, capture_rel)
    if bound["session"] != session:
        raise ValueError(f"invocation session {session} is not the capture's session {bound['session']}")
    design, design_sha, problems = pd.load_and_check(root, design_rel)
    if problems:
        raise ValueError("the design is not valid: " + "; ".join(problems))
    # the design's session is a lineage anchor; this run's session is bound to the design by its dispatch, and the
    # parsed tool_calls.json is the record of that very dispatch (review M2: same invocation folder)
    folder = PurePosixPath(invocation).parent.as_posix()
    try:
        lineage.dispatched_with(root, session, "design", design_rel, design_sha, PurePosixPath(invocation).parts[4])
    except ValueError as exc:
        raise ValueError(f"lineage: the invocation folder {folder} has no task.txt with the design's DATA_INPUT line "
                         f"(its tool_calls.json must be the record of the dispatched run): {exc}") from exc
    problems = conformance(root, design, capture_rel)
    if problems:
        raise ValueError(f"the stage-A capture {capture_rel} does not conform to the design: " + "; ".join(problems))
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
           "capture": {"path": capture_rel, "sha256": manifest_sha},
           "design": {"path": design_rel, "sha256": design_sha}, "signals": found["signals"],
           "behaviors": found["behaviors"], "source_sha256": hashlib.sha256(raw).hexdigest()}
    target = (root / design_rel).with_name("physicell_dictionary.json")
    try:
        pd.write_new(root, target, json.dumps(out, indent=2) + "\n")
    except FileExistsError as exc:
        raise ValueError(f"{target.relative_to(root).as_posix()} exists; the dictionary is never overwritten; re-capture against a new design purpose directory; never delete the old dictionary") from exc
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
