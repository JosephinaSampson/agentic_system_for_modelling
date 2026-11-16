#!/usr/bin/env python3
"""Fail closed on unreviewed, unapproved or stale downstream stage requests."""
from __future__ import annotations

import argparse
import json
import os
import posixpath
import re
from pathlib import Path, PurePosixPath
import sys
import unicodedata

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.state import ROOT, REGISTRY, checked_artifact, identifier, records, table, validate_record
from scripts.curation_contract import check_binding, check_parent_scope, check_readout_isolation
from scripts.data import approvals
from scripts.data.manifest import KIND_SCHEMAS

UPSTREAM = {"boolean-dynamics": ("network-curator", "topology"), "multicellular": ("boolean-dynamics-modeler", "boolean-dynamics"), "biomass_ode": ("network-curator", "topology")}
# Backend 2.5.0: NeKo->MaBoSS 2.1 and NeKo->BioMASS 2.0 carry curation_file;
# MaBoSS->PhysiCell 2.1 carries model_revision and readout definitions.
VERSIONS = {"neko-to-maboss": {"1.0", "2.0", "2.1"}, "maboss-to-physicell": {"1.0", "2.0", "2.1"},
            "neko-to-biomass": {"1.0", "2.0"}}
CURATED = {("neko-to-maboss", "2.1"): "bnet_file", ("neko-to-biomass", "2.0"): "network_file"}
SPECIALIST_STAGE = {"boolean_dynamics_modeler": "boolean-dynamics", "multicellular_configurator": "multicellular", "ode_modeler": "biomass_ode"}


def _names(value, label):
    if not isinstance(value, list) or not value or any(not isinstance(x, str) or not x.strip() for x in value) or len(set(value)) != len(value):
        raise ValueError(f"{label} must be a nonempty unique string list")
    return value


def _reference(root, ref, server, session, role):
    if not isinstance(ref, dict) or (ref.get("server"), ref.get("session_id"), ref.get("role")) != (server, session, role):
        raise ValueError(f"missing or wrong-owner {role} artifact reference")
    raw = ref.get("path")
    if not isinstance(raw, str) or not raw:
        raise ValueError("artifact path is missing")
    path = Path(raw)
    if path.is_absolute():
        path = path.relative_to(root.resolve())
    file = checked_artifact(root, {"path": path.as_posix(), "sha256": ref.get("sha256")})
    suffix, media = {"neko_bnet": (".bnet", "text/plain"), "maboss_bnd": (".bnd", "text/plain"),
                     "maboss_cfg": (".cfg", "text/plain"), "maboss_result": (".csv", "text/csv"),
                     "parent_manifest": (".json", "application/json"), "neko_ode_network": (".json", "application/json"),
                     "neko_history": (".json", "application/json")}[role]
    if ref.get("name") != file.name or ref.get("suffix") != suffix or file.suffix != suffix or ref.get("media_type") != media:
        raise ValueError("artifact name, suffix or media type mismatch")
    if type(ref.get("size_bytes")) is not int or ref["size_bytes"] != file.stat().st_size:
        raise ValueError("artifact byte count mismatch")
    return file


def ode_graph_nodes(file):
    """Node IDs of an exported NeKo-to-BioMASS graph, which must be unique."""
    try:
        graph = json.loads(file.read_text())
    except ValueError:
        raise ValueError("ODE network_file is not valid JSON") from None
    items = graph.get("nodes") if isinstance(graph, dict) else None
    if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
        raise ValueError("ODE network_file must contain a node list")
    return _names([item.get("node_id") for item in items], "ODE graph node IDs")


def verify_manifest(root, data, expected_type, session, outputs=None):
    """Verify the versioned identity, required payloads and complete parent chain.

    This read-only boundary adds checks before the server's own typed import;
    it never imports modelling libraries or executes a scientific operation.
    """
    if not isinstance(data, dict) or (data.get("schema_name"), data.get("handoff_type")) != ("mcp-biomodelling-handoff", expected_type):
        raise ValueError("expected typed handoff for this downstream stage")
    version = data.get("schema_version")
    if version not in VERSIONS[expected_type]:
        raise ValueError("unsupported typed handoff version for this downstream stage")
    if version != "1.0":
        from scripts.boolean_contract import validate_metadata
        validate_metadata(data)
    elif data.get("source_ancestry") is not None:
        raise ValueError("v1 handoffs cannot declare source ancestry")
    elif any(data.get(key) is not None for key in ("curation_file", "model_revision", "readouts")):
        raise ValueError("v1 handoffs cannot declare curation or readout metadata")
    server = "MaBoSS" if expected_type == "maboss-to-physicell" else "NeKo"
    source = data.get("source")
    if not isinstance(source, dict) or (source.get("server"), source.get("session_id")) != (server, session):
        raise ValueError("handoff source server/session mismatch")
    if expected_type == "neko-to-biomass":
        # An ODE graph has no output selection: its approval names the complete
        # node inventory of the exact exported graph.
        if data.get("network") is not None or data.get("lineage", []):
            raise ValueError("NeKo-to-BioMASS handoffs declare only their network_file graph")
        graph = _reference(root, data.get("network_file"), server, session, "neko_ode_network")
        nodes = ode_graph_nodes(graph)
        if outputs is not None and set(outputs) != set(nodes):
            raise ValueError("ODE approval outputs must name every node_id of the exported graph")
    else:
        network = data.get("network")
        if not isinstance(network, dict):
            raise ValueError("handoff network is missing")
        nodes = _names(network.get("nodes"), "network nodes")
        declared = _names(network.get("output_nodes"), "declared outputs")
        if not set(declared) <= set(nodes) or (outputs is not None and declared != outputs):
            raise ValueError("declared outputs do not match exported network")
    if expected_type == "neko-to-maboss":
        if data.get("lineage", []):
            raise ValueError("NeKo source cannot declare upstream lineage")
        bnet = _reference(root, data.get("bnet_file"), server, session, "neko_bnet")
        actual = [line.split(",", 1)[0].strip() for line in bnet.read_text().splitlines()
                  if line.strip() and not line.lstrip().startswith("#") and "," in line and line.split(",", 1)[0].strip().lower() != "targets"]
        if actual != nodes:
            raise ValueError("BNET targets do not match declared network nodes")
    if (expected_type, version) in CURATED:
        # The companion binds the original model path; every other identity
        # field and the pinned companion bytes must still match the payload.
        companion = _reference(root, data.get("curation_file"), "NeKo", session, "neko_history")
        model = _reference(root, data.get(CURATED[expected_type, version]), "NeKo", session,
                           "neko_bnet" if expected_type == "neko-to-maboss" else "neko_ode_network")
        tree = companion.resolve().relative_to(root.resolve()).parts
        if len(tree) < 4 or tree[0] != "runs" or tree[:3] != model.resolve().relative_to(root.resolve()).parts[:3]:
            raise ValueError("curation companion must be recorded in the same session tree as its model")
        check_binding(companion.read_bytes(), data[CURATED[expected_type, version]], session, data.get("history_state_id"))
    if expected_type == "maboss-to-physicell":
        bnd = _reference(root, data.get("bnd_file"), server, session, "maboss_bnd")
        cfg = _reference(root, data.get("cfg_file"), server, session, "maboss_cfg")
        text = re.sub(r"/\*.*?\*/", "", bnd.read_text(), flags=re.S)
        text = re.sub(r"//[^\n]*", "", text)
        if re.findall(r"(?im)^\s*Node\s+([A-Za-z_][A-Za-z0-9_]*)\s*\{", text) != nodes:
            raise ValueError("BND nodes do not match declared network nodes")
        readouts = data.get("readouts") or {}
        if version == "2.1":
            if not isinstance(data.get("model_revision"), str) or not re.fullmatch(r"[0-9a-f]{64}", data["model_revision"]):
                raise ValueError("MaBoSS v2.1 handoff requires an exact model_revision")
            if not isinstance(data.get("readouts"), dict):
                raise ValueError("MaBoSS v2.1 handoff requires explicit readout definitions")
        check_readout_isolation(bnd.read_text(), nodes, readouts)
        simulation = data.get("simulation")
        target = data.get("target")
        if not isinstance(simulation, dict) or not isinstance(target, dict) or not isinstance(target.get("cell_type"), str) or not target["cell_type"].strip():
            raise ValueError("MaBoSS simulation/target metadata is missing")
        if simulation.get("result_file") is not None:
            _reference(root, simulation["result_file"], server, session, "maboss_result")
        parent = data.get("parent_manifest")
        lineage = data.get("lineage", [])
        if parent is None:
            if lineage:
                raise ValueError("declared upstream lineage needs parent manifest")
        else:
            if not isinstance(lineage, list) or len(lineage) != 1 or not isinstance(lineage[0], dict) or lineage[0].get("server") != "NeKo":
                raise ValueError("parent manifest requires exactly one NeKo lineage entry")
            upstream = identifier(lineage[0].get("session_id"))
            file = _reference(root, parent, "NeKo", upstream, "parent_manifest")
            parent_data = json.loads(file.read_text())
            verify_manifest(root, parent_data, "neko-to-maboss", upstream)
            if parent_data.get("source") != lineage[0]:
                raise ValueError("parent provenance disagrees with declared stage lineage")
            # Only declared readouts may extend the NeKo core (none before v2.1).
            check_parent_scope(parent_data["network"]["nodes"], parent_data["network"]["output_nodes"],
                               nodes, declared, readouts)
            if version != "1.0":
                inherited = [item for item in data["source_ancestry"] if item["source"]["server"] == "NeKo"]
                if inherited != (parent_data.get("source_ancestry") or []):
                    raise ValueError("current ancestry disagrees with the exact stage parent")
    return data


def check(root, stage, session, outputs):
    if stage not in UPSTREAM:
        raise ValueError("unknown downstream stage")
    identifier(session)
    if not isinstance(outputs, list) or not outputs or any(not isinstance(x, str) or not x for x in outputs) or len(set(outputs)) != len(outputs):
        raise ValueError("request must name exact unique declared outputs")
    role, upstream_stage = UPSTREAM[stage]
    rows = table((root / "CURRENT_STATE.md").read_text(), REGISTRY)[3]
    matching = [r for r in rows if r["Session ID"] == session]
    if len(matching) != 1:
        raise ValueError("upstream session must have exactly one registry row")
    row = matching[0]
    if (row["Specialist"], row["Stage"], row["Status"], row["Handoff"]) != (role, upstream_stage, "active", "exported"):
        raise ValueError("upstream must be active, at the expected stage, and exactly exported")
    recs = records((root / "DECISIONS.md").read_text())
    approvals = [r for r in recs if r.get("kind") == "stage_approval" and r.get("session_id") == session and r.get("stage") == upstream_stage and r.get("downstream_stage") == stage and r.get("outputs") == outputs]
    if not approvals:
        raise ValueError("no researcher approval names this session, downstream stage and exact outputs")
    approval = approvals[-1]
    validate_record(root, approval)
    manifest = checked_artifact(root, approval["artifact"])
    prefix = f"runs/{role}/{session}/"
    if not approval["artifact"]["path"].startswith(prefix):
        raise ValueError("handoff artifact must be inside the exact upstream session")
    data = json.loads(manifest.read_text())
    expected_type = {"boolean-dynamics": "neko-to-maboss", "multicellular": "maboss-to-physicell", "biomass_ode": "neko-to-biomass"}[stage]
    verify_manifest(root, data, expected_type, session, outputs)
    if stage == "multicellular" and row["Derived from"] != "—":
        parents = {(data.get("parent_manifest") or {}).get("session_id")}
        if data.get("schema_version") != "1.0":
            # A separately approved BNET derivative retains its immediate source
            # parent without pretending it was an unchanged typed stage import.
            # Only the current session's direct NeKo parent can bind this row;
            # arbitrary/transitive historical sessions never qualify.
            parents.update(
                item["parent_manifest"]["session_id"]
                for item in data["source_ancestry"]
                if (item["source"]["server"], item["source"]["session_id"]) == ("MaBoSS", session)
                and (item.get("parent_manifest") or {}).get("server") == "NeKo"
            )
        if row["Derived from"] not in parents:
            raise ValueError("registry upstream lineage and exported parent disagree")
    reviews = [r for r in recs if r.get("kind") == "stage_review" and r.get("session_id") == session and r.get("stage") == upstream_stage]
    if not reviews:
        raise ValueError("independent scientific review and reproducibility audit have not been recorded")
    review = reviews[-1]
    validate_record(root, review)
    if review["outcome"] not in {"approved", "approved_with_limitations"} or review["artifact"] != approval["artifact"]:
        raise ValueError("latest independent review blocks or covers a different artifact revision")
    if recs.index(review) > recs.index(approval):
        raise ValueError("researcher approval must follow the independent review")
    return {"passed": True, "downstream_stage": stage, "upstream_session_id": session, "outputs": outputs, "approval_id": approval["id"], "review_id": review["id"], "artifact": approval["artifact"]}


def standalone(root, stage, decision_id, outputs):
    matching = [r for r in records((root / "DECISIONS.md").read_text()) if r.get("id") == decision_id]
    if len(matching) != 1:
        raise ValueError("missing explicit standalone authorization record")
    record = matching[0]
    validate_record(root, record)
    if record["kind"] != "standalone_approval" or record["stage"] != stage or record["outputs"] != outputs:
        raise ValueError("standalone approval scope mismatch")
    return {"passed": True, "downstream_stage": stage, "standalone_decision_id": decision_id, "artifact": record["artifact"], "outputs": outputs}


STAGE_GATE_FORMS = ({"upstream_session_id", "outputs"}, {"standalone_decision_id", "outputs"})


def stage_gate_form(line):
    """The value of a well-formed line-1 `STAGE_GATE` (strict JSON object: exactly upstream_session_id or
    standalone_decision_id, an identifier, and outputs, a nonempty unique list of node names); ValueError otherwise.
    Shape only: check_task resolves it against the durable records (lineage uses this to recognise a recorded brief
    whose dispatch the hook verified)."""
    if not isinstance(line, str) or not line.startswith("STAGE_GATE "):
        raise ValueError("line 1 is not a STAGE_GATE line")
    value = _strict_object(line[len("STAGE_GATE "):], "STAGE_GATE")
    if set(value) not in STAGE_GATE_FORMS:
        raise ValueError("STAGE_GATE must have exactly upstream_session_id or standalone_decision_id, and outputs")
    identifier(value.get("upstream_session_id", value.get("standalone_decision_id")))
    _names(value["outputs"], "STAGE_GATE outputs")
    return value


def check_task(root, specialist, task):
    """A literal first-line envelope makes dispatch context machine-checkable.

    The task still contains the full bounded scientific instructions. A prompt
    assertion alone cannot pass: matching durable records and hashes are required.
    """
    if specialist not in SPECIALIST_STAGE:
        return None
    first = task.splitlines()[0] if task else ""
    if not first.startswith("STAGE_GATE "):
        raise ValueError("downstream dispatch requires first-line STAGE_GATE JSON; run scripts/gate.py first")
    value = json.loads(first[len("STAGE_GATE "):])
    if not isinstance(value, dict):
        raise ValueError("STAGE_GATE must be a JSON object")
    stage = SPECIALIST_STAGE[specialist]
    if set(value) == {"standalone_decision_id", "outputs"}:
        return standalone(root, stage, value["standalone_decision_id"], value["outputs"])
    if set(value) != {"upstream_session_id", "outputs"}:
        raise ValueError("STAGE_GATE requires upstream_session_id and outputs")
    return check(root, stage, value["upstream_session_id"], value["outputs"])


# Detection runs on a normalised copy of the prompt (see _detection_text); any spelling
# of DATA_INPUT, data-analyst path or data session ID counts.
DATA_PATH = re.compile(r"runs[\\/]+data-analyst", re.I)
DATA_SESSION_PATH = re.compile(r"runs[\\/]+data-analyst[\\/]+([A-Za-z0-9][A-Za-z0-9._-]*)", re.I)
DATA_SESSION_ID = re.compile(r"data-\d{8}T\d{6}Z-[0-9a-f]{8}", re.I)
DATA_INPUT_MENTION = re.compile(r"DATA[_-]?INPUT", re.I)
# Payload-like mentions (an attempt at a second or miswritten DATA_INPUT line): one that
# starts its line after optional whitespace, quote marks, `>`, `-` or `*`; one with a `{`
# within the next PAYLOAD_WINDOW characters of its line (`DATA_INPUT => {`, `DATA_INPUTS {`,
# `| DATA_INPUT | {`); or one ending its line (only punctuation after it) whose next
# non-blank line starts with `{` after optional markup. Other mentions are prose.
QUOTES = "`'\"\u2018\u2019\u201c\u201d"
PAYLOAD_WINDOW = 16
PAYLOAD_PREFIX = re.compile(rf"[\s>*{QUOTES}-]*")
PAYLOAD_SUFFIX = re.compile(rf"[^\n]{{0,{PAYLOAD_WINDOW}}}\{{")
LINE_END = re.compile(r"[\W_]*")
MARKUP = re.compile(rf"[\s>*|{QUOTES}-]*")
QUOTE_CHARS = 60
DATA_REFUSAL = "brief references data-analyst artifacts without a DATA_INPUT line; see CLAUDE.md"
# Specialist -> (0-based line index of its DATA_INPUT line, human label).
DATA_INPUT_LINE = {"network_curator": (0, "network-curator DATA_INPUT must be line 1 of the prompt"),
                   "boolean_dynamics_modeler": (1, "boolean-dynamics-modeler DATA_INPUT must be line 2, after STAGE_GATE"),
                   "multicellular_configurator": (1, "multicellular-configurator DATA_INPUT must be line 2, after STAGE_GATE"),
                   "ode_modeler": (0, "ode-modeler DATA_INPUT must be line 1 of the prompt")}
SHA256 = re.compile(r"[0-9a-f]{64}")


def _strict_object(text, label):
    def unique(pairs):
        keys = [k for k, _ in pairs]
        if len(keys) != len(set(keys)):
            raise ValueError(f"{label} has duplicate keys")
        return dict(pairs)
    try:
        value = json.loads(text, object_pairs_hook=unique)
    except RecursionError:
        raise ValueError(f"{label} JSON is nested too deeply") from None
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def _detection_text(task):
    """The prompt NFKC-normalised and without format characters (BOM, zero-width), for detection only."""
    return "".join(ch for ch in unicodedata.normalize("NFKC", task) if unicodedata.category(ch) != "Cf")


def _data_sessions_named(text):
    """Data-analyst sessions named by `runs/data-analyst/<session>` paths or by data session IDs."""
    return ({m.rstrip(".") for m in DATA_SESSION_PATH.findall(text)} | set(DATA_SESSION_ID.findall(text)))


def _data_approvals(root, session):
    """Every `stage_approval` (stage data) of this data session, in DECISIONS.md order.

    Approvals are cumulative: a proposal is approved when any valid record that
    binds the current handoff.json lists it in `outputs`. There is no revocation
    record; a proposal is withdrawn by re-running the analysis in a new session.
    """
    recs = records((root / "DECISIONS.md").read_text(encoding="utf-8"))
    found = [r for r in recs if r.get("kind") == "stage_approval"
             and r.get("stage") == "data" and r.get("session_id") == session]
    if not found:
        raise ValueError(f"no researcher stage_approval of stage data names data session {session}")
    return found


def _approval_for(root, session, proposal_id):
    """The first valid data approval of the session that lists the proposal, else the refusal."""
    failures = []
    for candidate in _data_approvals(root, session):
        try:
            record = approvals.require_data_approval(root, candidate["id"], session)
            validate_record(root, record)
            approvals.require_proposal_in_outputs(record, proposal_id)
        except ValueError as exc:
            failures.append(str(exc))
            continue
        return record
    raise ValueError(f"proposal {proposal_id} is not in the approved outputs of any valid data approval of "
                     f"session {session} for its current handoff: " + "; ".join(failures))


NEKO_PROPOSALS_FORM = {"data_session", "proposals"}
NEKO_SEEDS_FORM = {"seeds", "sha256", "approval"}


def _check_neko_data_input(root, value):
    if set(value) == NEKO_SEEDS_FORM:
        return _check_neko_seeds_input(root, value)
    if set(value) != NEKO_PROPOSALS_FORM:
        raise ValueError("network-curator DATA_INPUT requires exactly data_session and proposals, "
                         "or exactly seeds, sha256 and approval")
    session = identifier(value["data_session"])
    proposals = value["proposals"]
    if not isinstance(proposals, dict) or not proposals:
        raise ValueError("DATA_INPUT proposals must be a nonempty object of proposal id -> sha256")
    _data_approvals(root, session)  # an unknown session is refused before any proposal is looked at
    resolved, used = {}, []
    for proposal_id, digest in proposals.items():
        if not isinstance(digest, str) or not SHA256.fullmatch(digest):
            raise ValueError(f"DATA_INPUT proposal {proposal_id} needs a lowercase hex sha256")
        record = _approval_for(root, session, proposal_id)
        used.append(record["id"])
        entry = approvals.require_proposal_approved(root, record, session, proposal_id)
        if entry.get("kind") != "seed_genes":
            raise ValueError(f"proposal {proposal_id} has kind {entry.get('kind')!r}; network-curator "
                             "DATA_INPUT takes only seed_genes")
        layer = KIND_SCHEMAS["seed_genes"]["layer"]
        if entry.get("target_layer") != layer:
            raise ValueError(f"proposal {proposal_id} must target {layer}, not {entry.get('target_layer')!r}")
        if entry["sha256"] != digest:
            raise ValueError(f"proposal {proposal_id} sha256 {digest[:12]} differs from the approved file "
                             f"{entry['path']} ({entry['sha256'][:12]})")
        resolved[proposal_id] = {"path": entry["path"], "sha256": entry["sha256"]}
    from scripts.data import mapping
    mapping.validate_data_handoff(root, session)
    return {"data_session": session, "approval_id": used[0], "approval_ids": list(dict.fromkeys(used)),
            "proposals": resolved}, {session}


# STAGE_GATE key -> the inputs.network key naming the same anchor, and how messages call it.
NETWORK_ANCHORS = {"upstream_session_id": ("neko_session", "NeKo session"),
                   "standalone_decision_id": ("standalone_decision_id", "standalone approval")}
REDRAFT_HINT = "redraft with mapping.py draft --from"


def _gate_anchor(first_line):
    """(STAGE_GATE key, value) of a MaBoSS brief's line 1: its upstream_session_id or standalone_decision_id."""
    gate_value = _strict_object(first_line[len("STAGE_GATE "):], "STAGE_GATE") if first_line.startswith("STAGE_GATE ") else {}
    for key in NETWORK_ANCHORS:
        if isinstance(gate_value.get(key), str):
            return key, gate_value[key]
    raise ValueError("a MaBoSS DATA_INPUT needs a line-1 STAGE_GATE naming upstream_session_id or standalone_decision_id")


def check_mapping_network(root, network, first_line):
    """Bind a mapping's inputs.network to the line-1 STAGE_GATE of the MaBoSS brief.

    NeKo upstream: network.neko_session equals upstream_session_id and network.sha256 is the
    BNET of the NeKo handoff that STAGE_GATE approves. Standalone: network.standalone_decision_id
    equals the STAGE_GATE's and network.sha256 is that standalone_approval's artifact sha256.
    check_task re-resolves the STAGE_GATE (not weakened by this). Returns (key, anchor).
    """
    key, anchor = _gate_anchor(first_line)
    if not isinstance(network, dict):
        raise ValueError("mapping inputs.network is missing")
    field, label = NETWORK_ANCHORS[key]
    if field not in network:
        other = next((k for k, (f, _) in NETWORK_ANCHORS.items() if f in network), None)
        if other is None:
            raise ValueError("mapping inputs.network names neither neko_session nor standalone_decision_id")
        other_field, other_label = NETWORK_ANCHORS[other]
        raise ValueError(f"mapping network is {other_label} {network[other_field]}, so its STAGE_GATE must name "
                         f"{other} {network[other_field]}, not {key} {anchor}")
    if network[field] != anchor:
        hint = REDRAFT_HINT if key == "upstream_session_id" else \
            f"check the STAGE_GATE record, or, if the network was re-authorized, {REDRAFT_HINT}"
        raise ValueError(f"mapping network is {label} {network[field]}, but STAGE_GATE names {anchor}; {hint}")
    gate_result = check_task(root, "boolean_dynamics_modeler", first_line)
    if key == "upstream_session_id":
        manifest = json.loads(checked_artifact(root, gate_result["artifact"]).read_text())
        approved = (manifest.get("bnet_file") or {}).get("sha256")
        what = "the BNET of the approved NeKo handoff"
    else:
        approved = gate_result["artifact"]["sha256"]
        what = f"the BNET of standalone approval {anchor}"
    if network.get("sha256") != approved:
        raise ValueError(f"mapping network {network.get('bnet')} (sha256 {str(network.get('sha256'))[:12]}) is not "
                         f"{what} (sha256 {str(approved)[:12]}); {REDRAFT_HINT}")
    return key, anchor


def check_mapping_outputs(data, first_line):
    """Every readout node of the mapping (so every node a criterion reads) must be a STAGE_GATE output.

    The STAGE_GATE `outputs` are the output nodes the researcher approved for this MaBoSS
    stage (NeKo upstream or standalone); a data mapping cannot add to them.
    """
    from scripts.data import mapping
    if not first_line.startswith("STAGE_GATE "):
        raise ValueError("a MaBoSS DATA_INPUT needs a line-1 STAGE_GATE naming its approved outputs")
    outputs = _strict_object(first_line[len("STAGE_GATE "):], "STAGE_GATE").get("outputs")
    if not isinstance(outputs, list) or any(not isinstance(x, str) for x in outputs):
        raise ValueError("STAGE_GATE outputs must be a list of node names")
    missing = [node for node in mapping.readout_nodes(data) if node not in outputs]
    if missing:
        raise ValueError(f"mapping readout node(s) {', '.join(missing)} are not among the STAGE_GATE outputs "
                         f"({', '.join(outputs)}) approved for this MaBoSS stage; a mapping may read only approved "
                         "output nodes: change its readouts and re-approve it, or have those outputs approved")


def _check_neko_seeds_input(root, value):
    """The seeds form: an approved runs/orchestrator/<p>/seeds.json built from researcher sources."""
    rel, digest, approval_id = value["seeds"], value["sha256"], value["approval"]
    if not all(isinstance(v, str) and v for v in (rel, digest, approval_id)):
        raise ValueError("DATA_INPUT seeds, sha256 and approval must be nonempty strings")
    if not SHA256.fullmatch(digest):
        raise ValueError("DATA_INPUT seeds needs a lowercase hex sha256")
    from scripts.data import seeds
    purpose = seeds.purpose(rel)
    record = approvals.require_seeds_approval(root, approval_id, rel, digest)
    validate_record(root, record)
    if record.get("session_id") != purpose:
        raise ValueError(f"seeds approval {approval_id} has session_id {record.get('session_id')!r}; a data_seeds "
                         f"approval's session_id is the purpose directory {purpose}")
    # Check exactly the approved bytes: one read gives both the parsed file and its hash.
    data, actual, errors = seeds.load_and_check(root, rel)
    if actual != digest:
        raise ValueError(f"{rel} changed since approval {approval_id} (sha256 mismatch)")
    if errors:
        raise ValueError(f"seeds check failed for {rel}: " + "; ".join(errors))
    symbols = seeds.symbols(data)
    if set(record["outputs"]) != set(symbols) or len(record["outputs"]) != len(symbols):
        raise ValueError(f"seeds approval {approval_id} outputs {sorted(record['outputs'])} must equal the "
                         f"seed symbols {sorted(symbols)}")
    return {"seeds": rel, "sha256": digest, "approval_id": approval_id, "symbols": symbols}, set()


def _check_maboss_data_input(root, value, first_line):
    if set(value) != {"mapping", "sha256", "approval"}:
        raise ValueError("boolean-dynamics-modeler DATA_INPUT requires exactly mapping, sha256 and approval")
    rel, digest, approval_id = value["mapping"], value["sha256"], value["approval"]
    if not all(isinstance(v, str) and v for v in (rel, digest, approval_id)):
        raise ValueError("DATA_INPUT mapping, sha256 and approval must be nonempty strings")
    key, anchor = _gate_anchor(first_line)
    record = approvals.require_mapping_approval(root, approval_id, rel, digest)
    validate_record(root, record)
    from scripts.data import mapping
    # Check exactly the approved bytes: one read gives both the parsed mapping and its hash.
    data, actual, errors = mapping.load_and_check(root, rel)
    if actual != digest:
        raise ValueError(f"{rel} changed since approval {approval_id} (sha256 mismatch)")
    if errors:
        raise ValueError(f"mapping check failed for {rel}: " + "; ".join(errors))
    # The mapping's network must be the BNET the STAGE_GATE approved: the NeKo handoff's, or the
    # standalone approval's artifact.
    check_mapping_network(root, data["inputs"]["network"], first_line)
    check_mapping_outputs(data, first_line)
    if record.get("session_id") != anchor:
        raise ValueError(f"mapping approval {approval_id} has session_id {record.get('session_id')!r}; a data_mapping "
                         f"approval's session_id is the mapping's network anchor (the NeKo session, or the "
                         f"standalone_decision_id), which STAGE_GATE names as {anchor}")
    ids = ({item.get("id") for item in data["perturbations"]}
           | {item.get("criterion_id") for item in data["criteria"]})
    if set(record["outputs"]) != ids:
        raise ValueError(f"mapping approval {approval_id} outputs {sorted(record['outputs'])} must equal the "
                         f"mapping's perturbation and criterion ids {sorted(ids, key=str)}")
    sessions = {item["session"] for item in data["inputs"]["data"]}
    anchor_key = "neko_session" if key == "upstream_session_id" else "standalone_decision_id"
    return {"mapping": rel, "sha256": digest, "approval_id": approval_id, anchor_key: anchor}, sessions


CALIBRATION_REL = re.compile(r"runs/orchestrator/[\w.-]+/jobs/[\w.-]+/calibration_proposal\.json")
ODE_CALIBRATION_KEYS = {"calibration", "sha256", "approval", "record_session_id", "accept"}


def _check_ode_calibration_input(root, value):
    """The ode-modeler form: an approved calibration_proposal.json applied to its own ODE session.

    {"calibration", "sha256", "approval", "record_session_id", "accept"}: record_session_id must be the
    proposal's ODE session (= the approval's session_id) and `accept` (unique, nonempty) exactly the
    approval's outputs, so the modeler takes the session and the parameter names only from this verified line.
    The proposal must come from its job: the job.json beside it has the proposal's job_sha256, job id
    (= the folder name) and mapping, an approved ode_mapping of the same ODE session
    (approvals.require_proposal_job, run by require_calibration_approval for the gate and score_holdout alike).
    """
    if set(value) != ODE_CALIBRATION_KEYS:
        raise ValueError("ode-modeler DATA_INPUT requires exactly calibration, sha256, approval, record_session_id "
                         f"and accept; got {sorted(value)}")
    rel, digest, approval_id = value["calibration"], value["sha256"], value["approval"]
    session, accept = value["record_session_id"], value["accept"]
    if not all(isinstance(v, str) and v for v in (rel, digest, approval_id)):
        raise ValueError("DATA_INPUT calibration, sha256 and approval must be nonempty strings")
    if not isinstance(session, str) or not session:
        raise ValueError("DATA_INPUT record_session_id must be a nonempty string (the proposal's ODE session)")
    if (not isinstance(accept, list) or not accept or not all(isinstance(a, str) and a for a in accept)
            or len(accept) != len(set(accept))):
        raise ValueError("DATA_INPUT accept must be a nonempty list of unique parameter names (the approval's "
                         f"outputs); got {accept!r}")
    if not SHA256.fullmatch(digest):
        raise ValueError("DATA_INPUT calibration needs a lowercase hex sha256")
    if not CALIBRATION_REL.fullmatch(rel):
        raise ValueError("DATA_INPUT calibration must be runs/orchestrator/<p>/jobs/<id>/calibration_proposal.json")
    # Every approval and proposal <-> job check (shared with score_holdout): approvals.require_calibration_approval.
    record, proposal = approvals.require_calibration_approval(root, approval_id, rel, digest)
    ode_session = proposal["model"]["ode_session"]
    if session != ode_session:
        raise ValueError(f"DATA_INPUT record_session_id {session!r} must be the proposal's ODE session "
                         f"{ode_session} (the session of approval {approval_id})")
    if set(accept) != set(record["outputs"]):
        raise ValueError(f"DATA_INPUT accept {sorted(accept)} must equal the outputs of approval {approval_id} "
                         f"{sorted(record['outputs'])}")
    return {"calibration": rel, "sha256": digest, "approval_id": approval_id, "ode_session": ode_session,
            "outputs": list(accept)}, set()


PHYSICELL_FORMS = {"design": {"design", "sha256", "approval", "record_session_id"},
                   "projection": {"projection", "sha256", "approval", "record_session_id"}}


def _maboss_sessions(design):
    """The MaBoSS sessions (runs/boolean-dynamics-modeler/<session>/...) of a checked design's handoffs."""
    return sorted({PurePosixPath(item["maboss"]["handoff"]).parts[2] for item in design["intracellular"]})


def _check_design_upstream(root, design, first_line):
    """Bind a checked design's MaBoSS handoffs to the line-1 STAGE_GATE of the configurator brief.

    upstream_session_id: the design imports at least one handoff and every handoff lives under
    runs/boolean-dynamics-modeler/<that session>/ (the design check already proved each one an exported,
    reviewed and approved maboss-to-physicell handoff of its session, with gate.check). A design mixing several
    MaBoSS sessions is refused, naming them. standalone_decision_id: the design imports no MaBoSS handoff.
    check_task resolves the STAGE_GATE (it runs again, unchanged, in the dispatch hook).
    """
    gate_result = check_task(root, "multicellular_configurator", first_line)
    sessions = _maboss_sessions(design)
    if "standalone_decision_id" in gate_result:
        if sessions:
            raise ValueError(f"STAGE_GATE names standalone_decision_id {gate_result['standalone_decision_id']}, so the "
                             f"design may import no MaBoSS handoff, but it imports handoffs of MaBoSS session(s) "
                             f"{', '.join(sessions)}; dispatch it with that MaBoSS session's upstream_session_id STAGE_GATE")
        return
    anchor = gate_result["upstream_session_id"]
    if not sessions:
        raise ValueError(f"STAGE_GATE names MaBoSS upstream_session_id {anchor}, but the design imports no MaBoSS "
                         "handoff; a design without MaBoSS models is dispatched with a standalone_decision_id STAGE_GATE")
    if sessions != [anchor]:
        raise ValueError(f"design MaBoSS handoffs come from session(s) {', '.join(sessions)}, but STAGE_GATE names "
                         f"upstream_session_id {anchor}; every design handoff must be under "
                         f"runs/boolean-dynamics-modeler/{anchor}/")


def _check_physicell_input(root, value, first_line):
    """The multicellular-configurator forms: an approved stage-A design or an approved stage-B projection.

    {"design", "sha256", "approval", "record_session_id"}: the physicell_design.json is re-checked
    (physicell_design.load_and_check, no problem allowed) on exactly the approved bytes, its physicell_design
    approval binds path and sha256, record_session_id is the approval's session_id (the design's anchor: the basis
    capture's session, else its purpose <p>), and its MaBoSS handoffs are the STAGE_GATE upstream's
    (_check_design_upstream). The stage-A run is bound to the design later by this very line (lineage.py).
    {"projection", "sha256", "approval", "record_session_id"}: the physicell_projection.json is re-checked
    (physicell_projection.load_and_check: capture, dictionary, the design and its approval), its
    physicell_projection approval binds path and sha256, and record_session_id is the approval's session_id and
    the projection's capture session; stage B replays the projection's design, so its MaBoSS handoffs are bound to the
    STAGE_GATE upstream exactly as for the design form (_check_design_upstream on the re-checked design).
    record_session_id is a lineage anchor, never a live session: the configurator starts a new PhysiCell session,
    builds the design (loading a design's basis XML, if any; for a projection, replaying the projection's design)
    and never passes record_session_id to a tool.
    The check runs before the approval is trusted: the approval's session derivation assumes a checked file.
    """
    form = next((name for name, keys in PHYSICELL_FORMS.items() if set(value) == keys), None)
    if form is None:
        raise ValueError("multicellular-configurator DATA_INPUT requires exactly design, sha256, approval and "
                         "record_session_id, or exactly projection, sha256, approval and record_session_id; "
                         f"got {sorted(value)}")
    rel, digest, approval_id, session = value[form], value["sha256"], value["approval"], value["record_session_id"]
    if not all(isinstance(v, str) and v for v in (rel, digest, approval_id)):
        raise ValueError(f"DATA_INPUT {form}, sha256 and approval must be nonempty strings")
    if not isinstance(session, str) or not session:
        raise ValueError("DATA_INPUT record_session_id must be a nonempty string (the approved file's PhysiCell "
                         "session anchor; the configurator always starts a new session)")
    if not SHA256.fullmatch(digest):
        raise ValueError(f"DATA_INPUT {form} needs a lowercase hex sha256")
    if form == "design":
        from scripts.physicell import physicell_design as module
        require = approvals.require_physicell_design_approval
    else:
        from scripts.physicell import physicell_projection as module
        require = approvals.require_physicell_projection_approval
    # Check exactly the approved bytes: one read gives both the parsed file and its hash.
    data, actual, errors = module.load_and_check(root, rel)
    if actual != digest:
        raise ValueError(f"{rel} has sha256 {actual[:12]}, not the DATA_INPUT sha256 {digest[:12]} "
                         f"(changed since approval {approval_id}?)")
    if errors:
        raise ValueError(f"{form} check failed for {rel}: " + "; ".join(errors))
    record = require(root, approval_id, rel, digest)  # runs validate_record
    if session != record.get("session_id"):
        raise ValueError(f"DATA_INPUT record_session_id {session!r} must be the session_id "
                         f"{record.get('session_id')} of approval {approval_id}")
    if form == "design":
        _check_design_upstream(root, data, first_line)
    else:
        capture = PurePosixPath(data["capture"]["path"]).parts[2]
        if session != capture:
            raise ValueError(f"DATA_INPUT record_session_id {session!r} must be the session {capture} of the "
                             f"projection's capture {data['capture']['path']}")
        # Stage B replays the projection's design, importing its MaBoSS handoffs: they must be the STAGE_GATE
        # upstream's, exactly as for the design form. The design is the one the projection check just re-checked
        # (same path, same sha256).
        from scripts.physicell import physicell_design
        design, design_sha = physicell_design._load(root, data["design"]["path"])
        if design_sha != data["design"]["sha256"]:
            raise ValueError(f"design {data['design']['path']} changed since the projection check")
        _check_design_upstream(root, design, first_line)
    return {form: rel, "sha256": digest, "approval_id": approval_id, "physicell_session": session}, set()


def _check_ode_session_mentions(root, text, session):
    """An ode-modeler brief with a verified calibration DATA_INPUT may not name any other existing BioMASS
    session (a directory name under runs/ode-modeler/) anywhere, in any case: the modeler applies the approved
    values only to the payload's record_session_id. No free-text value parsing: only the names of existing
    sessions are matched, as whole tokens (a longer token such as `<name>-2` or `<name>s` is not that session);
    other prose about values or sessions is left to the modeler, which takes them only from the DATA_INPUT line."""
    base = Path(root) / "runs" / "ode-modeler"
    if not base.is_dir():
        return
    folded = text.casefold()
    with os.scandir(base) as entries:
        names = sorted(e.name for e in entries if e.is_dir(follow_symlinks=False) and not e.name.startswith("."))
    for name in names:
        if name == session:
            continue
        if re.search(r"(?<![\w.-])" + re.escape(name.casefold()) + r"(?![\w-]|\.\w)", folded):
            raise ValueError(f"brief names BioMASS session {name} besides the DATA_INPUT record_session_id {session}; "
                             "an approved calibration is applied only to its record_session_id, so the brief may "
                             "name no other ODE session")


# Model input files (seeds, mapping, ode_mapping, calibration proposal, PhysiCell design, projection
# and dictionary) reach a specialist only as the DATA_INPUT artifact. Every runs/orchestrator path
# the brief mentions (any separator or case, after the detection normalisation) is resolved in the
# repository: a regular file, or each regular file directly inside a mentioned directory under
# runs/orchestrator (a glob names its directory; never recursive), whose content is a model input
# file (_model_input_kind), whatever its name, must be exactly the artifact the DATA_INPUT line names.
ORCHESTRATOR_START = re.compile(r"runs[\\/]+orchestrator(?![\w-])", re.I)
PATH_TOKEN = re.compile(r"[^\s`'\"<>|]+")
GLOB = re.compile(r"[*?\[]")
JSON_END = re.compile(r"\.json", re.I)
LINE_BREAK = re.compile(r"[\n\r\x0b\x0c\x1c-\x1e\x85  ]")
TRAILING = ".,;:!?)]}>'\"`"
SPACED_CANDIDATES = 5  # line prefixes ending in .json tried per mention (names with spaces)
MENTION_RULE = ("model input files (seeds, mapping, ode_mapping, calibration proposal, PhysiCell design, projection "
                "and dictionary) may be named only on the DATA_INPUT line; see CLAUDE.md")


def _resolve_mention(root, mention):
    """Existing paths inside the repository that a mentioned path names; a component that does not
    exist as spelled matches its case-insensitive spellings."""
    rel = posixpath.normpath(mention.replace("\\", "/"))
    if rel == ".." or rel.startswith("../") or rel.startswith("/"):
        return []
    current = [root]
    for part in rel.split("/"):
        following = []
        for directory in current:
            if os.path.lexists(directory / part):
                following.append(directory / part)
                continue
            try:
                with os.scandir(directory) as entries:
                    following += [directory / e.name for e in entries if e.name.casefold() == part.casefold()]
            except OSError:  # not a directory, or unreadable: names nothing readable
                continue
        current = following[:16]
        if not current:
            return []
    found = []
    for path in current:
        try:
            resolved = path.resolve()
            resolved.relative_to(root)
        except (OSError, ValueError):
            continue
        if resolved.exists():
            found.append(resolved)
    return found


def _orchestrator_mentions(root, text, cache=None):
    """(mention, existing paths) for each `runs/orchestrator` mention in the text.

    The mention is the unbroken token (trailing punctuation stripped; a glob cut back to its
    directory). Unless the token names a file, at most SPACED_CANDIDATES line prefixes ending
    in .json are tried too, stopping at the first that names something (names with spaces).
    ``cache`` ({mention: paths}) is shared with the caller's other resolutions of the same brief.
    """
    cache = {} if cache is None else cache

    def resolve(mention):
        if mention not in cache:
            cache[mention] = _resolve_mention(root, mention)
        return cache[mention]

    for start in ORCHESTRATOR_START.finditer(text):
        at = start.start()
        token = PATH_TOKEN.match(text, at).group()
        glob = GLOB.search(token)
        if glob:
            token = token[:glob.start()]
            token = token[:max(token.rfind("/"), token.rfind("\\")) + 1]
        token = token.rstrip(TRAILING)
        paths = resolve(token)
        yield token, paths
        if any(path.is_file() for path in paths):
            continue
        brk = LINE_BREAK.search(text, at)
        for n, end in enumerate(JSON_END.finditer(text, at, brk.start() if brk else len(text))):
            if n == SPACED_CANDIDATES:
                break
            candidate = text[at:end.end()]
            if candidate != token and resolve(candidate):
                yield candidate, resolve(candidate)
                break


def _candidate_files(root, path):
    """The file itself, or the regular files directly inside a directory under runs/orchestrator."""
    if path.is_file():
        return [path]
    if not path.is_dir() or [p.casefold() for p in path.relative_to(root).parts[:2]] != ["runs", "orchestrator"]:
        return []
    files = []
    try:
        with os.scandir(path) as entries:
            children = [Path(e.path) for e in entries]
    except OSError:  # unreadable directory: names nothing readable
        return []
    for child in sorted(children):
        try:
            resolved = child.resolve()
            resolved.relative_to(root)
        except (OSError, ValueError):
            continue
        if resolved.is_file():
            files.append(resolved)
    return files


def _is_seeds(data):
    """A seeds.json shape: schema_version with seeds, or seeds that are gene names (strings or
    objects with hgnc_symbol). A list of numbers (e.g. RNG seeds) is not."""
    if "seeds" not in data:
        return False
    seeds = data["seeds"]
    if "schema_version" in data or isinstance(seeds, str):
        return True
    return isinstance(seeds, list) and any(
        isinstance(x, str) or (isinstance(x, dict) and "hgnc_symbol" in x) for x in seeds)


MODEL_INPUT_KINDS = ("ode_mapping", "calibration_proposal", "physicell_design", "physicell_projection",
                     "physicell_dictionary")


def _model_input_kind(path):
    """"seeds", "mapping" or one of MODEL_INPUT_KINDS when the file's content has that schema's shape, else None.

    Unreadable or non-JSON files are neither; any other failure propagates (fail closed).
    """
    from scripts.data import mapping
    try:
        data = json.loads(path.read_bytes())
    except (OSError, ValueError):  # includes UnicodeDecodeError and JSONDecodeError
        return None
    if not isinstance(data, dict):
        return None
    if data.get("kind") in MODEL_INPUT_KINDS:
        return data["kind"]
    if _is_seeds(data):
        return "seeds"
    if len(set(data) & (set(mapping.KEYS) - {"schema_version"})) >= 2:
        return "mapping"
    return None


def _check_orchestrator_mentions(root, mentions, allowed_rel):
    """Refuse a seeds or mapping file among the resolved mentions unless it is the DATA_INPUT artifact."""
    allowed = None
    if allowed_rel:
        info = os.stat(root / allowed_rel)
        allowed = (info.st_dev, info.st_ino)
    checked = set()
    for mention, paths in mentions:
        for path in (f for p in paths for f in _candidate_files(root, p)):
            info = path.stat()
            key = (info.st_dev, info.st_ino)
            if key == allowed or key in checked:
                continue
            checked.add(key)
            kind = _model_input_kind(path)
            if kind:
                where = "is not the artifact of its DATA_INPUT line" if allowed_rel else "has no DATA_INPUT line"
                raise ValueError(f"brief mentions {kind} file {path.relative_to(root).as_posix()} (as {mention!r}) "
                                 f"but {where}; {MENTION_RULE}")


# Calibration job folders (runs/orchestrator/<p>/jobs/<job-id>/: report.md, holdout_score.json, figures,
# bundle, ...) and returned results (inputs/calibration-results/) never reach a specialist: values reach the
# ode-modeler only as the approved proposal on its DATA_INPUT line. Any spelling of such a path (case,
# separators, globs) is refused, whether or not it exists, and so is any runs/orchestrator mention that
# resolves into one; the exception is the DATA_INPUT calibration proposal itself and its own job folder.
JOB_MENTION = re.compile(r"runs[\\/]+orchestrator[\\/]+[^\s\\/`'\"<>|]+[\\/]+jobs(?![\w.-])", re.I)
RESULTS_MENTION = re.compile(r"inputs[\\/]+(?:\.[\\/]+)*calibration[\s_-]*results", re.I)
JOB_RULE = ("calibration job folders (runs/orchestrator/<p>/jobs/) and returned results (inputs/calibration-results/) "
            "reach a specialist only as the approved calibration proposal on the ode-modeler DATA_INPUT line; "
            "see CLAUDE.md")


def _job_area(root, path):
    """True for a path inside runs/orchestrator/<p>/jobs or inputs/calibration-results (after resolving)."""
    try:
        parts = [p.casefold() for p in path.relative_to(root).parts]
    except ValueError:
        return False
    return (parts[:2] == ["runs", "orchestrator"] and len(parts) >= 4 and parts[3] == "jobs") \
        or parts[:2] == ["inputs", "calibration-results"]


def _check_mentions(root, text, artifact_rel, model_inputs=True):
    """One scan of the (normalised) brief: every runs/orchestrator mention is resolved once (shared cache).

    Job rule (every gated specialist): no calibration job file or returned result, except the ode-modeler's
    verified DATA_INPUT proposal (`artifact_rel` when it is a calibration_proposal.json) and its own job folder.
    Model-input rule (`model_inputs`, the DATA_INPUT roles): no seeds, mapping, ode_mapping, calibration
    proposal or PhysiCell design, projection or dictionary file other than `artifact_rel`
    (see _check_orchestrator_mentions).
    """
    root = Path(root).resolve()
    allowed = set()
    if isinstance(artifact_rel, str) and CALIBRATION_REL.fullmatch(artifact_rel) and (root / artifact_rel).is_file():
        proposal = (root / artifact_rel).resolve()
        allowed = {proposal, proposal.parent}
    found = RESULTS_MENTION.search(text)
    if found:
        token = PATH_TOKEN.match(text, found.start()).group().rstrip(TRAILING)
        raise ValueError(f"brief mentions calibration job results {token!r}; {JOB_RULE}")
    cache = {}
    for found in JOB_MENTION.finditer(text):
        token = PATH_TOKEN.match(text, found.start()).group().rstrip(TRAILING)
        if token not in cache:
            cache[token] = _resolve_mention(root, token)
        if not cache[token] or not set(cache[token]) <= allowed:
            raise ValueError(f"brief mentions calibration job path {token!r}; {JOB_RULE}")
    mentions = list(_orchestrator_mentions(root, text, cache))
    for mention, paths in mentions:
        for path in paths:
            if path not in allowed and _job_area(root, path):
                raise ValueError(f"brief mentions {mention!r}, which is inside a calibration job folder or the "
                                 f"returned results ({path.relative_to(root).as_posix()}); {JOB_RULE}")
    if model_inputs:
        _check_orchestrator_mentions(root, mentions, artifact_rel)


def check_job_mentions(root, specialist, task):
    """The job-file rule for a gated specialist without a DATA_INPUT line (the dispatch hook runs it for those;
    check_data_input runs the same rule, once, for the DATA_INPUT roles after verifying their line)."""
    if specialist in DATA_INPUT_LINE:
        raise ValueError(f"{specialist} takes DATA_INPUT: its job-file rule runs inside check_data_input")
    if task is None:
        task = ""
    if not isinstance(task, str):
        raise ValueError("dispatch prompt must be a string")
    _check_mentions(root, _detection_text(task), None, model_inputs=False)


def _quote(line, match):
    """At most QUOTE_CHARS characters of the line around the match, with ... where cut."""
    start = max(0, min(match.start() - 20, len(line) - QUOTE_CHARS))
    end = min(len(line), start + QUOTE_CHARS)
    return ("..." if start else "") + line[start:end] + ("..." if end < len(line) else "")


def _payload_mentions(lines, canonical):
    """(1-based line, quote) of each payload-like DATA_INPUT mention outside the canonical line.

    ``canonical`` is the 0-based index of the canonical line, or None. Its leading
    `DATA_INPUT ` introduces the payload; any further mention on that line counts.
    """
    normalised = [_detection_text(raw) for raw in lines]

    def next_starts_object(number):
        for following in normalised[number + 1:]:
            rest = following[MARKUP.match(following).end():]
            if rest:  # the first line with content beyond markup (code fences, quotes, `>`)
                return rest.startswith("{")
        return False

    found = []
    for number, line in enumerate(normalised):
        for match in DATA_INPUT_MENTION.finditer(line):
            if number == canonical and match.start() == 0:
                continue
            if (number == canonical or PAYLOAD_PREFIX.fullmatch(line, 0, match.start())
                    or PAYLOAD_SUFFIX.match(line, match.end())
                    or (LINE_END.fullmatch(line, match.end()) and next_starts_object(number))):
                found.append((number + 1, _quote(line, match)))
    return found


def check_data_input(root, specialist, task):
    """Check the DATA_INPUT line by which the network-curator, boolean-dynamics-modeler, ode-modeler and
    multicellular-configurator take data.

    Data reach these specialists only as approved, hash-checked artifacts named on a
    literal DATA_INPUT line (line 1 for network-curator and ode-modeler, line 2 after STAGE_GATE for
    boolean-dynamics-modeler and multicellular-configurator); data are never pasted or pointed at in prose. Only that
    line may carry a DATA_INPUT payload: a payload-like mention elsewhere (one starting
    its line, or with `{` within 16 characters or on the next non-blank line) is refused
    with its line number and a quote, while a
    prose mention ("as defined in the DATA_INPUT mapping") is allowed. Returns None when
    the prompt has no DATA_INPUT line and no data-analyst path, data session id or model
    input file (seeds, mapping, ode_mapping, calibration proposal, PhysiCell design, projection
    or dictionary, recognised by content: _model_input_kind); a prose-only mention carries no data.

    network-curator: {"data_session", "proposals"} (approved seed_genes proposals) or
    {"seeds", "sha256", "approval"} (an approved data_seeds seeds.json from user sources).
    boolean-dynamics-modeler: {"mapping", "sha256", "approval"}, bound to the STAGE_GATE
    upstream_session_id (NeKo export) or standalone_decision_id (researcher BNET).
    ode-modeler: {"calibration", "sha256", "approval", "record_session_id", "accept"} (see
    _check_ode_calibration_input); the brief may name no existing BioMASS session other than record_session_id
    (_check_ode_session_mentions). Other prose is not parsed for values: the modeler takes the session and the
    accepted names only from the verified line and ignores prose (ode-modeler.md).
    multicellular-configurator: {"design", "sha256", "approval", "record_session_id"} (an approved stage-A
    physicell_design.json) or {"projection", "sha256", "approval", "record_session_id"} (an approved stage-B
    physicell_projection.json); see _check_physicell_input.

    The brief's runs/orchestrator mentions are then scanned once (_check_mentions): the job-file rule and the
    model-input rule, with only the verified artifact exempt.
    """
    if specialist not in DATA_INPUT_LINE:
        return None
    if task is None:
        task = ""
    if not isinstance(task, str):
        raise ValueError("dispatch prompt must be a string")
    lines = task.splitlines()  # the same line split as check_task
    expected, placement = DATA_INPUT_LINE[specialist]
    text = _detection_text(task)
    canonical = expected if len(lines) > expected and lines[expected].startswith("DATA_INPUT ") else None
    payloads = _payload_mentions(lines, canonical) if DATA_INPUT_MENTION.search(text) else []
    if payloads:
        number, quote = payloads[0]
        where = ("outside the canonical DATA_INPUT line" if canonical is not None
                 else "but there is no canonical DATA_INPUT line")
        raise ValueError(f'{placement}; line {number}: "{quote}" is a DATA_INPUT payload {where}. '
                         "Only that line may carry a DATA_INPUT payload; a prose mention must not "
                         "start a line or have `{` soon after it")
    if canonical is None:
        if DATA_PATH.search(text) or DATA_SESSION_ID.search(text):
            raise ValueError(DATA_REFUSAL)
        _check_mentions(root, text, None)
        return None
    value = _strict_object(lines[expected][len("DATA_INPUT "):], "DATA_INPUT")
    if specialist == "network_curator":
        result, sessions = _check_neko_data_input(root, value)
    elif specialist == "ode_modeler":
        result, sessions = _check_ode_calibration_input(root, value)
        _check_ode_session_mentions(root, text, result["ode_session"])
    elif specialist == "multicellular_configurator":
        result, sessions = _check_physicell_input(root, value, lines[0])
    else:
        result, sessions = _check_maboss_data_input(root, value, lines[0])
    other = sorted(_data_sessions_named(text) - sessions)
    if other:
        raise ValueError(f"brief names data-analyst session(s) {', '.join(other)} outside its DATA_INPUT")
    # The verified artifact (none for the NeKo proposals form, whose files live under runs/data-analyst).
    artifact = next((result[key] for key in ("seeds", "mapping", "calibration", "design", "projection")
                     if key in result), None)
    _check_mentions(root, text, artifact)
    return result


def resolve_condition(root, decision_id):
    from scripts.evidence_contract import report_identity, validate_footer
    matches = [r for r in records((root / "DECISIONS.md").read_text()) if r.get("id") == decision_id]
    if len(matches) != 1 or matches[0].get("kind") != "conditional_approval":
        raise ValueError("missing unique conditional approval")
    record = matches[0]
    validate_record(root, record)
    report = checked_artifact(root, record["evidence"])
    _, source, target, _, _ = report_identity(root.resolve(), report.resolve())
    footer = validate_footer(report.read_text(), source, target, record["session_id"])
    if footer["neko_session_id"] != record["session_id"]:
        raise ValueError("conditional evidence session mismatch")
    result = all(footer.get(field) in values for field, values in record["conditions"].items())
    return {"decision_id": decision_id, "satisfied": result, "evidence": record["evidence"], "supporting_pmids": footer["supporting_pmids"], "mutation_performed": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    sub = parser.add_subparsers(dest="command", required=True)
    checker = sub.add_parser("check")
    checker.add_argument("downstream_stage", choices=list(UPSTREAM))
    checker.add_argument("upstream_session")
    checker.add_argument("--outputs", nargs="+", required=True)
    condition = sub.add_parser("resolve")
    condition.add_argument("decision_id")
    args = parser.parse_args()
    try:
        result = check(args.root, args.downstream_stage, args.upstream_session, args.outputs) if args.command == "check" else resolve_condition(args.root, args.decision_id)
        print(json.dumps(result, indent=2))
        return 0 if result.get("passed", result.get("satisfied")) else 1
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print(json.dumps({"passed": False, "reason": str(exc)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
