"""``retry_hint`` — blame what actually failed (RBF-11 cases a-c).

Audit findings over the 2026-10-13..20 runs:

  * a reviewer **escalate** ("the blocker is environmental / a harness bug")
    was filed as ``needs_change: code`` with the failing verifier's reason,
    because ``escalate`` is absent from ``_RETRY_VERDICTS`` (case a);
  * a plan-stage chain that failed on SHAPE (rc 65) was reported as an auth
    failure on the one lane whose credentials were rejected (case b);
  * a passing, non-gating, skipped typecheck (``pass: true, gating: false,
    status: unverified``) was classified as an environment fault (case c).

Hermetic: a throwaway ``MINI_ORK_HOME`` (tmp_path + sqlite) with only the
tables the hint reads. No network, no provider credentials, no live DB.

Fixtures mirror ``tests/unit/test_retry_hint_prestart.py``.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from mini_ork.recovery import retry_hint

RUN = "run-rht-blame-0001"

_ESCALATE_NOTE = (
    "Blocker is environmental - the sandbox image lacks the pinned CLI; "
    "a person must fix the harness before any code change can be verified"
)


# ── fixtures ───────────────────────────────────────────────────────────────

_SCHEMA = """
CREATE TABLE task_runs (
    id TEXT PRIMARY KEY,
    title TEXT,
    recipe TEXT,
    status TEXT,
    notes TEXT,
    created_at INTEGER,
    ended_at INTEGER,
    trace_id TEXT,
    kickoff_path TEXT
);
CREATE TABLE run_events (
    event_id TEXT PRIMARY KEY,
    run_id TEXT,
    event_type TEXT,
    payload_json TEXT,
    created_at INTEGER
);
CREATE TABLE llm_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT,
    status TEXT,
    model_id TEXT,
    provider TEXT,
    feature_name TEXT,
    actor TEXT,
    error_category TEXT,
    error_message TEXT,
    ts TEXT,
    cost_usd REAL
);
"""


@pytest.fixture
def home(tmp_path: Path) -> Path:
    """A tmp home whose ``state.db`` mirrors what the hint module reads."""
    h = tmp_path / "mini-ork"
    h.mkdir(parents=True)
    con = sqlite3.connect(h / "state.db")
    con.executescript(_SCHEMA)
    con.execute(
        "INSERT INTO task_runs (id, title, recipe, status, notes, created_at,"
        " ended_at, trace_id, kickoff_path) VALUES (?,?,?,?,?,?,?,?,?)",
        (RUN, "blame run", "", "failed", "", 1, 2, "trace-rht", ""),
    )
    # Matched node_start/node_end pairs: the interruption guard declines and
    # the node names fall back to the events.
    ts = 0
    for node in ("implementer", "test_verifier", "reviewer"):
        for event_type in ("node_start", "node_end"):
            ts += 1
            con.execute(
                "INSERT INTO run_events (event_id, run_id, event_type,"
                " payload_json, created_at) VALUES (?,?,?,?,?)",
                (f"evt-{ts:03d}-{event_type}", RUN, event_type,
                 json.dumps({"node_id": node}), ts),
            )
    con.commit()
    con.close()
    (h / "runs" / RUN).mkdir(parents=True)
    return h


def _run_dir(home: Path) -> Path:
    return home / "runs" / RUN


def _write_review(run_dir: Path, node: str, payload: dict[str, Any]) -> None:
    (run_dir / f"review-{node}.json").write_text(
        json.dumps(payload), encoding="utf-8")


def _write_verifier(run_dir: Path, node: str, payload: dict[str, Any]) -> None:
    stem = node.replace("_", "-")
    (run_dir / f"verifier_{stem}.json").write_text(
        json.dumps({**payload, "verifier": stem}), encoding="utf-8")


def _row(lane: str, category: str, message: str) -> dict[str, Any]:
    return {
        "model_id": lane,
        "provider": lane,
        "feature_name": "mini-ork:planner",
        "actor": "planner",
        "error_category": category,
        "error_message": message,
        "ts": "2026-10-20T00:00:00Z",
    }


def _shape_row(lane: str) -> dict[str, Any]:
    return _row(lane, "shape",
                f"planner output failed the shape check on {lane} (not valid JSON)")


def _auth_row(lane: str) -> dict[str, Any]:
    return _row(lane, "auth", "401 unauthorized: invalid api key")


def _unknown_row(lane: str) -> dict[str, Any]:
    return _row(lane, "", "connection error while streaming planner output")


def _escalated_review(verdict: str = "escalate",
                      note: str = _ESCALATE_NOTE) -> dict[str, Any]:
    payload = {"verdict": verdict, "notes": [note]}
    return {**payload, "text": json.dumps(payload)}


# ── case a — a reviewer escalate is a person action ────────────────────────


def test_escalate_review_returns_person_hint() -> None:
    hint = retry_hint._case_escalated(
        Path("/nonexistent"),
        review=_escalated_review(),
        verifier={"pass": False, "status": "fail", "reason": "2 tests failed"},
        failed_name="test_verifier",
        run_id=RUN,
    )
    assert hint is not None
    assert hint["needs_change"]["kind"] == "person"
    assert "Blocker is environmental" in hint["needs_change"]["summary"]
    assert hint["retryable"] is False
    assert hint["strategy"] == "none"
    assert hint["command"] == ""


def test_escalate_verdict_is_case_insensitive() -> None:
    hint = retry_hint._case_escalated(
        Path("/nonexistent"), review=_escalated_review(verdict="ESCALATE"))
    assert hint is not None
    assert hint["needs_change"]["kind"] == "person"


def test_escalate_summary_is_capped_at_300_chars() -> None:
    long_note = "Blocker is environmental - " + "detail " * 120
    hint = retry_hint._case_escalated(
        Path("/nonexistent"), review=_escalated_review(note=long_note))
    assert hint is not None
    summary = hint["needs_change"]["summary"]
    assert len(summary) <= 300
    assert "Blocker is environmental" in summary


def test_needs_revision_review_does_not_escalate() -> None:
    hint = retry_hint._case_escalated(
        Path("/nonexistent"),
        review=_escalated_review(verdict="needs_revision"))
    assert hint is None


def test_compute_prefers_escalate_over_failed_verifier(home: Path) -> None:
    """The exact misroute: an escalate review + a failed verifier must never
    reach ``_case_code`` and blame the code."""
    run_dir = _run_dir(home)
    _write_review(run_dir, "reviewer", _escalated_review())
    _write_verifier(run_dir, "test_verifier",
                    {"pass": False, "status": "fail", "reason": "2 tests failed"})

    hint = retry_hint.compute(home, RUN)

    assert hint is not None
    assert hint["needs_change"]["kind"] == "person"
    assert "Blocker is environmental" in hint["needs_change"]["summary"]


# ── case b — planner shape is not an auth failure ──────────────────────────


def test_planner_lane_declines_mixed_shape_and_auth(home: Path) -> None:
    rows = [_shape_row("glm"), _shape_row("minimax"),
            _shape_row("deepseek_flash"), _auth_row("kimi")]
    assert retry_hint._case_planner_lane(home, RUN, rows) is None


def test_planner_lane_declines_unknown_row(home: Path) -> None:
    rows = [_auth_row("kimi"), _auth_row("kimi"), _auth_row("kimi"),
            _unknown_row("glm")]
    assert retry_hint._case_planner_lane(home, RUN, rows) is None


def test_planner_lane_still_fires_on_auth_only(home: Path) -> None:
    rows = [_auth_row("kimi"), _auth_row("kimi"), _auth_row("kimi")]
    hint = retry_hint._case_planner_lane(home, RUN, rows)
    assert hint is not None
    assert hint["needs_change"]["kind"] == "lane"
    assert hint["needs_change"]["error_kind"] == "auth"


def test_planner_shape_names_the_shape_lanes(home: Path) -> None:
    rows = [_shape_row("glm"), _shape_row("minimax"),
            _shape_row("deepseek_flash")]
    hint = retry_hint._case_planner_shape(home, RUN, rows)
    assert hint is not None
    assert hint["needs_change"]["kind"] == "lane"
    summary = hint["needs_change"]["summary"]
    assert "shape check" in summary.lower()
    for lane in ("glm", "minimax", "deepseek_flash"):
        assert lane in summary
    assert hint["retryable"] is True
    assert hint["strategy"] == "resume"
    # A lane suggestion would make auto-repair switch to a lane that already
    # failed the same shape check.
    assert hint["needs_change"]["suggestions"] == []


def test_planner_shape_fires_on_rc_65_note(home: Path) -> None:
    hint = retry_hint._case_planner_shape(
        home, RUN, [], "planner_dispatch_rc_65; dispatcher gave up")
    assert hint is not None
    assert hint["needs_change"]["kind"] == "lane"
    assert hint["retryable"] is True
    assert hint["strategy"] == "resume"


def test_planner_shape_declines_clean_rows(home: Path) -> None:
    assert retry_hint._case_planner_shape(home, RUN, [_auth_row("kimi")], "") is None


# ── case c — a passing, non-gating typecheck is not an environment fault ───


def _environment(home: Path, envelope: dict[str, Any]) -> Any:
    return retry_hint._case_environment(
        _run_dir(home), "", home, RUN, "typecheck", envelope)


def test_environment_skips_passing_nongating_typecheck(home: Path) -> None:
    assert _environment(
        home, {"pass": True, "gating": False, "status": "unverified"}) is None


def test_environment_skips_envelope_saying_pass_true(home: Path) -> None:
    assert _environment(
        home, {"pass": True, "status": "unverified"}) is None


def test_environment_skips_envelope_saying_gating_false(home: Path) -> None:
    assert _environment(
        home, {"pass": False, "gating": False, "status": "unverified"}) is None


def test_environment_still_fires_for_gating_unverified_failure(home: Path) -> None:
    assert _environment(
        home, {"pass": False, "gating": True, "status": "unverified"}) is not None
