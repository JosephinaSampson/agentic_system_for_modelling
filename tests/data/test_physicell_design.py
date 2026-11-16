from __future__ import annotations

import copy
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from scripts import state
from scripts.capture.record_physicell_config import record_physicell
from scripts.data import approvals
from scripts.physicell import physicell_design as pd

FIXTURES = Path(__file__).parent / "fixtures" / "physicell"
SESSION = "mc-session-1"
CAPTURE = "cap-1"
MABOSS = "maboss-session"
REL = "runs/orchestrator/p1/physicell_design.json"
RULES_REFUSAL = "the PhysiCell server (2.5.2) cannot reload a captured rules CSV"


def write_config(folder, rules=False):
    """The fixture PhysiCell_settings.xml in folder (with cell_rules.csv), its enabled rules ruleset removed unless
    `rules`: server 2.5.2 cannot reload a captured rules CSV, so designs and projections build on rule-free captures."""
    folder.mkdir(parents=True, exist_ok=True)
    text = (FIXTURES / "PhysiCell_settings.xml").read_text()
    if not rules:
        start, end = text.index("<cell_rules>"), text.index("</cell_rules>") + len("</cell_rules>")
        text = text[:start] + text[end:]
    (folder / "PhysiCell_settings.xml").write_text(text)
    shutil.copy(FIXTURES / "cell_rules.csv", folder)
    return folder / "PhysiCell_settings.xml"


def file_ref(root, path, server, session, role):
    return {"path": str(path.relative_to(root)), "server": server, "session_id": session, "role": role,
            "name": path.name, "suffix": path.suffix, "media_type": "text/plain",
            "size_bytes": path.stat().st_size, "sha256": state.digest(path)}


class Base(unittest.TestCase):
    def setUp(self):
        cache = Path.home() / ".cache"
        tmp = tempfile.TemporaryDirectory(dir=cache if cache.is_dir() else None)
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        self.root = base / "project"
        self.root.mkdir()
        source = base / "source"
        record_physicell(self.root, source, write_config(source / "config"), SESSION, CAPTURE)
        self.capture = f"runs/multicellular-configurator/{SESSION}/configs/{CAPTURE}"
        for name in ("CURRENT_STATE.md", "DECISIONS.md", "VALIDATION_PLAN.md"):
            shutil.copyfile(state.ROOT / "templates" / name, self.root / name)
        self.handoff = self.make_handoff()

    def make_handoff(self, session=MABOSS, nodes=("A", "B"), export=True, cell_type="tumor", outputs=None):
        outputs = list(nodes if outputs is None else outputs)
        folder = self.root / "runs/boolean-dynamics-modeler" / session
        folder.mkdir(parents=True)
        bnd = folder / "model.bnd"
        bnd.write_text("".join(f"Node {n} {{ logic = {n}; }}\n" for n in nodes))
        cfg = folder / "model.cfg"
        cfg.write_text("A.istate=0;")
        manifest = {"schema_name": "mcp-biomodelling-handoff", "schema_version": "1.0",
                    "handoff_type": "maboss-to-physicell", "source": {"server": "MaBoSS", "session_id": session},
                    "network": {"nodes": list(nodes), "output_nodes": outputs},
                    "bnd_file": file_ref(self.root, bnd, "MaBoSS", session, "maboss_bnd"),
                    "cfg_file": file_ref(self.root, cfg, "MaBoSS", session, "maboss_cfg"),
                    "lineage": [], "simulation": {}, "target": {"cell_type": cell_type}}
        path = folder / "handoff.json"
        path.write_text(json.dumps(manifest))
        ref = {"path": str(path.relative_to(self.root)), "sha256": state.digest(path)}
        review = {"id": f"review-{session}", "kind": "stage_review", "session_id": session, "stage": "boolean-dynamics",
                  "outcome": "approved", "artifact": ref}
        for role in ("scientific-reviewer", "reproducibility-auditor"):
            report = self.root / "runs" / f"{role}-{session}.md"
            report.write_text("independent bounded review")
            review[role] = {"path": str(report.relative_to(self.root)), "sha256": state.digest(report)}
        approval = {"id": f"approval-{session}", "kind": "stage_approval", "session_id": session,
                    "stage": "boolean-dynamics", "downstream_stage": "multicellular", "outputs": outputs,
                    "approved_by": "researcher", "authorization": "approved", "artifact": ref}
        state.update(self.root, "registry-add", dict(zip(state.REGISTRY, [
            "boolean-dynamics-modeler", session, "boolean-dynamics", "active", "—", "pending", ref["path"]])))
        state.update(self.root, "record", review)
        state.update(self.root, "record", approval)
        if export:
            state.update(self.root, "registry-handoff", {"session_id": session, "handoff": "approved"})
            state.update(self.root, "registry-handoff", {"session_id": session, "handoff": "exported", "artifact": ref})
        return ref

    def input_source(self):
        (self.root / "inputs").mkdir(exist_ok=True)
        (self.root / "inputs/paper.txt").write_text("paper")
        return [("s1", "inputs/paper.txt")]

    def complete(self):
        """A passing design built on the capture basis."""
        (self.root / REL).unlink(missing_ok=True)
        path = pd.draft(self.root, "p1", basis_capture=self.capture, sources=self.input_source(), data=[])
        data = json.loads(path.read_text())
        for item in data["cell_definitions"]:
            item["description"] = f"existing {item['name']}"
        data["cell_definitions"].append({"name": "tumor_hypoxic", "cycle_model": "Live", "description": "hypoxic variant",
                                         "from": {"source": "s1", "locator": "Fig. 2"}, "variant_of": "tumor"})
        for item in data["substrates"]:
            item["description"] = f"existing {item['name']}"
        data["intracellular"] = [{"cell_definition": "tumor", "maboss": {"handoff": self.handoff["path"],
                                                                         "sha256": self.handoff["sha256"]}}]
        data["coupling_plan"] = [{"cell_definition": "tumor", "behavior_group": "apoptosis", "route": "physiboss",
                                  "notes": "A drives apoptosis"},
                                 {"cell_definition": "tumor_hypoxic", "behavior_group": "motility", "route": "rules",
                                  "notes": "oxygen-dependent speed"}]
        data["cycle_models"] = ["Live"]
        return data

    def write(self, data, rel=REL):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2))
        return path

    def errors(self, data):
        self.write(data)
        return pd.check(self.root, REL)

    def assertRefused(self, data, pattern):
        errors = self.errors(data)
        self.assertTrue(any(pattern in e for e in errors), (pattern, errors))


class DraftTests(Base):
    def test_draft_prefills_from_basis_and_never_overwrites(self):
        (self.root / REL).unlink(missing_ok=True)
        path = pd.draft(self.root, "p1", basis_capture=self.capture, sources=self.input_source(), data=[])
        data = json.loads(path.read_text())
        self.assertEqual(data["kind"], "physicell_design")
        self.assertEqual(data["basis"], {"capture": self.capture})
        self.assertEqual([c["name"] for c in data["cell_definitions"]], ["default", "tumor"])
        self.assertEqual(data["cell_definitions"][1]["cycle_model"], "Flow cytometry model (separated)")
        self.assertEqual([s["name"] for s in data["substrates"]], ["oxygen", "drug"])
        self.assertEqual(data["substrates"][0]["units"], "mmHg")
        self.assertEqual(data["cell_definitions"][0]["description"], "TODO")
        self.assertEqual(data["session"], SESSION)
        with self.assertRaisesRegex(ValueError, "never overwrite"):
            pd.draft(self.root, "p1", basis_capture=self.capture, sources=[], data=[])

    def test_draft_without_basis_has_domain_skeleton(self):
        path = pd.draft(self.root, "p2", basis_capture=None, sources=[], data=[], session="mc-new")
        data = json.loads(path.read_text())
        self.assertIsNone(data["basis"])
        self.assertEqual(data["domain"]["units"], "micron")
        self.assertEqual(data["domain"]["time_units"], "min")
        self.assertIsNone(data["domain"]["x_min"])
        self.assertIsNone(data["cycle_models"])
        errors = pd.check(self.root, "runs/orchestrator/p2/physicell_design.json")
        self.assertTrue(any("record the server's cycle models first" in e for e in errors), errors)


class CheckTests(Base):
    def test_complete_design_passes(self):
        self.assertEqual(self.errors(self.complete()), [])

    def test_duplicate_cell_definition_refused(self):
        data = self.complete()
        data["cell_definitions"].append(copy.deepcopy(data["cell_definitions"][0]))
        self.assertRefused(data, "duplicate cell definition")

    def test_unknown_cycle_model_refused(self):
        data = self.complete()
        data["cell_definitions"][2]["cycle_model"] = "Nonsense"
        self.assertRefused(data, "cycle model")

    def test_coupling_plan_undeclared_cell_definition_refused(self):
        data = self.complete()
        data["coupling_plan"][0]["cell_definition"] = "ghost"
        self.assertRefused(data, "ghost")

    def test_pending_maboss_handoff_refused(self):
        data = self.complete()
        ref = self.make_handoff("maboss-pending", export=False)
        data["intracellular"][0]["maboss"] = {"handoff": ref["path"], "sha256": ref["sha256"]}
        self.assertRefused(data, "exactly exported")

    def test_superseded_maboss_handoff_refused(self):
        data = self.complete()
        state.update(self.root, "registry-supersede", {"session_id": MABOSS})
        self.assertRefused(data, "active")

    def test_unapproved_maboss_handoff_refused(self):
        data = self.complete()
        data["intracellular"][0]["maboss"]["sha256"] = "0" * 64
        self.assertRefused(data, "handoff")

    def test_physiboss_route_needs_intracellular(self):
        data = self.complete()
        data["intracellular"] = []
        self.assertRefused(data, "physiboss")

    def test_no_basis_without_domain_refused(self):
        data = self.complete()
        data.update(basis=None, domain=None, session="mc-new", cycle_models=["Live", "Flow cytometry model (separated)"])
        for item in data["cell_definitions"]:
            item["from"] = {"source": "s1", "locator": "Fig. 2"}
        for item in data["substrates"]:
            item["from"] = {"source": "s1", "locator": "Fig. 2"}
        self.assertRefused(data, "domain")

    def test_domain_units_refused(self):
        data = self.complete()
        data.update(basis=None, session="mc-new", cycle_models=["Live", "Flow cytometry model (separated)"])
        for item in data["cell_definitions"] + data["substrates"]:
            item["from"] = {"source": "s1", "locator": "Fig. 2"}
        data["domain"] = {"x_min": -400, "x_max": 400, "y_min": -400, "y_max": 400, "z_min": -10, "z_max": 10,
                          "dx": 20, "dy": 20, "dz": 20, "units": "mm", "max_time": 100, "time_units": "hours"}
        errors = self.errors(data)
        self.assertTrue(any("micron" in e for e in errors), errors)
        self.assertTrue(any("min" in e for e in errors), errors)
        data["domain"].update(units="micron", time_units="min")
        self.assertEqual(self.errors(data), [])

    def test_todo_reason_refused(self):
        data = self.complete()
        data["cell_definitions"][0]["description"] = "TODO"
        self.assertRefused(data, "TODO")

    def test_todo_in_unmapped_and_notes_refused(self):
        data = self.complete()
        data["unmapped"] = [{"item": "fibroblast", "reason": "TODO"}]
        data["coupling_plan"][0]["notes"] = "todo"
        errors = self.errors(data)
        self.assertEqual(sum("TODO" in e for e in errors), 2, errors)

    def test_basis_name_must_exist_in_capture(self):
        data = self.complete()
        data["cell_definitions"][0]["name"] = "stromal"
        self.assertRefused(data, "not in the basis")

    def test_uncited_source_and_unknown_keys_refused(self):
        data = self.complete()
        data["cell_definitions"][2]["from"] = "basis"
        self.assertTrue(any("never cited" in e for e in self.errors(data)))
        data = self.complete()
        data["extra"] = 1
        self.assertRefused(data, "keys must be exactly")

    def test_changed_capture_refused(self):
        data = self.complete()
        xml = self.root / self.capture / "package/config/PhysiCell_settings.xml"
        xml.write_text(xml.read_text() + "\n")
        self.assertRefused(data, "basis")

    def test_basis_with_enabled_rules_csv_refused(self):
        # Ruling T5-d(a): load_xml_configuration keeps no rules CSV rows, and no tool reloads one
        source = self.root.parent / "source-rules"
        record_physicell(self.root, source, write_config(source / "config", rules=True), SESSION, "cap-rules")
        self.assertEqual(self.errors(self.complete()), [])
        data = self.complete()
        data["basis"]["capture"] = f"runs/multicellular-configurator/{SESSION}/configs/cap-rules"
        self.assertRefused(data, RULES_REFUSAL)
        self.assertRefused(data, "apply projections to the stage-A capture (no rules), and revise and re-apply a "
                                 "projection from there")

    def test_load_and_check_returns_hash_of_bytes_read(self):
        path = self.write(self.complete())
        data, digest, errors = pd.load_and_check(self.root, REL)
        self.assertEqual(errors, [])
        self.assertEqual(digest, state.digest(path))
        self.assertEqual(data["kind"], "physicell_design")

    def test_duplicate_json_keys_refused(self):
        (self.root / REL).parent.mkdir(parents=True, exist_ok=True)
        (self.root / REL).write_text('{"kind": "a", "kind": "b"}')
        with self.assertRaises(ValueError):
            pd.check(self.root, REL)


class HandoffBindingTests(Base):
    def test_target_cell_type_must_match_cell_definition(self):
        data = self.complete()
        ref = self.make_handoff("maboss-fibro", cell_type="fibroblast")
        data["intracellular"][0]["maboss"] = {"handoff": ref["path"], "sha256": ref["sha256"]}
        self.assertRefused(data, "re-export the MaBoSS handoff for that cell type")
        data["cell_definitions"][2]["name"] = "fibroblast"
        data["intracellular"][0]["cell_definition"] = "fibroblast"
        data["coupling_plan"] = [{"cell_definition": "fibroblast", "behavior_group": "g", "route": "physiboss",
                                  "notes": "n"}]
        data["cell_definitions"][2].pop("variant_of")
        self.assertEqual(self.errors(data), [])

    def second_artifact(self, session=MABOSS):
        """A second approved artifact (outputs A only) of the same MaBoSS session, review before approval."""
        folder = self.root / "runs/boolean-dynamics-modeler" / session
        manifest = json.loads((self.root / self.handoff["path"]).read_text())
        manifest["network"] = {"nodes": ["A"], "output_nodes": ["A"]}
        bnd = folder / "model2.bnd"
        bnd.write_text("Node A { logic = A; }\n")
        manifest["bnd_file"] = file_ref(self.root, bnd, "MaBoSS", session, "maboss_bnd")
        path = folder / "handoff2.json"
        path.write_text(json.dumps(manifest))
        ref = {"path": str(path.relative_to(self.root)), "sha256": state.digest(path)}
        review = json.loads(json.dumps(state.records((self.root / "DECISIONS.md").read_text())[0]))
        review.update(id="review-2", artifact=ref)
        state.update(self.root, "record", review)
        approval = {"id": "approval-2", "kind": "stage_approval", "session_id": session, "stage": "boolean-dynamics",
                    "downstream_stage": "multicellular", "outputs": ["A"], "approved_by": "researcher",
                    "authorization": "approved", "artifact": ref}
        state.update(self.root, "record", approval)
        return ref

    def test_named_artifact_of_session_with_two_approvals(self):
        data = self.complete()
        ref2 = self.second_artifact()
        # the later review covers handoff2 only: the originally named handoff is refused, handoff2 is used
        self.assertRefused(data, "different artifact")
        data["intracellular"][0]["maboss"] = {"handoff": ref2["path"], "sha256": ref2["sha256"]}
        self.assertEqual(self.errors(data), [])

    def test_reexport_in_new_session_supersedes_named_handoff(self):
        data = self.complete()
        ref = self.make_handoff("maboss-v2")
        state.update(self.root, "registry-supersede", {"session_id": MABOSS})
        self.assertRefused(data, "active")
        data["intracellular"][0]["maboss"] = {"handoff": ref["path"], "sha256": ref["sha256"]}
        self.assertEqual(self.errors(data), [])


class VariantTests(Base):
    def test_variant_cycles_refused(self):
        data = self.complete()
        data["cell_definitions"].append({"name": "t3", "cycle_model": "Live", "description": "d",
                                         "from": {"source": "s1", "locator": "Fig. 2"}, "variant_of": "tumor_hypoxic"})
        data["cell_definitions"][2]["variant_of"] = "t3"
        self.assertRefused(data, "cycle")
        data = self.complete()
        data["cell_definitions"][2]["variant_of"] = "tumor_hypoxic"
        self.assertRefused(data, "variant_of")


class ApprovalTests(Base):
    def approve(self, data=None, **overrides):
        data = data or self.complete()
        path = self.write(data)
        ref = {"path": REL, "sha256": state.digest(path)}
        record = {"id": "design-1", "kind": "stage_approval", "session_id": SESSION, "stage": "physicell_design",
                  "outputs": ["default", "tumor", "tumor_hypoxic", "oxygen", "drug"], "approved_by": "researcher",
                  "authorization": "approved", "artifact": ref}
        record.update(overrides)
        state.update(self.root, "record", record)
        return ref

    def test_approval_passes(self):
        ref = self.approve()
        record = approvals.require_physicell_design_approval(self.root, "design-1", ref["path"], ref["sha256"])
        self.assertEqual(record["id"], "design-1")

    def test_outputs_mismatch_refused(self):
        ref = self.approve(outputs=["default", "tumor", "oxygen"])
        with self.assertRaisesRegex(ValueError, "outputs"):
            approvals.require_physicell_design_approval(self.root, "design-1", ref["path"], ref["sha256"])

    def test_wrong_session_refused(self):
        ref = self.approve(session_id="other-session")
        with self.assertRaisesRegex(ValueError, "session"):
            approvals.require_physicell_design_approval(self.root, "design-1", ref["path"], ref["sha256"])

    def test_missing_approved_by_refused(self):
        ref = self.approve()
        text = (self.root / "DECISIONS.md").read_text()
        (self.root / "DECISIONS.md").write_text(text.replace('"approved_by": "researcher"', '"approved_by": "model"'))
        with self.assertRaises(ValueError):
            approvals.require_physicell_design_approval(self.root, "design-1", ref["path"], ref["sha256"])

    def test_changed_design_refused(self):
        ref = self.approve()
        (self.root / REL).write_text((self.root / REL).read_text() + "\n")
        with self.assertRaisesRegex(ValueError, "sha256"):
            approvals.require_physicell_design_approval(self.root, "design-1", ref["path"], ref["sha256"])

    def test_new_configuration_uses_declared_session(self):
        data = self.complete()
        data.update(basis=None, session="mc-new", cycle_models=["Live", "Flow cytometry model (separated)"],
                    domain={"x_min": -400, "x_max": 400, "y_min": -400, "y_max": 400, "z_min": -10, "z_max": 10,
                            "dx": 20, "dy": 20, "dz": 20, "units": "micron", "max_time": 100, "time_units": "min"})
        for item in data["cell_definitions"] + data["substrates"]:
            item["from"] = {"source": "s1", "locator": "Fig. 2"}
        ref = self.approve(data, session_id="mc-new")
        approvals.require_physicell_design_approval(self.root, "design-1", ref["path"], ref["sha256"])


if __name__ == "__main__":
    unittest.main()
