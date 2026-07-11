"""Goodhart-guard for the outcome-gated process_reward (proof-integrity).

Before: activity (tool_calls>0, files>0) + duration were added even to FAILED
traces, so a busy/timely failure scored ~0.25 — muddying the success/failure
signal the GRPO group-relative advantage learns from. After: all activity/
timeliness/verdict credit is gated on status=='success'; failures score 0.

These assertions FAIL if activity ever leaks reward back into a failed trace.
"""
from __future__ import annotations

import json

from mini_ork.learning.process_reward import score_trace


def _t(status, *, tool=0, files=0, dur=30000, verdict="", cost=0.01):
    return {
        "status": status,
        "tool_calls": json.dumps([{}] * tool),
        "files_written": json.dumps([{}] * files),
        "files_read": "[]",
        "duration_ms": dur,
        "reviewer_verdict": verdict,
        "cost_usd": cost,
    }


def test_busy_failure_scores_zero():
    # a FAILED trace that made 5 tool calls, wrote 3 files, ran a reasonable time,
    # and even drew an (erroneous) approve — earns NOTHING. This is the fix.
    assert score_trace(_t("failed", tool=5, files=3, dur=30000, verdict="approve")) == 0.0


def test_bare_success_beats_busiest_failure():
    bare_success = score_trace(_t("success", tool=0, files=0, dur=0))
    busiest_failure = score_trace(_t("failed", tool=99, files=99, dur=30000, verdict="approve"))
    assert bare_success == 0.50           # exactly W_STATUS — no activity/cost bonus
    assert busiest_failure == 0.0
    assert bare_success > busiest_failure  # the separation the old reward blurred


def test_success_gradient_is_outcome_dominant():
    bare = score_trace(_t("success", tool=0, files=0, dur=0))
    active = score_trace(_t("success", tool=3, files=2, dur=30000))
    approved = score_trace(_t("success", tool=3, files=2, dur=30000, verdict="approve"))
    assert bare == 0.50
    assert active == 0.70                  # + capped activity(0.15) + duration(0.05)
    assert approved == 1.00                # + verdict(0.30)
    assert bare < active < approved <= 1.0


def test_cost_is_no_longer_rewarded():
    # cost>0 must contribute nothing (retired term) — a bare success is exactly 0.50
    assert score_trace(_t("success", tool=0, files=0, dur=0, cost=5.0)) == 0.50
