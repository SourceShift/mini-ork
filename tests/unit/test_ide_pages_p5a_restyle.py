"""IDE pages in Orca's language — the P5a restyle (spec level 2).

``lanes`` leads with an ``agents`` board, ``verify`` with a ``checks`` list,
``autos`` and ``recipes`` with ``columns`` boards. Level 1 must stay
byte-identical — an older IDE reads it — so every page is pinned at both
levels: the new section first with the old table still below it at level 2, and
exactly the old section list at level 1.

The fixture mirrors ``test_ide_pages_lanes.py``: a temp home with an
initialised DB and this repo's ``config/`` written into it, so ``lanes`` has
configured lanes and ``recipes`` reads the engine catalog.
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
from pathlib import Path

import pytest

from mini_ork import automations
from mini_ork.ide_pages import build_page
from mini_ork.stores import migrate as mig

REPO = Path(__file__).resolve().parents[2]

AGENTS = """
lanes:
  implementer: minimax
  planner: glm,minimax
  reviewer: glm
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
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for var in ("MINI_ORK_IDE_SPEC", "MINI_ORK_PROVIDERS", "MINI_ORK_SECRETS", "TEST_MINIMAX_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("TEST_GLM_KEY", "sk-test-value-never-shown")
    h = tmp_path / "proj" / ".mini-ork"
    (h / "config").mkdir(parents=True)
    rc, _out, err = mig.init_db(db=str(h / "state.db"), root=str(REPO))
    assert rc == 0, err
    (h / "config" / "agents.yaml").write_text(AGENTS, encoding="utf-8")
    (h / "config" / "providers.yaml").write_text(PROVIDERS, encoding="utf-8")
    return h


def _sql(home: Path, *statements: tuple[str, tuple]) -> None:
    con = sqlite3.connect(home / "state.db")
    for sql, params in statements:
        con.execute(sql, params)
    con.commit()
    con.close()


def _types(page: dict) -> list[str]:
    return [s["type"] for s in page["sections"]]


def _titles(page: dict) -> list[str]:
    return [s["title"] for s in page["sections"]]


def _first(page: dict) -> dict:
    return page["sections"][0]


# ── level 1 stays the old section list (env unset) ─────────────────────────


@pytest.mark.parametrize("key,tab,old", [
    ("lanes", "lanes", ["table", "list", "list"]),
    ("verify", "certify", ["kv", "table"]),
    ("autos", "automations", ["table"]),
    ("recipes", "recipes", ["table", "kv", "flow", "list", "list"]),
])
def test_level1_is_byte_identical(home: Path, key: str, tab: str, old: list[str]) -> None:
    page = build_page(home, key, tab, {})
    assert page["ok"] is True and page["errors"] == {}
    assert _types(page) == old


# ── level 2: the new section first, the old table still below it ───────────


@pytest.mark.parametrize("key,tab,new_type", [
    ("lanes", "lanes", "agents"),
    ("verify", "certify", "checks"),
    ("autos", "automations", "columns"),
    ("recipes", "recipes", "columns"),
])
def test_level2_leads_with_the_new_section_and_keeps_the_table(
        home: Path, monkeypatch: pytest.MonkeyPatch, key: str, tab: str, new_type: str) -> None:
    monkeypatch.setenv("MINI_ORK_IDE_SPEC", "2")
    page = build_page(home, key, tab, {})
    assert page["ok"] is True and page["errors"] == {}
    assert _first(page)["type"] == new_type
    assert "table" in _types(page)          # add, don't remove
    json.dumps(page)                        # the payload stays serialisable


# ── lanes: the agents board ────────────────────────────────────────────────


def _call(con: sqlite3.Connection, *, actor: str, model: str, status: str, cost: float = 0.0,
          provider: str = "gateway", error: str | None = None,
          ago_hours: float = 1.0) -> None:
    """One ``llm_calls`` row as dispatch writes it: the *role* in ``actor`` (a
    node name — `reviewer`, `gradient-extract`), the *lane alias* in
    ``model_id`` (`glm`, `codex`), and, when the call failed, its
    ``error_message``."""
    ts = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=ago_hours)).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z")
    con.execute(
        "INSERT INTO llm_calls (provider, model_id, tier, feature_name, actor, status, cost_usd, ts, "
        "run_id, total_tokens, error_message) "
        "VALUES (?, ?, 'std', 'mini-ork:worker', ?, ?, ?, ?, 'run-x', 100, ?)",
        (provider, model, actor, status, cost, ts, error))


def _table_row(page: dict, title: str, lane: str) -> list[dict]:
    table = next(s for s in page["sections"] if s.get("title") == title)
    for row in table["rows"]:
        cells = row["cells"] if isinstance(row, dict) else row
        if cells and cells[0]["t"] == lane:
            return cells
    raise AssertionError(f"no {lane!r} row in {title!r}")


def test_lanes_agents_show_calls_failed_and_state(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MINI_ORK_IDE_SPEC", "2")
    con = sqlite3.connect(home / "state.db")
    # Roles in ``actor``, lane aliases in ``model_id`` — the shape production
    # writes, and the one a header-based board read as zero calls.
    for _ in range(3):
        _call(con, actor="reviewer", model="glm", status="failed", cost=0.10, error="boom")
    _call(con, actor="reviewer", model="glm", status="success", cost=0.25)
    _call(con, actor="worker", model="minimax", status="success", cost=1.00)
    con.commit()
    con.close()

    page = build_page(home, "lanes", "lanes", {})
    board = _first(page)
    assert board["type"] == "agents" and board["title"] == "Lanes · last 24 h"
    rows = {r["id"]: r for r in board["rows"]}
    assert set(rows) == {"glm", "minimax", "opus"}        # one row per configured lane
    assert rows["glm"]["lane"] == "anthropic-compat"      # the provider kind
    assert rows["glm"]["model"] == "GLM-5.3"
    assert rows["glm"]["step"] == "4 calls · 3 failed"
    assert rows["glm"]["state"] == "failed"               # 3/4 failed, ≥ 3 calls
    assert rows["glm"]["cost"] == "$0.55"
    assert rows["glm"]["last"] == "boom"                  # the failing call's error_message
    assert rows["minimax"]["step"] == "1 calls · 0 failed"
    assert rows["minimax"]["state"] == "running"
    assert rows["minimax"]["last"] == ""                  # no failure → no "last" line
    assert rows["opus"]["state"] == "pending"             # no calls in the window

    # The board and the table below it read the same rows, so they agree — the
    # board used to show 0 calls for every lane while this table showed the
    # real counts.
    assert _titles(page)[1] == "All · Lanes"
    table_row = _table_row(page, "All · Lanes", "glm")
    assert table_row[3]["t"] == rows["glm"]["step"].split(" ")[0]   # calls
    assert table_row[5]["t"] == "3 errors"                          # failed


def test_lanes_agents_last_is_the_newest_failure(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The "last" line is the newest failing row's message, read by lane alias.

    A header roll-up buckets by ``actor`` (a role name here), so every lane
    would read an empty "last" even though the ledger's rows carry an
    ``error_message`` — the live board showed 39 failed calls and no message.
    """
    monkeypatch.setenv("MINI_ORK_IDE_SPEC", "2")
    con = sqlite3.connect(home / "state.db")
    _call(con, actor="reviewer", model="glm", status="failed", error="older: rate limit", ago_hours=5)
    _call(con, actor="reviewer", model="glm", status="failed", error="newer: context too long", ago_hours=2)
    con.commit()
    con.close()

    board = _first(build_page(home, "lanes", "lanes", {}))
    rows = {r["id"]: r for r in board["rows"]}
    assert rows["glm"]["last"] == "newer: context too long"   # newest wins, not oldest
    assert rows["glm"]["step"] == "2 calls · 2 failed"


# ── verify: the certificates checks list ───────────────────────────────────


def _cert(home: Path, name: str, **fields: object) -> None:
    (home / "certificates").mkdir(exist_ok=True)
    cert = {"schema": "mini-ork.certificate/v1", "verdict": "PROVEN", "reason": "probe flips",
            "claim": {"summary": "dark mode is lost on reload"},
            "repo": {"path": "/x/proj", "head": "9c1f00000000e07a"},
            "method": {"model": "sonnet"}, "cost": {"usd": 0.12}, "digest": "9c1f00000000e07a",
            "evidence": {"probe": "assert read() == 'dark'", "invariants": []}}
    cert.update(fields)
    (home / "certificates" / name).write_text(json.dumps(cert), encoding="utf-8")


def test_verify_certificates_are_checks_rows(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MINI_ORK_IDE_SPEC", "2")
    _cert(home, "c1.json")
    _cert(home, "c2.json", verdict="REFUTED", reason="probe never fails",
          claim={"summary": "the fix regresses reload"})
    page = build_page(home, "verify", "certify", {})
    checks = _first(page)
    assert checks["type"] == "checks" and checks["title"] == "Certificates"
    by_name = {r["name"]: r for r in checks["rows"]}
    proven = by_name["dark mode is lost on reload"]     # no target → the claim
    assert proven["state"] == "pass" and proven["detail"] == "probe flips"
    assert proven["do"]["path"].endswith("c1.json")
    assert by_name["the fix regresses reload"]["state"] == "fail"
    assert checks["summary"] == {"passing": 1, "failing": 1, "pending": 0, "na": 0}
    assert "All · Recent certificates" in _titles(page)


def test_verify_with_no_certificates_still_shows_the_section(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MINI_ORK_IDE_SPEC", "2")
    checks = _first(build_page(home, "verify", "certify", {}))
    assert checks["type"] == "checks"
    assert checks["rows"][0]["name"] == "No certificates yet"
    assert checks["rows"][0]["state"] == "na"


# ── autos: the state board ─────────────────────────────────────────────────


def _automation(aid: str, *, enabled: bool = True, last_error: str | None = None) -> dict:
    return {"id": aid, "name": aid, "recipe": "dep-check", "kickoff": "k", "schedule": "0 2 * * *",
            "workspace": "worktree", "enabled": enabled, "created_at": "2026-10-01T00:00:00",
            "last_fired_at": None, "last_run_id": None, "last_error": last_error, "runs": []}


def _write_automations(home: Path, items: list[dict]) -> None:
    automations._store_path(home).write_text(json.dumps({"automations": items}), encoding="utf-8")


def test_autos_board_groups_by_state(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MINI_ORK_IDE_SPEC", "2")
    _write_automations(home, [_automation("nightly"),
                              _automation("paused-one", enabled=False),
                              _automation("broken", last_error="no such recipe")])
    page = build_page(home, "autos", "automations", {})
    board = _first(page)
    assert board["type"] == "columns" and board["title"] == "Automations"
    assert [c["title"] for c in board["cols"]] == ["Enabled", "Paused", "Disabled"]
    # ``broken`` is enabled — it fires again on its schedule — so it stays in
    # Enabled with a ✗ mark, and the Disabled column is empty: the store has no
    # disabled state.
    assert [c["count"] for c in board["cols"]] == [2, 1, 0]
    by_title = {c["title"]: c for c in board["cols"]}
    cards = by_title["Enabled"]["cards"]
    nightly = next(c for c in cards if c["id"] == "nightly")
    assert set(nightly) == {"id", "title", "sub", "state", "mark", "meta", "do"}
    assert nightly["title"] == "nightly"
    assert nightly["sub"] == "every day at 02:00"        # the schedule, described
    assert nightly["state"] == "running" and nightly["mark"] == "●"
    assert nightly["do"] == {"set": {"auto": "nightly"}}
    broken = next(c for c in cards if c["id"] == "broken")
    assert broken["state"] == "failed" and broken["mark"] == "✗"
    assert broken["meta"][0]["t"] == "✗ not started"     # not the raw error text
    assert by_title["Paused"]["cards"][0]["mark"] == "⏸"
    assert by_title["Disabled"]["cards"] == []
    assert "All · Automations" in _titles(page)


def test_autos_card_meta_reuses_the_table_last_parse(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The chip carries the table's parsed "last" text, not the raw ``last_run``
    string — that one holds backticks, and on a failed start the whole error."""
    monkeypatch.setenv("MINI_ORK_IDE_SPEC", "2")
    fired = _automation("ran")
    fired["last_run_id"] = "run-7"
    fired["last_fired_at"] = int(dt.datetime.now().timestamp()) - 3 * 3600
    _write_automations(home, [fired])
    page = build_page(home, "autos", "automations", {})
    card = _first(page)["cols"][0]["cards"][0]
    # ``_last_cell``'s parse: mark + status + age.
    assert card["meta"][0]["t"] == "● starting · 3h ago"
    assert "`" not in card["meta"][0]["t"]


# ── recipes: the family board ──────────────────────────────────────────────


def test_recipes_board_groups_by_task_class_family(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MINI_ORK_IDE_SPEC", "2")
    page = build_page(home, "recipes", "recipes", {})
    board = _first(page)
    assert board["type"] == "columns" and board["title"] == "Recipes"
    assert [c["title"] for c in board["cols"]] == ["Code", "Research", "Ops / other"]
    by_title = {c["title"]: c for c in board["cols"]}
    code_ids = [c["id"] for c in by_title["Code"]["cards"]]
    assert "code-fix" in code_ids and "framework-edit" in code_ids
    assert "research-synthesis" in [c["id"] for c in by_title["Research"]["cards"]]
    card = next(c for c in by_title["Code"]["cards"] if c["id"] == "code-fix")
    assert set(card) == {"id", "title", "sub", "state", "mark", "meta", "do"}
    assert card["sub"]                                   # a one-line description
    assert "\n" not in card["sub"]
    assert card["do"] == {"set": {"recipe": "code-fix"}}
    assert "All · Catalog" in _titles(page)
