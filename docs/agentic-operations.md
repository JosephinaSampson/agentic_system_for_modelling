# Checked agentic operations

These helpers implement Point A of the 2026-10-05 diagnostic. They do not approve
scientific choices or repair missing server capabilities. Run from the repository
root with Python 3.11+; handoff capture uses the configured modelling environment
for its pure artifact-contract validators. No helper below calls modelling MCPs.

## Setup, isolation and PubMed

`python3 scripts/check_isolation.py --repository-only` is the CI check.
Without that flag it also checks `~/.claude.json`; supply `--codex-config PATH`
to inspect a user Codex configuration. Setup checks skill snapshots against the
installed/pinned backend version. An unsafe registration or permission is a failure.
The four Claude transports use distinct `neko-agent`, `maboss-agent`,
`physicell-agent`, `biomass-agent` names and remain specialist-inline only.

The guard's `--preview` lists planned paths. Its explicit `--apply` removes only
modelling registrations at global/current-project scope and matching local grants;
unrelated projects and PubMed remain intact. Private backups are saved under
ignored `.setup/backups/isolation/`. To add PubMed to an existing isolated profile,
pass `--literature-profile PATH`; custom transports/explicit disabled choices are
preserved. Fresh setup uses `https://pubmed.mcp.claude.com/mcp` and restricts Codex
to the seven read-only PubMed tools. Web search remains separately authorized,
disabled by default, with no silent fallback after an endpoint failure.

Codex supports remote URL MCP transport ([official configuration example](https://developers.openai.com/learn/docs-mcp)).
The dispatcher resolves its configured Codex home; that may differ from the desktop
chat's home. A working chat connector does not configure a subprocess. Verify the
actual profile, transport and restricted inventory. The bounded connectivity check is
`python3 scripts/setup_support/probe_pubmed.py --python /path/to/modelling/python`
(initialization and tool listing only). Restart clients after changing
registration: static checks cannot revoke tools already granted to a running client
or prove specialist authentication/connectivity. Keep the existing ODE readiness gate.

## State updates

`python3 scripts/state.py OPERATION --input request.json` reads a JSON file and
atomically updates one checked Markdown source of truth. The input is data, not
shell text. Do not interpolate researcher prose into commands. The helper rejects
missing/duplicate rows, malformed tables, traversal, symlinks and bad transitions.
Timestamps are timezone-aware UTC ISO-8601. Preserve original source timezone offsets
in provenance; convert before comparing dates across midnight.

| Operation | Input |
|---|---|
| `decision` | Decision, Alternatives considered, Rationale, Evidence, Approved by; Date generated |
| `registry-add` | Exact seven CURRENT_STATE registry columns; full unique session ID, matching role/stage, active status |
| `registry-supersede` | `session_id`; retains the historical row |
| `registry-handoff` | `session_id`, `handoff`; pending → approved → exported only; export includes verified `artifact` |
| `record` | One structured governance record described below |
| `validation-declare` | Criterion, Method, Required evidence; creates a unique pending criterion |
| `validation` | Supported artifact-validator JSON output; only updates already declared criterion IDs |
| `checkpoint` | Full content-commit SHA and `summary`; no self-referential metadata commit |
| `data-product` | Exactly `name`, `type`, `meaning`, `unit`, `identifiers`, `source_session` (data session ID), `path`, `sha256` (verified), `release`, `decision_id`, `notes`; appends a `DATA_DICTIONARY.md` row. `decision_id` must be the `id` of the researcher's `stage_approval` (stage `data`, same `session_id`, `artifact` = that session's unchanged `handoff.json`), recorded with `record` after `receive.py` prints `RECEIVED <id> OK`; the product must be a handoff artifact and a proposal's id must be in the record's `outputs` (see [data workflow](data-workflow.md)) |

An artifact reference is `{"path":"repository/relative/file","sha256":"..."}`.
Hashes are verified, not accepted as annotations. A validation result contains
criteria and input hashes; a changed input invalidates recording. These are
record-keeping guarantees, not cryptographic proof that a researcher approved a
record or that a report is scientifically correct. Inspect diffs before checkpointing.

## Exact downstream gates

Record governance JSON with `state.py record`. Records are append-only fenced JSON
blocks marked `<!-- governance-record -->` in DECISIONS.md, avoiding a competing
approval database. Every record has a unique `id`, `kind`, `stage`, and full
`session_id` (except standalone authorization).

Before downstream dispatch, independent review must finish; persist both reports in
`runs/orchestrator/reviews/<session>/`. Separate researcher approval is still required
before producing a conclusive export. A `stage_review` contains:

- `outcome`: approved, approved_with_limitations, revision_required or blocked;
- `artifact`: exact reviewed handoff path/hash;
- `scientific-reviewer` and `reproducibility-auditor`: report path/hash references.

Then record the researcher's `stage_approval` with `approved_by: "researcher"`,
`authorization` (exact quotation or durable request reference), `artifact`,
`downstream_stage`, and the exact ordered `outputs` list. A review recommendation
does not confer approval. Never invent authorization to satisfy the schema. Pending
scientific choices remain pending even if technical verification succeeds.

Run and quote:

```bash
python3 scripts/gate.py check boolean-dynamics FULL_NEKO_SESSION --outputs NODE_A NODE_B
python3 scripts/gate.py check multicellular FULL_MABOSS_SESSION --outputs READOUT_A READOUT_B
python3 scripts/gate.py check biomass_ode FULL_NEKO_SESSION --outputs NODE_ID_1 NODE_ID_2 ...
```

NeKo-to-BioMASS graphs carry no output selection, so for `biomass_ode` the
approval's `outputs` are every `node_id` in the exported `network_file`: the
researcher approves the whole graph's species scope. The ODE dispatch itself is
governed by the [ODE contract](ode-contract.md); the Claude dispatch hook enforces
`STAGE_GATE` only for the MaBoSS and PhysiCell specialists.

The checker requires the expected active upstream row with Handoff exactly
`exported`, a valid typed handoff and captured dependencies, matching outputs,
completed independent reviews and subsequent researcher approval. Changed hashes,
missing artifacts, a superseded session, or a later blocking review fail closed.

Boolean handoffs support versions 1.0, 2.0 and 2.1. Version 2.0 adds verified
source ancestry with embedded file snapshots; it does not grant approval of
derivatives. Version 2.1, written by `mcp-biomodelling-servers` 2.5.0, adds a
required `curation_file` NeKo history companion to NeKo-to-MaBoSS manifests and
the MaBoSS model revision plus explicit readout definitions (possibly empty) to
MaBoSS-to-PhysiCell manifests. Only declared readouts may extend the NeKo core.
NeKo-to-BioMASS 2.0 likewise names a `curation_file`. Capture and retain every
referenced file; approval binds to the exact NeKo state or MaBoSS revision.
The backend binds each `curation_file` to the model file's original absolute path,
so downstream import tools reject relocated copies: for these versions give the
importing specialist the recorder's `backend_import_manifest` (the original server
manifest, whose hash `relocation.json` records), while the project capture remains
the record that the gate and approvals bind to.
Before dispatching such an import, confirm that the manifest handed over is the
captured original:

```bash
python3 scripts/capture/verify_import_manifest.py --capture CAPTURE_DIR --manifest ORIGINAL_BACKEND_MANIFEST
```

Pass that manifest as `manifest_path`. Backend 2.5.2 also offers
`export_handoff_bundle` and a `handoff_bundle` import argument, plus `sif_text` and
`bnet_text` inputs, for hosts that give each server a private filesystem. All
specialists here share one host filesystem, so this project does not use them: a
bundle import reads copies from the consumer's `handoff-blobs/` store instead of
the original paths verified above, the bundle carries every referenced file
inline through the agent's context, and inline text replaces the preserved source
file as the recorded input. A
"manifest does not exist" error is a transport blocker to report.

The NeKo Boolean recorder also copies and hashes the `*.bnet.lineage.json`
source-lineage sidecar when the export produced one.
`scripts/boolean_contract.py` uses the backend's pure contract reader to validate
this metadata without following historical paths. If system Python lacks that
package, it uses the existing workstation Python selection from
`.setup/dispatcher.local.json` or `.setup/local.json`. An unavailable/old contract
package, invalid ancestry or timeout blocks dispatch; no dependency is installed
automatically. Current artifact paths must still remain inside the repository.
The Boolean capture commands accept explicit per-server `--artifact-root` storage
as well as the legacy installation `--server-root`; see
[capture instructions](../scripts/capture/README.md).

For a v2 MaBoSS source derivative, a registry `Derived from` NeKo session may match
the current session's verified **direct** source parent. Transitive historical
ancestors cannot satisfy that binding. This does not replace the current session's
review, exported status, exact artifact hash, ordered outputs or researcher approval.
An accepted typed stage parent retains its stricter matching node/output contract.

The bounded dispatch task begins with one literal line:

```text
STAGE_GATE {"upstream_session_id":"FULL_SESSION","outputs":["NODE_A","NODE_B"]}
```

Claude's Agent/Task PreToolUse hook and the Codex runtime check durable state rather
than trusting this prompt assertion. Codex checks new Boolean downstream sessions
before starting the child, with the hook's `check_data_input_and_stage_gate` (every
brief of a role with a `DATA_INPUT` form), then `check_task`; PhysiCell design/projection and ODE calibration
`DATA_INPUT` briefs are then refused before launch as Claude-only for now. Existing-session inspection without a valid gate is
restricted to audited non-creating tools: some backend methods named as reads
actually call `ensure_session` and can create sessions. Broader access needs the
gate. ODE authoring continues to use its own approved-source/readiness contract.

An explicitly authorized standalone source uses a `standalone_approval` record:
`id`, `kind`, target `stage`, researcher authorization, exact source `artifact` and
declared `outputs`. Its task envelope uses `standalone_decision_id` instead of
upstream_session_id. It records null upstream lineage rather than inventing a
handoff. This exception must be explicitly authorized, never inferred from a failed
import or missing artifact.

Conditional topology decisions use `kind: "conditional_approval"`, exact session,
researcher authorization, literal `edge: [source,effect,target]`, `evidence` path/hash, and `conditions` mapping supported
footer fields to string allowlists, for example `{"verdict":["supported"]}`.
`python3 scripts/gate.py resolve DECISION_ID` validates the evidence and returns
satisfied/unsatisfied plus supporting PMIDs. It never mutates topology, exports a
handoff, or authorizes an operation outside the recorded scope.

## Data inputs (`DATA_INPUT`)

The dispatch hook accepts data for network-curator, boolean-dynamics-modeler and
multicellular-configurator only through a `DATA_INPUT` line, checked by
`scripts/gate.py check_data_input` (the ode-modeler form is below):
- network-curator, line 1: `DATA_INPUT {"data_session": "...", "proposals": {"P1": "<sha256>"}}`
  (approved `seed_genes` proposals, hashes equal to manifest and file);
- boolean-dynamics-modeler, line 2 after `STAGE_GATE`:
  `DATA_INPUT {"mapping": "runs/orchestrator/<p>/mapping.json", "sha256": "...", "approval": "<record id>"}`.
  The approval is a `stage_approval` for stage `data_mapping` on that mapping and hash,
  whose `outputs` equal the mapping's perturbation and criterion ids; `mapping.py check`
  must pass, the mapping's NeKo session must equal the `STAGE_GATE` upstream (the approval's
  `session_id` is that NeKo session) and its BNET sha256 must equal the approved NeKo
  handoff's BNET.

- network-curator, line 1, seeds form: `DATA_INPUT {"seeds": "runs/orchestrator/<p>/seeds.json", "sha256": "...", "approval": "<record id>"}`
  (exactly one of the two forms). The approval is a `stage_approval` for stage `data_seeds` on
  that file and hash, with `session_id` = `<p>` and `outputs` = the seed symbols;
  `seeds.py check` must pass. The curator takes genes from `hgnc_symbol`.
- boolean-dynamics-modeler with a researcher BNET: `STAGE_GATE {"standalone_decision_id": "...", "outputs": [...]}`
  then the same mapping `DATA_INPUT`; the mapping network must carry that
  `standalone_decision_id`, and the approval's `session_id` equals it.
- multicellular-configurator, line 2 after `STAGE_GATE`, exactly one of:
  `DATA_INPUT {"design": "runs/orchestrator/<p>/physicell_design.json", "sha256": "...", "approval": "<record id>", "record_session_id": "..."}`
  or
  `DATA_INPUT {"projection": "runs/orchestrator/<p>/physicell_projection.json", "sha256": "...", "approval": "<record id>", "record_session_id": "..."}`.
  The file passes its `check` on exactly those bytes, the approval (stage `physicell_design` or
  `physicell_projection`) binds its path and sha256, and `record_session_id` is the approval's
  `session_id` (for a design, its anchor: the basis capture's session, else `<p>`). Design: every MaBoSS handoff it lists lives under
  `runs/boolean-dynamics-modeler/<STAGE_GATE upstream_session_id>/`; with a `standalone_decision_id` it lists
  none. Projection: `record_session_id` is also its capture's session, and its design and design approval are
  re-checked, and its `STAGE_GATE` must name the design's MaBoSS upstream (as for a design). `record_session_id` is a lineage anchor: the
  configurator starts a new session.

NeKo data approvals are cumulative: a proposal is approved if any valid `data` approval of the
session, binding the current handoff sha256, lists it. There is no revocation record; to
withdraw a proposal, re-run the data analysis in a new session (new handoff). The
mapping approval is looked up by its exact `approval` id. Any other occurrence of
`DATA_INPUT` (any case or separator) in the prompt, a `runs/data-analyst` path or a data
session id without the line, is refused, and any hook error denies the dispatch. Never
put data values in a brief. `state.py record` accepts `data_mapping` as a governance
stage for `stage_approval` and `stage_review`, and `data_seeds` likewise.

Mapping commands (details in docs/data-workflow.md):

```
python3 scripts/data/mapping.py draft --purpose P --data SESSION:PROPOSAL:APPROVAL ... --network NEKO_SESSION:BNET
python3 scripts/data/mapping.py draft --purpose P --source ID:inputs/FILE ... --standalone-network DECISION_ID:BNET   # sources only, researcher BNET
python3 scripts/data/mapping.py draft --from OLD_MAPPING --purpose P2 --network NEKO_SESSION:NEW_BNET                  # redraft after a re-export
python3 scripts/data/mapping.py check runs/orchestrator/P/mapping.json [--stage-gate '{"upstream_session_id": "...", "outputs": [...]}']
python3 scripts/data/seeds.py check runs/orchestrator/P/seeds.json
python3 scripts/data/mapping.py criteria runs/orchestrator/P/mapping.json --runs runs/orchestrator/P/runs.json --approval <data_mapping record id> [--merge-into VALIDATION_CRITERIA.yaml]
```

Procedure: draft, discuss with the researcher, fill, `check`, researcher approval
(`data_mapping`), dispatch, then `criteria` from the reported trajectories and
`scripts/validate/evaluate.py` (`criteria` refuses a mapping without its approval). Where the
context and perturbation fix the same node, the perturbation overrides the context.
`scenario_contrast` is documented in
`scripts/validate/README.md`.

### ODE calibration jobs and applying their result

```
python3 scripts/ode/ode_mapping.py draft --purpose P --ode-session S --bundle ZIP --measurements CSV [--revision R] [--source ID:PATH ...]   # revision derived from the bundle
python3 scripts/ode/ode_mapping.py check runs/orchestrator/P/ode_mapping.json
python3 scripts/jobs/build.py --mapping runs/orchestrator/P/ode_mapping.json --approval <ode_mapping record id> --settings SETTINGS.json
python3 scripts/jobs/launch.py local start|status|fetch <job-id>
python3 scripts/jobs/launch.py local validate <job-id> [--params FILE [--partial]] [--nominal] [--use fit|holdout|all] [--out FILE]   # holdout|all: after the ode_calibration approval only
python3 scripts/jobs/launch.py print <job-id> --host NAME                 # the scp/ssh commands, runs nothing
python3 scripts/jobs/launch.py ssh submit <job-id> --host NAME --authorization "<researcher quote>"
python3 scripts/jobs/launch.py ssh status|fetch|cancel <job-id> --host NAME
python3 scripts/jobs/launch.py slurm submit <job-id> [--resubmit [--force]]   # --force: sacct cannot tell; check squeue first
python3 scripts/jobs/import_results.py <job-id>
python3 scripts/jobs/score_holdout.py <job-id> --approval <ode_calibration record id>
```

`hosts.json` (`~/.config/agentic-modelling/hosts.json`) maps names to
`{"ssh": alias, "remote_dir": str, "max_cores": int}`; ssh always runs with `BatchMode=yes`. `ssh submit` appends
`{"time", "host", "ssh_alias", "bundle_sha256", "authorization"}` to the job folder's `dispatch.jsonl` before
anything leaves the machine, and only the bundle is sent. The `ode_mapping` and `ode_calibration` stages are
governance stages of `state.py record`. The ode-modeler hook (`stage_gate_guard.py`) accepts
a `DATA_INPUT {"calibration": ..., "sha256": ..., "approval": ..., "record_session_id": ..., "accept": [...]}` on
line 1 only. Shared with `score_holdout.py` (`approvals.require_calibration_approval`): approval stage
`ode_calibration` bound to that path and sha256 and passing `validate_record`, `outputs` a nonempty subset of
the proposal's parameter names, approval `session_id` = the proposal's ODE session, and the proposal bound to
its job (the proposal's `mapping` an approved `ode_mapping`, the `job.json` beside it matching its
`job_sha256`, `job_id` (= the folder name) and `mapping`; `approvals.require_proposal_job`). Gate-only
(`gate._check_ode_calibration_input`; `score_holdout.py` takes no such payload): `record_session_id` = the
proposal's ODE session and `accept` exactly the approval's `outputs`, and the brief may name no existing
BioMASS session (a directory under `runs/ode-modeler/`) other than `record_session_id`. Any other calibration
proposal or `ode_mapping.json` named in the brief, or a payload-like `DATA_INPUT` elsewhere, is refused; errors
deny. Other prose is not parsed for values: the modeler takes the session and names only from the line. For every gated specialist the hook refuses any path under `runs/orchestrator/<p>/jobs/`
or `inputs/calibration-results/` except the ode-modeler's `DATA_INPUT` proposal and its own job folder.
`import_results.py` computes `start` (nominal values) locally in the same `validate --nominal` run that
reproduces the best score (needs internet; builds `~/.cache/agentic-jobs/<job-id>/`); the archive's selftest
values are ignored. `score_holdout.py` writes `holdout_score.json` once, complete or not at all (temp file, fsync,
`os.link`), with `n_rows` (only the accepted
parameters are passed, at best; the others keep the model defaults); it writes nothing when no hold-out row
was predicted, any hold-out prediction failed or the score is the penalty.

### PhysiCell design, projection and verification

Discovery (a new configuration only): dispatch the multicellular-configurator with `STAGE_GATE` and no `DATA_INPUT`
to call `get_available_cycle_models`; the design's `cycle_models` copies the recorded `{key, name}` output. The
stage-A capture passes validate-stage before the projection is drafted against it, the verified stage-B capture
before build or closure. Registry: stage A `Derived from` = the `STAGE_GATE` upstream MaBoSS session or standalone
decision id (plus the basis capture's session), Artifacts the design path and short sha256; stage B `Derived from` =
the stage-A session, Artifacts the projection path and short sha256 and the verification file.

```
python3 scripts/physicell/physicell_design.py draft --purpose P [--basis runs/multicellular-configurator/S/configs/C] [--source ID:inputs/FILE ...] [--data ID:SESSION:PROPOSAL:APPROVAL ...]
python3 scripts/physicell/physicell_design.py check runs/orchestrator/P/physicell_design.json
python3 scripts/physicell/capture_dictionary.py runs/multicellular-configurator/S/invocation/ID/tool_calls.json runs/multicellular-configurator/S/configs/C runs/orchestrator/P/physicell_design.json
python3 scripts/physicell/physicell_projection.py draft --purpose P --capture runs/multicellular-configurator/S/configs/C --design runs/orchestrator/P/physicell_design.json --design-approval RECORD_ID [--dictionary FILE] [--source ...] [--data ...]
python3 scripts/physicell/physicell_projection.py check runs/orchestrator/P/physicell_projection.json
python3 scripts/physicell/verify_export.py runs/orchestrator/P/physicell_projection.json runs/multicellular-configurator/S2/configs/C2   # exit 0 passed, 1 failed, 2 refused
python3 scripts/physicell/build.py --template template|template_BM [--version 1.14.2] [--dest DIR] [--verification runs/orchestrator/P/physicell_verification.S2.C2.json] [--jobs N]
```

`physicell_design` and `physicell_projection` are governance stages of `state.py record`
(`approvals.require_physicell_design_approval`, `require_physicell_projection_approval`): design approval
`session_id` = the design's anchor (the basis capture's session, else `P`), `outputs` = the cell definition and
substrate names; projection approval `session_id` = the capture's session, `outputs` = the entry ids. The
dictionary is written once beside the design, from list calls made after the last structure-changing call, and
only for a capture bound to the design by dispatch lineage (`scripts/physicell/lineage.py`; the rule is stated once,
in docs/data-to-physicell-architecture.md, "Dispatch lineage"); the projection check and `verify_export.py` require
the same. `verify_export.py` writes `runs/orchestrator/P/physicell_verification.<session>.<capture>.json` (one file
per attempt). `build.py` copies settings only from a passed verification whose capture is unchanged and
whose recomputation (`verify_export.compute`) passes with the same result, and never runs the project. Details: docs/data-workflow.md.

## Evidence and provenance

Edge reports end in exactly one fenced `yaml` block using JSON syntax. Ordinary
YAML aliases/tags/unquoted fields are deliberately unsupported so the guards need
no parser dependency. Example footer syntax (illustrative identifiers/citations):

```yaml
{"verdict":"supported","interaction_type":"activation","confidence":"high","supporting_pmids":["12345678"],"excluded_pmids":[],"edge":["A","activation","B"],"neko_session_id":"FULL_SESSION"}
```

Use the verdict/type/confidence enums in `scripts/evidence_contract.py` and the
literal effect from the bounded review task; footer identity and
lists must match Markdown sections and canonical filenames. Supporting/excluded
PMIDs must be disjoint, and a supported verdict requires a supporting PMID.
The report body still records context, perturbation,
access limits and separate existence/direction/sign/directness judgments. Metadata
or abstract access is not full-text review. No PMID is evidence merely because it
appears in a report.

Candidate edges use `evidence/reports/<session>/candidates/A__B.md`; reviewed topology
edges use the session root. Re-review requires explicit `.r2.md`, `.r3.md`, etc.,
with an existing preceding revision. Codex's writer uses exclusive creation; the
Claude hook checks no-overwrite before Write. Keep review slices non-overlapping;
the Claude preflight check is not an atomic replacement for the underlying Write.
Every manual edge needs a dedicated report before inclusion; arbitrary backend
imports are not rewritten or retrospectively validated by these report helpers.
With backend 2.5.0, apply an approved manual edge or effect change through
NeKo `edit_interactions` rather than a hand-edited SIF: NeKo records its manual
origin, rationale and supplied PMIDs in the network history, separately from
database evidence. A missing PMID stays empty; it is never filled in.

Use provenance tags database-default, literature-derived, manual-literature,
fitted-for-dynamics and readout as defined in CLAUDE.md. Orchestrator-authored inputs
live under `runs/orchestrator/<purpose>/`, with CHANGES.md containing original
path/hash, exact ordered edits, evidence/PMIDs, authorization, output path/hash and
reproducible command or patch. Biological values are not inferred from signed edges.

## Capture and validation

See [capture commands](../scripts/capture/README.md) for one-command handoff/package
capture, recorder-written reports (the SubagentStop recorder saves each specialist's
final message as the immutable `report.md`), immutable invocation records, resource
metadata, index recovery and usage tables. The server still chooses its original artifact root;
these repository helpers capture exports without fixing category B server behavior.
Missing usage/resource fields remain null, not invented. Artifact copy and index
publication are separate recoverable operations.

Claude Code 2.1.311 does not reliably fire `SubagentStop` for background subagents, so
after every specialist completion the orchestrator runs `python3 scripts/capture/recover.py
--specialist <name> --agent-id <agentId> [--session <id>]` (before any registry or state
write) and proceeds only on its `RECOVERED <specialist> <session-or-unbound> <status>`
line. It records the missing invocation through the normal recorder, which also writes
`report.md`, and is a no-op (`already-recorded`) when the hook did fire. In both cases it
then saves the run's server exports (NeKo SIF/history, MaBoSS BND/CFG and run manifests with
their node-marginal trajectories and engine inputs, PhysiCell XML and bundles) to `runs/<specialist>/<session>/exports/<agentId>/` with an `exports.json` hash
manifest, since each MCP server and its sessions end with the run; rerunning it captures
exports for older runs that have no `exports/` directory yet; an existing capture is never
amended (details in the capture README). data-analyst uses `receive.py`,
which calls it.

After a `boolean-dynamics-modeler` recovery, recover.py also removes orphaned MaBoSS
temp directories and prints one `TMP CLEANUP: removed N maboss-run dirs (X MiB), skipped M`
line after `RECOVERED` (a cleanup error prints `TMP CLEANUP WARNING: ...` and never fails
the recovery). The MaBoSS server writes a 100–300 MB `maboss-run-*` directory per
simulation and leaves it behind after copying the results into its artifact store; on WSL
`/tmp` is a small RAM tmpfs that these directories filled ("No space left on device").
Setup therefore renders a disk-backed `TMPDIR` (`<cache>/mcp-biomodelling/tmp/<server>`,
mode 0700) into every modelling server's transport ([automatic setup](automatic-setup.md));
rerun setup to get it. Both that directory and `/tmp` are cleaned. A directory is removed
only if it is a real `maboss-run-*` directory owned by you, no process has a file or working
directory open inside it or names it on its command line, its newest entry is older than 10
minutes, and either it was last modified before the earliest start of every running MaBoSS
server (a process whose argv has an element `mcp-maboss-server` or `.../mcp-maboss-server`;
shell command lines merely mentioning the name do not count) or it is older than 24 hours.
Parallel runs keep their own directories; earlier runs' directories are orphans. A directory
that disappears meanwhile is reported as `vanished`. If the MaBoSS `TMPDIR` is missing at
recover time it is recreated (0700, refusing symlinks) with a `TMP CLEANUP WARNING` that the
server fell back to `/tmp` for that run. Manual use:

```bash
python3 scripts/capture/server_tmp.py status                 # free space per root, candidate size
python3 scripts/capture/server_tmp.py clean --dry-run        # what would be removed, and why not
python3 scripts/capture/server_tmp.py clean [--min-age SECONDS]
```

Both print `WARNING: <root> has <N> MiB free` when a root has less than 2 GiB free.

See [validation commands](../scripts/validate/README.md) for explicit criteria,
safe bounded Boolean analyses, trajectory metrics, seed planning and time-scaling
proposals. Initialize VALIDATION_CRITERIA.yaml only when starting a model. The
orchestrator may run these read-only artifact checks; this grants no modelling MCP
access, mutation or numerical-parameter approval. Store results under
`runs/orchestrator/validation/`, then update declared plan rows with state.py.

Readout Boolean-function exclusivity and asynchronous stored-node exclusivity are
different claims; use the appropriate analysis with explicit initial state and
update semantics. Spaced seeds avoid assumed consecutive overlap but do not prove
Monte Carlo independence. Calibration emulates a declared phase mapping and emits
a proposal requiring approval; it does not validate a PhysiCell simulation.
Every multicellular stage still needs both independent reviews before closure.
