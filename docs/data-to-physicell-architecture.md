# Data to PhysiCell settings: architecture and design rationale

**Document status:** describes how approved data reach PhysiCell settings through an approved
design (stage A) and an approved projection (stage B), plus a build skill (Claude Code only,
sub-project 4). Tested with Claude Code 2.1.311, `mcp-biomodelling-servers` 2.5.2 (PhysiCell
and MaBoSS servers) and PhysiCell 1.14.2. For the step-by-step procedure, see
[data workflow](data-workflow.md#data-to-physicell); for the typed handoffs and approvals this
extends, see the [data-to-model handoffs](data-to-model-handoffs-architecture.md) and
[data-to-ODE calibration](data-to-ode-calibration-architecture.md) architectures. Planned work
is in [ROADMAP.md](../ROADMAP.md). Where this document and the code disagree, the code is
authoritative.

## 1. Purpose and scope

A PhysiCell model is shaped mostly by structure: which cell definitions and substrates exist,
which cell definition carries which MaBoSS model, and how signals reach behaviors. The
signal/behavior dictionary exists only once that structure exists. So data enter in two
researcher-approved stages. **Stage A (design)** fixes the structure; the multicellular-configurator
builds it and the live dictionary is recorded. **Stage B (projection)** turns data into couplings
and values, checked against that dictionary and the exported XML, applied, and verified. No value
reaches PhysiCell through brief prose.

**In scope:** the design and projection files with their checks and approvals; the dispatch-hook
forms; dictionary capture with stage-A conformance; export verification; the `physicell-build`
skill; `ROADMAP.md`.

**Not covered here:** running or calibrating PhysiCell; spatial initial conditions; ODE-derived
proposals and direct ODE coupling; Codex parity (Codex returns `blocked`); MCP server changes.

## 2. Key decisions and rationale

| Decision | Rationale |
|---|---|
| **Two approved stages; settings only** | Names of signals and behaviors (phase exits follow each cell's cycle) exist only after cell definitions and substrates do, so structure is approved and built first. No stage runs a simulation. |
| **The design carries every build argument, each sourced (T9-a)** | `create_simulation_domain`, `add_single_substrate` and `add_single_cell_type` all have defaults. "Never invent parameter values" means the configurator passes every argument from the design, never a tool default or a typical value, and returns `blocked` if one is missing. The domain is in server terms (`domain_x`, `domain_y`, `use_2d`, `domain_z`, `dx`, `max_time`); `cycle_models` is the `[{key, name}]` list copied verbatim from a discovery run, because the XML writes the cycle name, not the key. |
| **Stage-A conformance before the dictionary (T9-b)** | Nothing else proves the configurator built what was approved. `capture_dictionary.conformance` compares the stage-A capture with the design and names every mismatch; the projection check runs it again, so the hook enforces it at stage-B dispatch. |
| **`record_session_id` is a lineage anchor, never a live session (T5-c, T8-c)** | MCP sessions end with each run and every configurator run is a new session. A design is approved before stage A exists, so it cannot name the stage-A session (the real-server smoke exposed this). The design's anchor is the basis capture's session, else the purpose `<p>`; captures are bound to their approved file by dispatch lineage instead. |
| **Stage B replays the design; it does not load the stage-A export (T5-e)** | Server probes: loading the captured package XML into a new session fails capture (its `./config` files are missing from the new session folder); loading the server's stage-A export fails capture (its files sit outside the new session's source root); `load_xml_configuration` keeps MaBoSS links but drops the rules CSV rows (T5-d). Replaying the design calls captured cleanly; the stage-A/stage-B diff held only the added content, with `.bnd`/`.cfg` byte-identical. |
| **One MaBoSS session per design (T5-a)** | `STAGE_GATE` has one `upstream_session_id`, and the gate binds every design handoff to it. A standalone gate means no handoff. Designs mixing sessions are refused, naming them. |
| **Units are exact, never inferred (P6, T4-a, T9-a)** | A factor changes the unit, so a converted unit is derived only for factor 1 and a non-compound unit, and a `target_unit` given there must equal that derived unit; a conversion (factor != 1 or `unit_factor`) requires `target_unit`, the researcher's approved conversion; factor 1 with a compound reciprocal is refused (use a factor or `unit_factor`). Settings and rule and link quantities alike. Units must equal the XML string exactly (no spelling normalisation). Design units are those the export writes; a probe showed server 2.5.2 writes `mmHg` for an added substrate's initial and Dirichlet values whatever its label, so the label must be `mmHg` too. |
| **Model values are materialised (T9-e)** | The configurator has no Bash and its arithmetic would be unchecked before use. Every quantity carries `model_value`; the check recomputes it (relative 1e-12) and prints the value to paste; the configurator applies it verbatim and compares the stored value (relative 1e-9). Verification recomputes `model_value(q)` independently. |
| **Projection flexible, one writer per behavior (P3, T4-d)** | Any numeric leaf of the exported XML may be targeted, so no fixed target list. Safety comes from names, paths and units checked against the capture, and one writer per (cell definition, behavior): one rule or one output link, never both, counting the capture's own links. Values must take effect, and verification compares files, not runtime behaviour. |
| **Verification compares files by content (T6-c, T6-e)** | Only the entries' changes may differ from stage A. Rulesets are compared by content, because adding one shifts positional paths (probe). Each attempt writes its own file, so a failed attempt stays an artifact. |
| **`build.py` recomputes, never trusts, the verification (T9-d)** | A file saying `passed` proves nothing; the build re-runs `verify_export.compute` on the file's projection and after capture. |
| **Filtered archive extraction (T7-b, T7-c)** | The PhysiCell archive is untrusted (trust on first use). Review showed `tar` allowed symlink and hardlink escapes. Extraction uses Python `tarfile` with a member pre-scan and `filter="data"`, and is refused on a Python without `tarfile.data_filter`. |

## 3. Components

| Component | Role |
|---|---|
| `scripts/physicell/settings_xml.py` | Binds a capture (`capture`: path + sha256 of the manifest, XML, rules CSV and dependencies), resolves setting paths, normalises and diffs XML, reads substrate facts. |
| `scripts/physicell/physicell_design.py` | `draft` and `check` for `physicell_design.json`. |
| `scripts/physicell/capture_dictionary.py` | Stage-A conformance, then records `physicell_dictionary.json` from the invocation record. |
| `scripts/physicell/physicell_projection.py` | `draft` and `check` for `physicell_projection.json`; `model_value` and `converted_unit`. |
| `scripts/physicell/lineage.py` | `dispatched_with`: which approved file a configurator session was dispatched with. |
| `scripts/physicell/verify_export.py` | `compute` (pure) and `verify` (write-new) of `physicell_verification.<session>.<capture>.json`. |
| `scripts/physicell/build.py` | Toolchain check, download, `make <template>`, optional verified settings copy, compile, report. |
| `scripts/data/approvals.py` | `require_physicell_design_approval`, `require_physicell_projection_approval`. |
| `scripts/gate.py` | `_check_physicell_input` (both `DATA_INPUT` forms), `_check_design_upstream`; design, projection and dictionary files named only on that line. |
| `scripts/state.py` | Governance stages `physicell_design` and `physicell_projection`. |
| `.claude/agents/multicellular-configurator.md`, `.claude/skills/physicell-workflow/SKILL.md` | Discovery run, building a design, replay plus entries, `blocked` conditions. |
| `.claude/skills/physicell-build/SKILL.md` | Orchestrator-only build skill. |
| `.claude/agents/scientific-reviewer.md`, `.claude/agents/reproducibility-auditor.md` | PhysiCell data-stage checklists. |

## 4. Flow

The procedure, with every command, is in [data workflow](data-workflow.md#data-to-physicell). In
outline:

1. **Discovery** (new configuration only): configurator with `STAGE_GATE`, no `DATA_INPUT`, calls
   `get_available_cycle_models`; its recorded output is copied into `cycle_models`.
2. **Stage A.** `physicell_design.py draft|check` -> `physicell_design` approval -> configurator
   with the design `DATA_INPUT` line: a new session builds the design from its values, lists signals
   and behaviors, exports -> `recover.py` and capture -> `capture_dictionary.py` (conformance, then
   the dictionary) -> validate-stage on the stage-A capture.
3. **Stage B.** `physicell_projection.py draft|check` -> `physicell_projection` approval ->
   configurator with the projection `DATA_INPUT` line: a new session replays the design, applies
   only the entries' `model_value`s, exports (rules CSV before XML) -> `recover.py` and capture ->
   `verify_export.py` -> validate-stage on the verified capture -> optional `build.py`.

## 5. Contracts

- **Design** (`kind: physicell_design`): `basis` (`{capture}` or null), `session` (the anchor),
  `domain` (null with a basis; lengths `micron`, `max_time` `min`, `domain_z` null iff `use_2d`),
  `cycle_models`, `cell_definitions` (`cycle_model` key, optional `variant_of`, no cycles),
  `substrates` (`diffusion_coefficient` `micron^2/min`, `decay_rate` `1/min`, `initial_condition`
  and `dirichlet {enabled, value}`, each `{value, unit, from}`), `intracellular`
  (`{cell_definition, maboss: {handoff, sha256}}`), `coupling_plan` (route `physiboss`, `rules`
  or `none`), `sources`, `data`, `unmapped`. Every basis cell definition and substrate is declared
  `from: "basis"`. Each handoff passes the gate's own `gate.check("multicellular", ...)` and its
  target `cell_type` must be the carrying cell definition. `TODO` anywhere is refused.
- **Stage-A conformance:** the cell-definition set and cycle names; the substrate set, label,
  values, units and Dirichlet settings (the library default `substrate` accepted only untouched,
  in a new configuration); domain extents, `dx`/`dy`/`dz`, `use_2D`, units and `max_time` (with a
  basis: the basis capture's); MaBoSS models on exactly the declared carriers, each carrier's
  captured `.bnd`/`.cfg` dependency sha256 equal to its design handoff manifest's `bnd_file`/`cfg_file`
  sha256 (import copies them verbatim: a stale, other or swapped export is refused); no PhysiBoSS link
  (import writes none); no enabled rules ruleset.
- **Dictionary** (`kind: physicell_dictionary`, write-new): session (= the capture's), invocation,
  capture and design bindings, signals, per-cell behaviors, `source_sha256` of `tool_calls.json`.
  Only list calls after the last structure-changing call's result count. Structure-changing is every
  PhysiCell tool outside an explicit read-only set (`capture_dictionary.READ_ONLY`, from server 2.5.2:
  the `readOnlyHint` tools plus `export_xml_configuration` and `export_cell_rules_csv`), so an unknown or
  newly added tool is a boundary (fail closed).
- **Projection** (`kind: physicell_projection`): `capture`, `dictionary`, `design` (with its
  approval), `sources`, `data`, `entries`, `unmapped`. Entry kinds: `physiboss_input`,
  `physiboss_output`, `rule`, `setting`. A quantity is `{value, value_unit, transform, factor,
  target_unit?, model_value}`; transforms `identity`, `unit_factor` (v x factor), `reciprocal`
  (factor/v), `ln2_over` (factor x ln2/v). A `setting` path resolves to one numeric leaf, never
  inside a cell's `phenotype/intracellular`; with no `units` attribute it needs `target_unit` and a
  justification. Rule values: `half_max` > 0, `hill_power` > 0, `saturation_value` >= 0. The check
  also re-derives the dictionary lists from the hashed record and re-runs conformance.
- **Verification** (write-new, one per attempt): projection, `before`, `after` (verbatim
  `settings_xml.capture` bindings), `lineage.invocation`, per-entry expected/found, `unexpected_changes`,
  `passed`. Rule rows are CBHG v3 with `apply_to_dead` 0; one enabled ruleset (`./config`, the
  rules CSV) iff there are rule entries. CLI exit 0 passed, 1 failed, 2 refused.
- **Approvals:** `physicell_design` (`session_id` = the anchor, `outputs` = cell definition and
  substrate names) and `physicell_projection` (`session_id` = the capture's session, `outputs` =
  entry ids), each binding path and sha256 and passing `validate_record`.
- **`DATA_INPUT`** (line 2, after `STAGE_GATE`): `{"design", "sha256", "approval",
  "record_session_id"}` or `{"projection", "sha256", "approval", "record_session_id"}`.

## 6. Bindings and governance

- **Path + sha256 everywhere:** design and projection in their approvals and `DATA_INPUT` lines;
  capture manifests in the dictionary and projection; dictionary and design in the projection;
  every capture file re-verified against its `capture.json` on each read.
- **Anchor:** the design's `session` is the basis capture's session, else `<p>`, and equals the
  approval's `session_id` and the `record_session_id`. A projection's anchor is its capture session.
- **Dispatch lineage:** `lineage.dispatched_with` finds, in
  `runs/multicellular-configurator/<session>/invocation/*/task.txt`, a line 2 that is exactly the
  design or projection `DATA_INPUT` form naming that path and sha256, under a well-formed line-1
  `STAGE_GATE` (`gate.stage_gate_form`), whose `approval` is a real approval of the matching stage binding
  exactly that path and sha256 (`approvals.require_physicell_design_approval` /
  `require_physicell_projection_approval`) and whose `record_session_id` is that approval's `session_id`
  (a run the hook never verified, e.g. under the Codex runtime, is no lineage). `capture_dictionary.py`
  requires it in the same invocation folder as the parsed `tool_calls.json`; the projection check
  binds its capture to the design; `verify_export.py` binds the stage-B capture (a session other
  than stage A's) to the projection.
- **Gate:** the file is re-checked on exactly the approved bytes, then the approval and
  `record_session_id`. Design form: with `upstream_session_id`, every handoff lies under
  `runs/boolean-dynamics-modeler/<that session>/` (at least one); with `standalone_decision_id`,
  none. Projection form: `record_session_id` = the capture session; its design and approval are
  re-checked, and the design's handoffs are bound to the `STAGE_GATE` exactly as in the design form
  (stage B replays the design). The line-1 `STAGE_GATE` check is unchanged and runs once per dispatch:
  the hook reuses the `check_task` result these bindings produced (`gate.check_data_input_and_stage_gate`).
- **Build:** `--verification` must be a passed `physicell_verification` whose after capture re-binds
  byte for byte and whose recomputed `entries`, `unexpected_changes`, `before`, `after` and
  `projection` equal the file's. The packaged XML and dependencies are copied into `config/`
  (replaced files kept as `.template-backup`); MaBoSS settings need `template_BM`. The report
  records the verification sha256 and never runs `./project`.
- **Registry** (CLAUDE.md): stage-A row `Derived from` = the `STAGE_GATE` upstream MaBoSS session
  or standalone decision id, plus the basis capture's session with a basis; Artifacts cite the
  design path and short sha256. Stage-B row `Derived from` = the stage-A session; Artifacts cite the
  projection and the verification file. Applied values are tagged `data-derived`.
- **Review:** validate-stage on the stage-A capture before a projection is drafted, and on the
  verified stage-B capture before build or closure, with the reviewers' PhysiCell checklists.

## 7. Testing

- **Unit tests** (stdlib, `python3 scripts/run_tests.py`): `tests/data/test_physicell_xml.py`,
  `test_physicell_design.py` (incl. design approvals), `test_physicell_dictionary.py` (incl.
  conformance), `test_physicell_projection.py`, `test_physicell_verify.py` (incl. forged and stale
  files refused by `build.py`), `test_physicell_build.py` (fake compilers, malicious archives);
  `tests/codex/test_data_input_gate.py` (both hook forms); `.claude/scripts/test_agent_mcp_access.py`
  (agent and skill text pins). Fixtures in `tests/data/fixtures/physicell/` were checked against a
  real `physicell_config` 0.6.3 export.
- **Smoke** (`python3 tests/integration/physicell_smoke.py`, in a repo copy under `~/.cache`):
  a real PhysiCell 1.14.2 `template` build; the fixture design -> dictionary -> projection chain
  through the CLIs and the real hook; and with the real servers in-process, a MaBoSS handoff, stage
  A from the design's values (conformance passes; a decay-tampered copy is refused), stage B by
  replay plus a rule and an input link, `verify_export.py` passed, tampered captures failed (changed
  decay, ruleset folder `./elsewhere`), and the verified settings copied into the built project.
- **SKIP conditions:** the build and settings-copy steps skip with `PHYSICELL_SMOKE_SKIP_BUILD=1`,
  without GitHub access or without an OpenMP toolchain. The real-server step skips without `uv`, when
  `uv run --with mcp-biomodelling-servers==2.5.2` fails, or when its modelling venv cannot be set up.

## 8. Known limitations

Server 2.5.2 (each listed in ROADMAP "Later"):
- **No basis with input files.** It cannot reload rules CSVs, MaBoSS files or initial cells into a
  new session, so a design basis may have none and a projection's capture has no enabled rules. A
  revised projection is re-applied from the stage-A capture.
- **Added substrates use `mmHg`.** The server writes `mmHg` for initial and Dirichlet values.
- **The library default `substrate` is always present.** No tool removes or edits it; conformance
  accepts it untouched, with values hard-coded from the probe (a server change fails closed).
- **One MaBoSS session per design.**

Also:
- **No `apply_to_dead`.** It is not a 2.5.2 argument; rule rows are written with 0.
- **Conformance checks MaBoSS placement,** not which handoff's `.bnd`/`.cfg` sits on each carrier.
- **Other stage-A phenotype content** is enforced by the agent text and the stage-A review, not
  by conformance.
- **3D domains and basis-domain paths** are tested by fixtures only.
- **A real MaBoSS handoff check needs the configured modelling Python**
  (`.setup/local.json` or `.setup/dispatcher.local.json`); without it a valid handoff is refused.
- **Build:** PhysiCell archive hashes are trust on first use; 1.14.2 is a default pin; no
  Windows-native build. `setting` paths depend on the pinned server's XML layout.
- **Codex has no dispatcher check** for these forms, so it returns `blocked`.

## 9. Roadmap

1. **Implemented:** data to PhysiCell settings with the `physicell-build` skill (planned release 1.2.0).
2. **Next:** spatial data as PhysiCell initial conditions (1.3.0).
3. **Later:** see [ROADMAP.md](../ROADMAP.md).
