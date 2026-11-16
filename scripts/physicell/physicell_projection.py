#!/usr/bin/env python3
"""Draft and check the stage-B PhysiCell projection: approved data values mapped onto PhysiCell.

The projection (runs/orchestrator/<purpose>/physicell_projection.json) is the researcher-reviewed list of values
the multicellular-configurator applies to the configuration built from the approved stage-A design: PhysiBoSS
input and output links, Hill rules and other settings of PhysiCell_settings.xml. Stdlib only.

Schema (schema_version 1; every key below is required):
  kind: "physicell_projection"
  capture: {path, sha256}        the configuration built from the design: runs/multicellular-configurator/<session>/
                                 configs/<capture>, sha256 of its capture.json (settings_xml.capture verifies it);
                                 <session> must have been dispatched with the design (lineage.dispatched_with: an
                                 invocation task.txt line 2 is the design's DATA_INPUT line, path and sha256)
  dictionary: {path, sha256}     the physicell_dictionary.json captured for exactly this capture and design
  design: {path, sha256, approval}  the stage-A design, its physicell_design stage_approval; it is re-checked
  sources: [{id, path, sha256}]  researcher files under inputs/
  data: [{id, session, proposal, approval}]  approved data-analyst proposals
  entries: [{entry_id, kind, from, justification, ...}], kind one of
    physiboss_input  {cell_definition, signal, node, action, threshold: Q, smoothing: int}
    physiboss_output {cell_definition, node, behavior, action, value: Q, base_value: Q, smoothing: int,
                      steepness: int | null}
    rule             {cell_definition, signal, direction, behavior, saturation_value: Q, half_max: Q, hill_power: Q}
    setting          {path, value, value_unit, transform, factor, target_unit?, target_unit_justification?, tool_hint}
  unmapped: [{from, reason}]     every cited source/proposal row that no entry uses, with the reason

Q is a quantity {value, value_unit, transform, factor, target_unit?}. The model value is
  identity v*factor, unit_factor v*factor, reciprocal factor/v, ln2_over factor*ln(2)/v
(value finite, factor finite and > 0, v != 0 for reciprocal/ln2_over). The converted unit is target_unit when given;
otherwise it is derived only when factor == 1 and the transform is not unit_factor: identity keeps value_unit,
reciprocal/ln2_over give "1/<u>" (or <rest> when u is "1/<rest>"); a compound unit (containing "/", "*", "(" or
spaces) is never inverted. Units are never inferred: anything else needs target_unit.

Checks: names exactly as in the captured dictionary (signals; behaviors per cell definition) and, for PhysiBoSS
links, the cell definition's approved MaBoSS handoff (input links: its network nodes; output links: its declared
output nodes and readouts); an unknown name lists up to 3 close matches. A setting path resolves to exactly one leaf
element of the captured XML holding a number; its converted unit equals the element's units attribute exactly (no
spelling normalisation); without a units attribute, target_unit plus a non-empty target_unit_justification is
required, and target_unit must equal the units the parent element declares, if any. Rule model values satisfy
half_max > 0, hill_power > 0, saturation_value >= 0. The capture must have no enabled rules ruleset: server 2.5.2
cannot reload a captured rules CSV (load_xml_configuration keeps links, not rules), so projections apply to the
stage-A capture and are revised and re-applied from there. One writer per behavior: one rule per (cell_definition,
signal, behavior); at most one physiboss_output per (cell_definition, behavior), and never a rule and an output link
on the same (cell_definition, behavior), counting the captured XML's PhysiBoSS output links; an input link not already in the captured XML; each XML element at most once,
never inside a cell definition's phenotype/intracellular (the links own it). target_unit_justification needs
target_unit. `from` is "basis" (the captured configuration), {source, locator}, {data, locator}
or a non-empty list of those; every declared source and proposal is cited by an entry or an unmapped row.
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import math
import sys
from pathlib import Path, PurePosixPath

if __package__ in (None, ""):  # run as a script: make the repo importable
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts import state  # noqa: E402
from scripts.data import approvals  # noqa: E402
from scripts.data.common import finite_number, project_root, reject_symlinks, sha256_file, strict_json  # noqa: E402
from scripts.physicell import lineage  # noqa: E402
from scripts.physicell import physicell_design as pd  # noqa: E402
from scripts.physicell import settings_xml as sx  # noqa: E402

KEYS = ("kind", "schema_version", "capture", "dictionary", "design", "sources", "data", "entries", "unmapped")
NAME = "physicell_projection.json"
DICTIONARY = "physicell_dictionary.json"
TRANSFORMS = ("identity", "unit_factor", "reciprocal", "ln2_over")
ACTIONS = ("activation", "inhibition")
DIRECTIONS = ("increases", "decreases")
QUANTITY = ("value", "value_unit", "transform", "factor")
COMMON = ("entry_id", "kind", "from", "justification")
KINDS = {
    "physiboss_input": ("cell_definition", "signal", "node", "action", "threshold", "smoothing"),
    "physiboss_output": ("cell_definition", "node", "behavior", "action", "value", "base_value", "smoothing",
                         "steepness"),
    "rule": ("cell_definition", "signal", "direction", "behavior", "saturation_value", "half_max", "hill_power"),
    "setting": ("path", "value", "value_unit", "transform", "factor", "tool_hint"),
}
OPTIONAL = {"setting": ("target_unit", "target_unit_justification")}
COMPOUND = ("/", "*", "(", ")", " ")


# ---------------------------------------------------------------- quantities

def model_value(q: dict) -> float:
    """The value the configurator stores for a quantity (or setting entry); ValueError if it is not valid."""
    if not isinstance(q, dict):
        raise ValueError("a quantity must be an object {value, value_unit, transform, factor}")
    value, factor, transform = q.get("value"), q.get("factor"), q.get("transform")
    if transform not in TRANSFORMS:
        raise ValueError(f"transform must be one of {', '.join(TRANSFORMS)}, not {transform!r}")
    if not finite_number(value):
        raise ValueError(f"value must be a finite number, not {value!r}")
    if not finite_number(factor) or factor <= 0:
        raise ValueError(f"factor must be a finite number > 0, not {factor!r}")
    if transform in ("identity", "unit_factor"):
        result = value * factor
    elif value == 0:
        raise ValueError(f"{transform} of a zero value is undefined")
    elif transform == "reciprocal":
        result = factor / value
    else:
        result = factor * math.log(2) / value
    if not math.isfinite(result):
        raise ValueError(f"the model value of {value!r} ({transform}, factor {factor!r}) is not finite")
    return float(result)


def converted_unit(q: dict) -> str:
    """The unit of model_value(q): target_unit, else derived as the module docstring says; ValueError otherwise."""
    if not isinstance(q, dict):
        raise ValueError("a quantity must be an object")
    unit = q.get("value_unit")
    if not pd.real_text(unit):
        raise ValueError(f"value_unit must be stated (never inferred), not {unit!r}")
    if "target_unit" in q:
        if not pd.real_text(q["target_unit"]):
            raise ValueError(f"target_unit must be a non-empty unit, not {q['target_unit']!r}")
        return q["target_unit"]
    transform, factor = q.get("transform"), q.get("factor")
    why = "give target_unit (units are never inferred)"
    if transform == "unit_factor" or not finite_number(factor) or factor != 1:
        raise ValueError(f"the converted unit of {transform} with factor {factor!r} is not derivable: {why}")
    if transform == "identity":
        return unit
    if transform in ("reciprocal", "ln2_over"):
        inner = unit[2:] if unit.startswith("1/") else unit
        if not inner or any(c in inner for c in COMPOUND):
            raise ValueError(f"the reciprocal of the compound unit {unit!r} is not derivable: {why}")
        return inner if unit.startswith("1/") else f"1/{unit}"
    raise ValueError(f"transform must be one of {', '.join(TRANSFORMS)}, not {transform!r}")


def _projection_path(root: Path, rel) -> Path:
    return pd.orchestrator_json(root, rel, "projection")


def _read(root: Path, rel: str, what: str) -> bytes:
    """The bytes of the runs/orchestrator/<purpose>/<name>.json `what` file."""
    return pd.orchestrator_json(root, rel, what).read_bytes()


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


# ---------------------------------------------------------------- draft

def draft(root: Path, purpose: str, *, capture: str, design: str, design_approval: str, dictionary: str | None = None,
          sources: list = (), data: list = ()) -> Path:
    """Write a projection skeleton bound to the capture, the dictionary (default: the physicell_dictionary.json
    beside the design) and the approved design; `entries` and `unmapped` are left for the researcher review.
    `sources` = [(id, path under inputs/)], `data` = [(id, data session, proposal id, approval id)]."""
    root = Path(root)
    state.identifier(purpose)
    target = root / "runs" / "orchestrator" / purpose / NAME
    message = f"{target.relative_to(root).as_posix()} exists; drafts never overwrite a projection"
    if target.exists() or target.is_symlink():
        raise ValueError(message)
    sx.capture(root, capture)
    manifest = root / capture / "capture.json"
    reject_symlinks(manifest, root)
    design_sha = _sha(_read(root, design, "design"))
    approvals.require_physicell_design_approval(root, design_approval, design, design_sha)
    if dictionary is None:
        dictionary = PurePosixPath(design).with_name(DICTIONARY).as_posix()
    dictionary_sha = _sha(_read(root, dictionary, "dictionary"))
    declared, proposals = pd.declare_inputs(root, sources, data)
    projection = {"kind": "physicell_projection", "schema_version": 1,
                  "capture": {"path": capture, "sha256": _sha(manifest.read_bytes())},
                  "dictionary": {"path": dictionary, "sha256": dictionary_sha},
                  "design": {"path": design, "sha256": design_sha, "approval": design_approval},
                  "sources": declared, "data": proposals, "entries": [], "unmapped": []}
    try:
        pd.write_new(root, target, json.dumps(projection, indent=2) + "\n")
    except FileExistsError as exc:
        raise ValueError(message) from exc
    return target


# ---------------------------------------------------------------- check

def _ref_key(ref) -> str:
    return json.dumps(ref, sort_keys=True)


class _Checker(pd.Citations):
    def __init__(self, root: Path):
        super().__init__(root, has_basis=True)
        self.capture: dict | None = None
        self.session: str | None = None
        self.xml = None
        self.parents: dict = {}
        self.design_ref: dict | None = None
        self.maboss: dict | None = None        # cell -> {"inputs": [...], "outputs": [...]}
        self.signals: list | None = None
        self.behaviors: dict | None = None
        self.used: set = set()
        self.targets: dict = {}
        self.writers: dict = {}               # (cell, behavior) -> [(kind, label)] of entries
        self.captured_writers: dict = {}      # (cell, behavior) -> description of the captured XML output link
        self.captured_inputs: dict = {}       # (cell, signal, node) -> description of the captured input link

    def head(self, data: dict) -> None:
        if data.get("kind") != "physicell_projection" or type(data.get("schema_version")) is not int \
                or data.get("schema_version") != 1:
            self.error('kind must be "physicell_projection" and schema_version 1')

    # -- bindings

    def capture_section(self, ref) -> None:
        if not isinstance(ref, dict) or set(ref) != {"path", "sha256"}:
            self.error("capture must be exactly {path, sha256}")
            return
        try:
            bound = sx.capture(self.root, ref["path"])
            manifest = self.root / ref["path"] / "capture.json"
            reject_symlinks(manifest, self.root)
            if _sha(manifest.read_bytes()) != ref["sha256"]:
                raise ValueError("its capture.json sha256 differs from the projection's")
            raw = (self.root / bound["xml_path"]).read_bytes()
            if _sha(raw) != bound["xml_sha256"]:
                raise ValueError("its settings XML changed while being read")
            self.xml = sx.parse_bytes(raw)
        except (ValueError, OSError, UnicodeDecodeError) as exc:
            self.xml = None
            self.error(f"capture {ref['path']!r}: {exc}")
            return
        self.parents = {child: parent for parent in self.xml.iter() for child in parent}
        self.capture = ref
        self.session = bound["session"]
        if bound["rules_path"] is not None:
            self.error(f"capture {ref['path']!r} has an enabled rules ruleset; {sx.RULES_RELOAD}")
        for definition in self.xml.findall("cell_definitions/cell_definition"):
            cell = definition.get("name")
            for link in definition.findall("phenotype/intracellular/mapping/input"):
                signal, node = link.get("physicell_name"), link.get("intracellular_name")
                self.captured_inputs.setdefault(
                    (cell, signal, node), f"captured XML input link {signal} -> {node} on {cell}")
            for link in definition.findall("phenotype/intracellular/mapping/output"):
                node, behavior = link.get("intracellular_name"), link.get("physicell_name")
                self.captured_writers[(cell, behavior)] = f"captured XML output link {node} -> {behavior} on {cell}"

    def design_section(self, ref) -> None:
        if not isinstance(ref, dict) or set(ref) != {"path", "sha256", "approval"} \
                or not all(pd.is_text(v) for v in ref.values()):
            self.error("design must be exactly {path, sha256, approval} (non-empty strings)")
            return
        try:
            design, digest, problems = pd.load_and_check(self.root, ref["path"])
        except (ValueError, OSError, UnicodeDecodeError) as exc:
            self.error(f"design {ref['path']!r}: {exc}")
            return
        if digest != ref["sha256"]:
            self.error(f"design {ref['path']}: its sha256 is {digest}, not the projection's {ref['sha256']}")
            return
        if problems:
            self.error(f"design {ref['path']} no longer passes its check: " + "; ".join(problems))
            return
        try:
            record = approvals.require_physicell_design_approval(self.root, ref["approval"], ref["path"], digest)
        except (ValueError, OSError) as exc:
            self.error(f"design approval {ref['approval']}: {exc}")
            return
        if self.session is not None:
            try:  # the design's session is a lineage anchor: the capture's session must have been built from it
                lineage.dispatched_with(self.root, self.session, "design", ref["path"], digest)
            except (ValueError, OSError) as exc:
                self.error(f"design {ref['path']}: the capture's session {self.session} was not dispatched with it: "
                           f"{exc}")
        self.design_ref = ref
        self.maboss = {}
        for item in design["intracellular"]:
            handoff = item["maboss"]
            try:
                raw = state.artifact(self.root, handoff["handoff"]).read_bytes()
                if _sha(raw) != handoff["sha256"]:
                    raise ValueError("it changed since the design check")
                manifest = strict_json(raw)
                network = manifest["network"]
                readouts = list(manifest.get("readouts") or {})
                self.maboss[item["cell_definition"]] = {
                    "inputs": list(network["nodes"]),
                    "outputs": list(dict.fromkeys(list(network["output_nodes"]) + readouts))}
            except (ValueError, OSError, KeyError, TypeError) as exc:
                self.error(f"design MaBoSS handoff {handoff['handoff']}: {exc}")

    def dictionary_section(self, ref, design_ref) -> None:
        if not isinstance(ref, dict) or set(ref) != {"path", "sha256"}:
            self.error("dictionary must be exactly {path, sha256}")
            return
        try:
            if PurePosixPath(str(ref["path"])).name != DICTIONARY:
                raise ValueError(f"it must be a {DICTIONARY} recorded by capture_dictionary.py")
            raw = _read(self.root, ref["path"], "dictionary")
            if _sha(raw) != ref["sha256"]:
                raise ValueError("its sha256 differs from the projection's")
            data = strict_json(raw)
            if not isinstance(data, dict) or data.get("kind") != "physicell_dictionary" \
                    or type(data.get("schema_version")) is not int or data["schema_version"] != 1:
                raise ValueError('kind must be "physicell_dictionary" and schema_version 1')
            if self.capture is not None and data.get("capture") != self.capture:
                raise ValueError(f"it was captured for {data.get('capture')!r}, not the projection's capture")
            if isinstance(design_ref, dict) and data.get("design") != {k: design_ref.get(k) for k in ("path", "sha256")}:
                raise ValueError(f"it was captured for design {data.get('design')!r}, not the projection's design")
            if self.session is not None and data.get("session") != self.session:
                raise ValueError(f"its session {data.get('session')!r} is not the capture's session {self.session}")
            source = state.artifact(self.root, data["invocation"]).read_bytes()
            if _sha(source) != data["source_sha256"]:
                raise ValueError(f"its invocation record {data['invocation']} changed since capture")
            signals = [item["name"] for item in data["signals"]]
            behaviors = {cell: [item["name"] for item in items] for cell, items in data["behaviors"].items()}
        except (ValueError, OSError, KeyError, TypeError, AttributeError) as exc:
            self.error(f"dictionary {ref.get('path')!r}: {exc}")
            return
        self.signals, self.behaviors = signals, behaviors

    # -- names

    def known(self, label: str, what: str, name, choices, scope: str = "") -> bool:
        if choices is None:
            return False  # the binding that lists them already failed
        if isinstance(name, str) and name in choices:
            return True
        close = difflib.get_close_matches(name, list(choices), n=3) if isinstance(name, str) else []
        hint = f"closest: {', '.join(close)}" if close else "no close match"
        self.error(f"{label}: unknown {what} {name!r}{scope} ({hint})")
        return False

    def cell(self, label: str, name) -> bool:
        if not self.known(label, "cell definition", name, self.behaviors):
            return False
        if self.xml is not None and name not in sx.cell_definitions(self.xml):
            self.error(f"{label}: cell definition {name!r} is not in the captured XML")
            return False
        return True

    def behavior(self, label: str, cell_ok: bool, cell, name) -> None:
        if cell_ok:
            self.known(label, "behavior", name, self.behaviors.get(cell), f" of {cell}")

    def node(self, label: str, cell_ok: bool, cell, name, side: str) -> None:
        if not cell_ok or self.maboss is None:
            return
        if cell not in self.maboss:
            self.error(f"{label}: cell definition {cell} carries no MaBoSS model in the design")
            return
        what = "MaBoSS node" if side == "inputs" else "MaBoSS output node or readout"
        self.known(label, what, name, self.maboss[cell][side], f" of {cell}")

    # -- values

    def quantity(self, label: str, key: str, q) -> float | None:
        if not isinstance(q, dict) or not set(QUANTITY) <= set(q) or set(q) - set(QUANTITY) - {"target_unit"}:
            self.error(f"{label}: {key} must be {{value, value_unit, transform, factor}} and optionally target_unit")
            return None
        try:
            converted_unit(q)
        except ValueError as exc:
            self.error(f"{label}: {key}: {exc}")
        try:
            return model_value(q)
        except ValueError as exc:
            self.error(f"{label}: {key}: {exc}")
            return None

    def integer(self, label: str, key: str, value, nullable: bool = False) -> None:
        if value is None and nullable:
            return
        if type(value) is not int or value < 0:
            self.error(f"{label}: {key} must be a non-negative integer{' or null' if nullable else ''}, "
                       f"not {value!r}")

    def choice(self, label: str, key: str, value, allowed) -> None:
        if value not in allowed:
            self.error(f"{label}: {key} must be one of {', '.join(allowed)}, not {value!r}")

    def once(self, label: str, key, what: str) -> None:
        if key in self.targets:
            self.error(f"{label}: {what} set twice (also {self.targets[key]})")
        else:
            self.targets[key] = label

    # -- entries

    def cite_from(self, label: str, refs) -> None:
        items = refs if isinstance(refs, list) else [refs]
        if not items:
            self.error(f"{label}: from must be a citation or a non-empty list of citations")
        for ref in items:
            self.cite(label, ref)
            if isinstance(ref, dict):
                self.used.add(_ref_key(ref))

    def physiboss_input(self, label: str, e: dict) -> None:
        cell = e["cell_definition"]
        ok = self.cell(label, cell)
        self.known(label, "signal", e["signal"], self.signals)
        self.node(label, ok, cell, e["node"], "inputs")
        self.choice(label, "action", e["action"], ACTIONS)
        self.quantity(label, "threshold", e["threshold"])
        self.integer(label, "smoothing", e["smoothing"])
        key = (cell, e["signal"], e["node"])
        if key in self.captured_inputs:
            self.error(f"{label}: input link {e['signal']} -> {e['node']} on {cell} already exists "
                       f"({self.captured_inputs[key]})")
        self.once(label, ("input", *key), f"link {e['signal']} -> {e['node']} on {cell}")

    def write_behavior(self, label: str, kind: str, cell: str, behavior: str) -> None:
        """One writer per (cell, behavior): an output link excludes every other writer; rules exclude outputs."""
        key = (cell, behavior)
        captured = self.captured_writers.get(key)  # only output links: a capture never carries rules (T5-d)
        if captured is not None:
            self.error(f"{label}: behavior {behavior!r} of {cell} is already written by {captured}")
        clashes = [other for other_kind, other in self.writers.get(key, ())
                   if kind == "physiboss_output" or other_kind == "physiboss_output"]
        if clashes:
            self.error(f"{label}: behavior {behavior!r} of {cell} is already written by {'; '.join(clashes)}")
        self.writers.setdefault(key, []).append((kind, label))

    def physiboss_output(self, label: str, e: dict) -> None:
        cell = e["cell_definition"]
        ok = self.cell(label, cell)
        self.node(label, ok, cell, e["node"], "outputs")
        self.behavior(label, ok, cell, e["behavior"])
        self.choice(label, "action", e["action"], ACTIONS)
        self.quantity(label, "value", e["value"])
        self.quantity(label, "base_value", e["base_value"])
        self.integer(label, "smoothing", e["smoothing"])
        self.integer(label, "steepness", e["steepness"], nullable=True)
        self.write_behavior(label, "physiboss_output", cell, e["behavior"])

    def rule(self, label: str, e: dict) -> None:
        cell = e["cell_definition"]
        ok = self.cell(label, cell)
        self.known(label, "signal", e["signal"], self.signals)
        self.behavior(label, ok, cell, e["behavior"])
        self.choice(label, "direction", e["direction"], DIRECTIONS)
        for key, test, domain in (("saturation_value", lambda v: v >= 0, ">= 0"),
                                  ("half_max", lambda v: v > 0, "> 0"), ("hill_power", lambda v: v > 0, "> 0")):
            value = self.quantity(label, key, e[key])
            if value is not None and not test(value):
                self.error(f"{label}: {key} model value must be {domain}, not {value!r}")
        triple = (cell, e["signal"], e["behavior"])
        key = ("rule", *triple)
        if key in self.targets:
            self.error(f"{label}: duplicate rule for ({cell}, {e['signal']}, {e['behavior']}) "
                       f"(also {self.targets[key]})")
        else:
            self.targets[key] = label
        self.write_behavior(label, "rule", cell, e["behavior"])

    def setting(self, label: str, e: dict) -> None:
        if "target_unit_justification" in e and "target_unit" not in e:
            self.error(f"{label}: target_unit_justification without target_unit; give both or neither")
        if not pd.real_text(e["tool_hint"]):
            self.error(f"{label}: tool_hint must name the configurator tool to use, not {e['tool_hint']!r}")
        q = {k: e[k] for k in (*QUANTITY, "target_unit") if k in e}
        unit = None
        try:
            unit = converted_unit(q)
        except ValueError as exc:
            self.error(f"{label}: {exc}")
        try:
            model_value(q)
        except ValueError as exc:
            self.error(f"{label}: {exc}")
        if self.xml is None:
            return
        try:
            element = sx.resolve(self.xml, e["path"])
        except ValueError as exc:
            self.error(f"{label}: {exc}")
            return
        ancestor, child = self.parents.get(element), element
        while ancestor is not None:
            if child.tag == "intracellular" and ancestor.tag == "phenotype":
                self.error(f"{label}: path {e['path']} lies inside a cell definition's phenotype/intracellular, "
                           "which PhysiBoSS links own; use a physiboss_input or physiboss_output entry")
                return
            ancestor, child = self.parents.get(ancestor), ancestor
        if len(element):
            self.error(f"{label}: path {e['path']} must name a leaf element, not <{element.tag}> with children")
            return
        try:
            float((element.text or "").strip())
        except ValueError:
            self.error(f"{label}: <{element.tag}> at {e['path']} holds {element.text!r}, not a number")
        attribute = sx.units(element)
        if attribute is not None:
            if unit is not None and unit != attribute:
                self.error(f"{label}: converted unit {unit!r} is not the element's units {attribute!r} "
                           f"(compared exactly as spelled)")
        else:
            if "target_unit" not in e:
                self.error(f"{label}: <{element.tag}> has no units attribute: give target_unit and "
                           f"target_unit_justification")
            elif not pd.real_text(e.get("target_unit_justification")):
                self.error(f"{label}: <{element.tag}> has no units attribute: target_unit needs a non-empty "
                           f"target_unit_justification")
            parent = self.parents.get(element)
            declared = sx.units(parent) if parent is not None else None
            if declared is not None and unit is not None and unit != declared:
                self.error(f"{label}: target_unit {unit!r} differs from the units {declared!r} its parent "
                           f"<{parent.tag}> declares")
        self.once(label, ("element", element), f"element {e['path']}")

    def entries_section(self, items) -> list:
        if not isinstance(items, list):
            self.error("entries must be a list")
            return []
        ids = []
        for n, e in enumerate(items):
            label = f"entries[{n}]"
            if not isinstance(e, dict) or e.get("kind") not in KINDS:
                self.error(f"{label}: kind must be one of {', '.join(KINDS)}")
                continue
            entry_id = e.get("entry_id")
            label = f"{label} ({entry_id})"
            kind = e["kind"]
            required = set(COMMON) | set(KINDS[kind])
            if not required <= set(e) or set(e) - required - set(OPTIONAL.get(kind, ())):
                optional = OPTIONAL.get(kind, ())
                self.error(f"{label}: a {kind} entry has exactly {', '.join(COMMON + KINDS[kind])}"
                           + (f" and optionally {', '.join(optional)}" if optional else ""))
                continue
            if not isinstance(entry_id, str) or not state.ID.fullmatch(entry_id):
                self.error(f"{label}: entry_id must be a safe identifier")
            elif entry_id in ids:
                self.error(f"{label}: duplicate entry_id {entry_id}")
            else:
                ids.append(entry_id)
            if not pd.real_text(e["justification"]):
                self.error(f"{label}: justification must be a non-empty reason, not empty or TODO")
            self.cite_from(label, e["from"])
            for key in ("cell_definition", "signal", "node", "behavior", "path"):
                if key in KINDS[kind] and not isinstance(e[key], str):
                    self.error(f"{label}: {key} must be text, not {e[key]!r}")
                    break
            else:
                getattr(self, kind)(label, e)
        return ids

    def unmapped_section(self, items) -> None:
        if not isinstance(items, list):
            self.error("unmapped must be a list of {from, reason}")
            return
        seen = set()
        for n, item in enumerate(items):
            label = f"unmapped[{n}]"
            if not isinstance(item, dict) or set(item) != {"from", "reason"}:
                self.error(f"{label} must have exactly from and reason")
                continue
            ref = item["from"]
            if not isinstance(ref, dict):
                self.error(f"{label}: from must be one {{source, locator}} or {{data, locator}} citation")
                continue
            self.cite(label, ref)
            key = _ref_key(ref)
            if key in self.used:
                self.error(f"{label}: {ref} is used by an entry; it cannot also be unmapped")
            if key in seen:
                self.error(f"{label}: {ref} is listed twice")
            seen.add(key)
            if not pd.real_text(item["reason"]):
                self.error(f"{label}: reason must be a non-empty explanation, not empty or TODO")


def _load(root: Path, rel: str) -> tuple[dict, str]:
    raw = _projection_path(root, rel).read_bytes()
    data = strict_json(raw)
    if not isinstance(data, dict):
        raise ValueError("projection must be a JSON object")
    return data, _sha(raw)


def load_and_check(root: Path, rel: str) -> tuple[dict, str, list[str]]:
    """(projection, sha256 of exactly those bytes, every problem); ValueError if it cannot be read at all."""
    root = Path(root)
    data, digest = _load(root, rel)
    checker = _Checker(root)
    checker.head(data)
    if set(data) != set(KEYS):
        checker.error(f"projection keys must be exactly {', '.join(KEYS)}")
    else:
        checker.capture_section(data["capture"])
        checker.design_section(data["design"])
        checker.dictionary_section(data["dictionary"], data["design"])
        checker.sources.declare(data["sources"], "sources")
        checker.data_section(data["data"])
        checker.entries_section(data["entries"])
        checker.unmapped_section(data["unmapped"])
        checker.sources.uncited()
        checker.uncited_data()
    return data, digest, checker.errors


def check(root: Path, rel: str) -> list[str]:
    """Every problem with the projection, one message each; [] when it is valid."""
    try:
        return load_and_check(root, rel)[2]
    except (UnicodeDecodeError, OSError) as exc:
        raise ValueError(f"cannot read {rel}: {exc}") from exc


# ---------------------------------------------------------------- CLI

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    d = sub.add_parser("draft")
    d.add_argument("--purpose", required=True)
    d.add_argument("--capture", required=True, help="runs/multicellular-configurator/<session>/configs/<capture>")
    d.add_argument("--design", required=True, help="runs/orchestrator/<purpose>/physicell_design.json")
    d.add_argument("--design-approval", required=True, metavar="RECORD_ID")
    d.add_argument("--dictionary", help=f"default: the {DICTIONARY} beside the design")
    d.add_argument("--source", action="append", default=[], metavar="ID:PATH")
    d.add_argument("--data", action="append", default=[], metavar="ID:SESSION:PROPOSAL:APPROVAL")
    c = sub.add_parser("check")
    c.add_argument("projection")
    args = parser.parse_args(argv)
    root = project_root().resolve()
    try:
        if args.command == "draft":
            sources, data = pd.parse_input_args(args.source, args.data)
            path = draft(root, args.purpose, capture=args.capture, design=args.design,
                         design_approval=args.design_approval, dictionary=args.dictionary, sources=sources,
                         data=data)
            print(f"physicell projection drafted: {path.relative_to(root).as_posix()} sha256={sha256_file(path)}")
            return 0
        errors = check(root, args.projection)
        for error in errors:
            print(error, file=sys.stderr)
        if errors:
            return 1
        print(f"physicell projection check passed: {args.projection}")
        return 0
    except (ValueError, OSError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
