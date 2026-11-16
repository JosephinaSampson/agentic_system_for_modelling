"""DATA_INPUT dispatch checks: NeKo seeds and MaBoSS mappings consume only approved, hash-checked data."""
import json
import os
import re
import shutil
import subprocess
import sys
import time
import unittest
from unittest import mock

from scripts import gate, state
from scripts.data import mapping
from tests.data.test_jobs_holdout import HoldoutFixture
from tests.data.test_physicell_design import MABOSS as MC_MABOSS, REL as DESIGN_REL, SESSION as MC_SESSION
from tests.data.test_physicell_design import STAGE_A as MC_STAGE_A
from tests.data.test_physicell_projection import DICT as MC_DICT, PROJ as MC_PROJ, ProjectionBase
from tests.data.test_ode_mapping import SESSION as ODE_SESSION
from tests.data.test_mapping import (BNET, BNET_TEXT, NEKO, SOURCE, STANDALONE_BNET, STANDALONE_OUTPUTS,
                                     MappingFixture, run_cli, sha)

REFUSAL = "brief references data-analyst artifacts without a DATA_INPUT line; see CLAUDE.md"
HOOK = state.ROOT / ".claude" / "scripts" / "stage_gate_guard.py"
OUTPUTS = ["Proliferation", "Survival"]
STAGE_GATE = "STAGE_GATE " + json.dumps({"upstream_session_id": NEKO, "outputs": OUTPUTS})


class DataInputFixture(MappingFixture):
    SEEDS_REL = "runs/orchestrator/kras-seeds/seeds.json"

    def setUp(self):
        super().setUp()
        self.seeds = f"{self.base}/proposals/P3_seed_genes.csv"
        self.mapping_rel, self.mapping_data = self.complete()
        self.approve_mapping("dec-map-1", self.mapping_rel)
        for name in ("CURRENT_STATE.md", "VALIDATION_PLAN.md"):
            shutil.copyfile(state.ROOT / "templates" / name, self.root / name)

    def write_seeds(self, rel, symbols, cite=True):
        data = {"schema_version": 1,
                "sources": [{"id": "smith2024", "path": SOURCE, "sha256": sha(self.root / SOURCE)}],
                "seeds": [{"hgnc_symbol": s, "from": [{"source": "smith2024", "locator": "Table 1"}] if cite else []}
                          for s in symbols]}
        (self.root / rel).parent.mkdir(parents=True, exist_ok=True)
        self.save(rel, data)

    def approve_seeds(self, record_id, rel, outputs, stage="data_seeds", session_id="kras-seeds"):
        state.update(self.root, "record", {
            "id": record_id, "kind": "stage_approval", "stage": stage, "session_id": session_id,
            "outputs": outputs, "artifact": {"path": rel, "sha256": sha(self.root / rel)},
            "approved_by": "researcher", "authorization": "researcher: seeds ok"})

    def seeds_line(self, rel=None, digest=None, approval="dec-seeds-1", **extra):
        rel = rel or self.SEEDS_REL
        return "DATA_INPUT " + json.dumps({"seeds": rel, "sha256": digest or sha(self.root / rel),
                                           "approval": approval, **extra})

    def seeds_task(self, **kw):
        return f"{self.seeds_line(**kw)}\nBuild the network from the seeds file named above; the source is {SOURCE}."

    def approve_mapping(self, record_id, rel, outputs=("S1", "V1")):
        state.update(self.root, "record", {
            "id": record_id, "kind": "stage_approval", "stage": "data_mapping", "session_id": NEKO,
            "outputs": list(outputs), "artifact": {"path": rel, "sha256": sha(self.root / rel)},
            "approved_by": "researcher", "authorization": "researcher: mapping ok"})

    def neko_line(self, proposals=None, session=None):
        proposals = {"P3": sha(self.root / self.seeds)} if proposals is None else proposals
        return "DATA_INPUT " + json.dumps({"data_session": session or self.sid, "proposals": proposals})

    def neko_task(self, **kw):
        return f"{self.neko_line(**kw)}\nBuild the network from the seed file {self.seeds} (hgnc_symbol column)."

    def maboss_line(self, rel=None, digest=None, approval="dec-map-1"):
        rel = rel or self.mapping_rel
        return "DATA_INPUT " + json.dumps({"mapping": rel, "sha256": digest or sha(self.root / rel), "approval": approval})

    def maboss_task(self, gate_line=STAGE_GATE, **kw):
        return f"{gate_line}\n{self.maboss_line(**kw)}\nSimulate exactly the mapping's scenarios."

    def stage_gate_ready(self):
        """Make the NeKo session an exported, reviewed, approved upstream so check_task passes."""
        bnet = self.root / BNET
        manifest = {"source": {"server": "NeKo", "session_id": NEKO}, "schema_name": "mcp-biomodelling-handoff",
                    "schema_version": "1.0", "handoff_type": "neko-to-maboss",
                    "network": {"nodes": ["EGFR", "MAPK1", "Proliferation", "Survival"], "output_nodes": OUTPUTS},
                    "bnet_file": {"path": BNET, "server": "NeKo", "session_id": NEKO, "role": "neko_bnet",
                                  "name": bnet.name, "suffix": ".bnet", "media_type": "text/plain",
                                  "size_bytes": bnet.stat().st_size, "sha256": sha(bnet)}}
        handoff = self.root / "runs" / "network-curator" / NEKO / "handoff.json"
        handoff.write_text(json.dumps(manifest), encoding="utf-8")
        ref = {"path": f"runs/network-curator/{NEKO}/handoff.json", "sha256": sha(handoff)}
        state.update(self.root, "registry-add", dict(zip(state.REGISTRY, [
            "network-curator", NEKO, "topology", "active", "—", "pending", ref["path"]])))
        review = {"id": "review-neko", "kind": "stage_review", "session_id": NEKO, "stage": "topology",
                  "outcome": "approved", "artifact": ref}
        for role in ("scientific-reviewer", "reproducibility-auditor"):
            report = self.root / "runs" / f"{role}.md"
            report.write_text("independent bounded review", encoding="utf-8")
            review[role] = {"path": f"runs/{role}.md", "sha256": sha(report)}
        state.update(self.root, "record", review)
        state.update(self.root, "record", {
            "id": "approval-neko", "kind": "stage_approval", "session_id": NEKO, "stage": "topology",
            "downstream_stage": "boolean-dynamics", "outputs": OUTPUTS, "approved_by": "researcher",
            "authorization": "researcher approved this revision and outputs", "artifact": ref})
        state.update(self.root, "registry-handoff", {"session_id": NEKO, "handoff": "approved"})
        state.update(self.root, "registry-handoff", {"session_id": NEKO, "handoff": "exported", "artifact": ref})

    def hook(self, subagent_type, prompt):
        event = {"tool_name": "Agent", "tool_input": {"subagent_type": subagent_type, "prompt": prompt}}
        env = {**os.environ, "CLAUDE_PROJECT_DIR": str(self.root), "PYTHONPATH": str(state.ROOT)}
        return subprocess.run([sys.executable, str(HOOK)], input=json.dumps(event), text=True,
                              capture_output=True, env=env, timeout=60)


class NekoDataInputTests(DataInputFixture):
    def test_neko_data_input_ok(self):
        result = gate.check_data_input(self.root, "network_curator", self.neko_task())
        self.assertEqual(result["data_session"], self.sid)
        self.assertEqual(result["approval_id"], "dec-data-1")
        self.assertEqual(result["proposals"], {"P3": {"path": self.seeds, "sha256": sha(self.root / self.seeds)}})

    def restrict_first_approval(self, outputs):
        decisions = self.root / "DECISIONS.md"
        text = decisions.read_text(encoding="utf-8")
        pattern = r'"outputs": \[\s*"P1",\s*"P2",\s*"P3"\s*\]'
        self.assertRegex(text, pattern)
        decisions.write_text(re.sub(pattern, f'"outputs": {json.dumps(outputs)}', text, count=1), encoding="utf-8")

    def test_neko_approvals_are_cumulative(self):
        # P3 approved in record A and P4 in a later record B: both usable in one DATA_INPUT.
        self.approve("dec-data-2", ["P4"])
        seeds4 = f"{self.base}/proposals/P4_seed_genes.csv"
        proposals = {"P3": sha(self.root / self.seeds), "P4": sha(self.root / seeds4)}
        result = gate.check_data_input(self.root, "network_curator", self.neko_task(proposals=proposals))
        self.assertEqual(set(result["proposals"]), {"P3", "P4"})
        # A later partial approval does not revoke earlier ones.
        self.approve("dec-data-3", ["P1"])
        self.assertEqual(set(gate.check_data_input(self.root, "network_curator",
                                                   self.neko_task(proposals=proposals))["proposals"]), {"P3", "P4"})

    def test_neko_wrong_hash_refused(self):
        with self.assertRaisesRegex(ValueError, "sha256"):
            gate.check_data_input(self.root, "network_curator", self.neko_task(proposals={"P3": "0" * 64}))
        for bad in ({}, {"P3": "abc"}, {"P3": 7}, ["P3"]):
            with self.assertRaises(ValueError):
                gate.check_data_input(self.root, "network_curator", self.neko_task(proposals=bad))
        # The seed file changed on disk after approval: no longer the approved handoff artifact.
        line = self.neko_line()
        (self.root / self.seeds).write_text("hgnc_symbol,tier,evidence\nKRAS,core,lit\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "approved handoff"):
            gate.check_data_input(self.root, "network_curator", line)

    def test_neko_unapproved_proposal_refused(self):
        with self.assertRaisesRegex(ValueError, "P9"):
            gate.check_data_input(self.root, "network_curator", self.neko_task(proposals={"P9": "0" * 64}))
        with self.assertRaisesRegex(ValueError, "no researcher stage_approval"):
            gate.check_data_input(self.root, "network_curator", self.neko_task(session="data-other-session"))
        # P3 listed only in a record that binds a stale handoff sha is refused.
        self.restrict_first_approval(["P1"])
        handoff = self.root / self.base / "handoff.json"
        original = handoff.read_text(encoding="utf-8")
        handoff.write_text(original + " ", encoding="utf-8")
        self.approve("dec-data-stale", ["P3"])
        handoff.write_text(original, encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "not in the approved outputs"):
            gate.check_data_input(self.root, "network_curator", self.neko_task())
        # P3 never listed at all.
        self.approve("dec-data-2", ["P1", "P2"])
        with self.assertRaisesRegex(ValueError, "not in the approved outputs"):
            gate.check_data_input(self.root, "network_curator", self.neko_task())
        # A handoff changed after approval invalidates the approval.
        handoff = self.root / self.base / "handoff.json"
        handoff.write_text(handoff.read_text(encoding="utf-8") + "\n\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "changed since approval"):
            gate.check_data_input(self.root, "network_curator", self.neko_task())

    def test_neko_wrong_kind_or_layer_refused(self):
        p1 = f"{self.base}/proposals/P1_perturbations.csv"
        with self.assertRaisesRegex(ValueError, "seed_genes"):
            gate.check_data_input(self.root, "network_curator",
                                  self.neko_task(proposals={"P1": sha(self.root / p1)}))
        entry = {"id": "P3", "kind": "seed_genes", "target_layer": "maboss", "path": self.seeds,
                 "sha256": sha(self.root / self.seeds)}
        with mock.patch.object(gate.approvals, "require_proposal_approved", return_value=entry):
            with self.assertRaisesRegex(ValueError, "neko"):
                gate.check_data_input(self.root, "network_curator", self.neko_task())

    def test_neko_handoff_must_validate_now(self):
        with mock.patch("scripts.codex.validate_handoff.validate_handoff", side_effect=ValueError("broken")):
            with self.assertRaisesRegex(ValueError, "no longer validates"):
                gate.check_data_input(self.root, "network_curator", self.neko_task())

    def test_neko_malformed_line_refused(self):
        for line in ("DATA_INPUT [1]", "DATA_INPUT {", 'DATA_INPUT {"data_session": "x"}',
                     "DATA_INPUT " + json.dumps({"data_session": self.sid, "proposals": {}, "extra": 1})):
            with self.assertRaises(ValueError):
                gate.check_data_input(self.root, "network_curator", line)
        # Line 1 only, never repeated or elsewhere.
        with self.assertRaisesRegex(ValueError, "line 1"):
            gate.check_data_input(self.root, "network_curator", "Build a network.\n" + self.neko_line())
        with self.assertRaisesRegex(ValueError, "line 1"):
            gate.check_data_input(self.root, "network_curator", self.neko_line() + "\n" + self.neko_line())

    def test_neko_data_path_of_other_session_refused(self):
        task = self.neko_task() + "\nAlso read runs/data-analyst/data-other-session/proposals/P1.csv."
        with self.assertRaisesRegex(ValueError, "data-other-session"):
            gate.check_data_input(self.root, "network_curator", task)


class MabossDataInputTests(DataInputFixture):
    def setUp(self):
        super().setUp()
        self.stage_gate_ready()

    def test_maboss_mapping_ok(self):
        result = gate.check_data_input(self.root, "boolean_dynamics_modeler", self.maboss_task())
        self.assertEqual(result, {"mapping": self.mapping_rel, "sha256": sha(self.root / self.mapping_rel),
                                  "approval_id": "dec-map-1", "neko_session": NEKO})
        # The mapping's own data paths may be named in the brief.
        task = self.maboss_task() + f"\nInputs: {self.base}/proposals/P1_perturbations.csv"
        self.assertEqual(gate.check_data_input(self.root, "boolean_dynamics_modeler", task)["approval_id"], "dec-map-1")

    def test_maboss_unapproved_mapping_refused(self):
        with self.assertRaisesRegex(ValueError, "data_mapping"):
            gate.check_data_input(self.root, "boolean_dynamics_modeler", self.maboss_task(approval="dec-data-1"))
        with self.assertRaisesRegex(ValueError, "not found"):
            gate.check_data_input(self.root, "boolean_dynamics_modeler", self.maboss_task(approval="nope"))
        with self.assertRaisesRegex(ValueError, "sha256"):
            gate.check_data_input(self.root, "boolean_dynamics_modeler", self.maboss_task(digest="0" * 64))
        other, _ = self.complete("other")
        with self.assertRaisesRegex(ValueError, "artifact must be"):
            gate.check_data_input(self.root, "boolean_dynamics_modeler", self.maboss_task(rel=other))
        # Changed after approval.
        task = self.maboss_task()
        data = dict(self.mapping_data, unmapped=self.mapping_data["unmapped"][:1])
        self.save(self.mapping_rel, data)
        with self.assertRaisesRegex(ValueError, "changed since approval"):
            gate.check_data_input(self.root, "boolean_dynamics_modeler", task)
        for bad in ('DATA_INPUT {"mapping": "x"}', "DATA_INPUT []", "DATA_INPUT " + json.dumps(
                {"mapping": self.mapping_rel, "sha256": "0" * 64, "approval": "dec-map-1", "x": 1})):
            with self.assertRaises(ValueError):
                gate.check_data_input(self.root, "boolean_dynamics_modeler", f"{STAGE_GATE}\n{bad}")

    def test_maboss_mapping_failing_check_refused(self):
        draft = self.draft("todo")  # approved as drafted: unmapped reasons are still TODO
        self.approve_mapping("dec-map-todo", draft)
        with self.assertRaisesRegex(ValueError, "TODO"):
            gate.check_data_input(self.root, "boolean_dynamics_modeler",
                                  self.maboss_task(rel=draft, approval="dec-map-todo"))
        # The mapping is unchanged but its network changed since: check fails at dispatch time.
        task = self.maboss_task()
        (self.root / BNET).write_text(BNET_TEXT + "Extra, EGFR\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "mapping check failed"):
            gate.check_data_input(self.root, "boolean_dynamics_modeler", task)

    def test_maboss_approval_outputs_must_equal_mapping_ids(self):
        for record_id, outputs in (("dec-map-zzz", ["ZZZ"]), ("dec-map-s1", ["S1"]),
                                   ("dec-map-extra", ["S1", "V1", "V2"])):
            self.approve_mapping(record_id, self.mapping_rel, outputs)
            with self.assertRaisesRegex(ValueError, "outputs"):
                gate.check_data_input(self.root, "boolean_dynamics_modeler", self.maboss_task(approval=record_id))
        self.approve_mapping("dec-map-reordered", self.mapping_rel, ["V1", "S1"])
        self.assertEqual(gate.check_data_input(self.root, "boolean_dynamics_modeler",
                                               self.maboss_task(approval="dec-map-reordered"))["approval_id"],
                         "dec-map-reordered")

    def test_maboss_network_mismatch_with_stage_gate_refused(self):
        other = "STAGE_GATE " + json.dumps({"upstream_session_id": "neko-session-2", "outputs": OUTPUTS})
        with self.assertRaisesRegex(ValueError, "neko-session-2"):
            gate.check_data_input(self.root, "boolean_dynamics_modeler", self.maboss_task(gate_line=other))
        standalone = "STAGE_GATE " + json.dumps({"standalone_decision_id": "sa-1", "outputs": OUTPUTS})
        with self.assertRaisesRegex(ValueError, "upstream_session_id"):
            gate.check_data_input(self.root, "boolean_dynamics_modeler", self.maboss_task(gate_line=standalone))
        with self.assertRaisesRegex(ValueError, "line 2"):
            gate.check_data_input(self.root, "boolean_dynamics_modeler", self.maboss_line())


    def test_maboss_mapping_bnet_must_be_the_approved_one(self):
        other = self.root / f"runs/network-curator/{NEKO}/exports/inv2/draft.bnet"
        other.parent.mkdir(parents=True)
        other.write_text(BNET_TEXT + "KRAS, KRAS\n", encoding="utf-8")
        rel = self.draft("other")
        data = self.load(rel)
        data["inputs"]["network"] = {"neko_session": NEKO, "bnet": other.relative_to(self.root).as_posix(),
                                     "sha256": sha(other)}
        data["contexts"] = {"LUAD_EGFR": [{"node": "KRAS", "state": 1}], "LUAD_wt": []}
        for key in ("perturbations", "readouts", "criteria", "unmapped"):
            data[key] = self.mapping_data[key]
        self.save(rel, data)
        self.assertEqual(mapping.check(self.root, rel), [])
        self.approve_mapping("dec-map-2", rel)
        task = self.maboss_task(rel=rel, approval="dec-map-2")
        with self.assertRaisesRegex(ValueError, "not the BNET of the approved NeKo handoff"):
            gate.check_data_input(self.root, "boolean_dynamics_modeler", task)
        self.assertEqual(self.hook("boolean-dynamics-modeler", task).returncode, 2)
        self.assertEqual(gate.check_data_input(self.root, "boolean_dynamics_modeler", self.maboss_task())["approval_id"],
                         "dec-map-1")

    def test_maboss_approval_session_must_be_the_neko_session(self):
        state.update(self.root, "record", {
            "id": "dec-map-bad", "kind": "stage_approval", "stage": "data_mapping", "session_id": "neko-session-2",
            "outputs": ["S1", "V1"], "artifact": {"path": self.mapping_rel, "sha256": sha(self.root / self.mapping_rel)},
            "approved_by": "researcher", "authorization": "researcher: mapping ok"})
        with self.assertRaisesRegex(ValueError, "session_id"):
            gate.check_data_input(self.root, "boolean_dynamics_modeler", self.maboss_task(approval="dec-map-bad"))


class MabossGateOnceTests(DataInputFixture):
    def test_stage_gate_checked_once_per_dispatch(self):
        # PR #17 review 4586113358: check_mapping_network's STAGE_GATE result is reused by the hook
        self.stage_gate_ready()
        for name, prompt in (("mapping", self.maboss_task()), ("STAGE_GATE only", f"{STAGE_GATE}\nSimulate.")):
            with self.subTest(form=name):
                status, err, tasks, checks = gate_calls(self.root, "boolean-dynamics-modeler", prompt)
                self.assertEqual((status, tasks, checks), (0, 1, 1), err)


class ScopeTests(DataInputFixture):
    def test_data_path_without_data_input_refused(self):
        prose = f"see {self.seeds} for the seeds"
        for specialist, task in (("network_curator", f"Build a network.\n{prose}"),
                                 ("network_curator", prose),
                                 ("boolean_dynamics_modeler", f"{STAGE_GATE}\nSimulate; {prose}")):
            with self.assertRaises(ValueError) as caught:
                gate.check_data_input(self.root, specialist, task)
            self.assertEqual(str(caught.exception), REFUSAL)

    def test_noncanonical_data_input_refused(self):
        line = self.maboss_line(digest="0" * 64)
        body = line[len("DATA_INPUT"):]
        variants = [f"`{line}`", f"> {line}", f"\ufeff{line}", f" {line}", "data_input" + body, "DATA-INPUT" + body,
                    "DATAINPUT" + body, "Data_Input" + body, "DATA\u200b_INPUT" + body, "DATA\uff3fINPUT" + body]
        for variant in variants:
            with self.assertRaises(ValueError, msg=repr(variant)):
                gate.check_data_input(self.root, "boolean_dynamics_modeler", f"{STAGE_GATE}\n{variant}\nSimulate.")
            with self.assertRaises(ValueError, msg=repr(variant)):
                gate.check_data_input(self.root, "network_curator", f"Build.\n{variant}")
        # The canonical line plus another payload-like mention (one starting a line or
        # followed by `{`) is refused; a prose mention is allowed (ProseMentionTests).
        for extra in ("DATA-INPUT", "- data_input: the mapping", "see `data_input` = {}"):
            with self.assertRaises(ValueError, msg=extra):
                gate.check_data_input(self.root, "boolean_dynamics_modeler", f"{self.maboss_task()}\n{extra}")
            with self.assertRaises(ValueError, msg=extra):
                gate.check_data_input(self.root, "network_curator", f"{self.neko_task()}\n{extra}")
        # Ordinary prose "data input" (with a space) is not a DATA_INPUT spelling.
        self.assertIsNone(gate.check_data_input(self.root, "network_curator", "No data input is used."))

    def test_data_session_id_or_bare_dir_without_data_input_refused(self):
        for specialist, prefix in (("network_curator", "Build.\n"), ("boolean_dynamics_modeler", f"{STAGE_GATE}\n")):
            for prose in (f"seeds come from data session {self.sid}", "outputs live in runs/data-analyst",
                          "see runs\\data-analyst\\x", "see RUNS/Data-Analyst/x", "data-20990101T000000Z-deadbeef"):
                with self.assertRaises(ValueError, msg=prose) as caught:
                    gate.check_data_input(self.root, specialist, prefix + prose)
                self.assertEqual(str(caught.exception), REFUSAL)
        # With a DATA_INPUT line, another data session id is out of scope.
        with self.assertRaisesRegex(ValueError, "data-20990101T000000Z-deadbeef"):
            gate.check_data_input(self.root, "network_curator",
                                  self.neko_task() + "\nalso data-20990101T000000Z-deadbeef")

    def test_other_specialists_unaffected(self):
        task = f"Review the evidence; see {self.seeds}.\nDATA_INPUT not-json"
        for specialist in ("literature_reviewer", "data_analyst", "scientific_reviewer", "reproducibility_auditor"):
            self.assertIsNone(gate.check_data_input(self.root, specialist, task))
        result = self.hook("literature-reviewer", task)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_no_data_unchanged(self):
        for specialist, task in (("network_curator", "Build a network from EGFR, KRAS.\nNo BNET/handoff export."),
                                 ("network_curator", ""),
                                 ("boolean_dynamics_modeler", f"{STAGE_GATE}\nImport and simulate.")):
            self.assertIsNone(gate.check_data_input(self.root, specialist, task))
        result = self.hook("network-curator", "Build a network from EGFR, KRAS.")
        self.assertEqual(result.returncode, 0, result.stderr)


class HookTests(DataInputFixture):
    def test_hook_neko_allow_and_deny(self):
        result = self.hook("network-curator", self.neko_task())
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.hook("network-curator", f"Build a network from {self.seeds}.")
        self.assertEqual(result.returncode, 2)
        self.assertIn(REFUSAL, result.stderr)
        result = self.hook("plugin:network-curator", self.neko_task(proposals={"P3": "0" * 64}))
        self.assertEqual(result.returncode, 2)
        self.assertIn("sha256", result.stderr)
        for prompt in (["DATA_INPUT"], 7, "DATA_INPUT " + "[" * 100000):
            result = self.hook("network-curator", prompt)
            self.assertEqual(result.returncode, 2, result.stderr)

    def test_hook_fails_closed_on_malformed_prompts(self):
        deep = {"boolean-dynamics-modeler": "STAGE_GATE " + "[" * 100000,
                "multicellular-configurator": "STAGE_GATE " + "[" * 100000,
                "network-curator": "DATA_INPUT " + "[" * 100000}
        for role in deep:
            for prompt in (7, ["x"], {"a": 1}, True, deep[role]):
                result = self.hook(role, prompt)
                self.assertEqual(result.returncode, 2, (role, repr(prompt)[:40], result.stderr[-300:]))
                self.assertIn("blocked", result.stderr)
        self.assertEqual(self.hook("network-curator", None).returncode, 0)

    def test_hook_maboss_allow_and_deny(self):
        # STAGE_GATE is checked first: with no exported upstream, the DATA_INPUT line cannot help.
        result = self.hook("boolean-dynamics-modeler", self.maboss_task())
        self.assertEqual(result.returncode, 2)
        self.assertIn("registry", result.stderr)
        self.stage_gate_ready()
        result = self.hook("boolean-dynamics-modeler", self.maboss_task())
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.hook("boolean-dynamics-modeler", f"{STAGE_GATE}\nSimulate; seeds at {self.seeds}.")
        self.assertEqual(result.returncode, 2)
        self.assertIn(REFUSAL, result.stderr)
        result = self.hook("boolean-dynamics-modeler", self.maboss_task(approval="dec-data-1"))
        self.assertEqual(result.returncode, 2)
        self.assertIn("data_mapping", result.stderr)


class ProseMentionTests(DataInputFixture):
    """Only the canonical line may carry a DATA_INPUT payload; prose mentions are allowed."""

    PROSE = "as defined in the DATA_INPUT mapping"

    def setUp(self):
        super().setUp()
        self.stage_gate_ready()

    def live_brief(self, extra=""):
        """The live-test brief shape: STAGE_GATE, canonical DATA_INPUT, prose mention on line 9."""
        body = ["Import the approved NeKo handoff and simulate the mapped scenarios.",
                "Use the session's BNET as exported; do not edit rules.",
                "Run each scenario with 5000 trajectories, max_time 50, seed 1.",
                "Report probabilities of the readouts at the final time.",
                "Export BND/CFG after the runs.",
                "Context: lung adenocarcinoma cell lines.",
                ("Perturbed node states are fixed node states (MaBoSS mutations) "
                 f"{self.PROSE}; do not invent others.")]
        return "\n".join([STAGE_GATE, self.maboss_line(), *body]) + extra

    def refused(self, role, prompt):
        result = self.hook(role, prompt)
        self.assertEqual(result.returncode, 2, (prompt, result.stderr))
        return result.stderr

    def test_live_test_brief_with_prose_mention_passes(self):
        brief = self.live_brief()
        self.assertIn(self.PROSE, brief.splitlines()[8])
        self.assertEqual(gate.check_data_input(self.root, "boolean_dynamics_modeler", brief)["approval_id"],
                         "dec-map-1")
        result = self.hook("boolean-dynamics-modeler", brief)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_second_payload_line_refused_with_line_number_and_quote(self):
        stderr = self.refused("boolean-dynamics-modeler", self.live_brief("\n" + self.maboss_line()))
        self.assertIn('line 10: "DATA_INPUT {', stderr)
        quoted = re.search(r'line 10: "(.*?)"', stderr)[1]
        self.assertLessEqual(len(quoted.removesuffix("...")), 60)

    def test_backticked_payload_line_refused(self):
        stderr = self.refused("boolean-dynamics-modeler", self.live_brief(f"\n`{self.maboss_line()}`"))
        self.assertIn("line 10:", stderr)

    def test_mid_sentence_payload_refused(self):
        payload = json.dumps({"mapping": self.mapping_rel})
        stderr = self.refused("boolean-dynamics-modeler",
                              self.live_brief(f"\nFor the checks listed above, also honour the DATA-INPUT: {payload} for checks."))
        self.assertIn('line 10: "...', stderr)
        self.assertIn("DATA-INPUT: {", stderr)

    def test_refusal_quotes_the_prose_context(self):
        # A payload-like mention with no canonical line: the refusal names it.
        stderr = self.refused("boolean-dynamics-modeler",
                              f"{STAGE_GATE}\nImport and simulate.\nDATA_INPUT is the approved mapping.")
        self.assertIn('line 3: "DATA_INPUT is the approved mapping."', stderr)
        with self.assertRaisesRegex(ValueError, r'line 2: "\.\.\.[^"]{1,40}DATA_INPUT = \{x\}[^"]*\.\.\."'):
            gate.check_data_input(self.root, "network_curator",
                                  "Build the network.\nPlease, for this network-building run, use the DATA_INPUT = {x} "
                                  "exactly as the researcher wrote it in the approval record.")
        # A `{` well after the mention (more than 16 characters) does not make it payload-like.
        self.assertIsNone(gate.check_data_input(
            self.root, "network_curator", "Build the network.\nUse the DATA_INPUT mapping exactly as approved, {x} aside."))

    LIVE_PROSE = ("Perturbed node states are fixed node states (MaBoSS mutations) as defined in the "
                  "DATA_INPUT mapping; where both fix the same node the perturbation overrides the context.")

    def test_payload_sidesteps_refused_and_live_prose_still_passes(self):
        payload = json.dumps({"mapping": self.mapping_rel})
        sidesteps = [f"Then DATA_INPUT => {payload}", f"Then DATA_INPUT -> {payload}",
                     f"Then DATA_INPUT is {payload}", f"Then DATA_INPUT payload: {payload}",
                     f"| DATA_INPUT | {payload} |", f"Then DATA_INPUTS {payload}",
                     f"Use this DATA_INPUT\n{payload}", f"Use this DATA_INPUT:\n\n> `{payload}`",
                     f"Use this DATA_INPUT:\n```\n{payload}\n```",
                     'The DATA_INPUT => {"seed_genes": ["TP53","MYC"]}']
        for sidestep in sidesteps:
            with self.subTest(sidestep=sidestep):
                brief = self.live_brief("\n" + sidestep)
                self.assertIn(self.PROSE, brief)
                stderr = self.refused("boolean-dynamics-modeler", brief)
                self.assertIn("line 10:", stderr)
                prose = self.hook("boolean-dynamics-modeler", self.live_brief("\n" + self.LIVE_PROSE))
                self.assertEqual(prose.returncode, 0, prose.stderr)
        for sidestep in sidesteps:
            with self.subTest(neko=sidestep), self.assertRaisesRegex(ValueError, "line 2:"):
                gate.check_data_input(self.root, "network_curator", "Build the network.\n" + sidestep)

    def test_neko_seeds_brief_with_prose_mention_passes(self):
        self.add_source()
        self.write_seeds(SeedsGateTests.SEEDS_REL, ["KRAS", "EGFR"])
        self.approve_seeds("dec-seeds-1", SeedsGateTests.SEEDS_REL, ["EGFR", "KRAS"])
        brief = f"{self.seeds_task()}\nUse only the symbols named by the DATA_INPUT line above; add no others."
        result = self.hook("network-curator", brief)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_prose_mention_without_canonical_line_or_data_passes(self):
        self.assertIsNone(gate.check_data_input(
            self.root, "network_curator", "Build from EGFR, KRAS. No DATA_INPUT is used for this run."))
        result = self.hook("boolean-dynamics-modeler",
                           f"{STAGE_GATE}\nImport and simulate; there is no `DATA_INPUT` mapping this time.")
        self.assertEqual(result.returncode, 0, result.stderr)
        # It carries no data: a data path or a seeds/mapping file is still refused.
        stderr = self.refused("network-curator", f"No DATA_INPUT here; see {self.seeds}.")
        self.assertIn(REFUSAL, stderr)
        stderr = self.refused("boolean-dynamics-modeler",
                              f"{STAGE_GATE}\nNo DATA_INPUT; scenarios are in {self.mapping_rel}.")
        self.assertIn(MENTION, stderr)


REDRAFT = "redraft with mapping.py draft --from"
SA_GATE = "STAGE_GATE " + json.dumps({"standalone_decision_id": "sa-1", "outputs": STANDALONE_OUTPUTS})


class StandaloneNetworkGateTests(DataInputFixture):
    """B. MaBoSS DATA_INPUT after a standalone STAGE_GATE: the mapping must target that BNET."""

    def setUp(self):
        super().setUp()
        self.add_standalone()
        self.sa_rel, self.sa_data = self.complete_sources(
            "sa", network={"standalone_decision_id": "sa-1", "bnet": STANDALONE_BNET})
        self.approve_sa_mapping("dec-map-sa", self.sa_rel)

    def approve_sa_mapping(self, record_id, rel, session_id="sa-1"):
        state.update(self.root, "record", {
            "id": record_id, "kind": "stage_approval", "stage": "data_mapping", "session_id": session_id,
            "outputs": ["S1", "V1"], "artifact": {"path": rel, "sha256": sha(self.root / rel)},
            "approved_by": "researcher", "authorization": "researcher: mapping ok"})

    def sa_task(self, gate_line=SA_GATE, rel=None, approval="dec-map-sa"):
        return self.maboss_task(gate_line=gate_line, rel=rel or self.sa_rel, approval=approval)

    def test_standalone_mapping_accepted_by_check_and_hook(self):
        result = gate.check_data_input(self.root, "boolean_dynamics_modeler", self.sa_task())
        self.assertEqual(result, {"mapping": self.sa_rel, "sha256": sha(self.root / self.sa_rel),
                                  "approval_id": "dec-map-sa", "standalone_decision_id": "sa-1"})
        hook = self.hook("boolean-dynamics-modeler", self.sa_task())
        self.assertEqual(hook.returncode, 0, hook.stderr)
        self.assertEqual(mapping.check(self.root, self.sa_rel, stage_gate=SA_GATE), [])

    def test_standalone_mismatched_sha_refused(self):
        # The standalone BNET changed after its authorization: the record and the mapping no longer bind.
        task = self.sa_task()
        (self.root / STANDALONE_BNET).write_text(BNET_TEXT + "# edited\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            gate.check_data_input(self.root, "boolean_dynamics_modeler", task)
        self.assertEqual(self.hook("boolean-dynamics-modeler", task).returncode, 2)

    def test_standalone_different_id_refused(self):
        self.add_standalone("sa-2", "inputs/models/other.bnet", BNET_TEXT + "KRAS, KRAS\n")
        other = "STAGE_GATE " + json.dumps({"standalone_decision_id": "sa-2", "outputs": STANDALONE_OUTPUTS})
        with self.assertRaisesRegex(ValueError, "sa-2") as caught:
            gate.check_data_input(self.root, "boolean_dynamics_modeler", self.sa_task(gate_line=other))
        self.assertTrue(str(caught.exception).endswith(
            "check the STAGE_GATE record, or, if the network was re-authorized, redraft with mapping.py draft --from"),
            str(caught.exception))
        self.assertEqual(self.hook("boolean-dynamics-modeler", self.sa_task(gate_line=other)).returncode, 2)
        self.assertError(mapping.check(self.root, self.sa_rel, stage_gate=other), "sa-2")
        # The approval's session_id must be the standalone decision id.
        self.approve_sa_mapping("dec-map-sa-bad", self.sa_rel, session_id="sa-2")
        with self.assertRaisesRegex(ValueError, "session_id"):
            gate.check_data_input(self.root, "boolean_dynamics_modeler", self.sa_task(approval="dec-map-sa-bad"))

    def test_standalone_wrong_record_kind_refused(self):
        self.add_standalone("sa-stage", STANDALONE_BNET, kind="stage_approval", stage="boolean-dynamics")
        wrong = "STAGE_GATE " + json.dumps({"standalone_decision_id": "sa-stage", "outputs": STANDALONE_OUTPUTS})
        with self.assertRaises(ValueError):
            gate.check_data_input(self.root, "boolean_dynamics_modeler", self.sa_task(gate_line=wrong))
        self.assertEqual(self.hook("boolean-dynamics-modeler", self.sa_task(gate_line=wrong)).returncode, 2)

    def test_standalone_mapping_with_neko_gate_refused(self):
        self.stage_gate_ready()
        with self.assertRaisesRegex(ValueError, "standalone_decision_id"):
            gate.check_data_input(self.root, "boolean_dynamics_modeler", self.sa_task(gate_line=STAGE_GATE))
        # The NeKo-upstream route is unchanged.
        self.assertEqual(gate.check_data_input(self.root, "boolean_dynamics_modeler", self.maboss_task())["approval_id"],
                         "dec-map-1")


class SeedsGateTests(DataInputFixture):
    """C. NeKo DATA_INPUT naming an approved seeds.json built from researcher sources."""

    def setUp(self):
        super().setUp()
        self.add_source()
        self.write_seeds(self.SEEDS_REL, ["KRAS", "EGFR"])
        self.approve_seeds("dec-seeds-1", self.SEEDS_REL, ["EGFR", "KRAS"])

    def test_seeds_form_accepted_through_hook(self):
        result = gate.check_data_input(self.root, "network_curator", self.seeds_task())
        self.assertEqual(result, {"seeds": self.SEEDS_REL, "sha256": sha(self.root / self.SEEDS_REL),
                                  "approval_id": "dec-seeds-1", "symbols": ["KRAS", "EGFR"]})
        hook = self.hook("network-curator", self.seeds_task())
        self.assertEqual(hook.returncode, 0, hook.stderr)

    def test_seeds_wrong_sha_refused(self):
        with self.assertRaisesRegex(ValueError, "sha256"):
            gate.check_data_input(self.root, "network_curator", self.seeds_task(digest="0" * 64))
        for bad in ("abc", 7, "A" * 64):
            with self.assertRaises(ValueError):
                gate.check_data_input(self.root, "network_curator", self.seeds_task(digest=bad))
        task = self.seeds_task()
        self.write_seeds(self.SEEDS_REL, ["KRAS", "EGFR", "BRAF"])
        with self.assertRaisesRegex(ValueError, "sha256"):
            gate.check_data_input(self.root, "network_curator", task)
        result = self.hook("network-curator", task)
        self.assertEqual(result.returncode, 2)

    def test_seeds_wrong_stage_refused(self):
        self.approve_seeds("dec-seeds-map", self.SEEDS_REL, ["EGFR", "KRAS"], stage="data_mapping")
        with self.assertRaisesRegex(ValueError, "data_seeds"):
            gate.check_data_input(self.root, "network_curator", self.seeds_task(approval="dec-seeds-map"))
        with self.assertRaisesRegex(ValueError, "data_seeds"):
            gate.check_data_input(self.root, "network_curator", self.seeds_task(approval="dec-data-1"))
        with self.assertRaisesRegex(ValueError, "not found"):
            gate.check_data_input(self.root, "network_curator", self.seeds_task(approval="nope"))

    def test_seeds_outputs_must_equal_symbols(self):
        for record_id, outputs in (("dec-seeds-less", ["KRAS"]), ("dec-seeds-more", ["KRAS", "EGFR", "BRAF"])):
            self.approve_seeds(record_id, self.SEEDS_REL, outputs)
            with self.assertRaisesRegex(ValueError, "outputs"):
                gate.check_data_input(self.root, "network_curator", self.seeds_task(approval=record_id))

    def test_seeds_session_id_must_be_purpose(self):
        self.approve_seeds("dec-seeds-p", self.SEEDS_REL, ["EGFR", "KRAS"], session_id="other-purpose")
        with self.assertRaisesRegex(ValueError, "session_id"):
            gate.check_data_input(self.root, "network_curator", self.seeds_task(approval="dec-seeds-p"))

    def test_seeds_failing_check_refused(self):
        rel = "runs/orchestrator/uncited/seeds.json"
        self.write_seeds(rel, ["KRAS"], cite=False)
        self.approve_seeds("dec-seeds-bad", rel, ["KRAS"], session_id="uncited")
        with self.assertRaisesRegex(ValueError, "seeds check failed"):
            gate.check_data_input(self.root, "network_curator", self.seeds_task(rel=rel, approval="dec-seeds-bad"))
        with self.assertRaisesRegex(ValueError, "runs/orchestrator"):
            gate.check_data_input(self.root, "network_curator", self.seeds_task(rel=SOURCE, approval="dec-seeds-1"))

    def test_both_forms_or_mixed_keys_refused(self):
        both = self.seeds_line(data_session=self.sid, proposals={"P3": sha(self.root / self.seeds)})
        mixed = "DATA_INPUT " + json.dumps({"seeds": self.SEEDS_REL, "sha256": sha(self.root / self.SEEDS_REL),
                                            "proposals": {"P3": sha(self.root / self.seeds)}})
        partial = "DATA_INPUT " + json.dumps({"seeds": self.SEEDS_REL, "approval": "dec-seeds-1"})
        for line in (both, mixed, partial, f"{self.seeds_line()}\n{self.neko_line()}"):
            with self.assertRaisesRegex(ValueError, "DATA_INPUT", msg=line):
                gate.check_data_input(self.root, "network_curator", line)
            self.assertEqual(self.hook("network-curator", line).returncode, 2)

    def test_seeds_form_scope_refuses_data_sessions(self):
        task = self.seeds_task() + f"\nAlso see {self.seeds}."
        with self.assertRaisesRegex(ValueError, "outside its DATA_INPUT"):
            gate.check_data_input(self.root, "network_curator", task)

    def test_seeds_stage_is_a_governance_stage(self):
        self.assertIn("data_seeds", state.GOVERNANCE_STAGES)


class StaleMappingTests(DataInputFixture):
    """D. A NeKo re-export makes the approved mapping stale; check and gate say how to recover."""

    NEW_BNET = f"runs/network-curator/{NEKO}/exports/inv2/model.bnet"

    def setUp(self):
        super().setUp()
        self.stage_gate_ready()

    def reexport(self, text=BNET_TEXT + "# re-exported\n"):
        """A new approved NeKo handoff of the same session whose BNET differs."""
        bnet = self.root / self.NEW_BNET
        bnet.parent.mkdir(parents=True)
        bnet.write_text(text, encoding="utf-8")
        handoff = self.root / "runs" / "network-curator" / NEKO / "handoff.json"
        manifest = json.loads(handoff.read_text(encoding="utf-8"))
        manifest["bnet_file"].update(path=self.NEW_BNET, size_bytes=bnet.stat().st_size, sha256=sha(bnet))
        second = handoff.with_name("handoff-2.json")
        second.write_text(json.dumps(manifest), encoding="utf-8")
        ref = {"path": f"runs/network-curator/{NEKO}/handoff-2.json", "sha256": sha(second)}
        review = {"id": "review-neko-2", "kind": "stage_review", "session_id": NEKO, "stage": "topology",
                  "outcome": "approved", "artifact": ref}
        for role in ("scientific-reviewer", "reproducibility-auditor"):
            review[role] = {"path": f"runs/{role}.md", "sha256": sha(self.root / "runs" / f"{role}.md")}
        state.update(self.root, "record", review)
        state.update(self.root, "record", {
            "id": "approval-neko-2", "kind": "stage_approval", "session_id": NEKO, "stage": "topology",
            "downstream_stage": "boolean-dynamics", "outputs": OUTPUTS, "approved_by": "researcher",
            "authorization": "researcher approved the re-export", "artifact": ref})

    def test_check_with_stage_gate(self):
        self.assertEqual(mapping.check(self.root, self.mapping_rel, stage_gate=STAGE_GATE), [])
        self.assertEqual(mapping.check(self.root, self.mapping_rel,
                                       stage_gate=STAGE_GATE[len("STAGE_GATE "):]), [])
        self.reexport()
        errors = mapping.check(self.root, self.mapping_rel, stage_gate=STAGE_GATE)
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("not the BNET of the approved NeKo handoff", errors[0])
        self.assertTrue(errors[0].endswith(REDRAFT), errors)
        self.assertEqual(mapping.check(self.root, self.mapping_rel), [])  # without --stage-gate: not detectable
        code, out, err = run_cli("check", self.mapping_rel, "--stage-gate", STAGE_GATE[len("STAGE_GATE "):])
        self.assertEqual(code, 1)
        self.assertIn(REDRAFT, err)
        bad = mapping.check(self.root, self.mapping_rel, stage_gate="{not json")
        self.assertEqual(len(bad), 1)
        self.assertIn("STAGE_GATE", bad[0])

    def test_gate_refusal_carries_hint_and_redraft_recovers(self):
        self.reexport()
        with self.assertRaises(ValueError) as caught:
            gate.check_data_input(self.root, "boolean_dynamics_modeler", self.maboss_task())
        self.assertIn("not the BNET of the approved NeKo handoff", str(caught.exception))
        self.assertTrue(str(caught.exception).endswith(REDRAFT), str(caught.exception))
        result = self.hook("boolean-dynamics-modeler", self.maboss_task())
        self.assertEqual(result.returncode, 2)
        self.assertIn(REDRAFT, result.stderr)
        path, notes = mapping.redraft(self.root, self.mapping_rel, "luad-r2", (NEKO, self.NEW_BNET))
        self.assertEqual(notes, [])
        new_rel = path.relative_to(self.root).as_posix()
        self.assertEqual(mapping.check(self.root, new_rel, stage_gate=STAGE_GATE), [])
        self.approve_mapping("dec-map-r2", new_rel)
        task = self.maboss_task(rel=new_rel, approval="dec-map-r2")
        self.assertEqual(gate.check_data_input(self.root, "boolean_dynamics_modeler", task)["approval_id"], "dec-map-r2")
        self.assertEqual(self.hook("boolean-dynamics-modeler", task).returncode, 0)


class ReadoutOutputsTests(DataInputFixture):
    """A mapping may read only nodes the STAGE_GATE approved as MaBoSS outputs."""

    def widen(self, rel, data, record_id, approve):
        data = json.loads(json.dumps(data))
        data["readouts"][0]["nodes"].append("MAPK1")  # a BNET node, not an approved output
        self.save(rel, data)
        self.assertEqual(mapping.check(self.root, rel), [])  # valid on its own
        approve(record_id, rel)

    def test_neko_route_readout_outside_outputs_refused(self):
        self.stage_gate_ready()
        self.widen(self.mapping_rel, self.mapping_data, "dec-map-wide", self.approve_mapping)
        task = self.maboss_task(approval="dec-map-wide")
        with self.assertRaisesRegex(ValueError, r"MAPK1.*STAGE_GATE outputs") as caught:
            gate.check_data_input(self.root, "boolean_dynamics_modeler", task)
        self.assertNotIn("Proliferation,", str(caught.exception).split("STAGE_GATE outputs")[0])
        result = self.hook("boolean-dynamics-modeler", task)
        self.assertEqual(result.returncode, 2)
        self.assertIn("MAPK1", result.stderr)
        errors = mapping.check(self.root, self.mapping_rel, stage_gate=STAGE_GATE)
        self.assertEqual(len(errors), 1, errors)
        self.assertIn(str(caught.exception), errors[0])
        code, _, err = run_cli("check", self.mapping_rel, "--stage-gate", STAGE_GATE[len("STAGE_GATE "):])
        self.assertEqual(code, 1)
        self.assertIn("MAPK1", err)

    def test_standalone_route_readout_outside_outputs_refused(self):
        self.add_standalone()
        rel, data = self.complete_sources("sa", network={"standalone_decision_id": "sa-1", "bnet": STANDALONE_BNET})

        def approve(record_id, path):
            state.update(self.root, "record", {
                "id": record_id, "kind": "stage_approval", "stage": "data_mapping", "session_id": "sa-1",
                "outputs": ["S1", "V1"], "artifact": {"path": path, "sha256": sha(self.root / path)},
                "approved_by": "researcher", "authorization": "researcher: mapping ok"})
        self.widen(rel, data, "dec-map-sa-wide", approve)
        task = self.maboss_task(gate_line=SA_GATE, rel=rel, approval="dec-map-sa-wide")
        with self.assertRaisesRegex(ValueError, r"MAPK1.*STAGE_GATE outputs"):
            gate.check_data_input(self.root, "boolean_dynamics_modeler", task)
        self.assertEqual(self.hook("boolean-dynamics-modeler", task).returncode, 2)
        self.assertError(mapping.check(self.root, rel, stage_gate=SA_GATE), "MAPK1", "STAGE_GATE outputs")


MENTION = "may be named only on the DATA_INPUT line"


class OrchestratorMentionTests(DataInputFixture):
    """Seeds and mapping files under runs/orchestrator reach a specialist only through DATA_INPUT."""

    def setUp(self):
        super().setUp()
        self.add_source()
        self.write_seeds(self.SEEDS_REL, ["KRAS", "EGFR"])
        self.approve_seeds("dec-seeds-1", self.SEEDS_REL, ["EGFR", "KRAS"])

    def assertRefused(self, role, prompt, fragment=MENTION):
        result = self.hook(role, prompt)
        self.assertEqual(result.returncode, 2, (prompt, result.stderr))
        self.assertIn(fragment, result.stderr, prompt)

    def assertAllowed(self, role, prompt):
        result = self.hook(role, prompt)
        self.assertEqual(result.returncode, 0, (prompt, result.stderr))

    def test_seeds_without_data_input_refused(self):
        self.assertRefused("network-curator", f"Build the network from {self.SEEDS_REL}.")
        self.assertRefused("network-curator", f"Build.\nSeeds: `{self.SEEDS_REL}`")

    def test_seeds_under_an_unusual_name_refused(self):
        odd = "runs/orchestrator/notes/genes_for_curator.JSON"
        (self.root / odd).parent.mkdir(parents=True)
        shutil.copyfile(self.root / self.SEEDS_REL, self.root / odd)
        self.assertRefused("network-curator", f"Build the network from {odd}")
        # Alongside a valid seeds DATA_INPUT the second file is still refused.
        self.assertAllowed("network-curator", self.seeds_task())
        self.assertRefused("network-curator", f"{self.seeds_task()}\nAlso use {odd}.")
        # And alongside the data-session form.
        self.assertRefused("network-curator", f"{self.neko_task()}\nAlso use {odd}.")

    def test_mapping_with_only_stage_gate_refused(self):
        self.stage_gate_ready()
        self.assertAllowed("boolean-dynamics-modeler", f"{STAGE_GATE}\nImport and simulate.")
        self.assertRefused("boolean-dynamics-modeler",
                           f"{STAGE_GATE}\nSimulate the scenarios listed in {self.mapping_rel}.")

    def test_second_mapping_or_seeds_alongside_data_input_refused(self):
        self.stage_gate_ready()
        other, _ = self.complete("other")
        self.assertRefused("boolean-dynamics-modeler", f"{self.maboss_task()}\nCompare with {other}.")
        self.assertRefused("boolean-dynamics-modeler", f"{self.maboss_task()}\nSeeds were {self.SEEDS_REL}.")
        self.assertRefused("network-curator", f"{self.seeds_task()}\nMapping: {self.mapping_rel}.")

    def test_data_input_artifact_mentioned_again_allowed(self):
        self.stage_gate_ready()
        self.assertAllowed("boolean-dynamics-modeler",
                           f"{self.maboss_task()}\nThe mapping (`{self.mapping_rel}`) is fixed.")
        self.assertAllowed("network-curator", f"{self.seeds_task()}\nSeeds file: \"{self.SEEDS_REL}\".")
        self.assertAllowed("network-curator",
                           f"{self.seeds_task()}\nSeeds file: {self.SEEDS_REL.replace('/', chr(92))}.")

    def test_other_orchestrator_files_allowed(self):
        base = self.root / "runs" / "orchestrator" / "notes"
        base.mkdir(parents=True)
        files = {"runs.json": '{"runs": []}', "summary.md": "# Summary\n", "notes.json": '{"note": "x"}',
                 "broken.json": '{"seeds": [', "list.json": "[1, 2]", "CHANGES.md": "edits\n",
                 "sim_config.json": '{"seeds": [1, 2, 3], "threads": 4}'}
        for name, text in files.items():
            (base / name).write_text(text, encoding="utf-8")
        mentions = " ".join(f"runs/orchestrator/notes/{name}," for name in files)
        mentions += " runs/orchestrator/notes/absent.json runs/orchestrator/notes and runs/orchestrator/notes/*.json"
        self.assertAllowed("network-curator", f"Build from EGFR, KRAS. Context: {mentions}")
        self.stage_gate_ready()
        self.assertAllowed("boolean-dynamics-modeler", f"{STAGE_GATE}\nImport and simulate. Context: {mentions}")
        self.assertAllowed("boolean-dynamics-modeler", f"{self.maboss_task()}\nContext: {mentions}")

    def test_rng_seeds_are_not_a_seeds_file(self):
        base = self.root / "runs" / "orchestrator" / "sim"
        base.mkdir(parents=True)
        cases = {'{"seeds": [1, 2, 3], "threads": 4}': True, '{"seeds": 42}': True, '{"seeds": []}': True,
                 '{"seeds": ["KRAS"]}': False, '{"seeds": [7, {"hgnc_symbol": "KRAS"}]}': False,
                 '{"seeds": "KRAS EGFR"}': False, '{"schema_version": 1, "seeds": []}': False}
        for n, (text, allowed) in enumerate(cases.items()):
            (base / f"c{n}.json").write_text(text, encoding="utf-8")
            prompt = f"Build from EGFR. Config: runs/orchestrator/sim/c{n}.json"
            if allowed:
                self.assertAllowed("network-curator", prompt)
            else:
                self.assertRefused("network-curator", prompt)

    def test_directory_mentions_refused(self):
        for prompt in ("Build from the approved seeds in runs/orchestrator/kras-seeds/",
                       "Read seeds.json in runs/orchestrator/kras-seeds/ and build",
                       "Use `runs/orchestrator/kras-seeds` (file `seeds.json`).",
                       "Build from runs/orchestrator/kras-seeds/*.json",
                       "Build from runs/orchestrator/kras-seeds/seed?.json",
                       "Build from RUNS\\Orchestrator\\Kras-Seeds."):
            self.assertRefused("network-curator", prompt)
        self.stage_gate_ready()
        self.assertRefused("boolean-dynamics-modeler", f"{STAGE_GATE}\nScenarios are in runs/orchestrator/luad/")
        self.assertRefused("boolean-dynamics-modeler", f"{self.maboss_task()}\nSeeds: runs/orchestrator/kras-seeds")

    def test_directory_holding_only_the_data_input_artifact_allowed(self):
        for purpose in ("kras-seeds", "luad"):
            for name, text in (("runs.json", '{"runs": []}'), ("CHANGES.md", "edits\n")):
                (self.root / "runs" / "orchestrator" / purpose / name).write_text(text, encoding="utf-8")
        self.assertAllowed("network-curator", f"{self.seeds_task()}\nWork in runs/orchestrator/kras-seeds/.")
        self.stage_gate_ready()
        self.assertAllowed("boolean-dynamics-modeler", f"{self.maboss_task()}\nSee runs/orchestrator/luad/*.json")
        # Another seeds file in the same directory is refused.
        shutil.copyfile(self.root / self.SEEDS_REL, self.root / "runs/orchestrator/kras-seeds/old.json")
        self.assertRefused("network-curator", f"{self.seeds_task()}\nWork in runs/orchestrator/kras-seeds/.")

    def test_orchestrator_root_scans_direct_children_only(self):
        # Only the named directory's own files are read: seeds in a purpose directory below are not.
        for mention in ("runs/orchestrator/", "`runs/orchestrator`", "runs\\orchestrator."):
            self.assertAllowed("network-curator", f"Build from EGFR; notes live under {mention}")
        shutil.copyfile(self.root / self.SEEDS_REL, self.root / "runs/orchestrator/stray.json")
        for mention in ("runs/orchestrator/", "`runs/orchestrator`", "runs\\orchestrator."):
            self.assertRefused("network-curator", f"Build from EGFR; notes live under {mention}")

    def test_many_mentions_on_one_line_are_fast(self):
        line = " ".join(f"runs/orchestrator/p{n}/x.json" for n in range(1000))
        start = time.monotonic()
        self.assertIsNone(gate.check_data_input(self.root, "network_curator", f"Build.\n{line}"))
        self.assertLess(time.monotonic() - start, 2.0)
        spaced = " ".join(f"runs/orchestrator/a b{n}/x y.json" for n in range(1000))
        start = time.monotonic()
        self.assertIsNone(gate.check_data_input(self.root, "network_curator", f"Build.\n{spaced}"))
        self.assertLess(time.monotonic() - start, 2.0)

    def test_spelling_variants_detected(self):
        spaced = "runs/orchestrator/kras-seeds/seeds copy.json"
        shutil.copyfile(self.root / self.SEEDS_REL, self.root / spaced)
        for variant in (self.SEEDS_REL.replace("/", "\\"), "RUNS/Orchestrator/KRAS-SEEDS/Seeds.JSON",
                        "runs\\\\orchestrator//kras-seeds/seeds.json", "runs／orchestrator／kras-seeds／seeds.json",
                        "runs/orches​trator/kras-seeds/seeds.json", f"./{self.SEEDS_REL}",
                        f"{self.root}/{self.SEEDS_REL}", f"runs/orchestrator/luad/../kras-seeds/seeds.json",
                        f"<{self.SEEDS_REL}>", f"({self.SEEDS_REL});", f"'{self.SEEDS_REL}'", spaced):
            self.assertRefused("network-curator", f"Build the network from {variant}")

    def test_gate_message_and_unreadable_files(self):
        with self.assertRaisesRegex(ValueError, f"{self.SEEDS_REL}.*{MENTION}"):
            gate.check_data_input(self.root, "network_curator", f"Build from {self.SEEDS_REL}")
        # An unexpected failure while inspecting a mentioned file fails closed through the hook.
        with mock.patch.object(gate, "_model_input_kind", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                gate.check_data_input(self.root, "network_curator", f"Build from {self.SEEDS_REL}")
        deep = self.root / "runs" / "orchestrator" / "luad" / "deep.json"
        deep.write_text("[" * 100000, encoding="utf-8")
        result = self.hook("network-curator", "Build from runs/orchestrator/luad/deep.json")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("blocked", result.stderr)


class OdeCalibrationInputTests(HoldoutFixture):
    """The ode-modeler DATA_INPUT: an approved calibration proposal, applied to its own ODE session."""

    def setUp(self):
        super().setUp()
        self.proposal_path = self.make_proposal()
        self.approve_calibration()

    def hook(self, subagent_type, prompt):
        event = {"tool_name": "Agent", "tool_input": {"subagent_type": subagent_type, "prompt": prompt}}
        env = {**os.environ, "CLAUDE_PROJECT_DIR": str(self.root), "PYTHONPATH": str(state.ROOT)}
        return subprocess.run([sys.executable, str(HOOK)], input=json.dumps(event), text=True,
                              capture_output=True, env=env, timeout=60)

    def line(self, digest=None, approval="cal-1", session=ODE_SESSION, accept=("kf1",), **extra):
        payload = {"calibration": self.proposal_rel, "sha256": digest or sha(self.root / self.proposal_rel),
                   "approval": approval, "record_session_id": session, "accept": list(accept), **extra}
        return "DATA_INPUT " + json.dumps(payload)

    def task(self, line=None, tail=""):
        return f"{line or self.line()}\nApply the accepted values.{tail}"

    def rewrite_record(self, record_id, edit):
        """Edit a DECISIONS.md record in place, bypassing state.py validation (a hand-edited record)."""
        decisions = self.root / "DECISIONS.md"
        text = decisions.read_text(encoding="utf-8")

        def replace(match):
            item = json.loads(match.group(1))
            if item.get("id") != record_id:
                return match.group(0)
            edit(item)
            return match.group(0).replace(match.group(1), json.dumps(item, indent=2))
        decisions.write_text(state.RECORD.sub(replace, text), encoding="utf-8")

    def refused(self, prompt, pattern):
        result = self.hook("ode-modeler", prompt)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertRegex(result.stderr, pattern)

    def test_accepted_brief_passes(self):
        result = self.hook("ode-modeler", self.task())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.hook("plugin:ode-modeler", self.task()).returncode, 0)
        found = gate.check_data_input(self.root, "ode_modeler", self.task())
        self.assertEqual(found["outputs"], ["kf1"])
        self.assertEqual(found["ode_session"], ODE_SESSION)

    def test_brief_without_data_input_is_unchanged(self):
        self.assertEqual(self.hook("ode-modeler", "Build the model from the reactions.").returncode, 0)

    def test_wrong_sha_stage_and_outputs_refused(self):
        self.refused(self.task(line=self.line(digest="0" * 64)), "sha256")
        self.approve_calibration("cal-stage", stage="ode_mapping")
        self.refused(self.task(line=self.line(approval="cal-stage")), "ode_calibration")
        self.approve_calibration("cal-out", outputs=("kf1", "nope"))
        self.refused(self.task(line=self.line(approval="cal-out")), "outputs")
        self.refused(self.task(line=self.line(approval="missing")), "decision_id")

    def test_edited_proposal_refused(self):
        self.proposal_path.write_text(self.proposal_path.read_text(encoding="utf-8") + " ", encoding="utf-8")
        self.refused(self.task(), "sha256")

    def test_approval_session_must_be_the_proposals(self):
        decisions = self.root / "DECISIONS.md"
        decisions.write_text(decisions.read_text(encoding="utf-8").replace(
            f'"session_id": "{ODE_SESSION}"', '"session_id": "other-session"'), encoding="utf-8")
        self.refused(self.task(), "session_id|ODE session")

    def test_record_session_id_is_a_payload_key(self):
        # PR 16 review gate.py:509/510: the session travels in the verified DATA_INPUT payload
        self.refused(self.task(line=self.line(session="other-session")), "record_session_id")
        self.refused(self.task(line=self.line(session="")), "record_session_id|nonempty")
        self.refused(self.task(line=self.line(session=1)), "record_session_id|string")
        # prose mentions of record_session_id no longer matter (no free-text parsing)
        for tail in (" record_session_id other-session.", " record_session_ids: other-session.",
                     " record_session_id is " + ODE_SESSION + "."):
            with self.subTest(tail=tail):
                result = self.hook("ode-modeler", self.task(tail=tail))
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_prose_naming_another_existing_ode_session_refused(self):
        # PR 16 review 2 gate.py:522: the reviewer's briefs (a)-(c), with "other-session" an existing ODE session
        (self.root / "runs" / "ode-modeler" / "other-session").mkdir(parents=True)
        for tail in ("\nApply the accepted values. Also apply kf2 at its best value in session other-session.",
                     '\nAccepted: {"accept": ["kf1","kf2"], "record_session_id": "other-session"}',
                     ' data input {"accept": ["kf2"]} for OTHER-SESSION',
                     " (runs/ode-modeler/other-session/)"):
            with self.subTest(tail=tail):
                self.refused(self.task(tail=tail), "other-session")
        # the payload's own session, a name that is not an existing session, or a longer token: allowed
        for tail in (f" in session {ODE_SESSION}.", " record_session_id other-thing.", " see other-session-2.",
                     " the other-sessions file"):
            with self.subTest(tail=tail):
                result = self.hook("ode-modeler", self.task(tail=tail))
                self.assertEqual(result.returncode, 0, result.stderr)
        # a brief without a DATA_INPUT line (model building) is not affected
        self.assertEqual(self.hook("ode-modeler", "Build the model; compare with other-session.").returncode, 0)

    def test_accept_must_equal_the_approval_outputs(self):
        # PR 16 review ode-modeler.md:53: the accepted names travel verified in the payload
        self.approve_calibration("cal-2", outputs=("kf1", "kf2"))
        self.assertEqual(self.hook("ode-modeler", self.task(
            line=self.line(approval="cal-2", accept=("kf2", "kf1")))).returncode, 0)
        found = gate.check_data_input(self.root, "ode_modeler",
                                      self.task(line=self.line(approval="cal-2", accept=("kf2", "kf1"))))
        self.assertEqual(sorted(found["outputs"]), ["kf1", "kf2"])
        for name, approval, accept in (("subset", "cal-2", ("kf1",)), ("superset", "cal-1", ("kf1", "kf2")),
                                       ("duplicate", "cal-1", ("kf1", "kf1")), ("empty", "cal-1", ()),
                                       ("other", "cal-1", ("kf2",)), ("non-string", "cal-1", (1,))):
            with self.subTest(name=name):
                self.refused(self.task(line=self.line(approval=approval, accept=accept)), "accept")
        self.refused(self.task(line=self.line(accept="kf1")), "accept")

    def test_missing_or_extra_payload_keys_refused(self):
        old = "DATA_INPUT " + json.dumps({"calibration": self.proposal_rel, "approval": "cal-1",
                                          "sha256": sha(self.root / self.proposal_rel)})
        self.refused(self.task(line=old), "record_session_id.*accept|exactly")
        for drop in ("record_session_id", "accept", "sha256"):
            payload = json.loads(self.line()[len("DATA_INPUT "):])
            payload.pop(drop)
            with self.subTest(drop=drop):
                self.refused(self.task(line="DATA_INPUT " + json.dumps(payload)), "exactly")
        self.refused(self.task(line=self.line(values={"kf1": 1.0})), "exactly")

    def test_proposal_must_belong_to_the_job_beside_it(self):
        # PR 16 review gate.py:582: job.json mapping and job_id must be the proposal's
        data = json.loads(self.proposal_path.read_text(encoding="utf-8"))
        job_json = self.proposal_path.parent / "job.json"
        job = json.loads(job_json.read_text(encoding="utf-8"))

        def install(n, proposal, job_data, folder=None):
            folder = folder or self.proposal_path.parent
            folder.mkdir(parents=True, exist_ok=True)
            raw = (json.dumps(job_data, indent=2) + "\n").encode()
            (folder / "job.json").write_bytes(raw)
            proposal = dict(proposal, job_sha256=sha(folder / "job.json"))
            (folder / "calibration_proposal.json").write_text(json.dumps(proposal, indent=2) + "\n",
                                                              encoding="utf-8")
            self.proposal_rel = (folder / "calibration_proposal.json").relative_to(self.root).as_posix()
            self.approve_calibration(f"cal-j{n}")
            return self.task(line=self.line(approval=f"cal-j{n}"))

        # a job.json naming another mapping approval (proposal re-hashed to it)
        self.refused(install(0, data, {**job, "mapping": {**job["mapping"], "approval": "ode-map-2"}}), "mapping")
        # a job.json of another job id
        self.refused(install(1, data, {**job, "job_id": "job-ffffffffffff"}), "job_id")
        # the proposal names another job id than its job.json and folder
        self.refused(install(2, {**data, "job_id": "job-ffffffffffff"}, job), "job_id")
        # the proposal and its job.json copied into a folder of another name
        other = self.proposal_path.parent.parent / "job-eeeeeeeeeeee"
        self.refused(install(3, data, job, folder=other), "folder|job_id")

    def test_hook_scans_mentions_once_per_dispatch(self):
        # PR 16 review gate.py:914: one runs/orchestrator mention scan per dispatch, after the payload is verified
        import importlib.util
        import io
        spec = importlib.util.spec_from_file_location("stage_gate_guard_once", HOOK)
        guard = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(guard)
        real, calls = gate._orchestrator_mentions, []

        def counting(root, text, cache=None):
            calls.append(text)
            yield from real(root, text, cache)
        for role, prompt, code in (("ode-modeler", self.task(tail=f" See {self.proposal_rel}."), 0),
                                   ("ode-modeler", "Build the model from the reactions.", 0),
                                   ("network-curator", "Build the network.", 0),
                                   ("multicellular-configurator", "Configure.", 2)):
            with self.subTest(role=role, prompt=prompt[:30]):
                calls.clear()
                event = {"tool_name": "Agent", "tool_input": {"subagent_type": role, "prompt": prompt}}
                with mock.patch.object(guard, "ROOT", self.root), \
                        mock.patch.object(gate, "_orchestrator_mentions", counting), \
                        mock.patch("sys.stdin", io.StringIO(json.dumps(event))), \
                        mock.patch("sys.stderr", io.StringIO()) as err:
                    self.assertEqual(guard.main(), code, err.getvalue())
                self.assertEqual(len(calls), 1)

    def test_mapping_approval_runs_validate_record(self):
        # PR 16 review gate.py:575: a hand-edited ode_mapping approval without researcher authorization
        mapping_approval = json.loads(self.proposal_path.read_text(encoding="utf-8"))["mapping"]["approval"]
        self.rewrite_record(mapping_approval, lambda r: r.pop("approved_by"))
        self.refused(self.task(), "authorization")
        self.rewrite_record(mapping_approval, lambda r: r.update(approved_by="researcher", authorization=""))
        self.refused(self.task(), "authorization")

    def test_calibration_approval_runs_validate_record(self):
        self.rewrite_record("cal-1", lambda r: r.pop("authorization"))
        self.refused(self.task(), "authorization")

    def test_proposal_or_mapping_named_elsewhere_refused(self):
        # the DATA_INPUT artifact itself may be named again; any other proposal or mapping may not
        self.assertEqual(self.hook("ode-modeler", self.task(tail=f" See {self.proposal_rel}.")).returncode, 0)
        other = self.root / "runs" / "orchestrator" / "p1" / "jobs" / "other" / "calibration_proposal.json"
        other.parent.mkdir(parents=True)
        other.write_bytes(self.proposal_path.read_bytes())
        self.refused(self.task(tail=f" Compare with {other.relative_to(self.root).as_posix()}."), "calibration_proposal")
        self.refused(self.task(tail=" Mapping: runs/orchestrator/p1/ode_mapping.json."), "ode_mapping")
        self.refused(f"Apply values from {self.proposal_rel}.", "DATA_INPUT")

    def test_payload_like_mentions_elsewhere_refused(self):
        self.refused(self.task(tail=f"\nDATA_INPUT {{\"calibration\": \"x\"}}"), "DATA_INPUT")
        self.refused(f"Apply.\n{self.line()}", "line 1")
        self.assertEqual(self.hook("ode-modeler", self.task(tail=" as defined in the DATA_INPUT calibration")).returncode, 0)

    def test_malformed_line_fails_closed(self):
        for line in ("DATA_INPUT not-json", "DATA_INPUT []", 'DATA_INPUT {"calibration": 1}',
                     'DATA_INPUT {"calibration": "x", "sha256": "y", "approval": "z", "more": 1}',
                     'DATA_INPUT {"calibration": "x", "sha256": "y", "approval": "z", "record_session_id": "s", '
                     '"accept": ["kf1"], "more": 1}'):
            self.refused(self.task(line=line), "blocked")
        self.assertEqual(self.hook("ode-modeler", ["x"]).returncode, 2)

    def test_job_files_and_returned_results_refused_without_data_input(self):
        job = self.proposal_path.parent.relative_to(self.root).as_posix()
        results = f"inputs/calibration-results/{self.job_id}/results.tar.gz"
        self.assertTrue((self.root / job / "report.md").is_file())
        for prompt in (f"Apply the values in {job}/report.md to session {ODE_SESSION}.",
                       f"See {job}/holdout_score.json for the hold-out.",
                       f"Unpack {results} and set the best values.",
                       f"Read the job folder {job}/ first.",
                       f"Read {job.upper()}/Report.MD.",
                       f"Read {job.replace('/', chr(92))}\\report.md.",
                       f"Read runs/orchestrator/*/jobs/{self.job_id}/report.md.",
                       "Results are under inputs/calibration-results/."):
            with self.subTest(prompt=prompt):
                self.refused(prompt, "calibration job")

    def test_job_files_refused_alongside_the_accepted_calibration(self):
        job = self.proposal_path.parent.relative_to(self.root).as_posix()
        self.refused(self.task(tail=f" Values: {job}/report.md."), "calibration job")
        self.refused(self.task(tail=f" Hold-out: {job}/holdout_score.json."), "calibration job")
        self.refused(self.task(tail=f" Raw: inputs/calibration-results/{self.job_id}/results.tar.gz."),
                     "calibration job")
        # the proposal itself and its own job folder stay allowed with this DATA_INPUT
        self.assertEqual(self.hook("ode-modeler", self.task(tail=f" Job folder: {job}/.")).returncode, 0)
        self.assertEqual(self.hook("ode-modeler", self.task(tail=f" See {self.proposal_rel}.")).returncode, 0)
        # but the job folder without that DATA_INPUT is refused
        self.refused(f"Job folder: {job}/.", "calibration job")

    def test_job_files_refused_for_every_gated_specialist(self):
        job = self.proposal_path.parent.relative_to(self.root).as_posix()
        for role in ("network-curator", "boolean-dynamics-modeler", "multicellular-configurator"):
            with self.subTest(role=role):
                result = self.hook(role, f"Use {job}/report.md.")
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("calibration job", result.stderr)

    def test_ordinary_and_handoff_ode_briefs_pass(self):
        handoff = self.root / "runs" / "network-curator" / "neko-1" / "exports" / "a1" / "biomass_handoff.json"
        handoff.parent.mkdir(parents=True)
        handoff.write_text(json.dumps({"handoff_type": "neko-to-biomass", "network": {"nodes": ["A"]}}),
                           encoding="utf-8")
        for prompt in ("Build the model from the reactions; then run a simulation.",
                       "STAGE_GATE {\"upstream_session_id\": \"neko-1\", \"outputs\": [\"A\"]}\n"
                       f"Import the NeKo to BioMASS handoff {handoff.relative_to(self.root).as_posix()}.",
                       "Notes are in runs/orchestrator/p1/notes.md; the jobs list is elsewhere."):
            with self.subTest(prompt=prompt):
                result = self.hook("ode-modeler", prompt)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_proposal_mapping_approval_and_job_sha_are_checked(self):
        data = json.loads(self.proposal_path.read_text(encoding="utf-8"))
        for n, (edit, pattern) in enumerate((
                (lambda d: d["mapping"].update(approval="missing-approval"), "missing-approval"),
                (lambda d: d["mapping"].update(sha256="0" * 64), "sha256"),
                (lambda d: d.update(job_sha256="0" * 64), "job_sha256|job.json"))):
            with self.subTest(n=n):
                broken = json.loads(json.dumps(data))
                edit(broken)
                self.proposal_path.write_text(json.dumps(broken, indent=2) + "\n", encoding="utf-8")
                self.approve_calibration(f"cal-h{n}")
                self.refused(self.task(line=self.line(approval=f"cal-h{n}")), pattern)


MC_OUTPUTS = ["A", "B"]
MC_GATE = "STAGE_GATE " + json.dumps({"upstream_session_id": MC_MABOSS, "outputs": MC_OUTPUTS})
MC_NAMES = ["default", "tumor", "tumor_hypoxic", "oxygen", "drug"]
DESIGN_B = "runs/orchestrator/p2/physicell_design.json"


class PhysiCellDataInputTests(ProjectionBase):
    """The multicellular-configurator DATA_INPUT (line 2, after STAGE_GATE): an approved stage-A design or an
    approved stage-B projection, each re-checked at dispatch. The design is anchored on its basis session
    (MC_SESSION); the projection is bound to its stage-A capture's session (MC_STAGE_A)."""

    def setUp(self):
        super().setUp()
        path = self.write_projection(self.projection())
        state.update(self.root, "record", {
            "id": "proj-1", "kind": "stage_approval", "session_id": MC_STAGE_A, "stage": "physicell_projection",
            "outputs": ["in1", "out1", "rule1", "set1"], "approved_by": "researcher", "authorization": "approved",
            "artifact": {"path": MC_PROJ, "sha256": sha(path)}})

    hook = DataInputFixture.hook

    def design_line(self, rel=DESIGN_REL, digest=None, approval="design-1", session=MC_SESSION, **extra):
        payload = {"design": rel, "sha256": digest or sha(self.root / rel), "approval": approval,
                   "record_session_id": session, **extra}
        return "DATA_INPUT " + json.dumps(payload)

    def projection_line(self, rel=MC_PROJ, digest=None, approval="proj-1", session=MC_STAGE_A, **extra):
        payload = {"projection": rel, "sha256": digest or sha(self.root / rel), "approval": approval,
                   "record_session_id": session, **extra}
        return "DATA_INPUT " + json.dumps(payload)

    def task(self, line, gate_line=MC_GATE, tail=""):
        return f"{gate_line}\n{line}\nApply exactly the approved file named on the DATA_INPUT line.{tail}"

    def accepted(self, prompt):
        result = self.hook("multicellular-configurator", prompt)
        self.assertEqual(result.returncode, 0, result.stderr)

    def refused(self, prompt, pattern):
        result = self.hook("multicellular-configurator", prompt)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertRegex(result.stderr, pattern)

    def approve_design(self, record_id, rel, data, session=MC_SESSION, stage="physicell_design"):
        path = self.write(data, rel)
        state.update(self.root, "record", {
            "id": record_id, "kind": "stage_approval", "session_id": session, "stage": stage,
            "outputs": MC_NAMES, "approved_by": "researcher", "authorization": "approved",
            "artifact": {"path": rel, "sha256": sha(path)}})

    def without_maboss(self):
        data = json.loads(json.dumps(self.design))
        data["intracellular"] = []
        for item in data["coupling_plan"]:
            item["route"] = "rules"
        return data

    def standalone_gate(self):
        state.update(self.root, "record", {
            "id": "sa-mc", "kind": "standalone_approval", "stage": "multicellular", "outputs": ["tissue"],
            "artifact": {"path": "inputs/paper.txt", "sha256": sha(self.root / "inputs/paper.txt")},
            "approved_by": "researcher", "authorization": "researcher: build my tissue model"})
        return "STAGE_GATE " + json.dumps({"standalone_decision_id": "sa-mc", "outputs": ["tissue"]})

    def test_both_forms_accepted(self):
        self.accepted(self.task(self.design_line()))
        self.accepted(self.task(self.projection_line()))
        self.assertEqual(self.hook("plugin:multicellular-configurator",
                                   self.task(self.design_line())).returncode, 0)
        found = gate.check_data_input(self.root, "multicellular_configurator", self.task(self.design_line()))
        self.assertEqual((found["design"], found["approval_id"], found["physicell_session"]),
                         (DESIGN_REL, "design-1", MC_SESSION))
        found = gate.check_data_input(self.root, "multicellular_configurator", self.task(self.projection_line()))
        self.assertEqual((found["projection"], found["approval_id"], found["physicell_session"]),
                         (MC_PROJ, "proj-1", MC_STAGE_A))

    def test_stage_gate_only_briefs_unchanged(self):
        self.accepted(f"{MC_GATE}\nImport the approved MaBoSS handoff onto tumor and export.")
        self.assertIsNone(gate.check_data_input(self.root, "multicellular_configurator", f"{MC_GATE}\nConfigure."))
        self.refused("Configure the tissue.", "STAGE_GATE")
        self.refused(f"{self.design_line()}\nConfigure.", "STAGE_GATE|line 2")
        bad_gate = "STAGE_GATE " + json.dumps({"upstream_session_id": MC_MABOSS, "outputs": ["A"]})
        self.refused(self.task(self.design_line(), gate_line=bad_gate), "approval")
        self.refused(self.task(self.projection_line(), gate_line=bad_gate), "approval")

    def test_duplicate_key_stage_gate_refused_by_the_hook(self):
        # PR #17 review 4587426903: one parser (gate.stage_gate_form) defines a well-formed STAGE_GATE; check_task
        # refuses duplicate keys as lineage does, so the hook never dispatches a brief lineage would refuse
        twice = ('STAGE_GATE {"upstream_session_id": "x", "upstream_session_id": "%s", "outputs": ["A", "B"]}'
                 % MC_MABOSS)
        for prompt in (f"{twice}\nImport the approved handoff.", self.task(self.design_line(), gate_line=twice),
                       self.task(self.projection_line(), gate_line=twice)):
            with self.subTest(prompt=prompt.splitlines()[1]):
                self.refused(prompt, "STAGE_GATE has duplicate keys")
        with self.assertRaisesRegex(ValueError, "STAGE_GATE has duplicate keys"):
            gate.check_task(self.root, "multicellular_configurator", twice)
        result = self.hook("boolean-dynamics-modeler", twice.replace(MC_MABOSS, "neko-1") + "\nImport.")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("STAGE_GATE has duplicate keys", result.stderr)
        self.accepted(f"{MC_GATE}\nImport the approved handoff.")

    def test_stage_gate_only_brief_naming_a_mapping_file_refused(self):
        # Intended (CLAUDE.md): model input files, recognised by content, are named only on a DATA_INPUT line
        rel = "runs/orchestrator/p5/mapping.json"
        (self.root / rel).parent.mkdir(parents=True)
        (self.root / rel).write_text(json.dumps({"schema_version": 1, "inputs": {}, "contexts": [],
                                                 "perturbations": [], "readouts": [], "criteria": []}))
        self.assertEqual(gate._model_input_kind(self.root / rel), "mapping")
        self.refused(f"{MC_GATE}\nImport the handoff; scenarios are in {rel}.", f"mapping file {rel}.*no DATA_INPUT line")
        self.refused(self.task(self.design_line(), tail=f" Scenarios: {rel}."), f"mapping file {rel}")

    def test_wrong_sha_refused(self):
        self.refused(self.task(self.design_line(digest="0" * 64)), "sha256")
        self.refused(self.task(self.projection_line(digest="0" * 64)), "sha256")
        self.refused(self.task(self.design_line(digest="ABC")), "sha256")
        (self.root / MC_PROJ).write_text((self.root / MC_PROJ).read_text() + "\n")
        self.refused(self.task(self.projection_line()), "sha256")

    def test_wrong_stage_approval_refused(self):
        self.refused(self.task(self.design_line(approval="proj-1")), "physicell_design")
        self.refused(self.task(self.projection_line(approval="design-1")), "physicell_projection")
        self.refused(self.task(self.design_line(approval="missing")), "decision_id")
        self.approve_design("design-x", DESIGN_B, self.design, stage="physicell_projection")
        self.refused(self.task(self.design_line(rel=DESIGN_B, approval="design-x")), "physicell_design")

    def test_record_session_id_mismatch_refused(self):
        self.refused(self.task(self.design_line(session="other-session")), "record_session_id")
        self.refused(self.task(self.projection_line(session="other-session")), "record_session_id")
        for session in ("", 1, None):
            with self.subTest(session=session):
                self.refused(self.task(self.design_line(session=session)), "record_session_id")
                self.refused(self.task(self.projection_line(session=session)), "record_session_id")

    def test_design_handoff_must_be_the_stage_gate_upstream(self):
        # Review Focus 5: an approved, exported MaBoSS session that is not the design's handoff
        self.make_handoff("maboss-other")
        other_gate = "STAGE_GATE " + json.dumps({"upstream_session_id": "maboss-other", "outputs": MC_OUTPUTS})
        self.accepted(f"{other_gate}\nImport the approved handoff.")  # the STAGE_GATE itself passes
        self.refused(self.task(self.design_line(), gate_line=other_gate), f"{MC_MABOSS}.*maboss-other")
        # handoffs from two MaBoSS sessions: refused, naming both
        hypoxic = self.make_handoff("maboss-hyp", cell_type="tumor_hypoxic")
        data = json.loads(json.dumps(self.design))
        data["intracellular"].append({"cell_definition": "tumor_hypoxic",
                                      "maboss": {"handoff": hypoxic["path"], "sha256": hypoxic["sha256"]}})
        self.write(data, DESIGN_B)
        self.assertEqual(self.errors_at(DESIGN_B), [])
        self.approve_design("design-2", DESIGN_B, data)
        self.refused(self.task(self.design_line(rel=DESIGN_B, approval="design-2")), "maboss-hyp.*maboss-session")
        # a design without MaBoSS handoffs under an upstream STAGE_GATE, or with them under a standalone one
        self.approve_design("design-3", "runs/orchestrator/p3/physicell_design.json", self.without_maboss())
        line = self.design_line(rel="runs/orchestrator/p3/physicell_design.json", approval="design-3")
        self.refused(self.task(line), "no MaBoSS handoff")
        sa_gate = self.standalone_gate()
        self.accepted(self.task(line, gate_line=sa_gate))
        self.refused(self.task(self.design_line(), gate_line=sa_gate), "standalone.*MaBoSS")

    def test_stage_gate_checked_once_per_dispatch(self):
        # PR #17 review 4586113358: the hook reuses check_data_input's STAGE_GATE result; gate.check runs once for
        # the STAGE_GATE plus once per design handoff (the design check's own binding of each handoff)
        for name, prompt, checks in (("design", self.task(self.design_line()), 2),
                                     ("projection", self.task(self.projection_line()), 2),
                                     ("STAGE_GATE only", f"{MC_GATE}\nImport the approved handoff.", 1)):
            with self.subTest(form=name):
                status, err, tasks, gate_checks = gate_calls(self.root, "multicellular-configurator", prompt)
                self.assertEqual((status, tasks, gate_checks), (0, 1, checks), err)
        # still refused when the STAGE_GATE fails, with or without DATA_INPUT
        bad = "STAGE_GATE " + json.dumps({"upstream_session_id": MC_MABOSS, "outputs": ["A"]})
        for prompt in (self.task(self.design_line(), gate_line=bad), f"{bad}\nImport."):
            status, err, tasks, _ = gate_calls(self.root, "multicellular-configurator", prompt)
            self.assertEqual((status, tasks), (2, 1), err)

    def test_projection_design_handoff_must_be_the_stage_gate_upstream(self):
        # PR #17 review 4586110268: the projection form binds its design's MaBoSS handoffs to the line-1 STAGE_GATE
        self.make_handoff("maboss-other")
        other_gate = "STAGE_GATE " + json.dumps({"upstream_session_id": "maboss-other", "outputs": MC_OUTPUTS})
        self.accepted(f"{other_gate}\nImport the approved handoff.")  # the STAGE_GATE itself passes
        self.refused(self.task(self.projection_line(), gate_line=other_gate), f"{MC_MABOSS}.*maboss-other")
        self.refused(self.task(self.projection_line(), gate_line=self.standalone_gate()), "standalone.*MaBoSS")
        self.accepted(self.task(self.projection_line()))

    def test_projection_dispatch_reuses_the_checked_design(self):
        # PR #17 review 4587428496: the projection branch binds the design its projection check loaded and checked;
        # the design is loaded once per projection dispatch (by that check), never again by the gate
        from scripts.physicell import physicell_design, physicell_projection
        hook = load_hook()
        with mock.patch.object(physicell_design, "_load", wraps=physicell_design._load) as loads:
            status, err = run_hook(hook, self.root, "multicellular-configurator", self.task(self.projection_line()))
        self.assertEqual(status, 0, err)
        self.assertEqual([c.args[1] for c in loads.call_args_list], [DESIGN_REL])
        data, digest, errors, design = physicell_projection.load_and_check_with_design(self.root, MC_PROJ)
        self.assertEqual((errors, design), ([], json.loads((self.root / DESIGN_REL).read_text())))
        self.assertEqual(physicell_projection.load_and_check(self.root, MC_PROJ), (data, digest, errors))

    def errors_at(self, rel):
        from scripts.physicell import physicell_design
        return physicell_design.check(self.root, rel)

    def test_design_failing_check_refused(self):
        data = json.loads(json.dumps(self.design))
        data["unmapped"] = [{"item": "x", "reason": "TODO"}]
        self.approve_design("design-bad", DESIGN_B, data)
        self.refused(self.task(self.design_line(rel=DESIGN_B, approval="design-bad")), "check failed.*TODO")
        # Review Focus 5: the approved design's MaBoSS handoff was superseded after approval
        self.accepted(self.task(self.design_line()))
        state.update(self.root, "registry-supersede", {"session_id": MC_MABOSS})
        self.refused(self.task(self.design_line()), "design check failed.*maboss-session.*active")
        self.refused(self.task(self.projection_line()), "projection check failed.*no longer passes")

    def test_projection_rechecked_and_design_approval_enforced(self):
        # a projection whose design approval is missing, though the projection itself is approved
        data = self.projection()
        data["design"]["approval"] = "nope"
        path = self.root / "runs/orchestrator/p4/physicell_projection.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(data, indent=2))
        rel = path.relative_to(self.root).as_posix()
        state.update(self.root, "record", {
            "id": "proj-2", "kind": "stage_approval", "session_id": MC_STAGE_A, "stage": "physicell_projection",
            "outputs": ["in1", "out1", "rule1", "set1"], "approved_by": "researcher", "authorization": "approved",
            "artifact": {"path": rel, "sha256": sha(path)}})
        self.refused(self.task(self.projection_line(rel=rel, approval="proj-2")), "design approval nope")
        # the dictionary binding is re-checked too
        dictionary = self.root / MC_DICT
        dictionary.write_text(dictionary.read_text() + "\n")
        self.refused(self.task(self.projection_line()), "dictionary")

    def test_extra_missing_or_mixed_keys_refused(self):
        design = json.loads(self.design_line()[len("DATA_INPUT "):])
        projection = json.loads(self.projection_line()[len("DATA_INPUT "):])
        cases = {"extra": {**design, "accept": ["x"]}, "extra-projection": {**projection, "values": {}},
                 "mixed": {**design, "projection": MC_PROJ}, "both": {**design, **projection}}
        for drop in ("sha256", "approval", "record_session_id", "design"):
            cases[f"no-{drop}"] = {k: v for k, v in design.items() if k != drop}
        cases["no-projection-session"] = {k: v for k, v in projection.items() if k != "record_session_id"}
        for name, payload in cases.items():
            with self.subTest(name=name):
                self.refused(self.task("DATA_INPUT " + json.dumps(payload)), "exactly")
        for line in ("DATA_INPUT not-json", "DATA_INPUT []", 'DATA_INPUT {"design": 1}'):
            with self.subTest(line=line):
                self.refused(self.task(line), "blocked")

    def test_payload_like_mentions_refused(self):
        self.refused(self.task(self.design_line(), tail=f"\n{self.projection_line()}"), "line 4")
        self.refused(self.task(self.design_line(), tail='\nDATA_INPUT {"design": "x"}'), "DATA_INPUT payload")
        self.refused(f"{MC_GATE}\nConfigure.\n{self.design_line()}", "line 2")
        self.accepted(self.task(self.design_line(), tail=" Use the design as defined in the DATA_INPUT line."))

    def test_other_physicell_files_named_in_prose_refused(self):
        self.write(self.design, DESIGN_B)
        # the DATA_INPUT artifact itself may be named again
        self.accepted(self.task(self.design_line(), tail=f" See {DESIGN_REL}."))
        self.accepted(self.task(self.projection_line(), tail=f" See {MC_PROJ}."))
        for line, tail, kind in ((self.design_line(), f" Compare {DESIGN_B}.", "physicell_design"),
                                 (self.design_line(), f" Then apply {MC_PROJ}.", "physicell_projection"),
                                 (self.design_line(), f" Names: {MC_DICT}.", "physicell_dictionary"),
                                 (self.projection_line(), f" Design: {DESIGN_REL}.", "physicell_design"),
                                 (self.projection_line(), f" Names: {MC_DICT}.", "physicell_dictionary"),
                                 (self.projection_line(), " Folder: runs/orchestrator/p1/", "physicell_")):
            with self.subTest(tail=tail):
                self.refused(self.task(line, tail=tail), f"{kind}.*{MENTION}")
        # without a DATA_INPUT line, no such file may be named either
        self.refused(f"{MC_GATE}\nBuild the design {DESIGN_REL}.", "physicell_design.*no DATA_INPUT line")
        # nor a data-analyst path or session
        self.refused(f"{MC_GATE}\nUse runs/data-analyst/x.", "data-analyst")
        self.refused(self.task(self.design_line(), tail=" data-20990101T000000Z-deadbeef"), "data-analyst")


def load_hook():
    """The dispatch hook module, loaded in-process (its main() reads the event from sys.stdin)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("stage_gate_guard_under_test", HOOK)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_hook(module, root, subagent_type, prompt):
    """(exit status, stderr) of the in-process hook on one Agent dispatch event."""
    import contextlib
    import io
    event = {"tool_name": "Agent", "tool_input": {"subagent_type": subagent_type, "prompt": prompt}}
    err = io.StringIO()
    with mock.patch.object(module, "ROOT", root), mock.patch.object(sys, "stdin", io.StringIO(json.dumps(event))), \
            contextlib.redirect_stderr(err):
        status = module.main()
    return status, err.getvalue()


def gate_calls(root, subagent_type, prompt):
    """(hook exit status, stderr, check_task calls, gate.check calls) of one in-process dispatch."""
    hook = load_hook()
    with mock.patch.object(gate, "check_task", wraps=gate.check_task) as task, \
            mock.patch.object(gate, "check", wraps=gate.check) as check:
        status, err = run_hook(hook, root, subagent_type, prompt)
    return status, err, task.call_count, check.call_count


class HookRoleTests(unittest.TestCase):
    """PR #17 review 4586112665: every gated role takes DATA_INPUT; the hook has no check_job_mentions branch and
    fails closed (exit 2) on a gated role without a DATA_INPUT form."""

    def test_every_gated_role_has_a_data_input_line(self):
        self.assertLessEqual(load_hook().GATED, set(gate.DATA_INPUT_LINE))

    def test_gated_role_without_data_input_form_is_blocked(self):
        hook = load_hook()
        lines = {k: v for k, v in gate.DATA_INPUT_LINE.items() if k != "network_curator"}
        with mock.patch.object(gate, "DATA_INPUT_LINE", lines):
            status, err = run_hook(hook, state.ROOT, "network-curator", "Build a network.")
        self.assertEqual(status, 2)
        self.assertIn("network_curator has no DATA_INPUT form", err)

    def test_hook_does_not_import_check_job_mentions(self):
        self.assertNotIn("check_job_mentions", HOOK.read_text())

    def test_check_job_mentions_is_gone(self):
        # PR #17 review 4587429015: no caller remained; check_data_input runs the job-file rule for every gated role
        self.assertFalse(hasattr(gate, "check_job_mentions"))


if __name__ == "__main__":
    unittest.main()
