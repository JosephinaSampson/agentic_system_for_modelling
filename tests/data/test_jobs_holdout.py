import io
import json
import unittest
from unittest import mock
from pathlib import Path

try:
    from tests.data.test_jobs_import import GOOD, ImportFixture, sha
except ImportError:  # discovered with -s tests/data
    from test_jobs_import import GOOD, ImportFixture, sha

from scripts import state
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
        archive = {}
        if no_start:  # an archive without selftest/validate.json: every start is null
            archive = {"drop": ["selftest/validate.json"],
                       "results_edit": lambda r: r["files"].pop("selftest/validate.json")}
        self.write_archive(**archive)
        path = self.run_import()
        self.proposal_rel = path.relative_to(self.root).as_posix()
        return path

    def runner(self, score=0.7):
        def run(root, job_id, params_file):
            self.seen.append((job_id, json.loads(Path(params_file).read_text(encoding="utf-8"))))
            return score
        return run

    def score(self, approval="cal-1", score=0.7):
        return score_holdout.score_holdout(self.root, self.job_id, approval, runner=self.runner(score))


class HoldoutTests(HoldoutFixture):
    def test_accepted_subset_at_best_others_at_start(self):
        self.make_proposal()
        self.approve_calibration()
        data = json.loads(self.score().read_text(encoding="utf-8"))
        self.assertEqual(self.seen, [(self.job_id, {"kf1": GOOD["kf1"], "kf2": 0.5})])
        self.assertEqual(data["score"], 0.7)
        self.assertEqual(data["accepted"], ["kf1"])
        self.assertEqual(data["parameter_source"], {"kf1": "accepted best", "kf2": "start"})
        self.assertEqual(data["proposal"]["sha256"], sha((self.root / self.proposal_rel).read_bytes()))
        self.assertFalse(list(self.job_dir.glob(".holdout-*")))

    def test_holdout_written_once(self):
        self.make_proposal()
        self.approve_calibration()
        path = self.score()
        before = path.read_bytes()
        with self.assertRaisesRegex(ValueError, "exists"):
            self.score(score=0.1)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(len(self.seen), 1)

    def test_null_start_for_a_non_accepted_parameter_refuses(self):
        self.make_proposal(no_start=True)
        self.approve_calibration()
        with self.assertRaisesRegex(ValueError, "kf2.*start.*unknown"):
            self.score()
        self.assertEqual(self.seen, [])
        self.assertFalse((self.job_dir / "holdout_score.json").exists())

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

    def test_failed_runner_writes_nothing(self):
        self.make_proposal()
        self.approve_calibration()

        def bad(root, job_id, params_file):
            raise ValueError("boom")
        with self.assertRaisesRegex(ValueError, "boom"):
            score_holdout.score_holdout(self.root, self.job_id, "cal-1", runner=bad)
        self.assertFalse((self.job_dir / "holdout_score.json").exists())
        self.assertFalse(list(self.job_dir.glob(".holdout-*")))


if __name__ == "__main__":
    unittest.main()
