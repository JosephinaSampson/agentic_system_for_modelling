#!/usr/bin/env bash
# Drive this calibration job on a remote machine (run from anywhere; it works in the job folder).
#
#   bash launch/remote.sh setup                  set up the environment (env/bootstrap.sh)
#   bash launch/remote.sh start [--parallel K]   run all restarts detached (survives logout), then collect
#   bash launch/remote.sh status                 done/running/failed counts, best score, tail of failed logs
#   bash launch/remote.sh collect                write results.tar.gz now (partial if restarts are pending)
#   bash launch/remote.sh cancel                 stop what `start` launched (and scancel a SLURM array)
#
# State (all inside this folder): state/pids = the process groups started by `start`; state/status.json =
# progress, refreshed while it runs and by `status`; state/slurm_job = the SLURM array job id (written when
# it was submitted). `start` is refused while any restart is running.
set -euo pipefail

JOB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$JOB_DIR"
PY="$JOB_DIR/.venv/bin/python"
USAGE="usage: launch/remote.sh setup|start [--parallel K]|status|collect|cancel"

need_env() {
  if [ ! -x "$PY" ]; then
    echo "remote.sh: environment missing; run: bash launch/remote.sh setup" >&2
    exit 2
  fi
}

# Progress report. $1 = report (text on stdout, writes state/status.json) | quiet (only the file)
# | running (prints the number of running restarts). SLURM array tasks that sacct says ended badly
# (FAILED, CANCELLED, TIMEOUT, OUT_OF_MEMORY, NODE_FAIL) and left no result.json count as failed.
progress() {
  "$PY" - "$1" "$JOB_DIR" <<'PYEOF'
import json, os, re, shutil, subprocess, sys
from pathlib import Path

mode, root = sys.argv[1], Path(sys.argv[2])
sys.path.insert(0, str(root))
import run

info = run.status(root, write=False)
BAD = {"FAILED", "CANCELLED", "TIMEOUT", "OUT_OF_MEMORY", "NODE_FAIL"}
slurm_id = None
bad_tasks = {}
try:
    text = (root / "state" / "slurm_job").read_text().strip()
    slurm_id = text if re.fullmatch(r"[0-9]+", text) else None
except OSError:
    pass
if slurm_id and shutil.which("sacct"):
    try:
        done = subprocess.run(["sacct", "-j", slurm_id, "--format=JobID,State", "--noheader", "-P"],
                              capture_output=True, text=True, timeout=60)
        lines = done.stdout.splitlines() if done.returncode == 0 else []
    except (OSError, subprocess.SubprocessError):
        lines = []
    for line in lines:
        job, _, state = line.partition("|")
        match = re.fullmatch(re.escape(slurm_id) + r"_([0-9]+)", job.strip())
        word = state.split()[0].upper() if state.split() else ""
        if match and word in BAD:
            bad_tasks[int(match.group(1))] = word

failed_results, ok_scores = [], []
for n in info["done"]:
    try:
        result = run.read_json(root / run.OUT / f"restart-{n}" / "result.json")
    except (OSError, ValueError):
        result = {}
    if result.get("status") == "ok" and isinstance(result.get("score"), (int, float)):
        ok_scores.append((result["score"], n))
    else:
        failed_results.append(n)
ended = sorted(n for n in bad_tasks if n in info["running"] + info["stale"])
info["running"] = [n for n in info["running"] if n not in ended]
info["stale"] = [n for n in info["stale"] if n not in ended]
info["failed_restarts"] = sorted(failed_results + ended)
info["failed"] = len(info["failed_restarts"])
info["slurm_job"] = slurm_id
info["slurm_states"] = {str(n): bad_tasks[n] for n in ended}
best = min(ok_scores) if ok_scores else None
info["best_score"] = best[0] if best else None
info["best_restart"] = best[1] if best else None
if mode == "running":
    print(len(info["running"]))
    sys.exit(0)
state_dir = root / "state"
state_dir.mkdir(exist_ok=True)
run.write_json(state_dir / "status.json", info)
if mode == "quiet":
    sys.exit(0)
print(f"{info['job_id']}: restarts {info['restarts']} | done {len(info['done'])} | ok {info['ok']} | "
      f"failed {info['failed']} | running {len(info['running'])} | stale {len(info['stale'])} | "
      f"pending {len(info['pending'])}")
print("best score: " + (f"{best[0]!r} (restart {best[1]})" if best else "none yet"))
if info["running"]:
    print(f"running: {info['running']}")
if info["stale"]:
    print(f"stale (process gone, rerun with `start`): {info['stale']}")
for n in info["failed_restarts"]:
    reason = f" (SLURM {bad_tasks[n]})" if n in bad_tasks else ""
    print(f"--- failed restart {n}{reason}: last 20 log lines")
    shown = False
    for log in (root / run.OUT / f"restart-{n}" / "log.txt",
                *(root / run.OUT).glob(f"slurm-{slurm_id}_{n}.log" if slurm_id else "slurm-none")):
        if log.is_file():
            print("\n".join(log.read_text(errors="replace").splitlines()[-20:]))
            shown = True
            break
    if not shown:
        print("(no log)")
PYEOF
}

alive() { kill -0 "$1" 2>/dev/null; }

case "${1:-}" in
  setup)
    bash env/bootstrap.sh ;;
  start)
    shift
    need_env
    if [ -f state/pids ]; then
      while read -r pid _; do
        if [ -n "$pid" ] && alive "$pid"; then
          echo "remote.sh: already running (pid $pid); use cancel first" >&2
          exit 1
        fi
      done <state/pids
    fi
    running="$(progress running)"
    if [ "$running" != "0" ]; then
      echo "remote.sh: $running restart(s) running; start is refused (it would delete a live restart). Use status or cancel." >&2
      exit 1
    fi
    mkdir -p state
    if command -v setsid >/dev/null 2>&1; then
      setsid nohup bash launch/remote.sh _supervise "$@" >launch.log 2>&1 </dev/null &
    else
      nohup bash launch/remote.sh _supervise "$@" >launch.log 2>&1 </dev/null &
    fi
    echo $! >state/pids
    echo "remote.sh: started (pid $!); log: $JOB_DIR/launch.log" ;;
  _supervise)  # internal: runs local.sh and keeps state/status.json fresh
    shift
    need_env
    bash launch/local.sh "$@" &
    child=$!
    while alive "$child"; do
      progress quiet || true
      sleep "${REMOTE_STATUS_INTERVAL:-30}"
    done
    wait "$child" || true
    progress quiet || true ;;
  status)
    need_env
    progress report ;;
  collect)
    need_env
    "$PY" run.py collect ;;
  cancel)
    if [ -f state/pids ]; then
      pids=""
      left=""
      while read -r pid _; do
        case "$pid" in '' | *[!0-9]*) continue ;; esac
        pids="$pids $pid"
        # the recorded pid leads its own process group (setsid): stop the whole group, else the pid and its children
        kill -TERM -- "-$pid" 2>/dev/null || {
          command -v pkill >/dev/null 2>&1 && pkill -TERM -P "$pid" 2>/dev/null || true
          kill -TERM "$pid" 2>/dev/null || true
        }
      done <state/pids
      for _ in 1 2 3 4 5 6 7 8 9 10; do
        left=""
        for pid in $pids; do alive "$pid" && left="$left $pid"; done
        [ -z "$left" ] && break
        sleep 0.5
      done
      for pid in $left; do kill -KILL -- "-$pid" 2>/dev/null || kill -KILL "$pid" 2>/dev/null || true; done
      : >state/pids
      echo "remote.sh: stopped${pids:- nothing}"
    else
      echo "remote.sh: no recorded processes (state/pids)"
    fi
    if [ -f state/slurm_job ]; then
      jobid="$(tr -cd '0-9' <state/slurm_job)"
      if [ -n "$jobid" ] && command -v scancel >/dev/null 2>&1; then
        scancel "$jobid"
        echo "remote.sh: scancel $jobid"
      else
        echo "remote.sh: SLURM job $jobid recorded but scancel is not available; cancel it by hand" >&2
      fi
    fi
    if [ -x "$PY" ]; then progress quiet || true; fi ;;
  *)
    echo "$USAGE" >&2
    exit 2 ;;
esac
