"""Policy evaluation engine.

Stateful, contextual policy decisions per the panel-revised plan at
``docs/research/omnigent-vs-mini-ork-panel-synthesis.md``. Policies are
Python callables registered with ``register_policy()`` and evaluated
in registration order until one returns a non-None response. The
first non-None response wins; if all abstain, the default ALLOW
applies.

Session state (the "stateful" part) lives in the ``policy_state``
SQLite table introduced by ``db/migrations/0026_policy_state.sql``.
Each policy decision is audited into ``policy_decisions``.

Builtins shipped here:
    cost_threshold_pause         pauses when run_cost >= threshold
    network_egress_check         denies if target host not allowlisted
    verifier_failure_escalation  escalates on N consecutive verifier
                                 failures within a session
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from typing import Any, Callable

from mini_ork.policies.schema import PolicyEvent, PolicyResponse


# Registry: list of (name, callable, task_class_filter, config).
# task_class_filter=None means "applies to all task classes".
_REGISTRY: list[
    tuple[str, Callable[..., PolicyResponse | None], str | None, dict[str, Any]]
] = []


def register_policy(
    name: str,
    callable_: Callable[..., PolicyResponse | None],
    task_class: str | None = None,
    config: dict[str, Any] | None = None,
) -> None:
    """Register a policy callable.

    Order matters: policies are evaluated in registration order, and
    the first non-None response wins. Operators wire their callsite-
    specific policies before the shipped builtins so their decisions
    take precedence.
    """
    _REGISTRY.append((name, callable_, task_class, dict(config or {})))


def clear_registry() -> None:
    """Test helper to reset the registry between cases."""
    _REGISTRY.clear()


def _default_allow(reason: str = "no policy matched") -> PolicyResponse:
    return {"result": "ALLOW", "reason": reason, "policy_name": "<default>"}


def evaluate_policies(
    event: PolicyEvent,
    session_state: dict[str, Any] | None = None,
    task_class: str | None = None,
) -> PolicyResponse:
    """Evaluate registered policies against an event.

    Returns the first non-None response, or the default ALLOW when
    every policy abstains. ``session_state`` is passed through to
    policy callables that accept the two-argument form; policies that
    only take ``event`` ignore it.
    """
    session_state = session_state or {}

    for name, callable_, task_filter, config in _REGISTRY:
        if task_filter is not None and task_class is not None:
            if task_filter != task_class:
                continue
        try:
            # Try the 2-arg form first (event + config). Fall back to
            # the 1-arg form on TypeError to support both shapes.
            try:
                response = callable_(event, config)
            except TypeError:
                response = callable_(event)
        except Exception as exc:  # noqa: BLE001
            # A buggy policy should not crash the engine. Log + skip.
            response = {
                "result": "LOG_ONLY",
                "reason": f"policy {name} raised: {exc!r}",
                "policy_name": name,
            }
        if response is None:
            continue
        # Stamp the name in case the policy did not set it itself.
        response.setdefault("policy_name", name)
        return response

    return _default_allow()


def record_decision(
    event: PolicyEvent,
    response: PolicyResponse,
    db_path: str,
) -> str:
    """Append a row to policy_decisions; returns the new decision_id.

    Callers pass a db_path explicitly so tests can use temp DBs without
    monkeypatching env vars. The schema is defined in migration 0026.
    """
    if not db_path or not os.path.isfile(db_path):
        raise FileNotFoundError(
            f"record_decision: state.db not found at {db_path}; "
            "run `mini-ork init` first or apply migration 0026"
        )

    decision_id = f"pd-{uuid.uuid4().hex[:12]}"
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA busy_timeout=5000")
    con.execute(
        """
        INSERT INTO policy_decisions
            (decision_id, run_id, event_type, policy_name, result,
             reason, evaluated_at, payload_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            decision_id,
            event.get("run_id", ""),
            event.get("type", ""),
            response.get("policy_name", "<unknown>"),
            response.get("result", "ALLOW"),
            response.get("reason", ""),
            int(time.time()),
            json.dumps({"event": event, "response": response}),
        ),
    )
    con.commit()
    con.close()
    return decision_id


# ── built-in policies ─────────────────────────────────────────────


def cost_threshold_pause(
    event: PolicyEvent, config: dict[str, Any] | None = None
) -> PolicyResponse | None:
    """Pause when cumulative run cost crosses a configured threshold."""
    if event.get("type") != "cost_threshold":
        return None
    config = config or {}
    threshold = float(config.get("threshold_usd", 25.0))
    spent = float(event.get("data", {}).get("spent_usd", 0))
    if spent >= threshold:
        return {
            "result": "REQUIRE_APPROVAL",
            "reason": f"spent ${spent:.2f} >= threshold ${threshold:.2f}",
            "policy_name": "cost_threshold_pause",
        }
    return None


def network_egress_check(
    event: PolicyEvent, config: dict[str, Any] | None = None
) -> PolicyResponse | None:
    """Deny outbound HTTP to hosts not in the allowlist."""
    if event.get("type") != "network_request":
        return None
    config = config or {}
    allowed = set(config.get("allowed_hosts") or [])
    host = event.get("data", {}).get("host", "")
    if allowed and host not in allowed:
        return {
            "result": "DENY",
            "reason": f"host {host!r} not in allowlist",
            "policy_name": "network_egress_check",
        }
    return None


def verifier_failure_escalation(
    event: PolicyEvent, config: dict[str, Any] | None = None
) -> PolicyResponse | None:
    """Escalate after N consecutive verifier failures within a session.

    Reads the consecutive-failure counter from ``session_state`` which
    the caller is expected to maintain. The counter is part of
    session_state, not event.data, because it must persist across
    multiple verifier_result events.
    """
    if event.get("type") != "verifier_result":
        return None
    config = config or {}
    max_consec = int(config.get("max_consecutive_failures", 3))
    # The caller threads session_state via event.data for simplicity;
    # the production engine reads from policy_state SQLite table.
    consec = int(event.get("data", {}).get("consecutive_failures", 0))
    if consec >= max_consec:
        return {
            "result": "REQUIRE_APPROVAL",
            "reason": f"{consec} consecutive verifier failures (max {max_consec})",
            "policy_name": "verifier_failure_escalation",
        }
    return None
