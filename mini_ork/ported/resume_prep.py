"""Turn-resume preparation for a recovered node (durable-dag E4).

Design source: ``internal-docs/architecture/2026-07-15-durable-dag-resume-design.md`` §6.

This is the bridge between the durable state (E1's ``node_checkpoints.session_ref``
+ the persisted transcript) and the dispatch layer's ``--resume`` gate
(``MO_RESUME_SESSION_ID`` in providers.dispatch_model). Before a recovery
re-dispatches a claude-lane node that stopped mid-conversation, call
``prepare_node_resume``: it reads the node's last attempt's session id +
persisted transcript ref, restores the transcript into the (possibly fresh)
sandbox's claude home, and returns the session id to export.

It is a *continuation optimization*, never a correctness dependency: if there
is no session id, no persisted transcript, or the restore fails, it returns ""
and the node re-runs from scratch (E1/E2 already guarantee that is safe).

codex/gemini lanes: these run their own session model and do not use the
claude transcript store — ``prepare_node_resume`` returns "" for them, so the
recovery falls back to node-level resume (documented, not faked).
"""
from __future__ import annotations

import os
import sqlite3
import sys
from typing import Optional

__all__ = ["node_session_ref", "prepare_node_resume"]

# Lanes that route through the claude binary share the transcript store and can
# turn-resume; codex/gemini have their own session model → node-level resume
# only (excluded in prepare_node_resume). Kept as documentation of the split.
CLAUDE_LANES = frozenset({"opus", "sonnet", "kimi", "minimax", "glm"})

_BUSY_MS = 5000


def _log(msg: str) -> None:
    sys.stderr.write(f"resume_prep: {msg}\n")


def node_session_ref(db: str, run_id: str, node_id: str) -> tuple[str, str]:
    """Return ``(provider_session_id, session_ref)`` for a node's most recent
    attempt, or ("", "") if none is recorded.

    ``provider_session_id`` comes from ``node_attempts`` (latest attempt);
    ``session_ref`` from ``node_checkpoints`` (the persisted transcript path,
    relative to the run dir). Both are needed to resume: the id names the
    conversation, the ref is the transcript to restore."""
    if not db or not run_id or not node_id or not os.path.isfile(db):
        return ("", "")
    try:
        con = sqlite3.connect(db, timeout=_BUSY_MS / 1000)
        con.execute(f"PRAGMA busy_timeout={_BUSY_MS}")
        try:
            ref_row = con.execute(
                "SELECT session_ref FROM node_checkpoints WHERE run_id=? AND node_id=?",
                (run_id, node_id),
            ).fetchone()
            sid_row = con.execute(
                "SELECT provider_session_id FROM node_attempts "
                "WHERE run_id=? AND node_id=? AND provider_session_id IS NOT NULL "
                "ORDER BY attempt_no DESC LIMIT 1",
                (run_id, node_id),
            ).fetchone()
        finally:
            con.close()
    except sqlite3.Error as e:
        _log(f"node_session_ref: {e}")
        return ("", "")
    session_ref = (ref_row[0] if ref_row and ref_row[0] else "") or ""
    session_id = (sid_row[0] if sid_row and sid_row[0] else "") or ""
    return (session_id, session_ref)


def prepare_node_resume(
    db: str,
    run_id: str,
    node_id: str,
    *,
    run_dir: str,
    model: str = "",
    cwd: Optional[str] = None,
) -> str:
    """Restore a node's transcript and return the session id to `--resume`.

    Steps:
      1. codex/gemini (non-claude lanes) → return "" (node-level resume).
      2. read the node's (session_id, session_ref) from durable state.
      3. restore the persisted transcript into ~/.claude/projects/<slug>/ so a
         fresh sandbox's `claude --resume <id>` finds it.
      4. return the session id on success, "" otherwise (→ re-run from scratch).

    The caller exports the return value as ``MO_RESUME_SESSION_ID`` before
    dispatching the node; providers.dispatch_model turns that into
    ``claude --resume <id>``.
    """
    # codex/gemini run their own session model → node-level resume only. Match
    # tolerant of lane suffixes (e.g. "codex_lens"). Every other lane is
    # claude-family; the actual `claude`-only rewrite is enforced downstream by
    # providers.apply_resume, so proceeding here for an unrecognized lane is
    # safe (a non-claude command is left unchanged).
    norm = (model or "").split("_")[0]
    if norm in {"codex", "gemini"}:
        return ""
    session_id, session_ref = node_session_ref(db, run_id, node_id)
    if not session_id:
        return ""
    if not session_ref:
        # We know the conversation id but never persisted the transcript; a
        # resume would only work if the transcript still lives in the current
        # home. Let the dispatch layer try — return the id.
        return session_id
    try:
        from mini_ork.ported.session_store import restore_session  # lazy
        ok = restore_session(run_dir, session_ref, session_id, cwd=cwd)
    except Exception as e:  # noqa: BLE001 — best-effort
        _log(f"prepare_node_resume: restore errored ({e})")
        return ""
    return session_id if ok else ""
