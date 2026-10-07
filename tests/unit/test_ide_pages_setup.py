"""``board page setup`` — the Setup & health page."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from mini_ork.acp import setup as acp_setup
from mini_ork.ide_pages import setup
from mini_ork.stores import migrate as mig

from test_ide_pages_orch import check_page

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def home(tmp_path: Path, monkeypatch) -> Path:
    h = tmp_path / "proj" / ".mini-ork"
    h.mkdir(parents=True)
    rc, _out, err = mig.init_db(db=str(h / "state.db"), root=str(REPO))
    assert rc == 0, err
    settings = tmp_path / "zed" / "settings.json"
    settings.parent.mkdir()
    settings.write_text(json.dumps({
        "agent_servers": {"mini-ork": {"type": "custom", "command": "/bin/sh", "args": ["acp"],
                                       "env": {"PATH": "/usr/bin", "OPENAI_API_KEY": "sk-SECRET-123"}}},
        "context_servers": {"mini-ork": {"command": "/bin/sh", "args": ["mcp-context"]}},
    }))
    monkeypatch.setenv("ZED_SETTINGS", str(settings))
    monkeypatch.setenv("MINI_ORK_PROJECTS_FILE", str(tmp_path / "projects.json"))
    monkeypatch.setattr(acp_setup, "_check_orchestrator",
                        lambda home, lane=None: acp_setup.Check("orchestrator", True, "claude subscription login OK"))
    monkeypatch.setattr("mini_ork.automations.scheduler_status",
                        lambda home: {"installed": False, "platform": "macos"})
    return h


def test_every_tab_builds(home: Path, monkeypatch) -> None:
    monkeypatch.setattr(setup, "_garden", lambda home: (setup.S.lst("garden · drift", [setup.S.ok("No drift")]),
                                                        {"errors": 0, "warnings": 0, "infos": 0}))
    for key, _label in setup.TABS:
        page = setup.build(home, key, {})
        check_page(page, "setup")
        assert page["tab"] == key and page["sections"] and page["errors"] == {}
    assert [t["key"] for t in setup.build(home, None, {})["tabs"]] == ["readiness", "zed", "projects", "health"]


def test_readiness(home: Path, monkeypatch) -> None:
    monkeypatch.setattr(acp_setup, "_load_lane_map", lambda home: {"implementer": "minimax", "reviewer": "glm,minimax"})

    def creds(model, *, role, home):
        return acp_setup.Check(f"lanes:{role}:{model}", model == "minimax", f"{model}: x")

    monkeypatch.setattr(acp_setup, "_check_provider_credentials", creds)
    items = {i["t"]: i for i in setup.build(home, "readiness", {})["sections"][0]["items"]}
    assert items[".mini-ork/ found"]["mc"] == "green" and "applied" in items[".mini-ork/ found"]["sub"]
    assert items["Orchestrator login"]["mc"] == "green"
    # a fallback list is ready when any of its lanes has keys
    assert items["Worker lanes"]["sub"] == "implementer, reviewer have keys"
    assert ".mini-ork/worktree-setup.sh missing" in items
    assert items["PATH captured for Dock launches"]["mc"] == "green"
    sched = items["Scheduler not installed"]
    assert sched["acts"][0]["do"] == {"cli": ["automations", "scheduler", "install"]}


def test_missing_lane_credentials(home: Path, monkeypatch) -> None:
    monkeypatch.setattr(acp_setup, "_load_lane_map", lambda home: {"planner": "glm"})
    monkeypatch.setattr(acp_setup, "_check_provider_credentials",
                        lambda model, *, role, home: acp_setup.Check(role, False, "no key"))
    items = {i["t"]: i for i in setup.build(home, "readiness", {})["sections"][0]["items"]}
    row = items["Worker lanes: missing credentials"]
    assert row["mc"] == "red" and "mini-ork providers configure glm" in row["sub"]


def test_zed_wiring_never_shows_env_values(home: Path) -> None:
    page = setup.build(home, "zed", {})
    kv = {i["k"]: i for i in page["sections"][0]["items"]}
    assert kv["agent_servers.mini-ork"]["v"] == "present"
    assert kv["Launcher"]["v"] == "/bin/sh" and kv["Launcher"]["sub"] == "executable"
    text = json.dumps(page)
    assert "sk-SECRET-123" not in text and "/usr/bin" not in text
    assert '"OPENAI_API_KEY": "…"' in page["sections"][1]["lines"][0]["t"]
    # setup / uninstall from here would pin --home into every thread: disabled
    assert [a["do"] for a in page["sections"][0]["actions"][:3]] == [None, None, None]


def test_unreadable_settings(home: Path, monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ZED_SETTINGS", str(tmp_path / "nope.json"))
    section = setup.build(home, "zed", {})["sections"][0]
    assert section["items"][0]["t"] == "Settings unreadable"


def test_projects_mark_the_current_one(home: Path, tmp_path: Path) -> None:
    other = tmp_path / "other" / ".mini-ork"
    other.mkdir(parents=True)
    (other / "state.db").write_text("")
    (tmp_path / "projects.json").write_text(json.dumps({"projects": [str(other), str(tmp_path / "gone" / ".mini-ork")]}))
    rows = setup.build(home, "projects", {})["sections"][0]["rows"]
    by_name = {r["cells"][0]["t"]: r for r in rows}
    assert by_name["proj"]["sel"] is True and by_name["proj"]["cells"][2]["t"] == "current"
    assert by_name["other"]["cells"][2]["t"] == "ready" and by_name["other"]["do"]["project"].endswith("other")
    assert by_name["gone"]["cells"][2]["t"] == "missing"


def test_health_and_exports(home: Path, monkeypatch) -> None:
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-SECRET")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-SECRET")
    monkeypatch.setenv("MINI_ORK_ON_EVENT", "curl https://hooks.example/SECRET")
    page = setup.build(home, "health", {})
    titles = [s["title"] for s in page["sections"]]
    assert titles == ["garden · drift", "Versions", "Exports"]
    assert "error(s)" in page["sections"][0]["note"]
    versions = {i["k"]: i["v"] for i in page["sections"][1]["items"]}
    assert versions["mini-ork"] not in ("", "—") and versions["Migrations"].endswith("applied")
    exports = {i["t"]: i for i in page["sections"][2]["items"]}
    assert exports["OTel / Langfuse"]["mc"] == "green" and exports["Event hook"]["mc"] == "green"
    assert "SECRET" not in json.dumps(page)
