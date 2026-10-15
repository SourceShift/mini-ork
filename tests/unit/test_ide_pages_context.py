"""``board page context`` — the Context (ContextNest) page.

Five behavioural assertions over the page's structure: the chip on
disabled vs reachable CN, the section title list, the context-pack
table's cite column on a multi-run home, the Ping action's
``CN_PING_TTL=0`` contract, and the 7-day ``semantic_memory_uses``
counter. The detailed coverage of every endpoint and tier lives in
:mod:`tests.unit.test_cn_ui_context_page`; this file is the smoke
that the page still has the same skeleton after the rewrite.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

import pytest

from mini_ork.ide_pages import context
from mini_ork.stores import migrate as mig

from test_ide_pages_orch import check_page

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def home(tmp_path: Path, monkeypatch) -> Path:
    h = tmp_path / "proj" / ".mini-ork"
    h.mkdir(parents=True)
    rc, _out, err = mig.init_db(db=str(h / "state.db"), root=str(REPO))
    assert rc == 0, err
    monkeypatch.delenv("MO_DISABLE_CN", raising=False)
    # Default to "CN not reachable" so each test can stand up its own
    # stub server / mock only when the assertion needs reachable state.
    monkeypatch.setattr("mini_ork.cn_client.available", lambda: False)
    # Keep the cn_client caches pointed at the tmp home so un-isolated
    # runs do not share state across tests in the suite.
    monkeypatch.setenv("MINI_ORK_HOME", str(h))
    return h


def _run_with_pack(home: Path, run_id: str, pack: dict, created: int) -> Path:
    run_dir = home / "runs" / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "context-pack.json").write_text(json.dumps(pack))
    con = sqlite3.connect(home / "state.db")
    con.execute("INSERT INTO task_runs (id, recipe, status, created_at, updated_at, task_class, kickoff_path, "
                "workflow_version) VALUES (?,?,?,?,?,?,?,?)",
                (run_id, "code-fix", "published", created, created, "code_fix", "", "latest"))
    con.commit()
    con.close()
    return run_dir


def _section(page, title: str) -> dict:
    """Find a section by its title — the page never relies on order to
    be stable, so a positional lookup would couple the test to the
    section list."""
    for s in page["sections"]:
        if s["title"] == title:
            return s
    raise AssertionError(f"no section {title!r} in {[s['title'] for s in page['sections']]}")


def test_empty_home(home: Path) -> None:
    page = context.build(home, None, {})
    check_page(page, "context")
    assert [t["key"] for t in page["tabs"]] == ["overview", "search"] and page["errors"] == {}
    assert page["chips"][0]["t"].startswith("not reachable") and page["chips"][0]["c"] == "yellow"
    # The six epic sections, in the order the IDE walks them. The old
    # 3-section layout (["Context pack", "Pre-fetch & hooks", "Cache"])
    # has been replaced by the cn_client-driven structure.
    assert [s["title"] for s in page["sections"]] == [
        "ContextNest status",
        "What the planner receives",
        "Capsule preview (14d)",
        "Context pack",
        "Context reuse (local)",
        "ContextNest LLM cache",
    ]
    pack = _section(page, "Context pack")
    assert pack["rows"][0]["cells"][0]["t"].startswith("No run has assembled")


def test_context_pack_rows_carry_their_cites(home: Path) -> None:
    now = int(time.time())
    _run_with_pack(home, "run-1791000000-old000",
                   {"workflow_node": "planner", "tokens_estimated": 1000}, now - 100)
    _run_with_pack(home, "run-1791000001-abcdef", {
        "workflow_node": "planner",
        "task_brief": {"cite": "task_brief_path",
                       "content": {"kickoff": "# Fix add()\nbody"}},
        "prior_similar_runs": [{"cite": "execution_traces/tr-1",
                                "status": "success", "cost_usd": 0.05}],
        "known_failure_modes": [{"cite": "gradient_records/g-1",
                                 "signal": "verifier passed a partial verdict"}],
        "graph_context": {"linked_gradients": [{"cite": "gradient_records/g-2",
                                                "signal": "linked"}]},
        "tokens_estimated": 3000, "budget_tokens": 64000,
    }, now)
    table = _section(context.build(home, None, {}), "Context pack · run-abcdef planner")
    rows = [[c["t"] for c in r["cells"]] for r in table["rows"]]
    assert ["[task_brief_path]", "brief", "the kickoff · Fix add()"] in rows
    assert ["[execution_traces/tr-1]", "prior run", "success · $0.05"] in rows
    assert ["[gradient_records/g-1]", "failure mode", "verifier passed a partial verdict"] in rows
    assert ["[gradient_records/g-2]", "gradient", "linked"] in rows
    assert "~3,000 tokens of a 64,000 budget" in table["note"]
    # Local reuse (sqlite counters) is the renamed "Cache" panel from
    # the older layout; the page keeps the distinct label so the
    # ContextNest LLM cache panel (drawn from /llm/v1/cache/stats) is
    # never conflated with the operator's own hits.
    local = _section(context.build(home, None, {}), "Context reuse (local)")
    cache = {i["k"]: i["v"] for i in local["items"]}
    assert cache["Avg pack size"] == "2.0K tokens"


def test_disabled_and_connected_chips(home: Path, monkeypatch) -> None:
    monkeypatch.setenv("MO_DISABLE_CN", "1")
    page = context.build(home, None, {})
    assert page["chips"][0]["t"] == "disabled · MO_DISABLE_CN=1"
    # The "What the planner receives" section is where the disabled
    # contract surfaces — the planner section's first item warns that
    # no ContextNest context is wired into the planner prompt.
    planner = _section(page, "What the planner receives")
    assert planner["items"][0]["t"] == "Planner context"
    assert "no ContextNest context" in planner["items"][0]["sub"]
    monkeypatch.delenv("MO_DISABLE_CN")
    monkeypatch.setattr("mini_ork.cn_client.available", lambda: True)
    # The page times its own health read to tell reachable from slow; a live
    # service on a loaded machine can cross the ping budget and read as slow.
    monkeypatch.setattr("mini_ork.cn_client._get_text", lambda *_a, **_k: "")
    monkeypatch.setattr("mini_ork.cn_client._get", lambda *_a, **_k: "{}")
    monkeypatch.setenv("CN_BASE_URL", "http://127.0.0.1:28080")
    page = context.build(home, None, {})
    assert page["chips"][0] == {"t": "connected · 127.0.0.1:28080", "c": "green"}


def test_ping_forces_a_fresh_probe(home: Path, monkeypatch) -> None:
    monkeypatch.setenv("CN_PING_TTL", "30")
    page = context.build(home, None, {"ping": "1"})
    assert os.environ["CN_PING_TTL"] == "0"
    assert "set" in page["actions"][0]["do"]


def test_memory_retrievals_count_the_last_week(home: Path) -> None:
    con = sqlite3.connect(home / "state.db")
    now = time.time()
    for at in (now - 60, now - 3600, now - 30 * 86400):
        con.execute("INSERT INTO semantic_memory_uses (memory_id, scope, retrieved_at) VALUES (?,?,?)",
                    (1, "task", at))
    con.commit()
    con.close()
    page = context.build(home, None, {})
    local = _section(page, "Context reuse (local)")
    cache = {i["k"]: i["v"] for i in local["items"]}
    assert cache["Memory retrievals · 7d"] == "2"
