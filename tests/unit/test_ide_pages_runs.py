"""IDE page ``runs`` — Runs & live execution, built from real run rows."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

from mini_ork.ide_pages import build_page
from mini_ork.ide_pages import runs as page
from mini_ork.stores import migrate as mig

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def home(tmp_path: Path) -> Path:
    h = tmp_path / "proj" / ".mini-ork"
    h.mkdir(parents=True)
    rc, _out, err = mig.init_db(db=str(h / "state.db"), root=str(REPO))
    assert rc == 0, err
    return h


def _run(home: Path, run_id: str, status: str, *, ago: int = 0, cost: float = 0.25) -> None:
    now = int(time.time()) - ago
    con = sqlite3.connect(home / "state.db")
    con.execute(
        "INSERT INTO task_runs (id, recipe, status, cost_usd, created_at, updated_at, task_class, "
        "kickoff_path, workflow_version) VALUES (?,?,?,?,?,?,?,?,?)",
        (run_id, "code-fix", status, cost, now, now, "code_fix", "", "latest"))
    con.commit()
    con.close()


def _event(home: Path, run_id: str, kind: str, payload: dict, *, at: int, beat_ms: int | None = None) -> None:
    con = sqlite3.connect(home / "state.db")
    con.execute(
        "INSERT INTO run_events (event_id, run_id, event_type, payload_json, created_at, last_heartbeat_at) "
        "VALUES (?,?,?,?,?,?)",
        (f"ev-{run_id}-{kind}-{at}-{payload.get('node_id')}", run_id, kind, json.dumps(payload), at, beat_ms))
    con.commit()
    con.close()


def _section(p: dict, kind: str, title: str = "") -> dict:
    return next(s for s in p["sections"] if s["type"] == kind and (not title or s["title"] == title))


def test_all_runs_lists_every_run_with_a_filter(home: Path) -> None:
    _run(home, "run-1791000000-aaaaaa", "published", ago=600)
    _run(home, "run-1791000001-bbbbbb", "failed", ago=300)
    _run(home, "run-1791000002-cccccc", "executing")
    p = build_page(home, "runs")
    assert p["ok"] and p["title"] == "Runs & live execution"
    assert [t["key"] for t in p["tabs"]] == ["runs", "active", "hooks"] and p["tab"] == "runs"
    assert p["chips"][0]["t"] == "3 runs"
    chips = _section(p, "chips")
    assert [c["t"] for c in chips["items"]][0] == "All 3"
    assert chips["items"][0]["on"] and chips["items"][3]["do"] == {"set": {"filter": "failed"}}
    table = _section(p, "table")
    assert table["head"] == ["", "run", "recipe", "step", "age", "cost", "change"]
    assert len(table["rows"]) == 3
    assert all(r["do"]["run"].startswith("run-") for r in table["rows"])
    failed = build_page(home, "runs", args={"filter": "failed"})
    rows = _section(failed, "table")["rows"]
    assert [r["do"]["run"] for r in rows] == ["run-1791000001-bbbbbb"]
    assert rows[0]["cells"][0] == {"t": "✗", "c": "red", "mono": False, "b": False}
    json.dumps(p)


def test_active_tab_shows_the_open_node_lane_and_a_stop_button(home: Path) -> None:
    _run(home, "run-1791000002-cccccc", "executing")
    now = int(time.time())
    _event(home, "run-1791000002-cccccc", "node_start",
           {"node_id": "plan", "node_type": "planner", "model_lane": "planner"}, at=now - 50)
    _event(home, "run-1791000002-cccccc", "node_end", {"node_id": "plan"}, at=now - 40)
    _event(home, "run-1791000002-cccccc", "node_start",
           {"node_id": "implementer", "model_lane": "sonnet"}, at=now - 30, beat_ms=(now - 5) * 1000)
    p = build_page(home, "runs", "active")
    table = _section(p, "table", "Active dispatches · heartbeat")
    cells = table["rows"][0]["cells"]
    assert [c["t"] for c in cells[:4]] == ["run-1791000002-cccccc", "implementer", "sonnet", "local"]
    assert cells[2]["c"] == "fam:sonnet"
    assert cells[4]["c"] == "green" and cells[4]["t"].endswith("s ago")
    controls = _section(p, "list", "Controls")
    acts_by_label = {a["label"]: a for a in controls["items"][0]["acts"]}
    assert acts_by_label["Stop"]["do"]["cli"] == ["board", "stop", "run-1791000002-cccccc"]
    assert acts_by_label["Stop"]["do"]["confirm"]
    assert acts_by_label["Kill"]["do"]["cli"] == ["board", "kill", "run-1791000002-cccccc"]
    assert acts_by_label["Kill"]["do"]["confirm"]
    assert controls["items"][-1]["acts"][0]["do"] == {"page": "verify", "tab": "autonomy"}


def test_a_cost_paused_run_is_listed_as_paused(home: Path) -> None:
    _run(home, "run-1791000003-dddddd", "executing")
    run_dir = home / "runs" / "run-1791000003-dddddd"
    run_dir.mkdir(parents=True)
    (run_dir / ".cost-pause").write_text("{}")
    p = build_page(home, "runs", "active")
    cells = _section(p, "table")["rows"][0]["cells"]
    assert cells[4]["t"] == "paused · cost"
    item = _section(p, "list", "Controls")["items"][0]
    assert "paused on cost" in item["t"] and "mini-ork resume" in item["sub"]
    resume = next(a for a in item["acts"] if a["label"] == "Resume")
    assert resume["do"] == {"cli": ["board", "resume", "run-1791000003-dddddd"]}


def test_hooks_tab_reports_the_handler_and_the_event_tail(home: Path, monkeypatch) -> None:
    monkeypatch.delenv("MINI_ORK_ON_EVENT", raising=False)
    _run(home, "run-1791000002-cccccc", "executing")
    _event(home, "run-1791000002-cccccc", "node_end",
           {"node_id": "verifier", "finish_reason": "error"}, at=int(time.time()))
    p = build_page(home, "runs", "hooks")
    handlers = _section(p, "list", "MINI_ORK_ON_EVENT handlers")
    assert handlers["items"][0]["t"] == "No handler configured"
    lines = _section(p, "code", "Last events")["lines"]
    assert "verifier" in lines[-1]["t"] and lines[-1]["c"] == "red"

    monkeypatch.setenv("MINI_ORK_ON_EVENT", "/usr/local/bin/notify-hook --token SECRET")
    p = build_page(home, "runs", "hooks")
    item = _section(p, "list", "MINI_ORK_ON_EVENT handlers")["items"][0]
    assert item["t"] == "notify-hook" and "SECRET" not in json.dumps(p)


def test_a_broken_source_costs_one_section(home: Path, monkeypatch) -> None:
    def boom(*_a, **_k):
        raise RuntimeError("db locked")

    monkeypatch.setattr(page, "_fleet", boom)
    p = build_page(home, "runs")
    assert p["ok"] and "db locked" in json.dumps(p["sections"])
    assert p["errors"]
