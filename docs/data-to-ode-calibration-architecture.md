# Data to ODE and portable calibration jobs: architecture and design rationale

**Document status:** describes how measured data drive the BioMASS ODE layer through a
checked mapping and a portable calibration job (Claude Code only, sub-project 3), tested
with Claude Code 2.1.311, `mcp-biomodelling-servers` 2.5.2 and `biomass` 0.14.0. For the
step-by-step procedure, see [ODE workflow](ode-workflow.md); for the data analyst, see
[data workflow](data-workflow.md); for the typed handoffs this builds on, see the
[data-to-model handoffs architecture](data-to-model-handoffs-architecture.md). Where this
document and the code disagree, the code is authoritative.

## 1. Purpose and scope

Measured data (time courses, dose-response viability, endpoint contrasts) should drive the
ODE layer in the same governed way as NeKo and MaBoSS. Calibration is expensive and often
needs a bigger machine. So the framework packages it as a self-contained job. The job runs
here, on an SSH-reachable lab machine, or on a SLURM cluster. Its results come back, are
verified, and become a proposal. No value enters the model until the researcher approves it.

**In scope:** a typed measurement table; a checked, approved data-to-ODE mapping; a
deterministic job bundle with an environment bootstrap; local, SSH and SLURM launchers;
verified import; a calibration proposal; applying approved values through the ode-modeler;
one-time hold-out scoring.

**Not covered here:**
- PhysiCell (sub-project 4) and Codex parity;
- offline installs and identifiability analysis;
- MCP server changes, and calibration by the data analyst.

## 2. Key decisions and rationale

| Decision | Rationale |
|---|---|
| **Framework scripts around the BioMASS export; no server changes; no data-analyst calibration** | The export zip already holds the generated model. Fitting is a long, resource-heavy job, so no specialist runs it. The analyst only proposes `ode_measurements` rows. |
| **The runner does not use `biomass.optimize`** | Generated condition branches mutate parameters across conditions, and the mapping rules (ratio to a reference, viability ratio) cannot be written in BioMASS data files. The runner reuses the generated `Observable.simulate`. It mirrors the BioMASS defaults: `differential_evolution` in gene space with the pinned options, the `gene2val` log10 mapping (so all bounds are positive) and an `InitialPopulation`-style start (random genes until the objective is finite and below 1e12). Cost: no BioMASS `out/` layout; `collect` draws its own figures. |
| **Deterministic bundle and job id** | The job id is `job-` plus 12 hex of a sha256 over mapping, approval id, settings, export and framework commit. The tar has no timestamps; gzip mtime is 0. Rebuilding gives the same job. The commit gets a `-dirty` suffix when `scripts/jobs/`, `scripts/ode/` or the lock have uncommitted changes. Build time and host go in `build.json`, outside the bundle. |
| **Environment bootstrap with pinned `uv`, Python 3.12 and `biomass==0.14.0`** | A biologist runs one command on a plain machine. `uv` is downloaded with a pinned sha256 per platform; packages install with `--require-hashes`. All writes stay inside the job folder. |
| **Three launchers: local, ssh, slurm** | The main case is a lab machine with SSH and no scheduler. SSH prints the commands by default. After a host is configured, it runs them itself, and each submission needs the researcher's quoted authorization, logged to `dispatch.jsonl` first. SLURM submits an array with one restart per task. |
| **Verified import; reproduce locally; start values computed locally** | The remote archive is untrusted. Import checks every member against `results.json`, the `job.json` hash, the environment pins, seeds and bounds. It re-runs the best fit in a local copy of the bundle and requires the same score within `reproduce_rtol`. Nominal start values come from that local run, never from the remote selftest. |
| **Restart agreement as a heuristic** | BioMASS practice is many independent `x_id` restarts. Good restarts spanning more than one decade flag a parameter `poorly_constrained`. It is labelled a heuristic, not identifiability. |
| **Fit/hold-out split; hold-out scored once (RD)** | Calibration sees only `use=fit` rows. Hold-out rows are scored once, on the bundled model with the accepted values. That model equals the applied one, so the score does not depend on when the modeler applies. `holdout_score.json` is never overwritten. |
| **Explicit time and dose unit blocks (S2-ode)** | Units are never inferred. The mapping has `time` and `dose` blocks with a model unit and a factor per measurement unit. A missing factor fails `check`, and the runner raises rather than default. |
| **Revision check before applying** | The modeler applies only when the session's current revision equals `proposal.model.revision`; otherwise it returns `blocked`. |
| **Gate binds proposal, mapping, `job.json` and `record_session_id`** | The ode-modeler `DATA_INPUT` names the proposal. The gate checks the `ode_calibration` approval, the proposal hash, accepted outputs, the proposal's `mapping` approval, `job_sha256` against `job.json`, and that the brief carries the proposal's ODE session as `record_session_id` and no other value. |
| **Job-folder and results mentions refused outside `DATA_INPUT`** | Job folders and returned results hold remote-derived values. They reach a specialist only as the approved proposal, so a brief naming them is refused. |

## 3. Components

| Component | Role |
|---|---|
| `scripts/ode/measurements.py` | Reads and checks the measurement table: columns, units, sources, `use`. |
| `scripts/ode/model_zip.py` | Introspects an export zip (parameters, species, observables) with `ast`; never imports it. |
| `scripts/ode/ode_mapping.py` | `draft` and `check` for `ode_mapping.json`: model binding, readouts, conditions, rules, fitted bounds, coverage, unit blocks. |
| `scripts/data/approvals.py` | Finds and verifies `ode_mapping` and `ode_calibration` approvals in `DECISIONS.md`. |
| `scripts/jobs/settings.py` | Settings schema and digest; BioMASS defaults written out explicitly. |
| `scripts/jobs/build.py` | Writes the deterministic `bundle.tar.gz`, `job.json` and `build.json`. |
| `scripts/jobs/runtime/` | Bundled runtime: `run.py` (`selftest`, `validate`, `calibrate`, `collect`), `compare.py` (the one comparison code path), `bootstrap.sh`, `local.sh`, `remote.sh`, `slurm.sbatch`, `README.txt`. |
| `scripts/jobs/launch.py` | `local`, `ssh` (`print`, `submit`, `status`, `fetch`, `cancel`) and `slurm` launchers; `hosts.json` and the authorization log. |
| `scripts/jobs/import_results.py` | Verifies the archive, reproduces the score, writes `calibration_proposal.json`, `report.md` and figures. |
| `scripts/jobs/score_holdout.py` | Scores hold-out rows once after the calibration approval. |
| `scripts/gate.py` | Ode-modeler calibration `DATA_INPUT` check, `record_session_id` rule, job-folder and results mention refusals. |
| `scripts/state.py`, `scripts/data/manifest.py` | Governance stages `ode_mapping` and `ode_calibration`; proposal kind `ode_measurements` (layer `biomass`). |
| `templates/calibration-settings.{first-pass,final}.json` | Starting settings (10 and 30 restarts). |
| `tests/integration/ode_calibration_smoke.py` | Synthetic model through mapping, bundle, bootstrap, two local restarts, import and the hook check. Network parts skip offline. |

## 4. Flow

The procedure is in [ODE workflow](ode-workflow.md). In outline:

1. **Mapping.** A measurement table is mapped onto a captured, accepted model revision.
   `ode_mapping.py draft`, fill with the researcher, `check`.
2. **Approval.** The researcher approves the mapping (`ode_mapping`).
3. **Build and launch.** `build.py` writes the bundle. A launcher runs it; `fetch` places
   `results.tar.gz` under `inputs/calibration-results/<job-id>/`.
4. **Import.** `import_results.py` verifies, reproduces locally and writes the proposal.
5. **Calibration approval.** The researcher approves a subset of parameters
   (`ode_calibration`).
6. **Apply.** The ode-modeler brief carries the `DATA_INPUT` calibration line. The hook
   checks it; the modeler sets exactly the accepted values and creates a new revision.
7. **Hold-out.** `score_holdout.py` scores the held-out rows once.

## 5. Contracts

- **Measurement table** `measurements.csv`: one row per value. Columns are
  `measurement_id`, `readout`, `condition`, `time`, `time_unit`, `dose`, `dose_unit`,
  `value`, `value_unit`, optional `error`, `error_kind`, `n`, then `use` (`fit` or
  `holdout`), `source`, `locator`. Each row cites a source or an approved proposal.
- **`ode_mapping.json`** (`kind: ode_mapping`): model binding (session, revision, export
  zip and table with sha256), `time` and `dose` unit blocks, readouts, conditions with
  overrides, comparison rules, fitted parameters (positive bounds, lower below upper, unit,
  justification) and `unmapped`. Every row is used once or listed in `unmapped` with a reason.
- **Comparison rules:** time course `fold_change_vs_t0`, `relative_to_condition`,
  `max_normalized`, `absolute`; endpoint `ratio` and `difference`; dose-response drug entry
  `target_activity_scaling` or `inhibitor_species` with a viability readout. A contrast
  reference uses rows with the same `use` as the test row.
- **Mapping approval:** `ode_mapping` `stage_approval`, `session_id` = the ODE session,
  `outputs` = measurement ids used plus fitted names, `artifact` = path and sha256.
- **Settings:** restarts, seed per restart, `optimizer_options`, `workers`, parallel
  restarts, SLURM limits, good-fit margin and `reproduce_rtol` (default 1e-3). BioMASS
  defaults: `best1bin`, `maxiter=50`, `popsize=3`, `tol=1e-4`, `mutation=0.1`,
  `recombination=0.5`, `polish=False`, `workers=1`.
- **Objective:** the unweighted sum of squared differences over `use=fit` rows. `error` is
  reported only. A solver failure or non-finite prediction scores 1e12.
- **`job.json`** (`kind: biomass-calibration`): job id, mapping path, hash and approval,
  export sha256, settings, framework commit and the sha256 of each bundle file.
- **`results.json`:** echoes the `job.json` hash and lists every result file with its hash.
- **`calibration_proposal.json`** (`kind: calibration_proposal`): per parameter the start
  value, bounds, best value, unit, good-restart spread and a `poorly_constrained` flag; a
  per-restart table; a `partial` flag. `report.md` carries the same, with figures.
- **Calibration approval:** `ode_calibration` `stage_approval`, `session_id` = the ODE
  session, `outputs` = accepted parameter names, `artifact` = proposal path and sha256.
- **Ode-modeler `DATA_INPUT`** (line 1): `{"calibration", "sha256", "approval"}` plus
  `record_session_id` = the ODE session in the brief.
- **Lineage:** values get the tag `data-calibrated`, citing job id, proposal hash and
  approval id. The new revision's row has Derived from = the calibrated session.

## 6. Guardrails and threat model

- **Dispatch hook:** refuses a calibration `DATA_INPUT` whose approval, hash, outputs,
  mapping approval, `job_sha256` or ODE session do not match, a missing or conflicting
  `record_session_id`, and any mention of a job folder or `inputs/calibration-results`
  outside the `DATA_INPUT` proposal. It also refuses stray ode_mapping, seeds and
  calibration files.
- **Untrusted remote archive:** members are checked against `results.json`; total size is
  capped at 2 GiB; name collisions fail; scores must be non-negative and not the penalty.
  The best score is reproduced locally.
- **Writes stay in the job folder:** bootstrap and runner write nothing elsewhere. SSH uses
  the user's keys only (BatchMode, no passwords) and a host list outside the repository.
- **Bounded transfer:** only the model, the measurement rows the rules use and the
  parameter bounds leave the machine.
- **Approvals bind content:** the mapping approval binds the mapping hash and id set; the
  calibration approval binds the proposal hash and accepted names.
- **Threat model:** as for the data analyst. The controls catch accidental and
  undisciplined behaviour, not forged provenance files.

## 7. Known limitations

- **Restart agreement is a heuristic,** not identifiability analysis.
- **BioMASS defaults stop early** with few fitted parameters. Raise `popsize` and `maxiter`.
- **`biomass==0.14.0` is pinned.** A server upgrade changes it, and import refuses a mismatch.
- **No offline installs.** Lab machines need internet and a POSIX shell.
- **Three of four `uv` platform checksums are not verified live;** only one platform was
  checked against a download.
- **The `record_session_id` rule over-refuses** some natural phrasings. Write it as
  `record_session_id <session>` or JSON.
- **Input files named like `calibration_results*`** are refused in briefs. Rename them.
- **Editing an `ode_mapping` in place** makes its earlier approved proposal unapplicable.
  Keep the mapping unchanged until the proposal is applied. For a changed mapping use a new
  purpose; the earlier proposal stays scoreable but cannot be applied.
- **Gzip bytes depend on the host,** so bundle hashes are stable per host, not across zlib builds.
- **Codex has no dispatcher check,** so it returns `blocked`.
- **Cancel cannot reach a process group whose supervisor died.**
- **Foreign-host SLURM restarts** depend on `sacct`.
- **Calibration does not change structure.** Structural misfit returns to topology review.

## 8. Roadmap

1. **Implemented:** the data-to-ODE mapping and portable calibration jobs above.
2. **Next:** sub-project 4, data to PhysiCell settings with a `physicell-build` skill.
3. **Later:** Codex parity; identifiability analysis; an offline wheelhouse.
