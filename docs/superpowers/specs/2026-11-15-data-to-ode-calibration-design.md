# Data to ODE and portable calibration jobs (sub-project 3): design

**Status:** approved in conversation on 2026-11-15, section by section. This document
records that design for review before planning. **Builds on:**
[data-to-model handoffs](../../data-to-model-handoffs-architecture.md) and
[data-analyst architecture](../../data-analyst-architecture.md).

## 1. Purpose and scope

Measured data should drive the ODE (BioMASS) layer the same governed way they drive
NeKo and MaBoSS. Examples are time courses, dose–response viability and
mutation-assay endpoints. Calibration is expensive, so the framework packages it as a
self-contained job. The job runs on this machine, on an SSH-reachable lab machine used
as a normal computer, or on a SLURM cluster. Its results come back, are verified, and
become a proposal the researcher approves before any value enters the model.

**In scope:**
- a typed measurement table and a checked, approved data → ODE mapping;
- local validation of the current model against the data;
- a calibration job bundle with an environment bootstrap and a self-test;
- local, SSH and SLURM launchers;
- result import with verification and a calibration proposal;
- applying approved values through the ode-modeler, with lineage.

**Not in scope:**
- PhysiCell (sub-project 4);
- Codex parity;
- offline wheelhouses (lab machines have internet access);
- statistical identifiability analysis;
- changes to the MCP servers;
- calibration by the data analyst.

## 2. Researcher decisions this design rests on

| # | Decision |
|---|---|
| D1 | Approach A: framework scripts around the BioMASS server's `export_model_bundle`. Do not touch the MCP servers, and do not use the data analyst to calibrate. |
| D2 | Measurement types: time courses, dose–response endpoints, and endpoint contrasts between conditions. Absolute-unit fitting is allowed when units are declared, but no special support is added. |
| D3 | One bundle format with three launchers: local, ssh and slurm. The SSH lab machine with no scheduler is the main case. |
| D4 | Every bundle carries an environment bootstrap, so a biologist runs one command. |
| D5 | SSH has two modes. By default the framework prints the commands. After a host is configured, the framework runs them itself, and each submission needs an authorization quote. |
| D6 | Restarts follow BioMASS practice: independent `x_id` runs, each with a recorded seed and its own initial population. Suggested counts are 10 for a first pass and 30 for a final fit. BioMASS's own optimiser defaults are written into the settings. |
| D7 | Measurements are marked fit or hold-out. Calibration sees only fit rows. Hold-out rows are scored once, after approval. |
| D8 | No offline wheelhouse for now. |

## 3. BioMASS facts the design depends on (biomass 0.14.0, as pinned by server 2.5.2)

- **Export bundle:** `export_model_bundle` writes a zip with:
  - `model/` (the generated BioMASS model package, built with `create_model`);
  - saved `simulate_*`/`graph_*` runs;
  - `requirements.txt` (`biomass==<version>`);
  - `run_simulation.py`.
- **Optimiser:** `biomass.optimize(model, x_id, optimizer_options=...)` wraps
  `scipy.optimize.differential_evolution`. Its defaults are `strategy=best1bin`,
  `maxiter=50`, `popsize=3`, `tol=1e-4`, `mutation=0.1`, `recombination=0.5`,
  `polish=False` ("highly recommended"; `True` warns) and `workers=1`.
  - Search runs in gene space (0,1).
  - `gene2val` maps a gene to `10**(lb + g·(ub−lb))` over the log10 bounds, so **every
    bound must be positive**.
  - Bounds are set in `search_param.py`.
- **Data and objective:**
  - Data and conditions live in `observable.py` (`set_data`, `normalization`, which
    defaults to max-normalisation).
  - The objective lives in `problem.py`.
  - `InitialPopulation(model).generate(n_proc)` builds a solvable start population.
- **Documented workflow** (c-Fos tutorial):
  - many `x_id` runs (30 in the example), with `workers=-1` inside each;
  - then `OptimizationResults` (CSV, boxplots, `trace_obj`) and
    `run_simulation(viz_type='average', stdev=True)` or `'best'`.
  - The docs do not cover: how many runs to do, clusters, convergence criteria or
    identifiability.

## 4. Data → ODE translation

### 4.1 Measurement table

`runs/orchestrator/<p>/measurements.csv` holds one row per measured value. It has
these columns:
- `measurement_id`, `readout`, `condition`;
- `time`, `time_unit`;
- `dose`, `dose_unit` (empty unless dose–response);
- `value`, `value_unit` (or a normalisation label);
- optional `error`, `error_kind` (`sd`/`sem`) and `n`;
- `use` (`fit`/`holdout`);
- `source`, `locator`.

Rules:
- **Sources:** each row cites a researcher source in `inputs/` (path and sha256 through
  the shared source rules) or an approved data-analyst proposal.
- **Units:** they are never inferred. A missing unit fails the check.
- **Data-analyst route:** the data analyst writes the same columns through a new
  proposal kind, `ode_measurements` (layer `biomass`).
- **Orchestrator route:** the orchestrator may transcribe small tables or values from a
  paper, each cited with a locator. The researcher's approval of the mapping covers
  them.

### 4.2 `ode_mapping.json`

**Model binding:**
- the ODE session id and revision;
- the captured export zip (path and sha256) under
  `runs/ode-modeler/<s>/exports/…`;
- the measurement table (path and sha256).

**Readouts:** each readout maps to an observable that exists in that revision.

**Conditions:** each condition maps to explicit overrides of parameters or initial
amounts. Each override declares:
- the model-side unit;
- an approved conversion factor.

The model must expose the target by name.

**Comparison rules** come from a fixed list. Each rule names its own parameters.

| Measurement type | Rules |
|---|---|
| Time course | `fold_change_vs_t0`, `relative_to_condition(<ref>)`, `max_normalized`, `absolute` |
| Endpoint contrast at time T | `ratio(<test>, <ref>)`, `difference(<test>, <ref>)` |
| Dose–response | Drug entry: `target_activity_scaling(<parameter>, form=1/(1+D/K))` with K fixed or fitted, or `inhibitor_species(<species>)`. Readout: `viability = observable(<name>)[treated]/observable(<name>)[untreated] at T`. |

**Fitted parameters:**
- each has a parameter or initial-amount name, a positive lower and upper bound, a
  unit, and a cited justification;
- every other parameter stays fixed;
- if the model's units are still unknown, they are settled before this mapping is
  approved.

**Coverage:** every measurement row is used exactly once or listed in `unmapped` with a
real reason.

### 4.3 Check and approval

`scripts/ode/ode_mapping.py draft|check` verifies everything in 4.1–4.2 and lists every
problem. Checks include:
- hashes;
- observable and target names in the exported model;
- unit presence;
- positive bounds with lower < upper;
- rule parameters;
- fit/hold-out labels;
- coverage.

A `stage_approval` with stage `ode_mapping` binds the mapping's path and sha256. Its
`outputs` must equal the measurement ids plus the fitted parameter names. The
`session_id` is the ODE session.

## 5. The calibration job bundle

`scripts/jobs/build.py --mapping … --approval … --settings …` writes
`runs/orchestrator/<p>/jobs/<job-id>/bundle.tar.gz` and `job.json`.

**`job.json`** records:
- the job id and kind (`biomass-calibration`);
- the mapping path, sha and approval id;
- the export sha;
- the framework commit;
- the settings;
- the sha256 of every bundle file.

**Settings** (researcher-approved, part of the build inputs):
- restarts (`x_id` count);
- seed per restart;
- `optimizer_options` (BioMASS defaults written out explicitly);
- `workers` per restart;
- parallel restarts for ssh/local;
- SLURM time limit and CPUs per task;
- the good-fit margin used at import;
- `reproduce_rtol` (default 1e-3), the import's score-reproduction tolerance.

**Bundle contents:**
- **`model/`:** the exported model package. Its `observable.py`, `search_param.py` and,
  where a rule needs it, `problem.py` are generated deterministically from the
  approved mapping. Nothing is hand-edited.
- **`env/requirements.lock`:** a hash-locked lock with the pinned Python and
  `biomass==0.14.0`.
- **`env/bootstrap.sh`:**
  - fetches a pinned `uv` (checksum verified) if it is missing;
  - lets uv install the pinned Python;
  - creates `.venv` in the job folder;
  - syncs with `--require-hashes`;
  - runs `run.py selftest` under a time cap;
  - writes `env_report.json`.
- **`run.py`:** subcommands `selftest`, `validate` (nominal or given parameters, no
  fitting), `calibrate --restart N` and `collect`. One comparison implementation serves
  `validate`, `calibrate` and the import check.
- **`launch/`:** `local.sh`, `remote.sh` and `slurm.sbatch`.
- **`README.txt`:** three commands, plus what success looks like.
- **Data minimisation:** the bundle contains only what the approved mapping needs: the
  model, the measurement rows and the bounds.
- **Results archive (`results.tar.gz`):**
  - per restart: fitted values, score, trace and seed;
  - best-fit simulations at every measured point;
  - figures made remotely: data vs fit, parameter boxplots, traces;
  - the environment report and logs;
  - `results.json`, which echoes the `job.json` sha256 and lists the sha256 of every
    result file.

## 6. Launchers

**Orchestrator side:** `scripts/jobs/launch.py`.

- **local:**
  - unpacks to `~/.cache/agentic-jobs/<job-id>/` and starts the job detached;
  - `status` and `fetch` work locally;
  - also used for `validate`.
- **ssh, printed commands (default):** `launch.py print <job> --host <name>` prints:
  - `scp` of the bundle;
  - `ssh … ./launch/remote.sh setup`, then `start --parallel K`, then `status`;
  - `scp` of `results.tar.gz` into `inputs/calibration-results/<job-id>/`.
- **ssh, automatic:**
  - Hosts live in an untracked `~/.config/agentic-modelling/hosts.json`: alias, remote
    folder, maximum cores.
  - It uses the user's `~/.ssh/config` and keys only (BatchMode, no passwords).
  - `submit` requires `--authorization "<quote>"`. It appends
    `{host, time, bundle sha, quote}` to
    `runs/orchestrator/<p>/jobs/<id>/dispatch.jsonl`.
  - `status`, `fetch` and `cancel` need no further approval. `fetch` only brings files
    into `inputs/`.
- **slurm:**
  - `remote.sh setup` runs on the login node.
  - `slurm.sbatch` submits an array with one restart per task.
  - `launch.py slurm submit` does both when run on the cluster; otherwise the command
    is printed.

**Remote side:** `remote.sh setup|start [--parallel K]|status|collect|cancel`.
- `start` detaches from the session (setsid/nohup), so logging out doesn't stop it.
- Each restart has its own log, and `status.json` is updated as it runs.
- Partial results can be collected and are marked partial.

## 7. Import, proposal, approval and application

**Import:** `scripts/jobs/import_results.py <job-id>` reads
`inputs/calibration-results/<job-id>/results.tar.gz`. It refuses the archive unless:
- every file matches `results.json`;
- `results.json` echoes the local `job.json` sha256;
- the environment report matches the pins;
- every restart's seed is the recorded one;
- every fitted value lies within bounds;
- re-running the best fit locally through `run.py validate` reproduces the reported
  score within `reproduce_rtol` (ODE solvers differ slightly across machines).

**Proposal:** the import writes
`runs/orchestrator/<p>/jobs/<id>/calibration_proposal.json` and `report.md`.

Per parameter, they give:
- the starting value and bounds;
- the best-fit value and its unit;
- the spread across good restarts (score within the settings margin of the best);
- a flag: "poorly constrained by these data".

They also give a per-restart table and links to the figures. The agreement checks are
labelled as heuristics.

**Approval:** a `stage_approval` with stage `ode_calibration` binds the proposal's sha.
Its `outputs` are the accepted parameters; a subset is allowed. The `session_id` is
the ODE session.

**Application:**
- **The dispatch:** the ode-modeler brief carries, on line 1,
  `DATA_INPUT {"calibration": "runs/orchestrator/<p>/jobs/<id>/calibration_proposal.json", "sha256": "…", "approval": "<id>"}`.
  - The ode-modeler has no STAGE_GATE hook today, because ODE authoring has its own
    approval contract. So `ode_modeler` joins the DATA_INPUT check only.
  - The gate verifies:
    - the approval;
    - the proposal sha;
    - that the accepted parameters exist in the proposal;
    - that the brief names the proposal's ODE session as `record_session_id`, and no
      other.
- **The modeler:**
  - sets exactly the accepted values through the BioMASS server;
  - re-applies every other configured value unchanged, because `configure_model`
    replaces the whole configuration;
  - creates a new revision and reports it.
- **Lineage:**
  - `DECISIONS.md` records the values with the new tag `data-calibrated`, citing the
    job id, the proposal sha256 and the approval;
  - the registry row cites the proposal.
- **Hold-out:** the hold-out rows are scored once on the new revision, through
  `validate`.

## 8. Testing

**Unit tests:**
- the measurement table and the mapping check (units, names, bounds, coverage,
  fit/hold-out);
- deterministic bundle builds (same inputs give same hashes);
- generated BioMASS files from the rules;
- printed SSH commands;
- the hosts config and the authorization log;
- import refusals: tampered, foreign job, out-of-bounds, wrong seed, score mismatch;
- the proposal contents;
- the new `DATA_INPUT` form for the ode-modeler, through the real hook;
- the new stages in `state.py`.

**Smoke test** (offline parts always run, network parts skip when offline):
1. a tiny synthetic BioMASS model goes through mapping, bundle, bootstrap and selftest;
2. two short local restarts run;
3. the results are imported into a proposal;
4. the hook check runs on the ode-modeler brief.

SSH is exercised with a fake `ssh`/`scp` on `PATH`.

**Live (researcher):**
- one real calibration on the lab machine;
- one approved application through the ode-modeler.

## 9. Order of work

1. Measurement table, `ode_mapping` and the new stages.
2. Bundle builder, `run.py` (`selftest`, `validate`, `calibrate`, `collect`), bootstrap
   and the local launcher.
3. SSH (both modes) and SLURM launchers.
4. Import and the proposal.
5. The ode-modeler `DATA_INPUT`, the gate, the `data-calibrated` tag and the docs.

Before the PR, this spec is condensed into
`docs/data-to-ode-calibration-architecture.md`.

## 10. Known limitations (accepted)

- Restart agreement is a heuristic, not identifiability analysis.
- The bundle pins `biomass==0.14.0`. A server upgrade changes the pin, and the import
  refuses mismatches.
- No offline installs.
- The lab machine must have internet access and a POSIX shell.
- Values read from papers are only as good as their locators. The researcher's approval
  is the control.
- Calibration does not change model structure. Structural misfit returns to topology
  review.
