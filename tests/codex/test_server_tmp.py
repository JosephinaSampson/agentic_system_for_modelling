"""Cleanup of orphaned MaBoSS server temp directories (maboss-run-*)."""
from __future__ import annotations

import io
import os
import shutil
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from scripts.capture import recover, server_tmp

HOUR = 3600


class ServerTmpTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / "tmp-root"
        self.other = self.base / "other-root"
        self.root.mkdir()
        self.other.mkdir()
        self.now = time.time()
        # No real /proc scan or server detection unless a test asks for it.
        self.paths = []
        self.running = False
        self.enterContext(mock.patch.object(server_tmp, "_process_paths", side_effect=lambda: list(self.paths)))
        self.enterContext(mock.patch.object(server_tmp, "servers_running", side_effect=lambda: self.running))

    def run_dir(self, name: str, age: float, root: Path | None = None, size: int = 1000) -> Path:
        path = (root or self.root) / name
        (path / "sub").mkdir(parents=True)
        files = [path / "res_probtraj.csv", path / "sub" / "input.cfg"]
        for file in files:
            file.write_bytes(b"x" * size)
        stamp = self.now - age
        for item in [*files, path / "sub", path]:
            os.utime(item, (stamp, stamp))
        return path

    def clean(self, **options):
        return server_tmp.clean([self.root, self.other], now=self.now, **options)

    def test_candidates_only_owned_real_maboss_run_directories(self):
        kept = self.run_dir("maboss-run-a1", 2 * HOUR)
        self.run_dir("maboss-check-b2", 2 * HOUR)
        self.run_dir("physicell-run-c3", 2 * HOUR)
        (self.root / "maboss-run-file").write_text("not a directory")
        target = self.run_dir("elsewhere", 2 * HOUR, root=self.other)
        (self.root / "maboss-run-link").symlink_to(target)
        nested = self.run_dir("nested", 2 * HOUR)
        (nested / "maboss-run-deep").mkdir()
        self.assertEqual(server_tmp.candidates([self.root, self.base / "missing-root"]), [kept])
        with mock.patch.object(server_tmp.os, "getuid", return_value=os.getuid() + 1):
            self.assertEqual(server_tmp.candidates([self.root]), [])

    def test_old_unused_directory_is_removed_and_reported(self):
        old = self.run_dir("maboss-run-old", 2 * HOUR, size=4096)
        summary = self.clean()
        self.assertFalse(old.exists())
        self.assertEqual(summary["removed"], 1)
        self.assertEqual(summary["removed_bytes"], 2 * 4096)
        self.assertIn("removed 1", server_tmp.summary_line(summary))

    def test_dry_run_deletes_nothing(self):
        old = self.run_dir("maboss-run-old", 2 * HOUR)
        summary = self.clean(dry_run=True)
        self.assertTrue(old.is_dir())
        self.assertEqual(summary["removed"], 1)
        self.assertIn("dry run", server_tmp.summary_line(summary))

    def test_in_use_directory_is_kept(self):
        busy = self.run_dir("maboss-run-busy", 2 * HOUR)
        self.paths = [str(busy / "res_probtraj.csv")]
        summary = self.clean()
        self.assertTrue(busy.is_dir())
        self.assertEqual(summary["removed"], 0)
        self.assertIn("in use", summary["skipped"][0]["reason"])
        self.assertTrue(server_tmp.in_use(busy, [str(busy)]))
        self.assertFalse(server_tmp.in_use(busy, [str(busy) + "-other/file"]))

    def test_young_directory_is_kept_by_newest_mtime_inside(self):
        young = self.run_dir("maboss-run-young", 60)
        mixed = self.run_dir("maboss-run-mixed", 2 * HOUR)
        os.utime(mixed / "sub" / "input.cfg", (self.now - 30, self.now - 30))
        summary = self.clean()
        self.assertTrue(young.is_dir())
        self.assertTrue(mixed.is_dir())
        self.assertEqual({row["reason"].split(" ")[0] for row in summary["skipped"]}, {"younger"})
        self.assertFalse(self.clean(min_age_seconds=10)["skipped"])

    def test_running_server_keeps_directories_younger_than_a_day(self):
        recent = self.run_dir("maboss-run-recent", 2 * HOUR)
        stale = self.run_dir("maboss-run-stale", 25 * HOUR)
        self.running = True
        summary = self.clean()
        self.assertTrue(recent.is_dir())
        self.assertFalse(stale.exists())
        self.assertEqual(summary["removed"], 1)
        self.assertIn("server is running", summary["skipped"][0]["reason"])

    def test_symlinked_and_other_prefix_entries_are_never_touched(self):
        target = self.run_dir("precious", 2 * HOUR, root=self.other)
        (self.root / "maboss-run-link").symlink_to(target)
        other = self.run_dir("maboss-check-x", 2 * HOUR)
        foreign = self.run_dir("tmpabc", 2 * HOUR)
        summary = self.clean()
        self.assertTrue((target / "res_probtraj.csv").is_file())
        self.assertTrue((self.root / "maboss-run-link").is_symlink())
        self.assertTrue(other.is_dir())
        self.assertTrue(foreign.is_dir())
        self.assertEqual(summary["removed"], 0)

    def test_symlink_inside_a_candidate_is_not_followed(self):
        outside = self.run_dir("precious", 2 * HOUR, root=self.other)
        run = self.run_dir("maboss-run-old", 2 * HOUR)
        (run / "escape").symlink_to(outside)
        os.utime(run / "escape", (self.now - 2 * HOUR,) * 2, follow_symlinks=False)
        os.utime(run, (self.now - 2 * HOUR,) * 2)
        self.clean()
        self.assertFalse(run.exists())
        self.assertTrue((outside / "res_probtraj.csv").is_file())

    def test_low_free_space_is_warned(self):
        usage = shutil.disk_usage(self.root)._replace(free=100 * 1024 * 1024)
        with mock.patch.object(server_tmp.shutil, "disk_usage", return_value=usage):
            summary = self.clean()
        self.assertIn(f"WARNING: {self.root} has 100 MiB free", summary["warnings"])

    def test_cli_clean_and_status(self):
        self.run_dir("maboss-run-old", 2 * HOUR)
        young = self.run_dir("maboss-run-young", 60)
        with mock.patch.object(server_tmp, "default_roots", return_value=[self.root]):
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(server_tmp.main(["status"]), 0)
            self.assertIn(str(self.root), out.getvalue())
            self.assertIn("2 maboss-run", out.getvalue())
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(server_tmp.main(["clean", "--dry-run"]), 0)
            self.assertIn("would remove 1", out.getvalue())
            self.assertIn(f"skipped: {young}", out.getvalue())
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(server_tmp.main(["clean", "--min-age", "0"]), 0)
            self.assertIn("removed 2", out.getvalue())

    def test_default_roots_are_the_rendered_maboss_tmpdir_and_system_tmp(self):
        with mock.patch.dict(os.environ, {"XDG_CACHE_HOME": str(self.base / "cache")}):
            self.assertEqual(server_tmp.default_roots(),
                             [self.base / "cache/mcp-biomodelling/tmp/maboss", Path("/tmp")])


class RealProcessScanTests(unittest.TestCase):
    def test_own_cwd_and_open_file_are_detected(self):
        if not Path("/proc/self/fd").is_dir():
            self.skipTest("no /proc")
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary).resolve()
            self.assertFalse(server_tmp.in_use(directory))
            with open(directory / "open.txt", "w"):
                self.assertTrue(server_tmp.in_use(directory))

    def test_server_detection_reads_command_lines(self):
        with mock.patch.object(server_tmp, "_cmdlines", return_value=[["python", "/env/bin/mcp-maboss-server"]]):
            self.assertTrue(server_tmp.servers_running())
        with mock.patch.object(server_tmp, "_cmdlines", return_value=[["uvx", "mcp-neko-server"]]):
            self.assertFalse(server_tmp.servers_running())


class RecoverCleanupTests(unittest.TestCase):
    RESULT = {"status": "recorded", "session_id": "sess-1", "capture_path": "runs/x", "report_md": None,
              "transcript": "t.jsonl", "exports": None}

    def run_main(self, specialist: str):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(recover, "recover", return_value=dict(self.RESULT)), \
                mock.patch.object(recover, "project_root", return_value=Path("/project")), \
                redirect_stdout(out), redirect_stderr(err):
            code = recover.main(["--specialist", specialist, "--agent-id", "agent-1"])
        return code, out.getvalue(), err.getvalue()

    def test_boolean_recovery_cleans_orphaned_run_directories(self):
        summary = {"removed": 2, "removed_bytes": 3 * 1024 ** 2, "skipped": [], "warnings": [], "dry_run": False}
        with mock.patch.object(server_tmp, "clean", return_value=summary) as clean:
            code, out, _ = self.run_main("boolean-dynamics-modeler")
        self.assertEqual(code, 0)
        clean.assert_called_once_with(server_tmp.default_roots())
        lines = out.splitlines()
        self.assertIn("RECOVERED boolean-dynamics-modeler sess-1 recorded", lines)
        self.assertGreater(lines.index(server_tmp.summary_line(summary)),
                           lines.index("RECOVERED boolean-dynamics-modeler sess-1 recorded"))

    def test_cleanup_failure_never_fails_a_recovery(self):
        with mock.patch.object(server_tmp, "clean", side_effect=PermissionError("denied")):
            code, out, err = self.run_main("boolean-dynamics-modeler")
        self.assertEqual(code, 0, err)
        self.assertIn("RECOVERED boolean-dynamics-modeler sess-1 recorded", out)
        self.assertIn("TMP CLEANUP WARNING: denied", out)
        self.assertNotIn("RECOVER FAILED", err)

    def test_other_specialists_do_not_clean(self):
        with mock.patch.object(server_tmp, "clean") as clean:
            code, out, _ = self.run_main("network-curator")
        self.assertEqual(code, 0)
        clean.assert_not_called()
        self.assertEqual(out.splitlines()[-1], "RECOVERED network-curator sess-1 recorded")


if __name__ == "__main__":
    unittest.main()
