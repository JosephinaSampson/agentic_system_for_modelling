"""Helpers for PhysiCell settings XML and for binding to captured configurations.

Stdlib only. XML is parsed with the same declaration-free builder as the
capture recorder, so DTDs and entities are refused.
"""
from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.capture.record_physicell_config import DeclarationFreeTreeBuilder
from scripts.data.common import reject_symlinks, sha256_file, strict_json

ROOT_TAG = "PhysiCell_settings"
SPECIALIST = "multicellular-configurator"
XML_NAME = "PhysiCell_settings.xml"
_IDENTITY = (("name",), ("start_index", "end_index"), ("index",), ("code",), ("ID",))


def load(path: Path) -> ET.Element:
    """Parse a settings XML, refusing DTD/entity declarations and other roots."""
    data = Path(path).read_bytes()
    try:
        root = ET.fromstring(data, parser=ET.XMLParser(target=DeclarationFreeTreeBuilder()))
    except ET.ParseError as error:
        raise ValueError(f"invalid XML: {error}") from error
    if root.tag != ROOT_TAG:
        raise ValueError(f"expected {ROOT_TAG} XML root, found {root.tag}")
    return root


def resolve(root: ET.Element, path: str) -> ET.Element:
    """Return the single element at an ElementTree XPath-subset path."""
    try:
        found = root.findall(path)
    except (SyntaxError, KeyError, ValueError) as error:
        raise ValueError(f"invalid path {path}: {error}") from error
    if len(found) != 1:
        raise ValueError(f"path {path} matches {len(found)} elements")
    return found[0]


def units(element: ET.Element) -> str | None:
    return element.get("units")


def _step(child: ET.Element, siblings: list[ET.Element]) -> str:
    same = [s for s in siblings if s.tag == child.tag]
    if len(same) == 1:
        return child.tag
    for keys in _IDENTITY:
        if all(k in s.attrib for s in same for k in keys):
            tokens = [tuple(s.get(k) for k in keys) for s in same]
            if len(set(tokens)) == len(tokens):
                return child.tag + "".join(f"[@{k}='{child.get(k)}']" for k in keys)
    return f"{child.tag}[{same.index(child) + 1}]"


def normalise(root: ET.Element) -> list[tuple[str, dict, str]]:
    """Canonical (path, attributes, stripped text) records in document order."""
    records: list[tuple[str, dict, str]] = []

    def walk(element: ET.Element, prefix: str) -> None:
        children = list(element)
        for child in children:
            path = f"{prefix}/{_step(child, children)}" if prefix else _step(child, children)
            records.append((path, dict(child.attrib), (child.text or "").strip()))
            walk(child, path)

    walk(root, "")
    return records


def diff(before_root: ET.Element, after_root: ET.Element) -> list[dict]:
    """Changed, added or removed elements, ignoring sibling order and whitespace."""
    def index(root):
        return {p: {"attributes": a, "text": t} for p, a, t in normalise(root)}

    before, after = index(before_root), index(after_root)
    paths = [p for p in before if p in after] + [p for p in before if p not in after] + \
        [p for p in after if p not in before]
    return [{"path": p, "before": before.get(p), "after": after.get(p)}
            for p in paths if before.get(p) != after.get(p)]


def cell_definitions(root: ET.Element) -> list[str]:
    return [e.get("name") for e in root.findall("cell_definitions/cell_definition")]


def substrates(root: ET.Element) -> list[str]:
    return [e.get("name") for e in root.findall("microenvironment_setup/variable")]


def cycle_model(root: ET.Element, cell_definition: str) -> str:
    matches = [e for e in root.findall("cell_definitions/cell_definition")
               if e.get("name") == cell_definition]
    if len(matches) != 1:
        raise ValueError(f"cell definition {cell_definition} matches {len(matches)} elements")
    cycle = matches[0].findall("phenotype/cycle")
    if len(cycle) != 1 or not cycle[0].get("name"):
        raise ValueError(f"cell definition {cell_definition} has no unique named cycle")
    return cycle[0].get("name")


def _check_hash(path: Path, expected, label: str) -> str:
    if not path.is_file():
        raise ValueError(f"{label} missing")
    actual = sha256_file(path)
    if actual != expected:
        raise ValueError(f"{label} changed since capture")
    return actual


def capture(root: Path, capture_rel: str) -> dict:
    """Bind a captured configuration, verified against its capture.json manifest."""
    root = Path(root)
    rel = PurePosixPath(capture_rel) if isinstance(capture_rel, str) else None
    if rel is None or rel.is_absolute() or "\\" in capture_rel or rel.as_posix() != capture_rel \
            or len(rel.parts) != 5 or ".." in rel.parts or "." in rel.parts \
            or rel.parts[:2] != ("runs", SPECIALIST) or rel.parts[3] != "configs":
        raise ValueError(f"capture must be runs/{SPECIALIST}/<session>/configs/<capture>")
    session, capture_id = rel.parts[2], rel.parts[4]
    folder = root / rel
    reject_symlinks(folder, root)
    if not folder.is_dir():
        raise ValueError(f"capture folder does not exist: {capture_rel}")
    manifest_path = folder / "capture.json"
    reject_symlinks(manifest_path, root)
    if not manifest_path.is_file():
        raise ValueError("capture manifest capture.json missing")
    manifest = strict_json(manifest_path.read_bytes())
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise ValueError("unsupported capture manifest")
    if manifest.get("session_id") != session or manifest.get("capture_id") != capture_id:
        raise ValueError("capture manifest identity disagrees with its path")

    original = folder / "original.xml"
    reject_symlinks(original, root)
    _check_hash(original, manifest.get("original_xml_sha256"), "original.xml")
    xml = folder / "package/config" / XML_NAME
    reject_symlinks(xml, root)
    xml_sha = _check_hash(xml, manifest.get("package_xml_sha256"), XML_NAME)

    deps = manifest.get("dependencies")
    if not isinstance(deps, list):
        raise ValueError("capture manifest dependencies must be a list")
    by_name = {}
    for dep in deps:
        if not isinstance(dep, dict) or not isinstance(dep.get("path"), str):
            raise ValueError("malformed dependency entry")
        parent = PurePosixPath(dep["path"])
        if parent.parent != PurePosixPath("package/config") or ".." in parent.parts:
            raise ValueError("dependency outside package/config")
        path = folder / parent
        reject_symlinks(path, root)
        by_name[parent.name] = _check_hash(path, dep.get("sha256"), f"dependency {parent.name}")

    rules = [e for e in load(xml).findall("cell_rules/rulesets/ruleset")
             if e.get("enabled", "").lower() not in {"false", "0"}]
    rules_path = rules_sha = None
    if len(rules) > 1:
        raise ValueError("more than one enabled ruleset; only one rules CSV is supported")
    if rules:
        name = (rules[0].findtext("filename") or "").strip()
        if name not in by_name:
            raise ValueError("enabled ruleset file is not a captured dependency")
        rules_path = (rel / "package/config" / name).as_posix()
        rules_sha = by_name[name]
    return {"session": session, "capture": capture_id,
            "xml_path": (rel / "package/config" / XML_NAME).as_posix(), "xml_sha256": xml_sha,
            "rules_path": rules_path, "rules_sha256": rules_sha}
