"""Hermetic tests for the ACP history read-model (``mini_ork.acp.history``).

A fresh tmp SQLite is initialised via the canonical
``mini_ork.stores.migrate.init_db`` (so ``StateDB`` sees the full schema), then
seeded with ``task_runs`` / ``run_events`` / ``llm_calls`` rows via raw
``sqlite3`` — ``StateDB`` is read-only, so seeding has to bypass it. No lane,
no network, no subprocess.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from mini_ork.acp import history  # noqa: E402
from mini_ork.stores import migrate as mig  # noqa: E402


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """A tmp ``.mini-ork`` home with a migrated ``state.db``."""
    db_path = tmp_path / "state.db"
    rc, out, err = mig.init_db(db=str(db_path), root=str(REPO))
    assert rc == 0, f"init_db failed:\n{out}\n{err}"
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path))
    monkeypatch.setenv("MINI_ORK_DB", str(db_path))
    return tmp_path


def _seed_task_run(
    home: Path,
    run_id: str,
    *,
    recipe: str | None = "code-fix",
    status: str = "published",
    cost_usd: float = 0.5,
    created_at: int = 1000,
    updated_at: int = 2000,
    kickoff_path: str | None = None,
    ended_at: int | None = None,
    trace_id: str | None = None,
) -> None:
    con = sqlite3.connect(str(home / "state.db"))
    try:
        con.execute(
            """
            INSERT INTO task_runs
              (id, task_class, recipe, kickoff_path, status, cost_usd,
               created_at, updated_at, ended_at, trace_id)
            VALUES (?, 'code_fix', ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                recipe,
                kickoff_path or f"{run_id}.md",
                status,
                cost_usd,
                created_at,
                updated_at,
                ended_at,
                trace_id,
            ),
        )
        con.commit()
    finally:
        con.close()


def _seed_node_event(
    home: Path, run_id: str, node_id: str, event_type: str = "node_start"
) -> None:
    con = sqlite3.connect(str(home / "state.db"))
    try:
        con.execute(
            """
            INSERT INTO run_events (event_id, run_id, event_type, payload_json, created_at)
            VALUES (?, ?, ?, ?, 1500)
            """,
            (
                f"{run_id}-{node_id}",
                run_id,
                event_type,
                json.dumps({"node_id": node_id, "node_type": "planner"}),
            ),
        )
        con.commit()
    finally:
        con.close()


def _seed_llm_call(home: Path, traceparent: str, cost_usd: float = 1.25) -> None:
    con = sqlite3.connect(str(home / "state.db"))
    try:
        con.execute(
            """
            INSERT INTO llm_calls
              (provider, model_id, tier, feature_name, input_tokens,
               output_tokens, total_tokens, cost_usd, status, traceparent)
            VALUES ('test', 'test-model', 'fast', 'test', 50, 50, 100, ?, 'success', ?)
            """,
            (cost_usd, traceparent),
        )
        con.commit()
    finally:
        con.close()


def _write_kickoff(home: Path, run_id: str, text: str) -> Path:
    inbox = home / "runs-inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    path = inbox / f"{run_id}.md"
    path.write_text(text, encoding="utf-8")
    return path


# ── list_runs ────────────────────────────────────────────────────────────────


def test_list_runs_newest_first_with_titles(home):
    for run_id, created in (("run-a", 1000), ("run-b", 2000), ("run-c", 3000)):
        path = _write_kickoff(home, run_id, f"# Title {run_id}\nbody")
        _seed_task_run(
            home,
            run_id,
            kickoff_path=str(path),
            created_at=created,
            updated_at=created + 100,
        )
    rows, next_offset = history.list_runs(home)
    assert [r["run_id"] for r in rows] == ["run-c", "run-b", "run-a"]
    assert [r["title"] for r in rows] == ["Title run-c", "Title run-b", "Title run-a"]
    assert next_offset is None
    assert rows[0]["status"] == "published"
    assert rows[0]["recipe"] == "code-fix"
    assert rows[0]["updated_at"] == "1970-01-01T00:51:40Z"  # epoch 3100


def test_list_runs_title_strips_heading_and_caps_80_chars(home):
    long_title = "# " + ("x" * 100)
    path = _write_kickoff(home, "run-a", long_title + "\nbody")
    _seed_task_run(home, "run-a", kickoff_path=str(path))
    rows, _ = history.list_runs(home)
    assert rows[0]["title"] == "x" * 80


def test_list_runs_fallback_title_without_kickoff(home):
    _seed_task_run(home, "run-a", recipe="code-fix")
    rows, _ = history.list_runs(home)
    assert rows[0]["title"] == "code-fix run"


def test_list_runs_fallback_title_without_recipe(home):
    _seed_task_run(home, "run-a", recipe=None)
    rows, _ = history.list_runs(home)
    assert rows[0]["title"] == "mini-ork run"


def test_list_runs_pages_with_next_offset(home):
    for i, created in enumerate(range(1000, 6000, 1000)):
        _seed_task_run(home, f"run-{i}", created_at=created, updated_at=created)
    page1, off1 = history.list_runs(home, limit=2, offset=0)
    assert [r["run_id"] for r in page1] == ["run-4", "run-3"]
    assert off1 == 2
    page2, off2 = history.list_runs(home, limit=2, offset=2)
    assert [r["run_id"] for r in page2] == ["run-2", "run-1"]
    assert off2 == 4
    page3, off3 = history.list_runs(home, limit=2, offset=4)
    assert [r["run_id"] for r in page3] == ["run-0"]
    assert off3 is None


def test_list_runs_skips_unsafe_ids(home):
    _seed_task_run(home, "../etc", created_at=5000)
    _seed_task_run(home, "run-safe", created_at=1000)
    rows, _ = history.list_runs(home)
    assert [r["run_id"] for r in rows] == ["run-safe"]


def test_list_runs_missing_db_returns_empty(tmp_path):
    rows, next_offset = history.list_runs(tmp_path / "no-such-home")
    assert rows == []
    assert next_offset is None


# ── kickoff_text ─────────────────────────────────────────────────────────────


def test_kickoff_text_reads_kickoff_path_column(home):
    path = _write_kickoff(home, "run-a", "# Kickoff one\nbody text here")
    _seed_task_run(home, "run-a", kickoff_path=str(path))
    assert history.kickoff_text(home, "run-a") == "# Kickoff one\nbody text here"


def test_kickoff_text_falls_back_to_runs_inbox(home):
    _seed_task_run(home, "run-a", kickoff_path="/nonexistent/run-a.md")
    _write_kickoff(home, "run-a", "# Fallback kickoff\n")
    assert history.kickoff_text(home, "run-a") == "# Fallback kickoff\n"


def test_kickoff_text_truncates(home):
    path = _write_kickoff(home, "run-a", "x" * 100)
    _seed_task_run(home, "run-a", kickoff_path=str(path))
    assert history.kickoff_text(home, "run-a", max_chars=10) == "x" * 10


def test_kickoff_text_missing_returns_empty(home):
    _seed_task_run(home, "run-a", kickoff_path="/nonexistent/run-a.md")
    assert history.kickoff_text(home, "run-a") == ""


# ── read_snapshot ────────────────────────────────────────────────────────────


def test_read_snapshot_returns_same_shape_with_events_and_calls(home):
    _seed_task_run(home, "run-1", status="published", trace_id="trace-abc")
    _seed_node_event(home, "run-1", "n1", "node_start")
    _seed_llm_call(home, "00-trace-abc-0000000000000000-01")
    snap = history.read_snapshot(home, "run-1")
    assert set(snap) == {"status", "events", "llm_calls"}
    assert snap["status"] == "published"
    assert [e["event_type"] for e in snap["events"]] == ["node_start"]
    assert len(snap["llm_calls"]) == 1
    assert snap["llm_calls"][0]["cost_usd"] == 1.25


def test_read_snapshot_missing_run_returns_none_status(home):
    snap = history.read_snapshot(home, "run-missing")
    assert snap == {"status": None, "events": [], "llm_calls": []}


def test_read_snapshot_missing_db_returns_none_status(tmp_path):
    snap = history.read_snapshot(tmp_path / "no-such-home", "run-1")
    assert snap == {"status": None, "events": [], "llm_calls": []}


def test_read_snapshot_isolates_run_id_from_concurrent_runs(home):
    """A snapshot for run A must not include concurrent run B's llm_calls,
    even when both share a time window. The run-id bridge short-circuits the
    trace-id + time-window fallback when A's rows carry run_id stamps."""
    _seed_task_run(home, "run-a", status="published", trace_id="trace-a")
    _seed_task_run(home, "run-b", status="published", trace_id="trace-b")

    con = sqlite3.connect(str(home / "state.db"))
    try:
        # run-a has its own rows, stamped with its run_id.
        con.execute(
            "INSERT INTO llm_calls (provider, model_id, tier, feature_name,"
            " cost_usd, status, traceparent, run_id)"
            " VALUES ('p', 'm', 't', 'f', 1.0, 'success', '00-a-0', 'run-a')"
        )
        # run-b's rows are concurrent (same window) — must NOT appear in A.
        con.execute(
            "INSERT INTO llm_calls (provider, model_id, tier, feature_name,"
            " cost_usd, status, traceparent, run_id)"
            " VALUES ('p', 'm', 't', 'f', 9.0, 'success', '00-b-0', 'run-b')"
        )
        con.commit()
    finally:
        con.close()

    snap_a = history.read_snapshot(home, "run-a")
    snap_b = history.read_snapshot(home, "run-b")

    # The bridge returns llm_calls rows; cost_usd is what the SELECT projects.
    # Sum is what proves isolation: A sees 1.0 (its own), B sees 9.0 (its own),
    # not the time-window sum (10.0) the legacy bridge would have produced.
    assert sum(c["cost_usd"] for c in snap_a["llm_calls"]) == 1.0
    assert sum(c["cost_usd"] for c in snap_b["llm_calls"]) == 9.0


def test_read_snapshot_legacy_bridge_when_no_run_id_rows(home):
    """When no llm_calls rows are stamped with run_id, the legacy trace-id
    + time-window bridge still applies (so old state.db files keep working)."""
    _seed_task_run(home, "run-a", status="published", trace_id="trace-legacy")
    _seed_llm_call(home, "00-trace-legacy-0000000000000000-01", cost_usd=2.5)

    snap = history.read_snapshot(home, "run-a")
    assert len(snap["llm_calls"]) == 1
    assert snap["llm_calls"][0]["cost_usd"] == 2.5


def test_kickoff_titles_skip_front_matter_and_rules() -> None:
    from mini_ork.acp.history import _title_from_kickoff

    assert _title_from_kickoff("---\nrecipe: x\ntitle: Fix the login\n---\n# Body\n") == "Fix the login"
    assert _title_from_kickoff("---\nrecipe: x\n---\n\n# Ship it\n") == "Ship it"
    assert _title_from_kickoff("***\n\n## Real title\n") == "Real title"
    assert _title_from_kickoff("# Plain\n") == "Plain"
    assert _title_from_kickoff("---\n") == ""


def test_kickoff_title_prefers_a_heading_over_a_preamble() -> None:
    from mini_ork.acp.history import _title_from_kickoff

    text = "---\nrecipe: x\n---\n> **RULE (binding): every jest run…**\n\n# acq-wave5 — step W5-40\n"
    assert _title_from_kickoff(text) == "acq-wave5 — step W5-40"
    assert _title_from_kickoff("> **Note:** keep it short\n") == "Note: keep it short"
