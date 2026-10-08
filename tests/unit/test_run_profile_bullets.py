"""``gen_profile``'s bullet parser and the run page's hero.

Two behaviours, both from the 2026-10-08 kickoff-bullet-continuation kickoff:

1. ``gen_profile``'s inner ``bullets(lines)`` keeps a wrapped markdown bullet
   (an item whose text spills onto an indented continuation line) as ONE item,
   so ``success_criteria`` / ``scope_allow`` don't split a sentence in two.
   Nested bullets stay separate.
2. ``_story_tab``'s hero blanks its goal when the goal repeats the page title
   (the kickoff's first heading) — the hero is the criteria, not the title
   twice.

``bullets`` is a closure inside ``gen_profile``, so every assertion here drives
the real ``gen_profile`` with a temp kickoff — the integration surface, not a
copy of the parser.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from mini_ork.cli import main as cli
from mini_ork.ide_pages import build_page
from mini_ork.stores import migrate as mig

REPO = Path(__file__).resolve().parents[2]
RUN = "run-1791000000-abc123"
T0 = 1_791_000_000

WORKFLOW = """\
version: 1
task_class: demo
nodes:
  - {name: planner, type: planner, model_lane: planner, prompt_ref: prompts/planner.md}
  - {name: implementer, type: implementer, model_lane: worker, prompt_ref: prompts/implementer.md,
     gates: [scope_gate, budget_gate]}
  - {name: test, type: verifier, verifier_ref: verifiers/test.py}
  - {name: reviewer, type: reviewer, model_lane: reviewer, prompt_ref: prompts/reviewer.md}
  - {name: publisher, type: publisher, gates: [deployment_gate]}
  - {name: rollback, type: rollback}
edges:
  - {from: planner, to: implementer, edge_type: depends_on}
  - {from: implementer, to: test, edge_type: verifies}
  - {from: test, to: reviewer, edge_type: depends_on}
  - {from: reviewer, to: publisher, edge_type: depends_on}
  - {from: test, to: rollback, edge_type: escalates_to}
  - {from: reviewer, to: rollback, edge_type: escalates_to}
"""


def _write_kickoff(tmp_path: Path, text: str) -> Path:
    kickoff = tmp_path / "kickoff.md"
    kickoff.write_text(text, encoding="utf-8")
    return kickoff


def _profile(tmp_path: Path, kickoff_text: str) -> dict:
    """Drive the real ``gen_profile`` and return its dict."""
    kickoff = _write_kickoff(tmp_path, kickoff_text)
    agents = tmp_path / "agents.yaml"
    agents.write_text("lanes:\n  implementer: codex\n", encoding="utf-8")
    return cli.gen_profile(
        kickoff, tmp_path, "code-fix", "code_fix",
        tmp_path / "run_profile.json", agents,
    )


class TestWrappedBullets:
    def test_wrapped_plain_and_nested_bullets_yield_three_items(self, tmp_path):
        data = _profile(tmp_path, """# Wrapped bullet fixture

## Done when

- `script/mini-ork-build` exits 0. Paste the `Finished` line and any warnings from
  `crates/mini_ork_ui`.
- tests pass
  - sub stays separate
""")
        criteria = data["success_criteria"]
        # Wrapped bullet + plain bullet + nested bullet.
        assert len(criteria) == 3
        assert criteria[0] == (
            "`script/mini-ork-build` exits 0. Paste the `Finished` line and any "
            "warnings from `crates/mini_ork_ui`."
        )
        assert criteria[1] == "tests pass"
        assert criteria[2] == "sub stays separate"

    def test_wrapped_scope_bullet_stays_one_entry(self, tmp_path):
        data = _profile(tmp_path, """# Wrapped scope fixture

## Files in scope

- `mini_ork/cli/main.py`: ONLY the inner `bullets` helper of `gen_profile`
  (~:289-295)
""")
        assert data["scope_allow"] == [
            "`mini_ork/cli/main.py`: ONLY the inner `bullets` helper of "
            "`gen_profile` (~:289-295)"
        ]


    def test_a_continuation_line_keeps_its_leading_dash_or_star(self, tmp_path):
        data = _profile(tmp_path, """# Continuation content fixture

## Done when

- run the suite with
  --maxWorkers=2 and read the *only* failure
""")
        assert data["success_criteria"] == [
            "run the suite with --maxWorkers=2 and read the *only* failure"]

    def test_a_path_on_the_continuation_line_reaches_scope(self, tmp_path):
        data = _profile(tmp_path, """# Wrapped scope paths fixture

## Files in scope

- `mini_ork/cli/main.py` and its test
  `tests/unit/test_run_profile_bullets.py`
""")
        assert len(data["scope_allow"]) == 1
        assert "`tests/unit/test_run_profile_bullets.py`" in data["scope_allow"][0]


class TestOneLineBulletsUnchanged:
    def test_flat_bullets_are_untouched(self, tmp_path):
        data = _profile(tmp_path, """# Flat fixture

## Success

- a
- b
""")
        assert data["success_criteria"] == ["a", "b"]


def _iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


@pytest.fixture
def home(tmp_path: Path) -> Path:
    """Run-page project home — the pattern from ``test_ide_pages_run.py``,
    copied (private test helpers are not imported across files)."""
    h = tmp_path / "proj" / ".mini-ork"
    h.mkdir(parents=True)
    rc, _out, err = mig.init_db(db=str(h / "state.db"), root=str(REPO))
    assert rc == 0, err
    recipe = h / "recipes" / "demo-recipe"
    recipe.mkdir(parents=True)
    (recipe / "workflow.yaml").write_text(WORKFLOW)
    (recipe / "task_class.yaml").write_text("name: demo\ndescription: a demo recipe\n")
    return h


def _seed(home: Path) -> Path:
    run_dir = home / "runs" / RUN
    run_dir.mkdir(parents=True)
    kickoff = home / "kickoffs" / "demo.md"
    kickoff.parent.mkdir(parents=True)
    kickoff.write_text("# Make the demo pass\n\nDetails.\n")
    con = sqlite3.connect(home / "state.db")
    con.execute(
        "INSERT INTO task_runs (id, recipe, status, cost_usd, created_at, updated_at, ended_at, "
        "task_class, kickoff_path, workflow_version, trace_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (RUN, "demo-recipe", "published", 0.5, T0, T0 + 120, T0 + 100, "demo", str(kickoff),
         "latest", "tr-demo-1"))
    events = [("node_start", "implementer", "implementer", "worker", T0 + 10, None),
              ("node_end", "implementer", "implementer", "worker", T0 + 40, "done"),
              ("node_start", "test", "verifier", "verifier", T0 + 40, None),
              ("node_end", "test", "verifier", "verifier", T0 + 45, "done")]
    for i, (kind, node, ntype, lane, ts, fin) in enumerate(events):
        payload = {"node_id": node, "node_type": ntype, "model_lane": lane}
        if fin:
            payload["finish_reason"] = fin
        con.execute("INSERT INTO run_events (event_id, run_id, event_type, payload_json, created_at) "
                    "VALUES (?,?,?,?,?)", (f"ev-{i}", RUN, kind, json.dumps(payload), ts))
    con.commit()
    con.close()
    (run_dir / "plan.json").write_text('{"objective": "x"}')
    return run_dir


def _section(page: dict, kind: str, title: str | None = None) -> dict:
    for s in page["sections"]:
        if s["type"] == kind and (title is None or s["title"] == title):
            return s
    raise AssertionError(f"no {kind} section {title!r}")


def test_hero_blanks_a_goal_that_repeats_the_title(home: Path, monkeypatch) -> None:
    """The kickoff title is also the hero's ``user_goal`` — the hero must not
    repeat it; it shows the criteria instead."""
    run_dir = _seed(home)
    monkeypatch.setenv("MINI_ORK_IDE_SPEC", "2")
    # Same title, different case + surrounding whitespace: the comparison is
    # case-insensitive and ignores surrounding whitespace.
    (run_dir / "run_profile.json").write_text(json.dumps({
        "user_goal": "  make THE demo pass  ",
        "success_criteria": ["it works", "it is fast"],
    }))
    page = build_page(home, "run", None, {"run": RUN})
    assert page["title"] == "Make the demo pass"
    hero = _section(page, "hero", "What this run is for")
    assert hero["goal"] == ""
    assert hero["criteria"] == ["it works", "it is fast"]


def test_hero_keeps_a_goal_that_differs_from_the_title(home: Path, monkeypatch) -> None:
    run_dir = _seed(home)
    monkeypatch.setenv("MINI_ORK_IDE_SPEC", "2")
    (run_dir / "run_profile.json").write_text(json.dumps({
        "user_goal": "Ship the thing",
        "success_criteria": ["it works"],
    }))
    page = build_page(home, "run", None, {"run": RUN})
    hero = _section(page, "hero", "What this run is for")
    assert hero["goal"] == "Ship the thing"


def test_hero_is_omitted_when_the_goal_is_blanked_and_there_are_no_criteria(
        home: Path, monkeypatch) -> None:
    """Goal == title and no criteria → nothing left for the hero to say."""
    run_dir = _seed(home)
    monkeypatch.setenv("MINI_ORK_IDE_SPEC", "2")
    (run_dir / "run_profile.json").write_text(json.dumps({
        "user_goal": "Make the demo pass",
        "success_criteria": [],
    }))
    page = build_page(home, "run", None, {"run": RUN})
    assert not [s for s in page["sections"] if s["type"] == "hero"]


def test_a_heading_over_80_chars_is_the_full_title_and_not_repeated(
        home: Path, monkeypatch) -> None:
    """The card title is the heading cut to 80 chars; the v2 page shows the
    whole heading as its title and the hero does not repeat it."""
    run_dir = _seed(home)
    long_heading = ("Make the demo pass on every platform we ship, including the slow "
                    "ARM runners and the nightly build")
    (home / "kickoffs" / "demo.md").write_text(f"# {long_heading}\n\nDetails.\n")
    monkeypatch.setenv("MINI_ORK_IDE_SPEC", "2")
    (run_dir / "run_profile.json").write_text(json.dumps({
        "user_goal": long_heading, "success_criteria": ["it works"]}))
    page = build_page(home, "run", None, {"run": RUN})
    assert page["title"] == long_heading
    hero = _section(page, "hero", "What this run is for")
    assert hero["goal"] == ""
    assert hero["criteria"] == ["it works"]
