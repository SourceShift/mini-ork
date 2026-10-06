"""``board page context`` — the Context (ContextNest) page."""
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
    monkeypatch.setattr("mini_ork.cn_client.available", lambda: False)
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


def test_empty_home(home: Path) -> None:
    page = context.build(home, None, {})
    check_page(page, "context")
    assert page["tabs"] == [] and page["errors"] == {}
    assert page["chips"][0]["t"].startswith("not reachable") and page["chips"][0]["c"] == "yellow"
    assert [s["title"] for s in page["sections"]] == ["Context pack", "Pre-fetch & hooks", "Cache"]
    assert page["sections"][0]["rows"][0]["cells"][0]["t"].startswith("No run has assembled")


def test_context_pack_rows_carry_their_cites(home: Path) -> None:
    now = int(time.time())
    _run_with_pack(home, "run-1791000000-old000", {"workflow_node": "planner", "tokens_estimated": 1000}, now - 100)
    _run_with_pack(home, "run-1791000001-abcdef", {
        "workflow_node": "planner",
        "task_brief": {"cite": "task_brief_path", "content": {"kickoff": "# Fix add()\nbody"}},
        "prior_similar_runs": [{"cite": "execution_traces/tr-1", "status": "success", "cost_usd": 0.05}],
        "known_failure_modes": [{"cite": "gradient_records/g-1", "signal": "verifier passed a partial verdict"}],
        "graph_context": {"linked_gradients": [{"cite": "gradient_records/g-2", "signal": "linked"}]},
        "tokens_estimated": 3000, "budget_tokens": 64000,
    }, now)
    table = context.build(home, None, {})["sections"][0]
    assert table["title"] == "Context pack · run-abcdef planner"
    rows = [[c["t"] for c in r["cells"]] for r in table["rows"]]
    assert ["[task_brief_path]", "brief", "the kickoff · Fix add()"] in rows
    assert ["[execution_traces/tr-1]", "prior run", "success · $0.05"] in rows
    assert ["[gradient_records/g-1]", "failure mode", "verifier passed a partial verdict"] in rows
    assert ["[gradient_records/g-2]", "gradient", "linked"] in rows
    assert "~3,000 tokens of a 64,000 budget" in table["note"]
    cache = {i["k"]: i["v"] for i in context.build(home, None, {})["sections"][2]["items"]}
    assert cache["Avg pack size"] == "2.0K tokens"


def test_disabled_and_connected_chips(home: Path, monkeypatch) -> None:
    monkeypatch.setenv("MO_DISABLE_CN", "1")
    page = context.build(home, None, {})
    assert page["chips"][0]["t"] == "disabled · MO_DISABLE_CN=1"
    assert page["sections"][1]["items"][1]["t"] == "ContextNest packs off"
    monkeypatch.delenv("MO_DISABLE_CN")
    monkeypatch.setattr("mini_ork.cn_client.available", lambda: True)
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
        con.execute("INSERT INTO semantic_memory_uses (memory_id, scope, retrieved_at) VALUES (?,?,?)", (1, "task", at))
    con.commit()
    con.close()
    cache = {i["k"]: i["v"] for i in context.build(home, None, {})["sections"][2]["items"]}
    assert cache["Memory retrievals · 7d"] == "2"
