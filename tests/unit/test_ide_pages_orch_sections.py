"""The Orchestrator page's P0 wiring (FR-001 … FR-006), section by section.

The requirement P0 left open was "a unit test per section, asserting the new
keys exist and that a missing source degrades rather than raising". Both halves
matter here, because every one of these sections reads something outside the
page — a features store, a thread store, a lint report, ContextNest, the OS
scheduler — and the page is built on a timer in an IDE that must never go blank
because one of them is absent or broken.

The stale shapes P1 changed are asserted in ``test_ide_pages_orch.py``, which is
left alone per the working agreement; this file is the replacement for the P0
sections only, and it names each section's keys rather than the tab's order.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

from mini_ork.cli import board_cmd
from mini_ork.ide_pages import orch, spec
from mini_ork.stores import migrate as mig

REPO = Path(__file__).resolve().parents[2]
TITLE = "Fix the login loop"


@pytest.fixture()
def home(tmp_path: Path) -> Path:
    h = tmp_path / "proj" / ".mini-ork"
    (h / "config" / "task_classes").mkdir(parents=True)
    rc, _out, err = mig.init_db(db=str(h / "state.db"), root=str(REPO))
    assert rc == 0, err
    return h


def _run(home: Path, run_id: str, *, status: str = "published", title: str = TITLE,
         profile: dict | None = None, run_profile_raw: str = "",
         kickoff_path: Path | None = None) -> Path:
    run_dir = home / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "kickoff.md").write_text(f"# {title}\n\nDo the thing.\n", encoding="utf-8")
    if run_profile_raw:
        (run_dir / "run_profile.json").write_text(run_profile_raw, encoding="utf-8")
    elif profile is not None:
        (run_dir / "run_profile.json").write_text(json.dumps(profile), encoding="utf-8")
    con = sqlite3.connect(home / "state.db")
    now = int(time.time())
    con.execute("INSERT OR REPLACE INTO task_runs (id, recipe, status, cost_usd, created_at, "
                "updated_at, task_class, kickoff_path, workflow_version) VALUES (?,?,?,?,?,?,?,?,?)",
                (run_id, "code-fix", status, 0.4, now, now, "code_fix",
                 str(kickoff_path or (run_dir / "kickoff.md")), "latest"))
    con.commit()
    con.close()
    return run_dir


def _kickoff(home: Path, name: str, text: str) -> Path:
    folder = home / "kickoffs"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_text(text, encoding="utf-8")
    return path


def _section(page: dict, kind: str, title: str) -> dict:
    found = [s for s in page["sections"]
             if s.get("type") == kind and str(s.get("title") or "").startswith(title)]
    assert found, [(s.get("type"), s.get("title")) for s in page["sections"]]
    return found[0]


# ── FR-001 — a broken source is visible on the page, not only in the JSON ──

def test_no_errors_draws_no_banner() -> None:
    assert spec.errors_callout({}) is None


def test_each_error_is_one_line_naming_its_source() -> None:
    banner = spec.errors_callout({"reaper": "malformed state.db", "profile:run-1": "bad json"})
    assert banner is not None and banner["type"] == "callout"
    assert "**reaper** — malformed state.db" in banner["text_md"]
    assert "**profile:run-1** — bad json" in banner["text_md"]


def test_the_banner_rides_after_the_page_it_belongs_to(home: Path, capsys) -> None:
    """The strip is first and the banner last — both outside the tab's own sections.

    They ride at the two ends of the list for one reason: whichever tab is open,
    what is blocked on the reader comes before the tab's content and what could
    not be read sits after it. A ninth section of the page would be neither.
    """
    _run(home, "run-broken", run_profile_raw="{ not json")
    rc = board_cmd.main(["page", "orch", "--tab", "session", "--home", str(home)], "")
    page = json.loads(capsys.readouterr().out)
    assert rc == 0
    kinds = [s.get("type") for s in page["sections"]]
    assert kinds[0] == "triage" and kinds[-1] == "callout"
    assert "profile:run-broken" in page["sections"][-1]["text_md"]


# ── FR-002 — a kickoff row says what the linter said, and links its log ────

def test_a_finding_reads_as_glyph_message_and_fix() -> None:
    assert orch._finding_text({"sev": "error", "msg": "The kickoff is empty.",
                               "fix": "Write the kickoff first."}) == \
        "✗ The kickoff is empty. — Write the kickoff first."
    assert orch._finding_text({"sev": "warn", "msg": "No title.", "fix": "Add one."}) == \
        "⚠ No title. — Add one."


def test_a_finding_without_a_fix_keeps_only_what_it_has() -> None:
    """No invented fix, and no dangling separator."""
    assert orch._finding_text({"sev": "warn", "msg": "No fix here."}) == "⚠ No fix here."


def test_a_kickoff_row_carries_the_lint_text_and_the_recipe(home: Path) -> None:
    _kickoff(home, "no-success.md",
             "---\nrecipe: code-fix\n---\n# Fix the thing\n\nNo success section here.\n")
    row = orch._scan_kickoffs(home)[0]
    parts = orch._kickoff_parts(row)
    assert parts[0] == "saved"
    assert parts[1] == f"lint: {orch._finding_text(row['findings'][0])}"
    assert parts[2] == "code-fix"


def test_a_kickoff_without_a_declared_recipe_says_the_recipe_is_the_default(home: Path) -> None:
    """The page resolved a recipe the file did not declare — and says so."""
    _kickoff(home, "plain.md", "# Fix the thing\n\nNo front matter.\n## Success\n\n- `x`\n")
    parts = orch._kickoff_parts(orch._scan_kickoffs(home)[0])
    assert parts[2] == "code-fix (default)"


def test_the_log_button_appears_only_when_there_is_a_log(home: Path) -> None:
    """A button that opens nothing is worse than no button."""
    kickoff = _kickoff(home, "draft.md",
                       "---\nrecipe: code-fix\n---\n# Fix the thing\n\n## Success\n\n- `x`\n")
    # A row links a run through ``task_runs.kickoff_path`` — the file it came
    # from, not the run's own copy of it.
    _run(home, "run-2", kickoff_path=kickoff)
    without = orch._kickoffs_section(home)["items"][0]["acts"]
    assert "Log" not in [a["label"] for a in without]

    (home / "runs" / "run-2" / "execute.log").write_text("tick\n", encoding="utf-8")
    assert orch._run_log(home, "run-2") == str(home / "runs" / "run-2" / "execute.log")
    with_log = orch._kickoffs_section(home)["items"][0]["acts"]
    log = next(a for a in with_log if a["label"] == "Log")
    assert log["do"] == {"path": str(home / "runs" / "run-2" / "execute.log")}
    # The row still links the kickoff it was written from, log or no log.
    assert str(kickoff) in [a["do"].get("path") for a in with_log if a["label"] == "Open"]


def test_a_kickoff_the_linter_rejects_still_gets_its_row(home: Path) -> None:
    """A lint error is advice, not a gate — the row renders, marked red."""
    _kickoff(home, "empty.md", "   \n")
    section = orch._kickoffs_section(home)
    assert section["items"][0]["mc"] == "red"
    assert "lint: ✗ The kickoff is empty." in section["items"][0]["sub"]


# ── FR-003 — three pickers, each naming where its value came from ──────────

def test_the_session_tab_has_the_three_pickers(home: Path) -> None:
    page = orch.build(home, "session", {})
    assert [s["title"] for s in page["sections"] if s["type"] == "chips"] == \
        ["Orchestrator lane", "Recipe", "Workspace"]


def test_each_picker_writes_its_own_variable_and_says_which_layer_won(home: Path) -> None:
    """The write is the seam the next render reads back."""
    page = orch.build(home, "session", {})
    chips = [s for s in page["sections"] if s["type"] == "chips"]
    assert len(chips) == 3
    for section, name in zip(chips, ("MO_ORCHESTRATOR_LANE", "MO_ACP_RECIPE", "MO_WORKSPACE_MODE"),
                             strict=True):
        assert "from built-in default" in section["note"], section["title"]
        marked = [i for i in section["items"] if i.get("on")]
        assert len(marked) == 1, f"{section['title']} marks no active chip"
        assert marked[0]["do"]["cli"][:3] == ["features", "set-env", name]
        assert marked[0]["do"]["home"] is False, "the features parser takes no --home"


def test_an_unreadable_features_store_does_not_blank_the_picker(home: Path, monkeypatch) -> None:
    """The picker degrades to what the shell says rather than raising."""
    from mini_ork.features.store import store_path

    store = store_path(home)
    store.parent.mkdir(parents=True, exist_ok=True)
    store.mkdir()  # a directory where a file belongs: the read cannot succeed
    monkeypatch.setenv("MO_ACP_RECIPE", "refactor")
    assert orch._env_source(home, "MO_ACP_RECIPE") in ("shell environment", "built-in default")
    page = orch.build(home, "session", {})
    recipe = next(s for s in page["sections"] if s.get("title") == "Recipe")
    assert any(i.get("on") for i in recipe["items"])


def test_the_lane_picker_reports_the_model_override_first(home: Path, monkeypatch) -> None:
    """``default_lane``'s own precedence, not a second derivation of it."""
    monkeypatch.setenv("MO_ACP_DEFAULT_MODEL", "claude-opus-4-7")
    assert orch._lane_source(home) == "MO_ACP_DEFAULT_MODEL"
    monkeypatch.delenv("MO_ACP_DEFAULT_MODEL")
    assert orch._lane_source(home) in ("lane map", "built-in default")


# ── FR-004 — history rows link what they say they link ────────────────────

def test_a_thread_row_opens_the_thread_and_a_run_row_opens_the_run(home: Path) -> None:
    from mini_ork.acp.threads import ThreadStore

    _run(home, "run-1791000000-aaaaaa", title=TITLE)
    store = ThreadStore(home)
    store.append("orch-1791000000-t1", {"type": "meta", "cwd": str(home.parent)})
    store.append("orch-1791000000-t1", {"type": "user", "text": "Add dark mode"})
    store.append("orch-1791000000-t1", {"type": "costs", "costs": {"turn": 0.25, "run": 0.5}})

    table = orch._history(home)
    rows = {str(r["cells"][0]["t"]): r for r in table["rows"]}
    thread, run = rows["Add dark mode"], rows[TITLE]
    assert thread["cells"][1]["t"] == "▤ thread"
    assert thread["cells"][2]["t"] == "$0.75"
    assert thread["do"] == {"page": "thread", "args": {"thread": "orch-1791000000-t1"}}
    assert run["cells"][1]["t"] == "◇ run"
    assert run["do"] == {"run": "run-1791000000-aaaaaa", "title": TITLE}


def test_an_empty_history_says_so_rather_than_rendering_nothing(home: Path) -> None:
    table = orch._history(home)
    assert [r["cells"][0]["t"] for r in table["rows"]] == ["No threads or runs yet"]
    assert table["rows"][0]["do"] is None, "a placeholder is not a link"
    assert "opens that thread" in table["note"]


# ── FR-005 — a button that opens a file must not open the wrong one ────────

def test_only_an_absolute_path_gets_an_open_button() -> None:
    assert orch._open_abs("/tmp/a.py") == {"path": "/tmp/a.py"}
    assert orch._open_abs("src/a.py") is None
    assert orch._open_abs("") is None and orch._open_abs(None) is None


def test_stopping_a_principal_carries_the_confirm_and_the_yes() -> None:
    """``--yes`` because the IDE has no tty; the confirm is the gate it sees."""
    action = orch._concord_stop("p-1", "p-1")
    assert action["cli"] == ["concord", "stop", "p-1", "--yes"]
    assert action["home"] is False, "the concord parser takes no --home"
    assert "Kills its process group." in action["confirm"]


def test_coordination_degrades_to_the_page_without_contextnest(home: Path) -> None:
    """ContextNest is a separate process; the tab renders without it."""
    assert isinstance(orch._coord(home, int(time.time())), list)


# ── FR-006 — the intake chain, one row per link ────────────────────────────

def _statuses(*specs: tuple[str, bool, bool, dict]) -> list:
    from mini_ork.intake.poll import SourceStatus
    from mini_ork.intake.trigger import Trigger

    return [SourceStatus(source=name, registered=registered, enabled=enabled,
                         trigger=Trigger(**rules))
            for name, registered, enabled, rules in specs]


@pytest.fixture()
def scheduler(monkeypatch):
    """State the scheduler's answer rather than inheriting the machine's."""
    from mini_ork import automations

    def _set(installed: bool, *, explode: Exception | None = None) -> None:
        if explode is not None:
            monkeypatch.setattr(automations, "scheduler_status",
                                lambda home: (_ for _ in ()).throw(explode))
        else:
            monkeypatch.setattr(automations, "scheduler_status",
                                lambda home: {"installed": installed, "last_tick": "10:00"})
    return _set


def test_every_row_state_is_one_the_renderer_knows(home: Path, scheduler) -> None:
    scheduler(True)
    checks = orch._chain_health(home, _statuses(
        ("github", True, True, {"label": "agent"}), ("slack", False, False, {})))
    assert {row["state"] for row in checks["rows"]} <= {"pass", "na", "fail"}, \
        "`pending` would render as an unfinished check on a chain that is finished"


def test_a_registered_source_gets_a_trigger_row_and_an_unregistered_one_does_not(
        home: Path, scheduler) -> None:
    scheduler(True)
    checks = orch._chain_health(home, _statuses(
        ("github", True, True, {"label": "agent"}), ("slack", False, False, {})))
    names = [row["name"] for row in checks["rows"]]
    assert "github source" in names and "github trigger" in names
    assert "slack source" in names and "slack trigger" not in names


def test_a_registered_source_with_no_rule_fails_its_trigger_row(home: Path, scheduler) -> None:
    """The one way the chain breaks silently: a trigger that matches nothing."""
    scheduler(True)
    checks = orch._chain_health(home, _statuses(("github", True, True, {})))
    row = next(r for r in checks["rows"] if r["name"] == "github trigger")
    assert row["state"] == "fail" and row["detail"] == "no trigger configured"
    assert checks["summary"]["failing"] == 1


def test_a_source_that_is_switched_off_is_na_not_a_fault(home: Path, scheduler) -> None:
    """Someone decided that; the summary must not read it as a problem."""
    scheduler(True)
    checks = orch._chain_health(home, _statuses(("github", True, False, {})))
    assert next(r for r in checks["rows"] if r["name"] == "github source")["state"] == "na"
    assert checks["summary"]["failing"] == 0


def test_the_scheduler_is_the_last_row_and_says_when_it_last_ticked(
        home: Path, scheduler) -> None:
    scheduler(True)
    checks = orch._chain_health(home, _statuses(("github", True, True, {"label": "agent"})))
    assert checks["rows"][-1]["name"] == "Scheduler"
    assert checks["rows"][-1]["state"] == "pass"
    assert "last tick 10:00" in checks["rows"][-1]["detail"]


def test_a_scheduler_that_cannot_be_read_fails_rather_than_passes(
        home: Path, scheduler) -> None:
    """Fail closed: an unreadable chain is not a passing one."""
    scheduler(False, explode=OSError("launchctl is not on PATH"))
    checks = orch._chain_health(home, _statuses(("github", True, True, {"label": "agent"})))
    row = checks["rows"][-1]
    assert row["name"] == "Scheduler" and row["state"] == "fail"
    assert "OSError" in row["detail"]


def test_the_chain_renders_on_the_intake_tab_at_ide_spec_two(home: Path, monkeypatch,
                                                             scheduler) -> None:
    """FR-006 is emitted only for the IDE that draws a checklist."""
    scheduler(True)
    monkeypatch.setattr(orch, "_source_statuses",
                        lambda home: _statuses(("github", True, True, {"label": "agent"})))
    monkeypatch.setenv("MINI_ORK_IDE_SPEC", "2")
    page = orch.build(home, "intake", {})
    assert _section(page, "checks", "Intake chain")["summary"]["failing"] == 0

    monkeypatch.setenv("MINI_ORK_IDE_SPEC", "1")
    older = orch.build(home, "intake", {})
    assert not [s for s in older["sections"] if s.get("type") == "checks"]
