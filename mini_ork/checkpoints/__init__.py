"""LangGraph-style checkpointer: per-step completion records (P1.2).

This module is the runtime seam for the per-node completion ledger
introduced by db/migrations/0071_node_completion_checkpoints.sql and
specified in ``/private/tmp/miniork-p1-specs/p1-2-checkpointer.md``.

LangGraph semantics replicated:

      every superstep persists writes; resume restores writes of SUCCEEDED
      nodes so they are not re-executed (``_reapply_writes_to_succeeded_
      nodes``), while failed / interrupted tasks stay eligible for
      re-execution; ``thread_id`` / ``checkpoint_id`` address a run and a
      point in it.

Mapping to this implementation:

      thread_id       -> ``run_id``
      checkpoint_id   -> ``step_id`` (usually the node id; one completion
                         row per (run_id, step_id) — F5)
      succeeded       -> status ``'succeeded'`` (resume SKIPS — F2)
      failed / ERROR  -> status ``'failed'`` or ``'error'`` (resume
                         RE-DISPATCHES — F3 / F4)

Distinct from ``mini_ork.stores.checkpoints`` (the E1 durable-DAG table
introduced by 0050_node_dag_checkpoints.sql): that table is the
"is this node's work durably reusable?" ledger keyed by
``(run_id, node_id)`` with input / recipe / config hashes and an artifact
manifest. This module is the lighter "did this step complete, and what was
its terminal artifact?" ledger keyed by ``(run_id, step_id)``, written on
every node completion (success, failure, or ERROR), and intentionally
without input / recipe / config hashing. Both tables coexist; the
durable-DAG validity check still wins on reuse.

Module-level gate (C2 — no behavior change for any existing recipe):
``record_completion`` is a no-op when the gate is OFF. The default is ON
(see ``enable()``); recipes that want dispatch-only semantics (a row
written only when the durable-DAG checkpoint commits, not on every
completion) can flip it OFF. The default keeps LangGraph parity.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Iterable

from .store import (
    CompletionRow,
    shape_reason,
    record_completion_db,
    list_completions_db,
    get_completion_db,
)

__all__ = [
    "CompletionRow",
    "enable",
    "is_enabled",
    "record_completion",
    "list_completions",
    "get_completion",
    "filter_resume_set",
    "CheckpointCorrupt",
]

_ENV_FLAG = "CHECKPOINTER_ENABLED"

# Module-level gate state. Default ON for LangGraph parity; C2 compliance
# comes from the gate being OFF-by-call (set_checkpointer_enabled(False)
# from a recipe that opts out), never from the default flipping.
_enabled: bool = os.environ.get(_ENV_FLAG, "1") not in ("0", "false", "False", "")


def enable(flag: bool) -> None:
    """Turn the per-completion write ON (True) or OFF (False).

    Default is ON. A recipe that wants dispatch-only semantics (no
    per-completion row, so resume behaves as before this module) can call
    ``enable(False)`` once at process start.
    """
    global _enabled
    _enabled = bool(flag)


def is_enabled() -> bool:
    """True iff the per-completion write is currently ON."""
    return _enabled


class CheckpointCorrupt(Exception):
    """Raised when a checkpoint row cannot be parsed safely. Per the Errors
    contract in the SpecCard, this MUST name the run_id and step_id (and the
    node_id when known) so the operator can locate the bad row. Resume
    MUST hard-error on this — silently re-executing a node that might have
    succeeded is exactly the double-side-effect bug this module prevents.
    """

    def __init__(self, run_id: str, step_id: str, reason: str,
                 node_id: str | None = None) -> None:
        self.run_id = run_id
        self.step_id = step_id
        self.node_id = node_id
        super().__init__(
            f"corrupt checkpoint for run_id={run_id!r} step_id={step_id!r}"
            + (f" node_id={node_id!r}" if node_id else "")
            + f": {reason}"
        )


@dataclass(frozen=True)
class CompletionRecord:
    """Public, JSON-friendly view of a node completion record (F1 fields).

    Mirrors the SQL row exactly so callers do not have to import the
    dataclass from ``.store``. ``started_at`` / ``ended_at`` are integer
    epoch seconds (the SQL column type).
    """

    run_id: str
    step_id: str
    node_id: str
    status: str         # one of 'succeeded' / 'failed' / 'error'
    artifact_path: str | None
    artifact_sha256: str | None
    rc: int | None
    started_at: int
    ended_at: int
    recorded_at: int

    @classmethod
    def from_row(cls, row: CompletionRow) -> "CompletionRecord":
        """Adapt a SQL row into a validated public record.

        Raises :class:`CheckpointCorrupt` when the row violates the F1 shape
        (see :func:`mini_ork.checkpoints.store.shape_reason`). This is the
        single read-path gate: ``list_completions``, ``get_completion`` and
        ``filter_resume_set`` all funnel through here, so a malformed row in
        the ledger hard-errors naming the run and node — never read back as
        a valid completion that resume would then silently skip.
        """
        reason = shape_reason(
            status=row.status,
            artifact_path=row.artifact_path,
            artifact_sha256=row.artifact_sha256,
            started_at=row.started_at,
            ended_at=row.ended_at,
        )
        if reason is not None:
            raise CheckpointCorrupt(
                row.run_id, row.step_id, reason, node_id=row.node_id,
            )
        return cls(
            run_id=row.run_id,
            step_id=row.step_id,
            node_id=row.node_id,
            status=row.status,
            artifact_path=row.artifact_path,
            artifact_sha256=row.artifact_sha256,
            rc=row.rc,
            started_at=row.started_at,
            ended_at=row.ended_at,
            recorded_at=row.recorded_at,
        )


def record_completion(
    db: str,
    *,
    run_id: str,
    step_id: str,
    node_id: str,
    status: str,
    artifact_path: str | None = None,
    artifact_sha256: str | None = None,
    rc: int | None = None,
    started_at: int,
    ended_at: int,
) -> int:
    """Write one completion record (F1 fields) atomically.

    Returns 0 on success. Returns 1 with no state change when the gate is
    OFF (C2 — recipes that opt out see dispatch-only semantics). Returns 2
    when the SQL write fails (caller can log + surface; the dispatch loop
    must not crash on a checkpointer write failure, because the durable
    DAG ledger is the source of truth for reuse, not this one).
    """
    if not _enabled:
        return 1
    return record_completion_db(
        db,
        run_id=run_id,
        step_id=step_id,
        node_id=node_id,
        status=status,
        artifact_path=artifact_path,
        artifact_sha256=artifact_sha256,
        rc=rc,
        started_at=started_at,
        ended_at=ended_at,
    )


def list_completions(db: str, *, run_id: str) -> list[CompletionRecord]:
    """Return every completion row for ``run_id``, in recorded_at order
    (stable under the AUTOINCREMENT id). Raises :class:`CheckpointCorrupt`
    if any row in the run cannot be parsed.
    """
    rows = list_completions_db(db, run_id=run_id)
    return [CompletionRecord.from_row(r) for r in rows]


def get_completion(db: str, *, run_id: str, step_id: str) -> CompletionRecord | None:
    """Return the single completion for ``(run_id, step_id)``, or None when
    no row exists. Raises :class:`CheckpointCorrupt` if the row exists but
    cannot be parsed — per the Errors contract.
    """
    row = get_completion_db(db, run_id=run_id, step_id=step_id)
    return CompletionRecord.from_row(row) if row is not None else None


# Terminal-status vocabulary per F2 / F3 / F4:
#   'succeeded'  -> resume SKIPS (the node already produced its output;
#                   re-dispatching risks a double side effect — the bug
#                   LangGraph names `_reapply_writes_to_succeeded_nodes`).
#   'failed'     -> resume RE-DISPATCHES (the node failed; previous output
#                   was either absent or partial).
#   'error'      -> resume RE-DISPATCHES (LangGraph distinguishes a
#                   controllable 'failed' from an uncaught ERROR; both re-enter
#                   the dispatch set on replay, never silently skipped —
#                   the F3 / F4 control-signal rule).
SKIP_ON_RESUME_STATUSES = frozenset({"succeeded"})
REDISPATCH_ON_RESUME_STATUSES = frozenset({"failed", "error"})


def filter_resume_set(
    db: str,
    *,
    run_id: str,
    candidates: Iterable[str],
) -> list[str]:
    """Drop succeeded nodes from a dispatch candidate set (F2).

    Given the list of ``step_id`` values the next dispatch pass is about
    to run, return the subset that MUST actually execute on this resume:

      - a candidate with no completion row    -> INCLUDED (never ran)
      - a candidate with status 'succeeded'   -> EXCLUDED (F2; skip)
      - a candidate with status 'failed'      -> INCLUDED (F3; re-dispatch)
      - a candidate with status 'error'       -> INCLUDED (F4; re-dispatch)

    The output preserves the input order. Unknown statuses are INCLUDED on
    purpose — a future migration that introduces a new terminal name MUST
    re-fail safely (re-dispatch), never silently skip a node that might
    have succeeded. This matches the F3 / F4 control-signal rule: an
    ERROR-marked node is never silently skipped.

    Raises :class:`CheckpointCorrupt` when a row in the run exists but
    cannot be parsed — see the Errors contract in the SpecCard. The
    filter MUST hard-error, never silently re-execute a node that might
    have succeeded.
    """
    # Build the candidate set first (de-duped, order-preserving) so two
    # identical candidates don't double-query the DB.
    seen: dict[str, None] = {}
    for c in candidates:
        if c not in seen:
            seen[c] = None
    if not seen:
        return []
    rows = list_completions(db, run_id=run_id)
    by_step: dict[str, CompletionRecord] = {r.step_id: r for r in rows}
    out: list[str] = []
    for step_id in seen:
        rec = by_step.get(step_id)
        if rec is None:
            out.append(step_id)
            continue
        if rec.status in SKIP_ON_RESUME_STATUSES:
            continue
        # Unknown OR 'failed' / 'error' -> re-dispatch.
        out.append(step_id)
    return out