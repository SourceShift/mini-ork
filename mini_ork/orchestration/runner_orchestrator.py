"""Per-run orchestrator — a pure decision core at each DAG node boundary.

Every mini-ork run already produces the raw material an orchestrator needs:
typed failure classes (``mini_ork/learning/failure_classifier.py``), a
failure-class → (surface, edit-kind) table
(``mini_ork/learning/harness_operator.py:_TYPED_MAPPING``), an outcome-gated
per-node reward (``mini_ork/learning/process_reward.py:score_trace``), a typed
node-outcome event stream (``mini_ork/observability/node_events.py``), an
operator-steering write channel (``mini_ork/web/control.py:steer_run``), and a
retries-edge revise machinery (``mini_ork/cli/execute.py``). This module wires
them into ONE decision: at a node boundary, pick one of five actions

    advance · help · repair · mutate · promote

from the node's typed outcome, and emit a learnable trace row. It ships behind
``MO_RUN_ORCHESTRATOR=1`` — **default off**. With the flag unset the loop is
byte-identical to today: nothing here is imported and nothing is called.

## Pure core / impure adapter

``decide`` and ``classify_outcome`` are the pure core: deterministic, no DB, no
lane, no model, no network — unit-testable with plain values. ``on_node_boundary``
is the thin adapter: it (best-effort) reads the node's ``execution_traces`` row,
builds the ``decide`` inputs, emits exactly one ``run_events`` row
(``orchestrator.<action>``) through the canonical ``mo_node_emit`` writer, and
returns the ``Action``. It never raises: any internal error returns ``None`` so a
run is never wedged by its own observer. This split mirrors the sibling
``mini_ork/orchestration/conductor.py`` (pure ``decide_for_epic`` + thin driver).

## Rule precedence (``decide``)

    1. rc == 0 and not stall                     -> ADVANCE
    2. stall (repeat failure, no progress)       -> HELP    (bounded content nudge)
    3. rc != 0, a retries edge, non-harness locus -> REPAIR  (re-run via the edge)
    4. rc != 0, harness-surface locus            -> MUTATE  (proposal only)
    5. rc != 0, infra (env) locus                -> REPAIR  (retry_policy; never PROMOTE)
    6. otherwise                                 -> ADVANCE (fail-open: never wedge)

Rule 3/4 tension, resolved deliberately: ``classify_outcome`` maps every live
failure class through ``_TYPED_MAPPING``, whose surfaces are only
``prompt``/``recovery``/``stage_order``/``verifier`` — it never emits ``node`` or
``model``. A failure that already owns a declared ``retries`` edge is therefore
promoted to the ``node`` locus (rule 3) so the existing edge repairs it — *unless*
the fault is a harness-surface fault (``prompt``/``stage_order``/``verifier``/the
umbrella ``harness``), which no node re-run can fix and which rule 4 proposes as a
harness mutation. This keeps both rules live: a harness-locus failure with a
retries edge still files a MUTATE proposal (rule 4), while a generic retries-edge
failure is repaired in place (rule 3).

## Guard

A HELP nudge may only *add information* to the content half of the node's work.
It may never change routing, termination, or schema. ``guard_steer`` rejects any
message that touches those levers; ``steer_message`` renders a nudge that is
built to pass it. A rejected nudge is dropped, not sent.

## Prior art (design evidence)

* Type-before-treat: 2607.28802.
* Narrow-don't-broaden at failure: 2605.08717, 2608.11772.
* Scope-the-edit: 2609.34313.
* Invariant-guarded mutation: 2607.20488.
* Execution-anchored gates only: 2609.02246.
* Invisible orchestrators suppress protective behaviour: 2605.13851.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Mapping

__all__ = [
    "ADVANCE",
    "HELP",
    "REPAIR",
    "MUTATE",
    "PROMOTE",
    "ALL_ACTIONS",
    "Action",
    "enabled",
    "classify_outcome",
    "decide",
    "steer_message",
    "guard_steer",
    "budget_ok",
    "on_node_boundary",
]

# ── action vocabulary ────────────────────────────────────────────────────────
ADVANCE, HELP, REPAIR, MUTATE, PROMOTE = "advance", "help", "repair", "mutate", "promote"
ALL_ACTIONS = (ADVANCE, HELP, REPAIR, MUTATE, PROMOTE)

# ── env contract ─────────────────────────────────────────────────────────────
_ENV_FLAG = "MO_RUN_ORCHESTRATOR"
_ENV_BUDGET = "MO_ORCH_BUDGET_USD"
_TRUTHY = frozenset({"1", "true", "yes", "on"})
_DEFAULT_BUDGET_USD = 1.0

# Content-only nudges cost a little; a harness proposal costs a little more.
_HELP_BUDGET_USD = 0.02
_MUTATE_BUDGET_USD = 0.05

# Tokens that would let a nudge reach into the node's CONTRACT. Any of these,
# case-insensitive, rejects the message — a nudge adds information only.
_GUARD_TOKENS = (
    "finish_reason",
    "exit_code",
    "schema",
    "unset mo_",
    "mo_run_orchestrator",
    "git reset",
    "rm -rf",
)

# Loci that name a *harness* fault (a stage, prompt, or gate), not a node rerun.
_HARNESS_LOCI = frozenset({"harness", "prompt", "stage_order", "verifier"})
# Loci a retries edge can repair by re-running the node.
_MODEL_LOCI = frozenset({"node", "model"})
_INFRA_LOCUS = "env"

# finish-reason fragments that name a bounded, argument-level correction (a
# cheap re-run of the same node with a fixed argument) rather than a structural
# replacement. Anything else on a failed node is treated as subgraph-level.
_EDIT_ARG_STOPS = ("edit-args", "edit_args", "arg-level", "arg_level", "fix-args", "fix_args")

_STEER_MAX_CHARS = 480


@dataclass(frozen=True)
class Action:
    """One orchestrator decision at a node boundary.

    ``requires_llm`` is True only when the action cannot be realized without
    generating new content (a HELP nudge or a MUTATE proposal); ``budget_usd``
    is the action's (small, bounded) expected spend.
    """

    kind: str
    reason: str
    payload: dict = field(default_factory=dict)
    requires_llm: bool = False
    budget_usd: float = 0.0


# ── flag / budget ────────────────────────────────────────────────────────────
def enabled() -> bool:
    """True when ``MO_RUN_ORCHESTRATOR`` is truthy (``1/true/yes/on``, any case)."""
    return os.environ.get(_ENV_FLAG, "").strip().lower() in _TRUTHY


def budget_ok(spent_usd: float) -> bool:
    """True while ``spent_usd`` is under the cap (``MO_ORCH_BUDGET_USD``, default 1.0)."""
    raw = os.environ.get(_ENV_BUDGET, "") or str(_DEFAULT_BUDGET_USD)
    try:
        cap = float(raw)
    except (TypeError, ValueError):
        cap = _DEFAULT_BUDGET_USD
    try:
        spent = float(spent_usd)
    except (TypeError, ValueError):
        return False
    return spent < cap


# ── classification (pure) ────────────────────────────────────────────────────
def _classify(finish_reason: str, max_turns_hit: bool) -> tuple[str, str]:
    """(failure_class, surface) via the live classifier + typed mapping.

    The two learning modules are imported lazily so this module's own import
    graph stays stdlib-only (the execute.py wiring wraps the import in a guard)
    and so the pure core carries no DB/lane/model. Degrades to
    ``("terminal", "verifier")`` if they are unavailable.
    """
    try:
        from mini_ork.learning import failure_classifier, harness_operator
    except Exception:  # pragma: no cover - defensive import guard
        return "terminal", "verifier"

    try:
        failure_class = failure_classifier.classify(
            reason=finish_reason or "", max_turns_hit=max_turns_hit)
    except Exception:  # pragma: no cover - classifier is pure but never trust it
        failure_class = "terminal"

    mapping = getattr(harness_operator, "_TYPED_MAPPING", {}) or {}
    pair = mapping.get(failure_class)
    surface = pair[0] if pair else "node"
    return failure_class, surface


def _scope_for(rc: int, attempts: int, finish_reason: str) -> str:
    """How wide the corrective edit must be, given the stop's shape."""
    if rc == 0:
        return "none"
    text = (finish_reason or "").lower()
    if any(tok in text for tok in _EDIT_ARG_STOPS):
        return "edit-args"
    if attempts >= 2:
        return "replace-node"
    return "replace-subgraph"


def classify_outcome(
    node_type: str,
    rc: int,
    finish_reason: str = "",
    *,
    attempts: int = 0,
    max_turns_hit: bool = False,
) -> tuple[str, str, str]:
    """``(failure_class, locus, scope)`` from a node stop. Rules-first, no model.

    * ``failure_class`` — delegated to ``failure_classifier.classify``.
    * ``locus`` — the harness surface from ``_TYPED_MAPPING``
      (``prompt``/``recovery``/``stage_order``/``verifier``), or ``env`` for an
      ``infra_interrupt`` (an environment locus, never a harness surface — the
      mapping would otherwise say ``recovery``), or ``node`` when unmapped.
    * ``scope`` — ``none`` (rc==0) / ``edit-args`` (an argument-level stop) /
      ``replace-node`` (a repeat, ``attempts>=2``) / ``replace-subgraph``.
    """
    failure_class, surface = _classify(finish_reason, max_turns_hit)
    if failure_class == "infra_interrupt":
        locus = _INFRA_LOCUS
    else:
        locus = surface
    scope = _scope_for(rc, attempts, finish_reason)
    return failure_class, locus, scope


# ── decision (pure) ──────────────────────────────────────────────────────────
def decide(
    *,
    node_id: str,
    node_type: str,
    rc: int,
    finish_reason: str = "",
    lane: str = "",
    attempts: int = 0,
    process_reward: float | None = None,
    has_retries_edge: bool = False,
    stall: bool = False,
) -> Action:
    """Pick ONE action for a node boundary. Deterministic; no I/O.

    See the module docstring for the precedence table and the rule 3/4
    reconciliation.
    """
    failure_class, locus, scope = classify_outcome(
        node_type, rc, finish_reason, attempts=attempts)

    # A declared retries edge is a repair channel for the node itself, but it
    # cannot fix a *harness* fault. Promote a non-harness retries-edge failure to
    # the node locus (rule 3); leave harness faults on their surface so rule 4
    # proposes the mutation. classify never emits node/model on its own.
    effective_locus = locus
    if rc != 0 and has_retries_edge and locus not in _HARNESS_LOCI:
        effective_locus = "node"

    base = {
        "node_id": node_id,
        "node_type": node_type,
        "failure_class": failure_class,
        "locus": locus,
        "scope": scope,
        "attempts": attempts,
        "lane": lane,
        "process_reward": process_reward,
    }

    # 1. success
    if rc == 0 and not stall:
        return Action(ADVANCE, f"{node_id}: rc=0 ({node_type})",
                      {**base, "check": "rc_zero"})

    # 2. stall — a bounded, content-half nudge
    if stall:
        return Action(HELP,
                      f"{node_id}: stalled — repeat failure with no progress",
                      {**base, "check": "stall"},
                      requires_llm=True, budget_usd=_HELP_BUDGET_USD)

    # 3. a retries edge already declares the repair — re-run through it
    if rc != 0 and has_retries_edge and effective_locus in _MODEL_LOCI:
        return Action(REPAIR,
                      f"{node_id}: {failure_class} — repair via existing retries edge",
                      {**base, "check": "retries_edge", "effective_locus": effective_locus})

    # 4. a harness-surface fault is not fixable by a node re-run — propose a mutation
    if rc != 0 and effective_locus in _HARNESS_LOCI:
        return Action(MUTATE,
                      f"{node_id}: {failure_class} on {effective_locus} — harness proposal",
                      {**base, "check": "harness_surface", "effective_locus": effective_locus},
                      requires_llm=True, budget_usd=_MUTATE_BUDGET_USD)

    # 5. infra is an environment blip — bounded retry policy; never PROMOTE
    if rc != 0 and effective_locus == _INFRA_LOCUS:
        return Action(REPAIR,
                      f"{node_id}: infra_interrupt — bounded retry_policy",
                      {**base, "check": "infra_env", "retry_policy": "bounded"})

    # 6. fail-open: an unclassified stop never wedges the run
    return Action(ADVANCE,
                  f"{node_id}: unclassified stop ({failure_class}) — fail-open",
                  {**base, "check": "fail_open"})


# ── steering guard + renderer (pure) ─────────────────────────────────────────
def guard_steer(message: str) -> bool:
    """Reject any nudge that would mutate the node's CONTRACT.

    Content-half only: a nudge may add information, never change routing,
    termination, or schema. Returns False when the message is empty or contains
    any guard token (case-insensitive).
    """
    if not message:
        return False
    text = message.lower()
    return not any(tok in text for tok in _GUARD_TOKENS)


def steer_message(action: Action) -> str:
    """Render a HELP Action into a bounded steering message that passes the guard."""
    if action.kind != HELP:
        return ""
    node_id = str(action.payload.get("node_id", "the node") or "the node")
    node_type = str(action.payload.get("node_type", "") or "")
    where = f"{node_id} ({node_type})" if node_type else node_id
    message = (
        f"Adapter note for {where}: the previous attempt repeated without progress. "
        "Keep the existing acceptance criteria and outputs unchanged; narrow the next "
        "attempt to the single smallest edit that unblocks the goal, and build on the "
        "work already in the tree rather than restarting."
    )
    return message[:_STEER_MAX_CHARS]


# ── adapter (impure) ─────────────────────────────────────────────────────────
def _field(ctx: Any, key: str, default: Any = None) -> Any:
    """Read ``key`` from a mapping context or an attribute-style context."""
    if ctx is None:
        return default
    if isinstance(ctx, Mapping):
        return ctx.get(key, default)
    return getattr(ctx, key, default)


def _trace_fields(db: Any, run_id: str, node_id: str) -> dict:
    """Best-effort read of ``execution_traces`` for (run_id, node_id).

    Schema-tolerant and fail-open: returns ``{}`` on any error or when the
    columns are absent (the table has no per-node column in every revision).
    """
    if not db or not run_id or not node_id:
        return {}
    try:
        from mini_ork.sqlite_read import connect_readonly
    except Exception:  # pragma: no cover - defensive
        return {}
    con = None
    try:
        con = connect_readonly(db, timeout=2.0)
        con.row_factory = __import__("sqlite3").Row
        cols = {row[1] for row in con.execute("PRAGMA table_info(execution_traces)").fetchall()}
        if not cols:
            return {}
        want = [c for c in ("lane", "route_source", "process_reward", "attempts", "node_id")
                if c in cols]
        if not want:
            return {}
        where = "run_id = ?"
        params: list = [run_id]
        if "node_id" in cols:
            where += " AND node_id = ?"
            params.append(node_id)
        order = " ORDER BY created_at DESC LIMIT 1" if "created_at" in cols else " LIMIT 1"
        row = con.execute(
            f"SELECT {', '.join(want)} FROM execution_traces WHERE {where}{order}", params
        ).fetchone()
        if row is None:
            return {}
        keys = set(row.keys())
        out: dict = {}
        if "lane" in keys and row["lane"] is not None:
            out["lane"] = row["lane"]
        if "route_source" in keys and row["route_source"] is not None:
            out.setdefault("lane", row["route_source"])
        if "process_reward" in keys and row["process_reward"] is not None:
            out["process_reward"] = float(row["process_reward"])
        if "attempts" in keys and row["attempts"] is not None:
            out["attempts"] = int(row["attempts"])
        return out
    except Exception:
        return {}
    finally:
        if con is not None:
            try:
                con.close()
            except Exception:
                pass


def _emit(run_id: str, node_id: str, node_type: str, event_type: str,
          payload: dict, db: Any) -> None:
    """Emit exactly one ``run_events`` row through the canonical writer."""
    try:
        from mini_ork.observability.node_events import mo_node_emit
    except Exception:  # pragma: no cover - defensive
        return
    try:
        mo_node_emit(run_id, node_id, node_type, event_type, json.dumps(payload), db=db)
    except Exception:
        pass


def on_node_boundary(ctx: Any, *, spent_usd: float = 0.0) -> Action | None:
    """Adapter: read the node's trace, decide, emit one audit row, return the Action.

    Returns ``None`` when the flag is off, when the global budget gate is closed,
    or on any internal error — the orchestrator must never fail a run. The
    context (``ctx``) carries ``run_id``/``node_id``/``node_type``/``rc`` and
    optional ``finish_reason``/``lane``/``attempts``/``process_reward``/
    ``has_retries_edge``/``stall``/``db``; missing lane/attempts/reward are
    filled from the node's ``execution_traces`` row when ``db`` is available.
    """
    try:
        if not enabled():
            return None
        if not budget_ok(spent_usd):
            return None

        node_id = str(_field(ctx, "node_id", "") or "")
        node_type = str(_field(ctx, "node_type", "") or "")
        run_id = str(_field(ctx, "run_id", "") or "")
        rc = int(_field(ctx, "rc", 1) or 0)
        finish_reason = str(_field(ctx, "finish_reason", "") or "")
        attempts = int(_field(ctx, "attempts", 0) or 0)
        lane = str(_field(ctx, "lane", "") or "")
        process_reward = _field(ctx, "process_reward", None)
        has_retries_edge = bool(_field(ctx, "has_retries_edge", False))
        stall = bool(_field(ctx, "stall", False))
        db = _field(ctx, "db", None)

        trace = _trace_fields(db, run_id, node_id)
        lane = lane or str(trace.get("lane", "") or "")
        if process_reward is None:
            process_reward = trace.get("process_reward")
        if not attempts:
            attempts = int(trace.get("attempts", 0) or 0)

        action = decide(
            node_id=node_id,
            node_type=node_type,
            rc=rc,
            finish_reason=finish_reason,
            lane=lane,
            attempts=attempts,
            process_reward=process_reward,
            has_retries_edge=has_retries_edge,
            stall=stall,
        )
        _emit(run_id, node_id, node_type, f"orchestrator.{action.kind}",
              {**action.payload, "reason": action.reason,
               "requires_llm": action.requires_llm, "budget_usd": action.budget_usd},
              db)
        return action
    except Exception:
        return None
