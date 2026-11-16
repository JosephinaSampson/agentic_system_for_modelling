from __future__ import annotations

import copy
import json
import shutil
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from scripts import state
from scripts.capture.record_physicell_config import record_physicell
from scripts.data import approvals
from scripts.physicell import physicell_design as pd

FIXTURES = Path(__file__).parent / "fixtures" / "physicell"
SESSION = "mc-session-1"
CAPTURE = "cap-1"
MABOSS = "maboss-session"
STAGE_A = "mc-stage-a"  # the new session the configurator built the design in (never the design's anchor)
REL = "runs/orchestrator/p1/physicell_design.json"
RULES_REFUSAL = "the PhysiCell server (2.5.2) cannot reload a captured rules CSV"
INPUT_FILES_REFUSAL = ("the PhysiCell server (2.5.2) cannot reload a captured configuration's input files (rules, "
                       "MaBoSS, initial cells) into a new session; use a basis without input files")


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


# the server's cycle models as get_available_cycle_models returns them (key: the tool argument, name: the XML name)
CYCLE_MODELS = [{"key": "live", "name": "live"},
                {"key": "flow_cytometry_separated", "name": "Flow cytometry model (separated)"}]
CITE = {"source": "s1", "locator": "Fig. 2"}


def substrate(name, units="mmHg", diffusion=100000.0, decay=0.1, initial=38.0, dirichlet=None, cite=CITE):
    """A design substrate added by the configurator: every add_single_substrate argument, cited."""
    def v(value, unit):
        return {"value": value, "unit": unit, "from": cite}
    return {"name": name, "units": units, "description": f"{name} supply", "from": cite,
            "diffusion_coefficient": v(diffusion, "micron^2/min"), "decay_rate": v(decay, "1/min"),
            "initial_condition": v(initial, units),
            "dirichlet": {"enabled": dirichlet is not None, "value": v(dirichlet if dirichlet is not None else initial,
                                                                        units)}}


def domain(x=800.0, y=800.0, use_2d=True, z=None, dx=20.0, max_time=1440.0, cite=CITE):
    def m(value, unit="micron"):
        return {"value": value, "unit": unit}
    return {"domain_x": m(x), "domain_y": m(y), "use_2d": use_2d, "domain_z": None if z is None else m(z),
            "dx": m(dx), "max_time": m(max_time, "min"), "from": cite}


def stage_a_xml(xml_root, links=False):
    """Make the fixture XML the stage-A build of `Base.complete()`: tumor carries the MaBoSS fixture model (with an
    empty mapping, as import_maboss_handoff writes it, unless `links`) and the new variant tumor_hypoxic (cycle
    live) exists."""
    tumor = xml_root.find("cell_definitions/cell_definition[@name='tumor']/phenotype")
    intracellular = ET.parse(FIXTURES / "intracellular_tumor.xml").getroot()
    if not links:
        for link in list(intracellular.find("mapping")):
            intracellular.find("mapping").remove(link)
    tumor.append(intracellular)
    definitions = xml_root.find("cell_definitions")
    variant = copy.deepcopy(definitions.find("cell_definition[@name='default']"))
    variant.set("name", "tumor_hypoxic")
    variant.set("ID", "2")
    variant.find("phenotype/cycle").set("name", "live")
    definitions.append(variant)


def built_capture(root, session, capture_id, rules=False, edit=None, links=False, bnd=None, cfg=None, files=None):
    """Record a capture of the stage-A build of `Base.complete()` (stage_a_xml: tumor carries the PhysiBoSS
    fixture model, empty mapping unless `links`: then input pressure -> B and output A -> apoptosis; tumor_hypoxic
    added). Its rules ruleset is removed unless `rules` (ruling T5-d: server 2.5.2 cannot reload a captured rules
    CSV). `edit(xml_root)`, if given, changes the XML before it is recorded.

    tumor.bnd / tumor.cfg hold `bnd` / `cfg` (bytes) when given, else the bytes of the MaBoSS handoff of session
    MABOSS when it exists (import_maboss_handoff copies them verbatim), else a 3-node model. `files` ({name: bytes})
    adds further config files (for models an `edit` attaches)."""
    source = Path(root).parent / f"source-{session}-{capture_id}"
    (source / "config").mkdir(parents=True)
    tree = ET.parse(FIXTURES / "PhysiCell_settings.xml")
    if not rules:
        tree.getroot().remove(tree.getroot().find("cell_rules"))
    stage_a_xml(tree.getroot(), links=links)
    if edit is not None:
        edit(tree.getroot())
    tree.write(source / "config/PhysiCell_settings.xml", encoding="utf-8", xml_declaration=True)
    (source / "config/cell_rules.csv").write_bytes((FIXTURES / "cell_rules.csv").read_bytes())
    handoff = Path(root) / "runs/boolean-dynamics-modeler" / MABOSS
    if bnd is None:
        bnd = (handoff / "model.bnd").read_bytes() if (handoff / "model.bnd").is_file() else \
            b"Node A { logic = A; }\nNode B { logic = B; }\nNode C { logic = C; }\n"
    if cfg is None:
        cfg = (handoff / "model.cfg").read_bytes() if (handoff / "model.cfg").is_file() else b"A.istate=0;"
    (source / "config/tumor.bnd").write_bytes(bnd)
    (source / "config/tumor.cfg").write_bytes(cfg)
    for name, content in (files or {}).items():
        (source / "config" / name).write_bytes(content)
    record_physicell(root, source, source / "config/PhysiCell_settings.xml", session, capture_id)
    return f"runs/multicellular-configurator/{session}/configs/{capture_id}"


def dispatch_design(root, session=STAGE_A, rel=REL, approval="design-1", anchor=SESSION, agent="agent-1",
                    digest=None):
    """The invocation task.txt of the configurator run (session `session`) dispatched with the design `rel`: line 2
    is its DATA_INPUT line (the dispatch lineage that binds the stage-A capture to the design)."""
    payload = {"design": rel, "sha256": digest or state.digest(Path(root) / rel), "approval": approval,
               "record_session_id": anchor}
    path = Path(root) / f"runs/multicellular-configurator/{session}/invocation/{agent}/task.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'STAGE_GATE {{"upstream_session_id": "{MABOSS}", "outputs": ["A", "B"]}}\n'
                    f"DATA_INPUT {json.dumps(payload)}\nBuild the approved PhysiCell design.\n")
    return path


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
        data["cell_definitions"].append({"name": "tumor_hypoxic", "cycle_model": "live", "description": "hypoxic variant",
                                         "from": {"source": "s1", "locator": "Fig. 2"}, "variant_of": "tumor"})
        for item in data["substrates"]:
            item["description"] = f"existing {item['name']}"
        data["intracellular"] = [{"cell_definition": "tumor", "maboss": {"handoff": self.handoff["path"],
                                                                         "sha256": self.handoff["sha256"]}}]
        data["coupling_plan"] = [{"cell_definition": "tumor", "behavior_group": "apoptosis", "route": "physiboss",
                                  "notes": "A drives apoptosis"},
                                 {"cell_definition": "tumor_hypoxic", "behavior_group": "motility", "route": "rules",
                                  "notes": "oxygen-dependent speed"}]
        data["cycle_models"] = copy.deepcopy(CYCLE_MODELS)
        return data

    def new_configuration(self, data=None, session="p1"):
        """complete() as a new configuration (no basis): the domain and every substrate value declared and cited,
        cell definitions with cycle model keys."""
        data = data or self.complete()
        data.update(basis=None, session=session, domain=domain(), cycle_models=copy.deepcopy(CYCLE_MODELS))
        keys = {"default": "live", "tumor": "flow_cytometry_separated"}
        for item in data["cell_definitions"]:
            item["from"] = CITE
            item["cycle_model"] = keys.get(item["name"], item["cycle_model"])
        data["substrates"] = [substrate("oxygen", dirichlet=38.0), substrate("drug", diffusion=1000.0, decay=0.01,
                                                                             initial=0.0)]
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
        # every add_single_substrate value is copied from the basis XML with its exact units, from "basis"
        self.assertEqual(data["substrates"][0]["diffusion_coefficient"],
                         {"value": 100000.0, "unit": "micron^2/min", "from": "basis"})
        self.assertEqual(data["substrates"][0]["decay_rate"], {"value": 0.1, "unit": "1/min", "from": "basis"})
        self.assertEqual(data["substrates"][0]["initial_condition"], {"value": 38.0, "unit": "mmHg", "from": "basis"})
        self.assertEqual(data["substrates"][0]["dirichlet"],
                         {"enabled": True, "value": {"value": 38.0, "unit": "mmHg", "from": "basis"}})
        self.assertEqual(data["substrates"][1]["dirichlet"]["enabled"], False)
        self.assertEqual(data["cell_definitions"][0]["description"], "TODO")
        self.assertEqual(data["session"], SESSION)
        with self.assertRaisesRegex(ValueError, "never overwrite"):
            pd.draft(self.root, "p1", basis_capture=self.capture, sources=[], data=[])

    def test_draft_without_basis_has_domain_skeleton(self):
        path = pd.draft(self.root, "p2", basis_capture=None, sources=[], data=[])
        data = json.loads(path.read_text())
        self.assertIsNone(data["basis"])
        self.assertEqual(data["session"], "p2")  # the anchor of a new configuration is its purpose
        # every create_simulation_domain argument is a TODO; the units are the ones the exported XML writes
        self.assertEqual(data["domain"], {"domain_x": {"value": "TODO", "unit": "micron"},
                                          "domain_y": {"value": "TODO", "unit": "micron"}, "use_2d": "TODO",
                                          "domain_z": "TODO", "dx": {"value": "TODO", "unit": "micron"},
                                          "max_time": {"value": "TODO", "unit": "min"}, "from": "TODO"})
        self.assertIsNone(data["cycle_models"])
        errors = pd.check(self.root, "runs/orchestrator/p2/physicell_design.json")
        for pattern in ("domain.domain_x.value", "domain.use_2d", "domain.max_time.value", "domain: a source citation"):
            self.assertTrue(any(pattern in e for e in errors), (pattern, errors))


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
        data = self.new_configuration()
        self.assertEqual(self.errors(data), [])
        data["domain"] = None
        self.assertRefused(data, "domain is required without a basis")

    def test_domain_units_refused(self):
        data = self.new_configuration()
        data["domain"]["domain_x"]["unit"] = "mm"
        data["domain"]["max_time"]["unit"] = "hours"
        errors = self.errors(data)
        self.assertTrue(any("domain.domain_x.unit must be exactly 'micron'" in e for e in errors), errors)
        self.assertTrue(any("domain.max_time.unit must be exactly 'min'" in e for e in errors), errors)
        data["domain"]["domain_x"]["unit"], data["domain"]["max_time"]["unit"] = "micron", "min"
        self.assertEqual(self.errors(data), [])

    def test_domain_every_argument_required(self):
        # ruling T9-a: every create_simulation_domain argument comes from the design, never a tool default
        for key in pd.DOMAIN_KEYS:
            data = self.new_configuration()
            del data["domain"][key]
            with self.subTest(missing=key):
                self.assertRefused(data, "domain is required without a basis, with exactly the keys")
        data = self.new_configuration()
        data["domain"]["dx"]["value"] = 0
        data["domain"]["max_time"]["value"] = "TODO"
        errors = self.errors(data)
        self.assertTrue(any("domain.dx.value must be a finite number > 0" in e for e in errors), errors)
        self.assertTrue(any("domain.max_time.value" in e for e in errors), errors)

    def test_domain_z_null_iff_use_2d(self):
        data = self.new_configuration()
        data["domain"]["domain_z"] = {"value": 40.0, "unit": "micron"}
        self.assertRefused(data, "domain_z must be null with use_2d")
        data["domain"]["use_2d"] = False
        self.assertEqual(self.errors(data), [])
        data["domain"]["domain_z"] = None
        self.assertRefused(data, "domain_z is required without use_2d")
        data["domain"]["use_2d"] = "yes"
        self.assertRefused(data, "use_2d must be true or false")

    def test_domain_needs_a_source(self):
        data = self.new_configuration()
        data["domain"]["from"] = "basis"
        self.assertRefused(data, 'domain: from "basis" needs a basis')
        data["domain"]["from"] = {"source": "s9", "locator": "Methods"}
        self.assertRefused(data, "source s9 is not declared")

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

    def test_basis_with_input_files_refused(self):
        # Ruling T5-e: a new session's load_xml_configuration lacks the capture's ./config input files
        from tests.data.test_physicell_projection import built_capture
        manifest = json.loads((self.root / self.capture / "capture.json").read_text())
        self.assertEqual(manifest["dependencies"], [])
        bnd = built_capture(self.root, SESSION, "cap-bnd")  # tumor carries a PhysiBoSS model: .bnd and .cfg files
        names = [d["path"] for d in json.loads((self.root / bnd / "capture.json").read_text())["dependencies"]]
        self.assertIn("package/config/tumor.bnd", names)
        data = self.complete()
        data["basis"]["capture"] = bnd
        self.assertRefused(data, INPUT_FILES_REFUSAL)
        self.assertRefused(data, "package/config/tumor.bnd")

    def test_basis_input_files_refuses_a_missing_dependency_list(self):
        self.assertEqual(pd._basis_input_files(self.root, self.capture), [])
        design = self.complete()
        manifest = self.root / self.capture / "capture.json"
        data = json.loads(manifest.read_text())
        for broken in ({k: v for k, v in data.items() if k != "dependencies"}, {**data, "dependencies": [1]}):
            with self.subTest(broken=sorted(broken)):
                manifest.write_text(json.dumps(broken))
                with self.assertRaisesRegex(ValueError, "dependencies"):
                    pd._basis_input_files(self.root, self.capture)
                self.assertRefused(design, "basis: ")  # settings_xml.capture refuses it first

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


class ServerArgumentTests(Base):
    """Ruling T9-a: every argument add_single_substrate and add_single_cell_type take comes from the design,
    with the units the exported XML writes; a value without a source is refused."""

    def test_every_substrate_argument_required(self):
        for key in ("diffusion_coefficient", "decay_rate", "initial_condition", "dirichlet"):
            data = self.new_configuration()
            del data["substrates"][0][key]
            with self.subTest(missing=key):
                self.assertRefused(data, "every add_single_substrate argument, never a tool default")

    def test_substrate_value_needs_a_source(self):
        data = self.new_configuration()
        data["substrates"][0]["decay_rate"]["from"] = "basis"
        self.assertRefused(data, 'substrates[0] (oxygen).decay_rate: from "basis" only for a value of a "basis" substrate')
        data = self.new_configuration()
        data["substrates"][0]["diffusion_coefficient"]["from"] = {"source": "s9", "locator": "Table 1"}
        self.assertRefused(data, "source s9 is not declared")
        data = self.new_configuration()
        del data["substrates"][0]["initial_condition"]["from"]
        self.assertRefused(data, "initial_condition must be exactly {value, unit, from}")

    def test_substrate_units_must_be_what_the_xml_writes(self):
        cases = (("diffusion_coefficient", "micron^2/s", "'micron^2/min'"), ("decay_rate", "1/h", "'1/min'"),
                 ("initial_condition", "mM", "'mmHg'"))
        for key, unit, expected in cases:
            data = self.new_configuration()
            data["substrates"][0][key]["unit"] = unit
            with self.subTest(key=key):
                self.assertRefused(data, f"{key}.unit must be exactly {expected}")
        data = self.new_configuration()
        data["substrates"][0]["dirichlet"]["value"]["unit"] = "mM"
        self.assertRefused(data, "dirichlet.value.unit must be exactly 'mmHg'")

    def test_added_substrate_label_must_be_the_server_concentration_unit(self):
        # server 2.5.2 writes initial_condition/Dirichlet units mmHg whatever the label (add_single_substrate)
        data = self.new_configuration()
        data["substrates"][1] = substrate("drug", units="dimensionless", initial=0.0)
        errors = self.errors(data)
        self.assertTrue(any("units 'dimensionless': server 2.5.2 writes initial_condition" in e for e in errors), errors)

    def test_substrate_value_domains(self):
        data = self.new_configuration()
        data["substrates"][0]["diffusion_coefficient"]["value"] = -1
        data["substrates"][0]["decay_rate"]["value"] = "TODO"
        errors = self.errors(data)
        self.assertTrue(any("diffusion_coefficient.value must be >= 0" in e for e in errors), errors)
        self.assertTrue(any("decay_rate.value must be a finite number" in e for e in errors), errors)

    def test_dirichlet_value_required_when_enabled(self):
        data = self.new_configuration()
        data["substrates"][0]["dirichlet"]["value"] = None
        self.assertRefused(data, "dirichlet.value is required when the Dirichlet condition is enabled")
        data["substrates"][0]["dirichlet"]["enabled"] = False
        self.assertEqual(self.errors(data), [])
        data["substrates"][0]["dirichlet"] = {"enabled": "no", "value": None}
        self.assertRefused(data, "dirichlet must be exactly {enabled: true|false, value}")

    def test_library_default_substrate_name_refused(self):
        data = self.new_configuration()
        data["substrates"][1] = substrate("substrate")
        self.assertRefused(data, "'substrate' is the library default substrate")

    def test_basis_substrate_values_must_equal_the_basis(self):
        data = self.complete()
        data["substrates"][0]["decay_rate"]["value"] = 0.2
        self.assertRefused(data, "decay_rate.value 0.2 differs from the basis ('0.1')")
        data = self.complete()
        data["substrates"][0]["decay_rate"]["from"] = CITE
        self.assertRefused(data, 'a "basis" substrate keeps its captured values: from must be "basis"')
        data = self.complete()
        data["substrates"][0]["initial_condition"]["unit"] = "mM"
        self.assertRefused(data, "initial_condition.unit must be exactly 'mmHg'")
        data = self.complete()
        data["substrates"][0]["dirichlet"]["enabled"] = False
        self.assertRefused(data, "dirichlet.enabled differs from the basis")

    def test_cycle_models_are_discovery_records(self):
        data = self.complete()
        data["cycle_models"] = ["live"]
        self.assertRefused(data, "cycle_models must be null or a list of {key, name}")
        data = self.complete()
        data["cycle_models"] = None
        self.assertRefused(data, "record the server's cycle models first")
        data = self.complete()
        data["cell_definitions"][2]["cycle_model"] = "live (XML name)"
        self.assertRefused(data, "is not one of the recorded cycle model keys")

    def test_basis_cells_and_substrates_must_be_declared(self):
        # review M3: basis ⊆ declared
        data = self.complete()
        data["cell_definitions"] = [c for c in data["cell_definitions"] if c["name"] != "default"]
        data["substrates"] = [s for s in data["substrates"] if s["name"] != "drug"]
        errors = self.errors(data)
        self.assertTrue(any("basis cell definition default is not declared" in e for e in errors), errors)
        self.assertTrue(any("basis substrate drug is not declared" in e for e in errors), errors)


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
        data["cell_definitions"].append({"name": "t3", "cycle_model": "live", "description": "d",
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
        data = self.new_configuration()
        ref = self.approve(data, session_id="p1")
        approvals.require_physicell_design_approval(self.root, "design-1", ref["path"], ref["sha256"])
        ref = self.approve(data, id="design-2", session_id="mc-new")
        with self.assertRaisesRegex(ValueError, "anchor is 'p1'"):
            approvals.require_physicell_design_approval(self.root, "design-2", ref["path"], ref["sha256"])

    def test_new_configuration_session_must_be_the_purpose(self):
        data = self.new_configuration(session="mc-new")
        self.write(data)
        self.assertTrue(any("must be the purpose p1" in e for e in pd.check(self.root, REL)))
        data["session"] = "p1"
        self.write(data)
        self.assertEqual(pd.check(self.root, REL), [])


if __name__ == "__main__":
    unittest.main()
