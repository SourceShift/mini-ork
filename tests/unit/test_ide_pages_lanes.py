"""IDE page ``lanes`` — Models, lanes & cost, from the project's config and ledger."""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
from pathlib import Path

import pytest

from mini_ork.ide_pages import build_page, lanes
from mini_ork.stores import migrate as mig

REPO = Path(__file__).resolve().parents[2]
SECRET = "sk-test-value-never-shown-0123456789"

AGENTS = """
lanes:
  implementer: minimax
  planner: glm,minimax
  reviewer: glm
budget:
  per_run_usd: 12.00
  per_epic_usd: 50.00
"""

PROVIDERS = """
providers:
  glm:
    kind: anthropic-compat
    model: GLM-5.3
    base_url: https://example.invalid/anthropic
    api_key_env: TEST_GLM_KEY
  minimax:
    kind: anthropic-compat
    model: MiniMax-M3
    base_url: https://example.invalid/anthropic
    api_key_env: TEST_MINIMAX_KEY
  opus:
    kind: anthropic-native
"""


@pytest.fixture
def home(tmp_path: Path, monkeypatch) -> Path:
    for var in ("MINI_ORK_SECRETS", "MINI_ORK_PROVIDERS", "MINI_ORK_AGENTS", "MO_ROUTING_POLICY",
                "TEST_MINIMAX_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("TEST_GLM_KEY", SECRET)
    monkeypatch.setenv("MO_DAILY_BUDGET_USD", "10")
    h = tmp_path / "proj" / ".mini-ork"
    (h / "config").mkdir(parents=True)
    rc, _out, err = mig.init_db(db=str(h / "state.db"), root=str(REPO))
    assert rc == 0, err
    (h / "config" / "agents.yaml").write_text(AGENTS, encoding="utf-8")
    (h / "config" / "providers.yaml").write_text(PROVIDERS, encoding="utf-8")
    return h


def _call(con, *, model: str, feature: str, cost: float, ago_h: float = 1, status: str = "success",
          run_id: str = "run-1791000000-aaaaaa", tokens: int = 2200) -> None:
    ts = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=ago_h)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    con.execute("INSERT INTO llm_calls (provider, model_id, tier, feature_name, status, cost_usd, ts, run_id, "
                "total_tokens) VALUES ('gateway', ?, 'std', ?, ?, ?, ?, ?, ?)",
                (model, feature, status, cost, ts, run_id, tokens))


@pytest.fixture
def ledger(home: Path) -> Path:
    con = sqlite3.connect(home / "state.db")
    _call(con, model="glm", feature="mini-ork:reviewer", cost=0.50)
    _call(con, model="glm", feature="mini-ork:gradient-extract", cost=0.25, status="failed")
    _call(con, model="minimax", feature="mini-ork:worker", cost=1.00)
    _call(con, model="minimax", feature="mini-ork:worker", cost=9.00, ago_h=24 * 3)
    con.execute("INSERT INTO agent_performance_memory (agent_version_id, role, model, task_class, runs_count, "
                "success_count, relative_advantage) VALUES ('a', 'implementer', 'minimax', 'code_fix', 30, 20, 0.1)")
    con.execute("INSERT INTO agent_performance_memory (agent_version_id, role, model, task_class, runs_count, "
                "success_count, relative_advantage) VALUES ('b', 'implementer', 'glm', 'code_fix', 10, 5, -0.2)")
    con.commit()
    con.close()
    return home


def _section(page: dict, title: str) -> dict:
    return next(s for s in page["sections"] if s["title"] == title)


def test_page_shape_matches_the_design(home: Path) -> None:
    page = lanes.build(home, None, {})
    assert page["ok"] and page["key"] == "lanes" and page["title"] == "Models, lanes & cost"
    assert [(t["key"], t["label"]) for t in page["tabs"]] == [
        ("lanes", "Lanes & providers"), ("routing", "Routing"), ("cost", "Cost & budget")]
    assert page["tab"] == "lanes"
    assert page["chips"][0]["t"].endswith(" today")
    assert [s["title"] for s in page["sections"]] == ["Lanes", "Credentials", "Bring your own provider"]
    assert page["errors"] == {}


def test_lanes_table_roles_calls_and_state(ledger: Path) -> None:
    table = _section(lanes.build(ledger, "lanes", {}), "Lanes")
    assert table["head"] == ["lane", "provider", "roles", "calls", "today", "state"]
    rows = {r["cells"][0]["t"]: [c["t"] for c in r["cells"]] for r in table["rows"]}
    assert rows["glm"][2] == "planner, reviewer"
    assert rows["glm"][3] == "2" and rows["glm"][4] == "$0.75"
    assert rows["glm"][5] == "1 error"
    assert rows["minimax"][2] == "implementer"
    assert rows["minimax"][3] == "1"                      # the 3-day-old call is outside the window
    assert rows["minimax"][5] == "no key"
    assert rows["opus"][2] == "—" and rows["opus"][5] == "ok"


def test_credentials_name_keys_but_never_show_values(home: Path) -> None:
    page = lanes.build(home, "lanes", {})
    creds = _section(page, "Credentials")
    texts = [i["t"] for i in creds["items"]]
    assert "TEST_GLM_KEY" in texts
    assert "TEST_MINIMAX_KEY missing" in texts
    assert any(t.startswith("Local logins: opus") for t in texts)
    assert SECRET not in json.dumps(page)


def test_secret_store_names_count_as_configured(home: Path) -> None:
    store = home / "config" / "secrets.local.sh"
    store.write_text(f"export TEST_MINIMAX_KEY='{SECRET}'\n", encoding="utf-8")
    store.chmod(0o600)
    page = lanes.build(home, "lanes", {})
    texts = [i["t"] for i in _section(page, "Credentials")["items"]]
    assert "TEST_GLM_KEY, TEST_MINIMAX_KEY" in texts
    assert SECRET not in json.dumps(page)


def test_routing_tab(ledger: Path) -> None:
    page = lanes.build(ledger, "routing", {})
    assert [s["title"] for s in page["sections"]] == [
        "Role → lane ladder", "GRPO bandit · implementer arms", "Router calibration", "Cost advisor · per turn"]
    ladder = {r["cells"][0]["t"]: r["cells"][1]["t"] for r in _section(page, "Role → lane ladder")["rows"]}
    assert ladder == {"implementer": "minimax", "planner": "glm → minimax", "reviewer": "glm"}
    bars = _section(page, "GRPO bandit · implementer arms")["items"]
    assert [(b["label"], b["pct"]) for b in bars] == [("minimax", 75.0), ("glm", 25.0)]
    assert bars[0]["val"] == "30 runs · adv +0.10"
    calib = {i["k"]: i["v"] for i in _section(page, "Router calibration")["items"]}
    assert calib["Policy"] == "default" and calib["Arm records"] == "2"


def test_cost_tab(ledger: Path) -> None:
    page = lanes.build(ledger, "cost", {})
    assert [s["title"] for s in page["sections"]] == [
        "Budget caps", "Today by stage", "Last 7 days", "Guards", "Ledger · latest calls"]
    caps = {i["k"]: i for i in _section(page, "Budget caps")["items"]}
    assert caps["Today"]["v"] == "$1.75 of $10.00"
    assert caps["Per run"]["v"] == "$12.00" and caps["Per epic"]["v"] == "$50.00"
    stages = {b["label"]: b["val"] for b in _section(page, "Today by stage")["items"]}
    assert stages == {"worker": "$1.00", "reviewer": "$0.50", "learning": "$0.25"}
    week = _section(page, "Last 7 days")["items"]
    assert len(week) == 7 and week[-1]["label"] == "Today"
    assert sum(float(b["val"].lstrip("$")) for b in week) == pytest.approx(10.75)
    guards = [i["t"] for i in _section(page, "Guards")["items"]]
    assert "No cost pause" in guards
    ledger_rows = _section(page, "Ledger · latest calls")["rows"]
    assert len(ledger_rows) == 4
    assert [c["t"] for c in ledger_rows[0]["cells"]][1:] == ["minimax", "worker", "2.2K", "$9.00"]


def test_entrypoint_every_tab(ledger: Path) -> None:
    for tab in ("lanes", "routing", "cost"):
        page = build_page(ledger, "lanes", tab, {})
        assert page["ok"] and page["tab"] == tab and page["errors"] == {}
        json.dumps(page)
