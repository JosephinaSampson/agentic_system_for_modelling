#!/usr/bin/env python3
"""Clean orphaned MaBoSS server temp directories (``maboss-run-*``).

Usage: server_tmp.py clean [--dry-run] [--min-age SECONDS] | server_tmp.py status

The MaBoSS MCP server writes one ``maboss-run-*`` directory per simulation (engine
inputs and a 100-300 MB probability trajectory) through ``tempfile`` and copies the
results into its own artifact store, but leaves the directory behind. Inline servers
live only as long as one specialist run, so these directories are garbage once no
process uses them. Roots are the TMPDIR that setup renders for the MaBoSS server
(``<cache>/mcp-biomodelling/tmp/maboss``) and the system ``/tmp``.

A directory is removed only if it is a real directory (never a symlink) named
``maboss-run-*`` directly under a root, owned by the current user, not in use (no
process has an open file or working directory inside it or names it on its command
line), its newest entry is older than ``--min-age`` (default 600 s), and either it was
last modified before the earliest start of any running MaBoSS server (a process with
an argv element ``mcp-maboss-server`` or ``.../mcp-maboss-server``) or it is older than
24 hours. Nothing else is ever deleted. ``status`` reports each root's free
space and the total size of the candidates. A root with less than 2 GiB free is warned
about; that is never a failure.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import stat
import sys
import time
from pathlib import Path
from typing import NamedTuple

if __package__ in (None, ""):  # run as a script: make the repo importable
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.setup_support.environment import ensure_private_tmpdir, server_tmpdir

PREFIX = "maboss-run-"
SERVER_MARKER = "mcp-maboss-server"
SYSTEM_TMP = Path("/tmp")
MIN_AGE_SECONDS = 600
SERVER_GRACE_SECONDS = 24 * 3600
LOW_SPACE_BYTES = 2 * 1024 ** 3
MIB = 1024 ** 2


def default_roots() -> list[Path]:
    """The rendered MaBoSS TMPDIR plus the system temp dir (older runs used it)."""
    return [server_tmpdir("maboss"), SYSTEM_TMP]


def candidates(roots) -> list[Path]:
    """Owned real ``maboss-run-*`` directories directly under each root."""
    found, seen, uid = [], set(), os.getuid()
    for root in roots:
        try:
            entries = sorted(os.scandir(root), key=lambda entry: entry.name)
        except OSError:
            continue
        for entry in entries:
            try:
                if (not entry.name.startswith(PREFIX) or entry.is_symlink()
                        or not entry.is_dir(follow_symlinks=False)
                        or entry.stat(follow_symlinks=False).st_uid != uid):
                    continue
            except OSError:
                continue
            path = Path(entry.path)
            key = os.path.realpath(path)
            if key not in seen:
                seen.add(key)
                found.append(path)
    return found


class Process(NamedTuple):
    pid: int
    argv: tuple
    start: float | None  # epoch seconds, None when unreadable
    paths: tuple  # open-file and working-directory targets


def _boot_time() -> float | None:
    try:
        for line in Path("/proc/stat").read_text().splitlines():
            if line.startswith("btime "):
                return float(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return None


def _stat_start(raw: str, *, boot: float, ticks: int) -> float:
    """Start time from /proc/<pid>/stat (field 22, clock ticks after boot)."""
    fields = raw[raw.rindex(")") + 2:].split()
    return boot + int(fields[19]) / ticks  # fields[0] is field 3 (state)


def _process_table() -> list[Process]:
    """argv, start time and open paths of every visible process; unreadable parts are skipped."""
    table = []
    try:
        pids = [name for name in os.listdir("/proc") if name.isdigit()]
    except OSError:
        return table
    boot = _boot_time()
    try:
        ticks = os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError):
        ticks = 100
    for pid in pids:
        base = f"/proc/{pid}"
        try:
            argv = tuple(part.decode(errors="replace")
                         for part in Path(f"{base}/cmdline").read_bytes().split(b"\0") if part)
        except OSError:
            continue  # the process exited
        start = None
        if boot is not None:
            try:
                start = _stat_start(Path(f"{base}/stat").read_text(), boot=boot, ticks=ticks)
            except (OSError, ValueError, IndexError):
                pass
        paths = []
        try:
            paths.append(os.readlink(f"{base}/cwd"))
        except OSError:
            pass
        try:
            descriptors = os.listdir(f"{base}/fd")
        except OSError:
            descriptors = []
        for fd in descriptors:
            try:
                paths.append(os.readlink(f"{base}/fd/{fd}"))
            except OSError:
                pass
        table.append(Process(int(pid), argv, start, tuple(paths)))
    return table


def _names(text: str, target: str) -> bool:
    """True if ``text`` contains ``target`` as a whole path or a path prefix."""
    return re.search(re.escape(target) + r"(?![\w.-])", text) is not None


def in_use(path, table=None) -> bool:
    """True if any process has an open fd or cwd inside ``path`` or names it on its argv."""
    targets = {os.path.realpath(path), os.path.abspath(path)}
    for process in _process_table() if table is None else table:
        for item in process.paths:
            if item.endswith(" (deleted)"):
                item = item[: -len(" (deleted)")]
            if any(item == target or item.startswith(target + os.sep) for target in targets):
                return True
        if any(_names(argument, target) for argument in process.argv for target in targets):
            return True
    return False


def is_maboss_server(argv) -> bool:
    """A genuine server launch: an argv element that is the server executable itself.

    Shell command lines that merely mention the name (``sh -c "pgrep -f ..."``) do not count.
    """
    return any(not any(ch.isspace() for ch in argument)
               and (argument == SERVER_MARKER or argument.endswith("/" + SERVER_MARKER))
               for argument in argv)


def servers_running(table=None) -> bool:
    """True if any process is a MaBoSS MCP server."""
    return any(is_maboss_server(p.argv) for p in (_process_table() if table is None else table))


def earliest_server_start(table) -> float | None:
    """The earliest start of a running MaBoSS server (0 if one's start is unreadable), or None."""
    starts = [p.start if p.start is not None else 0.0 for p in table if is_maboss_server(p.argv)]
    return min(starts) if starts else None


def _scan(path: Path) -> tuple[float, int]:
    """Newest mtime and total file bytes inside ``path``, never following symlinks."""
    newest, size = os.lstat(path).st_mtime, 0
    for directory, dirnames, filenames in os.walk(path, followlinks=False):
        for name in dirnames + filenames:
            try:
                info = os.lstat(os.path.join(directory, name))
            except OSError:
                continue
            newest = max(newest, info.st_mtime)
            if stat.S_ISREG(info.st_mode):
                size += info.st_size
    return newest, size


def space_warnings(roots) -> list[str]:
    warnings = []
    for root in roots:
        try:
            free = shutil.disk_usage(root).free
        except OSError:
            continue
        if free < LOW_SPACE_BYTES:
            warnings.append(f"WARNING: {root} has {free // MIB} MiB free")
    return warnings


def clean(roots, *, min_age_seconds: float = MIN_AGE_SECONDS, dry_run: bool = False,
          now: float | None = None) -> dict:
    """Remove orphaned run directories; returns removed count/bytes and skip reasons."""
    roots = list(roots)
    now = time.time() if now is None else now
    found = candidates(roots)
    table = _process_table() if found else []
    server_start = earliest_server_start(table)
    summary = {"dry_run": dry_run, "removed": 0, "removed_bytes": 0, "skipped": [], "vanished": [],
               "errors": []}
    for path in found:
        try:
            newest, size = _scan(path)
        except FileNotFoundError:
            summary["vanished"].append(str(path))  # removed by someone else meanwhile
            continue
        except OSError as exc:
            summary["errors"].append({"path": str(path), "reason": str(exc)})
            continue
        age = now - newest
        reason = None
        if in_use(path, table):
            reason = "in use by a running process (open file, cwd or argv)"
        elif age < min_age_seconds:
            reason = f"younger than {int(min_age_seconds)} s (newest entry {int(age)} s old)"
        elif server_start is not None and newest >= server_start and age < SERVER_GRACE_SECONDS:
            reason = f"modified after a running MaBoSS server started ({int(age)} s old, under 24 h)"
        if reason:
            summary["skipped"].append({"path": str(path), "reason": reason})
            continue
        if not dry_run:
            try:
                if path.is_symlink() or not path.is_dir():
                    if not path.exists() and not path.is_symlink():
                        summary["vanished"].append(str(path))
                    continue  # changed since the scan; never follow it
                shutil.rmtree(path)
            except FileNotFoundError:
                summary["vanished"].append(str(path))
                continue
            except OSError as exc:
                summary["errors"].append({"path": str(path), "reason": str(exc)})
                continue
        summary["removed"] += 1
        summary["removed_bytes"] += size
    summary["warnings"] = space_warnings(roots)
    return summary


def ensure_server_tmpdir() -> str | None:
    """Recreate the MaBoSS server's TMPDIR if missing; returns a warning when it was."""
    directory = server_tmpdir("maboss")
    if ensure_private_tmpdir(directory):
        return (f"{directory} was missing (recreated, mode 0700); the MaBoSS server fell back to /tmp "
                "for this run")
    return None


def summary_line(summary: dict) -> str:
    verb = "would remove" if summary["dry_run"] else "removed"
    label = "TMP CLEANUP (dry run)" if summary["dry_run"] else "TMP CLEANUP"
    line = (f"{label}: {verb} {summary['removed']} maboss-run dirs "
            f"({summary['removed_bytes'] / MIB:.1f} MiB), skipped {len(summary['skipped'])}")
    if summary.get("vanished"):
        line += f", vanished {len(summary['vanished'])}"
    if summary.get("errors"):
        line += f", errors {len(summary['errors'])}"
    return line


def detail_lines(summary: dict) -> list[str]:
    lines = [f"skipped: {row['path']}: {row['reason']}" for row in summary["skipped"]]
    lines += [f"error: {row['path']}: {row['reason']}" for row in summary.get("errors", [])]
    return lines + list(summary.get("warnings", []))


def status(roots) -> list[str]:
    lines, total_count, total_bytes = [], 0, 0
    for root in roots:
        try:
            free = f"{shutil.disk_usage(root).free // MIB} MiB free"
        except OSError:
            free = "missing"
        found = candidates([root])
        size = 0
        for path in found:
            try:
                size += _scan(path)[1]
            except OSError:
                pass
        total_count, total_bytes = total_count + len(found), total_bytes + size
        lines.append(f"{root}: {free}; {len(found)} maboss-run dirs, {size / MIB:.1f} MiB")
    lines.append(f"total: {total_count} maboss-run dirs, {total_bytes / MIB:.1f} MiB")
    return lines + space_warnings(roots)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    cleaning = commands.add_parser("clean", help="remove orphaned maboss-run-* directories")
    cleaning.add_argument("--dry-run", action="store_true")
    cleaning.add_argument("--min-age", type=float, default=MIN_AGE_SECONDS, metavar="SECONDS")
    commands.add_parser("status", help="free space per root and size of the candidates")
    args = parser.parse_args(argv)
    roots = default_roots()
    if args.command == "status":
        print("\n".join(status(roots)))
        return 0
    if args.min_age < 0:
        parser.error("--min-age must be nonnegative")
    summary = clean(roots, min_age_seconds=args.min_age, dry_run=args.dry_run)
    print("\n".join([summary_line(summary), *detail_lines(summary)]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
