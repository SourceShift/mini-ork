"""Spend against the caps — the run page's ``Spend`` section and outcome data.

Covers the engine half of C16 (run-page JTBD review): the per-run cap comes
from the watcher's own resolver, the daily cap from the scheduler's knob, the
projection only while a run is still spending, and the cost-pause Retry branch
names the cap that was hit.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from mini_ork.ide_pages import run as run_page
from mini_ork.ide_pages.run import Run

_SENTENCE = "$0.84 of a $1.00 per-run cap · $0.16 left"


@pytest.fixture(autouse=True)
def _clean_cap_env(monkeypatch):
    monkeypatch.setenv("MINI_ORK_IDE_SPEC", "2")
    for var in ("MO_WATCH_COST_USD", "MO_ORCH_BUDGET_USD", "MO_DAILY_BUDGET_USD"):
        monkeypatch.delenv(var, raising=False)


def _mk_run(tmp_path: Path, *, run_id: str = "run-spend-1", status: str = "running",
            cost_total: float | None = None, cost_usd: float | None = None,
            started_secs_ago: int = 300, ended_secs_ago: int | None = None) -> Run:
    run_dir = tmp_path / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    row: dict[str, Any] = {
        "id": run_id,
        "created_at": int(time.time()) - started_secs_ago,
        "recipe": "code-fix",
    }
    if ended_secs_ago is not None:
        row["ended_at"] = int(time.time()) - ended_secs_ago
    if cost_usd is not None:
        row["cost_usd"] = cost_usd
    card: dict[str, Any] = {"status": status, "recipe": "code-fix", "steps": []}
    if cost_total is not None:
        card["cost_total"] = cost_total
    return Run(id=run_id, home=tmp_path, run_dir=run_dir, row=row, card=card,
               nodes=[], cols=[], calls=[], recipe_dir=None, workspace=None,
               edges=[])


def _page(monkeypatch, run: Run, tab: str) -> dict[str, Any]:
    monkeypatch.setattr(run_page, "_load",
                        lambda home, run_id, with_files=True: run)
    return run_page.build(run.home, tab, {"run": run.id})


def _spend_sections(sections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [s for s in sections
            if isinstance(s, dict)
            and ("spend" in str(s.get("title") or "").casefold()
                 or "per-run cap" in json.dumps(s, default=str, ensure_ascii=False))]


def _find_spend(obj: Any) -> dict[str, Any] | None:
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key == "spend" and isinstance(value, dict):
                return value
        for value in obj.values():
            found = _find_spend(value)
            if found is not None:
                return found
    elif isinstance(obj, (list, tuple)):
        for entry in obj:
            found = _find_spend(entry)
            if found is not None:
                return found
    return None


def test_under_cap_sentence_and_remaining(tmp_path, monkeypatch):
    monkeypatch.setenv("MO_WATCH_COST_USD", "1.00")
    run = _mk_run(tmp_path, cost_total=0.84)
    section = run_page._spend_section(run)
    assert _SENTENCE in json.dumps(section, default=str, ensure_ascii=False)
    numbers = run_page._spend_numbers(run)
    assert numbers is not None
    assert numbers["remaining"] == pytest.approx(0.16)
    assert numbers["spent"] == pytest.approx(0.84)
    assert numbers["cap"] == pytest.approx(1.00)


def test_tones(tmp_path, monkeypatch):
    monkeypatch.setenv("MO_WATCH_COST_USD", "1.00")
    under = json.dumps(run_page._spend_section(
        _mk_run(tmp_path, run_id="under", cost_total=0.84)), default=str, ensure_ascii=False)
    assert "green" in under and "red" not in under

    over = json.dumps(run_page._spend_section(
        _mk_run(tmp_path, run_id="over", cost_total=1.40)), default=str, ensure_ascii=False)
    assert "red" in over and "green" not in over
    numbers = run_page._spend_numbers(
        _mk_run(tmp_path, run_id="over2", cost_total=1.40))
    assert numbers is not None and numbers["remaining"] == pytest.approx(0)


def test_cap_uses_watch_resolver(tmp_path, monkeypatch):
    from mini_ork.acp_orchestrator import watch

    monkeypatch.setattr(watch, "cost_cap_usd", lambda: 2.50)
    run = _mk_run(tmp_path, cost_total=0.84)
    assert "$0.84 of a $2.50 per-run cap · $1.66 left" in json.dumps(
        run_page._spend_section(run), default=str, ensure_ascii=False)


def test_orch_budget_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("MO_ORCH_BUDGET_USD", "0.50")
    run = _mk_run(tmp_path, cost_total=0.84)
    blob = json.dumps(run_page._spend_section(run), default=str, ensure_ascii=False)
    assert "$0.84 of a $0.50 per-run cap · $0.00 left" in blob
    assert "red" in blob


def test_daily_cap(tmp_path, monkeypatch):
    monkeypatch.setenv("MO_WATCH_COST_USD", "1.00")
    monkeypatch.setenv("MO_DAILY_BUDGET_USD", "20.0")
    run = _mk_run(tmp_path, cost_total=0.84)
    assert "$20.00" in json.dumps(run_page._spend_section(run), default=str, ensure_ascii=False)
    numbers = run_page._spend_numbers(run)
    assert numbers is not None and numbers["daily_cap"] == pytest.approx(20.0)


def test_projection_only_while_running_past_a_minute(tmp_path, monkeypatch):
    monkeypatch.setenv("MO_WATCH_COST_USD", "1.00")
    long_running = _mk_run(tmp_path, run_id="long", status="running",
                           cost_total=0.84, started_secs_ago=90)
    blob = json.dumps(run_page._spend_section(long_running), default=str, ensure_ascii=False)
    assert "at this rate, ~$" in blob
    numbers = run_page._spend_numbers(long_running)
    assert numbers is not None and numbers["projected"] > numbers["spent"]

    short = _mk_run(tmp_path, run_id="short", status="running",
                    cost_total=0.84, started_secs_ago=30)
    assert "projected" not in (run_page._spend_numbers(short) or {})
    assert "at this rate" not in json.dumps(
        run_page._spend_section(short), default=str, ensure_ascii=False)

    finished = _mk_run(tmp_path, run_id="done", status="published",
                       cost_total=0.84, started_secs_ago=3600,
                       ended_secs_ago=3000)
    assert "projected" not in (run_page._spend_numbers(finished) or {})


def test_broken_cap_source_is_absorbed(tmp_path, monkeypatch):
    from mini_ork.acp_orchestrator import watch

    def _blown() -> float:
        raise RuntimeError("cap source blew up")

    monkeypatch.setattr(watch, "cost_cap_usd", _blown)
    run = _mk_run(tmp_path, cost_total=0.84)
    blob = json.dumps(run_page._spend_section(run), default=str, ensure_ascii=False)  # must not raise
    assert "$0.84" in blob


def test_section_on_checks_and_outcome_equal(tmp_path, monkeypatch):
    monkeypatch.setenv("MO_WATCH_COST_USD", "1.00")
    run = _mk_run(tmp_path, status="published", cost_total=0.84,
                  started_secs_ago=3600, ended_secs_ago=3000)
    checks = _page(monkeypatch, run, "checks")["sections"]
    outcome = _page(monkeypatch, run, "outcome")["sections"]
    checks_spend = _spend_sections(checks)
    outcome_spend = _spend_sections(outcome)
    assert len(checks_spend) == 1
    assert len(outcome_spend) == 1
    assert checks_spend[0] == outcome_spend[0]
    assert _SENTENCE in json.dumps(checks_spend[0], default=str, ensure_ascii=False)


def test_placement_after_anchors(tmp_path, monkeypatch):
    monkeypatch.setenv("MO_WATCH_COST_USD", "1.00")
    run = _mk_run(tmp_path, status="published", cost_total=0.84,
                  started_secs_ago=3600, ended_secs_ago=3000)
    checks = _page(monkeypatch, run, "checks")["sections"]
    levels = [i for i, s in enumerate(checks)
              if s.get("title") == "Verification levels"]
    spend = [i for i, s in enumerate(checks) if s in _spend_sections(checks)]
    assert levels and spend and spend[0] > levels[0]

    outcome = _page(monkeypatch, run, "outcome")["sections"]
    assert outcome[0].get("type") == "triage"
    spend = [i for i, s in enumerate(outcome) if s in _spend_sections(outcome)]
    assert spend and spend[0] > 0


def test_outcome_carries_spend_numbers(tmp_path, monkeypatch):
    monkeypatch.setenv("MO_WATCH_COST_USD", "1.00")
    run = _mk_run(tmp_path, cost_total=0.84)
    spend = _find_spend(_page(monkeypatch, run, "outcome"))
    assert isinstance(spend, dict)
    assert spend["spent"] == pytest.approx(0.84)
    assert spend["cap"] == pytest.approx(1.00)
    assert spend["remaining"] == pytest.approx(0.16)


def test_no_cost_data_renders_section_without_spend_key(tmp_path, monkeypatch):
    run = _mk_run(tmp_path)
    page = _page(monkeypatch, run, "checks")
    assert page.get("ok") is True
    sections = _spend_sections(page["sections"])
    assert sections and "$0.00" in json.dumps(sections, default=str, ensure_ascii=False)
    assert _find_spend(page) is None


def test_no_new_tab_and_chips_untouched(tmp_path, monkeypatch):
    monkeypatch.setenv("MO_WATCH_COST_USD", "1.00")
    run = _mk_run(tmp_path, cost_total=0.84)
    page = _page(monkeypatch, run, "checks")
    for tab in page.get("tabs") or []:
        key = str(tab.get("key") or "") if isinstance(tab, dict) else str(tab)
        label = str(tab.get("label") or "") if isinstance(tab, dict) else ""
        assert "spend" not in key.casefold() and "spend" not in label.casefold()
    for chip in page.get("chips") or []:
        assert isinstance(chip, dict) and set(chip) == {"t", "c"}


def test_cost_paused_resume_names_cap(tmp_path, monkeypatch):
    from mini_ork.recovery import retry_hint

    monkeypatch.setenv("MO_WATCH_COST_USD", "1.00")
    run = _mk_run(tmp_path, run_id="run-capped", status="failed",
                  cost_total=1.40, started_secs_ago=600, ended_secs_ago=60)
    monkeypatch.setattr(
        retry_hint, "load_or_compute",
        lambda home, run_id, write=False: {
            "retryable": True, "strategy": "resume-cost", "from_node": "",
            "needs_change": {"kind": "budget", "summary": "cost cap reached",
                             "detail": "", "evidence": ""},
            "notes": []})
    retry = [s for s in run_page._overview_tab(run)
             if s.get("title") == "Retry"]
    assert retry
    assert "$1.00" in json.dumps(retry, default=str, ensure_ascii=False)
    buttons = [a for a in (retry[0].get("actions") or [])
               if isinstance(a, dict) and a.get("label") == "Resume"]
    assert len(buttons) == 1
    assert buttons[0]["kind"] == "primary"
    assert buttons[0]["do"] == {
        "cli": ["board", "retry", run.id],
        "confirm": f"Resume cost-paused {run.id}?",
    }
