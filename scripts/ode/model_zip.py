"""Read the names a BioMASS Text2Model export exposes, without importing it.

The package under `model/` of an export zip is untrusted code: names are taken from the
syntax tree with ast.literal_eval and nothing is executed.
"""

from __future__ import annotations

import ast
import zipfile
from pathlib import Path

REQUIREMENTS = "biomass==0.14.0\n"
MAX_MEMBER_BYTES = 4 * 1024 * 1024


def _read(zf: zipfile.ZipFile, name: str) -> str:
    try:
        info = zf.getinfo(name)
    except KeyError:
        raise ValueError(f"export zip has no {name}") from None
    if info.file_size > MAX_MEMBER_BYTES:
        raise ValueError(f"{name} is larger than {MAX_MEMBER_BYTES} bytes")
    try:
        return zf.read(info).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{name} is not UTF-8: {exc}") from exc


def _is_target(node, attr: str | None, name: str) -> bool:
    if attr is None:
        return isinstance(node, ast.Name) and node.id == name
    return (isinstance(node, ast.Attribute) and node.attr == name
            and isinstance(node.value, ast.Name) and node.value.id == attr)


def _literal_names(source: str, label: str, name: str, attr: str | None) -> list[str]:
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise ValueError(f"{label} is not valid Python: {exc}") from exc
    # NAMES is a module-level assignment; obs_names is assigned inside a method, so walk everything.
    nodes = tree.body if attr is None else list(ast.walk(tree))
    values = []
    for node in nodes:
        if isinstance(node, ast.Assign) and any(_is_target(t, attr, name) for t in node.targets):
            values.append(node.value)
        elif isinstance(node, ast.AnnAssign) and node.value is not None and _is_target(node.target, attr, name):
            values.append(node.value)
    shown = name if attr is None else f"{attr}.{name}"
    if len(values) != 1:
        raise ValueError(f"{label} must assign {shown} exactly once (found {len(values)})")
    try:
        names = ast.literal_eval(values[0])
    except ValueError as exc:
        raise ValueError(f"{label}: {shown} is not a literal list of strings") from exc
    if not isinstance(names, list) or not all(isinstance(n, str) and n for n in names):
        raise ValueError(f"{label}: {shown} must be a list of non-empty strings")
    if len(set(names)) != len(names):
        raise ValueError(f"{label}: {shown} has duplicate names")
    return names


def introspect(zip_path: Path) -> dict:
    """{"parameters", "species", "observables"} of an export zip; ValueError if it is not a BioMASS 0.14.0 export."""
    try:
        with zipfile.ZipFile(zip_path) as zf:
            if _read(zf, "requirements.txt") != REQUIREMENTS:
                raise ValueError(f"requirements.txt must be exactly {REQUIREMENTS!r}")
            return {
                "parameters": _literal_names(_read(zf, "model/name2idx/parameters.py"),
                                             "model/name2idx/parameters.py", "NAMES", None),
                "species": _literal_names(_read(zf, "model/name2idx/species.py"),
                                          "model/name2idx/species.py", "NAMES", None),
                "observables": _literal_names(_read(zf, "model/observable.py"),
                                              "model/observable.py", "obs_names", "self"),
            }
    except zipfile.BadZipFile as exc:
        raise ValueError(f"not a zip file: {exc}") from exc


def integrity(zip_path: Path) -> bytes:
    """The bytes of the export's model/integrity.json: the file inventory of the revision it was exported from.

    A captured revision folder (runs/ode-modeler/<session>/.../rev_<32 hex>/integrity.json) with the same bytes
    is that revision (the same binding scripts/codex/ode_artifacts.validate_bundle checks).
    """
    try:
        with zipfile.ZipFile(zip_path) as zf:
            return _read(zf, "model/integrity.json").encode("utf-8")
    except zipfile.BadZipFile as exc:
        raise ValueError(f"not a zip file: {exc}") from exc
