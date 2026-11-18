---
name: validate-stage
description: Validate a completed biological-modelling stage against lineage, artifacts, approvals, reproducibility, and VALIDATION_PLAN.md before any transition.
---

# Validate a modelling stage

The orchestrator must identify the claimed stage and conclusions, then inspect the
recorded specialist invocation, typed handoff, mandatory exports, hashes, warnings,
and upstream lineage. Reject unknown specialist/stage pairs, artifacts outside the
approved session directory, failed validation marked completed, and any skipped
researcher gate.

Compare the claims and limitations to `VALIDATION_PLAN.md`. Keep model behavior,
literature evidence, assumptions, and conclusions distinct. Classify the result as
`approved`, `approved_with_limitations`, `revision_required`, or `blocked` and ask
for researcher judgment on unresolved consequential assumptions.

Do not advance the stage merely because execution succeeded. First run the
provider-neutral `scripts/codex/validate_handoff.py` against the recorded JSON and
the expected specialist/session identity, then perform the scientific checks above;
machine-valid structure is not scientific validity. Record an accepted outcome in
`DECISIONS.md` and `CURRENT_STATE.md` only after researcher approval.


## ODE state

Apply `docs/ode-contract.md` to `ode_modeler` / `biomass_ode`. Verify source kind,
lineage, document version, selected revision, complete captured inventory, evidence,
actual numerical settings and scenario results. Validate ODE evidence with explicit
`--review-kind ode`. Distinguish provisional workflow authorization from exact
session/revision conclusive-export approval; a candidate snapshot keeps the
scientific registry pending. Existing `runs/` and `evidence/` lifecycle scopes
already include ODE artifacts. Successful solves are not biological validation.

## Sequential review gate

Complete both independent scientific review and reproducibility audit before any
downstream dispatch. A stage_review is valid only when both reports are recorded reviewer runs
(`runs/scientific-reviewer/<id>/report.md` and `runs/reproducibility-auditor/<id>/report.md`, written by
the invocation recorder with a brief that names the reviewed artifact sha256). Codex has no reviewer
specialists yet, so it cannot produce these: run the stage review from Claude Code and record it there,
then continue. Never write a report into those folders or cite another file as a review.
Use scripts/state.py record for a stage_review record identifying exact session,
artifact hash, both report paths/hashes and outcome approved, approved_with_limitations,
revision_required or blocked. Then obtain and record researcher stage_approval for
that exact artifact and declared outputs. Run scripts/gate.py check and quote its
JSON result before dispatch; a review recommendation is not researcher approval.

### A small topology edit after a review

When a reviewed NeKo-to-MaBoSS export is re-exported from the same session with only edges added,
removed or sign-changed, the earlier review carries over:

1. `python3 scripts/network_delta.py <reviewed handoff.json> <new handoff.json>` prints the exact
   `changes` (signed edges) and `rules` (before/after of each changed target). A refusal means a full
   review: changed node set, a rule changed without an edge change, a rule that is not plain signed
   regulators, or exports of different sessions.
2. Show the researcher that output and each change's support: its own edge report under
   `evidence/reports/` (added and sign-changed edges need verdict supported or context-dependent and a
   compatible interaction type; a removal accepts any verdict), or the data `stage_approval` it came from.
3. On approval, `scripts/state.py record` a `stage_delta`: `session_id`, `stage: "topology"`, `parent`,
   `artifact`, the printed `changes` (each with `evidence` {path, sha256} or `decision_id`) and `rules`,
   `approved_by: "researcher"` and `authorization`. Then record the `stage_approval` of the new export.
4. `gate.py check` reports `review_id` (the full review it rests on), `delta_id` and
   `edges_changed_since_review`. After 5 changed edges since the last full review the gate requires a
   new full review. Boolean rule, readout and rate edits always need the full review.
   A `stage_delta` needs no reviewer specialists, so Codex can record one on top of a review recorded from
   Claude Code.
A later blocking review or changed artifact invalidates the earlier gate.

PhysiCell closure also requires both reviews. Inspect all enabled package dependencies,
relative paths, NeKo/MaBoSS lineage, exact mappings, node/behavior existence, readout
exclusivity, timing/units, cycle phase exits/base rates, initial cells and declared
parameters. Read exported values after patches, especially apoptosis/necrosis/motility.
Record missing coverage as unverified; never equate valid XML/configuration with
an executed or scientifically validated multicellular simulation.
