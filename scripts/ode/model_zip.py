"""Read the names a BioMASS Text2Model export exposes, without importing it.

Export layout (BioMASS server 2.5.x `export_bundle`): `model/` holds the files of the exported revision
folder (`integrity.json`, which seals every other file of it, `model_summary.json`, `snapshot.json`, ...)
and the Text2Model package under `model/generated_model/`; saved runs sit under `simulate_<id>/` and
`graph_<id>/`, plus `requirements.txt` and `run_simulation.py`. The package is the single directory under
`model/` that holds `ode.py` and `name2idx/` (a flat package directly in `model/` is found the same way).

The package is untrusted code: names are taken from the syntax tree with ast.literal_eval and nothing
is executed.
"""

from __future__ import annotations

import ast
import hashlib
import json
import zipfile
from pathlib import Path

REQUIREMENTS = "biomass==0.14.0\n"
MODEL = "model/"
# files of the Text2Model package (relative to it) that the calibration runner needs
PACKAGE_FILES = ("__init__.py", "ode.py", "observable.py", "name2idx/__init__.py", "name2idx/parameters.py",
                 "name2idx/species.py")
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


def find_package(names) -> str:
    """The prefix ("model/.../") of the single Text2Model package among zip member names; ValueError on zero
    or several (a package = a directory under model/ holding ode.py and name2idx/; __pycache__ ignored)."""
    names = [n for n in names if n.startswith(MODEL) and "__pycache__" not in n.split("/")]
    found = sorted({n[:-len("ode.py")] for n in names if n.endswith("/ode.py") or n == MODEL + "ode.py"}
                   & {n[:n.index("name2idx/")] for n in names if "name2idx/" in n
                      and (n.index("name2idx/") == 0 or n[n.index("name2idx/") - 1] == "/")})
    if not found:
        raise ValueError("export zip has no Text2Model package under model/ (a folder with ode.py and name2idx/)")
    if len(found) > 1:
        raise ValueError(f"export zip has several Text2Model packages under model/: {', '.join(found)}")
    return found[0]


def package_prefix(zip_path: Path) -> str:
    try:
        with zipfile.ZipFile(zip_path) as zf:
            return find_package(zf.namelist())
    except zipfile.BadZipFile as exc:
        raise ValueError(f"not a zip file: {exc}") from exc


def verify_inventory(zip_path: Path) -> None:
    """The model/ files of the zip are exactly the ones model/integrity.json seals, with those sha256
    (__pycache__ excluded, as the server seals); ValueError naming the first differences otherwise."""
    try:
        with zipfile.ZipFile(zip_path) as zf:
            inventory = json.loads(_read(zf, MODEL + "integrity.json"))
            if not isinstance(inventory, dict):
                raise ValueError("model/integrity.json must be an object of path -> sha256")
            members = {info.filename[len(MODEL):]: info for info in zf.infolist()
                       if info.filename.startswith(MODEL) and not info.is_dir()
                       and "__pycache__" not in info.filename.split("/")
                       and info.filename != MODEL + "integrity.json"}
            differ = sorted(set(members) ^ set(inventory))
            for rel in sorted(set(members) & set(inventory)):
                info = members[rel]
                if info.file_size > MAX_MEMBER_BYTES * 16 or \
                        hashlib.sha256(zf.read(info)).hexdigest() != inventory[rel]:
                    differ.append(rel)
    except zipfile.BadZipFile as exc:
        raise ValueError(f"not a zip file: {exc}") from exc
    if differ:
        raise ValueError("export zip files differ from its model/integrity.json: "
                         + ", ".join(sorted(differ)[:10]))


def introspect(zip_path: Path) -> dict:
    """{"parameters", "species", "observables"} of an export zip; ValueError if it is not a BioMASS 0.14.0 export."""
    try:
        with zipfile.ZipFile(zip_path) as zf:
            if _read(zf, "requirements.txt") != REQUIREMENTS:
                raise ValueError(f"requirements.txt must be exactly {REQUIREMENTS!r}")
            pkg = find_package(zf.namelist())
            files = {rel: pkg + rel for rel in ("name2idx/parameters.py", "name2idx/species.py", "observable.py")}
            return {
                "parameters": _literal_names(_read(zf, files["name2idx/parameters.py"]),
                                             files["name2idx/parameters.py"], "NAMES", None),
                "species": _literal_names(_read(zf, files["name2idx/species.py"]),
                                          files["name2idx/species.py"], "NAMES", None),
                "observables": _literal_names(_read(zf, files["observable.py"]),
                                              files["observable.py"], "obs_names", "self"),
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
