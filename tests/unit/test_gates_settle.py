"""RB-17 / RB-19 — settle a published run's gates; repair budget counts repair spend.

Three independent behaviors, each over a hermetic tmp sqlite DB (never the
operator's live ``state.db``):

1. ``retry_notify.settle_gates_for_run`` marks every still-``pending`` gate of
   the run ``superseded`` and leaves another run's gate untouched.
2. ``auto_repair.decide`` returns ``none`` / ``review-gated`` for a ``reviewing``
   run or one carrying ``.review-pause`` — even with a stale withheld verdict.
3. The repair budget compares spend since the first attempt's snapshot, not the
   run's lifetime ``task_runs.cost_usd``.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from mini_ork.gates import oversight_inbox
from mini_ork.recovery import auto_repair, retry_notify

REASON = "run published — gate settled by test"

_DDL = """
CREATE TABLE IF NOT EXISTS task_runs (
  id TEXT PRIMARY KEY,
  status TEXT,
  cost_usd REAL NOT NULL DEFAULT 0,
  recipe TEXT NOT NULL DEFAULT '',
  notes TEXT NOT NULL DEFAULT '',
  created_at INTEGER,
  updated_at INTEGER,
  ended_at INTEGER,
  duration_ms INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS run_events (
  event_id TEXT PRIMARY KEY,
  run_id TEXT,
  event_type TEXT,
  payload_json TEXT,
  created_at INTEGER
);
"""


def _home(tmp_path: Path, run_id: str, *, status: str = "published",
          cost: float = 0.0) -> Path:
    home = tmp_path / "home"
    run_dir = home / "runs" / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "run_profile.json").write_text(
        json.dumps({"recipe": "framework-edit", "run_id": run_id}),
        encoding="utf-8")
    con = sqlite3.connect(home / "state.db")
    con.executescript(_DDL)
    now = int(time.time())
    con.execute(
        "INSERT INTO task_runs (id, status, cost_usd, recipe, created_at, "
        "updated_at) VALUES (?, ?, ?, 'framework-edit', ?, ?)",
        (run_id, status, cost, now, now))
    con.commit()
    con.close()
    return home


def _hermetic(monkeypatch, home: Path) -> None:
    for key in ("MINI_ORK_RUN_ID", "MINI_ORK_RUN_DIR", "MINI_ORK_ROOT"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    monkeypatch.setenv("MINI_ORK_DB", str(home / "state.db"))


def _gate(home: Path, feature: str, *, gate_id: str, blocks: str = "") -> int:
    return oversight_inbox.enqueue(
        gate_id=gate_id,
        feature=feature,
        phase="publish",
        context={"run_id": feature},
        blocks_dispatch_for=blocks,
        db_path=str(home / "state.db"),
    )


def _row(home: Path, inbox_id: int) -> dict:
    row = oversight_inbox.get(inbox_id, db_path=str(home / "state.db"))
    assert row is not None, f"gate row {inbox_id} vanished"
    return row


# ── 1. settle ────────────────────────────────────────────────────────────────


def test_settle_marks_only_the_runs_pending_gates_superseded(tmp_path,
                                                             monkeypatch):
    home = _home(tmp_path, "rp-e6-graph-focus")
    _hermetic(monkeypatch, home)
    mine = _gate(home, "rp-e6-graph-focus", gate_id="rp-e3-header",
                 blocks="dispatch/rp-e3-header")
    retry = _gate(home, "rp-e6-graph-focus", gate_id="retry_precondition")
    theirs = _gate(home, "hr-smoke-b-152611", gate_id="rp-e3-header-v2")

    closed = retry_notify.settle_gates_for_run(
        home, "rp-e6-graph-focus", REASON)

    assert set(closed) == {mine, retry}
    for inbox_id in (mine, retry):
        row = _row(home, inbox_id)
        assert row["status"] == "rejected"
        assert str(row["review_note"]).startswith("superseded")
        assert REASON in str(row.get("review_note") or "")
        assert row.get("resolved_at")
    other = _row(home, theirs)
    assert other["status"] == "pending"
    assert not other.get("review_note")


def test_settle_leaves_an_already_resolved_gate_alone(tmp_path, monkeypatch):
    home = _home(tmp_path, "rp-e6-graph-focus")
    _hermetic(monkeypatch, home)
    approved = _gate(home, "rp-e6-graph-focus", gate_id="rp-e3-header")
    oversight_inbox.resolve(approved, "approved",
                            review_note="human approved gate 43",
                            db_path=str(home / "state.db"))

    assert retry_notify.settle_gates_for_run(
        home, "rp-e6-graph-focus", REASON) == []

    row = _row(home, approved)
    assert row["status"] == "approved"
    assert "human approved" in str(row.get("review_note") or "")


# ── 2. review-gated repair ───────────────────────────────────────────────────


def test_decide_reviewing_run_is_review_gated(tmp_path, monkeypatch):
    home = _home(tmp_path, "hr-smoke-b-152611", status="reviewing")
    _hermetic(monkeypatch, home)
    monkeypatch.setenv("MO_AUTO_REPAIR", "1")
    monkeypatch.setenv("MO_MODEL_DIAGNOSER", "0")

    decision = auto_repair.decide(home, "hr-smoke-b-152611")

    assert decision["action"] == "none"
    assert "review-gated" in decision["reason"]


def test_decide_reviewing_run_with_stale_withheld_verdict_is_review_gated(
        tmp_path, monkeypatch):
    home = _home(tmp_path, "hr-smoke-b-152611", status="reviewing")
    run_dir = home / "runs" / "hr-smoke-b-152611"
    (run_dir / "verdict.json").write_text(json.dumps({
        "levels_decision": "abstain",
        "levels": {"applies": "UNPROVEN", "executes": "PROVEN",
                   "target": "n/a", "preserve": "n/a", "contract": "n/a"},
    }), encoding="utf-8")
    _hermetic(monkeypatch, home)
    monkeypatch.setenv("MO_AUTO_REPAIR", "1")
    monkeypatch.setenv("MO_MODEL_DIAGNOSER", "0")

    decision = auto_repair.decide(home, "hr-smoke-b-152611")

    assert decision["action"] == "none"
    assert "review-gated" in decision["reason"]


def test_decide_review_pause_marker_is_review_gated(tmp_path, monkeypatch):
    home = _home(tmp_path, "hr-smoke-b-152611", status="failed")
    (home / "runs" / "hr-smoke-b-152611" / ".review-pause").write_text(
        json.dumps({"inbox_id": 45}), encoding="utf-8")
    _hermetic(monkeypatch, home)
    monkeypatch.setenv("MO_AUTO_REPAIR", "1")
    monkeypatch.setenv("MO_MODEL_DIAGNOSER", "0")

    decision = auto_repair.decide(home, "hr-smoke-b-152611")

    assert decision["action"] == "none"
    assert "review-gated" in decision["reason"]


# ── 3. repair budget ─────────────────────────────────────────────────────────


def _first_repair_at(tmp_path: Path, monkeypatch, *, snapshot: float,
                     current: float) -> tuple[Path, str]:
    """Record the FIRST repair while cost reads ``snapshot``, then move it."""
    run_id = "learning-judge-4l"
    home = _home(tmp_path, run_id, status="failed", cost=snapshot)
    _hermetic(monkeypatch, home)
    monkeypatch.setenv("MO_AUTO_REPAIR", "1")
    monkeypatch.setenv("MO_MODEL_DIAGNOSER", "0")
    monkeypatch.setenv("MO_AUTO_ESCALATE", "0")
    monkeypatch.setenv("MO_AUTO_REPAIR_BUDGET_USD", "10")
    monkeypatch.setenv("MO_AUTO_REPAIR_MAX", "9")
    auto_repair.apply(home, run_id, {
        "action": "revise",
        "from_node": "implementer",
        "reason": "test: first repair attempt",
        "signature": "spec-sig-not-recomputable",
    }, spawn=False)
    con = sqlite3.connect(home / "state.db")
    con.execute("UPDATE task_runs SET cost_usd = ?", (current,))
    con.commit()
    con.close()
    return home, run_id


def test_budget_counts_only_spend_since_first_repair(tmp_path, monkeypatch):
    home, run_id = _first_repair_at(tmp_path, monkeypatch,
                                    snapshot=16.12, current=16.20)
    decision = auto_repair.decide(home, run_id)
    assert "repair budget exceeded" not in str(decision.get("reason") or "")


def test_budget_exceeded_when_spend_since_snapshot_tops_the_cap(
        tmp_path, monkeypatch):
    home, run_id = _first_repair_at(tmp_path, monkeypatch,
                                    snapshot=16.12, current=26.50)
    decision = auto_repair.decide(home, run_id)
    assert decision["action"] == "human"
    assert "repair budget exceeded" in decision["reason"]
