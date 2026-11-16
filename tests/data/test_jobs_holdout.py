import io
import json
import unittest
from unittest import mock
from pathlib import Path

try:
    from tests.data.test_jobs_import import GOOD, NOMINAL, ImportFixture, sha
except ImportError:  # discovered with -s tests/data
    from test_jobs_import import GOOD, NOMINAL, ImportFixture, sha

from scripts import state
from scripts.data import approvals
from scripts.jobs import score_holdout
from tests.data.test_ode_mapping import SESSION


class HoldoutFixture(ImportFixture):
    def setUp(self):
        super().setUp()
        self.seen = []

    def approve_calibration(self, record_id="cal-1", outputs=("kf1",), stage="ode_calibration"):
        rel = self.proposal_rel
        state.update(self.root, "record", {
            "id": record_id, "kind": "stage_approval", "stage": stage, "session_id": SESSION,
            "outputs": list(outputs), "artifact": {"path": rel, "sha256": sha((self.root / rel).read_bytes())},
            "approved_by": "researcher", "authorization": "researcher: accepted values"})
        return record_id

    def make_proposal(self, no_start=False):
        self.write_archive()
        # no_start: the local reproduce step returned no nominal values, so every start is null
        path = self.run_import(nominal={} if no_start else None)
        self.proposal_rel = path.relative_to(self.root).as_posix()
        return path

    def runner(self, score=0.7, n_rows=2, failed=()):
        def run(root, job_id, params_file):
            given = json.loads(Path(params_file).read_text(encoding="utf-8"))
            self.seen.append((job_id, given))
            return {"score": score, "n_rows": n_rows, "failed": list(failed),
                    "params": {**NOMINAL, **given}}
        return run

    def score(self, approval="cal-1", score=0.7, n_rows=2, failed=()):
        return score_holdout.score_holdout(self.root, self.job_id, approval,
                                           runner=self.runner(score, n_rows, failed))

    def rewrite_record(self, record_id, edit):
        """Edit a DECISIONS.md record in place, bypassing state.py validation (a hand-edited record)."""
        decisions = self.root / "DECISIONS.md"

        def replace(match):
            item = json.loads(match.group(1))
            if item.get("id") != record_id:
                return match.group(0)
            edit(item)
            return match.group(0).replace(match.group(1), json.dumps(item, indent=2))
        decisions.write_text(state.RECORD.sub(replace, decisions.read_text(encoding="utf-8")), encoding="utf-8")


class HoldoutTests(HoldoutFixture):
    def test_only_accepted_values_are_passed(self):
        # PR 16 review score_holdout.py:102: non-accepted values are not passed (the model defaults apply)
        self.make_proposal()
        self.approve_calibration()
        data = json.loads(self.score().read_text(encoding="utf-8"))
        self.assertEqual(self.seen, [(self.job_id, {"kf1": GOOD["kf1"]})])
        self.assertEqual(data["score"], 0.7)
        self.assertEqual(data["n_rows"], 2)
        self.assertEqual(data["accepted"], ["kf1"])
        self.assertEqual(data["parameters"], {"kf1": GOOD["kf1"]})
        self.assertEqual(data["effective_parameters"], {"kf1": GOOD["kf1"], "kf2": NOMINAL["kf2"]})
        self.assertNotIn("parameter_source", data)
        self.assertIn("model default", data["note"])
        self.assertEqual(data["proposal"]["sha256"], sha((self.root / self.proposal_rel).read_bytes()))
        self.assertFalse(list(self.job_dir.glob(".holdout-*")))

    def test_failed_holdout_predictions_write_nothing(self):
        # PR 16 review score_holdout.py:115: a solver failure (PENALTY) is never the one-time hold-out score
        self.make_proposal()
        self.approve_calibration()
        with self.assertRaisesRegex(ValueError, "failed.*h2"):
            self.score(score=1e12, n_rows=1, failed=("h2",))
        with self.assertRaisesRegex(ValueError, "penalty"):
            self.score(score=1e12, n_rows=2)
        self.assertFalse((self.job_dir / "holdout_score.json").exists())
        self.score()  # nothing was written, so a later successful scoring is still the first look
        self.assertTrue((self.job_dir / "holdout_score.json").is_file())

    def test_no_predicted_holdout_row_refuses(self):
        # compare.objective sums nothing (0.0) when no row has use=holdout: never a once-only score
        self.make_proposal()
        self.approve_calibration()
        with self.assertRaisesRegex(ValueError, "no hold-out"):
            self.score(score=0.0, n_rows=0)
        self.assertFalse((self.job_dir / "holdout_score.json").exists())

    def test_default_runner_counts_predicted_and_failed_holdout_rows(self):
        reports, calls = [], []

        def fake_validate(root, job_id, params, use, out, partial=False):
            calls.append((use, partial))
            Path(out).write_text(json.dumps(reports[-1]), encoding="utf-8")
            return 0
        rows = {"h1": {"use": "holdout", "role": "fit_target", "prediction": 1.0},
                "h2": {"use": "holdout", "role": "fit_target", "prediction": None},
                "h3": {"use": "holdout", "role": "reference", "prediction": None},
                "f1": {"use": "fit", "role": "fit_target", "prediction": 2.0}}
        params = self.root / "p.json"
        effective = {"kf1": 0.5, "kf2": 0.5}
        with mock.patch.object(score_holdout.launch, "validate", side_effect=fake_validate):
            reports.append({"score": 0.5, "params": effective, "predictions": rows})
            self.assertEqual(score_holdout.default_runner(self.root, self.job_id, params),
                             {"score": 0.5, "n_rows": 1, "failed": ["h2"], "params": effective})
            reports.append({"score": 0.0, "params": effective, "predictions": {"f1": rows["f1"]}})
            self.assertEqual(score_holdout.default_runner(self.root, self.job_id, params)["n_rows"], 0)
            reports.append({"score": 0.0, "params": effective})
            with self.assertRaisesRegex(ValueError, "predictions"):
                score_holdout.default_runner(self.root, self.job_id, params)
        self.assertEqual(calls[0], ("holdout", True))  # --partial: the others keep the model defaults

    def test_holdout_written_once(self):
        self.make_proposal()
        self.approve_calibration()
        path = self.score()
        before = path.read_bytes()
        with self.assertRaisesRegex(ValueError, "exists"):
            self.score(score=0.1)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(len(self.seen), 1)

    def test_null_start_for_a_non_accepted_parameter_is_not_needed(self):
        self.make_proposal(no_start=True)
        self.approve_calibration()
        self.score()
        self.assertEqual(self.seen, [(self.job_id, {"kf1": GOOD["kf1"]})])

    def test_null_start_is_fine_when_every_parameter_is_accepted(self):
        self.make_proposal(no_start=True)
        self.approve_calibration(outputs=("kf1", "kf2"))
        self.score()
        self.assertEqual(self.seen[0][1], GOOD)

    def test_approval_checks(self):
        self.make_proposal()
        self.approve_calibration("cal-wrong-stage", stage="ode_mapping")
        self.approve_calibration("cal-bad-output", outputs=("nope",))
        for record in ("cal-wrong-stage", "cal-bad-output", "missing"):
            with self.assertRaises(ValueError):
                self.score(record)
        self.assertEqual(self.seen, [])

    def test_malformed_or_wrong_kind_proposal_refuses_cleanly(self):
        path = self.make_proposal()
        self.approve_calibration()
        data = json.loads(path.read_text(encoding="utf-8"))
        for n, edit in enumerate((lambda d: d.update(kind="other"), lambda d: d.pop("parameters"),
                                  lambda d: d.update(parameters=[1]))):
            broken = json.loads(json.dumps(data))
            edit(broken)
            path.write_text(json.dumps(broken), encoding="utf-8")
            self.approve_calibration(f"cal-x{n}")
            with self.assertRaises(ValueError):
                self.score(f"cal-x{n}")
        self.assertEqual(self.seen, [])

    def test_non_finite_best_for_an_accepted_parameter_refuses(self):
        path = self.make_proposal()
        data = json.loads(path.read_text(encoding="utf-8"))
        data["parameters"][0]["best"] = None
        path.write_text(json.dumps(data), encoding="utf-8")
        self.approve_calibration()
        with self.assertRaisesRegex(ValueError, "kf1.*finite best"):
            self.score()

    def test_main_reports_malformed_input_without_traceback(self):
        path = self.make_proposal()
        self.approve_calibration()
        path.write_text("[]", encoding="utf-8")
        with mock.patch.object(score_holdout, "project_root", return_value=self.root), \
                mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            self.assertEqual(score_holdout.main([self.job_id, "--approval", "cal-1"]), 1)
        self.assertIn("error:", err.getvalue())

    def test_approval_is_checked_by_the_shared_approvals_helper(self):
        # PR 16 review score_holdout.py:89: one implementation for the gate and score_holdout
        self.make_proposal()
        self.approve_calibration()
        real, used = approvals.require_calibration_approval, []

        def spy(*args, **kw):
            used.append(args[1])
            return real(*args, **kw)
        with mock.patch.object(approvals, "require_calibration_approval", spy):
            self.score()
        self.assertEqual(used, ["cal-1"])

    def test_approval_without_researcher_authorization_refused(self):
        # score_holdout skipped validate_record before (drift from the gate)
        self.make_proposal()
        self.approve_calibration()
        self.rewrite_record("cal-1", lambda r: r.pop("authorization"))
        with self.assertRaisesRegex(ValueError, "authorization"):
            self.score()
        self.assertEqual(self.seen, [])

    def test_failed_runner_writes_nothing(self):
        self.make_proposal()
        self.approve_calibration()

        def bad(root, job_id, params_file):
            raise ValueError("boom")
        with self.assertRaisesRegex(ValueError, "boom"):
            score_holdout.score_holdout(self.root, self.job_id, "cal-1", runner=bad)
        self.assertFalse((self.job_dir / "holdout_score.json").exists())
        self.assertFalse(list(self.job_dir.glob(".holdout-*")))


class CalibrationApprovalTests(HoldoutFixture):
    """approvals.require_calibration_approval: every check of an ode_calibration approval, in one place."""

    def setUp(self):
        super().setUp()
        self.make_proposal()
        self.digest = sha((self.root / self.proposal_rel).read_bytes())

    def check(self, record_id="cal-1"):
        return approvals.require_calibration_approval(self.root, record_id, self.proposal_rel, self.digest)

    def test_returns_the_record_and_the_proposal_of_the_approved_bytes(self):
        self.approve_calibration()
        record, proposal = self.check()
        self.assertEqual(record["id"], "cal-1")
        self.assertEqual(proposal, json.loads((self.root / self.proposal_rel).read_text(encoding="utf-8")))

    def test_refusals(self):
        self.approve_calibration("cal-sub", outputs=("kf1", "nope"))
        self.approve_calibration("cal-stage", stage="ode_mapping")
        for record_id, pattern in (("cal-sub", "outputs"), ("cal-stage", "ode_calibration"),
                                   ("missing", "decision_id")):
            with self.subTest(record_id=record_id), self.assertRaisesRegex(ValueError, pattern):
                self.check(record_id)
        self.approve_calibration("cal-ses")
        self.rewrite_record("cal-ses", lambda r: r.update(session_id="other-session"))
        with self.assertRaisesRegex(ValueError, "ODE session"):
            self.check("cal-ses")
        self.approve_calibration("cal-auth")
        self.rewrite_record("cal-auth", lambda r: r.update(approved_by="someone"))
        with self.assertRaisesRegex(ValueError, "authorization"):
            self.check("cal-auth")
        self.approve_calibration("cal-dup")
        self.rewrite_record("cal-dup", lambda r: r.update(outputs=["kf1", "kf1"]))
        with self.assertRaisesRegex(ValueError, "outputs"):
            self.check("cal-dup")
        with self.assertRaisesRegex(ValueError, "sha256"):
            approvals.require_calibration_approval(self.root, "cal-sub", self.proposal_rel, "0" * 64)

    def test_not_a_proposal_refused(self):
        path = self.root / self.proposal_rel
        data = json.loads(path.read_text(encoding="utf-8"))
        path.write_text(json.dumps({**data, "kind": "other"}), encoding="utf-8")
        self.digest = sha(path.read_bytes())
        self.approve_calibration()
        with self.assertRaisesRegex(ValueError, "calibration_proposal"):
            self.check()


if __name__ == "__main__":
    unittest.main()
