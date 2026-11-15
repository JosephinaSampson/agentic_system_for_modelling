# ODE formulation and BioMASS workflow

## Availability

The ODE route uses the richer `ode_modeler` contract (Claude: the `ode-modeler`
agent; Codex: the dispatcher's shared runtime). Configure it with
`python scripts/setup.py --with-biomass` (see [setup](automatic-setup.md)).

### ODE readiness check

Start scientific ODE work only while this check passes for the backend version pinned
in `setup/dependencies.toml`. Rerun it after any backend upgrade or transport change.
A source profile or a passing software fixture alone is not proof of readiness.

1. **Real-backend smoke test:**
   `uvx --from mcp-biomodelling-servers==<pinned> python -B tests/integration/biomass_smoke.py`
   must report all eight checks passed.
2. **Live isolation check:** dispatch `ode-modeler` with a read-only infrastructure
   task: list its `mcp__biomass-agent__*` tools with ToolSearch, call only
   `list_sessions`, and report any other visible modelling or delegation tools. It must
   see BioMASS tools only (without the blocked `close_session` and
   `clean_generated_files`), no NeKo, MaBoSS or PhysiCell tools and no Task/Agent tool,
   and `list_sessions` must answer. The orchestrator itself must show no modelling tools.

**Last verified:** 2026-10-28 with `mcp-biomodelling-servers` 2.5.2 (plugin launch).
The smoke test passed all eight checks. `ode-modeler` saw 20 BioMASS tools, no other
modelling or delegation tools, and `list_sessions` answered. Its `import_neko_handoff`
schema showed the 2.5.2 `handoff_bundle` parameter, confirming the upgraded launch;
the check must run in a client session started after the launches were configured.

## Formulation and inputs

Compare the objective, observables, available measurements, molecular-state
resolution, mechanisms, time scales and parameter uncertainty before recommending
Boolean dynamics, mechanistic ODEs, or both. Record the researcher's choice in
`MODEL_SPEC.md` and `DECISIONS.md`. Comparisons use separate sequential sessions
and representation provenance; sharing a topology requires approval. Successful
execution with hypothetical numbers establishes executability only.

| Input | Required provenance |
|---|---|
| NeKo graph | Approved topology/evidence, an exported `neko-to-biomass` handoff, exact upstream session and verified project copy |
| Standalone Text2Model | Explicitly authorized source, original text/hash and null upstream lineage |
| Reaction construction | Explicit bounded construction request, source identifiers and null upstream lineage |

The NeKo specialist performs `export_biomass_handoff` only under an approved export
workflow. Preserve edge IDs, references and biological context. BNET rules are not
biochemical reactions; signed edges do not establish molecular composition or
kinetic laws. BNET and Boolean connectivity constraints are not prerequisites for
the ODE handoff. Topology choices still retain their existing approval gates.

## Bounded authoring workflow

1. Resolve input and active lineage from durable files. For NeKo input, require
   the source registry row to have `Handoff=exported` and verify the graph handoff.
   Create a fresh BioMASS session for a new hypothesis through the ODE specialist.
2. Inspect exact identifiers, document version, evidence, configuration and coverage.
   Return unresolved mechanism, kinetic-law and quantity claims to the orchestrator
   for bounded literature review. The ODE specialist never delegates or searches.
3. Assemble the requested candidate within the approved drafting scope. Keep
   proposed mechanisms, molecular-state mappings, approximations, units and
   numerical assumptions distinct from accepted inputs. When the researcher requests
   a consolidated whole-model review, collect its unresolved choices together;
   do not turn ordinary candidate assembly into repeated per-reaction approvals.
4. Preview edits with the expected authoring version. Apply only edits within the
   authorized scope. Preserve stable IDs and source-edge coverage, including
   indirect/combined or unresolved edges. Never add dummy reactions or silently
   drop scientific scope. Inspect generated names before configuring quantities.
5. Whole-document, reaction-list or configuration replacement may clear settings.
   Preserve and reapply only previously approved values after inspecting the change.
   Validate, generate and inspect equations and quantity origins. Unknown units
   remain unknown; generated defaults remain proposed placeholders.
6. Preserve reproducible candidate snapshots within the already approved workflow.
   Use `export_status=provisional` and the workflow authorization record specified
   in [the artifact contract](ode-contract.md). This needs no repeated approval for
   each snapshot and grants no scientific acceptance or permission to simulate.
7. Run only requested scenarios with explicit time bounds, conditions and approved
   numerical choices. Placeholder use requires explicit approval of the actual
   values. Regenerate after authoring changes; any requested old revision must be
   identified as historical. Optional graph rendering may be skipped if unavailable.
8. Present the complete model and results for review. Conclusive bundle export and
   stage acceptance require the exact session/revision approval in the artifact
   contract. ODE-to-PhysiCell coupling and sensitivity analysis remain outside this
   integrated workflow; calibration is the separate route below.

## Calibrating parameters to measurements

Calibration is a portable job the researcher approves step by step; no specialist fits anything. Steps:

1. **Measurement table to mapping.** A measurement table (from an approved `ode_measurements` data
   proposal, or a CSV under `runs/orchestrator/`) is mapped onto a captured, accepted model revision
   (`ode_mapping.py draft`, fill, `check`). The mapping fixes observables, condition overrides, comparison
   rules, fitted parameters with bounds and justification, hold-out rows (`use=holdout`) and the reasons
   for any unmapped row. `check` requires at least one `use=fit` row scored by a rule (a contrast
   reference row is not scored), every endpoint-contrast row and every dose row measured at the rule's
   time once converted (`time.factors`; compared with relative tolerance 1e-9: a rule `"time": 24` meant
   as hours against a minute model is refused, not silently read as 24 min), and the rows of a
   dose-response rule to have a readout mapped to `viability.observable`.
2. **Approval.** The researcher approves the mapping: `ode_mapping` `stage_approval` (`session_id` = the
   ODE session, `outputs` = measurement ids used plus fitted names, `artifact` = path and sha256).
3. **Build.** `build.py --mapping ... --approval ... --settings ...` writes the deterministic bundle and
   `job.json` under `runs/orchestrator/<p>/jobs/<job-id>/`. Settings come from
   `templates/calibration-settings.{first-pass,final}.json`; the BioMASS differential-evolution defaults
   stop early when few parameters are fitted, so raise `popsize` and `maxiter` in the settings.
4. **Launch.** `launch.py local start|status|fetch`, or `ssh submit` (a host from `hosts.json`, the
   researcher's authorization quote, logged to `dispatch.jsonl` before anything runs), or `slurm submit`
   on a login node. Only the bundle (model, the measurement rows the rules use, parameter bounds) leaves
   the machine. `fetch` places `results.tar.gz` under `inputs/calibration-results/<job-id>/`.
   `workers: -1` (scipy uses every core inside one restart) makes an ssh host run one restart at a time
   (K = 1); under SLURM it means the task's allocation (`slurm.cpus_per_task`).
   **SLURM return path, end to end:** on the login node, `launch.py slurm submit <job-id>` (or the printed
   `sbatch` and `echo <id> > state/slurm_job` lines) from the unpacked job folder; follow it with
   `bash launch/remote.sh status` there; when status shows every restart done (or failed), run
   `bash launch/remote.sh collect` there, which writes `<job folder>/results.tar.gz`; bring that file back
   to this repository with `mkdir -p inputs/calibration-results/<job-id> && scp <cluster>:<job folder>/results.tar.gz inputs/calibration-results/<job-id>/`
   (or `launch.py ssh fetch <job-id> --host NAME` when the cluster is a configured host whose
   `remote_dir` holds the job), then import.
   **Partial fetch or import:** do not fetch before `status` shows the run done: `fetch` never overwrites,
   so an early (partial) archive stays the job's archive. A failed import writes nothing and can be rerun
   after the cause is fixed. To run again (more restarts, other seeds or settings), change the settings and
   rebuild: that is a new job id with its own folders; a job's fetched archive and proposal are never
   replaced.
5. **Import.** `import_results.py <job-id>` verifies the archive (hashes, environment pins, seeds, bounds)
   and reproduces the best score locally, then writes `calibration_proposal.json`, `report.md` and figures.
   The reproduce step needs internet: it builds the job's own environment under
   `~/.cache/agentic-jobs/<job-id>/` (uv, Python 3.12, the pinned packages; minutes the first time) even
   for ssh and slurm jobs, and also computes the nominal values (`start`) locally from that verified copy
   of the bundle; the remote `selftest/validate.json` is never used for them.
   Nothing reaches the model. Partial results (failed or missing restarts) are flagged `partial`;
   parameters whose good restarts span more than a decade are flagged `poorly_constrained`; restart
   agreement is a heuristic, not an identifiability proof.
6. **Calibration approval.** The researcher reviews the report and approves a subset of parameters:
   `ode_calibration` `stage_approval` with `session_id` = the proposal's ODE session, `outputs` = the
   accepted parameter names and `artifact` = the proposal path and sha256.
7. **Apply.** Dispatch ode-modeler with line 1
   `DATA_INPUT {"calibration": "runs/orchestrator/<p>/jobs/<id>/calibration_proposal.json", "sha256": "...", "approval": "<record id>"}`
   and `record_session_id` = that ODE session (no other value, no other proposal or ode_mapping file named).
   The hook checks the approval, hash, outputs, session and `record_session_id`; the specialist applies
   exactly the accepted `best` values (only when the session's current revision equals
   `proposal.model.revision`; otherwise it returns `blocked`) and re-applies every other configured value unchanged
   (`configure_model` replaces the configuration), then reports the new revision. Tag the values
   `data-calibrated`; register the new revision with Derived from = the calibrated session, the job id,
   proposal short hash and approval id. The new revision needs the usual review and approval.
8. **Hold-out.** `score_holdout.py <job-id> --approval <id>` scores the held-out rows once with the accepted
   parameters at their best values and every other fitted parameter at its start value, and writes
   `holdout_score.json` with `n_rows`, the hold-out rows that received a prediction. It never overwrites;
   a null start for a non-accepted parameter refuses, and so does a run in which no hold-out row received a
   prediction (an objective over no rows is 0, not a score). It runs
   the bundled model with the accepted values, so it is independent of when the ode-modeler applies them:
   run it once, after the calibration approval.

The dispatch hook also refuses an ODE brief that names a data-analyst path or session id, or an orchestrator seeds/mapping/ode_mapping/calibration file other than the `DATA_INPUT` artifact, without a `DATA_INPUT` line.
For every gated specialist it refuses any path under a calibration job folder (`runs/orchestrator/<p>/jobs/`,
e.g. `report.md`, `holdout_score.json`, figures, the bundle) or under `inputs/calibration-results/`; the only
exception is the ode-modeler's `DATA_INPUT` proposal itself and its own job folder. With the calibration
`DATA_INPUT` it also checks that the proposal's `mapping` is an approved `ode_mapping` and that its
`job_sha256` is the sha256 of the `job.json` beside it. The hook records a malformed or ambiguous
`record_session_id` (any case or separator, joined to another id) as a refusal.

## Dispatch and isolation

Codex uses `specialist_dispatcher.start_ode_modeler(task=..., approved_tools=...)`.
For an existing BioMASS session pass `record_session_id`; for a new session put the
upstream NeKo ID in the task, never in that argument. Include exact sources,
objective, input kind, authorized edits, evidence paths, proposed versus accepted
assumptions, requested scenarios and required artifacts. Exact tool permissions
express existing authorization and do not replace scientific decisions.

Read `get_specialist_events` with its sequence cursor and `get_specialist_task`
until terminal. Only `succeeded` exposes a validated handoff; its scientific status
may still be `blocked` or `needs_approval`. Preserve failed invocation artifacts.
Do not silently retry mutations after interruption or infer a live session from a
past process. Reconstruct from approved source artifacts in a fresh workflow when
that reconstruction is authorized. `restore_session` applies only to an explicitly
approved existing server session; a portable ZIP is not a server session snapshot.

CLI fallback uses the same runtime:

```text
python scripts/codex/run_specialist.py ode_modeler --prompt-file .codex-tasks/ode-task.txt
```

Add `--record-session-id` and exact `--approve-tool` entries only as applicable.
Claude delegates to `ode-modeler` with its inline BioMASS transport and local
`biomass-workflow` references. No other specialist or orchestrator may expose
BioMASS. Codex inventory preflight and execution share fixed overrides; Claude
inline isolation still requires client-level verification. Neither specialist
changes shared scientific files or advances stages.

## Returning a candidate before project capture

A new-session specialist invocation may run the complete authorized workflow through
server-side bundle export. Pass the actual researcher authorization and scope in the
task; bind its preservation record to the returned session ID during parent recording.
Do not require a bootstrap invocation solely to obtain that ID or assume session
continuity for the next workflow.

Before verified repository copies exist, the read-only specialist returns metadata-only
`needs_approval`, `ode.export_status=pending`, empty project `artifacts` and
`simulation_paths`, and the server inventory in `server_artifacts`. Omit project
capture/bundle/authority fields; include actual revision/version and report drafts.
Name parent recording work separately from scientific choices. The parent captures
and verifies files and writes a **separate** provisional completion with the matching
manifest, report, authorization snapshot and hashes. It preserves the original
specialist result. This finalization consumes the existing authorization and does
not ask for another decision just to preserve work.

The current envelope requires nonempty `decisions_required` for `needs_approval`.
List operational work there with the prefix `Technical parent recording only`,
for example: “Capture and verify the inventory, record the already approved
preservation authority, and create the provisional completion; no new researcher
approval is requested.” Keep actual scientific decisions as separate entries.
The parent performs this recording work rather than sending it as an approval question.

The server label `assumed` does not establish accepted science: distinguish proposed
mechanisms from those accepted by a recorded researcher decision. Small-subsystem
examples guide internal assembly, not external per-module approval cycles.

## Evidence review

Use the existing literature specialist with explicit `review_kind=ode` and the
full BioMASS session ID. Send at most 13 coherent literal claims, each with a stable
claim ID, kind (`mechanism`, `kinetic-law`, `quantity`), context, known PMIDs/DOIs,
and originating reaction/edge/quantity IDs. No NeKo SIF is needed for standalone
ODE work. At most two independent reviews may run concurrently across both modes.
PubMed is preferred; Codex web search needs explicit authorization. Without a
permitted backend return `blocked` rather than infer evidence.

Codex dispatches `start_literature_reviewer(..., review_kind="ode",
record_session_id=...)`; CLI fallback adds `--review-kind ode`. The reviewer returns
report drafts. The orchestrator validates/writes them through
`write_literature_report.py --session-id SESSION --claim-id CLAIM --draft-file DRAFT`.
Claude writes through its literature guard and reads the exact destination back.

Reports are immutable at `evidence/reports/{biomass_session_id}/ode/{claim_id}.md`;
invocation provenance belongs under `runs/ode-modeler/{biomass_session_id}/`.
Retry only missing reports. Explicit re-review uses a new claim ID and identifies
what it supersedes. The [evidence contract](ode-contract.md#ode-claim-evidence)
requires access limitations, source context, support/conflict and reported quantities
with original units or null. Review evidence for mechanisms and numerical values
separately. Import reviewed findings with stable evidence IDs and source links.

## Artifacts and validation

Use common envelope version 1, role `ode_modeler`, stage `biomass_ode`, and
`ode.contract_version=2`. The [artifact contract](ode-contract.md) is authoritative
for metadata, revision checks, provisional/conclusive authority and capture helpers.
Returned external files belong in `server_artifacts`, not the project artifact list.
The orchestrator captures only explicit returned inventories, writes the report and
matching manifest, and validates the completed project result before synthesis.
Preserve the immutable original specialist result and each completion capture.

Generated-revision claims require complete captured files and hashes. Simulation
claims additionally require matched revision requests, numerical conditions and
species/observable trajectories. Provisional and accepted bundles receive the same
integrity checks. The registry remains `pending` during review; a provisional
snapshot never sets `Handoff=exported` or advances the global stage.

The existing lifecycle allowlist already includes `runs/` and `evidence/`. Claude's
stage validation requires the read-only `scientific-reviewer` and
`reproducibility-auditor` agents in `.claude/agents/`. Their source definitions
are present; actual deployment and execution must still be verified in Claude. Codex parent validation
is not independent scientific review. Syntax, numerical execution and biological
validity remain separate claims.

### Recover recording from a completed specialist invocation

When an ODE specialist returns a full artifact list but omits redundant per-entry
session IDs, use the same invocation's successful `list_generated_files` result
to establish ownership. Do not insert the current session ID based on context.

```sh
python scripts/codex/record_ode_artifacts.py \
  --artifact-root ~/.cache/mcp-biomodelling/artifacts/BioMASS \
  --session-id SESSION --capture-id candidate \
  --invocation /absolute/project/runs/ode-modeler/SESSION/specialist-invocations/INVOCATION
```

`--artifact-root` is the per-server directory 2.5.0 writes to
(`$MCP_BIOMODELLING_ARTIFACT_ROOT/BioMASS`, default shown above). `--server-root`
remains for legacy installations that kept `artifacts/` inside the package.

This mode validates the recorded typed result and clean invocation provenance,
requires the matching server catalogue, and compares every path, size and session.
The ordinary recorder then verifies all hashes and copies the artifacts. Source
handoff/event records remain unchanged, and their hashes are linked in
`capture.json`. Missing, conflicting or failed inventory evidence blocks recording.
`--inventory` remains available for an already explicit, fully owned inventory.

**Fixed in 2.5.1.** BioMASS 2.5.0 omitted every file whose absolute path contained
a `.cache` component, which included the default artifact root
`~/.cache/mcp-biomodelling/artifacts`. Under that root it listed no files, sealed
empty integrity inventories and exported bundles without model artifacts. 2.5.1
fixes this. A revision generated by 2.5.0 under the default root has an empty
inventory and now fails verification; regenerate it with 2.5.1 rather than
capturing it.

### Smoke test

`tests/integration/ode_calibration_smoke.py` drives the whole data-to-ODE calibration chain with a
tiny real BioMASS model (about 40 s):

```sh
python3 tests/integration/ode_calibration_smoke.py
```

In a temporary repository copy under `~/.cache` (job cache, uv downloads and configuration are
pointed there) it generates the model with Text2Model (`biomass==0.14.0`), writes synthetic
measurements from a known `kf2` (including a 7.5 min point and one hold-out row), drafts, checks
and approves the `ode_mapping`, builds the 2-restart bundle, runs it locally (real bootstrap,
selftest, collect), imports the results with the real reproduce (best score below the nominal
score), records the `ode_calibration` approval, passes the ode-modeler `DATA_INPUT` gate through
the real hook (a wrong sha is refused), scores the hold-out once and prints the `launch.py print`
commands. It also asserts that nothing is added to `~/.local/bin`. The scratch folder is removed
on success and kept (path printed) on failure.

It prints `SKIP: <reason>` and exits 0 when `uv` is missing or the network is unreachable (it
needs GitHub for uv and PyPI for biomass); any other failure exits 1. Like the backend smoke test
it is a software check on synthetic data, not scientific evidence.

### Real-backend smoke test

`tests/integration/biomass_smoke.py` exercises the released BioMASS backend end to
end in temporary storage. It covers generation, simulation, bundle reproduction,
project capture, typed v2 completion, edit preview, stale-version rejection and
placeholder rejection. CI runs it through the plugin launch:

```sh
uvx --from mcp-biomodelling-servers==2.5.2 python -B tests/integration/biomass_smoke.py
```

It is a software regression check with the server's example values, not
evidence for any scientific model.

Recording does not rerun any specialist or simulation and does not manufacture a
completed scientific handoff. Existing captures are never overwritten. After an
interruption, inspect existing receipts and artifacts; any later recording uses
an explicit invocation/capture ID, not automatic scientific execution. The parent
still verifies model meaning, accepted assumptions and the stage completion.
