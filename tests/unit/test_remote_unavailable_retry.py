"""RBF-22a — a remote outage is a retryable, named failure.

The run that motivated this file (run-1792101677-31337) hit a 605 s Daytona
exec read timeout while provisioning the implementer's sandbox. The dispatch
seam mapped every escaping exception to the generic finish reason ``error``,
so ``RemoteUnavailableError`` was not retryable, ``diagnose`` fell through to
``unclassified``, and the orchestrator blamed the verifier.

Hermetic by construction: temp dirs only, ambient ``MINI_ORK_*`` run identity
scrubbed, and the live DB is never touched.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from mini_ork.cli import execute as execute_mod
from mini_ork.dispatch import llm_dispatch as llm_dispatch_module
from mini_ork.recovery.diagnose import diagnose
from mini_ork.runtime.backends.remote import RemoteUnavailableError
from mini_ork.workflow.retry import (
    REMOTE_UNAVAILABLE_BACKOFF_SECONDS,
    backoff_for_finish_reason,
    default_retry_policy,
    retryable_finish_reason,
    should_retry,
)


@pytest.fixture(autouse=True)
def _hermetic_env(monkeypatch):
    """Scrub ambient run identity so no code path can reach the live DB/home."""
    for var in ("MINI_ORK_DB", "MINI_ORK_HOME", "MINI_ORK_RUN_ID", "MINI_ORK_RUN_DIR"):
        monkeypatch.delenv(var, raising=False)


def _dispatcher(monkeypatch, behavior):
    """``_default_llm_dispatch`` with its native seam pointed at ``behavior``."""

    def fake_llm_dispatch(argv=None, *, root=None, dispatch_fn=None):
        return behavior(argv, root)

    monkeypatch.setattr(llm_dispatch_module, "llm_dispatch", fake_llm_dispatch)
    return execute_mod._default_llm_dispatch("/nonexistent-root-for-stub")


def _finish_reason(out) -> tuple[int, str]:
    rc = int(out[0])
    if rc == 0:
        return rc, "done"
    return rc, execute_mod.finish_reason_for_failure(rc, out[1])


def _attempt_loop(dispatch, policy):
    """The executor's per-node attempt loop, without sleeping."""
    attempt, reasons, rc, finish_reason = 0, [], 1, ""
    while True:
        attempt += 1
        rc, finish_reason = _finish_reason(
            dispatch("framework-edit", "implementer", "prompt-text"))
        reasons.append(finish_reason)
        if not should_retry(rc, finish_reason, attempt, policy):
            return attempt, reasons, rc, finish_reason


# ── dispatch classification ─────────────────────────────────────────────────


def test_remote_unavailable_error_is_named_and_retried_to_success(monkeypatch):
    calls = []

    def behavior(argv, root):
        calls.append(1)
        if len(calls) == 1:
            raise RemoteUnavailableError("daytona: exec read timeout after 605 s")
        return 0

    dispatch = _dispatcher(monkeypatch, behavior)
    attempts, reasons, rc, finish_reason = _attempt_loop(
        dispatch, default_retry_policy())

    assert attempts == 2
    assert reasons[0] == "remote_unavailable"
    assert (rc, finish_reason) == (0, "done")


def test_three_consecutive_raises_fail_after_two_retries(monkeypatch):
    def behavior(argv, root):
        raise RemoteUnavailableError("node-agent unreachable after retries")

    dispatch = _dispatcher(monkeypatch, behavior)
    attempts, reasons, rc, finish_reason = _attempt_loop(
        dispatch, default_retry_policy())

    assert attempts == 3
    assert reasons == ["remote_unavailable"] * 3
    assert rc != 0
    assert finish_reason == "remote_unavailable"


def test_unrelated_exception_stays_generic_error(monkeypatch):
    def behavior(argv, root):
        raise ValueError("boom")

    dispatch = _dispatcher(monkeypatch, behavior)
    _rc, finish_reason = _finish_reason(
        dispatch("framework-edit", "implementer", "prompt-text"))
    assert finish_reason == "error"


# ── retry policy ────────────────────────────────────────────────────────────


def test_remote_unavailable_is_retryable_with_two_retries():
    policy = default_retry_policy()
    assert retryable_finish_reason("remote_unavailable") is True
    assert should_retry(1, "remote_unavailable", 1, policy) is True
    assert should_retry(1, "remote_unavailable", 2, policy) is True
    assert should_retry(1, "remote_unavailable", 3, policy) is False


def test_remote_unavailable_backoff_is_thirty_seconds():
    assert REMOTE_UNAVAILABLE_BACKOFF_SECONDS == 30.0
    policy = default_retry_policy()
    assert backoff_for_finish_reason(
        policy, "remote_unavailable", attempt=1) == 30.0
    # Another retryable reason keeps the policy's exponential envelope.
    assert backoff_for_finish_reason(policy, "timeout", attempt=1) != 30.0


# ── diagnosis ───────────────────────────────────────────────────────────────


def _failed_run_home(tmp_path, finish_reason, run_id="rbf-remote-run"):
    home = tmp_path / "home"
    (home / "runs" / run_id).mkdir(parents=True)
    con = sqlite3.connect(home / "state.db")
    con.execute(
        "CREATE TABLE run_events ("
        " event_id TEXT PRIMARY KEY, run_id TEXT, event_type TEXT,"
        " payload_json TEXT, created_at REAL, finish_reason TEXT NULL,"
        " last_heartbeat_at INTEGER NULL)"
    )
    rows = [
        (f"{run_id}-e1", run_id, "node_start",
         json.dumps({"node_id": "implementer", "node_type": "implementer"}),
         1000.0, None),
        (f"{run_id}-e2", run_id, "node_end",
         json.dumps({"node_id": "implementer", "node_type": "implementer",
                     "finish_reason": finish_reason}),
         1001.0, None),
    ]
    con.executemany(
        "INSERT INTO run_events(event_id, run_id, event_type, payload_json,"
        " created_at, finish_reason) VALUES (?,?,?,?,?,?)", rows)
    con.commit()
    con.close()
    return home, run_id


def test_diagnose_matches_remote_unavailable_rule(tmp_path):
    home, run_id = _failed_run_home(tmp_path, "remote_unavailable")
    result = diagnose(home, run_id, write=False)

    assert result["rule_id"] == "remote_unavailable"
    assert result["layer"] == "infra"
    assert result["routes_to"] == "retry"
    assert result["strategy"] == "resume"
    assert result["retryable"] is True
    assert result["evidence"]


def test_diagnose_generic_error_does_not_match_remote_rule(tmp_path):
    home, run_id = _failed_run_home(tmp_path, "error")
    result = diagnose(home, run_id, write=False)
    assert result["rule_id"] != "remote_unavailable"
