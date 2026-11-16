from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path

from scripts.physicell import capture_dictionary as cd
from tests.data.test_physicell_design import CAPTURE, REL, SESSION, Base

P = "mcp__physicell-agent__"
FIXTURE = Path(__file__).parent / "fixtures" / "physicell" / "tool_calls.json"
INVOCATION = f"runs/multicellular-configurator/{SESSION}/invocation/agent-1/tool_calls.json"
SIGNALS = [{"name": "oxygen", "signal_type": "substrate", "description": "o2", "requires": []},
           {"name": "pressure", "signal_type": "mechanics", "description": "p", "requires": []}]


def behaviors(cell):
    return [{"name": "migration speed", "behavior_type": "motility", "aliases": [], "description": f"{cell} speed",
             "requires": []},
            {"name": "exit from cycle phase 1", "behavior_type": "cycle", "aliases": [], "description": "x",
             "requires": []}]


def uniform(items, key):
    return [{"name": i["name"], "type": i[key]} for i in items]


def structured(key, items, session=SESSION):
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


class DictionaryTests(Base):
    def setUp(self):
        super().setUp()
        self.design = self.complete()
        self.write(self.design)

    def invoke(self, content, rel=INVOCATION):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(content))
        return rel

    def good(self, extra_before=()):
        return blocks(*extra_before,
                      call(10, "list_all_available_signals", {"session_id": SESSION}, structured("signals", SIGNALS)),
                      *[call(11 + i, "list_all_available_behaviors", {"session_id": SESSION, "cell_type": c},
                             structured("behaviors", behaviors(c)))
                        for i, c in enumerate(("default", "tumor", "tumor_hypoxic"))])

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
        self.assertEqual(out["session"], SESSION)
        self.assertEqual(out["invocation"], INVOCATION)
        self.assertEqual(out["capture"], {"path": self.capture, "sha256": hashlib.sha256(manifest).hexdigest()})
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
        add = call(5, "add_single_cell_type", {"session_id": SESSION}, "ok")
        content = blocks(*[call(1, "list_all_available_signals", {}, structured("signals", SIGNALS))],
                         add, *[call(11 + i, "list_all_available_behaviors", {"cell_type": c},
                                     structured("behaviors", behaviors(c)))
                                for i, c in enumerate(("default", "tumor", "tumor_hypoxic"))])
        self.refused(content, "after the last structure-changing")

    def test_list_before_add_physiboss_model_refused(self):
        content = self.good() + blocks(call(30, "add_physiboss_model", {"session_id": SESSION}, "ok"))
        self.refused(content, "after the last structure-changing")

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
        content[1]["content"] = json.dumps({"server": "PhysiCell", "session_id": SESSION,
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

    def test_dangling_structure_call_refused(self):
        content = self.good() + [{"type": "tool_use", "id": "dangling9", "name": P + "add_single_cell_type", "input": {}}]
        self.refused(content, "add_single_cell_type.*dangling9")

    def test_odd_case_name_preserved(self):
        self.design["cell_definitions"][1]["name"] = "Tumor_Cell"
        self.design["intracellular"] = []
        self.design["coupling_plan"] = []
        self.design["cell_definitions"][1]["from"] = {"source": "s1", "locator": "Fig. 1"}
        self.design["cell_definitions"][1]["cycle_model"] = "Live"
        self.design["cell_definitions"] = [c for c in self.design["cell_definitions"] if c["name"] != "tumor_hypoxic"]
        self.write(self.design)
        content = blocks(call(1, "list_all_available_signals", {}, structured("signals", SIGNALS)),
                         *[call(2 + i, "list_all_available_behaviors", {"cell_type": c}, structured("behaviors", behaviors(c)))
                           for i, c in enumerate(("default", "Tumor_Cell"))])
        out = json.loads(self.run_capture(content).read_text())
        self.assertEqual(list(out["behaviors"]), ["default", "Tumor_Cell"])

    def test_design_session_mismatch_refused(self):
        self.design["session"] = "other-session"
        self.write(self.design)
        self.refused(self.good(), "session")

    def test_folder_form_accepted(self):
        content = self.good()
        self.invoke(content)
        path = cd.capture_dictionary(self.root, INVOCATION.rsplit("/", 1)[0], self.capture, REL)
        self.assertEqual(json.loads(path.read_text())["invocation"], INVOCATION)

    def test_overwrite_message_says_new_purpose(self):
        self.run_capture(self.good())
        with self.assertRaisesRegex(ValueError, "new design purpose directory; never delete"):
            cd.capture_dictionary(self.root, INVOCATION, self.capture, REL)


if __name__ == "__main__":
    unittest.main()
