#!/usr/bin/env bash
# Drive this calibration job on a remote machine (run from anywhere; it works in the job folder).
#
#   bash launch/remote.sh setup                  set up the environment (env/bootstrap.sh)
#   bash launch/remote.sh start [--parallel K]   run all restarts detached (survives logout), then collect
#   bash launch/remote.sh status                 done/running/failed counts, best score, tail of failed logs
#   bash launch/remote.sh collect                write results.tar.gz now (partial if restarts are pending)
#   bash launch/remote.sh cancel                 stop what `start` launched (and scancel a SLURM array)
#
# State (all inside this folder): state/pids = the process groups started by `start` (emptied when the
# supervisor finishes and by `cancel`); state/status.json = progress, refreshed while it runs and by `status`;
# state/slurm_job = the SLURM array job id (written when it was submitted). `start` is refused while any
# restart is running. A recorded pid is acted on (by `start` and `cancel`) only while `ps -o args=` shows it
# is a `remote.sh _supervise` process: a pid reused by an unrelated process is never signalled. The supervisor
# leads its own process group (setsid, or bash job control `set -m` where setsid is missing, e.g. macOS);
# `cancel` stops that group and every recorded descendant, and reports `stopped` only once all are gone.
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
    ids = (root / "state" / "slurm_job").read_text().split()  # one array id per submit; the latest counts
    slurm_id = ids[-1] if ids and re.fullmatch(r"[0-9]+", ids[-1]) else None
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
ended = sorted(n for n in bad_tasks if n in info["running"] + info["stale"] + info["pending"])
info["running"] = [n for n in info["running"] if n not in ended]
info["stale"] = [n for n in info["stale"] if n not in ended]
info["pending"] = [n for n in info["pending"] if n not in ended]
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

# A live (not zombie) process / process group.
alive() {
  kill -0 "$1" 2>/dev/null || return 1
  case "$(ps -o stat= -p "$1" 2>/dev/null)" in Z*) return 1 ;; esac
  return 0
}
group_alive() {
  ps -A -o pgid= -o stat= 2>/dev/null | awk -v g="$1" '$1 == g && $2 !~ /^Z/ { found = 1 } END { exit !found }'
}

# The pid and all its descendants (pgrep -P walk; just the pid when pgrep is missing).
descendants() {
  local child
  echo "$1"
  if command -v pgrep >/dev/null 2>&1; then
    for child in $(pgrep -P "$1" 2>/dev/null); do descendants "$child"; done
  fi
}

# True when the pid is (still) a supervisor started by `start`, not a reused pid.
is_supervisor() {
  local args
  args="$(ps -o args= -p "$1" 2>/dev/null)" || return 1
  case "$args" in *"remote.sh _supervise"*) return 0 ;; *) return 1 ;; esac
}

case "${1:-}" in
  setup)
    bash env/bootstrap.sh ;;
  start)
    shift
    need_env
    if [ "$#" -ne 0 ]; then
      if [ "$#" -ne 2 ] || [ "$1" != "--parallel" ]; then
        echo "remote.sh: usage: start [--parallel K]" >&2; exit 2
      fi
      case "$2" in '' | *[!0-9]* | 0*) echo "remote.sh: --parallel must be a positive integer" >&2; exit 2 ;; esac
    fi
    if [ -f state/pids ]; then
      while read -r pid _; do
        case "$pid" in '' | *[!0-9]*) continue ;; esac
        if alive "$pid" && is_supervisor "$pid"; then
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
      supervisor=$!
    else
      # no setsid (macOS): with job control the background job gets its own process group (pgid = its pid)
      set -m
      nohup bash launch/remote.sh _supervise "$@" >launch.log 2>&1 </dev/null &
      supervisor=$!
      set +m
    fi
    echo "$supervisor" >state/pids
    # a supervisor that already finished (and emptied state/pids) must not stay recorded
    if ! is_supervisor "$supervisor"; then : >state/pids; fi
    echo "remote.sh: started (pid $supervisor); log: $JOB_DIR/launch.log" ;;
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
    progress quiet || true
    : >state/pids ;;  # finished: nothing left for start/cancel to act on
  status)
    need_env
    progress report ;;
  collect)
    need_env
    "$PY" run.py collect ;;
  cancel)
    unstopped=""
    if [ -f state/pids ]; then
      pids=""
      members=""
      while read -r pid _; do
        case "$pid" in '' | *[!0-9]*) continue ;; esac
        if ! is_supervisor "$pid"; then
          echo "remote.sh: pid $pid is not this job's supervisor any more (finished, or the pid was reused); not signalled"
          continue
        fi
        pids="$pids $pid"
        tree="$(descendants "$pid" | tr '\n' ' ')"  # before any signal: orphans cannot be found afterwards
        members="$members $tree"
        # the recorded pid leads its own process group: stop the whole group, else every recorded descendant
        if ! kill -TERM -- "-$pid" 2>/dev/null; then
          for member in $tree; do kill -TERM "$member" 2>/dev/null || true; done
        fi
      done <state/pids
      remaining() {
        local p
        for p in $pids; do group_alive "$p" && echo "$p"; done
        for p in $members; do alive "$p" && echo "$p"; done
        return 0
      }
      left=""
      for _ in 1 2 3 4 5 6 7 8 9 10; do
        left="$(remaining | sort -u | tr '\n' ' ')"
        [ -z "${left// /}" ] && break
        sleep 0.5
      done
      for p in $left; do kill -KILL -- "-$p" 2>/dev/null || true; kill -KILL "$p" 2>/dev/null || true; done
      for _ in 1 2 3 4 5 6 7 8 9 10; do
        left="$(remaining | sort -u | tr '\n' ' ')"
        [ -z "${left// /}" ] && break
        sleep 0.2
      done
      : >state/pids
      if [ -n "${left// /}" ]; then
        unstopped="$left"
        echo "remote.sh: could not stop: $left" >&2
      else
        echo "remote.sh: stopped${pids:- nothing}"
      fi
    else
      echo "remote.sh: no recorded processes (state/pids)"
    fi
    if [ -f state/slurm_job ]; then
      for jobid in $(tr -cs '0-9' ' ' <state/slurm_job); do
        if command -v scancel >/dev/null 2>&1; then
          if scancel "$jobid"; then
            echo "remote.sh: scancel $jobid"
          else
            echo "remote.sh: scancel $jobid failed" >&2
          fi
        else
          echo "remote.sh: SLURM job $jobid recorded but scancel is not available; cancel it by hand" >&2
        fi
      done
    fi
    if [ -x "$PY" ]; then progress quiet || true; fi
    if [ -n "$unstopped" ]; then exit 1; fi ;;
  *)
    echo "$USAGE" >&2
    exit 2 ;;
esac
