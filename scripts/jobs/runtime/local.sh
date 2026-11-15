#!/usr/bin/env bash
# Run every pending restart of this calibration job on this machine, then collect the results.
#
#   bash launch/local.sh [--parallel K]
#
# Sets up the environment first if needed (env/bootstrap.sh). K restarts run at the same time
# (default: "parallel" in job.json). Finished restarts are skipped, so rerunning resumes.
# Ends by writing results.tar.gz in the job folder.
set -euo pipefail

JOB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$JOB_DIR"

PARALLEL=""
while [ $# -gt 0 ]; do
  case "$1" in
    --parallel)
      PARALLEL="${2:-}"; shift 2 || { echo "usage: launch/local.sh [--parallel K]" >&2; exit 2; } ;;
    *)
      echo "usage: launch/local.sh [--parallel K]" >&2; exit 2 ;;
  esac
done

bash env/bootstrap.sh

PY="$JOB_DIR/.venv/bin/python"
setting() { "$PY" -c 'import json, sys; print(json.load(open("job.json"))["settings"][sys.argv[1]])' "$1"; }
[ -n "$PARALLEL" ] || PARALLEL="$(setting parallel)"
case "$PARALLEL" in
  '' | *[!0-9]* | 0) echo "local.sh: --parallel must be a positive integer" >&2; exit 2 ;;
esac
RESTARTS="$(setting restarts)"

echo "local.sh: running $RESTARTS restarts, $PARALLEL at a time"
seq 1 "$RESTARTS" | xargs -P "$PARALLEL" -I{} "$PY" run.py calibrate --restart {} || true
"$PY" run.py collect
