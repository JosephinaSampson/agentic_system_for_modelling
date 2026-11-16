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
from pathlib import Path

from scripts import state
from scripts.capture.record_physicell_config import record_physicell
from scripts.data import approvals
from scripts.physicell import capture_dictionary as cd
from scripts.physicell import physicell_projection as pp
from tests.data.test_physicell_design import (FIXTURES, MABOSS, REL, RULES_REFUSAL, SESSION, STAGE_A, Base,
                                              built_capture, dispatch_design)
from tests.data.test_physicell_dictionary import INVOCATION, good_calls

PROJ = "runs/orchestrator/p1/physicell_projection.json"
DICT = "runs/orchestrator/p1/physicell_dictionary.json"
TUMOR = "cell_definitions/cell_definition[@name='tumor']/phenotype"
APOPTOSIS = f"{TUMOR}/death/model[@name='apoptosis']/death_rate"
RATE0 = f"{TUMOR}/cycle/phase_transition_rates/rate[@start_index='0']"
SRC = {"source": "s1", "locator": "Table 1"}


def q(value, unit, transform="identity", factor=1, **extra):
    """A projection quantity without its model_value (fill_model_values pastes it, as the orchestrator does)."""
    return {"value": value, "value_unit": unit, "transform": transform, "factor": factor, **extra}


QUANTITY_KEYS = ("threshold", "value", "base_value", "saturation_value", "half_max", "hill_power")


def fill_model_values(data):
    """Paste the model value the check prints into every quantity (and setting entry) that has none yet: the
    orchestrator's step (ruling T9-e). An invalid quantity gets "TODO" (its own error is the one under test)."""
    def paste(target):
        if isinstance(target, dict) and "model_value" not in target:
            try:
                target["model_value"] = pp.model_value(target)
            except ValueError:
                target["model_value"] = "TODO"
    for entry in data.get("entries") or ():
        if not isinstance(entry, dict):
            continue
        if entry.get("kind") == "setting":
            paste(entry)
        else:
            for key in QUANTITY_KEYS:
                paste(entry.get(key))
    return data


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@contextlib.contextmanager
def binding_reads(root):
    """Count settings_xml.capture_tree calls per capture and Path.read_bytes of each file inside a capture folder
    (runs/multicellular-configurator/<session>/configs/<capture>/...). Yields (binds, reads): Counters keyed by
    capture rel and by (capture rel, file inside it)."""
    from collections import Counter
    from pathlib import Path
    from scripts.physicell import settings_xml as sx
    binds, reads = Counter(), Counter()
    original_read, original_tree = Path.read_bytes, sx.capture_tree

    def read(path):
        try:
            parts = path.relative_to(root).parts
        except ValueError:
            parts = ()
        if len(parts) > 5 and parts[:2] == ("runs", "multicellular-configurator") and parts[3] == "configs":
            reads[("/".join(parts[:5]), "/".join(parts[5:]))] += 1
        return original_read(path)

    def tree(where, capture_rel):
        binds[capture_rel] += 1
        return original_tree(where, capture_rel)
    with mock.patch.object(Path, "read_bytes", read), mock.patch.object(sx, "capture_tree", tree):
        yield binds, reads


BOUND_FILES = ("capture.json", "package/config/PhysiCell_settings.xml")


def assert_read_only_by_bindings(case, binds, reads):
    """capture.json and the package XML of every capture were read exactly once per binding of that capture: no
    caller re-reads them after settings_xml.capture_tree verified them."""
    for (capture, name), count in reads.items():
        if name in BOUND_FILES:
            case.assertEqual(count, binds[capture], f"{capture}/{name} read {count}x for {binds[capture]} binding(s)")


class ProjectionBase(Base):
    """A checked, approved design, its captured dictionary and a drafted projection over the built capture.

    The tumor's MaBoSS handoff has nodes A, B, C and declared outputs A, B only."""

    def make_handoff(self, session=MABOSS, nodes=("A", "B", "C"), outputs=("A", "B"), **kw):
        return super().make_handoff(session, nodes=nodes, outputs=outputs, **kw)

    links = False  # the stage-A capture carries the fixture's PhysiBoSS links (only below the conformance check)

    def edit_capture(self, xml_root):
        """Hook: change the built capture's XML before it is recorded (default: unchanged)."""

    def setUp(self):
        super().setUp()
        self.design = self.complete()  # its basis is the fixture capture cap-1
        self.basis = self.capture
        # stage A: a new session (STAGE_A) dispatched with the design (anchored on the basis session SESSION)
        self.capture = built_capture(self.root, STAGE_A, "cap-2", edit=self.edit_capture, links=self.links)
        self.write(self.design)
        self.design_sha = sha(self.root / REL)
        state.update(self.root, "record", {
            "id": "design-1", "kind": "stage_approval", "session_id": SESSION, "stage": "physicell_design",
            "outputs": ["default", "tumor", "tumor_hypoxic", "oxygen", "drug"], "approved_by": "researcher",
            "authorization": "approved", "artifact": {"path": REL, "sha256": self.design_sha}})
        dispatch_design(self.root)  # its DATA_INPUT line names design-1
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

    def write_projection(self, data, fill=True):
        path = self.root / PROJ
        path.write_text(json.dumps(fill_model_values(data) if fill else data, indent=2))
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


class NonTextJsonTests(ProjectionBase):
    """PR #17 review 4586111416: JSON values that are not text are reported, never a TypeError traceback."""

    def test_entry_kind_list_refused(self):
        data = self.projection()
        data["entries"][0]["kind"] = ["rule"]
        self.assertRefused(data, "entries[0]: kind must be one of")

    def test_design_with_non_text_values_is_an_error(self):
        design = json.loads((self.root / REL).read_text())
        design["cell_definitions"][2]["variant_of"] = ["tumor"]
        (self.root / REL).write_text(json.dumps(design, indent=2))
        data = self.projection()
        data["design"]["sha256"] = sha(self.root / REL)
        self.assertRefused(data, "variant_of ['tumor']")
        err = io.StringIO()
        with mock.patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": str(self.root)}), \
                contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(pp.main(["check", PROJ]), 1)
        self.assertIn("variant_of ['tumor']", err.getvalue())


class PhysiCellApprovalMessageTests(ProjectionBase):
    """The physicell_design / physicell_projection approval refusals (pinned across the PR #17 review 4586113631
    refactor of approvals.py)."""

    def approve(self, record_id, stage, rel, session, outputs):
        state.update(self.root, "record", {
            "id": record_id, "kind": "stage_approval", "session_id": session, "stage": stage, "outputs": outputs,
            "approved_by": "researcher", "authorization": "approved",
            "artifact": {"path": rel, "sha256": sha(self.root / rel)}})

    def test_design_approval_refusals(self):
        names = ["default", "tumor", "tumor_hypoxic", "oxygen", "drug"]
        self.approve("d-session", "physicell_design", REL, "other", names)
        with self.assertRaisesRegex(ValueError, "^decision d-session approves session 'other', but the design's "
                                                "session anchor is 'mc-session-1'$"):
            approvals.require_physicell_design_approval(self.root, "d-session", REL, self.design_sha)
        self.approve("d-outputs", "physicell_design", REL, SESSION, names[:2])
        with self.assertRaisesRegex(ValueError, "^decision d-outputs outputs must equal the cell definition and "
                                                r"substrate names of the design: \['default', 'drug', "):
            approvals.require_physicell_design_approval(self.root, "d-outputs", REL, self.design_sha)
        self.assertEqual(approvals.require_physicell_design_approval(self.root, "design-1", REL,
                                                                     self.design_sha)["id"], "design-1")

    def test_projection_approval_refusals(self):
        path = self.write_projection(self.projection())
        self.approve("p-session", "physicell_projection", PROJ, "other", ["in1", "out1", "rule1", "set1"])
        with self.assertRaisesRegex(ValueError, "^decision p-session approves session 'other', but the projection's "
                                                "capture is of PhysiCell session 'mc-stage-a'$"):
            approvals.require_physicell_projection_approval(self.root, "p-session", PROJ, sha(path))
        self.approve("p-outputs", "physicell_projection", PROJ, STAGE_A, ["in1"])
        with self.assertRaisesRegex(ValueError, "^decision p-outputs outputs must equal the entry ids of the "
                                                r"projection: \['in1', 'out1', 'rule1', 'set1'\]$"):
            approvals.require_physicell_projection_approval(self.root, "p-outputs", PROJ, sha(path))


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

    def test_factor_one_target_unit_must_be_the_derived_unit(self):
        # PR #17 review 4586110579: an identity "conversion" between different units is no conversion
        data = self.setting(value=2e-5, value_unit="1/hour", target_unit="1/min",
                            target_unit_justification="the XML's units")
        self.assertRefused(data, "set1", "target_unit '1/min'", "derived unit '1/hour'")
        for transform, unit, derived in (("reciprocal", "hour", "1/hour"), ("ln2_over", "1/hour", "hour")):
            with self.subTest(transform=transform):
                data = self.setting(value=2, value_unit=unit, transform=transform, target_unit="1/min")
                self.assertRefused(data, "set1", f"derived unit {derived!r}")
        # the same target_unit as the derived unit is fine (it states the unit, it converts nothing)
        self.assertEqual(self.errors(self.setting(target_unit="1/min")), [])

    def test_factor_one_compound_reciprocal_with_target_unit_refused(self):
        data = self.setting(value=2, value_unit="min/cell", transform="reciprocal", target_unit="1/min",
                            target_unit_justification="j")
        self.assertRefused(data, "set1", "not derivable", "factor != 1", "justification")
        # PR #17 review 4587426548: the hint no longer points at a factor-1 unit_factor
        self.assertNotIn("unit_factor", " ".join(self.errors(data)))

    def test_unit_factor_of_one_refused(self):
        # PR #17 review 4587426548: a unit_factor of 1 converts nothing (1/hour -> 1/min would be 60x off)
        data = self.setting(value=2e-5, value_unit="1/hour", transform="unit_factor", factor=1, target_unit="1/min",
                            target_unit_justification="the XML's units")
        self.assertRefused(data, "set1", "a unit_factor of 1 converts nothing; use identity with matching units")
        data, entry = self.entry("rule1")
        entry["half_max"] = q(5, "min", "unit_factor", 1.0, target_unit="min")
        self.assertRefused(data, "rule1", "half_max", "a unit_factor of 1 converts nothing")

    def test_tool_hint_required(self):
        self.assertRefused(self.setting(tool_hint=""), "tool_hint")


class CaptureBoundOnceTests(ProjectionBase):
    """PR #17 review 4587427664: the projection check binds its capture once (sx.capture_tree) and uses that
    binding's tree and manifest_sha256; it never reads capture.json or the package XML again."""

    def test_projection_check_binds_its_capture_once(self):
        self.write_projection(self.projection())
        with binding_reads(self.root) as (binds, reads):
            self.assertEqual(pp.check(self.root, PROJ), [])
        self.assertEqual(binds[self.capture], 1)
        self.assertEqual(reads[(self.capture, "capture.json")], 1)
        self.assertEqual(reads[(self.capture, "package/config/PhysiCell_settings.xml")], 1)
        assert_read_only_by_bindings(self, binds, reads)

    def test_draft_records_the_bound_manifest_sha256(self):
        with binding_reads(self.root) as (binds, reads):
            path = pp.draft(self.root, "p7", capture=self.capture, design=REL, design_approval="design-1",
                            dictionary=DICT)
        self.assertEqual(json.loads(path.read_text())["capture"]["sha256"],
                         sha(self.root / self.capture / "capture.json"))
        self.assertEqual(binds[self.capture], 1)
        assert_read_only_by_bindings(self, binds, reads)


class HandoffManifestTests(ProjectionBase):
    """PR #17 review 4587428064: one helper, physicell_design.handoff_manifest, reads a design's MaBoSS handoff once,
    checks the design's sha256 and parses it; the design check, conformance and the projection check all use it."""

    def test_projection_check_reads_handoffs_through_the_helper(self):
        from scripts.physicell import physicell_design as pd
        self.write_projection(self.projection())
        with mock.patch.object(pd, "handoff_manifest", wraps=pd.handoff_manifest) as helper:
            self.assertEqual(pp.check(self.root, PROJ), [])
        ref = self.design["intracellular"][0]["maboss"]
        # maboss_handoff (the design re-check), conformance and design_section: each through the helper
        self.assertEqual([c.args[1] for c in helper.call_args_list], [ref, ref, ref])

    def test_helper_reads_once_checks_sha256_and_parses(self):
        from scripts.physicell import physicell_design as pd
        ref = self.design["intracellular"][0]["maboss"]
        reads = []
        original = Path.read_bytes

        def counting(path):
            if path.name == Path(ref["handoff"]).name:
                reads.append(path)
            return original(path)
        with mock.patch.object(Path, "read_bytes", counting):
            manifest = pd.handoff_manifest(self.root, ref)
        self.assertEqual((len(reads), manifest), (1, json.loads((self.root / ref["handoff"]).read_text())))
        with self.assertRaisesRegex(ValueError, "^it changed since the design check$"):
            pd.handoff_manifest(self.root, {**ref, "sha256": "0" * 64})
        path = self.root / ref["handoff"]
        path.write_bytes(b"[1]")
        with self.assertRaisesRegex(ValueError, "not a JSON object"):
            pd.handoff_manifest(self.root, {**ref, "sha256": sha(path)})


class NonDecimalLeafTests(ProjectionBase):
    """PR #17 review 4586111969: a setting's element must hold a strict ASCII decimal (sx.number), as verify_export
    requires; float() would accept 'inf', 'nan', '1_000' and full-width digits."""

    LEAVES = ((APOPTOSIS, "inf"), (RATE0, "1_000"), (f"{TUMOR}/death/model[@name='necrosis']/death_rate", "nan"),
              (f"{TUMOR}/motility/speed", "\uff11"))

    def edit_capture(self, xml_root):
        for path, text in self.LEAVES:
            xml_root.find(path).text = text

    def test_non_decimal_leaf_refused(self):
        for path, text in self.LEAVES:
            with self.subTest(text=text):
                data, entry = self.entry("set1")
                entry.update(path=path, target_unit="1/min", target_unit_justification="the parent's units")
                self.assertRefused(data, f"holds {text!r}, not a number")


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
        self.assertEqual(pp.converted_unit(q(1, "1/h", "unit_factor", 1 / 60, target_unit="1/min")), "1/min")
        self.assertEqual(pp.converted_unit(q(1, "min", target_unit="min")), "min")
        self.assertEqual(pp.converted_unit(q(1, "min", "reciprocal", target_unit="1/min")), "1/min")
        for bad in (q(1, "1/hour", target_unit="1/min"), q(1, "hour", "reciprocal", target_unit="1/min"),
                    q(1, "1/hour", "ln2_over", target_unit="min"), q(1, "min/cell", "reciprocal", target_unit="x")):
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, "target_unit"):
                pp.converted_unit(bad)
        for bad in (q(1, "h", factor=60), q(1, "h", "unit_factor", 60), q(1, "micron/min", "reciprocal")):
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, "target_unit"):
                pp.converted_unit(bad)
        for bad in (q(1, "h", "unit_factor"), q(1, "1/h", "unit_factor", 1.0, target_unit="1/min")):
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, "a unit_factor of 1 converts nothing"):
                pp.converted_unit(bad)


class QuantityTargetUnitTests(ProjectionBase):
    """PR #17 review 4586110579: rule and link quantities follow the setting's target_unit rules."""

    def test_rule_quantity_identity_target_unit_refused(self):
        data, entry = self.entry("rule1")
        entry["half_max"] = q(5, "hour", target_unit="furlong")
        self.assertRefused(data, "rule1", "half_max", "target_unit 'furlong'", "derived unit 'hour'")

    def test_link_quantity_identity_target_unit_refused(self):
        data, entry = self.entry("in1")
        entry["threshold"] = q(10, "mmHg", target_unit="kPa")
        self.assertRefused(data, "in1", "threshold", "target_unit 'kPa'")

    def test_factor_or_unit_factor_conversion_keeps_its_target_unit(self):
        data, entry = self.entry("rule1")
        entry["half_max"] = q(2, "dimensionless", factor=0.5, target_unit="dimensionless")
        entry["saturation_value"] = q(0.6, "1/h", "unit_factor", 1 / 60, target_unit="1/min")
        self.assertEqual(self.errors(data), [])


class ModelValueTests(ProjectionBase):
    """Ruling T9-e: every quantity carries the model value the configurator copies verbatim; the check recomputes
    it and prints the value to paste."""

    def test_missing_model_value_refused(self):
        data = fill_model_values(self.projection())
        del data["entries"][0]["threshold"]["model_value"]
        self.write_projection(data, fill=False)
        del data["entries"][3]["model_value"]
        self.write_projection(data, fill=False)
        errors = pp.check(self.root, PROJ)
        self.assertTrue(any("in1" in e and "threshold: model_value must be a number: paste 10.0" in e
                            for e in errors), errors)
        self.assertTrue(any("set1" in e and "model_value must be a number: paste 2e-05" in e for e in errors), errors)
        data["entries"][0]["threshold"].pop("value")
        self.write_projection(data, fill=False)
        self.assertTrue(any("threshold must be {value, value_unit, transform, factor, model_value}" in e
                            for e in pp.check(self.root, PROJ)))

    def test_todo_model_value_refused_with_the_value_to_paste(self):
        data = fill_model_values(self.projection())
        data["entries"][2]["half_max"]["model_value"] = "TODO"
        data["entries"][3]["model_value"] = "TODO"
        self.write_projection(data, fill=False)
        errors = pp.check(self.root, PROJ)
        self.assertTrue(any("rule1" in e and "half_max: model_value must be a number: paste 1.0" in e for e in errors),
                        errors)
        self.assertTrue(any("set1" in e and "model_value must be a number: paste 2e-05" in e for e in errors), errors)

    def test_wrong_model_value_refused(self):
        data, entry = self.entry("rule1")
        entry["saturation_value"] = q(0.6, "1/h", "unit_factor", 1 / 60, target_unit="1/min", model_value=0.6)
        self.write_projection(data)
        errors = pp.check(self.root, PROJ)
        self.assertTrue(any("saturation_value: model_value 0.6 is not the model value 0.01" in e and "paste 0.01" in e
                            for e in errors), errors)
        entry["saturation_value"]["model_value"] = 0.01 * (1 + 1e-10)
        self.write_projection(data)
        self.assertTrue(any("relative 1e-12" in e for e in pp.check(self.root, PROJ)))
        entry["saturation_value"]["model_value"] = 0.6 / 60
        self.write_projection(data)
        self.assertEqual(pp.check(self.root, PROJ), [])

    def test_bool_model_value_refused(self):
        data, entry = self.entry("in1")
        entry["threshold"]["model_value"] = True
        self.assertRefused(data, "model_value must be a number")


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

    def test_capture_with_enabled_rules_csv_refused(self):
        # Ruling T5-d(b): load_xml_configuration keeps no rules CSV rows, and no tool reloads one
        rules = built_capture(self.root, STAGE_A, "cap-rules", rules=True)
        data = self.projection()
        data["capture"] = {"path": rules, "sha256": sha(self.root / rules / "capture.json")}
        self.assertRefused(data, RULES_REFUSAL, "apply projections to the stage-A capture (no rules), and revise "
                                                "and re-apply a projection from there")

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
    """Ruling T4-d: one writer per behavior; the captured PhysiBoSS links are consulted (a capture never carries
    rules, ruling T5-d)."""

    def rule(self, entry_id="rule9", signal="oxygen", behavior="necrosis"):
        entry = copy.deepcopy(self.entries()[2])
        entry.update(entry_id=entry_id, signal=signal, behavior=behavior)
        return entry

    def output(self, entry_id="out9", node="A", behavior="migration speed"):
        entry = copy.deepcopy(self.entries()[1])
        entry.update(entry_id=entry_id, node=node, behavior=behavior)
        return entry

    def test_rule_and_output_on_a_free_behavior_pass(self):
        self.assertEqual(self.errors(self.projection([self.rule(), self.output("out9", "A", "migration speed")])), [])

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

    def test_setting_inside_intracellular_refused(self):
        entry = copy.deepcopy(self.entries()[3])
        entry.update(path=f"{TUMOR}/intracellular/mapping/output/settings/value", value=0.001,
                     target_unit="1/min", target_unit_justification="j")
        self.assertRefused(self.projection([entry]), "set1", "intracellular")


class CapturedLinkTests(OneWriterTests):
    """Defense in depth below the stage-A conformance check (which refuses any captured PhysiBoSS link, ruling
    T9-b): with conformance bypassed, the one-writer rules still consult the captured links."""

    links = True

    def setUp(self):
        patcher = mock.patch.object(cd, "conformance", return_value=[])
        patcher.start()
        self.addCleanup(patcher.stop)
        super().setUp()

    def test_without_the_bypass_the_capture_is_refused(self):
        mock.patch.stopall()
        self.assertRefused(self.projection(), "does not conform to the design",
                           "cell definition tumor: 1 PhysiBoSS input link(s)")

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

    def test_dictionary_lists_rederived_from_the_invocation_record(self):
        # review M1: the dictionary's lists must be what the hashed invocation record yields
        original = json.loads((self.root / DICT).read_text())
        data = self.rewrite_dictionary(signals=[{"name": "oxygen", "type": "substrate"},
                                                {"name": "made up", "type": "substrate"}])
        self.assertRefused(data, "its signals are not the lists its invocation record yields")
        behaviors = {**original["behaviors"], "tumor": original["behaviors"]["tumor"][:1]}
        self.assertRefused(self.rewrite_dictionary(signals=original["signals"], behaviors=behaviors),
                           "its behaviors are not the lists its invocation record yields")

    def test_dictionary_invocation_outside_the_capture_session_refused(self):
        # review M1: the invocation record lies in the capture's session
        other = "runs/multicellular-configurator/mc-other/invocation/agent-1/tool_calls.json"
        (self.root / other).parent.mkdir(parents=True)
        (self.root / other).write_bytes((self.root / INVOCATION).read_bytes())
        self.assertRefused(self.rewrite_dictionary(invocation=other), "is not in the capture's session")

    def test_dictionary_session_mismatch_refused(self):
        self.assertRefused(self.rewrite_dictionary(session="other-session"), "other-session", "capture's session")

    def test_dictionary_invocation_changed_refused(self):
        (self.root / INVOCATION).write_text((self.root / INVOCATION).read_text() + "\n")
        self.assertRefused(self.projection(), "invocation record")

    def test_dictionary_schema_version_strict(self):
        self.assertRefused(self.rewrite_dictionary(schema_version=1.0), "schema_version")
        self.assertRefused(self.rewrite_dictionary(schema_version=True), "schema_version")

    def other_capture(self, session="mc-session-9"):
        """The projection rebound to a capture of another session (its dictionary rewritten to match)."""
        other = built_capture(self.root, session, "cap-9")
        manifest = self.root / other / "capture.json"
        invocation = INVOCATION.replace(STAGE_A, session)  # that session's own invocation record (review M1)
        (self.root / invocation).parent.mkdir(parents=True)
        (self.root / invocation).write_text((self.root / INVOCATION).read_text().replace(STAGE_A, session))
        data = self.rewrite_dictionary(session=session, capture={"path": other, "sha256": sha(manifest)},
                                       invocation=invocation, source_sha256=sha(self.root / invocation))
        data["capture"] = {"path": other, "sha256": sha(manifest)}
        return data

    def test_design_anchor_differs_from_capture_session(self):
        # the design is anchored on the basis session; the stage-A capture is in a new session
        self.assertNotEqual(SESSION, STAGE_A)
        self.assertEqual(self.errors(self.projection()), [])

    def test_capture_without_design_lineage_refused(self):
        self.assertRefused(self.other_capture(), "capture's session mc-session-9 was not dispatched with it",
                           "lineage: no invocation record")

    def test_capture_dispatched_with_another_design_sha_refused(self):
        data = self.other_capture()
        dispatch_design(self.root, session="mc-session-9", digest="0" * 64)
        self.assertRefused(data, "another sha256")
        dispatch_design(self.root, session="mc-session-9")
        self.assertEqual(self.errors(data), [])

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
        record = {"id": "proj-1", "kind": "stage_approval", "session_id": STAGE_A, "stage": "physicell_projection",
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
