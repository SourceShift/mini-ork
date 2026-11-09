"""The HTTP resume surface answers a budget park, not just the sentinel.

``resume_cost_run`` is the IDE's twin of ``mini-ork resume``. The CLI refuses a
resume while a run is still parked by its per-run ceiling; before this test the
HTTP path cleared only ``.cost-pause`` and returned ``ok: true`` — a reported
success whose run met the budget gate on its first dispatch and parked again.

Hermetic: temp homes, a real SQLite ledger, and the spawn helper monkeypatched
at its documented seam (no process is forked).
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

import mini_ork.cli.approvals_cmd as approvals_cmd
from mini_ork.web import control


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    for key in ("MINI_ORK_DB", "MO_BUDGET_POLICY", "MO_RUN_BUDGET_USD"):
        monkeypatch.delenv(key, raising=False)


def _home_with_both_holds(tmp_path: Path, run_id: str, *, spent: float,
                          budget: float | None):
    home = tmp_path / "home"
    run_dir = home / "runs" / run_id
    run_dir.mkdir(parents=True)
    (run_dir / ".cost-pause").write_text(
        json.dumps({"threshold_usd": 25, "run_id": run_id}) + "\n",
        encoding="utf-8",
    )
    (run_dir / ".budget-exhausted").write_text(json.dumps({
        "spent_usd": spent,
        "budget_usd": budget,
        "detail": f"spent ${spent:.2f} of a ${budget or 0:.2f} budget",
        "recorded_at": 1790000000,
    }) + "\n", encoding="utf-8")
    con = sqlite3.connect(home / "state.db")
    con.execute("CREATE TABLE IF NOT EXISTS llm_calls (run_id TEXT, cost_usd REAL)")
    con.execute("INSERT INTO llm_calls VALUES (?, ?)", (run_id, spent))
    con.commit()
    con.close()
    return home, run_dir


def _spawn_noop(monkeypatch) -> list[dict]:
    calls: list[dict] = []

    def fake_spawn_recover(home, run_id, extra_args, log_path=None):
        calls.append({"run_id": run_id, "extra_args": list(extra_args)})
        return 123, "/tmp/cost-resume.log"

    monkeypatch.setattr(approvals_cmd, "_spawn_recover", fake_spawn_recover)
    return calls


def _audit_rows(run_dir: Path) -> list[dict]:
    path = run_dir / ".cost-pause-approvals.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_resume_refuses_while_the_budget_still_blocks(tmp_path, monkeypatch):
    run_id = "run-web-park-1"
    home, run_dir = _home_with_both_holds(tmp_path, run_id, spent=0.84, budget=0.5)
    monkeypatch.setenv("MO_RUN_BUDGET_USD", "0.5")
    calls = _spawn_noop(monkeypatch)

    result = control.resume_cost_run(home, run_id, "op")

    assert result["ok"] is False
    assert result["error"] == "budget_exhausted"
    assert "$0.84" in result["detail"]
    assert (run_dir / ".cost-pause").is_file(), "the sentinel must be untouched"
    assert (run_dir / ".budget-exhausted").is_file()
    assert _audit_rows(run_dir) == []
    assert calls == [], "a refused resume must not restart the run"


def test_resume_refuses_when_the_ceiling_was_removed(tmp_path, monkeypatch):
    """Unsetting the knob is not a way to un-park through HTTP either."""
    run_id = "run-web-park-2"
    home, run_dir = _home_with_both_holds(tmp_path, run_id, spent=0.84, budget=0.5)
    _spawn_noop(monkeypatch)

    result = control.resume_cost_run(home, run_id, "op")

    assert result["ok"] is False
    assert result["error"] == "budget_exhausted"
    assert (run_dir / ".cost-pause").is_file()


def test_resume_clears_both_holds_once_the_run_fits(tmp_path, monkeypatch):
    run_id = "run-web-park-3"
    home, run_dir = _home_with_both_holds(tmp_path, run_id, spent=0.84, budget=0.5)
    monkeypatch.setenv("MO_RUN_BUDGET_USD", "12")
    calls = _spawn_noop(monkeypatch)

    result = control.resume_cost_run(home, run_id, "op")

    assert result["ok"] is True and result["restarted"] is True
    assert not (run_dir / ".cost-pause").exists()
    assert not (run_dir / ".budget-exhausted").exists(), "the park must be cleared"
    rows = _audit_rows(run_dir)
    assert len(rows) == 2, "one audit row per hold"
    assert rows[1]["sentinel_payload"]["marker"] == ".budget-exhausted"
    assert len(calls) == 1


def test_resume_clears_a_marker_only_park(tmp_path, monkeypatch):
    """A budget gate leaves the marker with no sentinel — the CLI accepts that."""
    run_id = "run-web-park-5"
    home = tmp_path / "home"
    run_dir = home / "runs" / run_id
    run_dir.mkdir(parents=True)
    (run_dir / ".budget-exhausted").write_text("{}\n", encoding="utf-8")
    con = sqlite3.connect(home / "state.db")
    con.execute("CREATE TABLE IF NOT EXISTS llm_calls (run_id TEXT, cost_usd REAL)")
    con.execute("INSERT INTO llm_calls VALUES (?, ?)", (run_id, 0.84))
    con.commit()
    con.close()
    monkeypatch.setenv("MO_RUN_BUDGET_USD", "12")
    calls = _spawn_noop(monkeypatch)

    result = control.resume_cost_run(home, run_id, "op")

    assert result["ok"] is True
    assert not (run_dir / ".budget-exhausted").exists()
    rows = _audit_rows(run_dir)
    assert len(rows) == 1
    assert rows[0]["sentinel_payload"]["marker"] == ".budget-exhausted"
    assert len(calls) == 1


def test_resume_without_a_marker_is_unchanged(tmp_path, monkeypatch):
    """The pre-existing single-hold path keeps its exact shape."""
    run_id = "run-web-park-4"
    home = tmp_path / "home"
    run_dir = home / "runs" / run_id
    run_dir.mkdir(parents=True)
    (run_dir / ".cost-pause").write_text("{}\n", encoding="utf-8")
    calls = _spawn_noop(monkeypatch)

    result = control.resume_cost_run(home, run_id, "op")

    assert result["ok"] is True
    assert len(_audit_rows(run_dir)) == 1
    assert len(calls) == 1
