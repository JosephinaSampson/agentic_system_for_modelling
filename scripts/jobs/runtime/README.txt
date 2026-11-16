BioMASS calibration job
=======================

This folder is a complete calibration job: the model, the measurements it is fitted to, the
fitting settings (job.json) and the scripts to run it. You need a Linux or macOS machine with
bash, curl (or wget), tar and internet access. Nothing else has to be installed: the first
command downloads a checksum-verified uv, a Python 3.12 and biomass 0.14.0 (hash-locked) into
this folder only.

Three commands, run inside this folder:

  1. bash env/bootstrap.sh
       Sets up the environment and runs a short self-test (a few minutes).
       Success: the last line is "bootstrap: environment ready" and env_report.json says
       "status": "ok".

  2. bash launch/local.sh            (or: bash launch/local.sh --parallel 8)
       Runs all restarts (see "restarts" in job.json), several at a time (never more than this
       machine's cores divided by job.json "workers"; one at a time for workers -1), then collects. Ctrl-C is safe and rerunning it resumes:
       finished restarts are kept, interrupted ones start again, running ones are never touched.
       A restart refused as "running" names its owner (host, pid). A dead or reused pid on this
       machine is detected and its folder taken over automatically. When the owner is on another
       machine and cannot be checked from here (no SLURM ids, or sacct cannot tell), first confirm
       that process is gone (ps on that machine, or squeue), then either run
         .venv/bin/python run.py calibrate --restart N --takeover
       for that restart and rerun this command, or remove out/restart-N by hand and rerun.
       A restart refused because "another process is starting it" names the claim folder
       out/.claim-restart-N; if that process is confirmed gone, remove that folder and rerun.
       Success: the last line is "results.tar.gz written: N/N restarts (... ok)".

  3. Send back results.tar.gz (it contains the fitted values, scores, traces, figures, logs
     and env_report.json).

On a server you log in to with ssh, use launch/remote.sh instead of step 2, so the run keeps
going after you log out:

  bash launch/remote.sh setup
  bash launch/remote.sh start --parallel 8
  bash launch/remote.sh status
  bash launch/remote.sh collect       (when status shows all restarts done; earlier = partial)

On a SLURM cluster: bash launch/remote.sh setup on the login node, then see the header of
launch/slurm.sbatch. To run unfinished restarts again, `scripts/jobs/launch.py slurm submit <job-id>
--resubmit` waits until sacct reports every earlier array ended; on a cluster without accounting
(sacct cannot tell) check `squeue -j <array id>` yourself and, if nothing of it is left, pass
--resubmit --force (an array sacct reports as active is never resubmitted).

When a job is sent to another machine with scripts/jobs/launch.py, only this bundle (the model, the
measurement rows used by the mapping, the parameter bounds) leaves the researcher's machine; no
other inputs/ content does.

Nothing here edits files outside this folder (uv's download cache is env/uv-cache).
