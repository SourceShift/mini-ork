"""Context v2 wiring: planner (plan._inject_context), nodes (execute._learned_block),
and `mini-ork metrics context`."""
from __future__ import annotations

import io
import json
import sqlite3
from pathlib import Path

import pytest

from mini_ork import context_assembler, context_v2
from mini_ork.cli import metrics_context
from mini_ork.cli import plan as plan_mod

KICKOFF = """# Add the code tab

## Files in scope (touch ONLY these)

- `mini_ork/ide_pages/learn/code.py`

Do NOT modify any other file.

## Verification command

```bash
python3.11 -m pytest -q tests/unit/test_code_tab.py
```
"""
BASE = "PLANNER PROMPT"


def _db(path: Path, findings=(), harvested=()) -> str:
    con = sqlite3.connect(path)
    con.execute("""CREATE TABLE code_findings (
        id INTEGER PRIMARY KEY, fingerprint TEXT, run_id TEXT, source TEXT, file TEXT,
        line INTEGER, severity TEXT, category TEXT, issue TEXT, snippet TEXT,
        verdict TEXT, ts TEXT)""")
    con.execute("CREATE TABLE task_runs (id TEXT PRIMARY KEY, status TEXT, cost_usd REAL,"
                " created_at INTEGER, kickoff_path TEXT)")
    con.execute("CREATE TABLE code_findings_runs (run_id TEXT PRIMARY KEY,"
                " harvested_at INTEGER NOT NULL, n INTEGER NOT NULL)")
    for i, (run_id, file, severity, issue) in enumerate(findings, 1):
        con.execute("INSERT INTO code_findings (id, fingerprint, run_id, source, file, line,"
                    " severity, category, issue, snippet, verdict, ts)"
                    " VALUES (?, ?, ?, 'review', ?, 1, ?, 'other', ?, '', 'fail', ?)",
                    (i, f"fp{i}", run_id, file, severity, issue, f"2026-10-0{i}"))
    for run_id in harvested:
        con.execute("INSERT INTO code_findings_runs VALUES (?, 1, 1)", (run_id,))
    con.commit()
    con.close()
    return str(path)


PRIOR_FINDING = ("old", "mini_ork/ide_pages/learn/overview.py", "high",
                 "the section lists items per kind instead of newest first as the kickoff asks")


@pytest.fixture
def setup(tmp_path, monkeypatch):
    db = _db(tmp_path / "state.db", findings=[PRIOR_FINDING])
    kickoff = tmp_path / "eng-code-tab.md"
    kickoff.write_text(KICKOFF, encoding="utf-8")
    monkeypatch.setenv("MINI_ORK_DB", db)
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("MINI_ORK_RUN_ID", "run-x")
    monkeypatch.setenv("MO_USE_ROLE_PACKS", "0")
    monkeypatch.setenv("MO_INJECT_LEARNINGS", "1")
    # These arm tests assert the v1 prompt, which historically carried the
    # shared-session blocks and the raw-gradient graph block. The 2026-10-07
    # planner-context cleanup made both opt-in, so opt in here; the new default
    # is covered by tests/unit/test_planner_context_cleanup.py.
    monkeypatch.setenv("MO_PLANNER_SHARED_CONTEXT", "1")
    monkeypatch.setenv("MO_INJECT_UNVERIFIED", "1")
    monkeypatch.setattr(context_assembler, "failure_modes_md", lambda *a, **k: "V1-FM")
    monkeypatch.setattr(context_assembler, "prior_runs_md", lambda *a, **k: "V1-PRIOR")
    monkeypatch.setattr(context_assembler, "graph_context_md", lambda *a, **k: "V1-GRAPH")
    monkeypatch.setattr(context_assembler, "context_assemble", lambda *a, **k: {"v1": True})
    monkeypatch.setattr(plan_mod, "_contextnest_atoms_md", lambda *a, **k: "")
    monkeypatch.setattr(plan_mod, "_contextnest_recent_sessions_md", lambda *a, **k: "CN-RECENT")
    import mini_ork.orchestration.active_state_index as asi
    monkeypatch.setattr(asi, "render_active_state_block", lambda *a, **k: "ACTIVE")
    return tmp_path, db, str(kickoff)


def _plan(tmp_path, kickoff, db, name, *, dry_run=False):
    run_dir = tmp_path / name
    run_dir.mkdir()
    out = plan_mod._inject_context(BASE, kickoff, "framework_edit", db,
                                   str(run_dir / "plan.json"), dry_run)
    return out, run_dir


V1_PROMPT = BASE + "".join(f"\n\n{b}\n" for b in
                           ("V1-FM", "V1-PRIOR", "V1-GRAPH", "CN-RECENT", "ACTIVE"))


# ── planner ─────────────────────────────────────────────────────────────────

def test_off_keeps_the_v1_prompt_and_records_the_planner_ledger(setup, monkeypatch):
    tmp_path, db, kickoff = setup
    monkeypatch.setenv("MO_CONTEXT_V2", "off")
    out, run_dir = _plan(tmp_path, kickoff, db, "off")
    assert out == V1_PROMPT
    assert not (run_dir / context_v2.PACK_FILENAME).exists()
    rec = json.loads((run_dir / "learned" / "planner.json").read_text())
    assert [s["kind"] for s in rec["sources"]] == [
        "failure_modes", "prior_runs", "graph_context", "contextnest_recent", "active_state"]
    assert rec["context_v2"]["arm"] == "off"
    assert "V1-FM" in (run_dir / "learned" / "planner.md").read_text()


def test_shadow_builds_the_pack_but_the_prompt_is_byte_identical(setup, monkeypatch):
    tmp_path, db, kickoff = setup
    monkeypatch.setenv("MO_CONTEXT_V2", "shadow")
    out, run_dir = _plan(tmp_path, kickoff, db, "shadow")
    assert out == V1_PROMPT
    pack = context_v2.load_pack(str(run_dir))
    assert pack["arm"] == "shadow" and pack["file_findings"]
    rec = json.loads((run_dir / "learned" / "planner.json").read_text())
    assert rec["context_v2"] == {"mode": "shadow", "arm": "shadow", "injected": False,
                                 "item_ids": []}


def test_on_replaces_only_the_task_class_blocks(setup, monkeypatch):
    tmp_path, db, kickoff = setup
    monkeypatch.setenv("MO_CONTEXT_V2", "on")
    monkeypatch.setenv("MO_CONTEXT_V2_HOLDOUT", "0")
    out, run_dir = _plan(tmp_path, kickoff, db, "on")
    for v1 in ("V1-FM", "V1-PRIOR", "V1-GRAPH"):
        assert v1 not in out
    assert "CN-RECENT" in out and "ACTIVE" in out
    assert "[c:0] Do NOT modify any other file." in out
    assert "newest first" in out and "context_used" in out
    rec = json.loads((run_dir / "learned" / "planner.json").read_text())
    assert rec["context_v2"]["arm"] == "v2" and rec["context_v2"]["injected"] is True
    assert "c:0" in rec["context_v2"]["item_ids"]
    assert {"kind": "context_v2", "id": "c:0"} in rec["sources"]
    assert "[c:0]" in (run_dir / "learned" / "planner.md").read_text()


def test_on_but_held_out_keeps_the_v1_prompt(setup, monkeypatch):
    tmp_path, db, kickoff = setup
    monkeypatch.setenv("MO_CONTEXT_V2", "on")
    monkeypatch.setenv("MO_CONTEXT_V2_HOLDOUT", "1")
    out, run_dir = _plan(tmp_path, kickoff, db, "hold")
    assert out == V1_PROMPT
    assert context_v2.load_pack(str(run_dir))["arm"] == "holdout"


def test_dry_run_writes_nothing(setup, monkeypatch):
    tmp_path, db, kickoff = setup
    monkeypatch.setenv("MO_CONTEXT_V2", "on")
    monkeypatch.setenv("MO_CONTEXT_V2_HOLDOUT", "0")
    out, run_dir = _plan(tmp_path, kickoff, db, "dry", dry_run=True)
    assert out == V1_PROMPT
    assert not any(run_dir.iterdir())


# ── nodes ───────────────────────────────────────────────────────────────────

@pytest.fixture
def node_env(setup, monkeypatch):
    tmp_path, db, kickoff = setup
    run_dir = tmp_path / "noderun"
    run_dir.mkdir()
    (run_dir / "run_profile.json").write_text(json.dumps({"kickoff_path": kickoff}))
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(run_dir))
    monkeypatch.setattr(context_assembler, "failure_modes_md", lambda *a, **k: "")
    from mini_ork.steering import operator_steering
    monkeypatch.setattr(operator_steering, "fetch_for", lambda *a, **k: [])
    return run_dir


def _learned(node_type, sources=None):
    from mini_ork.cli.execute import _learned_block
    return _learned_block("/root", "framework_edit", node_type, "lane", node_type,
                          sources=sources)


def test_node_block_is_injected_in_the_v2_arm_with_sources(node_env, monkeypatch):
    monkeypatch.setenv("MO_CONTEXT_V2", "on")
    monkeypatch.setenv("MO_CONTEXT_V2_HOLDOUT", "0")
    sources: list[dict] = []
    text = _learned("implementer", sources)
    assert "[c:0] Do NOT modify any other file." in text
    assert "re-check your change" in text
    assert {"kind": "context_v2", "id": "c:0"} in sources
    assert _learned("implementer") == text  # byte-identical with sources=None
    assert "name the id" in _learned("reviewer")
    # the node built and wrote the pack itself (no planner pack existed)
    assert (node_env / context_v2.PACK_FILENAME).exists()


@pytest.mark.parametrize("mode,holdout", [("shadow", "0"), ("off", "0"), ("on", "1")])
def test_node_block_is_absent_outside_the_v2_arm(node_env, monkeypatch, mode, holdout):
    monkeypatch.setenv("MO_CONTEXT_V2", mode)
    monkeypatch.setenv("MO_CONTEXT_V2_HOLDOUT", holdout)
    sources: list[dict] = []
    assert "[c:0]" not in _learned("implementer", sources)
    assert not any(s.get("kind") == "context_v2" for s in sources)


# ── metrics context ─────────────────────────────────────────────────────────

def _run(runs: Path, name: str, pack: dict, *, arm: str, plan=None, learned=None, diff=""):
    d = runs / name
    (d / "learned").mkdir(parents=True)
    context_v2.write_json(str(d / context_v2.PACK_FILENAME), {**pack, "arm": arm, "run_id": name})
    if plan is not None:
        (d / "plan.json").write_text(json.dumps(plan))
    for node, rec in (learned or {}).items():
        (d / "learned" / f"{node}.json").write_text(json.dumps(rec))
    if diff:
        (d / "review-diff.patch").write_text(diff)


def test_metrics_context_reports_delivery_ack_scope_and_recurrence(tmp_path, monkeypatch):
    db = _db(tmp_path / "state.db",
             findings=[PRIOR_FINDING,
                       ("run-a", "mini_ork/ide_pages/learn/code.py", "high",
                        "again lists items per kind instead of newest first as the kickoff asks"),
                       ("run-b", "mini_ork/ide_pages/learn/code.py", "medium",
                        "the cache key ignores the file mtime")],
             harvested=["run-a", "run-b"])
    kickoff = tmp_path / "eng-code-tab.md"
    kickoff.write_text(KICKOFF, encoding="utf-8")
    # The pack is built at planning time, before run-a/run-b were reviewed.
    plan_time_db = _db(tmp_path / "plan-time.db", findings=[PRIOR_FINDING])
    pack = context_v2.build(str(kickoff), db=plan_time_db, run_id="planning")
    assert len(pack["file_findings"]) == 1, "fixture must select one recurring problem"
    runs = tmp_path / "runs"
    diff = ("diff --git a/mini_ork/ide_pages/learn/code.py b/mini_ork/ide_pages/learn/code.py\n"
            "diff --git a/mini_ork/web/app.py b/mini_ork/web/app.py\n")
    _run(runs, "run-a", pack, arm="v2", plan={"context_used": ["c:0", "f:made-up"]},
         learned={"planner": {"context_v2": {"injected": True}},
                  "implementer": {"sources": [{"kind": "context_v2", "id": "c:0"}]},
                  "reviewer": {"sources": []}},
         diff=diff)
    _run(runs, "run-b", pack, arm="holdout", plan={})
    _run(runs, "run-c", pack, arm="v2", plan={})  # never harvested: no recurrence verdict
    (runs / "no-pack").mkdir()

    records = metrics_context.collect(runs, db)
    assert [r["run_id"] for r in records] == ["run-a", "run-b", "run-c"]
    result = metrics_context.summarize(records)
    v2, hold = result["arms"]["v2"], result["arms"]["holdout"]
    assert v2["runs"] == 2 and v2["harvested_runs"] == 1
    assert v2["planner_v2_rate"] == 0.5 and v2["node_v2_rate"] == 0.5
    assert v2["ack_rate"] == 0.5 and v2["invalid_ids"] == 1
    assert v2["outside_scope_rate"] == 1.0
    assert v2["recurrence_rate"] == 1.0 and hold["recurrence_rate"] == 0.0
    assert result["lift"]["status"] == "insufficient"
    monkeypatch.setattr(metrics_context, "MIN_N", 1)
    lift = metrics_context.summarize(records)["lift"]
    assert lift == {"value": -1.0, "status": "measured", "n_v2": 1, "n_holdout": 1}


def test_metrics_context_cli_json_and_empty_state(tmp_path):
    db = _db(tmp_path / "state.db")
    (tmp_path / "runs").mkdir()
    buf = io.StringIO()
    assert metrics_context.main(["--db", db, "--json"], stdout=buf) == 0
    assert json.loads(buf.getvalue())["arms"] == {}
    buf = io.StringIO()
    metrics_context.main(["--db", db], stdout=buf)
    assert "No run has a context-pack.v2.json yet" in buf.getvalue()


def test_metrics_dispatches_the_context_view(tmp_path):
    from mini_ork.cli import metrics
    db = _db(tmp_path / "state.db")
    buf = io.StringIO()
    assert metrics.main(["context", "--db", db, "--json"], stdout=buf, stderr=io.StringIO()) == 0
    assert "lift" in json.loads(buf.getvalue())
