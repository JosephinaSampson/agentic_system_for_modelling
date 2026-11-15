#!/usr/bin/env bash
#SBATCH --job-name=biomass-calibration
#SBATCH --output=out/slurm-%A_%a.log
# One restart per array task. From the job folder, after `bash launch/remote.sh setup` on the login node:
#
#   sbatch --array=1-<restarts> --time=<slurm.time> --cpus-per-task=<slurm.cpus_per_task> launch/slurm.sbatch
#
# (restarts and slurm.* are in job.json "settings"). When all tasks are done: bash launch/remote.sh collect
set -euo pipefail

cd "${SLURM_SUBMIT_DIR:?submit with sbatch from the job folder}"
.venv/bin/python run.py calibrate --restart "${SLURM_ARRAY_TASK_ID:?submit as an array: sbatch --array=1-N}"
