"""Hermetic tests for the run buddy (``mini_ork.orchestration.run_buddy``).

The contract under test is mostly about *silence*: the feature is off unless
explicitly enabled, a healthy node writes nothing, and no report — or missing
model, or raising sampler — can ever propagate into the run. Every case drives
the clock explicitly; nothing sleeps.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from mini_ork.orchestration import run_buddy as rb  # noqa: E402

NOW = 1_800_000_000


@pytest.fixture
def home(tmp_path: Path) -> Path:
    h = tmp_path / ".mini-ork"
    (h / "runs" / "r1").mkdir(parents=True)
    return h


@pytest.fixture
def enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MO_BUDDY", "1")


@pytest.fixture
def no_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """No provider in tests: the template path is what ships."""
    monkeypatch.setattr(rb, "narrate", lambda *a, **k: "")


def _log(home: Path, node: str, lines: list[str], *, mtime: float | None = None) -> Path:
    path = home / "runs" / "r1" / f"verifier_{node}.log"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if mtime is not None:
        import os

        os.utime(path, (mtime, mtime))
    return path


def _reports(home: Path) -> list[dict]:
    return rb.read_reports(home / "runs" / "r1")


# ── the flag ─────────────────────────────────────────────────────────────────
def test_off_unless_explicitly_enabled(home: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("MO_BUDDY", raising=False)
    assert rb.enabled() is False
    assert rb.assess(home, run_id="r1", node_id="test", started_at=NOW,
                     now=NOW, only_when_worried=False) is None
    assert _reports(home) == []
    # Even an explicit off is off (a typo must not turn it on).
    monkeypatch.setenv("MO_BUDDY", "0")
    assert rb.enabled() is False


def test_knobs_fall_back_to_defaults(home: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MO_BUDDY", "1")
    monkeypatch.delenv("MO_BUDDY_INTERVAL_SECONDS", raising=False)
    monkeypatch.setenv("MO_BUDDY_INTERVAL_SECONDS", "not-a-number")
    monkeypatch.delenv("MO_BUDDY_COST_USD", raising=False)
    assert rb.interval_seconds() == 180
    monkeypatch.setenv("MO_ORCH_BUDGET_USD", "2.5")
    assert rb.cost_cap_usd() == 2.5
    monkeypatch.setenv("MO_BUDDY_COST_USD", "0.4")
    assert rb.cost_cap_usd() == 0.4


# ── silence when healthy ─────────────────────────────────────────────────────
def test_a_healthy_node_writes_nothing(home: Path, enabled: None, no_model: None):
    _log(home, "test", ["running 12 checks", "all good"], mtime=NOW)
    assert rb.assess(home, run_id="r1", node_id="test", node_type="verifier",
                     started_at=NOW - 10, now=NOW) is None
    assert _reports(home) == []


def test_a_finished_stage_always_gets_a_line(home: Path, enabled: None, no_model: None):
    _log(home, "implementer", ["wrote 3 files"], mtime=NOW)
    report = rb.report_boundary(home, run_id="r1", node_id="implementer",
                                node_type="implementer", started_at=NOW - 60, now=NOW)
    assert report is not None
    assert report["phase"] == "stage"
    assert report["signals"] == []
    assert "finished its stage" in report["summary"]
    assert len(_reports(home)) == 1


def test_a_failed_boundary_says_failed_not_finished(home: Path, enabled: None, no_model: None):
    _log(home, "test", ["AssertionError: 3 != 4"], mtime=NOW)
    report = rb.report_boundary(home, run_id="r1", node_id="test", node_type="verifier",
                                started_at=NOW - 30, now=NOW, failed=True)
    assert report is not None
    assert rb.FAILED in report["signals"]
    assert "failed" in report["summary"]
    assert "AssertionError" in report["summary"]
    assert any(a["label"].startswith("Resume from") for a in report["actions"])


# ── the four signals ─────────────────────────────────────────────────────────
def test_stalled_progress(home: Path, enabled: None, no_model: None):
    _log(home, "test", ["still going"], mtime=NOW - 400)
    report = rb.assess(home, run_id="r1", node_id="test", node_type="verifier",
                       started_at=NOW - 500, now=NOW)
    assert report is not None
    assert report["signals"] == [rb.STALLED]
    assert "quiet" in report["summary"]


def test_repeating_error(home: Path, enabled: None, no_model: None):
    line = "E   ConnectionError: upstream refused"
    _log(home, "test", [line] * 4, mtime=NOW)
    report = rb.assess(home, run_id="r1", node_id="test", node_type="verifier",
                       started_at=NOW - 20, now=NOW)
    assert report is not None
    assert rb.REPEATING_ERROR in report["signals"]
    assert "same failure" in report["summary"]


def test_duration_overrun(home: Path, enabled: None, no_model: None):
    long_node = rb.long_node_seconds()
    _log(home, "test", ["working"], mtime=NOW)
    report = rb.assess(home, run_id="r1", node_id="test", node_type="verifier",
                       started_at=NOW - (long_node + 60), now=NOW)
    assert report is not None
    assert rb.DURATION_OVERRUN in report["signals"]
    assert "taking a long time" in report["summary"]


def test_cost_overrun(home: Path, enabled: None, no_model: None):
    (home / "runs" / "r1" / ".cost-spent").write_text("3.50", encoding="utf-8")
    _log(home, "test", ["working"], mtime=NOW)
    report = rb.assess(home, run_id="r1", node_id="test", node_type="verifier",
                       started_at=NOW - 5, now=NOW)
    assert report is not None
    assert report["signals"] == [rb.COST_OVERRUN]
    assert "$3.50" in report["summary"]


def test_two_signals_speak_once_with_both_listed(home: Path, enabled: None, no_model: None):
    (home / "runs" / "r1" / ".cost-spent").write_text("9.99", encoding="utf-8")
    _log(home, "test", ["E   boom"] * 3, mtime=NOW - 400)
    report = rb.assess(home, run_id="r1", node_id="test", node_type="verifier",
                       started_at=NOW - 900, now=NOW)
    assert report is not None
    assert len(_reports(home)) == 1
    assert rb.REPEATING_ERROR in report["signals"]
    assert rb.STALLED in report["signals"]
    assert rb.COST_OVERRUN in report["signals"]


# ── the in-stage ticker ──────────────────────────────────────────────────────
def test_watch_ticks_silently_until_a_signal(home: Path, enabled: None, no_model: None):
    watch = rb.Watch(home, run_id="r1", node_id="test", node_type="verifier", started_at=NOW)
    _log(home, "test", ["working"], mtime=NOW)
    assert watch.tick(now=NOW + 60) is None
    assert _reports(home) == []
    # Nothing new for longer than the stall window: now it speaks, once.
    assert watch.tick(now=NOW + 900) is not None
    assert len(_reports(home)) == 1


def test_watch_start_is_a_noop_when_disabled(home: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("MO_BUDDY", raising=False)
    watch = rb.Watch(home, run_id="r1", node_id="test", started_at=NOW)
    watch.start()
    watch.stop()  # must not raise, must not have written anything
    assert _reports(home) == []


# ── the report can never break a run ─────────────────────────────────────────
def test_a_raising_sampler_is_swallowed(home: Path, enabled: None, monkeypatch: pytest.MonkeyPatch):
    def boom(*args, **kwargs):
        raise RuntimeError("sampler exploded")

    monkeypatch.setattr(rb, "observe", boom)
    assert rb.assess(home, run_id="r1", node_id="test", started_at=NOW, now=NOW) is None
    assert rb.report_boundary(home, run_id="r1", node_id="test", started_at=NOW, now=NOW) is None


def test_the_template_carries_the_report_with_no_model(home: Path, enabled: None):
    """With narration empty the report is still complete — that is the simple tier."""
    _log(home, "test", ["E   boom"] * 3, mtime=NOW)
    obs = rb.observe(home / "runs" / "r1", run_id="r1", node_id="test",
                     node_type="verifier", started_at=NOW - 20, now=NOW)
    signals = rb.judge(obs)
    report = rb._report(obs, signals, phase="in-stage", narrative="")
    assert report["summary"] and report["next"]
    assert report["narrative"] == ""


def test_narrate_returns_empty_without_a_lane(home: Path, enabled: None):
    obs = rb.observe(home / "runs" / "r1", run_id="r1", node_id="test", started_at=NOW, now=NOW)
    assert rb.narrate(obs, [{"kind": rb.STALLED, "detail": "quiet"}]) == ""


# ── persistence ──────────────────────────────────────────────────────────────
def test_reports_are_append_only_and_ordered(home: Path, enabled: None, no_model: None):
    _log(home, "test", ["working"], mtime=NOW)
    rb.report_boundary(home, run_id="r1", node_id="a", started_at=NOW, now=NOW)
    rb.report_boundary(home, run_id="r1", node_id="b", started_at=NOW, now=NOW + 5)
    reports = _reports(home)
    assert [r["node_id"] for r in reports] == ["a", "b"]
    assert rb.latest_report(home / "runs" / "r1")["node_id"] == "b"
    # ``latest.json`` is a pointer, not a report.
    pointer = json.loads((home / "runs" / "r1" / "buddy" / "latest.json").read_text())
    assert pointer["node_id"] == "b"


def test_same_second_reports_do_not_overwrite(home: Path, enabled: None, no_model: None):
    _log(home, "test", ["working"], mtime=NOW)
    rb.report_boundary(home, run_id="r1", node_id="a", started_at=NOW, now=NOW)
    rb.report_boundary(home, run_id="r1", node_id="b", started_at=NOW, now=NOW)
    assert {r["node_id"] for r in _reports(home)} == {"a", "b"}


def test_unreadable_report_files_are_skipped(home: Path, enabled: None, no_model: None):
    _log(home, "test", ["working"], mtime=NOW)
    rb.report_boundary(home, run_id="r1", node_id="a", started_at=NOW, now=NOW)
    (home / "runs" / "r1" / "buddy" / "999.json").write_text("{not json", encoding="utf-8")
    assert [r["node_id"] for r in _reports(home)] == ["a"]
