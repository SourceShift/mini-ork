"""Artifact-based completion helpers for LLM nodes.

An LLM node's dispatch reports ``(rc, text)``; that self-report is a handshake,
not the deliverable. When the agent wrote every declared output artifact but the
handshake failed (watchdog rc=124 after the files landed, a malformed final
message), the executor used to fail the node and cascade-skip its dependents.
These helpers recompute the verdict from the artifacts instead, the same
principle the deterministic verifiers already encode.

Everything here is side-effect free except ``write_json_atomic`` and
``normalize_implementer_summary_file``, which rewrite one JSON file atomically.
"""
from __future__ import annotations

import json
import os
import stat
import tempfile
from typing import Any, Iterable, Mapping

import yaml

ARTIFACT_COMPLETION_LOG = "[ok] node completed via artifact check (self-report missing/invalid)"


def _str_entries(value: Any) -> list[str]:
    """Non-empty string entries of ``value`` when it is a list, else []."""
    if not isinstance(value, list):
        return []
    return [entry for entry in value if isinstance(entry, str) and entry]


def normalize_implementer_summary(summary: Mapping[str, Any]) -> dict[str, Any]:
    """Return a copy of ``summary`` in the canonical tier-verifier shape.

    The recursive-validate-impl verifiers gate on ``ready_for_tier1 is True``
    and a non-empty ``touched_files``. Agents (and, until K5, the engine's own
    summary writer) also emit ``files_changed``, so:

    * ``touched_files`` missing/empty → taken from a non-empty ``files_changed``;
      absent with nothing to infer from → ``[]``.
    * ``ready_for_tier1`` that is not a bool → ``True`` only when
      ``touched_files`` is non-empty, else ``False``. An explicit bool from the
      agent is preserved, so a real "not ready" is never masked.

    Every other key is kept. Idempotent on its own output.
    """
    normalized = dict(summary)
    if not _str_entries(normalized.get("touched_files")):
        changed = _str_entries(normalized.get("files_changed"))
        if changed:
            normalized["touched_files"] = changed
        elif "touched_files" not in normalized:
            normalized["touched_files"] = []
    if not isinstance(normalized.get("ready_for_tier1"), bool):
        normalized["ready_for_tier1"] = bool(_str_entries(normalized.get("touched_files")))
    return normalized


def write_json_atomic(path: str, payload: Any) -> None:
    """Write ``payload`` to ``path`` via a same-directory temp file + os.replace,
    so a concurrent verifier never reads a half-written summary."""
    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=f".{os.path.basename(path)}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def normalize_implementer_summary_file(path: str) -> bool:
    """Normalize an on-disk implementer summary in place.

    Rewrites only when normalization changed something. Returns True when the
    file holds a valid, normalized summary; False (file untouched) when it is
    missing, unreadable, not a JSON object, or cannot be rewritten.
    """
    try:
        with open(path, encoding="utf-8") as handle:
            summary = json.load(handle)
    except (OSError, ValueError):
        return False
    if not isinstance(summary, dict):
        return False
    normalized = normalize_implementer_summary(summary)
    if normalized == summary:
        return True
    try:
        write_json_atomic(path, normalized)
    except OSError:
        return False
    return True


def declared_artifacts_ok(paths: Iterable[str], *, since_mtime: float) -> tuple[bool, str]:
    """Check that every declared artifact proves the node delivered.

    Each path must be a regular file, non-empty, modified at or after
    ``since_mtime`` (the dispatch-start marker — so a previous recursion
    iteration's artifacts cannot stand in for this one's), and parse as JSON
    when it ends in ``.json``. Returns ``(ok, reason)``; the reason names the
    first violating path.
    """
    paths = [os.fspath(path) for path in paths]
    if not paths:
        return False, "no_declared_artifacts"
    for path in paths:
        try:
            info = os.stat(path)
        except OSError:
            return False, f"{path}: missing"
        if not stat.S_ISREG(info.st_mode):
            return False, f"{path}: not a regular file"
        if info.st_size <= 0:
            return False, f"{path}: empty"
        if info.st_mtime < since_mtime:
            return False, f"{path}: stale (written before this dispatch started)"
        if path.endswith(".json"):
            try:
                with open(path, encoding="utf-8") as handle:
                    json.load(handle)
            except (OSError, ValueError) as exc:
                return False, f"{path}: invalid JSON ({exc})"
    return True, "ok"


def node_strict_handshake(compiled_workflow: Any, workflow_path: str, node_id: str) -> bool:
    """Whether ``node_id`` opted out of artifact-based completion.

    Reads the compiled node when available; otherwise the raw workflow YAML
    (legacy callers without a compiled graph). Any lookup failure → False.
    """
    nodes = getattr(compiled_workflow, "nodes", None)
    if isinstance(nodes, Mapping) and node_id in nodes:
        return getattr(nodes[node_id], "strict_handshake", False) is True
    if not workflow_path or not os.path.isfile(workflow_path):
        return False
    try:
        with open(workflow_path, encoding="utf-8") as handle:
            document = yaml.safe_load(handle) or {}
    except (OSError, yaml.YAMLError):
        return False
    raw_nodes = document.get("nodes") if isinstance(document, Mapping) else None
    for raw in raw_nodes if isinstance(raw_nodes, list) else []:
        if isinstance(raw, Mapping) and str(raw.get("name") or "").strip() == node_id:
            return raw.get("strict_handshake") is True
    return False
