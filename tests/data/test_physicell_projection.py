from __future__ import annotations

import contextlib
import copy
import hashlib
import io
import json
import math
import os
import unittest
from unittest import mock

import xml.etree.ElementTree as ET

from scripts import state
from scripts.capture.record_physicell_config import record_physicell
from scripts.data import approvals
from scripts.physicell import capture_dictionary as cd
from scripts.physicell import physicell_projection as pp
from tests.data.test_physicell_design import FIXTURES, MABOSS, REL, SESSION, Base
from tests.data.test_physicell_dictionary import INVOCATION, good_calls

PROJ = "runs/orchestrator/p1/physicell_projection.json"
DICT = "runs/orchestrator/p1/physicell_dictionary.json"
TUMOR = "cell_definitions/cell_definition[@name='tumor']/phenotype"
APOPTOSIS = f"{TUMOR}/death/model[@name='apoptosis']/death_rate"
RATE0 = f"{TUMOR}/cycle/phase_transition_rates/rate[@start_index='0']"
SRC = {"source": "s1", "locator": "Table 1"}


def q(value, unit, transform="identity", factor=1, **extra):
    return {"value": value, "value_unit": unit, "transform": transform, "factor": factor, **extra}


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def built_capture(root, session, capture_id):
    """Record a capture of the fixture configuration whose tumor carries the PhysiBoSS fixture fragment (the
    configuration the configurator built from the design): captured input link pressure -> B, output A -> apoptosis."""
    source = root.parent / f"source-{session}-{capture_id}"
    (source / "config").mkdir(parents=True)
    tree = ET.parse(FIXTURES / "PhysiCell_settings.xml")
    tumor = tree.getroot().find("cell_definitions/cell_definition[@name='tumor']/phenotype")
    tumor.append(ET.parse(FIXTURES / "intracellular_tumor.xml").getroot())
    tree.write(source / "config/PhysiCell_settings.xml", encoding="utf-8", xml_declaration=True)
    (source / "config/cell_rules.csv").write_bytes((FIXTURES / "cell_rules.csv").read_bytes())
    (source / "config/tumor.bnd").write_text("Node A { logic = A; }\nNode B { logic = B; }\nNode C { logic = C; }\n")
    (source / "config/tumor.cfg").write_text("A.istate=0;")
    record_physicell(root, source, source / "config/PhysiCell_settings.xml", session, capture_id)
    return f"runs/multicellular-configurator/{session}/configs/{capture_id}"


class ProjectionBase(Base):
    """A checked, approved design, its captured dictionary and a drafted projection over the built capture.

    The tumor's MaBoSS handoff has nodes A, B, C and declared outputs A, B only."""

    def make_handoff(self, session=MABOSS, nodes=("A", "B", "C"), outputs=("A", "B"), **kw):
        return super().make_handoff(session, nodes=nodes, outputs=outputs, **kw)

    def setUp(self):
        super().setUp()
        self.design = self.complete()  # its basis is the fixture capture cap-1
        self.basis = self.capture
        self.capture = built_capture(self.root, SESSION, "cap-2")
        self.write(self.design)
        self.design_sha = sha(self.root / REL)
        state.update(self.root, "record", {
            "id": "design-1", "kind": "stage_approval", "session_id": SESSION, "stage": "physicell_design",
            "outputs": ["default", "tumor", "tumor_hypoxic", "oxygen", "drug"], "approved_by": "researcher",
            "authorization": "approved", "artifact": {"path": REL, "sha256": self.design_sha}})
        invocation = self.root / INVOCATION
        invocation.parent.mkdir(parents=True, exist_ok=True)
        invocation.write_text(json.dumps(good_calls(extra_behaviors=("necrosis", "apoptosis"))))
        cd.capture_dictionary(self.root, INVOCATION, self.capture, REL)
        path = pp.draft(self.root, "p1", capture=self.capture, design=REL, design_approval="design-1",
                        sources=[("s1", "inputs/paper.txt")])
        self.skeleton = json.loads(path.read_text())

    def entries(self):
        common = {"from": SRC, "justification": "reported in the source"}
        return [
            {"entry_id": "in1", "kind": "physiboss_input", **common, "cell_definition": "tumor", "signal": "oxygen",
             "node": "A", "action": "activation", "threshold": q(10, "mmHg"), "smoothing": 0},
            {"entry_id": "out1", "kind": "physiboss_output", **common, "cell_definition": "tumor", "node": "B",
             "behavior": "migration speed", "action": "activation", "value": q(0.5, "micron/min"),
             "base_value": q(0, "micron/min"), "smoothing": 0, "steepness": 10},
            {"entry_id": "rule1", "kind": "rule", **common, "cell_definition": "tumor", "signal": "pressure",
             "direction": "decreases", "behavior": "exit from cycle phase 1", "saturation_value": q(0, "1/min"),
             "half_max": q(1, "dimensionless"), "hill_power": q(4, "dimensionless")},
            {"entry_id": "set1", "kind": "setting", **common, "path": APOPTOSIS, "value": 2e-5, "value_unit": "1/min",
             "transform": "identity", "factor": 1, "tool_hint": "configure_cell_parameters"},
        ]

    def projection(self, entries=None):
        data = copy.deepcopy(self.skeleton)
        data["entries"] = self.entries() if entries is None else entries
        return data

    def write_projection(self, data):
        path = self.root / PROJ
        path.write_text(json.dumps(data, indent=2))
        return path

    def errors(self, data):
        self.write_projection(data)
        return pp.check(self.root, PROJ)

    def assertRefused(self, data, *patterns):
        errors = self.errors(data)
        for pattern in patterns:
            self.assertTrue(any(pattern in e for e in errors), (pattern, errors))

    def entry(self, entry_id):
        data = self.projection()
        return data, next(e for e in data["entries"] if e["entry_id"] == entry_id)


class DraftTests(ProjectionBase):
    def test_draft_binds_capture_dictionary_and_design(self):
        data = self.skeleton
        self.assertEqual(data["kind"], "physicell_projection")
        self.assertEqual(data["schema_version"], 1)
        manifest = self.root / self.capture / "capture.json"
        self.assertEqual(data["capture"], {"path": self.capture, "sha256": sha(manifest)})
        self.assertEqual(data["dictionary"], {"path": DICT, "sha256": sha(self.root / DICT)})
        self.assertEqual(data["design"], {"path": REL, "sha256": self.design_sha, "approval": "design-1"})
        self.assertEqual([s["id"] for s in data["sources"]], ["s1"])
        self.assertEqual((data["data"], data["entries"], data["unmapped"]), ([], [], []))
        with self.assertRaisesRegex(ValueError, "never overwrite"):
            pp.draft(self.root, "p1", capture=self.capture, design=REL, design_approval="design-1")

    def test_draft_refuses_unapproved_design(self):
        with self.assertRaisesRegex(ValueError, "decision_id not found"):
            pp.draft(self.root, "p2", capture=self.capture, design=REL, design_approval="nope", dictionary=DICT)


class HappyPathTests(ProjectionBase):
    def test_all_kinds_pass(self):
        self.assertEqual(self.errors(self.projection()), [])

    def test_each_kind_alone_passes(self):
        for entry in self.entries():
            with self.subTest(kind=entry["kind"]):
                self.assertEqual(self.errors(self.projection([entry])), [])

    def test_load_and_check_returns_hash_of_bytes_read(self):
        path = self.write_projection(self.projection())
        data, digest, errors = pp.load_and_check(self.root, PROJ)
        self.assertEqual((errors, digest, data["kind"]), ([], sha(path), "physicell_projection"))

    def test_cli_check(self):
        self.write_projection(self.projection())
        with mock.patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": str(self.root)}):
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(pp.main(["check", PROJ]), 0)
        self.assertIn("check passed", out.getvalue())


class NameTests(ProjectionBase):
    def test_unknown_signal_lists_close_matches(self):
        data, entry = self.entry("in1")
        entry["signal"] = "oxigen"
        self.assertRefused(data, "unknown signal 'oxigen'", "oxygen")

    def test_unknown_behavior_lists_close_matches(self):
        data, entry = self.entry("rule1")
        entry["behavior"] = "migration sped"
        self.assertRefused(data, "unknown behavior 'migration sped'", "migration speed")

    def test_unknown_node_refused(self):
        data, entry = self.entry("out1")
        entry["node"] = "C"
        self.assertRefused(data, "MaBoSS output node or readout 'C' of tumor")
        data, entry = self.entry("in1")
        entry["node"] = "AA"
        self.assertRefused(data, "MaBoSS node 'AA' of tumor (closest: A")

    def test_unknown_cell_definition_refused(self):
        data, entry = self.entry("rule1")
        entry["cell_definition"] = "tumour"
        self.assertRefused(data, "cell definition 'tumour'", "tumor")

    def test_physiboss_on_cell_without_maboss_refused(self):
        data, entry = self.entry("in1")
        entry["cell_definition"] = "default"
        self.assertRefused(data, "no MaBoSS model")


class SettingTests(ProjectionBase):
    def setting(self, **changes):
        data, entry = self.entry("set1")
        entry.update(changes)
        for key in [k for k, v in changes.items() if v is None]:
            entry.pop(key)
        return data

    def test_path_matching_two_elements_refused(self):
        self.assertRefused(self.setting(path=f"{TUMOR}/cycle/phase_transition_rates/rate"), "matches 2 elements")

    def test_unit_spelling_is_exact(self):
        self.assertRefused(self.setting(target_unit="min^-1"), "min^-1", "1/min")

    def test_value_unit_must_match_without_target(self):
        self.assertRefused(self.setting(value_unit="1/h"), "1/h")

    def test_ln2_over_doubling_time_in_hours_gives_per_minute(self):
        data = self.setting(path=RATE0, value=24, value_unit="h", transform="ln2_over", factor=1 / 60,
                            target_unit="1/min",
                            target_unit_justification="phase_transition_rates declares units 1/min")
        self.assertEqual(self.errors(data), [])
        entry = data["entries"][-1]
        self.assertAlmostEqual(pp.model_value(entry), math.log(2) / 24 / 60, places=15)

    def test_ln2_over_with_factor_needs_target_unit(self):
        data = self.setting(path=APOPTOSIS, value=24, value_unit="h", transform="ln2_over", factor=1 / 60)
        self.assertRefused(data, "target_unit")

    def test_missing_units_attribute_needs_justification(self):
        data = self.setting(path=RATE0, value=0.001, target_unit="1/min")
        self.assertRefused(data, "justification")
        data = self.setting(path=RATE0, value=0.001)
        self.assertRefused(data, "target_unit")

    def test_missing_units_attribute_target_must_agree_with_parent_units(self):
        data = self.setting(path=RATE0, value=0.001, value_unit="1/h", target_unit="1/h",
                            target_unit_justification="because")
        self.assertRefused(data, "phase_transition_rates")

    def test_same_element_twice_refused_across_spellings(self):
        data = self.projection()
        twin = copy.deepcopy(data["entries"][-1])
        twin.update(entry_id="set2", path=f"{TUMOR}/death/model[@code='100']/death_rate")
        data["entries"].append(twin)
        self.assertRefused(data, "set twice")

    def test_non_leaf_and_bad_value_refused(self):
        self.assertRefused(self.setting(path=f"{TUMOR}/death/model[@name='apoptosis']"), "leaf")
        self.assertRefused(self.setting(value="0.1"), "finite number")

    def test_tool_hint_required(self):
        self.assertRefused(self.setting(tool_hint=""), "tool_hint")


class TransformTests(unittest.TestCase):
    def test_model_values(self):
        self.assertAlmostEqual(pp.model_value(q(2, "h", factor=60)), 120)
        self.assertAlmostEqual(pp.model_value(q(2, "h", "unit_factor", 60)), 120)
        self.assertAlmostEqual(pp.model_value(q(4, "min", "reciprocal")), 0.25)
        self.assertAlmostEqual(pp.model_value(q(10, "min", "ln2_over")), math.log(2) / 10)

    def test_invalid_quantities_refused(self):
        for bad in (q(0, "min", "reciprocal"), q(0, "min", "ln2_over"), q(1, "min", factor=0),
                    q(1, "min", factor=-2), q(float("inf"), "min"), q(1, "min", "log"), q(True, "min")):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                pp.model_value(bad)

    def test_converted_units(self):
        self.assertEqual(pp.converted_unit(q(1, "min")), "min")
        self.assertEqual(pp.converted_unit(q(1, "min", "reciprocal")), "1/min")
        self.assertEqual(pp.converted_unit(q(1, "1/min", "ln2_over")), "min")
        self.assertEqual(pp.converted_unit(q(1, "h", factor=60, target_unit="min")), "min")
        for bad in (q(1, "h", factor=60), q(1, "h", "unit_factor"), q(1, "micron/min", "reciprocal")):
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, "target_unit"):
                pp.converted_unit(bad)


class QuantityAndRuleTests(ProjectionBase):
    def test_duplicate_rule_refused(self):
        data = self.projection()
        twin = copy.deepcopy(data["entries"][2])
        twin["entry_id"] = "rule2"
        twin["direction"] = "increases"
        data["entries"].append(twin)
        self.assertRefused(data, "duplicate rule")

    def test_duplicate_link_refused(self):
        data = self.projection()
        twin = copy.deepcopy(data["entries"][0])
        twin["entry_id"] = "in2"
        data["entries"].append(twin)
        self.assertRefused(data, "link")

    def test_duplicate_entry_id_refused(self):
        data = self.projection()
        data["entries"][1]["entry_id"] = "in1"
        self.assertRefused(data, "duplicate entry_id")

    def test_half_max_not_positive_refused(self):
        for value in (0, -1):
            data, entry = self.entry("rule1")
            entry["half_max"] = q(value, "dimensionless")
            self.assertRefused(data, "half_max")

    def test_hill_power_and_saturation_domains(self):
        data, entry = self.entry("rule1")
        entry["hill_power"] = q(0, "dimensionless")
        entry["saturation_value"] = q(-1, "1/min")
        self.assertRefused(data, "hill_power", "saturation_value")

    def test_rule_domain_checked_on_model_value(self):
        data, entry = self.entry("rule1")
        entry["half_max"] = q(2, "dimensionless", factor=0.5, target_unit="dimensionless")
        self.assertEqual(self.errors(data), [])

    def test_non_integer_steepness_refused(self):
        for value in (2.5, True, "10", -1):
            data, entry = self.entry("out1")
            entry["steepness"] = value
            self.assertRefused(data, "steepness")
        data, entry = self.entry("out1")
        entry["steepness"] = None
        self.assertEqual(self.errors(data), [])

    def test_non_integer_smoothing_refused(self):
        data, entry = self.entry("in1")
        entry["smoothing"] = 1.0
        self.assertRefused(data, "smoothing")

    def test_quantity_without_derivable_unit_refused(self):
        data, entry = self.entry("in1")
        entry["threshold"] = q(10, "mmHg", factor=2)
        self.assertRefused(data, "target_unit")

    def test_bad_action_and_direction_refused(self):
        data = self.projection()
        data["entries"][0]["action"] = "on"
        data["entries"][2]["direction"] = "up"
        self.assertRefused(data, "action", "direction")

    def test_unknown_keys_refused(self):
        data = self.projection()
        data["entries"][2]["apply_to_dead"] = 0
        self.assertRefused(data, "rule1")


class CitationTests(ProjectionBase):
    def test_undeclared_refs_refused(self):
        data = self.projection()
        data["entries"][0]["from"] = {"source": "s9", "locator": "x"}
        data["entries"][1]["from"] = {"data": "d9", "locator": "x"}
        self.assertRefused(data, "source s9 is not declared", "data proposal d9 is not declared")
        data["entries"][1]["from"] = {"data": ["d9"], "locator": "x"}
        self.assertRefused(data, "data proposal ['d9'] is not declared")

    def test_ref_lists_and_basis_accepted(self):
        data = self.projection()
        data["entries"][0]["from"] = [SRC, "basis"]
        self.assertEqual(self.errors(data), [])
        data["entries"][0]["from"] = []
        self.assertRefused(data, "from")

    def test_uncited_source_refused(self):
        entries = self.entries()
        for entry in entries:
            entry["from"] = "basis"
        self.assertRefused(self.projection(entries), "never cited")

    def test_unmapped_rows(self):
        data = self.projection()
        data["unmapped"] = [{"from": {"source": "s1", "locator": "Table 2"}, "reason": "no matching model field"}]
        self.assertEqual(self.errors(data), [])
        data["unmapped"][0]["reason"] = "TODO"
        self.assertRefused(data, "reason")
        data["unmapped"] = [{"from": SRC, "reason": "unused"}]
        self.assertRefused(data, "used by an entry")
        data["unmapped"] = [{"from": "basis", "reason": "x"}]
        self.assertRefused(data, "unmapped[0]")

    def test_justification_required(self):
        data = self.projection()
        data["entries"][0]["justification"] = "TODO"
        self.assertRefused(data, "justification")


class BindingTests(ProjectionBase):
    def test_dictionary_sha_mismatch_refused(self):
        data = self.projection()
        data["dictionary"]["sha256"] = "0" * 64
        self.assertRefused(data, "dictionary")

    def test_dictionary_of_other_capture_refused(self):
        dictionary = json.loads((self.root / DICT).read_text())
        dictionary["capture"]["sha256"] = "1" * 64
        (self.root / DICT).write_text(json.dumps(dictionary))
        data = self.projection()
        data["dictionary"]["sha256"] = sha(self.root / DICT)
        self.assertRefused(data, "capture")

    def test_dictionary_of_other_design_refused(self):
        dictionary = json.loads((self.root / DICT).read_text())
        dictionary["design"]["sha256"] = "1" * 64
        (self.root / DICT).write_text(json.dumps(dictionary))
        data = self.projection()
        data["dictionary"]["sha256"] = sha(self.root / DICT)
        self.assertRefused(data, "design")

    def test_capture_sha_mismatch_refused(self):
        data = self.projection()
        data["capture"]["sha256"] = "0" * 64
        self.assertRefused(data, "capture")

    def test_design_changed_refused(self):
        data = self.projection()
        data["design"]["sha256"] = "0" * 64
        self.assertRefused(data, "design")

    def test_design_approval_missing_refused(self):
        data = self.projection()
        data["design"]["approval"] = "nope"
        self.assertRefused(data, "design approval")

    def test_wrong_kind_and_keys_refused(self):
        data = self.projection()
        data["kind"] = "physicell_design"
        self.assertRefused(data, "kind")
        data = self.projection()
        data["extra"] = 1
        self.assertRefused(data, "keys must be exactly")


class OneWriterTests(ProjectionBase):
    """Ruling T4-d: one writer per behavior; the captured rules CSV and PhysiBoSS links are consulted."""

    def rule(self, entry_id="rule9", signal="oxygen", behavior="necrosis"):
        entry = copy.deepcopy(self.entries()[2])
        entry.update(entry_id=entry_id, signal=signal, behavior=behavior)
        return entry

    def output(self, entry_id="out9", node="A", behavior="migration speed"):
        entry = copy.deepcopy(self.entries()[1])
        entry.update(entry_id=entry_id, node=node, behavior=behavior)
        return entry

    def test_rule_already_in_captured_rules_csv_refused(self):
        data = self.projection([self.rule()])
        self.assertRefused(data, "rule9", "captured rules CSV row 1", "tumor,oxygen,decreases,necrosis")
        self.assertEqual(self.errors(self.projection([self.rule(behavior="migration speed")])), [])

    def test_no_captured_rules_csv_nothing_to_compare(self):
        self.assertEqual(pp.captured_rules(self.root, {"rules_path": None, "rules_sha256": None}), [])

    def test_output_on_behavior_driven_by_captured_rule_refused(self):
        data = self.projection([self.output("out9", "A", "necrosis")])
        self.assertRefused(data, "out9", "captured rules CSV row 1")

    def test_changed_rules_csv_refused(self):
        csv = self.root / self.capture / "package/config/cell_rules.csv"
        csv.write_text(csv.read_text() + "tumor,oxygen,increases,apoptosis,1,1,1,0\n")
        self.assertRefused(self.projection(), "capture")

    def test_two_outputs_on_one_behavior_refused(self):
        data = self.projection([self.output("out1", "A"), self.output("out2", "B")])
        self.assertRefused(data, "out2", "out1", "migration speed")

    def test_rule_and_output_on_one_behavior_refused(self):
        rule = self.rule("rule9", "pressure", "migration speed")
        self.assertRefused(self.projection([self.output("out1"), rule]), "rule9", "out1")
        self.assertRefused(self.projection([rule, self.output("out1")]), "out1", "rule9")

    def test_two_rules_on_one_behavior_with_different_signals_pass(self):
        rules = [self.rule("r1", "oxygen", "migration speed"), self.rule("r2", "pressure", "migration speed")]
        self.assertEqual(self.errors(self.projection(rules)), [])

    def test_input_link_already_in_captured_xml_refused(self):
        data, entry = self.entry("in1")
        entry.update(signal="pressure", node="B")
        self.assertRefused(data, "in1", "captured XML", "pressure -> B")

    def test_output_on_behavior_linked_in_captured_xml_refused(self):
        data = self.projection([self.output("out9", "B", "apoptosis")])
        self.assertRefused(data, "out9", "captured XML", "A -> apoptosis")

    def test_rule_on_behavior_linked_in_captured_xml_refused(self):
        data = self.projection([self.rule("rule9", "oxygen", "apoptosis")])
        self.assertRefused(data, "rule9", "captured XML", "A -> apoptosis")

    def test_setting_inside_intracellular_refused(self):
        entry = copy.deepcopy(self.entries()[3])
        entry.update(path=f"{TUMOR}/intracellular/mapping/output/settings/value", value=0.001,
                     target_unit="1/min", target_unit_justification="j")
        self.assertRefused(self.projection([entry]), "set1", "intracellular")


class ReviewMinorTests(ProjectionBase):
    """Ruling T4-e."""

    def test_case_variant_refused_with_hint(self):
        data, entry = self.entry("in1")
        entry["signal"] = "Oxygen"
        self.assertRefused(data, "unknown signal 'Oxygen'", "closest: oxygen")

    def rewrite_dictionary(self, **changes):
        dictionary = json.loads((self.root / DICT).read_text())
        dictionary.update(changes)
        (self.root / DICT).write_text(json.dumps(dictionary))
        data = self.projection()
        data["dictionary"]["sha256"] = sha(self.root / DICT)
        return data

    def test_dictionary_session_mismatch_refused(self):
        self.assertRefused(self.rewrite_dictionary(session="other-session"), "other-session", "capture's session")

    def test_dictionary_invocation_changed_refused(self):
        (self.root / INVOCATION).write_text((self.root / INVOCATION).read_text() + "\n")
        self.assertRefused(self.projection(), "invocation record")

    def test_dictionary_schema_version_strict(self):
        self.assertRefused(self.rewrite_dictionary(schema_version=1.0), "schema_version")
        self.assertRefused(self.rewrite_dictionary(schema_version=True), "schema_version")

    def test_design_approval_session_differs_from_capture(self):
        other = built_capture(self.root, "mc-session-2", "cap-9")
        manifest = self.root / other / "capture.json"
        data = self.rewrite_dictionary(session="mc-session-2", capture={"path": other, "sha256": sha(manifest)})
        data["capture"] = {"path": other, "sha256": sha(manifest)}
        self.assertRefused(data, "design approval design-1 is for session 'mc-session-1'")

    def test_justification_without_target_unit_refused(self):
        data, entry = self.entry("set1")
        entry["target_unit_justification"] = "why"
        self.assertRefused(data, "target_unit_justification", "without target_unit")

    def test_output_link_on_non_output_node_refused(self):
        data, entry = self.entry("out1")
        entry["node"] = "C"
        self.assertRefused(data, "MaBoSS output node or readout 'C' of tumor")
        data, entry = self.entry("in1")
        entry["node"] = "C"
        self.assertEqual(self.errors(data), [])


class ApprovalTests(ProjectionBase):
    def approve(self, **overrides):
        path = self.write_projection(self.projection())
        ref = {"path": PROJ, "sha256": sha(path)}
        record = {"id": "proj-1", "kind": "stage_approval", "session_id": SESSION, "stage": "physicell_projection",
                  "outputs": ["in1", "out1", "rule1", "set1"], "approved_by": "researcher",
                  "authorization": "approved", "artifact": ref}
        record.update(overrides)
        state.update(self.root, "record", record)
        return ref

    def test_approval_passes(self):
        ref = self.approve()
        record = approvals.require_physicell_projection_approval(self.root, "proj-1", ref["path"], ref["sha256"])
        self.assertEqual(record["id"], "proj-1")

    def test_outputs_mismatch_refused(self):
        ref = self.approve(outputs=["in1", "out1", "rule1"])
        with self.assertRaisesRegex(ValueError, "outputs"):
            approvals.require_physicell_projection_approval(self.root, "proj-1", ref["path"], ref["sha256"])

    def test_session_mismatch_refused(self):
        ref = self.approve(session_id="other-session")
        with self.assertRaisesRegex(ValueError, "session"):
            approvals.require_physicell_projection_approval(self.root, "proj-1", ref["path"], ref["sha256"])

    def test_wrong_stage_refused(self):
        ref = self.approve(stage="physicell_design")
        with self.assertRaisesRegex(ValueError, "physicell_projection"):
            approvals.require_physicell_projection_approval(self.root, "proj-1", ref["path"], ref["sha256"])

    def test_changed_projection_refused(self):
        ref = self.approve()
        (self.root / PROJ).write_text((self.root / PROJ).read_text() + "\n")
        with self.assertRaisesRegex(ValueError, "sha256"):
            approvals.require_physicell_projection_approval(self.root, "proj-1", ref["path"], ref["sha256"])


if __name__ == "__main__":
    unittest.main()
