"""The attention surface: one declared taxonomy, one stage resolver, one inbox.

The point of this module is that a sixth "needs you" source cannot be added
without declaring it. The tests below are what pin that:

* every word of the IDE's two state maps (``outcome.resolve``'s four and the
  kanban's six) resolves to a declared row;
* every row's ``next`` names a subcommand that exists in the registry, so a
  renamed verb fails here and not in an operator's shell;
* ``stage_of`` answers the four stages from the stores that already hold the
  facts, and the inbox unions them in the declared order.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from mini_ork import attention  # noqa: E402
from mini_ork.gates import merge_approval, node_review  # noqa: E402
from mini_ork.ide_pages import kanban  # noqa: E402
from mini_ork.stores import migrate as mig  # noqa: E402


@pytest.fixture
def home(tmp_path: Path) -> Path:
    h = tmp_path / "proj" / ".mini-ork"
    (h / "runs").mkdir(parents=True)
    rc, _, err = mig.init_db(db=str(h / "state.db"), root=str(REPO))
    assert rc == 0, err
    return h


def _run(home: Path, run_id: str, status: str, *, title: str | None = None) -> None:
    kickoff = home / "kickoffs" / f"{run_id}.md"
    kickoff.parent.mkdir(parents=True, exist_ok=True)
    kickoff.write_text(f"# {title or run_id}\n", encoding="utf-8")
    with sqlite3.connect(home / "state.db") as con:
        con.execute(
            "INSERT INTO task_runs (id, task_class, recipe, status, cost_usd, "
            "created_at, updated_at, kickoff_path) "
            "VALUES (?, 'code-fix', 'code-fix', ?, 0, ?, ?, ?)",
            (run_id, status, 1_700_000_000, 1_700_000_100, str(kickoff)),
        )


def _gate(home: Path, *, feature: str, gate_id: str, context: dict | None = None) -> None:
    with sqlite3.connect(home / "state.db") as con:
        con.execute(
            "INSERT INTO mo_inbox_gates (gate_id, feature, phase, context_json, "
            "status, enqueued_at) VALUES (?, ?, 'execute', ?, 'pending', 1)",
            (gate_id, feature, json.dumps(context or {})),
        )


# ── the taxonomy ───────────────────────────────────────────────────────────


def test_keys_are_unique_and_ordered():
    keys = [row.key for row in attention.TAXONOMY]
    assert len(keys) == len(set(keys))
    # Severity order: money, decisions, then the accumulate-only channel.
    assert keys.index("cost_pause") < keys.index("merge_approval")
    assert keys.index("worktree") < keys.index("oversight_gate")


def test_every_declared_stage_is_one_of_the_four():
    assert attention.stage_states() == ("person", "working", "idle", "ended")
    assert {row.state for row in attention.TAXONOMY} <= set(attention.stage_states())


def test_every_outcome_state_resolves_to_a_declared_row():
    """``outcome.resolve`` produces exactly these four words; the IDE and the
    inbox must never disagree about what one means."""
    for state in ("running", "needs_you", "failed", "done"):
        key = attention.row_for_outcome(state)
        row = attention.taxonomy(key)
        assert key in attention._BY_KEY, state
        assert row.state in attention.stage_states()


def test_every_ticket_state_resolves_to_a_declared_row():
    for state in kanban._TICKET_STATE:
        key = attention.row_for_ticket(state)
        assert key in attention._BY_KEY, state


def test_every_command_names_a_registered_subcommand():
    from mini_ork.cli.main import SUBCOMMAND_REGISTRY

    for row in attention.TAXONOMY:
        if not row.command:
            continue
        parts = row.command.split()
        assert parts[0] == "mini-ork", row.key
        assert parts[1] in SUBCOMMAND_REGISTRY, (row.key, parts[1])


def test_a_row_that_needs_nothing_carries_no_command():
    for row in attention.TAXONOMY:
        if row.state in ("working", "idle", "ended"):
            assert row.command == "", row.key


# ── the stage resolver ─────────────────────────────────────────────────────


def test_an_unknown_run_is_idle(home: Path):
    stage = attention.stage_of(home, "run-nope")
    assert (stage.state, stage.key) == ("idle", "idle")


def test_a_live_run_is_working(home: Path):
    _run(home, "run-live", "executing")
    stage = attention.stage_of(home, "run-live")
    assert (stage.state, stage.key) == ("working", "working")
    assert "executing" in stage.why


def test_a_published_run_is_done(home: Path):
    _run(home, "run-done", "published")
    stage = attention.stage_of(home, "run-done")
    assert (stage.state, stage.key) == ("ended", "done")


def test_a_cost_pause_beats_every_other_stage(home: Path):
    _run(home, "run-paused", "executing")
    (home / "runs" / "run-paused").mkdir(parents=True)
    (home / "runs" / "run-paused" / ".cost-pause").write_text("", encoding="utf-8")
    _gate(home, feature=merge_approval.FEATURE, gate_id="merge:run-paused")

    stage = attention.stage_of(home, "run-paused")
    assert (stage.state, stage.key) == ("person", "cost_pause")


def test_a_merge_approval_outranks_a_node_review(home: Path):
    _run(home, "run-gated", "reviewing")
    _gate(home, feature=node_review.FEATURE,
          gate_id="node:run-gated:impl:100")
    _gate(home, feature=merge_approval.FEATURE, gate_id="merge:run-gated")

    stage = attention.stage_of(home, "run-gated")
    assert (stage.state, stage.key) == ("person", "merge_approval")


def test_a_pending_node_review_names_the_node(home: Path):
    _run(home, "run-node", "reviewing")
    _gate(home, feature=node_review.FEATURE, gate_id="node:run-node:verify:100")

    stage = attention.stage_of(home, "run-node")
    assert (stage.state, stage.key) == ("person", "node_review")
    assert "verify" in stage.why


def test_a_failed_run_with_a_retry_hint_needs_a_person(home: Path, monkeypatch):
    _run(home, "run-failed", "failed")
    monkeypatch.setattr(
        attention, "_retry_hint",
        lambda h, r: {"retryable": True, "failed_node": "impl", "strategy": "retry"},
    )
    stage = attention.stage_of(home, "run-failed")
    assert (stage.state, stage.key) == ("person", "retry")
    assert "impl" in stage.why


def test_a_failed_run_that_cannot_be_retried_is_over(home: Path, monkeypatch):
    _run(home, "run-dead", "failed")
    monkeypatch.setattr(attention, "_retry_hint", lambda h, r: {"retryable": False})
    stage = attention.stage_of(home, "run-dead")
    assert stage.state == "ended"
    assert stage.key == "ended"


# ── the inbox ──────────────────────────────────────────────────────────────


def test_an_empty_home_has_nothing_to_show(tmp_path: Path):
    assert attention.inbox(tmp_path / "nothing") == []
    assert attention.render([]) == "Nothing needs you.\n"


def test_rows_are_ordered_by_the_declared_rank(home: Path, monkeypatch):
    _run(home, "run-work", "executing")
    (home / "runs" / "run-work").mkdir(parents=True)
    (home / "runs" / "run-work" / ".cost-pause").write_text("", encoding="utf-8")
    _run(home, "run-failed", "failed")
    monkeypatch.setattr(
        attention, "_retry_hint",
        lambda h, r: {"retryable": True, "failed_node": "impl"} if r == "run-failed" else None,
    )

    rows = attention.inbox(home)
    assert [r.key for r in rows] == ["cost_pause", "retry"]
    assert rows[0].next == "mini-ork resume run-work"
    assert rows[1].next == "mini-ork board retry run-failed"


def test_a_run_is_shown_once(home: Path, monkeypatch):
    """A gated run whose worktree also exists is one decision, not two."""
    _run(home, "run-once", "reviewing")
    _gate(home, feature=merge_approval.FEATURE, gate_id="merge:run-once")
    _fake_workspaces(monkeypatch, home, "run-once", ahead=2)

    rows = attention.inbox(home)
    assert [r.key for r in rows] == ["merge_approval"]


def test_a_terminal_run_with_a_worktree_is_a_decision(home: Path, monkeypatch):
    _run(home, "run-wt", "published")
    _fake_workspaces(monkeypatch, home, "run-wt", ahead=3)

    rows = attention.inbox(home)
    assert [r.key for r in rows] == ["worktree"]
    assert rows[0].next == "mini-ork board merge run-wt"
    assert "3 commits" in rows[0].why


def test_a_live_run_worktree_is_not_offered(home: Path, monkeypatch):
    _run(home, "run-live", "executing")
    _fake_workspaces(monkeypatch, home, "run-live", ahead=1)
    assert attention.inbox(home) == []


def test_an_oversight_gate_survives_without_a_run(home: Path):
    _gate(home, feature="retry_precondition", gate_id="retry:whatever")
    rows = attention.inbox(home)
    assert [r.key for r in rows] == ["oversight_gate"]
    assert rows[0].next.startswith("mini-ork oversight --resolve ")


def test_the_render_says_when_it_is_truncated(home: Path):
    for i in range(3):
        _gate(home, feature="retry_precondition", gate_id=f"retry:{i}")
    rows = attention.inbox(home, limit=1)
    out = attention.render(rows, total=3)
    assert "showing 1 of 3 waiting" in out
    assert "1 needs you" in out


def test_json_rows_carry_the_four_contract_fields(home: Path, monkeypatch):
    _run(home, "run-json", "failed")
    monkeypatch.setattr(
        attention, "_retry_hint",
        lambda h, r: {"retryable": True, "failed_node": "impl"},
    )
    row = attention.inbox(home)[0]
    payload = row.to_dict()
    for key in ("run_id", "what", "why", "next", "label", "state"):
        assert payload[key], key


# ── helpers ────────────────────────────────────────────────────────────────


class _FakeWorkspace:
    def __init__(self, home: Path, run_id: str):
        self.run_id = run_id
        self.path = home / "worktrees" / run_id
        self.branch = f"mini-ork/{run_id}"
        self.base_branch = "main"
        self.home = home


def _workspace(home: Path, run_id: str) -> _FakeWorkspace:
    ws = _FakeWorkspace(home, run_id)
    ws.path.mkdir(parents=True, exist_ok=True)
    (home / "worktrees").mkdir(parents=True, exist_ok=True)
    (home / "worktrees" / f"{run_id}.json").write_text(
        json.dumps({"run_id": run_id, "path": str(ws.path), "branch": ws.branch,
                    "base_branch": "main", "base_sha": "abc", "project": str(home),
                    "home": str(home)}),
        encoding="utf-8",
    )
    return ws


def _fake_workspaces(monkeypatch, home: Path, run_id: str, *, ahead: int) -> None:
    """A worktree record on disk, and a ``git``-free answer about its contents.

    ``workspaces.status`` shells out to git; the row builder only reads the
    four fields below, so a real repository would buy nothing but runtime.
    """
    from mini_ork import workspaces

    ws = _workspace(home, run_id)
    monkeypatch.setattr(workspaces, "list_open", lambda h: [ws])
    monkeypatch.setattr(
        workspaces,
        "status",
        lambda w: {"exists": True, "commits_ahead": ahead, "uncommitted": [],
                   "added": 10, "removed": 1, "files": []},
    )
