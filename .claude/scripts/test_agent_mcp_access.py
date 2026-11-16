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


RULES_RELOAD_RULE = ("Never re-add a captured configuration's rules by hand; if the captured configuration has a "
                     "rules CSV, return blocked.")
REVISION_RULE = "return `blocked` unless the session's current revision equals `proposal.model.revision`"


class DataHandoffInstructionTests(unittest.TestCase):
    def test_specialists_and_reviewers_carry_data_handoff_rules(self):
        expected = {
            ".claude/agents/network-curator.md": ("DATA_INPUT", "hgnc_symbol", "manifest.json", "absent from the database", "seeds.json"),
            ".claude/agents/boolean-dynamics-modeler.md": ("DATA_INPUT", "(context, perturbation)", "the context without the", "standalone approval"),
            ".claude/agents/multicellular-configurator.md": (
                "Applying an approved design or projection", 'DATA_INPUT {"design"', 'DATA_INPUT {"projection"',
                "`record_session_id` is a lineage anchor, never a live session", "Create a new PhysiCell session",
                "load the captured configuration's packaged XML with `load_xml_configuration`",
                "`<capture>/package/config/PhysiCell_settings.xml`, as an absolute path under the project root",
                "Never pass\n`record_session_id` to any tool", "Report the new full session ID",
                "`Derived from` = `record_session_id`",
                "Apply exactly the approved design", "import_maboss_handoff",
                "list_all_available_signals()", "list_all_available_behaviors(cell_type=...)",
                "so the invocation record holds them", "apply exactly the entries with the matching tools",
                "relative tolerance 1e-9", "Return `blocked` on any difference", "Never infer units",
                "take names and values only from the verified files named on the `DATA_INPUT` line",
                "no dispatcher check for this DATA_INPUT", RULES_RELOAD_RULE),
            ".claude/skills/physicell-workflow/SKILL.md": (RULES_RELOAD_RULE,
                "`DATA_INPUT` on line 2", "`record_session_id` is a\nlineage anchor, never a live session",
                "Create a new PhysiCell session", "`load_xml_configuration()`: `<capture>/package/config/PhysiCell_settings.xml`",
                "as an absolute path under the project root", "Never pass `record_session_id` to any tool",
                "Report the new full session ID", "`Derived from` = `record_session_id`",
                "apply exactly the approved design", "import each handoff\n  onto its cell definition",
                "so\n  the invocation record holds them", "apply exactly the entries with the matching\n  tools", "rel 1e-9",
                "return `blocked` on any\n  difference", "Never infer units",
                "take names and values only from the verified files named on\n  the `DATA_INPUT` line"),
            ".claude/agents/scientific-reviewer.md": ("Data-stage and mapping checklist", "unmapped", "locator"),
            ".claude/agents/reproducibility-auditor.md": ("Data-stage and mapping checklist", "data-derived", "locator", "seeds"),
            ".claude/skills/validate-stage/SKILL.md": ("data_mapping", "mapping.py check", "data_seeds", "--stage-gate"),
            "CLAUDE.md": ("DATA_INPUT", "data_mapping", "never paste data values", "seeds.json", "sha256 + locator", "--standalone-network", "draft --from"),
            ".claude/skills/neko-workflow/SKILL.md": ("seeds.json", "hgnc_symbol"),
            ".claude/skills/maboss-workflow/SKILL.md": ("standalone approval",),
            ".claude/agents/ode-modeler.md": ("Applying an approved calibration", "record_session_id", "data-calibrated",
                                              "re-apply every other configured value unchanged", "new revision",
                                              REVISION_RULE),
            ".claude/skills/biomass-workflow/SKILL.md": ("Apply an approved calibration", "calibration_proposal.json",
                                                         "re-applied unchanged", "record_session_id", "data-calibrated", "inspect_model",
                                                         "no dispatcher check", "never infer units", REVISION_RULE),
            "docs/ode-workflow.md": ("Calibrating parameters to measurements", "ode_calibration", "score_holdout.py",
                                     "popsize", "heuristic", "Only the bundle"),
            "docs/data-workflow.md": ("Data to ODE: calibration", "data-calibrated", "popsize"),
            "docs/agentic-operations.md": ("score_holdout.py", "dispatch.jsonl", "hosts.json", "import_results.py"),
            ".claude/agents/scientific-reviewer.md": ("ODE calibration checklist", "poorly_constrained", "hold-out"),
            ".claude/agents/reproducibility-auditor.md": ("ODE calibration checklist", "holdout_score.json", "results_sha256"),
        }
        for path, phrases in expected.items():
            text = (ROOT / path).read_text(encoding="utf-8")
            for phrase in phrases:
                with self.subTest(path=path, phrase=phrase):
                    self.assertIn(phrase, text)


if __name__ == "__main__":
    unittest.main()
