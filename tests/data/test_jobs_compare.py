import ast
import json
import math
import unittest
from pathlib import Path

from scripts.jobs.runtime import compare

SRC = Path(compare.__file__)


def row(mid, cond, time, value, *, unit="min", readout="r", dose=None, dose_unit=None, use="fit"):
    return {"measurement_id": mid, "readout": readout, "condition": cond, "time": time, "time_unit": unit,
            "dose": dose, "dose_unit": dose_unit, "value": value, "value_unit": "au", "error": None,
            "error_kind": None, "n": None, "use": use, "source": "s", "locator": "l"}


def ov(name, value, factor=1.0):
    return {"name": name, "value": value, "value_unit": "u", "model_unit": "u", "factor": factor}


def mapping(rules, conditions=None, **extra):
    conds = conditions or [{"condition": c, "parameters": [], "initials": []} for c in ("ctrl", "drug", "ref")]
    m = {"kind": "ode_mapping", "schema_version": 1, "time": {"model_unit": "min", "factors": {"min": 1, "h": 60}},
         "readouts": [{"readout": "r", "observable": "obs"}], "conditions": conds, "rules": rules,
         "dose": {"model_unit": "nM", "factors": {"uM": 1000.0, "nM": 1.0}},
         "fit": [{"name": "k1", "kind": "parameter", "lower": 0.01, "upper": 100.0, "unit": "1/min",
                  "justification": {"text": "t"}}], "unmapped": []}
    m.update(extra)
    return m


def tc(method, ids, **kw):
    return {"rule_id": "tc", "type": "time_course", "method": method, "measurements": ids, **kw}


class CompareTests(unittest.TestCase):
    def test_fold_change_vs_t0(self):
        rows = [row("m1", "ctrl", 30.0, 9.0)]
        spec = compare.resolve(mapping([tc("fold_change_vs_t0", ["m1"])]), rows)
        self.assertEqual(spec["times_model"]["ctrl"], [0.0, 30.0])
        pred = compare.predict(spec, {"ctrl": {"obs": {0.0: 0.5, 30.0: 2.0}}})
        self.assertEqual(pred["m1"], 4.0)

    def test_absolute(self):
        spec = compare.resolve(mapping([tc("absolute", ["m1"])]), [row("m1", "ctrl", 5.0, 1.0)])
        self.assertEqual(compare.predict(spec, {"ctrl": {"obs": {0.0: 1.0, 5.0: 3.0}}})["m1"], 3.0)

    def test_relative_to_condition(self):
        rows = [row("m1", "ctrl", 10.0, 1.0)]
        spec = compare.resolve(mapping([tc("relative_to_condition", ["m1"], reference="ref")]), rows)
        self.assertIn(10.0, spec["times_model"]["ref"])
        sims = {"ctrl": {"obs": {0.0: 1.0, 10.0: 3.0}}, "ref": {"obs": {0.0: 1.0, 10.0: 1.5}}}
        self.assertEqual(compare.predict(spec, sims)["m1"], 2.0)

    def test_max_normalized(self):
        # max over the rule's own measurement points (condition, time) of that observable: 3 and 8 and 2, not t=0
        rows = [row("m1", "ctrl", 10.0, 1.0), row("m2", "ctrl", 20.0, 1.0), row("m3", "drug", 10.0, 1.0)]
        spec = compare.resolve(mapping([tc("max_normalized", ["m1", "m2", "m3"])]), rows)
        sims = {"ctrl": {"obs": {0.0: 100.0, 10.0: 3.0, 20.0: 8.0}}, "drug": {"obs": {0.0: 50.0, 10.0: 2.0}}}
        pred = compare.predict(spec, sims)
        self.assertEqual([pred["m1"], pred["m2"], pred["m3"]], [3 / 8, 1.0, 2 / 8])
        sims["ctrl"]["obs"][10.0] = sims["ctrl"]["obs"][20.0] = sims["drug"]["obs"][10.0] = 0.0
        self.assertTrue(math.isnan(compare.predict(spec, sims)["m1"]))

    def test_zero_denominator_is_nan(self):
        spec = compare.resolve(mapping([tc("fold_change_vs_t0", ["m1"])]), [row("m1", "ctrl", 30.0, 1.0)])
        pred = compare.predict(spec, {"ctrl": {"obs": {0.0: 0.0, 30.0: 2.0}}})
        self.assertTrue(math.isnan(pred["m1"]))

    def test_endpoint_ratio_and_difference(self):
        rows = [row("a", "drug", 60.0, 6.0), row("b", "ref", 60.0, 2.0)]
        for method, sim, data in (("ratio", 2.5, 3.0), ("difference", 3.0, 4.0)):
            rule = {"rule_id": "ec", "type": "endpoint_contrast", "method": method, "test": "drug",
                    "reference": "ref", "time": 60.0, "measurements": ["a", "b"]}
            spec = compare.resolve(mapping([rule]), rows)
            sims = {"drug": {"obs": {0.0: 1.0, 60.0: 5.0}}, "ref": {"obs": {0.0: 1.0, 60.0: 2.0}}}
            pred = compare.predict(spec, sims)
            self.assertEqual(pred["a"], sim)
            self.assertNotIn("b", pred)  # the reference row only defines the data contrast
            self.assertEqual(spec["measurements"]["a"]["target"], data)

    def test_time_conversion_hours_to_minutes(self):
        spec = compare.resolve(mapping([tc("absolute", ["m1"])]), [row("m1", "ctrl", 1.5, 1.0, unit="h")])
        self.assertEqual(spec["times_model"]["ctrl"], [0.0, 90.0])
        self.assertEqual(spec["measurements"]["m1"]["time"], 90.0)
        spec = json.loads(json.dumps(spec))  # the runner reads spec back from JSON
        sims = {"ctrl": {"obs": {0.0: 0.0, 90.0: 7.0, 1.5: 9.0}}}
        self.assertEqual(compare.predict(spec, sims)["m1"], 7.0)
        self.assertTrue(math.isnan(compare.predict(spec, {"ctrl": {"obs": {0.0: 0.0, 1.5: 9.0}}})["m1"]))

    def test_condition_overrides_apply_factor(self):
        conds = [{"condition": "ctrl", "parameters": [ov("p", 2.0, 0.5)], "initials": [ov("S", 4.0, 10.0)]}]
        spec = compare.resolve(mapping([tc("absolute", ["m1"])], conds), [row("m1", "ctrl", 1.0, 1.0)])
        self.assertEqual(spec["conditions"]["ctrl"], {"parameters": {"p": 1.0}, "initials": {"S": 40.0}})
        self.assertEqual(compare.condition_overrides(spec, "ctrl", {}),
                         {"parameters": {"p": 1.0}, "initials": {"S": 40.0}, "scales": {}})

    def _dose_rule(self, entry):
        return {"rule_id": "dr", "type": "dose_response", "measurements": ["d1", "d2"], "drug_entry": entry,
                "viability": {"observable": "via", "untreated": "ctrl", "time": 48.0}}

    def test_dose_scaling_and_viability(self):
        rows = [row("d1", "drug", 48.0, 0.5, dose=2.0, dose_unit="uM"),
                row("d2", "drug", 48.0, 0.9, dose=0.0, dose_unit="uM", use="holdout")]
        entry = {"form": "target_activity_scaling", "parameter": "kp", "K": 1000.0}
        spec = compare.resolve(mapping([self._dose_rule(entry)]), rows)
        spec = json.loads(json.dumps(spec))
        name = spec["measurements"]["d1"]["dose_condition"]
        self.assertEqual(spec["measurements"]["d1"]["dose"], 2000.0)
        self.assertEqual(spec["times_model"][name], [0.0, 48.0])
        self.assertIn(48.0, spec["times_model"]["ctrl"])
        cond = compare.condition_overrides(spec, name, {})
        self.assertAlmostEqual(cond["scales"]["kp"], 1 / 3)
        sims = {name: {"via": {48.0: 3.0}}, "ctrl": {"via": {48.0: 6.0}}}
        sims[spec["measurements"]["d2"]["dose_condition"]] = {"via": {48.0: 6.0}}
        pred = compare.predict(spec, sims)
        self.assertEqual(pred["d1"], 0.5)
        self.assertEqual(pred["d2"], 1.0)

    def test_dose_scaling_fitted_k_and_base_override(self):
        conds = [{"condition": c, "parameters": [ov("kp", 6.0)] if c == "drug" else [], "initials": []}
                 for c in ("ctrl", "drug")]
        entry = {"form": "target_activity_scaling", "parameter": "kp", "K": {"fit": "k1"}}
        rows = [row("d1", "drug", 48.0, 0.5, dose=3.0, dose_unit="nM")]
        rule = self._dose_rule(entry)
        rule["measurements"] = ["d1"]
        spec = compare.resolve(mapping([rule], conds), rows)
        name = spec["measurements"]["d1"]["dose_condition"]
        self.assertEqual(spec["dose_conditions"][name]["K"], {"fit": "k1"})
        cond = compare.condition_overrides(spec, name, {"k1": 1.0})
        self.assertEqual(cond["parameters"], {"kp": 1.5})
        self.assertEqual(cond["scales"], {})

    def test_missing_dose_factor_raises(self):
        rows = [row("d1", "drug", 48.0, 0.5, dose=2.0, dose_unit="mM")]
        rule = self._dose_rule({"form": "inhibitor_species", "species": "I"})
        rule["measurements"] = ["d1"]
        with self.assertRaisesRegex(ValueError, "mM"):
            compare.resolve(mapping([rule]), rows)
        m = mapping([rule])
        del m["dose"]
        with self.assertRaises((KeyError, ValueError)):
            compare.resolve(m, rows)

    def test_um_to_nm_factor_and_shared_dose_condition(self):
        rows = [row("d1", "drug", 48.0, 0.5, dose=2.0, dose_unit="uM"),
                row("d2", "drug", 48.0, 0.6, dose=2000.0, dose_unit="nM"),
                row("d3", "drug", 48.0, 0.7, dose=1.0, dose_unit="uM")]
        entry = {"form": "target_activity_scaling", "parameter": "kp", "K": 1000.0}
        rule = self._dose_rule(entry)
        rule["measurements"] = ["d1", "d2", "d3"]
        spec = compare.resolve(mapping([rule]), rows)
        ms = spec["measurements"]
        self.assertEqual((ms["d1"]["dose"], ms["d2"]["dose"], ms["d3"]["dose"]), (2000.0, 2000.0, 1000.0))
        self.assertEqual(ms["d1"]["dose_condition"], ms["d2"]["dose_condition"])
        self.assertNotEqual(ms["d1"]["dose_condition"], ms["d3"]["dose_condition"])
        self.assertEqual(len(spec["dose_conditions"]), 2)

    def test_nonpositive_fitted_k_gives_nan_not_error(self):
        rows = [row("d1", "drug", 48.0, 0.5, dose=2.0, dose_unit="nM")]
        rule = self._dose_rule({"form": "target_activity_scaling", "parameter": "kp", "K": {"fit": "k1"}})
        rule["measurements"] = ["d1"]
        spec = compare.resolve(mapping([rule]), rows)
        name = spec["measurements"]["d1"]["dose_condition"]
        for k in (0.0, -1.0):
            self.assertTrue(math.isnan(compare.condition_overrides(spec, name, {"k1": k})["scales"]["kp"]))

    def test_dose_rows_are_evaluated_at_viability_time(self):
        rows = [row("d1", "drug", 48.0, 0.5, dose=2.0, dose_unit="nM")]
        rule = self._dose_rule({"form": "inhibitor_species", "species": "I"})
        rule["measurements"] = ["d1"]
        spec = compare.resolve(mapping([rule]), rows)
        self.assertEqual(spec["measurements"]["d1"]["time"], 48.0)
        self.assertEqual(spec["times_model"][spec["measurements"]["d1"]["dose_condition"]], [0.0, 48.0])

    def test_endpoint_reference_uses_same_use_only(self):
        rows = [row("a", "drug", 60.0, 6.0), row("b", "ref", 60.0, 2.0),
                row("c", "drug", 60.0, 9.0, use="holdout"), row("e", "ref", 60.0, 3.0, use="holdout"),
                row("f", "drug", 60.0, 4.0, use="holdout", readout="r2")]
        m = mapping([{"rule_id": "ec", "type": "endpoint_contrast", "method": "ratio", "test": "drug",
                      "reference": "ref", "time": 60.0, "measurements": ["a", "b", "c", "e", "f"]}])
        m["readouts"].append({"readout": "r2", "observable": "obs2"})
        ms = compare.resolve(m, rows)["measurements"]
        self.assertEqual(ms["a"]["target"], 3.0)
        self.assertEqual(ms["c"]["target"], 3.0)
        self.assertTrue(math.isnan(ms["f"]["target"]))  # no reference of that readout and use: nan, no raise

    def test_inhibitor_species(self):
        rows = [row("d1", "drug", 48.0, 0.5, dose=2.0, dose_unit="nM")]
        rule = self._dose_rule({"form": "inhibitor_species", "species": "I"})
        rule["measurements"] = ["d1"]
        spec = compare.resolve(mapping([rule]), rows)
        name = spec["measurements"]["d1"]["dose_condition"]
        cond = compare.condition_overrides(spec, name, {})
        self.assertEqual(cond["initials"], {"I": 2.0})
        self.assertEqual(cond["scales"], {})

    def test_fit_bounds_log10(self):
        spec = compare.resolve(mapping([tc("absolute", ["m1"])]), [row("m1", "ctrl", 1.0, 1.0)])
        self.assertEqual(spec["fit"][0]["log10_lower"], -2.0)
        self.assertEqual(spec["fit"][0]["log10_upper"], 2.0)

    def test_objective_fit_only_and_penalty(self):
        rows = [row("m1", "ctrl", 1.0, 2.0), row("m2", "ctrl", 2.0, 5.0, use="holdout")]
        spec = compare.resolve(mapping([tc("absolute", ["m1", "m2"])]), rows)
        self.assertEqual(compare.objective(spec, {"m1": 3.0, "m2": 100.0}), 1.0)
        self.assertEqual(compare.objective(spec, {"m1": 3.0, "m2": 100.0}, use="holdout"), 95.0 ** 2)
        self.assertEqual(compare.objective(spec, {"m1": 3.0, "m2": math.nan}), 1.0)
        self.assertEqual(compare.objective(spec, {"m1": math.nan, "m2": 5.0}), compare.PENALTY)
        self.assertEqual(compare.objective(spec, {"m1": math.inf, "m2": 5.0}), compare.PENALTY)
        self.assertEqual(compare.objective(spec, {"m2": 5.0}), compare.PENALTY)
        self.assertEqual(compare.PENALTY, 1e12)

    def test_failed_solver_never_raises(self):
        spec = compare.resolve(mapping([tc("absolute", ["m1"])]), [row("m1", "ctrl", 1.0, 2.0)])
        for sims in ({}, {"ctrl": None}, {"ctrl": {"obs": {}}}):
            pred = compare.predict(spec, sims)
            self.assertEqual(compare.objective(spec, pred), compare.PENALTY)

    def test_imports_nothing_from_scripts(self):
        tree = ast.parse(SRC.read_text(encoding="utf-8"))
        mods = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                mods += [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                self.assertEqual(node.level, 0)
                mods.append(node.module)
        self.assertFalse([m for m in mods if m.split(".")[0] in ("scripts", "numpy")], mods)
        self.assertTrue(set(m.split(".")[0] for m in mods) <= set(__import__("sys").stdlib_module_names), mods)


if __name__ == "__main__":
    unittest.main()
