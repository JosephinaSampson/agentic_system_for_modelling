"""Modelling servers get a private, disk-backed TMPDIR rendered by setup."""
from __future__ import annotations

import json
import os
import re
import shutil
import stat
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from scripts import setup
from scripts.setup_support import configure_claude, configure_codex, detect, environment
from scripts.setup_support.state import SetupError

ROOT = Path(__file__).resolve().parents[2]
AGENTS = {"network-curator": "neko", "boolean-dynamics-modeler": "maboss",
          "multicellular-configurator": "physicell", "ode-modeler": "biomass"}


def env_block(text: str) -> dict:
    header = text.split("---\n", 2)[1]
    block = header.split("      env:\n", 1)[1]
    values = {}
    for line in block.splitlines():
        match = re.fullmatch(r"        (\w+): (.+)", line)
        if not match:
            break
        values[match[1]] = json.loads(match[2])
    return values


class ServerTmpdirTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.root = self.directory / "project"
        self.root.mkdir()
        for name in ("setup", ".codex", ".codex-plugin", ".claude/agents", ".claude/skills"):
            shutil.copytree(ROOT / name, self.root / name)
        self.prefix = self.root / ".setup/environment"
        self.cache = self.directory / "xdg cache"
        self.enterContext(mock.patch.dict(os.environ, {"XDG_CACHE_HOME": str(self.cache)}))

    def expected(self, server: str) -> Path:
        return self.cache / "mcp-biomodelling/tmp" / server

    def test_cache_root_follows_xdg_or_home(self):
        self.assertEqual(environment.server_tmpdir("maboss"), self.expected("maboss"))
        home = self.directory / "home"
        with mock.patch.dict(os.environ, {"HOME": str(home)}):
            os.environ.pop("XDG_CACHE_HOME")
            self.assertEqual(environment.server_tmpdir("maboss"), home / ".cache/mcp-biomodelling/tmp/maboss")
            # A relative XDG_CACHE_HOME is invalid per the XDG specification.
            os.environ["XDG_CACHE_HOME"] = "relative/cache"
            self.assertEqual(environment.server_tmpdir("maboss"), home / ".cache/mcp-biomodelling/tmp/maboss")

    def test_claude_transports_render_a_per_server_tmpdir_after_the_existing_env(self):
        for plugin in (None, {"uvx": "/tools/uv/uvx", "manifest": tomllib.loads((ROOT / "setup/dependencies.toml").read_text())}):
            with self.subTest(plugin=bool(plugin)):
                rendered = configure_claude.render(self.root, None if plugin else self.prefix,
                                                   with_biomass=True, plugin=plugin)
                for role, server in AGENTS.items():
                    env = env_block(rendered[self.root / ".claude/agents" / f"{role}.md"])
                    self.assertEqual(env["TMPDIR"], str(self.expected(server)))
                    keys = list(env)
                    # PATH (and CONDA_PREFIX) keep their place; TMPDIR follows them.
                    self.assertEqual(keys.index("TMPDIR"), keys.index("PATH") + 1)
                    self.assertTrue(Path(env["TMPDIR"]).is_absolute())

    def test_codex_profiles_render_the_same_tmpdir(self):
        outputs = configure_codex.render(self.root, self.prefix, self.directory / "codex", with_biomass=True)
        found = {}
        for path, text in outputs.items():
            for server, config in tomllib.loads(text).get("mcp_servers", {}).items():
                if config.get("enabled") and "env" in config:
                    found[server] = config["env"].get("TMPDIR")
        self.assertEqual(found, {server: str(self.expected(server)) for server in AGENTS.values()})

    def execute(self, flags):
        args = setup.parser().parse_args(flags)
        with mock.patch.object(detect, "discover", side_effect=lambda name, override=None: None if name == "dot" else "/tools/executable"), \
             mock.patch.object(detect, "client_version", side_effect=lambda name, path: "3.12.0" if name == "python" else "2.0.0"), \
             mock.patch.object(environment, "install"), \
             mock.patch.object(environment, "verify_environment", return_value={"passed": True}), \
             mock.patch.object(setup.verify, "verify", return_value={"passed": True, "errors": []}):
            return setup.execute(args, self.root)

    def test_setup_creates_private_tmpdirs_and_check_accepts_them(self):
        report = self.execute(["--client", "claude", "--manager", "venv", "--non-interactive"])
        self.assertTrue(report["passed"], report["errors"])
        for server in ("neko", "maboss", "physicell"):
            directory = self.expected(server)
            self.assertTrue(directory.is_dir())
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
        self.assertFalse(self.expected("biomass").exists())
        check = self.execute(["--client", "claude", "--manager", "venv", "--check"])
        self.assertTrue(check["passed"], check["errors"])

    def test_setup_tightens_an_existing_tmpdir_and_refuses_a_symlink(self):
        directory = self.expected("maboss")
        directory.mkdir(parents=True, mode=0o755)
        os.chmod(directory, 0o755)
        self.assertTrue(self.execute(["--client", "claude", "--manager", "venv", "--non-interactive"])["passed"])
        self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
        shutil.rmtree(directory)
        directory.symlink_to(self.directory)
        with self.assertRaisesRegex(SetupError, "symlink"):
            self.execute(["--client", "claude", "--manager", "venv", "--non-interactive"])

    def test_setup_refuses_a_symlinked_cache_component(self):
        elsewhere = self.directory / "elsewhere"
        elsewhere.mkdir()
        self.cache.mkdir()
        (self.cache / "mcp-biomodelling").symlink_to(elsewhere)
        with self.assertRaisesRegex(SetupError, "symlink"):
            self.execute(["--client", "claude", "--manager", "venv", "--non-interactive"])
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_check_reports_missing_and_mismatched_tmpdir(self):
        self.assertTrue(self.execute(["--client", "claude", "--manager", "venv", "--non-interactive"])["passed"])
        agent = self.root / ".claude/agents/boolean-dynamics-modeler.md"
        text = agent.read_text()
        line = f"        TMPDIR: {json.dumps(str(self.expected('maboss')))}\n"
        self.assertIn(line, text)
        agent.write_text(text.replace(line, ""))
        report = self.execute(["--client", "claude", "--manager", "venv", "--check"])
        self.assertFalse(report["passed"])
        self.assertTrue(any("TMPDIR missing" in error and "boolean-dynamics-modeler.md" in error
                            for error in report["errors"]), report["errors"])
        # One error per file: the stale report carries the TMPDIR reason.
        self.assertEqual(len([e for e in report["errors"] if "boolean-dynamics-modeler.md" in e]), 1,
                         report["errors"])
        agent.write_text(text.replace(line, '        TMPDIR: "/tmp"\n'))
        report = self.execute(["--client", "claude", "--manager", "venv", "--check"])
        self.assertTrue(any("TMPDIR mismatch" in error and '"/tmp"' in error
                            for error in report["errors"]), report["errors"])
        agent.write_text(text)
        shutil.rmtree(self.expected("physicell"))
        report = self.execute(["--client", "claude", "--manager", "venv", "--check"])
        self.assertFalse(report["passed"])
        self.assertTrue(any("TMPDIR directory" in error and "physicell" in error
                            for error in report["errors"]), report["errors"])
        self.assertFalse(self.expected("physicell").exists())


if __name__ == "__main__":
    unittest.main()
