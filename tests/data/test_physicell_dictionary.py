from __future__ import annotations

import copy
import hashlib
import json
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from scripts.physicell import capture_dictionary as cd
from scripts.capture.record_physicell_config import record_physicell
from tests.data.test_physicell_design import MABOSS, REL, STAGE_A, Base, built_capture, dispatch_design

P = "mcp__physicell-agent__"
SIGNALS_TOOL, BEHAVIORS_TOOL = "list_all_available_signals", "list_all_available_behaviors"
FIXTURE = Path(__file__).parent / "fixtures" / "physicell" / "tool_calls.json"
INVOCATION = f"runs/multicellular-configurator/{STAGE_A}/invocation/agent-1/tool_calls.json"
SIGNALS = [{"name": "oxygen", "signal_type": "substrate", "description": "o2", "requires": []},
           {"name": "pressure", "signal_type": "mechanics", "description": "p", "requires": []}]


def behaviors(cell):
    return [{"name": "migration speed", "behavior_type": "motility", "aliases": [], "description": f"{cell} speed",
             "requires": []},
            {"name": "exit from cycle phase 1", "behavior_type": "cycle", "aliases": [], "description": "x",
             "requires": []}]


def uniform(items, key):
    return [{"name": i["name"], "type": i[key]} for i in items]


def structured(key, items, session=STAGE_A):
    md = "".join(f"- **{i['name']}**: {i['description']}\n" for i in items)
    return [{"type": "text", "text": "### X\n" + md},
            {"type": "text", "text": json.dumps({"server": "PhysiCell", "session_id": session, key: items})}]


def markdown(items, key="signal_type"):
    out = ""
    for kind in dict.fromkeys(i[key] for i in items):
        out += f"### {kind.upper()}\n" + "".join(
            f"- **{i['name']}**: {i['description']}\n  - *Requires: a*\n" for i in items if i[key] == kind)
    return out


def call(n, name, inp, content, error=False):
    result = {"type": "tool_result", "tool_use_id": f"t{n}", "content": content}
    if error:
        result["is_error"] = True
    return [{"type": "tool_use", "id": f"t{n}", "name": P + name, "input": inp}, result]


def blocks(*calls):
    return [b for c in calls for b in c]


def good_calls(extra_before=(), cells=("default", "tumor", "tumor_hypoxic"), extra_behaviors=()):
    """A tool_calls.json block list holding fresh signal and per-cell behavior lists (used by projection tests);
    `extra_behaviors` names are appended to every cell's behavior list."""
    extra = [{"name": n, "behavior_type": "death", "aliases": [], "description": n, "requires": []}
             for n in extra_behaviors]
    return blocks(*extra_before,
                  call(10, "list_all_available_signals", {"session_id": STAGE_A}, structured("signals", SIGNALS)),
                  *[call(11 + i, "list_all_available_behaviors", {"session_id": STAGE_A, "cell_type": c},
                         structured("behaviors", behaviors(c) + extra))
                    for i, c in enumerate(cells)])


class DictionaryTests(Base):
    def setUp(self):
        super().setUp()
        self.design = self.complete()  # anchored on the basis capture's session
        self.write(self.design)
        # stage A: a new configurator session, dispatched with the design, built and captured the configuration
        self.capture = built_capture(self.root, STAGE_A, "cap-a")
        dispatch_design(self.root)

    def invoke(self, content, rel=INVOCATION):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(content))
        return rel

    def good(self, extra_before=()):
        return good_calls(extra_before)

    def run_capture(self, content, **kw):
        return cd.capture_dictionary(self.root, self.invoke(content, **kw), self.capture, REL)

    def refused(self, content, pattern, **kw):
        with self.assertRaisesRegex(ValueError, pattern):
            self.run_capture(content, **kw)

    def test_fixture_file_matches_helpers(self):
        data = json.loads(FIXTURE.read_text())
        path = self.run_capture(data)
        self.assertEqual(json.loads(path.read_text())["signals"][0]["name"], "oxygen")

    def test_happy_path(self):
        content = self.good()
        path = self.run_capture(content)
        self.assertEqual(path, self.root / "runs/orchestrator/p1/physicell_dictionary.json")
        out = json.loads(path.read_text())
        raw = (self.root / INVOCATION).read_bytes()
        manifest = (self.root / self.capture / "capture.json").read_bytes()
        self.assertEqual(out["kind"], "physicell_dictionary")
        self.assertEqual(out["schema_version"], 1)
        self.assertEqual(out["session"], STAGE_A)
        self.assertEqual(out["invocation"], INVOCATION)
        self.assertEqual(out["capture"], {"path": self.capture, "sha256": hashlib.sha256(manifest).hexdigest()})
        self.assertEqual(out["design"], {"path": REL, "sha256": hashlib.sha256((self.root / REL).read_bytes()).hexdigest()})
        self.assertEqual(out["source_sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(out["signals"], uniform(SIGNALS, "signal_type"))
        self.assertEqual(set(out["behaviors"]), {"default", "tumor", "tumor_hypoxic"})
        self.assertEqual(out["behaviors"]["tumor"], uniform(behaviors("tumor"), "behavior_type"))
        with self.assertRaisesRegex(ValueError, "exists"):
            cd.capture_dictionary(self.root, INVOCATION, self.capture, REL)

    def test_markdown_only_and_repeats_deduped(self):
        content = blocks(
            call(1, "list_all_available_signals", {}, markdown(SIGNALS + SIGNALS[:1])),
            *[call(2 + i, "list_all_available_behaviors", {"cell_type": c}, [{"type": "text", "text": markdown(behaviors(c), "behavior_type")}])
              for i, c in enumerate(("default", "tumor", "tumor_hypoxic"))])
        out = json.loads(self.run_capture(content).read_text())
        self.assertEqual([s["name"] for s in out["signals"]], ["oxygen", "pressure"])
        self.assertEqual([b["name"] for b in out["behaviors"]["default"]], ["migration speed", "exit from cycle phase 1"])
        self.assertEqual(out["behaviors"]["default"][0], {"name": "migration speed", "type": "motility"})

    def test_error_result_refused(self):
        content = self.good()
        content[1]["is_error"] = True
        self.refused(content, "signals")

    def test_payload_iserror_refused(self):
        content = self.good()
        content[1]["content"] = json.dumps({"isError": True, "content": [{"type": "text", "text": "boom"}]})
        self.refused(content, "signals")

    def test_list_before_last_add_cell_type_refused(self):
        add = call(5, "add_single_cell_type", {"session_id": STAGE_A}, "ok")
        content = blocks(*[call(1, "list_all_available_signals", {}, structured("signals", SIGNALS))],
                         add, *[call(11 + i, "list_all_available_behaviors", {"cell_type": c},
                                     structured("behaviors", behaviors(c)))
                                for i, c in enumerate(("default", "tumor", "tumor_hypoxic"))])
        self.refused(content, "after the last structure-changing")

    def test_list_before_add_physiboss_model_refused(self):
        content = self.good() + blocks(call(30, "add_physiboss_model", {"session_id": STAGE_A}, "ok"))
        self.refused(content, "after the last structure-changing")

    def test_list_before_create_simulation_domain_refused(self):
        # PR #17 review 4586111592: (re)creating the domain inserts the library default substrate
        content = self.good() + blocks(call(30, "create_simulation_domain", {"domain_x": 200, "domain_y": 200}, "ok"))
        self.refused(content, "after the last structure-changing")

    def test_unknown_tool_is_a_boundary(self):
        # fail closed: a tool outside the read-only set (e.g. one a later server adds) is a structural boundary
        for tool in ("add_custom_variable", "set_default_session", "analyze_biological_scenario"):
            with self.subTest(tool=tool):
                self.refused(self.good() + blocks(call(30, tool, {}, "ok")), "after the last structure-changing")
        dangling = [{"type": "tool_use", "id": "x9", "name": P + "add_custom_variable", "input": {}}]
        self.refused(self.good() + dangling, "no result")

    def test_read_only_and_export_tools_are_not_boundaries(self):
        tools = sorted(cd.READ_ONLY - {SIGNALS_TOOL, BEHAVIORS_TOOL})  # a later list call would replace the lists
        after = blocks(*[call(30 + n, tool, {}, "ok") for n, tool in enumerate(tools)])
        self.assertTrue(self.run_capture(self.good() + after).is_file())

    def test_read_only_set_is_explicit(self):
        # server 2.5.2: the readOnlyHint tools, plus the three exports (they write files, never the lists' structure)
        self.assertEqual(cd.READ_ONLY, frozenset({
            "list_sessions", "list_artifact_sessions", "get_workflow_status", "get_maboss_context",
            "validate_xml_file", "list_loaded_components", "get_available_cycle_models", SIGNALS_TOOL, BEHAVIORS_TOOL,
            "get_simulation_summary", "list_generated_files", "get_help", "export_xml_configuration",
            "export_cell_rules_csv", "export_configuration_bundle"}))

    def test_lists_then_bundle_export_record_the_dictionary(self):
        # PR #17 review 4587426219: export_configuration_bundle (server 2.5.2) packages a deep copy of the
        # configuration into a ZIP; it changes nothing the lists are derived from
        content = self.good() + blocks(call(30, "export_xml_configuration", {"filename": "PhysiCell_settings.xml"},
                                            "ok"),
                                       call(31, "export_configuration_bundle", {"working_directory": "/w"},
                                            "bundle_0.zip"))
        out = json.loads(self.run_capture(content).read_text())
        self.assertEqual([s["name"] for s in out["signals"]], ["oxygen", "pressure"])

    def test_failed_structure_change_does_not_count(self):
        content = self.good() + blocks(call(30, "add_physiboss_model", {}, "nope", error=True))
        self.assertTrue(self.run_capture(content).is_file())

    def test_missing_behavior_list_refused(self):
        content = self.good()[:-2]
        self.refused(content, "tumor_hypoxic")

    def test_behavior_list_for_other_cell_type_not_used(self):
        content = self.good()
        content[-2]["input"]["cell_type"] = "other"
        self.refused(content, "tumor_hypoxic")

    def test_other_session_calls_ignored(self):
        other = blocks(call(40, "list_all_available_signals", {"session_id": "other"},
                            structured("signals", [], "other")))
        self.assertEqual(json.loads(self.run_capture(self.good() + other).read_text())["signals"],
                         uniform(SIGNALS, "signal_type"))

    def test_capture_from_another_session_refused(self):
        self.refused(self.good(), "session", rel="runs/multicellular-configurator/other/invocation/a/tool_calls.json")

    def test_wrong_invocation_path_refused(self):
        with self.assertRaisesRegex(ValueError, "invocation"):
            cd.capture_dictionary(self.root, "runs/x/tool_calls.json", self.capture, REL)

    def test_both_forms_give_identical_dictionary(self):
        cells = ("default", "tumor", "tumor_hypoxic")
        md = blocks(call(1, "list_all_available_signals", {}, markdown(SIGNALS)),
                    *[call(2 + i, "list_all_available_behaviors", {"cell_type": c}, markdown(behaviors(c), "behavior_type"))
                      for i, c in enumerate(cells)])
        a = json.loads(self.run_capture(md).read_text())
        (self.root / "runs/orchestrator/p1/physicell_dictionary.json").unlink()
        b = json.loads(self.run_capture(self.good()).read_text())
        for key in ("signals", "behaviors"):
            self.assertEqual(a[key], b[key])

    def test_markdown_item_without_heading_refused(self):
        content = self.good()
        content[1]["content"] = "- **oxygen**: o2\n"
        self.refused(content, "heading")

    def test_malformed_structured_payload_refused(self):
        content = self.good()
        content[1]["content"] = json.dumps({"server": "PhysiCell", "session_id": STAGE_A,
                                            "signals": [{"description": "no name"}]})
        content[1]["content"] = [{"type": "text", "text": "- **oxygen**: o2\n"}, {"type": "text", "text": content[1]["content"]}]
        self.refused(content, "malformed")

    def test_parallel_list_use_before_add_refused(self):
        lst = call(1, "list_all_available_signals", {}, structured("signals", SIGNALS))
        add = call(2, "add_single_cell_type", {}, "ok")
        rest = [call(11 + i, "list_all_available_behaviors", {"cell_type": c}, structured("behaviors", behaviors(c)))
                for i, c in enumerate(("default", "tumor", "tumor_hypoxic"))]
        content = [lst[0], add[0], add[1], lst[1]] + blocks(*rest)
        self.refused(content, "after the last structure-changing")

    def test_non_text_tool_ids_are_ignored_not_a_crash(self):
        # PR #17 review 4586111416: a block id that is not text is no call (never a TypeError)
        odd = [{"type": "tool_use", "id": ["t1"], "name": P + "list_all_available_signals", "input": {}},
               {"type": "tool_result", "tool_use_id": ["t1"], "content": "x"}]
        self.assertTrue(self.run_capture(odd + self.good()).is_file())

    def test_dangling_structure_call_refused(self):
        content = self.good() + [{"type": "tool_use", "id": "dangling9", "name": P + "add_single_cell_type", "input": {}}]
        self.refused(content, "add_single_cell_type.*dangling9")

    def test_odd_case_name_preserved(self):
        self.design["cell_definitions"][2]["name"] = "Tumor_Cell"
        self.design["coupling_plan"][1]["cell_definition"] = "Tumor_Cell"
        self.write(self.design)
        dispatch_design(self.root)  # stage A was dispatched with this design

        def rename(xml_root):
            xml_root.find("cell_definitions/cell_definition[@name='tumor_hypoxic']").set("name", "Tumor_Cell")

        self.capture = built_capture(self.root, STAGE_A, "cap-odd", edit=rename)
        content = blocks(call(1, "list_all_available_signals", {}, structured("signals", SIGNALS)),
                         *[call(2 + i, "list_all_available_behaviors", {"cell_type": c}, structured("behaviors", behaviors(c)))
                           for i, c in enumerate(("default", "tumor", "Tumor_Cell"))])
        out = json.loads(self.run_capture(content).read_text())
        self.assertEqual(list(out["behaviors"]), ["default", "tumor", "Tumor_Cell"])

    def test_design_anchor_is_not_the_capture_session(self):
        # the design's session is its basis anchor; stage A is bound by dispatch lineage, not by session
        self.assertNotEqual(self.design["session"], STAGE_A)
        self.assertTrue(self.run_capture(self.good()).is_file())

    def test_dispatch_naming_another_design_sha_refused(self):
        dispatch_design(self.root, digest="0" * 64)
        self.refused(self.good(), "lineage.*another sha256")

    def test_missing_dispatch_refused(self):
        (self.root / f"runs/multicellular-configurator/{STAGE_A}/invocation/agent-1/task.txt").unlink()
        self.refused(self.good(), "lineage: no invocation record")

    def test_changed_design_refused(self):
        self.design["unmapped"] = [{"item": "fibroblast", "reason": "out of scope"}]
        self.write(self.design)  # still a valid design, but not the one stage A was dispatched with
        self.refused(self.good(), "lineage")

    def test_lineage_refusal_carries_lineage_s_reason(self):
        # PR #17 review 4588364259: the capture_dictionary refusal says why lineage refused, never "has no task.txt"
        # when there is one
        task = self.root / f"runs/multicellular-configurator/{STAGE_A}/invocation/agent-1/task.txt"
        lines = task.read_text().splitlines()
        payload = json.loads(lines[1][len("DATA_INPUT "):])
        cases = {
            "malformed line 1": (["Build it.", *lines[1:]], r"line 1 is not a STAGE_GATE line\)$"),
            "line 2 not DATA_INPUT": ([lines[0], "Build the design.", *lines[2:]],
                                      r"agent-1/task.txt \(line 2 is not a DATA_INPUT line\)$"),
            "malformed line 2": ([lines[0], "DATA_INPUT {", *lines[2:]],
                                 r"agent-1/task.txt \(line 2 is not a strict JSON DATA_INPUT object: .*\)$"),
            "line 2 of another form": ([lines[0], "DATA_INPUT " + json.dumps({**payload, "x": 1}), *lines[2:]],
                                       r"agent-1/task.txt \(line 2 is not the design DATA_INPUT form "),
            "refused approval": ([lines[0], "DATA_INPUT " + json.dumps({**payload, "approval": "bogus"}), *lines[2:]],
                                 r"decision_id not found.*\)$"),
            "unrelated upstream": (['STAGE_GATE {"upstream_session_id": "maboss-unrelated", "outputs": ["A"]}',
                                    *lines[1:]], r"upstream_session_id maboss-unrelated; every design handoff must "
                                                 r"be under runs/boolean-dynamics-modeler/maboss-unrelated/\)$"),
        }
        for name, (text, reason) in cases.items():
            with self.subTest(case=name):
                task.write_text("\n".join(text) + "\n")
                with self.assertRaises(ValueError) as caught:
                    self.run_capture(self.good())
                message = str(caught.exception)
                self.assertNotIn("has no task.txt", message)
                self.assertRegex(message, r"^lineage of runs/multicellular-configurator/.*/agent-1 \(its "
                                          r"tool_calls.json must be the record of a verified dispatch of the design\): "
                                          r"lineage: no invocation record")
                self.assertRegex(message, reason)
        task.unlink()
        with self.assertRaisesRegex(ValueError, r"agent-1/task.txt does not exist$"):
            self.run_capture(self.good())

    def test_line_two_with_an_extra_or_missing_key_is_not_lineage(self):
        # parked minor 3: only the exact design form satisfies dispatched_with
        from scripts import state
        from scripts.physicell import lineage
        task = self.root / f"runs/multicellular-configurator/{STAGE_A}/invocation/agent-1/task.txt"
        lines = task.read_text().splitlines()
        payload = json.loads(lines[1][len("DATA_INPUT "):])
        digest = state.digest(self.root / REL)
        self.assertTrue(lineage.dispatched_with(self.root, STAGE_A, "design", REL, digest, design=self.design).endswith("task.txt"))
        for bad in ({**payload, "extra": 1}, {k: v for k, v in payload.items() if k != "approval"}):
            with self.subTest(keys=sorted(bad)):
                task.write_text("\n".join([lines[0], "DATA_INPUT " + json.dumps(bad), *lines[2:]]) + "\n")
                with self.assertRaisesRegex(ValueError, "lineage: no invocation record"):
                    lineage.dispatched_with(self.root, STAGE_A, "design", REL, digest, design=self.design)
                self.refused(self.good(), "lineage: no invocation record")

    def test_lineage_needs_a_stage_gate_a_real_approval_and_its_session(self):
        # PR #17 review 4586111748: a line 2 with the right keys, path and sha256 is not enough
        from scripts import state
        from scripts.physicell import lineage
        task = self.root / f"runs/multicellular-configurator/{STAGE_A}/invocation/agent-1/task.txt"
        lines = task.read_text().splitlines()
        payload = json.loads(lines[1][len("DATA_INPUT "):])
        digest = state.digest(self.root / REL)
        gate_line = lines[0]
        other = "runs/orchestrator/p9/physicell_design.json"
        self.write(self.design, other)
        state.update(self.root, "record", {
            "id": "proj-x", "kind": "stage_approval", "session_id": payload["record_session_id"],
            "stage": "physicell_projection", "outputs": ["x"], "approved_by": "researcher", "authorization": "approved",
            "artifact": {"path": REL, "sha256": digest}})
        cases = {
            "bogus approval": (gate_line, {**payload, "approval": "bogus"}, "decision_id not found"),
            "projection-stage approval": (gate_line, {**payload, "approval": "proj-x"}, "physicell_design"),
            "wrong record_session_id": (gate_line, {**payload, "record_session_id": "x"}, "record_session_id 'x'"),
            "no STAGE_GATE line": ("Build the approved PhysiCell design.", payload, "STAGE_GATE"),
            "STAGE_GATE with extra keys": (gate_line[:-1] + ', "x": 1}', payload, "STAGE_GATE"),
            "STAGE_GATE not JSON": ("STAGE_GATE {", payload, "STAGE_GATE"),
        }
        for name, (first, value, reason) in cases.items():
            with self.subTest(case=name):
                task.write_text("\n".join([first, "DATA_INPUT " + json.dumps(value), *lines[2:]]) + "\n")
                with self.assertRaisesRegex(ValueError, f"lineage: no invocation record.*{reason}"):
                    lineage.dispatched_with(self.root, STAGE_A, "design", REL, digest, design=self.design)
                self.refused(self.good(), "lineage: no invocation record")
        task.write_text("\n".join(lines) + "\n")
        self.assertTrue(lineage.dispatched_with(self.root, STAGE_A, "design", REL, digest, design=self.design).endswith("task.txt"))

    def test_lineage_stage_gate_is_bound_to_the_design(self):
        # PR #17 review 4587425869: a STAGE_GATE the hook would refuse for this design is not lineage
        from scripts import state
        from scripts.physicell import lineage
        task = self.root / f"runs/multicellular-configurator/{STAGE_A}/invocation/agent-1/task.txt"
        lines = task.read_text().splitlines()
        digest = state.digest(self.root / REL)
        cases = {"unrecorded standalone gate": ('STAGE_GATE {"standalone_decision_id": "never-recorded", '
                                                '"outputs": ["zzz"]}', "standalone_decision_id never-recorded.*"
                                                "may import no MaBoSS handoff"),
                 "unrelated MaBoSS upstream": ('STAGE_GATE {"upstream_session_id": "maboss-unrelated", '
                                               '"outputs": ["A"]}', "maboss-session.*upstream_session_id "
                                               "maboss-unrelated"),
                 # PR #17 review 4588363532: an upstream gate's outputs are those of the handoff's approval
                 "upstream gate with other outputs": ('STAGE_GATE {"upstream_session_id": "maboss-session", '
                                                      '"outputs": ["zzz"]}', r"STAGE_GATE outputs \['zzz'\] are not "
                                                      r"the outputs of any recorded approval of the design's MaBoSS "
                                                      r"handoffs \(\[\"A\", \"B\"\]\)")}
        for name, (first, reason) in cases.items():
            with self.subTest(case=name):
                task.write_text("\n".join([first, *lines[1:]]) + "\n")
                with self.assertRaisesRegex(ValueError, f"lineage: no invocation record.*{reason}"):
                    lineage.dispatched_with(self.root, STAGE_A, "design", REL, digest, design=self.design)
                self.refused(self.good(), "lineage: no invocation record")
        task.write_text("\n".join(lines) + "\n")
        self.assertTrue(self.run_capture(self.good()).is_file())  # the design's own MaBoSS upstream passes

    def test_re_approval_with_other_outputs_keeps_an_earlier_lineage(self):
        # round-3 follow-up item 4: lineage accepts the outputs of any recorded approval of the design's handoff
        # (exact path and sha256), so re-approving the same artifact with other outputs does not undo the lineage of
        # a capture dispatched under the earlier outputs; outputs no approval names are still refused
        from scripts import state
        from scripts.physicell import lineage
        digest = state.digest(self.root / REL)
        ref = self.design["intracellular"][0]["maboss"]
        state.update(self.root, "record", {
            "id": "approval-again", "kind": "stage_approval", "session_id": MABOSS, "stage": "boolean-dynamics",
            "downstream_stage": "multicellular", "outputs": ["A"], "approved_by": "researcher",
            "authorization": "approved", "artifact": {"path": ref["handoff"], "sha256": ref["sha256"]}})
        self.assertTrue(lineage.dispatched_with(self.root, STAGE_A, "design", REL, digest,
                                                design=self.design).endswith("task.txt"))  # gate ["A", "B"]
        task = self.root / f"runs/multicellular-configurator/{STAGE_A}/invocation/agent-1/task.txt"
        lines = task.read_text().splitlines()
        task.write_text("\n".join([lines[0].replace('["A", "B"]', '["A"]'), *lines[1:]]) + "\n")
        self.assertTrue(lineage.dispatched_with(self.root, STAGE_A, "design", REL, digest,
                                                design=self.design).endswith("task.txt"))  # gate ["A"]
        task.write_text("\n".join([lines[0].replace('["A", "B"]', '["zzz"]'), *lines[1:]]) + "\n")
        with self.assertRaisesRegex(ValueError, r"\['zzz'\] are not the outputs of any recorded approval"):
            lineage.dispatched_with(self.root, STAGE_A, "design", REL, digest, design=self.design)

    def test_standalone_lineage_needs_a_design_without_handoffs_and_the_record(self):
        # PR #17 review 4587425869: standalone_decision_id only for a design importing no MaBoSS handoff, and only
        # with a recorded standalone_approval (gate.standalone)
        from scripts import state
        from scripts.physicell import lineage
        bare = copy.deepcopy(self.design)
        bare["intracellular"] = []
        if bare["basis"] is None:
            bare["session"] = "p8"  # a new configuration's anchor is its purpose
        rel = "runs/orchestrator/p8/physicell_design.json"
        self.write(bare, rel)
        task = dispatch_design(self.root, session="mc-stage-sa", rel=rel)
        lines = task.read_text().splitlines()
        digest = state.digest(self.root / rel)
        with self.assertRaisesRegex(ValueError, "imports no MaBoSS handoff"):  # the fixture's upstream gate
            lineage.dispatched_with(self.root, "mc-stage-sa", "design", rel, digest, design=bare)
        gate_line = 'STAGE_GATE {"standalone_decision_id": "sa-mc", "outputs": ["tissue"]}'
        task.write_text("\n".join([gate_line, *lines[1:]]) + "\n")
        with self.assertRaisesRegex(ValueError, "missing explicit standalone authorization record"):
            lineage.dispatched_with(self.root, "mc-stage-sa", "design", rel, digest, design=bare)
        source = self.root / "inputs/tissue.txt"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("researcher tissue model\n")
        state.update(self.root, "record", {
            "id": "sa-mc", "kind": "standalone_approval", "stage": "multicellular", "outputs": ["tissue"],
            "artifact": {"path": "inputs/tissue.txt", "sha256": state.digest(source)},
            "approved_by": "researcher", "authorization": "researcher: build my tissue model"})
        self.assertTrue(lineage.dispatched_with(self.root, "mc-stage-sa", "design", rel, digest,
                                                design=bare).endswith("task.txt"))

    def test_lineage_forms_are_the_gate_forms(self):
        # PR #17 review 4586112855: one definition of the DATA_INPUT forms, shared with the gate
        from scripts import gate
        from scripts.physicell import lineage
        self.assertIs(lineage.FORMS, gate.PHYSICELL_FORMS)

    def test_lineage_session_must_be_an_identifier(self):
        # parked minor 1: the session is validated locally, never used as a path fragment
        from scripts.physicell import lineage
        for bad in ("../mc-stage-a", "a/b", "", "..", None):
            with self.subTest(session=bad), self.assertRaisesRegex(ValueError, "identifier"):
                lineage.dispatched_with(self.root, bad, "design", REL, "0" * 64, design=self.design)

    def test_tool_calls_must_sit_beside_the_dispatched_task(self):
        # review M2: the parsed tool_calls.json is in the invocation folder whose task.txt has the design line
        other = f"runs/multicellular-configurator/{STAGE_A}/invocation/agent-2/tool_calls.json"
        self.refused(self.good(), "lineage of .*/agent-2 .*agent-2/task.txt does not exist$", rel=other)
        dispatch_design(self.root, agent="agent-2")
        self.assertTrue(self.run_capture(self.good(), rel=other).is_file())

    def test_folder_form_accepted(self):
        content = self.good()
        self.invoke(content)
        path = cd.capture_dictionary(self.root, INVOCATION.rsplit("/", 1)[0], self.capture, REL)
        self.assertEqual(json.loads(path.read_text())["invocation"], INVOCATION)

    def test_overwrite_message_says_new_purpose(self):
        self.run_capture(self.good())
        with self.assertRaisesRegex(ValueError, "new design purpose directory; never delete"):
            cd.capture_dictionary(self.root, INVOCATION, self.capture, REL)


def find(xml_root, path):
    found = xml_root.find(path)
    assert found is not None, path
    return found


def oxygen(xml_root, path=""):
    return find(xml_root, "microenvironment_setup/variable[@name='oxygen']" + (f"/{path}" if path else ""))


class ConformanceTests(DictionaryTests):
    """Ruling T9-b: the stage-A capture equals the design before its dictionary is recorded; every mismatch is
    named. The conforming capture of setUp passes (test_happy_path)."""

    count = 0

    def refused_capture(self, pattern, edit=None, **kw):
        ConformanceTests.count += 1
        self.capture = built_capture(self.root, "mc-stage-a", f"cap-c{self.count}", edit=edit, **kw)
        self.refused(self.good(), "does not conform to the design.*" + pattern)

    def conform(self, design, capture):
        """conformance on the caller's binding of `capture` (settings_xml.capture_tree) and, as the design check
        hands them to its callers, the basis tree and the handoff manifests."""
        basis = None if design["basis"] is None else cd.sx.capture_tree(self.root, design["basis"]["capture"])[1]
        manifests = {i["cell_definition"]: cd.pd.handoff_manifest(self.root, i["maboss"])
                     for i in design["intracellular"]}
        return cd.conformance(design, *cd.sx.capture_tree(self.root, capture), basis, manifests)

    def test_conforming_capture_passes(self):
        self.assertEqual(self.conform(self.design, self.capture), [])

    def test_cell_definition_set(self):
        def extra(xml_root):
            definitions = find(xml_root, "cell_definitions")
            stromal = copy.deepcopy(find(definitions, "cell_definition[@name='default']"))
            stromal.set("name", "stromal")
            definitions.append(stromal)
        self.refused_capture(r"cell definitions \['stromal'\] are in the capture but not in the design", extra)

        def missing(xml_root):
            definitions = find(xml_root, "cell_definitions")
            definitions.remove(find(definitions, "cell_definition[@name='tumor_hypoxic']"))
        self.refused_capture(r"cell definitions \['tumor_hypoxic'\] are in the design but not in the capture", missing)

    def test_cycle_model(self):
        def cycle(xml_root):
            find(xml_root, "cell_definitions/cell_definition[@name='tumor_hypoxic']/phenotype/cycle").set(
                "name", "Ki67 (basic)")
        self.refused_capture("cell definition tumor_hypoxic: cycle 'Ki67 \\(basic\\)' is not the design's 'live'", cycle)

    def test_substrate_set_and_label(self):
        def extra(xml_root):
            setup = find(xml_root, "microenvironment_setup")
            glucose = copy.deepcopy(oxygen(xml_root))
            glucose.set("name", "glucose")
            setup.insert(0, glucose)
        self.refused_capture(r"substrates \['glucose'\] are in the capture but not in the design", extra)
        self.refused_capture("substrate oxygen: units label 'mM' is not the design's 'mmHg'",
                             lambda x: oxygen(x).set("units", "mM"))

    def test_substrate_values_and_units(self):
        cases = (("physical_parameter_set/diffusion_coefficient", "100", "diffusion_coefficient: the capture holds '100'"),
                 ("physical_parameter_set/decay_rate", "0.2", "decay_rate: the capture holds '0.2'"),
                 ("initial_condition", "21", "initial_condition: the capture holds '21'"),
                 ("Dirichlet_boundary_condition", "20", "Dirichlet value: the capture holds '20'"))
        for path, text, pattern in cases:
            with self.subTest(path=path):
                def edit(xml_root, path=path, text=text):
                    oxygen(xml_root, path).text = text
                self.refused_capture(pattern, edit)
        self.refused_capture("decay_rate: the capture's units '1/s' are not the design's '1/min'",
                             lambda x: oxygen(x, "physical_parameter_set/decay_rate").set("units", "1/s"))
        self.refused_capture("Dirichlet enabled: the capture holds 'false'",
                             lambda x: oxygen(x, "Dirichlet_boundary_condition").set("enabled", "false"))

    def test_domain_of_a_basis_design_is_the_basis_domain(self):
        self.refused_capture("domain: differs from the basis capture's",
                             lambda x: setattr(find(x, "domain/x_max"), "text", "500"))
        self.refused_capture("overall/max_time: differs from the basis capture's",
                             lambda x: setattr(find(x, "overall/max_time"), "text", "60"))

    def test_maboss_models_on_exactly_the_declared_cells(self):
        def moved(xml_root):
            tumor = find(xml_root, "cell_definitions/cell_definition[@name='tumor']/phenotype")
            model = find(tumor, "intracellular")
            tumor.remove(model)
            find(xml_root, "cell_definitions/cell_definition[@name='tumor_hypoxic']/phenotype").append(model)
        self.refused_capture("cell definition tumor: the design's MaBoSS model is not its one intracellular model", moved)
        self.refused_capture("cell definition tumor_hypoxic: carries an intracellular model the design does not declare",
                             moved)

    def test_carrier_model_is_the_design_handoff_bytes(self):
        # PR #17 review 4586110937: a stale or other MaBoSS export imported onto the carrier is refused
        self.refused_capture("cell definition tumor: its MaBoSS .bnd .*tumor.bnd.* is not the bnd_file .* of the "
                             "design's handoff", bnd=b"Node A { logic = B; }\nNode B { logic = A; }\n")
        self.refused_capture("cell definition tumor: its MaBoSS .cfg .*tumor.cfg.* is not the cfg_file",
                             cfg=b"A.istate=1;")

    def test_two_carriers_with_swapped_models_refused(self):
        hypoxic = self.make_handoff("maboss-hyp", nodes=("A", "B", "X"), cell_type="tumor_hypoxic")
        folder = self.root / "runs/boolean-dynamics-modeler"
        design = copy.deepcopy(self.design)
        design["intracellular"].append({"cell_definition": "tumor_hypoxic",
                                        "maboss": {"handoff": hypoxic["path"], "sha256": hypoxic["sha256"]}})

        def second_model(xml_root):
            getattr(self, "to_new", lambda x: None)(xml_root)  # the new-configuration form's stage-A XML
            model = copy.deepcopy(find(xml_root, "cell_definitions/cell_definition[@name='tumor']/phenotype/"
                                                 "intracellular"))
            find(model, "bnd_filename").text = "./config/hyp.bnd"
            find(model, "cfg_filename").text = "./config/hyp.cfg"
            find(xml_root, "cell_definitions/cell_definition[@name='tumor_hypoxic']/phenotype").append(model)

        mine = {"bnd": (folder / "maboss-session/model.bnd").read_bytes(),
                "cfg": (folder / "maboss-session/model.cfg").read_bytes()}
        theirs = {"bnd": (folder / "maboss-hyp/model.bnd").read_bytes(),
                  "cfg": (folder / "maboss-hyp/model.cfg").read_bytes()}
        right = built_capture(self.root, STAGE_A, "cap-two", edit=second_model, **mine,
                              files={"hyp.bnd": theirs["bnd"], "hyp.cfg": theirs["cfg"]})
        self.assertEqual(self.conform(design, right), [])
        swapped = built_capture(self.root, STAGE_A, "cap-swap", edit=second_model, **theirs,
                                files={"hyp.bnd": mine["bnd"], "hyp.cfg": mine["cfg"]})
        problems = self.conform(design, swapped)
        for cell in ("tumor", "tumor_hypoxic"):
            self.assertTrue(any(p.startswith(f"cell definition {cell}: its MaBoSS .bnd") for p in problems), problems)

    def test_basis_tree_is_the_verified_bytes(self):
        # PR #17 review 4586112201: the basis tree comes from the bytes sx.capture hashed (simulated: the file
        # replaced between the hash and a later sx.load)
        from unittest import mock
        if self.design["basis"] is None:
            return  # a new configuration has no basis
        tampered = cd.sx.load(self.root / self.design["basis"]["capture"] / "package/config" / cd.sx.XML_NAME)
        tampered.find("domain/x_max").text = "500"
        with mock.patch.object(cd.sx, "load", return_value=tampered):
            self.assertEqual(self.conform(self.design, self.capture), [])

    def test_capture_bound_once_per_dictionary(self):
        # PR #17 review 4587427279: capture_dictionary binds the stage-A capture once (sx.capture_tree) and passes the
        # binding to conformance; capture.json, original.xml, the package XML and each dependency are read once
        from unittest import mock
        reads = []
        original = Path.read_bytes
        stage_a = self.root / self.capture

        def counting(path):
            if stage_a in path.parents:
                reads.append(path.relative_to(stage_a).as_posix())
            return original(path)
        with mock.patch.object(Path, "read_bytes", counting), \
                mock.patch.object(cd.sx, "capture_tree", wraps=cd.sx.capture_tree) as binds:
            out = json.loads(self.run_capture(self.good()).read_text())
        # PR #17 review 4588363965: the basis too is bound once per dictionary (by the design check)
        basis = [] if self.design["basis"] is None else [self.design["basis"]["capture"]]
        self.assertEqual(sorted(c.args[1] for c in binds.call_args_list), sorted([self.capture, *basis]))
        files = ["capture.json", "original.xml", *(d["path"] for d in json.loads(
            (stage_a / "capture.json").read_text())["dependencies"]), f"package/config/{cd.sx.XML_NAME}"]
        self.assertEqual(sorted(reads), sorted(files))
        self.assertEqual(out["capture"]["sha256"], hashlib.sha256((stage_a / "capture.json").read_bytes()).hexdigest())

    def test_conformance_takes_the_binding(self):
        # PR #17 reviews 4587427279, 4588363965 and 4588366540: conformance binds no capture (neither the stage-A
        # capture nor the basis) and reads no handoff; it takes them from its caller
        from unittest import mock
        checked = cd.pd.load_checked(self.root, REL)
        self.assertEqual(checked.errors, [])
        bound, tree = cd.sx.capture_tree(self.root, self.capture)
        with mock.patch.object(cd.sx, "capture_tree", wraps=cd.sx.capture_tree) as binds, \
                mock.patch.object(cd.pd, "handoff_manifest", wraps=cd.pd.handoff_manifest) as reads:
            self.assertEqual(cd.conformance(checked.data, bound, tree, checked.basis_tree, checked.manifests), [])
        self.assertEqual((binds.call_count, reads.call_count), (0, 0))

    def test_no_enabled_rules_ruleset(self):
        self.refused_capture("enabled rules ruleset", rules=True)

    def test_no_physiboss_links(self):
        # import_maboss_handoff (server 2.5.2) attaches the model with an empty mapping
        self.refused_capture("cell definition tumor: 1 PhysiBoSS input link", links=True)
        self.refused_capture("cell definition tumor: 1 PhysiBoSS output link", links=True)

    def test_every_mismatch_named(self):
        def many(xml_root):
            oxygen(xml_root, "physical_parameter_set/decay_rate").text = "0.2"
            find(xml_root, "cell_definitions/cell_definition[@name='tumor_hypoxic']/phenotype/cycle").set("name", "x")
        self.refused_capture("tumor_hypoxic: cycle.*; substrate oxygen decay_rate", many)


def new_configuration_xml(xml_root):
    """The fixture stage-A XML as built for Base.new_configuration(): default's cycle is the server name `live`,
    drug is an added substrate in mmHg."""
    find(xml_root, "cell_definitions/cell_definition[@name='default']/phenotype/cycle").set("name", "live")
    drug = find(xml_root, "microenvironment_setup/variable[@name='drug']")
    for element in (drug, find(drug, "initial_condition"), find(drug, "Dirichlet_boundary_condition")):
        element.set("units", "mmHg")


def library_default(xml_root, decay="10"):
    """The library default substrate server 2.5.2 inserts with create_simulation_domain (probe output)."""
    variable = ET.fromstring(
        '<variable name="substrate" units="dimensionless" ID="0"><physical_parameter_set>'
        '<diffusion_coefficient units="micron^2/min">100000</diffusion_coefficient>'
        f'<decay_rate units="1/min">{decay}</decay_rate></physical_parameter_set>'
        '<initial_condition units="mmHg">0</initial_condition>'
        '<Dirichlet_boundary_condition units="mmHg" enabled="false">0</Dirichlet_boundary_condition>'
        '<Dirichlet_options><boundary_value ID="xmin" enabled="false" /></Dirichlet_options></variable>')
    find(xml_root, "microenvironment_setup").insert(0, variable)


class NewConfigurationConformanceTests(ConformanceTests):
    """A new configuration: the domain and max_time are the design's; the untouched library default substrate is
    the server's own."""

    def setUp(self):
        super().setUp()
        self.design = self.new_configuration(copy.deepcopy(self.design))
        self.write(self.design)
        dispatch_design(self.root, anchor="p1")
        self.capture = built_capture(self.root, STAGE_A, "cap-new", edit=self.to_new)

    def to_new(self, xml_root):
        new_configuration_xml(xml_root)
        library_default(xml_root)

    def refused_capture(self, pattern, edit=None, **kw):
        def both(xml_root):
            self.to_new(xml_root)
            if edit is not None:
                edit(xml_root)
        super().refused_capture(pattern, both, **kw)

    def test_happy_path(self):
        self.assertTrue(self.run_capture(self.good()).is_file())

    def test_capture_from_another_session_refused(self):
        self.refused(self.good(), "session", rel="runs/multicellular-configurator/other/invocation/a/tool_calls.json")

    def test_odd_case_name_preserved(self):
        pass  # covered by the basis form

    def test_changed_design_refused(self):
        self.design["unmapped"] = [{"item": "fibroblast", "reason": "out of scope"}]
        self.write(self.design)
        self.refused(self.good(), "lineage")

    def test_domain_of_a_basis_design_is_the_basis_domain(self):
        pass  # a new configuration: test_domain below

    def test_domain(self):
        cases = (("domain/x_max", "500", "domain x_max: the capture holds '500', the design 400.0"),
                 ("domain/y_min", "-300", "domain y_min: the capture holds '-300', the design -400.0"),
                 ("domain/z_max", "20", "domain z_max: the capture holds '20', the design 10.0"),
                 ("domain/dy", "10", "domain dy: the capture holds '10', the design 20.0"),
                 ("domain/use_2D", "false", "domain use_2D: the capture holds 'false', the design 'true'"),
                 ("overall/max_time", "60", "overall max_time: the capture holds '60', the design 1440.0"),
                 ("overall/space_units", "mm", "overall space_units: the capture holds 'mm', the design 'micron'"))
        for path, text, pattern in cases:
            with self.subTest(path=path):
                def edit(xml_root, path=path, text=text):
                    find(xml_root, path).text = text
                self.refused_capture(pattern, edit)
        self.refused_capture("overall max_time: the capture's units 'h' are not the design's 'min'",
                             lambda x: find(x, "overall/max_time").set("units", "h"))

    def test_library_default_substrate_must_be_untouched(self):
        self.refused_capture(r"substrates \['substrate'\] are in the capture but not in the design",
                             lambda x: setattr(find(x, "microenvironment_setup/variable[@name='substrate']"
                                                       "/physical_parameter_set/decay_rate"), "text", "5"))

    def test_added_substrate_dirichlet_boundaries(self):
        def boundary(xml_root):
            find(xml_root, "microenvironment_setup/variable[@name='oxygen']/Dirichlet_options/boundary_value"
                           "[@ID='xmin']").attrib.update(enabled="true")
        self.refused_capture("substrate oxygen Dirichlet boundary xmin: the capture holds '0.0', the design 38.0",
                             boundary)

        def drug_boundary(xml_root):
            options = ET.SubElement(find(xml_root, "microenvironment_setup/variable[@name='drug']"), "Dirichlet_options")
            ET.SubElement(options, "boundary_value", {"ID": "xmin", "enabled": "true"}).text = "0"
        self.refused_capture("substrate drug Dirichlet boundary xmin: enabled, the design's Dirichlet condition is "
                             "disabled", drug_boundary)


if __name__ == "__main__":
    unittest.main()
