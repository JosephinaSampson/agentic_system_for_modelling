#!/usr/bin/env python3
"""End-to-end smoke test of data to ODE (BioMASS) calibration with a tiny real model.

Run: python3 tests/integration/ode_calibration_smoke.py

Works in a temporary copy of the repo under a scratch folder in ~/.cache (never /tmp); the job
cache, uv downloads and configuration are pointed into that folder. It builds a tiny Text2Model
model with biomass==0.14.0 in a uv environment, maps synthetic measurements onto it, builds the
portable job, runs it locally (real bootstrap, 2 restarts), imports the results (real reproduce),
applies the ode-modeler DATA_INPUT gate, scores the hold-out once and prints the ssh commands.

Prints SKIP (exit 0) when `uv` or the network is unavailable; any other failure exits 1.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

sys.dont_write_bytecode = True
REPO = Path(__file__).resolve().parents[2]
SESSION = "ode-smoke-1"
PURPOSE = "smoke"
REVISION = "rev_" + "5" * 32  # the captured revision the export is bound to (model/integrity.json)
TRUE_KF2 = 0.5
TRUE_KF3 = 0.05
PROBES = ("https://github.com/astral-sh/uv/releases/download/0.13.4/uv-x86_64-unknown-linux-gnu.tar.gz.sha256",
          "https://pypi.org/simple/biomass/")

TINY = """\
E + R <--> ER | kf=1e-2, kr=1e-1 | E=10, R=5
ER --> pER | kf=1e-1
pER --> ER | kf=5e-2
@obs pR: u[pER]
@sim tspan: [0, 60]
"""

SIMULATE = """\
import json, sys
import numpy as np
from scipy.integrate import solve_ivp

kf1, kr1, kf2, kf3 = 1e-2, 1e-1, float(sys.argv[1]), float(sys.argv[2])


def pER(e0, t):
    def rhs(_t, y):
        e, r, er, p = y
        b = kf1 * e * r - kr1 * er
        return [-b, -b, b - kf2 * er + kf3 * p, kf2 * er - kf3 * p]
    sol = solve_ivp(rhs, (0, max(t)), [e0, 5.0, 0.0, 0.0], t_eval=t, method="LSODA", rtol=1e-10, atol=1e-12)
    return sol.y[3].tolist()


control = [0.0, 7.5, 15.0, 30.0, 60.0]
print(json.dumps({"control": dict(zip(control, pER(10.0, control))), "highE": pER(20.0, [0.0, 30.0])[1]}))
"""

SETTINGS = {
    "restarts": 2, "seeds": [1, 2],
    "optimizer_options": {"strategy": "best1bin", "maxiter": 3, "popsize": 3, "tol": 0.0001, "mutation": 0.1,
                          "recombination": 0.5, "polish": False},
    "workers": 1, "parallel": 2, "slurm": {"time": "01:00:00", "cpus_per_task": 1},
    "good_fit_margin": 0.1, "reproduce_rtol": 0.001,
}

STARTED = time.monotonic()


def step(msg: str) -> None:
    print(f"[ok] {msg} ({time.monotonic() - STARTED:.0f}s)", flush=True)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def probe_network() -> str | None:
    """None when every host answers; otherwise the reason."""
    for url in PROBES:
        request = urllib.request.Request(url, method="HEAD")
        try:
            urllib.request.urlopen(request, timeout=10).close()
        except urllib.error.HTTPError:
            continue  # the server answered
        except (urllib.error.URLError, OSError, ValueError) as exc:
            return f"network unreachable ({url}: {exc})"
    return None


def listing(path: Path) -> list[str]:
    return sorted(p.name for p in path.iterdir()) if path.is_dir() else []


def main() -> int:
    if shutil.which("uv") is None:
        print("SKIP: uv is not installed")
        return 0
    reason = probe_network()
    if reason:
        print(f"SKIP: {reason}")
        return 0
    cache = Path.home() / ".cache"
    cache.mkdir(parents=True, exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix="ode-smoke-", dir=cache)).resolve()
    local_bin = Path.home() / ".local" / "bin"
    before = listing(local_bin)
    try:
        run(scratch)
        after = listing(local_bin)
        assert after == before, f"~/.local/bin changed: {sorted(set(after) ^ set(before))}"
        step("~/.local/bin unchanged by the whole run")
    except BaseException:
        print(f"FAILED; scratch dir left at {scratch}", file=sys.stderr)
        raise
    shutil.rmtree(scratch)
    assert not scratch.exists()
    step(f"scratch dir removed ({scratch.name})")
    print("ODE CALIBRATION SMOKE PASSED")
    return 0


def run(scratch: Path) -> None:
    root = scratch / "repo"
    shutil.copytree(REPO, root, symlinks=True, ignore=shutil.ignore_patterns(
        ".git", ".venv-*", "data-cache", "__pycache__", ".superpowers", "runs"))
    (root / "runs").mkdir(exist_ok=True)
    shutil.copyfile(REPO / "templates" / "DECISIONS.md", root / "DECISIONS.md")
    xdg = {"XDG_CACHE_HOME": str(scratch / "xdg-cache"), "XDG_CONFIG_HOME": str(scratch / "xdg-config"),
           "XDG_DATA_HOME": str(scratch / "xdg-data"), "UV_CACHE_DIR": str(scratch / "uv-cache"),
           "UV_PYTHON_INSTALL_DIR": str(scratch / "uv-python"), "UV_PYTHON_BIN_DIR": str(scratch / "uv-bin"),
           "MPLCONFIGDIR": str(scratch / "mplconfig")}
    for value in xdg.values():
        Path(value).mkdir(parents=True, exist_ok=True)
    env = {**os.environ, **xdg, "CLAUDE_PROJECT_DIR": str(root), "PYTHONDONTWRITEBYTECODE": "1"}
    env.pop("VIRTUAL_ENV", None)

    def sh(*args: str, check: bool = True, cwd: Path = root, stdin: str | None = None) -> subprocess.CompletedProcess:
        proc = subprocess.run(list(args), cwd=cwd, env=env, text=True, capture_output=True, input=stdin)
        if check and proc.returncode != 0:
            sys.stderr.write(proc.stdout + proc.stderr)
            raise SystemExit(f"command failed ({proc.returncode}): {' '.join(args)}")
        return proc

    def py(*args: str, **kw) -> subprocess.CompletedProcess:
        return sh(sys.executable, *args, **kw)

    def write_json(path: Path, data) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def record(record_: dict) -> None:
        file = scratch / f"{record_['id']}.json"
        write_json(file, record_)
        py("scripts/state.py", "record", "--input", str(file))

    # 1. the tiny Text2Model model in a uv environment, generated like the BioMASS server worker does:
    #    the description is written as generated_model.txt, so the package is generated_model/
    work = scratch / "model-build"
    work.mkdir()
    sh("uv", "venv", "-q", str(work / "venv"), cwd=work)
    venv_python = str(work / "venv" / "bin" / "python")
    sh("uv", "pip", "install", "-q", "--python", venv_python, "biomass==0.14.0", cwd=work)
    revision_src = work / REVISION  # the server's revision folder
    revision_src.mkdir()
    (revision_src / "model.txt").write_text(TINY, encoding="utf-8")
    (revision_src / "generated_model.txt").write_text(TINY, encoding="utf-8")
    sh(venv_python, "-c", "from biomass import Text2Model; Text2Model('generated_model.txt').convert()",
       cwd=revision_src)
    assert (revision_src / "generated_model" / "name2idx" / "parameters.py").is_file()
    (revision_src / "model_summary.json").write_text(json.dumps({"biomass_version": "0.14.0"}, indent=2),
                                                     encoding="utf-8")
    for cache in list(revision_src.rglob("__pycache__")):
        shutil.rmtree(cache)
    step("Text2Model generated generated_model/ with biomass==0.14.0 (params kf1 kr1 kf2 kf3, observable pR)")

    # 2. sealed and exported like BioMASS 2.5.2 (services/artifacts.py seal + export_bundle): integrity.json
    #    = {relative path: sha256} of every other file of the revision folder (json indent 2); the zip holds
    #    that folder under model/, requirements.txt (from model_summary.json) and run_simulation.py. The
    #    revision folder is captured as runs/ode-modeler/<session>/captures/c1/<revision>/ (record_ode_artifacts).
    sealed = sorted(path for path in revision_src.rglob("*") if path.is_file())
    (revision_src / "integrity.json").write_text(json.dumps(
        {str(path.relative_to(revision_src)): sha256(path) for path in sealed}, indent=2), encoding="utf-8")
    capture_dir = root / "runs" / "ode-modeler" / SESSION / "captures" / "c1"
    capture_dir.mkdir(parents=True)
    shutil.copytree(revision_src, capture_dir / REVISION)
    export_rel = f"runs/ode-modeler/{SESSION}/captures/c1/export_{'e' * 32}.zip"
    export = root / export_rel
    with zipfile.ZipFile(export, "x", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(p for p in revision_src.rglob("*") if p.is_file()):
            zf.write(path, f"model/{path.relative_to(revision_src).as_posix()}")
        zf.writestr("requirements.txt", "biomass==0.14.0\n")
        zf.writestr("run_simulation.py", '"""Run an explicitly configured scenario from this exported bundle."""\n')
    with zipfile.ZipFile(export) as zf:
        assert "model/generated_model/name2idx/parameters.py" in zf.namelist(), zf.namelist()
    step(f"export zipped as {export_rel} (model/generated_model/...); revision {REVISION} captured")

    # 3. synthetic measurements generated with a known kf2 (including 7.5 min and one hold-out row)
    truth = json.loads(sh(venv_python, "-c", SIMULATE, str(TRUE_KF2), str(TRUE_KF3), cwd=work).stdout)
    header = "measurement_id,readout,condition,time,time_unit,dose,dose_unit,value,value_unit,error,error_kind,n,use,source,locator"
    rows, ids = [], []
    for i, (t, v) in enumerate(truth["control"].items(), start=1):
        rows.append(f"M{i},pR,control,{float(t):g},min,,,{v:.6f},molecule,,,,fit,s1,synthetic table row {i}")
        ids.append(f"M{i}")
    rows.append(f"M{len(ids) + 1},pR,highE,30,min,,,{truth['highE']:.6f},molecule,,,,holdout,s1,synthetic table hold-out")
    ids.append(f"M{len(ids) + 1}")
    meas_rel = f"runs/orchestrator/{PURPOSE}/measurements.csv"
    (root / meas_rel).parent.mkdir(parents=True)
    (root / meas_rel).write_text("\n".join([header, *rows]) + "\n", encoding="utf-8")
    (root / "inputs").mkdir(exist_ok=True)
    (root / "inputs/synthetic.txt").write_text(f"synthetic data, kf2={TRUE_KF2}, kf3={TRUE_KF3}\n", encoding="utf-8")
    assert "7.5" in "\n".join(rows)
    step(f"{len(rows)} synthetic measurements (kf2={TRUE_KF2}; control 0/7.5/15/30/60 min fit, highE 30 min hold-out)")

    # 4. draft, fill, check and approve the mapping
    mapping_rel = f"runs/orchestrator/{PURPOSE}/ode_mapping.json"
    py("scripts/ode/ode_mapping.py", "draft", "--purpose", PURPOSE, "--ode-session", SESSION,
       "--bundle", export_rel, "--measurements", meas_rel, "--source", "s1:inputs/synthetic.txt")
    refused = py("scripts/ode/ode_mapping.py", "check", mapping_rel, check=False)
    assert refused.returncode == 1 and refused.stderr, "a draft must not pass check"
    mapping = json.loads((root / mapping_rel).read_text(encoding="utf-8"))
    assert mapping["inputs"]["model"]["revision"] == REVISION, mapping["inputs"]["model"]
    assert mapping["values"] == {"readouts": {"pR": {"model_unit": "TODO", "factors": {"molecule": None}}},
                                 "viability": {"factors": {}}}, mapping["values"]
    mapping["time"] = {"model_unit": "min", "factors": {"min": 1.0}}
    mapping["dose"] = {"model_unit": "nM", "factors": {}}
    # absolute values: an explicit factor from the table's value_unit into the observable's model unit
    mapping["values"] = {"readouts": {"pR": {"model_unit": "molecule", "factors": {"molecule": 1.0}}},
                         "viability": {"factors": {}}}
    mapping["readouts"] = [{"readout": "pR", "observable": "pR"}]
    mapping["conditions"] = [
        {"condition": "control", "parameters": [], "initials": []},
        {"condition": "highE", "parameters": [],
         "initials": [{"name": "E", "value": 20.0, "value_unit": "molecule", "model_unit": "molecule", "factor": 1.0}]}]
    mapping["rules"] = [{"rule_id": "R1", "type": "time_course", "method": "absolute", "measurements": ids}]
    mapping["fit"] = [
        {"name": name, "kind": "parameter", "lower": 1e-3, "upper": 1.0, "unit": "1/min",
         "justification": {"text": "smoke test: broad prior range"}} for name in ("kf2", "kf3")]
    mapping["unmapped"] = []
    write_json(root / mapping_rel, mapping)
    checked = py("scripts/ode/ode_mapping.py", "check", mapping_rel, check=False)
    assert checked.returncode == 0 and not checked.stderr, checked.stdout + checked.stderr
    record({"id": "ode-map-1", "kind": "stage_approval", "stage": "ode_mapping", "session_id": SESSION,
            "outputs": sorted([*ids, "kf2", "kf3"]), "artifact": {"path": mapping_rel, "sha256": sha256(root / mapping_rel)},
            "approved_by": "researcher", "authorization": "researcher: smoke mapping ok"})
    step(f"ode_mapping drafted (revision {REVISION} derived; refused), filled, check passed, approval recorded")

    # 5. the portable bundle, 2 restarts and maxiter=3
    settings_rel = f"runs/orchestrator/{PURPOSE}/calibration-settings.json"
    write_json(root / settings_rel, SETTINGS)
    built = py("scripts/jobs/build.py", "--mapping", mapping_rel, "--approval", "ode-map-1", "--settings", settings_rel)
    job_id = next(w.split("=", 1)[1] for w in built.stdout.split() if w.startswith("job_id="))
    job_dir = root / "runs" / "orchestrator" / PURPOSE / "jobs" / job_id
    assert job_id.startswith("job-") and (job_dir / "bundle.tar.gz").is_file(), built.stdout
    again = py("scripts/jobs/build.py", "--mapping", mapping_rel, "--approval", "ode-map-1", "--settings", settings_rel)
    assert f"job_id={job_id}" in again.stdout, "rebuild must be identical"
    step(f"bundle built: {job_id} (2 restarts, maxiter 3); an identical rebuild returns the same job")

    # 6. local start, wait, fetch
    py("scripts/jobs/launch.py", "local", "start", job_id)
    unpacked = scratch / "xdg-cache" / "agentic-jobs" / job_id
    results = unpacked / "results.tar.gz"
    deadline = time.monotonic() + 1200
    while not results.is_file():
        if time.monotonic() > deadline:
            log = (unpacked / "launch.log")
            raise SystemExit("timeout waiting for results.tar.gz; launch.log:\n" + (log.read_text() if log.is_file() else "(none)"))
        time.sleep(3)
    time.sleep(1)
    status = json.loads(py("scripts/jobs/launch.py", "local", "status", job_id).stdout)
    assert status["ok"] == [1, 2] or status["ok"] == 2, status
    py("scripts/jobs/launch.py", "local", "fetch", job_id)
    fetched = root / "inputs" / "calibration-results" / job_id / "results.tar.gz"
    assert fetched.is_file()
    with tarfile.open(fetched) as tf:  # the hold-out stays unseen until score_holdout
        shown = json.loads(tf.extractfile("predictions.json").read())
        simulated = json.loads(tf.extractfile("simulations.json").read())["conditions"]
    assert set(shown["rows"]) == set(ids[:-1]) and "highE" not in simulated, (sorted(shown["rows"]), sorted(simulated))
    step(f"local run finished (bootstrap, selftest, 2 restarts, collect) and fetched; status ok={status['ok']}")

    # 7. import with the real reproduce; best score below the nominal-score validate
    py("scripts/jobs/import_results.py", job_id)
    proposal_path = job_dir / "calibration_proposal.json"
    proposal = json.loads(proposal_path.read_text(encoding="utf-8"))
    best = min(r["score"] for r in proposal["restarts"] if r["status"] == "ok")
    nominal_out = scratch / "nominal.json"
    py("scripts/jobs/launch.py", "local", "validate", job_id, "--use", "fit", "--out", str(nominal_out))
    nominal = json.loads(nominal_out.read_text(encoding="utf-8"))["score"]
    assert math.isfinite(best) and best < nominal, f"best {best} is not lower than the nominal score {nominal}"
    assert not proposal["partial"] and {p["name"] for p in proposal["parameters"]} == {"kf2", "kf3"}, proposal
    step(f"import verified and reproduced; best score {best:.4g} < nominal score {nominal:.4g}")

    # 8. the ode_calibration approval
    proposal_rel = proposal_path.relative_to(root).as_posix()
    record({"id": "ode-cal-1", "kind": "stage_approval", "stage": "ode_calibration", "session_id": SESSION,
            "outputs": ["kf2"], "artifact": {"path": proposal_rel, "sha256": sha256(proposal_path)},
            "approved_by": "researcher", "authorization": "researcher: accept kf2 from the smoke calibration"})
    step("ode_calibration approval recorded (accepted: kf2)")

    # 9. the ode-modeler DATA_INPUT gate, through the real hook script
    hook = root / ".claude" / "scripts" / "stage_gate_guard.py"

    def gate(digest: str) -> subprocess.CompletedProcess:
        line = "DATA_INPUT " + json.dumps({"calibration": proposal_rel, "sha256": digest, "approval": "ode-cal-1"})
        prompt = f"{line}\nApply the accepted values; record_session_id {SESSION}."
        event = {"tool_name": "Agent", "tool_input": {"subagent_type": "ode-modeler", "prompt": prompt}}
        return subprocess.run([sys.executable, str(hook)], input=json.dumps(event), text=True, capture_output=True,
                              cwd=root, env={**env, "PYTHONPATH": str(root)}, timeout=60)

    accepted = gate(sha256(proposal_path))
    assert accepted.returncode == 0, accepted.stderr
    wrong = gate("0" * 64)
    assert wrong.returncode == 2 and "sha256" in wrong.stderr, (wrong.returncode, wrong.stderr)
    step("ode-modeler gate: brief accepted (exit 0); the same brief with a wrong sha refused (exit 2)")

    # 10. the hold-out, once
    py("scripts/jobs/score_holdout.py", job_id, "--approval", "ode-cal-1")
    holdout = json.loads((job_dir / "holdout_score.json").read_text(encoding="utf-8"))
    assert math.isfinite(holdout["score"]) and holdout["accepted"] == ["kf2"], holdout
    again = py("scripts/jobs/score_holdout.py", job_id, "--approval", "ode-cal-1", check=False)
    assert again.returncode != 0, "the hold-out must be scored only once"
    step(f"hold-out scored once: {holdout['score']:.4g} (a second scoring is refused)")

    # 11. printed commands for a (fake) host
    printed = py("scripts/jobs/launch.py", "print", job_id, "--host", "fake-cluster").stdout
    for needle in ("scp", "ssh", "fake-cluster", f"{job_id}.tar.gz", "remote.sh"):
        assert needle in printed, (needle, printed)
    step("launch.py print lists the scp/ssh commands for host fake-cluster")


if __name__ == "__main__":
    sys.exit(main())
