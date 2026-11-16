from __future__ import annotations

import contextlib
import copy
import io
import json
import os
import unittest
from unittest import mock

import xml.etree.ElementTree as ET

from scripts.capture.record_physicell_config import record_physicell
from scripts.physicell import physicell_projection as pp
from scripts.physicell import settings_xml as sx
from scripts.physicell import verify_export as ve
from tests.data.test_physicell_design import SESSION
from tests.data.test_physicell_projection import APOPTOSIS, PROJ, SRC, ProjectionBase, q, sha

AFTER = "mc-session-2"
CAPTURE_ID = "cap-1"
RULES_CSV = "rules.csv"
OUTPUT = f"runs/orchestrator/p1/physicell_verification.{AFTER}.{CAPTURE_ID}.json"
INVOCATION = f"runs/multicellular-configurator/{AFTER}/invocation/agent-1/task.txt"


def fmt(value):
    return repr(float(value))


def rule_row(entry):
    return [entry["cell_definition"], entry["signal"], entry["direction"], entry["behavior"],
            fmt(pp.model_value(entry["saturation_value"])), fmt(pp.model_value(entry["half_max"])),
            fmt(pp.model_value(entry["hill_power"])), "0"]


def link_element(entry):
    if entry["kind"] == "physiboss_input":
        element = ET.Element("input", {"physicell_name": entry["signal"], "intracellular_name": entry["node"]})
        values = [("action", entry["action"]), ("threshold", fmt(pp.model_value(entry["threshold"]))),
                  ("smoothing", str(entry["smoothing"]))]
    else:
        element = ET.Element("output", {"physicell_name": entry["behavior"], "intracellular_name": entry["node"]})
        values = [("action", entry["action"]), ("value", fmt(pp.model_value(entry["value"]))),
                  ("base_value", fmt(pp.model_value(entry["base_value"])))]
        if entry["steepness"] is not None:
            values.append(("steepness", str(entry["steepness"])))
        values.append(("smoothing", str(entry["smoothing"])))
    settings = ET.SubElement(element, "settings")
    for tag, text in values:
        ET.SubElement(settings, tag).text = text
    return element


class VerifyBase(ProjectionBase):
    """The stage-A capture (cap-2 of mc-session-1, no rules) and an approved-shape projection over it; `export`
    simulates the stage-B configurator run: a new session's capture with the applied entries and its invocation."""

    def setUp(self):
        super().setUp()
        self.write_projection(self.projection())

    def link_cell(self, xml_root, cell):
        return xml_root.find(f"cell_definitions/cell_definition[@name='{cell}']/phenotype/intracellular/mapping")

    def export(self, applied=None, *, mutate=None, rows=None, session=AFTER, capture_id=CAPTURE_ID,
               invocation=True, digest=None, ruleset=None):
        """Record the stage-B capture: the stage-A package XML with `applied` entries (default: the projection's)
        applied; `mutate(xml_root, config_dir)` changes it further; `rows` replaces the rules CSV rows."""
        data = json.loads((self.root / PROJ).read_text())
        applied = data["entries"] if applied is None else applied
        package = self.root / self.capture / "package/config"
        source = self.root.parent / f"after-{session}-{capture_id}"
        config = source / "config"
        config.mkdir(parents=True)
        for name in ("tumor.bnd", "tumor.cfg"):
            (config / name).write_bytes((package / name).read_bytes())
        tree = ET.parse(package / sx.XML_NAME)
        xml_root = tree.getroot()
        csv_rows = []
        for entry in applied:
            if entry["kind"] == "setting":
                sx.resolve(xml_root, entry["path"]).text = fmt(pp.model_value(entry))
            elif entry["kind"] == "rule":
                csv_rows.append(rule_row(entry))
            else:
                self.link_cell(xml_root, entry["cell_definition"]).append(link_element(entry))
        if rows is not None:
            csv_rows = rows
        if ruleset if ruleset is not None else bool(csv_rows):
            rules = xml_root.find("cell_rules")
            if rules is None:
                rules = ET.SubElement(xml_root, "cell_rules")
            rulesets = rules.find("rulesets")
            if rulesets is None:
                rulesets = ET.SubElement(rules, "rulesets")
            enabled = ET.SubElement(rulesets, "ruleset", {"protocol": "CBHG", "version": "3.0", "format": "csv",
                                                           "enabled": "true"})
            ET.SubElement(enabled, "folder").text = "./config"
            ET.SubElement(enabled, "filename").text = RULES_CSV
            (config / RULES_CSV).write_text("".join(",".join(row) + "\n" for row in csv_rows))
        if mutate is not None:
            mutate(xml_root, config)
        tree.write(config / sx.XML_NAME, encoding="utf-8", xml_declaration=True)
        record_physicell(self.root, source, config / sx.XML_NAME, session, capture_id)
        if invocation:
            self.invocation(session, digest=digest)
        return f"runs/multicellular-configurator/{session}/configs/{capture_id}"

    def invocation(self, session=AFTER, agent="agent-1", digest=None, projection=PROJ):
        path = self.root / f"runs/multicellular-configurator/{session}/invocation/{agent}/task.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"projection": projection, "sha256": digest or sha(self.root / PROJ), "approval": "proj-1",
                   "record_session_id": SESSION}
        path.write_text('STAGE_GATE {"upstream_session_id": "maboss-session", "outputs": ["A", "B"]}\n'
                        f"DATA_INPUT {json.dumps(payload)}\nApply the approved projection.\n")
        return path

    def verify(self, after):
        path = ve.verify(self.root, PROJ, after)
        return path, json.loads(path.read_text())

    def by_id(self, result):
        return {e["entry_id"]: e for e in result["entries"]}


class BriefTests(VerifyBase):
    def test_all_applied_passes(self):
        after = self.export()
        path, result = self.verify(after)
        self.assertEqual(path, self.root / OUTPUT)
        self.assertTrue(result["passed"], result)
        self.assertEqual(result["kind"], "physicell_verification")
        self.assertEqual(result["schema_version"], 1)
        self.assertEqual(result["projection"], {"path": PROJ, "sha256": sha(self.root / PROJ)})
        self.assertEqual(result["before"]["path"], self.capture)
        self.assertEqual(result["before"]["session"], SESSION)
        self.assertEqual(result["after"]["path"], after)
        self.assertEqual(result["after"]["session"], AFTER)
        self.assertEqual(result["after"]["sha256"], sha(self.root / after / "capture.json"))
        self.assertEqual(result["lineage"], {"invocation": INVOCATION})
        self.assertEqual(result["unexpected_changes"], [])
        self.assertEqual([e["entry_id"] for e in result["entries"]], ["in1", "out1", "rule1", "set1"])
        self.assertTrue(all(e["ok"] for e in result["entries"]))
        self.assertAlmostEqual(self.by_id(result)["set1"]["expected"], 2e-5)

    def test_one_entry_missing_fails_and_is_named(self):
        entries = [e for e in self.entries() if e["entry_id"] != "set1"]
        _, result = self.verify(self.export(entries))
        self.assertFalse(result["passed"])
        entries = self.by_id(result)
        self.assertFalse(entries["set1"]["ok"])
        self.assertTrue(all(entries[k]["ok"] for k in ("in1", "out1", "rule1")))
        self.assertEqual(result["unexpected_changes"], [])

    def test_missing_link_fails(self):
        entries = [e for e in self.entries() if e["entry_id"] != "in1"]
        _, result = self.verify(self.export(entries))
        self.assertFalse(result["passed"])
        self.assertFalse(self.by_id(result)["in1"]["ok"])
        self.assertIsNone(self.by_id(result)["in1"]["found"])

    def test_extra_unrelated_change_fails_and_is_listed(self):
        def mutate(xml_root, config):
            xml_root.find("user_parameters/random_seed").text = "42"
        _, result = self.verify(self.export(mutate=mutate))
        self.assertFalse(result["passed"])
        self.assertTrue(all(e["ok"] for e in result["entries"]))
        paths = [c["path"] for c in result["unexpected_changes"]]
        self.assertEqual(paths, ["user_parameters/random_seed"])

    def test_order_and_whitespace_ignored(self):
        rule2 = copy.deepcopy(self.entries()[2])
        rule2.update(entry_id="rule2", signal="oxygen", direction="increases", behavior="necrosis",
                     half_max=q(5, "dimensionless"))
        data = self.projection(self.entries() + [rule2])
        self.write_projection(data)
        rows = [[f"  {v} " for v in rule_row(rule2)], rule_row(data["entries"][2])]

        def mutate(xml_root, config):
            cells = xml_root.find("cell_definitions")
            children = list(cells)
            for child in children:
                cells.remove(child)
            for child in reversed(children):
                cells.append(child)
            death = sx.resolve(xml_root, APOPTOSIS)
            death.text = f"\n\t  {death.text}  \n"
            ET.indent(xml_root, space="    ")
        _, result = self.verify(self.export(mutate=mutate, rows=rows))
        self.assertTrue(result["passed"], result)

    def test_rule_value_checked_in_csv(self):
        data = json.loads((self.root / PROJ).read_text())
        rule = data["entries"][2]
        row = rule_row(rule)
        row[5] = "1.5"  # half_max 1 in the projection
        _, result = self.verify(self.export(rows=[row]))
        self.assertFalse(result["passed"])
        entry = self.by_id(result)["rule1"]
        self.assertFalse(entry["ok"])
        self.assertEqual(entry["expected"]["half_max"], 1.0)
        self.assertEqual(entry["found"], row)

    def test_rule_value_within_tolerance_passes(self):
        data = json.loads((self.root / PROJ).read_text())
        row = rule_row(data["entries"][2])
        row[5] = "1.0000000000001"
        _, result = self.verify(self.export(rows=[row]))
        self.assertTrue(result["passed"], result)

    def test_apply_to_dead_must_be_zero(self):
        data = json.loads((self.root / PROJ).read_text())
        row = rule_row(data["entries"][2])
        row[7] = "1"
        _, result = self.verify(self.export(rows=[row]))
        self.assertFalse(self.by_id(result)["rule1"]["ok"])


class RulingTests(VerifyBase):
    def test_lineage_missing_refused(self):
        after = self.export(invocation=False)
        with self.assertRaisesRegex(ValueError, "invocation") as caught:
            ve.verify(self.root, PROJ, after)
        self.assertIn(PROJ, str(caught.exception))
        self.assertFalse((self.root / OUTPUT).exists())

    def test_lineage_wrong_sha_refused(self):
        after = self.export(digest="0" * 64)
        with self.assertRaisesRegex(ValueError, "sha256"):
            ve.verify(self.root, PROJ, after)
        self.assertFalse((self.root / OUTPUT).exists())

    def test_lineage_other_projection_refused(self):
        after = self.export(invocation=False)
        self.invocation(projection="runs/orchestrator/p2/physicell_projection.json")
        with self.assertRaisesRegex(ValueError, "invocation"):
            ve.verify(self.root, PROJ, after)

    def test_lineage_any_matching_record_passes(self):
        after = self.export(digest="0" * 64)
        self.invocation(agent="agent-2")
        _, result = self.verify(after)
        self.assertEqual(result["lineage"], {"invocation": INVOCATION.replace("agent-1", "agent-2")})
        self.assertTrue(result["passed"], result)

    def test_same_session_refused(self):
        after = self.export(session=SESSION, capture_id="cap-9")
        with self.assertRaisesRegex(ValueError, "session"):
            ve.verify(self.root, PROJ, after)

    def test_projection_check_errors_refused(self):
        after = self.export()
        data = json.loads((self.root / PROJ).read_text())
        data["entries"][0]["justification"] = "TODO"
        self.write_projection(data)
        with self.assertRaisesRegex(ValueError, "justification"):
            ve.verify(self.root, PROJ, after)

    def test_dependency_changed_fails(self):
        def mutate(xml_root, config):
            (config / "tumor.cfg").write_text("A.istate=1;")
        _, result = self.verify(self.export(mutate=mutate))
        self.assertFalse(result["passed"])
        self.assertTrue(all(e["ok"] for e in result["entries"]))
        changes = result["unexpected_changes"]
        self.assertEqual([c["path"] for c in changes], ["package/config/tumor.cfg"])
        self.assertEqual(changes[0]["kind"], "dependency")

    def test_steepness_null_but_int_in_xml_fails(self):
        data = json.loads((self.root / PROJ).read_text())
        data["entries"][1]["steepness"] = None
        self.write_projection(data)
        applied = copy.deepcopy(data["entries"])
        applied[1]["steepness"] = 10
        _, result = self.verify(self.export(applied))
        self.assertFalse(result["passed"])
        entry = self.by_id(result)["out1"]
        self.assertFalse(entry["ok"])
        self.assertNotIn("steepness", entry["expected"])
        self.assertEqual(entry["found"]["steepness"], "10")
        self.assertEqual(result["unexpected_changes"], [])

    def test_steepness_int_but_absent_in_xml_fails(self):
        applied = self.entries()
        applied[1]["steepness"] = None
        _, result = self.verify(self.export(applied))
        self.assertFalse(self.by_id(result)["out1"]["ok"])

    def test_steepness_must_be_exact_int(self):
        def mutate(xml_root, config):
            self.link_cell(xml_root, "tumor").find("output[@physicell_name='migration speed']/settings/steepness") \
                .text = "10.0"
        _, result = self.verify(self.export(mutate=mutate))
        self.assertFalse(self.by_id(result)["out1"]["ok"])

    def test_link_threshold_checked(self):
        def mutate(xml_root, config):
            self.link_cell(xml_root, "tumor").find("input[@physicell_name='oxygen']/settings/threshold").text = "11"
        _, result = self.verify(self.export(mutate=mutate))
        entry = self.by_id(result)["in1"]
        self.assertFalse(entry["ok"])
        self.assertEqual(entry["found"]["threshold"], "11")
        self.assertEqual(result["unexpected_changes"], [])

    def test_extra_csv_row_fails(self):
        data = json.loads((self.root / PROJ).read_text())
        extra = ["tumor", "oxygen", "increases", "necrosis", "0.1", "5", "2", "0"]
        _, result = self.verify(self.export(rows=[rule_row(data["entries"][2]), extra]))
        self.assertFalse(result["passed"])
        self.assertTrue(self.by_id(result)["rule1"]["ok"])
        changes = result["unexpected_changes"]
        self.assertEqual(len(changes), 1)
        self.assertEqual((changes[0]["kind"], changes[0]["after"]), ("rules_csv", extra))

    def test_ruleset_without_rule_entries_fails(self):
        data = self.projection([e for e in self.entries() if e["kind"] != "rule"])
        self.write_projection(data)
        _, result = self.verify(self.export(rows=[], ruleset=True))
        self.assertFalse(result["passed"])
        self.assertEqual([c["kind"] for c in result["unexpected_changes"]], ["cell_rules"])

    def test_rule_entries_without_ruleset_fail(self):
        _, result = self.verify(self.export(rows=[]))
        self.assertFalse(result["passed"])
        self.assertFalse(self.by_id(result)["rule1"]["ok"])
        self.assertIn("cell_rules", [c["kind"] for c in result["unexpected_changes"]])

    def test_setting_wrong_value_fails_once(self):
        def mutate(xml_root, config):
            sx.resolve(xml_root, APOPTOSIS).text = "3e-05"
        _, result = self.verify(self.export(mutate=mutate))
        entry = self.by_id(result)["set1"]
        self.assertFalse(entry["ok"])
        self.assertEqual(entry["found"], "3e-05")
        self.assertEqual(result["unexpected_changes"], [])

    def test_setting_units_change_is_unexpected(self):
        def mutate(xml_root, config):
            sx.resolve(xml_root, APOPTOSIS).set("units", "1/h")
        _, result = self.verify(self.export(mutate=mutate))
        self.assertFalse(result["passed"])
        self.assertEqual([c["path"] for c in result["unexpected_changes"]], [APOPTOSIS])

    def test_one_file_per_attempt_never_overwritten(self):
        after = self.export()
        self.verify(after)
        before = (self.root / OUTPUT).read_bytes()
        with self.assertRaisesRegex(ValueError, "exists"):
            ve.verify(self.root, PROJ, after)
        self.assertEqual((self.root / OUTPUT).read_bytes(), before)
        _, result = self.verify(self.export(capture_id="cap-2", invocation=False))
        self.assertTrue((self.root / f"runs/orchestrator/p1/physicell_verification.{AFTER}.cap-2.json").is_file())
        self.assertTrue(result["passed"])


class PlaceholderTests(VerifyBase):
    """The server's stage-A XML carries a disabled placeholder ruleset; the enabled one is added before it."""

    def edit_capture(self, xml_root):
        rulesets = ET.SubElement(ET.SubElement(xml_root, "cell_rules"), "rulesets")
        placeholder = ET.SubElement(rulesets, "ruleset", {"protocol": "CBHG", "version": "3.0", "format": "csv",
                                                          "enabled": "false"})
        ET.SubElement(placeholder, "folder").text = "./config"
        ET.SubElement(placeholder, "filename").text = "cell_rules.csv"

    def test_ruleset_positional_shift_not_reported(self):
        after = self.export()
        before_xml = sx.load(self.root / self.capture / "package/config" / sx.XML_NAME)
        after_xml = sx.load(self.root / after / "package/config" / sx.XML_NAME)
        raw = [d["path"] for d in sx.diff(before_xml, after_xml)]
        self.assertIn("cell_rules/rulesets/ruleset", raw)  # the normaliser sees a shift
        _, result = self.verify(after)
        self.assertTrue(result["passed"], result)

    def test_disabled_ruleset_changed_fails(self):
        def mutate(xml_root, config):
            placeholder = next(r for r in xml_root.findall("cell_rules/rulesets/ruleset")
                               if r.get("enabled") == "false")
            placeholder.find("filename").text = "other.csv"
        _, result = self.verify(self.export(mutate=mutate))
        self.assertFalse(result["passed"])
        self.assertEqual([c["kind"] for c in result["unexpected_changes"]], ["cell_rules"])


class CliTests(VerifyBase):
    def run_cli(self, after):
        with mock.patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": str(self.root)}):
            with contextlib.redirect_stdout(io.StringIO()) as out, \
                    contextlib.redirect_stderr(io.StringIO()) as err:
                code = ve.main([PROJ, after])
        return code, out.getvalue() + err.getvalue()

    def test_passed_exits_0(self):
        code, text = self.run_cli(self.export())
        self.assertEqual(code, 0)
        self.assertIn(OUTPUT, text)

    def test_failed_exits_1_and_says_why(self):
        entries = [e for e in self.entries() if e["entry_id"] != "set1"]

        def mutate(xml_root, config):
            xml_root.find("user_parameters/random_seed").text = "42"
        code, text = self.run_cli(self.export(entries, mutate=mutate))
        self.assertEqual(code, 1)
        self.assertIn("set1", text)
        self.assertIn("user_parameters/random_seed", text)
        self.assertIn(OUTPUT, text)

    def test_refusal_exits_2_and_says_why(self):
        code, text = self.run_cli(self.export(invocation=False))
        self.assertEqual(code, 2)
        self.assertIn("invocation", text)


if __name__ == "__main__":
    unittest.main()
