#!/usr/bin/env python3
"""Draft and check the mapping of measurements onto a BioMASS ODE model.

The mapping (runs/orchestrator/<purpose>/ode_mapping.json) is the artifact the researcher
and orchestrator agree on: which model revision (a captured export zip), which measurement
table, which observables, condition overrides and comparison rules, which parameters are
fitted within which bounds, and why any measurement is left out.

Schema (schema_version 1; every key below is required unless marked optional):
  kind: "ode_mapping"
  inputs.model: {ode_session, revision, bundle, sha256}  bundle = a BioMASS export .zip under
                                                         runs/ode-modeler/<ode_session>/ (model/ = the revision
                                                         folder, the Text2Model package in model/generated_model/);
                                                         revision = the captured revision (rev_<32 hex>) whose
                                                         integrity.json, in every captured folder of that name
                                                         (runs/ode-modeler/<ode_session>/captures/<capture>/<rev>/,
                                                         where record_ode_artifacts puts it; folders elsewhere are
                                                         ignored), is byte-identical to the zip's
                                                         model/integrity.json, and the zip's model/ files must match
                                                         that inventory
  inputs.measurements: {path, sha256}                    a .csv under runs/orchestrator/ (orchestrator route; each
                                                         row's `source` is a declared source id)
                    or {session, proposal, approval}     an approved data-analyst `ode_measurements` proposal
                                                         (each row's `source` is the literal `proposal`)
  inputs.sources: [{id, path, sha256}]                   researcher files under inputs/, cited by rows and fit
  time: {model_unit, factors: {unit: positive float}}    measurement time_unit -> multiplier into model_unit
                                                         (model_unit itself has factor 1)
  dose: {model_unit, factors: {unit: positive float}}    measurement dose_unit -> multiplier into the model dose
                                                         unit (K of a target_activity_scaling is in that unit);
                                                         factors may be empty when no row has a dose
  values: {readouts: {readout: {model_unit, factors: {value_unit: positive float}}},
           viability: {factors: {value_unit: positive float}}}
                                                         measurement value_unit -> multiplier: an `absolute`
                                                         time_course row becomes value x factor in the observable's
                                                         model_unit; a dose_response row becomes value x factor as a
                                                         fraction (e.g. "%": 0.01, "fraction": 1); a difference
                                                         contrast is converted like `absolute`. Never inferred: a
                                                         row without its factor fails the check. Normalised rules
                                                         need no factor (units cancel), but every row of one
                                                         normalisation group must share one value_unit.
  readouts: [{readout, observable}]
  conditions: [{condition, parameters: [override], initials: [override]}]
      override: {name, value, value_unit, model_unit, factor}  model value = value * factor
  rules: [{rule_id, type, measurements: [ids], ...}]
      time_course: method in fold_change_vs_t0|relative_to_condition|max_normalized|absolute
                   (reference = a condition, for relative_to_condition only). Data and predictions are
                   normalised the same way. Divisor rows: a fit row is divided by fit rows only (hold-out rows
                   never change a fit target); a hold-out row by the hold-out rows of the same kind when there are
                   some, else by the fit rows:
                     fold_change_vs_t0      each row / the mean of the time-0 rows of the same readout and condition
                                            (divisor rows as above; required, non-zero; t0 rows are kept, target 1)
                     relative_to_condition  each row / the mean of the reference-condition rows of the same
                                            readout at the same converted time (divisor rows as above; required,
                                            non-zero; the reference rows belong to the rule)
                     max_normalized         every row / the max of the rule's FIT rows of the same readout
                                            (positive; hold-out rows too, so they are scored on the fit's scale;
                                            hold-out rows without fit rows of their readout are refused); the
                                            prediction uses the same fit rows' simulated max
                     absolute               value x values.readouts factor (model unit)
                   One value_unit per (readout, condition) group (fold_change) or per readout (the others).
      endpoint_contrast: method in ratio|difference, test, reference (conditions), time (model time unit)
      dose_response: drug_entry = {form: target_activity_scaling, parameter, K: number | {fit: name}}
                                or {form: inhibitor_species, species}
                     a fitted K needs a positive model default (read from ode.py param_values without importing
                     it): hold-out scoring keeps the default of a K the researcher did not accept
                     viability = {observable, untreated (condition), time (model time unit)}
                     every dose row's time, converted, must equal viability.time (dose rows are evaluated there),
                     and every row's readout must map (readouts) to viability.observable
      endpoint_contrast: every row's condition is the test or the reference; every test row needs a reference
                     row of the same readout and the same `use`; a ratio needs a non-zero reference mean (and is
                     unitless); a difference needs a values.readouts factor (its target, value differences, is
                     converted into the model unit like an absolute value); every
                     row's time, converted, must equal the rule time
      (times are compared with math.isclose, rel_tol 1e-9: 0.03 h x 60 = 1.7999999999999998 is 1.8 min)
  fit: [{name, kind: parameter|initial, lower, upper, unit, justification: {source, locator} | {text}}]
  unmapped: [{measurement_id, reason}]

Every measurement row is used by exactly one rule or listed in unmapped with a real reason, and at
least one use=fit row is scored by a rule with information: a contrast reference row is not scored, and a row
whose normalised target and prediction are 1 by construction does not count (a fold_change_vs_t0 row at time 0,
a relative_to_condition row of the reference condition, the only fit row of a max_normalized group). A fitted name that a
condition overrides (or an inhibitor_species dose sets) is refused: the override would mask it there.

CLI:
  ode_mapping.py draft --purpose P --ode-session S --bundle ZIP --measurements CSV [--revision R] [--source ID:PATH ...]
                      (the revision is derived from the bundle; --revision, when given, must be that revision)
  ode_mapping.py check MAPPING        (exit 1, one problem per line on stderr)
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import re
import sys
from pathlib import Path, PurePosixPath

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts import state  # noqa: E402
from scripts.data import approvals  # noqa: E402
from scripts.data import sources as user_sources  # noqa: E402
from scripts.data.common import finite_number as _is_number  # noqa: E402
from scripts.data.common import RUNS_ROOT, project_root, reject_symlinks, sha256_file, strict_json  # noqa: E402
from scripts.ode import measurements as table, model_zip  # noqa: E402

KEYS = ("kind", "schema_version", "inputs", "time", "dose", "values", "readouts", "conditions", "rules", "fit",
        "unmapped")
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
TIME_RTOL = 1e-9  # converted row times equal a rule time within this relative tolerance (float products)
REVISION = re.compile(r"rev_[0-9a-f]{32}\Z")  # a generated BioMASS revision id (docs/ode-contract.md)


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


def proposal_table(root: Path, meas: dict) -> Path:
    """The table of an approved data-analyst `ode_measurements` proposal {session, proposal, approval}."""
    return _proposal_entry(root, meas)[0]


def _proposal_entry(root: Path, meas: dict) -> tuple[Path, str]:
    session, pid, record_id = meas["session"], meas["proposal"], meas["approval"]
    if not all(isinstance(v, str) and v for v in (session, pid, record_id)):
        raise ValueError("session, proposal and approval must be non-empty strings")
    record = approvals.require_data_approval(root, record_id, session)
    entry = approvals.require_proposal_approved(root, record, session, pid)
    if entry.get("kind") != "ode_measurements" or entry.get("target_layer") != "biomass":
        raise ValueError(f"proposal {pid} must be an ode_measurements proposal for biomass, "
                         f"not {entry.get('kind')!r} for {entry.get('target_layer')!r}")
    if not entry["path"].endswith(".csv"):
        raise ValueError(f"proposal {pid} must be a .csv table")
    return state.artifact(root, entry["path"]), entry["sha256"]


def measurement_table(root: Path, meas: dict) -> tuple[Path, str]:
    """(path, expected sha256) of a mapping's inputs.measurements, for either route.

    The orchestrator route returns the sha256 bound in the mapping; the data-analyst route returns
    the sha256 the approved handoff lists for the proposal table.
    """
    if set(meas) == FILE_KEYS:
        return _measurements_path(root, meas["path"]), meas["sha256"]
    return _proposal_entry(root, meas)


def _revision_folders(root: Path, session: str) -> dict[str, list[tuple[str, bytes | None]]]:
    """{revision name: [(folder, integrity.json bytes or None)]} of the captured revision folders of a session:
    exactly runs/ode-modeler/<session>/captures/<capture>/rev_<32 hex>/, where record_ode_artifacts puts the
    BioMASS server's <session>/rev_<hex>/ revision folders. A folder named rev_<hex> anywhere else (scratch,
    exports, nested deeper) is not a capture and is ignored; symlinks are never followed."""
    root = Path(root)
    base = root / ODE_RUNS[0] / ODE_RUNS[1] / session
    reject_symlinks(base, root)
    captures = base / "captures"
    found: dict = {}
    if captures.is_symlink() or not captures.is_dir():
        return found
    for capture in sorted(captures.iterdir()):
        if capture.is_symlink() or not capture.is_dir():
            continue
        for folder in sorted(capture.iterdir()):
            if not REVISION.fullmatch(folder.name) or folder.is_symlink() or not folder.is_dir():
                continue
            seal = folder / "integrity.json"
            data = seal.read_bytes() if seal.is_file() and not seal.is_symlink() else None
            found.setdefault(folder.name, []).append((folder.relative_to(root).as_posix(), data))
    return found


def captured_revisions(root: Path, session: str, integrity: bytes) -> set[str]:
    """Names of the captured revisions (see _revision_folders) whose integrity.json is byte-identical to
    `integrity` in EVERY captured folder of that name (one disagreeing copy and the name is never returned)."""
    return {name for name, copies in _revision_folders(root, session).items()
            if all(data == integrity for _folder, data in copies)}


def bundle_revision(root: Path, session: str, bundle: Path, revision=None) -> str:
    """The captured revision the export zip was made from (its model/integrity.json); ValueError when there is
    none, when `revision` is given and is not it, when several captured revisions match and none is given, when
    captured copies of the revision disagree, or when the zip's model/ files are not exactly the ones that
    integrity.json seals."""
    sealed = model_zip.integrity(bundle)
    folders = _revision_folders(root, session)
    names = {name for name, copies in folders.items() if all(data == sealed for _folder, data in copies)}
    shown = ", ".join(sorted(names)) or "none"
    if revision is not None:
        if revision not in names:
            copies = folders.get(revision, [])
            if any(data == sealed for _folder, data in copies):
                differ = ", ".join(folder for folder, data in copies if data != sealed)
                raise ValueError(f"captured folders of revision {revision!r} disagree: {differ} do(es) not carry "
                                 "the export zip's model/integrity.json (every captured copy of a revision must be "
                                 "identical)")
            raise ValueError(f"revision {revision!r} is not the captured revision of the export zip (captured "
                             f"revisions under runs/ode-modeler/{session}/captures/<capture>/ with its "
                             f"model/integrity.json: {shown})")
    elif not names:
        raise ValueError(f"no captured revision under runs/ode-modeler/{session}/captures/<capture>/ has the export "
                         "zip's model/integrity.json (in every captured copy); capture the revision the bundle was "
                         "exported from first")
    elif len(names) > 1:
        raise ValueError(f"several captured revisions match the export zip ({shown}); pass --revision")
    model_zip.verify_inventory(bundle)  # the sealed inventory binds the package files too
    return revision if revision is not None else names.pop()


# ---------------------------------------------------------------- draft

def draft(root: Path, purpose: str, *, ode_session: str, bundle_rel: str, measurements_rel: str,
          sources: list[tuple[str, str]] = (), revision=None) -> Path:
    """Write a skeleton mapping: model binding, measurement table and sources (sha256 computed).

    The revision is derived from the bundle (bundle_revision); a given `revision` must be that one.
    Readouts, rules and fit are empty, every condition of the table is listed without
    overrides, every measurement is `unmapped` with the reason "TODO", and time.model_unit,
    the time/dose factors and the values block (one entry per readout with its value units,
    and the viability units of dosed rows) are TODO: `check` refuses all of these until they
    are filled in (delete the values.readouts entries no `absolute` rule needs).
    """
    root = Path(root)
    state.identifier(purpose)
    target = root / ORCHESTRATOR[0] / ORCHESTRATOR[1] / purpose / "ode_mapping.json"
    if target.exists() or target.is_symlink():
        raise ValueError(f"{target.relative_to(root).as_posix()} exists; drafts never overwrite a mapping")
    bundle = _bundle_path(root, ode_session, bundle_rel)
    model_zip.introspect(bundle)
    revision = bundle_revision(root, ode_session, bundle, revision)
    meas = _measurements_path(root, measurements_rel)
    rows = table.read(meas)
    declared = []
    for source_id, rel in sources:
        if any(item["id"] == source_id for item in declared):
            raise ValueError(f"source id {source_id} is listed twice (source ids must be unique in a mapping)")
        declared.append(user_sources.entry(root, source_id, rel))
    conditions = list(dict.fromkeys(row["condition"] for row in rows))
    dose_units = list(dict.fromkeys(row["dose_unit"] for row in rows if row["dose"] is not None and row["dose_unit"]))
    units = list(dict.fromkeys(row["time_unit"] for row in rows if row["time_unit"]))
    value_units: dict = {}
    for row in rows:
        if row["dose"] is None and row["readout"] and row["value_unit"]:
            value_units.setdefault(row["readout"], {})[row["value_unit"]] = None
    viability_units = {row["value_unit"]: None for row in rows if row["dose"] is not None and row["value_unit"]}
    mapping = {
        "kind": "ode_mapping",
        "schema_version": 1,
        "inputs": {"model": {"ode_session": ode_session, "revision": revision, "bundle": bundle_rel,
                             "sha256": sha256_file(bundle)},
                   "measurements": {"path": measurements_rel, "sha256": sha256_file(meas)},
                   "sources": declared},
        "time": {"model_unit": "TODO", "factors": {unit: None for unit in units}},
        "dose": {"model_unit": "TODO", "factors": {unit: None for unit in dose_units}},
        "values": {"readouts": {name: {"model_unit": "TODO", "factors": units_}
                                for name, units_ in value_units.items()},
                   "viability": {"factors": viability_units}},
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
        self.time_factors: dict = {}
        self.dose_factors: dict = {}
        self.value_factors: dict[str, dict] = {}  # readout -> {value_unit: factor} (values.readouts)
        self.viability_factors: dict = {}  # value_unit -> factor into a fraction (values.viability)
        self.overrides: dict[str, dict[str, set]] = {}  # condition -> {"parameters": names, "initials": names}
        self.inhibitors: list[tuple[str, object]] = []  # (rule label, species set to the dose)
        self.readouts: set[str] = set()
        self.readout_observable: dict[str, object] = {}
        self.scored: set[str] = set()  # measurement ids that are fit targets of a rule (not references)
        self.trivial: dict[str, str] = {}  # scored id -> why its normalised target is 1 by construction
        self.defaults: dict | None = None  # parameter -> model default (None: not readable), from ode.py
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
        revision_ok = isinstance(revision, str) and bool(REVISION.fullmatch(revision))
        if not revision_ok:
            self.error("inputs.model: revision must be a generated revision id rev_<32 lowercase hex>")
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
            self.defaults = model_zip.parameter_defaults(path)
        except (ValueError, OSError) as exc:
            self.error(f"inputs.model: cannot read {model['bundle']}: {exc}")
            return
        if revision_ok:
            try:
                bundle_revision(self.root, model["ode_session"], path, revision)
            except (ValueError, OSError) as exc:
                self.error(f"inputs.model: {exc}")

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
        return proposal_table(self.root, meas)

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
                self.time_factors[name] = factor
        if model_ok and factors.get(unit) != 1:
            self.error(f"time.factors must map the model_unit {unit!r} to 1")
        for mid in self.row_order:
            row_unit = self.rows[mid]["time_unit"]
            if row_unit and row_unit not in self.units:
                self.error(f'measurement {mid}: time_unit "{row_unit}" has no positive factor in time.factors')

    def dose(self, value) -> None:
        if not isinstance(value, dict) or set(value) != {"model_unit", "factors"}:
            self.error("dose must have exactly model_unit and factors")
            return
        unit, factors = value["model_unit"], value["factors"]
        if not isinstance(unit, str):
            self.error("dose.model_unit must be a string (the model dose unit)")
        if not isinstance(factors, dict):
            self.error("dose.factors must be an object of measurement dose unit -> positive factor")
            return
        for name, factor in factors.items():
            if not _is_number(factor) or factor <= 0:
                self.error(f"dose.factors[{name!r}] must be a positive number")
            else:
                self.dose_factors[name] = factor
        dosed = [self.rows[mid] for mid in self.row_order if self.rows[mid]["dose"] is not None]
        if dosed and (not _is_text(unit) or unit.strip().upper() == "TODO"):
            self.error("dose.model_unit must be a real unit when measurements have a dose")
        for row in dosed:
            if not row["dose_unit"] or row["dose_unit"] not in self.dose_factors:
                self.error(f'measurement {row["measurement_id"]}: dose_unit "{row["dose_unit"]}" has no '
                           "positive factor in dose.factors")

    # -- values (decision U: explicit factors for absolute values and viability)
    def _factor_map(self, label: str, factors) -> dict:
        if not isinstance(factors, dict):
            self.error(f"{label}.factors must be an object of value unit -> positive factor")
            return {}
        good = {}
        for unit, factor in factors.items():
            if not _is_number(factor) or factor <= 0:
                self.error(f"{label}.factors[{unit!r}] must be a positive number (units are never inferred)")
            else:
                good[unit] = factor
        return good

    def values_section(self, value) -> None:
        if not isinstance(value, dict) or set(value) != {"readouts", "viability"}:
            self.error("values must have exactly readouts and viability")
            return
        readouts = value["readouts"]
        if not isinstance(readouts, dict):
            self.error("values.readouts must be an object of readout -> {model_unit, factors}")
        else:
            for name, entry in readouts.items():
                label = f"values.readouts[{name!r}]"
                if name not in self.readouts:
                    self.error(f"{label}: {name} is not declared under readouts")
                if not isinstance(entry, dict) or set(entry) != {"model_unit", "factors"}:
                    self.error(f"{label} must have exactly model_unit and factors")
                    continue
                unit = entry["model_unit"]
                if not _is_text(unit) or unit.strip().upper() == "TODO":
                    self.error(f"{label}: model_unit must be a real unit (the observable's unit in the model), "
                               "not empty or TODO")
                self.value_factors[name] = self._factor_map(label, entry["factors"])
            units: dict = {}  # model_unit describes the observable: its readouts must agree
            for name, entry in readouts.items():
                obs = self.readout_observable.get(name)
                if isinstance(entry, dict) and _is_text(entry.get("model_unit")) and isinstance(obs, str):
                    units.setdefault(obs, {})[name] = entry["model_unit"]
            for obs, by_readout in sorted(units.items()):
                if len(set(by_readout.values())) > 1:
                    shown = ", ".join(f"{r}: {u!r}" for r, u in sorted(by_readout.items()))
                    self.error(f"values.readouts: readouts of observable {obs} give different model_unit ({shown}); "
                               "model_unit is the observable's unit in the model")
        via = value["viability"]
        if not isinstance(via, dict) or set(via) != {"factors"}:
            self.error("values.viability must have exactly factors (value unit -> factor into a fraction)")
        else:
            self.viability_factors = self._factor_map("values.viability", via["factors"])

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
            self.readout_observable.setdefault(name, obs)
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
            overridden = self.overrides.setdefault(cid, {"parameters": set(), "initials": set()})
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
                        overridden[key].add(name)

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
        reference_ok = method != "relative_to_condition" or self.condition(label, "reference", item["reference"])
        self.forbid_dose(label, rows)
        self.scored.update(row["measurement_id"] for row in rows)
        if method == "absolute":
            self.value_factor(label, rows, "absolute values")
        elif reference_ok:
            self.normalised(label, method, rows, item.get("reference"))

    def value_factor(self, label: str, rows: list[dict], what: str) -> None:
        """Every row needs a values.readouts factor for its value_unit (decision U: never inferred)."""
        for row in rows:
            factors = self.value_factors.get(row["readout"], {})
            if row["value_unit"] and row["value_unit"] not in factors:
                self.error(f"{label}: measurement {row['measurement_id']} value_unit {row['value_unit']!r} has no "
                           f"positive factor in values.readouts[{row['readout']!r}].factors ({what} are converted "
                           "into the observable's model unit; units are never inferred)")

    def converted(self, row: dict):
        """The row's time in the model unit, or None when its unit or time is reported elsewhere."""
        factor = self.time_factors.get(row["time_unit"])
        return row["time"] * factor if factor is not None and _is_number(row["time"]) else None

    def one_unit(self, label: str, rows: list[dict], what: str) -> None:
        units = sorted({row["value_unit"] for row in rows if row["value_unit"]})
        if len(units) > 1:
            self.error(f"{label}: {what} mixes value_unit {units}; data are normalised within one group, "
                       "so its rows must share one unit")

    @staticmethod
    def _groups(rows: list[dict], *keys: str) -> dict:
        groups: dict = {}
        for row in rows:
            groups.setdefault(tuple(row[k] for k in keys), []).append(row)
        return groups

    @staticmethod
    def _divisors(rows: list[dict], use) -> list[dict]:
        """compare._divisors: a fit row divides by fit rows only; a hold-out row by the hold-out rows when there
        are some, else by the fit rows."""
        same = [row for row in rows if row["use"] == use]
        return same if same or use == "fit" else [row for row in rows if row["use"] == "fit"]

    def normalised(self, label: str, method, rows: list[dict], reference) -> None:
        """Decision N: the data of a normalised time_course are divided like the predictions; check the divisor.
        Rows whose target is 1 by construction are noted in self.trivial (they carry no information)."""
        if any(self.converted(row) is None or not _is_number(row["value"]) for row in rows):
            return  # reported by the time section / the table check
        if method == "fold_change_vs_t0":
            for (readout, cond), group in self._groups(rows, "readout", "condition").items():
                self.one_unit(label, group, f"readout {readout} in condition {cond}")
                zero = [row for row in group if self.converted(row) == 0.0]
                for row in zero:
                    self.trivial[row["measurement_id"]] = f"{label}: {row['measurement_id']} (a time-0 row)"
                reported = set()
                for row in group:
                    t0 = self._divisors(zero, row["use"])
                    if not t0:
                        among = ("a fit time-0 row (a fit row is never divided by a hold-out row)"
                                 if row["use"] == "fit" else "a time-0 row (hold-out or fit)")
                        self.error(f"{label}: measurement {row['measurement_id']} has no time-0 row of the same "
                                   f"readout and condition: it needs {among}; fold_change_vs_t0 divides data and "
                                   "predictions by their t0 value")
                        continue
                    key = tuple(r["measurement_id"] for r in t0)
                    if sum(r["value"] for r in t0) / len(t0) == 0 and key not in reported:
                        reported.add(key)
                        self.error(f"{label}: the t0 value of readout {readout} in condition {cond} "
                                   f"({', '.join(key)}) is 0; fold_change_vs_t0 cannot divide by it")
        elif method == "relative_to_condition":
            for readout, group in self._groups(rows, "readout").items():
                readout = readout[0]
                self.one_unit(label, group, f"readout {readout}")
                for row in group:
                    if row["condition"] == reference:
                        self.trivial[row["measurement_id"]] = (f"{label}: {row['measurement_id']} (a row of the "
                                                               "reference condition)")
                    t = self.converted(row)
                    refs = self._divisors([o for o in group if o["condition"] == reference
                                           and math.isclose(self.converted(o), t, rel_tol=TIME_RTOL)], row["use"])
                    if not refs:
                        among = "fit reference rows" if row["use"] == "fit" else "reference rows (hold-out or fit)"
                        self.error(f"{label}: measurement {row['measurement_id']} has no reference row (condition "
                                   f"{reference!r}, same readout) at time {t!r}: it needs {among}; "
                                   "relative_to_condition divides data and predictions by the reference")
                    elif sum(o["value"] for o in refs) / len(refs) == 0:
                        self.error(f"{label}: measurement {row['measurement_id']}: the reference value (condition "
                                   f"{reference!r}) at time {t!r} is 0; relative_to_condition cannot divide by it")
        elif method == "max_normalized":
            for readout, group in self._groups(rows, "readout").items():
                readout = readout[0]
                self.one_unit(label, group, f"readout {readout}")
                fit = [row for row in group if row["use"] == "fit"]
                if not fit:
                    shown = ", ".join(row["measurement_id"] for row in group)
                    self.error(f"{label}: measurements {shown} (readout {readout}) have no fit row of the same "
                               "readout in this rule; max_normalized divides every row, hold-out rows included, by "
                               "the fit rows' maximum")
                    continue
                if len(fit) == 1:
                    self.trivial[fit[0]["measurement_id"]] = (f"{label}: {fit[0]['measurement_id']} (the only fit "
                                                              f"row of readout {readout})")
                top = max(row["value"] for row in fit)
                if top <= 0:
                    self.error(f"{label}: the max of the fit rows of readout {readout} is {top!r}; max_normalized "
                               "needs a positive maximum")

    def same_time(self, label: str, rows: list[dict], time, what: str) -> None:
        """Each row's time, converted to the model unit, must equal `time` (relative tolerance 1e-9)."""
        for row in rows:
            factor = self.time_factors.get(row["time_unit"])
            if factor is None or not _is_number(row["time"]):
                continue  # reported by the time section / the table check
            converted = row["time"] * factor
            if not math.isclose(converted, time, rel_tol=TIME_RTOL):
                self.error(f"{label}: measurement {row['measurement_id']} time {converted!r} (model unit) "
                           f"must equal {what} {time!r}")

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
        else:  # rows are evaluated at the rule time: a row measured at another time (or unit) is refused
            self.same_time(label, rows, item["time"], "the rule time")
        self.forbid_dose(label, rows)
        if test_ok:
            self.scored.update(row["measurement_id"] for row in rows if row["condition"] == item["test"])
        if test_ok and ref_ok:
            present = {row["condition"] for row in rows}
            for what in ("test", "reference"):
                if item[what] not in present:
                    self.error(f"{label}: no measurement of the {what} condition {item[what]!r}")
            for row in rows:  # a third condition would be averaged into the reference mean
                if row["condition"] not in (item["test"], item["reference"]):
                    self.error(f"{label}: measurement {row['measurement_id']} condition {row['condition']!r} is "
                               f"neither the test {item['test']!r} nor the reference {item['reference']!r}")
            paired = [row for row in rows if row["condition"] in (item["test"], item["reference"])]
            if method == "difference":  # a difference keeps the data unit; a ratio is unitless
                self.value_factor(label, paired, "difference contrasts")
            for (readout, use), group in self._groups(paired, "readout", "use").items():
                self.one_unit(label, group, f"readout {readout} (use={use})")
                refs = [row["value"] for row in group if row["condition"] == item["reference"]]
                if method == "ratio" and refs and all(_is_number(v) for v in refs) and sum(refs) / len(refs) == 0:
                    self.error(f"{label}: the reference mean of readout {readout} (use={use}, condition "
                               f"{item['reference']!r}) is {sum(refs) / len(refs)!r}; a ratio contrast cannot "
                               "divide by it")
            refs = {(row["readout"], row["use"]) for row in rows if row["condition"] == item["reference"]}
            for row in rows:
                if row["condition"] == item["test"] and (row["readout"], row["use"]) not in refs:
                    self.error(f"{label}: test measurement {row['measurement_id']} ({row['readout']}, "
                               f"use={row['use']}) needs a reference measurement of the same readout and use")

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
        self.scored.update(row["measurement_id"] for row in rows)
        for row in rows:
            if row["value_unit"] and row["value_unit"] not in self.viability_factors:
                self.error(f"{label}: measurement {row['measurement_id']} value_unit {row['value_unit']!r} has no "
                           "positive factor in values.viability.factors (viability is converted into a fraction; "
                           "units are never inferred)")
        via = item["viability"]
        if isinstance(via, dict) and _is_number(via.get("time")):
            self.same_time(label, [row for row in rows if row["dose"] is not None], via["time"], "viability.time")
        if not isinstance(via, dict) or set(via) != {"observable", "untreated", "time"}:
            self.error(f"{label}: viability must have exactly observable, untreated, time")
            return
        if self.names is not None and via["observable"] not in self.names["observables"]:
            self.error(f"{label}: viability observable {via['observable']!r} is not an observable of the model")
        for row in rows:  # the rows are compared with the viability observable: their readout must map to it
            mapped = self.readout_observable.get(row["readout"])
            if row["readout"] in self.readout_observable and mapped != via["observable"]:
                self.error(f"{label}: measurement {row['measurement_id']} readout {row['readout']!r} maps to "
                           f"observable {mapped!r}, not to the viability observable {via['observable']!r}")
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
                elif self.defaults is not None:
                    default = self.defaults.get(fitted)
                    if default is None or not default > 0:
                        shown = "cannot be read from ode.py param_values (a literal)" if default is None \
                            else f"is {default!r}"
                        self.error(f"{label}: drug_entry K fits {fitted!r}, whose model default {shown}; a fitted K "
                                   "needs a positive model default, because hold-out scoring keeps the default of "
                                   "a parameter the researcher does not accept (1/(1 + D/K) needs K > 0)")
            elif not _is_number(k) or k <= 0:
                self.error(f"{label}: drug_entry K must be a positive number or {{\"fit\": name}}")
        elif form == "inhibitor_species" and set(entry) == {"form", "species"}:
            self.inhibitors.append((label, entry["species"]))
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

    def fit_overrides(self) -> None:
        """A fitted name a condition overrides (or an inhibitor dose sets) is masked there: refuse it."""
        for cid, overridden in self.overrides.items():
            for key, kind in (("parameters", "parameter"), ("initials", "initial")):
                for name in sorted(overridden[key]):
                    if self.fit_kinds.get(name) == kind:
                        self.error(f"fit {name}: condition {cid} also overrides it ({key}); the override replaces "
                                   f"the fitted value there, so those measurements would not constrain {name}")
        for label, species in self.inhibitors:
            if isinstance(species, str) and self.fit_kinds.get(species) == "initial":
                self.error(f"fit {species}: {label} sets it to the dose (inhibitor_species), which replaces the "
                           "fitted value in every dose condition")

    def fit_targets(self) -> None:
        """At least one use=fit row is scored with information; rows 1 by construction (self.trivial) do not count."""
        fit = sorted(mid for mid in self.scored if mid in self.rows and self.rows[mid]["use"] == "fit")
        if self.rows and not [mid for mid in fit if mid not in self.trivial]:
            trivial = "; ".join(self.trivial[mid] for mid in fit)
            self.error("no use=fit measurement is scored by a rule with information (contrast reference rows are not "
                       "scored; a normalised row that is 1 by construction carries none"
                       + (f": {trivial}" if trivial else "") + "): the calibration objective would be constant")

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
    if set(data) != set(KEYS):
        checker.error(f"mapping keys must be exactly {', '.join(KEYS)}")
    else:
        # inputs first: the other sections are checked against the model names and table rows.
        checker.inputs(data["inputs"])
        checker.readouts_section(data["readouts"])
        checker.conditions_section(data["conditions"])
        checker.fit_section(data["fit"])
        checker.time(data["time"])
        checker.dose(data["dose"])
        checker.values_section(data["values"])
        checker.rules_section(data["rules"])
        checker.unmapped_section(data["unmapped"])
        checker.fit_overrides()
        checker.coverage()
        checker.fit_targets()
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
    d.add_argument("--revision", help="optional: must equal the captured revision derived from the bundle")
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
