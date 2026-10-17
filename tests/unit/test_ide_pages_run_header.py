"""``run_header.build_header`` and its wiring into the v2 run page.

The run's controls live in the page header now — ``_build_v2`` draws them from
``build_header`` and the Story tab's triage carries none — so these tests pin
the two things that decide whether a user can act on a run from any tab:

* which buttons the header offers for a given state (Stop/Kill while it runs,
  Merge first when it is finished with a worktree), and
* which contract-v2.1 fields the page carries at all.

Everything is hermetic: a stub ``Run`` on ``tmp_path``, an ``outcome`` dict of
the shape :func:`mini_ork.ide_pages.outcome.resolve` returns, and no DB, fleet
or network. ``_build_v2`` is exercised with ``O.resolve`` monkeypatched for the
same reason — it must not reach for a live run.
"""
from __future__ import annotations

import time
from types import SimpleNamespace

from mini_ork.ide_pages import run as R
from mini_ork.ide_pages import spec as S
from mini_ork.ide_pages.run_header import build_header

# The keys ``S.page`` emitted before the header fields existed — the snapshot
# every caller that passes none of the new kwargs must keep receiving.
_OLD_PAGE_KEYS = {"ok", "key", "title", "sub", "desc", "chips", "actions",
                  "tabs", "tab", "args", "sections", "errors"}


def _run(tmp_path, *, run_id: str = "rp-e3-header-v2", status: str = "running",
         title: str = "", recipe: str = "", workspace=None) -> R.Run:
    """A minimal ``Run`` — enough for ``_title_v2``, ``_wall`` and the buttons."""
    card: dict = {"status": status}
    if title:
        card["title"] = title
    if recipe:
        card["recipe"] = recipe
    return R.Run(id=run_id, home=tmp_path, run_dir=tmp_path / "runs" / run_id,
                 row={}, card=card, nodes=[], cols=[], calls=[],
                 recipe_dir=None, workspace=workspace)


def _workspace(tmp_path) -> SimpleNamespace:
    return SimpleNamespace(base_branch="main", branch="rp-e3-header-v2",
                           path=str(tmp_path / "wt"))


def _running_outcome(run: R.Run) -> dict:
    """The outcome ``O.resolve`` produces for a run in flight, menu included."""
    return {
        "state": "running", "tone": "yellow", "icon": "●",
        "text": "Running · spec test", "detail": "",
        "counts": [{"t": "2/5 nodes", "c": "sub"}],
        "actions": R._stop_kill_actions(run),
        "callouts": [],
        "menu": [
            S.btn("Certify this change", S.page_link("verify", "certify", run=run.id)),
            S.btn("Open run folder", S.reveal(str(run.run_dir)), "ghost"),
        ],
    }


def _finished_outcome(run: R.Run) -> dict:
    return {
        "state": "needs_you", "tone": "yellow", "icon": "✋",
        "text": "Ready to review", "detail": "", "counts": [],
        "actions": R._review_actions(run),
        "callouts": [],
        "menu": [S.btn("Open run folder", S.reveal(str(run.run_dir)), "ghost")],
    }


# ── build_header ────────────────────────────────────────────────────────────

def test_header_is_the_four_contract_fields(tmp_path) -> None:
    """Contract v2.1: the header is ``label`` / ``actions`` / ``menu`` /
    ``updated_at`` — nothing more, and the two lists are always lists."""
    header = build_header(_run(tmp_path, title="Fix the login loop"),
                          _running_outcome(_run(tmp_path, title="Fix the login loop")))
    assert set(header) == {"label", "actions", "menu", "updated_at"}
    assert isinstance(header["actions"], list) and isinstance(header["menu"], list)


def test_running_run_offers_stop_then_kill(tmp_path) -> None:
    """A running run's next actions are Stop (warn) then Kill (danger), each
    with the confirm text the triage used to carry and the new pending/done."""
    run = _run(tmp_path, title="Fix the login loop")
    actions = build_header(run, _running_outcome(run))["actions"]
    assert [a["label"] for a in actions] == ["Stop", "Kill"]
    stop, kill = actions
    assert stop["kind"] == "warn" and kill["kind"] == "danger"
    assert stop["do"]["confirm"] == "Stop this run after its current node?"
    assert stop["do"]["pending"] == "Stopping…"
    assert stop["do"]["done"] == "Run will stop after this node"
    assert kill["do"]["confirm"] == f"Kill {run.id}? SIGTERM, then SIGKILL after 2 s."
    assert kill["do"]["pending"] == "Killing…"
    assert kill["do"]["done"] == "Run killed"


def test_running_menu_drops_certify_and_keeps_the_rest(tmp_path) -> None:
    """A change that is still being written cannot be certified: the menu loses
    "Certify this change" while the run is in flight, and everything else stays."""
    run = _run(tmp_path, title="Fix the login loop")
    labels = [m["label"] for m in build_header(run, _running_outcome(run))["menu"]]
    assert "Certify this change" not in labels
    assert labels == ["Open run folder"]


def test_menu_subtracts_what_the_header_already_draws(tmp_path) -> None:
    """A menu entry that duplicates a header button is drawn once — the header
    wins, so the same control never appears twice with two different toasts."""
    run = _run(tmp_path, status="published", title="Fix the login loop",
               workspace=_workspace(tmp_path))
    outcome = _finished_outcome(run)
    outcome["menu"] = [S.btn("Merge into main", S.page_link("verify", "certify", run=run.id)),
                       *outcome["menu"]]
    header = build_header(run, outcome)
    assert [a["label"] for a in header["actions"]][:2] == ["Merge into main", "Discard"]
    assert [m["label"] for m in header["menu"]] == ["Open run folder"]


def test_actions_are_capped_at_two(tmp_path) -> None:
    """Contract v2.1: ``actions`` holds at most two buttons — the IDE draws them
    beside the title and a third would not fit."""
    run = _run(tmp_path, status="published", title="Fix the login loop")
    outcome = {"state": "done", "tone": "green", "icon": "✓", "text": "Published",
               "detail": "", "counts": [], "callouts": [], "menu": [],
               "actions": [S.btn(f"Action {i}", S.page_link("run", run=run.id))
                           for i in (1, 2, 3)]}
    header = build_header(run, outcome)
    assert [a["label"] for a in header["actions"]] == ["Action 1", "Action 2"]


def test_finished_run_with_a_worktree_leads_with_merge(tmp_path) -> None:
    """The first button is the run's next action: Merge for a finished run whose
    change is sitting in a worktree."""
    run = _run(tmp_path, status="published", title="Fix the login loop",
               workspace=_workspace(tmp_path))
    actions = build_header(run, _finished_outcome(run))["actions"]
    assert actions and actions[0]["label"] == "Merge into main"
    assert actions[0]["do"]["pending"] == "Merging…"
    assert actions[0]["do"]["done"] == "Merged into main"
    assert actions[1]["do"]["done"] == "Worktree discarded"


def test_review_actions_carry_pending_and_done(tmp_path) -> None:
    """Discard says what it did, and both review buttons say what they are doing
    while the command runs."""
    run = _run(tmp_path, status="published", title="Fix the login loop",
               workspace=_workspace(tmp_path))
    merge, discard = R._review_actions(run)
    assert merge["do"]["pending"] == "Merging…" and merge["do"]["done"] == "Merged into main"
    assert discard["do"]["pending"] == "Discarding…"
    assert discard["do"]["done"] == "Worktree discarded"


def test_a_run_without_a_worktree_gets_no_review_buttons(tmp_path) -> None:
    """Nothing to merge or discard: a published run with no workspace keeps an
    empty header action list rather than a dead button."""
    run = _run(tmp_path, status="published", title="Fix the login loop")
    outcome = {"state": "done", "tone": "green", "icon": "✓", "text": "Published",
               "detail": "", "counts": [], "callouts": [], "menu": [], "actions": []}
    assert build_header(run, outcome)["actions"] == []


def test_label_never_certifies_itself_into_the_actions(tmp_path) -> None:
    """Certify is offered from the Verify page, not the run header — in every
    state, so a finished run's header does not lead with a judgement the
    operator has not made yet."""
    run = _run(tmp_path, status="published", title="Fix the login loop")
    outcome = {"state": "done", "tone": "green", "icon": "✓", "text": "Published",
               "detail": "", "counts": [], "callouts": [], "menu": [],
               "actions": [S.btn("Certify this change",
                                 S.page_link("verify", "certify", run=run.id))]}
    assert build_header(run, outcome)["actions"] == []


# ── label ───────────────────────────────────────────────────────────────────

def test_label_is_the_title_when_it_fits(tmp_path) -> None:
    run = _run(tmp_path, title="Fix the login loop")
    label = build_header(run, _running_outcome(run))["label"]
    assert label == "Fix the login loop"
    assert len(label) <= 40 and label != run.id


def test_label_is_cut_at_a_word_boundary_with_an_ellipsis(tmp_path) -> None:
    """The tab is a fixed width: a long heading is cut on a word boundary and
    marked with ``…`` rather than left to the renderer to clip mid-word."""
    run = _run(tmp_path, title="Ship the state-aware header on every tab of the run page")
    label = build_header(run, _running_outcome(run))["label"]
    assert len(label) <= 40
    assert label == "Ship the state-aware header on every…"
    assert label.endswith("…") and not label.endswith(" …")


def test_label_is_the_run_id_when_the_run_has_no_title(tmp_path) -> None:
    """No card title and no ``run_profile.json`` → ``_title_v2`` falls back to
    the id, and the id is returned whole (a truncated id names no run)."""
    run = _run(tmp_path, title="")
    assert build_header(run, _running_outcome(run))["label"] == run.id


def test_label_prefers_the_full_goal_over_the_placeholder_title(tmp_path) -> None:
    """``run_card`` titles a run without a readable kickoff ``"<recipe> run"``;
    that placeholder carries nothing, so the operator's goal is used instead."""
    run = _run(tmp_path, title="framework-edit run", recipe="framework-edit")
    run.run_dir.mkdir(parents=True)
    (run.run_dir / "run_profile.json").write_text(
        '{"user_goal": "Fix the login loop"}', encoding="utf-8")
    assert build_header(run, _running_outcome(run))["label"] == "Fix the login loop"


def test_updated_at_is_the_read_time(tmp_path) -> None:
    run = _run(tmp_path, title="Fix the login loop")
    before = int(time.time())
    header = build_header(run, _running_outcome(run))
    assert isinstance(header["updated_at"], int) and not isinstance(header["updated_at"], bool)
    assert before - 1 <= header["updated_at"] <= int(time.time()) + 1


# ── a broken source costs its part, never the header ────────────────────────

class _PoisonCard(dict):
    """A card whose every read raises — the "broken source" to absorb."""

    def get(self, *args, **kwargs):
        raise RuntimeError("card unreadable")

    def __getitem__(self, key):
        raise RuntimeError("card unreadable")


def test_a_broken_card_costs_the_label_not_the_buttons(tmp_path) -> None:
    """Every part absorbs its own failure (the ``S.guarded`` contract): an
    unreadable card must not take the actions, the menu or the timestamp with
    it."""
    run = R.Run(id="rp-e3-header-v2", home=tmp_path, run_dir=tmp_path / "runs" / "rp-e3-header-v2",
                row={}, card=_PoisonCard(), nodes=[], cols=[], calls=[],
                recipe_dir=None, workspace=None)
    header = build_header(run, _running_outcome(run))
    assert set(header) == {"label", "actions", "menu", "updated_at"}
    assert isinstance(header["label"], str)
    assert [a["label"] for a in header["actions"]] == ["Stop", "Kill"]
    assert isinstance(header["updated_at"], int)


def test_a_broken_outcome_costs_the_buttons_not_the_label(tmp_path) -> None:
    """The other direction: a malformed outcome is not fatal either."""
    run = _run(tmp_path, title="Fix the login loop")
    header = build_header(run, None)  # type: ignore[arg-type]
    assert header["label"] == "Fix the login loop"
    assert header["actions"] == [] and header["menu"] == []


# ── the page wiring ─────────────────────────────────────────────────────────

def test_story_triage_gives_up_its_buttons(tmp_path) -> None:
    """The header draws the controls now: the triage keeps the outcome's reading
    (text, tone, icon, detail, counts) and carries no actions or menu."""
    run = _run(tmp_path, title="Fix the login loop")
    outcome = _running_outcome(run)
    triage = R._story_tab(run, outcome)[0]
    assert triage["type"] == "triage"
    assert triage["actions"] == [] and triage["menu"] == []
    assert triage["text"] == outcome["text"]
    assert triage["tone"] == outcome["tone"] and triage["icon"] == outcome["icon"]
    assert triage["detail"] == outcome["detail"]
    assert triage["counts"] == outcome["counts"]


def test_build_v2_passes_the_header_to_the_page(tmp_path, monkeypatch) -> None:
    """``_build_v2`` passes ``label`` / ``actions`` / ``menu`` / ``updated_at``
    from ``build_header`` to ``S.page``, on every tab — and the triage below it
    still draws none of them."""
    run = _run(tmp_path, title="Fix the login loop")
    outcome = _running_outcome(run)
    monkeypatch.setattr(R.O, "resolve", lambda _run: outcome)
    for tab in ("story", "graph", "kickoff"):
        page = R._build_v2(run, tab, {"run": run.id})
        assert page["label"] == "Fix the login loop"
        assert [a["label"] for a in page["actions"]] == ["Stop", "Kill"]
        assert "Certify this change" not in [m["label"] for m in page["menu"]]
        assert isinstance(page["updated_at"], int)
        if tab == "story":
            assert page["sections"][0]["actions"] == []
            assert page["sections"][0]["menu"] == []


# ── the spec.py contract the header rides on ────────────────────────────────

def test_page_without_the_new_kwargs_keeps_its_keys() -> None:
    """Every existing page is byte-identical: no caller that passes none of the
    new kwargs sees a new key."""
    page = S.page("run", "The title", "sub line", chips_=[S.chip("running", "yellow")],
                  actions=[S.btn("Stop", S.cli("board", "stop", "rp-1"), "warn")],
                  tabs=[("story", "Story")], tab="story", args={"run": "rp-1"},
                  sections=[S.triage("Running", tone="yellow")], errors={}, desc="d")
    assert set(page) == _OLD_PAGE_KEYS
    for key in ("label", "menu", "crumbs", "updated_at"):
        assert key not in page


def test_page_emits_the_new_fields_only_when_non_empty() -> None:
    """Empty is absent: an empty label / menu / crumbs / timestamp leaves the key
    out entirely, one at a time and together."""
    menu = [S.btn("Open run folder", S.reveal("/tmp/rp-1"), "ghost")]
    page = S.page("run", "T", label="A short label", menu=menu,
                  crumbs=[S.chip("rp-1")], updated_at=1710000000)
    assert page["label"] == "A short label"
    assert list(page["menu"]) == menu
    assert list(page["crumbs"]) == [S.chip("rp-1")]
    assert page["updated_at"] == 1710000000
    for kwargs in ({"label": ""}, {"menu": []}, {"crumbs": []}, {"updated_at": None}):
        emitted = S.page("run", "T", **kwargs)
        for key in kwargs:
            assert key not in emitted, f"{key}={kwargs[key]!r} is empty — key must stay out"


def test_tabs_accept_dicts_and_pass_count_and_tone_through() -> None:
    """A tab is a ``(key, label)`` pair or a dict; a dict's ``count`` and
    ``tone`` reach the IDE, and a dict without them emits neither."""
    page = S.page("run", "T", tabs=[
        ("story", "Story"),
        {"key": "agents", "label": "Agents", "count": 3, "tone": "yellow"},
        {"key": "graph", "label": "Graph"},
    ])
    assert page["tabs"][0] == {"key": "story", "label": "Story"}
    assert page["tabs"][1]["count"] == 3 and page["tabs"][1]["tone"] == "yellow"
    assert page["tabs"][2] == {"key": "graph", "label": "Graph"}


def test_btn_carries_done_and_pending_only_when_asked() -> None:
    """``btn`` sets the two contract-v2.1 strings on the action's ``do`` dict; a
    button built without them keeps its exact old shape, and ``do=None`` (a
    disabled button) is left alone rather than crashed on."""
    do = S.cli("board", "stop", "rp-1", confirm="Stop this run after its current node?")
    button = S.btn("Stop", do, "warn", pending="Stopping…", done="Run will stop after this node")
    assert button["do"]["pending"] == "Stopping…"
    assert button["do"]["done"] == "Run will stop after this node"
    assert button["do"]["confirm"] == "Stop this run after its current node?"
    plain_do = {"cli": ["board", "stop", "rp-1"]}
    assert S.btn("Stop", plain_do, "warn") == {"label": "Stop", "kind": "warn", "do": plain_do}
    assert S.btn("Stop", None, "warn", pending="Stopping…", done="Run killed")["do"] is None


def test_copy_is_a_clipboard_action() -> None:
    """``copy(text)`` is an action dict, not a button: the run id reaches the
    clipboard and the toast says so."""
    assert S.copy("rp-e3-header-v2") == {"copy": "rp-e3-header-v2", "done": "Copied"}
    assert S.copy("rp-e3-header-v2", done="Copied run id") == {
        "copy": "rp-e3-header-v2", "done": "Copied run id"}


def test_wall_time_is_a_duration_string(tmp_path) -> None:
    """``_wall`` reports the node's own seconds — a node that ran 30 s reads
    ``30s``, never ``None``/``0s`` (the DAG cell and the inspector share it)."""
    node = R.Node(id="implementer", start=100, end=130)
    assert R._wall(node) == "30s"
    assert R._wall(R.Node(id="planner")) == "—"
