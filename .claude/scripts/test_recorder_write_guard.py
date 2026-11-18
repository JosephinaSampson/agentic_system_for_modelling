#!/usr/bin/env python3
"""Tests for the recorder-owned record write guard."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

GUARD = Path(__file__).resolve().with_name("recorder_write_guard.py")
SETTINGS = GUARD.parents[1] / "settings.json"


class RecorderWriteGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()

    def run_guard(self, tool: str, path: str | None, raw: str | None = None, cwd: str | None = None):
        event = {"tool_name": tool, "tool_input": {} if path is None else {"file_path": path}}
        if cwd is not None:
            event["cwd"] = cwd
        return subprocess.run([sys.executable, str(GUARD)], input=json.dumps(event) if raw is None else raw,
                              text=True, capture_output=True, env={**os.environ, "CLAUDE_PROJECT_DIR": str(self.root)})

    def test_recorder_owned_records_are_refused(self) -> None:
        for path in ("runs/scientific-reviewer/a1/report.md",
                     "runs/reproducibility-auditor/a1/invocation.json",
                     "runs/Scientific-Reviewer/new/report.md",
                     "runs/network-curator/s1/invocation/a1/task.txt",
                     "runs/data-analyst/d1/invocation/a1/tool_calls.json",
                     "runs/orchestrator/unbound-invocations/ode-modeler/a1/final_report.md",
                     "runs/orchestrator/../scientific-reviewer/a1/report.md",
                     str(self.root / "runs/scientific-reviewer/a1/task.txt")):
            for tool in ("Write", "Edit", "MultiEdit"):
                with self.subTest(path=path, tool=tool):
                    result = self.run_guard(tool, path)
                    self.assertEqual(result.returncode, 2, result)
                    self.assertIn("recover.py", result.stderr)
        result = self.run_guard("Write", "scientific-reviewer/a1/report.md", cwd=str(self.root / "runs"))
        self.assertEqual(result.returncode, 2, result)

    def test_a_symlink_into_a_reviewer_folder_is_resolved(self) -> None:
        (self.root / "runs/scientific-reviewer").mkdir(parents=True)
        try:
            (self.root / "runs/orchestrator").symlink_to(self.root / "runs/scientific-reviewer")
        except OSError:
            self.skipTest("symlinks unavailable")
        self.assertEqual(self.run_guard("Write", "runs/orchestrator/report.md").returncode, 2)

    def test_other_writes_and_tools_pass(self) -> None:
        for path in ("MODEL_SPEC.md", "runs/orchestrator/reviews/summary.md", "runs/network-curator/s1/notes.md",
                     "runs/orchestrator/p1/invocation-notes.md", "evidence/reports/s1/A__B.md",
                     str(self.root.parent / "elsewhere/runs/scientific-reviewer/x.md")):
            with self.subTest(path=path):
                self.assertEqual(self.run_guard("Write", path).returncode, 0)
        self.assertEqual(self.run_guard("Read", "runs/scientific-reviewer/a1/report.md").returncode, 0)

    def test_malformed_events_block(self) -> None:
        self.assertEqual(self.run_guard("Write", None).returncode, 2)
        for raw in ("", "[]", "{not json"):
            with self.subTest(raw=raw):
                self.assertEqual(self.run_guard("Write", None, raw=raw).returncode, 2)

    def test_settings_register_the_guard_for_every_file_tool(self) -> None:
        groups = json.loads(SETTINGS.read_text())["hooks"]["PreToolUse"]
        self.assertIn(("Write|Edit|MultiEdit|NotebookEdit",
                       'python3 "$CLAUDE_PROJECT_DIR/.claude/scripts/recorder_write_guard.py" || exit 2'),
                      {(group["matcher"], hook["command"]) for group in groups for hook in group["hooks"]})


if __name__ == "__main__":
    unittest.main()
