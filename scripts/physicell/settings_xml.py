"""Helpers for PhysiCell settings XML and for binding to captured configurations.

Stdlib only. XML is parsed with the same declaration-free builder as the
capture recorder, so DTDs and entities are refused.

diff() compares attribute values and element text as stripped strings: "1.0"
and "1" differ. Callers that need numeric equality compare numbers explicitly.
"""
from __future__ import annotations

import hashlib
import math
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path, PurePosixPath

if __package__ in (None, ""):  # run as a script: make the repo importable
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.capture.record_physicell_config import DeclarationFreeTreeBuilder
from scripts.data.common import reject_symlinks, strict_json

ROOT_TAG = "PhysiCell_settings"
SPECIALIST = "multicellular-configurator"
XML_NAME = "PhysiCell_settings.xml"
_IDENTITY = (("name",), ("substrate",), ("start_index", "end_index"), ("index",), ("code",), ("ID",))


def load(path: Path) -> ET.Element:
    """Parse a settings XML, refusing DTD/entity declarations and other roots."""
    return parse_bytes(Path(path).read_bytes())


def parse_bytes(data: bytes) -> ET.Element:
    try:
        root = ET.fromstring(data, parser=ET.XMLParser(target=DeclarationFreeTreeBuilder()))
    except ET.ParseError as error:
        raise ValueError(f"invalid XML: {error}") from error
    if root.tag != ROOT_TAG:
        raise ValueError(f"expected {ROOT_TAG} XML root, found {root.tag}")
    return root


def resolve(root: ET.Element, path: str) -> ET.Element:
    """Return the single element at an ElementTree XPath-subset path."""
    if not isinstance(path, str) or not path or path.startswith(("/", "..")) \
            or ".." in path.split("/"):
        raise ValueError(f"path {path!r} is not allowed: absolute and ancestor steps are refused")
    try:
        found = root.findall(path)
    except Exception as error:
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
            if len(set(tokens)) == len(tokens) and not any(
                    q in v for t in tokens for v in t for q in "'\""):
                return child.tag + "".join(f"[@{k}='{child.get(k)}']" for k in keys)
    return f"{child.tag}[{same.index(child) + 1}]"


def normalise(root: ET.Element) -> list[tuple[str, dict, str]]:
    """Canonical (path, attributes, stripped text) records in document order; the first is the root element itself
    (path ".", review M7), so a change of its attributes is a difference too."""
    records: list[tuple[str, dict, str]] = [(".", dict(root.attrib), (root.text or "").strip())]

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


DECIMAL = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")  # ASCII; no _, nan or inf


def number(text) -> float | None:
    """The finite float an XML text holds as a strict ASCII decimal, else None."""
    text = (text or "").strip() if isinstance(text, str) or text is None else None
    if text is None or not DECIMAL.fullmatch(text):
        return None
    value = float(text)
    return value if math.isfinite(value) else None


def close(found: float | None, expected: float, rel: float = 1e-9) -> bool:
    return found is not None and math.isclose(found, expected, rel_tol=rel, abs_tol=0.0)


def _valued(element: ET.Element | None) -> dict | None:
    if element is None:
        return None
    return {"text": (element.text or "").strip(), "units": element.get("units"), "enabled": element.get("enabled")}


def substrate_facts(root: ET.Element) -> dict[str, dict]:
    """{name: {units, diffusion_coefficient, decay_rate, initial_condition, dirichlet, boundaries, count}} of every
    microenvironment_setup/variable: values are {text, units, enabled} of the element (None when absent),
    boundaries the Dirichlet_options/boundary_value {ID, enabled, text}; count > 1 flags a duplicate name."""
    facts: dict[str, dict] = {}
    for variable in root.findall("microenvironment_setup/variable"):
        name = variable.get("name")
        if name in facts:
            facts[name]["count"] += 1
            continue
        facts[name] = {
            "units": variable.get("units"), "count": 1,
            "diffusion_coefficient": _valued(variable.find("physical_parameter_set/diffusion_coefficient")),
            "decay_rate": _valued(variable.find("physical_parameter_set/decay_rate")),
            "initial_condition": _valued(variable.find("initial_condition")),
            "dirichlet": _valued(variable.find("Dirichlet_boundary_condition")),
            "boundaries": [{"ID": b.get("ID"), "enabled": b.get("enabled"), "text": (b.text or "").strip()}
                           for b in variable.findall("Dirichlet_options/boundary_value")]}
    return facts


def _read_verified(path: Path, expected, label: str) -> tuple[bytes, str]:
    """Read a file once; hash the same bytes that the caller then uses."""
    if not path.is_file():
        raise ValueError(f"{label} missing")
    data = path.read_bytes()
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected:
        raise ValueError(f"{label} changed since capture")
    return data, actual


def _enabled(element: ET.Element, parents: dict) -> bool:
    while element is not None:
        if element.get("enabled", "").lower() in {"false", "0"}:
            return False
        element = parents.get(element)
    return True


# Server 2.5.2: load_xml_configuration keeps the intracellular blocks and links but no rules CSV rows
# (physicell_config's cell_rules.load_from_xml registers rulesets only, and no MCP tool reloads a CSV); a later
# export_cell_rules_csv writes only the new rules and leaves two enabled rulesets in the XML.
RULES_RELOAD = ("the PhysiCell server (2.5.2) cannot reload a captured rules CSV; apply projections to the stage-A "
                "capture (no rules), and revise and re-apply a projection from there")
# Loading a captured package XML into a new session also leaves its ./config input files (rules CSV, MaBoSS .bnd/.cfg,
# initial-cell CSV) behind: they are not in the new session's directory (probe, ruling T5-e).
INPUT_FILES_RELOAD = ("the PhysiCell server (2.5.2) cannot reload a captured configuration's input files (rules, "
                      "MaBoSS, initial cells) into a new session; use a basis without input files")

def capture(root: Path, capture_rel: str) -> dict:
    """Bind a captured configuration, verified against its capture.json manifest.

    Returns {session, capture, xml_path, xml_sha256, rules_path, rules_sha256, manifest, manifest_sha256}; `manifest`
    is the capture.json parsed from the same bytes whose hashes were verified here, `manifest_sha256` the sha256 of
    exactly those capture.json bytes."""
    return capture_tree(root, capture_rel)[0]


def capture_tree(root: Path, capture_rel: str) -> tuple[dict, ET.Element]:
    """(capture(root, capture_rel), the package XML parsed from exactly the bytes whose sha256 it verified): read once,
    so a caller never loads a verified path again (capture.json included: the binding's manifest_sha256)."""
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
    manifest_raw = manifest_path.read_bytes()
    manifest = strict_json(manifest_raw)
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise ValueError("unsupported capture manifest")
    if manifest.get("session_id") != session or manifest.get("capture_id") != capture_id:
        raise ValueError("capture manifest identity disagrees with its path")

    original = folder / "original.xml"
    reject_symlinks(original, root)
    _read_verified(original, manifest.get("original_xml_sha256"), "original.xml")
    xml = folder / "package/config" / XML_NAME
    reject_symlinks(xml, root)
    xml_bytes, xml_sha = _read_verified(xml, manifest.get("package_xml_sha256"), XML_NAME)

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
        by_name[parent.name] = _read_verified(path, dep.get("sha256"), f"dependency {parent.name}")[1]

    tree = parse_bytes(xml_bytes)
    parents = {child: parent for parent in tree.iter() for child in parent}
    rules = [e for e in tree.findall("cell_rules/rulesets/ruleset") if _enabled(e, parents)]
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
            "rules_path": rules_path, "rules_sha256": rules_sha, "manifest": manifest,
            "manifest_sha256": hashlib.sha256(manifest_raw).hexdigest()}, tree
