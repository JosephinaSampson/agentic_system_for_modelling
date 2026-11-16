"""Turn simulated observables into predictions and an objective (stdlib only).

This file is copied verbatim into calibration bundles as `calibration/compare.py`; it must not
import anything from `scripts/` or any third-party package.

`resolve(mapping, rows)` takes an already-checked ode_mapping dict and the parsed measurement rows
and returns a JSON-serialisable `spec` (string keys only; times and doses are floats):
  conditions       {name: {parameters: {name: value*factor}, initials: {species: value*factor}}}
  dose_conditions  {name: {base, dose, scale_parameter, K: number | {fit: name}}
                         | {base, dose, inhibitor_species}}     one per (rule, condition, dose)
  times_model      {condition or dose condition: sorted [float, ...] in the model time unit, incl. 0.0}; only
                   the simulations some row needs (needed(spec, "all")): an unused condition is never simulated
  rules            the mapping's rules
  fit              [{name, kind, lower, upper, log10_lower, log10_upper}]
  measurements     {id: {rule_id, type, readout, observable, condition, time, dose, dose_condition, use, value,
                         value_unit, target, role}}   time/dose in model units; value is the raw table value,
                   target the value normalised (or converted) exactly like the prediction; role is
                   "fit_target" or "reference"
  time_model_unit  the model time unit
  dose_model_unit  the model dose unit (mapping["dose"]["model_unit"]; the dose axis of the figures)

Runner contract:
  * for every name in spec["conditions"] and spec["dose_conditions"], call
    `condition_overrides(spec, name, fitted)` (fitted = {fit name: value in model units}); it returns
    {"parameters": {...}, "initials": {...}, "scales": {parameter: multiplier}}; `parameters` and
    `initials` are absolute values to set; `scales` multiplies the model's current value of that
    parameter (it only arises from target_activity_scaling when the base condition does not itself
    override that parameter; otherwise the product is already in `parameters`);
  * simulate that condition and record every time in spec["times_model"][name] (the runner may simulate only
    needed(spec, use), the conditions and times the rows of that use need);
  * call `predict(spec, sims)` with sims[name][observable] = {time: value}. Times are looked up as
    the exact floats stored in spec (after a JSON round trip), so the runner must key by those floats.
  * `objective(spec, predictions, use)`.

Rules (per measurement; a missing sim, solver failure or zero denominator gives nan, never raises). The data
target is normalised the same way as the prediction (decision N). "Group" = the rule's rows of the same readout;
a divisor is never taken from hold-out rows for a fit row, so hold-out rows never change a fit target or a fit
prediction. The divisor rows of a fit row are fit rows; those of a hold-out row are the hold-out rows of the same
kind when there are some, else the fit rows (a t0 value or a reference is only a normaliser, and the fit sees it):
  fold_change_vs_t0       v(t) / v(0); target = value / mean of the group's time-0 rows of the same condition
                          (divisor rows as above)
  relative_to_condition   v_cond(t) / v_reference(t); target = value / mean of the group's reference-condition
                          rows at the same converted time (math.isclose, rel_tol 1e-9; divisor rows as above)
  max_normalized          every row of the group, fit or hold-out, is divided by the FIT rows' scale: prediction
                          v(t) / max of v over the simulated points (condition, time) of the group's fit rows;
                          target = value / max of the group's fit values (nan unless that max is > 0, or when the
                          group has no fit row). A hold-out row is thus scored on the fit's scale (a single
                          hold-out row is not 1/1 by construction), and needed(spec, "holdout") includes the fit
                          rows' points of such a group.
  absolute                v(t); target = value * mapping["values"]["readouts"][readout]["factors"][value_unit]
                          (ValueError when that factor is missing or not positive)
  endpoint_contrast       every row's condition must be the test or the reference (ValueError otherwise) and its
                          converted time must equal the rule time T (math.isclose, rel_tol 1e-9;
                          ValueError otherwise); the rows of the test condition are predicted as ratio v_test(T)/v_ref(T) or
                          difference v_test(T)-v_ref(T); their target is the same contrast of the data
                          (test value vs the mean of the reference rows of the same readout AND the same
                          `use`, so fit targets never see hold-out references; none gives a nan target); a
                          difference target is then converted into the model unit with the values.readouts
                          factor (as for absolute; the group shares one value_unit), a ratio is unitless. Reference
                          rows are role "reference": no prediction, no term in the objective.
  dose_response           viability = obs(treated at dose D, T) / obs(untreated, T), T = viability.time;
                          target = value * mapping["values"]["viability"]["factors"][value_unit] (a fraction;
                          ValueError when the factor is missing);
                          D = dose * mapping["dose"]["factors"][dose_unit] (required; ValueError when a unit
                          has no positive factor); K is in the model dose unit mapping["dose"]["model_unit"].
                          A fitted K <= 0 gives a nan scale (hence nan predictions and PENALTY).
                          Every dose row is evaluated at viability.time (each dose row's converted time must
                          equal it within rel_tol 1e-9, ValueError otherwise; the mapping check also requires the
                          rows' readout to map to viability.observable); rows of one condition with the same converted
                          dose share one dose condition.
                          target_activity_scaling multiplies the parameter by 1/(1 + D/K);
                          inhibitor_species sets the species' initial amount to D.

A fitted name that a condition overrides (or an inhibitor_species dose sets) raises ValueError in resolve.

objective = unweighted sum of squared differences prediction - target over rows with the requested
`use` (default "fit"); `error` columns are not weights. Any nan/inf prediction or target, or a missing
prediction, gives PENALTY.
"""

from __future__ import annotations

import math

PENALTY = 1e12
TIME_RTOL = 1e-9  # a row's converted time equals a rule time within this relative tolerance


def _overrides(items) -> dict:
    return {item["name"]: item["value"] * item["factor"] for item in items}


def _dose_factor(mapping: dict, unit) -> float:
    factors = mapping["dose"]["factors"]
    if unit not in factors or not factors[unit] > 0:
        raise ValueError(f"dose_unit {unit!r} has no positive factor in dose.factors")
    return float(factors[unit])


def _same_time(mid: str, converted: float, time: float, what: str) -> None:
    if not math.isclose(converted, time, rel_tol=TIME_RTOL):
        raise ValueError(f"measurement {mid}: time {converted!r} (model unit) is not {what} {time!r}")


def _positive_factor(factors, unit, mid: str, where: str) -> float:
    factor = factors.get(unit) if isinstance(factors, dict) else None
    if isinstance(factor, bool) or not isinstance(factor, (int, float)) or not factor > 0:
        raise ValueError(f"measurement {mid}: value_unit {unit!r} has no positive factor in {where} "
                         "(units are never inferred)")
    return float(factor)


def _readout_factor(mapping: dict, row: dict) -> float:
    entry = mapping["values"]["readouts"].get(row["readout"])
    factors = entry.get("factors") if isinstance(entry, dict) else None
    return _positive_factor(factors, row["value_unit"], row["measurement_id"],
                            f"values.readouts[{row['readout']!r}].factors")


def _viability_factor(mapping: dict, row: dict) -> float:
    return _positive_factor(mapping["values"]["viability"]["factors"], row["value_unit"], row["measurement_id"],
                            "values.viability.factors")


def _mean(values: list) -> float | None:
    return sum(values) / len(values) if values else None


def _fit_overrides(mapping: dict) -> None:
    """A fitted name that a condition (or an inhibitor dose) also sets would be ignored there: refuse it."""
    fitted = {(f["kind"], f["name"]) for f in mapping["fit"]}
    for c in mapping["conditions"]:
        for kind, key in (("parameter", "parameters"), ("initial", "initials")):
            for item in c[key]:
                if (kind, item["name"]) in fitted:
                    raise ValueError(f"fit {item['name']}: condition {c['condition']} overrides it ({key}); "
                                     "the override would replace the fitted value there")
    for rule in mapping["rules"]:
        drug = rule.get("drug_entry") if rule["type"] == "dose_response" else None
        if drug and drug["form"] == "inhibitor_species" and ("initial", drug["species"]) in fitted:
            raise ValueError(f"fit {drug['species']}: rule {rule['rule_id']} sets it to the dose (inhibitor_species)")


def _divisors(rows: list[dict], use: str) -> list[dict]:
    """The divisor rows of a row of this `use`, among candidate `rows` (same rule, readout and condition or
    time): a fit row is divided by fit rows only; a hold-out row by the hold-out rows when there are some, else
    by the fit rows."""
    same = [o for o in rows if o["use"] == use]
    return same if same or use == "fit" else [o for o in rows if o["use"] == "fit"]


def _normalise_time_course(rule: dict, entries: list[dict]) -> None:
    """The data side of a time_course rule, normalised exactly like the predictions (decision N)."""
    method = rule["method"]
    for e in entries:
        group = [o for o in entries if o["readout"] == e["readout"]]
        if method == "fold_change_vs_t0":
            t0 = _divisors([o for o in group if o["condition"] == e["condition"] and o["time"] == 0.0], e["use"])
            e["target"] = _ratio(e["value"], _mean([o["value"] for o in t0]) if t0 else math.nan)
        elif method == "relative_to_condition":
            refs = _divisors([o for o in group if o["condition"] == rule["reference"]
                              and math.isclose(o["time"], e["time"], rel_tol=TIME_RTOL)], e["use"])
            e["target"] = _ratio(e["value"], _mean([o["value"] for o in refs]) if refs else math.nan)
        elif method == "max_normalized":
            fit = [o["value"] for o in group if o["use"] == "fit"]
            top = max(fit) if fit else math.nan
            e["target"] = e["value"] / top if top > 0 else math.nan


def resolve(mapping: dict, rows: list[dict]) -> dict:
    _fit_overrides(mapping)
    by_id = {r["measurement_id"]: r for r in rows}
    observable = {x["readout"]: x["observable"] for x in mapping["readouts"]}
    factors = mapping["time"]["factors"]
    conditions = {c["condition"]: {"parameters": _overrides(c["parameters"]), "initials": _overrides(c["initials"])}
                  for c in mapping["conditions"]}
    dose_conditions: dict = {}
    meas: dict = {}
    unit_factor: dict = {}  # measurement id -> values.readouts factor (difference contrasts)
    for rule in mapping["rules"]:
        kind, rid = rule["type"], rule["rule_id"]
        entries = []
        for mid in rule["measurements"]:
            row = by_id[mid]
            entry = {"rule_id": rid, "type": kind, "readout": row["readout"], "observable": observable[row["readout"]],
                     "condition": row["condition"], "time": float(row["time"]) * float(factors[row["time_unit"]]),
                     "dose": None, "dose_condition": None, "use": row["use"], "value": row["value"],
                     "value_unit": row["value_unit"], "target": row["value"], "role": "fit_target"}
            if kind == "time_course":
                if rule["method"] == "absolute":
                    entry["target"] = row["value"] * _readout_factor(mapping, row)
            elif kind == "endpoint_contrast":
                if row["condition"] not in (rule["test"], rule["reference"]):
                    raise ValueError(f"measurement {mid}: condition {row['condition']!r} is neither the test "
                                     f"{rule['test']!r} nor the reference {rule['reference']!r} of rule {rid}")
                _same_time(mid, entry["time"], float(rule["time"]), "the rule time")
                entry["time"] = float(rule["time"])
                if rule["method"] == "difference":  # a difference keeps the data unit: convert it
                    unit_factor[mid] = _readout_factor(mapping, row)
                if row["condition"] == rule["reference"]:
                    entry["role"] = "reference"
            else:
                via, drug = rule["viability"], rule["drug_entry"]
                entry["observable"] = via["observable"]  # the check requires the readout to map to it
                _same_time(mid, entry["time"], float(via["time"]), "viability.time")
                entry["time"] = float(via["time"])
                entry["target"] = row["value"] * _viability_factor(mapping, row)
                entry["dose"] = float(row["dose"]) * _dose_factor(mapping, row["dose_unit"])
                name = f"{rid}|{row['condition']}|dose={entry['dose']!r}"
                if drug["form"] == "target_activity_scaling":
                    dose_conditions[name] = {"base": row["condition"], "dose": entry["dose"],
                                             "scale_parameter": drug["parameter"], "K": drug["K"]}
                else:
                    dose_conditions[name] = {"base": row["condition"], "dose": entry["dose"],
                                             "inhibitor_species": drug["species"]}
                entry["dose_condition"] = name
            meas[mid] = entry
            entries.append(entry)
        if kind == "time_course":
            _normalise_time_course(rule, entries)
        elif kind == "endpoint_contrast":
            for mid, e in zip(rule["measurements"], entries):
                if e["role"] == "fit_target":
                    refs = [o["value"] for o in entries if o["role"] == "reference"
                            and o["readout"] == e["readout"] and o["use"] == e["use"]]
                    e["target"] = _contrast(rule["method"], e["value"], _mean(refs)) * unit_factor.get(mid, 1.0)
    fit = [{"name": f["name"], "kind": f["kind"], "lower": float(f["lower"]), "upper": float(f["upper"]),
            "log10_lower": math.log10(f["lower"]), "log10_upper": math.log10(f["upper"])} for f in mapping["fit"]]
    spec = {"time_model_unit": mapping["time"]["model_unit"], "dose_model_unit": mapping["dose"]["model_unit"],
            "conditions": conditions,
            "dose_conditions": dose_conditions,
            "rules": mapping["rules"], "fit": fit, "measurements": meas}
    spec["times_model"] = needed(spec, "all")
    return spec


def needed(spec: dict, use: str = "fit") -> dict:
    """{simulation name: sorted model times incl. 0.0} that the predictions of the rows with this use need
    ("all": every row). A condition no rule, reference or untreated entry needs is never simulated."""
    rules = {r["rule_id"]: r for r in spec["rules"]}
    out: dict = {}

    def add(name: str, time: float) -> None:
        out.setdefault(name, {0.0}).add(float(time))

    fit_points: dict = {}  # (rule, readout) -> the fit rows' points of a max_normalized group (its scale)
    for m in spec["measurements"].values():
        if m["type"] == "time_course" and m["use"] == "fit" and rules[m["rule_id"]]["method"] == "max_normalized":
            fit_points.setdefault((m["rule_id"], m["readout"]), []).append((m["condition"], m["time"]))
    for m in spec["measurements"].values():
        if (use != "all" and m["use"] != use) or m["role"] == "reference":
            continue
        rule, t = rules[m["rule_id"]], m["time"]
        if m["type"] == "time_course":
            add(m["condition"], t)
            if rule["method"] == "relative_to_condition":
                add(rule["reference"], t)
            elif rule["method"] == "max_normalized" and m["use"] != "fit":
                for condition, time in fit_points.get((m["rule_id"], m["readout"]), []):
                    add(condition, time)  # a hold-out row is divided by the fit rows' simulated maximum
        elif m["type"] == "endpoint_contrast":
            add(rule["test"], t)
            add(rule["reference"], t)
        else:
            add(m["dose_condition"], t)
            add(rule["viability"]["untreated"], t)
    return {name: sorted(times) for name, times in out.items()}


def condition_overrides(spec: dict, name: str, fitted: dict) -> dict:
    """Materialise a condition (or synthetic dose condition) for the fitted values {fit name: value}."""
    if name in spec["conditions"]:
        base = spec["conditions"][name]
        return {"parameters": dict(base["parameters"]), "initials": dict(base["initials"]), "scales": {}}
    dose = spec["dose_conditions"][name]
    base = spec["conditions"][dose["base"]]
    parameters, initials, scales = dict(base["parameters"]), dict(base["initials"]), {}
    if "scale_parameter" in dose:
        k = dose["K"]
        k = fitted[k["fit"]] if isinstance(k, dict) else k
        factor = 1.0 / (1.0 + dose["dose"] / k) if k > 0 else math.nan
        target = dose["scale_parameter"]
        if target in parameters:
            parameters[target] *= factor
        else:
            scales[target] = factor
    else:
        initials[dose["inhibitor_species"]] = dose["dose"]
    return {"parameters": parameters, "initials": initials, "scales": scales}


def _contrast(method: str, test, reference) -> float:
    if test is None or reference is None:
        return math.nan
    if method == "ratio":
        return test / reference if reference != 0 else math.nan
    return test - reference


def _get(sims: dict, condition: str, observable: str, time: float) -> float:
    try:
        return float(sims[condition][observable][float(time)])
    except (KeyError, TypeError, ValueError):
        return math.nan


def _ratio(num: float, den: float) -> float:
    if not (math.isfinite(num) and math.isfinite(den)) or den == 0:
        return math.nan
    return num / den


def predict(spec: dict, sims: dict) -> dict:
    meas = spec["measurements"]
    rules = {r["rule_id"]: r for r in spec["rules"]}
    maxima: dict = {}  # one normaliser per (rule, readout), from its fit rows: hold-out rows never change it
    for mid, m in meas.items():
        if m["type"] == "time_course" and m["use"] == "fit" and rules[m["rule_id"]]["method"] == "max_normalized":
            key = (m["rule_id"], m["readout"])
            maxima.setdefault(key, []).append(_get(sims, m["condition"], m["observable"], m["time"]))
    out = {}
    for mid, m in meas.items():
        rule, obs, t = rules[m["rule_id"]], m["observable"], m["time"]
        if m["role"] == "reference":
            continue
        if m["type"] == "time_course":
            v = _get(sims, m["condition"], obs, t)
            method = rule["method"]
            if method == "absolute":
                out[mid] = v
            elif method == "fold_change_vs_t0":
                out[mid] = _ratio(v, _get(sims, m["condition"], obs, 0.0))
            elif method == "relative_to_condition":
                out[mid] = _ratio(v, _get(sims, rule["reference"], obs, t))
            else:
                points = maxima.get((m["rule_id"], m["readout"]), [])
                out[mid] = _ratio(v, max(points)) if points and all(math.isfinite(p) for p in points) else math.nan
        elif m["type"] == "endpoint_contrast":
            test = _get(sims, rule["test"], obs, t)
            ref = _get(sims, rule["reference"], obs, t)
            out[mid] = _contrast(rule["method"], test, ref) if math.isfinite(test + ref) else math.nan
        else:
            out[mid] = _ratio(_get(sims, m["dose_condition"], obs, t),
                              _get(sims, rule["viability"]["untreated"], obs, t))
    return out


def objective(spec: dict, predictions: dict, use: str = "fit") -> float:
    total = 0.0
    try:
        for mid, m in spec["measurements"].items():
            if m["use"] != use or m["role"] == "reference":
                continue
            diff = float(predictions[mid]) - float(m["target"])
            if not math.isfinite(diff):
                return PENALTY
            total += diff * diff
    except (KeyError, TypeError, ValueError):
        return PENALTY
    return total if math.isfinite(total) else PENALTY
