import hashlib
import io
import json
import math
import tarfile
import unittest
from pathlib import Path
from unittest import mock

try:
    from tests.data.test_jobs_build import MAP, SETTINGS, JobFixture, template
except ImportError:  # discovered with -s tests/data
    from test_jobs_build import MAP, SETTINGS, JobFixture, template

from scripts.jobs import import_results, launch

SEEDS = [11, 12, 13]
GOOD = {"kf1": 0.5, "kf2": 0.1}


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def dump(obj) -> bytes:
    return (json.dumps(obj, indent=2) + "\n").encode()


class ImportFixture(JobFixture):
    def setUp(self):
        super().setUp()
        data = template()
        data.update(restarts=3, seeds=SEEDS, good_fit_margin=0.1, reproduce_rtol=1e-3)
        (self.root / SETTINGS).write_text(json.dumps(data), encoding="utf-8")
        self.job_dir = self.build()
        self.job_id = self.job_dir.name
        self.job_bytes = (self.job_dir / "job.json").read_bytes()
        self.calls = []

    @property
    def archive_path(self) -> Path:
        return self.root / "inputs" / "calibration-results" / self.job_id / "results.tar.gz"

    def restart(self, n, score, params=None, status="ok", seed=None):
        return {"restart": n, "seed": SEEDS[n - 1] if seed is None else seed,
                "score": score if status == "ok" else None, "status": status,
                "params": dict(GOOD if params is None else params) if status == "ok" else {}, "wall_time_s": 1.5 + n}

    def write_archive(self, restarts=None, *, env=None, job_sha=None, tamper=None, extra=None, drop=None,
                      results_edit=None, partial=False):
        restarts = restarts or [self.restart(1, 1.0), self.restart(2, 1.05), self.restart(3, 1.5)]
        files = {}
        for r in restarts:
            files[f"restarts/restart-{r['restart']}/result.json"] = dump(r)
            files[f"restarts/restart-{r['restart']}/log.txt"] = b"log\n"
        files["selftest/validate.json"] = dump({"params": {"kf1": 1.0, "kf2": 0.5}, "score": 0.1})
        env_report = {"status": "ok", "python": {"version": "3.12.15", "implementation": "CPython"},
                      "packages": {"biomass": "0.14.0", "numpy": "2.5.4"}}
        env_report.update(env or {})
        files["env_report.json"] = dump(env_report)
        files["figures/data_vs_fit.png"] = b"\x89PNG-fake"
        files["figures/parameters.png"] = b"\x89PNG-fake2"
        results = {"job_sha256": job_sha or sha(self.job_bytes), "partial": partial, "restarts": restarts,
                   "files": {k: sha(v) for k, v in sorted(files.items())}}
        if results_edit:
            results_edit(results)
        for name in drop or []:
            files.pop(name)
        for name, value in (tamper or {}).items():
            files[name] = value
        files["results.json"] = dump(results)
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            for name, data in sorted(files.items()):
                info = tarfile.TarInfo(name)
                info.size = len(data)
                tf.addfile(info, io.BytesIO(data))
            for info, data in extra or []:
                tf.addfile(info, io.BytesIO(data) if data is not None else None)
        self.archive_path.parent.mkdir(parents=True, exist_ok=True)
        self.archive_path.write_bytes(buf.getvalue())
        return self.archive_path

    def fake(self, score=1.0):
        def reproduce(root, job_id, params_file):
            self.calls.append((job_id, json.loads(Path(params_file).read_text(encoding="utf-8"))))
            return score
        return reproduce

    def run_import(self, score=1.0):
        return import_results.import_results(self.root, self.job_id, reproduce=self.fake(score))

    def proposal(self, **kw):
        return json.loads(self.run_import(**kw).read_text(encoding="utf-8"))

    def refuses(self, pattern, score=1.0):
        with self.assertRaisesRegex(ValueError, pattern):
            self.run_import(score)
        self.assertFalse((self.job_dir / "calibration_proposal.json").exists())
        self.assertFalse(list(self.job_dir.glob(".import-*")))


class RefusalTests(ImportFixture):
    def test_file_hash_differs(self):
        self.write_archive(tamper={"restarts/restart-1/log.txt": b"changed\n"})
        self.refuses("restarts/restart-1/log.txt.*sha256")

    def test_job_sha_differs(self):
        self.write_archive(job_sha="0" * 64)
        self.refuses("job_sha256")

    def test_env_python_version(self):
        self.write_archive(env={"python": {"version": "3.11.9", "implementation": "CPython"}})
        self.refuses("Python 3.12")

    def test_env_biomass_version(self):
        self.write_archive(env={"packages": {"biomass": "0.13.0"}})
        self.refuses("biomass 0.14.0")

    def test_seed_differs(self):
        self.write_archive([self.restart(1, 1.0, seed=99), self.restart(2, 1.05)])
        self.refuses("seed 99.*restart 1")

    def test_value_outside_bounds(self):
        self.write_archive([self.restart(1, 1.0, params={"kf1": 50.0, "kf2": 0.1})])
        self.refuses("kf1.*outside")

    def test_zero_successful_restarts(self):
        self.write_archive([self.restart(1, None, status="failed"), self.restart(2, None, status="failed")])
        self.refuses("zero successful restarts")

    def test_reproduced_score_differs(self):
        self.write_archive()
        self.refuses("reproduc.*differs", score=1.01)
        self.assertEqual(self.calls[0], (self.job_id, GOOD))

    def test_reproduced_score_within_rtol_is_accepted(self):
        self.write_archive()
        self.assertEqual(self.proposal(score=1.0 * (1 + 5e-4))["best_restart"], 1)

    def test_missing_archive(self):
        self.refuses("results.tar.gz")


class ArchiveMemberTests(ImportFixture):
    def test_unlisted_member(self):
        info = tarfile.TarInfo("extra.txt")
        info.size = 3
        self.write_archive(extra=[(info, b"abc")])
        self.refuses("extra.txt.*not listed")

    def test_listed_member_missing(self):
        self.write_archive(drop=["figures/parameters.png"])
        self.refuses("figures/parameters.png.*missing")

    def test_traversal_member(self):
        for name in ("../evil.txt", "/abs.txt", "a/../../b.txt"):
            with self.subTest(name=name):
                info = tarfile.TarInfo(name)
                info.size = 1
                self.write_archive(extra=[(info, b"x")])
                self.refuses("unsafe")

    def test_symlink_hardlink_and_device_members(self):
        for kind in (tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.CHRTYPE, tarfile.DIRTYPE):
            with self.subTest(kind=kind):
                info = tarfile.TarInfo("link")
                info.type = kind
                info.linkname = "results.json"
                self.write_archive(extra=[(info, None)])
                self.refuses("not a regular file")
        self.assertFalse((self.root / "evil.txt").exists())

    def test_duplicate_member(self):
        info = tarfile.TarInfo("env_report.json")
        info.size = 2
        self.write_archive(extra=[(info, b"{}")])
        self.refuses("duplicate")


class ProposalTests(ImportFixture):
    def test_proposal_kind_and_model_binding(self):
        self.write_archive()
        path = self.run_import()
        self.assertEqual(path, self.job_dir / "calibration_proposal.json")
        data = json.loads(path.read_text(encoding="utf-8"))
        job = json.loads(self.job_bytes)
        mapping = self.load()
        self.assertEqual(data["kind"], "calibration_proposal")
        self.assertEqual(data["schema_version"], 1)
        self.assertEqual(data["job_id"], self.job_id)
        self.assertEqual(data["job_sha256"], sha(self.job_bytes))
        self.assertEqual(data["results_sha256"], sha(self.archive_path.read_bytes()))
        self.assertEqual(data["mapping"], {"path": MAP, "sha256": job["mapping"]["sha256"], "approval": "ode-map-1"})
        self.assertEqual(data["model"], {"ode_session": mapping["inputs"]["model"]["ode_session"],
                                         "revision": mapping["inputs"]["model"]["revision"],
                                         "bundle_sha256": job["export_sha256"]})
        self.assertFalse(data["partial"])
        self.assertEqual(data["best_restart"], 1)
        self.assertIn("heuristics_note", data)
        self.assertEqual(data["figures"], ["figures/data_vs_fit.png", "figures/parameters.png"])
        for fig in data["figures"]:
            self.assertTrue((self.job_dir / fig).is_file())
        report = (self.job_dir / "report.md").read_text(encoding="utf-8")
        self.assertIn("(figures/data_vs_fit.png)", report)
        self.assertIn("kf1", report)
        self.assertEqual((self.job_dir / "job.json").read_bytes(), self.job_bytes)

    def test_parameter_rows_carry_mapping_fields_and_start(self):
        self.write_archive()
        rows = {p["name"]: p for p in self.proposal()["parameters"]}
        self.assertEqual(rows["kf1"]["kind"], "parameter")
        self.assertEqual(rows["kf1"]["unit"], "1/min")
        self.assertEqual((rows["kf1"]["lower"], rows["kf1"]["upper"]), (0.001, 10.0))
        self.assertEqual(rows["kf1"]["start"], 1.0)  # selftest/validate.json = nominal values
        self.assertEqual(rows["kf2"]["start"], 0.5)
        self.assertEqual(set(rows["kf1"]), {"name", "kind", "unit", "start", "lower", "upper", "best", "good_min",
                                            "good_median", "good_max", "poorly_constrained"})

    def test_partial_results_accepted_and_labelled(self):
        self.write_archive([self.restart(1, 1.0), self.restart(2, 1.02), self.restart(3, None, status="failed")],
                           partial=False)
        data = self.proposal()
        self.assertTrue(data["partial"])
        self.assertEqual(data["good_restarts"], [1, 2])
        statuses = {r["restart"]: r["status"] for r in data["restarts"]}
        self.assertEqual(statuses, {1: "ok", 2: "ok", 3: "failed"})
        self.assertEqual(set(data["restarts"][0]), {"restart", "seed", "score", "status", "wall_time_s"})

    def test_missing_restarts_make_it_partial(self):
        self.write_archive([self.restart(1, 1.0), self.restart(2, 1.02)])
        data = self.proposal()
        self.assertTrue(data["partial"])
        self.assertEqual(data["good_restarts"], [1, 2])

    def test_good_restarts_and_poorly_constrained_flag(self):
        self.write_archive([
            self.restart(1, 1.0, params={"kf1": 1e-3, "kf2": 0.1}),
            self.restart(2, 1.05, params={"kf1": 1e-1, "kf2": 0.2}),
            self.restart(3, 1.5, params={"kf1": 5.0, "kf2": 0.9})])
        data = self.proposal()
        self.assertEqual(data["good_restarts"], [1, 2])
        rows = {p["name"]: p for p in data["parameters"]}
        self.assertEqual((rows["kf1"]["good_min"], rows["kf1"]["good_max"]), (1e-3, 1e-1))
        self.assertTrue(rows["kf1"]["poorly_constrained"])
        self.assertEqual(rows["kf1"]["best"], 1e-3)
        self.assertFalse(rows["kf2"]["poorly_constrained"])
        self.assertAlmostEqual(rows["kf2"]["good_median"], 0.15)
        self.assertTrue(math.isclose(import_results.POORLY_CONSTRAINED_LOG10_SPAN, 1.0))

    def test_best_restart_is_lowest_score_and_reproduced(self):
        self.write_archive([self.restart(1, 2.0), self.restart(2, 1.0, params={"kf1": 0.2, "kf2": 0.3}),
                            self.restart(3, 1.5)])
        data = self.proposal()
        self.assertEqual(data["best_restart"], 2)
        self.assertEqual(self.calls[0][1], {"kf1": 0.2, "kf2": 0.3})

    def test_failure_writes_nothing_and_rerun_after_fix_works(self):
        self.write_archive()
        self.refuses("reproduc", score=2.0)
        self.assertTrue(self.run_import().is_file())

    def test_reimport_identical_ok_but_never_overwrites_a_different_proposal(self):
        self.write_archive()
        first = self.run_import().read_bytes()
        self.assertEqual(self.run_import().read_bytes(), first)
        self.write_archive([self.restart(1, 1.0), self.restart(2, 1.3), self.restart(3, 1.5)])
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.run_import()

    def test_inputs_are_not_modified(self):
        self.write_archive()
        before = self.archive_path.read_bytes()
        self.run_import()
        self.assertEqual(self.archive_path.read_bytes(), before)
        self.assertEqual(sorted(p.name for p in self.archive_path.parent.iterdir()), ["results.tar.gz"])


class DefaultReproduceTests(ImportFixture):
    def test_default_reproduce_runs_launch_validate_with_best_params(self):
        self.write_archive()
        seen = {}

        def fake_validate(root, job_id, params, use, out):
            seen.update(job_id=job_id, params=json.loads(Path(params).read_text(encoding="utf-8")), use=use)
            Path(out).write_text(json.dumps({"score": 1.0, "use": use}), encoding="utf-8")
            return 0

        with mock.patch.object(launch, "validate", fake_validate):
            path = import_results.import_results(self.root, self.job_id)
        self.assertEqual(seen, {"job_id": self.job_id, "params": GOOD, "use": "fit"})
        self.assertTrue(path.is_file())

    def test_default_reproduce_failure_is_a_refusal(self):
        self.write_archive()
        with mock.patch.object(launch, "validate", lambda *a: 1), \
                self.assertRaisesRegex(ValueError, "validate.*exit 1"):
            import_results.import_results(self.root, self.job_id)

    def test_cli(self):
        self.write_archive()
        out = io.StringIO()
        with mock.patch.dict("os.environ", {"CLAUDE_PROJECT_DIR": str(self.root)}), \
                mock.patch.object(import_results, "default_reproduce", self.fake()), mock.patch("sys.stdout", out):
            code = import_results.main([self.job_id])
        self.assertEqual(code, 0)
        self.assertIn("calibration_proposal.json", out.getvalue())


if __name__ == "__main__":
    unittest.main()
