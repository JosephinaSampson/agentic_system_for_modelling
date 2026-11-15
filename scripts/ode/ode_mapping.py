#!/usr/bin/env python3
"""Draft and check the mapping of measurements onto a BioMASS ODE model.

The mapping (runs/orchestrator/<purpose>/ode_mapping.json) is the artifact the researcher
and orchestrator agree on: which model revision (a captured export zip), which measurement
table, which observables, condition overrides and comparison rules, which parameters are
fitted within which bounds, and why any measurement is left out.

Schema (schema_version 1; every key below is required unless marked optional):
  kind: "ode_mapping"
  inputs.model: {ode_session, revision, bundle, sha256}  bundle = .zip under runs/ode-modeler/<ode_session>/
  inputs.measurements: {path, sha256}                    a .csv under runs/orchestrator/ (orchestrator route; each
                                                         row's `source` is a declared source id)
                    or {session, proposal, approval}     an approved data-analyst `ode_measurements` proposal
                                                         (each row's `source` is the literal `proposal`)
  inputs.sources: [{id, path, sha256}]                   researcher files under inputs/, cited by rows and fit
  time: {model_unit, factors: {unit: positive float}}    measurement time_unit -> multiplier into model_unit
                                                         (model_unit itself has factor 1)
  readouts: [{readout, observable}]
  conditions: [{condition, parameters: [override], initials: [override]}]
      override: {name, value, value_unit, model_unit, factor}  model value = value * factor
  rules: [{rule_id, type, measurements: [ids], ...}]
      time_course: method in fold_change_vs_t0|relative_to_condition|max_normalized|absolute
                   (reference = a condition, for relative_to_condition only)
      endpoint_contrast: method in ratio|difference, test, reference (conditions), time (model time unit)
      dose_response: drug_entry = {form: target_activity_scaling, parameter, K: number | {fit: name}}
                                or {form: inhibitor_species, species}
                     viability = {observable, untreated (condition), time (model time unit)}
  fit: [{name, kind: parameter|initial, lower, upper, unit, justification: {source, locator} | {text}}]
  unmapped: [{measurement_id, reason}]

Every measurement row is used by exactly one rule or listed in unmapped with a real reason.

CLI:
  ode_mapping.py draft --purpose P --ode-session S --revision R --bundle ZIP --measurements CSV [--source ID:PATH ...]
  ode_mapping.py check MAPPING        (exit 1, one problem per line on stderr)
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import sys
from pathlib import Path, PurePosixPath

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts import state  # noqa: E402
from scripts.data import approvals  # noqa: E402
from scripts.data import sources as user_sources  # noqa: E402
from scripts.data.common import RUNS_ROOT, project_root, reject_symlinks, sha256_file, strict_json  # noqa: E402
from scripts.ode import measurements as table, model_zip  # noqa: E402

KEYS = ("kind", "schema_version", "inputs", "time", "readouts", "conditions", "rules", "fit", "unmapped")
ORCHESTRATOR = ("runs", "orchestrator")
ODE_RUNS = ("runs", "ode-modeler")
MODEL_KEYS = {"ode_session", "revision", "bundle", "sha256"}
FILE_KEYS = {"path", "sha256"}
PROPOSAL_KEYS = {"session", "proposal", "approval"}
OVERRIDE_KEYS = {"name", "value", "value_unit", "model_unit", "factor"}
TIME_COURSE_METHODS = ("fold_change_vs_t0", "relative_to_condition", "max_normalized", "absolute")
CONTRAST_METHODS = ("ratio", "difference")
FIT_KINDS = ("parameter", "initial")
BASE_RULE_KEYS = {"rule_id", "type", "measurements"}
OPTIONAL_RULE_KEYS = {"note"}
SHA256 = user_sources.SHA256


def _parts(rel) -> tuple:
    if not isinstance(rel, str) or not rel or "\\" in rel:
        return ()
    return PurePosixPath(rel).parts


def _mapping_path(root: Path, rel) -> Path:
    parts = _parts(rel)
    if len(parts) != 4 or parts[:2] != ORCHESTRATOR or not rel.endswith(".json"):
        raise ValueError(f"mapping must be runs/orchestrator/<purpose>/<name>.json, not {rel!r}")
    state.identifier(parts[2])
    return state.artifact(root, rel)


def _bundle_path(root: Path, session, rel) -> Path:
    state.identifier(session)
    parts = _parts(rel)
    if len(parts) < 5 or parts[:3] != (*ODE_RUNS, session) or not rel.endswith(".zip"):
        raise ValueError(f"bundle must be a .zip under runs/ode-modeler/{session}/, not {rel!r}")
    return state.artifact(root, rel)


def _measurements_path(root: Path, rel) -> Path:
    parts = _parts(rel)
    if len(parts) < 4 or parts[:2] != ORCHESTRATOR or not rel.endswith(".csv"):
        raise ValueError(f"measurement table must be a .csv under runs/orchestrator/<purpose>/, not {rel!r}")
    return state.artifact(root, rel)


def _write_new(root: Path, path: Path, text: str) -> None:
    reject_symlinks(path, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    reject_symlinks(path, root)
    with path.open("x", encoding="utf-8") as handle:
        handle.write(text)


# ---------------------------------------------------------------- draft

def draft(root: Path, purpose: str, *, ode_session: str, revision, bundle_rel: str, measurements_rel: str,
          sources: list[tuple[str, str]] = ()) -> Path:
    """Write a skeleton mapping: model binding, measurement table and sources (sha256 computed).

    Readouts, rules and fit are empty, every condition of the table is listed without
    overrides, every measurement is `unmapped` with the reason "TODO", and time.model_unit
    and the factors are TODO: `check` refuses all of these until they are filled in.
    """
    root = Path(root)
    state.identifier(purpose)
    target = root / ORCHESTRATOR[0] / ORCHESTRATOR[1] / purpose / "ode_mapping.json"
    if target.exists() or target.is_symlink():
        raise ValueError(f"{target.relative_to(root).as_posix()} exists; drafts never overwrite a mapping")
    bundle = _bundle_path(root, ode_session, bundle_rel)
    model_zip.introspect(bundle)
    meas = _measurements_path(root, measurements_rel)
    rows = table.read(meas)
    declared = []
    for source_id, rel in sources:
        if any(item["id"] == source_id for item in declared):
            raise ValueError(f"source id {source_id} is listed twice (source ids must be unique in a mapping)")
        declared.append(user_sources.entry(root, source_id, rel))
    conditions = list(dict.fromkeys(row["condition"] for row in rows))
    units = list(dict.fromkeys(row["time_unit"] for row in rows if row["time_unit"]))
    mapping = {
        "kind": "ode_mapping",
        "schema_version": 1,
        "inputs": {"model": {"ode_session": ode_session, "revision": revision, "bundle": bundle_rel,
                             "sha256": sha256_file(bundle)},
                   "measurements": {"path": measurements_rel, "sha256": sha256_file(meas)},
                   "sources": declared},
        "time": {"model_unit": "TODO", "factors": {unit: None for unit in units}},
        "readouts": [],
        "conditions": [{"condition": c, "parameters": [], "initials": []} for c in conditions],
        "rules": [],
        "fit": [],
        "unmapped": [{"measurement_id": row["measurement_id"], "reason": "TODO"} for row in rows],
    }
    try:
        _write_new(root, target, json.dumps(mapping, indent=2) + "\n")
    except FileExistsError as exc:
        raise ValueError(f"{target.relative_to(root).as_posix()} exists; drafts never overwrite a mapping") from exc
    return target


# ---------------------------------------------------------------- check

def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _is_text(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


class _Checker:
    def __init__(self, root: Path):
        self.root = root
        self.errors: list[str] = []
        self.names: dict | None = None  # introspect() result
        self.rows: dict[str, dict] = {}  # measurement id -> row
        self.row_order: list[str] = []
        self.sources = user_sources.Sources(root, self.error)
        self.units: set[str] = set()
        self.readouts: set[str] = set()
        self.conditions: set[str] = set()
        self.fit_names: set[str] = set()
        self.fit_kinds: dict[str, object] = {}
        self.uses: dict[str, list[str]] = {}

    def error(self, message: str) -> None:
        self.errors.append(message)

    # -- inputs
    def inputs(self, inputs) -> None:
        if not isinstance(inputs, dict) or set(inputs) != {"model", "measurements", "sources"}:
            self.error("inputs must have exactly the keys model, measurements and sources")
            return
        self.sources.declare(inputs["sources"], "inputs.sources")
        self.model(inputs["model"])
        self.measurements(inputs["measurements"])

    def model(self, model) -> None:
        if not isinstance(model, dict) or set(model) != MODEL_KEYS:
            self.error("inputs.model must have exactly ode_session, revision, bundle, sha256")
            return
        revision = model["revision"]
        if not (_is_text(revision) or (type(revision) is int and revision >= 0)):
            self.error("inputs.model: revision must be a non-empty string or a non-negative integer")
        try:
            path = _bundle_path(self.root, model["ode_session"], model["bundle"])
        except (ValueError, OSError) as exc:
            self.error(f"inputs.model: {exc}")
            return
        if not isinstance(model["sha256"], str) or not SHA256.fullmatch(model["sha256"]):
            self.error("inputs.model: sha256 must be a lowercase hex sha256")
        elif sha256_file(path) != model["sha256"]:
            self.error(f"inputs.model: {model['bundle']} does not match its sha256")
            return
        try:
            self.names = model_zip.introspect(path)
        except (ValueError, OSError) as exc:
            self.error(f"inputs.model: cannot read {model['bundle']}: {exc}")

    def measurements(self, meas) -> None:
        if not isinstance(meas, dict) or set(meas) not in (FILE_KEYS, PROPOSAL_KEYS):
            self.error("inputs.measurements must have exactly path, sha256 "
                       "or exactly session, proposal, approval")
            return
        if set(meas) == FILE_KEYS:
            try:
                path = _measurements_path(self.root, meas["path"])
            except (ValueError, OSError) as exc:
                self.error(f"inputs.measurements: {exc}")
                return
            if not isinstance(meas["sha256"], str) or sha256_file(path) != meas["sha256"]:
                self.error(f"inputs.measurements: {meas['path']} does not match its sha256")
                return
            source_ids = set(self.sources.declared)
        else:
            try:
                path = self.proposal_table(meas)
            except (ValueError, OSError) as exc:
                self.error(f"inputs.measurements: {exc}")
                return
            source_ids = {table.PROPOSAL_SOURCE}
        try:
            rows = table.read(path)
        except (ValueError, OSError, UnicodeDecodeError, csv.Error) as exc:
            self.error(f"inputs.measurements: cannot read the measurement table: {exc}")
            return
        for problem in table.check(rows, source_ids):
            self.error(f"inputs.measurements: {problem}")
        cited = set()
        for row in rows:
            mid = row["measurement_id"]
            if mid not in self.rows:
                self.rows[mid] = row
                self.row_order.append(mid)
                self.uses[mid] = []
            cited.add(row["source"])
        for sid in cited & set(self.sources.cited):
            self.sources.cited[sid] += 1

    def proposal_table(self, meas) -> Path:
        session, pid, record_id = meas["session"], meas["proposal"], meas["approval"]
        if not all(isinstance(v, str) and v for v in (session, pid, record_id)):
            raise ValueError("session, proposal and approval must be non-empty strings")
        record = approvals.require_data_approval(self.root, record_id, session)
        entry = approvals.require_proposal_approved(self.root, record, session, pid)
        if entry.get("kind") != "ode_measurements" or entry.get("target_layer") != "biomass":
            raise ValueError(f"proposal {pid} must be an ode_measurements proposal for biomass, "
                             f"not {entry.get('kind')!r} for {entry.get('target_layer')!r}")
        if not entry["path"].endswith(".csv"):
            raise ValueError(f"proposal {pid} must be a .csv table")
        return state.artifact(self.root, entry["path"])

    # -- time
    def time(self, value) -> None:
        if not isinstance(value, dict) or set(value) != {"model_unit", "factors"}:
            self.error("time must have exactly model_unit and factors")
            return
        unit, factors = value["model_unit"], value["factors"]
        model_ok = _is_text(unit) and unit.strip().upper() != "TODO"
        if not model_ok:
            self.error("time.model_unit must be a real unit, not empty or TODO")
        if not isinstance(factors, dict):
            self.error("time.factors must be an object of measurement time unit -> positive factor")
            return
        for name, factor in factors.items():
            if not _is_number(factor) or factor <= 0:
                self.error(f"time.factors[{name!r}] must be a positive number")
            else:
                self.units.add(name)
        if model_ok and factors.get(unit) != 1:
            self.error(f"time.factors must map the model_unit {unit!r} to 1")
        for mid in self.row_order:
            row_unit = self.rows[mid]["time_unit"]
            if row_unit and row_unit not in self.units:
                self.error(f'measurement {mid}: time_unit "{row_unit}" has no positive factor in time.factors')

    # -- readouts and conditions
    def readouts_section(self, items) -> None:
        if not isinstance(items, list):
            self.error("readouts must be a list")
            return
        for n, item in enumerate(items):
            if not isinstance(item, dict) or set(item) != {"readout", "observable"}:
                self.error(f"readouts[{n}] must have exactly readout and observable")
                continue
            name, obs = item["readout"], item["observable"]
            if not _is_text(name):
                self.error(f"readouts[{n}]: readout must be a non-empty string")
                continue
            if name in self.readouts:
                self.error(f"readouts[{n}]: readout {name} is declared twice")
            self.readouts.add(name)
            if self.names is not None and obs not in self.names["observables"]:
                self.error(f"readouts[{n}]: observable {obs!r} is not an observable of the model "
                           f"({', '.join(self.names['observables'])})")

    def override(self, label: str, item, universe_key: str, noun: str) -> str | None:
        if not isinstance(item, dict) or set(item) != OVERRIDE_KEYS:
            self.error(f"{label} must have exactly {', '.join(sorted(OVERRIDE_KEYS))}")
            return None
        name = item["name"]
        if not _is_text(name):
            self.error(f"{label}: name must be a non-empty string")
            return None
        if self.names is not None and name not in self.names[universe_key]:
            self.error(f"{label}: {name!r} is not a {noun} of the model")
        if not _is_number(item["value"]):
            self.error(f"{label}: value must be a finite number")
        for key in ("value_unit", "model_unit"):
            if not _is_text(item[key]) or item[key].strip().upper() == "TODO":
                self.error(f"{label}: {key} must be a real unit (units are never inferred)")
        if not _is_number(item["factor"]) or item["factor"] <= 0:
            self.error(f"{label}: factor must be a positive number (model value = value * factor)")
        return name

    def conditions_section(self, items) -> None:
        if not isinstance(items, list):
            self.error("conditions must be a list")
            return
        for n, item in enumerate(items):
            if not isinstance(item, dict) or set(item) != {"condition", "parameters", "initials"}:
                self.error(f"conditions[{n}] must have exactly condition, parameters and initials")
                continue
            cid = item["condition"]
            if not _is_text(cid):
                self.error(f"conditions[{n}]: condition must be a non-empty string")
                continue
            if cid in self.conditions:
                self.error(f"conditions[{n}]: condition {cid} is declared twice")
            self.conditions.add(cid)
            for key, universe, noun in (("parameters", "parameters", "parameter"),
                                        ("initials", "species", "species")):
                entries = item[key]
                if not isinstance(entries, list):
                    self.error(f"condition {cid}: {key} must be a list")
                    continue
                seen = set()
                for m, entry in enumerate(entries):
                    name = self.override(f"condition {cid} {key}[{m}]", entry, universe, noun)
                    if name is not None:
                        if name in seen:
                            self.error(f"condition {cid}: {name} is set twice in {key}")
                        seen.add(name)

    # -- fit
    def fit_section(self, items) -> None:
        if not isinstance(items, list):
            self.error("fit must be a list")
            return
        for n, item in enumerate(items):
            keys = {"name", "kind", "lower", "upper", "unit", "justification"}
            if not isinstance(item, dict) or set(item) != keys:
                self.error(f"fit[{n}] must have exactly {', '.join(sorted(keys))}")
                continue
            name, kind = item["name"], item["kind"]
            label = f"fit {name}"
            if not _is_text(name):
                self.error(f"fit[{n}]: name must be a non-empty string")
                continue
            if name in self.fit_names:
                self.error(f"{label}: fitted twice")
            self.fit_names.add(name)
            self.fit_kinds[name] = kind if isinstance(kind, str) else None
            if kind not in FIT_KINDS:
                self.error(f"{label}: kind must be one of {FIT_KINDS}")
            elif self.names is not None:
                universe = self.names["parameters" if kind == "parameter" else "species"]
                if name not in universe:
                    self.error(f"{label}: {name!r} is not a {'parameter' if kind == 'parameter' else 'species'} "
                               "of the model")
            lower, upper = item["lower"], item["upper"]
            if not _is_number(lower) or lower <= 0:
                self.error(f"{label}: lower must be a positive number")
            if not _is_number(upper) or upper <= 0:
                self.error(f"{label}: upper must be a positive number")
            if _is_number(lower) and _is_number(upper) and lower >= upper:
                self.error(f"{label}: lower must be below upper")
            if not _is_text(item["unit"]) or item["unit"].strip().upper() == "TODO":
                self.error(f"{label}: unit must be a real unit")
            self.justification(label, item["justification"])

    def justification(self, label: str, value) -> None:
        if isinstance(value, dict) and set(value) == {"text"}:
            if not _is_text(value["text"]) or value["text"].strip().upper() == "TODO":
                self.error(f"{label}: justification text must be a real explanation")
        elif user_sources.is_ref(value):
            self.sources.ref(f"{label} justification", value)
        else:
            self.error(f"{label}: justification must be {{source, locator}} or {{text}}")

    # -- rules
    def rules_section(self, items) -> None:
        if not isinstance(items, list):
            self.error("rules must be a list")
            return
        seen = set()
        for n, item in enumerate(items):
            if not isinstance(item, dict):
                self.error(f"rules[{n}] must be an object")
                continue
            rid, kind = item.get("rule_id"), item.get("type")
            label = f"rule {rid}"
            if not isinstance(rid, str) or not state.ID.fullmatch(rid):
                self.error(f"rules[{n}]: rule_id {rid!r} must be a safe identifier")
            elif rid in seen:
                self.error(f"duplicate rule id {rid}")
            else:
                seen.add(rid)
            handler = self.RULE_TYPES.get(kind) if isinstance(kind, str) else None
            if handler is None:
                self.error(f"{label}: type must be one of time_course, endpoint_contrast, dose_response")
                continue
            handler(self, label, item, rid)

    def keys(self, label: str, item: dict, extra: set) -> bool:
        base = BASE_RULE_KEYS | extra
        if not base <= set(item) <= base | OPTIONAL_RULE_KEYS:
            self.error(f"{label}: keys must be exactly {', '.join(sorted(base))} (optional note), "
                       f"not {', '.join(sorted(item))}")
            return False
        return True

    def rule_rows(self, label: str, item: dict, rid) -> list[dict]:
        ids = item["measurements"]
        if not isinstance(ids, list) or not ids or not all(isinstance(i, str) for i in ids):
            self.error(f"{label}: measurements must be a non-empty list of measurement ids")
            return []
        rows = []
        for mid in ids:
            if mid not in self.rows:
                if self.rows:
                    self.error(f"{label}: measurement {mid} is not in the measurement table")
                continue
            self.uses[mid].append(label)
            row = self.rows[mid]
            rows.append(row)
            if row["readout"] not in self.readouts:
                self.error(f"{label}: measurement {mid} readout {row['readout']!r} is not declared under readouts")
            if row["condition"] not in self.conditions:
                self.error(f"{label}: measurement {mid} condition {row['condition']!r} is not declared "
                           "under conditions")
        return rows

    def condition(self, label: str, what: str, value) -> bool:
        if not isinstance(value, str) or value not in self.conditions:
            self.error(f"{label}: {what} {value!r} is not declared under conditions")
            return False
        return True

    def forbid_dose(self, label: str, rows: list[dict]) -> None:
        for row in rows:
            if row["dose"] is not None:
                self.error(f"{label}: measurement {row['measurement_id']} has a dose; "
                           "only dose_response rules take dosed rows")

    def rule_time_course(self, label: str, item: dict, rid) -> None:
        method = item.get("method")
        extra = {"method"} | ({"reference"} if method == "relative_to_condition" else set())
        if method not in TIME_COURSE_METHODS:
            self.error(f"{label}: method must be one of {TIME_COURSE_METHODS}")
            extra = {"method"}
        if not self.keys(label, item, extra):
            if "measurements" in item:
                self.rule_rows(label, item, rid)
            return
        rows = self.rule_rows(label, item, rid)
        if method == "relative_to_condition":
            self.condition(label, "reference", item["reference"])
        self.forbid_dose(label, rows)

    def rule_endpoint_contrast(self, label: str, item: dict, rid) -> None:
        method = item.get("method")
        if method not in CONTRAST_METHODS:
            self.error(f"{label}: method must be one of {CONTRAST_METHODS}")
        if not self.keys(label, item, {"method", "test", "reference", "time"}):
            if "measurements" in item:
                self.rule_rows(label, item, rid)
            return
        rows = self.rule_rows(label, item, rid)
        test_ok = self.condition(label, "test", item["test"])
        ref_ok = self.condition(label, "reference", item["reference"])
        if test_ok and ref_ok and item["test"] == item["reference"]:
            self.error(f"{label}: test and reference conditions must differ")
        if not _is_number(item["time"]) or item["time"] < 0:
            self.error(f"{label}: time must be a finite number >= 0 (in the model time unit)")
        self.forbid_dose(label, rows)
        if test_ok and ref_ok:
            present = {row["condition"] for row in rows}
            for what in ("test", "reference"):
                if item[what] not in present:
                    self.error(f"{label}: no measurement of the {what} condition {item[what]!r}")

    def rule_dose_response(self, label: str, item: dict, rid) -> None:
        if not self.keys(label, item, {"drug_entry", "viability"}):
            if "measurements" in item:
                self.rule_rows(label, item, rid)
            return
        rows = self.rule_rows(label, item, rid)
        for row in rows:
            if row["dose"] is None:
                self.error(f"{label}: measurement {row['measurement_id']} has no dose; "
                           "dose_response needs dosed rows")
        self.drug_entry(label, item["drug_entry"])
        via = item["viability"]
        if not isinstance(via, dict) or set(via) != {"observable", "untreated", "time"}:
            self.error(f"{label}: viability must have exactly observable, untreated, time")
            return
        if self.names is not None and via["observable"] not in self.names["observables"]:
            self.error(f"{label}: viability observable {via['observable']!r} is not an observable of the model")
        self.condition(label, "viability untreated condition", via["untreated"])
        if not _is_number(via["time"]) or via["time"] < 0:
            self.error(f"{label}: viability time must be a finite number >= 0 (in the model time unit)")

    def drug_entry(self, label: str, entry) -> None:
        form = entry.get("form") if isinstance(entry, dict) else None
        if form == "target_activity_scaling" and set(entry) == {"form", "parameter", "K"}:
            if self.names is not None and entry["parameter"] not in self.names["parameters"]:
                self.error(f"{label}: drug_entry parameter {entry['parameter']!r} is not a parameter of the model")
            k = entry["K"]
            if isinstance(k, dict) and set(k) == {"fit"}:
                fitted = k["fit"]
                if not isinstance(fitted, str) or fitted not in self.fit_names:
                    self.error(f"{label}: drug_entry K fits {fitted!r}, which is not under fit")
                elif self.fit_kinds[fitted] != "parameter":
                    self.error(f"{label}: drug_entry K fits {fitted!r}, which must be a fit entry of kind parameter")
            elif not _is_number(k) or k <= 0:
                self.error(f"{label}: drug_entry K must be a positive number or {{\"fit\": name}}")
        elif form == "inhibitor_species" and set(entry) == {"form", "species"}:
            if self.names is not None and entry["species"] not in self.names["species"]:
                self.error(f"{label}: drug_entry species {entry['species']!r} is not a species of the model")
        else:
            self.error(f"{label}: drug_entry must be {{form: target_activity_scaling, parameter, K}} "
                       "or {form: inhibitor_species, species}")

    RULE_TYPES = {"time_course": rule_time_course, "endpoint_contrast": rule_endpoint_contrast,
                  "dose_response": rule_dose_response}

    # -- unmapped and coverage
    def unmapped_section(self, items) -> None:
        if not isinstance(items, list):
            self.error("unmapped must be a list")
            return
        for n, item in enumerate(items):
            if not isinstance(item, dict) or set(item) != {"measurement_id", "reason"}:
                self.error(f"unmapped[{n}] must have exactly measurement_id and reason")
                continue
            mid, reason = item["measurement_id"], item["reason"]
            label = f"unmapped[{n}] ({mid})"
            if not _is_text(reason) or reason.strip().upper() == "TODO":
                self.error(f"{label}: reason must be a non-empty explanation, not empty or TODO")
            if not isinstance(mid, str) or (self.rows and mid not in self.rows):
                self.error(f"{label}: measurement {mid} is not in the measurement table")
            elif mid in self.uses:
                self.uses[mid].append(label)

    def coverage(self) -> None:
        for mid, labels in self.uses.items():
            if not labels:
                self.error(f"measurement {mid} is neither used by a rule nor listed in unmapped")
            elif len(labels) > 1:
                self.error(f"measurement {mid} must be used exactly once, not {len(labels)} times "
                           f"({'; '.join(labels)})")


def _load_mapping(root: Path, rel: str) -> tuple[dict, str]:
    raw = _mapping_path(root, rel).read_bytes()
    data = strict_json(raw)
    if not isinstance(data, dict):
        raise ValueError("mapping must be a JSON object")
    return data, hashlib.sha256(raw).hexdigest()


def load_and_check(root: Path, rel: str) -> tuple[dict, str, list[str]]:
    """(mapping, sha256 of exactly those bytes, every problem); ValueError if it cannot be read at all."""
    root = Path(root)
    data, digest = _load_mapping(root, rel)
    checker = _Checker(root)
    if data.get("kind") != "ode_mapping" or type(data.get("schema_version")) is not int \
            or data.get("schema_version") != 1:
        checker.error('kind must be "ode_mapping" and schema_version 1')
    if tuple(data) != KEYS and set(data) != set(KEYS):
        checker.error(f"mapping keys must be exactly {', '.join(KEYS)}")
    else:
        # inputs first: the other sections are checked against the model names and table rows.
        checker.inputs(data["inputs"])
        checker.readouts_section(data["readouts"])
        checker.conditions_section(data["conditions"])
        checker.fit_section(data["fit"])
        checker.time(data["time"])
        checker.rules_section(data["rules"])
        checker.unmapped_section(data["unmapped"])
        checker.coverage()
        checker.sources.uncited()
    return data, digest, checker.errors


def check(root: Path, rel: str) -> list[str]:
    """Every problem with the mapping, one message each; [] when it is valid."""
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
    d.add_argument("--ode-session", required=True)
    d.add_argument("--revision", required=True)
    d.add_argument("--bundle", required=True, metavar="ZIP")
    d.add_argument("--measurements", required=True, metavar="CSV")
    d.add_argument("--source", action="append", default=[], metavar="ID:PATH",
                   help="a researcher-provided file under inputs/ (repeatable); its sha256 is computed")
    c = sub.add_parser("check")
    c.add_argument("mapping")
    args = parser.parse_args(argv)
    root = project_root().resolve()
    try:
        if args.command == "draft":
            sources = []
            for value in args.source:
                parts = value.split(":", 1)
                if len(parts) != 2 or not all(parts):
                    raise ValueError(f"expected ID:PATH, got {value!r}")
                sources.append(tuple(parts))
            path = draft(root, args.purpose, ode_session=args.ode_session, revision=args.revision,
                         bundle_rel=args.bundle, measurements_rel=args.measurements, sources=sources)
            print(f"ode mapping drafted: {path.relative_to(root).as_posix()} sha256={sha256_file(path)}")
            return 0
        errors = check(root, args.mapping)
        for error in errors:
            print(error, file=sys.stderr)
        if errors:
            return 1
        print(f"ode mapping check passed: {args.mapping}")
        return 0
    except (ValueError, OSError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
