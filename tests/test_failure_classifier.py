"""Unit tests for the failure-class state machine (E3).

Pins the design §5 contract + the kickoff acceptance criteria:
  * max-turns stop  → provider_limit, NOT auto-recoverable (no unbounded retry)
  * only `terminal`  marks the run failed; the other four leave a recovery record
  * infra signals    → infra_interrupt, the ONLY auto-recoverable class
  * unknown/ambiguous → terminal (fail-closed default)
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from mini_ork.learning import failure_classifier as fc


# ── the load-bearing rule: max-turns is provider_limit, never auto-retry ───
def test_max_turns_hit_is_provider_limit_not_infra():
    assert fc.classify(max_turns_hit=True) == fc.PROVIDER_LIMIT


def test_max_turns_reason_string_is_provider_limit():
    assert fc.classify(reason="agent stopped: max-turns reached") == fc.PROVIDER_LIMIT
    assert fc.classify(reason="hit turn limit") == fc.PROVIDER_LIMIT


def test_provider_limit_is_not_auto_recoverable():
    # explicit + budget-bounded only — must never become an auto-retry loop
    assert fc.auto_recoverable(fc.PROVIDER_LIMIT) is False
    assert fc.recovery_policy(fc.PROVIDER_LIMIT)["needs_llm"] is True


# ── only terminal marks the run failed; the other four leave a record ──────
def test_only_terminal_marks_run_failed():
    assert fc.marks_run_failed(fc.TERMINAL) is True
    for c in (fc.INFRA_INTERRUPT, fc.PROVIDER_LIMIT, fc.OUTPUT_INVALID, fc.INPUT_REQUIRED):
        assert fc.marks_run_failed(c) is False, c


def test_four_non_terminal_leave_recovery_record():
    for c in (fc.INFRA_INTERRUPT, fc.PROVIDER_LIMIT, fc.OUTPUT_INVALID, fc.INPUT_REQUIRED):
        assert fc.leaves_recovery_record(c) is True, c
    assert fc.leaves_recovery_record(fc.TERMINAL) is False


# ── infra is the only auto-recoverable class ───────────────────────────────
def test_infra_is_only_auto_recoverable():
    assert fc.auto_recoverable(fc.INFRA_INTERRUPT) is True
    for c in (fc.PROVIDER_LIMIT, fc.OUTPUT_INVALID, fc.INPUT_REQUIRED, fc.TERMINAL):
        assert fc.auto_recoverable(c) is False, c


def test_sigkill_exit_is_infra():
    assert fc.classify(exit_code=137) == fc.INFRA_INTERRUPT     # 128+9 OOM
    assert fc.classify(exit_code=143) == fc.INFRA_INTERRUPT     # 128+15 SIGTERM
    assert fc.classify(signal=9) == fc.INFRA_INTERRUPT
    assert fc.classify(reason="worker died: sandbox teardown") == fc.INFRA_INTERRUPT
    assert fc.classify(stderr="connection reset by peer") == fc.INFRA_INTERRUPT


# ── provider status codes ──────────────────────────────────────────────────
def test_rate_limit_status_is_provider_limit():
    assert fc.classify(provider_status=429) == fc.PROVIDER_LIMIT
    assert fc.classify(provider_status=529) == fc.PROVIDER_LIMIT
    assert fc.classify(reason="glm 429 Fair Usage") == fc.PROVIDER_LIMIT


# ── output-invalid → repair (explicit LLM) ─────────────────────────────────
def test_malformed_output_is_output_invalid():
    assert fc.classify(reason="failed to parse response as JSON") == fc.OUTPUT_INVALID
    assert fc.classify(stderr="Expecting value: line 1 column 1") == fc.OUTPUT_INVALID
    assert fc.recovery_policy(fc.OUTPUT_INVALID)["needs_llm"] is True
    assert fc.auto_recoverable(fc.OUTPUT_INVALID) is False


# ── input-required → human/planner, not a retry ────────────────────────────
def test_input_required_class():
    assert fc.classify(reason="planner needs_answers before dispatch") == fc.INPUT_REQUIRED
    assert fc.classify(reason="blocked on question from profile gate") == fc.INPUT_REQUIRED
    assert fc.recovery_policy(fc.INPUT_REQUIRED)["needs_llm"] is False
    assert fc.marks_run_failed(fc.INPUT_REQUIRED) is False


# ── explicit terminal wins even against other signals ──────────────────────
def test_explicit_terminal_wins():
    assert fc.classify(reason="unrecoverable: poisoned state") == fc.TERMINAL
    # terminal beats an infra-looking exit code when the reason says terminal
    assert fc.classify(reason="fatal non-retryable error", exit_code=137) == fc.TERMINAL


# ── fail-closed default ────────────────────────────────────────────────────
def test_unknown_stop_defaults_to_terminal():
    assert fc.classify() == fc.TERMINAL
    assert fc.classify(reason="something we have never seen") == fc.TERMINAL
    # fail-closed = never auto-recover an unclassifiable stop
    assert fc.auto_recoverable(fc.classify(reason="???")) is False


def test_unknown_class_string_treated_as_terminal():
    pol = fc.recovery_policy("not-a-real-class")
    assert pol["marks_run_failed"] is True
    assert pol["auto_recoverable"] is False


def test_all_classes_have_a_policy():
    for c in fc.ALL_CLASSES:
        pol = fc.recovery_policy(c)
        assert set(pol.keys()) == {"auto_recoverable", "needs_llm", "marks_run_failed", "leaves_record"}
