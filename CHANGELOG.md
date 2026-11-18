# Changelog

## Unreleased

### Fixed

- **Credential redaction no longer rewrites scientific text.** The sanitizer applied to
  every recorded transcript event, report and Codex handoff matched substrings
  (`secret`, `auth`, `token`, the word "basic"), so `secretion_rate = 10` and "a basic
  helix-loop-helix factor" were stored as `[REDACTED]` in `report.md`, `task.txt` and
  `tool_calls.json`. Names are now read as whole words, an authorization scheme needs
  a header name or a credential's shape, and a researcher's authorization sentence is
  kept. Records written before this fix may contain such redactions.
- **The Windows-to-WSL Codex bridge no longer passes the task through a shell.**
  `wsl.exe` was called without `--exec`, so bash expanded backticks, `$VAR` and
  `$(...)` in a brief: Markdown code spans were deleted from the task and a quoted
  `$(...)` was executed. The bridge now uses `wsl.exe --exec`.
- **The Claude dispatch hook now gates ode-modeler briefs that use a NeKo graph.** It
  resolved `STAGE_GATE` only for the MaBoSS and PhysiCell specialists, so an ODE brief
  could point at an unreviewed NeKo export. A first-line `STAGE_GATE` on an ODE brief
  is now checked (`biomass_ode`), and a brief naming a NeKo source without one is
  refused. Standalone Text2Model and reaction briefs are unchanged.

## 1.2.0 — 2026-11-18

Tested with `mcp-biomodelling-servers` **2.5.2** (NeKo `nekomata` **1.10.3**); the
backend pins are unchanged from 1.0.3 and no MCP server was modified. This release
contains the data layer that the roadmap planned as 1.1.0 and the data-to-PhysiCell
work planned as 1.2.0. No 1.1.0 was tagged, so both are released together here.

The whole data layer is **Claude Code only**; Codex refuses these briefs until parity.

### Added

- **`data-analyst` specialist.** Retrieves and analyses public and researcher data with
  twelve vendored, hash-locked skills (`vendor/scientific-agent-skills/`), under guards
  that allowlist its Bash and confine its writes to the data session. It returns hashed
  data products and proposals; nothing reaches a model without researcher approval.
  Downloads go to a content-addressed cache with a source ledger, the handoff is
  generated mechanically, and `state.py data-product` registers approved products in
  `DATA_DICTIONARY.md`. Setup: `setup.py --with-data` (pinned `.venv-data`), optional
  `--with-cellprofiler`. See [data workflow](docs/data-workflow.md).
- **Typed data-to-model handoffs for NeKo and MaBoSS.** Approved inputs reach a
  specialist only through one `DATA_INPUT` line that the dispatch hook checks against
  hashes and approval records: data-analyst proposals or researcher sources
  (`seeds.json`) to NeKo, and a researcher-approved `mapping.json` to MaBoSS. New
  governance stages `data_mapping` and `data_seeds`; `scenario_contrast` validation
  criteria; a researcher-supplied BNET can stand in for the network layer.
- **Data to ODE calibration.** An approved `ode_mapping.json` binds measurements to a
  captured BioMASS export. Calibration runs as a deterministic, portable job on this
  machine, a lab machine over SSH or a SLURM cluster; the result is verified on import
  and comes back as a proposal. Approved values are applied only by the `ode-modeler`
  behind the dispatch gate. New stages `ode_mapping` and `ode_calibration`, the
  `data-calibrated` provenance tag, and a hold-out that is scored once.
- **Data to PhysiCell.** Two approved stages: the multicellular design (domain, cell
  definitions, substrates, which cell type carries which MaBoSS model, coupling plan),
  then the data projection, where every value is checked against the live
  signal/behavior dictionary with explicit units. The exported configuration is
  verified so that only approved entries differ. A `physicell-build` skill compiles a
  PhysiCell or PhysiBoSS project with the verified settings. New stages
  `physicell_design` and `physicell_projection`; `gate.py check --design`.
- **Capture recovery.** `scripts/capture/recover.py` records a specialist run from its
  transcript when the SubagentStop hook did not fire, and saves MaBoSS run manifests,
  node-marginal trajectories and engine inputs.
- **`ROADMAP.md`.**

### Changed

- **Specialist reports are written by the recorder.** Specialists return their report
  as their final message and the recorder writes the immutable `report.md`, because
  Claude Code rejects subagent writes to report-like filenames.
- **MaBoSS temporary files stay off `/tmp`.** Setup renders a private, disk-backed
  `TMPDIR` into every modelling server's environment, and
  `scripts/capture/server_tmp.py` removes orphaned `maboss-run-*` directories after a
  run. The underlying leak is in the 2.5.2 server.
- The validator clamps MaBoSS floating-point overshoot within 1e-4 of [0, 1] and
  reports the clamped values; anything further outside still fails.
- State table cells refuse control and line-break characters.

### Known limitations

- The guards protect against accidental or undisciplined writes, not against an agent
  that deliberately forges records; there is no OS-level sandbox.
- No example model or benchmark ships with the repository yet.
- PhysiCell limits of server 2.5.2 are listed in
  [data to PhysiCell architecture](docs/data-to-physicell-architecture.md) and in
  `ROADMAP.md` under "Later".

## 1.0.3 — 2026-10-28

Tested with `mcp-biomodelling-servers` **2.5.2** (NeKo `nekomata` **1.10.3**).
Handoff manifest versions, path-based calls, scientific defaults and engine
behaviour are unchanged from 2.5.1, so the governance, capture and gate scripts
are unchanged.

### Changed

- Pinned backend raised to 2.5.2 and `nekomata` to 1.10.3:
  `setup/dependencies.toml`, the four specialist launches
  (`uvx --from mcp-biomodelling-servers==2.5.2 …`), install and documentation
  strings, the release-version tests and the source-install example (`v2.5.2`,
  `d9079ee`; NeKo `v1.10.3`, `9429581`). A managed environment needs a new
  `--env-prefix`; plugin mode needs only the regenerated launches.
- Skill snapshots checked against the live 2.5.2 schemas. Compared with 2.5.1:
  new read-only `export_handoff_bundle` on NeKo and MaBoSS; optional
  `handoff_bundle` on MaBoSS/BioMASS `import_neko_handoff` and PhysiCell
  `import_maboss_handoff` (so `manifest_path` is no longer schema-required);
  optional `sif_text` on NeKo `create_network` and `bnet_text` on MaBoSS
  `bnet_to_bnd_and_cfg`; updated NeKo `create_network`, `export_network` and
  `remove_bimodal_interactions` descriptions. No tool was removed.
- The bundle and inline-text routes serve hosts that sandbox each server. All
  specialists here share one filesystem, so the skills and
  `docs/agentic-operations.md` keep handoffs and inputs on paths (the verified
  `backend_import_manifest`, preserved SIF/BNET files) and treat a missing file
  as a blocker to report.
- NeKo skill: bimodal edges block BNET export; resolving them (`set_effect`
  under manual-literature authorization, or `remove_bimodal_interactions`,
  which deletes them with their references) happens only as the invocation
  authorizes. The new "generated multiple BNET models" error lists them.
  Seeds missing from the database are dropped without appearing in the tool
  result (NeKo 1.10.3 issues a warning instead of printing), so coverage is
  checked with `analyze_gene_set`.
- MaBoSS skill: quote the new heavy-workload warning (nodes × `max_time` ×
  `sample_count` / `thread_count` above 10⁸); change those settings only to
  supplied, approved values.

- ODE readiness re-verified on 2026-10-28 with 2.5.2: the BioMASS smoke test
  passed all eight checks; the setup check (plugin mode) found 2.5.2 with
  `nekomata` 1.10.3 on all four servers; `ode-modeler` saw 20 BioMASS tools
  (including the 2.5.2 `handoff_bundle` argument), no other modelling or
  delegation tools, and `list_sessions` answered. ODE work remains available.

### Not affected

- NeKo 1.10.3's stricter parsing of custom interaction tables does not reach this
  system: the servers load only the built-in OmniPath and SIGNOR resources, and
  SIF import uses its own parser. The new `seed_mapping` field in the initial
  history state is accepted by the `curation_file` checks.

## 1.0.2 — 2026-10-26

Documentation and governance release for handing the framework to biologists.
Tested with `mcp-biomodelling-servers` **2.5.1** (NeKo `nekomata` 1.10.2); the code
and backend pins are unchanged from 1.0.1.

### Added

- `docs/user-guide.md`, for biologists who are not developers: capabilities and
  limits, how a session goes, the decisions asked at each stage, bringing data and
  papers, the state files, reading results, checkpoints, an example first
  conversation and an FAQ.
- The developer docs now describe only the current system. The dispatcher guide is
  rewritten, `docs/codex-enforcement.md` replaces the two Codex phase notes, and
  `docs/README.md` is a documentation map.

### Changed

- **ODE readiness gate made concrete.** The "Phase 5 live-isolation" wording came
  from the integration worktree. It is replaced by a repeatable check in
  `docs/ode-workflow.md`: the real-backend BioMASS smoke test, plus a live read-only
  `ode-modeler` isolation check, both against the pinned backend and rerun after
  any backend or transport change. CLAUDE.md, AGENTS.md and both BioMASS skills
  point to it. Verified on 2026-10-26 with 2.5.1: the smoke test passed all eight
  checks; `ode-modeler` saw 20 BioMASS tools and no other modelling or delegation
  tools. ODE work is available.

### Removed

- Development records written while building and upgrading the system: the
  improvement and code-review plans, the Wave 1/2 records, the Codex 0.153
  isolation note, the Phase 7 benchmark files and timeout diagnosis, the
  dispatcher phase validation JSONs, `docs/integration/` and `docs/audits/` (89
  files). They remain in Git history and at tag
  [`v1.0.1`](https://github.com/marcorusc/agentic_system_for_modelling/tree/v1.0.1/docs).

## 1.0.1 — 2026-10-22

Tested with `mcp-biomodelling-servers` **2.5.1** (NeKo `nekomata` 1.10.2). The
tool surface and handoff schemas are identical to 2.5.0: same tools and
parameters, with two MaBoSS descriptions updated.

### Changed

- Pinned backend raised to 2.5.1 everywhere: `setup/dependencies.toml`, the four
  specialist launches (`uvx --from mcp-biomodelling-servers==2.5.1 …`), the skill
  snapshot lines (checked against the live 2.5.1 tool schemas), the install and
  documentation strings, and the source-install example (`v2.5.1`, `50d34c8`).
- The MaBoSS skill now reads results only through
  `get_simulation_result(run_id=...)`; 2.5.1's `run_simulation` description no
  longer points to a resource.
- `tests/integration/biomass_smoke.py` places its artifact root under a `.cache`
  directory, like the default root. It now fails on 2.5.0 and passes on 2.5.1.

### Fixed

- `scripts/capture/record_ode_artifacts.py` accepts `--artifact-root` (exactly one
  of it or `--server-root`). Without it, v1.0.0 could not capture BioMASS
  artifacts from the 2.5.0 default storage
  `~/.cache/mcp-biomodelling/artifacts/BioMASS`.

### Added

- `tests/integration/biomass_smoke.py`: a real-backend BioMASS end-to-end smoke
  test, ported from the retired `ODE-specialist` branch. It covers generation,
  simulation, bundle reproduction, project capture, typed v2 completion, edit
  preview, stale-version rejection and placeholder rejection. CI runs it through
  the plugin launch.
- The 2026-09-15 Claude ODE integration validation record is preserved under
  `docs/audits/2026-09-15-ode-claude-integration/` (removed later; at tag v1.0.1).

### Resolved backend issues (MCP 2.5.1)

- BioMASS listed, sealed and exported nothing under the default `~/.cache`
  artifact root in 2.5.0. 2.5.1 fixes it. Revisions generated by 2.5.0 under that
  root fail verification and must be regenerated.
- NeKo starts without network access and no longer writes `pypath_log/` into the
  working directory. Server commands accept `--help`/`--version`. MaBoSS plots no
  longer abort under a display.

### Removed

- `docs/agentic-biomodelling-architecture.pdf`. The Markdown document, which
  GitHub renders with its diagrams, is the single source.

## 1.0.0 — 2026-10-21

First tagged release. Tested with `mcp-biomodelling-servers` **2.5.0**
(NeKo `nekomata` 1.10.2, PhysiCell Settings 0.6.3, bundled cmaboss, BioMASS 0.14).

### Highlights

- **Marketplace plugin launch, kept isolated.** Specialists start their servers
  with the marketplace plugins' own command,
  `uvx --from mcp-biomodelling-servers==2.5.0 mcp-<server>-server`, inline in each
  specialist only. The tracked agent definitions ship this portable launch; setup's
  new `--environment-mode plugin` writes absolute paths and needs only
  [uv](https://docs.astral.sh/uv/). The `neko`/`maboss`/`physicell`/`biomass`
  plugins themselves must **not** be enabled in this project: Claude Code connects
  an enabled plugin's servers to the main session, giving the orchestrator
  modelling tools. Setup and `scripts/check_isolation.py` fail if they are enabled
  in project, local or user settings.
- **Wave 1 governance and provenance** (merged as PR #6): durable capture and
  indexing of modelling artifacts, guarded specialist reports and invocation
  records, `scripts/state.py` / `scripts/gate.py` stage approvals bound to exact
  reviewed outputs, structured immutable edge evidence, read-only artifact
  validators (`scripts/validate/`), shared PubMed access and the setup isolation
  guard. See `docs/agentic-operations.md` and
  [`docs/wave1-pr-review.md`](https://github.com/marcorusc/agentic_system_for_modelling/blob/v1.0.1/docs/wave1-pr-review.md) (at tag v1.0.1).
- **Wave 2 adoption of the 2.5.0 servers** ([`docs/wave2-design-decisions.md`](https://github.com/marcorusc/agentic_system_for_modelling/blob/v1.0.1/docs/wave2-design-decisions.md), at tag v1.0.1):
  - NeKo `edit_interactions` for explicitly authorized `manual-literature` edges
    between existing nodes, never hand-edited SIF; paged history and history export.
  - MaBoSS `edit_maboss_readouts` for researcher-named `readout` nodes, per-node
    rates via `update_maboss_parameters(rates=…, expected_revision=…)`, durable run
    IDs for ordinary and mutant runs, and run-scoped results and plots.
  - PhysiCell `configure_simulation_inputs` (typed user parameters with units,
    existing initial-cell CSV) and `export_configuration_bundle`; stored-value
    readback and required-substrate origin.
  - Approvals bind to the exact NeKo state or MaBoSS `model_revision`; any later
    interaction, rule, readout or rate edit supersedes them.

### Handoff contracts

- `gate.py` and the capture recorders accept NeKo→MaBoSS and MaBoSS→PhysiCell
  1.0/2.0/**2.1** and NeKo→BioMASS 1.0/**2.0**. Unknown versions are rejected,
  and 1.0 documents cannot carry the new fields.
- The `curation_file` NeKo history companion is copied, hashed and checked against
  the handoff's session, state and every non-path identity field.
- MaBoSS 2.1 requires `model_revision` and explicit `readouts`. Each readout must
  match its BND rule and may not feed core nodes. Readouts are the only nodes
  allowed beyond the NeKo parent.
- **Workflow change:** 2.5.0 binds each companion to the model file's original
  absolute path, so import tools reject relocated copies. Specialists import the
  recorder's `backend_import_manifest`; the project capture stays the record that
  gates and approvals bind to.
- `scripts/capture/verify_import_manifest.py` confirms, before dispatch, that the
  backend manifest handed to a specialist is byte-identical to the captured
  original and that its payloads are unchanged. All three recorders return
  `backend_import_manifest` when a curation binding applies.
- The NeKo Boolean recorder captures the `<bnet>.lineage.json` source-lineage
  sidecar unchanged when present, after checking it describes exactly this BNET and
  history companion.
- `gate.py check biomass_ode` now works for real NeKo→BioMASS manifests: the
  approval's `--outputs` must name every `node_id` of the exported graph (the
  whole-graph scope), in addition to the hash, companion, review and registry
  checks. Previously this stage could never pass.
- The ODE recorder accepts `--artifact-root`. 2.5.0 servers write to
  `~/.cache/mcp-biomodelling/artifacts/<Server>` by default.

### Setup

- `setup/dependencies.toml` pins 2.5.0 / `nekomata` 1.10.2.
- No MaBoSS executable or system Graphviz is required. Setup no longer
  auto-discovers `dot`, conda installs only Python, and `--graphviz-path` remains
  an optional override.
- Plugin mode verifies the resolved server version, reports dependency drift from
  the tested pins as warnings, and probes every server through the exact launch.
  Settings saved from another mode are not carried over.
- The source-install example (`setup/backend-sources.integration.json`) pins the
  `v2.5.0` and official NeKo `v1.10.2` release commits.
- CI installs the released backend set from `setup/dependencies.toml`, and a new
  job starts all four servers through the uvx plugin launch.
  `setup/requirements-wave1-review.txt` (unreleased Wave 1 sources) is removed.

### Prompts and skills

- All operational skills are snapshots of 2.5.0, checked against the live tool
  schemas. The 2.4.0 prompt audit is merged; literature review keeps Wave 1's
  per-edge search caps.
- The README now documents the `scientific-reviewer` and `reproducibility-auditor`
  agents behind `/validate-stage`, and the portable transports.
- The Codex plugin manifest version is now `1.0.0`. Setup refreshes installed
  copies.

### Upgrading

- Re-run setup after pulling: `python scripts/setup.py --client claude
  --environment-mode plugin` (uv only), or select a **new** `--env-prefix` for a
  managed 2.5.0 environment. An existing 2.4.0 environment cannot be upgraded in
  place. Restart Claude Code / Codex afterwards.
- Disable any `neko@marcorusc`, `maboss@marcorusc`, `physicell@marcorusc` or
  `biomass@marcorusc` plugin enabled at user scope before working in this project.
- Historical artifacts keep their original versions and remain readable. New
  manifests from 2.5.0 servers need this release's gate and recorders.

### Known limitations

- Headless runs (`claude -p`, SDK) and untrusted folders skip inline subagent MCP
  servers. Verify specialists interactively (`/agents`, then a read-only task).
- Codex: the parent-isolation check blocks enabled servers named `neko`, `maboss`,
  `physicell` or `biomass` in `codex mcp list`. Whether Codex lists servers from an
  installed plugin there is unverified, so do not `codex plugin add` the modelling
  plugins for this project.
- The plugin launch pins only the server package. For an exact installed snapshot
  (`resolved-requirements.txt`), use a managed environment.
- Deferred Wave 2 items remain deferred: the NeKo→MaBoSS import investigation
  (B-MB-2), MaBoSS_test integration, new analysis algorithms and the broader
  testing campaign.
