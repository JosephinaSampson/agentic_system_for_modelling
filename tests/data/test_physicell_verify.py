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
from tests.data.test_physicell_design import STAGE_A, ensure_approval
from tests.data.test_physicell_projection import (APOPTOSIS, PROJ, SRC, ProjectionBase, assert_read_only_by_bindings,
                                                  binding_reads, q, sha)

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


def placeholder_rules(xml_root):
    """The server's stage-A cell_rules (probe, server 2.5.2): a disabled placeholder ruleset and empty settings."""
    rules = ET.SubElement(xml_root, "cell_rules")
    rulesets = ET.SubElement(rules, "rulesets")
    placeholder = ET.SubElement(rulesets, "ruleset", {"protocol": "CBHG", "version": "3.0", "format": "csv",
                                                      "enabled": "false"})
    ET.SubElement(placeholder, "folder").text = "./config"
    ET.SubElement(placeholder, "filename").text = "cell_rules.csv"
    ET.SubElement(rules, "settings")


class VerifyBase(ProjectionBase):
    """The stage-A capture (cap-2 of the stage-A session mc-stage-a, the server's placeholder cell_rules, no enabled
    rules) and an approved-shape projection over it; `export` simulates the stage-B configurator run: a new session's
    capture with the applied entries and its invocation."""

    def edit_capture(self, xml_root):
        placeholder_rules(xml_root)

    def setUp(self):
        super().setUp()
        self.write_projection(self.projection())

    def link_cell(self, xml_root, cell):
        intracellular = xml_root.find(f"cell_definitions/cell_definition[@name='{cell}']/phenotype/intracellular")
        mapping = intracellular.find("mapping")
        return ET.SubElement(intracellular, "mapping") if mapping is None else mapping

    def tamper(self, after, change):
        """Change the recorded package XML of capture `after` and re-hash it in its capture.json (a forged
        capture: the recorder relocates folders, so some server outputs cannot be produced through it)."""
        folder = self.root / after
        xml = folder / "package/config" / sx.XML_NAME
        tree = ET.parse(xml)
        change(tree.getroot())
        tree.write(xml, encoding="utf-8", xml_declaration=True)
        manifest = json.loads((folder / "capture.json").read_text())
        manifest["package_xml_sha256"] = sha(xml)
        (folder / "capture.json").write_text(json.dumps(manifest, indent=2) + "\n")
        return after

    def link(self, xml_root, tag="input", name="oxygen"):
        return self.link_cell(xml_root, "tumor").find(f"{tag}[@physicell_name='{name}']")

    def enabled_ruleset(self, xml_root):
        return next(r for r in xml_root.findall("cell_rules/rulesets/ruleset") if r.get("enabled") == "true")

    def assertEntryFails(self, result, entry_id, *patterns):
        entry = self.by_id(result)[entry_id]
        self.assertFalse(result["passed"])
        self.assertFalse(entry["ok"], entry)
        for pattern in patterns:
            self.assertIn(pattern, entry["problem"])
        return entry

    def assertCellRulesChange(self, result, *patterns):
        self.assertFalse(result["passed"])
        self.assertTrue(all(e["ok"] for e in result["entries"]), result["entries"])
        text = json.dumps(result["unexpected_changes"])
        for pattern in patterns:
            self.assertIn(pattern, text)

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

    def invocation(self, session=AFTER, agent="agent-1", digest=None, projection=PROJ, approval=None,
                   record_session_id=STAGE_A):
        """The stage-B run's task.txt: line 2 names the projection with a physicell_projection approval of its
        current bytes (recorded when there is none, as the researcher does before the dispatch)."""
        path = self.root / f"runs/multicellular-configurator/{session}/invocation/{agent}/task.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        actual = sha(self.root / projection) if (self.root / projection).is_file() else None
        if approval is None and digest in (None, actual) and actual is not None:
            ids = [e["entry_id"] for e in json.loads((self.root / projection).read_text())["entries"]]
            approval = ensure_approval(self.root, "physicell_projection", projection, STAGE_A, ids)
        payload = {"projection": projection, "sha256": digest or actual, "approval": approval or "proj-unrecorded",
                   "record_session_id": record_session_id}
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
        # before/after are the settings_xml.capture bindings verbatim (build.py re-binds `after` and compares)
        self.assertEqual(result["before"], sx.capture(self.root, self.capture))
        self.assertEqual(result["before"]["session"], STAGE_A)
        self.assertEqual(result["after"], sx.capture(self.root, after))
        self.assertEqual(result["after"]["session"], AFTER)
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
        after = self.export(session=STAGE_A, capture_id="cap-9", invocation=False)
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
    """The server's stage-A XML carries a disabled placeholder ruleset; the enabled one is added after it."""

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


class LinkStructureTests(VerifyBase):
    """Ruling T6-e (I-1, M-4): an added link is exactly {physicell_name, intracellular_name} with one settings child,
    and nothing under it carries attributes; a mapping the entries create carries none either."""

    def test_extra_attribute_on_input_fails(self):
        _, result = self.verify(self.export(mutate=lambda x, c: self.link(x).set("extra", "y")))
        self.assertEntryFails(result, "in1", "attributes", "extra")

    def test_extra_child_beside_settings_fails(self):
        _, result = self.verify(self.export(mutate=lambda x, c: ET.SubElement(self.link(x), "foo")))
        self.assertEntryFails(result, "in1", "children", "foo")

    def test_units_on_threshold_fails(self):
        _, result = self.verify(self.export(
            mutate=lambda x, c: self.link(x).find("settings/threshold").set("units", "mmHg")))
        self.assertEntryFails(result, "in1", "threshold", "units")

    def test_attribute_on_settings_fails(self):
        _, result = self.verify(self.export(
            mutate=lambda x, c: self.link(x).find("settings").set("enabled", "false")))
        self.assertEntryFails(result, "in1", "settings", "enabled")

    def test_attribute_on_output_value_fails(self):
        _, result = self.verify(self.export(
            mutate=lambda x, c: self.link(x, "output", "migration speed").find("settings/value").set("units", "x")))
        self.assertEntryFails(result, "out1", "value", "units")

    def duplicate(self, tag, name, child):
        def mutate(xml_root, config):
            settings = self.link(xml_root, tag, name).find("settings")
            settings.append(copy.deepcopy(settings.find(child)))
        return mutate

    def test_duplicate_threshold_fails(self):
        _, result = self.verify(self.export(mutate=self.duplicate("input", "oxygen", "threshold")))
        self.assertEntryFails(result, "in1", "duplicate", "threshold")

    def test_duplicate_action_fails(self):
        _, result = self.verify(self.export(mutate=self.duplicate("output", "migration speed", "action")))
        self.assertEntryFails(result, "out1", "duplicate", "action")

    def test_duplicate_steepness_fails(self):
        _, result = self.verify(self.export(mutate=self.duplicate("output", "migration speed", "steepness")))
        self.assertEntryFails(result, "out1", "duplicate", "steepness")

    def test_link_without_settings_fails(self):
        def mutate(xml_root, config):
            link = self.link(xml_root)
            link.remove(link.find("settings"))
        _, result = self.verify(self.export(mutate=mutate))
        entry = self.assertEntryFails(result, "in1", "cannot be compared")
        self.assertIsNone(entry["found"])

    def test_duplicate_link_fails(self):
        def mutate(xml_root, config):
            self.link_cell(xml_root, "tumor").append(copy.deepcopy(self.link(xml_root)))
        _, result = self.verify(self.export(mutate=mutate))
        entry = self.assertEntryFails(result, "in1", "2 matching")
        self.assertEqual(len(entry["found"]), 2)

    def test_link_on_wrong_cell_fails(self):
        def mutate(xml_root, config):
            link = self.link(xml_root)
            self.link_cell(xml_root, "tumor").remove(link)
            phenotype = xml_root.find("cell_definitions/cell_definition[@name='default']/phenotype")
            ET.SubElement(ET.SubElement(phenotype, "intracellular", {"type": "maboss"}), "mapping").append(link)
        _, result = self.verify(self.export(mutate=mutate))
        entry = self.assertEntryFails(result, "in1", "0 matching")
        self.assertIsNone(entry["found"])
        self.assertTrue(any("name='default'" in c["path"] for c in result["unexpected_changes"]))


class CreatedMappingTests(VerifyBase):
    """The stage-A tumor has a MaBoSS model but no mapping block (the probe's capA): the entries create it."""

    def edit_capture(self, xml_root):
        super().edit_capture(xml_root)
        intracellular = xml_root.find("cell_definitions/cell_definition[@name='tumor']/phenotype/intracellular")
        intracellular.remove(intracellular.find("mapping"))

    def test_created_mapping_passes(self):
        _, result = self.verify(self.export())
        self.assertTrue(result["passed"], result)

    def test_created_mapping_with_attribute_fails(self):
        _, result = self.verify(self.export(mutate=lambda x, c: self.link_cell(x, "tumor").set("mode", "x")))
        self.assertEntryFails(result, "in1", "mapping", "mode")
        self.assertEntryFails(result, "out1", "mapping", "mode")


class RulesetContentTests(VerifyBase):
    """Ruling T6-e (I-2): only ruleset elements leave the XML diff; rulesets are compared by full content."""

    def test_enabled_folder_elsewhere_fails(self):
        after = self.tamper(self.export(), lambda x: setattr(self.enabled_ruleset(x).find("folder"), "text",
                                                             "./elsewhere"))
        _, result = self.verify(after)
        self.assertCellRulesChange(result, "./elsewhere")

    def test_enabled_version_fails(self):
        _, result = self.verify(self.export(mutate=lambda x, c: self.enabled_ruleset(x).set("version", "2.0")))
        self.assertCellRulesChange(result, "2.0")

    def test_enabled_extra_attribute_fails(self):
        _, result = self.verify(self.export(mutate=lambda x, c: self.enabled_ruleset(x).set("note", "n")))
        self.assertCellRulesChange(result, "note")

    def test_enabled_extra_child_fails(self):
        _, result = self.verify(self.export(
            mutate=lambda x, c: ET.SubElement(self.enabled_ruleset(x), "comment").__setattr__("text", "c")))
        self.assertCellRulesChange(result, "comment")

    def test_cell_rules_settings_child_fails(self):
        def mutate(xml_root, config):
            ET.SubElement(xml_root.find("cell_rules/settings"), "foo").text = "1"
        _, result = self.verify(self.export(mutate=mutate))
        self.assertCellRulesChange(result, "cell_rules/settings/foo")

    def test_rulesets_attribute_fails(self):
        _, result = self.verify(self.export(mutate=lambda x, c: x.find("cell_rules/rulesets").set("a", "b")))
        self.assertCellRulesChange(result, "cell_rules/rulesets")

    def test_disabled_protocol_changed_fails(self):
        def mutate(xml_root, config):
            next(r for r in xml_root.findall("cell_rules/rulesets/ruleset") if r.get("enabled") == "false") \
                .set("protocol", "X")
        _, result = self.verify(self.export(mutate=mutate))
        self.assertCellRulesChange(result, "disabled")


class NoStageACellRulesTests(VerifyBase):
    """A stage-A capture without cell_rules (the server always writes one): the cell_rules the run adds are a
    difference outside the ruleset elements, so the verification fails closed."""

    def edit_capture(self, xml_root):
        pass

    def test_added_cell_rules_block_fails(self):
        _, result = self.verify(self.export())
        self.assertCellRulesChange(result, "cell_rules")


class StrictNumberTests(VerifyBase):
    """Ruling T6-e (M-1, M-5): numbers are ASCII decimals; NaN and infinities never match."""

    def threshold(self, text):
        def mutate(xml_root, config):
            self.link(xml_root).find("settings/threshold").text = text
        return mutate

    def test_underscore_and_fullwidth_refused_in_xml(self):
        for text in ("1_0", "\uff11\uff10", "nan", "inf", "-inf", "10e", ".", "0x0a"):
            with self.subTest(text=text):
                self.tearDown_and_setUp()
                _, result = self.verify(self.export(mutate=self.threshold(text)))
                self.assertEntryFails(result, "in1", "threshold")

    def test_ascii_decimals_accepted(self):
        for text in ("10", "10.", "10.0", "+10", "1e1", "1.0E+1", " 10 "):
            with self.subTest(text=text):
                self.tearDown_and_setUp()
                _, result = self.verify(self.export(mutate=self.threshold(text)))
                self.assertTrue(result["passed"], result)

    def test_nan_inf_and_underscore_refused_in_csv(self):
        data = json.loads((self.root / PROJ).read_text())
        for text in ("nan", "inf", "1_0", "\uff11"):
            with self.subTest(text=text):
                self.tearDown_and_setUp()
                row = rule_row(data["entries"][2])
                row[6] = text  # hill_power 4
                _, result = self.verify(self.export(rows=[row]))
                self.assertEntryFails(result, "rule1")

    def test_nan_setting_refused(self):
        _, result = self.verify(self.export(mutate=lambda x, c: setattr(sx.resolve(x, APOPTOSIS), "text", "nan")))
        self.assertEntryFails(result, "set1")

    def tearDown_and_setUp(self):
        """A fresh project per subtest: each export records a capture that can exist only once."""
        self.doCleanups()
        self.setUp()


class BuildHandoffTests(VerifyBase):
    """Ruling T9-d: build.py never trusts the verification file; it recomputes the verification."""

    def test_passed_verification_is_accepted_by_build(self):
        from scripts.physicell import build
        path, result = self.verify(self.export())
        rel, data, raw, _ = build._verification(self.root, OUTPUT)
        self.assertEqual((rel, data), (OUTPUT, result))

    def test_no_capture_file_is_read_outside_a_binding(self):
        # PR #17 review 4587427664: compute binds with sx.capture_tree (verify_export._bind) and uses the binding's
        # manifest_sha256; build._verification binds with capture_tree and _physiboss uses that tree
        from scripts.physicell import build
        after = self.export()
        self.verify(after)
        with binding_reads(self.root) as (binds, reads):
            ve.compute(self.root, PROJ, after)
        self.assertEqual(binds[after], 1)
        assert_read_only_by_bindings(self, binds, reads)
        with binding_reads(self.root) as (binds, reads):
            rel, data, raw, tree = build._verification(self.root, OUTPUT)
            self.assertTrue(build._physiboss(tree))  # the stage-A design carries a MaBoSS model
        self.assertEqual(binds[after], 2)  # the file's binding and the recomputation (ruling T9-d), nothing else
        assert_read_only_by_bindings(self, binds, reads)

    def forge(self, after, **changes):
        """A verification file written by hand for `after`: passed true and the real after binding."""
        bound = sx.capture(self.root, after)
        data = {"kind": "physicell_verification", "schema_version": 1,
                "projection": {"path": PROJ, "sha256": sha(self.root / PROJ)},
                "before": sx.capture(self.root, self.capture), "after": bound,
                "lineage": {"invocation": INVOCATION}, "entries": [], "unexpected_changes": [], "passed": True}
        data.update(changes)
        path = ve.target(self.root, PROJ, bound)
        path.write_text(json.dumps(data, indent=2) + "\n")
        return path.relative_to(self.root).as_posix()

    def test_forged_passed_file_with_real_after_binding_refused(self):
        from scripts.physicell import build
        after = self.export([e for e in self.entries() if e["entry_id"] != "set1"])  # set1 not applied: fails
        self.assertFalse(ve.compute(self.root, PROJ, after)["passed"])
        with self.assertRaisesRegex(ValueError, "re-verification"):
            build._verification(self.root, self.forge(after))

    def test_forged_file_matching_a_passing_capture_but_not_its_report_refused(self):
        from scripts.physicell import build
        after = self.export()
        with self.assertRaisesRegex(ValueError, "entries differ from a re-verification"):
            build._verification(self.root, self.forge(after))

    def test_stale_file_whose_projection_no_longer_checks_refused(self):
        from scripts import state
        from scripts.physicell import build
        from tests.data.test_physicell_design import MABOSS
        self.verify(self.export())
        state.update(self.root, "registry-supersede", {"session_id": MABOSS})  # the design no longer checks
        with self.assertRaisesRegex(ValueError, "cannot be re-verified.*projection .* fails its check"):
            build._verification(self.root, OUTPUT)


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


class ProjectionLineageTests(VerifyBase):
    """PR #17 review 4586111748: the stage-B lineage needs a real physicell_projection approval of exactly this
    projection, its record_session_id and a line-1 STAGE_GATE."""

    def test_unverified_dispatch_is_not_lineage(self):
        after = self.export(invocation=False)
        for name, kw, first in (("bogus approval", {"approval": "bogus"}, None),
                                ("design approval", {"approval": "design-1"}, None),
                                ("wrong record_session_id", {"record_session_id": "x"}, None),
                                ("no STAGE_GATE", {}, "Apply the approved projection.")):
            with self.subTest(case=name):
                path = self.invocation(**kw)
                if first is not None:
                    lines = path.read_text().splitlines()
                    path.write_text("\n".join([first, *lines[1:]]) + "\n")
                with self.assertRaisesRegex(ValueError, "lineage: no invocation record"):
                    ve.compute(self.root, PROJ, after)
        self.invocation()
        self.assertTrue(ve.compute(self.root, PROJ, after)["passed"])

    def test_stage_gate_bound_to_the_projection_design(self):
        # PR #17 review 4587425869: the line-1 STAGE_GATE of a projection dispatch must name its design's MaBoSS
        # upstream; standalone is refused for a design importing MaBoSS handoffs
        after = self.export(invocation=False)
        for first, reason in (('STAGE_GATE {"upstream_session_id": "maboss-unrelated", "outputs": ["A"]}',
                               "upstream_session_id maboss-unrelated"),
                              ('STAGE_GATE {"standalone_decision_id": "never-recorded", "outputs": ["zzz"]}',
                               "may import no MaBoSS handoff"),
                              # PR #17 review 4588363532: the upstream gate's outputs are checked too
                              ('STAGE_GATE {"upstream_session_id": "maboss-session", "outputs": ["zzz"]}',
                               r"STAGE_GATE outputs \['zzz'\] are not the outputs \['A', 'B'\] of approval")):
            with self.subTest(gate=first):
                path = self.invocation()
                lines = path.read_text().splitlines()
                path.write_text("\n".join([first, *lines[1:]]) + "\n")
                with self.assertRaisesRegex(ValueError, f"lineage: no invocation record.*{reason}"):
                    ve.compute(self.root, PROJ, after)
        self.invocation()
        self.assertTrue(ve.compute(self.root, PROJ, after)["passed"])


if __name__ == "__main__":
    unittest.main()
