"""Checkpoint primitives — Python port of lib/checkpoint.sh.

Faithful port of the per-node checkpoint primitives used by the recipe
resume protocol. The bash ``lib/checkpoint.sh`` stays in place
(strangler-fig co-existence); this module gives Python callers an
in-process surface and ``tests/unit/test_checkpoint_py.py`` a stable
target to byte-diff against the live bash subprocess.

Public API (1:1 with bash ``lib/checkpoint.sh``):
  write(node_id, status, artifact_path=None) -> int
        rc=0 on success; rc=2 + matching stderr substring on validation
        failure. Writes ``${MINI_ORK_RUN_DIR}/.checkpoint.json`` with the
        same JSON shape bash emits:
          {"nodes": {"<id>": {"status": ..., "artifact_path": ...,
                              "completed_at": <unix_seconds>}}}
  can_resume(node_id) -> str
        Always rc=0. Emits ``"yes\\t<artifact>"`` (TAB separator) on a
        resumable success-with-artifact-present node, otherwise ``"no"``.
        Also writes the same string to stdout so grep-parity callers
        and Python callers share the surface.
  clear(node_id=None) -> int
        rc=0 on success; rc=2 if MINI_ORK_RUN_DIR is unset. With no
        ``node_id``, removes the entire file. With a ``node_id``,
        removes that node from the JSON ``nodes`` map.
  summary() -> str
        rc=0 always. Prints the fixed-column table bash prints
        (``node_id<24>status<10>age_s<8> artifact`` plus 80-char
        separator plus sorted data rows) to stdout, then returns it.

State file path: ``${MINI_ORK_RUN_DIR}/.checkpoint.json`` — derived by
``_resolve_path`` which mirrors ``_ckpt_resolve`` exactly, including
the rc=2 + stderr variants for env-unset and mkdir-failure paths.

Parity is enforced by ``tests/unit/test_checkpoint_py.py`` (>=8
live-subprocess cases that run ``bash`` for real and diff stdout/stderr/
JSON against this module).
"""
from __future__ import annotations

import json
import os
import sys
import time

__all__ = ["write", "can_resume", "clear", "summary"]


_VALID_STATUSES = {"success", "failure", "skipped"}


def _log_err(msg: str) -> None:
    sys.stderr.write(msg + "\n")


def _resolve_path() -> tuple[str, int]:
    """Mirror ``_ckpt_resolve`` in lib/checkpoint.sh.

    Returns (path, rc). rc=0 on success. On failure writes the same
    stderr text bash writes and returns rc=2 — case (h) asserts this.
    """
    rd = os.environ.get("MINI_ORK_RUN_DIR", "")
    if not rd:
        _log_err("checkpoint.sh: MINI_ORK_RUN_DIR unset; cannot persist")
        return ("", 2)
    try:
        if not os.path.isdir(rd):
            os.makedirs(rd, exist_ok=True)
    except OSError:
        _log_err(f"checkpoint.sh: failed to create {rd}")
        return ("", 2)
    return (os.path.join(rd, ".checkpoint.json"), 0)


def _load(path: str) -> tuple[dict, bool]:
    """Load the checkpoint file leniently (write/clear/can_resume path).

    Returns (data, parse_ok). ``parse_ok`` is False iff the file exists
    but could not be parsed or has the wrong shape — summary() needs
    that bit to mirror bash's "checkpoint file unparseable" branch.
    Other callers treat both absent AND unparseable as empty.
    """
    if not os.path.isfile(path):
        return ({"nodes": {}}, True)
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return ({"nodes": {}}, False)
    if not isinstance(data, dict):
        return ({"nodes": {}}, False)
    nodes = data.get("nodes")
    if not isinstance(nodes, dict):
        return ({"nodes": {}}, False)
    return (data, True)


def _dump(path: str, data: dict) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")


def write(node_id: str, status: str, artifact_path: str | None = None) -> int:
    """Mirror bash ``checkpoint_write``.

    Validation order (matches bash):
      1. empty node_id OR empty status → rc=2 + stderr usage line
      2. status not in {success, failure, skipped} → rc=2 + stderr
      3. resolve path: rc=2 + stderr on env-unset / mkdir-failure
      4. write JSON to disk, rc=0.
    """
    if not node_id or not status:
        _log_err("checkpoint_write: usage: checkpoint_write <node_id> <status> [<artifact_path>]")
        return 2
    if status not in _VALID_STATUSES:
        _log_err(
            f"checkpoint_write: status must be success|failure|skipped, got {status}"
        )
        return 2

    path, rc = _resolve_path()
    if rc != 0:
        return rc

    data, _ = _load(path)
    data.setdefault("nodes", {})[node_id] = {
        "status": status,
        "artifact_path": artifact_path,
        "completed_at": int(time.time()),
    }
    _dump(path, data)
    return 0


def can_resume(node_id: str) -> str:
    """Mirror bash ``checkpoint_can_resume``. Always rc=0.

    Emits and returns ``"yes\\t<artifact>"`` (TAB separator) when the
    node has ``status == "success"`` AND any recorded artifact still
    exists on disk. Otherwise emits and returns ``"no"``. Empty
    ``node_id`` emits a usage stderr line + ``"no"`` to match bash's
    `echo "..." >&2; echo "no"; return 0` pattern.
    """
    if not node_id:
        _log_err("checkpoint_can_resume: usage: checkpoint_can_resume <node_id>")
        sys.stdout.write("no\n")
        return "no"

    path, rc = _resolve_path()
    if rc != 0:
        sys.stdout.write("no\n")
        return "no"
    if not os.path.isfile(path):
        sys.stdout.write("no\n")
        return "no"

    data, _ = _load(path)
    node = (data.get("nodes") or {}).get(node_id)
    if not node or node.get("status") != "success":
        sys.stdout.write("no\n")
        return "no"
    art = node.get("artifact_path")
    if art and not os.path.exists(art):
        sys.stdout.write("no\n")
        return "no"
    out = "yes\t" + (art or "")
    sys.stdout.write(out + "\n")
    return out


def clear(node_id: str | None = None) -> int:
    """Mirror bash ``checkpoint_clear``.

    With no ``node_id``: remove the .checkpoint.json file (no-op if
    missing). With a ``node_id``: drop that key from the ``nodes`` map
    and rewrite the file. rc=2 only when MINI_ORK_RUN_DIR is unset;
    rc=0 otherwise (even when the file doesn't exist).
    """
    path, rc = _resolve_path()
    if rc != 0:
        return rc
    if not os.path.isfile(path):
        return 0

    if not node_id:
        try:
            os.remove(path)
        except OSError:
            pass
        return 0

    data, _ = _load(path)
    data.setdefault("nodes", {}).pop(node_id, None)
    _dump(path, data)
    return 0


def summary() -> str:
    """Mirror bash ``checkpoint_summary``. Always rc=0.

    Output variants (in this order):
      - env-unset / mkdir-fail → empty string, rc=2 (handled by caller)
      - file missing           → "no checkpoint file at <path>"
      - file unparseable       → "checkpoint file unparseable"
      - no nodes               → "(no nodes recorded)"
      - has nodes              → header + 80-char '-' separator +
                                 sorted data rows (fixed widths: 24/10/8)
    """
    path, rc = _resolve_path()
    if rc != 0:
        return ""

    if not os.path.isfile(path):
        out = f"no checkpoint file at {path}"
        sys.stdout.write(out + "\n")
        return out

    data, parse_ok = _load(path)
    if not parse_ok:
        out = "checkpoint file unparseable"
        sys.stdout.write(out + "\n")
        return out

    nodes = data.get("nodes") or {}
    if not nodes:
        out = "(no nodes recorded)"
        sys.stdout.write(out + "\n")
        return out

    lines = [
        f"{'node_id':<24} {'status':<10} {'age_s':<8} artifact",
        "-" * 80,
    ]
    now = int(time.time())
    for nid, n in sorted(nodes.items()):
        age = now - (n.get("completed_at") or 0)
        art = n.get("artifact_path") or ""
        st = n.get("status") or "?"
        lines.append(f"{nid:<24} {st:<10} {age:<8} {art}")
    out = "\n".join(lines)
    sys.stdout.write(out + "\n")
    return out
