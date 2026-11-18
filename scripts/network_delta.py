#!/usr/bin/env python3
"""The exact difference between two NeKo-to-MaBoSS exports of one session.

Usage: network_delta.py <reviewed handoff manifest> <new handoff manifest> [--root ROOT]

A small edit of a reviewed network (one edge after a literature or data discussion) does not need
both reviewers to read the whole network again. It needs the researcher to see exactly what changed.
This computes that from the two exported BNET files, never from a description: the signed regulator
edges added, removed or sign-changed, and the before/after rule of every target whose rule differs.
The output is what a `stage_delta` governance record must contain (scripts/state.py checks the record
against a fresh computation).

It refuses, and a full stage review is then required, when the two exports are not NeKo-to-MaBoSS
handoffs of the same session, when the node sets differ, when a rule cannot be read as signed
regulators (a regulator used with both signs, or unknown syntax), or when a rule changed without any
regulator change (a logic edit, not an edge edit).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

if __package__ in (None, ""):  # run as a script: make the repo importable
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.state import ROOT, checked_artifact, digest, identifier

FULL_REVIEW = "; a full stage review is required"
TOKEN = re.compile(r"\s*(?:([A-Za-z_][A-Za-z_0-9]*)|([!&|()])|([01]))")
CONSTANTS = {"true", "false"}
SIGNS = ("activation", "inhibition")
CHANGES = ("added", "removed", "sign_changed")


def regulators(rule: str) -> dict[str, str]:
    """{regulator: "activation" | "inhibition"} of one Boolean rule over !, &, | and parentheses."""
    found: dict[str, str] = {}
    negated, pending, stack, position = False, False, [], 0
    rule = rule.strip()
    while position < len(rule):
        match = TOKEN.match(rule, position)
        if match is None:
            raise ValueError(f"unsupported rule syntax at {rule[position:position + 12]!r}")
        position = match.end()
        name, operator, constant = match.groups()
        if operator == "!":
            pending = not pending
        elif operator == "(":
            stack.append(negated)
            negated, pending = negated ^ pending, False
        elif operator == ")":
            if not stack or pending:
                raise ValueError("unbalanced rule")
            negated = stack.pop()
        elif operator:
            if pending:
                raise ValueError("unsupported rule syntax: operator after !")
        else:
            if constant is None and name.lower() not in CONSTANTS:
                sign = SIGNS[negated ^ pending]
                if found.setdefault(name, sign) != sign:
                    raise ValueError(f"regulator {name} is used with both signs")
            pending = False
    if stack or pending:
        raise ValueError("unbalanced rule")
    return found


def bnet_rules(text: str) -> dict[str, str]:
    """{target: rule} of a BNET file, whitespace-normalised, in file order."""
    rules: dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#") or "," not in line:
            continue
        target, rule = (part.strip() for part in line.split(",", 1))
        if target.lower() == "targets":
            continue
        if target in rules:
            raise ValueError(f"BNET target {target} is defined twice")
        rules[target] = re.sub(r"\s+", " ", rule)
    return rules


def delta(before: str, after: str) -> dict:
    """{"changes": [...], "rules": [...]} between two BNET texts; ValueError when it is not an edge-only edit."""
    old, new = bnet_rules(before), bnet_rules(after)
    if set(old) != set(new):
        names = ", ".join(sorted(set(old) ^ set(new)))
        raise ValueError(f"the node sets differ ({names})" + FULL_REVIEW)
    changes, rules = [], []
    for target in sorted(new):
        if re.sub(r"\s", "", old[target]) == re.sub(r"\s", "", new[target]):
            continue
        try:
            was, now = regulators(old[target]), regulators(new[target])
        except ValueError as exc:
            raise ValueError(f"the rule of {target} cannot be read as signed edges ({exc})" + FULL_REVIEW) from None
        if was == now:
            raise ValueError(f"the rule of {target} changed without an edge change" + FULL_REVIEW)
        rules.append({"target": target, "before": old[target], "after": new[target]})
        for source in sorted(set(was) | set(now)):
            if was.get(source) == now.get(source):
                continue
            change = "added" if source not in was else "removed" if source not in now else "sign_changed"
            changes.append({"change": change, "source": source, "target": target,
                            "sign": was[source] if change == "removed" else now[source]})
    return {"changes": changes, "rules": rules}


def _bnet(root: Path, ref: dict, session: str) -> str:
    prefix = f"runs/network-curator/{session}/"
    if not isinstance(ref, dict) or not isinstance(ref.get("path"), str) or not ref["path"].startswith(prefix):
        raise ValueError(f"a delta compares handoff manifests under {prefix}" + FULL_REVIEW)
    try:
        data = json.loads(checked_artifact(root, ref).read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        raise ValueError(f"{ref['path']} is not a JSON handoff manifest") from None
    source = data.get("source") if isinstance(data, dict) else None
    if (not isinstance(source, dict) or data.get("handoff_type") != "neko-to-maboss"
            or (source.get("server"), source.get("session_id")) != ("NeKo", session)):
        raise ValueError(f"{ref['path']} is not a NeKo-to-MaBoSS handoff of session {session}" + FULL_REVIEW)
    bnet = data.get("bnet_file")
    if not isinstance(bnet, dict) or not isinstance(bnet.get("path"), str):
        raise ValueError(f"{ref['path']} names no BNET file")
    path = Path(bnet["path"])
    if path.is_absolute():
        path = path.relative_to(root.resolve())
    return checked_artifact(root, {"path": path.as_posix(), "sha256": bnet.get("sha256")}).read_text(encoding="utf-8")


def manifest_delta(root: Path, parent: dict, artifact: dict, session: str) -> dict:
    """delta() of the BNET files named by two handoff manifests ({"path", "sha256"}) of `session`."""
    identifier(session)
    if parent == artifact:
        raise ValueError("the new export is the reviewed export")
    result = delta(_bnet(root, parent, session), _bnet(root, artifact, session))
    if not result["changes"]:
        raise ValueError("the two exports have the same rules: nothing to approve")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("parent", help="repository-relative path of the reviewed handoff manifest")
    parser.add_argument("artifact", help="repository-relative path of the new handoff manifest")
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args()
    root = args.root.resolve()
    try:
        refs = []
        for value in (args.parent, args.artifact):
            path = root / value
            if not path.is_file():
                raise ValueError(f"missing artifact: {value}")
            refs.append({"path": Path(value).as_posix(), "sha256": digest(path)})
        parts = Path(refs[0]["path"]).parts
        if len(parts) < 4 or parts[:2] != ("runs", "network-curator"):
            raise ValueError("the reviewed manifest must be under runs/network-curator/<session>/")
        session = parts[2]
        result = manifest_delta(root, refs[0], refs[1], session)
        print(json.dumps({"session_id": session, "parent": refs[0], "artifact": refs[1], **result}, indent=2))
        return 0
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print(json.dumps({"passed": False, "reason": str(exc)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
