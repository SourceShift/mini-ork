"""What the queue needs to know about the ledger — built once, used by both verbs.

``plan`` and a spending cycle ask the same question ("is this gap already merged,
already claimed, already failed?") and must get the same answer. Keeping this in
the CLI would mean the read-only verb and the spending verb each own a copy of
the rule, and the two would drift the first time one is edited.

The only non-SQL work here is the ancestry check, which needs git and therefore
cannot live in ``queue.py``. It is deliberately *advisory at the edges*: when
``origin/main`` cannot be resolved the merged set is empty, so a merged gap
returns to the queue and costs one re-check. The opposite default — assume
everything merged stays merged — would let a manually reverted regression sit
hidden forever because a remote moved.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import ledger
from .candidates import Candidate
from .queue import LedgerView

__all__ = ["ledger_view", "merged_ancestor_signatures", "resolve_db"]

#: Seconds allowed for one git query. A hung git must not stall a cycle: every
#: failure path here degrades to "cannot prove ancestry", which is safe.
_GIT_TIMEOUT_S = 30


def resolve_db(root: str | Path, *, db: str | None = None,
               home: str | None = None) -> Path:
    """The state DB path, from explicit flags then the environment contract.

    Deliberately the same precedence as ``mini_ork.context``: a ``--db`` that the
    harvest honours and a probe does not is how one cycle reads two databases.
    """
    if db:
        return Path(db).expanduser()
    if home:
        return Path(home).expanduser() / "state.db"
    env_db = os.environ.get("MINI_ORK_DB")
    if env_db:
        return Path(env_db).expanduser()
    env_home = os.environ.get("MINI_ORK_HOME")
    if env_home:
        return Path(env_home).expanduser() / "state.db"
    return Path(root) / ".mini-ork" / "state.db"


def _merge_base_is_ancestor(root: Path, sha: str, ref: str) -> bool | None:
    """``True``/``False`` from git, ``None`` when git cannot answer."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "merge-base", "--is-ancestor", sha, ref],
            capture_output=True, text=True, timeout=_GIT_TIMEOUT_S, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode == 0:
        return True
    if proc.returncode == 1:
        return False
    return None


def merged_ancestor_signatures(
    root: str | Path, rows: Sequence[Mapping[str, Any]]
) -> frozenset[str]:
    """Signatures whose ``merged_sha`` is already an ancestor of ``origin/main``."""
    shas = {str(r["merged_sha"]) for r in rows if r.get("merged_sha")}
    if not shas:
        return frozenset()
    try:
        base = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--verify", "origin/main"],
            capture_output=True, text=True, timeout=_GIT_TIMEOUT_S, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return frozenset()
    if base.returncode != 0:
        return frozenset()

    reachable = {sha for sha in shas if _merge_base_is_ancestor(Path(root), sha, "origin/main")}
    return frozenset(str(r["signature"]) for r in rows if r.get("merged_sha") in reachable)


def ledger_view(
    con,
    candidates: Sequence[Candidate],
    *,
    root: str | Path,
) -> LedgerView:
    """Read the avoidance state for these candidates.

    One query per distinct signature, not a bulk scan: a harvest produces dozens
    of candidates, and a bulk read would have to mirror the ledger's own index
    (``signature, status``) in hand-written SQL — a second definition of
    "already handled" is exactly what this module exists to avoid.
    """
    by_signature: dict[str, Mapping[str, Any]] = {}
    merged_rows: list[Mapping[str, Any]] = []
    for sig in sorted({c.signature for c in candidates}):
        row = ledger.candidate_by_signature(con, sig)
        if row is None:
            continue
        by_signature[sig] = row
        if row.get("status") == "merged":
            merged_rows.append(row)

    claimed = {
        str(r["candidate_id"])
        for r in con.execute(
            "SELECT candidate_id FROM rsi_cycles"
            " WHERE status = 'running' AND candidate_id IS NOT NULL"
        )
    }
    return LedgerView(
        by_signature=by_signature,
        claimed_ids=frozenset(claimed),
        merged_ancestor_signatures=merged_ancestor_signatures(root, merged_rows),
    )
