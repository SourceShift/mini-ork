"""Smoke test for the observability HTTP surface.

Exercises the route handlers directly (no httpx dep) to assert each endpoint
returns sensible shapes when pointed at the repo's own .mini-ork/state.db.
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def home() -> Path:
    h = ROOT / ".mini-ork"
    if not (h / "state.db").exists():
        pytest.skip(f"no state.db at {h}; run `mini-ork init` first")
    return h


@pytest.fixture(scope="module")
def db(home: Path):
    from mini_ork.web.deps import set_home_override, get_db

    set_home_override(home)
    return get_db()


def test_health(db) -> None:
    from mini_ork.web.routes.fleet import health

    out = health(db)
    assert out["ok"] is True
    assert out["has_task_runs"] is True


def test_task_runs_summary(db) -> None:
    from mini_ork.web.routes.fleet import task_runs_summary

    out = task_runs_summary(db)
    assert "by_recipe" in out
    assert "by_status" in out
    assert "total_cost_usd" in out


def test_task_runs_list(db) -> None:
    from mini_ork.web.routes.fleet import list_task_runs

    rows = list_task_runs(db, limit=5)
    assert isinstance(rows, list)
    if rows:
        assert "id" in rows[0]
        assert "recipe" in rows[0]


def test_active_runs(db) -> None:
    from mini_ork.web.routes.fleet import active_runs

    rows = active_runs(db)
    assert isinstance(rows, list)


def test_self_improve(db) -> None:
    from mini_ork.web.routes.trajectory import self_improve_runs

    rows = self_improve_runs(db, limit=3)
    assert isinstance(rows, list)


def test_cost_by_day(db) -> None:
    from mini_ork.web.routes.trajectory import cost_by_day

    rows = cost_by_day(db)
    assert isinstance(rows, list)


def test_fingerprint_recursive_self_improve() -> None:
    from mini_ork.web.routes.fingerprint import fingerprint

    out = fingerprint(recipe="recursive-self-improve")
    assert out["recipe"] == "recursive-self-improve"
    assert out["nodes"], "recipe should have nodes"
    # The framework's load-bearing claim: this recipe must be heterogeneous.
    assert out["coalition"] in ("heterogeneous", "low"), (
        f"recursive-self-improve regressed to {out['coalition']} "
        f"(families: {out['families_used']})"
    )


def test_app_factory_boots(home: Path) -> None:
    from mini_ork.web.app import create_app

    app = create_app(home=home, dev_cors=False)
    paths = [r.path for r in app.routes]
    assert "/api/v1/health" in paths
    assert "/api/v1/task-runs" in paths
    assert "/api/v1/fingerprint" in paths


def test_self_improve_detail(db) -> None:
    """Detail endpoint returns parsed notes + linked task_run + sibling context."""
    from mini_ork.web.routes.trajectory import self_improve_detail, self_improve_runs

    rows = self_improve_runs(db, limit=1)
    if not rows:
        pytest.skip("no self_improve_runs to detail")
    rid = rows[0]["run_id"]
    out = self_improve_detail(run_id=rid, db=db)
    assert out["run_id"] == rid
    assert "parsed_notes" in out
    assert isinstance(out["parsed_notes"], list)
    assert "siblings" in out
    # Every parsed note should have key/value/kind
    for n in out["parsed_notes"]:
        assert {"key", "value", "kind"}.issubset(n.keys())
        assert n["kind"] in ("flag", "kv", "sha")


def test_agents_endpoint_enumerates_recipe_nodes(db) -> None:
    """The /agents endpoint must surface every recipe node as a dispatched agent."""
    from mini_ork.web.routes.run_detail import list_agents
    from mini_ork.web.routes.fleet import list_task_runs
    from mini_ork.web.deps import get_home

    runs = [r for r in list_task_runs(db, limit=10) if r.get("recipe") == "recursive-self-improve"]
    if not runs:
        pytest.skip("no recursive-self-improve task_runs")
    home = get_home()
    out = list_agents(task_run_id=runs[0]["id"], db=db, home=home)
    assert out["recipe"] == "recursive-self-improve"
    names = {a["node_id"] for a in out["agents"]}
    # The recursive-self-improve recipe must have these load-bearing nodes
    assert {"bottleneck_lens", "opus_synthesizer", "self_tests_pass"} <= names


def test_agent_detail_loads_prompt(db) -> None:
    """Agent detail must resolve prompt_ref → recipes/<name>/<file>.md content."""
    from mini_ork.web.routes.run_detail import agent_detail
    from mini_ork.web.routes.fleet import list_task_runs
    from mini_ork.web.deps import get_home

    runs = [r for r in list_task_runs(db, limit=10) if r.get("recipe") == "recursive-self-improve"]
    if not runs:
        pytest.skip("no recursive-self-improve task_runs")
    home = get_home()
    out = agent_detail(
        task_run_id=runs[0]["id"],
        node_id="opus_synthesizer",
        db=db,
        home=home,
    )
    assert out["node"]["name"] == "opus_synthesizer"
    assert out["prompt"]["path"] is not None
    assert out["prompt"]["content"], "prompt content should load from recipes/<name>/prompts/<file>"
    assert "llm_calls" in out
    assert "artifacts" in out


def test_load_transcript_prefers_stable_agent_sidecar(tmp_path: Path) -> None:
    """Agent transcript lookup must work when llm_dispatch used a temp stdout file."""
    from mini_ork.web.agents import load_transcript

    home = tmp_path / ".mini-ork"
    run_dir = home / "runs" / "run-test"
    run_dir.mkdir(parents=True)
    (run_dir / "agent-tiny_researcher.transcript.json").write_text(
        (
            '{"turns":[{"turn_index":0,"model":"codex",'
            '"input_tokens":0,"output_tokens":0,"text":"visible",'
            '"tool_uses":[],"stop_reason":null,"session_id":null}],'
            '"fallback":"text-output"}'
        ),
        encoding="utf-8",
    )

    out = load_transcript(home, "run-test", "tiny_researcher")
    assert out["available"] is True
    assert out["transcript_path"] == "runs/run-test/agent-tiny_researcher.transcript.json"
    assert out["turns"][0]["text"] == "visible"
    assert out["fallback"] == "text-output"


def test_load_transcript_falls_back_to_output_artifact(tmp_path: Path) -> None:
    """Legacy runs without sidecars should still show the agent's visible output."""
    from mini_ork.web.agents import load_transcript

    home = tmp_path / ".mini-ork"
    run_dir = home / "runs" / "run-legacy"
    run_dir.mkdir(parents=True)
    (run_dir / "context-tiny_researcher.json").write_text('{"summary":"legacy"}', encoding="utf-8")

    out = load_transcript(home, "run-legacy", "tiny_researcher")
    assert out["available"] is True
    assert out["fallback"] == "text-output"
    assert out["transcript_path"] == "runs/run-legacy/context-tiny_researcher.json"
    assert "legacy" in out["turns"][0]["text"]


def test_run_inputs_endpoint_lists_and_reads_context(db) -> None:
    """Run inputs are source context, separate from output artifacts."""
    from mini_ork.web.routes.fleet import list_task_runs
    from mini_ork.web.routes.run_detail import list_inputs, read_input
    from mini_ork.web.deps import get_home

    runs = [r for r in list_task_runs(db, limit=20) if r.get("kickoff_path")]
    if not runs:
        pytest.skip("no task_runs with kickoff_path")

    home = get_home()
    task_run_id = runs[0]["id"]
    inputs = list_inputs(task_run_id=task_run_id, db=db, home=home)
    assert any(i["key"] == "kickoff" for i in inputs)

    kickoff = read_input(task_run_id=task_run_id, input_key="kickoff", db=db, home=home)
    assert kickoff["content"]
    assert kickoff["kind"] == "markdown"


def test_run_learning_endpoint_exposes_memory_and_injection(db) -> None:
    """Run detail must expose persisted learning plus injection provenance."""
    from mini_ork.web.routes.fleet import list_task_runs
    from mini_ork.web.routes.run_detail import get_learning

    runs = list_task_runs(db, limit=5)
    if not runs:
        pytest.skip("no task_runs")

    out = get_learning(task_run_id=runs[0]["id"], db=db)
    assert out["task_run_id"] == runs[0]["id"]
    assert "summary" in out
    assert "produced" in out
    assert "self_improve" in out
    assert "injected_candidates" in out
    assert "injection_points" in out["injected_candidates"]
    for row in out["produced"]["gradients"]:
        assert "agent_attribution" in row


def test_summary_endpoint_uses_cache(db) -> None:
    """Two summary calls within TTL must return the same object (cache hit)."""
    from mini_ork.web.routes.fleet import task_runs_summary

    db._result_cache.clear()
    a = task_runs_summary(db)
    b = task_runs_summary(db)
    assert a is b, "second call within TTL should return the cached object"


def test_correlation_reports_bridge_methods(db) -> None:
    """Correlation endpoint must enumerate available bridge methods + warn on gaps."""
    from mini_ork.web.routes.run_detail import get_correlation
    from mini_ork.web.routes.fleet import list_task_runs

    runs = list_task_runs(db, limit=1)
    if not runs:
        pytest.skip("no task_runs to correlate")
    out = get_correlation(task_run_id=runs[0]["id"], db=db)
    assert "bridge_methods" in out
    assert "run_events.run_id" in out["bridge_methods"], (
        "run_events.run_id should always be listed — it's the deterministic bridge for "
        "node lifecycle events emitted by bin/mini-ork-execute"
    )
    # If trace_id is set (post-fix or backfill), strict methods must be available
    if out["trace_id"]:
        assert "mo_events.trace_id" in out["bridge_methods"]
        assert "llm_calls.traceparent" in out["bridge_methods"]


def test_events_carry_bridge_attribution(db) -> None:
    """Each event row must declare via which bridge it was matched."""
    from mini_ork.web.routes.run_detail import get_events
    from mini_ork.web.routes.fleet import list_task_runs

    runs = list_task_runs(db, limit=5)
    if not runs:
        pytest.skip("no task_runs")
    for r in runs:
        evs = get_events(task_run_id=r["id"], db=db)
        for e in evs:
            assert "bridge" in e, f"event missing bridge attribution: {e}"
            assert e["bridge"] in ("trace_id", "run_id", "time-window")


def test_dag_carries_node_status(db) -> None:
    """DAG endpoint must merge node_start/node_end events into per-node status.

    Looks for any task_run with node events; skips when none exist (typical
    of fresh checkouts before any runs have completed).
    """
    from mini_ork.web.routes.run_detail import get_dag

    rows = db.rows(
        """
        SELECT DISTINCT t.id
        FROM task_runs t
        JOIN run_events e ON e.run_id = t.id
        WHERE e.event_type IN ('node_start', 'node_end') AND t.recipe IS NOT NULL
        LIMIT 1
        """
    )
    if not rows:
        pytest.skip("no task_runs with node events yet — re-run after a real dispatch")
    task_run_id = rows[0]["id"]
    out = get_dag(task_run_id=task_run_id, db=db)
    statuses = {n["name"]: n["status"] for n in out["nodes"]}
    assert any(s in ("running", "done", "failed") for s in statuses.values()), (
        f"expected at least one observed node, got: {statuses}"
    )
