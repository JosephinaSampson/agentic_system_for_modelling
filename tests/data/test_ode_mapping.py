import copy
import hashlib
import io
import json
import shutil
import tempfile
import unittest
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from scripts import state
from scripts.data import approvals
from scripts.ode import measurements, model_zip, ode_mapping

SESSION = "ode-1"
BUNDLE = "runs/ode-modeler/ode-1/captures/c1/export.zip"
MEAS = "runs/orchestrator/p1/measurements.csv"
MAP = "runs/orchestrator/p1/ode_mapping.json"
SOURCE = "inputs/papers/s1.pdf"
HEADER = ",".join(measurements.COLUMNS)
ROWS = [
    "M1,pR,ligand,0,min,,,1.0,a.u.,0.1,sd,3,fit,s1,Fig. 1A",
    "M2,pR,ligand,30,min,,,3.0,a.u.,0.2,sd,3,fit,s1,Fig. 1A",
    "M3,pR,ctrl,30,min,,,1.0,a.u.,0.1,sem,3,holdout,s1,Fig. 1B",
    "M4,pR,ligand,30,min,,,2.8,a.u.,0.3,sem,3,holdout,s1,Fig. 1B",
    "M5,pR,ligand,30,min,0.1,uM,0.8,fraction,,,,fit,s1,Fig. 2",
    "M6,pR,ligand,30,min,1,uM,0.4,fraction,,,,holdout,s1,Fig. 2",
]
PARAMS = ["kf1", "kr1", "kf2", "kf3"]
SPECIES = ["E", "R", "ER", "pER"]

PARAMETERS_PY = ("from typing import List\n\nNAMES: List[str] = [\n"
                 + "".join(f"    '{n}',\n" for n in PARAMS) + "]\n\nfor idx, name in enumerate(NAMES):\n"
                 "    exec('{} = {:d}'.format(name, idx))\n")
SPECIES_PY = "from typing import List\n\nNAMES: List[str] = [\n" + "".join(f"    '{n}',\n" for n in SPECIES) + "]\n"
OBSERVABLE_PY = ("class Observable:\n    def __init__(self):\n        super().__init__()\n"
                 "        self.obs_names: list = [\n            'pR',\n        ]\n")


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def make_zip(path, parameters=PARAMETERS_PY, species=SPECIES_PY, observable=OBSERVABLE_PY,
             requirements="biomass==0.14.0\n"):
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("model/name2idx/parameters.py", parameters)
        zf.writestr("model/name2idx/species.py", species)
        zf.writestr("model/observable.py", observable)
        zf.writestr("requirements.txt", requirements)
        zf.writestr("run_simulation.py", "print('x')\n")


class OdeFixture(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        shutil.copyfile(state.ROOT / "templates" / "DECISIONS.md", self.root / "DECISIONS.md")
        make_zip(self.root / BUNDLE)
        self.write_rows(ROWS)
        src = self.root / SOURCE
        src.parent.mkdir(parents=True)
        src.write_bytes(b"%PDF-1.4 synthetic\n")

    def write_rows(self, rows):
        path = self.root / MEAS
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join([HEADER, *rows]) + "\n", encoding="utf-8")

    def load(self, rel=MAP):
        return json.loads((self.root / rel).read_text(encoding="utf-8"))

    def save(self, data, rel=MAP):
        (self.root / rel).write_text(json.dumps(data, indent=2), encoding="utf-8")

    def complete(self):
        """A draft turned into a complete, valid mapping (written to MAP)."""
        ode_mapping.draft(self.root, "p1", ode_session=SESSION, revision="r1", bundle_rel=BUNDLE,
                          measurements_rel=MEAS, sources=[("s1", SOURCE)])
        data = self.load()
        data["time"] = {"model_unit": "min", "factors": {"min": 1.0}}
        data["dose"] = {"model_unit": "nM", "factors": {"uM": 1000.0}}
        data["readouts"] = [{"readout": "pR", "observable": "pR"}]
        override = {"name": "kf1", "value": 2.0, "value_unit": "x", "model_unit": "x", "factor": 1.0}
        data["conditions"] = [
            {"condition": "ctrl", "parameters": [],
             "initials": [{"name": "E", "value": 2.0, "value_unit": "nM", "model_unit": "molecule", "factor": 6.0}]},
            {"condition": "ligand", "parameters": [override], "initials": []}]
        data["rules"] = [
            {"rule_id": "R1", "type": "time_course", "method": "fold_change_vs_t0", "measurements": ["M1", "M2"]},
            {"rule_id": "R2", "type": "endpoint_contrast", "method": "ratio", "test": "ligand", "reference": "ctrl",
             "time": 30.0, "measurements": ["M3", "M4"]},
            {"rule_id": "R3", "type": "dose_response", "measurements": ["M5", "M6"],
             "drug_entry": {"form": "target_activity_scaling", "parameter": "kf3", "K": {"fit": "kf1"}},
             "viability": {"observable": "pR", "untreated": "ctrl", "time": 30.0}}]
        data["fit"] = [
            {"name": "kf1", "kind": "parameter", "lower": 0.001, "upper": 10.0, "unit": "1/min",
             "justification": {"source": "s1", "locator": "Table 1"}},
            {"name": "kf2", "kind": "parameter", "lower": 0.01, "upper": 1.0, "unit": "1/min",
             "justification": {"text": "prior range from the literature"}}]
        data["unmapped"] = []
        self.save(data)
        return data

    def errors(self, data=None):
        if data is not None:
            self.save(data)
        return ode_mapping.check(self.root, MAP)

    def assertError(self, errors, *needles):
        self.assertTrue(any(all(n in e for n in needles) for e in errors), (needles, errors))


class IntrospectTests(OdeFixture):
    def test_introspect_reads_names_without_import(self):
        marker = self.root / "imported"
        evil = PARAMETERS_PY + f"\nopen({str(marker)!r}, 'w').write('x')\n"
        path = self.root / "evil.zip"
        make_zip(path, parameters=evil)
        self.assertEqual(model_zip.introspect(path), {"parameters": PARAMS, "species": SPECIES, "observables": ["pR"]})
        self.assertEqual(model_zip.introspect(self.root / BUNDLE)["parameters"], PARAMS)
        self.assertFalse(marker.exists())

    def test_introspect_refuses_wrong_requirements_and_missing_names(self):
        path = self.root / "bad.zip"
        make_zip(path, requirements="biomass==0.13.0\n")
        with self.assertRaisesRegex(ValueError, "requirements"):
            model_zip.introspect(path)
        make_zip(path, species="X = 1\n")
        with self.assertRaisesRegex(ValueError, "NAMES"):
            model_zip.introspect(path)
        make_zip(path, observable="class O:\n    pass\n")
        with self.assertRaisesRegex(ValueError, "obs_names"):
            model_zip.introspect(path)


class DraftTests(OdeFixture):
    def test_draft_refuses_overwrite_and_lists_todo(self):
        path = ode_mapping.draft(self.root, "p1", ode_session=SESSION, revision="r1", bundle_rel=BUNDLE,
                                 measurements_rel=MEAS, sources=[("s1", SOURCE)])
        self.assertEqual(path.relative_to(self.root).as_posix(), MAP)
        data = self.load()
        self.assertEqual(tuple(data), ode_mapping.KEYS)
        self.assertEqual((data["kind"], data["schema_version"]), ("ode_mapping", 1))
        self.assertEqual(data["inputs"]["model"], {"ode_session": SESSION, "revision": "r1", "bundle": BUNDLE,
                                                   "sha256": sha(self.root / BUNDLE)})
        self.assertEqual(data["inputs"]["measurements"], {"path": MEAS, "sha256": sha(self.root / MEAS)})
        self.assertEqual([u["id"] for u in data["inputs"]["sources"]], ["s1"])
        self.assertEqual(data["readouts"], [])
        self.assertEqual(data["rules"], [])
        self.assertEqual(data["fit"], [])
        self.assertEqual(data["unmapped"], [{"measurement_id": f"M{n}", "reason": "TODO"} for n in range(1, 7)])
        self.assertEqual([c["condition"] for c in data["conditions"]], ["ligand", "ctrl"])
        with self.assertRaisesRegex(ValueError, "exists"):
            ode_mapping.draft(self.root, "p1", ode_session=SESSION, revision="r1", bundle_rel=BUNDLE,
                              measurements_rel=MEAS, sources=[])
        errors = ode_mapping.check(self.root, MAP)
        self.assertError(errors, "TODO")

    def test_draft_refuses_bundle_outside_session(self):
        other = "runs/ode-modeler/ode-2/captures/c/export.zip"
        make_zip(self.root / other)
        with self.assertRaisesRegex(ValueError, "runs/ode-modeler/ode-1"):
            ode_mapping.draft(self.root, "p1", ode_session=SESSION, revision="r1", bundle_rel=other,
                              measurements_rel=MEAS, sources=[])

    def test_cli_draft_and_check(self):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict("os.environ", {"CLAUDE_PROJECT_DIR": str(self.root)}), \
                redirect_stdout(out), redirect_stderr(err):
            code = ode_mapping.main(["draft", "--purpose", "p1", "--ode-session", SESSION, "--revision", "r1",
                                     "--bundle", BUNDLE, "--measurements", MEAS, "--source", f"s1:{SOURCE}"])
            self.assertEqual(code, 0, err.getvalue())
            self.assertEqual(ode_mapping.main(["check", MAP]), 1)
        self.assertIn("TODO", err.getvalue())
        (self.root / MAP).unlink()
        self.complete()
        out = io.StringIO()
        with mock.patch.dict("os.environ", {"CLAUDE_PROJECT_DIR": str(self.root)}), redirect_stdout(out):
            self.assertEqual(ode_mapping.main(["check", MAP]), 0)
        self.assertIn("passed", out.getvalue())


class CheckTests(OdeFixture):
    def setUp(self):
        super().setUp()
        self.data = self.complete()

    def mutated(self, fn):
        data = copy.deepcopy(self.data)
        fn(data)
        return self.errors(data)

    def test_check_passes_complete_mapping(self):
        self.assertEqual(ode_mapping.check(self.root, MAP), [])
        data, digest, errors = ode_mapping.load_and_check(self.root, MAP)
        self.assertEqual((errors, digest), ([], sha(self.root / MAP)))
        self.assertEqual(data["kind"], "ode_mapping")

    def test_missing_time_factor_refused(self):
        self.write_rows([ROWS[0].replace(",min,", ",h,"), *ROWS[1:]])
        data = copy.deepcopy(self.data)
        data["inputs"]["measurements"]["sha256"] = sha(self.root / MEAS)
        self.assertError(self.errors(data), '"h"')

    def test_time_model_unit_needs_factor_one(self):
        self.assertError(self.mutated(lambda d: d["time"]["factors"].update(min=2.0)), "model_unit", "1")
        self.assertError(self.mutated(lambda d: d["time"]["factors"].update(h=0)), "factor")

    def test_missing_unit_refused(self):
        self.write_rows([ROWS[0].replace("a.u.", ""), *ROWS[1:]])
        data = copy.deepcopy(self.data)
        data["inputs"]["measurements"]["sha256"] = sha(self.root / MEAS)
        self.assertError(self.errors(data), "value_unit")

    def test_override_units_and_factor(self):
        def bad(d):
            o = d["conditions"][1]["parameters"][0]
            o["value_unit"], o["model_unit"], o["factor"] = "", "", -1
        errors = self.mutated(bad)
        self.assertError(errors, "value_unit")
        self.assertError(errors, "model_unit")
        self.assertError(errors, "factor")

    def test_unknown_observable_parameter_species_refused(self):
        def bad(d):
            d["readouts"][0]["observable"] = "pX"
            d["conditions"][1]["parameters"][0]["name"] = "kz"
            d["conditions"][0]["initials"][0]["name"] = "Z"
            d["fit"][0]["name"] = "Q"
        errors = self.mutated(bad)
        self.assertError(errors, "pX")
        self.assertError(errors, "kz")
        self.assertError(errors, "Z")
        self.assertError(errors, "Q")

    def test_species_name_in_parameter_override_refused(self):
        self.assertError(self.mutated(lambda d: d["conditions"][1]["parameters"][0].update(name="E")), "E")

    def test_bounds_positive_and_ordered(self):
        self.assertError(self.mutated(lambda d: d["fit"][0].update(lower=0)), "lower", "positive")
        self.assertError(self.mutated(lambda d: d["fit"][0].update(lower=10.0, upper=1.0)), "lower", "upper")
        self.assertError(self.mutated(lambda d: d["fit"][0].update(lower=1.0, upper=1.0)), "lower", "upper")

    def test_fit_justification_and_unit(self):
        self.assertError(self.mutated(lambda d: d["fit"][0].update(justification={"text": " "})), "justification")
        self.assertError(self.mutated(lambda d: d["fit"][0].update(justification={"source": "zz", "locator": "x"})),
                         "zz")
        self.assertError(self.mutated(lambda d: d["fit"][0].update(unit="")), "unit")

    def test_coverage_exactly_once(self):
        def twice(d):
            d["rules"][0]["measurements"].append("M3")
        self.assertError(self.mutated(twice), "M3", "once")

        def none(d):
            d["rules"][2]["measurements"] = ["M5"]
        self.assertError(self.mutated(none), "M6", "neither")

        def todo(d):
            d["rules"][2]["measurements"] = ["M5"]
            d["unmapped"] = [{"measurement_id": "M6", "reason": "TODO"}]
        self.assertError(self.mutated(todo), "TODO")

        def fine(d):
            d["rules"][2]["measurements"] = ["M5"]
            d["unmapped"] = [{"measurement_id": "M6", "reason": "holdout only, no model arm"}]
        self.assertEqual(self.mutated(fine), [])

        def unknown(d):
            d["unmapped"] = [{"measurement_id": "M99", "reason": "no such row"}]
        self.assertError(self.mutated(unknown), "M99")

    def test_rule_parameters_validated(self):
        def noref(d):
            d["rules"][0] = {"rule_id": "R1", "type": "time_course", "method": "relative_to_condition",
                             "measurements": ["M1", "M2"]}
        self.assertError(self.mutated(noref), "reference")

        def unknown_test(d):
            d["rules"][1]["test"] = "nope"
        self.assertError(self.mutated(unknown_test), "nope")

        def nondose(d):
            d["rules"][2]["measurements"] = ["M5", "M4"]
            d["rules"][1]["measurements"] = ["M3", "M6"]
        self.assertError(self.mutated(nondose), "M4", "dose")

        def dose_elsewhere(d):
            d["rules"][0]["measurements"] = ["M1", "M2", "M5"]
            d["rules"][2]["measurements"] = ["M6"]
        self.assertError(self.mutated(dose_elsewhere), "M5", "dose")

        self.assertError(self.mutated(lambda d: d["rules"][0].update(method="bogus")), "method")
        self.assertError(self.mutated(lambda d: d["rules"][1].update(extra=1)), "keys")
        self.assertError(self.mutated(lambda d: d["rules"][1].update(method="ratio", test="ctrl")), "differ")
        self.assertError(self.mutated(lambda d: d["rules"][2]["drug_entry"].update(parameter="kz")), "kz")
        self.assertError(self.mutated(lambda d: d["rules"][2]["drug_entry"].update(K={"fit": "kr1"})), "kr1")
        self.assertError(self.mutated(lambda d: d["rules"][2]["drug_entry"].update(K=-1)), "K")
        self.assertError(self.mutated(lambda d: d["rules"][2]["viability"].update(observable="pX")), "pX")
        self.assertError(self.mutated(lambda d: d["rules"][2].update(
            drug_entry={"form": "inhibitor_species", "species": "Z"})), "Z")
        self.assertEqual(self.mutated(lambda d: d["rules"][2].update(
            drug_entry={"form": "inhibitor_species", "species": "ER"})), [])

    def test_dose_factors_required_for_dosed_rows(self):
        self.assertError(self.mutated(lambda d: d["dose"].update(factors={})), "M5", "dose_unit")
        self.assertError(self.mutated(lambda d: d["dose"]["factors"].update(uM=0)), "dose.factors")
        self.assertError(self.mutated(lambda d: d["dose"].update(model_unit="TODO")), "dose.model_unit")
        self.assertError(self.mutated(lambda d: d.pop("dose")), "keys")

    def rewrite(self, d, rows):
        self.write_rows(rows)
        d["inputs"]["measurements"]["sha256"] = sha(self.root / MEAS)

    def test_empty_dose_block_is_fine_without_dosed_rows(self):
        def drop(d):
            self.rewrite(d, [r for r in ROWS if "uM" not in r])
            d["dose"] = {"model_unit": "", "factors": {}}
            d["rules"].pop()
        self.assertEqual(self.mutated(drop), [])
        self.assertError(self.mutated(lambda d: d.update(dose={"model_unit": 3, "factors": {}})), "dose.model_unit")

    def test_endpoint_reference_must_match_use(self):
        def leak(d):
            self.rewrite(d, [r.replace("holdout", "fit") if r.startswith("M3") else r for r in ROWS])
        self.assertError(self.mutated(leak), "M4", "same readout and use")

    def test_dose_row_time_must_equal_viability_time(self):
        self.assertError(self.mutated(lambda d: d["rules"][2]["viability"].update(time=60.0)), "viability.time")

    def test_dose_row_time_compared_with_float_tolerance(self):
        # 0.03 h * 60 = 1.7999999999999998 must count as 1.8 min
        def hours(d):
            self.rewrite(d, [r.replace(",30,min,0", ",0.03,h,0").replace(",30,min,1", ",0.03,h,1")
                             if r[:2] in ("M5", "M6") else r for r in ROWS])
            d["time"]["factors"]["h"] = 60.0
            d["rules"][2]["viability"]["time"] = 1.8
        self.assertEqual(self.mutated(hours), [])

    def test_contrast_row_time_must_equal_rule_time(self):
        # a rule time meant in hours against a minute model, and a row at another time, are refused
        errors = self.mutated(lambda d: d["rules"][1].update(time=24))
        self.assertError(errors, "M3", "rule time")
        self.assertError(errors, "M4", "rule time")

        def other_time(d):
            self.rewrite(d, [r.replace(",30,min,", ",60,min,") if r.startswith("M4") else r for r in ROWS])
        errors = self.mutated(other_time)
        self.assertError(errors, "M4", "rule time")
        self.assertFalse(any("M3" in e for e in errors), errors)

        def hours(d):  # 0.03 h is 1.8 min: accepted (float tolerance)
            self.rewrite(d, [r.replace(",30,min,", ",0.03,h,") if r[:2] in ("M3", "M4") else r for r in ROWS])
            d["time"]["factors"]["h"] = 60.0
            d["rules"][1]["time"] = 1.8
        self.assertEqual(self.mutated(hours), [])

    def test_dose_response_readout_must_map_to_viability_observable(self):
        two = OBSERVABLE_PY.replace("'pR',\n", "'pR',\n            'pS',\n")
        make_zip(self.root / BUNDLE, observable=two)

        def viability_ps(d):
            d["inputs"]["model"]["sha256"] = sha(self.root / BUNDLE)
            d["rules"][2]["viability"]["observable"] = "pS"
        self.assertError(self.mutated(viability_ps), "M5", "viability observable")

        def mapped(d):  # pR readout mapped to pS: consistent again
            d["inputs"]["model"]["sha256"] = sha(self.root / BUNDLE)
            d["readouts"] = [{"readout": "pR", "observable": "pS"}]
            d["rules"][2]["viability"]["observable"] = "pS"
        self.assertEqual(self.mutated(mapped), [])

    def test_at_least_one_fit_row_must_be_scored(self):
        def holdout_only(d):
            d["rules"] = [d["rules"][1]]  # the contrast of hold-out rows only
            d["unmapped"] = [{"measurement_id": m, "reason": "not used in this calibration"}
                             for m in ("M1", "M2", "M5", "M6")]
        self.assertError(self.mutated(holdout_only), "use=fit")

        def fit_reference_only(d):  # a use=fit row that is only a contrast reference is not scored
            self.rewrite(d, [r.replace("holdout", "fit") if r.startswith("M3") else r for r in ROWS])
            d["rules"] = [{"rule_id": "R2", "type": "endpoint_contrast", "method": "ratio", "test": "ligand",
                           "reference": "ctrl", "time": 30.0, "measurements": ["M3"]}]
            d["unmapped"] = [{"measurement_id": m, "reason": "not used in this calibration"}
                             for m in ("M1", "M2", "M4", "M5", "M6")]
        self.assertError(self.mutated(fit_reference_only), "use=fit")

    def test_rule_type_is_not_a_method_name(self):
        for kind in ("rows", "keys", "condition", "forbid_dose"):
            errors = self.mutated(lambda d, k=kind: d["rules"][0].update(type=k))
            self.assertError(errors, "type must be one of")

    def test_rule_without_measurements_is_an_error_not_an_exception(self):
        for n in range(3):
            data = copy.deepcopy(self.data)
            del data["rules"][n]["measurements"]
            self.assertError(self.errors(data), "keys")
        data = copy.deepcopy(self.data)
        data["rules"][0] = {"rule_id": "R1", "type": "rows"}
        self.assertTrue(self.errors(data))
        self.assertError(self.mutated(lambda d: d["rules"][2]["drug_entry"].update(K={"fit": ["kf1"]})), "K")

    def test_k_fit_must_name_a_parameter_fit(self):
        def initial(d):
            d["fit"].append({"name": "E", "kind": "initial", "lower": 1.0, "upper": 5.0, "unit": "nM",
                             "justification": {"text": "range"}})
            d["rules"][2]["drug_entry"]["K"] = {"fit": "E"}
        self.assertError(self.mutated(initial), "kind parameter")

    def test_relative_to_condition_reference_must_be_a_condition(self):
        def ok(d):
            d["rules"][0] = {"rule_id": "R1", "type": "time_course", "method": "relative_to_condition",
                             "reference": "ctrl", "measurements": ["M1", "M2"]}
        self.assertEqual(self.mutated(ok), [])

        def bad(d):
            ok(d)
            d["rules"][0]["reference"] = "ghost"
        self.assertError(self.mutated(bad), "ghost")

    def test_bundle_path_must_be_under_ode_session(self):
        other = "runs/ode-modeler/ode-2/captures/c/export.zip"
        make_zip(self.root / other)
        self.assertError(self.mutated(lambda d: d["inputs"]["model"].update(
            bundle=other, sha256=sha(self.root / other))), "runs/ode-modeler/ode-1")
        self.assertError(self.mutated(lambda d: d["inputs"]["model"].update(bundle="runs/ode-modeler/ode-1/x.txt")),
                         "zip")

    def test_bundle_hash_mismatch_refused(self):
        self.assertError(self.mutated(lambda d: d["inputs"]["model"].update(sha256="0" * 64)), "sha256")

    def test_measurements_and_source_hash_mismatch_refused(self):
        self.assertError(self.mutated(lambda d: d["inputs"]["measurements"].update(sha256="0" * 64)), "sha256")
        (self.root / SOURCE).write_bytes(b"changed")
        self.assertError(self.errors(), "sha256")

    def test_row_source_must_be_declared_and_declared_must_be_cited(self):
        self.write_rows([r.replace(",s1,", ",s9,") for r in ROWS])
        data = copy.deepcopy(self.data)
        data["inputs"]["measurements"]["sha256"] = sha(self.root / MEAS)
        data["fit"][0]["justification"] = {"text": "prior range"}
        errors = self.errors(data)
        self.assertError(errors, "s9")
        self.assertError(errors, "never cited")

    def test_row_must_name_declared_readout_and_condition(self):
        self.assertError(self.mutated(lambda d: d.update(readouts=[])), "pR")
        self.assertError(self.mutated(lambda d: d.update(conditions=d["conditions"][:1])), "ligand")

    def test_shape_errors_do_not_crash(self):
        for key in ode_mapping.KEYS[2:]:
            self.assertTrue(self.mutated(lambda d, k=key: d.update({k: 5})), key)
        self.assertTrue(self.mutated(lambda d: d.pop("fit")))
        self.assertTrue(self.mutated(lambda d: d.update(kind="mapping")))

    def test_symlinked_mapping_refused(self):
        real = self.root / "real.json"
        shutil.move(self.root / MAP, real)
        (self.root / MAP).symlink_to(real)
        with self.assertRaises(ValueError):
            ode_mapping.check(self.root, MAP)


class ApprovalTests(OdeFixture):
    def add(self, record):
        record = {"approved_by": "researcher", "authorization": "researcher: ok", **record}
        state.update(self.root, "record", record)

    def test_approval_outputs_equal_measurements_plus_fit(self):
        self.complete()
        digest = sha(self.root / MAP)
        ref = {"path": MAP, "sha256": digest}
        good = ["M1", "M2", "M3", "M4", "M5", "M6", "kf1", "kf2"]
        self.add({"id": "bad-1", "kind": "stage_approval", "stage": "ode_mapping", "session_id": SESSION,
                  "outputs": ["M1", "kf1"], "artifact": ref})
        with self.assertRaisesRegex(ValueError, "outputs"):
            approvals.require_ode_mapping_approval(self.root, "bad-1", MAP, digest)
        self.add({"id": "ok-1", "kind": "stage_approval", "stage": "ode_mapping", "session_id": SESSION,
                  "outputs": good, "artifact": ref})
        self.assertEqual(approvals.require_ode_mapping_approval(self.root, "ok-1", MAP, digest)["id"], "ok-1")
        with self.assertRaisesRegex(ValueError, "sha256"):
            approvals.require_ode_mapping_approval(self.root, "ok-1", MAP, "0" * 64)
        self.add({"id": "ses-1", "kind": "stage_approval", "stage": "ode_mapping", "session_id": "ode-9",
                  "outputs": good, "artifact": ref})
        with self.assertRaisesRegex(ValueError, "session"):
            approvals.require_ode_mapping_approval(self.root, "ses-1", MAP, digest)
        self.add({"id": "stg-1", "kind": "stage_approval", "stage": "data_mapping", "session_id": SESSION,
                  "outputs": good, "artifact": ref})
        with self.assertRaisesRegex(ValueError, "ode_mapping"):
            approvals.require_ode_mapping_approval(self.root, "stg-1", MAP, digest)

    def test_calibration_approval_binds_artifact(self):
        (self.root / "job.json").write_text("{}")
        digest = sha(self.root / "job.json")
        self.add({"id": "cal-1", "kind": "stage_approval", "stage": "ode_calibration", "session_id": SESSION,
                  "outputs": ["job"], "artifact": {"path": "job.json", "sha256": digest}})
        self.assertEqual(approvals.require_calibration_approval(self.root, "cal-1", "job.json", digest)["id"], "cal-1")
        with self.assertRaisesRegex(ValueError, "sha256"):
            approvals.require_calibration_approval(self.root, "cal-1", "job.json", "0" * 64)


class DataAnalystRouteTests(OdeFixture):
    SID = "data-20260101T000000Z-abcdef12"

    def setUp(self):
        super().setUp()
        self.base = f"runs/data-analyst/{self.SID}"
        folder = self.root / self.base
        (folder / "proposals").mkdir(parents=True)
        rows = [r.replace(",s1,", ",proposal,") for r in ROWS]
        (folder / "proposals/P1_ode_measurements.csv").write_text("\n".join([HEADER, *rows]) + "\n")
        (folder / "proposals/index.json").write_text(json.dumps([
            {"id": "P1", "kind": "ode_measurements", "target_layer": "biomass",
             "path": "proposals/P1_ode_measurements.csv"}]))
        rels = ["proposals/index.json", "proposals/P1_ode_measurements.csv"]
        arts = [{"path": f"{self.base}/{r}", "sha256": sha(folder / r)} for r in rels]
        (folder / "handoff.json").write_text(json.dumps({"session_id": self.SID, "artifacts": arts}))
        state.update(self.root, "record", {
            "approved_by": "researcher", "authorization": "researcher: ok", "id": "dec-1", "kind": "stage_approval",
            "stage": "data", "session_id": self.SID, "outputs": ["P1"],
            "artifact": {"path": f"{self.base}/handoff.json", "sha256": sha(folder / "handoff.json")}})

    def build(self):
        data = self.complete()
        data["inputs"]["measurements"] = {"session": self.SID, "proposal": "P1", "approval": "dec-1"}
        data["inputs"]["sources"] = []
        data["fit"][0]["justification"] = {"text": "prior range"}
        return data

    def test_data_analyst_route_accepts_proposal_source(self):
        self.assertEqual(self.errors(self.build()), [])

    def test_proposal_source_refused_on_file_route(self):
        self.complete()
        self.write_rows([r.replace(",s1,", ",proposal,") for r in ROWS])
        data = self.load()
        data["inputs"]["measurements"]["sha256"] = sha(self.root / MEAS)
        self.assertError(self.errors(data), "proposal")

    def test_data_analyst_route_needs_approved_ode_measurements(self):
        data = self.build()
        data["inputs"]["measurements"]["proposal"] = "P7"
        self.assertError(self.errors(data), "P7")
        data["inputs"]["measurements"].update(proposal="P1", approval="nope")
        self.assertError(self.errors(data), "inputs.measurements")


if __name__ == "__main__":
    unittest.main()
