"""``mini_ork.ide_pages.orch_tab`` — the run page's Orchestrator tab.

The tab is a read-only projection: orchestrator events, the steering queue,
and the watcher's own state file. Each test builds the state in a real
``state.db`` and reads the sections the page would receive, so a change in a
column name or a payload key fails here rather than as a blank panel.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

import pytest

from mini_ork.ide_pages import orch_tab
from mini_ork.ide_pages.run import Run
from mini_ork.stores import migrate as mig

REPO = Path(__file__).resolve().parents[2]
RUN = "run-1791000000-abc123"


@pytest.fixture()
def home(tmp_path: Path) -> Path:
    h = tmp_path / "proj" / ".mini-ork"
    h.mkdir(parents=True)
    rc, _out, err = mig.init_db(db=str(h / "state.db"), root=str(REPO))
    assert rc == 0, err
    return h


@pytest.fixture()
def run(home: Path) -> Run:
    run_dir = home / "runs" / RUN
    run_dir.mkdir(parents=True)
    return Run(id=RUN, home=home, run_dir=run_dir, row={},
               card={"status": "executing"}, nodes=[], cols=[], calls=[],
               recipe_dir=None, workspace=None)


def _event(home: Path, kind: str, *, payload: dict | None = None,
           age_s: int = 0) -> None:
    con = sqlite3.connect(home / "state.db")
    try:
        con.execute(
            "INSERT INTO run_events (event_id, run_id, event_type, payload_json, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (f"{RUN}-{kind}-{age_s}", RUN, f"orchestrator.{kind}",
             json.dumps(payload or {}), int(time.time()) - age_s),
        )
        con.commit()
    finally:
        con.close()


def _steering(home: Path, *, message: str, severity: str = "info",
              source: str = "operator", age_s: int = 0,
              consumed: bool = False, expires_in_s: int = 3600) -> None:
    now_ms = int(time.time() * 1000)
    con = sqlite3.connect(home / "state.db")
    try:
        con.execute(
            "INSERT INTO operator_steering (run_id, role_target, severity, message, "
            "source, confidence, created_at, consumed_at, expires_at) "
            "VALUES (?, 'any', ?, ?, ?, 0.8, ?, ?, ?)",
            (RUN, severity, message, source, now_ms - age_s * 1000,
             now_ms if consumed else None, now_ms + expires_in_s * 1000),
        )
        con.commit()
    finally:
        con.close()


def _feed(sections: list[dict]) -> dict:
    return next(s for s in sections if s["type"] == "feed")


def _lst(sections: list[dict], title: str) -> dict:
    return next(s for s in sections if s["type"] == "list" and s["title"] == title)


# ── activity feed ───────────────────────────────────────────────────────────

def test_feed_leads_with_the_newest_event(home: Path, run: Run) -> None:
    _event(home, "watching", payload={"reason": "watching · no signal yet"}, age_s=90)
    _event(home, "advance", payload={"node_id": "planner", "reason": "ok"}, age_s=60)
    _event(home, "signal", payload={"kind": "stalled", "node_id": "impl",
                                    "reason": "idle 12m"}, age_s=20)

    feed = _feed(orch_tab.orchestrator_tab(run))

    labels = [item["t"] for item in feed["items"]]
    assert labels == ["stalled", "advance", "watching"]
    assert feed["items"][0]["c"] == "yellow"      # stalled is the amber kind
    assert feed["items"][1]["c"] == "blue"        # advance
    assert feed["items"][0]["g"] == "!"
    assert "impl" in feed["items"][0]["d"]
    assert feed["items"][0]["when"] != "—"


def test_feed_filter_shows_one_category_and_counts_all(home: Path, run: Run) -> None:
    _event(home, "advance", payload={"node_id": "planner"}, age_s=50)
    _event(home, "signal", payload={"kind": "stalled", "node_id": "impl"}, age_s=40)
    _event(home, "watching", age_s=30)

    feed = _feed(orch_tab.orchestrator_tab(run, {"orch_view": "signal"}))

    assert [item["t"] for item in feed["items"]] == ["stalled"]
    counts = {f["t"].split(" ")[0]: f["on"] for f in feed["filters"]}
    assert counts == {"all": False, "decisions": False, "signals": True,
                      "steers": False, "lifecycle": False}
    labels = [f["t"] for f in feed["filters"]]
    assert labels[0] == "all 3" and labels[1] == "decisions 1" and labels[2] == "signals 1"


def test_feed_offers_earlier_events_when_the_window_truncates(home: Path, run: Run) -> None:
    for i in range(4):
        _event(home, "advance", payload={"node_id": f"n{i}"}, age_s=i)

    feed = _feed(orch_tab.orchestrator_tab(run, {"orch_limit": 3}))

    assert len(feed["items"]) == 3
    assert feed["more"]["do"]["set"] == {"orch_limit": 6}


def test_quiet_run_says_quiet_not_blank(home: Path, run: Run) -> None:
    feed = _feed(orch_tab.orchestrator_tab(run))
    assert feed["items"] == []
    assert "No interventions" in feed["empty"]


def test_steered_row_links_to_its_node_tab(home: Path, run: Run) -> None:
    _event(home, "steered", payload={"node_id": "impl", "reason": "nudged"}, age_s=5)

    feed = _feed(orch_tab.orchestrator_tab(run))
    row = feed["items"][0]

    assert row["t"] == "steered"
    assert row["c"] == "purple"
    assert row["do"]["args"] == {"run": RUN, "node": "impl"}
    assert row["acts"][0]["label"] == "Open"


def test_ended_run_is_not_live_and_has_no_composer(home: Path) -> None:
    run_dir = home / "runs" / RUN
    run_dir.mkdir(parents=True)
    finished = Run(id=RUN, home=home, run_dir=run_dir, row={},
                   card={"status": "published"}, nodes=[], cols=[], calls=[],
                   recipe_dir=None, workspace=None)

    sections = orch_tab.orchestrator_tab(finished)

    assert not [s for s in sections if s["type"] == "composer"]
    assert orch_tab.refresh_seconds(finished) is None
    assert orch_tab.refresh_seconds(finished.__class__(**{**finished.__dict__,
                                                          "card": {"status": "executing"}})) == 5


# ── steering queue ──────────────────────────────────────────────────────────

def test_steering_queue_distinguishes_queued_read_and_expired(home: Path, run: Run) -> None:
    _steering(home, message="queued note", age_s=5)
    _steering(home, message="already read", age_s=10, consumed=True)
    _steering(home, message="stale note", age_s=100, expires_in_s=-10,
              source="orchestrator-watch", severity="warn")

    queue = _lst(orch_tab.orchestrator_tab(run), "Steering queue")
    subs = {item["t"].split(" · ", 1)[1]: item["sub"] for item in queue["items"]}

    assert subs["queued note"].endswith("queued")
    assert subs["already read"].endswith("read")
    assert subs["stale note"].endswith("expired")
    assert "orchestrator-watch" in subs["stale note"]


def test_steering_queue_empty_state_explains_the_composer(home: Path, run: Run) -> None:
    queue = _lst(orch_tab.orchestrator_tab(run), "Steering queue")
    assert "No steering rows" in queue["items"][0]["t"]


# ── watcher card ────────────────────────────────────────────────────────────

def test_watcher_card_reports_a_live_process(home: Path, run: Run,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MO_ORCH_WATCH", "1")
    monkeypatch.setenv("MO_ORCH_WATCH_INTERVAL_S", "20")
    monkeypatch.setenv("MO_ORCH_WATCH_MAX_STEERS", "3")
    now = time.time()
    (run.run_dir / ".orchestrator-watch.json").write_text(json.dumps({
        "pid": os.getpid(), "started_at": now - 120, "last_tick": now,
        "tick_count": 7, "steers": 1,
    }), encoding="utf-8")

    sections = orch_tab.orchestrator_tab(run)
    feed = _feed(sections)
    card = next(s for s in sections if s["type"] == "kv")

    assert feed["status"] == "watching"
    assert feed["status_c"] == "green"
    assert "steers 1/3" in feed["meta"]
    kv = {item["k"]: item["v"] for item in card["items"]}
    assert kv["state"] == "watching"
    assert kv["steers"] == "1/3"
    assert kv["cadence"] == "every 20s"


def test_watcher_off_is_named_not_hidden(home: Path, run: Run,
                                         monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MO_ORCH_WATCH", "0")

    feed = _feed(orch_tab.orchestrator_tab(run))

    assert feed["status"] == "off"
    assert feed["meta"] == "MO_ORCH_WATCH=0"


def test_armed_run_without_a_state_file_says_armed(home: Path, run: Run,
                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MO_ORCH_WATCH", "1")
    monkeypatch.delenv("MO_ORCH_WATCH_AUTONOMY", raising=False)

    feed = _feed(orch_tab.orchestrator_tab(run))

    assert feed["status"] == "armed"
    assert "has not ticked" in feed["meta"]


def test_composer_rides_the_orchestrator_tab_while_the_run_lives(home: Path, run: Run) -> None:
    composer = [s for s in orch_tab.orchestrator_tab(run) if s["type"] == "composer"]

    assert len(composer) == 1
    assert composer[0]["cli"][:3] == ["board", "steer", RUN]
