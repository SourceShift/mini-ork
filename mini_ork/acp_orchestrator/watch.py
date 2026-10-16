"""Per-run watch: the boundaries to narrate, and the trouble worth a word.

The orchestrator that owns a run narrates it stage by stage — before a node
starts, after it ends, and on the run's terminal transition. Those boundaries
are already in the database (``node_start`` / ``node_end`` in ``run_events``,
read by ``mini_ork.acp.history.read_snapshot``); this module turns the raw rows
into the small list of narration jobs the ACP thread emits, and decides when a
still-running node is in enough trouble to interrupt.

Two sources, deliberately kept apart:

* **Boundaries** come from the events the poll already holds. Free, ordered,
  and exactly the shape the run page renders.
* **Trouble** is a *sample of the run dir* (see :func:`sample`) — the node's log
  mtime, its repeated lines, the run's spend sidecar. It cannot come from the
  heartbeat rows: a hung node still emits ``node_heartbeat`` (the ticker is
  independent of the node's progress), so a heartbeat proves the dispatcher is
  awake, never that the node is moving. Log mtime is the only signal that
  actually distinguishes "working" from "wedged".

Moved here from the deleted ``mini_ork/orchestration/run_buddy.py``: the
observation and its :func:`judge` are the same pure, tested functions, kept
because an LLM turn should not be the thing that notices the same error three
times. Delegating the reporting job does not mean re-deriving the rule.

Nothing here writes, spawns or raises into a run: every entry point returns a
value or ``None`` on any internal error.
"""
from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "COST_OVERRUN",
    "DURATION_OVERRUN",
    "REPEATING_ERROR",
    "STALLED",
    "Observation",
    "boundaries",
    "current_node",
    "judge",
    "narration_budget",
    "payload_of",
    "sample",
    "signal_key",
    "stall_seconds",
]

# ── signal vocabulary ────────────────────────────────────────────────────────
STALLED = "stalled"
REPEATING_ERROR = "repeating_error"
DURATION_OVERRUN = "duration_overrun"
COST_OVERRUN = "cost_overrun"

# ── knobs ────────────────────────────────────────────────────────────────────
_ENV_STALL = "MO_WATCH_STALL_SECONDS"
_ENV_LONG_NODE = "MO_WATCH_LONG_NODE_SECONDS"
_ENV_COST = "MO_WATCH_COST_USD"
_ENV_MAX_TURNS = "MO_WATCH_MAX_TURNS"

_DEFAULT_STALL = 300
_DEFAULT_LONG_NODE = 900
_DEFAULT_COST_USD = 1.0
# A narration turn is a model call: an opening, two per node, the signals that
# trip, and one closing. The cap is a brake on a runaway graph, not a budget —
# it is set well above any realistic node count.
_DEFAULT_MAX_TURNS = 120

# Identical normalized lines in the tail before "the same error on repeat".
_REPEAT_LIMIT = 3
_TAIL_LINES = 200

# Log candidates in the order the node view looks for them
# (``ide_pages.node._overview_command_headline``).
_LOG_TEMPLATES = ("verifier_{node}.log", "evidence/{node}.log", "impl-{node}.log")


def _env_int(name: str, default: int) -> int:
    try:
        value = int(str(os.environ.get(name, "")).strip())
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _env_float(name: str, default: float) -> float:
    try:
        value = float(str(os.environ.get(name, "")).strip())
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def stall_seconds() -> int:
    return _env_int(_ENV_STALL, _DEFAULT_STALL)


def long_node_seconds() -> int:
    """How long a node may run before "long" alone is worth a word."""
    return _env_int(_ENV_LONG_NODE, _DEFAULT_LONG_NODE)


def cost_cap_usd() -> float:
    """The run's spend cap for the watch; falls back to the orchestrator's."""
    default = _env_float("MO_ORCH_BUDGET_USD", _DEFAULT_COST_USD)
    return _env_float(_ENV_COST, default)


def narration_budget() -> int:
    """Most narration turns one watched run may spend."""
    return _env_int(_ENV_MAX_TURNS, _DEFAULT_MAX_TURNS)


# ── boundaries (from the polled events) ──────────────────────────────────────
def payload_of(event: dict[str, Any]) -> dict[str, Any]:
    """The event's payload as a dict; JSON text is parsed, anything else is {}.

    ``run_events.payload_json`` is TEXT in the schema and already-decoded in
    some readers (``RunDetailRepository`` does not decode), so both shapes
    arrive here.
    """
    payload = event.get("payload_json") or {}
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, str):
        try:
            import json

            decoded = json.loads(payload)
        except (ValueError, TypeError):
            return {}
        return decoded if isinstance(decoded, dict) else {}
    return {}


def _node_of(event: dict[str, Any]) -> tuple[str, str]:
    payload = payload_of(event)
    return (
        str(payload.get("node_id") or event.get("node_id") or "node"),
        str(payload.get("node_type") or event.get("node_type") or ""),
    )


def boundary_key(event: dict[str, Any]) -> str | None:
    """Stable key for a node transition, or ``None`` for a non-boundary event.

    Same shape as ``MiniOrkAcpAgent._event_transition_key`` — one node's
    ``start`` and ``end`` are different keys, so both are narrated.
    """
    event_type = str(event.get("event_type") or "")
    if event_type not in ("node_start", "node_end"):
        return None
    node_id, _ = _node_of(event)
    return f"{node_id}:{event_type.removeprefix('node_')}"


def boundaries(
    events: list[dict[str, Any]], seen: set[str]
) -> list[dict[str, Any]]:
    """The events in ``events`` that are new relative to ``seen``, in order.

    Mutates ``seen`` to include every key it returns, so the caller narrates
    each transition exactly once. Each entry is a narration job:
    ``{"key", "phase", "node_id", "node_type", "lane", "verdict",
    "duration_ms", "finish_reason", "created_at"}``.
    """
    out: list[dict[str, Any]] = []
    for event in events or []:
        key = boundary_key(event)
        if key is None or key in seen:
            continue
        seen.add(key)
        payload = payload_of(event)
        node_id, node_type = _node_of(event)
        out.append({
            "key": key,
            "phase": str(event.get("event_type") or "").removeprefix("node_") or "node",
            "node_id": node_id,
            "node_type": node_type,
            "lane": str(payload.get("model_lane") or ""),
            "verdict": str(payload.get("verdict") or ""),
            "duration_ms": payload.get("duration_ms"),
            "finish_reason": str(payload.get("finish_reason") or ""),
            "created_at": event.get("created_at"),
        })
    return out


def prime(events: list[dict[str, Any]], seen: set[str]) -> None:
    """Mark every transition in ``events`` as already narrated.

    Called on the first poll so attaching to a half-finished run does not
    replay its whole history as a burst of narration turns.
    """
    for event in events or []:
        key = boundary_key(event)
        if key is not None:
            seen.add(key)


def current_node(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The node that has started and not ended, or ``None`` when none is live.

    The last unmatched ``node_start`` wins: a recipe runs nodes one at a time,
    so a later start means the earlier one's ``node_end`` was lost rather than
    that two nodes are live.
    """
    live: dict[str, dict[str, Any]] = {}
    for event in events or []:
        event_type = str(event.get("event_type") or "")
        if event_type not in ("node_start", "node_end"):
            continue
        node_id, node_type = _node_of(event)
        if event_type == "node_end":
            live.pop(node_id, None)
            continue
        live[node_id] = {
            "node_id": node_id,
            "node_type": node_type,
            "started_at": float(event.get("created_at") or 0.0),
        }
    if not live:
        return None
    return max(live.values(), key=lambda entry: entry["started_at"])


# ── observation (from the run dir) ───────────────────────────────────────────
@dataclass(frozen=True)
class Observation:
    """One instant of a node's life, read from files only."""

    run_id: str
    node_id: str
    node_type: str
    started_at: float
    now: float
    idle_seconds: float
    spent_usd: float = 0.0
    repeats: int = 0
    repeat_line: str = ""
    last_line: str = ""

    @property
    def elapsed_seconds(self) -> float:
        return max(0.0, self.now - self.started_at)


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def node_log(run_dir: Path, node_id: str) -> Path | None:
    """The node's own log file, when it has one."""
    for template in _LOG_TEMPLATES:
        candidate = run_dir / template.format(node=node_id)
        if candidate.is_file():
            return candidate
    return None


def _normalize(line: str) -> str:
    """A log line with its variable parts masked, so repeats compare equal."""
    stripped = re.sub(r"\d+", "#", line.strip())
    stripped = re.sub(r"0x[0-9a-fA-F]+", "#", stripped)
    return re.sub(r"\s+", " ", stripped)


def _tail(path: Path | None) -> list[str]:
    if path is None:
        return []
    lines = _read_text(path).splitlines()
    return [line for line in lines[-_TAIL_LINES:] if line.strip()]


def _repeat_of(lines: list[str]) -> tuple[int, str]:
    """The most-repeated normalized line and its count (``0, ""`` when none)."""
    counts: dict[str, int] = {}
    for line in lines:
        key = _normalize(line)
        counts[key] = counts.get(key, 0) + 1
    if not counts:
        return 0, ""
    top = max(counts, key=lambda key: counts[key])
    return counts[top], top


def _spent_usd(run_dir: Path) -> float:
    """The run's spend so far, from the ``.cost-spent`` sidecar."""
    try:
        return float(_read_text(run_dir / ".cost-spent").strip() or 0.0)
    except ValueError:
        return 0.0


def sample(
    run_dir: Path,
    *,
    run_id: str,
    node_id: str,
    node_type: str = "",
    started_at: float,
    now: float | None = None,
) -> Observation:
    """Sample everything the signals need. Never raises on a missing file."""
    now = time.time() if now is None else now
    log = node_log(run_dir, node_id)
    lines = _tail(log)
    idle = now - started_at
    if log is not None:
        try:
            idle = now - log.stat().st_mtime
        except OSError:
            pass
    repeats, repeat_line = _repeat_of(lines)
    if repeats < _REPEAT_LIMIT:
        repeats, repeat_line = 0, ""
    return Observation(
        run_id=run_id,
        node_id=node_id,
        node_type=node_type,
        started_at=started_at,
        now=now,
        idle_seconds=max(0.0, idle),
        spent_usd=_spent_usd(run_dir),
        repeats=repeats,
        repeat_line=repeat_line,
        last_line=lines[-1].strip() if lines else "",
    )


# ── judgement (pure) ─────────────────────────────────────────────────────────
def _clock(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


def judge(
    obs: Observation,
    *,
    stall: int | None = None,
    long_node: int | None = None,
    cost_cap: float | None = None,
) -> list[dict[str, str]]:
    """The signals this observation crosses, most urgent first.

    Pure: same observation in, same signals out. An uninteresting node yields
    ``[]`` — the caller then says nothing, which is what keeps a healthy run
    from spending a turn per poll.
    """
    stall = stall_seconds() if stall is None else stall
    long_node = long_node_seconds() if long_node is None else long_node
    cost_cap = cost_cap_usd() if cost_cap is None else cost_cap

    signals: list[dict[str, str]] = []
    if obs.repeats >= _REPEAT_LIMIT:
        signals.append({
            "kind": REPEATING_ERROR,
            "detail": f"the same line {obs.repeats}x: {obs.repeat_line[:160]}",
        })
    if obs.idle_seconds >= stall:
        signals.append({
            "kind": STALLED,
            "detail": f"nothing new for {_clock(obs.idle_seconds)}",
        })
    if obs.elapsed_seconds > long_node:
        signals.append({
            "kind": DURATION_OVERRUN,
            "detail": f"running for {_clock(obs.elapsed_seconds)}",
        })
    if obs.spent_usd > cost_cap:
        signals.append({
            "kind": COST_OVERRUN,
            "detail": f"the run has spent ${obs.spent_usd:.2f} of a ${cost_cap:.2f} cap",
        })
    return signals


def signal_key(node_id: str, signal: dict[str, str]) -> str:
    """Dedupe key for one raised signal on one node.

    A stall that lasts an hour is *one* alert, not sixty: the key deliberately
    omits the measured value, so only a *different* kind of trouble on the same
    node speaks again.
    """
    return f"{node_id}:{signal.get('kind', '')}"
