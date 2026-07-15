"""E5: recovery trace continuity — one root trace across original + recovered
attempts, with queryable run/node/attempt attributes.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from mini_ork.ported import recovery_trace as rt  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("MINI_ORK_ROOT_TRACE_ID", "MINI_ORK_RECOVERY_CLOSURE",
              "MINI_ORK_RECOVERY_FROM", "MINI_ORK_RECOVERY_REQUEST", "MO_RESUME_SESSION_ID"):
        monkeypatch.delenv(k, raising=False)


def test_trace_root_derived_and_stable_for_a_run(monkeypatch):
    # pin_env=False to test the pure derivation (pinning is sticky by design —
    # once a root is established in a process it propagates to later calls).
    a = rt.root_trace_id("run-xyz", pin_env=False)
    b = rt.root_trace_id("run-xyz", pin_env=False)
    assert a == b and a.startswith("rt-")           # stable across calls for one run
    assert rt.root_trace_id("run-other", pin_env=False) != a   # distinct per run


def test_trace_caller_supplied_root_wins(monkeypatch):
    monkeypatch.setenv("MINI_ORK_ROOT_TRACE_ID", "caller-root-1")
    assert rt.root_trace_id("run-xyz") == "caller-root-1"


def test_trace_root_pinned_into_env_for_propagation(monkeypatch):
    import os
    assert os.environ.get("MINI_ORK_ROOT_TRACE_ID") is None
    got = rt.root_trace_id("run-xyz")
    # pinned so the recover→execute→dispatch(→sandbox) handoff inherits it
    assert os.environ["MINI_ORK_ROOT_TRACE_ID"] == got


def test_trace_recovered_attempt_shares_root_with_original(monkeypatch):
    original = rt.attempt_span_attrs("run-1", "critic", attempt=1)
    # simulate a recovery context for the resumed attempt
    monkeypatch.setenv("MINI_ORK_RECOVERY_FROM", "critic")
    monkeypatch.setenv("MINI_ORK_RECOVERY_REQUEST", "req-9")
    monkeypatch.setenv("MO_RESUME_SESSION_ID", "sess-9")
    recovered = rt.attempt_span_attrs("run-1", "critic", attempt=2, checkpoint_status="failure")

    # same root trace → one trajectory, not two disconnected traces
    assert recovered["trace.root_id"] == original["trace.root_id"]
    assert original["recovery.is_recovery"] is False
    assert recovered["recovery.is_recovery"] is True
    assert recovered["node.attempt"] == 2
    assert recovered["recovery.request_id"] == "req-9"
    assert recovered["resume.session_id"] == "sess-9"
    assert recovered["checkpoint.status"] == "failure"


def test_trace_attrs_expose_queryable_ids(monkeypatch):
    attrs = rt.attempt_span_attrs("run-7", "impl", attempt=3)
    assert attrs["run.id"] == "run-7"
    assert attrs["node.id"] == "impl"
    assert attrs["node.attempt"] == 3
    assert "trace.root_id" in attrs
