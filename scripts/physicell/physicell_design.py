#!/usr/bin/env python3
"""Draft and check the stage-A PhysiCell design: the researcher-approved multicellular structure.

The design (runs/orchestrator/<purpose>/physicell_design.json) fixes which cell definitions (and
variants) and substrates the multicellular-configurator builds, which cell definition carries which
exported MaBoSS model, and how each behavior group is coupled. Stdlib only.

Schema (schema_version 1; every key below is required):
  kind: "physicell_design"
  basis: {capture} | null        capture = runs/multicellular-configurator/<session>/configs/<capture>, a
                                 recorded configuration (settings_xml.capture verifies its hashes) with no input
                                 files (no capture dependencies: server 2.5.2 cannot reload a rules CSV, MaBoSS or
                                 initial-cell files into a new session); null = a new configuration
  session: str                   the design's lineage anchor, never a live session: the basis capture's session with
                                 a basis, else the purpose <p> (this file's folder name). Every configurator run is
                                 a new session; the stage-A capture is bound to this design by its dispatch lineage
                                 (lineage.dispatched_with: its invocation task.txt line 2 is this design's
                                 DATA_INPUT line), not by session. The physicell_design approval's session_id and
                                 the DATA_INPUT record_session_id equal this anchor.
  domain: null | {domain_x, domain_y, use_2d, domain_z, dx, max_time, from}
                                 every argument of the server's create_simulation_domain, explicitly (ruling T9-a):
                                 domain_x, domain_y, dx = {value > 0, unit: "micron"}, max_time = {value > 0, unit:
                                 "min"} (the units the exported XML writes: overall/space_units and max_time@units),
                                 use_2d a boolean, domain_z a {value > 0, unit: "micron"} without use_2d and null
                                 with it (the server then makes z one voxel, dx, thick); `from` cites them.
                                 Required when basis is null; must be null with a basis (the basis holds the domain)
  cycle_models: [{key, name}] | null  the server's cycle models, copied VERBATIM from the configurator's discovery run
                                 (get_available_cycle_models: key = the add_single_cell_type argument, name = the
                                 <cycle name> the exported XML writes); never typed from memory. Required whenever a
                                 cell definition is not from "basis" (check refuses: record the server's cycle models
                                 first)
  cell_definitions: [{name, cycle_model, description, from, variant_of?}]
                                 cycle_model: a cycle_models key, or for a "basis" cell its captured <cycle name>
  substrates: [{name, units, description, from, diffusion_coefficient, decay_rate, initial_condition, dirichlet}]
                                 every argument of add_single_substrate, explicitly: diffusion_coefficient,
                                 decay_rate, initial_condition = {value, unit, from}; dirichlet = {enabled: bool,
                                 value: {value, unit, from} | null (only when disabled)}; `units` is the concentration
                                 label. The units must equal what the exported XML writes: diffusion "micron^2/min",
                                 decay "1/min"; for an added substrate initial_condition and Dirichlet "mmHg" (server
                                 2.5.2 writes mmHg there whatever the label, so the label must be "mmHg" too); for a
                                 "basis" substrate the captured units and values, each value from "basis".
                                 "substrate" is the library default the server inserts with the domain: a new
                                 configuration cannot add it
  Every basis cell definition and substrate is declared (from "basis").
  intracellular: [{cell_definition, maboss: {handoff, sha256}}]
                                 handoff = an exported, approved maboss-to-physicell manifest; it is validated
                                 with gate.check("multicellular", ...), the gate's own check
  coupling_plan: [{cell_definition, behavior_group, route: physiboss|rules|none, notes}]
  sources: [{id, path, sha256}]  researcher files under inputs/
  data: [{id, session, proposal, approval}]  approved data-analyst proposals
  unmapped: [{item, reason}]
  variant_of: a variant is created from its base cell definition; how it differs from the base is NOT part of
  this design, it is the later stage-B patches. variant_of chains must not form a cycle.
  `from` is "basis" (the name exists in the capture and keeps its cycle model/units), a citation
  {source, locator} or {data, locator} of a declared source / data proposal.
A "TODO" in any reason, description, units, value or note is refused. Units are never inferred: substrate units are
copied only from the captured XML, else TODO; a unit other than the one the exported XML writes is refused.
The stage-A capture is compared with the design before its dictionary is recorded (capture_dictionary.conformance).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path, PurePosixPath

if __package__ in (None, ""):  # run as a script: make the repo importable
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts import gate, state  # noqa: E402
from scripts.data import approvals  # noqa: E402
from scripts.data import sources as user_sources  # noqa: E402
from scripts.data.common import finite_number, project_root, reject_symlinks, sha256_file, strict_json  # noqa: E402
from scripts.physicell import settings_xml as sx  # noqa: E402

KEYS = ("kind", "schema_version", "basis", "session", "domain", "cycle_models", "cell_definitions", "substrates",
        "intracellular", "coupling_plan", "sources", "data", "unmapped")
ORCHESTRATOR = ("runs", "orchestrator")
DOMAIN_KEYS = ("domain_x", "domain_y", "use_2d", "domain_z", "dx", "max_time", "from")
# The unit strings the exported XML writes (server 2.5.2 / physicell_config): overall/space_units, max_time@units,
# physical_parameter_set/diffusion_coefficient@units and decay_rate@units; add_single_substrate passes no initial
# unit, so initial_condition@units and Dirichlet_boundary_condition@units are always "mmHg" for an added substrate.
LENGTH_UNIT, TIME_UNIT = "micron", "min"
DIFFUSION_UNIT, DECAY_UNIT = "micron^2/min", "1/min"
ADDED_CONCENTRATION_UNIT = "mmHg"
LIBRARY_SUBSTRATE = "substrate"  # the library default create_simulation_domain inserts (origin library_default)
SUBSTRATE_KEYS = ("name", "units", "description", "from", "diffusion_coefficient", "decay_rate", "initial_condition",
                  "dirichlet")
SUBSTRATE_VALUES = ("diffusion_coefficient", "decay_rate", "initial_condition")
ROUTES = ("physiboss", "rules", "none")
MABOSS_RUNS = ("runs", "boolean-dynamics-modeler")
DATA_KEYS = {"id", "session", "proposal", "approval"}
BASIS_FROM = "basis"
TODO = "TODO"


def is_text(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def real_text(value) -> bool:
    return is_text(value) and value.strip().upper() != TODO


def orchestrator_json(root: Path, rel, what: str = "design") -> Path:
    """The existing runs/orchestrator/<purpose>/<name>.json file `rel` names (no traversal or symlinks)."""
    parts = PurePosixPath(rel).parts if isinstance(rel, str) and rel and "\\" not in rel else ()
    if len(parts) != 4 or parts[:2] != ORCHESTRATOR or not rel.endswith(".json"):
        raise ValueError(f"{what} must be runs/orchestrator/<purpose>/<name>.json, not {rel!r}")
    state.identifier(parts[2])
    return state.artifact(root, rel)


def _basis_facts(root: Path, capture_rel: str) -> dict:
    """{session, cell_definitions: {name: cycle model}, substrates: {name: units|None}, values: {name:
    settings_xml.substrate_facts}} of a verified capture."""
    return _facts(*sx.capture_tree(root, capture_rel))  # the tree of exactly the verified bytes


def _facts(bound: dict, tree) -> dict:
    """_basis_facts of a capture bound once (settings_xml.capture_tree: the binding and its verified tree)."""
    cells = {name: sx.cycle_model(tree, name) for name in sx.cell_definitions(tree)}
    values = sx.substrate_facts(tree)
    subs = {name: facts["units"] for name, facts in values.items()}
    return {"session": bound["session"], "cell_definitions": cells, "substrates": subs, "values": values}


def _basis_value(fact: dict | None) -> dict:
    """A {value, unit, from: "basis"} copied from a captured element, TODO where it holds no number or unit."""
    value = sx.number(fact["text"]) if fact else None
    unit = fact.get("units") if fact else None
    return {"value": TODO if value is None else value, "unit": unit or TODO, "from": BASIS_FROM}


def _basis_substrate(name: str, facts: dict) -> dict:
    dirichlet = facts["dirichlet"]
    enabled = (dirichlet or {}).get("enabled")
    return {"name": name, "units": facts["units"] or TODO, "description": TODO, "from": BASIS_FROM,
            **{key: _basis_value(facts[key]) for key in SUBSTRATE_VALUES},
            "dirichlet": {"enabled": {"true": True, "false": False}.get((enabled or "").lower(), TODO),
                          "value": _basis_value(dirichlet)}}


def _basis_input_files(bound: dict) -> list:
    """The dependency paths (package/config/...) of a capture bound by settings_xml.capture, from the manifest it
    verified (it refuses a `dependencies` that is not a list of {path, sha256, ...}); nothing is read again."""
    return [dep["path"] for dep in bound["manifest"]["dependencies"]]


def handoff_manifest(root: Path, ref: dict) -> dict:
    """The MaBoSS handoff manifest a design pins (ref = its intracellular {handoff, sha256}): read once, its sha256
    must be ref["sha256"] and the object is parsed (strict_json) from those very bytes; ValueError otherwise. The one
    reader of a design handoff after gate.check: the design check, conformance and the projection check use it."""
    raw = state.artifact(Path(root), ref["handoff"]).read_bytes()
    if hashlib.sha256(raw).hexdigest() != ref["sha256"]:
        raise ValueError("it changed since the design check")
    manifest = strict_json(raw)
    if not isinstance(manifest, dict):
        raise ValueError("it is not a JSON object")
    return manifest


def write_new(root: Path, path: Path, text: str) -> None:
    reject_symlinks(path, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    reject_symlinks(path, root)
    with path.open("x", encoding="utf-8") as handle:
        handle.write(text)


def declare_inputs(root: Path, sources, data) -> tuple[list, list]:
    """(sources, data) declarations for a draft: sources = [(id, path under inputs/)] (sha256 computed now),
    data = [(id, data session, proposal id, approval id)], each proposal checked as approved."""
    declared, proposals = [], []
    for source_id, rel in sources:
        if any(item["id"] == source_id for item in declared):
            raise ValueError(f"source id {source_id} is listed twice")
        declared.append(user_sources.entry(root, source_id, rel))
    for data_id, data_session, proposal, approval in data:
        if any(item["id"] == data_id for item in proposals):
            raise ValueError(f"data id {data_id} is listed twice")
        record = approvals.require_data_approval(root, approval, data_session)
        approvals.require_proposal_approved(root, record, data_session, proposal)
        proposals.append({"id": data_id, "session": data_session, "proposal": proposal, "approval": approval})
    return declared, proposals


def draft(root: Path, purpose: str, *, basis_capture: str | None, sources: list = (), data: list = ()) -> Path:
    """Write a skeleton design. With a basis, cell definitions and substrates are pre-filled from the capture
    (description TODO, from "basis") and the session is the capture's; without one the domain is a TODO skeleton
    and the session is the purpose. `sources` = [(id, path under inputs/)],
    `data` = [(id, data session, proposal id, approval id)]."""
    root = Path(root)
    state.identifier(purpose)
    target = root / ORCHESTRATOR[0] / ORCHESTRATOR[1] / purpose / "physicell_design.json"
    if target.exists() or target.is_symlink():
        raise ValueError(f"{target.relative_to(root).as_posix()} exists; drafts never overwrite a design")
    declared, proposals = declare_inputs(root, sources, data)
    cells, subs, session = [], [], purpose
    if basis_capture is not None:
        facts = _basis_facts(root, basis_capture)
        session = facts["session"]
        cells = [{"name": n, "cycle_model": c, "description": TODO, "from": BASIS_FROM}
                 for n, c in facts["cell_definitions"].items()]
        subs = [_basis_substrate(n, f) for n, f in facts["values"].items()]
    length = {"value": TODO, "unit": LENGTH_UNIT}
    domain = None if basis_capture is not None else {
        "domain_x": dict(length), "domain_y": dict(length), "use_2d": TODO, "domain_z": TODO, "dx": dict(length),
        "max_time": {"value": TODO, "unit": TIME_UNIT}, "from": TODO}
    design = {"kind": "physicell_design", "schema_version": 1,
              "basis": {"capture": basis_capture} if basis_capture is not None else None,
              "session": session, "domain": domain, "cycle_models": None,
              "cell_definitions": cells, "substrates": subs, "intracellular": [], "coupling_plan": [],
              "sources": declared, "data": proposals, "unmapped": []}
    try:
        write_new(root, target, json.dumps(design, indent=2) + "\n")
    except FileExistsError as exc:
        raise ValueError(f"{target.relative_to(root).as_posix()} exists; drafts never overwrite a design") from exc
    return target


# ---------------------------------------------------------------- check

class Citations:
    """Declared sources and approved data proposals and the `from` refs citing them; problems go to `errors`.

    Shared by the stage-A design and the stage-B projection. A ref is "basis" (allowed only when `has_basis`),
    {source, locator} or {data, locator}; `cite` validates and counts it, `uncited_data` and
    `sources.uncited()` report declarations never cited."""

    def __init__(self, root: Path, has_basis: bool = False):
        self.root = root
        self.errors: list[str] = []
        self.sources = user_sources.Sources(root, self.error)
        self.data_ids: dict[str, int] = {}
        self.has_basis = has_basis

    def error(self, message: str) -> None:
        self.errors.append(message)

    def cite(self, label: str, ref) -> None:
        if ref == BASIS_FROM:
            if not self.has_basis:
                self.error(f'{label}: from "basis" needs a basis')
            return
        if isinstance(ref, dict) and set(ref) == {"data", "locator"}:
            if not isinstance(ref["data"], str) or ref["data"] not in self.data_ids:
                self.error(f"{label}: data proposal {ref['data']} is not declared")
            else:
                self.data_ids[ref["data"]] += 1
            problem = user_sources.check_locator(ref["locator"])
            if problem:
                self.error(f"{label}: {problem}")
            return
        self.sources.ref(label, ref)

    def data_section(self, items) -> None:
        if not isinstance(items, list):
            self.error("data must be a list of {id, session, proposal, approval}")
            return
        for n, item in enumerate(items):
            here = f"data[{n}]"
            if not isinstance(item, dict) or set(item) != DATA_KEYS or not all(is_text(v) for v in item.values()):
                self.error(f"{here} must have exactly id, session, proposal, approval (non-empty strings)")
                continue
            if item["id"] in self.data_ids:
                self.error(f"{here}: duplicate data id {item['id']}")
                continue
            self.data_ids[item["id"]] = 0
            try:
                record = approvals.require_data_approval(self.root, item["approval"], item["session"])
                approvals.require_proposal_approved(self.root, record, item["session"], item["proposal"])
            except (ValueError, OSError) as exc:
                self.error(f"{here}: {exc}")

    def uncited_data(self) -> None:
        for data_id, count in self.data_ids.items():
            if not count:
                self.error(f"data proposal {data_id} is never cited")


def handoff_approval(recs: list, ref: dict) -> dict:
    """The researcher stage_approval a design's MaBoSS handoff is verified against (ref = its intracellular maboss
    {handoff, sha256}, a path under runs/boolean-dynamics-modeler/<session>/): the latest boolean-dynamics
    stage_approval of that session for the multicellular stage binding exactly this path and sha256, among the
    DECISIONS.md records `recs` (append-only, durable); ValueError if none. The design check runs gate.check with its
    outputs; lineage requires an upstream STAGE_GATE to name exactly those outputs."""
    rel, digest = ref["handoff"], ref["sha256"]
    session = PurePosixPath(rel).parts[2]
    candidates = [r for r in recs if r.get("kind") == "stage_approval" and r.get("session_id") == session
                  and r.get("stage") == "boolean-dynamics" and r.get("downstream_stage") == "multicellular"
                  and isinstance(r.get("artifact"), dict) and r["artifact"].get("path") == rel
                  and r["artifact"].get("sha256") == digest]
    if not candidates:
        raise ValueError("no researcher approval of this exact handoff path and sha256 for the multicellular stage")
    return candidates[-1]


class _Checker(Citations):
    def __init__(self, root: Path):
        super().__init__(root)
        self.basis: dict | None = None
        self.cycle_names: dict = {}
        self.cells: set = set()
        self.substrate_names: set = set()
        self.intracellular: set = set()

    def head(self, data: dict) -> None:
        if data.get("kind") != "physicell_design" or type(data.get("schema_version")) is not int \
                or data.get("schema_version") != 1:
            self.error('kind must be "physicell_design" and schema_version 1')

    def basis_section(self, basis, session, purpose: str) -> None:
        if basis is not None:
            if not isinstance(basis, dict) or set(basis) != {"capture"}:
                self.error("basis must be null or exactly {capture}")
            else:
                self.has_basis = True
                try:
                    bound, tree = sx.capture_tree(self.root, basis["capture"])  # bound once per check
                    if bound["rules_path"] is not None:
                        self.error(f"basis: capture {basis['capture']} has an enabled rules ruleset; {sx.RULES_RELOAD}")
                    else:
                        files = _basis_input_files(bound)
                        if files:
                            self.error(f"basis: capture {basis['capture']} has input files ({', '.join(files)}); "
                                       f"{sx.INPUT_FILES_RELOAD}")
                    self.basis = _facts(bound, tree)
                except (ValueError, OSError) as exc:
                    self.error(f"basis: {exc}")
        if not isinstance(session, str) or not state.ID.fullmatch(session):
            self.error(f"session must be the design's anchor (the basis capture's session, else the purpose "
                       f"{purpose}), not {session!r}")
        elif self.basis is not None and session != self.basis["session"]:
            self.error(f"session {session} is not the basis capture's session {self.basis['session']}")
        elif basis is None and session != purpose:
            self.error(f"session {session} must be the purpose {purpose} for a design without a basis (a lineage "
                       "anchor; the stage-A session is bound by its DATA_INPUT dispatch, not declared here)")

    def measure(self, label: str, q, unit: str, keys=("value", "unit")) -> float | None:
        """A {value > 0, unit} (plus `from` when in keys) whose unit is exactly `unit`; its value, else None."""
        if not isinstance(q, dict) or set(q) != set(keys):
            self.error(f"{label} must be exactly {{{', '.join(keys)}}}, not {q!r}")
            return None
        if q["unit"] != unit:
            self.error(f"{label}.unit must be exactly {unit!r} (the unit the exported XML writes), not {q['unit']!r}")
        if not finite_number(q["value"]) or q["value"] <= 0:
            self.error(f"{label}.value must be a finite number > 0, not {q['value']!r}")
            return None
        return q["value"]

    def domain_section(self, domain) -> None:
        if self.has_basis:
            if domain is not None:
                self.error("domain must be null with a basis (the captured configuration holds the domain)")
            return
        if not isinstance(domain, dict) or set(domain) != set(DOMAIN_KEYS):
            self.error(f"domain is required without a basis, with exactly the keys {', '.join(DOMAIN_KEYS)} "
                       "(every create_simulation_domain argument, never a tool default)")
            return
        for key in ("domain_x", "domain_y", "dx"):
            self.measure(f"domain.{key}", domain[key], LENGTH_UNIT)
        self.measure("domain.max_time", domain["max_time"], TIME_UNIT)
        if not isinstance(domain["use_2d"], bool):
            self.error(f"domain.use_2d must be true or false, not {domain['use_2d']!r}")
        elif domain["use_2d"]:
            if domain["domain_z"] is not None:
                self.error("domain.domain_z must be null with use_2d (the server makes z one voxel, dx, thick)")
        elif domain["domain_z"] is None:
            self.error("domain.domain_z is required without use_2d (a 3D domain)")
        else:
            self.measure("domain.domain_z", domain["domain_z"], LENGTH_UNIT)
        self.cite("domain", domain["from"])

    def cycle_models_section(self, models) -> None:
        self.cycle_names: dict = {}
        if models is None:
            return
        if not isinstance(models, list) or not all(
                isinstance(m, dict) and set(m) == {"key", "name"} and real_text(m["key"]) and real_text(m["name"])
                for m in models) or len({m["key"] for m in models}) != len(models):
            self.error("cycle_models must be null or a list of {key, name} with unique keys, copied verbatim from "
                       "get_available_cycle_models")
            return
        self.cycle_names = {m["key"]: m["name"] for m in models}

    def entity_common(self, label: str, item: dict) -> None:
        if not real_text(item.get("description")):
            self.error(f"{label}: description must be a non-empty reason, not empty or TODO")
        self.cite(label, item.get("from"))

    def cell_section(self, items) -> None:
        if not isinstance(items, list) or not items:
            self.error("cell_definitions must be a non-empty list")
            return
        base = self.basis["cell_definitions"] if self.basis else {}
        for n, item in enumerate(items):
            label = f"cell_definitions[{n}]"
            if not isinstance(item, dict) or not {"name", "cycle_model", "description", "from"} <= set(item) \
                    or set(item) - {"name", "cycle_model", "description", "from", "variant_of"}:
                self.error(f"{label} must have name, cycle_model, description, from and optionally variant_of")
                continue
            name = item["name"]
            label = f"{label} ({name})"
            if not real_text(name):
                self.error(f"{label}: name must be non-empty text")
                continue
            if name in self.cells:
                self.error(f"{label}: duplicate cell definition {name}")
                continue
            self.cells.add(name)
            self.entity_common(label, item)
            model = item["cycle_model"]
            if self.has_basis and item["from"] == BASIS_FROM:
                if self.basis is None:
                    pass  # the basis failed to load: already reported
                elif name not in base:
                    self.error(f'{label}: from "basis" but {name} is not in the basis capture')
                elif model != base[name]:
                    self.error(f"{label}: cycle model {model!r} differs from the basis ({base[name]!r})")
                continue
            if self.basis is not None and name in base:
                self.error(f'{label}: {name} is already in the basis; use from "basis" (change it by a patch later)')
            if not self.cycle_names:
                self.error(f"{label}: cycle_models is absent: record the server's cycle models first (a configurator "
                           "discovery run: get_available_cycle_models) before declaring a new cell definition")
            elif not isinstance(model, str) or model not in self.cycle_names:
                self.error(f"{label}: cycle model {model!r} is not one of the recorded cycle model keys "
                           f"{sorted(self.cycle_names)}")
        bases = {}
        for n, item in enumerate(items):
            variant = item.get("variant_of") if isinstance(item, dict) else None
            if variant is None or not isinstance(item.get("name"), str) or item["name"] not in self.cells:
                continue  # no variant, or an item already refused above
            if not isinstance(variant, str) or variant not in self.cells or variant == item["name"]:
                self.error(f"cell_definitions[{n}]: variant_of {variant!r} must name another declared cell definition")
            else:
                bases[item["name"]] = variant
        for name in bases:
            seen, node = [], name
            while node in bases and node not in seen:
                seen.append(node)
                node = bases[node]
            if node == name:
                self.error(f"cell_definitions ({name}): variant_of forms a cycle: {' -> '.join(seen + [name])}")

    def substrate_section(self, items) -> None:
        if not isinstance(items, list) or not items:
            self.error("substrates must be a non-empty list")
            return
        seen = self.substrate_names
        base = self.basis["substrates"] if self.basis else {}
        for n, item in enumerate(items):
            label = f"substrates[{n}]"
            if not isinstance(item, dict) or set(item) != set(SUBSTRATE_KEYS):
                self.error(f"{label} must have exactly {', '.join(SUBSTRATE_KEYS)} (every add_single_substrate "
                           "argument, never a tool default)")
                continue
            name = item["name"]
            label = f"{label} ({name})"
            if not real_text(name):
                self.error(f"{label}: name must be non-empty text")
                continue
            if name in seen:
                self.error(f"{label}: duplicate substrate {name}")
                continue
            seen.add(name)
            if name in self.cells:
                self.error(f"{label}: name is also a cell definition; names must be distinct")
            if not real_text(item["units"]):
                self.error(f"{label}: units must be stated (never inferred), not empty or TODO")
            self.entity_common(label, item)
            from_basis = self.has_basis and item["from"] == BASIS_FROM
            if from_basis:
                if self.basis is None:
                    continue  # the basis failed to load: already reported
                if name not in base:
                    self.error(f'{label}: from "basis" but {name} is not in the basis capture')
                    continue
                if base[name] is not None and item["units"] != base[name]:
                    self.error(f"{label}: units {item['units']!r} differ from the basis ({base[name]!r})")
                self.substrate_values(label, item, self.basis["values"][name])
                continue
            if name in base:
                self.error(f'{label}: {name} is already in the basis; use from "basis"')
            if not self.has_basis and name == LIBRARY_SUBSTRATE:
                self.error(f"{label}: {LIBRARY_SUBSTRATE!r} is the library default substrate the server inserts "
                           "with the domain (add_single_substrate cannot add it again); choose another name")
            if real_text(item["units"]) and item["units"] != ADDED_CONCENTRATION_UNIT:
                self.error(f"{label}: units {item['units']!r}: server 2.5.2 writes initial_condition and "
                           f"Dirichlet_boundary_condition units {ADDED_CONCENTRATION_UNIT!r} for an added substrate "
                           "whatever its label, so only a substrate in that unit can be added consistently (units are "
                           "never inferred)")
            self.substrate_values(label, item, None)

    def value(self, label: str, q, unit, basis: dict | None, minimum: float | None = None) -> None:
        """A substrate {value, unit, from}: unit exactly `unit` (what the exported XML writes); for a "basis" substrate
        `basis` is the captured element ({text, units}) and the value must equal it, from "basis"."""
        if not isinstance(q, dict) or set(q) != {"value", "unit", "from"}:
            self.error(f"{label} must be exactly {{value, unit, from}}, not {q!r}")
            return
        if not finite_number(q["value"]):
            self.error(f"{label}.value must be a finite number, not {q['value']!r}")
        elif minimum is not None and q["value"] < minimum:
            self.error(f"{label}.value must be >= {minimum:g}, not {q['value']!r}")
        if unit is None or q["unit"] != unit:
            self.error(f"{label}.unit must be exactly {unit!r} (the unit the exported XML writes; never inferred), "
                       f"not {q['unit']!r}")
        if basis is None:
            if q["from"] == BASIS_FROM:
                self.error(f'{label}: from "basis" only for a value of a "basis" substrate')
            else:
                self.cite(label, q["from"])
            return
        if q["from"] != BASIS_FROM:
            self.error(f'{label}: a "basis" substrate keeps its captured values: from must be "basis"')
        if finite_number(q["value"]) and not sx.close(sx.number(basis["text"]), q["value"]):
            self.error(f"{label}.value {q['value']!r} differs from the basis ({basis['text']!r})")

    def substrate_values(self, label: str, item: dict, facts: dict | None) -> None:
        def captured(key):
            return facts[key] if facts else None

        def unit(key, added):
            if facts is None:
                return added
            return (facts[key] or {}).get("units")

        missing = [k for k in (*SUBSTRATE_VALUES, "dirichlet") if facts is not None and facts[k] is None]
        if missing:
            self.error(f"{label}: the basis capture has no {', '.join(missing)} for this substrate")
            return
        self.value(f"{label}.diffusion_coefficient", item["diffusion_coefficient"],
                   unit("diffusion_coefficient", DIFFUSION_UNIT), captured("diffusion_coefficient"), 0)
        self.value(f"{label}.decay_rate", item["decay_rate"], unit("decay_rate", DECAY_UNIT), captured("decay_rate"),
                   0)
        self.value(f"{label}.initial_condition", item["initial_condition"],
                   unit("initial_condition", ADDED_CONCENTRATION_UNIT), captured("initial_condition"))
        dirichlet = item["dirichlet"]
        if not isinstance(dirichlet, dict) or set(dirichlet) != {"enabled", "value"} \
                or not isinstance(dirichlet["enabled"], bool):
            self.error(f"{label}.dirichlet must be exactly {{enabled: true|false, value}}, not {dirichlet!r}")
            return
        if facts is not None and dirichlet["enabled"] != ((facts["dirichlet"].get("enabled") or "").lower() == "true"):
            self.error(f"{label}.dirichlet.enabled differs from the basis ({facts['dirichlet'].get('enabled')!r})")
        if dirichlet["value"] is None:
            if dirichlet["enabled"]:
                self.error(f"{label}.dirichlet.value is required when the Dirichlet condition is enabled")
            elif facts is not None:
                self.error(f'{label}.dirichlet.value: a "basis" substrate keeps its captured value (from "basis")')
            return
        self.value(f"{label}.dirichlet.value", dirichlet["value"], unit("dirichlet", ADDED_CONCENTRATION_UNIT),
                   captured("dirichlet"))

    def basis_coverage(self) -> None:
        """Review M3: every basis cell definition and substrate is declared in the design (from "basis")."""
        if self.basis is None:
            return
        for name in self.basis["cell_definitions"]:
            if name not in self.cells:
                self.error(f'basis cell definition {name} is not declared: declare it with from "basis"')
        for name in self.basis["substrates"]:
            if name not in self.substrate_names:
                self.error(f'basis substrate {name} is not declared: declare it with from "basis"')

    def maboss_handoff(self, label: str, maboss, cell) -> None:
        if not isinstance(maboss, dict) or set(maboss) != {"handoff", "sha256"} \
                or not all(is_text(v) for v in maboss.values()):
            self.error(f"{label}: maboss must be exactly {{handoff, sha256}}")
            return
        rel, digest = maboss["handoff"], maboss["sha256"]
        parts = PurePosixPath(rel).parts if "\\" not in rel else ()
        if len(parts) < 4 or parts[:2] != MABOSS_RUNS:
            self.error(f"{label}: handoff must be a file under runs/boolean-dynamics-modeler/<session>/, not {rel!r}")
            return
        session = parts[2]
        try:
            approval = handoff_approval(state.records((self.root / "DECISIONS.md").read_text(encoding="utf-8")),
                                        maboss)
            # The gate's own check: registry row active+exported, approval of exact outputs, independent
            # review before approval, the manifest and its parent chain verified, artifact hash unchanged.
            result = gate.check(self.root, "multicellular", session, approval["outputs"])
            if result["artifact"] != {"path": rel, "sha256": digest}:
                raise ValueError("the gate's approved handoff is a different artifact (superseded re-export?)")
            # The gate verified these exact bytes; import attaches the model to the manifest's target cell type.
            manifest = handoff_manifest(self.root, maboss)
            target = (manifest.get("target") or {}).get("cell_type")
            if target != cell:
                raise ValueError(f"its target cell_type is {target!r} but it is bound to cell definition {cell!r}; "
                                 f"re-export the MaBoSS handoff for that cell type")
        except (ValueError, OSError, KeyError, TypeError) as exc:
            self.error(f"{label}: MaBoSS handoff {rel} is not an exported, approved maboss-to-physicell "
                       f"handoff: {exc}")

    def intracellular_section(self, items) -> None:
        if not isinstance(items, list):
            self.error("intracellular must be a list of {cell_definition, maboss}")
            return
        for n, item in enumerate(items):
            label = f"intracellular[{n}]"
            if not isinstance(item, dict) or set(item) != {"cell_definition", "maboss"}:
                self.error(f"{label} must have exactly cell_definition and maboss")
                continue
            cell = item["cell_definition"]
            if not isinstance(cell, str) or cell not in self.cells:
                self.error(f"{label}: cell definition {cell!r} is not declared")
            elif cell in self.intracellular:
                self.error(f"{label}: cell definition {cell} carries two MaBoSS models")
            else:
                self.intracellular.add(cell)
            self.maboss_handoff(label, item["maboss"], cell)

    def coupling_section(self, items) -> None:
        if not isinstance(items, list):
            self.error("coupling_plan must be a list of {cell_definition, behavior_group, route, notes}")
            return
        seen = set()
        for n, item in enumerate(items):
            label = f"coupling_plan[{n}]"
            if not isinstance(item, dict) or set(item) != {"cell_definition", "behavior_group", "route", "notes"}:
                self.error(f"{label} must have exactly cell_definition, behavior_group, route, notes")
                continue
            cell, group, route = item["cell_definition"], item["behavior_group"], item["route"]
            declared = isinstance(cell, str) and cell in self.cells
            if not declared:
                self.error(f"{label}: cell definition {cell!r} is not declared")
            if not real_text(group):
                self.error(f"{label}: behavior_group must be non-empty text")
            if not isinstance(route, str) or route not in ROUTES:
                self.error(f"{label}: route must be one of {', '.join(ROUTES)}, not {route!r}")
            elif route == "physiboss" and declared and cell not in self.intracellular:
                self.error(f"{label}: route physiboss needs an intracellular MaBoSS model on {cell}")
            if not real_text(item["notes"]):
                self.error(f"{label}: notes must be a non-empty explanation, not empty or TODO")
            if not declared or not isinstance(group, str):
                continue  # already refused: no duplicate test on a value that is not text
            if (cell, group) in seen:
                self.error(f"{label}: duplicate coupling for {cell!r}, {group!r}")
            seen.add((cell, group))

    def unmapped_section(self, items) -> None:
        if not isinstance(items, list):
            self.error("unmapped must be a list")
            return
        for n, item in enumerate(items):
            if not isinstance(item, dict) or set(item) != {"item", "reason"} or not is_text(item["item"]):
                self.error(f"unmapped[{n}] must have exactly item and reason")
            elif not real_text(item["reason"]):
                self.error(f"unmapped[{n}] ({item['item']}): reason must be a non-empty explanation, not empty or TODO")


def _load(root: Path, rel: str) -> tuple[dict, str]:
    raw = orchestrator_json(root, rel).read_bytes()
    data = strict_json(raw)
    if not isinstance(data, dict):
        raise ValueError("design must be a JSON object")
    return data, hashlib.sha256(raw).hexdigest()


def load_and_check(root: Path, rel: str) -> tuple[dict, str, list[str]]:
    """(design, sha256 of exactly those bytes, every problem); ValueError if it cannot be read at all."""
    root = Path(root)
    data, digest = _load(root, rel)
    checker = _Checker(root)
    checker.head(data)
    if set(data) != set(KEYS):
        checker.error(f"design keys must be exactly {', '.join(KEYS)}")
    else:
        checker.basis_section(data["basis"], data["session"], PurePosixPath(rel).parts[2])
        checker.sources.declare(data["sources"], "sources")
        checker.data_section(data["data"])
        checker.domain_section(data["domain"])
        checker.cycle_models_section(data["cycle_models"])
        checker.cell_section(data["cell_definitions"])
        checker.substrate_section(data["substrates"])
        checker.basis_coverage()
        checker.intracellular_section(data["intracellular"])
        checker.coupling_section(data["coupling_plan"])
        checker.unmapped_section(data["unmapped"])
        checker.sources.uncited()
        checker.uncited_data()
    return data, digest, checker.errors


def check(root: Path, rel: str) -> list[str]:
    """Every problem with the design, one message each; [] when it is valid."""
    try:
        return load_and_check(root, rel)[2]
    except (UnicodeDecodeError, OSError) as exc:
        raise ValueError(f"cannot read {rel}: {exc}") from exc


# ---------------------------------------------------------------- CLI

def parse_input_args(source_values, data_values) -> tuple[list, list]:
    """CLI --source ID:PATH and --data ID:SESSION:PROPOSAL:APPROVAL values as declare_inputs tuples."""
    sources, data = [], []
    for value in source_values:
        parts = value.split(":", 1)
        if len(parts) != 2 or not all(parts):
            raise ValueError(f"expected ID:PATH, got {value!r}")
        sources.append(tuple(parts))
    for value in data_values:
        parts = value.split(":")
        if len(parts) != 4 or not all(parts):
            raise ValueError(f"expected ID:SESSION:PROPOSAL:APPROVAL, got {value!r}")
        data.append(tuple(parts))
    return sources, data


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    d = sub.add_parser("draft")
    d.add_argument("--purpose", required=True)
    d.add_argument("--basis", metavar="CAPTURE", help="runs/multicellular-configurator/<session>/configs/<capture>")
    d.add_argument("--source", action="append", default=[], metavar="ID:PATH")
    d.add_argument("--data", action="append", default=[], metavar="ID:SESSION:PROPOSAL:APPROVAL")
    c = sub.add_parser("check")
    c.add_argument("design")
    args = parser.parse_args(argv)
    root = project_root().resolve()
    try:
        if args.command == "draft":
            sources, data = parse_input_args(args.source, args.data)
            path = draft(root, args.purpose, basis_capture=args.basis, sources=sources, data=data)
            print(f"physicell design drafted: {path.relative_to(root).as_posix()} sha256={sha256_file(path)}")
            return 0
        errors = check(root, args.design)
        for error in errors:
            print(error, file=sys.stderr)
        if errors:
            return 1
        print(f"physicell design check passed: {args.design}")
        return 0
    except (ValueError, OSError, KeyError, TypeError, AttributeError) as exc:  # reported, never a traceback
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
