---
name: physicell-build
description: Set up and compile PhysiCell (template or template_BM) from a pinned release, optionally with a verified captured configuration copied in. Use when the researcher asks to set up, install or compile PhysiCell. Orchestrator only; no specialist.
---

# PhysiCell build

Orchestrator-only. No specialist runs this skill; the orchestrator runs the script
itself.

## When to use

The researcher asks to set up or compile PhysiCell (for example, to run a configuration
the multicellular-configurator exported). The build only prepares the project. Running
and calibrating PhysiCell is someone else's job.

## Choose the template

- `template`: plain PhysiCell.
- `template_BM`: PhysiBoSS/MaBoSS designs (a design with intracellular Boolean models).
  It needs internet access to fetch MaBoSS during `make template_BM`, and `python3`.

## Run

From the repository root:

`python3 scripts/physicell/build.py --template template|template_BM [--version 1.14.2] [--dest DIR] [--jobs N]`

- It checks the toolchain before downloading anything: `make`, `curl` (or `git`),
  and a compiler that builds an OpenMP program (`$PHYSICELL_CPP`, `g++`, then `g++-NN`).
  On macOS with only Apple clang it stops and prints the fix (`brew install gcc`,
  `export PHYSICELL_CPP=g++-NN`). Relay that line to the researcher.
- The pinned release lands in `<dest>/PhysiCell-<version>-<template>/` (default
  `~/PhysiCell`). An existing folder is refused, never overwritten.
- To compile a verified design, add
  `--verification runs/orchestrator/<p>/physicell_verification.<session>.<capture>.json`.
  The verification must have passed and its captured files must be unchanged;
  otherwise the script refuses. The packaged `PhysiCell_settings.xml` and its
  dependencies (rules CSV, `.bnd`, ...) are copied into `config/`, and each replaced
  file is kept as `<name>.template-backup`.

## Never run the project

Never run `./project` (or anything that starts a simulation). The report names the run
command, `./project config/PhysiCell_settings.xml`, as text for the researcher to run
or hand on.

## Relay the report

The script writes `runs/orchestrator/physicell-build/<UTC>-<template>/report.json` and
`report.md` and prints the path. Relay the report path and a short summary: status,
project folder, compiler, project sha256, copied files, and the run command. On
`status: "failed"`, relay the failing step and the output tail; the folder is kept for
inspection. Do not retry with a different `--dest` or delete anything unless asked.
