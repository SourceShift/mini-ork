"""Hermetic tests for the shared node terminal-state classifier.

``mini_ork.acp.node_status.node_end_state`` is the one rule the node view
(``acp.fleet._steps``), the run header (``acp.task_state._node_end_failure``)
and the node tab all read. The regression it exists to prevent: the reaper's
synthetic ``node_end`` (verdict CRASH, no ``finish_reason``) read "done" on the
node view and "CRASH" in the header for the same row
(run-1791930074-82118).

Pure payloads only — no db, no home, no clock.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from mini_ork.acp import node_status as ns  # noqa: E402
from mini_ork.acp import task_state  # noqa: E402


def test_clean_finish_reasons_are_done():
    for finish in ns.TERMINAL_OK_FINISH:
        assert ns.node_end_state({"finish_reason": finish}) == (ns.DONE, "")


def test_explicit_failure_finish_reasons_keep_the_reason():
    for finish in ("error", "timeout", "interrupted", "max_steps", "cost_limit"):
        assert ns.node_end_state({"finish_reason": finish}) == (ns.FAILED, finish)


def test_reaper_kill_close_payload_is_failed():
    # ``web.control._close_dangling_node_events`` before it also wrote
    # ``finish_reason``: verdict + interrupted, no finish_reason. Old rows of
    # this shape must still classify as failed.
    payload = {"node_id": "test", "node_type": "verifier", "verdict": "CRASH",
               "interrupted": True, "duration_ms": 0}
    assert ns.node_end_state(payload) == (ns.FAILED, "CRASH")


def test_failure_signals_individually():
    assert ns.node_end_state({"verdict": "crash"}) == (ns.FAILED, "crash")
    assert ns.node_end_state({"verdict": "REQUEST_CHANGES"}) == (ns.FAILED, "REQUEST_CHANGES")
    assert ns.node_end_state({"error": "boom"}) == (ns.FAILED, "error")
    assert ns.node_end_state({"interrupted": True}) == (ns.FAILED, "interrupted")
    assert ns.node_end_state({"interrupted": "true"}) == (ns.FAILED, "interrupted")


def test_a_missing_finish_reason_with_no_signal_is_not_a_failure():
    # The rule that keeps a code-fix implementer's bare node_end from being
    # blamed: absent finish_reason, no verdict, no error, not interrupted.
    assert ns.node_end_state({}) == (ns.DONE, "")
    assert ns.node_end_state(None) == (ns.DONE, "")
    assert ns.node_end_state({"node_id": "implementer", "node_type": "implementer"}) == (ns.DONE, "")


def test_non_failing_verdict_and_falsy_interrupted_are_done():
    assert ns.node_end_state({"verdict": "approve"}) == (ns.DONE, "")
    assert ns.node_end_state({"interrupted": False}) == (ns.DONE, "")
    assert ns.node_end_state({"interrupted": "0"}) == (ns.DONE, "")


def test_an_explicit_finish_reason_outranks_the_other_signals():
    # Documented precedence, not an accident: an explicit finish_reason is the
    # node's own report and decides on its own.
    assert ns.node_end_state({"finish_reason": "done", "verdict": "CRASH"}) == (ns.DONE, "")
    assert ns.node_end_state({"finish_reason": "verdict_revise", "verdict": "approve"}) == (
        ns.FAILED, "verdict_revise")


def test_task_state_reader_agrees_with_the_classifier():
    """The run-header reader must answer exactly what the classifier says."""
    failed = {"verdict": "CRASH", "interrupted": True, "duration_ms": 0}
    assert task_state._node_end_failure(failed) == "CRASH"
    assert ns.node_end_state(failed) == (ns.FAILED, "CRASH")

    assert task_state._node_end_failure({}) is None
    assert ns.node_end_state({}) == (ns.DONE, "")

    assert task_state._node_end_failure({"finish_reason": "done"}) is None
