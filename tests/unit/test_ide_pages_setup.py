"""``board page setup`` — the Setup & health page."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from mini_ork import settings_catalogue as sc
from mini_ork.acp import setup as acp_setup
from mini_ork.features import store as feat_store
from mini_ork.features.registry import CONTROLLABLE_FEATURES
from mini_ork.ide_pages import setup
from mini_ork.stores import migrate as mig

from test_ide_pages_actions import _walk_cli_prefixes
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
    assert [t["key"] for t in setup.build(home, None, {})["tabs"]] == [
        "readiness", "features", "settings", "zed", "projects", "health"]


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
    # Every row but the current one can be forgotten, and forgetting is
    # confirmed because the reader cannot see the registry from here.
    assert by_name["proj"]["acts"] == []
    for name in ("other", "gone"):
        forget = by_name[name]["acts"][0]["do"]
        assert forget["project_forget"].endswith(name)
        assert forget["confirm"].startswith(f"Remove {name} ") and "stay" in forget["confirm"]


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


# ── the Features tab ────────────────────────────────────────────────────────


def _features_table(home: Path) -> dict:
    page = setup.build(home, "features", {})
    assert page["errors"] == {}
    table = page["sections"][0]
    assert table["type"] == "table" and table["title"] == "Features"
    return table


def test_features_tab_has_one_row_per_registered_feature(home: Path) -> None:
    ids = [row["cells"][1]["t"] for row in _features_table(home)["rows"]]
    assert ids == sorted(CONTROLLABLE_FEATURES)


def test_each_feature_row_renders_the_model_not_a_re_derivation(home: Path) -> None:
    """Every cell must equal what the store says, computed the same way the page did."""
    data = feat_store.load(feat_store.store_path(home))
    secrets = setup._secret_names()
    env_only = setup._external_env(home, data, secrets)
    for row in _features_table(home)["rows"]:
        _label, fid, tier, cost, now, source, wizard = (c["t"] for c in row["cells"])
        feat = CONTROLLABLE_FEATURES[fid]
        assert tier == ("premium" if feat.tier == "premium" else "baseline"), fid
        cur = feat_store.current(feat, environ=env_only, data=data, secrets=secrets)
        assert cost == f"×{cur['multiplier']:g}", fid
        assert now == ("off" if cur["off"] else cur["value"]), fid
        assert source == setup._SOURCES.get(cur["source"], cur["source"]), fid
        assert wizard == (feat.default or "—"), fid


def test_the_switch_for_each_bool_feature_toggles_it(home: Path) -> None:
    page = setup.build(home, "features", {})
    chips = [c for s in page["sections"] if s["type"] == "chips" for c in s["items"]]
    bool_feats = [f for f in CONTROLLABLE_FEATURES.values() if f.kind == "bool"]
    assert {c["t"] for c in chips} == {f.label for f in bool_feats}
    for chip in chips:
        cli = chip["do"]["cli"]
        assert cli[0] == "features" and cli[1] == "set" and cli[3] in ("on", "off")
        assert chip["do"].get("home") is False
        # A lit chip is one whose click turns the feature OFF (and vice versa).
        assert chip["on"] == (cli[3] == "off")


def test_every_feature_control_is_homeless_and_writes_features(home: Path) -> None:
    for tab in ("features", "settings"):
        for action in _walk_cli_prefixes(setup.build(home, tab, {})):
            assert action["cli"][0] == "features", action
            assert action.get("home", True) is False, action


def test_a_premium_feature_puts_the_opt_in_in_its_own_argv(home: Path) -> None:
    """The action path can pass no flag the user cannot see, so the opt-in rides the argv."""
    page = setup.build(home, "features", {})
    premium = {f.id for f in CONTROLLABLE_FEATURES.values() if f.tier == "premium"}
    non_bool = {f.id for f in CONTROLLABLE_FEATURES.values() if f.kind != "bool"}
    composers = {s["cli"][2]: s for s in page["sections"]
                 if s["type"] == "composer" and s["cli"][:2] == ["features", "set"]}
    # Every feature carrying a value is a composer; the bool ones are chips.
    assert set(composers) == non_bool
    for fid in non_bool:
        carries_opt_in = "--accept-premium" in composers[fid]["cli"]
        assert carries_opt_in == (fid in premium), fid
        assert (setup._OPT_IN in composers[fid]["note"]) == (fid in premium), fid


def test_the_premium_callout_is_present(home: Path) -> None:
    titles = [s["title"] for s in setup.build(home, "features", {})["sections"]]
    assert "Premium features" in titles


# ── the Settings tab ────────────────────────────────────────────────────────


def test_settings_tab_lists_every_catalogued_setting(home: Path) -> None:
    page = setup.build(home, "settings", {})
    assert page["errors"] == {}
    tables = {s["title"]: s for s in page["sections"] if s["type"] == "table"}
    assert set(tables) == set(sc.GROUPS)
    for group in sc.GROUPS:
        names = [row["cells"][1]["t"] for row in tables[group]["rows"]]
        assert names == [s.name for s in sc.settings_for(group)], group
    composers = [s for s in page["sections"] if s["type"] == "composer"]
    assert composers and all(s["cli"][:2] == ["features", "set-env"] for s in composers)


def test_a_project_set_setting_offers_a_clear_button(home: Path) -> None:
    feat_store.set_env("MO_DAILY_BUDGET_USD", "20", path=feat_store.store_path(home))
    table = next(s for s in setup.build(home, "settings", {})["sections"]
                 if s["type"] == "table" and s["title"] == "Cost & limits")
    row = next(r for r in table["rows"] if r["cells"][1]["t"] == "MO_DAILY_BUDGET_USD")
    assert row["cells"][2]["t"] == "20" and row["cells"][3]["t"] == "features.yaml"
    assert row["acts"][0]["do"] == {
        "cli": ["features", "unset-env", "MO_DAILY_BUDGET_USD"], "home": False}


# ── the page never carries a secret, in either direction ─────────────────────


def test_no_secret_value_reaches_the_config_tabs(home: Path) -> None:
    """The zed settings file in the home fixture holds a sentinel credential."""
    for tab in ("features", "settings"):
        assert "sk-SECRET-123" not in json.dumps(setup.build(home, tab, {})), tab


def test_no_action_enters_a_credential(home: Path) -> None:
    """``providers configure`` reads from a hidden prompt; a button would hang."""
    for tab in ("features", "settings"):
        for action in _walk_cli_prefixes(setup.build(home, tab, {})):
            cli = action["cli"]
            assert cli[:2] != ["providers", "configure"], cli
            assert not (cli[:2] == ["mcp", "add"] and "--secret" in cli), cli
