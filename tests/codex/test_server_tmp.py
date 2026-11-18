"""Cleanup of orphaned MaBoSS server temp directories (maboss-run-*)."""
from __future__ import annotations

import io
import os
import shutil
import stat
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from scripts.capture import recover, server_tmp

HOUR = 3600
# Captured before setUp patches it, so one test can exercise the real scan.
REAL_PROCESS_TABLE = server_tmp._process_table


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
        # No real /proc scan unless a test asks for it: tests set the process table.
        self.table = []
        self.enterContext(mock.patch.object(server_tmp, "_process_table", side_effect=lambda: list(self.table)))

    def process(self, argv=("python",), start=None, paths=()):
        return server_tmp.Process(len(self.table) + 100, tuple(argv), start, tuple(paths))

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

    def test_no_process_table_removes_nothing_and_notes_it(self):
        # macOS has no /proc: the in-use check is impossible, so every candidate stays.
        old = self.run_dir("maboss-run-old", 2 * HOUR)
        with mock.patch.object(server_tmp, "_process_table", return_value=None):
            summary = self.clean()
            self.assertTrue(server_tmp.in_use(old))
            self.assertFalse(server_tmp.servers_running())
        self.assertTrue(old.is_dir())
        self.assertEqual(summary["removed"], 0)
        self.assertEqual([row["path"] for row in summary["skipped"]], [str(old)])
        self.assertIn("no readable process table", summary["skipped"][0]["reason"])
        self.assertTrue(any(line.startswith("TMP CLEANUP NOTE:") for line in summary["warnings"]))
        self.assertIn("skipped 1", server_tmp.summary_line(summary))

    def test_unreadable_proc_yields_no_table(self):
        with mock.patch.object(server_tmp.os, "listdir", side_effect=OSError("no /proc")):
            self.assertIsNone(REAL_PROCESS_TABLE())

    def test_in_use_directory_is_kept(self):
        busy = self.run_dir("maboss-run-busy", 2 * HOUR)
        self.table = [self.process(paths=[str(busy / "res_probtraj.csv")])]
        summary = self.clean()
        self.assertTrue(busy.is_dir())
        self.assertEqual(summary["removed"], 0)
        self.assertIn("in use", summary["skipped"][0]["reason"])
        self.assertTrue(server_tmp.in_use(busy, [self.process(paths=[str(busy)])]))
        self.assertFalse(server_tmp.in_use(busy, [self.process(paths=[str(busy) + "-other/file"])]))

    def test_directory_named_on_a_process_argv_is_in_use(self):
        busy = self.run_dir("maboss-run-argv", 2 * HOUR)
        free = self.run_dir("maboss-run-argvx", 2 * HOUR)
        # The engine worker receives the run directory on its command line.
        self.table = [self.process(argv=["engine_worker", "--out=" + str(busy) + "/res", "-c", "x.cfg"])]
        summary = self.clean()
        self.assertTrue(busy.is_dir())
        self.assertFalse(free.exists())
        self.assertEqual([row["path"] for row in summary["skipped"]], [str(busy)])

    def test_young_directory_is_kept_by_newest_mtime_inside(self):
        young = self.run_dir("maboss-run-young", 60)
        mixed = self.run_dir("maboss-run-mixed", 2 * HOUR)
        os.utime(mixed / "sub" / "input.cfg", (self.now - 30, self.now - 30))
        summary = self.clean()
        self.assertTrue(young.is_dir())
        self.assertTrue(mixed.is_dir())
        self.assertEqual({row["reason"].split(" ")[0] for row in summary["skipped"]}, {"younger"})
        self.assertFalse(self.clean(min_age_seconds=10)["skipped"])

    def test_running_server_keeps_only_directories_modified_since_it_started(self):
        recent = self.run_dir("maboss-run-recent", 2 * HOUR)
        orphan = self.run_dir("maboss-run-orphan", 5 * HOUR)
        stale = self.run_dir("maboss-run-stale", 25 * HOUR)
        self.table = [self.process(["/env/bin/python", "/env/bin/mcp-maboss-server"], start=self.now - 3 * HOUR)]
        summary = self.clean()
        self.assertTrue(recent.is_dir())
        self.assertFalse(orphan.exists())
        self.assertFalse(stale.exists())
        self.assertEqual(summary["removed"], 2)
        self.assertIn("MaBoSS server", summary["skipped"][0]["reason"])

    def test_earliest_live_server_start_counts_and_backstop_is_a_day(self):
        middle = self.run_dir("maboss-run-middle", 4 * HOUR)
        backstop = self.run_dir("maboss-run-backstop", 25 * HOUR)
        self.table = [self.process(["uvx", "--from", "pkg", "mcp-maboss-server"], start=self.now - HOUR),
                      self.process(["/env/bin/mcp-maboss-server"], start=self.now - 30 * HOUR)]
        self.clean()
        self.assertTrue(middle.is_dir())
        self.assertFalse(backstop.exists())

    def test_command_lines_mentioning_the_server_are_not_servers(self):
        recent = self.run_dir("maboss-run-recent", 2 * HOUR)
        self.table = [self.process(["bash", "-c", "pgrep -f mcp-maboss-server"], start=self.now - 10 * HOUR),
                      self.process(["grep", "mcp-maboss-server-old"], start=self.now - 10 * HOUR),
                      self.process(["vim", "notes about mcp-maboss-server"], start=self.now - 10 * HOUR)]
        self.assertFalse(server_tmp.servers_running(self.table))
        self.clean()
        self.assertFalse(recent.exists())

    def test_server_with_unknown_start_keeps_everything_below_a_day(self):
        recent = self.run_dir("maboss-run-recent", 20 * HOUR)
        self.table = [self.process(["mcp-maboss-server"], start=None)]
        self.clean()
        self.assertTrue(recent.is_dir())

    def test_vanished_directory_is_reported_not_an_error(self):
        gone = self.run_dir("maboss-run-gone", 2 * HOUR)
        real_scan = server_tmp._scan
        def vanish(path):
            shutil.rmtree(path)
            return real_scan(path)
        with mock.patch.object(server_tmp, "_scan", side_effect=vanish):
            summary = self.clean()
        self.assertFalse(gone.exists())
        self.assertEqual(summary["errors"], [])
        self.assertEqual(summary["vanished"], [str(gone)])
        self.assertIn("vanished 1", server_tmp.summary_line(summary))

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

    def test_own_process_has_argv_and_a_plausible_start_time(self):
        if not Path("/proc/self/stat").is_file():
            self.skipTest("no /proc")
        own = [p for p in server_tmp._process_table() if p.pid == os.getpid()]
        self.assertEqual(len(own), 1)
        self.assertTrue(own[0].argv)
        self.assertLessEqual(own[0].start, time.time() + 1)
        self.assertGreater(own[0].start, time.time() - 7 * 24 * HOUR)

    def test_stat_start_time_parsing_survives_odd_command_names(self):
        fields = ["S"] + ["0"] * 18 + ["2500"] + ["0"] * 10
        raw = "4242 (a) b (c)) " + " ".join(fields)
        self.assertEqual(server_tmp._stat_start(raw, boot=1000.0, ticks=100), 1025.0)

    def test_server_detection_matches_argv_elements(self):
        def proc(*argv):
            return server_tmp.Process(1, argv, 0.0, ())
        self.assertTrue(server_tmp.servers_running([proc("python", "/env/bin/mcp-maboss-server")]))
        self.assertTrue(server_tmp.servers_running([proc("uvx", "--from", "x", "mcp-maboss-server")]))
        self.assertFalse(server_tmp.servers_running([proc("uvx", "mcp-neko-server")]))
        self.assertFalse(server_tmp.servers_running([proc("sh", "-c", "ps | grep mcp-maboss-server")]))


class RecoverCleanupTests(unittest.TestCase):
    RESULT = {"status": "recorded", "session_id": "sess-1", "capture_path": "runs/x", "report_md": None,
              "transcript": "t.jsonl", "exports": None}

    def setUp(self):
        # The cleanup recreates the MaBoSS TMPDIR: keep it inside the fixture.
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.enterContext(mock.patch.dict(os.environ, {"XDG_CACHE_HOME": temporary.name}))

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

    def test_cleanup_import_failure_never_fails_a_recovery(self):
        with mock.patch.dict(sys.modules, {"scripts.capture.server_tmp": None}):
            code, out, err = self.run_main("boolean-dynamics-modeler")
        self.assertEqual(code, 0, err)
        self.assertIn("RECOVERED boolean-dynamics-modeler sess-1 recorded", out)
        self.assertIn("TMP CLEANUP WARNING:", out)

    def test_missing_server_tmpdir_is_recreated_with_a_fallback_warning(self):
        summary = {"removed": 0, "removed_bytes": 0, "skipped": [], "warnings": [], "dry_run": False}
        with tempfile.TemporaryDirectory() as temporary, \
                mock.patch.dict(os.environ, {"XDG_CACHE_HOME": temporary}), \
                mock.patch.object(server_tmp, "clean", return_value=summary) as clean:
            directory = Path(temporary) / "mcp-biomodelling/tmp/maboss"
            code, out, _ = self.run_main("boolean-dynamics-modeler")
            self.assertEqual(code, 0)
            self.assertTrue(directory.is_dir())
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
            self.assertIn(f"TMP CLEANUP WARNING: {directory} was missing", out)
            self.assertIn("fell back to /tmp", out)
            clean.assert_called_once()
            code, out, _ = self.run_main("boolean-dynamics-modeler")
            self.assertNotIn("was missing", out)
            # A symlinked component is refused (warned), and cleanup still runs.
            shutil.rmtree(directory.parent.parent)
            (Path(temporary) / "elsewhere").mkdir()
            directory.parent.parent.symlink_to(Path(temporary) / "elsewhere")
            code, out, _ = self.run_main("boolean-dynamics-modeler")
            self.assertEqual(code, 0)
            self.assertIn("TMP CLEANUP WARNING:", out)
            self.assertIn("symlink", out)
            self.assertFalse((Path(temporary) / "elsewhere/tmp").exists())
            self.assertEqual(clean.call_count, 3)

    def test_other_specialists_do_not_clean(self):
        with mock.patch.object(server_tmp, "clean") as clean:
            code, out, _ = self.run_main("network-curator")
        self.assertEqual(code, 0)
        clean.assert_not_called()
        self.assertEqual(out.splitlines()[-1], "RECOVERED network-curator sess-1 recorded")


if __name__ == "__main__":
    unittest.main()
