"""Smoke test for the observability HTTP surface.

Exercises the route handlers directly (no httpx dep) to assert each endpoint
returns sensible shapes when pointed at the repo's own .mini-ork/state.db.
"""

from __future__ import annotations

import sqlite3
import subprocess
import json
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


def test_recovery_ui_route_returns_projection_shape(db) -> None:
    # E5: the recovery projection endpoint wires + always returns a renderable
    # dict (empty nodes for an unknown run), reading the E1–E3 tables.
    from mini_ork.web.routes.recovery import recovery_view

    out = recovery_view("no-such-run-e5", db)
    assert set(out.keys()) >= {"run_id", "nodes", "active_recovery", "lease", "next_action"}
    assert out["run_id"] == "no-such-run-e5"
    assert isinstance(out["nodes"], list)


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


def test_active_runs_includes_unfinished_task_runs(tmp_path: Path) -> None:
    """Universal task-loop runs are active even without legacy heartbeat rows."""
    import sqlite3
    import time

    from mini_ork.web.db import StateDB
    from mini_ork.web.routes.fleet import active_runs

    db_path = tmp_path / "state.db"
    now = int(time.time())
    con = sqlite3.connect(db_path)
    con.execute(
        """
        CREATE TABLE task_runs (
          id TEXT PRIMARY KEY,
          parent_epic_id TEXT,
          task_class TEXT NOT NULL,
          recipe TEXT,
          status TEXT NOT NULL,
          verdict TEXT,
          cost_usd REAL NOT NULL DEFAULT 0,
          created_at INTEGER NOT NULL,
          updated_at INTEGER NOT NULL,
          ended_at INTEGER
        )
        """
    )
    con.execute(
        """
        INSERT INTO task_runs (
          id, parent_epic_id, task_class, recipe, status, verdict,
          cost_usd, created_at, updated_at, ended_at
        )
        VALUES ('run-live', NULL, 'code_fix', 'code-fix', 'executing', NULL, 0.25, ?, ?, NULL)
        """,
        (now - 5, now),
    )
    con.execute(
        """
        INSERT INTO task_runs (
          id, parent_epic_id, task_class, recipe, status, verdict,
          cost_usd, created_at, updated_at, ended_at
        )
        VALUES ('run-classified', NULL, 'code_fix', 'code-fix', 'classified', NULL, 0.25, ?, ?, NULL)
        """,
        (now - 5, now),
    )
    con.execute(
        """
        INSERT INTO task_runs (
          id, parent_epic_id, task_class, recipe, status, verdict,
          cost_usd, created_at, updated_at, ended_at
        )
        VALUES ('run-stale', NULL, 'code_fix', 'code-fix', 'executing', NULL, 0.25, 10, 20, NULL)
        """
    )
    con.execute(
        """
        INSERT INTO task_runs (
          id, parent_epic_id, task_class, recipe, status, verdict,
          cost_usd, created_at, updated_at, ended_at
        )
        VALUES ('run-done', NULL, 'code_fix', 'code-fix', 'published', 'APPROVE', 0.50, 1, 2, 3)
        """
    )
    con.commit()
    con.close()

    rows = active_runs(StateDB(db_path))

    assert [r["id"] for r in rows] == ["run-live"]
    assert rows[0]["source"] == "task_runs"
    assert rows[0]["task_run_id"] == "run-live"
    assert rows[0]["test_status"] == "executing"


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

    out = fingerprint(recipe="recursive-self-improve", home=None)
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

    # Use the OpenAPI schema as the source of truth for registered
    # paths. FastAPI 0.10x flattens app.include_router() routes inline
    # so each one has .path; FastAPI 0.111+ wraps them as Mount(s)
    # whose sub-routes live under mount.app.routes, with the mount
    # prefix only applied at OpenAPI emission time. Iterating
    # app.routes directly misses the prefix-concat step on the new
    # FastAPI; reading openapi().paths handles both shapes uniformly.
    paths = set(app.openapi().get("paths", {}).keys())
    assert "/api/v1/health" in paths
    assert "/api/v1/task-runs" in paths
    assert "/api/v1/fingerprint" in paths
    # Idea tree endpoints (plan: docs/plans/2026-06-11-arbor-techniques-into-mini-ork.md item 1)
    assert "/api/v1/idea-tree/roots" in paths
    assert "/api/v1/idea-tree/{root_node_id}" in paths


def test_idea_tree_roots_returns_backfilled_sessions(db) -> None:
    """list_roots() must surface every root node with subtree counts.

    Relies on the migration + backfill having run. If state.db has no
    idea_tree_nodes table at all, skip — the test only covers the
    post-backfill happy path. Real production runs should always have
    at least the synthetic roots from scripts/backfill_idea_tree.py.
    """
    from mini_ork.web.idea_tree import list_roots

    if not db.has_table("idea_tree_nodes"):
        pytest.skip("idea_tree_nodes table missing — apply migration 0020")
    roots = list_roots(db)
    if not roots:
        pytest.skip("no idea_tree_nodes rows — run scripts/backfill_idea_tree.py")
    for r in roots:
        # Required fields the UI Trajectory page renders.
        assert {"node_id", "status", "node_count"}.issubset(r.keys())
        # node_count includes the root itself, so >= 1 for any non-empty tree.
        assert r["node_count"] >= 1


def test_idea_tree_read_tree_includes_depth_and_edges(db) -> None:
    """read_tree() must emit nodes with depth + matching edges list.

    Depth-first invariant: every non-root edge has a from-id that
    appears in nodes with depth strictly less than the to-id's depth.
    """
    from mini_ork.web.idea_tree import list_roots, read_tree

    if not db.has_table("idea_tree_nodes"):
        pytest.skip("idea_tree_nodes table missing")
    roots = list_roots(db)
    if not roots:
        pytest.skip("no roots to read")
    tree = read_tree(db, roots[0]["node_id"])
    assert tree["root_node_id"] == roots[0]["node_id"]
    assert tree["nodes"], "root should have itself + descendants"
    # Root node's depth must be 0.
    root_in_tree = next(n for n in tree["nodes"] if n["node_id"] == roots[0]["node_id"])
    assert root_in_tree["depth"] == 0
    # Edges must reference declared node ids.
    node_ids = {n["node_id"] for n in tree["nodes"]}
    for e in tree["edges"]:
        assert e["from"] in node_ids and e["to"] in node_ids
    # Stats sanity.
    assert tree["stats"]["total"] == len(tree["nodes"])
    assert tree["stats"]["max_depth"] >= 0


def test_idea_tree_walk_to_root_ends_at_root(db) -> None:
    """walk_to_root() must return a chain that ends at a parent-less node."""
    from mini_ork.web.idea_tree import list_roots, read_tree, walk_to_root

    if not db.has_table("idea_tree_nodes"):
        pytest.skip("idea_tree_nodes table missing")
    roots = list_roots(db)
    if not roots:
        pytest.skip("no roots")
    tree = read_tree(db, roots[0]["node_id"])
    # Pick the deepest leaf and walk up.
    leaves = [n for n in tree["nodes"] if n["depth"] == tree["stats"]["max_depth"]]
    if not leaves:
        pytest.skip("no leaves to walk")
    chain = walk_to_root(db, leaves[0]["node_id"])
    assert chain, "chain must be non-empty"
    # Last entry in the chain is the root (parent_node_id IS NULL).
    assert chain[-1]["parent_node_id"] is None
    # First entry is the leaf we started from.
    assert chain[0]["node_id"] == leaves[0]["node_id"]


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


def test_load_transcript_strips_z_insight_blocks(tmp_path: Path) -> None:
    """Spawned CLIs inherit the operator's global CLAUDE.md and emit
    <z-insight> protocol blocks into deliverables (run-1781095892-69202).
    Render-time strip covers transcripts persisted before the engine fix."""
    import json as _json

    from mini_ork.web.agents import load_transcript

    home = tmp_path / ".mini-ork"
    run_dir = home / "runs" / "run-polluted"
    run_dir.mkdir(parents=True)
    polluted = '{"ok":true}\n<z-insight>\n{"leak":1}\n</z-insight>'
    (run_dir / "agent-implementer.transcript.json").write_text(
        _json.dumps({"turns": [{"turn_index": 0, "text": polluted}]}),
        encoding="utf-8",
    )

    out = load_transcript(home, "run-polluted", "implementer")
    assert "<z-insight>" not in out["turns"][0]["text"]
    assert out["turns"][0]["text"] == '{"ok":true}'


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


def test_dag_carries_node_status(db, home) -> None:
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
    out = get_dag(task_run_id=task_run_id, db=db, home=home)
    statuses = {n["name"]: n["status"] for n in out["nodes"]}
    assert any(s in ("running", "done", "failed") for s in statuses.values()), (
        f"expected at least one observed node, got: {statuses}"
    )


def test_error_category_column_exists(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    con = sqlite3.connect(db_path)
    con.executescript(
        """
        CREATE TABLE schema_migrations(filename TEXT PRIMARY KEY, applied_at TEXT, checksum TEXT);
        CREATE TABLE llm_calls (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          provider TEXT NOT NULL,
          model_id TEXT NOT NULL,
          tier TEXT NOT NULL,
          feature_name TEXT NOT NULL,
          actor TEXT,
          status TEXT NOT NULL CHECK (status IN ('success','failed')),
          duration_ms INTEGER NOT NULL DEFAULT 0,
          cost_usd REAL NOT NULL DEFAULT 0,
          error_message TEXT,
          iter INTEGER,
          run_id TEXT,
          traceparent TEXT,
          input_tokens INTEGER NOT NULL DEFAULT 0,
          output_tokens INTEGER NOT NULL DEFAULT 0,
          total_tokens INTEGER NOT NULL DEFAULT 0,
          metadata_json TEXT NOT NULL DEFAULT '{}',
          session_id TEXT,
          ts TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
        );
        CREATE TABLE run_events (
          event_id TEXT PRIMARY KEY,
          run_id TEXT NOT NULL,
          event_type TEXT NOT NULL,
          payload_json TEXT NOT NULL DEFAULT '{}',
          created_at INTEGER NOT NULL DEFAULT (strftime('%s','now'))
        );
        """
    )
    con.executescript((ROOT / "db/migrations/0021_error_taxonomy_finish_reasons.sql").read_text())

    cols = {r[1] for r in con.execute("PRAGMA table_info(llm_calls)").fetchall()}
    assert {"error_category", "retryable"} <= cols
    con.execute(
        """
        INSERT INTO llm_calls (
          provider, model_id, tier, feature_name, status,
          error_category, retryable
        ) VALUES ('gateway', 'glm', 'default', 'mini-ork:test', 'failed', 'auth', 0)
        """
    )
    with pytest.raises(sqlite3.IntegrityError):
        con.execute(
            """
            INSERT INTO llm_calls (
              provider, model_id, tier, feature_name, status,
              error_category, retryable
            ) VALUES ('gateway', 'glm', 'default', 'mini-ork:test', 'failed', 'auth_failed', 0)
            """
        )
    con.close()


def test_finish_reason_column_exists(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    con = sqlite3.connect(db_path)
    con.executescript(
        """
        CREATE TABLE schema_migrations(filename TEXT PRIMARY KEY, applied_at TEXT, checksum TEXT);
        CREATE TABLE llm_calls (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          provider TEXT NOT NULL,
          model_id TEXT NOT NULL,
          tier TEXT NOT NULL,
          feature_name TEXT NOT NULL,
          status TEXT NOT NULL CHECK (status IN ('success','failed'))
        );
        CREATE TABLE run_events (
          event_id TEXT PRIMARY KEY,
          run_id TEXT NOT NULL,
          event_type TEXT NOT NULL,
          payload_json TEXT NOT NULL DEFAULT '{}',
          created_at INTEGER NOT NULL DEFAULT (strftime('%s','now'))
        );
        """
    )
    con.executescript((ROOT / "db/migrations/0021_error_taxonomy_finish_reasons.sql").read_text())

    cols = {r[1] for r in con.execute("PRAGMA table_info(run_events)").fetchall()}
    assert "finish_reason" in cols
    con.execute(
        """
        INSERT INTO run_events(event_id, run_id, event_type, payload_json, finish_reason)
        VALUES ('evt-ok', 'run-1', 'node_end', '{}', 'verdict_revise')
        """
    )
    with pytest.raises(sqlite3.IntegrityError):
        con.execute(
            """
            INSERT INTO run_events(event_id, run_id, event_type, payload_json, finish_reason)
            VALUES ('evt-bad', 'run-1', 'node_end', '{}', 'needs_revision')
            """
        )
    con.close()


def test_dispatch_config_snapshot_columns_exist(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    con = sqlite3.connect(db_path)
    con.executescript(
        """
        CREATE TABLE schema_migrations(filename TEXT PRIMARY KEY, applied_at TEXT, checksum TEXT);
        CREATE TABLE task_runs (
          id TEXT PRIMARY KEY,
          updated_at INTEGER NOT NULL
        );
        """
    )
    con.executescript((ROOT / "db/migrations/0022_dispatch_config_snapshot.sql").read_text())

    cols = {r[1] for r in con.execute("PRAGMA table_info(task_runs)").fetchall()}
    assert {"dispatch_config_json", "agents_yaml_sha"} <= cols
    indexes = {r[1] for r in con.execute("PRAGMA index_list(task_runs)").fetchall()}
    assert "idx_task_runs_agents_yaml_sha" in indexes
    con.close()


def test_node_heartbeat_fuse_columns_exist(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    con = sqlite3.connect(db_path)
    con.executescript(
        """
        CREATE TABLE schema_migrations(filename TEXT PRIMARY KEY, applied_at TEXT, checksum TEXT);
        CREATE TABLE run_events (
          event_id TEXT PRIMARY KEY,
          run_id TEXT NOT NULL,
          event_type TEXT NOT NULL,
          payload_json TEXT NOT NULL DEFAULT '{}',
          created_at INTEGER NOT NULL DEFAULT (strftime('%s','now'))
        );
        CREATE TABLE task_runs (
          id TEXT PRIMARY KEY,
          updated_at INTEGER NOT NULL
        );
        """
    )
    con.executescript((ROOT / "db/migrations/0023_node_heartbeat_fuse.sql").read_text())

    run_event_cols = {r[1]: r for r in con.execute("PRAGMA table_info(run_events)").fetchall()}
    task_run_cols = {r[1]: r for r in con.execute("PRAGMA table_info(task_runs)").fetchall()}
    assert "last_heartbeat_at" in run_event_cols
    assert run_event_cols["last_heartbeat_at"][3] == 0
    assert {"fuse_blown_lane", "fuse_consecutive_failures"} <= set(task_run_cols)
    assert task_run_cols["fuse_blown_lane"][3] == 0
    assert task_run_cols["fuse_consecutive_failures"][4] == "0"
    indexes = {r[1] for r in con.execute("PRAGMA index_list(run_events)").fetchall()}
    assert "idx_run_events_last_heartbeat_at" in indexes
    con.close()


def test_llm_calls_cache_columns_exist(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    con = sqlite3.connect(db_path)
    con.executescript(
        """
        CREATE TABLE schema_migrations(filename TEXT PRIMARY KEY, applied_at TEXT, checksum TEXT);
        CREATE TABLE llm_calls (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          provider TEXT NOT NULL,
          model_id TEXT NOT NULL,
          tier TEXT NOT NULL,
          feature_name TEXT NOT NULL,
          status TEXT NOT NULL CHECK (status IN ('success','failed'))
        );
        """
    )
    con.executescript((ROOT / "db/migrations/0024_cache_aware_cost.sql").read_text())

    cols = {r[1]: r for r in con.execute("PRAGMA table_info(llm_calls)").fetchall()}
    assert {
        "cached_input_tokens",
        "cache_creation_input_tokens",
        "cost_input_uncached_usd",
        "cost_input_cached_usd",
        "cost_cache_write_usd",
    } <= set(cols)
    assert cols["cached_input_tokens"][4] == "0"
    assert cols["cache_creation_input_tokens"][4] == "0"
    con.close()


def test_cache_cost_components_sum_to_cost_usd(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    con = sqlite3.connect(db_path)
    con.executescript(
        """
        CREATE TABLE schema_migrations(filename TEXT PRIMARY KEY, applied_at TEXT, checksum TEXT);
        CREATE TABLE llm_calls (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          provider TEXT NOT NULL,
          model_id TEXT NOT NULL,
          tier TEXT NOT NULL,
          feature_name TEXT NOT NULL,
          actor TEXT,
          status TEXT NOT NULL CHECK (status IN ('success','failed')),
          duration_ms INTEGER NOT NULL DEFAULT 0,
          cost_usd REAL NOT NULL DEFAULT 0,
          error_message TEXT,
          iter INTEGER,
          run_id TEXT,
          traceparent TEXT,
          input_tokens INTEGER NOT NULL DEFAULT 0,
          output_tokens INTEGER NOT NULL DEFAULT 0,
          total_tokens INTEGER NOT NULL DEFAULT 0,
          metadata_json TEXT NOT NULL DEFAULT '{}',
          session_id TEXT,
          ts TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
        );
        """
    )
    con.executescript((ROOT / "db/migrations/0024_cache_aware_cost.sql").read_text())
    con.close()

    script = (
        "source lib/llm-dispatch.sh; "
        f"MINI_ORK_DB={db_path} _mo_llm_write_llm_calls_row "
        "anthropic claude-opus-4 default mini-ork:test tester success 100 0.009 '' "
        "1000 25 '{}' 200 300"
    )
    result = subprocess.run(["bash", "-lc", script], cwd=ROOT)
    assert result.returncode == 0

    con = sqlite3.connect(db_path)
    row = con.execute(
        """
        SELECT cost_input_uncached_usd, cost_input_cached_usd, cost_cache_write_usd,
               cost_usd, cached_input_tokens, cache_creation_input_tokens
        FROM llm_calls
        """
    ).fetchone()
    con.close()

    assert row[4] == 200
    assert row[5] == 300
    expected_input_cost = (500 * 15.0 + 200 * 1.5 + 300 * 18.75) / 1_000_000
    component_sum = row[0] + row[1] + row[2]
    assert component_sum == pytest.approx(expected_input_cost)
    assert row[3] == pytest.approx(0.009)


def test_legacy_rows_with_zero_cache_still_query(tmp_path: Path) -> None:
    from mini_ork.web.db import StateDB
    from mini_ork.web.routes.run_detail import get_llm_calls

    db_path = tmp_path / "state.db"
    now = 1_800_000_000
    con = sqlite3.connect(db_path)
    con.executescript(
        """
        CREATE TABLE task_runs (
          id TEXT PRIMARY KEY,
          trace_id TEXT,
          created_at INTEGER NOT NULL,
          ended_at INTEGER
        );
        CREATE TABLE llm_calls (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          provider TEXT NOT NULL,
          model_id TEXT NOT NULL,
          tier TEXT NOT NULL,
          feature_name TEXT NOT NULL,
          actor TEXT,
          input_tokens INTEGER NOT NULL DEFAULT 0,
          output_tokens INTEGER NOT NULL DEFAULT 0,
          total_tokens INTEGER NOT NULL DEFAULT 0,
          cost_usd REAL NOT NULL DEFAULT 0,
          duration_ms INTEGER NOT NULL DEFAULT 0,
          status TEXT NOT NULL CHECK (status IN ('success','failed')),
          finish_reason TEXT,
          traceparent TEXT,
          ts TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
        );
        """
    )
    con.execute(
        "INSERT INTO task_runs(id, trace_id, created_at, ended_at) VALUES ('run-legacy', 'trace-legacy', ?, ?)",
        (now - 10, now + 10),
    )
    con.execute(
        """
        INSERT INTO llm_calls (
          provider, model_id, tier, feature_name, status,
          input_tokens, output_tokens, total_tokens, cost_usd, traceparent, ts
        ) VALUES (
          'anthropic', 'claude-opus-4', 'default', 'mini-ork:planner', 'success',
          100, 20, 120, 0.01, '00-trace-legacy-span-01', '2027-01-15T08:00:00.000Z'
        )
        """
    )
    con.commit()
    con.close()

    rows = get_llm_calls(task_run_id="run-legacy", db=StateDB(db_path))
    assert len(rows) == 1
    assert rows[0]["cached_input_tokens"] == 0


def test_lane_fuse_trips_after_three_retryable_failures(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    con = sqlite3.connect(db_path)
    con.executescript(
        """
        CREATE TABLE llm_calls (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          feature_name TEXT NOT NULL,
          status TEXT NOT NULL,
          error_category TEXT,
          retryable INTEGER
        );
        INSERT INTO llm_calls(feature_name, status, error_category, retryable)
        VALUES
          ('framework_edit:glm_lens', 'failed', 'network', 1),
          ('framework_edit:glm_lens', 'failed', 'network', 1),
          ('framework_edit:glm_lens', 'failed', 'network', 1);
        """
    )
    con.close()

    script = (
        "source lib/llm-dispatch.sh; "
        f"MINI_ORK_DB={db_path} MO_FUSE_ENABLED=1 "
        "_mo_check_lane_fuse glm_lens network >/dev/null"
    )
    result = subprocess.run(["bash", "-lc", script], cwd=ROOT)
    assert result.returncode == 1


def test_lane_fuse_ignores_nonretryable_failures(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    con = sqlite3.connect(db_path)
    con.executescript(
        """
        CREATE TABLE llm_calls (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          feature_name TEXT NOT NULL,
          status TEXT NOT NULL,
          error_category TEXT,
          retryable INTEGER
        );
        INSERT INTO llm_calls(feature_name, status, error_category, retryable)
        VALUES
          ('framework_edit:glm_lens', 'failed', 'auth', 0),
          ('framework_edit:glm_lens', 'failed', 'auth', 0),
          ('framework_edit:glm_lens', 'failed', 'auth', 0);
        """
    )
    con.close()

    script = (
        "source lib/llm-dispatch.sh; "
        f"MINI_ORK_DB={db_path} MO_FUSE_ENABLED=1 "
        "_mo_check_lane_fuse glm_lens auth >/dev/null"
    )
    result = subprocess.run(["bash", "-lc", script], cwd=ROOT)
    assert result.returncode == 0


def test_agents_endpoint_legacy_null_snapshot_uses_fallback(tmp_path: Path, monkeypatch) -> None:
    from mini_ork.web.db import StateDB
    from mini_ork.web.routes.run_detail import list_agents

    home = tmp_path / ".mini-ork"
    home.mkdir()
    db_path = home / "state.db"
    con = sqlite3.connect(db_path)
    con.execute(
        """
        CREATE TABLE task_runs (
          id TEXT PRIMARY KEY,
          recipe TEXT,
          trace_id TEXT,
          created_at INTEGER,
          ended_at INTEGER,
          status TEXT,
          cost_usd REAL,
          dispatch_config_json TEXT,
          agents_yaml_sha TEXT
        )
        """
    )
    con.execute(
        """
        INSERT INTO task_runs (
          id, recipe, created_at, ended_at, status, cost_usd,
          dispatch_config_json, agents_yaml_sha
        ) VALUES ('run-legacy', 'framework-edit', 1, 2, 'published', 0, NULL, NULL)
        """
    )
    con.commit()
    con.close()

    monkeypatch.setenv("MINI_ORK_ROOT", str(ROOT))
    out = list_agents(task_run_id="run-legacy", db=StateDB(db_path), home=home)
    code_lens = next(a for a in out["agents"] if a["node_id"] == "code_impact_lens")
    assert code_lens["model_lane"] == "kimi_lens"
    assert code_lens["family"] == "kimi"
    assert code_lens["model_id"] is None


def test_agents_endpoint_prefers_dispatch_config_snapshot(tmp_path: Path, monkeypatch) -> None:
    from mini_ork.web.db import StateDB
    from mini_ork.web.routes.run_detail import agent_detail, list_agents

    home = tmp_path / ".mini-ork"
    home.mkdir()
    db_path = home / "state.db"
    snapshot = {
        "kimi_lens": {
            "family": "historical-family",
            "model_id": "historical-model",
            "provider": "historical-provider",
            "base_url": "https://historical.example/v1",
        }
    }
    con = sqlite3.connect(db_path)
    con.execute(
        """
        CREATE TABLE task_runs (
          id TEXT PRIMARY KEY,
          recipe TEXT,
          trace_id TEXT,
          created_at INTEGER,
          ended_at INTEGER,
          status TEXT,
          cost_usd REAL,
          dispatch_config_json TEXT,
          agents_yaml_sha TEXT
        )
        """
    )
    con.execute(
        """
        INSERT INTO task_runs (
          id, recipe, created_at, ended_at, status, cost_usd,
          dispatch_config_json, agents_yaml_sha
        ) VALUES ('run-snapshot', 'framework-edit', 1, 2, 'published', 0, ?, 'sha')
        """,
        (json.dumps(snapshot),),
    )
    con.commit()
    con.close()

    monkeypatch.setenv("MINI_ORK_ROOT", str(ROOT))
    out = list_agents(task_run_id="run-snapshot", db=StateDB(db_path), home=home)
    code_lens = next(a for a in out["agents"] if a["node_id"] == "code_impact_lens")
    assert code_lens["family"] == "historical-family"
    assert code_lens["model_id"] == "historical-model"
    assert code_lens["provider"] == "historical-provider"
    assert code_lens["base_url"] == "https://historical.example/v1"

    detail = agent_detail(
        task_run_id="run-snapshot",
        node_id="code_impact_lens",
        db=StateDB(db_path),
        home=home,
    )
    assert detail["node"]["family"] == "historical-family"
    assert detail["node"]["model_id"] == "historical-model"


def test_llm_dispatch_classifies_invalid_api_key_as_auth() -> None:
    script = (
        "set -euo pipefail; "
        "MINI_ORK_ROOT=$PWD; "
        "source lib/llm-dispatch.sh; "
        "_mo_llm_classify_error 'HTTP 401 invalid api key' 1"
    )
    out = subprocess.run(
        ["bash", "--noprofile", "--norc", "-c", script],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    assert out.stdout.strip() == "auth"


def test_agents_yaml_has_capabilities_section() -> None:
    import yaml

    cfg = yaml.safe_load((ROOT / "config/agents.yaml").read_text()) or {}
    capabilities = cfg.get("capabilities") or {}
    expected = {"opus", "sonnet", "codex", "glm", "kimi", "deepseek", "minimax"}
    assert expected <= set(capabilities)
    for family in expected:
        assert {"vision", "tools", "reasoning", "search"} <= set(capabilities[family])


def test_capability_check_passes_when_family_supports_all() -> None:
    # kimi_lens is the canonical "supports vision + tools" lane after the
    # 2026-06-13 no-opus standing directive removed opus_lens from
    # .mini-ork/config/agents.yaml. Kimi exposes vision=true + tools=true
    # in config/agents.yaml's capabilities map, which is what the gate
    # asserts against. opus_lens used to play this role but no longer
    # resolves under the override; codex / glm / minimax all lack vision.
    script = (
        "set -euo pipefail; "
        "MINI_ORK_ROOT=$PWD; "
        "MINI_ORK_HOME=$PWD/.mini-ork; "
        "MO_LANE_REQUIRES_CAPABILITY='vision,tools'; "
        "source lib/lane-helpers.sh; "
        "mo_assert_lane_capability kimi_lens"
    )
    subprocess.run(
        ["bash", "--noprofile", "--norc", "-c", script],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )


def test_capability_check_fails_when_family_missing_one() -> None:
    script = (
        "set -euo pipefail; "
        "MINI_ORK_ROOT=$PWD; "
        "MINI_ORK_HOME=$PWD/.mini-ork; "
        "MO_LANE_REQUIRES_CAPABILITY='vision,tools'; "
        "source lib/lane-helpers.sh; "
        "mo_assert_lane_capability codex_lens"
    )
    result = subprocess.run(
        ["bash", "--noprofile", "--norc", "-c", script],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert result.returncode == 1
    assert result.stderr.strip() == "vision"


def test_llm_calls_route_tolerates_null_taxonomy_columns(tmp_path: Path) -> None:
    from mini_ork.web.db import StateDB
    from mini_ork.web.routes.run_detail import get_llm_calls

    db_path = tmp_path / "state.db"
    now = 1_800_000_000
    con = sqlite3.connect(db_path)
    con.executescript(
        """
        CREATE TABLE task_runs (
          id TEXT PRIMARY KEY,
          trace_id TEXT,
          created_at INTEGER NOT NULL,
          ended_at INTEGER
        );
        CREATE TABLE llm_calls (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          provider TEXT NOT NULL,
          model_id TEXT NOT NULL,
          tier TEXT NOT NULL,
          feature_name TEXT NOT NULL,
          actor TEXT,
          input_tokens INTEGER NOT NULL DEFAULT 0,
          output_tokens INTEGER NOT NULL DEFAULT 0,
          total_tokens INTEGER NOT NULL DEFAULT 0,
          cost_usd REAL NOT NULL DEFAULT 0,
          duration_ms INTEGER NOT NULL DEFAULT 0,
          status TEXT NOT NULL CHECK (status IN ('success','failed')),
          finish_reason TEXT,
          error_message TEXT,
          traceparent TEXT,
          metadata_json TEXT NOT NULL DEFAULT '{}',
          session_id TEXT,
          error_category TEXT,
          retryable INTEGER,
          ts TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
        );
        """
    )
    con.execute(
        "INSERT INTO task_runs(id, trace_id, created_at, ended_at) VALUES ('run-1', 'trace-1', ?, ?)",
        (now - 10, now + 10),
    )
    con.execute(
        """
        INSERT INTO llm_calls (
          provider, model_id, tier, feature_name, status, finish_reason,
          error_category, retryable, traceparent, ts
        ) VALUES (
          'gateway', 'glm', 'default', 'mini-ork:reviewer', 'success', NULL,
          NULL, NULL, '00-trace-1-span-01', '2027-01-15T08:00:00.000Z'
        )
        """
    )
    con.commit()
    con.close()

    rows = get_llm_calls(task_run_id="run-1", db=StateDB(db_path))
    assert len(rows) == 1
    assert rows[0]["feature_name"] == "mini-ork:reviewer"


def test_profile_answerer_helper_exists() -> None:
    helper = ROOT / "lib" / "profile_answerer.sh"
    text = helper.read_text()

    assert "mo_answer_profile_questions" in text
    assert "llm_dispatch" in text
    assert "--node-type \"profile_answerer\"" in text
    assert "\"auto_answered\": True" in text


def test_python_plan_references_native_auto_answer() -> None:
    plan = (ROOT / "mini_ork" / "ported" / "mini_ork_plan.py").read_text()

    assert "MO_AUTO_ANSWER_PROFILE" in plan
    assert "mini_ork.ported.profile_answerer" in plan
    assert "answer_profile_questions" in plan


# ── project switcher (GET /projects, POST /projects/switch) ─────────────────


def test_projects_list_includes_active(db, home, monkeypatch, tmp_path) -> None:
    from mini_ork.web.deps import get_home
    from mini_ork.web.routes.projects import list_projects

    monkeypatch.setenv("MINI_ORK_PROJECTS_FILE", str(tmp_path / "projects.json"))
    out = list_projects(get_home())
    assert out["active"] == str(home)
    actives = [p for p in out["projects"] if p["active"]]
    assert len(actives) == 1
    assert actives[0]["home"] == str(home)
    assert actives[0]["exists"] is True


def test_projects_switch_rejects_bogus_path(db, monkeypatch, tmp_path) -> None:
    from fastapi import HTTPException

    from mini_ork.web.routes.projects import switch_project

    monkeypatch.setenv("MINI_ORK_PROJECTS_FILE", str(tmp_path / "projects.json"))
    with pytest.raises(HTTPException) as e:
        switch_project({"home": str(tmp_path / "nope")})
    assert e.value.status_code == 400
    with pytest.raises(HTTPException) as e:
        switch_project({"home": "  "})
    assert e.value.status_code == 422


def test_projects_switch_swaps_db_and_registers(db, home, monkeypatch, tmp_path) -> None:
    """Switch to a second home, verify get_db points at it, switch back."""
    import sqlite3

    from mini_ork.web.deps import get_db, get_home, set_home_override
    from mini_ork.web.routes.projects import list_projects, switch_project

    monkeypatch.setenv("MINI_ORK_PROJECTS_FILE", str(tmp_path / "projects.json"))

    other = tmp_path / "researcher" / ".mini-ork"
    other.mkdir(parents=True)
    con = sqlite3.connect(other / "state.db")
    con.execute("CREATE TABLE task_runs (id TEXT PRIMARY KEY, status TEXT)")
    con.commit()
    con.close()

    try:
        # convenience: project folder (parent of .mini-ork) is accepted too
        out = switch_project({"home": str(other.parent)})
        assert out["ok"] is True
        assert out["active"] == str(other)
        assert out["name"] == "researcher"
        assert get_home() == other
        assert get_db().db_path == other / "state.db"

        listed = list_projects(get_home())
        assert str(other) in [p["home"] for p in listed["projects"]]
    finally:
        set_home_override(home)
    # StateDB.db_path is canonicalised via .resolve(); resolve the expected
    # side too so the assert holds whether state.db is a real file or a symlink
    # (e.g. a worktree/vendored install whose state.db links elsewhere).
    assert get_db().db_path == (home / "state.db").resolve()


def test_projects_validate_and_add(db, home, monkeypatch, tmp_path) -> None:
    import sqlite3

    from mini_ork.web.deps import get_home
    from mini_ork.web.routes.projects import add_project, list_projects, validate_project

    monkeypatch.setenv("MINI_ORK_PROJECTS_FILE", str(tmp_path / "projects.json"))
    active = get_home()

    bad = validate_project(str(tmp_path / "nowhere"), active)
    assert bad["ok"] is False and "error" in bad

    other = tmp_path / "researcher" / ".mini-ork"
    other.mkdir(parents=True)
    sqlite3.connect(other / "state.db").close()

    good = validate_project(str(other.parent), active)  # project folder accepted
    assert good["ok"] is True
    assert good["home"] == str(other)
    assert good["name"] == "researcher"
    assert good["registered"] is False

    out = add_project({"home": str(other.parent)}, active)
    assert out["ok"] is True and out["project"]["home"] == str(other)
    # add registers without switching
    assert validate_project(str(other), active)["registered"] is True
    listed = list_projects(active)
    assert str(other) in [p["home"] for p in listed["projects"]]
    assert listed["active"] == str(home)


def test_workspace_scoped_home_resolution(db, home, tmp_path) -> None:
    """Per-request workspace: the X-Mini-Ork-Home header (or `home` query
    param for SSE) resolves to its own home + DB without touching the
    server-wide default."""
    import sqlite3

    from fastapi import HTTPException

    from mini_ork.web.deps import db_for, get_db, get_default_home, get_home, get_home_lenient

    other = tmp_path / "researcher" / ".mini-ork"
    other.mkdir(parents=True)
    sqlite3.connect(other / "state.db").close()

    assert get_home(str(other.parent), None) == other  # project folder accepted
    assert get_home(None, str(other)) == other  # query param (SSE) works
    assert get_home(str(other), str(home)) == other  # header beats query param
    assert get_home(None, None) == get_default_home()  # no workspace → default

    # request-scoped DB resolution leaves the server default untouched
    assert get_db(get_home(str(other), None)).db_path == other / "state.db"
    # StateDB.db_path is canonicalised via .resolve(); resolve the expected
    # side too so the assert holds whether state.db is a real file or a symlink
    # (e.g. a worktree/vendored install whose state.db links elsewhere).
    assert get_db().db_path == (home / "state.db").resolve()
    assert db_for(other) is db_for(other)  # per-home handle cache

    with pytest.raises(HTTPException) as e:
        get_home(str(tmp_path / "nope"), None)
    assert e.value.status_code == 404
    # lenient variant (projects routes) falls back instead of locking out
    assert get_home_lenient(str(tmp_path / "nope"), None) == get_default_home()


# ── E4 cost-pause + E6 auth HTTP integration ──────────────────────────────


def test_pause_cost_writes_sentinel(tmp_path: Path) -> None:
    from mini_ork.web.control import pause_cost_run

    run_id = "run-test-pause"
    run_dir = tmp_path / "runs" / run_id
    run_dir.mkdir(parents=True)

    result = pause_cost_run(tmp_path, run_id, threshold_usd=42.0)
    assert result["ok"] is True
    assert result["threshold_usd"] == 42.0
    sentinel = run_dir / ".cost-pause"
    assert sentinel.is_file()
    payload = json.loads(sentinel.read_text())
    assert payload["threshold_usd"] == 42.0
    assert payload["source"] == "http-api"
    assert payload["run_id"] == run_id


def test_pause_cost_rejects_missing_run(tmp_path: Path) -> None:
    from mini_ork.web.control import pause_cost_run

    result = pause_cost_run(tmp_path, "run-nope")
    assert result["ok"] is False
    assert "not found" in result["error"]


def test_resume_cost_clears_sentinel_and_audits(tmp_path: Path) -> None:
    from mini_ork.web.control import pause_cost_run, resume_cost_run

    run_id = "run-test-resume"
    run_dir = tmp_path / "runs" / run_id
    run_dir.mkdir(parents=True)
    pause_cost_run(tmp_path, run_id, threshold_usd=10.0)

    result = resume_cost_run(tmp_path, run_id, approver="amir")
    assert result["ok"] is True
    assert result["approver"] == "amir"
    assert not (run_dir / ".cost-pause").exists()

    approvals = run_dir / ".cost-pause-approvals.jsonl"
    assert approvals.is_file()
    audit_line = json.loads(approvals.read_text().splitlines()[0])
    assert audit_line["approver"] == "amir"
    assert audit_line["run_id"] == run_id
    assert audit_line["source"] == "http-api"


def test_resume_cost_without_sentinel_returns_error(tmp_path: Path) -> None:
    from mini_ork.web.control import resume_cost_run

    run_id = "run-test-no-sentinel"
    run_dir = tmp_path / "runs" / run_id
    run_dir.mkdir(parents=True)

    result = resume_cost_run(tmp_path, run_id, approver="amir")
    assert result["ok"] is False
    assert "no cost-pause sentinel" in result["error"]


def test_auth_require_token_rejects_missing_header() -> None:
    from fastapi import HTTPException

    from mini_ork.web.auth import require_token

    # FastAPI Request stub: only headers is consulted.
    class _StubReq:
        headers = {}  # type: ignore[assignment]

    with pytest.raises(HTTPException) as exc:
        require_token(_StubReq())  # type: ignore[arg-type]
    assert exc.value.status_code == 401


def test_auth_require_token_accepts_valid_token(tmp_path: Path, monkeypatch) -> None:
    from mini_ork.web.auth import require_token

    tokens_file = tmp_path / "auth-tokens.txt"
    tokens_file.write_text("abc123 amir\n# comment\ndef456 ops-bot\n")
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path))

    class _StubReq:
        headers = {"authorization": "Bearer abc123"}

    assert require_token(_StubReq()) == "amir"  # type: ignore[arg-type]
