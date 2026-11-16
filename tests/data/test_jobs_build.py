import copy
import hashlib
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from scripts import state

try:
    from tests.data.test_ode_mapping import BUNDLE, MAP, ROWS, SESSION, OdeFixture
except ImportError:  # discovered with -s tests/data
    from test_ode_mapping import BUNDLE, MAP, ROWS, SESSION, OdeFixture

from scripts.jobs import build, launch, settings
from scripts.jobs.runtime import run

REPO = state.ROOT
SETTINGS = "runs/orchestrator/p1/calibration-settings.json"
RUNTIME = REPO / "scripts" / "jobs" / "runtime"
DE_KEYS = {"strategy", "maxiter", "popsize", "tol", "mutation", "recombination", "polish"}


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def template(name="first-pass"):
    return json.loads((REPO / "templates" / f"calibration-settings.{name}.json").read_text(encoding="utf-8"))


class SettingsTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "s.json"

    def load(self, data):
        self.path.write_text(json.dumps(data), encoding="utf-8")
        return settings.load(self.path)

    def test_templates_carry_biomass_defaults_and_suggested_restarts(self):
        for name, restarts in (("first-pass", 10), ("final", 30)):
            data = self.load(template(name))
            self.assertEqual(data["restarts"], restarts)
            self.assertEqual(len(data["seeds"]), restarts)
            self.assertEqual(data["optimizer_options"], {
                "strategy": "best1bin", "maxiter": 50, "popsize": 3, "tol": 1e-4, "mutation": 0.1,
                "recombination": 0.5, "polish": False})
            self.assertEqual(data["workers"], 1)
            self.assertEqual(data["reproduce_rtol"], 1e-3)

    def test_settings_requires_every_key_and_pinned_optimizer_keys(self):
        good = template()
        self.assertEqual(self.load(good)["restarts"], 10)
        for key in good:
            bad = copy.deepcopy(good)
            del bad[key]
            with self.assertRaisesRegex(ValueError, key):
                self.load(bad)
        with self.assertRaisesRegex(ValueError, "extra"):
            self.load({**good, "note": "x"})
        for key in DE_KEYS:
            bad = copy.deepcopy(good)
            del bad["optimizer_options"][key]
            with self.assertRaisesRegex(ValueError, "optimizer_options"):
                self.load(bad)
        bad = copy.deepcopy(good)
        bad["optimizer_options"]["init"] = "latinhypercube"
        with self.assertRaisesRegex(ValueError, "optimizer_options"):
            self.load(bad)

    def test_settings_value_checks(self):
        good = template()
        cases = [
            ("seeds", list(range(9))), ("seeds", [1] * 10), ("seeds", [1.5] + list(range(9))),
            ("restarts", 0), ("restarts", True), ("workers", 0), ("workers", -2), ("parallel", 0),
            ("slurm", {"time": "1h", "cpus_per_task": 1}), ("slurm", {"time": "01:00:00"}),
            ("good_fit_margin", 0), ("reproduce_rtol", -1.0), ("reproduce_rtol", "1e-3"),
        ]
        for key, value in cases:
            with self.subTest(key=key, value=value), self.assertRaisesRegex(ValueError, key):
                self.load({**good, key: value})
        for key, value in (("maxiter", 0), ("popsize", 0), ("strategy", "nope"), ("polish", 0),
                           ("recombination", 1.5), ("mutation", 2.5), ("tol", -1)):
            bad = copy.deepcopy(good)
            bad["optimizer_options"][key] = value
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, key):
                self.load(bad)
        self.assertEqual(self.load({**good, "workers": -1})["workers"], -1)
        with self.assertRaisesRegex(ValueError, "duplicate"):
            self.path.write_text('{"restarts": 1, "restarts": 2}', encoding="utf-8")
            settings.load(self.path)


class JobFixture(OdeFixture):
    def setUp(self):
        super().setUp()
        import zipfile
        with zipfile.ZipFile(self.root / BUNDLE, "a") as zf:
            zf.writestr("model/__init__.py", "from .name2idx import C, V\n")
            zf.writestr("model/ode.py", "def param_values():\n    return [1, 1, 1, 1]\n")
            zf.writestr("model/name2idx/__init__.py", "from .parameters import C\nfrom .species import V\n")
            zf.writestr("model/__pycache__/ode.cpython-312.pyc", b"junk")
            zf.writestr("simulate_1/out.csv", "1,2\n")
        self.write_rows(ROWS + ["M7,pR,ctrl,60,min,,,9.9,a.u.,,,,fit,s1,Fig. 3"])
        data = self.complete()
        data["unmapped"] = [{"measurement_id": "M7", "reason": "readout outside the model scope"}]
        self.save(data)
        self.assertEqual(self.errors(), [])
        settings_path = self.root / SETTINGS
        settings_path.write_text(json.dumps(template(), indent=2), encoding="utf-8")
        self.approve()

    def approve(self, record_id="ode-map-1"):
        digest = sha((self.root / MAP).read_bytes())
        outputs = ["M1", "M2", "M3", "M4", "M5", "M6", "kf1", "kf2"]
        state.update(self.root, "record", {
            "id": record_id, "kind": "stage_approval", "stage": "ode_mapping", "session_id": SESSION,
            "outputs": outputs, "artifact": {"path": MAP, "sha256": digest},
            "approved_by": "researcher", "authorization": "researcher: ok"})
        return record_id

    def build(self, approval="ode-map-1"):
        return build.build(self.root, MAP, approval, SETTINGS)

    def members(self, job_dir):
        with tarfile.open(job_dir / "bundle.tar.gz") as tf:
            return {m.name: (m, tf.extractfile(m).read()) for m in tf.getmembers()}


class BuildTests(JobFixture):
    def test_build_refuses_unapproved_mapping(self):
        with self.assertRaisesRegex(ValueError, "not found"):
            self.build("missing-1")
        data = self.load()
        data["fit"][1]["upper"] = 2.0
        self.save(data)  # changed after approval
        self.assertEqual(self.errors(), [])
        with self.assertRaisesRegex(ValueError, "sha256"):
            self.build()
        self.assertFalse((self.root / "runs/orchestrator/p1/jobs").exists())

    def test_build_refuses_failing_mapping(self):
        data = self.load()
        data["unmapped"] = []
        self.save(data)
        self.approve("ode-map-2")
        with self.assertRaisesRegex(ValueError, "M7"):
            self.build("ode-map-2")

    def test_build_is_deterministic(self):
        first = self.build()
        bundle1 = (first / "bundle.tar.gz").read_bytes()
        self.assertTrue(re.fullmatch(r"job-[0-9a-f]{12}", first.name))
        self.assertEqual(first.parent.relative_to(self.root).as_posix(), "runs/orchestrator/p1/jobs")
        # Rebuilding the same inputs yields the same job (left untouched).
        self.assertEqual(self.build(), first)
        shutil.rmtree(first)
        second = self.build()
        self.assertEqual(second.name, first.name)
        self.assertEqual(sha((second / "bundle.tar.gz").read_bytes()), sha(bundle1))
        raw = (second / "bundle.tar.gz").read_bytes()
        self.assertEqual(raw[4:8], b"\0\0\0\0")  # gzip mtime 0
        for name, (member, _) in self.members(second).items():
            self.assertEqual((member.mtime, member.uid, member.gid, member.uname, member.gname), (0, 0, 0, "", ""))
        names = [m.name for m in tarfile.open(second / "bundle.tar.gz").getmembers()]
        self.assertEqual(names, sorted(names))
        info = json.loads((second / "build.json").read_text(encoding="utf-8"))
        self.assertEqual(info["bundle_sha256"], sha(raw))
        self.assertIn("built_at", info)
        self.assertIn("host", info)

    def test_job_id_depends_on_inputs(self):
        first = self.build()
        data = template()
        data["restarts"], data["seeds"] = 2, [5, 6]
        (self.root / SETTINGS).write_text(json.dumps(data), encoding="utf-8")
        second = self.build()
        self.assertNotEqual(first.name, second.name)
        job = json.loads((second / "job.json").read_text(encoding="utf-8"))
        self.assertEqual(job["settings"]["seeds"], [5, 6])
        expected = sha("\n".join([job["mapping"]["sha256"], "ode-map-1", settings.digest(job["settings"]),
                                  job["export_sha256"], job["framework_commit"]]).encode())
        self.assertEqual(second.name, "job-" + expected[:12])

    def test_settings_key_order_does_not_change_the_bundle(self):
        first = self.build()
        bundle = (first / "bundle.tar.gz").read_bytes()
        shutil.rmtree(first)
        data = template()
        reordered = {key: data[key] for key in reversed(list(data))}
        reordered["optimizer_options"] = dict(reversed(list(data["optimizer_options"].items())))
        (self.root / SETTINGS).write_text(json.dumps(reordered, indent=4), encoding="utf-8")
        second = self.build()
        self.assertEqual(second.name, first.name)
        self.assertEqual((second / "bundle.tar.gz").read_bytes(), bundle)

    def test_bundle_contents_and_manifest(self):
        job_dir = self.build()
        members = self.members(job_dir)
        prefix = job_dir.name + "/"
        self.assertTrue(all(name.startswith(prefix) for name in members))
        files = {name[len(prefix):]: data for name, (_, data) in members.items()}
        job = json.loads(files["job.json"])
        self.assertEqual(files["job.json"], (job_dir / "job.json").read_bytes())
        self.assertEqual(list(job), ["kind", "job_id", "mapping", "export_sha256", "settings", "framework_commit",
                                     "files"])
        self.assertEqual(job["kind"], "biomass-calibration")
        self.assertEqual(job["job_id"], job_dir.name)
        self.assertEqual(job["mapping"], {"path": MAP, "sha256": sha((self.root / MAP).read_bytes()),
                                          "approval": "ode-map-1"})
        self.assertEqual(job["export_sha256"], self.load()["inputs"]["model"]["sha256"])
        self.assertEqual(job["settings"], settings.load(self.root / SETTINGS))
        self.assertEqual(set(job["files"]), set(files) - {"job.json"})
        for rel, digest in job["files"].items():
            self.assertEqual(sha(files[rel]), digest, rel)
        for rel in ("run.py", "env/requirements.lock", "env/bootstrap.sh", "launch/local.sh", "launch/remote.sh",
                    "launch/slurm.sbatch", "README.txt", "calibration/compare.py", "calibration/spec.json",
                    "model/name2idx/parameters.py", "model/observable.py"):
            self.assertIn(rel, files)
        self.assertEqual(files["calibration/compare.py"], (RUNTIME / "compare.py").read_bytes())
        self.assertEqual(files["run.py"], (RUNTIME / "run.py").read_bytes())
        self.assertEqual(files["env/requirements.lock"], (REPO / "setup" / "biomass-calibration.lock").read_bytes())
        # Only the model package of the export: no saved runs, run_simulation.py or requirements.txt.
        self.assertNotIn("run_simulation.py", files)
        self.assertNotIn("requirements.txt", files)
        self.assertFalse([rel for rel in files if "__pycache__" in rel or rel.startswith("simulate_")])
        modes = {name[len(prefix):]: m.mode for name, (m, _) in members.items()}
        self.assertEqual(modes["env/bootstrap.sh"], 0o755)
        self.assertEqual(modes["run.py"], 0o755)
        self.assertEqual(modes["calibration/spec.json"], 0o644)

    def test_bundle_contains_only_mapping_rows(self):
        job_dir = self.build()
        files = {n.split("/", 1)[1]: d for n, (_, d) in self.members(job_dir).items()}
        spec = json.loads(files["calibration/spec.json"])
        used = {mid for rule in self.load()["rules"] for mid in rule["measurements"]}
        self.assertEqual(set(spec["measurements"]), used)
        self.assertNotIn("M7", json.dumps(spec))
        self.assertEqual(spec["measurements"]["M2"]["value"], 3.0)
        self.assertFalse([rel for rel in files if rel.startswith(("inputs/", "runs/")) or rel.endswith(".csv")])
        blob = b"".join(files.values())
        self.assertNotIn(b"%PDF", blob)
        self.assertNotIn(b"Fig. 3", blob)

    def test_bundle_refuses_unsafe_export_members(self):
        import zipfile
        bundle = self.root / self.load()["inputs"]["model"]["bundle"]
        with zipfile.ZipFile(bundle, "a") as zf:
            zf.writestr("model/../evil.py", "x = 1\n")
        data = self.load()
        data["inputs"]["model"]["sha256"] = sha(bundle.read_bytes())
        self.save(data)
        self.approve("ode-map-3")
        with self.assertRaisesRegex(ValueError, "evil"):
            self.build("ode-map-3")

    def test_cli_build(self):
        out = io.StringIO()
        with mock.patch.dict("os.environ", {"CLAUDE_PROJECT_DIR": str(self.root)}), \
                mock.patch("sys.stdout", out):
            code = build.main(["--mapping", MAP, "--approval", "ode-map-1", "--settings", SETTINGS])
        self.assertEqual(code, 0)
        self.assertIn("job-", out.getvalue())


class FrameworkCommitTests(unittest.TestCase):
    def git(self, *args):
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.org", "-C", str(self.repo), *args],
                       check=True, capture_output=True)

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = Path(tmp.name)
        for rel in ("scripts/jobs/build.py", "scripts/ode/x.py", "setup/biomass-calibration.lock", "docs/a.md"):
            (self.repo / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.repo / rel).write_text("v1\n")
        self.git("init", "-q")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "init")
        self.head = subprocess.run(["git", "-C", str(self.repo), "rev-parse", "HEAD"], capture_output=True,
                                   text=True, check=True).stdout.strip()

    def test_dirty_marker_only_for_runtime_paths(self):
        self.assertEqual(build.framework_commit(self.repo), self.head)
        (self.repo / "docs/a.md").write_text("v2\n")
        self.assertEqual(build.framework_commit(self.repo), self.head)
        for rel in ("scripts/jobs/build.py", "setup/biomass-calibration.lock"):
            (self.repo / rel).write_text("v2\n")
            self.assertEqual(build.framework_commit(self.repo), self.head + "-dirty", rel)
            self.git("checkout", "--", rel)
        (self.repo / "scripts/ode/new.py").write_text("x\n")  # untracked counts too
        self.assertEqual(build.framework_commit(self.repo), self.head + "-dirty")

    def test_no_git_is_unknown(self):
        with tempfile.TemporaryDirectory() as other:
            self.assertEqual(build.framework_commit(Path(other)), "unknown")


class RuntimeConcurrencyTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def test_concurrent_status_writes_never_raise(self):
        import threading
        target = self.root / "out" / "status.json"
        errors = []

        def writer(k):
            try:
                for i in range(200):
                    run.write_json(target, {"writer": k, "i": i, "pad": "x" * 2000})
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=writer, args=(k,)) for k in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(json.loads(target.read_text())["i"], 199)
        self.assertEqual([p.name for p in target.parent.iterdir()], ["status.json"])  # no temp files left

    def job(self, restarts=2):
        (self.root / "job.json").write_text(json.dumps({"job_id": "job-000000000000", "settings": {
            "restarts": restarts, "seeds": [1, 2], "optimizer_options": {}, "workers": 1}}))

    def test_calibrate_status_writes_are_best_effort(self):
        self.job()
        done = {"restart": 1, "seed": 1, "score": 1.0, "status": "ok", "params": {}, "wall_time_s": 0.0}
        with mock.patch.object(run, "status", side_effect=FileNotFoundError("raced")), \
                mock.patch.object(run, "run_restart", return_value=done) as restart, \
                mock.patch("sys.stdout", io.StringIO()):
            self.assertEqual(run.cmd_calibrate(self.root, 1), 0)
        restart.assert_called_once()
        owner = json.loads((self.root / "out" / "restart-1" / "owner.json").read_text())
        self.assertEqual(owner["pid"], os.getpid())

    def test_workers_minus_one_uses_the_slurm_allocation(self):
        (self.root / "job.json").write_text(json.dumps({"job_id": "job-000000000000", "settings": {
            "restarts": 2, "seeds": [1, 2], "optimizer_options": {}, "workers": -1}}))
        done = {"restart": 1, "seed": 1, "score": 1.0, "status": "ok", "params": {}, "wall_time_s": 0.0}
        for env, expected in (({"SLURM_CPUS_PER_TASK": "6"}, 6), ({}, -1), ({"SLURM_CPUS_PER_TASK": "x"}, -1)):
            shutil.rmtree(self.root / "out", ignore_errors=True)
            with mock.patch.dict(os.environ, env, clear=False), mock.patch.object(run, "status"), \
                    mock.patch.object(run, "run_restart", return_value=done) as restart, \
                    mock.patch("sys.stdout", io.StringIO()):
                if not env:
                    os.environ.pop("SLURM_CPUS_PER_TASK", None)
                self.assertEqual(run.cmd_calibrate(self.root, 1), 0)
            self.assertEqual(restart.call_args.args[4], expected, env)

    def test_selftest_cap_is_hard(self):
        self.job()

        def stubborn(*args, **kwargs):  # swallows ordinary exceptions, as Problem.__call__ does
            give_up = time.monotonic() + 4
            while time.monotonic() < give_up:
                try:
                    time.sleep(0.05)
                except Exception:  # noqa: BLE001
                    pass
            return {"restart": 0, "seed": 1, "score": 1.0, "status": "ok", "params": {}, "wall_time_s": 4.0}

        with mock.patch.object(run, "SELFTEST_CAP_S", 1), mock.patch.object(run, "run_restart", stubborn), \
                mock.patch("sys.stdout", io.StringIO()):
            started = time.monotonic()
            self.assertEqual(run.cmd_selftest(self.root), 1)
        self.assertLess(time.monotonic() - started, 3)
        report = json.loads((self.root / "out" / "selftest" / "selftest.json").read_text())
        self.assertEqual(report["status"], "failed")
        self.assertIn("cap", report["error"])
        self.assertTrue(issubclass(run.SelftestTimeout, BaseException))
        self.assertFalse(issubclass(run.SelftestTimeout, Exception))


FAKE_MODEL = {
    "model/__init__.py": "",
    "model/ode.py": "def param_values():\n    return [1.0, 3.0]\n\n\ndef initial_values():\n    return [2.0]\n",
    "model/name2idx/__init__.py": "class C:\n    NAMES = ['k1', 'k2']\n\n\nclass V:\n    NAMES = ['S']\n",
    "model/observable.py": "class Observable:\n    def __init__(self):\n        self.obs_names = ['obs']\n",
}


class RuntimeReviewTests(unittest.TestCase):
    """PR 16 review (batch A): interrupts, live restart folders, the per-process cache, hold-out isolation,
    worker/parallel rule and the selftest worker count."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def job(self, **settings):
        base = {"restarts": 2, "seeds": [1, 2], "optimizer_options": {"maxiter": 1, "popsize": 1}, "workers": 1,
                "parallel": 4}
        (self.root / "job.json").write_text(json.dumps({"job_id": "job-000000000000",
                                                        "settings": {**base, **settings}}))

    def spec(self):
        try:
            from tests.data.test_jobs_compare import mapping, row, tc
        except ImportError:  # discovered with -s tests/data
            from test_jobs_compare import mapping, row, tc
        from scripts.jobs.runtime import compare
        rows = [row("m1", "ctrl", 10.0, 2.0), row("h1", "drug", 20.0, 5.0, use="holdout"),
                row("h2", "ctrl", 30.0, 7.0, use="holdout")]
        return compare.resolve(mapping([tc("absolute", ["m1", "h1", "h2"])]), rows)

    def fake_numpy(self):
        import types
        numpy, scipy, optimize = (types.ModuleType(n) for n in ("numpy", "scipy", "scipy.optimize"))
        optimize.differential_evolution = mock.Mock(side_effect=AssertionError("not reached"))
        scipy.optimize = optimize
        patcher = mock.patch.dict(sys.modules, {"numpy": numpy, "scipy": scipy, "scipy.optimize": optimize})
        patcher.start()
        self.addCleanup(patcher.stop)

    # run.py:384
    def test_interrupt_leaves_no_failed_result_and_the_rerun_resumes(self):
        self.fake_numpy()
        out = self.root / "out" / "restart-1"
        for exc in (KeyboardInterrupt(), SystemExit(1)):
            with mock.patch.object(run, "Problem", side_effect=exc):
                with self.assertRaises(type(exc)):
                    run.run_restart(self.root, 1, 1, {}, 1, out)
            self.assertFalse((out / "result.json").exists(), exc)
            self.assertIn("interrupted", (out / "log.txt").read_text())
        with mock.patch.object(run, "Problem", side_effect=run.SelftestTimeout("cap")):
            with self.assertRaises(run.SelftestTimeout):
                run.run_restart(self.root, 1, 1, {}, 1, out)
        self.assertEqual(json.loads((out / "result.json").read_text())["status"], "failed")
        (out / "result.json").unlink()
        with mock.patch.object(run, "Problem", side_effect=RuntimeError("boom")):
            self.assertEqual(run.run_restart(self.root, 1, 1, {}, 1, out)["status"], "failed")
        self.assertEqual(json.loads((out / "result.json").read_text())["status"], "failed")

    # run.py:424
    def calibrate(self, env=None, **owner):
        folder = self.root / "out" / "restart-1"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "trace.csv").write_text("live\n")
        (folder / "owner.json").write_text(json.dumps({"host": socket.gethostname(), "pid": os.getpid(),
                                                       "started_at": "t", **owner}))
        done = {"restart": 1, "seed": 1, "score": 1.0, "status": "ok", "params": {}, "wall_time_s": 0.0}
        err = io.StringIO()
        with mock.patch.object(run, "status"), mock.patch.object(run, "run_restart", return_value=done) as restart, \
                mock.patch.dict(os.environ, env or {}), mock.patch("sys.stdout", io.StringIO()), \
                mock.patch("sys.stderr", err):
            code = run.cmd_calibrate(self.root, 1)
        return code, restart.called, (folder / "trace.csv").exists(), err.getvalue()

    def test_calibrate_never_deletes_a_restart_folder_whose_owner_is_alive(self):
        self.job()
        code, started, kept, err = self.calibrate()  # owner = this live process on this host
        self.assertEqual((code, started, kept), (3, False, True))
        self.assertIn("running", err)
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        code, started, kept, _ = self.calibrate(pid=dead.pid)  # stale: removed and run again
        self.assertEqual((code, started, kept), (0, True, False))
        code, started, kept, _ = self.calibrate(host="node-9", pid=1)  # another host, nothing to ask: kept
        self.assertEqual((code, started, kept), (3, False, True))

    def test_calibrate_asks_slurm_about_an_owner_on_another_node(self):
        self.job()
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        sacct = bin_dir / "sacct"
        sacct.write_text("#!/bin/sh\nprintf '%s' \"$FAKE_SACCT\"\n")
        sacct.chmod(0o755)
        path = {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}
        owner = {"host": "node-9", "pid": 1, "slurm_job": "4242", "slurm_task": "1"}
        code, started, kept, _ = self.calibrate({**path, "FAKE_SACCT": "4242_1|RUNNING\n"}, **owner)
        self.assertEqual((code, started, kept), (3, False, True))
        code, started, kept, _ = self.calibrate({**path, "FAKE_SACCT": "4242_1|CANCELLED by 1\n"}, **owner)
        self.assertEqual((code, started, kept), (0, True, False))
        # a requeued task finds its own earlier attempt: that is not a live owner
        own = {**path, "FAKE_SACCT": "4242_1|RUNNING\n", "SLURM_ARRAY_JOB_ID": "4242", "SLURM_ARRAY_TASK_ID": "1"}
        code, started, kept, _ = self.calibrate(own, **owner)
        self.assertEqual((code, started, kept), (0, True, False))

    def test_owner_records_the_slurm_task(self):
        self.job()
        done = {"restart": 1, "seed": 1, "score": 1.0, "status": "ok", "params": {}, "wall_time_s": 0.0}
        with mock.patch.object(run, "status"), mock.patch.object(run, "run_restart", return_value=done), \
                mock.patch.dict(os.environ, {"SLURM_ARRAY_JOB_ID": "77", "SLURM_ARRAY_TASK_ID": "1"}), \
                mock.patch("sys.stdout", io.StringIO()):
            run.cmd_calibrate(self.root, 1)
        owner = json.loads((self.root / "out" / "restart-1" / "owner.json").read_text())
        self.assertEqual((owner["slurm_job"], owner["slurm_task"]), ("77", "1"))

    # run.py:193
    def bundle(self):
        for rel, text in FAKE_MODEL.items():
            (self.root / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.root / rel).write_text(text)
        (self.root / "calibration").mkdir()
        shutil.copyfile(RUNTIME / "compare.py", self.root / "calibration" / "compare.py")
        spec = self.spec()
        spec["fit"] = [{"name": "k1", "kind": "parameter", "lower": 0.1, "upper": 10.0, "log10_lower": -1.0,
                        "log10_upper": 1.0}]
        (self.root / "calibration" / "spec.json").write_text(json.dumps(spec))

        def forget():
            for name in [m for m in sys.modules if m == "model" or m.startswith("model.")]:
                del sys.modules[name]
            if str(self.root) in sys.path:
                sys.path.remove(str(self.root))
        forget()
        self.addCleanup(forget)

    def test_problem_state_is_loaded_once_per_process(self):
        import pickle
        self.fake_numpy()
        self.bundle()
        with mock.patch.object(run, "load_compare", wraps=run.load_compare) as loads:
            first = run.Problem(self.root)
            self.assertEqual(first.nominal(), {"k1": 1.0})
            second = pickle.loads(pickle.dumps(first))  # what a scipy pool worker receives per chunk
            self.assertEqual(second.nominal(), {"k1": 1.0})
            self.assertEqual(run.Problem(self.root).nominal(), {"k1": 1.0})
        self.assertEqual(loads.call_count, 1)

    # run.py:593, compare.py:197, compare.py:93
    def fake_problem(self, spec):
        from scripts.jobs.runtime import compare
        simulated = []

        class Fake(run.Problem):
            def _load(self):
                self.spec, self.compare, self.fit, self.names = spec, compare, spec["fit"], list(spec["times_model"])
                self._ready = True

            def simulate(self, name, fitted):
                simulated.append(name)
                return {"obs": {t: 1.0 + t for t in run.model_times(spec, name)}}
        return Fake, simulated

    def test_fit_evaluation_simulates_only_what_fit_rows_need(self):
        Fake, simulated = self.fake_problem(self.spec())
        score, predictions, sims = Fake(self.root).evaluate({"k1": 1.0}, "fit")
        self.assertEqual(simulated, ["ctrl"])
        self.assertEqual(score, (11.0 - 2.0) ** 2)
        simulated.clear()
        Fake(self.root).evaluate({"k1": 1.0}, "all")
        self.assertEqual(sorted(simulated), ["ctrl", "drug"])

    def test_collect_shows_no_hold_out_prediction(self):
        self.job()
        spec = self.spec()
        Fake, _ = self.fake_problem(spec)
        folder = self.root / "out" / "restart-1"
        folder.mkdir(parents=True)
        (folder / "result.json").write_text(json.dumps({"restart": 1, "seed": 1, "status": "ok", "score": 1.0,
                                                        "params": {"k1": 1.0}, "wall_time_s": 0.0}))
        shown = {}

        def figures(folder_, rows, results, spec_, traces):
            shown.update(rows)
            return []
        with mock.patch.object(run, "Problem", Fake), mock.patch.object(run, "_figures", figures), \
                mock.patch("sys.stdout", io.StringIO()):
            self.assertEqual(run.cmd_collect(self.root), 0)
        stage = self.root / "out" / "collect"
        predictions = json.loads((stage / "predictions.json").read_text())
        self.assertEqual(sorted(predictions["rows"]), ["m1"])
        self.assertEqual(sorted(shown), ["m1"])
        sims = json.loads((stage / "simulations.json").read_text())["conditions"]
        self.assertEqual(sims, {"ctrl": {"obs": [[0.0, 1.0], [10.0, 11.0]]}})  # not drug, not ctrl at t=30

    def test_validate_reports_only_rows_of_the_requested_use(self):
        Fake, _ = self.fake_problem(self.spec())
        with mock.patch.object(run, "Problem", Fake):
            self.assertEqual(sorted(run.validate(self.root, {"k1": 1.0}, "fit")["predictions"]), ["m1"])
            self.assertEqual(sorted(run.validate(self.root, {"k1": 1.0}, "holdout")["predictions"]), ["h1", "h2"])
            self.assertEqual(sorted(run.validate(self.root, {"k1": 1.0}, "all")["predictions"]), ["h1", "h2", "m1"])

    # launch.py:294 + run.py:525
    def test_parallel_is_one_whenever_a_restart_uses_more_than_one_worker(self):
        self.assertEqual(run.effective_parallel({"workers": 1, "parallel": 4}), 4)
        self.assertEqual(run.effective_parallel({"workers": 1, "parallel": 4}, 2), 2)
        for workers in (-1, 2, 8):
            self.assertEqual(run.effective_parallel({"workers": workers, "parallel": 4}, 3), 1, workers)
        with mock.patch.dict(os.environ, {"SLURM_CPUS_PER_TASK": "1"}):  # -1 under a 1-cpu allocation is 1 worker
            self.assertEqual(run.effective_parallel({"workers": -1, "parallel": 4}), 4)

    def test_selftest_runs_one_worker(self):
        self.job(workers=-1)
        done = {"restart": 0, "seed": 1, "score": 1.0, "status": "ok", "params": {}, "wall_time_s": 0.0}
        with mock.patch.object(run, "run_restart", return_value=done) as restart, \
                mock.patch.object(run, "validate", return_value={"score": 1.0}), mock.patch("sys.stdout", io.StringIO()):
            self.assertEqual(run.cmd_selftest(self.root), 0)
        self.assertEqual(restart.call_args.args[4], 1)

    def test_local_sh_applies_the_parallel_rule(self):
        self.job(workers=-1, restarts=0)
        shutil.copytree(RUNTIME, self.root / "launch", ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copyfile(RUNTIME / "run.py", self.root / "run.py")
        (self.root / "env").mkdir()
        (self.root / "env" / "bootstrap.sh").write_text("exit 0\n")
        (self.root / ".venv" / "bin").mkdir(parents=True)
        (self.root / ".venv" / "bin" / "python").symlink_to(sys.executable)
        (self.root / "launch" / "local.sh").chmod(0o755)
        with mock.patch.dict(os.environ, {"MPLCONFIGDIR": str(self.root / "mpl")}):
            os.environ.pop("SLURM_CPUS_PER_TASK", None)
            done = subprocess.run(["bash", str(self.root / "launch" / "local.sh"), "--parallel", "4"],
                                  capture_output=True, text=True, timeout=60)
        self.assertIn("1 at a time", done.stdout, done.stdout + done.stderr)


class RuntimeFileTests(unittest.TestCase):
    def test_bootstrap_script_pins(self):
        path = RUNTIME / "bootstrap.sh"
        subprocess.run(["bash", "-n", str(path)], check=True)
        text = path.read_text(encoding="utf-8")
        self.assertIn("UV_VERSION=0.13.4", text)
        for triple in ("x86_64-unknown-linux-gnu", "aarch64-unknown-linux-gnu", "x86_64-apple-darwin",
                       "aarch64-apple-darwin"):
            self.assertIn(triple, text)
        self.assertEqual(len(set(re.findall(r"\b[0-9a-f]{64}\b", text))), 4)
        self.assertIn("--require-hashes", text)
        self.assertIn("selftest", text)
        self.assertIn("env_report", text)
        # Every uv write stays in the job folder: no versioned python in ~/.local/bin, no ~/.local/share/uv.
        self.assertRegex(text, r'"\$UV" python install --no-bin ')
        for var in ("UV_PYTHON_BIN_DIR", "UV_PYTHON_INSTALL_DIR", "UV_CACHE_DIR", "UV_TOOL_DIR", "UV_TOOL_BIN_DIR",
                    "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_BIN_HOME"):
            self.assertRegex(text, rf'export {var}="\$\{{?JOB_DIR', var)

    def test_launch_scripts_parse(self):
        for name in ("local.sh", "remote.sh", "slurm.sbatch"):
            subprocess.run(["bash", "-n", str(RUNTIME / name)], check=True)

    def test_lock_pins_biomass(self):
        text = (REPO / "setup" / "biomass-calibration.lock").read_text(encoding="utf-8")
        self.assertIn("biomass==0.14.0", text)
        self.assertIn("--hash=sha256:", text)
        for line in text.splitlines():
            if line and not line.startswith((" ", "#")):
                self.assertRegex(line, r"^[A-Za-z0-9_.-]+==[^ ]+( ;.*)? \\$", line)

    def test_runner_imports_without_numpy(self):
        # run.py is imported by the repository tests and launch.py with the stdlib only.
        source = (RUNTIME / "run.py").read_text(encoding="utf-8")
        top = [line for line in source.splitlines() if line.startswith(("import ", "from "))]
        self.assertFalse([line for line in top if re.search(r"numpy|scipy|biomass|matplotlib", line)], top)

    def test_runner_times_exact(self):
        spec = {"times_model": {"ctrl": [0.0, 7.5, 90.0], "dose": [0.0, 30.0]}}
        self.assertEqual(run.model_times(spec, "ctrl"), [0.0, 7.5, 90.0])
        self.assertTrue(all(type(t) is float for t in run.model_times(spec, "ctrl")))
        spec = json.loads(json.dumps({"times_model": {"c": [90.0, 0.0, 7.5]}}))
        self.assertEqual(run.model_times(spec, "c"), [0.0, 7.5, 90.0])
        self.assertEqual(run.model_times({"times_model": {"c": [30]}}, "c"), [0.0, 30.0])

    def test_gene_to_value_matches_biomass(self):
        fit = [{"name": "k", "log10_lower": -3.0, "log10_upper": 1.0}]
        self.assertAlmostEqual(run.gene2val(fit, [0.0])["k"], 1e-3)
        self.assertAlmostEqual(run.gene2val(fit, [1.0])["k"], 10.0)
        self.assertAlmostEqual(run.gene2val(fit, [0.5])["k"], 0.1)


class LaunchTests(JobFixture):
    def setUp(self):
        super().setUp()
        self.job_dir = self.build()
        self.job_id = self.job_dir.name
        self.cache = self.root / "cache"
        env = mock.patch.dict(os.environ, {"XDG_CACHE_HOME": str(self.cache)})
        env.start()
        self.addCleanup(env.stop)

    def fake_results(self, job_sha=None):
        cache_job = launch.unpack(self.root, self.job_id)
        self.assertEqual(cache_job, self.cache / "agentic-jobs" / self.job_id)
        payload = json.dumps({"job_sha256": job_sha or sha((self.job_dir / "job.json").read_bytes()),
                              "partial": False, "restarts": [], "files": {}}).encode()
        with tarfile.open(cache_job / "results.tar.gz", "w:gz") as tf:
            info = tarfile.TarInfo("results.json")
            info.size = len(payload)
            tf.addfile(info, io.BytesIO(payload))
        return cache_job

    def test_unpack_verifies_manifest(self):
        cache_job = launch.unpack(self.root, self.job_id)
        self.assertTrue((cache_job / "run.py").is_file())
        self.assertTrue(os.access(cache_job / "env" / "bootstrap.sh", os.X_OK))
        self.assertEqual(launch.unpack(self.root, self.job_id), cache_job)  # reused
        (cache_job / "run.py").write_text("tampered\n")
        with self.assertRaisesRegex(ValueError, "run.py"):
            launch.unpack(self.root, self.job_id)

    def test_unknown_job_refused(self):
        with self.assertRaisesRegex(ValueError, "job id"):
            launch.find_job(self.root, "../x")
        with self.assertRaisesRegex(ValueError, "no job"):
            launch.find_job(self.root, "job-000000000000")

    def test_fetch_refuses_overwrite(self):
        self.fake_results()
        dest = launch.fetch(self.root, self.job_id)
        self.assertEqual(dest.relative_to(self.root).as_posix(),
                         f"inputs/calibration-results/{self.job_id}/results.tar.gz")
        before = dest.read_bytes()
        with self.assertRaisesRegex(ValueError, "overwrite"):
            launch.fetch(self.root, self.job_id)
        self.assertEqual(dest.read_bytes(), before)

    def test_fetch_refuses_results_of_another_job(self):
        self.fake_results(job_sha="0" * 64)
        with self.assertRaisesRegex(ValueError, "job.json"):
            launch.fetch(self.root, self.job_id)
        self.assertFalse((self.root / "inputs" / "calibration-results").exists())

    def test_fetch_without_results(self):
        launch.unpack(self.root, self.job_id)
        with self.assertRaisesRegex(ValueError, "results.tar.gz"):
            launch.fetch(self.root, self.job_id)

    def test_status_counts_restarts(self):
        cache_job = launch.unpack(self.root, self.job_id)
        for n, status in ((1, "ok"), (2, "failed")):
            out = cache_job / "out" / f"restart-{n}"
            out.mkdir(parents=True)
            (out / "result.json").write_text(json.dumps({"restart": n, "status": status}))
        info = launch.status(self.root, self.job_id)
        self.assertEqual((info["restarts"], info["ok"], info["failed"]), (10, 1, 1))
        self.assertEqual(info["pending"], list(range(3, 11)))
        self.assertEqual((info["running"], info["stale"]), ([], []))
        self.assertFalse(info["launcher_running"])
        self.assertFalse(info["results"])

    def test_status_reports_stale_restarts(self):
        cache_job = launch.unpack(self.root, self.job_id)
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        owners = {3: {"host": socket.gethostname(), "pid": os.getpid()},   # alive here
                  4: {"host": socket.gethostname(), "pid": dead.pid},      # gone
                  5: None,                                                 # no owner recorded
                  6: {"host": "compute-node-17", "pid": 1}}               # another host (SLURM): cannot check
        for n, owner in owners.items():
            folder = cache_job / "out" / f"restart-{n}"
            folder.mkdir(parents=True)
            if owner:
                (folder / "owner.json").write_text(json.dumps(owner))
        info = run.status(cache_job, write=False)
        self.assertEqual(info["running"], [3, 6])
        self.assertEqual(info["stale"], [4, 5])
        self.assertEqual(info["pending"], [1, 2, 7, 8, 9, 10])

    def test_validate_refused_while_start_bootstraps(self):
        cache_job = launch.unpack(self.root, self.job_id)
        (cache_job / "launch.pid").write_text(f"{os.getpid()}\n")  # a live launcher, environment not ready
        with mock.patch.object(launch.subprocess, "run") as called:
            with self.assertRaisesRegex(ValueError, "running"):
                launch.validate(self.root, self.job_id, None, "fit", None)
            called.assert_not_called()
        (cache_job / "env_report.json").write_text(json.dumps({"status": "ok"}))
        with mock.patch.object(launch.subprocess, "run") as called:
            called.return_value.returncode = 0
            self.assertEqual(launch.validate(self.root, self.job_id, None, "fit", None), 0)


if __name__ == "__main__":
    unittest.main()
