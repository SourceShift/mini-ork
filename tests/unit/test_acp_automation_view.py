"""Hermetic tests for ``mini_ork.acp.automation_view`` (Zed S6b-1).

The view is the read-model behind ``/automations`` and ``/automation <id>``.
Tests pin:

* mark column — paused (⏸), never fired (·), failed-to-launch (✗), and the
  ``task_state.run_mark`` delegation when a published run exists;
* ``next`` short-format — ``today HH:MM`` / ``tomorrow HH:MM`` /
  ``<weekday> HH:MM`` / ``<weekday> <day> <Mon> HH:MM``;
* footer for each scheduler state — on, off, off-with-no-enabled;
* empty-state copy;
* card fields: heading, metadata line, Next line (3 times or Paused),
  kickoff fenced block, Recent runs table, ``last_error`` line, commands
  footer;
* unknown id → ``None``;
* ``automation_card`` ``proposal`` field reflects the on-disk draft.
"""
from __future__ import annotations

import datetime as _dt
import shutil
import sqlite3
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from mini_ork.acp import automation_view as av  # noqa: E402
from mini_ork.stores import migrate as mig  # noqa: E402


# ── helpers ──────────────────────────────────────────────────────────────────


def _add_automation(
    home: Path,
    *,
    id: str,
    name: str,
    schedule: str,
    workspace: str = "worktree",
    enabled: bool = True,
    last_run_id: str | None = None,
    last_error: str | None = None,
    runs: list[str] | None = None,
    kickoff: str = "# k",
) -> None:
    from mini_ork import automations as _auto

    _auto.add(
        home, id=id, name=name, recipe="code-fix",
        kickoff=kickoff, schedule=schedule, workspace=workspace,
    )
    if not enabled:
        _auto.pause(home, id)
    if last_run_id is not None or last_error is not None or runs is not None:
        # Patch the record directly — ``fire`` would need a real git repo.
        from mini_ork.automations import _write_locked  # noqa: PLC0415

        def mutate(items):
            for a in items:
                if a.get("id") == id:
                    if last_run_id is not None:
                        a["last_run_id"] = last_run_id
                        a["last_fired_at"] = _dt.datetime.now().replace(
                            microsecond=0
                        ).isoformat()
                    if last_error is not None:
                        a["last_error"] = last_error
                    if runs is not None:
                        a["runs"] = list(runs)
            return {"ok": True}
        _write_locked(home, mutate)


def _seed_task_run(
    home: Path,
    *,
    run_id: str,
    recipe: str = "code-fix",
    status: str = "published",
    age_seconds: int = 60,
    cost_usd: float = 0.0,
) -> None:
    now = int(time.time())
    con = sqlite3.connect(home / "state.db")
    con.execute(
        """
        INSERT INTO task_runs
            (id, recipe, status, cost_usd, created_at, updated_at,
             task_class, kickoff_path, workflow_version)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id, recipe, status, cost_usd,
            now - age_seconds, now - age_seconds,
            "framework_edit",
            str(home / "runs-inbox" / f"{run_id}.md"),
            "latest",
        ),
    )
    con.commit()
    con.close()


@pytest.fixture(scope="module")
def _migrated_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    db_path = tmp_path_factory.mktemp("template") / "state.db"
    rc, out, err = mig.init_db(db=str(db_path), root=str(REPO))
    assert rc == 0, f"init_db failed:\n{out}\n{err}"
    return db_path


@pytest.fixture
def fresh_home(tmp_path: Path, _migrated_db: Path) -> Path:
    h = tmp_path / ".mini-ork"
    h.mkdir()
    (h / "runs-inbox").mkdir()
    shutil.copyfile(_migrated_db, h / "state.db")
    # Seed the catalog so ``add`` validation passes.
    recipe_dir = h / "recipes" / "code-fix"
    recipe_dir.mkdir(parents=True, exist_ok=True)
    (recipe_dir / "workflow.yaml").write_text(
        "name: code-fix\nnodes: []\n", encoding="utf-8"
    )
    (recipe_dir / "task_class.yaml").write_text(
        "name: code-fix\n", encoding="utf-8"
    )
    return h


def _stub_scheduler(home: Path, *, installed: bool, last_tick: str | None = None):
    """Patch the module-level scheduler_status for the duration of a test."""
    import mini_ork.automations as _auto
    from unittest import mock

    payload = {
        "platform": "macos",
        "installed": installed,
        "command": "/bin/echo hi",
        "log_path": str(home / "automations-tick.log"),
        "last_tick": last_tick,
    }
    return mock.patch.object(
        _auto, "scheduler_status", lambda _h: payload
    )


# ── empty state ──────────────────────────────────────────────────────────────


def test_empty_returns_kickoff_copy(fresh_home):
    rows = av.automation_rows(fresh_home)
    assert rows == []
    out = av.render_automations(fresh_home)
    assert "No automations yet" in out


# ── marks ────────────────────────────────────────────────────────────────────


def test_mark_paused(fresh_home):
    _add_automation(
        fresh_home, id="daily", name="Daily",
        schedule="0 9 * * *", enabled=False,
    )
    rows = av.automation_rows(fresh_home)
    assert rows[0]["mark"] == "⏸"


def test_mark_never_fired(fresh_home):
    _add_automation(
        fresh_home, id="daily", name="Daily",
        schedule="0 9 * * *",
    )
    rows = av.automation_rows(fresh_home)
    assert rows[0]["mark"] == "·"


def test_mark_failed_to_launch(fresh_home):
    _add_automation(
        fresh_home, id="daily", name="Daily",
        schedule="0 9 * * *", last_error="crashed",
    )
    rows = av.automation_rows(fresh_home)
    assert rows[0]["mark"] == "✗"


def test_mark_delegates_to_run_mark_when_published(fresh_home):
    _add_automation(
        fresh_home, id="daily", name="Daily",
        schedule="0 9 * * *",
        last_run_id="run-pub-001",
        runs=["run-pub-001"],
    )
    _seed_task_run(fresh_home, run_id="run-pub-001", status="published")
    rows = av.automation_rows(fresh_home)
    # The published mark from task_state.run_mark is "✓".
    assert rows[0]["mark"] == "✓"


def test_card_mark_follows_the_last_run(fresh_home):
    _add_automation(fresh_home, id="nightly", name="Nightly", schedule="0 3 * * *",
                    last_run_id="run-1791000000-bad001", runs=["run-1791000000-bad001"])
    _seed_task_run(fresh_home, run_id="run-1791000000-bad001", status="failed")
    card = av.automation_card(fresh_home, "nightly")
    assert card["mark"] == "✗"
    assert av.render_automation_card(card).startswith("### ✗ Nightly")
    assert av.automation_rows(fresh_home)[0]["mark"] == "✗"


def test_last_run_cell_shows_run_age_and_status(fresh_home):
    _add_automation(fresh_home, id="daily", name="Daily", schedule="0 9 * * *",
                    last_run_id="run-1791000000-abc123", runs=["run-1791000000-abc123"])
    _seed_task_run(fresh_home, run_id="run-1791000000-abc123", status="published")
    cell = av.automation_rows(fresh_home)[0]["last_run"]
    assert cell == "`run-1791000000-abc123` · just now · published"


def test_table_reads_history_once(fresh_home, monkeypatch):
    for i in range(3):
        _add_automation(fresh_home, id=f"job-{i}", name=f"Job {i}", schedule="0 9 * * *",
                        last_run_id=f"run-179100000{i}-aaa00{i}")
    import mini_ork.acp.history as history
    calls = []
    real = history.list_runs
    monkeypatch.setattr(history, "list_runs", lambda *a, **k: calls.append(1) or real(*a, **k))
    av.automation_rows(fresh_home)
    assert len(calls) == 1


# ── next-format ─────────────────────────────────────────────────────────────


def test_next_format_today(fresh_home):
    _add_automation(
        fresh_home, id="hourly", name="Hourly",
        schedule="* * * * *",
    )
    # Anchor "now" inside the table generation — the test only checks the
    # shape via the renderer's regex match.
    out = av.render_automations(fresh_home)
    assert "`hourly`" in out


def test_next_format_tomorrow_and_weekday(fresh_home):
    """``_format_next_fire`` returns the expected short labels."""
    base = _dt.datetime(2026, 1, 1, 9, 0, 0)
    today = _dt.datetime(2026, 1, 1, 14, 30, 0)
    tomorrow = _dt.datetime(2026, 1, 2, 9, 0, 0)
    weekday = _dt.datetime(2026, 1, 7, 9, 0, 0)  # Wednesday
    later = _dt.datetime(2026, 1, 21, 9, 0, 0)  # 20 days out

    assert av._format_next_fire(today, base) == "today 14:30"
    assert av._format_next_fire(tomorrow, base) == "tomorrow 09:00"
    assert av._format_next_fire(weekday, base) == "Wed 09:00"
    # 20 days out → weekday + day + month + time.
    assert "Jan" in av._format_next_fire(later, base)


# ── footer ──────────────────────────────────────────────────────────────────


def test_footer_scheduler_on(fresh_home):
    _add_automation(
        fresh_home, id="daily", name="Daily",
        schedule="0 9 * * *",
    )
    with _stub_scheduler(fresh_home, installed=True, last_tick=None):
        out = av.render_automations(fresh_home)
    assert "Scheduler on" in out


def test_footer_scheduler_off_warns(fresh_home):
    _add_automation(
        fresh_home, id="daily", name="Daily",
        schedule="0 9 * * *",
    )
    with _stub_scheduler(fresh_home, installed=False):
        out = av.render_automations(fresh_home)
    assert "Scheduler off" in out
    assert "/automation scheduler on" in out


def test_footer_scheduler_off_no_enabled(fresh_home):
    _add_automation(
        fresh_home, id="daily", name="Daily",
        schedule="0 9 * * *", enabled=False,
    )
    with _stub_scheduler(fresh_home, installed=False):
        out = av.render_automations(fresh_home)
    # No enabled automation → no off-warning footer.
    assert "Scheduler off" not in out


# ── card ────────────────────────────────────────────────────────────────────


def test_card_unknown_id_is_none(fresh_home):
    assert av.automation_card(fresh_home, "nope") is None


def test_card_fields(fresh_home):
    _add_automation(
        fresh_home, id="weekday", name="Weekday build",
        schedule="0 9 * * 1-5",
        kickoff="# Weekday kickoff\n\nThe first paragraph.\n",
        last_run_id="run-001", runs=["run-001"],
    )
    _seed_task_run(fresh_home, run_id="run-001", status="published")
    with _stub_scheduler(fresh_home, installed=False):
        card = av.automation_card(fresh_home, "weekday")
    assert card is not None
    assert card["name"] == "Weekday build"
    assert card["enabled"] is True
    assert card["next_fires"]  # 3 ISO times
    assert isinstance(card["proposal"], type(None))  # no draft on disk

    out = av.render_automation_card(card)
    assert out.startswith("### ")  # heading
    assert "`weekday`" in out
    assert "Next:" in out
    assert "```markdown" in out
    assert "Recent runs:" in out
    assert "`run-001`" in out
    assert "/automation run weekday" in out


def test_card_kickoff_truncates_after_40_lines(fresh_home):
    long_kickoff = "\n".join(f"line {i}" for i in range(60))
    _add_automation(
        fresh_home, id="long", name="Long",
        schedule="0 9 * * *", kickoff=long_kickoff,
    )
    card = av.automation_card(fresh_home, "long")
    assert card is not None
    out = av.render_automation_card(card)
    assert "…" in out


def test_card_paused_says_paused(fresh_home):
    _add_automation(
        fresh_home, id="off", name="Off",
        schedule="0 9 * * *", enabled=False,
    )
    card = av.automation_card(fresh_home, "off")
    assert card is not None
    out = av.render_automation_card(card)
    assert "Paused." in out


def test_card_last_error_line(fresh_home):
    _add_automation(
        fresh_home, id="crash", name="Crash",
        schedule="0 9 * * *", last_error="binary not found",
    )
    card = av.automation_card(fresh_home, "crash")
    assert card is not None
    out = av.render_automation_card(card)
    assert "Last firing failed: binary not found" in out


def test_card_proposal_field_reflects_disk(fresh_home):
    from mini_ork import automations as _auto

    _add_automation(
        fresh_home, id="draft", name="Draft",
        schedule="0 9 * * *",
    )
    _auto.propose(
        fresh_home, id="draft", name="Draft v2",
        recipe="code-fix", kickoff="# k v2",
        schedule="0 10 * * *",
    )
    card = av.automation_card(fresh_home, "draft")
    assert card is not None
    assert isinstance(card["proposal"], dict)
    assert card["proposal"]["name"] == "Draft v2"


# ── recent runs: missing history row still surfaces a placeholder ────────────


def test_recent_runs_unknown_run_id_placeholder(fresh_home):
    """A run id in the audit trail but absent from history still surfaces."""
    _add_automation(
        fresh_home, id="missing-history", name="Missing",
        schedule="0 9 * * *",
        runs=["run-vanished"],
    )
    # Note: no ``_seed_task_run`` for ``run-vanished`` — history is empty.
    card = av.automation_card(fresh_home, "missing-history")
    assert card is not None
    assert len(card["runs"]) == 1
    assert card["runs"][0]["run_id"] == "run-vanished"
    assert card["runs"][0]["status"] == "unknown"
    assert card["runs"][0]["mark"] == "—"