"""Typed, immutable run-graph edits applied through ``recover --workflow``.

A *graph patch* is a small YAML file under ``<run_dir>/graph-patches/NNN.yaml``
(``NNN`` = zero-padded three-digit sequence from ``001``). It records one
intended change to a run's workflow — rerun a node, swap a node's lane, insert
a node before another, replace a node, or ask for a replan — together with the
reason and the actor that proposed it. The file is written exactly once
(``O_CREAT|O_EXCL``); materializing it produces ``NNN.workflow.yaml``, the
compiled workflow a detached ``mini_ork.recovery.planner`` revive reads, and
applying it writes ``NNN.applied.json`` and marks ``repair.json`` so the
SDE-4a supervisor labels the revived run's outcome (R2).

Why immutable files rather than a mutable graph in the DB: the patch is the
audit record *and* the input. A revived run reads the same bytes an operator
can inspect, and a later credit pass can join ``NNN.outcome.json`` back to the
patch kind without trusting runtime state.

Public API::

    propose(run_dir, patch) -> int
    base_workflow(home, run_id) -> str
    materialize(home, run_id, n) -> str
    apply(home, run_id, n, *, engine_root, spawn=None, dry_run=False) -> dict
    list_patches(home, run_id) -> list[dict]
    credit(home) -> dict

A ``replan`` patch asks the run's planner lane for a short replacement chain
built only from node templates the base workflow already declares; the chain is
cached once under ``NNN.replan.json`` and grafted in place of the failing node
(SDE-6a).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

import yaml

from mini_ork.context import context_env, run_context_scope, scoped_environ
from mini_ork.dispatch.models import DispatchRequest
from mini_ork.workflow.compiler import WorkflowCompileError, compile_workflow

PATCH_DIRNAME = "graph-patches"
KINDS = ("rerun", "swap_lane", "insert_before", "replace", "replan")
SOURCES = ("orchestrator", "operator", "supervisor")
_ALLOWED_KEYS = frozenset(
    {"kind", "from_node", "node", "lane", "new_node", "reason", "source", "created_at"}
)
# Edge types that route control (revise target, failure route) rather than
# data; ``insert_before`` leaves them pointing at the original node.
_CONTROL_EDGE_TYPES = frozenset({"retries", "escalates_to"})
_NNN_RE = re.compile(r"^(\d{3})\.yaml$")

# ── replan (SDE-6a) ────────────────────────────────────────────────────────

# The supervisor's R6 starts applying ``replan`` patches only when this is true.
REPLAN_SUPPORTED = True

_REPLAN_MAX_NODES = 3
# A replan is one JSON reply with no tool use, so it is bounded like the
# model diagnoser rather than like a 25-minute node dispatch.
_REPLAN_TIMEOUT_S = 180.0
_REPLAN_MAX_TURNS = 3
_REPLAN_DEFAULT_LANE = "glm"
_REPLAN_LOG_LINES = 60
_REPLAN_LOG_TAIL_BYTES = 64 * 1024
_REPLAN_TEMPLATE_KEYS = ("type", "model_lane", "prompt_ref")
# The evidence is in the prompt, so the call needs no tools, MCP servers or
# session; pin them so a leaked node env cannot change the call's shape.
_REPLAN_DISPATCH_ENV = {
    "MO_RESOLVED_NODE_TOOLS": "Read|",
    "MO_MCP_SERVERS": "",
    "MO_RESUME_SESSION_ID": "",
}

Spawn = Callable[[list[str]], "int | None"]


class GraphPatchError(Exception):
    """A patch that cannot be proposed, materialized, or applied as declared."""


# ── paths and small helpers ─────────────────────────────────────────────────


def _run_dir(home: str | Path, run_id: str) -> Path:
    return Path(home) / "runs" / str(run_id)


def _patch_dir(run_dir: str | Path) -> Path:
    return Path(run_dir) / PATCH_DIRNAME


def _patch_files(patch_dir: Path) -> list[tuple[int, Path]]:
    """Every ``NNN.yaml`` patch, ascending by number (materialized workflows
    and sidecars never match — they carry a longer suffix)."""
    found: list[tuple[int, Path]] = []
    if not patch_dir.is_dir():
        return found
    for path in patch_dir.glob("*.yaml"):
        match = _NNN_RE.match(path.name)
        if match:
            found.append((int(match.group(1)), path))
    found.sort(key=lambda item: item[0])
    return found


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _applied_numbers(patch_dir: Path) -> list[int]:
    return [n for n, _ in _patch_files(patch_dir)
            if (patch_dir / f"{n:03d}.applied.json").is_file()]


def _read_yaml(path: Path) -> Any:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise GraphPatchError(f"cannot read {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise GraphPatchError(f"invalid YAML in {path}: {exc}") from exc


def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    """Temp file in the same directory, then ``os.replace`` (readers never see
    a half-written file). Mirrors the supervisor/escalation repair writers."""
    tmp = path.parent / f".{path.name}.tmp.{os.getpid()}.{time.time_ns()}"
    try:
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n",
                       encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


# ── schema ──────────────────────────────────────────────────────────────────


def _require_non_empty_str(patch: Mapping[str, Any], key: str) -> str:
    value = patch.get(key)
    if not isinstance(value, str) or not value.strip():
        raise GraphPatchError(f"graph patch requires a non-empty {key!r}")
    return value


def _validate_new_node(new_node: Any) -> dict[str, Any]:
    if not isinstance(new_node, Mapping):
        raise GraphPatchError("graph patch requires new_node to be a node mapping")
    name = new_node.get("name")
    node_type = new_node.get("type")
    if not isinstance(name, str) or not name.strip():
        raise GraphPatchError("new_node requires a non-empty name")
    if not isinstance(node_type, str) or not node_type.strip():
        raise GraphPatchError("new_node requires a non-empty type")
    return dict(new_node)


def _validate_schema(patch: Mapping[str, Any]) -> dict[str, Any]:
    """Schema check only — never compiles or consults a workflow."""
    if not isinstance(patch, Mapping):
        raise GraphPatchError("graph patch must be a mapping")
    unknown = set(patch) - _ALLOWED_KEYS
    if unknown:
        raise GraphPatchError(
            f"unknown graph patch key(s): {', '.join(sorted(unknown))}")
    kind = patch.get("kind")
    if kind not in KINDS:
        raise GraphPatchError(f"unknown graph patch kind: {kind!r}")
    from_node = _require_non_empty_str(patch, "from_node")
    _require_non_empty_str(patch, "reason")
    source = patch.get("source")
    if source is not None and source not in SOURCES:
        raise GraphPatchError(f"unknown graph patch source: {source!r}")

    if kind == "swap_lane":
        node = _require_non_empty_str(patch, "node")
        _require_non_empty_str(patch, "lane")
        if from_node != node:
            raise GraphPatchError(
                "swap_lane requires from_node to equal node")
    elif kind == "insert_before":
        _require_non_empty_str(patch, "node")
        new_node = _validate_new_node(patch.get("new_node"))
        if from_node != new_node["name"]:
            raise GraphPatchError(
                "insert_before requires from_node to equal new_node.name")
    elif kind == "replace":
        node = _require_non_empty_str(patch, "node")
        new_node = _validate_new_node(patch.get("new_node"))
        if new_node["name"] != node:
            raise GraphPatchError(
                "replace requires new_node.name to equal node")
        if from_node != node:
            raise GraphPatchError("replace requires from_node to equal node")
    # rerun: only the shared from_node/reason checks apply. replan: same.
    return dict(patch)


def _load_patch(patch_dir: Path, n: int) -> dict[str, Any]:
    path = patch_dir / f"{int(n):03d}.yaml"
    if not path.is_file():
        raise GraphPatchError(f"graph patch {int(n):03d} not found in {patch_dir}")
    document = _read_yaml(path)
    if not isinstance(document, Mapping):
        raise GraphPatchError(f"graph patch {path.name} is not a mapping")
    return _validate_schema(document)


# ── propose ────────────────────────────────────────────────────────────────


def propose(run_dir: str | Path, patch: Mapping[str, Any]) -> int:
    """Validate and write the next ``NNN.yaml`` patch; return its number.

    A ``rerun`` or ``replan`` proposal identical in (``kind``, ``from_node``)
    to an existing *not-yet-applied* proposal returns that proposal's number
    instead of writing a second file — the same failure re-observed must not
    fan out into duplicate patches.
    """
    data = _validate_schema(dict(patch))
    patch_dir = _patch_dir(run_dir)
    existing = _patch_files(patch_dir)
    applied = set(_applied_numbers(patch_dir))

    if data["kind"] in ("rerun", "replan"):
        for number, path in existing:
            if number in applied:
                continue
            try:
                document = _read_yaml(path)
            except GraphPatchError:
                continue
            if not isinstance(document, Mapping):
                continue
            if (document.get("kind") == data["kind"]
                    and document.get("from_node") == data["from_node"]):
                return number

    data.setdefault("created_at", _now_iso())
    number = existing[-1][0] + 1 if existing else 1
    patch_dir.mkdir(parents=True, exist_ok=True)
    destination = patch_dir / f"{number:03d}.yaml"
    try:
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError as exc:
        raise GraphPatchError(
            f"graph patch {destination.name} already exists; refusing to rewrite"
        ) from exc
    except OSError as exc:
        raise GraphPatchError(
            f"cannot write graph patch {destination.name}: {exc}") from exc
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(yaml.safe_dump(data, sort_keys=False))
    except OSError as exc:
        raise GraphPatchError(
            f"cannot write graph patch {destination.name}: {exc}") from exc
    return number


# ── base workflow ──────────────────────────────────────────────────────────


def base_workflow(home: str | Path, run_id: str) -> str:
    """The newest applied patch's workflow, else the run's own workflow path.

    The fallback calls ``mini_ork.recovery.planner``'s own resolver (the one
    ``recover`` uses) rather than copying its precedence rules. ``home`` is
    pinned through both env layers so a leaked ``MINI_ORK_RUN_DIR`` from the
    caller's shell cannot steer the resolution to another run.
    """
    patch_dir = _patch_dir(_run_dir(home, run_id))
    applied = _applied_numbers(patch_dir)
    if applied:
        newest = applied[-1]
        return str(patch_dir / f"{newest:03d}.workflow.yaml")

    from mini_ork.recovery import planner as recovery_planner

    overrides = {"MINI_ORK_HOME": str(home), "MINI_ORK_RUN_DIR": None}
    try:
        with scoped_environ(overrides), run_context_scope(overrides):
            _run_dir_resolved, _db, workflow, _recipe = (
                recovery_planner._resolve_default_paths(str(run_id)))
    except Exception as exc:  # noqa: BLE001 — surface as a patch failure
        raise GraphPatchError(
            f"cannot resolve the base workflow for run {run_id}: {exc}") from exc
    return workflow or ""


# ── materialize ────────────────────────────────────────────────────────────


def _nodes_by_name(document: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    raw_nodes = document.get("nodes")
    if not isinstance(raw_nodes, list):
        raise GraphPatchError("base workflow nodes must be a list")
    nodes: dict[str, dict[str, Any]] = {}
    for raw in raw_nodes:
        if isinstance(raw, Mapping) and raw.get("name"):
            nodes[str(raw["name"])] = dict(raw)
    return nodes


def _apply_transform(document: dict[str, Any], patch: Mapping[str, Any]) -> None:
    kind = patch["kind"]
    nodes = _nodes_by_name(document)

    if kind == "rerun":
        if patch["from_node"] not in nodes:
            raise GraphPatchError(
                f"rerun names unknown node {patch['from_node']!r}")
        return

    if kind == "swap_lane":
        name = patch["node"]
        if name not in nodes:
            raise GraphPatchError(f"swap_lane names unknown node {name!r}")
        for raw in document["nodes"]:
            if isinstance(raw, Mapping) and raw.get("name") == name:
                raw["model_lane"] = patch["lane"]
                return
        raise GraphPatchError(f"swap_lane names unknown node {name!r}")

    if kind == "insert_before":
        name = patch["node"]
        new_node = dict(patch["new_node"])
        if name not in nodes:
            raise GraphPatchError(f"insert_before names unknown node {name!r}")
        if new_node["name"] in nodes:
            raise GraphPatchError(
                f"new node {new_node['name']!r} already exists in the workflow")
        raw_nodes = document["nodes"]
        index = next(i for i, raw in enumerate(raw_nodes)
                     if isinstance(raw, Mapping) and raw.get("name") == name)
        raw_nodes.insert(index, new_node)
        raw_edges = document.get("edges") or []
        if not isinstance(raw_edges, list):
            raise GraphPatchError("base workflow edges must be a list")
        redirected: list[Any] = []
        for edge in raw_edges:
            # Only dataflow edges move to the inserted node. A control edge
            # into ``name`` (``retries`` = the revise target, ``escalates_to`` =
            # the failure route) still means ``name`` itself; redirecting it
            # would re-point a reviewer's revise loop at the new node.
            if (isinstance(edge, Mapping) and edge.get("to") == name
                    and edge.get("edge_type") not in _CONTROL_EDGE_TYPES):
                edge = dict(edge)
                edge["to"] = new_node["name"]
            redirected.append(edge)
        redirected.append(
            {"from": new_node["name"], "to": name, "edge_type": "depends_on"})
        document["edges"] = redirected
        return

    if kind == "replace":
        name = patch["node"]
        if name not in nodes:
            raise GraphPatchError(f"replace names unknown node {name!r}")
        new_node = dict(patch["new_node"])
        for index, raw in enumerate(document["nodes"]):
            if isinstance(raw, Mapping) and raw.get("name") == name:
                document["nodes"][index] = new_node
                return
        raise GraphPatchError(f"replace names unknown node {name!r}")

    raise GraphPatchError(f"unknown graph patch kind: {kind!r}")


# ── replan: ask the planner lane for a replacement chain ───────────────────


def _node_triple(node: Mapping[str, Any]) -> tuple[Any, ...]:
    return tuple(node.get(key) for key in _REPLAN_TEMPLATE_KEYS)


def _replan_lane(run_dir: Path) -> str:
    """``MO_REPLAN_LANE``, else the run's planner lane, else ``glm``.

    The run's lane comes from its own ``config/agents.yaml`` snapshot (the
    launch-time copy), not from the home's current policy.
    """
    lane = context_env("MO_REPLAN_LANE", "") or ""
    if lane.strip():
        return lane.strip()
    agents = Path(run_dir) / "config" / "agents.yaml"
    document: Any = {}
    if agents.is_file():
        try:
            document = yaml.safe_load(agents.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            document = {}
    lanes = document.get("lanes") if isinstance(document, Mapping) else None
    planner = lanes.get("planner") if isinstance(lanes, Mapping) else None
    if isinstance(planner, str) and planner.strip():
        return planner.strip()
    return _REPLAN_DEFAULT_LANE


def _tail_lines(path: Path, n: int, max_bytes: int = _REPLAN_LOG_TAIL_BYTES) -> str:
    """The last ``n`` lines of ``path`` from a bounded tail read."""
    try:
        with Path(path).open("rb") as handle:
            handle.seek(0, 2)
            start = max(0, handle.tell() - max_bytes)
            handle.seek(start)
            data = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return ""
    lines = data.splitlines()
    if start and lines:
        lines = lines[1:]
    return "\n".join(lines[-n:])


def _last_repair_attempt(run_dir: Path) -> Mapping[str, Any]:
    path = Path(run_dir) / "repair.json"
    if not path.is_file():
        return {}
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(document, Mapping):
        return {}
    attempts = document.get("attempts")
    if isinstance(attempts, list) and attempts:
        last = attempts[-1]
        if isinstance(last, Mapping):
            return last
    return {}


def _newest_node_log(run_dir: Path, failing: str) -> tuple[str, str]:
    """``(name, last 60 lines)`` of the newest ``*.log`` naming the failing node."""
    candidates = [path for path in Path(run_dir).glob("*.log")
                  if failing and failing in path.name]
    if not candidates:
        return "", ""

    def _mtime(path: Path) -> float:
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0

    newest = max(candidates, key=_mtime)
    return newest.name, _tail_lines(newest, _REPLAN_LOG_LINES)


def _replan_evidence(run_dir: Path, failing: str) -> str:
    """Failure evidence: the last repair attempt, diagnosis.json, log tail."""
    sections: list[str] = []
    attempt = _last_repair_attempt(run_dir)
    reason = attempt.get("reason")
    if reason:
        sections.append(f"last repair attempt reason: {reason}")
    diagnosis = attempt.get("diagnosis")
    if diagnosis is not None:
        sections.append("last repair attempt diagnosis: "
                        + json.dumps(diagnosis, sort_keys=True))
    diagnosis_path = Path(run_dir) / "diagnosis.json"
    if diagnosis_path.is_file():
        try:
            document = json.loads(diagnosis_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            document = None
        if document is not None:
            sections.append("diagnosis.json: "
                            + json.dumps(document, sort_keys=True))
    log_name, log_text = _newest_node_log(run_dir, failing)
    if log_text.strip():
        sections.append(f"=== NODE LOG ({log_name}, last {_REPLAN_LOG_LINES} lines) ===")
        sections.append(log_text)
    return "\n".join(sections)


def _replan_prompt(run_id: str, patch: Mapping[str, Any],
                   base_document: Mapping[str, Any], run_dir: Path) -> str:
    failing = str(patch["from_node"])
    workflow_yaml = yaml.safe_dump(
        {"nodes": base_document.get("nodes"), "edges": base_document.get("edges")},
        sort_keys=False)
    evidence = _replan_evidence(run_dir, failing)
    return (
        "You repair failed mini-ork runs by re-planning one failing step.\n"
        f"Run {run_id} ended at failing node {failing!r}.\n\n"
        "Replace that node with a chain of 1 to 3 nodes that together do its job.\n"
        "Answer with ONE JSON object and nothing else — no prose, no code fence:\n"
        '{"nodes": [{"name": "...", "type": "...", "model_lane": "...", '
        '"prompt_ref": "..."}]}\n\n'
        "Rules:\n"
        "- 1 to 3 nodes, in execution order.\n"
        "- Use ONLY a (type, model_lane, prompt_ref) triple that already appears "
        "on some node of the base workflow below; copy every other key of that "
        "template node.\n"
        "- Give every node a NEW unique name that is absent from the base "
        "workflow.\n\n"
        "=== PATCH REASON ===\n"
        f"{patch.get('reason', '')}\n"
        "=== BASE WORKFLOW ===\n"
        f"{workflow_yaml}"
        "=== FAILURE EVIDENCE ===\n"
        f"{evidence}\n"
    )


def _parse_replan_reply(text: Any) -> dict[str, Any]:
    """The reply must be strict JSON: one object with a ``nodes`` list."""
    raw = str(text or "").strip()
    if not raw:
        raise GraphPatchError("replan reply was empty")
    # Models wrap JSON in a ```json fence even when told not to; a whole-reply
    # fence carries no extra content, so unwrap it rather than fail the patch.
    fenced = re.fullmatch(r"```[a-zA-Z]*\s*\n?(.*?)\n?\s*```", raw, re.DOTALL)
    if fenced:
        raw = fenced.group(1).strip()
    try:
        document = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise GraphPatchError(f"replan reply is not valid JSON: {exc}") from exc
    if not isinstance(document, Mapping):
        raise GraphPatchError("replan reply is not a JSON object")
    return dict(document)


def _validate_replan_chain(document: Mapping[str, Any],
                           base_document: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Validate one planner reply against the template-only contract.

    Returns the chain as template nodes with the reply's names (so every other
    key — artifact declarations included — is copied from the base node).
    """
    raw_nodes = document.get("nodes")
    if not isinstance(raw_nodes, list) or not 1 <= len(raw_nodes) <= _REPLAN_MAX_NODES:
        raise GraphPatchError(
            f"replan reply must carry 1 to {_REPLAN_MAX_NODES} nodes")
    base_nodes = _nodes_by_name(base_document)
    templates: dict[tuple[Any, ...], dict[str, Any]] = {
        _node_triple(node): node for node in base_nodes.values()}
    chain: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_nodes):
        if not isinstance(raw, Mapping):
            raise GraphPatchError(f"replan node {index} is not an object")
        name = raw.get("name")
        if not isinstance(name, str) or not name.strip():
            raise GraphPatchError(f"replan node {index} requires a non-empty name")
        name = name.strip()
        if name in seen:
            raise GraphPatchError(f"replan node name {name!r} is duplicated")
        if name in base_nodes:
            raise GraphPatchError(
                f"replan node name {name!r} already exists in the base workflow")
        template = templates.get(_node_triple(raw))
        if template is None:
            raise GraphPatchError(
                f"replan node {name!r} uses a (type, model_lane, prompt_ref) "
                "triple that no base workflow node declares")
        node = dict(template)
        node["name"] = name
        chain.append(node)
        seen.add(name)
    return chain


def _replan_number(patch_dir: Path, patch: Mapping[str, Any]) -> int:
    """The file number of the on-disk replan patch this mapping came from."""
    for number, path in _patch_files(patch_dir):
        try:
            document = _read_yaml(path)
        except GraphPatchError:
            continue
        if (isinstance(document, Mapping)
                and document.get("kind") == patch.get("kind")
                and document.get("from_node") == patch.get("from_node")):
            return number
    existing = _patch_files(patch_dir)
    return existing[-1][0] + 1 if existing else 1


def _read_replan_cache(path: Path) -> list[dict[str, Any]]:
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise GraphPatchError(f"cannot read replan cache {path}: {exc}") from exc
    except ValueError as exc:
        raise GraphPatchError(f"invalid replan cache {path}: {exc}") from exc
    nodes = document.get("nodes") if isinstance(document, Mapping) else document
    if not isinstance(nodes, list) or not all(
            isinstance(node, Mapping) for node in nodes):
        raise GraphPatchError(f"invalid replan cache {path}: no node list")
    return [dict(node) for node in nodes]


def _store_replan_chain(path: Path, chain: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Write the accepted chain once with ``O_CREAT|O_EXCL``; return what won."""
    payload = json.dumps({"nodes": chain}, indent=2, sort_keys=True) + "\n"
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        # Another materialize won the race: its chain is the deterministic one.
        return _read_replan_cache(path)
    except OSError as exc:
        raise GraphPatchError(f"cannot write replan cache {path}: {exc}") from exc
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
    except OSError as exc:
        try:
            path.unlink()
        except OSError:
            pass
        raise GraphPatchError(f"cannot write replan cache {path}: {exc}") from exc
    return chain


def _replan_chain(home: str | Path, run_id: str, patch: Mapping[str, Any],
                  base_document: Mapping[str, Any], *,
                  dispatch: Callable[[Any], Any] | None = None,
                  number: int | None = None) -> list[dict[str, Any]]:
    """The accepted replacement chain for ``patch``, cached under ``NNN.replan.json``.

    The cache is the determinism guarantee: after the first accepted reply a
    later materialize re-reads it rather than asking the model again, and the
    patch YAML itself is never rewritten.
    """
    run_dir = _run_dir(home, run_id)
    patch_dir = _patch_dir(run_dir)
    if number is None:
        number = _replan_number(patch_dir, patch)
    cache_path = patch_dir / f"{int(number):03d}.replan.json"
    if cache_path.is_file():
        return _read_replan_cache(cache_path)

    failing = patch.get("from_node")
    if not isinstance(failing, str) or not failing.strip():
        raise GraphPatchError("replan patch requires a non-empty from_node")

    lane = _replan_lane(run_dir)
    prompt = _replan_prompt(str(run_id), patch, base_document, run_dir)
    if dispatch is None:
        from mini_ork.dispatch.providers import dispatch_model
        dispatch = dispatch_model
    try:
        # An empty scratch cwd: the call reads the prompt, not a tree.
        with tempfile.TemporaryDirectory(prefix="mo-replan-") as scratch:
            result = dispatch(DispatchRequest(
                model=lane,
                prompt=prompt,
                timeout_s=_REPLAN_TIMEOUT_S,
                max_turns=_REPLAN_MAX_TURNS,
                cwd=scratch,
                env=dict(_REPLAN_DISPATCH_ENV),
            ))
    except Exception as exc:  # noqa: BLE001 — a failed call is a patch failure
        raise GraphPatchError(
            f"replan dispatch failed ({type(exc).__name__}: {exc})") from exc

    if not getattr(result, "ok", False):
        raise GraphPatchError(
            "replan dispatch was not ok: "
            f"{getattr(result, 'error', '') or 'unknown'}")
    document = _parse_replan_reply(getattr(result, "text", ""))
    chain = _validate_replan_chain(document, base_document)
    return _store_replan_chain(cache_path, chain)


def _apply_replan_transform(document: dict[str, Any], failing: str,
                            chain: list[dict[str, Any]]) -> None:
    """Graft ``chain`` in place of ``failing`` and rewire the edges around it."""
    if not chain:
        raise GraphPatchError("replan chain is empty")
    raw_nodes = document.get("nodes")
    if not isinstance(raw_nodes, list):
        raise GraphPatchError("base workflow nodes must be a list")
    index = next(
        (i for i, raw in enumerate(raw_nodes)
         if isinstance(raw, Mapping) and raw.get("name") == failing), None)
    if index is None:
        raise GraphPatchError(f"replan names unknown node {failing!r}")
    existing = {str(raw.get("name")) for raw in raw_nodes
                if isinstance(raw, Mapping) and raw.get("name")}
    for node in chain:
        if node["name"] in existing:
            raise GraphPatchError(
                f"replan node {node['name']!r} already exists in the workflow")

    raw_nodes[index:index + 1] = [dict(node) for node in chain]

    raw_edges = document.get("edges") or []
    if not isinstance(raw_edges, list):
        raise GraphPatchError("base workflow edges must be a list")
    head, tail = chain[0]["name"], chain[-1]["name"]
    rewritten: list[Any] = []
    for edge in raw_edges:
        if isinstance(edge, Mapping):
            edge = dict(edge)
            if edge.get("to") == failing:
                # Dataflow now enters the chain's head; a control edge
                # (retries/escalates_to) means "the node doing the failing
                # node's job", which is the chain's tail.
                edge["to"] = (tail if edge.get("edge_type") in _CONTROL_EDGE_TYPES
                              else head)
            if edge.get("from") == failing:
                edge["from"] = tail
        rewritten.append(edge)
    for left, right in zip(chain, chain[1:]):
        rewritten.append({"from": left["name"], "to": right["name"],
                          "edge_type": "depends_on"})
    document["edges"] = rewritten


def materialize(home: str | Path, run_id: str, n: int) -> str:
    """Apply patch ``n`` to its base workflow and compile the result.

    Writes ``NNN.workflow.yaml`` only after the transformed document compiles;
    a compile error (or any rule failure) raises :class:`GraphPatchError` and
    leaves no materialized workflow behind.
    """
    number = int(n)
    patch_dir = _patch_dir(_run_dir(home, run_id))
    patch = _load_patch(patch_dir, number)

    base = base_workflow(home, run_id)
    if not base or not Path(base).is_file():
        raise GraphPatchError(
            f"base workflow not found for run {run_id}: {base!r}")
    document = _read_yaml(Path(base))
    if not isinstance(document, dict):
        raise GraphPatchError(f"base workflow {base} is not a mapping")

    if patch["kind"] == "replan":
        chain = _replan_chain(home, run_id, patch, document, number=number)
        _apply_replan_transform(document, str(patch["from_node"]), chain)
    else:
        _apply_transform(document, patch)

    destination = patch_dir / f"{number:03d}.workflow.yaml"
    tmp = patch_dir / f".{number:03d}.workflow.yaml.tmp.{os.getpid()}.{time.time_ns()}"
    try:
        tmp.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
        compile_workflow(tmp)
    except WorkflowCompileError as exc:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise GraphPatchError(
            f"graph patch {number:03d} does not compile: {exc}") from exc
    except OSError as exc:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise GraphPatchError(
            f"cannot materialize graph patch {number:03d}: {exc}") from exc
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    os.replace(tmp, destination)
    return str(destination)


# ── apply ──────────────────────────────────────────────────────────────────


def _engine_sha(engine_root: str | Path) -> str:
    try:
        proc = subprocess.run(
            ["git", "-C", str(engine_root), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _live_status(home: Path, run_id: str) -> str:
    """The run's ``task_runs.status`` when it is still live, else ``""``.

    The DB is ``MINI_ORK_DB`` or ``<home>/state.db``; an absent DB, table or row
    reads as not live (a run with no row cannot be holding a lease).
    """
    import sqlite3

    from mini_ork.run_status import LIVE

    db = os.environ.get("MINI_ORK_DB") or str(Path(home) / "state.db")
    if not os.path.isfile(db):
        return ""
    try:
        con = sqlite3.connect(db, timeout=5.0)
        try:
            row = con.execute("SELECT status FROM task_runs WHERE id=?",
                              (run_id,)).fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return ""
    status = str(row[0]) if row and row[0] else ""
    return status if status in LIVE else ""


def _update_repair(run_dir: Path, from_node: str, number: int) -> None:
    """Mark the run ``reviving`` while keeping every other ``repair.json`` key
    (the SDE-4a supervisor keys its R2 outcome label on these fields)."""
    path = run_dir / "repair.json"
    data: dict[str, Any] = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            loaded = None
        if isinstance(loaded, dict):
            data = loaded
    data["state"] = "reviving"
    data["revived_at"] = int(time.time())
    data["revive_from_node"] = from_node
    data["revive_patch"] = f"{int(number):03d}"
    _write_json_atomic(path, data)


def _default_spawn(home: Path, run_id: str, number: int,
                   engine_root: str, run_dir: Path) -> Spawn:
    """The supervisor's detach/env shape, plus ``--workflow`` and a patch log."""

    def _spawn(argv: list[str]) -> int | None:
        env = dict(os.environ)
        env["MINI_ORK_HOME"] = str(home)
        env["MINI_ORK_ROOT"] = str(engine_root)
        env["MO_AUTO_REPAIR"] = "1"
        for key in ("MINI_ORK_RUN_ID", "MINI_ORK_RUN_DIR", "MINI_ORK_PLAN_PATH"):
            env.pop(key, None)
        log_path = run_dir / f"graph-patch-{int(number):03d}.log"
        log_handle = open(log_path, "ab")
        try:
            proc = subprocess.Popen(
                argv,
                cwd=str(engine_root),
                env=env,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
        finally:
            log_handle.close()
        return getattr(proc, "pid", None)

    return _spawn


def apply(home: str | Path, run_id: str, n: int, *, engine_root: str | Path,
          spawn: Spawn | None = None, dry_run: bool = False) -> dict[str, Any]:
    """Materialize patch ``n`` and (unless ``dry_run``) revive the run from it."""
    number = int(n)
    home_path = Path(home)
    run_dir = _run_dir(home_path, run_id)
    patch_dir = _patch_dir(run_dir)
    patch = _load_patch(patch_dir, number)

    applied = _applied_numbers(patch_dir)
    if number in applied:
        raise GraphPatchError(f"graph patch {number:03d} is already applied")
    higher = [number_ for number_ in applied if number_ > number]
    if higher:
        raise GraphPatchError(
            f"graph patch {higher[-1]:03d} is already applied; "
            f"refusing to apply {number:03d}")
    live_status = _live_status(home_path, str(run_id))
    if live_status:
        # A live run owns its lease: the revive would be refused, yet this
        # function would already have marked the run reviving.
        raise GraphPatchError(
            f"run {run_id} is still {live_status}; apply a graph patch only to "
            "a run that has stopped")

    workflow_path = materialize(home, run_id, number)
    # A replan chain replaced the failing node, so the run must revive from the
    # chain's head, which is what the materialized workflow actually contains.
    if patch["kind"] == "replan":
        chain = _read_replan_cache(
            patch_dir / f"{number:03d}.replan.json")
        from_node = str(chain[0]["name"])
    else:
        from_node = str(patch["from_node"])
    result: dict[str, Any] = {
        "number": number,
        "workflow": workflow_path,
        "from_node": from_node,
        "dry_run": bool(dry_run),
        "pid": None,
    }
    if dry_run:
        return result

    argv = [
        sys.executable, "-m", "mini_ork.recovery.planner", str(run_id),
        "--from-node", from_node,
        "--workflow", workflow_path,
        "--force",
    ]
    spawn_fn = spawn or _default_spawn(home_path, str(run_id), number,
                                       str(engine_root), run_dir)
    pid = spawn_fn(argv)
    _write_json_atomic(patch_dir / f"{number:03d}.applied.json", {
        "applied_at": _now_iso(),
        "pid": pid,
        "engine_sha": _engine_sha(engine_root),
        "from_node": from_node,
    })
    _update_repair(run_dir, from_node, number)
    result["pid"] = pid
    return result


# ── list / credit ──────────────────────────────────────────────────────────


def list_patches(home: str | Path, run_id: str) -> list[dict[str, Any]]:
    """Every patch with its number, kind, from_node, reason, status, outcome."""
    patch_dir = _patch_dir(_run_dir(home, run_id))
    entries: list[dict[str, Any]] = []
    for number, path in _patch_files(patch_dir):
        try:
            document = _read_yaml(path)
        except GraphPatchError:
            document = {}
        if not isinstance(document, Mapping):
            document = {}
        outcome = None
        outcome_path = patch_dir / f"{number:03d}.outcome.json"
        if outcome_path.is_file():
            try:
                loaded = json.loads(outcome_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                loaded = None
            if isinstance(loaded, dict):
                outcome = loaded
        entries.append({
            "number": number,
            "kind": document.get("kind"),
            "from_node": document.get("from_node"),
            "reason": document.get("reason"),
            "source": document.get("source"),
            "created_at": document.get("created_at"),
            "status": ("applied"
                       if (patch_dir / f"{number:03d}.applied.json").is_file()
                       else "proposed"),
            "outcome": outcome,
        })
    return entries


def credit(home: str | Path) -> dict[str, dict[str, Any]]:
    """Per-kind outcome rates over every run's ``NNN.outcome.json``.

    ``rerun`` is the control arm (a blind retry). Any other kind is
    ``credited`` only when both it and ``rerun`` have at least five outcomes
    and its pass rate is *strictly* above the control's.
    """
    counts: dict[str, dict[str, int]] = {}
    runs_dir = Path(home) / "runs"
    for patch_dir in sorted(runs_dir.glob(f"*/{PATCH_DIRNAME}")):
        if not patch_dir.is_dir():
            continue
        for number, path in _patch_files(patch_dir):
            try:
                document = _read_yaml(path)
            except GraphPatchError:
                continue
            if not isinstance(document, Mapping):
                continue
            kind = document.get("kind")
            if not isinstance(kind, str) or not kind:
                continue
            entry = counts.setdefault(kind, {"n": 0, "passed": 0})
            outcome_path = patch_dir / f"{number:03d}.outcome.json"
            if not outcome_path.is_file():
                continue
            try:
                outcome = json.loads(outcome_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(outcome, Mapping) or "passed" not in outcome:
                continue
            entry["n"] += 1
            if outcome.get("passed"):
                entry["passed"] += 1

    rerun = counts.get("rerun", {"n": 0, "passed": 0})
    rerun_rate = rerun["passed"] / rerun["n"] if rerun["n"] else 0.0
    rerun_floor = rerun["n"] >= 5
    result: dict[str, dict[str, Any]] = {}
    for kind, entry in counts.items():
        rate = entry["passed"] / entry["n"] if entry["n"] else 0.0
        result[kind] = {
            "n": entry["n"],
            "passed": entry["passed"],
            "rate": rate,
            "credited": bool(
                kind != "rerun"
                and rerun_floor
                and entry["n"] >= 5
                and rate > rerun_rate
            ),
        }
    return result
