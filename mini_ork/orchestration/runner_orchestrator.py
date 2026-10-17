"""Per-run orchestrator — one decision core on two cadences.

**Cadence 1, node boundaries** (:func:`on_node_boundary`): decide one action per
node transition. **Cadence 2, between boundaries** (:func:`observe`): sample the
live node, judge it, and speak only when it is in trouble. Both run under the
same flag, budget and event prefix, so a run has exactly one orchestrator; the
interval cadence is hosted by a detached ``mini-ork orchestrator watch``
process because it must outlive the dispatcher it watches.

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

from the node's typed outcome, and emit a learnable trace row. It is
**default on**: every dependency-graph run gets it with no flag. Opt out per
environment with ``MO_RUN_ORCHESTRATOR=0`` — with the flag explicitly
disabled the loop is byte-identical to a no-orchestrator run: nothing is
imported and nothing is called. The safety story that makes default-on
tenable: green nodes are skipped outright (``rc == 0 → continue``), every
action is bounded by ``MO_ORCH_BUDGET_USD`` (default $1) against the run's
real spend, HELP messages pass ``guard_steer``, and the whole adapter is
fail-safe — an orchestrator error can never take a run down.

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
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

__all__ = [
    "ADVANCE",
    "HELP",
    "REPAIR",
    "MUTATE",
    "PROMOTE",
    "ALL_ACTIONS",
    "WATCH_EVENTS",
    "WATCH_LOCK_FILE",
    "WATCH_LOG_FILE",
    "WATCH_STATE_FILE",
    "Action",
    "SteerRequest",
    "WatchConfig",
    "WatchDecision",
    "WatchInputs",
    "WatchState",
    "enabled",
    "classify_outcome",
    "decide",
    "steer_message",
    "guard_steer",
    "budget_ok",
    "on_node_boundary",
    "load_watch_state",
    "observe",
    "save_watch_state",
    "watch_active",
    "watch_enabled",
    "watch_state_path",
    "watch_steer_message",
]

# ── action vocabulary ────────────────────────────────────────────────────────
ADVANCE, HELP, REPAIR, MUTATE, PROMOTE = "advance", "help", "repair", "mutate", "promote"
ALL_ACTIONS = (ADVANCE, HELP, REPAIR, MUTATE, PROMOTE)

# ── env contract ─────────────────────────────────────────────────────────────
_ENV_FLAG = "MO_RUN_ORCHESTRATOR"
_ENV_BUDGET = "MO_ORCH_BUDGET_USD"
_FALSY = frozenset({"0", "false", "no", "off"})
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
    """True unless ``MO_RUN_ORCHESTRATOR`` is explicitly falsy (``0/false/no/off``, any case).

    Default ON: unset, empty, or any unrecognized value enables the
    orchestrator. Only an explicit opt-out disables it. Unrecognized values
    stay "on" deliberately — a typo must not silently disable the layer;
    the ``MO_ORCH_BUDGET_USD`` cap is the real brake, not this flag.
    """
    return os.environ.get(_ENV_FLAG, "").strip().lower() not in _FALSY


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


# ════════════════════════════════════════════════════════════════════════════
# Interval cadence — observe a live run between node boundaries
# ════════════════════════════════════════════════════════════════════════════
# The detection rules live in ``mini_ork.acp_orchestrator.watch`` (log mtime,
# repeated lines, spend); this half owns the run-level policy on top of them:
# when to speak, when to steer, and when to stop watching. Nothing here opens a
# model call, writes ``.pid``, changes a run's status, or writes liveness.

WATCH_EVENTS = (
    "orchestrator.watching",
    "orchestrator.signal",
    "orchestrator.steered",
    "orchestrator.ended",
)
WATCH_STATE_FILE = ".orchestrator-watch.json"
WATCH_LOCK_FILE = ".orchestrator-watch.lock"
WATCH_LOG_FILE = "orchestrator-watch.log"

_ENV_WATCH = "MO_ORCH_WATCH"
_ENV_INTERVAL = "MO_ORCH_WATCH_INTERVAL_S"
_ENV_AUTONOMY = "MO_ORCH_WATCH_AUTONOMY"
_ENV_MAX_STEERS = "MO_ORCH_WATCH_MAX_STEERS"
_ENV_MAX_HOURS = "MO_ORCH_WATCH_MAX_HOURS"

_DEFAULT_INTERVAL_S = 20.0
_DEFAULT_MAX_STEERS = 2
_DEFAULT_MAX_HOURS = 12.0
_DEFAULT_AUTONOMY = "steer"
_AUTONOMY = frozenset({"steer", "notify", "off"})
# Only a node that is visibly stuck is worth a nudge. A long-running or
# expensive node is reported, never steered: the remedy there is a human
# decision, not an injection of text.
_WATCH_STEERABLE = frozenset({"stalled", "repeating_error"})
_MAX_STEER_ATTEMPTS = 3
_STEER_TTL_SECS = 1800
_STALE_AFTER_S = 60.0
_MISSING_PID_TICKS = 2


def watch_enabled() -> bool:
    """True unless ``MO_ORCH_WATCH`` is explicitly falsy.

    The spawn switch and the watcher read the same value, so a run that opts
    out never starts a process that would immediately exit.
    """
    return os.environ.get(_ENV_WATCH, "").strip().lower() not in _FALSY


@dataclass(frozen=True)
class WatchConfig:
    interval_s: float = _DEFAULT_INTERVAL_S
    autonomy: str = _DEFAULT_AUTONOMY
    max_steers: int = _DEFAULT_MAX_STEERS
    max_hours: float = _DEFAULT_MAX_HOURS

    @classmethod
    def from_env(cls) -> "WatchConfig":
        """Read the knobs; an unrecognized autonomy fails closed to ``notify``."""
        try:
            interval = float(os.environ.get(_ENV_INTERVAL, "") or _DEFAULT_INTERVAL_S)
        except (TypeError, ValueError):
            interval = _DEFAULT_INTERVAL_S
        try:
            max_steers = int(os.environ.get(_ENV_MAX_STEERS, "") or _DEFAULT_MAX_STEERS)
        except (TypeError, ValueError):
            max_steers = _DEFAULT_MAX_STEERS
        try:
            max_hours = float(os.environ.get(_ENV_MAX_HOURS, "") or _DEFAULT_MAX_HOURS)
        except (TypeError, ValueError):
            max_hours = _DEFAULT_MAX_HOURS
        raw = (os.environ.get(_ENV_AUTONOMY, "") or _DEFAULT_AUTONOMY).strip().lower()
        autonomy = raw if raw in _AUTONOMY else "notify"
        return cls(
            interval_s=interval if interval > 0 else _DEFAULT_INTERVAL_S,
            autonomy=autonomy,
            max_steers=max(0, max_steers),
            max_hours=max_hours if max_hours > 0 else _DEFAULT_MAX_HOURS,
        )

    @property
    def stale_after_s(self) -> float:
        """How old the state may be before a reader stops trusting it."""
        return max(_STALE_AFTER_S, 3.0 * self.interval_s)


@dataclass
class WatchState:
    """What the watcher remembers across ticks. Persisted as JSON."""

    pid: int = 0
    started_at: float | None = None
    last_tick: float | None = None
    tick_count: int = 0
    missing_pid_ticks: int = 0
    steers: int = 0
    last_status: str | None = None
    ended_reason: str | None = None
    signaled: dict[str, float] = field(default_factory=dict)
    steered: dict[str, float] = field(default_factory=dict)
    steer_attempts: dict[str, int] = field(default_factory=dict)
    # Set by observe() when a nudge is due, cleared by the shell when the write
    # succeeds. Kept across ticks so a failed write is retried, not forgotten.
    pending: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "pid": self.pid,
            "started_at": self.started_at,
            "last_tick": self.last_tick,
            "tick_count": self.tick_count,
            "missing_pid_ticks": self.missing_pid_ticks,
            "steers": self.steers,
            "last_status": self.last_status,
            "ended_reason": self.ended_reason,
            "signaled": self.signaled,
            "steered": self.steered,
            "steer_attempts": self.steer_attempts,
            "pending": self.pending,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "WatchState":
        if not isinstance(data, dict):
            return cls()
        return cls(
            pid=_opt_int(data.get("pid")),
            started_at=_opt_float(data.get("started_at")),
            last_tick=_opt_float(data.get("last_tick")),
            tick_count=_opt_int(data.get("tick_count")),
            missing_pid_ticks=_opt_int(data.get("missing_pid_ticks")),
            steers=_opt_int(data.get("steers")),
            last_status=_opt_str(data.get("last_status")),
            ended_reason=_opt_str(data.get("ended_reason")),
            signaled=_float_map(data.get("signaled")),
            steered=_float_map(data.get("steered")),
            steer_attempts=_int_map(data.get("steer_attempts")),
            pending=data.get("pending") if isinstance(data.get("pending"), dict) else None,
        )


@dataclass(frozen=True)
class WatchInputs:
    """One tick's inputs, already read by the shell.

    ``probe`` is a ``run_reaper.Probe.verdict`` string
    (``alive|dead|unknown|remote|paused|finished``); the shell maps a raising
    probe to ``unknown`` so a permission error never kills the watcher.
    """

    status: str | None = None
    events: tuple[dict[str, Any], ...] = ()
    probe: str = "unknown"
    probe_detail: str = ""
    pid_present: bool = True
    stop_requested: bool = False


@dataclass(frozen=True)
class SteerRequest:
    key: str
    node_id: str
    kind: str
    message: str
    role_target: str = "any"
    ttl_secs: int = _STEER_TTL_SECS


@dataclass(frozen=True)
class WatchDecision:
    actions: tuple[tuple[str, dict[str, Any]], ...] = ()
    steer: SteerRequest | None = None
    exit_reason: str | None = None
    exit_detail: str = ""


def _opt_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _opt_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _opt_str(value: Any) -> str | None:
    return str(value) if value is not None else None


def _float_map(value: Any) -> dict[str, float]:
    if not isinstance(value, dict):
        return {}
    out: dict[str, float] = {}
    for key, entry in value.items():
        number = _opt_float(entry)
        if number is not None:
            out[str(key)] = number
    return out


def _int_map(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    out: dict[str, int] = {}
    for key, entry in value.items():
        try:
            out[str(key)] = int(entry)
        except (TypeError, ValueError):
            continue
    return out


def watch_state_path(run_dir: str | os.PathLike[str]) -> Path:
    return Path(run_dir) / WATCH_STATE_FILE


def load_watch_state(run_dir: str | os.PathLike[str]) -> WatchState:
    """The persisted state, or a fresh one; a corrupt file never raises."""
    try:
        data = json.loads(watch_state_path(run_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return WatchState()
    return WatchState.from_dict(data)


def save_watch_state(run_dir: str | os.PathLike[str], state: WatchState) -> None:
    """Atomically replace the state file, owner-only."""
    path = watch_state_path(run_dir)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(state.to_dict(), sort_keys=True), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def watch_active(
    run_dir: str | os.PathLike[str],
    *,
    now: float | None = None,
    stale_after_s: float | None = None,
) -> bool:
    """True when a live watcher process owns ``run_dir``.

    Readers (the ACP thread, a second spawn attempt) use this to yield to the
    sidecar. ``stale_after_s`` bounds a wedged-but-alive process to one
    interval, after which the older observers resume.
    """
    state = load_watch_state(run_dir)
    if state.ended_reason or state.tick_count <= 0 or state.last_tick is None:
        return False
    now = time.time() if now is None else now
    stale_after = _STALE_AFTER_S if stale_after_s is None else stale_after_s
    if now - state.last_tick > stale_after:
        return False
    return state.pid > 0 and _pid_alive(state.pid)


def watch_steer_message(node_id: str, detail: str = "") -> str:
    """A bounded nudge for a stuck node, built to pass :func:`guard_steer`.

    The detail comes from a log tail and can contain a guard token by
    accident; when it does the note is sent without it rather than dropped.
    """
    advice = (
        "Keep the existing acceptance criteria and outputs unchanged; narrow the "
        "next attempt to the single smallest edit that unblocks the goal, and build "
        "on the work already in the tree rather than restarting."
    )
    base = f"Orchestrator note for {node_id}: this node looks stuck. {advice}"
    if detail:
        with_detail = f"Orchestrator note for {node_id}: {detail}. {advice}"
        if len(with_detail) <= _STEER_MAX_CHARS and guard_steer(with_detail):
            return with_detail
    return base[:_STEER_MAX_CHARS]


def observe(
    *,
    now: float,
    run_id: str,
    run_dir: str | os.PathLike[str],
    inputs: WatchInputs,
    state: WatchState,
    config: WatchConfig | None = None,
    sampler: Any = None,
    judge_fn: Any = None,
    enabled_fn: Any = None,
    budget_fn: Any = None,
) -> WatchDecision:
    """One interval tick: decide what to say, steer, and whether to stop.

    Pure given its inputs and the run dir's files: no DB, no model, no network.
    The shell writes the returned actions and performs the steer; this function
    mutates ``state`` so the caller can persist it either way.
    """
    config = config or WatchConfig.from_env()
    actions: list[tuple[str, dict[str, Any]]] = []
    state.tick_count += 1
    state.last_tick = now
    if state.started_at is None:
        state.started_at = now
    if inputs.status:
        state.last_status = inputs.status

    def ended(reason: str, detail: str = "") -> WatchDecision:
        state.ended_reason = reason
        actions.append(("orchestrator.ended", {
            "reason": detail or reason,
            "exit": reason,
            "status": inputs.status or "",
            "node_id": "",
        }))
        return WatchDecision(tuple(actions), None, reason, detail)

    is_enabled = enabled_fn if enabled_fn is not None else enabled
    if not is_enabled():
        return ended("disabled", "the per-run orchestrator is disabled")
    if config.autonomy == "off":
        return ended("off", "the interval watcher is off (MO_ORCH_WATCH_AUTONOMY=off)")
    elapsed = now - (state.started_at or now)
    if elapsed > config.max_hours * 3600:
        return ended("max-lifetime", f"watched for {config.max_hours:.0f}h")

    from mini_ork import run_status

    # Terminal first: teardown writes the status before it releases ``.pid``,
    # so a probe taken at the same instant can still say ``dead``.
    if inputs.status is not None and run_status.is_terminal(inputs.status):
        return ended(f"terminal:{inputs.status}",
                     f"run finished: {run_status.label(inputs.status)}")
    if inputs.probe == "dead":
        return ended("dispatcher-dead", inputs.probe_detail or "the dispatcher pid is gone")
    if inputs.probe == "finished":
        return ended("finished", inputs.probe_detail or "the run finished without a publish step")

    # A remote run's local logs are not its logs, and a paused run is not
    # stuck. Stay attached: the terminal transition is still worth recording.
    if inputs.probe in ("remote", "paused"):
        state.missing_pid_ticks = 0
        _announce(actions, state, config, inputs)
        return WatchDecision(tuple(actions), None)

    if not inputs.pid_present:
        state.missing_pid_ticks += 1
        if state.missing_pid_ticks >= _MISSING_PID_TICKS:
            return ended("owner-record-gone", "no .pid in the run dir for two ticks")
        _announce(actions, state, config, inputs)
        return WatchDecision(tuple(actions), None)
    state.missing_pid_ticks = 0

    _announce(actions, state, config, inputs)

    from mini_ork.acp_orchestrator import watch

    node = watch.current_node(list(inputs.events))
    if node is None:
        return WatchDecision(tuple(actions), None)

    sample = sampler or watch.sample
    observation = sample(
        Path(run_dir),
        run_id=run_id,
        node_id=str(node.get("node_id") or ""),
        node_type=str(node.get("node_type") or ""),
        started_at=float(node.get("started_at") or 0.0),
        now=now,
    )
    judge = judge_fn or watch.judge
    signals = judge(observation) or []

    node_id = observation.node_id
    new_keys: list[tuple[str, dict[str, str]]] = []
    for signal in signals:
        key = watch.signal_key(node_id, signal)
        if key in state.signaled:
            continue
        state.signaled[key] = now
        new_keys.append((key, signal))
        actions.append(("orchestrator.signal", {
            "reason": str(signal.get("detail") or signal.get("kind") or ""),
            "detail": str(signal.get("detail") or ""),
            "kind": str(signal.get("kind") or ""),
            "node_id": node_id,
            "autonomy": config.autonomy,
            "steers": state.steers,
            "spent_usd": observation.spent_usd,
            "interval_s": config.interval_s,
        }))

    steer = _steer_for(
        new_keys, observation, inputs, state, config, budget_fn or budget_ok,
    )
    return WatchDecision(tuple(actions), steer)


def _announce(
    actions: list[tuple[str, dict[str, Any]]],
    state: WatchState,
    config: WatchConfig,
    inputs: WatchInputs,
) -> None:
    """Emit ``orchestrator.watching`` on the first tick only."""
    if state.tick_count != 1:
        return
    actions.append(("orchestrator.watching", {
        "reason": "watching · no signal yet",
        "node_id": "",
        "autonomy": config.autonomy,
        "interval_s": config.interval_s,
        "status": inputs.status or "",
    }))


def _steer_for(
    new_keys: list[tuple[str, dict[str, str]]],
    observation: Any,
    inputs: WatchInputs,
    state: WatchState,
    config: WatchConfig,
    budget_fn: Any,
) -> SteerRequest | None:
    """The one nudge this tick may send, if every gate is open."""
    if config.autonomy != "steer":
        return None
    from mini_ork import run_status

    if inputs.status not in run_status.LIVE:
        return None
    if inputs.probe != "alive" or inputs.stop_requested:
        return None
    if state.steers >= config.max_steers:
        return None
    if not budget_fn(observation.spent_usd):
        return None
    pending = state.pending or {}
    pending_key = str(pending.get("key") or "")
    if pending_key and state.steer_attempts.get(pending_key, 0) < _MAX_STEER_ATTEMPTS:
        state.steer_attempts[pending_key] = state.steer_attempts.get(pending_key, 0) + 1
        return SteerRequest(
            key=pending_key,
            node_id=str(pending.get("node_id") or observation.node_id),
            kind=str(pending.get("kind") or ""),
            message=str(pending.get("message") or ""),
            role_target=str(pending.get("role_target") or "any"),
        )
    for key, signal in new_keys:
        kind = str(signal.get("kind") or "")
        detail = str(signal.get("detail") or "")
        if kind not in _WATCH_STEERABLE or key in state.steered:
            continue
        if state.steer_attempts.get(key, 0) >= _MAX_STEER_ATTEMPTS:
            continue
        message = watch_steer_message(observation.node_id, detail)
        if not guard_steer(message):
            continue
        state.steer_attempts[key] = state.steer_attempts.get(key, 0) + 1
        request = SteerRequest(
            key=key,
            node_id=observation.node_id,
            kind=kind,
            message=message,
        )
        state.pending = {
            "key": request.key,
            "node_id": request.node_id,
            "kind": request.kind,
            "message": request.message,
            "role_target": request.role_target,
        }
        return request
    return None
