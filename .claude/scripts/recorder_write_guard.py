#!/usr/bin/env python3
"""Claude PreToolUse Write/Edit guard: recorder-owned records are written only by the recorder.

Reviewer runs (runs/scientific-reviewer/, runs/reproducibility-auditor/) and every invocation
record (runs/<specialist>/<session>/invocation/, unbound invocations) prove that an agent ran and
what it returned. A stage_review cites them, so no session, main or subagent, may author or
change them with a file tool; scripts/capture/record_invocation.py is their only writer.

This closes the direct route only. A shell command can still write anywhere: the Bash guard is
a tripwire, not a sandbox (docs/data-workflow.md, threat model).
"""
import json
import os
from pathlib import Path
import sys

ROOT = Path(os.environ.get("CLAUDE_PROJECT_DIR", Path(__file__).resolve().parents[2])).resolve()
TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
REVIEWERS = {"scientific-reviewer", "reproducibility-auditor"}


def protected(path: Path) -> str | None:
    """Why `path` is recorder-owned, or None. Case-folded: a case-insensitive filesystem reaches the same file."""
    try:
        parts = [part.casefold() for part in path.relative_to(ROOT).parts]
    except ValueError:
        return None  # outside the project
    if len(parts) < 2 or parts[0] != "runs":
        return None
    if parts[1] in REVIEWERS:
        return f"runs/{parts[1]}/ holds recorded reviewer runs"
    if "invocation" in parts[2:-1] or parts[1:3] == ["orchestrator", "unbound-invocations"]:
        return "invocation records are written by the recorder"
    return None


def main() -> int:
    # Fail closed: only exit 2 blocks, so any error must end there.
    try:
        event = json.load(sys.stdin)
        if not isinstance(event, dict):
            raise ValueError("hook event must be an object")
        if event.get("tool_name") not in TOOLS:
            return 0
        inputs = event.get("tool_input")
        target = inputs.get("file_path", inputs.get("notebook_path")) if isinstance(inputs, dict) else None
        if not isinstance(target, str) or not target:
            raise ValueError("file tool call without a file path")
        path = Path(target)
        base = Path(event["cwd"]) if isinstance(event.get("cwd"), str) and event["cwd"] else ROOT
        # resolve() follows symlinks and `..`, so the check sees the file the tool would write
        reason = protected((path if path.is_absolute() else base / path).resolve())
        if reason:
            raise ValueError(f"{reason}; run scripts/capture/recover.py instead of writing them")
        return 0
    except Exception as exc:  # noqa: BLE001 - fail closed
        print(f"Write blocked: {type(exc).__name__}: {exc}"[:2000], file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
