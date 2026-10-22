"""Per-epic budget enforcement in the canonical scheduler (SDE-4c).

Hermetic: a temp repo root + home + sqlite state.db built from the repo's own
migration SQL. Dispatch runs through ``dispatch_epic``'s ``runner_cmd`` seam (a
stub that touches a marker — never a real run), so no network, provider
credentials, or run-local state are involved.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

from mini_ork import scheduler

REPO_ROOT = Path(scheduler.__file__).resolve().parents[1]
MIGRATIONS = REPO_ROOT / "db" / "migrations"

# Cleared for every test so a stray operator value cannot change a verdict.
_SCHED_ENV = (
    "MO_PER_EPIC_USD",
    "MINI_ORK_AGENTS",
    "MINI_ORK_DB",
    "MINI_ORK_HOME",
    "MINI_ORK_ROOT",
    "MO_SCHED_PRE_DISPATCH_HOOK",
    "MO_SCHED_POST_VERDICT_HOOK",
    "MO_SCHED_REQUIRED_VERIFIERS",
    "MO_SCHED_MAX_ATTEMPTS",
    "MO_SCHED_EPIC_FILTER",
    "MO_TARGET_CWD",
    "MO_SCHED_CARRY_OVER",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in _SCHED_ENV:
        monkeypatch.delenv(var, raising=False)
    # Skip carry-over kickoff construction: these tests exercise the budget
    # guard, not run-history aggregation.
    monkeypatch.setenv("MO_SCHED_CARRY_OVER", "0")


def _apply_migrations(db: Path) -> None:
    con = sqlite3.connect(db)
    try:
        for name in ("0001_core.sql", "0013_task_runs.sql",
                     "0027_epic_dependencies.sql"):
            con.executescript((MIGRATIONS / name).read_text(encoding="utf-8"))
            con.commit()
    finally:
        con.close()


def _exec(db: Path, sql: str, params=()) -> None:
    con = sqlite3.connect(db)
    try:
        con.execute(sql, params)
        con.commit()
    finally:
        con.close()


def _add_epic(db, epic_id, *, status="not started", kickoff_path=None,
              max_attempts=None, held_reason=None):
    _exec(db, "INSERT INTO epics (id, title, status, kickoff_path, "
              "max_attempts, held_reason) VALUES (?, ?, ?, ?, ?, ?)",
          (epic_id, f"title {epic_id}", status, kickoff_path, max_attempts,
           held_reason))


def _add_attempt(db, epic_id, attempt, run_id):
    _exec(db, "INSERT INTO epic_attempts (epic_id, attempt, run_id) "
              "VALUES (?, ?, ?)", (epic_id, attempt, run_id))


def _add_run(db, run_id, cost_usd):
    _exec(db, "INSERT INTO task_runs (id, task_class, kickoff_path, status, "
              "cost_usd, created_at, updated_at, ended_at) "
              "VALUES (?, 'spec', 'kickoffs/x.md', 'failed', ?, 1, 1, 2)",
          (run_id, cost_usd))


def _epic(db, epic_id) -> dict:
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    try:
        return dict(con.execute("SELECT * FROM epics WHERE id=?",
                                (epic_id,)).fetchone())
    finally:
        con.close()


def _attempt_count(db, epic_id) -> int:
    con = sqlite3.connect(db)
    try:
        return con.execute("SELECT COUNT(*) FROM epic_attempts WHERE epic_id=?",
                           (epic_id,)).fetchone()[0]
    finally:
        con.close()


def _write_config(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _runner_cmd(marker: Path, run_id: str = "r9"):
    """``runner_cmd`` seam: touch a marker (proof of invocation), print a
    run_id= line the way a real runner does."""
    code = (f"import pathlib\n"
            f"pathlib.Path({str(marker)!r}).write_text('ran')\n"
            f"print('run_id={run_id}')\n")
    return [sys.executable, "-c", code]


def _spent_350(world):
    """An epic with two recorded attempts whose runs cost $1.25 + $2.25."""
    db = world["db"]
    _add_run(db, "r1", 1.25)
    _add_run(db, "r2", 2.25)
    _add_attempt(db, "E1", 1, "r1")
    _add_attempt(db, "E1", 2, "r2")


def _dispatch(world, marker, **kw):
    return scheduler.dispatch_epic(
        "E1", world["root_s"], world["home_s"], "epic-runner",
        db=world["db_s"], runner_cmd=_runner_cmd(marker), **kw)


@pytest.fixture
def world(tmp_path):
    """Temp repo root + home + migrated state.db with one dispatchable epic."""
    root = tmp_path / "root"
    (root / "kickoffs").mkdir(parents=True)
    (root / "kickoffs" / "E1.md").write_text("# epic E1\n", encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()
    db = tmp_path / "state.db"
    _apply_migrations(db)
    scheduler.ensure_priority_column(str(db))
    scheduler.ensure_retry_schema(str(db))
    _add_epic(db, "E1", kickoff_path="kickoffs/E1.md", max_attempts=3)
    return {"root": root, "home": home, "db": db,
            "root_s": str(root), "home_s": str(home), "db_s": str(db)}


# ── epic_spend_usd ───────────────────────────────────────────────────────────


def test_epic_spend_usd_sums_attempts_and_counts_last_run_id_once(world):
    db, db_s = world["db"], world["db_s"]
    _add_run(db, "r1", 1.25)
    _add_run(db, "r2", 2.25)
    _add_run(db, "r3", 0.75)
    _add_attempt(db, "E1", 1, "r1")
    _add_attempt(db, "E1", 2, "r2")

    _exec(db, "UPDATE epics SET last_run_id='r2' WHERE id='E1'")
    assert scheduler.epic_spend_usd("E1", db=db_s) == pytest.approx(3.50)

    # last_run_id not among the attempt runs → added
    _exec(db, "UPDATE epics SET last_run_id='r3' WHERE id='E1'")
    assert scheduler.epic_spend_usd("E1", db=db_s) == pytest.approx(4.25)

    # a run id repeated across attempts is still one distinct run
    _add_attempt(db, "E1", 3, "r1")
    assert scheduler.epic_spend_usd("E1", db=db_s) == pytest.approx(4.25)


def test_epic_spend_usd_missing_tables_or_rows_is_zero(world, tmp_path):
    empty = tmp_path / "empty.db"
    sqlite3.connect(empty).close()  # no tables at all
    assert scheduler.epic_spend_usd("E1", db=str(empty)) == 0.0
    assert scheduler.epic_spend_usd("E1", db=world["db_s"]) == 0.0


# ── per_epic_cap ─────────────────────────────────────────────────────────────


def test_no_cap_configured_is_none_and_dispatch_proceeds(world, tmp_path):
    assert scheduler.per_epic_cap(world["home_s"], world["root_s"]) is None
    marker = tmp_path / "m-no-cap"
    verdict, _rc = _dispatch(world, marker)
    assert verdict != "budget_blocked"
    assert marker.exists()


def test_per_epic_cap_env_when_set(world, monkeypatch):
    _write_config(world["home"] / "config" / "agents.yaml",
                  "budget:\n  per_epic_usd: 3\n")
    monkeypatch.setenv("MO_PER_EPIC_USD", "10")
    assert scheduler.per_epic_cap(world["home_s"], world["root_s"]) == 10.0


def test_per_epic_cap_bad_env_raises_naming_the_variable(world, monkeypatch):
    for bad in ("abc", "-1"):
        monkeypatch.setenv("MO_PER_EPIC_USD", bad)
        with pytest.raises(ValueError) as exc:
            scheduler.per_epic_cap(world["home_s"], world["root_s"])
        assert "MO_PER_EPIC_USD" in str(exc.value)
    # zero is neither non-numeric nor negative — a valid cap
    monkeypatch.setenv("MO_PER_EPIC_USD", "0")
    assert scheduler.per_epic_cap(world["home_s"], world["root_s"]) == 0.0


def test_per_epic_cap_reads_home_agents_yaml(world):
    _write_config(world["home"] / "config" / "agents.yaml",
                  "budget:\n  per_epic_usd: 3\n")
    assert scheduler.per_epic_cap(world["home_s"], world["root_s"]) == 3.0


def test_per_epic_cap_root_fallback_and_home_file_precedence(world):
    _write_config(world["root"] / "config" / "agents.yaml",
                  "budget:\n  per_epic_usd: 5\n")
    # no HOME config → ROOT's is read
    assert scheduler.per_epic_cap(world["home_s"], world["root_s"]) == 5.0
    # a HOME file exists → it is the base even without a budget block
    _write_config(world["home"] / "config" / "agents.yaml",
                  "lanes:\n  coder: codex\n")
    assert scheduler.per_epic_cap(world["home_s"], world["root_s"]) is None


def test_personal_overlay_budget_wins_over_home_file(world):
    _write_config(world["home"] / "config" / "agents.yaml",
                  "budget:\n  per_epic_usd: 3\n")
    _write_config(world["home"] / "config" / "agents.local.yaml",
                  "budget:\n  per_epic_usd: 7\n")
    assert scheduler.per_epic_cap(world["home_s"], world["root_s"]) == 7.0


def test_per_epic_cap_non_numeric_config_raises(world):
    _write_config(world["home"] / "config" / "agents.yaml",
                  "budget:\n  per_epic_usd: plenty\n")
    with pytest.raises(ValueError):
        scheduler.per_epic_cap(world["home_s"], world["root_s"])


# ── dispatch_epic enforcement ────────────────────────────────────────────────


def test_dispatch_epic_blocks_over_budget_epic(world, tmp_path):
    _write_config(world["home"] / "config" / "agents.yaml",
                  "budget:\n  per_epic_usd: 3\n")
    _spent_350(world)
    marker = tmp_path / "m-block"
    assert _dispatch(world, marker) == ("budget_blocked", 0)
    assert not marker.exists()          # the runner was never invoked
    row = _epic(world["db"], "E1")
    assert row["status"] == "blocked"
    assert row["held_reason"] == "per_epic_budget"
    assert row["attempts"] == 0         # dispatch nothing: no new attempt
    assert _attempt_count(world["db"], "E1") == 2


def test_budget_block_note_and_printed_line_two_decimals(world, tmp_path,
                                                         capsys):
    _write_config(world["home"] / "config" / "agents.yaml",
                  "budget:\n  per_epic_usd: 3\n")
    _spent_350(world)
    assert _dispatch(world, tmp_path / "m-note") == ("budget_blocked", 0)
    line = "per-epic budget: spent $3.50 >= cap $3.00"
    assert line in capsys.readouterr().out
    assert line in (_epic(world["db"], "E1")["notes"] or "")


def test_spend_equal_to_cap_blocks(world, tmp_path):
    _write_config(world["home"] / "config" / "agents.yaml",
                  "budget:\n  per_epic_usd: 3\n")
    _add_run(world["db"], "r1", 3.00)
    _add_attempt(world["db"], "E1", 1, "r1")
    assert _dispatch(world, tmp_path / "m-eq") == ("budget_blocked", 0)


def test_same_epic_with_env_10_dispatches(world, monkeypatch, tmp_path):
    _write_config(world["home"] / "config" / "agents.yaml",
                  "budget:\n  per_epic_usd: 3\n")
    _spent_350(world)
    monkeypatch.setenv("MO_PER_EPIC_USD", "10")
    marker = tmp_path / "m-dispatch"
    verdict, _rc = _dispatch(world, marker)
    assert verdict != "budget_blocked"
    assert marker.exists()


def test_dry_run_over_budget_prints_would_block_and_changes_nothing(
        world, tmp_path, capsys):
    _write_config(world["home"] / "config" / "agents.yaml",
                  "budget:\n  per_epic_usd: 3\n")
    _spent_350(world)
    marker = tmp_path / "m-dry"
    _dispatch(world, marker, dry_run=True)
    out = capsys.readouterr().out
    assert ("[dry-run] would block: E1 "
            "(per-epic budget: spent $3.50 >= cap $3.00)") in out
    assert "would dispatch" not in out
    assert not marker.exists()
    row = _epic(world["db"], "E1")
    assert row["status"] == "not started"
    assert row["held_reason"] is None
    assert "per-epic budget" not in (row["notes"] or "")
    assert row["attempts"] == 0


def test_budget_rechecked_before_every_retry_attempt(world, monkeypatch,
                                                     tmp_path):
    monkeypatch.setenv("MO_PER_EPIC_USD", "10")  # attempt 1 is under the cap
    m1, m2 = tmp_path / "m-retry1", tmp_path / "m-retry2"
    verdict1, _rc = _dispatch(world, m1)
    assert verdict1 != "budget_blocked"
    assert m1.exists()
    # the first attempt recorded run r9; its cost lands after the run
    _add_run(world["db"], "r9", 5.00)
    monkeypatch.setenv("MO_PER_EPIC_USD", "3")
    assert _dispatch(world, m2) == ("budget_blocked", 0)
    assert not m2.exists()
    row = _epic(world["db"], "E1")
    assert row["status"] == "blocked"
    assert row["held_reason"] == "per_epic_budget"


def test_pick_ready_does_not_return_budget_blocked_epic(world):
    _add_epic(world["db"], "E2", status="blocked",
              held_reason="per_epic_budget")
    ready = scheduler.pick_ready(db=world["db_s"])
    assert "E1" in ready          # control: a ready epic is still picked
    assert "E2" not in ready


# ── docs ─────────────────────────────────────────────────────────────────────


def test_env_vars_doc_documents_per_epic_enforcement():
    doc = (REPO_ROOT / "docs" / "operator" / "env-vars.md").read_text(
        encoding="utf-8")
    flat = " ".join(doc.lower().split())
    assert "per_epic_usd" in flat
    assert "mo_per_epic_usd" in flat
    i = flat.find("per_epic_usd")
    assert "enforc" in flat[i:i + 220]
    for line in doc.splitlines():
        if "per_epic_usd" in line.lower():
            assert "display only" not in line.lower()
    assert "per_run_usd" in flat and "daily_cap_usd" in flat
    assert "display only" in flat
    assert "mo_daily_budget_usd" in flat
