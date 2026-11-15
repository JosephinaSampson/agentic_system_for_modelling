# Data to ODE and Portable Calibration Jobs Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Measured data drive BioMASS ODE models through an approved data → ODE mapping.
Calibration runs as a portable job (local, SSH, SLURM) whose results return as a
verified proposal. Approved values reach the model only through the ode-modeler.

**Architecture:**
- Framework scripts sit around the BioMASS server's exported model zip. The MCP servers
  are not modified.
- `scripts/ode/` holds the measurement table and the mapping.
- `scripts/jobs/` builds a deterministic job bundle, launches it, and imports results.
- The bundle carries its own pinned environment, a stdlib comparison module and a runner
  that drives the exported model with SciPy differential evolution, using BioMASS's
  defaults.

**Tech Stack:**
- Framework side: Python 3 stdlib only (repo tests run with system `python3`, no numpy).
- Bundle runtime: Python 3.12, `biomass==0.14.0`, numpy/scipy (via biomass), bash, `uv`
  0.13.4.

**Spec:** `docs/superpowers/specs/2026-11-15-data-to-ode-calibration-design.md`

## Global Constraints

**Scope limits**
- Do not modify the MCP servers (`mcp-biomodelling-servers`).
- The data-analyst never calibrates.
- No offline wheelhouse.

**Bundle runtime pins**
- `biomass==0.14.0` and Python `3.12`, installed through a hash-locked lock (`--require-hashes`).
- `uv` 0.13.4, downloaded with a pinned sha256 per platform.
- Platforms: `x86_64-unknown-linux-gnu`, `aarch64-unknown-linux-gnu`,
  `x86_64-apple-darwin`, `aarch64-apple-darwin`.

**Optimiser**
- BioMASS 0.14.0 differential-evolution defaults, written explicitly into every settings
  file: `strategy="best1bin"`, `maxiter=50`, `popsize=3`, `tol=1e-4`, `mutation=0.1`,
  `recombination=0.5`, `polish=False`, `workers=1`.
- Gene → value mapping, identical to BioMASS `gene2val`:
  `value = 10 ** (log10(lb) + g * (log10(ub) - log10(lb)))`, with g in [0, 1].
  Every bound is positive and `lb < ub`.
- Initial population as BioMASS `InitialPopulation`: draw random genes until the
  objective is finite and `< 1e12`. Population size = `popsize × n_parameters`. It is
  seeded by the restart seed.

**Settings defaults and heuristics**
- `reproduce_rtol` default `1e-3`.
- Suggested restarts: `10` for a first pass, `30` for a final fit (template settings
  files only; not enforced).
- Restart-agreement heuristic constant: `POORLY_CONSTRAINED_LOG10_SPAN = 1.0`.

**Governance and provenance**
- New governance stages: `ode_mapping` and `ode_calibration`. New provenance tag:
  `data-calibrated`.
- New proposal kind `ode_measurements`, layer `biomass`.
- New JSON files carry a discriminator `"kind"`: `"ode_mapping"` or
  `"calibration_proposal"`.

**Paths**
- `runs/orchestrator/<p>/measurements.csv`
- `runs/orchestrator/<p>/ode_mapping.json`
- `runs/orchestrator/<p>/jobs/<job-id>/{bundle.tar.gz, job.json, build.json, dispatch.jsonl, calibration_proposal.json, report.md, holdout_score.json}`
- `inputs/calibration-results/<job-id>/results.tar.gz`
- `<XDG_CACHE_HOME or ~/.cache>/agentic-jobs/<job-id>/`
- `<XDG_CONFIG_HOME or ~/.config>/agentic-modelling/hosts.json`

**Conventions**
- ssh and scp always run with `-o BatchMode=yes`. `launch.py ssh submit` requires
  `--authorization "<quote>"`.
- Reuse `scripts/data/common.py` (`strict_json`, `sha256_file`, `reject_symlinks`) and
  `scripts/data/sources.py` (`source_file`, `entry`, `check_locator`, `is_ref`). Hash
  large files with `sha256_file` (streamed).
- The repo test suites (`scripts/run_tests.py`) must not need numpy, scipy or biomass.
- Commits end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Controller rulings (from spec + live inspection of a Text2Model package, biomass 0.14.0)

- **RA, runner design.** The generated model package exposes:
  - `model/ode.py` with `param_values()`, `initial_values()` and
    `DifferentialEquation.diffeq`;
  - `model/name2idx/{parameters,species}.py` with `NAMES` lists, and `C`/`V` index
    objects;
  - `model/observable.py`, a `class Observable(DifferentialEquation)` with literal
    `obs_names`, `t`, `conditions`, and `simulate(x, y0)` that loops over `conditions`.
  - Named condition branches in `simulate` mutate `x` in place across conditions.

  The runner therefore does **not** use `biomass.optimize` or generated
  `observable.py`/`search_param.py`/`problem.py` data files. It:
  1. builds `x`/`y0` per mapped condition from defaults plus approved overrides;
  2. sets `obs.conditions=["__mapped__"]`, `obs.t=<sorted model-unit times incl. 0>` and
     `obs.simulations=np.empty((len(obs_names),1,len(t)))`;
  3. calls `obs.simulate(list(x), list(y0))`, so the generated observable expressions are
     reused, and treats a `False` return as a solver failure;
  4. runs `scipy.optimize.differential_evolution` in gene space with the pinned defaults
     and `seed=<restart seed>`.

  Why: BioMASS's data files cannot express the spec's cross-condition rules (ratio to a
  reference, viability ratio) and the generated condition branches mutate parameters.
  Cost: the BioMASS `out/` layout and plots are not produced, so `collect` makes its own
  figures. Flagged to the researcher with the plan.
- **RB, model introspection.** Framework-side introspection of an export zip (names of
  parameters, species and observables) parses the files with `ast`, never imports them.
- **RC, job id.** The job id is deterministic:
  `job-` + first 12 hex of sha256 over (mapping sha256, approval id, settings sha256,
  export sha256, framework commit).
  - The bundle contains no timestamps (tar entries mtime 0, uid/gid 0, sorted; gzip mtime 0).
  - `build.json`, outside the bundle, records build time and host.
- **RD, hold-out scoring.** Hold-out rows are scored once, with the accepted parameter
  values, on the job's bundled model. That is the same model, because the ode-modeler
  applies exactly those values; the gate and the reproducibility audit check that.
  `holdout_score.json` is written once and never overwritten.
- **RE, objective.** The objective is the unweighted sum of squared differences between
  transformed predictions and data over `use=fit` rows (BioMASS's default objective
  form). `error` columns are carried for reporting only. A solver failure or non-finite
  prediction scores `1e12` (never crashes).

## Review Focus

1. **Unit mismatch.** A measurement in `h` against a model in `min`, or a dose in `µM`
   against a model in `nM`. Expected: `check` refuses a missing conversion factor, and
   predictions use converted times and doses (test in Task 2).
2. **Times off the integer grid.** Measurement times are not on BioMASS's default integer
   grid (e.g. 7.5 min). Expected: predictions are taken at the exact times, not the
   nearest grid point (test in Task 3, runner `times` construction, and the smoke test).
3. **Partial remote failure.** Some restarts crash or return non-finite scores. Expected:
   `collect` packs the successful ones marked `partial`, import accepts ≥1 success,
   excludes failed ones from agreement, and the proposal says how many failed (test in
   Task 5).
4. **Wrong or duplicate results archive.** Results from another job are dropped into the
   folder, or a fetch runs twice. Expected: import refuses a foreign `job.json` sha256,
   and fetch refuses to overwrite an existing results file (tests in Tasks 4 and 5).
5. **Unsolvable parameter sets.** Some parameter sets make the ODE solver fail during
   optimisation. Expected: objective `1e12`, no exception, and the initial population
   skips them (test in Task 2 with a fake simulator; real path in the smoke test).

---

### Task 1: Stages, measurement table, `ode_mapping` draft/check, proposal kind

**Files:**
- Modify: `scripts/state.py` (`GOVERNANCE_STAGES` += `ode_mapping`, `ode_calibration`)
- Modify: `scripts/data/manifest.py` (`KIND_SCHEMAS["ode_measurements"]`, layer `biomass`, required columns = the measurement table columns, enums `use∈{fit,holdout}` and `error_kind∈{sd,sem,""}`, unique `measurement_id`)
- Modify: `scripts/data/approvals.py` (`require_ode_mapping_approval`, `require_calibration_approval`, both via `_require_artifact_approval`)
- Create: `scripts/ode/__init__.py`, `scripts/ode/model_zip.py`, `scripts/ode/measurements.py`, `scripts/ode/ode_mapping.py`
- Test: `tests/data/test_ode_mapping.py`, `tests/data/test_ode_measurements.py`, additions in `tests/data/test_manifest.py` and `tests/data/test_approvals.py`

**Interfaces:**
- Produces:
  - `model_zip.introspect(zip_path: Path) -> dict` returns `{"parameters": [str], "species": [str], "observables": [str]}`. It reads `model/name2idx/parameters.py`, `model/name2idx/species.py` (literal `NAMES`) and `model/observable.py` (literal `self.obs_names`) with `ast`, and requires `requirements.txt == "biomass==0.14.0\n"`.
  - `measurements.COLUMNS = ("measurement_id","readout","condition","time","time_unit","dose","dose_unit","value","value_unit","error","error_kind","n","use","source","locator")`.
  - `measurements.read(path: Path) -> list[dict]`: utf-8-sig; numeric fields parsed to float (`n` to int); empty `dose`/`error`/`n` become `None`.
  - `measurements.check(rows, source_ids: set[str]) -> list[str]`. It requires:
    - every unit present (`time_unit`; `dose_unit` when `dose` is set; `value_unit`);
    - finite values;
    - `use` in the enum;
    - `source` among `source_ids` with a valid locator (`sources.check_locator`).
  - `ode_mapping.KEYS = ("kind","schema_version","inputs","time","readouts","conditions","rules","fit","unmapped")`. The top level has `"kind": "ode_mapping"` and `"schema_version": 1`.
  - `inputs`:
    - `{"model": {"ode_session","revision","bundle","sha256"}, "measurements": {"path","sha256"} | {"session","proposal","approval"}, "sources": [{id,path,sha256}]}`.
    - `bundle` must be a `.zip` under `runs/ode-modeler/<ode_session>/`.
  - `time`: `{"model_unit": str, "factors": {unit: positive float}}`.
  - `readouts`: `[{"readout", "observable"}]`.
  - `conditions`: `[{"condition", "parameters": [{"name","value","value_unit","model_unit","factor"}], "initials": [same keys, "name" = species]}]`.
  - `rules`: `[{"rule_id","type","measurements":[ids], ...}]`, with `type`/method keys exactly as in spec §4.2:
    - `time_course` + `method ∈ {fold_change_vs_t0, relative_to_condition, max_normalized, absolute}` (+ `reference` for `relative_to_condition`);
    - `endpoint_contrast` + `method ∈ {ratio, difference}` + `test`, `reference`, `time`;
    - `dose_response` + `drug_entry` (`{"form":"target_activity_scaling","parameter","K": number | {"fit": name}}` or `{"form":"inhibitor_species","species"}`) + `viability` (`{"observable","untreated","time"}`).
  - `fit`: `[{"name","kind": "parameter"|"initial","lower","upper","unit","justification": {"source","locator"} | {"text"}}]`.
  - `unmapped`: `[{"measurement_id","reason"}]`.
  - `ode_mapping.check(root: Path, rel: str) -> list[str]`; `ode_mapping.load_and_check(root, rel) -> (data, sha256, errors)`; `ode_mapping.draft(root, purpose, *, ode_session, revision, bundle_rel, measurements_rel, sources: list[tuple[id, path]]) -> Path` (refuses to overwrite; fills `readouts`/`rules`/`fit` empty and every measurement into `unmapped` with reason `"TODO"`, which `check` refuses).
  - CLI: `python3 scripts/ode/ode_mapping.py draft …` and `check MAPPING` (exit 1, one problem per line on stderr).
  - `approvals.require_ode_mapping_approval(root, record_id, rel, sha256) -> dict` and `approvals.require_calibration_approval(root, record_id, rel, sha256) -> dict`.

- [ ] **Step 1: Write failing tests** in `tests/data/test_ode_mapping.py`, with a fixture building a fake export zip (`model/name2idx/parameters.py` `NAMES = ["kf1","kr1","kf2","kf3"]`, species `["E","R","ER","pER"]`, observable `obs_names = ['pR']`, `requirements.txt` `biomass==0.14.0\n`) and a 6-row measurements table:
  - `test_introspect_reads_names_without_import`: names equal the fixture lists.
  - `test_check_passes_complete_mapping`: `check() == []`.
  - `test_missing_time_factor_refused`: a row with `time_unit` `"h"` absent from `time.factors` → error mentions `"h"`.
  - `test_missing_unit_refused`: an empty `value_unit` → error.
  - `test_unknown_observable_parameter_species_refused`: one error each for `readouts[].observable="pX"`, `conditions[].parameters[].name="kz"`, `fit[].name="Q"`.
  - `test_bounds_positive_and_ordered`: `lower=0`, `lower>=upper` → errors.
  - `test_coverage_exactly_once`: a row in two rules, or in no rule and not unmapped → errors; `"TODO"` reason refused.
  - `test_rule_parameters_validated`: `relative_to_condition` without `reference`, `endpoint_contrast` with an unknown `test` condition, `dose_response` with a non-dose row → errors.
  - `test_bundle_path_must_be_under_ode_session` and `test_bundle_hash_mismatch_refused`.
  - `test_approval_outputs_equal_measurements_plus_fit`: `require_ode_mapping_approval` with wrong outputs → refused (mirrors S6).
  - `test_draft_refuses_overwrite_and_lists_todo`.
- [ ] **Step 2: Run** `python3 -m unittest tests.data.test_ode_mapping -v`. Expected: FAIL (module missing).
- [ ] **Step 3: Implement** `scripts/ode/*` and the approvals/state/manifest additions. The `outputs` equality is checked in `require_ode_mapping_approval` against `{measurement ids in rules} ∪ {fit names}`.
- [ ] **Step 4: Run** the module tests, then `python3 scripts/run_tests.py`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(ode): measurement table and data to ODE mapping with draft/check and approvals`.

### Task 2: Comparison module (stdlib, shipped into bundles)

**Files:**
- Create: `scripts/jobs/__init__.py`, `scripts/jobs/runtime/compare.py` (stdlib only; copied verbatim into bundles as `calibration/compare.py`)
- Test: `tests/data/test_jobs_compare.py`

**Interfaces:**
- Consumes: the mapping shapes from Task 1.
- Produces, in `compare.py`:
  - `resolve(mapping: dict, rows: list[dict]) -> dict` builds a model-ready `spec`:
    - `conditions[name] -> {"parameters": {name: model_value}, "initials": {species: model_value}}` (value × factor);
    - `times_model[condition] -> sorted set of model-unit times, including 0.0`;
    - `rules`, and `fit` with `log10` bounds;
    - `measurements` with model-unit time and dose and the `use` flag;
    - `dose_conditions`: one synthetic condition per (rule, dose).
  - `predict(spec, sims) -> dict[measurement_id, float]`. `sims[condition][observable] -> {time: value}`, and the rule methods follow the spec. `fold_change_vs_t0` = value(t)/value(0); `max_normalized` divides by the max over that observable's simulated points in the rule's conditions. A zero denominator → `math.nan`.
  - `objective(spec, predictions, use="fit") -> float`: SSE over that subset; any nan or inf → `1e12`.
  - `PENALTY = 1e12`.

- [ ] **Step 1: Write failing tests** with hand-built `sims`:
  - `test_fold_change_vs_t0`: value(30)=2, value(0)=0.5 → prediction 4.0.
  - `test_relative_to_condition`.
  - `test_max_normalized`.
  - `test_endpoint_ratio_and_difference`.
  - `test_time_conversion_hours_to_minutes`: a row at 1.5 h with factor 60 reads sims at 90.0, not at 90 rounded, nor at 1.5.
  - `test_dose_scaling_and_viability`: dose 2 µM with factor 1000 and K=1000 → parameter × 1/(1+2) in the synthetic condition; viability = obs(treated)/obs(untreated) at T.
  - `test_objective_fit_only_and_penalty`: hold-out rows excluded; a nan prediction → `1e12`.
- [ ] **Step 2: Run** `python3 -m unittest tests.data.test_jobs_compare -v`. Expected: FAIL.
- [ ] **Step 3: Implement** `compare.py`.
- [ ] **Step 4: Run** the tests and `python3 scripts/run_tests.py`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(jobs): stdlib comparison and objective shared by validate, calibrate and import`.

### Task 3: Settings, deterministic bundle builder, runtime runner, bootstrap, local launcher

**Files:**
- Create:
  - `scripts/jobs/settings.py`, `scripts/jobs/build.py`
  - `scripts/jobs/runtime/run.py` (bundle runner, uses numpy/scipy/biomass at runtime only)
  - `scripts/jobs/runtime/bootstrap.sh`, `scripts/jobs/runtime/local.sh`, `scripts/jobs/runtime/remote.sh`, `scripts/jobs/runtime/slurm.sbatch`, `scripts/jobs/runtime/README.txt`
  - `setup/biomass-calibration.lock`
  - `scripts/jobs/launch.py` (local mode in this task)
  - `templates/calibration-settings.first-pass.json` (restarts 10), `templates/calibration-settings.final.json` (restarts 30)
- Test: `tests/data/test_jobs_build.py`

**Interfaces:**
- Consumes: Task 1 `ode_mapping.load_and_check`, `require_ode_mapping_approval`; Task 2 `compare.py`.
- Produces:
  - `settings.load(path) -> dict`. It refuses missing or extra keys. Exact keys:
    - `restarts` (int ≥ 1), `seeds` (list of int, length == restarts, unique);
    - `optimizer_options` (exactly the pinned DE keys);
    - `workers` (int ≥ 1 or -1), `parallel` (int ≥ 1);
    - `slurm` (`{"time": "HH:MM:SS", "cpus_per_task": int}`);
    - `good_fit_margin` (float > 0, relative to the best score);
    - `reproduce_rtol` (float > 0).
  - `build.build(root, mapping_rel, approval_id, settings_rel) -> Path` returns the job dir. The bundle layout:
    - `job.json`: `{"kind":"biomass-calibration","job_id","mapping":{path,sha256,approval},"export_sha256","settings":<dict>,"framework_commit","files":{relpath: sha256}}`;
    - `model/…` (from the export zip);
    - `calibration/compare.py`, `calibration/spec.json` (from `compare.resolve`);
    - `run.py`, `env/requirements.lock`, `env/bootstrap.sh`, `launch/{local.sh,remote.sh,slurm.sbatch}`, `README.txt`.

    It writes `<job-dir>/{bundle.tar.gz, job.json, build.json}` and refuses an unapproved or changed mapping.
  - Bundle runner CLI:
    - `python run.py selftest|validate [--params FILE] [--use fit|holdout|all] [--out FILE]|calibrate --restart N|collect`;
    - `calibrate` writes `out/restart-<N>/{result.json, trace.csv, log.txt}`, where `result.json` = `{restart, seed, score, status: "ok"|"failed", params: {name: value}, wall_time_s}`;
    - `selftest` = one restart with `maxiter=1` and `popsize=1` under a 300 s cap, plus a `validate` at nominal values;
    - `collect` writes `results.tar.gz` with `results.json` (`{"job_sha256", "partial": bool, "restarts": [...], "files": {path: sha256}}`), `env_report.json`, the best-fit predictions for all rows, and figures (matplotlib Agg; data vs fit per readout/condition, parameter strip plot, traces).
  - `launch.py local start|status|fetch|validate <job-id>`: unpacks to the cache job dir, runs `env/bootstrap.sh` then `launch/local.sh`, detached. `fetch` copies `results.tar.gz` to `inputs/calibration-results/<job-id>/` and refuses to overwrite.

- [ ] **Step 1: Write failing tests:**
  - `test_settings_requires_every_key_and_pinned_optimizer_keys`.
  - `test_build_refuses_unapproved_mapping`.
  - `test_build_is_deterministic`: two builds give the same `bundle.tar.gz` sha256 and the same job id.
  - `test_bundle_contents_and_manifest`: every file is listed in `job.json.files` with the right sha256.
  - `test_bundle_contains_only_mapping_rows`: the measurements in `spec.json` equal the mapping's rows, and no other `inputs/` content is present.
  - `test_bootstrap_script_pins`: `bash -n` passes; the script contains `uv` version `0.13.4`, four platform sha256 strings and `--require-hashes`.
  - `test_lock_pins_biomass`: the lock contains `biomass==0.14.0` and `--hash=sha256:`.
  - `test_fetch_refuses_overwrite`.
  - `test_runner_times_exact` (pure-function part: the runner's `model_times(spec, condition)` returns the exact model-unit times including 0.0, e.g. `[0.0, 7.5, 90.0]`).
- [ ] **Step 2: Run** `python3 -m unittest tests.data.test_jobs_build -v`. Expected: FAIL.
- [ ] **Step 3: Generate the lock.** Run `uv pip compile --universal --python-version 3.12 --generate-hashes` on a one-line `biomass==0.14.0` input. Write `setup/biomass-calibration.lock`.
- [ ] **Step 4: Implement** settings, build, the runtime files and local launch.
  - The bootstrap script:
    1. downloads the `uv` 0.13.4 tarball for `uname -sm`;
    2. verifies its sha256 (from GitHub release `.sha256` files, values pinned in the script);
    3. runs `uv python install 3.12`;
    4. runs `uv venv` and `uv pip sync --require-hashes env/requirements.lock`;
    5. runs `run.py selftest`;
    6. writes `env_report.json`.
  - The runner follows ruling RA.
- [ ] **Step 5: Run** the tests and `python3 scripts/run_tests.py`. Expected: PASS.
- [ ] **Step 6: Commit** `feat(jobs): deterministic calibration bundle, runner, bootstrap and local launcher`.

### Task 4: SSH (printed and automatic) and SLURM launchers

**Files:**
- Modify: `scripts/jobs/launch.py`
- Modify: `scripts/jobs/runtime/remote.sh`, `scripts/jobs/runtime/slurm.sbatch`
- Test: `tests/data/test_jobs_launch.py` (fake `ssh`/`scp`/`sbatch` executables on `PATH`, recording their argv)

**Interfaces:**
- Produces:
  - `launch.py print <job-id> --host NAME`: prints numbered commands:
    1. `scp -o BatchMode=yes <bundle> <ssh-alias>:<remote_dir>/`
    2. `ssh … 'cd <remote_dir> && tar xzf <job-id>.tar.gz && cd <job-id> && ./launch/remote.sh setup'`
    3. `… remote.sh start --parallel <K>`
    4. `… remote.sh status`
    5. `… remote.sh collect`
    6. `scp … <ssh-alias>:<remote_dir>/<job-id>/results.tar.gz inputs/calibration-results/<job-id>/`

    `NAME` is a configured host. Without a config, `--host` is used as the ssh alias, `remote_dir` is `~/agentic-jobs` and K = settings `parallel`.
  - `launch.py ssh submit <job-id> --host NAME --authorization "<quote>"` runs steps 1–3. Before running, it appends `{"time","host","ssh_alias","bundle_sha256","authorization"}` to `runs/orchestrator/<p>/jobs/<job-id>/dispatch.jsonl`. An empty or missing quote is refused.
  - `launch.py ssh status|fetch|cancel <job-id> --host NAME`.
  - `launch.py slurm submit <job-id>`: needs `sbatch` on `PATH`; runs `remote.sh setup` then `sbatch --array=1-<restarts> launch/slurm.sbatch`. Otherwise it prints those commands.
  - The hosts config: `{"hosts": {NAME: {"ssh": alias, "remote_dir": str, "max_cores": int}}}`, strict JSON, unknown keys refused. `K = min(parallel, max_cores // max(workers,1))`, at least 1.
  - `remote.sh`:
    - `setup|start [--parallel K]|status|collect|cancel`;
    - `start` uses `setsid nohup`, writes pids to `state/pids` and keeps `state/status.json` updated;
    - `status` prints done, running and failed counts, the best score and the last 20 log lines of failures.

- [ ] **Step 1: Write failing tests:**
  - `test_print_commands_exact`.
  - `test_submit_requires_authorization_and_logs_before_running`: the log line exists even if the fake ssh fails.
  - `test_batchmode_always_set`.
  - `test_unknown_host_key_refused`.
  - `test_fetch_refuses_overwrite_and_foreign_job_dir`.
  - `test_slurm_prints_when_no_sbatch` and `test_slurm_array_size_equals_restarts`.
  - `test_remote_sh_syntax` (`bash -n`).
- [ ] **Step 2: Run** `python3 -m unittest tests.data.test_jobs_launch -v`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the tests and `python3 scripts/run_tests.py`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(jobs): ssh (printed and automatic) and slurm launchers with authorization log`.

### Task 5: Result import, verification and calibration proposal

**Files:**
- Create: `scripts/jobs/import_results.py`
- Test: `tests/data/test_jobs_import.py`

**Interfaces:**
- Consumes: Task 3's `job.json` and `results.json` shapes; Task 2's `compare`.
- Produces:
  - `import_results(root, job_id, *, reproduce=<callable or None>) -> Path` (the proposal path). By default `reproduce` runs `launch.py local validate <job-id> --params <best>`, bootstrapping the cache env if missing; tests inject a fake.
  - Refusals (`ValueError` with a specific message):
    - a file hash differs from `results.json`;
    - `results.json.job_sha256` ≠ the sha256 of the local `job.json`;
    - `env_report` versions ≠ the pins;
    - a restart seed ≠ the settings seed;
    - a fitted value is outside its bounds;
    - zero successful restarts;
    - the reproduced score differs by more than `reproduce_rtol` relative.
  - `calibration_proposal.json`:
    - `{"kind":"calibration_proposal","schema_version":1,"job_id","job_sha256","results_sha256","mapping":{path,sha256,approval},"model":{"ode_session","revision","bundle_sha256"}}`;
    - `restarts`: `[{restart, seed, score, status, wall_time_s}]`, `partial`, `best_restart`, `good_restarts`;
    - `parameters`: `[{name, kind, unit, start, lower, upper, best, good_min, good_median, good_max, poorly_constrained}]`, where `poorly_constrained = log10(good_max/good_min) > 1.0`;
    - `heuristics_note`, `figures`.
  - `report.md`, human-readable, with figure links.
  - CLI: `python3 scripts/jobs/import_results.py <job-id>`.

- [ ] **Step 1: Write failing tests** with synthetic results archives:
  - one test per refusal above;
  - `test_partial_results_accepted_and_labelled`: 2 ok + 1 failed → `partial: true`, the failed restart excluded from `good_restarts`;
  - `test_good_restarts_and_poorly_constrained_flag`: best = 1.0, margin 0.1, scores [1.0, 1.05, 1.5]; good = first two; a parameter spanning 1e-3..1e-1 is flagged;
  - `test_proposal_kind_and_model_binding`.
- [ ] **Step 2: Run** `python3 -m unittest tests.data.test_jobs_import -v`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the tests and `python3 scripts/run_tests.py`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(jobs): verified result import and calibration proposal`.

### Task 6: Apply through the ode-modeler, hold-out scoring, docs

**Files:**
- Modify: `scripts/gate.py`:
  - `DATA_INPUT_LINE["ode_modeler"] = (0, "ode-modeler DATA_INPUT must be line 1 of the prompt")`;
  - new `_check_ode_calibration_input`;
  - `_model_input_kind` recognises `"kind"` `ode_mapping`/`calibration_proposal` (an ode_mapping or calibration proposal mentioned outside DATA_INPUT is refused, as for mappings).
- Modify: `.claude/scripts/stage_gate_guard.py` (`ode_modeler` added to the DATA_INPUT roles only; no `check_task` for ODE)
- Create: `scripts/jobs/score_holdout.py` (`python3 scripts/jobs/score_holdout.py <job-id> --approval <id>` writes `holdout_score.json` once)
- Modify (docs):
  - `.claude/agents/ode-modeler.md`, `.claude/skills/biomass-workflow/SKILL.md`: apply exactly the accepted values via the server, re-applying every other configured value (since `configure_model` replaces the configuration); report the new revision.
  - `CLAUDE.md`: a short paragraph, plus the `data-calibrated` tag in the provenance list.
  - `docs/data-workflow.md`, `docs/ode-workflow.md`, `docs/agentic-operations.md`.
  - `.claude/agents/scientific-reviewer.md`, `reproducibility-auditor.md`: a checklist for the mapping, the proposal and the hold-out.
- Test: `tests/codex/test_data_input_gate.py` (new class), `tests/data/test_jobs_holdout.py`, `.claude/scripts/test_agent_mcp_access.py` (pins)

**Interfaces:**
- Consumes: `approvals.require_calibration_approval`, the proposal shape from Task 5.
- Produces:
  - ode-modeler DATA_INPUT `{"calibration": "runs/orchestrator/<p>/jobs/<id>/calibration_proposal.json", "sha256", "approval"}`. Checks:
    - approval stage `ode_calibration` bound to that path and sha;
    - `outputs` non-empty and ⊆ the proposal's parameter names;
    - approval `session_id` == `proposal.model.ode_session`;
    - the prompt contains `record_session_id` with exactly that session, and no other `record_session_id` value.
  - `score_holdout(root, job_id, approval_id, *, runner=<callable or None>) -> Path`: uses the accepted subset with the other parameters at their starting values. By default `runner` runs `launch.py local validate <job-id> --use holdout --params <file>` (Task 3); tests inject a fake. Refuses if `holdout_score.json` exists.

- [ ] **Step 1: Write failing tests** through the real hook:
  - an accepted brief → exit 0;
  - wrong sha, wrong stage, outputs not in the proposal, a missing or different `record_session_id`, a second calibration proposal named in prose → exit 2;
  - existing NeKo/MaBoSS DATA_INPUT tests unchanged;
  - `test_holdout_written_once`.
- [ ] **Step 2: Run** the new tests. Expected: FAIL.
- [ ] **Step 3: Implement** the code and docs.
- [ ] **Step 4: Run** `python3 scripts/run_tests.py`, `python3 tests/integration/data_smoke.py` and `python3 scripts/check_isolation.py --repository-only`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(ode): apply approved calibration through the ode-modeler; hold-out scoring; docs`.

### Task 7: End-to-end smoke test

**Files:**
- Create: `tests/integration/ode_calibration_smoke.py`
- Modify: `docs/ode-workflow.md` (smoke section)

**Interfaces:**
- Consumes: everything above.
- Behaviour: run `python3 tests/integration/ode_calibration_smoke.py`. It prints one `[ok]` line per step and ends `ODE CALIBRATION SMOKE PASSED`, or `SKIP: <reason>` (exit 0) when `uv` or the network is unavailable. Steps, in a temp repo copy, with `XDG_CACHE_HOME`/`XDG_CONFIG_HOME` pointed into the temp dir:
  1. Build the tiny Text2Model model in a uv env (`biomass==0.14.0`). This is the same model as the plan's spike:
     - reactions `E + R <--> ER | kf=1e-2, kr=1e-1 | E=10, R=5`, `ER --> pER | kf=1e-1`, `pER --> ER | kf=5e-2`;
     - observable `pR: u[pER]`.
  2. Zip it as `runs/ode-modeler/<s>/captures/c1/export.zip` with `requirements.txt`.
  3. Write synthetic measurements at times including 7.5 min, generated with known `kf2`.
  4. Draft, fill and check `ode_mapping`, then record its approval through `state.py record`.
  5. Build the bundle with 2 restarts (`maxiter=3`).
  6. `launch.py local start`; wait for completion; `fetch`.
  7. Run import with the real reproduce. The best score must be lower than the nominal-score validate.
  8. Record the `ode_calibration` approval.
  9. Run `check_data_input` for an ode-modeler brief: accepted, and a wrong sha is refused.
  10. Score the hold-out once.
  11. `launch.py print` with a fake host; the commands are printed.

- [ ] **Step 1: Write the smoke script.**
- [ ] **Step 2: Run it.** Expected: `ODE CALIBRATION SMOKE PASSED` (or `SKIP` offline). If it fails because of Tasks 1–6 code, report BLOCKED with evidence.
- [ ] **Step 3: Run** `python3 scripts/run_tests.py` and `python3 scripts/check_isolation.py --repository-only`. Expected: OK.
- [ ] **Step 4: Commit** `test(ode): end-to-end calibration smoke (mapping, bundle, local run, import, gate)`.
