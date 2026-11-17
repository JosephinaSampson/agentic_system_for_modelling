#!/usr/bin/env python3
"""Regression tests for isolated specialist MCP and workflow-skill access."""

from __future__ import annotations

import json
import re
import shutil
import tempfile
import tomllib
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]

AGENTS = {
    "ode-modeler.md": ("biomass", "mcp-biomass-server", "biomass-workflow"),
    "network-curator.md": ("neko", "mcp-neko-server", "neko-workflow"),
    "boolean-dynamics-modeler.md": (
        "maboss",
        "mcp-maboss-server",
        "maboss-workflow",
    ),
    "multicellular-configurator.md": (
        "physicell",
        "mcp-physicell-server",
        "physicell-workflow",
    ),
}

RESOURCE_BRIDGE_NAMES = {
    "ListMcpResources",
    "ReadMcpResource",
    "ListMcpResourcesTool",
    "ReadMcpResourceTool",
}


def agent_text(filename: str) -> str:
    return (ROOT / ".claude" / "agents" / filename).read_text(encoding="utf-8")


def frontmatter(text: str) -> str:
    marker = "---\n"
    if not text.startswith(marker):
        raise AssertionError("agent definition has no opening frontmatter marker")
    parts = text[len(marker) :].split("\n---\n", 1)
    if len(parts) != 2:
        raise AssertionError("agent definition has no closing frontmatter marker")
    return parts[0]


class SpecialistMcpAccessTests(unittest.TestCase):
    def check_transport(self, metadata: str, server: str, executable: str, *, generated: bool = False) -> None:
        self.assertIn(f"mcpServers:\n  - {server}-agent:\n", metadata)
        block = metadata.split("mcpServers:\n", 1)[1].split("\ntools:", 1)[0]
        self.assertEqual(re.findall(r"^  - ([\w-]+):$", block, re.MULTILINE), [server + "-agent"])
        def value(key: str):
            matches = re.findall(rf"^ +{key}: (.+)$", block, re.MULTILINE)
            self.assertLessEqual(len(matches), 1)
            return json.loads(matches[0]) if matches else None
        command = value("command")
        arguments = value("args")
        if arguments is not None:
            # Marketplace plugin launch, pinned to the tested release; tracked
            # defaults use a bare uvx and setup writes an absolute one.
            manifest = tomllib.loads((ROOT / "setup/dependencies.toml").read_text())
            self.assertEqual(Path(command).name, "uvx")
            self.assertEqual(arguments, ["--from", f"{manifest['package']}=={manifest['version']}", executable])
            self.assertIsNone(value("CONDA_PREFIX"))
            if Path(command).is_absolute():
                self.assertEqual(value("PATH").split(":")[0], str(Path(command).parent))
        else:
            prefix = Path(value("CONDA_PREFIX"))
            self.assertTrue(prefix.is_absolute())
            self.assertEqual(command, str(prefix / "bin" / executable))
            self.assertEqual(value("PATH").split(":")[0], str(prefix / "bin"))
        self.assertNotIn("${MCP_MODELLING_ENV}", metadata)
        if generated:
            # Setup gives each server a private disk-backed TMPDIR (engine scratch files).
            tmpdir = Path(value("TMPDIR"))
            self.assertTrue(tmpdir.is_absolute())
            self.assertEqual(tmpdir.parts[-3:], ("mcp-biomodelling", "tmp", server))

    def test_agents_define_isolated_inline_servers(self) -> None:
        for filename, (server, executable, _) in AGENTS.items():
            with self.subTest(agent=filename):
                self.check_transport(frontmatter(agent_text(filename)), server, executable)

    def test_generated_transports_support_other_environment_paths(self) -> None:
        from scripts.setup_support import configure_claude
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            shutil.copytree(ROOT / ".claude/agents", root / ".claude/agents")
            rendered = configure_claude.render(root, root / "another environment", with_biomass=True)
            for filename, (server, executable, _) in AGENTS.items():
                self.check_transport(frontmatter(rendered[root / ".claude/agents" / filename]), server, executable,
                                     generated=True)

    def test_generated_plugin_transports_pin_the_released_launch(self) -> None:
        import tomllib as toml
        from scripts.setup_support import configure_claude
        manifest = toml.loads((ROOT / "setup/dependencies.toml").read_text())
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            shutil.copytree(ROOT / ".claude/agents", root / ".claude/agents")
            plugin = {"uvx": "/opt/uv tools/bin/uvx", "manifest": manifest}
            rendered = configure_claude.render(root, None, with_biomass=True, plugin=plugin)
            for filename, (server, executable, _) in AGENTS.items():
                self.check_transport(frontmatter(rendered[root / ".claude/agents" / filename]), server, executable,
                                     generated=True)
            hook = rendered[root / ".claude/agents/literature-reviewer.md"]
            self.assertIn('          command: "python3"\n', hook)
            for path, text in rendered.items():
                path.write_text(text)
            self.assertEqual(configure_claude.render(root, None, with_biomass=True, plugin=plugin), rendered)

    def test_required_validation_reviewers_exist_and_are_read_only(self) -> None:
        workflow = (ROOT / ".claude/skills/validate-stage/SKILL.md").read_text()
        for role in ("scientific-reviewer", "reproducibility-auditor"):
            self.assertIn(f"`{role}`", workflow)
            text = agent_text(role + ".md")
            metadata = frontmatter(text)
            allowed = metadata.split("\ntools:\n", 1)[1].split("\ndisallowedTools:", 1)[0]
            self.assertEqual(allowed.splitlines(), ["  - Read", "  - Grep", "  - Glob"])
            self.assertIn("  - 'mcp__*'", metadata)
            self.assertNotIn("mcpServers:", metadata)
            self.assertIn("does\nnot authorize a stage transition", text)

    def test_agents_preload_matching_workflow_skills(self) -> None:
        for filename, (_, _, skill) in AGENTS.items():
            with self.subTest(agent=filename):
                metadata = frontmatter(agent_text(filename))
                skill_path = ROOT / ".claude" / "skills" / skill / "SKILL.md"
                self.assertIn(f"skills:\n  - {skill}\n", metadata)
                self.assertTrue(skill_path.is_file(), f"missing skill: {skill_path}")
                self.assertIn(
                    f"name: {skill}\n", skill_path.read_text(encoding="utf-8")
                )

    def test_agents_keep_tool_search_and_server_tools(self) -> None:
        for filename, (server, _, _) in AGENTS.items():
            with self.subTest(agent=filename):
                metadata = frontmatter(agent_text(filename))
                self.assertIn("  - ToolSearch\n", metadata)
                self.assertIn(f"  - 'mcp__{server}-agent__*'\n", metadata)

    def test_resource_bridge_dependency_is_removed(self) -> None:
        paths = [
            ROOT / "CLAUDE.md",
            ROOT / "README.md",
            ROOT / "docs" / "agentic-biomodelling-architecture.md",
        ]
        paths.extend(ROOT / ".claude" / "agents" / filename for filename in AGENTS)
        paths.extend(
            ROOT / ".claude" / "skills" / skill / "SKILL.md"
            for _, _, skill in AGENTS.values()
        )

        for path in paths:
            with self.subTest(path=path):
                text = path.read_text(encoding="utf-8")
                for name in RESOURCE_BRIDGE_NAMES:
                    self.assertNotIn(name, text)
                self.assertNotIn("docs://neko/agent_manual", text)
                self.assertNotIn("docs://maboss/agent_manual", text)
                self.assertNotIn("docs://physicell/agent_manual", text)


class DataAnalystAccessTests(unittest.TestCase):
    def setUp(self):
        self.text = agent_text("data-analyst.md")
        self.metadata = frontmatter(self.text)

    def test_data_analyst_has_no_mcp_and_denies_mcp(self):
        self.assertNotIn("mcpServers", self.metadata)
        deny = self.metadata.split("disallowedTools:\n", 1)[1].split("\npermissionMode:", 1)[0]
        for item in ("Edit", "Skill", "Agent", "'mcp__*'"):
            self.assertIn(f"  - {item}\n", deny + "\n")

    def test_data_analyst_preloads_data_workflow(self):
        self.assertIn("skills:\n  - data-workflow\n", self.metadata)
        self.assertTrue((ROOT / ".claude" / "skills" / "data-workflow" / "SKILL.md").is_file())

    def test_data_analyst_hooks_reference_data_guard(self):
        for matcher, mode in (("Bash", "bash"), ("Write", "write")):
            self.assertIn(f'matcher: "{matcher}"', self.metadata)
            self.assertIn(
                'python3 \\"$CLAUDE_PROJECT_DIR/.claude/scripts/data_guard.py\\" ' + mode,
                self.metadata,
            )


PHYSICELL_DATA_RULES = (
    "`record_session_id` is a lineage anchor, never a live session", "Create a new PhysiCell session for either form",
    "Never pass `record_session_id` to any tool", "Report the new full session ID",
    "the orchestrator registers it (stage A: `Derived from` = the `STAGE_GATE`\n  upstream MaBoSS session or standalone "
    "decision id, plus the basis capture's session with a basis; stage B:\n  `Derived from` = the stage-A session).",
    "Never infer units", "get_available_cycle_models()",
    "take names and values only from the verified files named on the `DATA_INPUT` line",
    "Never re-add a captured configuration's rules by hand; if the captured configuration has a rules CSV, return blocked.",
    "for a projection, also the design its `design.path` names and the MaBoSS handoff manifests that design lists",
    "Return `blocked` if a MaBoSS handoff manifest the design names is missing or fails `import_maboss_handoff`'s "
    "verification.",
    # ruling T9-c: every tool argument comes from the design
    "Pass every tool argument explicitly from the design: domain, substrate parameters and Dirichlet settings, cycle "
    "models. Never use a tool default or a typical value. If the design lacks a value a tool requires, return "
    "blocked. Add no rule, link or other value.",
)
# Ruling T5-e: a projection is applied by replaying its design in a new session, never by loading its capture.
PHYSICELL_STALE = ("`Derived from` = `record_session_id`", "for every projection", "projection's `capture.path`", "On the loaded capture", "on the loaded capture",
                   "load the captured configuration's packaged XML",
                   "Add no rule, link, parameter or other value while building", "Add no rule, link or value while building",
                   # ruling T9-e: the configurator copies model values, never computes them
                   "value*factor", "factor/value", "factor*ln(2)/value")
REVISION_RULE = "return `blocked` unless the session's current revision equals `proposal.model.revision`"


class DataHandoffInstructionTests(unittest.TestCase):
    def test_specialists_and_reviewers_carry_data_handoff_rules(self):
        expected = {
            ".claude/agents/network-curator.md": ("DATA_INPUT", "hgnc_symbol", "manifest.json", "absent from the database", "seeds.json"),
            ".claude/agents/boolean-dynamics-modeler.md": ("DATA_INPUT", "(context, perturbation)", "the context without the", "standalone approval"),
            ".claude/agents/multicellular-configurator.md": PHYSICELL_DATA_RULES + (
                "Applying an approved design or projection", 'DATA_INPUT {"design"', 'DATA_INPUT {"projection"',
                "load the basis capture's packaged XML with `load_xml_configuration`",
                "`<basis capture>/package/config/PhysiCell_settings.xml`, as an absolute path under the project root",
                "Import each `intracellular` handoff onto its cell definition with `import_maboss_handoff`",
                "Apply exactly the approved design", "list_all_available_signals()",
                "list_all_available_behaviors(cell_type=...)", "so the invocation record holds them",
                "Re-apply the approved design exactly as in stage A",
                "build the design named in the verified\n  projection's `design.path`",
                "Do not call the discovery list tools again", "Then apply exactly the entries with the matching tools",
                "relative tolerance 1e-9", "Return `blocked` on any difference",
                "Apply each quantity's `model_value` verbatim", "do no arithmetic",
                "Apply only the projection entries. Then export (Cell Rules CSV before XML).",
                "under Codex the runtime refuses\nthis DATA_INPUT (Claude-only for now)"),
            ".claude/skills/physicell-workflow/SKILL.md": PHYSICELL_DATA_RULES + (
                "`DATA_INPUT` on line 2", "load the basis capture's packaged XML with\n  `load_xml_configuration()`",
                "(`<basis capture>/package/config/PhysiCell_settings.xml`, as an absolute path under\n  the project root)",
                "import each handoff onto its cell definition", "apply exactly the approved design",
                "so the invocation record holds them", "re-apply the approved design exactly as in stage A",
                "build the design named in the verified\n  projection's `design.path`",
                "do not call the discovery list tools again", "apply exactly the entries with\n  the matching tools",
                "rel 1e-9", "return `blocked` on any difference", "Apply only the projection entries",
                "at each quantity's `model_value`, applied verbatim (no arithmetic)",
                "Then export (rules CSV before XML)."),
            # review M6: one key per file, so every assertion runs
            ".claude/agents/scientific-reviewer.md": (
                "Data-stage and mapping checklist", "unmapped", "locator",
                "ODE calibration checklist", "poorly_constrained", "hold-out",
                "PhysiCell data-stage checklist", "no value is a tool default", "exactly the one the exported XML writes",
                "the stage-A capture conforms to the design", "projection transforms", "`model_value` is",
                "one writer per behavior"),
            ".claude/agents/reproducibility-auditor.md": (
                "Data-stage and mapping checklist", "data-derived", "locator", "seeds",
                "ODE calibration checklist", "holdout_score.json", "results_sha256",
                "PhysiCell data-stage checklist", "hashes:", "lineage:", "task.txt", "verification file:",
                "build report:"),
            ".claude/skills/validate-stage/SKILL.md": ("data_mapping", "mapping.py check", "data_seeds", "--stage-gate"),
            "CLAUDE.md": ("DATA_INPUT", "data_mapping", "never paste data values", "seeds.json", "sha256 + locator", "--standalone-network", "draft --from",
                          "configurator discovery run (`STAGE_GATE` without `DATA_INPUT`) calling `get_available_cycle_models`",
                          "validate-stage on the stage-A capture before the projection is drafted against it",
                          "validate-stage on the verified capture before build\nor closure",
                          "the stage-A row\nhas `Derived from` = the `STAGE_GATE` upstream MaBoSS session or standalone decision id",
                          "the stage-B row has `Derived from` = the stage-A\nsession"),
            ".claude/skills/neko-workflow/SKILL.md": ("seeds.json", "hgnc_symbol"),
            ".claude/skills/maboss-workflow/SKILL.md": ("standalone approval",),
            ".claude/agents/ode-modeler.md": ("Applying an approved calibration", "record_session_id", "data-calibrated",
                                              "re-apply every other configured value unchanged", "new revision",
                                              REVISION_RULE),
            ".claude/skills/biomass-workflow/SKILL.md": ("Apply an approved calibration", "calibration_proposal.json",
                                                         "re-applied unchanged", "record_session_id", "data-calibrated", "inspect_model",
                                                         "Applying a calibration is Claude-only for now: the Codex runtime refuses", "never infer units", REVISION_RULE),
            "docs/ode-workflow.md": ("Calibrating parameters to measurements", "ode_calibration", "score_holdout.py",
                                     "popsize", "heuristic", "Only the bundle"),
            "docs/data-workflow.md": ("Data to ODE: calibration", "data-calibrated", "popsize",
                                      "| discovery | configurator, `STAGE_GATE` without `DATA_INPUT`", "| review A | validate-stage",
                                      "| review B | validate-stage", "The stage-A row has `Derived from` = the",
                                      "The stage-B row has `Derived from` = the stage-A session"),
            "docs/agentic-operations.md": ("score_holdout.py", "dispatch.jsonl", "hosts.json", "import_results.py",
                                           "Discovery (a new configuration only)", "passes validate-stage before the projection"),
        }
        for path, phrases in expected.items():
            text = (ROOT / path).read_text(encoding="utf-8")
            for phrase in phrases:
                with self.subTest(path=path, phrase=phrase):
                    self.assertIn(phrase, text)

    def test_expected_pins_have_no_repeated_key(self):
        # review M6: a repeated dict key silently drops the earlier tuple, so its assertions never run
        import ast
        tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Dict):
                keys = [k.value for k in node.keys if isinstance(k, ast.Constant)]
                self.assertEqual(sorted({k for k in keys if keys.count(k) > 1}), [], keys)

    def test_configurator_never_loads_a_projection_capture(self):
        for path in (".claude/agents/multicellular-configurator.md", ".claude/skills/physicell-workflow/SKILL.md"):
            text = (ROOT / path).read_text(encoding="utf-8")
            for phrase in PHYSICELL_STALE:
                with self.subTest(path=path, phrase=phrase):
                    self.assertNotIn(phrase, text)


if __name__ == "__main__":
    unittest.main()
