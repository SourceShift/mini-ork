"""IDE page ``learn`` — Learning & memory, from state.db and the TraceOtter output."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

from mini_ork.ide_pages import build_page, learn
from mini_ork.stores import migrate as mig

REPO = Path(__file__).resolve().parents[2]
TABS = ["learnings", "memory", "improve", "bugs", "traceotter"]


@pytest.fixture
def home(tmp_path: Path) -> Path:
    h = tmp_path / "proj" / ".mini-ork"
    h.mkdir(parents=True)
    rc, _out, err = mig.init_db(db=str(h / "state.db"), root=str(REPO))
    assert rc == 0, err
    return h


@pytest.fixture
def seeded(home: Path) -> Path:
    now = int(time.time())
    con = sqlite3.connect(home / "state.db")
    con.execute("INSERT INTO gradient_records (gradient_id, target, signal, suggested_change, evidence, "
                "confidence, created_at, task_class) VALUES ('g1', 'verifier.code_fix', 'checks unclear', "
                "'record the checks', '{}', 0.84, ?, 'code_fix')", (now,))
    for i in range(3):
        con.execute("INSERT INTO failure_memory (run_id, workflow_stage, failure_category, error_message, "
                    "occurred_at) VALUES (?, 'execute', 'dispatch_error', 'rc=124', ?)", (i, f"2026-10-0{i + 1}"))
    have = {r[1] for r in con.execute("PRAGMA table_info(semantic_memory)")}
    for col, decl in (("uses", "INTEGER NOT NULL DEFAULT 0"), ("wins", "INTEGER NOT NULL DEFAULT 0"),
                      ("retired_at", "REAL NOT NULL DEFAULT 0")):
        if col not in have:   # mini_ork.memory.semantic adds these on first use
            con.execute(f"ALTER TABLE semantic_memory ADD COLUMN {col} {decl}")
    con.execute("INSERT INTO semantic_memory (scope, text, embedding, created_at, uses, wins, retired_at) "
                "VALUES ('code_fix', 'a', x'00', ?, 10, 1, 0)", (now,))      # decaying
    con.execute("INSERT INTO semantic_memory (scope, text, embedding, created_at, uses, wins, retired_at) "
                "VALUES ('code_fix', 'b', x'00', ?, 1, 1, 0)", (now,))       # active
    con.execute("INSERT INTO semantic_memory (scope, text, embedding, created_at, uses, wins, retired_at) "
                "VALUES ('code_fix', 'c', x'00', ?, 9, 0, ?)", (now, now))  # retired
    con.execute("INSERT INTO self_improve_runs (run_id, started_at, finished_at, iter, soft_deadline_at, "
                "hard_deadline_at, outcome, notes) VALUES ('si-1', ?, ?, 3, 0, 0, 'success', 'inject g-121')",
                (now - 600, now))
    con.execute("INSERT INTO llm_calls (provider, model_id, tier, feature_name, status, cost_usd, ts, run_id) "
                "VALUES ('gateway', 'glm', 'std', 'mini-ork:worker', 'success', 1.5, '2026-10-06T00:00:00Z', 'si-1')")
    con.execute("INSERT INTO promotion_records (promotion_id, candidate_id, from_version_id, to_version_id, "
                "utility_before, utility_after, decision, decided_at, decided_by) VALUES "
                "('p1', 'cand-1', 'v0.4.1', 'v0.4.2', 0.86, 0.91, 'promoted', '2026-10-05T10:00:00Z', 'gate')")
    con.execute("INSERT INTO bug_reports (fingerprint, agent_role, title, observed_in, confidence, status, "
                "first_seen_at, last_seen_at, updated_at) VALUES ('fp', 'scheduler', 'verdict.json mismatch', "
                "'bin/mini-ork-scheduler', 0.95, 'open', ?, ?, ?)", (now, now, now))
    con.execute("INSERT INTO idea_tree_nodes (node_id, parent_node_id, root_node_id, hypothesis, status, "
                "created_at) VALUES ('r', NULL, 'r', 'dark mode support', 'pending', ?)", (now,))
    con.execute("INSERT INTO idea_tree_nodes (node_id, parent_node_id, root_node_id, hypothesis, status, "
                "created_at) VALUES ('c1', 'r', 'r', 'tokens map', 'harvested', ?)", (now + 1,))
    con.execute("INSERT INTO idea_tree_nodes (node_id, parent_node_id, root_node_id, hypothesis, status, "
                "created_at) VALUES ('c2', 'r', 'r', 'css media query', 'pruned', ?)", (now + 2,))
    con.commit()
    con.close()
    return home


def _section(page: dict, title: str) -> dict:
    return next(s for s in page["sections"] if s["title"] == title)


def test_page_shape_and_every_tab_on_an_empty_project(home: Path) -> None:
    for tab in TABS:
        page = learn.build(home, tab, {})
        assert page["ok"] and page["key"] == "learn" and page["title"] == "Learning & memory"
        assert [(t["key"], t["label"]) for t in page["tabs"]] == [
            ("learnings", "Learnings"), ("memory", "Memory"), ("improve", "Self-improve"),
            ("bugs", "Bug reports"), ("traceotter", "TraceOtter")]
        assert page["tab"] == tab
        assert page["errors"] == {}, (tab, page["errors"])
        json.dumps(page)
    assert learn.build(home, None, {})["tab"] == "learnings"


def test_learnings_tab(seeded: Path) -> None:
    page = learn.build(seeded, "learnings", {})
    assert [s["title"] for s in page["sections"]] == ["Gradients", "Patterns", "Failure modes"]
    grads = _section(page, "Gradients")
    assert grads["head"] == ["gradient", "task class", "conf", "injected"]
    assert [c["t"] for c in grads["rows"][0]["cells"]] == [
        "verifier.code_fix · checks unclear", "code_fix", "0.84", "—"]
    fm = _section(page, "Failure modes")["items"][0]
    assert fm["t"] == "dispatch_error · execute" and fm["m"] == "✗"
    assert fm["sub"].startswith("3 times in 3 runs")


def test_memory_tab(seeded: Path) -> None:
    page = learn.build(seeded, "memory", {})
    bars = {b["label"]: b["val"] for b in _section(page, "Namespaces · state.db")["items"]}
    assert set(bars) == {"task", "workflow", "agent_performance", "failure", "recovery", "user_preference",
                         "artifact", "benchmark"}
    assert bars["failure"] == "3"
    life = {i["k"]: i["v"] for i in _section(page, "Semantic memory lifecycle")["items"]}
    assert life == {"Active": "1", "Decaying": "1", "Retired": "1"}
    tree = _section(page, "Idea tree · dark mode support")
    lines = [line["t"] for line in tree["lines"]]
    assert lines[0] == "root  dark mode support"
    assert lines[1].startswith("├─ tokens map") and lines[2].startswith("└─ css media query")
    assert [line["c"] for line in tree["lines"][1:]] == ["green", "red"]


def test_improve_tab(seeded: Path) -> None:
    page = learn.build(seeded, "improve", {})
    assert [s["type"] for s in page["sections"]] == ["flow", "kv", "table", "list"]
    loop = {i["label"]: i["sub"] for i in _section(page, "Loop")["items"]}
    assert loop["reflect"] == "1 gradients this week"
    assert loop["promote"] == "promoted"
    cand = _section(page, "Candidate cand-1 vs v0.4.1")
    assert {i["k"]: i["v"] for i in cand["items"]}["Utility"] == "0.86 → 0.91"
    ledger = _section(page, "Self-improve ledger")
    assert [c["t"] for c in ledger["rows"][0]["cells"]] == ["3", "success", "inject g-121", "$1.50", "10m 00s"]
    assert ledger["actions"][0]["do"] is None          # started from a terminal, not a button


def test_bugs_tab(seeded: Path) -> None:
    table = _section(learn.build(seeded, "bugs", {}), "Bug reports")
    assert table["head"] == ["id", "report", "source", "score"]
    assert [c["t"] for c in table["rows"][0]["cells"]][1:] == ["verdict.json mismatch", "mini-ork-scheduler", "0.95"]
    actions = {a["label"]: a["do"] for a in table["actions"]}
    assert actions["Sweep runs"]["cli"] == ["bugs", "sweep"]
    assert actions["Sweep runs"]["home"] is False
    assert actions["Promote top 3"]["cli"] == ["bugs", "promote", "--top", "3"]
    assert actions["Promote top 3"]["home"] is False


def test_traceotter_not_run_is_distinct_from_empty(home: Path) -> None:
    page = learn.build(home, "traceotter", {})
    corpus = _section(page, "Corpus")
    assert corpus["items"][0]["t"] == "TraceOtter has not run here"


def test_traceotter_corpus(home: Path) -> None:
    d = home / "traceotter"
    d.mkdir()
    (d / "report.json").write_text(json.dumps({"episodes": 2, "skills": 1,
                                               "llamafactory": {"examples": 1}}), encoding="utf-8")
    (d / "episodes.jsonl").write_text(
        json.dumps({"labels": {"shouldImitate": True, "failureModes": ["timeout"]}}) + "\n"
        + json.dumps({"labels": {"shouldImitate": False}}) + "\n", encoding="utf-8")
    page = learn.build(home, "traceotter", {})
    flow = [i["sub"] for i in _section(page, "Pipeline")["items"]]
    assert flow[0] == "2 trajectories" and flow[1] == "1 worth imitating"
    corpus = {i["k"]: i["v"] for i in _section(page, "Corpus")["items"]}
    assert corpus["Episodes"] == "2" and corpus["Skills"] == "1" and corpus["SFT examples"] == "1"
    assert _section(page, "Failure modes the distiller saw")["items"][0]["t"] == "timeout"


def test_entrypoint(seeded: Path) -> None:
    start = time.monotonic()
    for tab in TABS:
        assert build_page(seeded, "learn", tab, {})["ok"]
    assert time.monotonic() - start < 5
