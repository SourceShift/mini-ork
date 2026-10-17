"""Run-page breadcrumbs, Copy summary and the worktree segment in the v2 sub.

The header is the one strip every tab shares, so this file pins the two
relations a reader needs from it — where the run sits (C18) and what to paste
into a PR or chat (C21) — plus the worktree path the v2 sub dropped. Every
fixture is a stub ``Run`` on ``tmp_path``, built the way
``test_ide_pages_run_header.py`` builds them: no DB, no fleet, no network.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

from mini_ork.ide_pages import run as R
from mini_ork.ide_pages.run_header import build_header

RID = "run-crumbs-1"
GOAL = "Fix the frobnicator queue"


def _run(tmp_path, *, card: dict[str, Any] | None = None,
         row: dict[str, Any] | None = None, verdict: dict[str, Any] | None = None,
         profile: dict[str, Any] | None = None,
         workspace: Any = None, rid: str = RID) -> R.Run:
    """A minimal ``Run`` plus only the files a test itself writes."""
    run_dir = tmp_path / "proj" / ".mini-ork" / "runs" / rid
    run_dir.mkdir(parents=True, exist_ok=True)
    if verdict is not None:
        (run_dir / "run-verdict.json").write_text(json.dumps(verdict), encoding="utf-8")
    if profile is not None:
        (run_dir / "run_profile.json").write_text(json.dumps(profile), encoding="utf-8")
    return R.Run(id=rid, home=tmp_path / "proj" / ".mini-ork", run_dir=run_dir,
                 row=dict(row or {}), card=dict(card or {}), nodes=[], cols=[],
                 calls=[], recipe_dir=None, workspace=workspace)


def _outcome(state: str = "done", text: str = "Published · verified") -> dict[str, Any]:
    return {"state": state, "tone": "green", "icon": "✓", "text": text,
            "detail": "", "counts": [], "actions": [], "menu": []}


def _crumbs(run: R.Run, outcome: dict[str, Any] | None = None) -> list | None:
    return build_header(run, outcome if outcome is not None else _outcome()).get("crumbs")


def _copy_entry(run: R.Run, outcome: dict[str, Any] | None = None) -> dict[str, Any]:
    menu = build_header(run, outcome if outcome is not None else _outcome())["menu"]
    entries = [m for m in menu if isinstance(m, dict) and m.get("label") == "Copy summary"]
    assert len(entries) == 1, [m.get("label") for m in menu]
    return entries[0]


# ── crumbs ──────────────────────────────────────────────────────────────────

def test_parent_epic_yields_an_epic_crumb_then_the_run(tmp_path) -> None:
    """C18: a run with a parent epic reads ``epic › run``; the epic crumb links
    the parent and the run crumb is the mono run id with nowhere to go."""
    crumbs = _crumbs(_run(tmp_path, card={"status": "published", "epic_id": "run-epic-42"}))
    assert isinstance(crumbs, list) and len(crumbs) == 2, crumbs
    epic, run_crumb = crumbs
    assert epic["t"] == "run-epic-42"
    assert isinstance(epic["do"], dict) and "run-epic-42" in json.dumps(epic["do"])
    assert run_crumb == {"t": RID, "do": None, "mono": True}


def test_epic_key_and_parent_run_id_are_also_read(tmp_path) -> None:
    """The kickoff names ``epic``, ``epic_id`` and a parent run id — read what
    the card carries, and never invent a key it does not."""
    for key in ("epic", "parent_run_id"):
        crumbs = _crumbs(_run(tmp_path, card={"status": "published", key: "run-parent-7"}))
        assert crumbs and crumbs[0]["t"] == "run-parent-7", (key, crumbs)


def test_root_run_has_no_crumbs(tmp_path) -> None:
    """A root run with neither parent nor attempt gets no crumb at all — the
    key is omitted rather than filled with a fabricated position."""
    assert _crumbs(_run(tmp_path, card={"status": "published", "title": GOAL})) in ([], None)


def test_recovered_run_gets_a_retry_crumb_after_the_run(tmp_path) -> None:
    """C18: a recovery reads ``run › retry #N``, with the retry marker last."""
    crumbs = _crumbs(_run(tmp_path, card={"status": "published", "attempt": 2,
                                          "recovered_from": "run-orig-7"}))
    assert [c["t"] for c in crumbs] == [RID, "retry #2"], crumbs
    assert "run-orig-7" in json.dumps(crumbs[1]["do"])


def test_attempt_alone_still_gets_a_retry_marker(tmp_path) -> None:
    """An attempt number with no recorded origin still places the run."""
    crumbs = _crumbs(_run(tmp_path, card={"status": "published", "attempt": 3}))
    assert [c["t"] for c in crumbs] == [RID, "retry #3"], crumbs
    assert crumbs[1]["do"] is None


def test_epic_run_retry_order(tmp_path) -> None:
    """Both relations together read ``epic › run › retry #N``."""
    crumbs = _crumbs(_run(tmp_path, card={"status": "published", "epic_id": "run-epic-9",
                                          "attempt": 2}))
    assert [c["t"] for c in crumbs] == ["run-epic-9", RID, "retry #2"], crumbs


def test_every_crumb_has_t_and_a_dict_or_null_do(tmp_path) -> None:
    """CONTRACT v2.1: ``[{t, do, mono}]`` — a crumb's ``do`` is an action or null."""
    for card in ({"epic": "run-epic-1"}, {"epic_id": "run-epic-2"},
                 {"attempt": 1, "recovered_from": "run-orig-3"},
                 {"recovered_from": "run-orig-4"}):
        crumbs = _crumbs(_run(tmp_path, card={"status": "published", **card}))
        assert isinstance(crumbs, list) and crumbs, card
        for crumb in crumbs:
            assert isinstance(crumb, dict) and str(crumb.get("t") or "").strip(), crumb
            assert crumb.get("do") is None or isinstance(crumb.get("do"), dict), crumb


def test_an_unreadable_card_omits_crumbs_and_keeps_the_header(tmp_path) -> None:
    """A card that raises on every read costs the crumbs, never the header."""
    class _Poison(dict):
        def get(self, *args, **kwargs):
            raise RuntimeError("card unreadable")

    run = R.Run(id=RID, home=tmp_path, run_dir=tmp_path / "runs" / RID, row={},
                card=_Poison(), nodes=[], cols=[], calls=[], recipe_dir=None,
                workspace=None)
    header = build_header(run, _outcome())
    assert header.get("crumbs", []) in ([], None)
    assert header["label"] == RID


# ── Copy summary ────────────────────────────────────────────────────────────

def test_copy_summary_is_one_menu_entry_never_a_header_button(tmp_path) -> None:
    """C21: one ``Copy summary`` action inside the menu, never among the top
    two header buttons; its ``do`` is the clipboard action."""
    run = _run(tmp_path, card={"status": "published", "title": GOAL, "cost_total": 1.25})
    entry = _copy_entry(run)
    assert entry["do"]["done"] == "Summary copied"
    assert isinstance(entry["do"]["copy"], str) and entry["do"]["copy"].strip()
    labels = [a["label"] for a in build_header(run, _outcome())["actions"]]
    assert "Copy summary" not in labels and len(labels) <= 2


def test_summary_has_goal_outcome_levels_cost_and_link(tmp_path) -> None:
    """The markdown carries the goal, the outcome line, one line per level,
    cost and wall time, and the pasteable link."""
    run = _run(tmp_path, card={"status": "published", "title": GOAL, "cost_total": 1.25},
               row={"created_at": 1790000000, "ended_at": 1790000300},
               verdict={"levels": {"build": "PROVEN", "tests": "REFUTED"}})
    md = _copy_entry(run)["do"]["copy"]
    assert GOAL in md and "Published · verified" in md
    assert "build: PROVEN" in md and "tests: REFUTED" in md
    assert "$1.25" in md and "5m 00s" in md
    assert f"mini-ork://run/{RID}" in md and "\n" in md


def test_summary_goal_falls_back_to_the_profile_and_cost_to_the_row(tmp_path) -> None:
    """No kickoff title → the operator's goal; no card rollup → the DB cost."""
    run = _run(tmp_path, card={"status": "published"}, row={"cost_usd": 2.5},
               profile={"user_goal": "Make the queue fast again"})
    md = _copy_entry(run)["do"]["copy"]
    assert "Make the queue fast again" in md and "$2.50" in md


def test_summary_absorbs_a_broken_vector_and_a_missing_diff(tmp_path) -> None:
    """A broken level vector or a card with no changed files omits those lines
    and keeps the rest — never raises, never empties the summary."""
    run = _run(tmp_path, card={"status": "published", "title": GOAL, "cost_total": 1.25},
               verdict={"levels": "not-a-dict"})
    md = _copy_entry(run)["do"]["copy"]
    assert md.strip() and GOAL in md and "Published · verified" in md and "$1.25" in md
    assert "file" not in md


def test_summary_survives_an_empty_outcome(tmp_path) -> None:
    """The goal and link live on the run, so an empty outcome still summarizes."""
    run = _run(tmp_path, card={"status": "published", "title": GOAL, "cost_total": 1.25})
    md = _copy_entry(run, {})["do"]["copy"]
    assert GOAL in md and md.strip()


# ── page wiring: crumbs, menu, and the worktree in the sub ─────────────────

def test_v2_page_carries_crumbs_and_copy_summary(tmp_path) -> None:
    """``_build_v2`` passes the header crumbs to the page and keeps the summary
    in the menu — not in the page's buttons."""
    run = _run(tmp_path, card={"status": "published", "recipe": "framework_edit",
                               "title": GOAL, "cost_total": 1.25,
                               "epic_id": "run-epic-77"})
    page = R._build_v2(run, "dag", {"run": RID})
    assert [c["t"] for c in page["crumbs"]] == ["run-epic-77", RID]
    entries = [m for m in page["menu"] if m.get("label") == "Copy summary"]
    assert len(entries) == 1 and GOAL in entries[0]["do"]["copy"]
    assert "Copy summary" not in [a.get("label") for a in page["actions"]]


def _workspace(tmp_path) -> SimpleNamespace:
    """A worktree under the project root, so the sub shows the relative path."""
    return SimpleNamespace(base_branch="main", branch="wt/rp-x",
                           path=str(tmp_path / "proj" / "wts" / "rp-x"))


def test_sub_appends_the_worktree_after_the_title(tmp_path) -> None:
    """C18: the v2 sub regains `` · worktree <relpath>`` after the title —
    appended, never substituted."""
    run = _run(tmp_path, card={"status": "published", "title": GOAL},
               workspace=_workspace(tmp_path))
    sub = R._build_v2(run, "dag", {"run": RID})["sub"]
    assert GOAL in sub and "worktree wts/rp-x" in sub
    assert sub.index("worktree") > sub.index(GOAL)


def test_sub_without_a_title_keeps_recipe_branch_and_id(tmp_path) -> None:
    """With no title the sub is the recipe · branch · id line; the worktree is
    appended after it and none of the existing content is pushed out."""
    run = _run(tmp_path, card={"status": "published", "recipe": "framework_edit"},
               workspace=_workspace(tmp_path))
    sub = R._build_v2(run, "dag", {"run": RID})["sub"]
    for part in ("framework_edit", "branch wt/rp-x", RID, "worktree wts/rp-x"):
        assert part in sub, (part, sub)
    assert sub.index("worktree") > sub.index(RID)


def test_sub_without_a_workspace_gains_no_worktree(tmp_path) -> None:
    """Only a run with a worktree names one — no fabricated path."""
    run = _run(tmp_path, card={"status": "published", "title": GOAL}, workspace=None)
    sub = R._build_v2(run, "dag", {"run": RID})["sub"]
    assert GOAL in sub and "worktree" not in sub
