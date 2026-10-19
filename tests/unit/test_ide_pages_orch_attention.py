"""The attention strip (FR-040).

One strip, prepended to every tab of the orchestrator page, whose numbers are
the tabs' own numbers: each input is read through the helper its own tab reads
it with, so the strip and the tab cannot disagree.

Every test drives the real ``board page`` argv rather than ``orch.build`` — the
strip is assembled in ``board_cmd.build_payload``, after the run reaper's error
has joined the page's error map, and a direct call would not exercise that.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

from mini_ork.ide_pages import orch
from mini_ork.stores import migrate as mig

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture()
def home(tmp_path: Path) -> Path:
    h = tmp_path / "proj" / ".mini-ork"
    h.mkdir(parents=True)
    rc, _out, err = mig.init_db(db=str(h / "state.db"), root=str(REPO))
    assert rc == 0, err
    return h


def _chain(monkeypatch, *sources: tuple[str, dict], scheduler_installed: bool = True) -> None:
    """State the intake chain rather than inheriting the machine's.

    Two of its links depend on things a temporary home cannot have: the source
    list comes from the user's ``pm.yaml``, and the scheduler tick is a
    LaunchAgent under the user's home. Both go through reads the strip and the
    intake tab share — ``orch._source_statuses`` and
    ``automations.scheduler_status`` — so patching those two states the chain.

    It also mirrors the real machine: a home with no tick installed has a
    permanently non-empty strip, because the chain that starts runs really is
    incomplete. That is the honest answer, and it is why every test here says
    which chain it means.
    """
    from mini_ork import automations
    from mini_ork.intake.poll import SourceStatus
    from mini_ork.intake.trigger import Trigger

    monkeypatch.setattr(orch, "_source_statuses", lambda home: [
        SourceStatus(source=name, registered=True, enabled=True, trigger=Trigger(**rules))
        for name, rules in sources])
    if scheduler_installed:
        monkeypatch.setattr(automations, "scheduler_status", lambda home: {"installed": True})


@pytest.fixture()
def chain_intact(monkeypatch) -> None:
    """A chain with every link passing."""
    _chain(monkeypatch, ("github", {"label": "agent"}))


def _page(home: Path, tab: str, capsys) -> dict:
    from mini_ork.cli import board_cmd

    rc = board_cmd.main(["page", "orch", "--tab", tab, "--home", str(home)], "")
    assert rc == 0
    return json.loads(capsys.readouterr().out)


def _section(page: dict, kind: str) -> dict | None:
    return next((s for s in page["sections"] if s.get("type") == kind), None)


def _strip(page: dict) -> dict:
    """The strip, which every test below expects to be there."""
    strip = _section(page, "triage")
    assert strip is not None, [s.get("type") for s in page["sections"]]
    return strip


def _pills(strip: dict) -> dict[str, str]:
    return {p["t"]: p["c"] for p in strip["counts"]}


def _button(strip: dict, label: str) -> dict:
    return next(b for b in strip["actions"] if b["label"] == label)


def _run(home: Path, run_id: str, *, status: str = "published", race_lane: str = "",
         created: int | None = None, title: str = "Debounce the search box",
         workspace: bool = True, profile: dict | None = None,
         run_profile_raw: str = "") -> Path:
    run_dir = home / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "kickoff.md").write_text(f"# {title}\n\nDo the thing.\n", encoding="utf-8")
    if race_lane:
        (run_dir / "config").mkdir(exist_ok=True)
        (run_dir / "config" / "agents.race.yaml").write_text(
            f"lanes:\n  implementer: {race_lane}\n", encoding="utf-8")
    if run_profile_raw:
        (run_dir / "run_profile.json").write_text(run_profile_raw, encoding="utf-8")
    elif profile is not None:
        (run_dir / "run_profile.json").write_text(json.dumps(profile), encoding="utf-8")
    con = sqlite3.connect(home / "state.db")
    now = created if created is not None else int(time.time())
    con.execute("INSERT OR REPLACE INTO task_runs (id, recipe, status, cost_usd, created_at, "
                "updated_at, task_class, kickoff_path, workflow_version) VALUES (?,?,?,?,?,?,?,?,?)",
                (run_id, "code-fix", status, 0.4, now, now, "code_fix",
                 str(run_dir / "kickoff.md"), "latest"))
    con.commit()
    con.close()
    if workspace:
        wt = home / "worktrees" / run_id
        wt.mkdir(parents=True, exist_ok=True)
        (home / "worktrees" / f"{run_id}.json").write_text(json.dumps({
            "run_id": run_id, "path": str(wt), "branch": f"mini-ork/{run_id}",
            "base_branch": "main", "base_sha": "0" * 40, "project": str(home.parent),
            "adopted": False, "home": str(home),
        }), encoding="utf-8")
    return run_dir


def _parked(home: Path, run_id: str) -> Path:
    """A run the profile gate parked on an unanswered question."""
    return _run(home, run_id, status="executing", profile={
        "task_class": "ui_change", "recipe": "code-fix",
        "profile_status": "needs_answers", "human_questions": ["Apply to API keys too?"]})


def _race(home: Path, run_ids: tuple[str, ...], *,
          lanes: tuple[str, ...] = ("codex", "glm", "qwen"),
          title: str = "Debounce the search box") -> None:
    """Two or more runs on one kickoff, each marked a contestant by its race config."""
    for lane, run_id in zip(lanes, run_ids, strict=False):
        _run(home, run_id, race_lane=lane, title=title)


# ── absent when there is nothing to say ────────────────────────────────────

def test_a_home_with_nothing_wrong_gets_no_strip(home: Path, chain_intact, capsys) -> None:
    """Not an empty strip: no section at all."""
    page = _page(home, "intake", capsys)
    assert _section(page, "triage") is None
    assert page["sections"], "the tab still draws itself"


def test_a_home_with_nothing_wrong_gets_no_strip_on_any_tab(home: Path, chain_intact,
                                                           capsys) -> None:
    for tab, _label in orch.TABS:
        assert _section(_page(home, tab, capsys), "triage") is None, tab


# ── reads waiting on an answer ─────────────────────────────────────────────

def test_a_parked_run_is_the_strip_and_its_button_goes_to_the_tab_it_is_on(
        home: Path, chain_intact, capsys) -> None:
    _parked(home, "run-parked")
    page = _page(home, "races", capsys)
    strip = _strip(page)
    assert page["sections"][0] is strip, "the strip is first, above the tab's own sections"
    assert _pills(strip) == {"1 waiting": "yellow"}
    assert strip["detail"] == "1 run waiting on an answer"
    assert strip["text"] == "1 thing needs you"
    assert strip["icon"] == "✋" and strip["tone"] == "yellow"
    assert _button(strip, "Answer")["do"] == {"page": "orch", "tab": "decide"}


def test_the_strip_rides_every_tab(home: Path, chain_intact, capsys) -> None:
    _parked(home, "run-parked")
    for tab, _label in orch.TABS:
        assert _pills(_strip(_page(home, tab, capsys))) == {"1 waiting": "yellow"}, tab


def test_two_parked_runs_read_as_two(home: Path, chain_intact, capsys) -> None:
    _parked(home, "run-a")
    _parked(home, "run-b")
    strip = _strip(_page(home, "intake", capsys))
    assert _pills(strip) == {"2 waiting": "yellow"}
    assert strip["text"] == "2 things need you"
    assert strip["detail"] == "2 runs waiting on an answer"


def test_an_answered_question_is_not_waiting(home: Path, chain_intact, capsys) -> None:
    """The same predicate the Decide tab draws its entries with."""
    run_dir = _parked(home, "run-parked")
    (run_dir / "profile-answers.json").write_text(
        json.dumps({"Apply to API keys too?": "yes"}), encoding="utf-8")
    assert _section(_page(home, "intake", capsys), "triage") is None


# ── undecided races ────────────────────────────────────────────────────────

def test_a_race_holding_two_worktrees_is_undecided(home: Path, chain_intact, capsys) -> None:
    _race(home, ("run-a", "run-b"))
    strip = _strip(_page(home, "session", capsys))
    assert _pills(strip) == {"1 race": "orange"}
    assert strip["detail"] == "1 undecided race"
    assert _button(strip, "Decide")["do"] == {"page": "orch", "tab": "races"}


def test_a_decided_race_is_not_undecided(home: Path, chain_intact, capsys) -> None:
    """A keep merges the winner's worktree and discards the others, so a race
    that is over holds no more than one — and the strip stops asking."""
    _race(home, ("run-a", "run-b"))
    for victim in ("run-a", "run-b"):
        (home / "worktrees" / f"{victim}.json").unlink()
    assert _section(_page(home, "session", capsys), "triage") is None


def test_a_lone_run_holding_a_worktree_is_not_a_race(home: Path, chain_intact, capsys) -> None:
    _run(home, "run-only", race_lane="codex")
    assert _section(_page(home, "session", capsys), "triage") is None


# ── the intake chain ───────────────────────────────────────────────────────

def test_an_unconfigured_trigger_is_a_failing_link_named_in_the_strip(
        home: Path, monkeypatch, capsys) -> None:
    """A registered, enabled source with no rule selects nothing — and the
    checklist says so, so the strip must too."""
    _chain(monkeypatch, ("github", {}))
    strip = _strip(_page(home, "session", capsys))
    assert _pills(strip) == {"1 chain link": "red"}
    assert strip["detail"] == "intake chain: github trigger"
    assert strip["tone"] == "red"
    assert _button(strip, "Fix the chain")["do"] == {"page": "orch", "tab": "intake"}


def test_a_missing_scheduler_is_a_failing_link(home: Path, monkeypatch, capsys) -> None:
    """The chain's last link, unpatched: this machine has no tick for this home."""
    from mini_ork import automations

    _chain(monkeypatch, ("github", {"label": "agent"}), scheduler_installed=False)
    assert not automations.scheduler_status(home).get("installed"), \
        "this test is about a machine without one"
    assert _strip(_page(home, "session", capsys))["detail"] == "intake chain: Scheduler"


# ── the page's own errors ──────────────────────────────────────────────────

def test_an_unreadable_profile_is_a_strip_item_and_a_banner_line(
        home: Path, chain_intact, capsys) -> None:
    """The strip is built before the banner, so its discovery reaches both.

    A profile the reader cannot parse is only found by asking — and the strip
    asks on the session tab, which would otherwise never look.
    """
    _run(home, "run-broken", run_profile_raw="{ not json")
    page = _page(home, "session", capsys)
    strip = _strip(page)
    assert _pills(strip) == {"1 error": "red"}
    assert strip["detail"] == "could not be read: profile:run-broken"
    banner = _section(page, "callout")
    assert banner is not None
    assert "profile:run-broken" in banner["text_md"]


def test_the_error_item_re_reads_the_tab_you_are_on(home: Path, chain_intact, capsys) -> None:
    """A read failure is answered by reading again — and a page action without a
    tab would only focus the tab, not reload it."""
    _run(home, "run-broken", run_profile_raw="{ not json")
    strip = _strip(_page(home, "races", capsys))
    assert _button(strip, "Re-read")["do"] == {"page": "orch", "tab": "races"}
    assert _button(strip, "Re-read")["kind"] == "ghost"


# ── the counts are the tabs' counts ────────────────────────────────────────

def test_the_waiting_count_is_the_decide_tabs_own_count(home: Path, chain_intact,
                                                        capsys) -> None:
    _parked(home, "run-a")
    _parked(home, "run-b")
    _run(home, "run-ready", profile={"profile_status": "ready", "human_questions": []})
    page = _page(home, "decide", capsys)
    panels = [s for s in page["sections"]
              if str(s.get("title", "")).startswith("Blocked on you")]
    assert panels, "the tab draws the panel"
    drawn = sum(len(s.get("items") or []) for s in panels)
    assert "2 waiting" in _pills(_strip(page)) and drawn == 2


def test_the_race_count_is_the_races_holding_more_than_one_worktree(
        home: Path, chain_intact, capsys) -> None:
    _race(home, ("run-a", "run-b"))
    _race(home, ("run-c", "run-d"), title="Some other task entirely")
    page = _page(home, "races", capsys)
    assert _pills(_strip(page)) == {"2 races": "orange"}
    drawn = [s for s in page["sections"] if str(s.get("title", "")).startswith("Race · ")]
    assert len(drawn) == 2
    assert sum(len(s["rows"]) for s in drawn) == 4


def test_the_chain_count_is_the_checklists_own_failing_count(home: Path, monkeypatch,
                                                             capsys) -> None:
    """The new IDE draws the checklist, so both numbers are on screen at once."""
    monkeypatch.setenv("MINI_ORK_IDE_SPEC", "2")
    _chain(monkeypatch, ("github", {}))
    page = _page(home, "intake", capsys)
    checks = _section(page, "checks")
    assert checks is not None
    failing = [row for row in checks["rows"] if row["state"] == "fail"]
    assert checks["summary"]["failing"] == len(failing) == 1
    assert _pills(_strip(page)) == {"1 chain link": "red"}


def test_the_strip_carries_one_button_per_item_and_none_for_what_is_empty(
        home: Path, chain_intact, capsys) -> None:
    _parked(home, "run-parked")
    _race(home, ("run-a", "run-b"))
    _run(home, "run-broken", run_profile_raw="{ not json")
    strip = _strip(_page(home, "session", capsys))
    assert [b["label"] for b in strip["actions"]] == ["Answer", "Decide", "Re-read"]
    assert [b["kind"] for b in strip["actions"]] == ["primary", "default", "ghost"]
    assert strip["text"] == "3 things need you"


# ── the orchestrator watcher's signals ─────────────────────────────────────

def _signal(home: Path, run_id: str, *, event_type: str = "orchestrator.signal",
            payload: dict | None = None, age_s: int = 60) -> None:
    """One watcher event, ``age_s`` old, on a run the caller already created."""
    con = sqlite3.connect(home / "state.db")
    try:
        con.execute(
            "INSERT INTO run_events (event_id, run_id, event_type, payload_json, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (f"{run_id}-{event_type}-{age_s}", run_id, event_type,
             json.dumps(payload or {"kind": "stalled", "node_id": "impl",
                                    "reason": "impl has been idle 12m"}),
             int(time.time()) - age_s))
        con.commit()
    finally:
        con.close()


def test_a_live_runs_fresh_signal_reaches_the_strip(home: Path, chain_intact, capsys) -> None:
    _run(home, "run-stuck", status="executing")
    _signal(home, "run-stuck")
    strip = _strip(_page(home, "intake", capsys))
    assert _pills(strip) == {"1 watching": "orange"}
    assert strip["detail"] == "1 run need attention: run-stuck · impl — impl has been idle 12m"
    assert strip["text"] == "1 thing needs you"
    assert _button(strip, "Open")["do"] == {
        "page": "run", "tab": "orchestrator", "args": {"run": "run-stuck"}}


def test_a_finished_runs_signal_is_history_not_attention(
        home: Path, chain_intact, capsys) -> None:
    """The watcher writes its last rows before status settles; the tab refuses
    to nag about a run that is already over."""
    _run(home, "run-done", status="published")
    _signal(home, "run-done")
    assert _section(_page(home, "intake", capsys), "triage") is None


def test_an_old_signal_ages_out(home: Path, chain_intact, capsys) -> None:
    _run(home, "run-stale", status="executing")
    _signal(home, "run-stale",
            age_s=orch._WATCH_WINDOW_S + 60)
    assert _section(_page(home, "intake", capsys), "triage") is None


def test_a_dead_dispatcher_is_attention_even_before_the_reaper_flips_status(
        home: Path, chain_intact, capsys) -> None:
    """The reaper owns ``status``; the watcher's ``ended`` row is often first."""
    _run(home, "run-orphan", status="executing")
    _signal(home, "run-orphan", event_type="orchestrator.ended",
            payload={"exit": "dispatcher-dead", "node_id": "impl",
                     "reason": "dispatcher pid 42 is gone"})
    strip = _strip(_page(home, "intake", capsys))
    assert _pills(strip) == {"1 watching": "orange"}
    assert "dispatcher pid 42 is gone" in strip["detail"]


def test_an_ordinary_end_is_not_attention(home: Path, chain_intact, capsys) -> None:
    """A run that finished normally, or whose watcher exited for its own
    reasons, did not need the reader."""
    _run(home, "run-ended", status="executing")
    for index, exit_reason in enumerate(("terminal:executing", "max-lifetime", "disabled")):
        _signal(home, "run-ended", event_type="orchestrator.ended",
                payload={"exit": exit_reason}, age_s=60 + index)
    assert _section(_page(home, "intake", capsys), "triage") is None


def test_one_entry_per_run_newest_signal_first(home: Path, chain_intact, capsys) -> None:
    """A run with four signals is one thing needing the reader, not four."""
    _run(home, "run-noisy", status="executing")
    for age in (300, 240, 180, 60):
        _signal(home, "run-noisy", payload={
            "kind": "stalled", "node_id": "impl", "reason": f"idle {age}s"}, age_s=age)
    strip = _strip(_page(home, "intake", capsys))
    assert _pills(strip) == {"1 watching": "orange"}
    assert "idle 60s" in strip["detail"]
    assert "idle 300s" not in strip["detail"]


def test_the_signal_count_joins_the_other_items_in_the_total(
        home: Path, chain_intact, capsys) -> None:
    _parked(home, "run-parked")
    _run(home, "run-stuck", status="executing")
    _signal(home, "run-stuck")
    strip = _strip(_page(home, "intake", capsys))
    assert _pills(strip) == {"1 waiting": "yellow", "1 watching": "orange"}
    assert strip["text"] == "2 things need you"
    assert [b["label"] for b in strip["actions"]] == ["Answer", "Open"]
