# Roadmap

Planned releases, in order. Versions after 1.1.0 are indicative and may be
reordered as work progresses. Released changes are listed in
[CHANGELOG.md](CHANGELOG.md).

## 1.1.0: data layer (next release)

- A data-analyst specialist retrieves and analyses public and researcher data, and
  returns proposals that you review.
- Approved data reach NeKo and MaBoSS only through typed, hash-checked handoffs. These
  can use your own sources in any format and your own networks.
- Data → ODE: measurements are mapped onto BioMASS models. Calibration jobs run on this
  machine, a lab machine over SSH, or a SLURM cluster. Results come back as proposals
  you approve.

## 1.2.0: data to PhysiCell

- You approve the multicellular design first:
  - cell definitions and variants;
  - substrates;
  - which cell type carries which MaBoSS model;
  - how signals drive behaviors (PhysiBoSS links or Hill-function rules).
- Data are then projected into the PhysiCell settings and checked against the live
  signal/behavior dictionary and the settings file's own units.
- A build skill compiles a PhysiCell project (plain or PhysiBoSS template) ready to run
  your approved settings.

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
- Codex parity.
- Offline installation of calibration environments.
- OS-level sandboxing of specialists.
- Direct ODE–PhysiCell coupling through libRoadRunner, once BioMASS can export SBML.
