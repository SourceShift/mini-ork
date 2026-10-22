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

``replan`` patches are accepted by :func:`propose` but refused by
:func:`materialize` / :func:`apply` until SDE-6 implements them.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

import yaml

from mini_ork.context import run_context_scope, scoped_environ
from mini_ork.workflow.compiler import WorkflowCompileError, compile_workflow

PATCH_DIRNAME = "graph-patches"
KINDS = ("rerun", "swap_lane", "insert_before", "replace", "replan")
SOURCES = ("orchestrator", "operator", "supervisor")
_ALLOWED_KEYS = frozenset(
    {"kind", "from_node", "node", "lane", "new_node", "reason", "source", "created_at"}
)
_REPLAN_REFUSAL = "replan patches are not applicable yet"
# Edge types that route control (revise target, failure route) rather than
# data; ``insert_before`` leaves them pointing at the original node.
_CONTROL_EDGE_TYPES = frozenset({"retries", "escalates_to"})
_NNN_RE = re.compile(r"^(\d{3})\.yaml$")

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


def materialize(home: str | Path, run_id: str, n: int) -> str:
    """Apply patch ``n`` to its base workflow and compile the result.

    Writes ``NNN.workflow.yaml`` only after the transformed document compiles;
    a compile error (or any rule failure) raises :class:`GraphPatchError` and
    leaves no materialized workflow behind.
    """
    number = int(n)
    patch_dir = _patch_dir(_run_dir(home, run_id))
    patch = _load_patch(patch_dir, number)
    if patch["kind"] == "replan":
        raise GraphPatchError(_REPLAN_REFUSAL)

    base = base_workflow(home, run_id)
    if not base or not Path(base).is_file():
        raise GraphPatchError(
            f"base workflow not found for run {run_id}: {base!r}")
    document = _read_yaml(Path(base))
    if not isinstance(document, dict):
        raise GraphPatchError(f"base workflow {base} is not a mapping")

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
    result: dict[str, Any] = {
        "number": number,
        "workflow": workflow_path,
        "from_node": patch["from_node"],
        "dry_run": bool(dry_run),
        "pid": None,
    }
    if dry_run:
        return result

    argv = [
        sys.executable, "-m", "mini_ork.recovery.planner", str(run_id),
        "--from-node", str(patch["from_node"]),
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
        "from_node": patch["from_node"],
    })
    _update_repair(run_dir, str(patch["from_node"]), number)
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
