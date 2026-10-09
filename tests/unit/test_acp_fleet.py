"""Hermetic tests for the ACP fleet view (``mini_ork.acp.fleet``).

``fleet_rows`` / ``render_fleet`` back the ``/runs`` table; ``run_card`` /
``render_card`` back the ``/status`` run card. Both are pure read-model
projections over a migrated ``state.db`` plus a handful of run-directory
files (``acp-diffs.json``, ``.cost-pause``, ``verdict.json``).

Fixtures mirror ``test_acp_commands.py``: a module-scoped migrated db copied
once per test, a per-test ``.mini-ork`` home, and seed helpers for
``task_runs`` / ``run_events`` / ``llm_calls`` / ``learning_record``. Every
timestamp is a fixed epoch (``NOW``) so time rendering is deterministic.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from mini_ork.acp import fleet as fl  # noqa: E402
from mini_ork.acp.fleet import FleetRow  # noqa: E402
from mini_ork.stores import migrate as mig  # noqa: E402

# Fixed epoch so "3m12s" / "2d ago" cells are stable regardless of wall clock.
NOW = 1_800_000_000


@pytest.fixture(scope="module")
def _migrated_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Migrate once per module (a full migration takes seconds); tests copy it."""
    db_path = tmp_path_factory.mktemp("template") / "state.db"
    rc, out, err = mig.init_db(db=str(db_path), root=str(REPO))
    assert rc == 0, f"init_db failed:\n{out}\n{err}"
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    con.close()
    return db_path


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _migrated_db: Path) -> Path:
    """A migrated ``.mini-ork`` home; seeded rows come via the helpers below."""
    h = tmp_path / ".mini-ork"
    h.mkdir()
    (h / "runs-inbox").mkdir()
    (h / "runs").mkdir()
    shutil.copyfile(_migrated_db, h / "state.db")
    monkeypatch.setenv("MINI_ORK_HOME", str(h))
    return h


def seed_run(
    home: Path,
    *,
    run_id: str,
    recipe: str = "code-fix",
    status: str = "published",
    cost_usd: float = 0.25,
    created_at: int = NOW - 3600,
    updated_at: int = NOW - 3600,
    title: str = "Fix bug",
) -> None:
    """Insert one task_runs row + a kickoff file so ``list_runs`` finds it."""
    kickoff_path = str(home / "runs-inbox" / f"{run_id}.md")
    (home / "runs-inbox" / f"{run_id}.md").write_text(
        f"# {title}\n", encoding="utf-8"
    )
    con = sqlite3.connect(home / "state.db")
    con.execute(
        """
        INSERT INTO task_runs
            (id, recipe, status, cost_usd, created_at, updated_at,
             task_class, kickoff_path, workflow_version)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (run_id, recipe, status, cost_usd, created_at, updated_at,
         "framework_edit", kickoff_path, "latest"),
    )
    con.commit()
    con.close()


def seed_event(
    home: Path,
    *,
    run_id: str,
    event_id: str,
    event_type: str,
    node_id: str,
    node_type: str = "",
    model_lane: str = "",
    finish_reason: str = "",
    created_at: int = NOW - 100,
) -> None:
    """Insert one run_events lifecycle row with a node payload."""
    payload: dict[str, object] = {"node_id": node_id}
    if node_type:
        payload["node_type"] = node_type
    if model_lane:
        payload["model_lane"] = model_lane
    if finish_reason:
        payload["finish_reason"] = finish_reason
    con = sqlite3.connect(home / "state.db")
    con.execute(
        """
        INSERT INTO run_events (event_id, run_id, event_type, payload_json, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (event_id, run_id, event_type, json.dumps(payload), created_at),
    )
    con.commit()
    con.close()


def seed_llm_call(
    home: Path,
    *,
    run_id: str,
    feature_name: str,
    actor: str = "worker",
    cost_usd: float = 0.5,
    finish_reason: str = "done",
) -> None:
    """Insert one llm_call attributed to ``run_id`` (the cost-by-stage input)."""
    con = sqlite3.connect(home / "state.db")
    con.execute(
        """
        INSERT INTO llm_calls
            (provider, model_id, tier, feature_name, actor, cost_usd,
             status, finish_reason, run_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("test", "test-model", "default", feature_name, actor, cost_usd,
         "success", finish_reason, run_id),
    )
    con.commit()
    con.close()


def seed_learning(home: Path, *, run_id: str, title: str, rank: int = 1) -> None:
    """Insert one learning_record row (the card's learnings list)."""
    con = sqlite3.connect(home / "state.db")
    con.execute(
        """
        INSERT INTO learning_record
            (run_id, iter, rank, category, title, evidence_paths, arxiv_refs,
             patch_summary, outcome, severity, confidence, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (run_id, 1, rank, "meta", title, "[]", "[]", None, "open", "medium",
         0.5, NOW, NOW),
    )
    con.commit()
    con.close()


def _run_dir(home: Path, run_id: str) -> Path:
    """``<home>/runs/<run_id>`` — created on demand for run-directory files."""
    d = home / "runs" / run_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_diff_cache(home: Path, run_id: str, diffs: list[dict]) -> None:
    """Stage ``<run_dir>/acp-diffs.json`` with the test's diff triples."""
    cache = _run_dir(home, run_id) / "acp-diffs.json"
    cache.write_text(json.dumps(diffs), encoding="utf-8")


# ── rows + counts ────────────────────────────────────────────────────────────


def test_rows_and_counts_per_state(home: Path):
    seed_run(home, run_id="r-done", status="published")
    seed_run(home, run_id="r-work", status="executing")
    seed_run(home, run_id="r-fail", status="failed")
    seed_run(home, run_id="r-pause", status="executing")
    (_run_dir(home, "r-pause") / ".cost-pause").write_text("", encoding="utf-8")

    rows, counts = fl.fleet_rows(home)

    assert counts == {"working": 1, "needs_you": 1, "done": 1, "failed": 1}
    assert all(isinstance(r, FleetRow) for r in rows)
    by_id = {r.run_id: r for r in rows}
    assert by_id["r-done"].state == "done"
    assert by_id["r-work"].state == "working"
    assert by_id["r-fail"].state == "failed"
    # The cost-pause sentinel flips the executing run into needs_you.
    assert by_id["r-pause"].state == "needs_you"
    assert by_id["r-pause"].mark == "✋"
    assert by_id["r-done"].mark == "✓"
    assert by_id["r-fail"].mark == "✗"


# ── filters ──────────────────────────────────────────────────────────────────


def test_state_recipe_and_limit_filters(home: Path):
    seed_run(home, run_id="a-done", status="published", recipe="code-fix")
    seed_run(home, run_id="a-work", status="executing", recipe="code-fix")
    seed_run(home, run_id="b-done", status="published", recipe="docs")

    rows, _ = fl.fleet_rows(home, state="done", recipe="code-fix")
    assert [r.run_id for r in rows] == ["a-done"]

    rows, _ = fl.fleet_rows(home, state="working")
    assert [r.run_id for r in rows] == ["a-work"]

    # needs-you spelling is accepted alongside the underscore form.
    rows, _ = fl.fleet_rows(home, state="needs-you")
    assert rows == []


def test_limit_caps_shown_rows(home: Path):
    for i in range(30):
        seed_run(home, run_id=f"r-{i:03d}", status="published", created_at=NOW - i)
    rows, counts = fl.fleet_rows(home, limit=5)
    assert len(rows) == 5
    # Counts cover every candidate, not just the shown slice.
    assert counts == {"working": 0, "needs_you": 0, "done": 30, "failed": 0}


def test_precise_state_only_computed_for_shown_rows(
    home: Path, monkeypatch: pytest.MonkeyPatch
):
    for i in range(10):
        seed_run(home, run_id=f"r-{i:03d}", status="published", created_at=NOW - i)

    calls: list[str] = []
    batched: list[list[str]] = []
    orig_state = fl.task_state
    orig_events = fl._events_by_run

    def spy_state(run_dir, snapshot):
        calls.append(Path(run_dir).name)
        return orig_state(run_dir, snapshot)

    def spy_events(h, run_ids):
        batched.append(list(run_ids))
        return orig_events(h, run_ids)

    monkeypatch.setattr(fl, "task_state", spy_state)
    monkeypatch.setattr(fl, "_events_by_run", spy_events)

    rows, _ = fl.fleet_rows(home, limit=3)
    assert len(rows) == 3
    # Precise state (and with it the diff reads) is paid only for the rows that
    # survive filtering and the limit — not all 10 candidates — and their events
    # come from one batched query over exactly those rows.
    assert calls == ["r-000", "r-001", "r-002"]
    assert batched == [["r-000", "r-001", "r-002"]]


# ── time formatting ──────────────────────────────────────────────────────────


def test_format_duration_compact():
    assert fl._format_duration(0) == "0m00s"
    assert fl._format_duration(192) == "3m12s"
    assert fl._format_duration(3840) == "1h04m"
    assert fl._format_duration(86400) == "1d"
    assert fl._format_duration(-5) == "0m00s"


def test_render_fleet_time_cells(home: Path):
    # Working row started 200s ago → elapsed.
    seed_run(home, run_id="r-work", status="executing",
             created_at=NOW - 200, updated_at=NOW - 200)
    # Done row ran 1h and finished 2d ago → duration + age.
    seed_run(home, run_id="r-done", status="published",
             created_at=NOW - 2 * 86400 - 3600, updated_at=NOW - 2 * 86400)

    rows, counts = fl.fleet_rows(home)
    out = fl.render_fleet(rows, counts, state="all", now=NOW)
    assert "3m20s" in out
    assert "1h00m · 2d ago" in out


# ── change column ────────────────────────────────────────────────────────────


def test_change_from_diff_cache(home: Path):
    seed_run(home, run_id="r-done", status="published")
    _write_diff_cache(
        home, "r-done",
        [{"path": "f.py", "old_text": "a\n", "new_text": "a\nb\nc\n"}],
    )
    rows, counts = fl.fleet_rows(home, state="done")
    assert rows[0].added == 2
    assert rows[0].removed == 0
    out = fl.render_fleet(rows, counts, state="done", now=NOW)
    # U+2212 unicode minus, NOT ASCII "-".
    assert "+2 −0" in out


# ── empty result ─────────────────────────────────────────────────────────────


def test_empty_result(home: Path):
    rows, counts = fl.fleet_rows(home)
    out = fl.render_fleet(rows, counts, state="all", now=NOW)
    assert "No runs match." in out


# ── run card ─────────────────────────────────────────────────────────────────


def test_card_steps_with_durations_and_failed_step(home: Path):
    seed_run(home, run_id="r-card", status="executing",
             created_at=NOW - 400, updated_at=NOW)
    seed_event(home, run_id="r-card", event_id="e1", event_type="node_start",
               node_id="planner", node_type="planner", model_lane="opus",
               created_at=NOW - 300)
    seed_event(home, run_id="r-card", event_id="e2", event_type="node_end",
               node_id="planner", node_type="planner", model_lane="opus",
               finish_reason="done", created_at=NOW - 260)
    seed_event(home, run_id="r-card", event_id="e3", event_type="node_start",
               node_id="implementer", node_type="implementer", model_lane="codex",
               created_at=NOW - 250)
    seed_event(home, run_id="r-card", event_id="e4", event_type="node_end",
               node_id="implementer", node_type="implementer", model_lane="codex",
               finish_reason="error", created_at=NOW - 100)

    card = fl.run_card(home, "r-card")
    assert card is not None
    steps = card["steps"]
    assert [s["node_id"] for s in steps] == ["planner", "implementer"]
    assert steps[0]["duration"] == 40
    assert steps[0]["state"] == "done"
    assert steps[1]["duration"] == 150
    assert steps[1]["state"] == "failed"
    assert steps[1]["finish_reason"] == "error"

    out = fl.render_card(card, now=NOW, serve_url=None)
    assert "| `planner` | planner | `opus` | 0m40s | done |" in out
    assert "| `implementer` | implementer | `codex` | 2m30s | failed (error) |" in out


def test_a_new_attempt_after_a_revise_verdict_is_running(home: Path):
    # Round 1's reviewer asked for a revision; round 2's reviewer is running.
    # The step must describe the latest attempt (running), not round 1's end.
    seed_run(home, run_id="r-revise", status="executing",
             created_at=NOW - 400, updated_at=NOW)
    seed_event(home, run_id="r-revise", event_id="e1", event_type="node_start",
               node_id="reviewer", node_type="reviewer", model_lane="opus",
               created_at=NOW - 300)
    seed_event(home, run_id="r-revise", event_id="e2", event_type="node_end",
               node_id="reviewer", node_type="reviewer", model_lane="opus",
               finish_reason="verdict_revise", created_at=NOW - 200)
    seed_event(home, run_id="r-revise", event_id="e3", event_type="node_start",
               node_id="reviewer", node_type="reviewer", model_lane="opus",
               created_at=NOW - 50)

    step = fl.run_card(home, "r-revise")["steps"][0]
    assert step["state"] == "running"
    assert step["start"] == NOW - 50
    assert step["end"] is None
    assert step["finish_reason"] == ""

    # Once round 2 ends, its own end is reported.
    seed_event(home, run_id="r-revise", event_id="e4", event_type="node_end",
               node_id="reviewer", node_type="reviewer", model_lane="opus",
               finish_reason="done", created_at=NOW - 10)
    step = fl.run_card(home, "r-revise")["steps"][0]
    assert step["state"] == "done"
    assert step["duration"] == 40


def test_same_second_prior_end_never_pairs_with_the_new_round(home: Path):
    # A revise round restarts the reviewer, and the new node_start shares a
    # second with round 1's node_end — the row order the 1-second clock (or a
    # reaper-synthesized dangling end) can produce. The new start must NOT
    # adopt round 1's end: that pairing rendered the node as a 0s finished
    # step (start == end, verdict of the *prior* round). It is running until
    # its OWN end lands.
    seed_run(home, run_id="r-tie", status="executing",
             created_at=NOW - 400, updated_at=NOW)
    seed_event(home, run_id="r-tie", event_id="e1", event_type="node_start",
               node_id="reviewer", node_type="reviewer", model_lane="opus",
               created_at=NOW - 300)
    # Round 2's start is inserted BEFORE round 1's end, and both share a
    # second — the out-of-order arrival the pairing must survive.
    seed_event(home, run_id="r-tie", event_id="e3", event_type="node_start",
               node_id="reviewer", node_type="reviewer", model_lane="opus",
               created_at=NOW - 200)
    seed_event(home, run_id="r-tie", event_id="e2", event_type="node_end",
               node_id="reviewer", node_type="reviewer", model_lane="opus",
               finish_reason="verdict_revise", created_at=NOW - 200)

    step = fl.run_card(home, "r-tie")["steps"][0]
    assert step["state"] == "running"
    assert step["start"] == NOW - 200
    assert step["end"] is None
    assert step["duration"] is None

    # Round 2's own end (a later second) then pairs with round 2's start.
    seed_event(home, run_id="r-tie", event_id="e4", event_type="node_end",
               node_id="reviewer", node_type="reviewer", model_lane="opus",
               finish_reason="done", created_at=NOW - 100)
    step = fl.run_card(home, "r-tie")["steps"][0]
    assert step["state"] == "done"
    assert step["duration"] == 100


def test_cost_by_stage_grouping_and_labels(home: Path):
    seed_run(home, run_id="r-cost", status="published")
    seed_llm_call(home, run_id="r-cost", feature_name="mini-ork:gradient-extract",
                  cost_usd=0.49)
    seed_llm_call(home, run_id="r-cost", feature_name="mini-ork:pattern-induct",
                  cost_usd=0.5)
    seed_llm_call(home, run_id="r-cost", feature_name="mini-ork:profile_answerer",
                  cost_usd=0.87)

    card = fl.run_card(home, "r-cost")
    assert card is not None
    groups = card["cost_by_stage"]
    assert groups["learning"] == pytest.approx(0.99)
    assert groups["profiling"] == pytest.approx(0.87)
    assert card["cost_total"] == pytest.approx(1.86)

    out = fl.render_card(card, now=NOW, serve_url=None)
    assert "learning $0.99" in out
    assert "profiling $0.87" in out


def test_files_from_cache_show_no_note(home: Path):
    seed_run(home, run_id="r-f", status="published")
    _write_diff_cache(
        home, "r-f",
        [{"path": "f.py", "old_text": "a\n", "new_text": "a\nb\n"}],
    )
    card = fl.run_card(home, "r-f")
    assert card is not None
    assert card["files_from_cache"] is True
    out = fl.render_card(card, now=NOW, serve_url=None)
    assert "`f.py` +1 −0" in out
    assert "compared with the files as they are now" not in out


def test_files_computed_show_note(home: Path, monkeypatch: pytest.MonkeyPatch):
    seed_run(home, run_id="r-f", status="published")
    monkeypatch.setattr(
        fl, "cached_or_computed",
        lambda _run_dir: ([{"path": "g.py", "old_text": "x\n", "new_text": "x\ny\n"}], False),
    )
    card = fl.run_card(home, "r-f")
    assert card is not None
    assert card["files_from_cache"] is False
    out = fl.render_card(card, now=NOW, serve_url=None)
    assert "compared with the files as they are now" in out


def test_verdict_and_learnings(home: Path):
    seed_run(home, run_id="r-v", status="published")
    d = _run_dir(home, "r-v")
    (d / "verdict.json").write_text(
        json.dumps({"verdict": "pass", "reason": "tests green"}), encoding="utf-8"
    )
    seed_learning(home, run_id="r-v", title="remember to plan", rank=1)
    seed_learning(home, run_id="r-v", title="second learning", rank=2)

    card = fl.run_card(home, "r-v")
    assert card is not None
    assert card["verdict"] == {"verdict": "pass", "reason": "tests green"}
    assert card["learnings"] == ["remember to plan", "second learning"]

    out = fl.render_card(card, now=NOW, serve_url=None)
    assert "Verdict: **pass** — tests green" in out
    assert "remember to plan" in out


def test_unknown_run_returns_none(home: Path):
    assert fl.run_card(home, "nope-000") is None
