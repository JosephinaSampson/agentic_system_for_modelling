# Data to PhysiCell settings and a PhysiCell build skill (sub-project 4): design

**Status:** the conversational design was approved on 2026-11-15 (section 5 of the
sub-project 3 brainstorm). This document records it, plus the details fixed while
writing it, for review before planning. **Builds on:** the
[data-to-model handoffs](../../data-to-model-handoffs-architecture.md) and
[data-to-ODE calibration](../../data-to-ode-calibration-architecture.md) designs (PR #16, merged); it extends the same gate and approval code.

## 1. Purpose and scope

Approved measurements become exact PhysiCell settings through the same governed
pattern. The steps are:
1. the orchestrator drafts a translation;
2. the researcher discusses it row by row;
3. a script checks it;
4. the researcher approves it;
5. the multicellular-configurator applies it through the PhysiCell server.

Separately, a skill lets a biologist get a compiled PhysiCell project (plain template
or PhysiBoSS) to run approved settings.

**In scope:**
- (4a) the `physicell_mapping` translation, its check, approval, gated application and
  post-export verification;
- (4b) the `physicell-build` skill and script.

**Not in scope (researcher decisions):**
- running PhysiCell simulations;
- calibrating PhysiCell parameters (another team handles both);
- Codex parity;
- MCP server changes.

## 2. Researcher decisions

| # | Decision |
|---|---|
| P1 | Settings only. Approved data change the exported settings (XML and rules CSV) and nothing else. No run calibration. |
| P2 | Same governance pattern as the MaBoSS/ODE mappings: draft, discussion, check, approval, then `DATA_INPUT` application through the specialist and its server. |
| P3 | A build skill that clones/downloads PhysiCell at a pinned release outside the repository, checks the toolchain, and compiles one of two templates: plain (`template`) or the PhysiBoSS template (`template_BM`). It can optionally copy approved settings into the project's `config/`. Nothing is executed. |
| P4 | Units are never inferred. PhysiCell's fixed units are minutes and microns. Every conversion is an explicit, approved transform or factor. |

## 3. PhysiCell facts the design depends on (PhysiCell 1.14.2, 20 Jan 2025)

- **Server tools** (mcp-biomodelling-servers 2.5.2, `physicell-workflow` skill). All
  use patch semantics: omitted values are preserved, and tools return the stored
  values.
  - `configure_cell_parameters` (cycle transitions `{from_phase, to_phase, rate, fixed_duration}`, death, motility, volume);
  - `set_substrate_interaction` (secretion and uptake rates);
  - `configure_simulation_inputs` (typed user parameters with units, `number_of_cells`);
  - rule tools;
  - `export_xml_configuration`;
  - `export_cell_rules_csv`.
- **Captured configurations.** Exported configurations are captured by
  `scripts/capture/record_physicell_config.py` into a relocatable package. The
  multicellular-configurator's dispatch already requires `STAGE_GATE` on line 1: the
  MaBoSS→PhysiCell upstream, or `standalone_decision_id`.
- **Build facts** (official repository, master Makefile):
  - `make template` populates the plain project. `make template_BM` is "the template
    project of PhysiCell, with PhysiBoSS support".
  - PhysiBoSS builds MaBoSS through `python3 addons/PhysiBoSS/setup_libmaboss.py`
    (`MABOSS_MAX_NODES` default 256).
  - The executable is `project`. Settings live in `config/PhysiCell_settings.xml` and
    rules in `config/cell_rules.csv`.
  - The compiler is `g++` with `-fopenmp`. macOS needs an OpenMP-enabled g++ via
    `PHYSICELL_CPP`.
  - `make reset` restores the original state. The default `all` target populates
    `heterogeneity-sample`, so the script never calls bare `make` before a project is
    populated.

## 4. (4a) The data → PhysiCell translation

### 4.1 `runs/orchestrator/<p>/physicell_mapping.json`

`"kind": "physicell_mapping"`, `schema_version: 1`.

**Inputs:**
- `configuration`: the captured PhysiCell package. It records the PhysiCell session id,
  the capture folder under `runs/multicellular-configurator/<session>/`, the XML path
  and sha256, and the rules CSV path and sha256 (when present).
- `sources`: researcher files in `inputs/`, cited by path and sha256 (the shared
  `scripts/data/sources.py` rules).
- `data`: approved data-analyst proposals, cited by session, proposal and approval (the
  existing proposal route).

**`entries`.** Each entry has:
- `entry_id`;
- `target` (below);
- `value` and `value_unit`, as measured;
- `transform`;
- `target_unit`;
- `from`: one or more refs, either `{source, locator}` or `{proposal, row}`;
- `justification`.

**Target kinds**, each pointing at exactly one setting:

| `target.kind` | Fields | Setting |
|---|---|---|
| `cycle_rate` | `cell_type`, `from_phase`, `to_phase` | transition rate (1/min) |
| `cycle_duration` | `cell_type`, `from_phase`, `to_phase` | fixed phase duration (min) |
| `death_rate` | `cell_type`, `model` (`apoptosis`/`necrosis`) | death rate (1/min) |
| `motility` | `cell_type`, `field` (`speed`/`persistence_time`/`migration_bias`) | µm/min, min, dimensionless |
| `volume` | `cell_type`, `field` (`total`) | µm³ |
| `secretion_rate` / `uptake_rate` | `cell_type`, `substrate` | 1/min |
| `substrate_initial` | `substrate` | configured substrate unit |
| `cell_count` | `cell_type` | initial number of cells (dimensionless) |
| `user_parameter` | `name`, `type` | as declared |
| `rule_parameter` | `cell_type`, `signal`, `behavior`, `field` (`half_max`/`hill_power`/`saturation`) | rule value |

**Transforms.** A fixed list, applied as `model_value = transform(value) × factor`:
- `identity`;
- `reciprocal`, e.g. a duration into a rate;
- `ln2_over`, a doubling time into a rate (`ln 2 / T`);
- `unit_factor` only.

Each entry declares the transform and a positive `factor` from `value_unit` to
`target_unit`, e.g. hours → minutes (60) for durations, or 1/h → 1/min (1/60) for rates.
`target_unit` must equal PhysiCell's fixed unit for that kind. For `substrate_initial`
it must equal the configured substrate's unit label.

**`unmapped`.** Lists every source row or proposal row not used, each with a real reason
(coverage is exactly once for proposal rows; every declared source is cited).

### 4.2 Check and approval

`scripts/physicell/physicell_mapping.py draft|check` verifies:
- **inputs:** hashes, and that the captured package is unchanged;
- **targets:** every target exists in the captured XML or rules CSV (cell types, phases
  of the configured cycle model, substrates, rule signal/behavior pairs);
- **units:** unit and transform consistency with the target kind, and positive factors;
- **values:** finite resulting values that respect each kind's domain (rates ≥ 0,
  durations > 0, counts non-negative integers);
- **coverage**, and no target set by two entries.

`check` lists every problem. A `stage_approval` with stage `physicell_mapping` binds the
mapping's path and sha256 (`validate_record` applies). Its `outputs` must equal the
entry ids, and its `session_id` is the PhysiCell session.

### 4.3 Application and verification

- **Dispatch line.** The multicellular-configurator brief has `STAGE_GATE` on line 1 (as
  today) and, on line 2,
  `DATA_INPUT {"mapping": "runs/orchestrator/<p>/physicell_mapping.json", "sha256": "…", "approval": "<id>", "record_session_id": "<PhysiCell session>"}`.
- **Gate checks.** `multicellular_configurator` joins `DATA_INPUT_LINE` at index 1. The
  gate re-runs the check and verifies:
  - the approval;
  - that `record_session_id` equals the mapping's configuration session;
  - for a NeKo/MaBoSS upstream, that the configuration descends from the `STAGE_GATE`
    upstream.

  It also applies the existing mention rules (other mapping files, data-analyst paths,
  job folders).
- **What the configurator does.** It applies exactly the mapped values with the patch
  tools. It compares each returned stored value with the requested model value
  (relative tolerance 1e-9) and returns `blocked` on any difference. Then it exports a
  new XML and rules CSV.
- **Post-export verification.** `scripts/physicell/verify_export.py <mapping> <new
  capture>` reads the new captured XML and rules CSV and confirms every target holds its
  mapped value, and that nothing else changed versus the mapping's configuration. It
  normalises the XML, ignores ordering and whitespace, and lists differences. It writes
  `runs/orchestrator/<p>/physicell_verification.json`. This implements CLAUDE.md's
  "compare the returned stored value and inspect the exported value".
- **Lineage.** `DECISIONS.md` tags each applied value `data-derived`, citing the mapping
  sha256, the entry id and its sources or proposals.
- **Unchanged.** The registry rule: a new configuration session derives from the
  previous one. `scripts/validate/calibrate_cycle.py` (Boolean trajectories → cycle
  timing) stays as it is.

## 5. (4b) The `physicell-build` skill

`scripts/physicell/build.py --template template|template_BM [--version 1.14.2] [--dest DIR] [--settings XML --rules CSV] [--jobs N]`.

The skill `.claude/skills/physicell-build/SKILL.md` tells the orchestrator when and how
to use it, and to report the build report verbatim. The script runs in the main session
under `Bash`: it is infrastructure, not a modelling tool.

1. **Toolchain check, before any download.** It needs:
   - `make`, `tar` and `curl` (or `git`);
   - `python3` for PhysiBoSS;
   - a C++ compiler that compiles and links a tiny OpenMP program
     (`#pragma omp parallel`). It tries `$PHYSICELL_CPP`, then `g++`, then versioned
     `g++-NN` on macOS.

   On macOS with Apple clang only, it stops with the Homebrew fix
   (`brew install gcc` and `export PHYSICELL_CPP=g++-NN`). Any missing tool stops with a
   one-line remedy.
2. **Download.** It fetches the release source archive for the pinned tag (default
   `1.14.2`) into `<dest>/PhysiCell-<version>-<template>/`. The default `dest` is
   `~/PhysiCell`, outside the repository. It records the archive's sha256 (trust on
   first use; the hash is in the report) and refuses to overwrite an existing folder.
   `--version` lets the researcher pin a newer tag.
3. **Build.**
   - It runs `make <template>`, then `make -j N` (N defaults to the CPU count).
   - For `template_BM` it lets the Makefile run `setup_libmaboss.py`. This needs
     internet and is reported as a step.
   - It verifies that the `project` executable exists and records its sha256.
4. **Optional settings copy.** With `--settings`/`--rules`, it copies an approved
   captured XML and rules CSV into `config/` (under their standard names, after a backup
   of the template's own). It records their sha256 and the approval or verification
   they come from. It refuses a file that is not a captured configuration under `runs/`.
5. **Report.** It writes `runs/orchestrator/physicell-build/<UTC time>-<template>/report.json` and `report.md` with:
   - versions;
   - archive, executable and settings sha256;
   - compiler path and version;
   - make output tail;
   - duration;
   - the exact command to run the project, documented as "for the team that runs
     PhysiCell". The build never runs it.

   On failure the report keeps the failing step and its output tail.

It never deletes anything outside the folder it created. A failed build leaves that
folder for inspection.

## 6. Testing

- **Unit tests (stdlib):**
  - mapping check: every target kind, transforms and factors, unit mismatches, missing
    targets in a fixture XML, domain violations, coverage, duplicate targets;
  - the approval (outputs, session, `validate_record`);
  - real-hook gate tests: accepted; wrong sha, session or upstream; a payload in prose;
    and existing STAGE_GATE behaviour unchanged;
  - `verify_export` on synthetic before/after XMLs: an applied change, a missed change,
    an extra change;
  - `build.py` toolchain detection with fake compilers on `PATH`, the macOS clang
    message, refusal to overwrite, the report on failure, and the settings-copy
    refusal of non-captured files.
- **Smoke test** (`tests/integration/physicell_smoke.py`). It is skipped without
  network or a C++/OpenMP toolchain.
  1. It runs `build.py --template template` into a scratch folder, compiles, and
     checks `project` exists.
  2. On a synthetic captured configuration (fixture XML), it runs mapping draft → check
     → approval → hook check → `verify_export` against a hand-applied fixture.

  `template_BM` is tested live by the researcher because of the MaBoSS build.
- **Live (researcher):**
  - one data-derived settings change applied through the multicellular-configurator;
  - one `template_BM` build with an approved configuration copied in.

## 7. Known limitations

- The PhysiCell archive hash is trust-on-first-use. No upstream checksum list is
  published.
- 1.14.2 was the latest release seen when this was written. The pin is an explicit
  default.
- Rule-parameter targets depend on the rules CSV shape the server exports.
- Values derived from figures are only as good as their locators.
- No Windows-native build: Linux, WSL and macOS only.

## 8. Order of work

1. The mapping, check, stage and approval.
2. The gate and the application instructions (agent and skill), plus `verify_export`.
3. The build script and skill.
4. The smoke test and docs.

Before the PR, this spec is condensed into
`docs/data-to-physicell-architecture.md`.
