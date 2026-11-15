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
        self.write_archive()
        # no_start: the local reproduce step returned no nominal values, so every start is null
        path = self.run_import(nominal={} if no_start else None)
        self.proposal_rel = path.relative_to(self.root).as_posix()
        return path

    def runner(self, score=0.7, n_rows=2):
        def run(root, job_id, params_file):
            self.seen.append((job_id, json.loads(Path(params_file).read_text(encoding="utf-8"))))
            return score, n_rows
        return run

    def score(self, approval="cal-1", score=0.7, n_rows=2):
        return score_holdout.score_holdout(self.root, self.job_id, approval, runner=self.runner(score, n_rows))


class HoldoutTests(HoldoutFixture):
    def test_accepted_subset_at_best_others_at_start(self):
        self.make_proposal()
        self.approve_calibration()
        data = json.loads(self.score().read_text(encoding="utf-8"))
        self.assertEqual(self.seen, [(self.job_id, {"kf1": GOOD["kf1"], "kf2": 0.5})])
        self.assertEqual(data["score"], 0.7)
        self.assertEqual(data["n_rows"], 2)
        self.assertEqual(data["accepted"], ["kf1"])
        self.assertEqual(data["parameter_source"], {"kf1": "accepted best", "kf2": "start"})
        self.assertEqual(data["proposal"]["sha256"], sha((self.root / self.proposal_rel).read_bytes()))
        self.assertFalse(list(self.job_dir.glob(".holdout-*")))

    def test_no_predicted_holdout_row_refuses(self):
        # compare.objective sums nothing (0.0) when no row has use=holdout: never a once-only score
        self.make_proposal()
        self.approve_calibration()
        with self.assertRaisesRegex(ValueError, "no hold-out"):
            self.score(score=0.0, n_rows=0)
        self.assertFalse((self.job_dir / "holdout_score.json").exists())

    def test_default_runner_counts_predicted_holdout_rows(self):
        reports = []

        def fake_validate(root, job_id, params, use, out):
            self.assertEqual(use, "holdout")
            Path(out).write_text(json.dumps(reports[-1]), encoding="utf-8")
            return 0
        rows = {"h1": {"use": "holdout", "role": "fit_target", "prediction": 1.0},
                "h2": {"use": "holdout", "role": "fit_target", "prediction": None},
                "h3": {"use": "holdout", "role": "reference", "prediction": None},
                "f1": {"use": "fit", "role": "fit_target", "prediction": 2.0}}
        params = self.root / "p.json"
        with mock.patch.object(score_holdout.launch, "validate", side_effect=fake_validate):
            reports.append({"score": 0.5, "predictions": rows})
            self.assertEqual(score_holdout.default_runner(self.root, self.job_id, params), (0.5, 1))
            reports.append({"score": 0.0, "predictions": {"f1": rows["f1"]}})
            self.assertEqual(score_holdout.default_runner(self.root, self.job_id, params), (0.0, 0))
            reports.append({"score": 0.0})
            with self.assertRaisesRegex(ValueError, "predictions"):
                score_holdout.default_runner(self.root, self.job_id, params)

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
