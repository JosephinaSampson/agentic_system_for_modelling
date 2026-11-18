"""Incremental approval: an edge-only edit of a reviewed network carries the earlier review over."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from scripts import gate, network_delta, state
from tests.support.reviews import recorded_reviews

PROSE = ("## Edge: {s} -> {t}\n**Verdict:** {verdict}\n**Interaction type:** {kind}\n**Confidence:** high\n"
         "### Evidence summary\nBounded evidence.\n### Supporting evidence\n- PMID 123 (DOI: none): experiment\n"
         "### Conflicting or context-specific evidence\nNone.\n### Excluded references\n"
         "### Open questions for researcher judgment\nNone.\n")


class NetworkDeltaTests(unittest.TestCase):
    def test_signed_regulators_follow_nested_negation(self):
        self.assertEqual(network_delta.regulators("(A | B) & !(C | !D) & !E"),
                         {"A": "activation", "B": "activation", "C": "inhibition", "D": "activation", "E": "inhibition"})
        self.assertEqual(network_delta.regulators("!!A & 1"), {"A": "activation"})
        for rule in ("A & !A", "A ^ B", "(A | B", "A & !", "A )"):
            with self.subTest(rule=rule), self.assertRaises(ValueError):
                network_delta.regulators(rule)

    def test_delta_lists_added_removed_and_sign_changed_edges_with_their_rules(self):
        before = "targets, factors\nA, A\nB, A & !C\nC, B\n"
        after = "targets, factors\nA, A | C\nB, A & C\nC, C\n"
        result = network_delta.delta(before, after)
        self.assertEqual(result["changes"], [
            {"change": "added", "source": "C", "target": "A", "sign": "activation"},
            {"change": "sign_changed", "source": "C", "target": "B", "sign": "activation"},
            {"change": "removed", "source": "B", "target": "C", "sign": "activation"},
            {"change": "added", "source": "C", "target": "C", "sign": "activation"}])
        self.assertEqual(result["rules"][0], {"target": "A", "before": "A", "after": "A | C"})
        self.assertEqual(network_delta.delta(before, before.replace("A & !C", "A  &  !C")), {"changes": [], "rules": []})

    def test_anything_but_an_edge_edit_needs_a_full_review(self):
        base = "A, A\nB, A & C\nC, C\n"
        for label, other in (("node sets differ", "A, A\nB, A & C\nC, C\nD, A\n"),
                             ("without an edge change", "A, A\nB, A | C\nC, C\n"),
                             ("cannot be read as signed edges", "A, A\nB, (A & !C) | (!A & C)\nC, C\n")):
            with self.subTest(label=label), self.assertRaisesRegex(ValueError, label + ".*full stage review"):
                network_delta.delta(base, other)


class StageDeltaTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        for name in ("CURRENT_STATE.md", "DECISIONS.md", "VALIDATION_PLAN.md"):
            shutil.copyfile(state.ROOT / "templates" / name, self.root / name)
        self.session = "session-123"
        self.ref = self.export("v1", "A, A\nB, A\nC, C\n")
        review = {"id": "review-1", "kind": "stage_review", "session_id": self.session, "stage": "topology",
                  "outcome": "approved", "artifact": self.ref}
        review.update(recorded_reviews(self.root, self.ref, "review-1"))
        state.update(self.root, "registry-add", dict(zip(state.REGISTRY, [
            "network-curator", self.session, "topology", "active", "—", "pending", self.ref["path"]])))
        state.update(self.root, "record", review)
        state.update(self.root, "record", self.approval("approval-1", self.ref))
        state.update(self.root, "registry-handoff", {"session_id": self.session, "handoff": "approved"})
        state.update(self.root, "registry-handoff", {"session_id": self.session, "handoff": "exported", "artifact": self.ref})
        # the researcher adds one edge after discussion: C activates B
        self.new = self.export("v2", "A, A\nB, A | C\nC, C\n")
        self.evidence = self.report("C", "B", "stimulation")

    def export(self, name, bnet_text):
        folder = self.root / "runs/network-curator" / self.session / "exports" / name
        folder.mkdir(parents=True)
        bnet = folder / "network.bnet"
        bnet.write_text("targets, factors\n" + bnet_text)
        manifest = {"source": {"server": "NeKo", "session_id": self.session}, "schema_name": "mcp-biomodelling-handoff",
                    "schema_version": "1.0", "handoff_type": "neko-to-maboss",
                    "network": {"nodes": ["A", "B", "C"], "output_nodes": ["B"]},
                    "bnet_file": {"path": str(bnet), "server": "NeKo", "session_id": self.session, "role": "neko_bnet",
                                  "name": bnet.name, "suffix": ".bnet", "media_type": "text/plain",
                                  "size_bytes": bnet.stat().st_size, "sha256": state.digest(bnet)}}
        handoff = folder / "handoff.json"
        handoff.write_text(json.dumps(manifest))
        return {"path": handoff.relative_to(self.root).as_posix(), "sha256": state.digest(handoff)}

    def report(self, source, target, effect, verdict="supported", kind="activation", session=None):
        folder = self.root / "evidence/reports" / (session or self.session) / "candidates"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{source}__{target}.md"
        footer = {"verdict": verdict, "interaction_type": kind, "confidence": "high", "supporting_pmids": ["123"],
                  "excluded_pmids": [], "edge": [source, effect, target], "neko_session_id": session or self.session}
        path.write_text(PROSE.format(s=source, t=target, verdict=verdict, kind=kind) + "```yaml\n" + json.dumps(footer) + "\n```\n")
        return {"path": path.relative_to(self.root).as_posix(), "sha256": state.digest(path)}

    def approval(self, name, ref):
        return {"id": name, "kind": "stage_approval", "session_id": self.session, "stage": "topology",
                "downstream_stage": "boolean-dynamics", "outputs": ["B"], "approved_by": "researcher",
                "authorization": "User approved this exact export and outputs.", "artifact": ref}

    def delta(self, name="delta-1", parent=None, artifact=None, **support):
        parent, artifact = parent or self.ref, artifact or self.new
        computed = network_delta.manifest_delta(self.root, parent, artifact, self.session)
        support = support or {"evidence": self.evidence}
        return {"id": name, "kind": "stage_delta", "session_id": self.session, "stage": "topology", "parent": parent,
                "artifact": artifact, "changes": [{**change, **support} for change in computed["changes"]],
                "rules": computed["rules"], "approved_by": "researcher",
                "authorization": "User: add C -> B as listed, keep the rest as reviewed."}

    def check(self):
        return gate.check(self.root, "boolean-dynamics", self.session, ["B"])

    def test_one_approved_edge_carries_the_review_over(self):
        state.update(self.root, "record", self.approval("approval-2", self.new))
        with self.assertRaisesRegex(ValueError, "different artifact revision"):
            self.check()  # an approval alone never covers an unreviewed export
        state.update(self.root, "record", self.delta())
        with self.assertRaisesRegex(ValueError, "approval must follow"):
            self.check()
        state.update(self.root, "record", self.approval("approval-3", self.new))
        result = self.check()
        self.assertTrue(result["passed"])
        self.assertEqual((result["review_id"], result["delta_id"], result["edges_changed_since_review"], result["artifact"]),
                         ("review-1", "delta-1", 1, self.new))
        prompt = 'STAGE_GATE {"upstream_session_id":"session-123","outputs":["B"]}\nImport.'
        self.assertTrue(gate.check_task(self.root, "boolean_dynamics_modeler", prompt)["passed"])

    def test_the_record_must_equal_the_computed_difference(self):
        good = self.delta()
        state.validate_record(self.root, good)
        cases = {
            "differ from the computed": [{**good, "changes": []},
                                         {**good, "changes": [{**good["changes"][0], "source": "A"}]},
                                         {**good, "changes": [{**good["changes"][0], "sign": "inhibition"}]},
                                         {**good, "rules": [{**good["rules"][0], "after": "A"}]}],
            "requires exactly": [{k: v for k, v in good.items() if k != "rules"}, {**good, "outcome": "approved"}],
            "topology stage only": [{**good, "stage": "boolean-dynamics"}],
            "explicit authorization": [{**good, "approved_by": "orchestrator"}, {**good, "authorization": ""}],
            "the reviewed export": [{**good, "artifact": self.ref}],
            "handoff manifests under": [{**good, "parent": self.evidence}],
        }
        for pattern, items in cases.items():
            for item in items:
                with self.subTest(pattern=pattern), self.assertRaisesRegex(ValueError, pattern):
                    state.validate_record(self.root, item)
        # the new export edited after the researcher saw the difference
        bnet = self.root / "runs/network-curator" / self.session / "exports/v2/network.bnet"
        bnet.write_text(bnet.read_text().replace("A | C", "A | C | B"))
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            state.validate_record(self.root, good)

    def test_each_change_needs_its_own_support(self):
        def refused(pattern, **support):
            item = self.delta()
            item["changes"] = [{k: v for k, v in item["changes"][0].items() if k != "evidence"} | support]
            with self.assertRaisesRegex(ValueError, pattern):
                state.validate_record(self.root, item)

        refused("exactly one of evidence")
        refused("exactly one of evidence", evidence=self.evidence, decision_id="approval-1")
        refused("is not about C -> B", evidence=self.report("A", "B", "stimulation"))
        refused("does not support activation", evidence=self.report("C", "B", "inhibition", kind="inhibition", session="s-inh"))
        refused("does not support activation", evidence=self.report("C", "B", "stimulation", verdict="contradicted", session="s-con"))
        refused("not a recorded stage_approval of stage data", decision_id="approval-1")
        refused("unknown stage_delta change field", evidence=self.evidence, note="trust me")
        # a removal may rest on a report of any verdict
        removed = self.export("v3", "A, A\nB, C\nC, C\n")
        weak = self.report("A", "B", "stimulation", verdict="insufficient evidence", session="s-weak")
        item = self.delta(artifact=removed)
        item["changes"] = [{k: v for k, v in c.items() if k != "evidence"}
                           | {"evidence": weak if c["change"] == "removed" else self.evidence} for c in item["changes"]]
        state.validate_record(self.root, item)

    def test_a_delta_needs_a_reviewed_parent(self):
        orphan = self.export("v0", "A, A\nB, A\nC, C\n")  # the same rules as v1, but never reviewed
        with self.assertRaisesRegex(ValueError, "no earlier recorded review"):
            state.update(self.root, "record", self.delta(parent=orphan, artifact=self.new))
        self.assertNotIn("delta-1", (self.root / "DECISIONS.md").read_text())
        state.update(self.root, "record", self.delta())
        state.update(self.root, "record", self.approval("approval-2", self.new))
        self.assertTrue(self.check()["passed"])
        # a later blocking review of the parent takes the delta down with it
        block = {"id": "review-2", "kind": "stage_review", "session_id": self.session, "stage": "topology",
                 "outcome": "blocked", "artifact": self.ref, **recorded_reviews(self.root, self.ref, "review-2")}
        state.update(self.root, "record", block)
        with self.assertRaises(ValueError):
            self.check()

    def test_accumulated_changes_past_the_limit_need_a_full_review(self):
        state.update(self.root, "record", self.delta())
        third = self.export("v3", "A, A | B\nB, A | C\nC, C\n")
        chained = self.delta("delta-2", parent=self.new, artifact=third, evidence=self.report("B", "A", "stimulation"))
        with mock.patch.object(state, "DELTA_REVIEW_LIMIT", 1), self.assertRaisesRegex(ValueError, "2 edges changed"):
            state.update(self.root, "record", chained)
        state.update(self.root, "record", chained)
        state.update(self.root, "record", self.approval("approval-2", third))
        result = self.check()
        self.assertEqual((result["review_id"], result["delta_id"], result["edges_changed_since_review"]),
                         ("review-1", "delta-2", 2))
        with mock.patch.object(state, "DELTA_REVIEW_LIMIT", 1), self.assertRaisesRegex(ValueError, "full stage review"):
            self.check()

    def test_the_command_prints_what_the_record_must_contain(self):
        result = subprocess.run([sys.executable, str(state.ROOT / "scripts/network_delta.py"), self.ref["path"],
                                 self.new["path"], "--root", str(self.root)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        printed = json.loads(result.stdout)
        self.assertEqual((printed["session_id"], printed["parent"], printed["artifact"]), (self.session, self.ref, self.new))
        self.assertEqual(printed["changes"], [{"change": "added", "source": "C", "target": "B", "sign": "activation"}])
        refused = subprocess.run([sys.executable, str(state.ROOT / "scripts/network_delta.py"), self.ref["path"],
                                  self.ref["path"], "--root", str(self.root)], capture_output=True, text=True)
        self.assertEqual(refused.returncode, 2)
        self.assertFalse(json.loads(refused.stdout)["passed"])


if __name__ == "__main__":
    unittest.main()
