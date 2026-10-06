"""IDE page ``autos`` — Automations & scheduling, built from the project's real state."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

from mini_ork import automations
from mini_ork.ide_pages import autos, build_page
from mini_ork.stores import migrate as mig

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def home(tmp_path: Path) -> Path:
    h = tmp_path / "proj" / ".mini-ork"
    h.mkdir(parents=True)
    rc, _out, err = mig.init_db(db=str(h / "state.db"), root=str(REPO))
    assert rc == 0, err
    return h


def _write_automations(home: Path, items: list[dict]) -> None:
    automations._store_path(home).write_text(json.dumps({"automations": items}), encoding="utf-8")


def _automation(aid: str, *, enabled: bool = True, kickoff: str = "Bump deps; run the suite.",
                schedule: str = "0 2 * * *") -> dict:
    return {"id": aid, "name": aid, "recipe": "dep-check", "kickoff": kickoff, "schedule": schedule,
            "workspace": "worktree", "enabled": enabled, "created_at": "2026-10-01T00:00:00",
            "last_fired_at": None, "last_run_id": None, "last_error": None, "runs": []}


def _section(page: dict, title: str) -> dict:
    return next(s for s in page["sections"] if s["title"] == title)


def test_page_shape_matches_the_design(home: Path) -> None:
    page = autos.build(home, None, {})
    assert page["ok"] and page["key"] == "autos"
    assert page["title"] == "Automations & scheduling"
    assert [t["key"] for t in page["tabs"]] == ["automations", "schedulers"]
    assert [t["label"] for t in page["tabs"]] == ["Automations", "Schedulers"]
    assert page["tab"] == "automations"
    assert page["chips"][0]["t"] in {"scheduler on", "scheduler off"}
    assert page["actions"][0]["label"] == "New automation"
    assert page["actions"][0]["do"] == {"thread": "/automation new "}
    json.dumps(page)


def test_no_automations_is_an_honest_empty_table(home: Path) -> None:
    page = autos.build(home, "automations", {})
    table = _section(page, "Automations")
    assert table["type"] == "table" and table["full"]
    assert table["head"] == ["automation", "when", "next", "last"]
    assert table["rows"][0]["cells"][0]["t"] == "No automations yet"
    assert page["errors"] == {}


def test_automations_table_detail_firings_and_kickoff(home: Path) -> None:
    kickoff = home.parent / "kickoffs" / "deps.md"
    kickoff.parent.mkdir()
    kickoff.write_text("# Deps\nBump everything within semver.\n", encoding="utf-8")
    _write_automations(home, [_automation("deps-nightly", kickoff="kickoffs/deps.md"),
                              _automation("obs-hourly", enabled=False, schedule="0 * * * *")])
    page = autos.build(home, "automations", {})
    table = _section(page, "Automations")
    ids = [r["cells"][0]["t"] for r in table["rows"]]
    assert ids == ["deps-nightly", "obs-hourly"]          # enabled first
    assert table["rows"][0]["sel"] and not table["rows"][1]["sel"]
    assert table["rows"][1]["do"] == {"set": {"auto": "obs-hourly"}}
    assert table["rows"][1]["cells"][2]["t"] == "paused"
    assert table["rows"][0]["cells"][3]["t"] == "never"

    detail = _section(page, "deps-nightly")
    assert detail["type"] == "kv"
    assert [i["k"] for i in detail["items"]] == ["Recipe", "When", "State"]
    assert detail["items"][2]["v"] == "active"
    labels = {a["label"]: a["do"] for a in detail["actions"]}
    assert labels["Run now"]["cli"] == ["automations", "run", "deps-nightly"]
    assert labels["Run now"]["confirm"].startswith("Start a run of ")
    assert labels["Pause"] == {"cli": ["automations", "pause", "deps-nightly"]}
    assert labels["Delete"]["cli"] == ["automations", "remove", "deps-nightly"]
    assert labels["Delete"]["confirm"]

    firings = _section(page, "Next three firings")
    assert len(firings["items"]) == 3 and all("02:00" in i["t"] for i in firings["items"])

    code = _section(page, "Kickoff each run receives")
    assert [line["t"] for line in code["lines"]] == ["# Deps", "Bump everything within semver."]


def test_selecting_a_paused_automation(home: Path) -> None:
    _write_automations(home, [_automation("deps-nightly"),
                              _automation("obs-hourly", enabled=False, kickoff="probe every surface")])
    page = autos.build(home, "automations", {"auto": "obs-hourly"})
    detail = _section(page, "obs-hourly")
    assert detail["items"][2]["v"] == "paused"
    assert any(a["label"] == "Resume" and a["do"] == {"cli": ["automations", "resume", "obs-hourly"]}
               for a in detail["actions"])
    assert _section(page, "Next three firings")["items"][0]["t"].startswith("Paused")
    assert _section(page, "Kickoff each run receives")["lines"][0]["t"] == "probe every surface"


def test_schedulers_tab_reads_real_sources(home: Path) -> None:
    con = sqlite3.connect(home / "state.db")
    con.execute("INSERT INTO agent_performance_memory (agent_version_id, role, model, task_class, runs_count, "
                "success_count, avg_cost_usd) VALUES ('a1', 'implementer', 'glm', 'code_fix', 10, 9, 0.04)")
    con.execute("INSERT INTO epics (id, title, status) VALUES ('e1', 'one', 'blocked')")
    con.commit()
    con.close()
    page = autos.build(home, "schedulers", {})
    titles = [s["title"] for s in page["sections"]]
    assert titles == ["Project scheduler", "Epic scheduler", "Conductor · last decisions", "Watchdog",
                      "Lifetime leaderboard"]
    proj = _section(page, "Project scheduler")
    assert proj["actions"][0]["do"]["cli"][:2] == ["automations", "scheduler"]
    epic = {i["k"]: i["v"] for i in _section(page, "Epic scheduler")["items"]}
    assert epic["Blocked"] == "1"
    assert _section(page, "Conductor · last decisions")["items"][0]["t"] == "No conductor decisions yet"
    assert _section(page, "Watchdog")["items"][0]["m"] == "✓"
    board = _section(page, "Lifetime leaderboard")
    assert board["head"] == ["lane", "runs", "success", "cost/run"]
    assert [c["t"] for c in board["rows"][0]["cells"]] == ["glm", "10", "90%", "$0.04"]
    assert board["rows"][0]["cells"][0]["c"] == "fam:glm"
    assert page["errors"] == {}


def test_a_broken_source_costs_one_section(home: Path, monkeypatch) -> None:
    monkeypatch.setattr(autos, "_conductor", lambda _h: (_ for _ in ()).throw(RuntimeError("db locked")))
    page = autos.build(home, "schedulers", {})
    assert page["ok"]
    assert "Conductor · last decisions" in page["errors"]
    assert "Watchdog" in [s["title"] for s in page["sections"]]


def test_board_page_entrypoint_and_speed(home: Path) -> None:
    start = time.monotonic()
    for tab in ("automations", "schedulers"):
        page = build_page(home, "autos", tab, {})
        assert page["ok"] and page["tab"] == tab
    assert time.monotonic() - start < 3
