"""Native contract for the executor's optional intervention gate.

The Bash executor treated ``lib/intervention_gate.sh`` as an optional hook and
proceeded when that file was absent. The hook was never implemented or tracked,
so fork closure preserves the only production behavior that existed: proceed.

Keeping this policy behind a named function gives a future implementation a
Python-owned extension point without leaving a dangling Bash source edge.

``intervention_decision`` is the future hook site for the per-node
human-review gate (``mini_ork.gates.node_review.REVIEW_PAUSE_RC`` is the exit
code the executor will use to terminate a parked run). It is a separate
function so the existing ``intervention_gate_check`` keeps its byte-for-byte
contract while the wiring change is staged in a later commit.
"""
from __future__ import annotations

from typing import Any, Mapping


def intervention_gate_check(
    node_id: str,
    node_type: str,
    lane: str,
    node_desc: str,
) -> bool:
    """Return ``True`` to preserve the absent-hook fail-open contract."""
    del node_id, node_type, lane, node_desc
    return True


def intervention_decision(
    run_id: str,
    node_id: str,
    node_type: str,
    lane: str,
    node_desc: str,
    *,
    db_path: str | None = None,
    run_dir: str | None = None,
    env: Mapping[str, str] | None = None,
) -> tuple[str, dict[str, Any]]:
    """Decide whether the next node's dispatch must wait for a human review.

    Delegates to ``mini_ork.gates.node_review.decide`` and never propagates
    an exception: the executor's gate hook must answer a question, not crash.

    Resolution ladder:

    * Loader ``ValueError`` → ``("block", {"error": str(e)})``.
    * Any other exception (e.g. ``sqlite3.OperationalError`` from a DB
      without ``node_checkpoints``) when ``policy.after_nodes`` is non-empty
      → ``("block", {"error": ...})``; the same failure with empty
      ``after_nodes`` → ``("proceed", {})`` (the gate is off, keep going).
    """
    del lane, node_desc  # unused; signature is fixed by the executor wiring

    # Lazy import: keep the existing hook import-free so a future wiring
    # change does not pull node_review into the no-review code path.
    try:
        from mini_ork.gates import node_review
        from mini_ork.gates.review_policy import load_policy
    except Exception as exc:  # noqa: BLE001 — gate hook must never raise
        return ("block", {"error": f"intervention_decision import failed: {exc}"})

    try:
        return node_review.decide(
            run_id,
            node_id,
            db_path=db_path,
            run_dir=run_dir,
            env=env,
        )
    except ValueError as exc:
        return ("block", {"error": str(exc)})
    except Exception as exc:  # noqa: BLE001 — gate hook must never raise
        # Distinguish "gate is off, keep going" from "gate is on, fail
        # closed" without re-raising. Re-loading the policy here is the
        # same call node_review.decide made, so it has the same failure
        # modes; an empty after_nodes still means proceed.
        try:
            policy = load_policy(env=env)
        except Exception as inner:  # noqa: BLE001 — same purpose
            return ("block", {"error": f"{exc}; policy load also failed: {inner}"})
        if policy.after_nodes:
            return ("block", {"error": str(exc)})
        return ("proceed", {})
