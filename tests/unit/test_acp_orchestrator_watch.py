"""Hermetic tests for the per-run watch (``mini_ork.acp_orchestrator.watch``).

This module is the *detection* half of the per-run orchestrator: the stage
boundaries it narrates and the trouble worth interrupting for. Both were moved
out of the deleted run buddy, and the contract is the same one — mostly about
*silence*. A healthy node yields ``[]`` from :func:`judge`, and a boundary is
narrated exactly once, so a long healthy run costs two turns per node and
nothing in between.

Every case drives the clock explicitly; nothing sleeps.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from mini_ork.acp_orchestrator import watch  # noqa: E402

NOW = 1_800_000_000


@pytest.fixture
def run_dir(tmp_path: Path) -> Path:
    d = tmp_path / ".mini-ork" / "runs" / "r1"
    d.mkdir(parents=True)
    return d


def _log(run_dir: Path, node: str, lines: list[str], *, mtime: float | None = None) -> Path:
    path = run_dir / f"verifier_{node}.log"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if mtime is not None:
        import os

        os.utime(path, (mtime, mtime))
    return path


def _event(event_type: str, node_id: str, *, created_at: float = NOW, **payload) -> dict:
    return {
        "event_type": event_type,
        "created_at": created_at,
        "payload_json": json.dumps({"node_id": node_id, **payload}),
    }


# ── boundaries ───────────────────────────────────────────────────────────────
def test_boundaries_returns_each_transition_once():
    seen: set[str] = set()
    events = [_event("node_start", "planner"), _event("node_end", "planner"),
              _event("node_start", "implementer")]
    jobs = watch.boundaries(events, seen)
    assert [j["key"] for j in jobs] == ["planner:start", "planner:end", "implementer:start"]
    assert [j["phase"] for j in jobs] == ["start", "end", "start"]
    # The same events again are old news: no second narration turn.
    assert watch.boundaries(events, seen) == []


def test_boundaries_ignores_non_lifecycle_events():
    seen: set[str] = set()
    events = [_event("node_heartbeat", "planner"), _event("run_note", "planner")]
    assert watch.boundaries(events, seen) == []
    assert seen == set()


def test_prime_swallows_the_history_of_a_run_already_under_way():
    seen: set[str] = set()
    events = [_event("node_start", "a"), _event("node_end", "a"), _event("node_start", "b")]
    watch.prime(events, seen)
    assert watch.boundaries(events, seen) == []
    # ...but the *next* transition still speaks.
    later = events + [_event("node_end", "b")]
    assert [j["key"] for j in watch.boundaries(later, seen)] == ["b:end"]


def test_a_boundary_job_carries_the_result_fields():
    seen: set[str] = set()
    events = [_event("node_end", "test", verdict="pass", duration_ms=1234,
                     model_lane="codex", finish_reason="stop")]
    job = watch.boundaries(events, seen)[0]
    assert job["node_id"] == "test"
    assert job["verdict"] == "pass"
    assert job["duration_ms"] == 1234
    assert job["lane"] == "codex"
    assert job["finish_reason"] == "stop"


def test_payload_of_reads_both_the_text_and_the_decoded_shape():
    assert watch.payload_of({"payload_json": {"node_id": "x"}}) == {"node_id": "x"}
    assert watch.payload_of({"payload_json": '{"node_id": "x"}'}) == {"node_id": "x"}
    assert watch.payload_of({"payload_json": "{not json"}) == {}
    assert watch.payload_of({}) == {}


# ── the live node ────────────────────────────────────────────────────────────
def test_current_node_is_the_last_unmatched_start():
    events = [_event("node_start", "a", created_at=NOW),
              _event("node_end", "a"),
              _event("node_start", "b", created_at=NOW + 10)]
    live = watch.current_node(events)
    assert live is not None and live["node_id"] == "b"


def test_current_node_is_none_when_everything_ended():
    assert watch.current_node([_event("node_start", "a"), _event("node_end", "a")]) is None
    assert watch.current_node([]) is None


def test_a_lost_end_does_not_leave_two_nodes_live():
    # A recipe runs nodes one at a time: a later start supersedes, it does not
    # mean both are running.
    events = [_event("node_start", "a", created_at=NOW),
              _event("node_start", "b", created_at=NOW + 5)]
    live = watch.current_node(events)
    assert live is not None and live["node_id"] == "b"


# ── the observation ──────────────────────────────────────────────────────────
def test_a_healthy_node_yields_no_signals(run_dir: Path):
    _log(run_dir, "test", ["running 12 checks", "all good"], mtime=NOW)
    obs = watch.sample(run_dir, run_id="r1", node_id="test", node_type="verifier",
                       started_at=NOW - 10, now=NOW)
    assert watch.judge(obs) == []


def test_a_missing_log_falls_back_to_the_start_time(run_dir: Path):
    obs = watch.sample(run_dir, run_id="r1", node_id="test", started_at=NOW - 400, now=NOW)
    assert obs.idle_seconds == 400
    assert watch.STALLED in [s["kind"] for s in watch.judge(obs)]


def test_stalled_progress(run_dir: Path):
    _log(run_dir, "test", ["still going"], mtime=NOW - 400)
    obs = watch.sample(run_dir, run_id="r1", node_id="test", node_type="verifier",
                       started_at=NOW - 500, now=NOW)
    kinds = [s["kind"] for s in watch.judge(obs)]
    assert kinds[0] == watch.STALLED
    assert "6m40s" in watch.judge(obs)[0]["detail"]


def test_repeating_error_needs_the_same_line_three_times(run_dir: Path):
    line = "E   ConnectionError: upstream refused"
    _log(run_dir, "test", [line] * 4, mtime=NOW)
    obs = watch.sample(run_dir, run_id="r1", node_id="test", started_at=NOW - 20, now=NOW)
    kinds = [s["kind"] for s in watch.judge(obs)]
    assert watch.REPEATING_ERROR in kinds
    # Twice is not a loop.
    _log(run_dir, "test", [line] * 2, mtime=NOW)
    obs = watch.sample(run_dir, run_id="r1", node_id="test", started_at=NOW - 20, now=NOW)
    assert watch.REPEATING_ERROR not in [s["kind"] for s in watch.judge(obs)]


def test_lines_that_differ_only_in_numbers_count_as_the_same_line(run_dir: Path):
    _log(run_dir, "test", [f"E   timeout after {n}s" for n in (1, 2, 3)], mtime=NOW)
    obs = watch.sample(run_dir, run_id="r1", node_id="test", started_at=NOW - 20, now=NOW)
    assert watch.REPEATING_ERROR in [s["kind"] for s in watch.judge(obs)]


def test_duration_overrun(run_dir: Path):
    long_node = watch.long_node_seconds()
    _log(run_dir, "test", ["working"], mtime=NOW)
    obs = watch.sample(run_dir, run_id="r1", node_id="test", started_at=NOW - (long_node + 60),
                       now=NOW)
    assert watch.DURATION_OVERRUN in [s["kind"] for s in watch.judge(obs)]


def test_cost_overrun_reads_the_sidecar(run_dir: Path):
    (run_dir / ".cost-spent").write_text("3.50", encoding="utf-8")
    _log(run_dir, "test", ["working"], mtime=NOW)
    obs = watch.sample(run_dir, run_id="r1", node_id="test", started_at=NOW - 5, now=NOW)
    signals = watch.judge(obs)
    assert [s["kind"] for s in signals] == [watch.COST_OVERRUN]
    assert "$3.50" in signals[0]["detail"]


def test_a_junk_cost_sidecar_is_not_a_signal(run_dir: Path):
    (run_dir / ".cost-spent").write_text("not a number", encoding="utf-8")
    _log(run_dir, "test", ["working"], mtime=NOW)
    obs = watch.sample(run_dir, run_id="r1", node_id="test", started_at=NOW - 5, now=NOW)
    assert watch.judge(obs) == []


def test_signals_are_returned_most_urgent_first(run_dir: Path):
    (run_dir / ".cost-spent").write_text("9.99", encoding="utf-8")
    _log(run_dir, "test", ["E   boom"] * 3, mtime=NOW - 400)
    obs = watch.sample(run_dir, run_id="r1", node_id="test", started_at=NOW - 1200, now=NOW)
    kinds = [s["kind"] for s in watch.judge(obs)]
    assert kinds == [watch.REPEATING_ERROR, watch.STALLED, watch.DURATION_OVERRUN,
                     watch.COST_OVERRUN]


# ── one alert, not sixty ─────────────────────────────────────────────────────
def test_signal_key_ignores_the_measured_value():
    first = watch.signal_key("test", {"kind": watch.STALLED, "detail": "nothing new for 5m00s"})
    an_hour_later = watch.signal_key("test", {"kind": watch.STALLED,
                                              "detail": "nothing new for 1h00m"})
    assert first == an_hour_later == "test:stalled"
    # A *different* kind of trouble on the same node still speaks.
    assert watch.signal_key("test", {"kind": watch.COST_OVERRUN}) != first


# ── the knobs ────────────────────────────────────────────────────────────────
def test_knobs_fall_back_on_junk(monkeypatch: pytest.MonkeyPatch):
    for name in ("MO_WATCH_STALL_SECONDS", "MO_WATCH_LONG_NODE_SECONDS", "MO_WATCH_MAX_TURNS"):
        monkeypatch.setenv(name, "not-a-number")
    monkeypatch.setenv("MO_WATCH_COST_USD", "")
    monkeypatch.delenv("MO_ORCH_BUDGET_USD", raising=False)
    assert watch.stall_seconds() == 300
    assert watch.long_node_seconds() == 900
    assert watch.narration_budget() == 120
    assert watch.cost_cap_usd() == 1.0


def test_a_knob_of_zero_is_not_a_zero_window(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MO_WATCH_STALL_SECONDS", "0")
    assert watch.stall_seconds() == 300


def test_the_cost_cap_falls_back_to_the_orchestrator_budget(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("MO_WATCH_COST_USD", raising=False)
    monkeypatch.setenv("MO_ORCH_BUDGET_USD", "2.5")
    assert watch.cost_cap_usd() == 2.5
    monkeypatch.setenv("MO_WATCH_COST_USD", "0.4")
    assert watch.cost_cap_usd() == 0.4
