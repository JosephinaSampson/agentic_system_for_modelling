#!/usr/bin/env python3
"""End-to-end smoke test of approved data into PhysiCell settings (design -> projection -> verify -> build).

Run: python3 tests/integration/physicell_smoke.py

Works in a temporary copy of the repo under a scratch folder in ~/.cache (never /tmp), removed on success.
  1. build      scripts/physicell/build.py --template template downloads PhysiCell 1.14.2 from GitHub and compiles
                it into the scratch folder (minutes); `project` exists and the report says the run command was not
                executed. SKIP when GitHub is unreachable, there is no OpenMP toolchain or PHYSICELL_SMOKE_SKIP_BUILD=1.
  2. fixtures   with the fixture captures of tests/data: design draft -> fill -> check -> approval; the dictionary
                from a fixture invocation record; projection draft -> fill -> check -> approval (all through the CLIs).
  3. hook       the real dispatch hook accepts both multicellular-configurator DATA_INPUT forms and refuses a wrong sha.
  4. verify     verify_export.py passes on a hand-applied fixture "after" capture. Then, with the real PhysiCell and
                MaBoSS servers (mcp-biomodelling-servers 2.5.2 in-process through `uv run`; SKIP when unavailable):
                a MaBoSS handoff, stage A, design/dictionary/projection with their approvals, stage B by replay plus
                one rule and one input link, capture, verify -> passed; tampered captures -> failed.
  5. settings   the verified fixture capture is copied into the built project (build.py's copy_settings, after the
                same verification re-binding the --verification flag runs). SKIP when step 1 was skipped.
Prints `[ok]` lines and ends with PHYSICELL SMOKE PASSED; any failure exits non-zero and keeps the scratch folder.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

sys.dont_write_bytecode = True
REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from scripts import state  # noqa: E402
from scripts.capture.record_physicell_config import record_physicell  # noqa: E402
from scripts.physicell import build as physicell_build  # noqa: E402
from scripts.physicell import capture_dictionary as cd  # noqa: E402
from scripts.physicell import settings_xml as sx  # noqa: E402
from tests.data.test_physicell_design import CAPTURE, MABOSS, REL, SESSION, STAGE_A, write_config  # noqa: E402
from tests.data.test_physicell_dictionary import INVOCATION, good_calls  # noqa: E402
from tests.data.test_physicell_projection import PROJ, built_capture, fill_model_values, q  # noqa: E402
from tests.data.test_physicell_verify import VerifyBase, placeholder_rules  # noqa: E402

SERVERS = "mcp-biomodelling-servers==2.5.2"
VERSION = physicell_build.DEFAULT_VERSION
ARCHIVE = physicell_build.URL.format(version=VERSION)
DICT = "runs/orchestrator/p1/physicell_dictionary.json"
REAL = "p2"  # purpose of the real-server step
REAL_DESIGN = f"runs/orchestrator/{REAL}/physicell_design.json"
REAL_PROJ = f"runs/orchestrator/{REAL}/physicell_projection.json"
PREFIX = "mcp__physicell-agent__"
FIXTURE_GATE = 'STAGE_GATE {"upstream_session_id": "%s", "outputs": ["A", "B"]}' % MABOSS
STARTED = time.monotonic()

# Run inside `uv run --no-project --with mcp-biomodelling-servers==2.5.2 python -I`: the MaBoSS and PhysiCell
# servers' tool functions called in-process (each run is a fresh process, like an MCP session ending with its run).
SERVER_SCRIPT = r'''
import inspect, json, os, sys
from pathlib import Path

args = json.loads(sys.argv[2])
sys.path.insert(0, args["root"])  # -I: the repo copy is not importable otherwise
from pydantic.fields import FieldInfo
from pydantic_core import PydanticUndefined

ART = Path(os.environ["MCP_BIOMODELLING_ARTIFACT_ROOT"])
PROJECT = Path(args["root"])
CALLS = []


def invoke(fn, **given):
    """Call a decorated tool function in-process; parameters not given take their declared Field defaults."""
    kw = {}
    for name, p in inspect.signature(fn).parameters.items():
        if name in given:
            kw[name] = given[name]
        elif isinstance(p.default, FieldInfo):
            if p.default.default is not PydanticUndefined:
                kw[name] = p.default.default
            elif p.default.default_factory is not None:
                kw[name] = p.default.default_factory()
            else:
                raise TypeError(f"{fn.__name__}: {name} is required")
        elif p.default is inspect.Parameter.empty:
            raise TypeError(f"{fn.__name__}: {name} is required")
        else:
            kw[name] = p.default
    return fn(**kw)


def content(result):
    if isinstance(result, str):
        return [{"type": "text", "text": result}]
    if getattr(result, "isError", False):
        raise RuntimeError(f"tool error: {result}")
    if hasattr(result, "content"):
        return [block.model_dump(mode="json", exclude_none=True) for block in result.content]
    return [{"type": "text", "text": result.model_dump_json()}]  # a structured-output model: its JSON text


def physicell(server, name, **given):
    """A PhysiCell tool call, recorded as Claude Code's tool_calls.json content blocks."""
    result = invoke(getattr(server, name), **given)
    n = len(CALLS) // 2 + 1
    CALLS.append({"type": "tool_use", "id": f"toolu_{n:02d}", "name": "mcp__physicell-agent__" + name,
                  "input": given})
    CALLS.append({"type": "tool_result", "tool_use_id": f"toolu_{n:02d}", "content": content(result)})
    return result


def maboss():
    from mcp_biomodelling_servers.MaBoSS import server as m
    from scripts.capture.record_maboss_physicell_handoff import record_maboss
    work = Path(args["work"])
    work.mkdir(parents=True, exist_ok=True)
    (work / "smoke.bnd").write_text("Node A { logic = B; rate_up = 1; rate_down = 1; }\n"
                                    "Node B { logic = A; rate_up = 1; rate_down = 1; }\n")
    (work / "smoke.cfg").write_text("time_tick = 0.1;\nmax_time = 10;\nsample_count = 10;\n")
    invoke(m.create_session, set_as_default=True)
    invoke(m.build_simulation, bnd_path=str(work / "smoke.bnd"), cfg_path=str(work / "smoke.cfg"))
    invoke(m.set_maboss_output_nodes, output_nodes=["B"])
    invoke(m.export_maboss_handoff, target_cell_type="tumor", biological_context="physicell smoke",
           include_result=False, artifact_prefix="smoke_handoff")
    [manifest] = sorted((ART / "MaBoSS").glob("*/smoke_handoff.handoff.json"))
    record = record_maboss(PROJECT, None, manifest, "h1", artifact_root=ART / "MaBoSS")
    session = manifest.parent.name
    rel = f"runs/boolean-dynamics-modeler/{session}/physicell-handoffs/h1/import.handoff.json"
    assert (PROJECT / rel).is_file(), record
    return {"session": session, "handoff": rel}


def build_design(s, design):
    """The configurator's build of an approved new-configuration design: every tool argument from the design
    (ruling T9-a/T9-c), never a tool default."""
    d = design["domain"]
    physicell(s, "create_simulation_domain", domain_x=d["domain_x"]["value"], domain_y=d["domain_y"]["value"],
              use_2d=d["use_2d"], domain_z=None if d["domain_z"] is None else d["domain_z"]["value"],
              dx=d["dx"]["value"], max_time=d["max_time"]["value"])
    for sub in design["substrates"]:
        dirichlet = sub["dirichlet"]
        physicell(s, "add_single_substrate", substrate_name=sub["name"],
                  diffusion_coefficient=sub["diffusion_coefficient"]["value"], decay_rate=sub["decay_rate"]["value"],
                  initial_condition=sub["initial_condition"]["value"], units=sub["units"],
                  dirichlet_enabled=dirichlet["enabled"],
                  dirichlet_value=None if dirichlet["value"] is None else dirichlet["value"]["value"])
    for cell in design["cell_definitions"]:
        physicell(s, "add_single_cell_type", cell_type_name=cell["name"], cycle_model=cell["cycle_model"])
    for n, item in enumerate(design["intracellular"]):
        physicell(s, "import_maboss_handoff", manifest_path=str(PROJECT / item["maboss"]["handoff"]),
                  artifact_prefix=f"maboss_import_{n}")


def configure(stage):
    """Stage A builds the design and lists signals/behaviors; stage B replays the same calls in a new session,
    then applies exactly the projection's entries (model values) and exports the rules CSV before the XML."""
    from mcp_biomodelling_servers.PhysiCell import server as s
    design = json.loads((PROJECT / args["design"]).read_text())
    physicell(s, "create_session", set_as_default=True)
    build_design(s, design)
    if stage == "A":
        physicell(s, "list_all_available_signals")
        physicell(s, "list_all_available_behaviors", cell_type="tumor")
    else:
        projection = json.loads((PROJECT / args["projection"]).read_text())
        rules = False
        for e in projection["entries"]:
            if e["kind"] == "rule":
                # the configurator copies each model_value verbatim and does no arithmetic (ruling T9-e)
                physicell(s, "add_single_cell_rule", cell_type=e["cell_definition"], signal=e["signal"],
                          direction=e["direction"], behavior=e["behavior"],
                          saturation_value=e["saturation_value"]["model_value"],
                          half_max=e["half_max"]["model_value"], hill_power=e["hill_power"]["model_value"])
                rules = True
            elif e["kind"] == "physiboss_input":
                physicell(s, "add_physiboss_input_link", cell_type=e["cell_definition"], physicell_signal=e["signal"],
                          boolean_node=e["node"], action=e["action"], threshold=e["threshold"]["model_value"],
                          smoothing=e["smoothing"])
            else:
                raise ValueError(f"the smoke applies only rule and physiboss_input entries, not {e['kind']}")
        if rules:
            physicell(s, "export_cell_rules_csv", filename="cell_rules.csv")
    physicell(s, "export_xml_configuration", filename="PhysiCell_settings.xml")
    session = s._require_configuration(None).session_id
    folder = ART / "PhysiCell" / session
    from scripts.capture.record_physicell_config import record_physicell
    record_physicell(PROJECT, folder, folder / "PhysiCell_settings.xml", session, args["capture"])
    invocation = PROJECT / "runs/multicellular-configurator" / session / "invocation" / args["agent"]
    invocation.mkdir(parents=True)
    (invocation / "tool_calls.json").write_text(json.dumps(CALLS, indent=2))
    return {"session": session, "artifacts": str(folder),
            "capture": f"runs/multicellular-configurator/{session}/configs/{args['capture']}",
            "invocation": (invocation / "tool_calls.json").relative_to(PROJECT).as_posix()}


def discover():
    """The configurator's discovery run before a new-configuration design: the server's cycle models."""
    from mcp_biomodelling_servers.PhysiCell import server as s
    invoke(s.create_session, set_as_default=True)
    cycle = invoke(s.get_available_cycle_models)
    return {"cycle_models": [{"key": m["key"], "name": m["name"]} for m in cycle.structured_content["models"]]}


mode = sys.argv[1]
print("RESULT " + json.dumps(maboss() if mode == "maboss" else discover() if mode == "discover" else configure(mode)))
'''


def step(msg: str) -> None:
    print(f"[ok] {msg} ({time.monotonic() - STARTED:.0f}s)", flush=True)


def skip(what: str, reason: str) -> None:
    print(f"SKIP {what}: {reason}", flush=True)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def probe_network() -> str | None:
    request = urllib.request.Request(ARCHIVE, method="HEAD")
    try:
        urllib.request.urlopen(request, timeout=15).close()
    except urllib.error.HTTPError:
        return None  # the server answered
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return f"network unreachable ({ARCHIVE}: {exc})"
    return None


class Smoke:
    def __init__(self, scratch: Path):
        self.scratch = scratch
        self.root = scratch / "repo"
        shutil.copytree(REPO, self.root, symlinks=True, ignore=shutil.ignore_patterns(
            ".git", ".venv-*", "data-cache", "__pycache__", ".superpowers", "runs"))
        (self.root / "runs").mkdir(exist_ok=True)
        for name in ("CURRENT_STATE.md", "DECISIONS.md", "VALIDATION_PLAN.md"):
            shutil.copyfile(REPO / "templates" / name, self.root / name)
        (self.root / "inputs").mkdir(exist_ok=True)
        (self.root / "inputs" / "paper.txt").write_text("smoke source: oxygen and pressure response of tumor cells\n")
        self.tmp = scratch / "tmp"
        self.tmp.mkdir()
        self.env = {**os.environ, "CLAUDE_PROJECT_DIR": str(self.root), "PYTHONDONTWRITEBYTECODE": "1",
                    "TMPDIR": str(self.tmp)}
        self.env.pop("VIRTUAL_ENV", None)
        self.project: Path | None = None
        self.fixture_verification: str | None = None
        # the fixture helpers of tests/data, bound to the repo copy (their methods only use self.root/self.capture)
        self.helper = VerifyBase()
        self.helper.root = self.root

    # -- helpers

    def sh(self, *args: str, check: bool = True, stdin: str | None = None, env: dict | None = None,
           timeout: int | None = None) -> subprocess.CompletedProcess:
        proc = subprocess.run(list(args), cwd=self.root, env=env or self.env, text=True, capture_output=True,
                              input=stdin, timeout=timeout)
        if check and proc.returncode != 0:
            sys.stderr.write(proc.stdout + proc.stderr)
            raise SystemExit(f"command failed ({proc.returncode}): {' '.join(args)}")
        return proc

    def py(self, *args: str, **kw) -> subprocess.CompletedProcess:
        return self.sh(sys.executable, *args, **kw)

    def record(self, record: dict) -> None:
        file = self.scratch / f"{record['id']}.json"
        write_json(file, record)
        self.py("scripts/state.py", "record", "--input", str(file))

    def approve(self, record_id: str, stage: str, session: str, outputs: list, rel: str) -> None:
        self.record({"id": record_id, "kind": "stage_approval", "stage": stage, "session_id": session,
                     "outputs": outputs, "artifact": {"path": rel, "sha256": sha256(self.root / rel)},
                     "approved_by": "researcher", "authorization": f"researcher: smoke {stage} approved"})

    def hook(self, first_line: str, payload: dict, task: str) -> subprocess.CompletedProcess:
        prompt = f"{first_line}\nDATA_INPUT {json.dumps(payload)}\n{task}"
        event = {"tool_name": "Agent", "tool_input": {"subagent_type": "multicellular-configurator", "prompt": prompt}}
        proc = self.sh(sys.executable, str(self.root / ".claude/scripts/stage_gate_guard.py"), check=False,
                       stdin=json.dumps(event), env={**self.env, "PYTHONPATH": str(self.root)}, timeout=120)
        proc.prompt = prompt
        return proc

    def task_record(self, session: str, agent: str, prompt: str) -> None:
        """The invocation recorder's task.txt of a configurator run: the dispatched brief, verbatim."""
        task = self.root / f"runs/multicellular-configurator/{session}/invocation/{agent}/task.txt"
        task.parent.mkdir(parents=True, exist_ok=True)
        task.write_text(prompt + "\n", encoding="utf-8")

    def verify(self, projection: str, capture: str) -> tuple[int, dict, str]:
        proc = self.py("scripts/physicell/verify_export.py", projection, capture, check=False)
        assert proc.returncode in (0, 1), (proc.returncode, proc.stdout, proc.stderr)
        rel = (proc.stdout + proc.stderr).strip().splitlines()[-1].split(": ")[-1]
        return proc.returncode, json.loads((self.root / rel).read_text()), rel

    def draft_projection(self, purpose: str, capture: str, design: str, approval: str) -> Path:
        self.py("scripts/physicell/physicell_projection.py", "draft", "--purpose", purpose, "--capture", capture,
                "--design", design, "--design-approval", approval, "--source", "s1:inputs/paper.txt")
        return self.root / "runs" / "orchestrator" / purpose / "physicell_projection.json"

    def check_refused_then_passes(self, script: str, rel: str, fill) -> None:
        refused = self.py(script, "check", rel, check=False)
        assert refused.returncode == 1 and refused.stderr, f"a draft must not pass {script} check"
        data = json.loads((self.root / rel).read_text())
        fill(data)
        write_json(self.root / rel, data)
        passed = self.py(script, "check", rel, check=False)
        assert passed.returncode == 0 and not passed.stderr, passed.stdout + passed.stderr

    # -- 1. build

    def build(self) -> None:
        if os.environ.get("PHYSICELL_SMOKE_SKIP_BUILD") == "1":
            return skip("build (steps 1 and 5)", "PHYSICELL_SMOKE_SKIP_BUILD=1")
        reason = probe_network()
        if reason:
            return skip("build (steps 1 and 5)", reason)
        try:
            tool = physicell_build.toolchain(self.env, "template")
        except ValueError as exc:
            return skip("build (steps 1 and 5)", f"no PhysiCell toolchain: {exc}")
        dest = self.scratch / "physicell"
        began = time.monotonic()
        out = self.py("scripts/physicell/build.py", "--template", "template", "--version", VERSION,
                      "--dest", str(dest)).stdout
        seconds = time.monotonic() - began
        project = dest / f"PhysiCell-{VERSION}-template"
        assert (project / "project").is_file(), out
        [report_json] = sorted((self.root / "runs/orchestrator/physicell-build").glob("*-template/report.json"))
        report = json.loads(report_json.read_text())
        assert report["status"] == "ok" and report["project_sha256"] == sha256(project / "project"), report
        assert report["run_command"] == physicell_build.RUN_COMMAND and report["settings"] is None, report
        assert f"run command (not executed): `{physicell_build.RUN_COMMAND}`" in \
            (report_json.parent / "report.md").read_text(), "the report must say the run command was not executed"
        assert "run (not executed)" in out, out
        produced = [p.name for p in (project / "output").iterdir()] if (project / "output").is_dir() else []
        assert not any(name.endswith((".xml", ".mat", ".svg")) for name in produced), produced
        self.project = project
        step(f"build.py --template template: PhysiCell {VERSION} downloaded and compiled with {tool['compiler']} in "
             f"{seconds:.0f}s; `project` exists; report {report_json.parent.name} says the run command was not "
             "executed (output/ holds no simulation output)")

    # -- 2. fixture design, dictionary and projection

    def fixtures(self) -> None:
        h = self.helper
        source = self.scratch / "fixture-basis"
        record_physicell(self.root, source, write_config(source / "config"), SESSION, CAPTURE)
        basis = f"runs/multicellular-configurator/{SESSION}/configs/{CAPTURE}"
        h.capture = basis
        handoff = h.make_handoff()  # MaBoSS session maboss-session: reviewed, approved, exported (nodes A B C)
        self.py("scripts/physicell/physicell_design.py", "draft", "--purpose", "p1", "--basis", basis,
                "--source", "s1:inputs/paper.txt")

        def fill_design(data):
            for item in data["cell_definitions"] + data["substrates"]:
                item["description"] = f"existing {item['name']}"
            data["cell_definitions"].append({"name": "tumor_hypoxic", "cycle_model": "live",
                                             "description": "hypoxic variant", "variant_of": "tumor",
                                             "from": {"source": "s1", "locator": "Fig. 2"}})
            data["intracellular"] = [{"cell_definition": "tumor",
                                      "maboss": {"handoff": handoff["path"], "sha256": handoff["sha256"]}}]
            data["coupling_plan"] = [
                {"cell_definition": "tumor", "behavior_group": "motility", "route": "physiboss",
                 "notes": "oxygen drives node A; node B drives migration speed"},
                {"cell_definition": "tumor_hypoxic", "behavior_group": "motility", "route": "rules",
                 "notes": "oxygen-dependent speed"}]
            data["cycle_models"] = [{"key": "live", "name": "live"}]

        self.check_refused_then_passes("scripts/physicell/physicell_design.py", REL, fill_design)
        # the design is anchored on its basis capture's session; it is approved before stage A runs
        self.approve("design-1", "physicell_design", SESSION, ["default", "tumor", "tumor_hypoxic", "oxygen", "drug"],
                     REL)
        dispatched = self.hook(FIXTURE_GATE, {"design": REL, "sha256": sha256(self.root / REL),
                                              "approval": "design-1", "record_session_id": SESSION},
                               "Build the approved PhysiCell design.")
        assert dispatched.returncode == 0, dispatched.stderr
        # stage A: a new session (STAGE_A) built the design (the server's placeholder cell_rules); its recorded
        # brief carries the design's DATA_INPUT line (the lineage binding the capture to the design)
        self.task_record(STAGE_A, "agent-1", dispatched.prompt)
        h.capture = built_capture(self.root, STAGE_A, "cap-2", edit=placeholder_rules)
        invocation = self.root / INVOCATION
        invocation.parent.mkdir(parents=True, exist_ok=True)
        invocation.write_text(json.dumps(good_calls(extra_behaviors=("necrosis", "apoptosis"))))
        out = self.py("scripts/physicell/capture_dictionary.py", INVOCATION, h.capture, REL).stdout.strip()
        assert out == DICT, out
        dictionary = json.loads((self.root / DICT).read_text())
        assert dictionary["signals"][0]["name"] == "oxygen" and "tumor" in dictionary["behaviors"], dictionary
        self.draft_projection("p1", h.capture, REL, "design-1")
        self.check_refused_then_passes("scripts/physicell/physicell_projection.py", PROJ,
                                       lambda data: fill_model_values(data.update(entries=h.entries()) or data))
        self.approve("proj-1", "physicell_projection", STAGE_A, ["in1", "out1", "rule1", "set1"], PROJ)
        step(f"fixtures: design drafted (refused), filled, check passed, design-1 approved (anchor {SESSION}) and "
             f"dispatched; stage A in session {STAGE_A}; dictionary captured from its invocation record (bound by the "
             "design's DATA_INPUT line); projection drafted (refused), filled (in1 out1 rule1 set1), check passed, "
             f"proj-1 approved (session {STAGE_A})")

    # -- 3. the real dispatch hook

    def hooks(self) -> None:
        gate = FIXTURE_GATE
        design = {"design": REL, "sha256": sha256(self.root / REL), "approval": "design-1",
                  "record_session_id": SESSION}
        projection = {"projection": PROJ, "sha256": sha256(self.root / PROJ), "approval": "proj-1",
                      "record_session_id": STAGE_A}
        for payload, task in ((design, "Build the approved PhysiCell design."),
                              (projection, "Apply the approved projection.")):
            accepted = self.hook(gate, payload, task)
            assert accepted.returncode == 0, accepted.stderr
        wrong = self.hook(gate, {**projection, "sha256": "0" * 64}, "Apply the approved projection.")
        assert wrong.returncode == 2 and "sha256" in wrong.stderr, (wrong.returncode, wrong.stderr)
        wrong = self.hook(gate, {**design, "sha256": "0" * 64}, "Build the approved PhysiCell design.")
        assert wrong.returncode == 2 and "sha256" in wrong.stderr, (wrong.returncode, wrong.stderr)
        step("dispatch hook: design and projection DATA_INPUT forms accepted (exit 0); a wrong sha256 refused (exit 2)")

    # -- 4. verify a hand-applied fixture export

    def verify_fixture(self) -> None:
        after = self.helper.export()  # mc-session-2/cap-1 with every entry applied, plus its task.txt (lineage)
        code, report, rel = self.verify(PROJ, after)
        assert code == 0 and report["passed"] and not report["unexpected_changes"], report
        self.fixture_verification = rel
        step(f"verify_export.py on the hand-applied fixture capture {after}: passed ({rel})")

    # -- 4 (real servers)

    def server(self, mode: str, **args) -> dict:
        script = self.scratch / "server-scripts" / "server_steps.py"
        if not script.is_file():
            script.parent.mkdir(parents=True)
            script.write_text(SERVER_SCRIPT, encoding="utf-8")
        env = {**self.env, "MCP_BIOMODELLING_ARTIFACT_ROOT": str(self.scratch / "artifacts"),
               "TMPDIR": str(self.tmp)}
        proc = subprocess.run(["uv", "run", "--no-project", "--with", SERVERS, "python", "-I", str(script), mode,
                               json.dumps({"root": str(self.root), **args})],
                              cwd=script.parent, env=env, text=True, capture_output=True, timeout=900)
        if proc.returncode != 0:
            sys.stderr.write(proc.stdout + proc.stderr)
            raise SystemExit(f"server step {mode} failed ({proc.returncode})")
        return json.loads(next(line for line in proc.stdout.splitlines() if line.startswith("RESULT "))[7:])

    def servers_available(self) -> str | None:
        if shutil.which("uv") is None:
            return "uv is not installed"
        probe = self.scratch / "server-probe"
        probe.mkdir()
        try:
            proc = subprocess.run(["uv", "run", "--no-project", "--with", SERVERS, "python", "-I", "-c",
                                   "import mcp_biomodelling_servers.PhysiCell.server, "
                                   "mcp_biomodelling_servers.MaBoSS.server"],
                                  cwd=probe, text=True, capture_output=True, timeout=900,
                                  env={**self.env, "MCP_BIOMODELLING_ARTIFACT_ROOT": str(probe / "artifacts")})
        except (OSError, subprocess.SubprocessError) as exc:
            return f"`uv run --with {SERVERS}` failed: {exc}"
        if proc.returncode != 0:
            return f"`uv run --with {SERVERS}` failed: " + (proc.stderr.strip().splitlines() or ["?"])[-1]
        # the gate validates v2.1 handoff metadata with the backend contract package, through the configured
        # modelling Python (scripts/codex/dispatcher/launch.py): a scratch venv for the repo copy
        venv = self.scratch / "modelling-venv"
        python = venv / "bin" / "python"
        for argv in (["uv", "venv", "-q", str(venv)], ["uv", "pip", "install", "-q", "--python", str(python), SERVERS]):
            try:
                done = subprocess.run(argv, cwd=probe, env=self.env, text=True, capture_output=True, timeout=900)
            except (OSError, subprocess.SubprocessError) as exc:  # e.g. TimeoutExpired: SKIP this step, no crash
                return f"`{' '.join(argv[:3])}` failed: {type(exc).__name__}: {exc}"
            if done.returncode != 0:
                return f"`{' '.join(argv[:3])}` failed: " + (done.stderr.strip().splitlines() or ["?"])[-1]
        write_json(self.root / ".setup" / "dispatcher.local.json", {"python_executable": str(python)})
        return None

    def approve_handoff(self, session: str, rel: str, outputs: list) -> None:
        """Registry row, independent review and researcher approval of an exported MaBoSS handoff (like the
        fixture make_handoff)."""
        ref = {"path": rel, "sha256": sha256(self.root / rel)}
        review = {"id": f"review-{session}", "kind": "stage_review", "session_id": session,
                  "stage": "boolean-dynamics", "outcome": "approved", "artifact": ref}
        for role in ("scientific-reviewer", "reproducibility-auditor"):
            report = self.root / "runs" / f"{role}-{session}.md"
            report.write_text("independent bounded review (smoke)")
            review[role] = {"path": report.relative_to(self.root).as_posix(), "sha256": sha256(report)}
        state.update(self.root, "registry-add", dict(zip(state.REGISTRY, [
            "boolean-dynamics-modeler", session, "boolean-dynamics", "active", "—", "pending", rel])))
        self.record(review)
        self.record({"id": f"approval-{session}", "kind": "stage_approval", "session_id": session,
                     "stage": "boolean-dynamics", "downstream_stage": "multicellular", "outputs": outputs,
                     "approved_by": "researcher", "authorization": "researcher: smoke handoff approved",
                     "artifact": ref})
        state.update(self.root, "registry-handoff", {"session_id": session, "handoff": "approved"})
        state.update(self.root, "registry-handoff", {"session_id": session, "handoff": "exported", "artifact": ref})

    def real_servers(self) -> None:
        reason = self.servers_available()
        if reason:
            return skip("real-server replay", reason)
        # MaBoSS: a 2-node model, output B, exported for tumor and captured into the project
        mab = self.server("maboss", work=str(self.scratch / "maboss-input"))
        self.approve_handoff(mab["session"], mab["handoff"], ["B"])
        manifest = str(self.root / mab["handoff"])
        step(f"real MaBoSS handoff exported, captured ({mab['handoff']}), reviewed and approved")

        # the design of a new configuration, anchored on its purpose, approved before stage A runs
        found = self.server("discover")
        self.py("scripts/physicell/physicell_design.py", "draft", "--purpose", REAL, "--source", "s1:inputs/paper.txt")
        cite = {"source": "s1", "locator": "Methods"}

        def fill_design(data):
            def length(value):
                return {"value": value, "unit": "micron"}

            def value(number, unit):
                return {"value": number, "unit": unit, "from": cite}

            # every create_simulation_domain / add_single_substrate / add_single_cell_type argument, cited
            data["domain"] = {"domain_x": length(200.0), "domain_y": length(200.0), "use_2d": True, "domain_z": None,
                              "dx": length(20.0), "max_time": {"value": 100.0, "unit": "min"}, "from": cite}
            data["cycle_models"] = found["cycle_models"]
            data["cell_definitions"] = [{"name": "tumor", "cycle_model": "Ki67_basic", "description": "tumor cells",
                                         "from": cite}]
            data["substrates"] = [{"name": "oxygen", "units": "mmHg", "description": "oxygen", "from": cite,
                                   "diffusion_coefficient": value(100000.0, "micron^2/min"),
                                   "decay_rate": value(0.1, "1/min"), "initial_condition": value(38.0, "mmHg"),
                                   "dirichlet": {"enabled": True, "value": value(38.0, "mmHg")}}]
            data["intracellular"] = [{"cell_definition": "tumor",
                                      "maboss": {"handoff": mab["handoff"], "sha256": sha256(Path(manifest))}}]
            data["coupling_plan"] = [
                {"cell_definition": "tumor", "behavior_group": "oxygen sensing", "route": "physiboss",
                 "notes": "oxygen drives node A"},
                {"cell_definition": "tumor", "behavior_group": "apoptosis", "route": "rules",
                 "notes": "pressure increases apoptosis"}]

        self.check_refused_then_passes("scripts/physicell/physicell_design.py", REAL_DESIGN, fill_design)
        assert json.loads((self.root / REAL_DESIGN).read_text())["session"] == REAL
        self.approve("design-2", "physicell_design", REAL, ["tumor", "oxygen"], REAL_DESIGN)
        gate = 'STAGE_GATE {"upstream_session_id": "%s", "outputs": ["B"]}' % mab["session"]
        design_form = self.hook(gate, {"design": REAL_DESIGN, "sha256": sha256(self.root / REAL_DESIGN),
                                       "approval": "design-2", "record_session_id": REAL},
                                "Build the approved PhysiCell design.")
        assert design_form.returncode == 0, design_form.stderr

        # stage A: a new PhysiCell session builds the design, imports the handoff, lists signals and behaviors;
        # the recorder keeps the dispatched brief (line 2: the design's DATA_INPUT line) as its task.txt
        a = self.server("A", design=REAL_DESIGN, capture="capA", agent="agent-a")
        assert a["session"] != REAL
        self.task_record(a["session"], "agent-a", design_form.prompt)
        bound = sx.capture(self.root, a["capture"])
        assert bound["rules_path"] is None, bound
        # import_maboss_handoff copies the handoff's .bnd/.cfg verbatim (server copy_verified_handoff_artifact) and the
        # recorder copies them into package/config: conformance compares exactly these bytes (PR #17 review 4586110937)
        handoff = json.loads((self.root / mab["handoff"]).read_text())
        deps = {Path(d["path"]).name: d["sha256"] for d in bound["manifest"]["dependencies"]}
        model = ET.fromstring((self.root / bound["xml_path"]).read_bytes()).find(
            "cell_definitions/cell_definition[@name='tumor']/phenotype/intracellular")
        for tag, field in (("bnd_filename", "bnd_file"), ("cfg_filename", "cfg_file")):
            name = Path(model.findtext(tag).strip()).name
            assert deps[name] == handoff[field]["sha256"], (tag, name, deps, handoff[field])
        # the design check (pd.load_checked, whose basis tree and manifests conformance takes) needs the scratch
        # project's modelling Python, so it runs in capture_dictionary.py below; here the same inputs are built
        design = json.loads((self.root / REAL_DESIGN).read_text())
        basis = None if design["basis"] is None else sx.capture_tree(self.root, design["basis"]["capture"])[1]
        manifests = {i["cell_definition"]: cd.pd.handoff_manifest(self.root, i["maboss"])
                     for i in design["intracellular"]}
        problems = cd.conformance(design, *sx.capture_tree(self.root, a["capture"]), basis, manifests)
        assert problems == [], problems
        # a stage-A capture that differs from the design (oxygen decay_rate) is refused before any dictionary
        folder = Path(a["artifacts"])
        tree = ET.parse(folder / sx.XML_NAME)
        tree.getroot().find("microenvironment_setup/variable[@name='oxygen']/physical_parameter_set/decay_rate").text = "0.2"
        tree.write(folder / "nonconforming.xml", encoding="utf-8", xml_declaration=True)
        record_physicell(self.root, folder, folder / "nonconforming.xml", a["session"], "capA-bad")
        bad = self.py("scripts/physicell/capture_dictionary.py", a["invocation"],
                      f"runs/multicellular-configurator/{a['session']}/configs/capA-bad", REAL_DESIGN, check=False)
        assert bad.returncode == 1 and "does not conform to the design" in bad.stderr \
            and "oxygen decay_rate: the capture holds '0.2', the design 0.1" in bad.stderr, bad.stderr
        out = self.py("scripts/physicell/capture_dictionary.py", a["invocation"], a["capture"],
                      REAL_DESIGN).stdout.strip()
        dictionary = json.loads((self.root / out).read_text())
        signals = [s["name"] for s in dictionary["signals"]]
        behaviors = [b["name"] for b in dictionary["behaviors"]["tumor"]]
        assert {"oxygen", "pressure"} <= set(signals) and "apoptosis" in behaviors, (signals, behaviors)
        step(f"real design {REAL} (anchor {REAL}) checked, approved and dispatched (hook exit 0); stage A in session "
             f"{a['session']} built from the design's values and captured ({a['capture']}); its MaBoSS .bnd/.cfg are "
             "the handoff's bytes (import_maboss_handoff copies verbatim); it conforms to the design "
             "(a decay_rate-tampered copy is refused); dictionary from its real invocation record, bound by the "
             f"design's DATA_INPUT line ({len(signals)} signals, {len(behaviors)} tumor behaviors)")

        self.draft_projection(REAL, a["capture"], REAL_DESIGN, "design-2")
        common = {"from": cite, "justification": "reported in the source"}
        entries = [
            {"entry_id": "rule1", "kind": "rule", **common, "cell_definition": "tumor", "signal": "pressure",
             "direction": "increases", "behavior": "apoptosis",
             "saturation_value": q(0.6, "1/h", "unit_factor", 1 / 60, target_unit="1/min"),
             "half_max": q(0.5, "dimensionless"), "hill_power": q(2, "dimensionless")},
            {"entry_id": "in1", "kind": "physiboss_input", **common, "cell_definition": "tumor", "signal": "oxygen",
             "node": "A", "action": "activation", "threshold": q(10, "mmHg"), "smoothing": 0}]
        # without model values the check refuses and prints the value to paste (the orchestrator pastes them)
        draft = json.loads((self.root / REAL_PROJ).read_text())
        write_json(self.root / REAL_PROJ, {**draft, "entries": entries})
        unpasted = self.py("scripts/physicell/physicell_projection.py", "check", REAL_PROJ, check=False)
        assert unpasted.returncode == 1 and "saturation_value: model_value must be a number: paste 0.01" in \
            unpasted.stderr, unpasted.stderr
        write_json(self.root / REAL_PROJ, draft)
        self.check_refused_then_passes("scripts/physicell/physicell_projection.py", REAL_PROJ,
                                       lambda data: fill_model_values(data.update(entries=entries) or data))
        self.approve("proj-2", "physicell_projection", a["session"], ["rule1", "in1"], REAL_PROJ)
        dispatched = self.hook(gate, {"projection": REAL_PROJ, "sha256": sha256(self.root / REAL_PROJ),
                                      "approval": "proj-2", "record_session_id": a["session"]},
                               "Replay the approved design, then apply the approved projection.")
        assert dispatched.returncode == 0, dispatched.stderr
        step("real projection (rule1: pressure increases apoptosis, 0.6 1/h -> 0.01 1/min; in1: oxygen -> A) "
             "refused without model values (the check prints the value to paste), pasted, checked and approved; the "
             "hook accepts its DATA_INPUT line; stage B applies each model_value verbatim")

        # stage B: a new session replays the design, applies the entries, exports rules CSV then XML
        b = self.server("B", design=REAL_DESIGN, capture="capB", agent="agent-b", projection=REAL_PROJ)
        assert b["session"] != a["session"]
        self.task_record(b["session"], "agent-b", dispatched.prompt)
        code, report, rel = self.verify(REAL_PROJ, b["capture"])
        assert code == 0 and report["passed"], json.dumps(report["entries"] + report["unexpected_changes"])
        step(f"real stage B: session {b['session']} replayed the design and applied rule1 and in1; "
             f"verify_export.py passed ({rel})")

        # tampered copies of the stage-B XML fail verification
        folder = Path(b["artifacts"])
        tree = ET.parse(folder / sx.XML_NAME)
        decay = tree.getroot().find("microenvironment_setup/variable[@name='oxygen']/physical_parameter_set/decay_rate")
        decay.text = "0.2"
        tree.write(folder / "tampered_decay.xml", encoding="utf-8", xml_declaration=True)
        record_physicell(self.root, folder, folder / "tampered_decay.xml", b["session"], "capB-decay")
        code, report, _ = self.verify(REAL_PROJ, f"runs/multicellular-configurator/{b['session']}/configs/capB-decay")
        assert code == 1 and not report["passed"] and "decay_rate" in json.dumps(report["unexpected_changes"]), report
        # the recorder relocates every ruleset folder to ./config, so a moved folder exists only as a changed
        # package XML (re-hashed in capture.json, the fixture tests' VerifyBase.tamper)
        shutil.copyfile(folder / sx.XML_NAME, folder / "tampered_folder.xml")
        record_physicell(self.root, folder, folder / "tampered_folder.xml", b["session"], "capB-folder")

        def move_folder(xml_root):
            enabled = next(r for r in xml_root.findall("cell_rules/rulesets/ruleset") if r.get("enabled") == "true")
            enabled.find("folder").text = "./elsewhere"

        forged = self.helper.tamper(f"runs/multicellular-configurator/{b['session']}/configs/capB-folder", move_folder)
        code, report, _ = self.verify(REAL_PROJ, forged)
        assert code == 1 and not report["passed"] and "./elsewhere" in json.dumps(report["unexpected_changes"]), report
        step("tampered stage-B captures fail verify_export.py (exit 1): oxygen decay_rate changed (re-captured); "
             "enabled ruleset folder ./elsewhere (forged package XML)")

    # -- 5. settings copy

    def settings(self) -> None:
        if self.project is None:
            return skip("settings copy (step 5)", "step 1 (build) was skipped")
        verified = physicell_build._verification(self.root, self.fixture_verification)  # recomputes (T9-d)
        rel, data, tree = verified.rel, verified.data, verified.tree
        # build.py refuses these MaBoSS-bearing settings with --template template (review M5); the copy mechanism is
        # exercised directly on the built template project here
        assert physicell_build._physiboss(tree), "the fixture capture carries a MaBoSS model"
        template_xml = (self.project / "config" / sx.XML_NAME).read_bytes()
        copied = physicell_build.copy_settings(self.root, self.project, rel, data)
        package = self.root / Path(data["after"]["xml_path"]).parent
        for item in copied:
            assert (self.project / "config" / item["name"]).read_bytes() == (package / item["name"]).read_bytes()
        backup = self.project / "config" / f"{sx.XML_NAME}.template-backup"
        assert backup.read_bytes() == template_xml, "the template settings must be kept as a backup"
        step(f"verification recomputed by build.py and agreed; verified capture copied into the built project's config/ "
             f"({', '.join(c['name'] for c in copied)}); the template PhysiCell_settings.xml kept as .template-backup "
             "(build.py itself would require template_BM for these MaBoSS settings)")


def main() -> int:
    cache = Path.home() / ".cache"
    cache.mkdir(parents=True, exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix="physicell-smoke-", dir=cache)).resolve()
    try:
        smoke = Smoke(scratch)
        smoke.build()
        smoke.fixtures()
        smoke.hooks()
        smoke.verify_fixture()
        smoke.real_servers()
        smoke.settings()
    except BaseException:
        print(f"FAILED; scratch dir left at {scratch}", file=sys.stderr)
        raise
    shutil.rmtree(scratch)
    assert not scratch.exists()
    step(f"scratch dir removed ({scratch.name})")
    print("PHYSICELL SMOKE PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
