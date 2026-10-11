"""``mini_ork.ide_pages.run`` — the run page's ``desc``: one paragraph, five lines.

The description is a fold over evidence that already exists (the planner's
``plan.json`` objective, the run's own outcome sentence, the nodes' states), so
these tests pin the fold's two rules — what leads, and what the five-line cap
costs — plus the fail-soft promise that a description can never break a page.
"""
from __future__ import annotations

import itertools
import json
from pathlib import Path

from mini_ork.ide_pages import run as R
from mini_ork.ide_pages import spec as S

T0 = 1_791_000_000
_SEQ = itertools.count()


def _run(tmp_path: Path, *, states: list[str], objective: str = "",
         goal: str = "", status: str = "published", ids: list[str] | None = None) -> R.Run:
    """A ``Run`` built by hand: the description reads only these fields.

    Each call gets its own run dir — the intent falls back to ``run_profile.json``
    and ``plan.json``, so two runs sharing a dir would read each other's files.
    """
    run_dir = tmp_path / "runs" / f"run-{next(_SEQ)}"
    run_dir.mkdir(parents=True, exist_ok=True)
    if objective:
        (run_dir / "plan.json").write_text(json.dumps({"objective": objective}))
    if goal:
        (run_dir / "run_profile.json").write_text(json.dumps({"user_goal": goal}))
    names = ids or [f"n{i}" for i in range(len(states))]
    return R.Run(
        id=run_dir.name, home=tmp_path, run_dir=run_dir,
        row={"created_at": T0, "updated_at": T0 + 60, "ended_at": T0 + 60, "cost_usd": 1.5},
        card={"status": status, "title": "A card title", "cost_total": 1.94},
        nodes=[R.Node(id=n, type="implementer", state=s) for n, s in zip(names, states)],
        cols=[], calls=[], recipe_dir=None, workspace=None,
    )


# ── wrapping ───────────────────────────────────────────────────────────────

def test_wrap_caps_lines_and_marks_the_cut() -> None:
    wrapped = R._desc_wrap("word " * 400)
    lines = wrapped.split("\n")
    assert len(lines) == R._DESC_LINES
    assert lines[-1].endswith("…")
    assert all(len(line) <= R._DESC_LINE_CHARS for line in lines), lines


def test_wrap_leaves_text_that_fits_alone() -> None:
    assert R._desc_wrap("Short enough.") == "Short enough."


# ── intent ─────────────────────────────────────────────────────────────────

def test_intent_prefers_the_planners_own_objective(tmp_path: Path) -> None:
    run = _run(tmp_path, states=["done"], objective="Make the demo pass.", goal="ignored")
    assert R._desc_intent(run) == "Make the demo pass."


def test_intent_falls_back_to_the_goal_then_the_card_title(tmp_path: Path) -> None:
    assert R._desc_intent(_run(tmp_path, states=["done"], goal="Goal line\nmore")) == "Goal line"
    assert R._desc_intent(_run(tmp_path, states=["done"])) == "A card title"


def test_intent_clips_a_paragraph_of_a_goal_to_two_lines(tmp_path: Path) -> None:
    run = _run(tmp_path, states=["done"], goal="the " * 300)
    assert len(R._clip(R._desc_intent(run), R._DESC_LINE_CHARS * 2)) <= R._DESC_LINE_CHARS * 2 + 1


# ── outcome ────────────────────────────────────────────────────────────────

def test_outcome_leads_with_the_runs_own_sentence(tmp_path: Path) -> None:
    """The lead is the outcome text the Story tab's triage already shows, so the
    header and the page cannot disagree about what became of the run."""
    outcome = {"text": "Failed at tier4_synth (cost_limit)"}
    run = _run(tmp_path, states=["done", "failed"], status="failed",
               ids=["judge", "tier4_synth"])
    text = R._desc_outcome(run, outcome)
    assert text.startswith("Failed at tier4_synth (cost_limit).")
    assert "1/2 nodes done" in text and "1 failed" in text


def test_outcome_falls_back_to_the_node_itself_without_an_outcome(tmp_path: Path) -> None:
    run = _run(tmp_path, states=["done", "failed"], status="failed", ids=["judge", "panel"])
    text = R._desc_outcome(run, None)
    assert text.startswith("Failed at panel")
    assert text.count("panel") == 1


def test_outcome_counts_skipped_as_not_run(tmp_path: Path) -> None:
    run = _run(tmp_path, states=["done", "skipped"])
    assert "1 not run" in R._desc_outcome(run, {"text": "Published"})


# ── the fold ───────────────────────────────────────────────────────────────

def test_fold_is_one_paragraph_within_the_cap(tmp_path: Path) -> None:
    run = _run(tmp_path, states=["done", "done"], objective="Make the demo pass.")
    desc = R._desc_fold(run, {"text": "Published"})
    assert desc.startswith("Make the demo pass. Published.")
    assert len(desc.split("\n")) <= R._DESC_LINES


def test_fold_drops_an_intent_that_only_repeats_the_title(tmp_path: Path) -> None:
    """The header draws the title directly above the description — the same
    words twice is worse than no description at all."""
    run = _run(tmp_path, states=["done"], objective="Make the demo pass.")
    repeated = R._desc_fold(run, {"text": "Published"}, "Make the demo pass")
    assert repeated.startswith("Published.") and "Make the demo pass" not in repeated
    kept = R._desc_fold(run, {"text": "Published"}, "Something else.")
    assert kept.startswith("Make the demo pass. Published.")


def test_fold_is_empty_without_nodes(tmp_path: Path) -> None:
    """A run that has not started has nothing to describe, and its intent is
    its title — so the header renders exactly as before."""
    run = _run(tmp_path, states=[])
    assert R._desc_fold(run, {"text": "Published"}, "A card title") == ""


def test_fold_never_raises(tmp_path: Path, monkeypatch) -> None:
    run = _run(tmp_path, states=["done"])

    def boom(_):
        raise RuntimeError("no description today")

    monkeypatch.setattr(R, "_desc_intent", boom)
    assert R._desc_fold(run, {"text": "Published"}) == ""


# ── the page field ─────────────────────────────────────────────────────────

def test_spec_page_carries_desc_and_defaults_to_empty() -> None:
    assert S.page("run", "t", "s")["desc"] == ""
    assert S.page("run", "t", "s", desc="One line.")["desc"] == "One line."
