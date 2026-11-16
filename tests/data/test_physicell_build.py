"""scripts/physicell/build.py with fake curl/git/tar/make/g++/clang/python3 executables on a temp PATH.

No test touches the network or a real compiler."""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts.physicell import build
from scripts.physicell import settings_xml as sx

SESSION, CAPTURE = "mc-session-2", "cap-1"
CAPTURE_REL = f"runs/multicellular-configurator/{SESSION}/configs/{CAPTURE}"
XML = b"<PhysiCell_settings><cell_rules><rulesets><ruleset enabled=\"true\"><filename>rules.csv</filename></ruleset></rulesets></cell_rules></PhysiCell_settings>\n"
RULES = b"tumor,oxygen,decreases,cycle entry,0.1,0.5,4,0\n"
BND = b"node A { logic = B; }\n"
VERIFICATION = "runs/orchestrator/p1/physicell_verification.mc-session-2.cap-1.json"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def script(body: str) -> str:
    return f"#!{sys.executable}\nimport os, sys, pathlib\n{body}\n"


FAKES = {
    "curl": """
args = sys.argv[1:]
if os.environ.get("FAKE_CURL_FAIL"):
    print("curl: (6) could not resolve host"); sys.exit(6)
pathlib.Path(os.environ["FAKE_LOG"]).open("a").write("curl " + args[-1] + "\\n")
pathlib.Path(args[args.index("-o") + 1]).write_bytes(b"fake archive")
""",
    # the "archive" extracts to PhysiCell-<version>/ with a Makefile-less tree
    "tar": """
dest = pathlib.Path(sys.argv[sys.argv.index("-C") + 1])
(dest / os.environ.get("FAKE_TOP", "PhysiCell-1.14.2")).mkdir()
""",
    "make": """
args = sys.argv[1:]
pathlib.Path(os.environ["FAKE_LOG"]).open("a").write("make " + " ".join(args) + "\\n")
if args[:1] in (["template"], ["template_BM"]):
    cfg = pathlib.Path("config"); cfg.mkdir()
    (cfg / "PhysiCell_settings.xml").write_text("template xml")
    (cfg / "cell_rules.csv").write_text("template rules")
    print("template ready")
elif os.environ.get("FAKE_MAKE_FAIL"):
    print("\\n".join(f"compile line {i}" for i in range(100))); print("error: boom"); sys.exit(2)
else:
    pathlib.Path("project").write_text("binary")
    print("built")
""",
    "python3": "print('Python 3.12')",
    "git": "print('git version 2.40')",
    "g++": """
if "--version" in sys.argv:
    print("g++ (Fake) 13.2.0"); sys.exit(0)
out = sys.argv[sys.argv.index("-o") + 1]
assert "-fopenmp" in sys.argv
pathlib.Path(out).write_text("omp")
""",
    "clang++": """
if "--version" in sys.argv:
    print("Apple clang version 15.0.0"); sys.exit(0)
print("clang: error: unsupported option '-fopenmp'"); sys.exit(1)
""",
}


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.root = self.tmp / "repo"
        self.root.mkdir()
        self.dest = self.tmp / "dest"
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.log = self.tmp / "log.txt"
        self.log.write_text("")
        for name in ("curl", "tar", "make", "python3", "g++"):
            self.fake(name)
        self.env = {"PATH": str(self.bin), "FAKE_LOG": str(self.log)}

    def fake(self, name, body=None):
        path = self.bin / name
        path.write_text(script(FAKES[name] if body is None else body))
        path.chmod(path.stat().st_mode | stat.S_IXUSR)

    def build(self, template="template", **kwargs):
        return build.build(self.root, template, dest=self.dest, jobs=2, env=self.env, **kwargs)

    def report(self, folder):
        return json.loads((folder / "report.json").read_text(encoding="utf-8"))


class ToolchainTests(Base):
    def test_gpp_with_openmp_is_found(self):
        tool = build.toolchain(self.env)
        self.assertEqual((tool["compiler"], tool["openmp"], tool["fetch"]), ("g++", True, "curl"))
        self.assertIn("13.2.0", tool["compiler_version"])
        self.assertTrue(tool["make"].endswith("make"))
        self.assertTrue(tool["python3"].endswith("python3"))

    def test_physicell_cpp_comes_first_and_git_is_the_fallback_fetch(self):
        self.fake("clang++", FAKES["g++"])
        (self.bin / "curl").unlink()
        self.fake("git")
        tool = build.toolchain({**self.env, "PHYSICELL_CPP": "clang++"})
        self.assertEqual((tool["compiler"], tool["fetch"]), ("clang++", "git"))

    def test_versioned_gpp_is_tried_after_gpp(self):
        (self.bin / "g++").unlink()
        self.fake("g++-14", FAKES["g++"])
        self.assertEqual(build.toolchain(self.env)["compiler"], "g++-14")

    def test_apple_clang_only_gets_the_homebrew_remedy(self):
        (self.bin / "g++").unlink()
        self.fake("g++", FAKES["clang++"])  # on macOS g++ is Apple clang
        with self.assertRaises(ValueError) as caught:
            build.toolchain(self.env)
        message = str(caught.exception)
        self.assertIn("brew install gcc", message)
        self.assertIn("PHYSICELL_CPP=g++-NN", message)
        self.assertNotIn("\n", message)

    def test_no_compiler_is_refused(self):
        (self.bin / "g++").unlink()
        with self.assertRaisesRegex(ValueError, "no compiler"):
            build.toolchain(self.env)

    def test_missing_make_is_refused(self):
        (self.bin / "make").unlink()
        with self.assertRaisesRegex(ValueError, "make not found"):
            build.toolchain(self.env)
        with self.assertRaisesRegex(ValueError, "make not found"):
            self.build()
        self.assertFalse(self.dest.exists())

    def test_missing_fetch_tool_is_refused(self):
        (self.bin / "curl").unlink()
        with self.assertRaisesRegex(ValueError, "curl"):
            build.toolchain(self.env)

    def test_python3_only_needed_for_template_bm(self):
        (self.bin / "python3").unlink()
        build.toolchain(self.env, "template")
        with self.assertRaisesRegex(ValueError, "python3"):
            build.toolchain(self.env, "template_BM")


class BuildTests(Base):
    def test_successful_build_downloads_pinned_archive_and_reports(self):
        folder, report = self.build()
        project = self.dest / "PhysiCell-1.14.2-template"
        self.assertTrue((project / "project").is_file())
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["project_sha256"], sha(b"binary"))
        self.assertEqual(report["archive_sha256"], sha(b"fake archive"))
        self.assertEqual(report["run_command"], "./project config/PhysiCell_settings.xml")
        self.assertEqual(report["url"], "https://github.com/MathCancer/PhysiCell/archive/refs/tags/1.14.2.tar.gz")
        self.assertEqual(self.report(folder), report)
        self.assertEqual(folder.parent, self.root / "runs/orchestrator/physicell-build")
        self.assertTrue(folder.name.endswith("-template"))
        self.assertIn("./project config/PhysiCell_settings.xml", (folder / "report.md").read_text())
        self.assertEqual(self.log.read_text().splitlines(),
                         ["curl https://github.com/MathCancer/PhysiCell/archive/refs/tags/1.14.2.tar.gz",
                          "make template", "make -j 2"])
        self.assertEqual([p.name for p in self.dest.iterdir()], ["PhysiCell-1.14.2-template"])

    def test_template_bm_and_version(self):
        self.env["FAKE_TOP"] = "PhysiCell-1.15.0"
        folder, report = self.build("template_BM", version="1.15.0")
        self.assertTrue((self.dest / "PhysiCell-1.15.0-template_BM/project").is_file())
        self.assertIn("make template_BM", self.log.read_text())
        self.assertTrue(folder.name.endswith("-template_BM"))

    def test_existing_destination_is_refused_untouched(self):
        existing = self.dest / "PhysiCell-1.14.2-template"
        existing.mkdir(parents=True)
        (existing / "keep.txt").write_text("mine")
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.build()
        self.assertEqual((existing / "keep.txt").read_text(), "mine")
        self.assertEqual(self.log.read_text(), "")
        self.assertFalse((self.root / "runs").exists())

    def test_make_failure_writes_report_with_tail_and_keeps_folder(self):
        self.env["FAKE_MAKE_FAIL"] = "1"
        folder, report = self.build()
        self.assertEqual((report["status"], report["failed_step"]), ("failed", "make"))
        lines = report["failure_tail"].splitlines()
        self.assertEqual(len(lines), 40)
        self.assertEqual(lines[-1], "error: boom")
        self.assertTrue((self.dest / "PhysiCell-1.14.2-template").is_dir())
        self.assertIsNone(report["project_sha256"])
        self.assertIn("error: boom", (folder / "report.md").read_text())

    def test_download_failure_reports_and_leaves_no_folder(self):
        self.env["FAKE_CURL_FAIL"] = "1"
        folder, report = self.build()
        self.assertEqual((report["status"], report["failed_step"]), ("failed", "download"))
        self.assertIn("could not resolve host", report["failure_tail"])
        self.assertEqual(list(self.dest.iterdir()), [])

    def test_missing_project_after_make_fails(self):
        self.fake("make", FAKES["make"].replace('pathlib.Path("project").write_text("binary")', "pass"))
        _, report = self.build()
        self.assertEqual(report["failed_step"], "make")
        self.assertIn("project", report["error"])

    def test_main_exit_codes(self):
        argv = ["--template", "template", "--dest", str(self.dest), "--jobs", "1"]
        with mock.patch.dict(os.environ, self.env, clear=True), \
                mock.patch.object(build, "project_root", return_value=self.root):
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(build.main(argv), 0)
            self.assertIn("physicell build ok", out.getvalue())
            with contextlib.redirect_stderr(io.StringIO()) as err:
                self.assertEqual(build.main(argv), 1)  # destination now exists
            self.assertIn("already exists", err.getvalue())

    def test_run_command_is_never_executed(self):
        real, calls = subprocess.run, []

        def spy(argv, *args, **kwargs):
            calls.append(list(argv))
            return real(argv, *args, **kwargs)

        with mock.patch.object(build.subprocess, "run", side_effect=spy):
            self.build()
        self.assertTrue(calls)
        for argv in calls:
            self.assertNotIn(os.path.basename(argv[0]), ("project", "./project"))
            self.assertNotIn(argv[0], ("./project", "project"))


class SettingsCopyTests(Base):
    def setUp(self):
        super().setUp()
        self.folder = self.root / CAPTURE_REL
        (self.folder / "package/config").mkdir(parents=True)
        config = self.folder / "package/config"
        (config / "PhysiCell_settings.xml").write_bytes(XML)
        (config / "rules.csv").write_bytes(RULES)
        (config / "model.bnd").write_bytes(BND)
        (self.folder / "original.xml").write_bytes(b"<original/>")
        (self.folder / "capture.json").write_text(json.dumps({
            "schema_version": 1, "session_id": SESSION, "capture_id": CAPTURE,
            "original_xml_sha256": sha(b"<original/>"), "package_xml_sha256": sha(XML),
            "dependencies": [{"path": "package/config/rules.csv", "sha256": sha(RULES)},
                             {"path": "package/config/model.bnd", "sha256": sha(BND)}]}))
        self.after = sx.capture(self.root, CAPTURE_REL)
        self.verification_path = self.root / VERIFICATION
        self.write_verification()

    def write_verification(self, **overrides):
        data = {"kind": "physicell_verification", "schema_version": 1, "projection": {}, "before": {},
                "after": self.after, "entries": [], "unexpected_changes": [], "passed": True}
        data.update(overrides)
        self.verification_path.parent.mkdir(parents=True, exist_ok=True)
        self.verification_path.write_text(json.dumps(data), encoding="utf-8")

    def test_verified_capture_copies_xml_rules_and_bnd_with_backups(self):
        folder, report = self.build(verification=VERIFICATION)
        config = self.dest / "PhysiCell-1.14.2-template/config"
        self.assertEqual((config / "PhysiCell_settings.xml").read_bytes(), XML)
        self.assertEqual((config / "rules.csv").read_bytes(), RULES)
        self.assertEqual((config / "model.bnd").read_bytes(), BND)
        self.assertEqual((config / "PhysiCell_settings.xml.template-backup").read_text(), "template xml")
        self.assertEqual((config / "cell_rules.csv").read_text(), "template rules")  # not replaced, no backup
        self.assertFalse((config / "rules.csv.template-backup").exists())
        settings = report["settings"]
        self.assertEqual(settings["verification"], VERIFICATION)
        self.assertEqual(settings["verification_sha256"], sha(self.verification_path.read_bytes()))
        files = {f["name"]: f for f in settings["files"]}
        self.assertEqual(set(files), {"PhysiCell_settings.xml", "rules.csv", "model.bnd"})
        self.assertEqual(files["rules.csv"]["sha256"], sha(RULES))
        self.assertEqual(files["model.bnd"]["sha256"], sha(BND))
        self.assertEqual(files["PhysiCell_settings.xml"]["backup"], "config/PhysiCell_settings.xml.template-backup")
        self.assertIsNone(files["model.bnd"]["backup"])
        self.assertIn("rules.csv", (folder / "report.md").read_text())

    def refused(self, pattern):
        with self.assertRaisesRegex(ValueError, pattern):
            self.build(verification=VERIFICATION)
        self.assertFalse(self.dest.exists())  # nothing was downloaded
        self.assertEqual(self.log.read_text(), "")

    def test_missing_verification_is_refused(self):
        self.verification_path.unlink()
        self.refused("unverified")

    def test_failed_verification_is_refused(self):
        self.write_verification(passed=False)
        self.refused("did not pass")

    def test_wrong_kind_or_schema_is_refused(self):
        self.write_verification(kind="other")
        self.refused("physicell_verification")
        self.write_verification(schema_version=2)
        self.refused("schema_version")

    def test_verification_outside_runs_orchestrator_is_refused(self):
        other = self.root / "elsewhere.json"
        other.write_bytes(self.verification_path.read_bytes())
        with self.assertRaisesRegex(ValueError, "runs/orchestrator"):
            self.build(verification="elsewhere.json")

    def test_changed_capture_bytes_are_refused(self):
        (self.folder / "package/config/rules.csv").write_bytes(RULES + b"x\n")
        self.refused("changed since capture")

    def test_binding_mismatch_is_refused(self):
        self.write_verification(after={**self.after, "xml_sha256": "0" * 64})
        self.refused("no longer matches")

    def test_existing_backup_is_refused_and_report_names_settings(self):
        # a build that finds an existing backup: pre-seed through a make that creates one
        self.fake("make", FAKES["make"].replace(
            '(cfg / "cell_rules.csv")', '(cfg / "PhysiCell_settings.xml.template-backup").write_text("old"); (cfg / "cell_rules.csv")'))
        _, report = self.build(verification=VERIFICATION)
        self.assertEqual((report["status"], report["failed_step"]), ("failed", "settings"))
        self.assertIn("already exists", report["error"])
        config = self.dest / "PhysiCell-1.14.2-template/config"
        self.assertEqual((config / "PhysiCell_settings.xml").read_text(), "template xml")


if __name__ == "__main__":
    unittest.main()
