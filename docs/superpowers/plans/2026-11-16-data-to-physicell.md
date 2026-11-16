# Data to PhysiCell Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Approved multicellular designs (stage A) and data projections (stage B) reach
PhysiCell only through checked, approved, gate-verified handoffs to the
multicellular-configurator. The exported settings are verified afterwards, and a skill
builds a PhysiCell project to run them.

**Architecture:**
- New `scripts/physicell/` module: settings-XML helpers, a design draft/check, a
  dictionary capture, a projection draft/check, export verification, and a build
  script.
- The existing gate gains two `DATA_INPUT` forms for the multicellular-configurator (line
  2, after `STAGE_GATE`).
- Approvals reuse `scripts/data/approvals.py`.
- The configurator's agent and skill text describe how to apply each form.

**Tech Stack:**
- Python 3 stdlib only: `xml.etree.ElementTree` with DTD/entity rejection; repo tests
  run with system `python3`.
- bash, make and a C++/OpenMP compiler for the build skill.

**Spec:** `docs/superpowers/specs/2026-11-16-data-to-physicell-design.md`

## Global Constraints

**Scope**
- No PhysiCell runs, no PhysiCell calibration, no MCP server changes.
- Units are never inferred. Conversions are explicit and approved; transforms are
  `identity`, `reciprocal`, `ln2_over` and `unit_factor`, each with a positive `factor`.

**Governance stages and `DATA_INPUT` forms**
- New governance stages: `physicell_design`, `physicell_projection`.
- New JSON kinds: `"physicell_design"`, `"physicell_projection"`,
  `"physicell_dictionary"`, `"physicell_verification"`, each with `schema_version: 1`.
- multicellular-configurator `DATA_INPUT` is line 2, after `STAGE_GATE`. Exactly one of
  these key sets:
  - `{"design", "sha256", "approval", "record_session_id"}`;
  - `{"projection", "sha256", "approval", "record_session_id"}`.

**Paths**
- `runs/orchestrator/<p>/physicell_design.json`
- `runs/orchestrator/<p>/physicell_dictionary.json`
- `runs/orchestrator/<p>/physicell_projection.json`
- `runs/orchestrator/<p>/physicell_verification.json`
- `runs/orchestrator/physicell-build/<UTC>-<template>/report.{json,md}`
- Captured configurations: `runs/multicellular-configurator/<session>/configs/<capture>/` (from `record_physicell_config.py`).
- Invocation records: `runs/multicellular-configurator/<session>/invocation/<id>/tool_calls.json`.

**Server tool arguments (2.5.2)**
- `add_physiboss_input_link(cell_type, physicell_signal, boolean_node, action∈{activation,inhibition}, threshold: float, smoothing: int≥0)`
- `add_physiboss_output_link(cell_type, boolean_node, physicell_behavior, action, value: float, base_value: float, smoothing: int≥0, steepness: int≥0|None)`
- `add_single_cell_rule(cell_type, signal, direction∈{increases,decreases}, behavior, saturation_value: float, half_max: float>0, hill_power: float>0)`
- `list_all_available_signals(session_id)`, `list_all_available_behaviors(session_id, cell_type)`
- Spec §5.1's `apply_to_dead` is not a server argument in 2.5.2 and is not implemented.

**Numbers**
- Stored-value comparison and verification use relative tolerance `1e-9`.
- Unit strings are compared exactly (no unit algebra).

**Build**
- Default PhysiCell version `1.14.2`; templates `template` and `template_BM`.
- Default dest `~/PhysiCell`; the folder is `PhysiCell-<version>-<template>`.
- Executable `project`.
- Never run bare `make` before `make <template>`. Never run the executable.

**Conventions**
- Reuse `scripts/data/common.py` (`strict_json`, `sha256_file`, `reject_symlinks`,
  `finite_number`), `scripts/data/sources.py` and `scripts/data/approvals.py`
  (`_require_artifact_approval`, `validate_record` through the state module).
- XML parsing rejects DTD/entities (reuse `DeclarationFreeTreeBuilder` from
  `scripts/capture/record_physicell_config.py`).
- The hook stays fail-closed: Claude Code blocks only on exit 2.
- `/tmp` is a small RAM tmpfs: scratch goes under `~/.cache`.
- Commits end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

1. **Settings paths matching zero or several elements.** For example `rate` without
   `start_index`, or a duplicate `cell_definition` name. Expected: refused, naming the
   count (Task 1 test).
2. **Unit strings that differ only in spelling.** For example `1/min` vs `min^-1`, or
   `micron/min` vs `µm/min`. Expected: refused as a mismatch with both strings shown,
   never silently equated (Task 4 test).
3. **Signal and behavior names with spaces or odd case.** For example
   `"contact with luad_egfr"` or `"cycle entry"`. Expected: an exact match against the
   captured dictionary; a near-miss lists the closest names (Task 4 test).
4. **A dictionary capture from a failed or partial tool call.** For example an error
   result, a missing per-cell-type behavior list, or a list obtained before the last
   `add_single_cell_type`. Expected: the capture is refused, naming the missing piece
   (Task 3 test).
5. **A design citing a MaBoSS handoff that is pending or superseded.** Expected: check
   and gate both refuse (Task 2 and Task 5 tests).

---

### Task 1: Settings-XML helpers and capture binding

**Files:**
- Create: `scripts/physicell/__init__.py`
- Create: `scripts/physicell/settings_xml.py`
- Test: `tests/data/test_physicell_xml.py`, with fixtures in `tests/data/fixtures/physicell/`
  (a small exported `PhysiCell_settings.xml` with 2 cell definitions, 2 substrates and
  `units` attributes; a rules CSV).

**Interfaces:**
- Produces:
  - `load(path: Path) -> ET.Element`: rejects DTD/entities; root must be `PhysiCell_settings`.
  - `resolve(root: ET.Element, path: str) -> ET.Element`. The path is ElementTree
    XPath-subset syntax, relative to the root. It raises `ValueError` unless exactly one
    element matches (message: `path <p> matches <n> elements`).
  - `units(element) -> str | None`.
  - `normalise(root) -> list[tuple[str, dict, str]]`: canonical (path, attributes,
    stripped text) records, ordered.
  - `diff(before_root, after_root) -> list[dict]`: `{path, before, after}` per changed,
    added or removed element.
  - `cell_definitions(root) -> list[str]`, `substrates(root) -> list[str]`,
    `cycle_model(root, cell_definition) -> str`.
  - `capture(root: Path, capture_rel: str) -> dict`: binds a captured configuration
    folder under `runs/multicellular-configurator/<s>/configs/<c>/`. Returns `{session,
    capture, xml_path, xml_sha256, rules_path|None, rules_sha256|None}`, verified
    against the capture's own manifest written by `record_physicell_config.py` (read
    that file to learn its manifest name and fields). Refuses symlinks and changed
    files.

- [ ] **Step 1: Write failing tests.**
  - `test_resolve_exactly_one`: `rate` without `start_index` matches 2 → ValueError
    "matches 2 elements"; with `[@start_index='0']` → one element.
  - `test_units_attribute_read`.
  - `test_dtd_refused`.
  - `test_diff_reports_changed_added_removed_ignoring_order_and_whitespace`.
  - `test_capture_binding_refuses_changed_xml`.
  - `test_capture_must_be_under_configs`.
- [ ] **Step 2: Run** `python3 -m unittest tests.data.test_physicell_xml -v`. Expected: FAIL (ImportError).
- [ ] **Step 3: Implement** `settings_xml.py`.
- [ ] **Step 4: Run** the tests, then `python3 scripts/run_tests.py`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(physicell): settings XML helpers and capture binding`.

### Task 2: Stage A, `physicell_design` draft/check and approval

**Files:**
- Create: `scripts/physicell/physicell_design.py`
- Modify:
  - `scripts/state.py` (`GOVERNANCE_STAGES` += `physicell_design`, `physicell_projection`);
  - `scripts/data/approvals.py` (`require_physicell_design_approval`, `require_physicell_projection_approval`).
- Test: `tests/data/test_physicell_design.py`

**Interfaces:**
- Consumes: Task 1 `capture`, `cell_definitions`, `substrates`.
- Produces:
  - **Design schema:** `"kind": "physicell_design"`, `schema_version: 1`, plus:
    - `basis`: `{capture}` or `null`;
    - `domain` (required when `basis` is null): `{x_min, x_max, y_min, y_max, z_min, z_max, dx, dy, dz, units: "micron", max_time, time_units: "min"}`;
    - `cell_definitions`: `[{name, cycle_model, description, from, variant_of?}]`;
    - `substrates`: `[{name, units, description, from}]`;
    - `intracellular`: `[{cell_definition, maboss: {handoff, sha256}}]`;
    - `coupling_plan`: `[{cell_definition, behavior_group, route ∈ {physiboss, rules, none}, notes}]`;
    - `sources`, `data`, `unmapped`.
  - `check(root, rel) -> list[str]` and `load_and_check(root, rel) -> (data, sha256, errors)`.
  - `draft(root, purpose, *, basis_capture: str|None, sources, data) -> Path`. Draft
    refuses to overwrite; with a basis, it pre-fills cell definitions and substrates
    from it, with reason TODO.
  - CLI: `python3 scripts/physicell/physicell_design.py draft|check`.
  - `cycle_model` keys are checked against a `cycle_models` list recorded in the design
    (from the configurator's `get_available_cycle_models` discovery). When basis is null
    and the list is absent, check refuses with "record the server's cycle models first".
  - `approvals.require_physicell_design_approval(root, record_id, rel, sha256) -> dict`:
    the artifact binding, `validate_record`, `outputs` equal to the set of cell
    definition and substrate names, and `session_id` equal to the basis capture session
    (or the design's declared PhysiCell session for a new configuration, carried as
    `session`).
  - Each MaBoSS handoff must be an exported, approved `maboss-to-physicell` handoff.
    Reuse the gate's existing handoff validation for STAGE_GATE multicellular (read
    `gate.check`/`check_task` to find the function that validates an exported
    maboss-to-physicell handoff and its approval; call it, do not duplicate it).

- [ ] **Step 1: Write failing tests:**
  - complete design passes;
  - duplicate cell-definition name refused;
  - unknown cycle model refused;
  - coupling plan naming an undeclared cell definition refused;
  - a pending or superseded MaBoSS handoff refused (Review Focus 5);
  - `basis: null` without `domain` refused;
  - domain units other than micron/min refused;
  - TODO reason refused;
  - approval outputs mismatch refused;
  - approval without `approved_by` refused (`validate_record`).
- [ ] **Step 2: Run** the new test module. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** it and `python3 scripts/run_tests.py`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(physicell): stage A design draft/check and approval`.

### Task 3: Dictionary capture

**Files:**
- Create: `scripts/physicell/capture_dictionary.py`
- Test: `tests/data/test_physicell_dictionary.py`, with a fixture `tool_calls.json`
  holding `tool_use`/`tool_result` blocks.

**Interfaces:**
- Consumes:
  - the invocation record `runs/multicellular-configurator/<s>/invocation/<id>/tool_calls.json`
    (tool names `mcp__physicell-agent__list_all_available_signals` and
    `mcp__physicell-agent__list_all_available_behaviors`; check the exact prefix in
    `.claude/agents/multicellular-configurator.md`);
  - Task 1 `capture`.
- Produces:
  - `capture_dictionary(root, invocation_rel, capture_rel, design_rel) -> Path`. Writes
    `physicell_dictionary.json` next to the design: `{"kind": "physicell_dictionary",
    schema_version, session, invocation, capture: {path, sha256}, signals: [..],
    behaviors: {cell_definition: [..]}, source_sha256 (tool_calls.json)}`.
  - The last successful `list_all_available_signals` call, and per cell definition the
    last successful `list_all_available_behaviors(cell_type=…)` call, must come after
    the last structure-changing call (`add_single_cell_type`, `add_single_substrate`,
    `import_maboss_handoff`) in the same invocation.
  - Every design cell definition needs a behavior list. Error results are refused.
  - CLI: `python3 scripts/physicell/capture_dictionary.py <invocation> <capture> <design>`.

- [ ] **Step 1: Write failing tests:**
  - a happy path;
  - an error result refused;
  - a list obtained before the last `add_single_cell_type` refused (Review Focus 4);
  - a missing per-cell-type behavior list refused;
  - a capture from another session refused.
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the tests and `python3 scripts/run_tests.py`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(physicell): capture the live signal/behavior dictionary`.

### Task 4: Stage B, `physicell_projection` draft/check and approval

**Files:**
- Create: `scripts/physicell/physicell_projection.py`
- Test: `tests/data/test_physicell_projection.py`

**Interfaces:**
- Consumes: Task 1 helpers, Task 3 dictionary, MaBoSS handoff readouts and nodes (the
  handoff's BNET/BND node names and its declared readouts).
- Produces:
  - **Inputs:** `{capture: {path, sha256}, dictionary: {path, sha256}, design: {path,
    sha256, approval}, sources, data}`.
  - **`entries`** (each with `entry_id`, `kind`, `from` and `justification`), one of:
    - `physiboss_input {cell_definition, signal, node, action, threshold:{value, value_unit, transform, factor}, smoothing:int}`;
    - `physiboss_output {cell_definition, node, behavior, action, value:{…}, base_value:{…}, smoothing:int, steepness:int|null}`;
    - `rule {cell_definition, signal, direction, behavior, saturation_value:{…}, half_max:{…}, hill_power:{…}}`;
    - `setting {path, value, value_unit, transform, factor, target_unit?, target_unit_justification?, tool_hint}`.
  - `unmapped` lists unused cited rows.
  - `check` / `load_and_check` / `draft` / CLI, as in Task 2.
  - **Name checks:** exact match against the dictionary (signals; behaviors per cell
    definition) and the handoff nodes and readouts. A near-miss error lists up to 3
    closest names (`difflib.get_close_matches`).
  - **`setting` paths:** resolve exactly one element in the captured XML. The converted
    unit (`target_unit` if present, else derived from the transform: `reciprocal` and
    `ln2_over` invert the unit string as `1/<u>`) must equal the element's `units`
    attribute exactly. A missing attribute requires `target_unit` plus a non-empty
    justification.
  - **Rule domains:** `half_max > 0`, `hill_power > 0`, `saturation_value ≥ 0`.
    Integers for `smoothing`/`steepness`.
  - **Uniqueness:** one rule per (cell_definition, signal, behavior); each `setting` path
    and each link at most once.
  - `approvals.require_physicell_projection_approval(root, record_id, rel, sha256) ->
    dict`: binding, `validate_record`, `outputs` equal to the entry ids, `session_id`
    equal to the capture session.

- [ ] **Step 1: Write failing tests** for each entry kind (happy path) plus:
  - unknown signal with a close-match hint (Review Focus 3);
  - a path matching 2 elements;
  - a unit spelled `min^-1` against `1/min` refused (Review Focus 2);
  - `ln2_over` of a doubling time in `h` with factor `1/60` gives `1/min`;
  - a missing `units` attribute without justification refused;
  - a duplicate rule refused;
  - `half_max ≤ 0` refused;
  - a non-integer `steepness` refused;
  - approval mismatches refused.
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the tests and `python3 scripts/run_tests.py`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(physicell): stage B projection draft/check and approval`.

### Task 5: Gate forms and configurator instructions

**Files:**
- Modify:
  - `scripts/gate.py`: `DATA_INPUT_LINE["multicellular_configurator"] = (1, "multicellular-configurator DATA_INPUT must be line 2, after STAGE_GATE")`;
    `_check_physicell_input` for both forms; `_model_input_kind` recognises the new
    kinds for the mention rule.
  - `.claude/scripts/stage_gate_guard.py`: the multicellular-configurator joins the
    DATA_INPUT roles; `check_task` still runs.
- Modify (docs): `.claude/agents/multicellular-configurator.md` and
  `.claude/skills/physicell-workflow/SKILL.md`, plus any byte-identical Codex copy that
  an equality test requires.
- Test: `tests/codex/test_data_input_gate.py` (a new class), pins in
  `.claude/scripts/test_agent_mcp_access.py`.

**Interfaces:**
- Consumes: Task 2 and Task 4 approvals and checks.
- Produces gate checks:
  - **Design form:** the approval; the design re-checked; `record_session_id` equals the
    approval session; each design MaBoSS handoff is the STAGE_GATE upstream when the
    STAGE_GATE uses `upstream_session_id`.
  - **Projection form:** the approval; the projection re-checked; the session; the
    projection's design is approved and its capture session equals `record_session_id`.
  - **Mention rule:** other design or projection files named in prose are refused.

- **Agent and skill text:**
  - Apply exactly the approved design (create the declared substrates and cell
    definitions, import each handoff onto its cell definition). Then call the discovery
    tools for the signal list and for every cell definition's behavior list, so the
    invocation record holds them. Then export.
  - For a projection, apply exactly the entries with the matching tools, compare each
    returned stored value (rel 1e-9), return `blocked` on any difference, then export.
  - Never infer units; take names and values only from the verified files named on the
    `DATA_INPUT` line.

- [ ] **Step 1: Write failing real-hook tests:**
  - both forms accepted;
  - wrong sha refused;
  - a wrong stage approval refused;
  - `record_session_id` mismatch refused;
  - a design handoff not equal to the STAGE_GATE upstream refused (Review Focus 5);
  - extra or missing keys refused;
  - a payload-like mention in prose refused;
  - existing multicellular STAGE_GATE-only briefs still pass.
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement** the gate and docs.
- [ ] **Step 4: Run** `python3 scripts/run_tests.py`, `python3 tests/integration/data_smoke.py` and `python3 scripts/check_isolation.py --repository-only`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(gate): multicellular-configurator design and projection DATA_INPUT`.

### Task 6: Export verification

**Files:**
- Create: `scripts/physicell/verify_export.py`
- Test: `tests/data/test_physicell_verify.py`

**Interfaces:**
- Consumes: Task 1 `diff`/`resolve`/`capture`, Task 4 projection.
- Produces: `verify(root, projection_rel, new_capture_rel) -> Path`. It writes
  `physicell_verification.json`: `{"kind": "physicell_verification", schema_version,
  projection: {path, sha256}, before: capture, after: capture, entries: [{entry_id,
  expected, found, ok}], unexpected_changes: [...], passed: bool}`. Every entry's
  expected model value must appear (rel 1e-9):
  - `setting` entries: in the XML element;
  - links: in the PhysiBoSS XML sections;
  - rules: in the rules CSV, using the CSV columns the server exports (read
    `export_cell_rules_csv`'s format from a fixture).

  Any other difference versus the before-capture is unexpected. The new capture must
  derive from the same session lineage. CLI exits 1 when `passed` is false.

- [ ] **Step 1: Write failing tests:**
  - all applied → passed;
  - one entry missing → failed and named;
  - an extra unrelated change → failed and listed;
  - order and whitespace differences ignored;
  - a rule value checked in the CSV.
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the tests and `python3 scripts/run_tests.py`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(physicell): verify the exported settings against the projection`.

### Task 7: PhysiCell build script and skill

**Files:**
- Create: `scripts/physicell/build.py`, `.claude/skills/physicell-build/SKILL.md`
- Test: `tests/data/test_physicell_build.py` (fake `make`/`curl`/compilers on `PATH`)

**Interfaces:**
- Produces:
  - CLI `build.py --template template|template_BM [--version 1.14.2] [--dest DIR] [--settings XML --rules CSV] [--jobs N]`.
  - `toolchain(env) -> dict`: `{compiler, compiler_version, openmp: true, make, fetch: "curl"|"git", python3}`. It refuses with a one-line remedy; with Apple clang only, the remedy is `brew install gcc` and `export PHYSICELL_CPP=g++-NN`.
  - **Download:** `https://github.com/MathCancer/PhysiCell/archive/refs/tags/<version>.tar.gz`
    into `<dest>/PhysiCell-<version>-<template>/`, recording its sha256. It refuses an
    existing folder.
  - **Build:** `make <template>`, then `make -j N`. It requires `project` and records its
    sha256.
  - **Settings copy:** only from a captured configuration whose verification
    (`physicell_verification.json`) passed. It backs up the template's
    `config/PhysiCell_settings.xml` and `config/cell_rules.csv` first.
  - **Report:** `report.json`/`report.md` under
    `runs/orchestrator/physicell-build/<UTC>-<template>/`, including the documented run
    command `./project config/PhysiCell_settings.xml`. The command is never executed.
  - **Skill:** when to use it, which template to choose, internet needed for
    `template_BM`, and relay the report.

- [ ] **Step 1: Write failing tests:**
  - toolchain detection with a fake `g++` that compiles OpenMP;
  - Apple clang only → the remedy message;
  - a missing `make` refused;
  - the dest folder exists → refused;
  - a failure in `make` → report written with the failing step and its tail, folder kept;
  - a settings copy from an unverified capture refused;
  - the run command is never executed (assert no subprocess call runs `./project`).
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the tests and `python3 scripts/run_tests.py`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(physicell): build skill for template and template_BM`.

### Task 8: Smoke test, docs and roadmap link

**Files:**
- Create: `tests/integration/physicell_smoke.py`
- Modify:
  - `docs/data-workflow.md` and `docs/agentic-operations.md` (CLI forms, DATA_INPUT forms);
  - `CLAUDE.md`: a short paragraph on design → dictionary → projection → verify;
  - `README.md`: link `ROADMAP.md`;
  - `ROADMAP.md`: mark 1.2.0 contents accurately.

**Interfaces:** consumes everything above.

- [ ] **Step 1: Write the smoke test.** It prints `[ok]` lines and ends with
  `PHYSICELL SMOKE PASSED`, or `SKIP: <reason>` (exit 0) without network or an OpenMP
  toolchain. Scratch goes under a `mktemp -d` in `~/.cache`, removed on success. Steps:
  1. `build.py --template template` into scratch; `project` exists, and the report says
     it was not run.
  2. In a temp repo copy with fixture captures: design draft → fill → check → approval;
     dictionary capture from a fixture invocation record; projection draft → fill →
     check → approval.
  3. Real hook: both `DATA_INPUT` forms accepted, and a wrong sha refused.
  4. `verify_export` against a hand-applied fixture "after" capture → passed.
  5. A settings copy into the built project from the verified capture.
- [ ] **Step 2: Run it.** Expected: `PHYSICELL SMOKE PASSED` (or SKIP offline).
- [ ] **Step 3: Run** `python3 scripts/run_tests.py`, `python3 tests/integration/data_smoke.py`, `python3 tests/integration/ode_calibration_smoke.py` and `python3 scripts/check_isolation.py --repository-only`. Expected: OK.
- [ ] **Step 4: Commit** `test(physicell): end-to-end smoke; docs and roadmap link`.
