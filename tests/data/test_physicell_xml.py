from __future__ import annotations

import json
import shutil
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from scripts.capture.record_physicell_config import record_physicell
from scripts.physicell import settings_xml as sx

FIXTURES = Path(__file__).parent / "fixtures" / "physicell"
SESSION = "mc-session-1"
CAPTURE = "cap-1"


def parse(text):
    return ET.fromstring(text)


class XmlHelperTests(unittest.TestCase):
    def setUp(self):
        self.root = sx.load(FIXTURES / "PhysiCell_settings.xml")

    def test_resolve_exactly_one(self):
        base = "cell_definitions/cell_definition[@name='tumor']/phenotype/cycle/phase_transition_rates/rate"
        with self.assertRaisesRegex(ValueError, "matches 2 elements"):
            sx.resolve(self.root, base)
        element = sx.resolve(self.root, base + "[@start_index='0']")
        self.assertEqual(element.text, "0.00334")
        with self.assertRaisesRegex(ValueError, "matches 0 elements"):
            sx.resolve(self.root, "nothing/here")

    def test_units_attribute_read(self):
        speed = sx.resolve(self.root, "cell_definitions/cell_definition[@name='tumor']/phenotype/motility/speed")
        self.assertEqual(sx.units(speed), "micron/min")
        self.assertIsNone(sx.units(sx.resolve(self.root, "domain/x_min")))

    def test_names_and_cycle(self):
        self.assertEqual(sx.cell_definitions(self.root), ["default", "tumor"])
        self.assertEqual(sx.substrates(self.root), ["oxygen", "drug"])
        self.assertEqual(sx.cycle_model(self.root, "tumor"), "Flow cytometry model (separated)")
        with self.assertRaises(ValueError):
            sx.cycle_model(self.root, "absent")

    def test_dtd_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x.xml"
            path.write_text('<!DOCTYPE PhysiCell_settings [<!ENTITY a "b">]><PhysiCell_settings>&a;</PhysiCell_settings>')
            with self.assertRaises(ValueError):
                sx.load(path)
            path.write_text("<other/>")
            with self.assertRaisesRegex(ValueError, "PhysiCell_settings"):
                sx.load(path)

    def test_diff_reports_changed_added_removed_ignoring_order_and_whitespace(self):
        before = parse("<PhysiCell_settings><a><x>1</x><y>2</y></a><b units='u'>3</b><gone>1</gone></PhysiCell_settings>")
        after = parse("<PhysiCell_settings>\n <b units='u'> 3 </b>\n <a><y>5</y><x>1</x></a><new>7</new></PhysiCell_settings>")
        changes = {c["path"]: c for c in sx.diff(before, after)}
        self.assertEqual(set(changes), {"a/y", "gone", "new"})
        self.assertEqual(changes["a/y"]["before"]["text"], "2")
        self.assertEqual(changes["a/y"]["after"]["text"], "5")
        self.assertIsNone(changes["gone"]["after"])
        self.assertIsNone(changes["new"]["before"])
        self.assertEqual(sx.diff(self.root, self.root), [])

    def test_diff_detects_attribute_change(self):
        before = parse("<PhysiCell_settings><b units='u'>3</b></PhysiCell_settings>")
        after = parse("<PhysiCell_settings><b units='v'>3</b></PhysiCell_settings>")
        self.assertEqual(len(sx.diff(before, after)), 1)


class ReviewFixTests(unittest.TestCase):
    def setUp(self):
        self.root = sx.load(FIXTURES / "PhysiCell_settings.xml")

    def test_malformed_path_is_value_error(self):
        for bad in ("domain[", "domain[@", "x[@a='"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                sx.resolve(self.root, bad)

    def test_ancestor_and_absolute_paths_refused(self):
        for bad in ("../domain", "domain/../overall", "/domain", "//domain", "domain/.."):
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, "not allowed"):
                sx.resolve(self.root, bad)

    def test_substrate_identity_order_independent(self):
        before = parse("<PhysiCell_settings><s><c substrate='a'>1</c><c substrate='b'>2</c></s></PhysiCell_settings>")
        after = parse("<PhysiCell_settings><s><c substrate='b'>2</c><c substrate='a'>1</c></s></PhysiCell_settings>")
        self.assertEqual(sx.diff(before, after), [])
        real = sx.resolve(self.root, "cell_definitions/cell_definition[@name='tumor']/phenotype/motility/"
                          "advanced_chemotaxis/chemotactic_sensitivities/chemotactic_sensitivity[@substrate='drug']")
        self.assertEqual(real.text, "0.0")

    def test_quote_in_identity_falls_back_to_position(self):
        root = parse("<PhysiCell_settings><v name=\"a'b\">1</v><v name='c'>2</v></PhysiCell_settings>")
        for path, _, _ in sx.normalise(root):
            self.assertEqual(sx.resolve(root, path).tag, "v")
        self.assertEqual(len({p for p, _, _ in sx.normalise(root)}), 2)

    def test_fixture_real_export_blocks_resolve(self):
        sx.resolve(self.root, "microenvironment_setup/variable[@name='oxygen']/Dirichlet_options/boundary_value[@ID='xmin']")
        sx.resolve(self.root, "cell_definitions/cell_definition[@name='tumor']/initial_parameter_distributions/distribution")


class CaptureBindingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path.home() / ".cache") if (Path.home() / ".cache").is_dir() else tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.project = base / "project"
        self.source = base / "source"
        (self.source / "config").mkdir(parents=True)
        self.project.mkdir()
        shutil.copy(FIXTURES / "PhysiCell_settings.xml", self.source / "config")
        shutil.copy(FIXTURES / "cell_rules.csv", self.source / "config")
        record_physicell(self.project, self.source, self.source / "config/PhysiCell_settings.xml", SESSION, CAPTURE)
        self.rel = f"runs/multicellular-configurator/{SESSION}/configs/{CAPTURE}"
        self.folder = self.project / self.rel

    def test_capture_binding(self):
        result = sx.capture(self.project, self.rel)
        self.assertEqual(result["session"], SESSION)
        self.assertEqual(result["capture"], CAPTURE)
        self.assertEqual(result["xml_path"], self.rel + "/package/config/PhysiCell_settings.xml")
        self.assertEqual(result["rules_path"], self.rel + "/package/config/cell_rules.csv")
        self.assertEqual(len(result["xml_sha256"]), 64)
        self.assertEqual(len(result["rules_sha256"]), 64)
        sx.load(self.project / result["xml_path"])

    def test_capture_without_rules(self):
        shutil.rmtree(self.project / "runs")
        text = (self.source / "config/PhysiCell_settings.xml").read_text().replace('format="csv" enabled="true"', 'format="csv" enabled="false"')
        (self.source / "config/PhysiCell_settings.xml").write_text(text)
        record_physicell(self.project, self.source, self.source / "config/PhysiCell_settings.xml", SESSION, CAPTURE)
        result = sx.capture(self.project, self.rel)
        self.assertIsNone(result["rules_path"])
        self.assertIsNone(result["rules_sha256"])

    def test_capture_binding_refuses_changed_xml(self):
        xml = self.folder / "package/config/PhysiCell_settings.xml"
        xml.write_text(xml.read_text().replace("<x_min>-400", "<x_min>-500"))
        with self.assertRaisesRegex(ValueError, "changed"):
            sx.capture(self.project, self.rel)

    def test_capture_binding_refuses_changed_rules(self):
        rules = self.folder / "package/config/cell_rules.csv"
        rules.write_text(rules.read_text() + "x\n")
        with self.assertRaisesRegex(ValueError, "changed"):
            sx.capture(self.project, self.rel)

    def test_capture_ruleset_under_disabled_ancestor_is_disabled(self):
        shutil.rmtree(self.project / "runs")
        path = self.source / "config/PhysiCell_settings.xml"
        path.write_text(path.read_text().replace("<rulesets>", '<rulesets enabled="false">'))
        record_physicell(self.project, self.source, path, SESSION, CAPTURE)
        self.assertIsNone(sx.capture(self.project, self.rel)["rules_path"])

    def test_capture_refuses_symlink(self):
        xml = self.folder / "package/config/PhysiCell_settings.xml"
        real = self.folder / "real.xml"
        xml.rename(real)
        xml.symlink_to(real)
        with self.assertRaises(ValueError):
            sx.capture(self.project, self.rel)

    def test_capture_refuses_manifest_identity_mismatch(self):
        manifest = self.folder / "capture.json"
        data = json.loads(manifest.read_text())
        data["capture_id"] = "other"
        manifest.write_text(json.dumps(data))
        with self.assertRaises(ValueError):
            sx.capture(self.project, self.rel)

    def test_capture_must_be_under_configs(self):
        for bad in (f"runs/multicellular-configurator/{SESSION}/exports/{CAPTURE}",
                    f"runs/network-curator/{SESSION}/configs/{CAPTURE}",
                    f"runs/multicellular-configurator/{SESSION}/configs/{CAPTURE}/package",
                    f"runs/multicellular-configurator/{SESSION}/configs/../configs/{CAPTURE}",
                    "/etc"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                sx.capture(self.project, bad)


if __name__ == "__main__":
    unittest.main()
