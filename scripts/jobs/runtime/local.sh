#!/usr/bin/env bash
# Run every pending restart of this calibration job on this machine, then collect the results.
#
#   bash launch/local.sh [--parallel K]
#
# Sets up the environment first if needed (env/bootstrap.sh). K restarts run at the same time
# (default: "parallel" in job.json), capped so restarts never oversubscribe this machine: `run.py parallel`
# gives min(K, cores // workers), and 1 for workers -1. Finished restarts are skipped and live ones are never
# touched, so rerunning resumes.
# Ends by writing results.tar.gz in the job folder.
set -euo pipefail

JOB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$JOB_DIR"

PARALLEL=""
GIVEN=0
while [ $# -gt 0 ]; do
  case "$1" in
    --parallel)
      PARALLEL="${2:-}"; GIVEN=1; shift 2 || { echo "usage: launch/local.sh [--parallel K]" >&2; exit 2; } ;;
    *)
      echo "usage: launch/local.sh [--parallel K]" >&2; exit 2 ;;
  esac
done

bash env/bootstrap.sh

PY="$JOB_DIR/.venv/bin/python"
setting() { "$PY" -c 'import json, sys; print(json.load(open("job.json"))["settings"][sys.argv[1]])' "$1"; }
if [ "$GIVEN" = 1 ]; then
  case "$PARALLEL" in
    '' | *[!0-9]* | 0*) echo "local.sh: --parallel must be a positive integer" >&2; exit 2 ;;
  esac
  PARALLEL="$("$PY" run.py parallel --requested "$PARALLEL")"
else
  PARALLEL="$("$PY" run.py parallel)"  # settings parallel, capped by this machine's cores and workers
fi
RESTARTS="$(setting restarts)"

echo "local.sh: running $RESTARTS restarts, $PARALLEL at a time"
seq 1 "$RESTARTS" | xargs -P "$PARALLEL" -I{} "$PY" run.py calibrate --restart {} || true
"$PY" run.py collect
