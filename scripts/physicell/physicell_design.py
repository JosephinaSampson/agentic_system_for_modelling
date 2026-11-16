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
  session: str                   the PhysiCell session; with a basis it must be the capture's session
  domain: null | {x_min,x_max,y_min,y_max,z_min,z_max,dx,dy,dz, units: "micron", max_time, time_units: "min"}
                                 required (all numbers) when basis is null; must be null with a basis
  cycle_models: [str] | null     the server's cycle models, copied VERBATIM from the configurator's
                                 get_available_cycle_models discovery output (never typed from memory); required
                                 when basis is null (check refuses: record the server's cycle models first);
                                 with a basis the cycle models of the captured XML are also accepted
  cell_definitions: [{name, cycle_model, description, from, variant_of?}]
  substrates: [{name, units, description, from}]
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
A "TODO" in any reason, description, units or note is refused. Units are never inferred: substrate units are
copied only from the captured XML, else TODO.
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
DOMAIN_KEYS = ("x_min", "x_max", "y_min", "y_max", "z_min", "z_max", "dx", "dy", "dz", "units", "max_time",
               "time_units")
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
    """{session, cell_definitions: {name: cycle model}, substrates: {name: units|None}} of a verified capture."""
    bound = sx.capture(root, capture_rel)
    tree = sx.load(root / bound["xml_path"])
    cells = {name: sx.cycle_model(tree, name) for name in sx.cell_definitions(tree)}
    subs = {e.get("name"): e.get("units") for e in tree.findall("microenvironment_setup/variable")}
    return {"session": bound["session"], "cell_definitions": cells, "substrates": subs}


def _basis_input_files(root: Path, capture_rel: str) -> list:
    """The dependency paths (package/config/...) of a capture already verified by settings_xml.capture."""
    manifest = strict_json((Path(root) / capture_rel / "capture.json").read_bytes())
    return [dep["path"] for dep in manifest["dependencies"]]


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


def draft(root: Path, purpose: str, *, basis_capture: str | None, sources: list = (), data: list = (),
          session: str | None = None) -> Path:
    """Write a skeleton design. With a basis, cell definitions and substrates are pre-filled from the capture
    (description TODO, from "basis"); without one the domain is a TODO skeleton and `session` (optional here,
    required by `check`) names the new PhysiCell session. `sources` = [(id, path under inputs/)],
    `data` = [(id, data session, proposal id, approval id)]."""
    root = Path(root)
    state.identifier(purpose)
    target = root / ORCHESTRATOR[0] / ORCHESTRATOR[1] / purpose / "physicell_design.json"
    if target.exists() or target.is_symlink():
        raise ValueError(f"{target.relative_to(root).as_posix()} exists; drafts never overwrite a design")
    declared, proposals = declare_inputs(root, sources, data)
    cells, subs = [], []
    if basis_capture is not None:
        facts = _basis_facts(root, basis_capture)
        session = facts["session"]
        cells = [{"name": n, "cycle_model": c, "description": TODO, "from": BASIS_FROM}
                 for n, c in facts["cell_definitions"].items()]
        subs = [{"name": n, "units": u or TODO, "description": TODO, "from": BASIS_FROM}
                for n, u in facts["substrates"].items()]
    domain = None if basis_capture is not None else {
        **{k: None for k in DOMAIN_KEYS[:9]}, "units": "micron", "max_time": None, "time_units": "min"}
    design = {"kind": "physicell_design", "schema_version": 1,
              "basis": {"capture": basis_capture} if basis_capture is not None else None,
              "session": session or TODO, "domain": domain, "cycle_models": None,
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


class _Checker(Citations):
    def __init__(self, root: Path):
        super().__init__(root)
        self.basis: dict | None = None
        self.cycle_allowed: set = set()
        self.cells: set = set()
        self.intracellular: set = set()

    def head(self, data: dict) -> None:
        if data.get("kind") != "physicell_design" or type(data.get("schema_version")) is not int \
                or data.get("schema_version") != 1:
            self.error('kind must be "physicell_design" and schema_version 1')

    def basis_section(self, basis, session) -> None:
        if basis is not None:
            if not isinstance(basis, dict) or set(basis) != {"capture"}:
                self.error("basis must be null or exactly {capture}")
            else:
                self.has_basis = True
                try:
                    if sx.capture(self.root, basis["capture"])["rules_path"] is not None:
                        self.error(f"basis: capture {basis['capture']} has an enabled rules ruleset; {sx.RULES_RELOAD}")
                    else:
                        files = _basis_input_files(self.root, basis["capture"])
                        if files:
                            self.error(f"basis: capture {basis['capture']} has input files ({', '.join(files)}); "
                                       f"{sx.INPUT_FILES_RELOAD}")
                    self.basis = _basis_facts(self.root, basis["capture"])
                except (ValueError, OSError) as exc:
                    self.error(f"basis: {exc}")
        if not isinstance(session, str) or not state.ID.fullmatch(session):
            self.error(f"session must be the PhysiCell session identifier, not {session!r} (TODO is refused)")
        elif self.basis is not None and session != self.basis["session"]:
            self.error(f"session {session} is not the basis capture's session {self.basis['session']}")

    def domain_section(self, domain) -> None:
        if self.has_basis:
            if domain is not None:
                self.error("domain must be null with a basis (the captured configuration holds the domain)")
            return
        if not isinstance(domain, dict) or set(domain) != set(DOMAIN_KEYS):
            self.error(f"domain is required without a basis, with exactly the keys {', '.join(DOMAIN_KEYS)}")
            return
        if domain["units"] != "micron":
            self.error(f"domain.units must be micron, not {domain['units']!r}")
        if domain["time_units"] != "min":
            self.error(f"domain.time_units must be min, not {domain['time_units']!r}")
        for key in DOMAIN_KEYS:
            if key not in ("units", "time_units") and not finite_number(domain[key]):
                self.error(f"domain.{key} must be a finite number, not {domain[key]!r}")
        if all(finite_number(domain[k]) for k in DOMAIN_KEYS if k not in ("units", "time_units")):
            for axis in "xyz":
                if domain[f"{axis}_min"] >= domain[f"{axis}_max"]:
                    self.error(f"domain.{axis}_min must be below {axis}_max")
                if domain[f"d{axis}"] <= 0:
                    self.error(f"domain.d{axis} must be positive")
            if domain["max_time"] <= 0:
                self.error("domain.max_time must be positive")

    def cycle_models_section(self, models) -> None:
        if models is None:
            if not self.has_basis:
                self.error("cycle_models is absent: record the server's cycle models first "
                           "(configurator get_available_cycle_models) before drafting a new configuration")
            models = []
        if not isinstance(models, list) or not all(real_text(m) for m in models) or len(set(models)) != len(models):
            self.error("cycle_models must be null or a list of unique non-empty names")
            return
        self.cycle_allowed = set(models)
        if self.basis is not None:
            self.cycle_allowed |= set(self.basis["cell_definitions"].values())

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
            if not isinstance(model, str) or model not in self.cycle_allowed:
                self.error(f"{label}: cycle model {model!r} is not one of the recorded cycle models "
                           f"{sorted(self.cycle_allowed)}")
            if self.has_basis and self.basis is not None:
                if item["from"] == BASIS_FROM:
                    if name not in base:
                        self.error(f'{label}: from "basis" but {name} is not in the basis capture')
                    elif model != base[name]:
                        self.error(f"{label}: cycle model {model!r} differs from the basis ({base[name]!r})")
                elif name in base:
                    self.error(f'{label}: {name} is already in the basis; use from "basis" (change it by a patch later)')
            elif item["from"] == BASIS_FROM:
                pass  # already reported by cite
        bases = {}
        for n, item in enumerate(items):
            variant = item.get("variant_of") if isinstance(item, dict) else None
            if variant is not None and (variant not in self.cells or variant == item.get("name")):
                self.error(f"cell_definitions[{n}]: variant_of {variant!r} must name another declared cell definition")
            elif variant is not None:
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
        seen = set()
        base = self.basis["substrates"] if self.basis else {}
        for n, item in enumerate(items):
            label = f"substrates[{n}]"
            if not isinstance(item, dict) or set(item) != {"name", "units", "description", "from"}:
                self.error(f"{label} must have exactly name, units, description, from")
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
            if self.basis is not None:
                if item["from"] == BASIS_FROM:
                    if name not in base:
                        self.error(f'{label}: from "basis" but {name} is not in the basis capture')
                    elif base[name] is not None and item["units"] != base[name]:
                        self.error(f"{label}: units {item['units']!r} differ from the basis ({base[name]!r})")
                elif name in base:
                    self.error(f'{label}: {name} is already in the basis; use from "basis"')

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
            recs = state.records((self.root / "DECISIONS.md").read_text(encoding="utf-8"))
            candidates = [r for r in recs if r.get("kind") == "stage_approval" and r.get("session_id") == session
                          and r.get("stage") == "boolean-dynamics" and r.get("downstream_stage") == "multicellular"
                          and isinstance(r.get("artifact"), dict) and r["artifact"].get("path") == rel
                          and r["artifact"].get("sha256") == digest]
            if not candidates:
                raise ValueError("no researcher approval of this exact handoff path and sha256 for the "
                                 "multicellular stage")
            # The gate's own check: registry row active+exported, approval of exact outputs, independent
            # review before approval, the manifest and its parent chain verified, artifact hash unchanged.
            result = gate.check(self.root, "multicellular", session, candidates[-1]["outputs"])
            if result["artifact"] != {"path": rel, "sha256": digest}:
                raise ValueError("the gate's approved handoff is a different artifact (superseded re-export?)")
            # The gate verified these exact bytes; import attaches the model to the manifest's target cell type.
            manifest = json.loads((self.root / result["artifact"]["path"]).read_bytes())
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
            if cell not in self.cells:
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
            if cell not in self.cells:
                self.error(f"{label}: cell definition {cell!r} is not declared")
            if not real_text(group):
                self.error(f"{label}: behavior_group must be non-empty text")
            if route not in ROUTES:
                self.error(f"{label}: route must be one of {', '.join(ROUTES)}, not {route!r}")
            elif route == "physiboss" and cell in self.cells and cell not in self.intracellular:
                self.error(f"{label}: route physiboss needs an intracellular MaBoSS model on {cell}")
            if not real_text(item["notes"]):
                self.error(f"{label}: notes must be a non-empty explanation, not empty or TODO")
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
        checker.basis_section(data["basis"], data["session"])
        checker.domain_section(data["domain"])
        checker.cycle_models_section(data["cycle_models"])
        checker.sources.declare(data["sources"], "sources")
        checker.data_section(data["data"])
        checker.cell_section(data["cell_definitions"])
        checker.substrate_section(data["substrates"])
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
    d.add_argument("--session", help="PhysiCell session of a new configuration (no --basis)")
    d.add_argument("--source", action="append", default=[], metavar="ID:PATH")
    d.add_argument("--data", action="append", default=[], metavar="ID:SESSION:PROPOSAL:APPROVAL")
    c = sub.add_parser("check")
    c.add_argument("design")
    args = parser.parse_args(argv)
    root = project_root().resolve()
    try:
        if args.command == "draft":
            sources, data = parse_input_args(args.source, args.data)
            path = draft(root, args.purpose, basis_capture=args.basis, sources=sources, data=data,
                         session=args.session)
            print(f"physicell design drafted: {path.relative_to(root).as_posix()} sha256={sha256_file(path)}")
            return 0
        errors = check(root, args.design)
        for error in errors:
            print(error, file=sys.stderr)
        if errors:
            return 1
        print(f"physicell design check passed: {args.design}")
        return 0
    except (ValueError, OSError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
