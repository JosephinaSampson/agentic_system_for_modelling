"""scripts/physicell/build.py with fake curl/git/tar/make/g++/clang/python3 executables on a temp PATH.

No test touches the network or a real compiler."""
from __future__ import annotations

import contextlib
import hashlib
import copy
import io
import json
import os
import stat
import tarfile
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
import shutil
shutil.copy(os.environ["FAKE_ARCHIVE"], args[args.index("-o") + 1])
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
        for name in ("curl", "make", "python3", "g++"):
            self.fake(name)
        self.archive_path = self.tmp / "archive.tar.gz"
        self.env = {"PATH": str(self.bin), "FAKE_LOG": str(self.log), "FAKE_ARCHIVE": str(self.archive_path)}
        self.archive()

    def archive(self, top="PhysiCell-1.14.2", extra=()):
        """Write the fake release archive: <top>/README plus `extra` TarInfo members (info, data)."""
        with tarfile.open(self.archive_path, "w:gz") as tf:
            info = tarfile.TarInfo(f"{top}/README")
            data = b"physicell"
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
            for member, payload in extra:
                tf.addfile(member, io.BytesIO(payload) if payload is not None else None)

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
        self.assertEqual(report["archive_sha256"], sha(self.archive_path.read_bytes()))
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

    def test_git_fallback_records_the_commit(self):
        # review M5: without curl, the clone's `git rev-parse HEAD` is recorded
        (self.bin / "curl").unlink()
        self.fake("git", """
args = sys.argv[1:]
pathlib.Path(os.environ["FAKE_LOG"]).open("a").write("git " + " ".join(args) + "\\n")
if args[:1] == ["clone"]:
    target = pathlib.Path(args[-1]); target.mkdir(parents=True); (target / "README").write_text("physicell")
elif args[-2:] == ["rev-parse", "HEAD"]:
    print("0123456789abcdef0123456789abcdef01234567")
else:
    print("git version 2.40")
""")
        folder, report = self.build()
        self.assertEqual(report["status"], "ok", report)
        self.assertIsNone(report["archive_sha256"])
        self.assertEqual(report["git_commit"], "0123456789abcdef0123456789abcdef01234567")
        self.assertIn("0123456789abcdef0123456789abcdef01234567", (folder / "report.md").read_text())

    def test_curl_download_records_no_commit(self):
        _, report = self.build()
        self.assertIsNone(report["git_commit"])

    def test_template_bm_and_version(self):
        self.archive("PhysiCell-1.15.0")
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

    def test_zero_or_negative_jobs_refused(self):
        # PR #17 review 4586112420: --jobs 0 is refused, never silently os.cpu_count()
        for jobs in (0, -1):
            with self.subTest(jobs=jobs), self.assertRaisesRegex(ValueError, "--jobs must be at least 1"):
                build.build(self.root, "template", dest=self.dest, jobs=jobs, env=self.env)
        self.assertFalse(self.dest.exists())
        err = io.StringIO()
        with contextlib.redirect_stderr(err), mock.patch.object(build, "project_root", return_value=self.root):
            self.assertEqual(build.main(["--template", "template", "--dest", str(self.dest), "--jobs", "0"]), 1)
        self.assertIn("--jobs must be at least 1", err.getvalue())
        _, report = build.build(self.root, "template", dest=self.dest, jobs=None, env=self.env)
        self.assertEqual(report["jobs"], os.cpu_count() or 1)

    def test_extracted_tree_is_the_release(self):
        self.build()
        self.assertEqual((self.dest / "PhysiCell-1.14.2-template/README").read_text(), "physicell")

    def test_os_error_while_running_make_is_a_failed_report(self):
        real = subprocess.run

        def boom(argv, *args, **kwargs):
            if argv[0] == "make":
                raise PermissionError("denied")
            return real(argv, *args, **kwargs)

        with mock.patch.object(build.subprocess, "run", side_effect=boom):
            _, report = self.build()
        self.assertEqual((report["status"], report["failed_step"]), ("failed", "make template"))
        self.assertIn("denied", report["error"])

    def test_os_error_while_downloading_is_a_failed_report(self):
        real = subprocess.run

        def boom(argv, *args, **kwargs):
            if argv[0] == "curl":
                raise FileNotFoundError("curl vanished")
            return real(argv, *args, **kwargs)

        with mock.patch.object(build.subprocess, "run", side_effect=boom):
            _, report = self.build()
        self.assertEqual((report["status"], report["failed_step"]), ("failed", "download"))
        self.assertEqual([p.name for p in self.dest.iterdir()], [])

    def test_unwritable_destination_is_a_failed_report(self):
        blocker = self.tmp / "blocker"
        blocker.write_text("a file, not a folder")
        self.dest = blocker / "sub"
        _, report = self.build()
        self.assertEqual((report["status"], report["failed_step"]), ("failed", "download"))

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


class UnsafeArchiveTests(Base):
    def member(self, kind, name, linkname=""):
        info = tarfile.TarInfo(name)
        info.type = kind
        info.linkname = linkname
        return info

    def refuses(self, *members, escape=None):
        """`escape` is where the malicious archive would write if extracted unchecked."""
        self.archive(extra=[(m, None) for m in members])
        _, report = self.build()
        self.assertEqual((report["status"], report["failed_step"]), ("failed", "download"))
        self.assertIn("archive refused", report["error"])
        if escape is not None:
            self.assertFalse(os.path.lexists(escape), f"{escape} was written")
        self.assertEqual(list(self.dest.iterdir()), [])  # no folder, staging cleaned up
        self.assertEqual(self.log.read_text().count("make"), 0)

    def test_symlink_to_etc_is_refused(self):
        self.refuses(self.member(tarfile.SYMTYPE, "PhysiCell-1.14.2/link", "/etc"))

    def test_relative_symlink_escaping_is_refused(self):
        self.refuses(self.member(tarfile.SYMTYPE, "PhysiCell-1.14.2/link", "../../evil"),
                     escape=self.dest / "evil")

    def test_hardlink_outside_is_refused(self):
        self.refuses(self.member(tarfile.LNKTYPE, "PhysiCell-1.14.2/hard", "../../etc/passwd"),
                     escape=self.dest / "etc")

    def test_dotdot_member_is_refused(self):
        self.refuses(self.member(tarfile.REGTYPE, "../evil"), escape=self.dest / "evil")

    def test_absolute_member_is_refused(self):
        self.refuses(self.member(tarfile.REGTYPE, str(self.tmp / "evil")), escape=self.tmp / "evil")

    def test_symlink_chain_escaping_is_refused(self):
        top = "PhysiCell-1.14.2"
        self.refuses(self.member(tarfile.SYMTYPE, f"{top}/a", ".."),
                     self.member(tarfile.SYMTYPE, f"{top}/l", "a/../.."),
                     self.member(tarfile.REGTYPE, f"{top}/l/x"), escape=self.tmp / "x")

    def test_hardlink_with_dotdot_is_refused(self):
        self.refuses(self.member(tarfile.LNKTYPE, "PhysiCell-1.14.2/h", "PhysiCell-1.14.2/../README"))

    def test_symlink_with_dotdot_inside_the_folder_is_refused(self):
        self.refuses(self.member(tarfile.SYMTYPE, "PhysiCell-1.14.2/s", "sub/../README"))

    def test_python_without_extraction_filters_is_refused(self):
        fake = mock.Mock(spec=tarfile)
        fake.open = tarfile.open
        fake.TarError = tarfile.TarError
        del fake.data_filter
        with mock.patch.object(build, "tarfile", fake):
            _, report = self.build()
        self.assertEqual((report["status"], report["failed_step"]), ("failed", "download"))
        self.assertIn("needs Python >=3.12, or 3.8.17+/3.9.17+/3.10.12+/3.11.4+ with tarfile extraction filters",
                      report["error"])
        self.assertEqual(list(self.dest.iterdir()), [])

    def test_special_members_are_refused(self):
        self.refuses(self.member(tarfile.FIFOTYPE, "PhysiCell-1.14.2/fifo"))
        self.refuses(self.member(tarfile.CHRTYPE, "PhysiCell-1.14.2/dev"))

    def test_safe_internal_symlink_is_accepted(self):
        self.archive(extra=[(self.member(tarfile.SYMTYPE, "PhysiCell-1.14.2/alias", "README"), None)])
        _, report = self.build()
        self.assertEqual(report["status"], "ok")


class SettingsCopyTests(Base):
    """The copy of a verified capture. The verification recomputation (ruling T9-d) is patched to agree with the file
    here; test_physicell_verify.BuildHandoffTests runs it for real (forged and stale files refused)."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(build.ve, "compute", side_effect=self.recompute)
        self.recomputed = patcher.start()
        self.addCleanup(patcher.stop)
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

    def recompute(self, root, projection, after, after_binding):
        self.assertEqual((projection, after), ("runs/orchestrator/p1/physicell_projection.json", CAPTURE_REL))
        self.assertEqual(after_binding[0], sx.capture(root, CAPTURE_REL))  # _verification's own binding is passed
        return json.loads(self.verification_path.read_text(encoding="utf-8"))

    def write_verification(self, **overrides):
        data = {"kind": "physicell_verification", "schema_version": 1,
                "projection": {"path": "runs/orchestrator/p1/physicell_projection.json", "sha256": "0" * 64},
                "before": {},
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

    def rewrite_manifest(self, edit):
        path = self.folder / "capture.json"
        manifest = json.loads(path.read_text())
        edit(manifest)
        path.write_text(json.dumps(manifest))

    def bound_manifest(self, edit):
        """self.after with its bound (verified) manifest edited: what _package_files copies from."""
        manifest = copy.deepcopy(self.after["manifest"])
        edit(manifest)
        return {**self.after, "manifest": manifest}

    def test_manifest_without_dependencies_is_a_failed_settings_report(self):
        # sx.capture refuses such a manifest, so _verification never binds one; the copy refuses it too
        after = self.bound_manifest(lambda m: m.pop("dependencies"))
        with self.assertRaisesRegex(ValueError, "no dependencies"):
            build._package_files(self.root, after)
        with mock.patch.object(build, "_verification", return_value=(VERIFICATION, {"after": after}, b"{}", sx.capture_tree(self.root, CAPTURE_REL)[1])):
            _, report = self.build(verification=VERIFICATION)
        self.assertEqual((report["status"], report["failed_step"]), ("failed", "settings"))
        self.assertIn("no dependencies", report["error"])

    def tamper_during_make_template(self, tamper):
        """Run `tamper` after _verification bound the capture, while `make template` runs (before the copy)."""
        original = build._make

        def make(step, *args, **kwargs):
            if step == "make template":
                tamper()
            return original(step, *args, **kwargs)
        return mock.patch.object(build, "_make", side_effect=make)

    def test_capture_json_changed_after_verification_is_refused(self):
        # PR #17 review 4586111204: the copy takes the dependency hashes _verification bound, never a re-read
        def swap_rules():
            (self.folder / "package/config/rules.csv").write_bytes(RULES + b"x\n")
            self.rewrite_manifest(lambda m: m["dependencies"][0].update(sha256=sha(RULES + b"x\n")))
        with self.tamper_during_make_template(swap_rules):
            _, report = self.build(verification=VERIFICATION)
        self.assertEqual((report["status"], report["failed_step"]), ("failed", "settings"), report)
        self.assertIn("rules.csv changed while being copied", report["error"])
        self.assertFalse((self.dest / "PhysiCell-1.14.2-template/config/rules.csv").exists())

    def test_dependency_added_to_capture_json_after_verification_is_not_copied(self):
        def add_dependency():
            (self.folder / "package/config/extra.csv").write_bytes(b"extra\n")
            self.rewrite_manifest(lambda m: m["dependencies"].append(
                {"path": "package/config/extra.csv", "sha256": sha(b"extra\n")}))
        with self.tamper_during_make_template(add_dependency):
            _, report = self.build(verification=VERIFICATION)
        self.assertEqual(report["status"], "ok", report)
        self.assertEqual({f["name"] for f in report["settings"]["files"]},
                         {"PhysiCell_settings.xml", "rules.csv", "model.bnd"})
        self.assertFalse((self.dest / "PhysiCell-1.14.2-template/config/extra.csv").exists())

    def test_dependency_basename_collision_is_refused_naming_both_paths(self):
        after = self.bound_manifest(lambda m: m["dependencies"].append(
            {"path": "package/config/sub/rules.csv", "sha256": sha(RULES)}))
        with mock.patch.object(build, "_verification", return_value=(VERIFICATION, {"after": after}, b"{}", sx.capture_tree(self.root, CAPTURE_REL)[1])):
            _, report = self.build(verification=VERIFICATION)
        self.assertEqual(report["failed_step"], "settings")
        self.assertIn("package/config/rules.csv", report["error"])
        self.assertIn("package/config/sub/rules.csv", report["error"])
        config = self.dest / "PhysiCell-1.14.2-template/config"
        self.assertFalse((config / "rules.csv").exists())

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

    def test_recomputation_must_pass_and_agree(self):
        self.recomputed.side_effect = None
        self.recomputed.return_value = {**json.loads(self.verification_path.read_text()), "passed": False}
        self.refused("does not pass")
        self.recomputed.return_value = {**json.loads(self.verification_path.read_text()),
                                        "unexpected_changes": [{"kind": "xml"}]}
        self.refused("unexpected_changes differ from a re-verification")
        self.recomputed.side_effect = ValueError("projection p fails its check: x")
        self.refused("cannot be re-verified: projection p fails its check")

    def maboss_capture(self):
        """Rewrite the verified capture's XML to carry a MaBoSS intracellular model (re-hashed, re-verified)."""
        xml = XML.replace(b"<cell_rules>", b"<cell_definitions><cell_definition name='tumor'><phenotype>"
                          b"<intracellular type=\"maboss\" /></phenotype></cell_definition></cell_definitions>"
                          b"<cell_rules>")
        (self.folder / "package/config/PhysiCell_settings.xml").write_bytes(xml)
        self.rewrite_manifest(lambda m: m.update(package_xml_sha256=sha(xml)))
        self.after = sx.capture(self.root, CAPTURE_REL)
        self.write_verification()

    def test_maboss_settings_need_template_bm(self):
        # review M5: a PhysiBoSS configuration cannot run in the plain template project
        self.maboss_capture()
        with self.assertRaisesRegex(ValueError, "intracellular type=\"maboss\".*template_BM"):
            self.build("template", verification=VERIFICATION)
        self.assertFalse(self.dest.exists())
        _, report = self.build("template_BM", verification=VERIFICATION)
        self.assertEqual(report["status"], "ok", report)

    def test_binding_mismatch_is_refused(self):
        self.write_verification(after={**self.after, "xml_sha256": "0" * 64})
        self.refused("no longer matches.*regenerate the verification with verify_export.py")

    def test_verification_written_before_manifest_sha256_is_refused(self):
        # PR #17 review 4587427279: the after binding gained manifest_sha256; an older file must be regenerated
        old = {k: v for k, v in self.after.items() if k != "manifest_sha256"}
        self.write_verification(after=old)
        self.refused("without manifest_sha256.*regenerate the verification with verify_export.py")

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
