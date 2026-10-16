"""``board page orch --tab decide`` — the runs the profile gate parked on you.

Two properties carry this tab. The first is *ordering*: the whole reason the tab
exists is that a run parked three runs back is invisible to the Planner tab's
"newest profiled run", so the oldest wait has to come first. The second is the
*binding*: ``board answer`` refuses a bare ``--text`` when several questions are
outstanding, so every box must name its own question — a composer without its
``--for`` would hand the operator a button that always fails.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

import pytest

from mini_ork.ide_pages import orch, spec
from mini_ork.stores import migrate as mig

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture()
def home(tmp_path: Path) -> Path:
    h = tmp_path / "proj" / ".mini-ork"
    h.mkdir(parents=True)
    rc, _out, err = mig.init_db(db=str(h / "state.db"), root=str(REPO))
    assert rc == 0, err
    return h


@pytest.fixture(autouse=True)
def _fixed_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    """The alert threshold is read from the environment at import; pin it so an
    operator's own ``MO_DECIDE_ALERT_S`` cannot re-tone these rows."""
    monkeypatch.setattr(orch, "_WAIT_ALERT_S", 3600)


def _parked(home: Path, run_id: str, questions: list[str], *, waited_s: int,
            created_s: int | None = None, recipe: str = "code-fix",
            answers: dict[str, str] | None = None, profile: bool = True,
            raw_profile: str | None = None, status: str | None = None) -> Path:
    run_dir = home / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    now = int(time.time())
    if raw_profile is not None:
        # An unreadable profile still belongs to a run that *looks* parked: the
        # page must report it rather than quietly treat it as "never asked".
        (run_dir / "run_profile.json").write_text(raw_profile)
    elif profile:
        path = run_dir / "run_profile.json"
        path.write_text(json.dumps({
            "profile_status": status or ("ready" if answers else "needs_answers"),
            "confidence": 0.4, "recipe": recipe, "human_questions": questions,
        }))
        # The wait is measured from the profile's mtime — the file nothing
        # rewrites until the question is answered.
        stamp = now - waited_s
        os.utime(path, (stamp, stamp))
        if answers:
            (run_dir / "profile-answers.json").write_text(json.dumps(answers))
    con = sqlite3.connect(home / "state.db")
    con.execute("INSERT INTO task_runs (id, recipe, status, cost_usd, created_at, updated_at, "
                "task_class, kickoff_path, workflow_version) VALUES (?,?,?,?,?,?,?,?,?)",
                (run_id, recipe, "planned", 0.1, now - (created_s if created_s is not None else waited_s),
                 now, "code_fix", "", "latest"))
    con.commit()
    con.close()
    return run_dir


def _section(page: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    return [s for s in page["sections"] if s["type"] == kind]


def _titles(page: dict[str, Any]) -> list[str]:
    return [s["title"] for s in _section(page, "list")]


def test_the_tab_is_reachable(home: Path) -> None:
    assert "decide" in [key for key, _label in orch.TABS]
    page = orch.build(home, "decide", {})
    assert page["ok"] is True and page["tab"] == "decide"


def test_nothing_waiting_says_so(home: Path) -> None:
    _parked(home, "run-quiet", [], waited_s=10, profile=False)
    page = orch.build(home, "decide", {})
    lists = _section(page, "list")
    assert len(lists) == 1 and lists[0]["title"] == "Blocked on you"
    assert lists[0]["items"][0]["t"] == "Nothing is waiting for an answer"
    assert _section(page, "composer") == []
    assert page["errors"] == {}


def test_only_parked_runs_are_listed_longest_waiting_first(home: Path) -> None:
    _parked(home, "run-fresh", ["Which lane?"], waited_s=60)
    _parked(home, "run-old", ["Which lane?"], waited_s=4 * 3600)
    _parked(home, "run-mid", ["Which lane?"], waited_s=2 * 3600)
    _parked(home, "run-answered", ["Which lane?"], waited_s=5 * 3600,
            answers={"Which lane?": "codex"})
    _parked(home, "run-unplanned", [], waited_s=9 * 3600, profile=False)

    page = orch.build(home, "decide", {})
    titles = _titles(page)
    assert titles[0].endswith("run-old")
    assert titles[1].endswith("run-mid")
    assert titles[2].endswith("run-fresh")
    assert len(titles) == 3, "an answered run and one that never asked are not waiting"
    assert page["errors"] == {}


def test_each_question_gets_a_composer_bound_to_its_id(home: Path) -> None:
    run_dir = _parked(home, "run-1791000000-aaaaaa", ["Which lane?", "Which workspace?"],
                      waited_s=120)
    page = orch.build(home, "decide", {})
    composers = _section(page, "composer")
    assert [c["cli"] for c in composers] == [
        ["board", "answer", "run-1791000000-aaaaaa", "--for", "q1", "--text"],
        ["board", "answer", "run-1791000000-aaaaaa", "--for", "q2", "--text"],
    ]
    # The typed text becomes the argv's last element, so the prefix has to end
    # on ``--text`` for the answer to be the flag's value.
    assert all(c["cli"][-1] == "--text" for c in composers)
    assert composers[0]["placeholder"].startswith("Answer q1:")
    assert [i["t"] for i in _section(page, "list")[0]["items"]] == [
        "Which lane?", "Which workspace?"]
    del run_dir


def test_a_partly_answered_run_offers_only_what_is_left(home: Path) -> None:
    """One of two questions carries an answer and the run is still parked — so
    the tab shows the *other* one, under its own id. Numbering the ids over the
    whole list (not over what is left) is what keeps ``q2`` meaning the second
    question the planner asked, which is the id ``board answer --for`` resolves.
    """
    _parked(home, "run-1791000000-aaaaaa", ["Which lane?", "Which workspace?"],
            waited_s=120, answers={"Which lane?": "codex"}, status="needs_answers")
    page = orch.build(home, "decide", {})
    assert [i["t"] for i in _section(page, "list")[0]["items"]] == ["Which workspace?"]
    composers = _section(page, "composer")
    assert [c["cli"][-2] for c in composers] == ["q2"]


def test_the_tone_escalates_with_the_wait(home: Path) -> None:
    _parked(home, "run-fresh", ["q?"], waited_s=60)
    _parked(home, "run-quarter", ["q?"], waited_s=20 * 60)
    _parked(home, "run-hour", ["q?"], waited_s=2 * 3600)
    _parked(home, "run-day", ["q?"], waited_s=8 * 3600)

    page = orch.build(home, "decide", {})
    tones = {}
    for section in _section(page, "list"):
        if section["items"] and section["items"][0]["sub"].startswith("waiting"):
            tones[section["title"].rsplit(" · ", 1)[-1]] = section["items"][0]["mc"]
    assert tones["run-fresh"] == "sub"
    assert tones["run-quarter"] == "yellow"
    assert tones["run-hour"] == "orange"
    assert tones["run-day"] == "red"
    assert all(c in spec.COLOURS for c in tones.values())


def test_a_long_wait_names_the_recipe_and_the_start(home: Path) -> None:
    _parked(home, "run-parked", ["Which lane?"], waited_s=2 * 3600,
            created_s=3 * 3600, recipe="epic-runner")
    page = orch.build(home, "decide", {})
    sub = _section(page, "list")[0]["items"][0]["sub"]
    assert "waiting 2h 00m" in sub
    assert "epic-runner" in sub and "started" in sub


def test_a_short_wait_leaves_the_cost_out(home: Path) -> None:
    _parked(home, "run-parked", ["Which lane?"], waited_s=90, recipe="epic-runner")
    page = orch.build(home, "decide", {})
    sub = _section(page, "list")[0]["items"][0]["sub"]
    assert sub == "waiting 1m"


def test_an_unreadable_profile_is_reported_not_skipped(home: Path) -> None:
    _parked(home, "run-broken", [], waited_s=0, raw_profile="{not json")
    _parked(home, "run-fine", ["Which lane?"], waited_s=60)
    page = orch.build(home, "decide", {})
    assert list(page["errors"]) == ["profile:run-broken"]
    assert "unreadable" in page["errors"]["profile:run-broken"]
    assert len(_titles(page)) == 1, "the readable run still renders"


def test_a_profile_that_is_not_an_object_is_reported(home: Path) -> None:
    _parked(home, "run-list", [], waited_s=0, raw_profile='["Which lane?"]')
    page = orch.build(home, "decide", {})
    assert page["errors"]["profile:run-list"] == "run_profile.json is not an object"


def test_the_pages_composer_argv_actually_answers(home: Path, capsys) -> None:
    """The seam: the box the tab draws and the verb it dispatches into must
    agree on the question id. The IDE appends the typed text as the argv's last
    element, then ``--home`` — so the prefix has to end on ``--text`` and the
    ``--for`` id has to be one ``board answer`` resolves.
    """
    from mini_ork.cli import board_cmd

    run_dir = _parked(home, "run-1791000000-aaaaaa", ["Which lane?", "Which workspace?"],
                      waited_s=300)
    page = orch.build(home, "decide", {})
    composer = _section(page, "composer")[1]
    assert composer["cli"][0] == "board"
    rc = board_cmd.main([*composer["cli"][1:], "in-place", "--home", str(home)], "")
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0 and payload["ok"] is True
    assert json.loads((run_dir / "profile-answers.json").read_text()) == {
        "Which workspace?": "in-place"}


def test_a_dict_question_answers_under_its_text(home: Path) -> None:
    """A planner that writes ``{"text": …}`` must yield the same id the CLI
    resolves, or the operator's box would answer a question id nobody asked."""
    run_dir = _parked(home, "run-dict", [], waited_s=60)
    path = run_dir / "run_profile.json"
    path.write_text(json.dumps({"profile_status": "needs_answers", "recipe": "code-fix",
                                "human_questions": [{"text": "Which lane?"},
                                                    {"question": "Which workspace?"}]}))
    page = orch.build(home, "decide", {})
    items = _section(page, "list")[0]["items"]
    assert [i["t"] for i in items] == ["Which lane?", "Which workspace?"]
    assert [c["cli"][-2] for c in _section(page, "composer")] == ["q1", "q2"]
