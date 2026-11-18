# Roadmap

Planned releases, in order. Versions after 1.2.1 are indicative and may be
reordered as work progresses. Released changes are listed in
[CHANGELOG.md](CHANGELOG.md).

## Released

- **1.2.0** (2026-11-18): the data layer (data-analyst specialist, typed data handoffs
  to NeKo and MaBoSS, data to ODE calibration) and data to PhysiCell. The data layer
  was planned as 1.1.0 and was not tagged separately.

## 1.2.1: hardening (next release)

- Fixes from the November 2026 code audit: credential redaction that no longer
  rewrites scientific text, the Windows-to-WSL bridge, the stage gate for ODE briefs
  that use a NeKo graph, no continuation of a specialist outside the gate, guards that
  block when they cannot run, replicate-aware normalisation in calibration, and stage
  reviews that must cite recorded reviewer runs.
- Incremental approval: an edge-only edit of a reviewed network is approved as an
  exact, computed difference, without a new full review.
- A licence for the repository, and the PubMed server step in the quick start.

## 1.2.x: macOS

- Setup and the test suite on macOS, including Apple Silicon. The Codex dispatcher
  stays Linux/WSL until it is validated there.

## Before 1.3.0: a worked example

- One end-to-end case study in the repository, from specification to PhysiCell
  settings, with its decisions, evidence and validation results. Today the repository
  ships the workflow but no model that shows what it produces.

## 1.3.0: spatial data as initial conditions

- Images and spatial transcriptomics define PhysiCell initial conditions: cell
  positions, and cell types matched to the approved cell definitions.
- Existing tools, such as the BIWT extension of PhysiCell Studio, will be evaluated
  first.

## 1.4.0: ODE-informed PhysiCell

- Calibrated ODE responses become proposals for PhysiCell rules and rates.

## Later

- Spatial data as a PhysiCell fitting condition.
- Identifiability analysis for ODE calibration.
- Codex parity: the data layer, and reviewer specialists (after 1.2.1 a stage review
  can only be recorded from Claude Code).
- Incremental approval for Boolean rule edits and for the NeKo-to-BioMASS path.
- The remaining medium- and low-priority findings of the November 2026 audit.
- Offline installation of calibration environments.
- OS-level sandboxing of specialists.
- Direct ODE–PhysiCell coupling through libRoadRunner, once BioMASS can export SBML.
- Start from an existing PhysiCell project with input files (rules, MaBoSS models,
  initial cells), once the server can reload them.
- Combine MaBoSS models from several MaBoSS sessions in one PhysiCell design.
- Substrates in units other than mmHg, and control over PhysiCell's default substrate,
  once the server supports them.
- Hill rules that also apply to dead cells, once the server supports them.
