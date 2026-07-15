"""Trace continuity for recovery (durable-dag E5, design §8).

One logical run — its original attempts AND every recovered attempt — must
correlate under ONE root trace, so an operator/UI sees a single trajectory
rather than a disconnected synthetic trace per recovery. This module is the
small, testable core of that:

  * ``root_trace_id(run_id)`` resolves the run's root trace id with this
    precedence: a CALLER-SUPPLIED context (``MINI_ORK_ROOT_TRACE_ID``, e.g. the
    Researcher compose planner) wins; else a stable id DERIVED from run_id so
    all attempts of a run share it even with no caller context. It also PINS
    the resolved id back into the env so it propagates across the
    recover → execute → dispatch (and sandbox) boundary.

  * ``attempt_span_attrs(...)`` returns the queryable attributes a resumed
    attempt's span carries — root_trace_id + run/node/checkpoint/attempt ids —
    so a recovered attempt nests under the run's root trace as a new child
    span, never a fresh unrelated trace.

Pure + env-only (no DB, no OTel dependency) so it composes with whatever trace
backend is active and is trivially unit-testable.
"""
from __future__ import annotations

import hashlib
import os
from typing import Optional

__all__ = ["root_trace_id", "attempt_span_attrs", "ROOT_TRACE_ENV"]

ROOT_TRACE_ENV = "MINI_ORK_ROOT_TRACE_ID"


def root_trace_id(run_id: str, *, pin_env: bool = True) -> str:
    """Resolve the run's root trace id.

    Precedence:
      1. a caller-supplied ``MINI_ORK_ROOT_TRACE_ID`` (external orchestrator /
         compose planner passing its own root context) — preserved verbatim so
         recovery spans nest under the CALLER's trace.
      2. a stable id derived from run_id (``rt-<sha16>``) so every attempt of a
         run shares one root even when no caller context exists.

    When ``pin_env`` is set (default) the resolved id is written back to the
    env so it survives the recover → execute → dispatch handoff and a fresh
    sandbox inherits it — this is what keeps a resumed attempt on the same
    root trace instead of minting a synthetic one.
    """
    caller = os.environ.get(ROOT_TRACE_ENV, "").strip()
    resolved = caller or (f"rt-{hashlib.sha256(run_id.encode()).hexdigest()[:16]}" if run_id else "")
    if pin_env and resolved:
        os.environ[ROOT_TRACE_ENV] = resolved
    return resolved


def attempt_span_attrs(
    run_id: str,
    node_id: str,
    *,
    attempt: int = 1,
    checkpoint_status: str = "",
    recovery_request_id: str = "",
    is_recovery: Optional[bool] = None,
) -> dict:
    """Queryable trace attributes for one node attempt.

    These become span attributes so run/node/checkpoint/attempt ids are all
    filterable, and ``recovery.is_recovery`` distinguishes a resumed attempt
    from an original one — both under the same ``trace.root_id``.
    """
    if is_recovery is None:
        is_recovery = bool(
            os.environ.get("MINI_ORK_RECOVERY_CLOSURE", "").strip()
            or os.environ.get("MINI_ORK_RECOVERY_FROM", "").strip()
        )
    attrs = {
        "trace.root_id": root_trace_id(run_id),
        "run.id": run_id,
        "node.id": node_id,
        "node.attempt": int(attempt),
        "recovery.is_recovery": bool(is_recovery),
    }
    if checkpoint_status:
        attrs["checkpoint.status"] = checkpoint_status
    rrid = recovery_request_id or os.environ.get("MINI_ORK_RECOVERY_REQUEST", "").strip()
    if rrid:
        attrs["recovery.request_id"] = rrid
    resume_sid = os.environ.get("MO_RESUME_SESSION_ID", "").strip()
    if resume_sid:
        attrs["resume.session_id"] = resume_sid
    return attrs
