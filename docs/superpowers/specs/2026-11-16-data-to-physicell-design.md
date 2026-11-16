# Data to PhysiCell: design, projection and a build skill (sub-project 4): design

**Status:** revised on 2026-11-16 after the researcher's review of the first draft. The
two-stage structure (approved design first, then data projection) was agreed in
conversation. **Builds on:** the
[data-to-model handoffs](../../data-to-model-handoffs-architecture.md) and
[data-to-ODE calibration](../../data-to-ode-calibration-architecture.md) designs (PR #16,
merged). It extends the same gate and approval code.

## 1. Purpose and scope

PhysiCell models are mostly shaped by structural decisions:
- which cell definitions (types or variants) exist;
- which substrates are present;
- which cell definition carries which MaBoSS model;
- how signals reach behaviors.

The PhysiCell signal/behavior dictionary only exists once cell definitions and
substrates exist. The work is therefore done in two approved stages:
- **Stage A, design:** the structure, informed by data and built by the
  multicellular-configurator. Afterwards the live signal/behavior dictionary is
  captured.
- **Stage B, projection:** data become couplings and values (PhysiBoSS links, Hill-rule
  parameters, any other settings field). They are checked against the captured
  dictionary and the exported settings, applied, and verified.

A separate skill builds a PhysiCell project (plain or PhysiBoSS template) for running
approved settings.

**In scope:**
- stage A;
- stage B;
- the build skill;
- `ROADMAP.md` (new, repository root).

**Not in scope:**
- running or calibrating PhysiCell;
- spatial initial conditions (sub-project 5);
- ODE-derived proposals;
- direct ODE coupling;
- Codex parity;
- MCP server changes.

These are listed in `ROADMAP.md`.

## 2. Researcher decisions

| # | Decision |
|---|---|
| P1 | Settings only: no PhysiCell runs and no run calibration. |
| P2 | Two approved stages. The structure (stage A) is approved before any data are projected into couplings or values (stage B). |
| P3 | Flexible projection. The agent reads the data and proposes where they go, by addressing any field the server or the exported settings expose. A fixed list of target kinds is not used. Safety comes from checking every entry against the captured dictionary and the exported XML, including the XML's own `units` attributes. |
| P4 | Both coupling routes are first-class: PhysiBoSS links (signal → input node, output node or readout → behavior) and standard Hill-function rules (signal → behavior). They can coexist in one model. |
| P5 | The build skill pins a PhysiCell release outside the repository and compiles `template` or `template_BM`. It never runs a simulation. |
| P6 | Units are never inferred. Conversions are explicit, approved transforms and factors. |
| P7 | `ROADMAP.md` lists what is done, in progress, planned, blocked and to do, briefly. |

## 3. Facts the design depends on

**PhysiCell server** (mcp-biomodelling-servers 2.5.2, `physicell-workflow` skill):
- **Structure tools:** `add_single_substrate` and `add_single_cell_type`.
- **Patch tools.** These preserve omitted values and return what was stored:
  - `configure_cell_parameters`;
  - `set_substrate_interaction`;
  - `configure_simulation_inputs`;
  - `add_single_cell_rule`;
  - the PhysiBoSS tools `import_maboss_handoff`, `configure_physiboss_settings`, `add_physiboss_input_link`, `add_physiboss_output_link`, `apply_physiboss_mutation`.
- **Discovery:** `list_all_available_signals` and `list_all_available_behaviors(cell_type=…)` give exact names; with `cell_type`, phase-exit behaviors follow that cell's cycle.
- **Export:** `export_xml_configuration`, `export_cell_rules_csv` and `export_configuration_bundle`.
- **Capture:** exported configurations are captured by `scripts/capture/record_physicell_config.py`.
- **Dispatch:** the multicellular-configurator already has `STAGE_GATE` on line 1 (MaBoSS upstream or `standalone_decision_id`).

**PhysiCell XML:** most settings elements carry a `units` attribute (e.g. `1/min`,
`micron/min`, `micron^3`).

**PhysiCell build** (release 1.14.2, master Makefile):
- `make template` populates the plain project.
- `make template_BM` populates the template with PhysiBoSS support; it builds MaBoSS via `addons/PhysiBoSS/setup_libmaboss.py`.
- The executable is `project`.
- Settings live in `config/PhysiCell_settings.xml` and rules in `config/cell_rules.csv`.
- The compiler is `g++ -fopenmp`; macOS needs `PHYSICELL_CPP`.
- The default `all` target populates `heterogeneity-sample`, so `make` is never run bare.

## 4. Stage A: PhysiCell design

### 4.1 `runs/orchestrator/<p>/physicell_design.json`

`"kind": "physicell_design"`, `schema_version: 1`.

**`basis`**, the starting configuration. One of:
- a captured PhysiCell configuration to extend;
- `null` for a new configuration; the domain and time settings are then given in
  `domain`, with explicit units.

**`cell_definitions`.** One entry per cell definition:
- `name`;
- `cycle_model` (a key from `get_available_cycle_models`);
- `description`;
- `from` (refs to sources, data-analyst proposal rows, or earlier approved decisions,
  with locators);
- optional `variant_of` (e.g. an EGFR-mutant variant of a parental type).

**`substrates`.** One entry per substrate:
- `name`;
- `units`;
- `description`;
- `from`.

The library default substrate is reported, never presented as biologically neutral.

**`intracellular`.** One entry per cell definition carrying a MaBoSS model:
- `cell_definition`;
- `maboss`: the approved `maboss-to-physicell` handoff (path and sha256, revision, readouts).

**`coupling_plan`.** Per cell definition, the intended route for each behavior or
behavior group:
- `physiboss` (which output nodes/readouts and input nodes are expected);
- `rules` (which signals);
- `none`.

The concrete names and parameters come in stage B.

### 4.2 Check, approval, build, dictionary capture

**Check.** `scripts/physicell/physicell_design.py draft|check` verifies:
- hashes and sources;
- unique names;
- cycle-model keys against the server's list (recorded in the design by the configurator's discovery run, see below);
- that each MaBoSS handoff is an approved, exported `maboss-to-physicell` handoff;
- that `coupling_plan` names only declared cell definitions and substrates;
- coverage of cited proposal rows.

**Approval.** A `stage_approval` with stage `physicell_design` binds the design's
sha256 (`validate_record`). Its `outputs` equal the cell-definition and substrate names.
The `session_id` is the PhysiCell session, or the basis capture's session for a new
configuration.

**Build.**
- **Dispatch.** The multicellular-configurator brief has `STAGE_GATE` on line 1 and, on
  line 2, `DATA_INPUT {"design": "...physicell_design.json", "sha256": "...", "approval": "<id>", "record_session_id": "<PhysiCell session>"}`.
- **Gate checks** (`multicellular_configurator` joins `DATA_INPUT_LINE` at index 1):
  - the approval;
  - the session;
  - for a MaBoSS upstream, that the design's MaBoSS handoffs are the `STAGE_GATE` upstream.
- **What the configurator does:**
  1. creates exactly the declared substrates and cell definitions;
  2. imports each MaBoSS handoff onto its cell definition;
  3. exports, with no data values yet.

**Dictionary capture.** The configurator also returns, per cell definition, the
server's signal and behavior lists. The orchestrator records them as
`runs/orchestrator/<p>/physicell_dictionary.json`, using a new
`scripts/physicell/capture_dictionary.py` that takes them from the captured invocation
record, not from prose. The file is hashed and bound to the exported configuration
capture.

## 5. Stage B: data projection

### 5.1 `runs/orchestrator/<p>/physicell_projection.json`

`"kind": "physicell_projection"`, `schema_version: 1`.

**Inputs:**
- the stage-A configuration capture (path and sha256);
- `physicell_dictionary.json` (path and sha256);
- `sources`;
- `data` (approved data-analyst proposals).

**`entries`.** Each entry has:
- `entry_id`;
- `kind`, one of the forms below;
- `from` refs;
- `justification`.

**`physiboss_input`:**
- `cell_definition`, `signal` and `node`;
- the link parameters the server accepts (`action`, `threshold`, `smoothing`, …), each with value, unit and source.

**`physiboss_output`:**
- `cell_definition`, `node` (or readout) and `behavior`;
- `action`, `value` and the optional integer `steepness`.

**`rule`** (Hill function):
- `cell_definition`, `signal`, `direction` (`increases`/`decreases`) and `behavior`;
- `saturation_value`, `half_max`, `hill_power` and `apply_to_dead`, each with value, unit, transform and factor.

Example: a dose–response IC50 gives `half_max` and the Hill slope gives `hill_power`.

**`setting`** (any other field):
- `path`, an XPath-like path into the exported `PhysiCell_settings.xml`, e.g.
  `cell_definitions/cell_definition[@name='luad_egfr']/phenotype/cycle/phase_transition_rates/rate[@start_index='0']`;
- `value`, `value_unit`, `transform` (`identity`, `reciprocal`, `ln2_over`, `unit_factor`) and `factor`;
- the tool the configurator should use, as a hint, e.g. `configure_cell_parameters`.

**`unmapped`.** Lists every cited source row or proposal row that is not used, each with
a reason.

### 5.2 Check and approval

`scripts/physicell/physicell_projection.py draft|check` verifies:
- **Names:** every signal, behavior, node and cell definition exists in the captured
  dictionary, the captured MaBoSS handoffs (input/output nodes, readouts) or the
  exported XML.
- **Paths:** every `setting` path resolves to exactly one element of the captured XML.
- **Units:** the entry's converted unit equals that element's `units` attribute. A
  missing `units` attribute requires an explicit, approved `target_unit` with a written
  justification.
- **Rule values:** rule parameters are finite and in domain (`half_max > 0`,
  `hill_power > 0`, saturation ≥ 0).
- **Rules.** Signal and behavior pairs are unique per cell definition.
- **Each target set once.** No element or link may be set by two entries.
- **Coverage.**

A `stage_approval` with stage `physicell_projection` binds the projection's sha256
(`validate_record`). Its `outputs` equal the entry ids and its `session_id` is the
PhysiCell session.

### 5.3 Application and verification

- **Dispatch.** The configurator brief has `STAGE_GATE` on line 1 and, on line 2,
  `DATA_INPUT {"projection": "...physicell_projection.json", "sha256": "...", "approval": "<id>", "record_session_id": "<session>"}`.
  The gate checks the approval, the session and the binding to the stage-A capture.
- **What the configurator does.** It applies exactly the entries through the
  corresponding tools and compares each returned stored value with the requested model
  value (relative 1e-9). On any difference it returns `blocked`. Then it exports.
- **Verification.** `scripts/physicell/verify_export.py <projection> <new capture>`
  checks the new XML and rules CSV. Every entry must hold its mapped value, and nothing
  else may differ from the stage-A capture. The XML is normalised (order and whitespace
  ignored). It writes `physicell_verification.json`.
- **Lineage.** Values are tagged `data-derived` in `DECISIONS.md`. Design decisions cite
  their sources. A new configuration session derives from the previous one.
  `calibrate_cycle.py` is unchanged.

## 6. The `physicell-build` skill

`scripts/physicell/build.py --template template|template_BM [--version 1.14.2] [--dest DIR] [--settings XML --rules CSV] [--jobs N]`, with skill `.claude/skills/physicell-build/SKILL.md`.

1. **Toolchain check before any download.** It needs:
   - `make`, `tar` and `curl` (or `git`);
   - `python3` for `template_BM`;
   - a compiler that builds and links a tiny OpenMP program, tried in this order: `$PHYSICELL_CPP`, `g++`, then `g++-NN` on macOS.

   Apple clang alone stops with the Homebrew fix.
2. **Download.** It fetches the pinned release archive into
   `<dest>/PhysiCell-<version>-<template>/`, with `~/PhysiCell` as the default. It
   records the archive sha256 (trust on first use) and never overwrites.
3. **Build.** It runs `make <template>`, then `make -j N`. `template_BM` needs internet
   for MaBoSS. It verifies that `project` exists and records its sha256.
4. **Optional settings copy.** It copies an approved captured XML and rules CSV
   (verified by `verify_export`) into `config/`, after backing up the template's own.
   Anything else is refused.
5. **Report.** It writes `runs/orchestrator/physicell-build/<UTC>-<template>/report.{json,md}`
   with versions, hashes, compiler, make tail, duration and the documented run command.
   It never runs that command, and keeps the failing step on failure.

## 7. Testing

**Unit tests (stdlib):**
- stage A check: names, cycle keys, MaBoSS handoff approval, coupling plan;
- dictionary capture from a recorded invocation;
- stage B check for every entry form: names against a fixture dictionary and handoff,
  path resolution and `units` attributes against a fixture XML, rule domains, duplicate
  targets, coverage;
- approvals;
- real-hook gate tests for both new `DATA_INPUT` forms (accepted; wrong sha, session or
  upstream; payload in prose), with existing `STAGE_GATE` behaviour unchanged;
- `verify_export` on before/after XMLs;
- `build.py` with fake compilers on `PATH` (macOS clang message, overwrite refusal,
  failure report, settings-copy refusal).

**Smoke test** (`tests/integration/physicell_smoke.py`). It is skipped without network
or an OpenMP toolchain:
1. build `template`;
2. on a fixture configuration and dictionary, run the design and projection
   check → approval → hook → `verify_export` chain.

**Live (researcher):**
- one design and projection applied through the configurator;
- one `template_BM` build with an approved configuration copied in.

## 8. Known limitations

- PhysiCell archive hashes are trust-on-first-use.
- 1.14.2 is a default pin.
- `setting` paths depend on the exported XML layout of the pinned server.
- Values read from figures are only as good as their locators.
- No Windows-native build.

## 9. Order of work

1. Stage A: design, check, approval, gate, configurator instructions, dictionary
   capture.
2. Stage B: projection, check, approval, gate, application instructions,
   `verify_export`.
3. The build script and skill.
4. Smoke test, docs and `ROADMAP.md`.

Before the PR, this spec is condensed into
`docs/data-to-physicell-architecture.md`.
