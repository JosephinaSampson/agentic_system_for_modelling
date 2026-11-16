#!/usr/bin/env python3
"""Claude PreToolUse Agent/Task guard for downstream specialist dispatch."""
import json
import os
from pathlib import Path
import sys

ROOT = Path(os.environ.get("CLAUDE_PROJECT_DIR", Path(__file__).resolve().parents[2])).resolve()
sys.path.insert(0, str(ROOT))
GATED = {"boolean_dynamics_modeler", "multicellular_configurator", "network_curator", "ode_modeler"}


def main():
    # Fail closed: any exception, including an import failure or a crash inside a
    # check, blocks the dispatch (exit 2); exit 1 would let it proceed.
    try:
        event = json.load(sys.stdin)
        if not isinstance(event, dict):
            raise ValueError("hook event must be an object")
        if event.get("tool_name") not in {"Agent", "Task"}:
            return 0
        inputs = event.get("tool_input", {})
        if not isinstance(inputs, dict) or not isinstance(inputs.get("subagent_type", ""), str):
            raise ValueError("invalid agent dispatch input")
        role = inputs.get("subagent_type", "").split(":")[-1].replace("-", "_")
        if role not in GATED:
            return 0
        from scripts import gate
        prompt = inputs.get("prompt")
        prompt = "" if prompt is None else prompt
        if not isinstance(prompt, str):
            raise ValueError("dispatch prompt must be a string")
        # Data enter NeKo, MaBoSS, the ODE (approved calibration values) and PhysiCell (an approved stage-A
        # design or stage-B projection) only through an approved, hash-checked DATA_INPUT line. check_data_input
        # also runs, once and after verifying that line, the rule that no specialist is pointed at a calibration
        # job folder or returned results (only the ode-modeler's approved proposal is exempt). Every gated role
        # has a DATA_INPUT form; a gated role without one is blocked (it would skip those rules).
        if role not in gate.DATA_INPUT_LINE:
            raise ValueError(f"gated role {role} has no DATA_INPUT form in scripts/gate.py DATA_INPUT_LINE")
        gate.check_data_input(ROOT, role, prompt)
        # ODE authoring has its own source/readiness/whole-model approval contract;
        # this hook enforces the two sequential Boolean downstream boundaries. The
        # multicellular-configurator's line-1 STAGE_GATE is checked here, unchanged, with or without
        # a line-2 DATA_INPUT.
        if role in {"boolean_dynamics_modeler", "multicellular_configurator"}:
            gate.check_task(ROOT, role, prompt)
        return 0
    except Exception as exc:  # noqa: BLE001 - see fail-closed note above
        print(f"Downstream stage dispatch blocked: {type(exc).__name__}: {exc}"[:2000], file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
