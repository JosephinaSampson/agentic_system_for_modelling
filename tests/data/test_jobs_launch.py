"""SSH (printed and automatic) and SLURM launchers; remote.sh. Fake ssh/scp/sbatch/sacct/scancel on PATH."""
import contextlib
import io
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

try:
    from tests.data.test_jobs_build import RUNTIME, JobFixture, sha
except ImportError:  # discovered with -s tests/data
    from test_jobs_build import RUNTIME, JobFixture, sha

from scripts.jobs import launch

def kill_group_at_cleanup(testcase, pgid):
    """Last-resort cleanup: SIGKILL the process group even when the test fails or is interrupted."""
    def kill():
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pgid, signal.SIGKILL)
    testcase.addCleanup(kill)


# A harmless stand-in for a running supervisor: `ps -o args=` shows "... remote.sh _supervise".
SUPERVISOR_LIKE = [sys.executable, "-c", "import time; time.sleep(300)", "remote.sh", "_supervise"]

FAKE = """#!/usr/bin/env python3
import json, os, shutil, sys
name = os.path.basename(sys.argv[0])
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write(json.dumps([name] + sys.argv[1:]) + "\\n")
if name == "scp" and os.environ.get("FAKE_SCP_PAYLOAD") and not os.environ.get("FAKE_FAIL"):
    shutil.copyfile(os.environ["FAKE_SCP_PAYLOAD"], sys.argv[-1])
if name == "sbatch":
    print("Submitted batch job 4242")
if name == "sacct":
    sys.stdout.write(os.environ.get("FAKE_SACCT", ""))
sys.exit(1 if os.environ.get("FAKE_FAIL") and name in os.environ["FAKE_FAIL"].split(",") else 0)
"""


class FakeTools:
    """A temp dir with fake executables on PATH, recording argv to a log."""

    def make(self, testcase, names=("ssh", "scp")):
        self.tmp = Path(tempfile.mkdtemp(prefix="launch-test-", dir=os.environ.get("TMPDIR") or None))
        testcase.addCleanup(shutil.rmtree, self.tmp, True)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        for name in names:
            self.make_tool(name)
        self.log = self.tmp / "calls.log"
        self.log.touch()
        env = {"PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}", "FAKE_LOG": str(self.log),
               "XDG_CONFIG_HOME": str(self.tmp / "config"), "XDG_CACHE_HOME": str(self.tmp / "cache")}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        testcase.addCleanup(patcher.stop)
        return self

    def make_tool(self, name):
        path = self.bin / name
        path.write_text(FAKE, encoding="utf-8")
        path.chmod(0o755)

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def hosts(self, data):
        path = self.tmp / "config" / "agentic-modelling" / "hosts.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")


HOST = {"hosts": {"lab": {"ssh": "labpc", "remote_dir": "~/jobs", "max_cores": 8}}}


class LauncherFixture(JobFixture):
    names = ("ssh", "scp")

    def setUp(self):
        super().setUp()
        self.job_dir = self.build()
        self.job_id = self.job_dir.name
        self.fake = FakeTools().make(self, self.names)
        env = mock.patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": str(self.root)})
        env.start()
        self.addCleanup(env.stop)

    def run_main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = launch.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def job_settings(self):
        return json.loads((self.job_dir / "job.json").read_text())["settings"]

    def dispatch(self):
        path = self.job_dir / "dispatch.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


class HostsTests(LauncherFixture):
    def test_unknown_host_key_refused(self):
        self.fake.hosts({"hosts": {"lab": {"ssh": "labpc", "remote_dir": "~/jobs", "max_cores": 8, "password": "x"}}})
        code, _, err = self.run_main("print", self.job_id, "--host", "lab")
        self.assertEqual(code, 1)
        self.assertIn("password", err)
        self.fake.hosts({"hosts": {"lab": {"ssh": "labpc"}}, "extra": 1})
        self.assertEqual(self.run_main("print", self.job_id, "--host", "lab")[0], 1)
        self.fake.hosts('{"hosts": {"lab": {"ssh": "a", "ssh": "b", "remote_dir": "~/j", "max_cores": 2}}}')
        self.assertEqual(self.run_main("print", self.job_id, "--host", "lab")[0], 1)

    def test_bad_values_refused(self):
        for bad in ({"ssh": "-oProxyCommand=x", "remote_dir": "~/j", "max_cores": 2},
                    {"ssh": "a b", "remote_dir": "~/j", "max_cores": 2},
                    {"ssh": "a", "remote_dir": "~/j; rm -rf /", "max_cores": 2},
                    {"ssh": "a", "remote_dir": "~/j", "max_cores": 0}):
            self.fake.hosts({"hosts": {"lab": bad}})
            self.assertEqual(self.run_main("print", self.job_id, "--host", "lab")[0], 1, bad)

    def test_cores_cap_parallel(self):
        s = self.job_settings()
        self.assertEqual(launch.cores_parallel(s, 10 ** 6), 1 if s["workers"] == -1 else
                         max(1, min(s["parallel"], 10 ** 6 // s["workers"])))
        self.assertEqual(launch.cores_parallel({"parallel": 8, "workers": 4}, 8), 2)
        self.assertEqual(launch.cores_parallel({"parallel": 8, "workers": 4}, 2), 1)
        self.assertEqual(launch.cores_parallel({"parallel": 8, "workers": 1}, 2), 2)
        self.assertEqual(launch.cores_parallel({"parallel": 3, "workers": 1}, 64), 3)
        # workers -1 lets scipy use every core of the machine: one restart at a time
        self.assertEqual(launch.cores_parallel({"parallel": 3, "workers": -1}, 64), 1)


class ParallelRuleTests(LauncherFixture):
    """R-A3: K = min(parallel, max_cores // workers) for workers >= 1, K = 1 for workers -1; local mode uses
    os.cpu_count(); an unconfigured (printed) host has unknown cores: K = 1 unless workers is 1."""

    def set_workers(self, workers):
        job = json.loads((self.job_dir / "job.json").read_text())
        job["settings"]["workers"] = workers
        return job["settings"]

    def test_unconfigured_host_and_print_use_the_rule(self):
        for workers in (-1, 2):
            settings = self.set_workers(workers)
            self.assertEqual(launch.resolve_host("somebox", settings, configured_only=False)["parallel"], 1)
        settings = self.set_workers(1)
        self.assertEqual(launch.resolve_host("somebox", settings, configured_only=False)["parallel"],
                         settings["parallel"])

    def start_command(self, workers, requested, cores):
        settings = self.set_workers(workers)
        with mock.patch.object(launch, "_job_and_settings", return_value=(self.job_dir, settings)), \
                mock.patch.object(launch, "unpack", return_value=self.fake.tmp), \
                mock.patch.object(launch.os, "cpu_count", return_value=cores), \
                mock.patch.object(launch.subprocess, "Popen") as popen:
            popen.return_value.pid = 12345
            launch.start(self.root, self.job_id, requested)
        return popen.call_args.args[0][-1]

    def test_local_start_passes_the_effective_parallel(self):
        self.assertIn("--parallel 1", self.start_command(-1, 4, 16))
        self.assertIn("--parallel 4", self.start_command(2, 4, 16))
        self.assertIn("--parallel 2", self.start_command(2, 4, 4))
        self.assertIn("--parallel 3", self.start_command(1, 3, 16))


class SlurmReviewTests(LauncherFixture):
    names = ("ssh", "scp", "sbatch", "sacct")

    def setUp(self):
        super().setUp()
        p = mock.patch.object(launch, "slurm_setup")
        p.start()
        self.addCleanup(p.stop)
        self.record = launch.unpack(self.root, self.job_id) / "state" / "slurm_job"

    def test_empty_slurm_job_record_is_a_clear_error(self):
        # review launch.py:448: an empty state/slurm_job (an interrupted earlier submit) crashed with IndexError
        self.record.parent.mkdir(parents=True)
        self.record.write_text("")
        code, _, err = self.run_main("slurm", "submit", self.job_id)
        self.assertEqual(code, 1, err)
        self.assertIn("already submitted", err)

    def test_record_is_written_atomically(self):
        self.assertEqual(self.run_main("slurm", "submit", self.job_id)[0], 0)
        self.assertEqual(self.record.read_text(), "4242\n")
        self.assertEqual(sorted(p.name for p in self.record.parent.iterdir()), ["slurm_job"])

    def test_resubmit_refused_while_the_earlier_array_is_active(self):
        # review run.py:424: a resubmit while the first array runs would start the same seeds twice
        self.assertEqual(self.run_main("slurm", "submit", self.job_id)[0], 0)
        with mock.patch.dict(os.environ, {"FAKE_SACCT": "4242_1|RUNNING\n4242_[2-10]|PENDING\n"}):
            code, _, err = self.run_main("slurm", "submit", self.job_id, "--resubmit")
        self.assertEqual(code, 1)
        self.assertIn("4242", err)
        self.assertIn("active", err)
        with mock.patch.dict(os.environ, {"FAKE_SACCT": ""}):  # sacct cannot tell: refused too
            self.assertEqual(self.run_main("slurm", "submit", self.job_id, "--resubmit")[0], 1)
        with mock.patch.dict(os.environ, {"FAKE_SACCT": "4242_1|COMPLETED\n4242_2|CANCELLED by 1\n"}):
            code, _, err = self.run_main("slurm", "submit", self.job_id, "--resubmit")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.record.read_text().split(), ["4242", "4242"])


    def test_resubmit_force_when_sacct_cannot_tell(self):
        # review 2 launch.py:454: on a cluster without accounting the documented recovery could never work
        self.assertEqual(self.run_main("slurm", "submit", self.job_id)[0], 0)
        with mock.patch.dict(os.environ, {"FAKE_SACCT": ""}):
            code, _, err = self.run_main("slurm", "submit", self.job_id, "--resubmit")
            self.assertEqual(code, 1)
            self.assertIn("--force", err)
            self.assertIn("squeue", err)
            code, _, err = self.run_main("slurm", "submit", self.job_id, "--resubmit", "--force")
            self.assertEqual(code, 0, err)
            self.assertIn("--force", err)  # the override is reported
        self.assertEqual(self.record.read_text().split(), ["4242", "4242"])
        with mock.patch.dict(os.environ, {"FAKE_SACCT": "4242_1|RUNNING\n"}):  # an active array: never forced
            code, _, err = self.run_main("slurm", "submit", self.job_id, "--resubmit", "--force")
        self.assertEqual(code, 1)
        self.assertIn("active", err)
        code, _, err = self.run_main("slurm", "submit", self.job_id, "--force")
        self.assertEqual(code, 1)
        self.assertIn("--resubmit", err)

    def test_record_follows_the_umask(self):
        # review 2 launch.py:472: mkstemp made state/slurm_job 0600; collaborators must be able to read it
        old = os.umask(0o022)
        try:
            self.assertEqual(self.run_main("slurm", "submit", self.job_id)[0], 0)
        finally:
            os.umask(old)
        self.assertEqual(self.record.stat().st_mode & 0o777, 0o644)


class LocalFetchSymlinkTests(LauncherFixture):
    def test_fetch_creates_nothing_through_a_symlinked_inputs_folder(self):
        # review launch.py:201: mkdir ran before reject_symlinks and created folders at the symlink target
        cache_job = launch.unpack(self.root, self.job_id)
        payload = json.dumps({"job_sha256": sha((self.job_dir / "job.json").read_bytes())}).encode()
        import tarfile
        with tarfile.open(cache_job / "results.tar.gz", "w:gz") as tf:
            info = tarfile.TarInfo("results.json")
            info.size = len(payload)
            tf.addfile(info, io.BytesIO(payload))
        outside = self.fake.tmp / "outside"
        outside.mkdir()
        shutil.rmtree(self.root / "inputs", ignore_errors=True)
        (self.root / "inputs").symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "symlink"):
            launch.fetch(self.root, self.job_id)
        self.assertEqual(list(outside.iterdir()), [])


class PrintTests(LauncherFixture):
    def test_print_commands_exact(self):
        self.fake.hosts(HOST)
        s = self.job_settings()
        k = launch.cores_parallel(s, 8)
        code, out, _ = self.run_main("print", self.job_id, "--host", "lab")
        self.assertEqual(code, 0)
        bundle = (self.job_dir / "bundle.tar.gz").resolve()
        j, rd = self.job_id, "~/jobs"
        ssh = "ssh -o BatchMode=yes labpc"
        self.assertEqual(out.splitlines()[:7], [
            f"1. {ssh} 'mkdir -p {rd}'",
            f"2. scp -o BatchMode=yes {bundle} labpc:{rd}/{j}.tar.gz",
            f"3. {ssh} 'cd {rd} && tar xzf {j}.tar.gz && cd {j} && ./launch/remote.sh setup'",
            f"4. {ssh} 'cd {rd}/{j} && ./launch/remote.sh start --parallel {k}'",
            f"5. {ssh} 'cd {rd}/{j} && ./launch/remote.sh status'",
            f"6. {ssh} 'cd {rd}/{j} && ./launch/remote.sh collect'",
            f"7. mkdir -p inputs/calibration-results/{j} && "
            f"scp -o BatchMode=yes labpc:{rd}/{j}/results.tar.gz inputs/calibration-results/{j}/"])
        self.assertIn("only the bundle", out.lower())
        self.assertIn("leaves this machine", out)
        self.assertEqual(self.fake.calls(), [])  # printing runs nothing

    def test_print_without_config_uses_host_as_alias(self):
        s = self.job_settings()
        code, out, _ = self.run_main("print", self.job_id, "--host", "somebox")
        self.assertEqual(code, 0)
        self.assertIn(f"scp -o BatchMode=yes {(self.job_dir / 'bundle.tar.gz').resolve()} somebox:~/agentic-jobs/{self.job_id}.tar.gz", out)
        self.assertIn(f"--parallel {s['parallel']}'", out)


class SshTests(LauncherFixture):
    def test_submit_requires_authorization_and_logs_before_running(self):
        self.fake.hosts(HOST)
        for argv in (["--authorization", ""], ["--authorization", "   "], []):
            code, _, err = self.run_main("ssh", "submit", self.job_id, "--host", "lab", *argv)
            self.assertNotEqual(code, 0)
        self.assertEqual(self.fake.calls(), [])
        self.assertEqual(self.dispatch(), [])
        with mock.patch.dict(os.environ, {"FAKE_FAIL": "ssh,scp"}):  # the log line must exist even if ssh fails
            code, _, err = self.run_main("ssh", "submit", self.job_id, "--host", "lab",
                                         "--authorization", "researcher: yes, run it on lab")
        self.assertEqual(code, 1)
        entries = self.dispatch()
        self.assertEqual(len(entries), 1)
        e = entries[0]
        self.assertEqual(set(e), {"time", "host", "ssh_alias", "bundle_sha256", "authorization"})
        self.assertEqual((e["host"], e["ssh_alias"]), ("lab", "labpc"))
        self.assertEqual(e["bundle_sha256"], sha((self.job_dir / "bundle.tar.gz").read_bytes()))
        self.assertEqual(e["authorization"], "researcher: yes, run it on lab")

    def test_submit_runs_mkdir_upload_setup_start_and_batchmode_always_set(self):
        self.fake.hosts(HOST)
        code, _, err = self.run_main("ssh", "submit", self.job_id, "--host", "lab", "--authorization", "ok")
        self.assertEqual(code, 0, err)
        calls = self.fake.calls()
        self.assertEqual([c[0] for c in calls], ["ssh", "scp", "ssh", "ssh"])
        self.assertEqual(calls[0][-1], "mkdir -p ~/jobs")
        self.assertEqual(calls[1][-1], f"labpc:~/jobs/{self.job_id}.tar.gz")
        self.assertTrue(calls[3][-1].endswith("./launch/remote.sh start --parallel "
                                              f"{launch.cores_parallel(self.job_settings(), 8)}"))
        for c in calls:
            self.assertEqual(c[1:3], ["-o", "BatchMode=yes"])
        # status / cancel need no authorization and also use BatchMode
        for command in ("status", "cancel"):
            code, _, err = self.run_main("ssh", command, self.job_id, "--host", "lab")
            self.assertEqual(code, 0, err)
        last = self.fake.calls()[-2:]
        self.assertTrue(last[0][-1].endswith("./launch/remote.sh status"))
        self.assertTrue(last[1][-1].endswith("./launch/remote.sh cancel"))
        for c in last:
            self.assertEqual(c[1:4], ["-o", "BatchMode=yes", "labpc"])
        self.assertEqual(len(self.dispatch()), 1)  # only submit is logged

    def test_dispatch_log_symlink_refused(self):
        self.fake.hosts(HOST)
        target = self.fake.tmp / "elsewhere.jsonl"
        target.touch()
        (self.job_dir / "dispatch.jsonl").symlink_to(target)
        code, _, err = self.run_main("ssh", "submit", self.job_id, "--host", "lab", "--authorization", "ok")
        self.assertEqual(code, 1)
        self.assertEqual(self.fake.calls(), [])
        self.assertEqual(target.read_text(), "")

    def test_submit_refuses_a_modified_bundle(self):
        self.fake.hosts(HOST)
        with (self.job_dir / "bundle.tar.gz").open("ab") as h:
            h.write(b"x")
        code, _, err = self.run_main("ssh", "submit", self.job_id, "--host", "lab", "--authorization", "ok")
        self.assertEqual(code, 1)
        self.assertEqual(self.fake.calls(), [])

    def test_ssh_modes_need_a_configured_host(self):
        code, _, err = self.run_main("ssh", "submit", self.job_id, "--host", "nowhere", "--authorization", "ok")
        self.assertEqual(code, 1)
        self.assertEqual(self.fake.calls(), [])
        self.assertEqual(self.dispatch(), [])

    def fake_payload(self, job_sha=None):
        import io as _io
        import tarfile
        payload = json.dumps({"job_sha256": job_sha or sha((self.job_dir / "job.json").read_bytes()),
                              "partial": False, "restarts": [], "files": {}}).encode()
        path = self.fake.tmp / "payload.tar.gz"
        with tarfile.open(path, "w:gz") as tf:
            info = tarfile.TarInfo("results.json")
            info.size = len(payload)
            tf.addfile(info, _io.BytesIO(payload))
        os.environ["FAKE_SCP_PAYLOAD"] = str(path)
        self.addCleanup(os.environ.pop, "FAKE_SCP_PAYLOAD", None)

    def test_fetch_refuses_overwrite_and_foreign_job_dir(self):
        self.fake.hosts(HOST)
        self.fake_payload()
        code, out, err = self.run_main("ssh", "fetch", self.job_id, "--host", "lab")
        self.assertEqual(code, 0, err)
        dest = self.root / "inputs" / "calibration-results" / self.job_id / "results.tar.gz"
        self.assertTrue(dest.is_file())
        before = dest.read_bytes()
        code, _, err = self.run_main("ssh", "fetch", self.job_id, "--host", "lab")
        self.assertEqual(code, 1)
        self.assertIn("never overwrites", err)
        self.assertEqual(dest.read_bytes(), before)
        # only the bundle's results land in inputs/calibration-results/<job-id>/; nothing else is created
        self.assertEqual(sorted(p.name for p in dest.parent.iterdir()), ["results.tar.gz"])
        # a job that is not in this project, and results of another job
        self.assertEqual(self.run_main("ssh", "fetch", "job-000000000000", "--host", "lab")[0], 1)
        shutil.rmtree(dest.parent)
        self.fake_payload(job_sha="0" * 64)
        code, _, err = self.run_main("ssh", "fetch", self.job_id, "--host", "lab")
        self.assertEqual(code, 1)
        self.assertIn("job_sha256", err)
        self.assertFalse(dest.exists())
        # the destination folder must not be a symlink
        target = self.fake.tmp / "elsewhere"
        target.mkdir()
        dest.parent.symlink_to(target)
        self.fake_payload()
        self.assertEqual(self.run_main("ssh", "fetch", self.job_id, "--host", "lab")[0], 1)
        self.assertEqual(list(target.iterdir()), [])

    def test_fetch_scp_uses_batchmode_and_a_temp_target(self):
        self.fake.hosts(HOST)
        self.fake_payload()
        self.run_main("ssh", "fetch", self.job_id, "--host", "lab")
        (call,) = self.fake.calls()
        self.assertEqual(call[:3], ["scp", "-o", "BatchMode=yes"])
        self.assertEqual(call[3], f"labpc:~/jobs/{self.job_id}/results.tar.gz")


class SlurmTests(LauncherFixture):
    names = ("ssh", "scp", "sbatch")

    def setUp(self):
        super().setUp()
        p = mock.patch.object(launch, "slurm_setup")
        self.setup_mock = p.start()
        self.addCleanup(p.stop)

    def test_slurm_array_size_equals_restarts(self):
        s = self.job_settings()
        code, out, err = self.run_main("slurm", "submit", self.job_id)
        self.assertEqual(code, 0, err)
        self.setup_mock.assert_called_once()
        (call,) = self.fake.calls()
        self.assertEqual(call[0], "sbatch")
        self.assertIn(f"--array=1-{s['restarts']}", call)
        self.assertEqual(call[-1], "launch/slurm.sbatch")
        self.assertIn(f"--time={s['slurm']['time']}", call)
        self.assertIn(f"--cpus-per-task={s['slurm']['cpus_per_task']}", call)
        job = launch.cache_root() / self.job_id
        self.assertEqual((job / "state" / "slurm_job").read_text().strip(), "4242")
        code, _, err = self.run_main("slurm", "submit", self.job_id)  # a second array would double-run
        self.assertEqual(code, 1)
        self.assertIn("already", err)
        self.assertEqual(len(self.fake.calls()), 1)
        self.fake.make_tool("sacct")  # --resubmit asks sacct that the earlier array has ended
        with mock.patch.dict(os.environ, {"FAKE_SACCT": "4242_1|COMPLETED\n"}):
            self.assertEqual(self.run_main("slurm", "submit", self.job_id, "--resubmit")[0], 0)
        self.assertEqual((job / "state" / "slurm_job").read_text().split(), ["4242", "4242"])  # appended
        self.assertEqual(len([c for c in self.fake.calls() if c[0] == "sbatch"]), 2)

    def test_slurm_prints_when_no_sbatch(self):
        s = self.job_settings()
        with mock.patch.dict(os.environ, {"PATH": str(self.fake.bin / "none")}):
            code, out, err = self.run_main("slurm", "submit", self.job_id)
        self.assertEqual(code, 0, err)
        self.assertIn(f"sbatch --array=1-{s['restarts']}", out)
        self.assertIn("remote.sh setup", out)
        self.assertIn(f"cd {self.job_id}\n", out)
        self.assertNotIn(str(launch.cache_root()), out)
        self.assertEqual(self.fake.calls(), [])
        self.setup_mock.assert_not_called()


REMOTE_FAKE = """#!/usr/bin/env python3
import os, shutil, subprocess, sys
name = os.path.basename(sys.argv[0])
home = os.environ["FAKE_REMOTE_HOME"]
args = sys.argv[1:]
assert args[:2] == ["-o", "BatchMode=yes"], args
args = args[2:]
if name == "scp":
    src, dst = [a.replace("labpc:~", home) for a in args]
    if os.path.isdir(dst):
        dst = os.path.join(dst, os.path.basename(src))
    shutil.copyfile(src, dst)
else:
    alias, command = args
    assert alias == "labpc"
    command = command.replace("./launch/remote.sh setup", "mkdir -p .venv/bin && ln -sf " + os.environ["FAKE_PY"]
                              + " .venv/bin/python && printf 'sleep 300 & wait\\n' > launch/local.sh")
    sys.exit(subprocess.run(["bash", "-c", command], cwd=home, env={**os.environ, "HOME": home}).returncode)
"""


class EndToEndTests(LauncherFixture):
    def test_submit_status_fetch_against_a_fake_remote_home(self):
        import io as _io
        import tarfile
        home = self.fake.tmp / "remote-home"
        home.mkdir()
        for name in ("ssh", "scp"):
            (self.fake.bin / name).write_text(REMOTE_FAKE, encoding="utf-8")
        env = mock.patch.dict(os.environ, {"FAKE_REMOTE_HOME": str(home), "FAKE_PY": sys.executable,
                                           "REMOTE_STATUS_INTERVAL": "1"})
        env.start()
        self.addCleanup(env.stop)
        self.fake.hosts(HOST)
        self.addCleanup(lambda: self.run_main("ssh", "cancel", self.job_id, "--host", "lab"))
        code, _, err = self.run_main("ssh", "submit", self.job_id, "--host", "lab", "--authorization", "ok")
        self.assertEqual(code, 0, err)
        remote = home / "jobs" / self.job_id
        for pid in (remote / "state" / "pids").read_text().split():
            kill_group_at_cleanup(self, int(pid))  # the remote supervisor leads its own process group
        self.assertTrue((home / "jobs" / f"{self.job_id}.tar.gz").is_file())
        self.assertTrue((remote / "job.json").is_file())
        code, out, err = self.run_main("ssh", "status", self.job_id, "--host", "lab")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads((remote / "state" / "status.json").read_text())["job_id"], self.job_id)
        payload = json.dumps({"job_sha256": sha((self.job_dir / "job.json").read_bytes()),
                              "partial": False, "restarts": [], "files": {}}).encode()
        with tarfile.open(remote / "results.tar.gz", "w:gz") as tf:
            info = tarfile.TarInfo("results.json")
            info.size = len(payload)
            tf.addfile(info, _io.BytesIO(payload))
        code, out, err = self.run_main("ssh", "fetch", self.job_id, "--host", "lab")
        self.assertEqual(code, 0, err)
        self.assertTrue((self.root / "inputs" / "calibration-results" / self.job_id / "results.tar.gz").is_file())
        self.assertEqual(self.run_main("ssh", "cancel", self.job_id, "--host", "lab")[0], 0)


class RemoteShTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="remote-test-", dir=os.environ.get("TMPDIR") or None))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.job = self.tmp / "job-aaaaaaaaaaaa"
        (self.job / "launch").mkdir(parents=True)
        (self.job / ".venv" / "bin").mkdir(parents=True)
        (self.job / ".venv" / "bin" / "python").symlink_to(sys.executable)
        shutil.copy(RUNTIME / "remote.sh", self.job / "launch" / "remote.sh")
        shutil.copy(RUNTIME / "run.py", self.job / "run.py")
        (self.job / "job.json").write_text(json.dumps(
            {"job_id": "job-aaaaaaaaaaaa", "settings": {"restarts": 3, "seeds": [1, 2, 3], "workers": 1}}))
        (self.job / "launch" / "local.sh").write_text("#!/usr/bin/env bash\nsleep 300 &\nwait\n")
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.env = {**os.environ, "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
                    "FAKE_LOG": str(self.tmp / "calls.log"), "REMOTE_STATUS_INTERVAL": "1"}
        (self.tmp / "calls.log").touch()
        self.addCleanup(self.cleanup_processes)

    def cleanup_processes(self):
        pids = self.job / "state" / "pids"
        if pids.exists():
            self.remote("cancel")

    def tool(self, name):
        path = self.bin / name
        path.write_text(FAKE, encoding="utf-8")
        path.chmod(0o755)

    def remote(self, *args, env=None):
        done = subprocess.run(["bash", str(self.job / "launch" / "remote.sh"), *args], capture_output=True,
                              text=True, env={**self.env, **(env or {})}, timeout=60)
        if args[:1] == ("start",) and done.returncode == 0:
            for pid in (self.job / "state" / "pids").read_text().split():
                kill_group_at_cleanup(self, int(pid))  # the supervisor leads its own process group
        return done

    def spawn(self, argv):
        proc = subprocess.Popen(argv, start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(proc.wait)
        kill_group_at_cleanup(self, proc.pid)  # runs before the wait (cleanups are LIFO)
        return proc

    def restart(self, n, host=None, pid=None, result=None, log=None):
        folder = self.job / "out" / f"restart-{n}"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "owner.json").write_text(json.dumps(
            {"host": host or socket.gethostname(), "pid": pid or os.getpid(), "started_at": "t"}))
        if result:
            (folder / "result.json").write_text(json.dumps(result))
        if log:
            (folder / "log.txt").write_text(log)

    def test_remote_sh_syntax(self):
        for name in ("remote.sh", "slurm.sbatch", "local.sh"):
            subprocess.run(["bash", "-n", str(RUNTIME / name)], check=True)

    def test_usage_lists_cancel(self):
        done = self.remote("nonsense")
        self.assertEqual(done.returncode, 2)
        self.assertIn("cancel", done.stderr)

    def test_start_refused_while_a_restart_is_running(self):
        self.restart(1)  # owner = this test process, alive
        done = self.remote("start")
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertIn("running", done.stderr)
        self.assertFalse((self.job / "state" / "pids").exists())
        self.assertTrue((self.job / "out" / "restart-1" / "owner.json").exists())  # untouched

    def test_start_records_pids_keeps_status_and_cancel_stops_it(self):
        done = self.remote("start", "--parallel", "2")
        self.assertEqual(done.returncode, 0, done.stderr)
        pids = [int(x) for x in (self.job / "state" / "pids").read_text().split()]
        self.assertTrue(pids)
        for pid in pids:
            os.kill(pid, 0)
        for _ in range(50):
            if (self.job / "state" / "status.json").exists():
                break
            time.sleep(0.2)
        status = json.loads((self.job / "state" / "status.json").read_text())
        self.assertEqual(status["restarts"], 3)
        self.assertEqual(self.remote("start").returncode, 1)  # the launcher is alive
        done = self.remote("cancel")
        self.assertEqual(done.returncode, 0, done.stderr)
        for _ in range(50):
            alive = []
            for pid in pids:
                try:
                    os.kill(pid, 0)
                    # a zombie child of this test is "alive" for kill 0; check /proc state
                    if "Z" not in Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]:
                        alive.append(pid)
                except (ProcessLookupError, FileNotFoundError):
                    pass
            if not alive:
                break
            time.sleep(0.2)
        self.assertEqual(alive, [])

    def test_cancel_kills_recorded_supervisors(self):
        procs = [self.spawn(SUPERVISOR_LIKE) for _ in range(2)]
        (self.job / "state").mkdir()
        (self.job / "state" / "pids").write_text("".join(f"{p.pid}\n" for p in procs))
        done = self.remote("cancel")
        self.assertEqual(done.returncode, 0, done.stderr)
        for p in procs:
            self.assertEqual(p.wait(timeout=15), -signal.SIGTERM)
        self.assertEqual((self.job / "state" / "pids").read_text(), "")

    def test_cancel_and_start_ignore_a_reused_pid(self):
        # a recorded pid now belonging to an unrelated process (pid reuse) is never signalled nor blocks start
        other = self.spawn(["sleep", "300"])
        (self.job / "state").mkdir()
        (self.job / "state" / "pids").write_text(f"{other.pid}\n")
        done = self.remote("cancel")
        self.assertEqual(done.returncode, 0, done.stderr)
        time.sleep(0.3)
        self.assertIsNone(other.poll())
        self.assertIn("not this job", done.stdout + done.stderr)
        self.assertEqual((self.job / "state" / "pids").read_text(), "")
        (self.job / "state" / "pids").write_text(f"{other.pid}\n")
        done = self.remote("start")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertNotEqual((self.job / "state" / "pids").read_text().split(), [str(other.pid)])
        self.assertIsNone(other.poll())

    def test_supervise_clears_pids_when_it_finishes(self):
        (self.job / "launch" / "local.sh").write_text("#!/usr/bin/env bash\nexit 0\n")
        done = self.remote("start")
        self.assertEqual(done.returncode, 0, done.stderr)
        pids = self.job / "state" / "pids"
        for _ in range(100):
            if pids.read_text().strip() == "":
                break
            time.sleep(0.1)
        self.assertEqual(pids.read_text().strip(), "")

    def test_cancel_runs_scancel_for_the_recorded_array(self):
        self.tool("scancel")
        (self.job / "state").mkdir()
        (self.job / "state" / "slurm_job").write_text("4242\n")
        done = self.remote("cancel")
        self.assertEqual(done.returncode, 0, done.stderr)
        calls = [json.loads(x) for x in (self.tmp / "calls.log").read_text().splitlines()]
        self.assertEqual(calls, [["scancel", "4242"]])

    def test_status_counts_best_score_and_failure_tail(self):
        self.restart(1, result={"restart": 1, "status": "ok", "score": 0.5})
        self.restart(2, result={"restart": 2, "status": "ok", "score": 0.25})
        self.restart(3, result={"restart": 3, "status": "failed", "score": None},
                     log="".join(f"line {i}\n" for i in range(1, 26)))
        done = self.remote("status")
        self.assertEqual(done.returncode, 0, done.stderr)
        out = done.stdout
        self.assertRegex(out, r"done 3")
        self.assertRegex(out, r"failed 1")
        self.assertRegex(out, r"running 0")
        self.assertRegex(out, r"best score: 0\.25 \(restart 2\)")
        self.assertIn("line 25", out)
        self.assertIn("line 6\n", out)
        self.assertNotIn("line 5\n", out)

    def test_status_sacct_marks_ended_tasks_failed(self):
        self.tool("sacct")
        (self.job / "state").mkdir()
        (self.job / "state" / "slurm_job").write_text("4242\n")
        self.restart(1, host="node1", result={"restart": 1, "status": "ok", "score": 0.5})
        self.restart(2, host="node1", log="boom\n")
        self.restart(3, host="node1")
        sacct = ("4242_1|COMPLETED\n4242_1.batch|COMPLETED\n4242_2|OUT_OF_MEMORY\n4242_2.batch|OUT_OF_MEMORY\n"
                 "4242_3|RUNNING\n")
        done = self.remote("status", env={"FAKE_SACCT": sacct})
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertRegex(done.stdout, r"failed 1")
        self.assertRegex(done.stdout, r"running 1")
        self.assertIn("OUT_OF_MEMORY", done.stdout)
        status = json.loads((self.job / "state" / "status.json").read_text())
        self.assertEqual(status["running"], [3])
        self.assertEqual(status["failed_restarts"], [2])
        calls = [json.loads(x) for x in (self.tmp / "calls.log").read_text().splitlines()]
        self.assertEqual(calls[0], ["sacct", "-j", "4242", "--format=JobID,State", "--noheader", "-P"])
        # CANCELLED / TIMEOUT / NODE_FAIL (with a "by <uid>" suffix) count the same; a start is then allowed
        sacct = "4242_1|COMPLETED\n4242_2|CANCELLED by 1000\n4242_3|TIMEOUT\n"
        done = self.remote("status", env={"FAKE_SACCT": sacct})
        self.assertRegex(done.stdout, r"failed 3|failed 2")
        self.assertRegex(done.stdout, r"running 0")

    def test_status_without_sacct_keeps_other_host_restarts_running(self):
        (self.job / "state").mkdir()
        (self.job / "state" / "slurm_job").write_text("4242\n")
        self.restart(1, host="node1")
        done = self.remote("status", env={"PATH": f"{self.bin}{os.pathsep}/usr/bin{os.pathsep}/bin"})
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertRegex(done.stdout, r"running 1")

    def test_sacct_ended_pending_restarts_become_failed(self):
        self.tool("sacct")
        (self.job / "state").mkdir()
        (self.job / "state" / "slurm_job").write_text("4242\n")
        self.restart(1, host="node1", result={"restart": 1, "status": "ok", "score": 0.5})
        done = self.remote("status", env={"FAKE_SACCT": "4242_1|COMPLETED\n4242_2|TIMEOUT\n4242_3|NODE_FAIL\n"})
        self.assertRegex(done.stdout, r"failed 2")
        self.assertRegex(done.stdout, r"pending 0")
        status = json.loads((self.job / "state" / "status.json").read_text())
        self.assertEqual(status["failed_restarts"], [2, 3])

    def test_cancel_kills_group_members_that_ignore_term(self):
        (self.job / "launch" / "local.sh").write_text(
            "#!/usr/bin/env bash\nbash -c 'trap \"\" TERM; sleep 300' &\nwait\n")
        self.assertEqual(self.remote("start").returncode, 0)  # remote() registers the killpg cleanup
        time.sleep(1)
        pid = int((self.job / "state" / "pids").read_text().split()[0])
        self.assertEqual(self.remote("cancel").returncode, 0)
        time.sleep(0.5)
        with self.assertRaises(ProcessLookupError):
            os.killpg(pid, 0)

    def test_cancel_survives_a_failing_scancel_and_cancels_every_recorded_array(self):
        self.tool("scancel")
        (self.job / "state").mkdir()
        (self.job / "state" / "slurm_job").write_text("4242\n4300\n")
        done = self.remote("cancel", env={"FAKE_FAIL": "scancel"})
        self.assertEqual(done.returncode, 0, done.stderr)
        calls = [json.loads(x) for x in (self.tmp / "calls.log").read_text().splitlines()]
        self.assertEqual(calls, [["scancel", "4242"], ["scancel", "4300"]])
        self.assertIn("failed", done.stderr)

    def test_status_follows_the_latest_array(self):
        self.tool("sacct")
        (self.job / "state").mkdir()
        (self.job / "state" / "slurm_job").write_text("4242\n4300\n")
        self.restart(1, host="node1")
        self.remote("status", env={"FAKE_SACCT": ""})
        calls = [json.loads(x) for x in (self.tmp / "calls.log").read_text().splitlines()]
        self.assertEqual(calls[0][2], "4300")

    def test_start_validates_parallel(self):
        for bad in (["--parallel"], ["--parallel", "0"], ["--parallel", "x"], ["--bogus"]):
            done = self.remote("start", *bad)
            self.assertEqual(done.returncode, 2, bad)
            self.assertNotIn("started", done.stdout)
        self.assertFalse((self.job / "state" / "pids").exists())

    def marker_pids(self, marker):
        found = []
        for proc in Path("/proc").iterdir():
            if proc.name.isdigit():
                try:
                    if marker.encode() in (proc / "cmdline").read_bytes():
                        state = (proc / "stat").read_text().rsplit(")", 1)[1].split()[0]
                        if state != "Z":
                            found.append(int(proc.name))
                except OSError:
                    pass
        return found

    @unittest.skipUnless(Path("/proc").is_dir(), "needs /proc to find the calibration processes")
    def test_cancel_stops_every_process_without_setsid(self):
        # review remote.sh:155: without setsid (macOS) the supervisor led no process group, cancel killed only
        # local.sh and reported `stopped` while the calibration processes kept running
        farm = self.tmp / "nosetsid"
        farm.mkdir()
        for folder in os.environ["PATH"].split(os.pathsep):
            if not os.path.isdir(folder):
                continue
            for entry in os.scandir(folder):
                target = farm / entry.name
                if entry.name != "setsid" and not target.exists() and entry.is_file() and os.access(entry.path, os.X_OK):
                    target.symlink_to(entry.path)
        marker = f"calib-marker-{os.getpid()}-{time.monotonic_ns()}"

        def kill_markers():
            for pid in self.marker_pids(marker):
                with contextlib.suppress(ProcessLookupError):
                    os.kill(pid, signal.SIGKILL)
        self.addCleanup(kill_markers)
        (self.job / "launch" / "local.sh").write_text(
            "#!/usr/bin/env bash\n"
            f"{sys.executable} -c 'import time; time.sleep(300)' {marker} &\n"
            f"{sys.executable} -c 'import time; time.sleep(300)' {marker} &\nwait\n")
        env = {"PATH": f"{self.bin}{os.pathsep}{farm}"}
        self.assertEqual(self.remote("start", env=env).returncode, 0)
        for _ in range(50):
            if len(self.marker_pids(marker)) == 2:
                break
            time.sleep(0.1)
        self.assertEqual(len(self.marker_pids(marker)), 2)
        done = self.remote("cancel", env=env)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("stopped", done.stdout)
        for _ in range(20):
            if not self.marker_pids(marker):
                break
            time.sleep(0.1)
        self.assertEqual(self.marker_pids(marker), [])

    def test_readme_states_data_minimisation(self):
        self.assertIn("leaves", (RUNTIME / "README.txt").read_text())


if __name__ == "__main__":
    unittest.main()
