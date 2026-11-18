# Roadmap

Planned releases, in order. Versions are indicative and may be
reordered as work progresses. Released changes are listed in
[CHANGELOG.md](CHANGELOG.md).

## Released

- **1.2.0** (2026-11-18): the data layer (data-analyst specialist, typed data handoffs
  to NeKo and MaBoSS, data to ODE calibration) and data to PhysiCell. The data layer
  was planned as 1.1.0 and was not tagged separately.
- **1.2.1** (2026-11-18): hardening. The high-priority fixes of the November 2026 code
  audit, incremental approval of edge-only topology edits, setup and tests on macOS
  (including Apple Silicon), a licence (GPL-3.0) and the PubMed step in the quick start.

## Next: a worked example (before 1.3.0)

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
- Codex parity: the data layer, and reviewer specialists (since 1.2.1 a stage review
  can only be recorded from Claude Code).
- The Codex dispatcher on macOS (setup supports macOS since 1.2.1; the dispatcher is
  validated on Linux/WSL only).
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
