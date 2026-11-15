"""Turn simulated observables into predictions and an objective (stdlib only).

This file is copied verbatim into calibration bundles as `calibration/compare.py`; it must not
import anything from `scripts/` or any third-party package.

`resolve(mapping, rows)` takes an already-checked ode_mapping dict and the parsed measurement rows
and returns a JSON-serialisable `spec` (string keys only; times and doses are floats):
  conditions       {name: {parameters: {name: value*factor}, initials: {species: value*factor}}}
  dose_conditions  {name: {base, dose, scale_parameter, K: number | {fit: name}}
                         | {base, dose, inhibitor_species}}     one per (rule, condition, dose)
  times_model      {condition or dose condition: sorted [float, ...] in the model time unit, incl. 0.0}
  rules            the mapping's rules
  fit              [{name, kind, lower, upper, log10_lower, log10_upper}]
  measurements     {id: {rule_id, type, observable, condition, time, dose, dose_condition, use, value,
                         target, role}}   time/dose in model units; role is "fit_target" or "reference"
  time_model_unit  the model time unit
  dose_model_unit  the model dose unit (mapping["dose"]["model_unit"]; the dose axis of the figures)

Runner contract:
  * for every name in spec["conditions"] and spec["dose_conditions"], call
    `condition_overrides(spec, name, fitted)` (fitted = {fit name: value in model units}); it returns
    {"parameters": {...}, "initials": {...}, "scales": {parameter: multiplier}}; `parameters` and
    `initials` are absolute values to set; `scales` multiplies the model's current value of that
    parameter (it only arises from target_activity_scaling when the base condition does not itself
    override that parameter; otherwise the product is already in `parameters`);
  * simulate that condition and record every time in spec["times_model"][name];
  * call `predict(spec, sims)` with sims[name][observable] = {time: value}. Times are looked up as
    the exact floats stored in spec (after a JSON round trip), so the runner must key by those floats.
  * `objective(spec, predictions, use)`.

Rules (per measurement; a missing sim, solver failure or zero denominator gives nan, never raises):
  fold_change_vs_t0       v(t) / v(0)
  relative_to_condition   v_cond(t) / v_reference(t)
  max_normalized          v(t) / max of v over the simulated points (condition, time) of the rule's own
                          measurements for that observable (all conditions of the rule, one max per observable)
  absolute                v(t)
  endpoint_contrast       every row's converted time must equal the rule time T (math.isclose, rel_tol 1e-9;
                          ValueError otherwise); the rows of the test condition are predicted as ratio v_test(T)/v_ref(T) or
                          difference v_test(T)-v_ref(T); their target is the same contrast of the data
                          (test value vs the mean of the reference rows of the same readout AND the same
                          `use`, so fit targets never see hold-out references; none gives a nan target). Reference
                          rows are role "reference": no prediction, no term in the objective.
  dose_response           viability = obs(treated at dose D, T) / obs(untreated, T), T = viability.time;
                          D = dose * mapping["dose"]["factors"][dose_unit] (required; ValueError when a unit
                          has no positive factor); K is in the model dose unit mapping["dose"]["model_unit"].
                          A fitted K <= 0 gives a nan scale (hence nan predictions and PENALTY).
                          Every dose row is evaluated at viability.time (each dose row's converted time must
                          equal it within rel_tol 1e-9, ValueError otherwise; the mapping check also requires the
                          rows' readout to map to viability.observable); rows of one condition with the same converted
                          dose share one dose condition.
                          target_activity_scaling multiplies the parameter by 1/(1 + D/K);
                          inhibitor_species sets the species' initial amount to D.

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


def _times(spec_times: dict, condition: str, *times: float) -> None:
    spec_times.setdefault(condition, {0.0}).update(float(t) for t in times)


def _dose_factor(mapping: dict, unit) -> float:
    factors = mapping["dose"]["factors"]
    if unit not in factors or not factors[unit] > 0:
        raise ValueError(f"dose_unit {unit!r} has no positive factor in dose.factors")
    return float(factors[unit])


def _same_time(mid: str, converted: float, time: float, what: str) -> None:
    if not math.isclose(converted, time, rel_tol=TIME_RTOL):
        raise ValueError(f"measurement {mid}: time {converted!r} (model unit) is not {what} {time!r}")


def resolve(mapping: dict, rows: list[dict]) -> dict:
    by_id = {r["measurement_id"]: r for r in rows}
    observable = {x["readout"]: x["observable"] for x in mapping["readouts"]}
    factors = mapping["time"]["factors"]
    conditions = {c["condition"]: {"parameters": _overrides(c["parameters"]), "initials": _overrides(c["initials"])}
                  for c in mapping["conditions"]}
    times: dict = {name: {0.0} for name in conditions}
    dose_conditions: dict = {}
    meas: dict = {}
    for rule in mapping["rules"]:
        kind, rid = rule["type"], rule["rule_id"]
        for mid in rule["measurements"]:
            row = by_id[mid]
            entry = {"rule_id": rid, "type": kind, "observable": observable[row["readout"]],
                     "condition": row["condition"], "time": float(row["time"]) * float(factors[row["time_unit"]]),
                     "dose": None, "dose_condition": None, "use": row["use"], "value": row["value"],
                     "target": row["value"], "role": "fit_target"}
            if kind == "time_course":
                _times(times, row["condition"], entry["time"])
                if rule["method"] == "relative_to_condition":
                    _times(times, rule["reference"], entry["time"])
            elif kind == "endpoint_contrast":
                _same_time(mid, entry["time"], float(rule["time"]), "the rule time")
                entry["time"] = float(rule["time"])
                for name in (rule["test"], rule["reference"]):
                    _times(times, name, entry["time"])
                if row["condition"] != rule["test"]:
                    entry["role"] = "reference"
            else:
                via, drug = rule["viability"], rule["drug_entry"]
                entry["observable"] = via["observable"]  # the check requires the readout to map to it
                _same_time(mid, entry["time"], float(via["time"]), "viability.time")
                entry["time"] = float(via["time"])
                entry["dose"] = float(row["dose"]) * _dose_factor(mapping, row["dose_unit"])
                name = f"{rid}|{row['condition']}|dose={entry['dose']!r}"
                if drug["form"] == "target_activity_scaling":
                    dose_conditions[name] = {"base": row["condition"], "dose": entry["dose"],
                                             "scale_parameter": drug["parameter"], "K": drug["K"]}
                else:
                    dose_conditions[name] = {"base": row["condition"], "dose": entry["dose"],
                                             "inhibitor_species": drug["species"]}
                entry["dose_condition"] = name
                _times(times, name, entry["time"])
                _times(times, via["untreated"], entry["time"])
            meas[mid] = entry
        if kind == "endpoint_contrast":
            for mid in rule["measurements"]:
                if meas[mid]["role"] == "fit_target":
                    refs = [meas[r]["value"] for r in rule["measurements"] if meas[r]["role"] == "reference"
                            and by_id[r]["readout"] == by_id[mid]["readout"]
                            and by_id[r]["use"] == by_id[mid]["use"]]
                    meas[mid]["target"] = _contrast(rule["method"], meas[mid]["value"],
                                                    sum(refs) / len(refs) if refs else None)
    fit = [{"name": f["name"], "kind": f["kind"], "lower": float(f["lower"]), "upper": float(f["upper"]),
            "log10_lower": math.log10(f["lower"]), "log10_upper": math.log10(f["upper"])} for f in mapping["fit"]]
    return {"time_model_unit": mapping["time"]["model_unit"], "dose_model_unit": mapping["dose"]["model_unit"],
            "conditions": conditions,
            "dose_conditions": dose_conditions,
            "times_model": {name: sorted(values) for name, values in times.items()},
            "rules": mapping["rules"], "fit": fit, "measurements": meas}


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
    maxima: dict = {}
    for mid, m in meas.items():
        if m["type"] == "time_course" and rules[m["rule_id"]]["method"] == "max_normalized":
            key = (m["rule_id"], m["observable"])
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
                points = maxima[(m["rule_id"], obs)]
                out[mid] = _ratio(v, max(points)) if all(math.isfinite(p) for p in points) else math.nan
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
