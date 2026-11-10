"""`mini-ork why` joins the four provenance stores (P8).

Attribution answers "which model produced this artifact, and why", but it lives
in four places: the routing reason on the trace row, the served-lane file the
dispatcher writes, the lane calls in ``llm_calls``, and the injected-memory
ledger under ``learned/``. Each test here pins one join and one honest gap —
a store with no row says "not recorded" rather than printing a confident zero.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from mini_ork.cli import why  # noqa: E402


def _db(path: Path) -> str:
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE execution_traces (
          trace_id TEXT, agent_version_id TEXT, route_source TEXT,
          route_explore INTEGER, route_score REAL, route_margin REAL,
          predicted_error REAL, status TEXT, verifier_output TEXT,
          created_at TEXT, run_id TEXT);
        CREATE TABLE llm_calls (
          id INTEGER PRIMARY KEY, actor TEXT, model_id TEXT, status TEXT,
          input_tokens INTEGER, output_tokens INTEGER, cost_usd REAL,
          session_id TEXT, ts TEXT, feature_name TEXT, run_id TEXT);
        """
    )
    con.commit()
    con.close()
    return str(path)


def _home(tmp_path: Path, run: str = "r1") -> tuple[Path, Path, str]:
    home = tmp_path / ".mini-ork"
    run_dir = home / "runs" / run
    (run_dir / "learned").mkdir(parents=True)
    return home, run_dir, _db(home / "state.db")


def _lane(run_dir: Path, node: str, requested: str, served: str, **extra) -> None:
    doc = {"requested": requested, "served": served,
           "fallback": served != requested, "at": 1, **extra}
    (run_dir / f".lane-{node}.json").write_text(json.dumps(doc), encoding="utf-8")


def _learned(run_dir: Path, node: str, **extra) -> None:
    doc = {"node_id": node, "node_type": "researcher", "injected": True,
           "reason": "", "arm": "v2", "attempt": 1,
           "sources": [{"id": "c:0", "kind": "context_v2"},
                       {"id": "pat-1", "kind": "pattern"}], **extra}
    (run_dir / "learned" / f"{node}.json").write_text(json.dumps(doc), encoding="utf-8")


def _trace(db: str, run: str, node: str, **over) -> None:
    row = {"trace_id": f"tr-researcher-{node}-abc", "agent_version_id": "glm",
           "route_source": "policy", "route_explore": 0, "route_score": 0.5,
           "route_margin": 0.2, "predicted_error": 0.11, "status": "success",
           "verifier_output": json.dumps({"node_type": "researcher",
                                          "node_id": node}), "created_at": "2026"}
    row.update(over)
    con = sqlite3.connect(db)
    con.execute(
        "INSERT INTO execution_traces VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (row["trace_id"], row["agent_version_id"], row["route_source"],
         row["route_explore"], row["route_score"], row["route_margin"],
         row["predicted_error"], row["status"], row["verifier_output"],
         row["created_at"], run))
    con.commit()
    con.close()


def _call(db: str, run: str, actor: str, lane: str, **over) -> None:
    row = {"status": "success", "input_tokens": 100, "output_tokens": 20,
           "cost_usd": 0.25, "session_id": "s-1", "ts": "2026-01-01T00:00:00",
           "feature_name": f"mini-ork:{actor}"}
    row.update(over)
    con = sqlite3.connect(db)
    con.execute(
        "INSERT INTO llm_calls (actor, model_id, status, input_tokens, "
        "output_tokens, cost_usd, session_id, ts, feature_name, run_id) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (actor, lane, row["status"], row["input_tokens"], row["output_tokens"],
         row["cost_usd"], row["session_id"], row["ts"], row["feature_name"], run))
    con.commit()
    con.close()


def _run(*args) -> tuple[int, str]:
    import io
    from contextlib import redirect_stdout

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = why.main([*args], root=str(REPO))
    return rc, buf.getvalue()


def test_full_card_joins_all_four_stores(tmp_path):
    home, run_dir, db = _home(tmp_path)
    _lane(run_dir, "lens", "opus_lens", "glm")
    _learned(run_dir, "lens")
    _trace(db, "r1", "lens")
    _call(db, "r1", "lens", "glm")
    card = why.collect("r1", "lens", home=str(home), db=db)
    assert card["lane"] == {"requested": "opus_lens", "served": "glm",
                            "fallback": True, "family": "zai",
                            "requested_family": "anthropic"}
    assert card["route"]["source"] == "policy"
    assert card["route"]["margin"] == 0.2
    assert card["walked"][0]["lane"] == "glm"
    assert [s["id"] for s in card["memory"]["sources"]] == ["c:0", "pat-1"]
    assert card["node_type"] == "researcher"
    assert card["notes"] == []


def test_render_names_the_fallback_and_the_cost(tmp_path):
    home, run_dir, db = _home(tmp_path)
    _lane(run_dir, "lens", "opus_lens", "glm")
    _learned(run_dir, "lens")
    _trace(db, "r1", "lens")
    _call(db, "r1", "lens", "glm")
    rc, out = _run("r1", "lens", "--home", str(home), "--db", db)
    assert rc == 0
    assert "opus_lens (anthropic) -> glm (zai)" in out
    assert "[fell back]" in out
    assert "predicted_error=0.1100" in out
    assert "total $0.2500 across 1 call(s)" in out
    assert "2 source id(s): c:0, pat-1" in out


def test_pinned_route_says_so_in_words(tmp_path):
    home, run_dir, db = _home(tmp_path)
    _lane(run_dir, "lens", "glm", "glm")
    _trace(db, "r1", "lens", route_source="pinned")
    card = why.collect("r1", "lens", home=str(home), db=db)
    assert "pinned by the recipe" in why.render(card)


def test_unknown_run_is_exit_2_and_says_where_it_looked(tmp_path):
    home, _, _ = _home(tmp_path)
    rc, out = _run("nope", "lens", "--home", str(home))
    assert rc == 2
    assert "no run directory for 'nope'" in out


def test_known_run_with_unknown_node_is_exit_1_not_a_crash(tmp_path):
    home, _, db = _home(tmp_path)
    rc, out = _run("r1", "ghost", "--home", str(home), "--db", db)
    assert rc == 1
    # lane, route, walked, memory — each field line, not the notes that repeat it
    assert [line for line in out.splitlines() if line.endswith(": not recorded")] == [
        "lane: not recorded", "route: not recorded",
        "walked: not recorded", "memory: not recorded"]
    assert "no execution_traces row for node 'ghost'" in out


def test_a_missing_database_is_not_created(tmp_path):
    home, run_dir, db = _home(tmp_path)
    _lane(run_dir, "lens", "glm", "glm")
    (home / "state.db").unlink()
    card = why.collect("r1", "lens", home=str(home), db=db)
    assert not (home / "state.db").exists()
    assert any("no database at" in note for note in card["notes"])
    assert card["lane"]["served"] == "glm"     # the file store still answers


def test_role_shaped_actor_is_found_by_feature_name(tmp_path):
    """`synthesizer` logs its calls under actor `reviewer`."""
    home, run_dir, db = _home(tmp_path)
    _learned(run_dir, "synthesizer", node_type="reviewer")
    _call(db, "r1", "reviewer", "minimax", feature_name="mini-ork:synthesizer")
    _call(db, "r1", "reviewer", "opus", feature_name="mini-ork:rubric")
    card = why.collect("r1", "synthesizer", home=str(home), db=db)
    assert [c["lane"] for c in card["walked"]] == ["minimax"]


def test_a_payloadless_trace_row_is_matched_by_trace_id(tmp_path):
    home, _run_dir_, db = _home(tmp_path)
    _trace(db, "r1", "lens", verifier_output="{}")
    card = why.collect("r1", "lens", home=str(home), db=db)
    assert card["route"]["trace_id"] == "tr-researcher-lens-abc"


def test_json_mode_carries_every_section(tmp_path):
    import io
    from contextlib import redirect_stdout

    home, run_dir, db = _home(tmp_path)
    _lane(run_dir, "lens", "glm", "glm")
    _learned(run_dir, "lens")
    _trace(db, "r1", "lens")
    _call(db, "r1", "lens", "glm")
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = why.main(["r1", "lens", "--home", str(home), "--db", db, "--json"],
                      root=str(REPO))
    assert rc == 0
    doc = json.loads(buf.getvalue())
    assert set(doc) == {"run", "node", "run_dir", "node_type", "lane", "affinity",
                        "route", "memory", "walked", "outcomes", "notes"}
    # A store without 0092 contributes no outcomes — and no fabricated zero.
    assert doc["outcomes"] == {}


def test_no_arguments_prints_usage_and_exits_2(tmp_path):
    rc, out = _run()
    assert rc == 2
    assert "mini-ork why" in out
