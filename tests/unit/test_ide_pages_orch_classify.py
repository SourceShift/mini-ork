"""The classifications table — AC-009 (J9).

The tab used to report on the newest run that happened to hold a
``run_profile.json``: no history, no order, nothing to click. The requirement is
that the router's recent decisions are *reviewable* — every profiled run in one
table, ordered by doubt — and that a row shows **that** run's plan, which is the
half a "newest run" pane cannot do at any order.

The order is the requirement, not a decoration: the reason to open this tab is
a run the router got wrong, so the panel is written to be read from the top.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

from mini_ork.cli import board_cmd
from mini_ork.ide_pages import orch
from mini_ork.stores import migrate as mig

REPO = Path(__file__).resolve().parents[2]
LOW = 0.62
HIGH = 0.91


@pytest.fixture()
def home(tmp_path: Path) -> Path:
    h = tmp_path / "proj" / ".mini-ork"
    (h / "config" / "task_classes").mkdir(parents=True)
    rc, _out, err = mig.init_db(db=str(h / "state.db"), root=str(REPO))
    assert rc == 0, err
    return h


def _run(home: Path, run_id: str, *, created_at: int, status: str = "published",
         task_class: str = "code_fix", profile: dict | None = None,
         plan: dict | None = None, raw_profile: str = "") -> Path:
    """A run with a profile (and optionally a plan) on disk."""
    run_dir = home / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "kickoff.md").write_text(f"# {run_id}\n\nDo it.\n", encoding="utf-8")
    if raw_profile:
        (run_dir / "run_profile.json").write_text(raw_profile, encoding="utf-8")
    elif profile is not None:
        (run_dir / "run_profile.json").write_text(json.dumps(profile), encoding="utf-8")
    if plan is not None:
        (run_dir / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
    con = sqlite3.connect(home / "state.db")
    con.execute("INSERT OR REPLACE INTO task_runs (id, recipe, status, cost_usd, created_at, "
                "updated_at, task_class, kickoff_path, workflow_version) VALUES (?,?,?,?,?,?,?,?,?)",
                (run_id, "code-fix", status, 0.0, created_at, created_at, task_class,
                 str(run_dir / "kickoff.md"), "latest"))
    con.commit()
    con.close()
    return run_dir


def _plan(objective: str, action: str) -> dict:
    return {"objective": objective,
            "decomposition": [{"step": 1, "node_type": "implementer",
                               "action": f"{action}. Then more.", "target_file": "a.ts"}]}


def _table(page: dict) -> dict:
    found = [s for s in page["sections"] if s.get("type") == "table"]
    assert found, [s.get("title") for s in page["sections"]]
    return found[0]


def _row_texts(table: dict) -> list[list[str]]:
    return [[cell["t"] for cell in row["cells"]] for row in table["rows"]]


def _page(home: Path, capsys, *args: str) -> dict:
    rc = board_cmd.main(["page", "orch", "--tab", "planner", *args, "--home", str(home)], "")
    out = capsys.readouterr().out
    assert rc == 0
    return json.loads(out)


# ── every recent classification, ordered by doubt ──────────────────────────

def test_twelve_profiled_runs_all_appear_ordered_by_confidence(home: Path) -> None:
    """AC-009's own fixture: twelve runs, two of them below 0.7."""
    now = int(time.time())
    # Oldest first, so the two doubtful runs are neither the newest nor adjacent
    # to each other — an order that survives a newest-first sort by accident.
    for index in range(12):
        confidence = (LOW, 0.55)[index % 2] if index in (3, 8) else 0.80 + index / 100
        _run(home, f"run-{index:02d}", created_at=now - (12 - index) * 60,
             profile={"task_class": "code_fix", "recipe": "code-fix",
                      "confidence": confidence, "profile_status": "ready"})
    table = _table(orch.build(home, "planner", {}))
    rows = _row_texts(table)
    assert len(rows) == 12
    confidences = [float(row[3]) for row in rows]
    assert confidences == sorted(confidences), "the doubtful rows are the ones at the top"
    assert confidences[:2] == [0.55, LOW]
    assert "2 of 12 below confidence 0.7" in table["note"]


def test_a_profile_with_no_confidence_is_not_treated_as_the_worst(home: Path) -> None:
    """The profile gate writes confidence; a gate-skipping recipe does not."""
    now = int(time.time())
    _run(home, "run-nogate", created_at=now, task_class="epic",
         profile={"task_class": "epic", "recipe": "epic-runner", "profile_status": "ready"})
    _run(home, "run-known", created_at=now - 60,
         profile={"task_class": "code_fix", "recipe": "code-fix", "confidence": 0.4,
                  "profile_status": "ready"})
    rows = _row_texts(_table(orch.build(home, "planner", {})))
    assert [row[3] for row in rows] == ["0.40", "—"]
    assert rows[-1][0].startswith("run-nogate")
    assert "1 of 2 below" in _table(orch.build(home, "planner", {}))["note"]


def test_the_row_says_the_task_class_the_outcome_and_the_recipe(home: Path) -> None:
    now = int(time.time())
    _run(home, "run-1791000000-aaaaaa", created_at=now, status="failed",
         task_class="code_fix",
         profile={"task_class": "ui_change", "recipe": "code-fix", "confidence": HIGH,
                  "profile_status": "ready"})
    row = _row_texts(_table(orch.build(home, "planner", {})))[0]
    # The id column is the short tail the Decide tab shows too — the full id
    # rides on the row's Review action and the detail's Open run.
    assert row == ["00000-aaaaaa", "ui_change", "code-fix", "0.91", "✗ failed"]
    assert _table(orch.build(home, "planner", {}))["rows"][0]["do"] == \
        {"set": {"run_id": "run-1791000000-aaaaaa"}}


def test_the_task_class_falls_back_to_the_row_when_the_profile_omits_it(home: Path) -> None:
    _run(home, "run-x", created_at=int(time.time()), task_class="bug_fix",
         profile={"recipe": "code-fix", "confidence": HIGH, "profile_status": "ready"})
    assert _row_texts(_table(orch.build(home, "planner", {})))[0][1] == "bug_fix"


def test_the_three_outcomes_read_as_landed_lost_and_running() -> None:
    assert _outcome("published") == ("✓ published", "green")
    assert _outcome("rolled_back") == ("✗ rolled_back", "red")
    assert _outcome("failed") == ("✗ failed", "red")
    assert _outcome("executing") == ("· executing", "sub")


def _outcome(status: str) -> tuple[str, str]:
    cell = orch._outcome_cell(status)
    return (cell["t"], cell["c"])


# ── selecting a row shows that run's plan ──────────────────────────────────

def test_the_row_click_and_its_button_are_the_same_pick(home: Path) -> None:
    """One action, two affordances: a click anywhere, or the named button."""
    _run(home, "run-pick", created_at=int(time.time()),
         profile={"recipe": "code-fix", "confidence": HIGH, "profile_status": "ready"})
    row = _table(orch.build(home, "planner", {}))["rows"][0]
    assert row["do"] == {"set": {"run_id": "run-pick"}}
    assert row["acts"][0]["label"] == "Review"
    assert row["acts"][0]["do"] == row["do"]


def test_selecting_a_row_shows_that_runs_plan_not_the_newest_runs(home: Path) -> None:
    now = int(time.time())
    _run(home, "run-newest", created_at=now,
         profile={"recipe": "code-fix", "confidence": HIGH, "profile_status": "ready"},
         plan=_plan("Newest objective", "Edit newest.ts"))
    _run(home, "run-older", created_at=now - 600,
         profile={"recipe": "epic-runner", "confidence": 0.41, "profile_status": "ready"},
         plan=_plan("Older objective", "Edit older.ts"))

    # Default: the least confident row, detailed below it.
    default = orch.build(home, "planner", {})["sections"]
    assert default[1]["items"][3]["v"] == "0.41"
    assert default[2]["items"][0]["t"] == "1 · Edit older.ts"
    assert default[2]["note"] == "Older objective"

    # Picked: the newer, higher-confidence run — the one that is *not* the
    # default, so this proves the click drives the panel.
    picked = orch.build(home, "planner", {"run_id": "run-newest"})["sections"]
    assert picked[2]["items"][0]["t"] == "1 · Edit newest.ts"
    assert picked[2]["note"] == "Newest objective"
    assert picked[1]["title"].endswith("run-newest")


def test_the_selected_row_is_the_one_marked_in_the_table(home: Path) -> None:
    now = int(time.time())
    _run(home, "run-a", created_at=now,
         profile={"recipe": "code-fix", "confidence": 0.2, "profile_status": "ready"})
    _run(home, "run-b", created_at=now - 60,
         profile={"recipe": "code-fix", "confidence": HIGH, "profile_status": "ready"})
    table = _table(orch.build(home, "planner", {"run_id": "run-b"}))
    marked = [row for row in table["rows"] if row["sel"]]
    assert len(marked) == 1 and marked[0]["cells"][0]["t"] == "run-b"
    # And by default the marked row is the first one, so the panel cannot detail
    # a run the table is not pointing at.
    default = _table(orch.build(home, "planner", {}))
    assert default["rows"][0]["sel"] and not default["rows"][1]["sel"]


def test_an_id_that_names_no_row_falls_back_rather_than_being_read(home: Path) -> None:
    """``--arg run_id=`` is a string from the IDE, not a path."""
    _run(home, "run-only", created_at=int(time.time()),
         profile={"recipe": "code-fix", "confidence": HIGH, "profile_status": "ready"})
    for bogus in ("../../etc", "run-elsewhere", ""):
        page = orch.build(home, "planner", {"run_id": bogus})
        assert page["sections"][1]["title"].endswith("run-only"), bogus


def test_a_run_with_no_plan_artifact_still_details_its_classification(home: Path) -> None:
    _run(home, "run-noplan", created_at=int(time.time()),
         profile={"recipe": "code-fix", "confidence": 0.5, "profile_status": "ready"})
    sections = orch.build(home, "planner", {})["sections"]
    assert sections[1]["type"] == "kv"
    assert not [s for s in sections if str(s.get("title") or "").startswith("Plan ·")]
    assert sections[-1]["title"] == "Planner questions"


# ── the tab, end to end through the argv the IDE sends ─────────────────────

def test_the_argv_the_ide_sends_selects_the_row(home: Path, capsys) -> None:
    now = int(time.time())
    _run(home, "run-1", created_at=now,
         profile={"recipe": "code-fix", "confidence": HIGH, "profile_status": "ready"},
         plan=_plan("First objective", "Edit one.ts"))
    _run(home, "run-2", created_at=now - 60,
         profile={"recipe": "code-fix", "confidence": 0.3, "profile_status": "ready"},
         plan=_plan("Second objective", "Edit two.ts"))
    page = _page(home, capsys, "--arg", "run_id=run-1")
    detail = [s for s in page["sections"] if str(s.get("title") or "").startswith("Plan ·")][0]
    assert detail["note"] == "First objective"
    assert page["args"] == {"run_id": "run-1"}


def test_an_unreadable_profile_is_named_rather_than_silently_dropped(home: Path, capsys) -> None:
    """Same rule as the Decide tab: the run is absent, and the reason is said."""
    _run(home, "run-broken", created_at=int(time.time()), raw_profile="{ not json")
    page = _page(home, capsys)
    assert "profile:run-broken" in page["errors"]
    assert [s["type"] for s in page["sections"]][-1] == "callout"
    assert "profile:run-broken" in page["sections"][-1]["text_md"]


def test_a_home_with_no_profile_at_all_says_so(home: Path) -> None:
    sections = orch.build(home, "planner", {})["sections"]
    assert [s["type"] for s in sections] == ["list"]
    assert sections[0]["items"][0]["t"] == "No run has been classified yet"
