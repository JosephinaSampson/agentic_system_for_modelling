"""Copy a specialist invocation's server exports out of the transient MCP artifact store.

Each specialist's inline MCP server lives only as long as its agent run, and some
exports are overwritten in place (NeKo always writes ``<session>/Network.sif``).
After a run, ``capture_exports`` scans that invocation's transcript for successful
results of the specialist's own export tools, reads the exported file paths
(structured ``file``-style fields first; NeKo's ``SIF/History/BNET exported: <path>``
text prefix only when no structured field is present), and copies each file that
lies inside ``<artifact root>/<Server>/<session>/`` (no symlinks) to
``runs/<specialist>/<session>/exports/<invocation_id>/<file name>`` with an
``exports.json`` manifest. Anything else is refused and reported, never copied.
No export results -> no ``exports/`` directory. A rerun never amends a capture: it
re-verifies the saved copies (a no-op when unchanged), fails closed when a saved copy
or a recopied source differs, and only reports new refusals or vanished sources.
MaBoSS ``run_simulation``/``simulate_mutation`` results are captured through their
``run_manifest``: the manifest plus its ``node_marginals`` and ``engine_input`` files, each
checked against the manifest's sha256; larger state/engine dumps stay in the server store. ode-modeler (record_ode_artifacts.py)
and data-analyst (no MCP exports) are skipped.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

from scripts.capture.artifact_capture import reject_symlinks, staged_capture
from scripts.codex.launcher_config import validate_session_id

# specialist -> (server artifact directory, MCP tool-name prefix, {tool: ((field, format), ...)})
# A format of None means "use the result's own `format` field" (NeKo export_network).
EXPORT_TOOLS = {
    'network-curator': ('NeKo', 'neko', {
        'export_network': (('file', None), ('curation_file', 'curation')),
    }),
    'boolean-dynamics-modeler': ('MaBoSS', 'maboss', {
        'export_maboss_bnd_cfg': (('bnd_file', 'bnd'), ('cfg_file', 'cfg')),
        'export_maboss_handoff': (('manifest_file', 'handoff-manifest'),),
        'run_simulation': (('run_manifest', 'maboss-run-manifest'),),
        'simulate_mutation': (('run_manifest', 'maboss-run-manifest'),),
    }),
    'multicellular-configurator': ('PhysiCell', 'physicell', {
        'export_xml_configuration': (('file', 'xml'),),
        'export_cell_rules_csv': (('file', 'csv'),),
        'export_configuration_bundle': (('bundle', 'bundle'),),
    }),
}
NEKO_TEXT = re.compile(r'\A(SIF|History|BNET) exported: `?(/[^`\n]*?\.(?:sif|json|bnet))`?(?=[.\s]|\Z)')
MANIFEST = 'exports.json'
# Small run files copied with a captured MaBoSS run manifest; state/engine dumps (often
# hundreds of MB) stay in the server store and are identified by the manifest's hashes.
RUN_COMPANION_ROLES = ('node_marginals', 'engine_input')


def artifact_root() -> Path:
    """Shared server artifact root, resolved as the 2.5.x servers do (artifact_manager.artifact_root)."""
    configured = os.environ.get('MCP_BIOMODELLING_ARTIFACT_ROOT')
    if configured:
        root = Path(configured).expanduser()
        if not root.is_absolute():
            raise ValueError('MCP_BIOMODELLING_ARTIFACT_ROOT must be absolute')
        return root
    cache = Path(os.environ.get('XDG_CACHE_HOME', str(Path.home() / '.cache'))).expanduser()
    if not cache.is_absolute():
        cache = Path.home() / '.cache'
    return cache / 'mcp-biomodelling' / 'artifacts'


def _structured(value, depth=0):
    """Unwrap an MCP result (JSON text, CallToolResult dict, content list) to its structured payload."""
    if depth > 4:
        return None
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (json.JSONDecodeError, RecursionError):
            return None
    if isinstance(value, list):
        for block in value:
            if isinstance(block, dict) and block.get('type') == 'text':
                found = _structured(block.get('text'), depth + 1)
                if found is not None:
                    return found
        return None
    if not isinstance(value, dict) or value.get('isError') is True:
        return None
    for key in ('structuredContent', 'structured_content'):
        if isinstance(value.get(key), dict):
            return value[key]
    if 'server' in value and 'session_id' in value:
        return value
    return None


def _texts(value):
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [b['text'] for b in value if isinstance(b, dict) and b.get('type') == 'text' and isinstance(b.get('text'), str)]
    return []


def _paths(tool, fields, payload, content):
    """Return [(format, path-string)] for one successful export result."""
    found = []
    if payload is not None:
        for field, fmt in fields:
            value = payload.get(field)
            path = value.get('path') if isinstance(value, dict) else value
            if isinstance(path, str) and path:
                fmt = fmt or payload.get('format')
                if field == 'curation_file' and payload.get('format'):
                    fmt = f"{payload['format']}-curation"
                found.append((str(fmt), path))
    if not found and tool == 'export_network':
        for text in _texts(content):
            match = NEKO_TEXT.match(text)
            if match:
                found.append((match[1].lower(), match[2]))
                break
    return found


def transcript_exports(transcript: Path, specialist: str) -> list[dict]:
    """Successful export results of this specialist's own server, in transcript order."""
    _, prefix, tools = EXPORT_TOOLS[specialist]
    name = re.compile(r'\Amcp__' + prefix + r'(?:-agent|_agent)?__(.+)\Z')
    reject_symlinks(Path(transcript))
    calls, found = {}, []
    # JSONL records end at '\n'; str.splitlines() would also split on U+0085/U+2028.
    for line in Path(transcript).read_text(encoding='utf-8').split('\n'):
        if not line.strip():
            continue
        event = json.loads(line)
        if not isinstance(event, dict):
            raise ValueError('transcript events must be objects')
        message = event.get('message', {})
        content = message.get('content', []) if isinstance(message, dict) else []
        blocks = [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []
        results = [b for b in blocks if b.get('type') == 'tool_result']
        for block in blocks:
            match = name.match(block.get('name', '') or '')
            if block.get('type') == 'tool_use' and match and match[1] in tools and isinstance(block.get('id'), str):
                calls[block['id']] = match[1]
            elif block.get('type') == 'tool_result' and block.get('tool_use_id') in calls and block.get('is_error') is not True:
                tool = calls[block['tool_use_id']]
                payload = _structured(block.get('content'))
                if payload is None and len(results) == 1:
                    payload = _structured(event.get('toolUseResult'))
                if isinstance(payload, dict) and payload.get('isError') is True:
                    continue
                for fmt, path in _paths(tool, tools[tool], payload, block.get('content')):
                    found.append({'tool': tool, 'format': fmt, 'source_path': path})
    return found


def _check_source(path: str, session_dir: Path) -> tuple[str | None, Path | None]:
    """Return (refusal reason or None, path)."""
    candidate = Path(path)
    if not candidate.is_absolute():
        return 'not an absolute path', None
    if '..' in candidate.parts:
        return 'path contains ..', None
    try:
        relative = candidate.relative_to(session_dir)
    except ValueError:
        return f'outside the server session artifact directory {session_dir}', None
    if not relative.parts:
        return 'not a file', None
    try:
        reject_symlinks(candidate)
    except ValueError:
        return 'path contains a symlink', None
    if os.path.lexists(candidate) and not candidate.is_file():
        return 'not a regular file', None
    return None, candidate


RUN_TOOLS = ('run_simulation', 'simulate_mutation')


def _run_companions(item: dict, source: Path, session_dir: Path) -> list[dict]:
    """Rows for an accepted run manifest's small companions, each with the manifest's expected sha256."""
    try:
        files = json.loads(source.read_text(encoding='utf-8')).get('files')
        if not isinstance(files, list):
            raise ValueError('files is not a list')
    except (OSError, ValueError, AttributeError, RecursionError) as error:
        return [{'tool': item['tool'], 'format': 'maboss-run-companions', 'source_path': item['source_path'],
                 'rejected': True, 'reason': f'run manifest unreadable ({type(error).__name__}); companions not captured'}]
    rows = []
    for entry in files:
        if not isinstance(entry, dict) or entry.get('role') not in RUN_COMPANION_ROLES:
            continue
        name, expected = entry.get('name'), entry.get('sha256')
        row = {'tool': item['tool'], 'format': f"maboss-run-{entry['role']}"}
        if not isinstance(name, str) or not name or Path(name).name != name or name in ('.', '..'):
            rows.append({**row, 'source_path': str(name), 'rejected': True,
                         'reason': 'run manifest file name is not a plain file name'})
        elif not isinstance(expected, str) or not re.fullmatch(r'[0-9a-f]{64}', expected):
            rows.append({**row, 'source_path': str(session_dir / name), 'rejected': True,
                         'reason': 'run manifest gives no sha256 for this file'})
        else:
            rows.append({**row, 'source_path': str(session_dir / name), 'expected_sha256': expected})
    return rows


def capture_exports(project: Path, specialist: str, session: str, invocation: str, transcript: Path) -> dict | None:
    """Copy this invocation's exports; None when the specialist is excluded."""
    if specialist not in EXPORT_TOOLS:
        return None
    validate_session_id(session)
    validate_session_id(invocation)
    project = Path(project).resolve(strict=True)
    server, _, _ = EXPORT_TOOLS[specialist]
    session_dir = artifact_root().absolute() / server / session
    target = project / 'runs' / specialist / session / 'exports' / invocation
    relative_target = target.relative_to(project).as_posix()
    # Later results for the same file supersede earlier ones (the file holds the last write).
    latest = {}
    for item in transcript_exports(transcript, specialist):
        latest.pop(item['source_path'], None)
        latest[item['source_path']] = item
    rows, payloads, names, seen = [], {}, {}, set()

    def take(item):
        """Check and stage one item; return its source path when accepted, else None."""
        reason, source = _check_source(item['source_path'], session_dir)
        if reason is None and source.name == MANIFEST:
            reason = f'{MANIFEST} is reserved for the capture manifest'
        elif reason is None and source.name in names and names[source.name] != item['source_path']:
            reason = f'file name collides with {names[source.name]}'
        if reason is not None:
            rows.append({**item, 'rejected': True, 'reason': reason})
            return None
        if not source.is_file():
            rows.append({**item, 'missing': True})
            return None
        content = source.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        if 'expected_sha256' in item and digest != item['expected_sha256']:
            rows.append({**item, 'rejected': True, 'reason': 'sha256 differs from the run manifest'})
            return None
        names[source.name] = item['source_path']
        rows.append({**item, 'dest_path': f'{relative_target}/{source.name}', 'sha256': digest, 'bytes': len(content)})
        payloads[source.name] = content
        return source

    for item in latest.values():
        source = take(item)
        if source is None or item['tool'] not in RUN_TOOLS:
            continue
        # Companions of an accepted run manifest; a file shared by several runs is staged once.
        for companion in _run_companions(item, source, session_dir):
            key = (companion['source_path'], companion.get('expected_sha256'))
            if key in seen:
                continue
            seen.add(key)
            if companion.get('rejected'):
                rows.append(companion)
            else:
                take(companion)
    summary = {'path': None, 'copied': [r for r in rows if 'sha256' in r],
               'missing': [r for r in rows if r.get('missing')], 'rejected': [r for r in rows if r.get('rejected')]}
    if not rows:
        return summary
    reject_symlinks(target)
    if os.path.lexists(target):
        return _verify_existing(target, relative_target, rows)
    manifest = {'specialist': specialist, 'session_id': session, 'invocation_id': invocation, 'exports': rows}
    with staged_capture(project, target) as stage:
        for file_name, content in payloads.items():
            (stage / file_name).write_bytes(content)
        (stage / MANIFEST).write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
    summary['path'] = relative_target
    return summary


def _verify_existing(target: Path, relative_target: str, rows: list) -> dict:
    """Rerun: verify the earlier capture's copies; never amend it.

    Every previously copied file must still hash as recorded and any file copied again
    must match it, else fail closed. Rows that differ only because the environment or
    the source changed (new refusals, vanished sources, files absent from the earlier
    capture) are reported, not written.
    """
    manifest = target / MANIFEST
    reject_symlinks(manifest)
    try:
        previous = [row for row in json.loads(manifest.read_text(encoding='utf-8'))['exports'] if 'sha256' in row]
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        raise ValueError(f'existing {relative_target}/{MANIFEST} is unreadable ({error}); refusing overwrite') from error
    for row in previous:
        dest = target / Path(row['dest_path']).name
        reject_symlinks(dest)
        if not dest.is_file() or hashlib.sha256(dest.read_bytes()).hexdigest() != row['sha256']:
            raise ValueError(f'existing export {relative_target}/{dest.name} was modified; refusing overwrite')
    by_source = {row['source_path']: row for row in previous}
    missing, rejected = [], []
    for row in rows:
        old = by_source.get(row['source_path'])
        if 'sha256' in row:
            if old is None:
                rejected.append({key: row[key] for key in ('tool', 'format', 'source_path')}
                                | {'rejected': True, 'reason': f'not in the existing capture {relative_target}; '
                                                              'captures are never amended'})
            elif old['sha256'] != row['sha256']:
                raise ValueError(f'existing export {row["dest_path"]} differs from the server file; refusing overwrite')
        elif row.get('missing') and old is None:
            missing.append(row)
        elif row.get('rejected'):
            rejected.append(row)
    return {'path': relative_target, 'copied': previous, 'missing': missing, 'rejected': rejected}
