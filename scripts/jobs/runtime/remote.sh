#!/usr/bin/env bash
# Drive this calibration job on a remote machine (run from anywhere; it works in the job folder).
#
#   bash launch/remote.sh setup                  set up the environment (env/bootstrap.sh)
#   bash launch/remote.sh start [--parallel K]   run all restarts detached (survives logout), then collect
#   bash launch/remote.sh status                 progress of the restarts (also written to out/status.json)
#   bash launch/remote.sh collect                write results.tar.gz now (partial if restarts are pending)
set -euo pipefail

JOB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$JOB_DIR"
PY="$JOB_DIR/.venv/bin/python"

need_env() {
  if [ ! -x "$PY" ]; then
    echo "remote.sh: environment missing; run: bash launch/remote.sh setup" >&2
    exit 2
  fi
}

case "${1:-}" in
  setup)
    bash env/bootstrap.sh ;;
  start)
    shift
    need_env
    if [ -f launch.pid ] && kill -0 "$(cat launch.pid)" 2>/dev/null; then
      echo "remote.sh: already running (pid $(cat launch.pid))" >&2
      exit 1
    fi
    if command -v setsid >/dev/null 2>&1; then
      setsid nohup bash launch/local.sh "$@" >launch.log 2>&1 </dev/null &
    else
      nohup bash launch/local.sh "$@" >launch.log 2>&1 </dev/null &
    fi
    echo $! >launch.pid
    echo "remote.sh: started (pid $!); log: $JOB_DIR/launch.log" ;;
  status)
    need_env
    "$PY" run.py status ;;
  collect)
    need_env
    "$PY" run.py collect ;;
  *)
    echo "usage: launch/remote.sh setup|start [--parallel K]|status|collect" >&2
    exit 2 ;;
esac
