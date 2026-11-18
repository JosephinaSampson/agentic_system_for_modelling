"""Test fixture: recorded reviewer runs for a stage_review record.

A stage_review must cite reports written by the invocation recorder (state.checked_review), so fixtures
make them the way a real run does: a reviewer transcript whose brief names the reviewed artifact, passed
to scripts.capture.record_invocation.record.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from scripts.capture.record_invocation import REVIEWERS, record


def transcript(brief: str, report: str) -> str:
    """A minimal reviewer transcript: one user brief, one assistant final message."""
    return "".join(json.dumps(event) + "\n" for event in (
        {"type": "user", "message": {"content": brief}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": report}]}}))


def recorded_reviews(root, artifact: dict, tag: str = "review-1", report: str = "independent bounded review") -> dict:
    """{role: {"path", "sha256"}} for both reviewers of `artifact` ({"path", "sha256"}), recorded under
    runs/<role>/<tag>/ in `root`. `tag` is the agent id: use a new one per reviewed artifact."""
    refs = {}
    with tempfile.TemporaryDirectory() as temporary:
        for role in REVIEWERS:
            source = Path(temporary) / f"{role}.jsonl"
            source.write_text(transcript(f"Review {artifact['path']} sha256 {artifact['sha256']}.", report),
                              encoding="utf-8")
            result = record(Path(root), {"agent_type": role, "agent_id": tag, "agent_transcript_path": str(source)})
            refs[role] = result["report"]
    return refs
