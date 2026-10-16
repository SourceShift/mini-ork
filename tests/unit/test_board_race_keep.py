"""``board race-keep`` — keep one race contestant, discard the rest (FR-030).

The verb exists because the IDE action vocabulary cannot chain ``merge &&
discard && discard``, and a half-decided race — winner merged, runners-up still
holding worktrees — is the state it prevents. Two properties carry the safety:

* every discard must share the winner's kickoff, and an unreadable kickoff is
  ``""``, which matches nothing (not even another ``""``);
* nothing is discarded unless the merge landed, so a refused merge leaves the
  race exactly as it was.

``merge``/``discard`` are the git-touching seams, so they are replaced here; the
workspace records they resolve are real files, because resolving them is half of
what the verb does.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

from mini_ork import workspaces
from mini_ork.cli import board_cmd
from mini_ork.stores import migrate as mig

REPO = Path(__file__).resolve().parents[2]
WINNER = "run-1791000000-aaaaaa"
LOSER = "run-1791000001-bbbbbb"
OTHER = "run-1791000002-cccccc"


@pytest.fixture()
def home(tmp_path: Path) -> Path:
    h = tmp_path / "proj" / ".mini-ork"
    h.mkdir(parents=True)
    rc, _out, err = mig.init_db(db=str(h / "state.db"), root=str(REPO))
    assert rc == 0, err
    return h


def _contestant(home: Path, run_id: str, *, title: str, lane: str = "codex",
                adopted: bool = False, workspace: bool = True,
                kickoff: bool = True) -> Path:
    """One race contestant: a run dir, its ``task_runs`` row, its worktree record."""
    run_dir = home / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    if kickoff:
        (run_dir / "kickoff.md").write_text(f"# {title}\n\nDo the thing.\n", encoding="utf-8")
    (run_dir / "config").mkdir(exist_ok=True)
    (run_dir / "config" / "agents.race.yaml").write_text(
        f"lanes:\n  implementer: {lane}\n", encoding="utf-8")
    con = sqlite3.connect(home / "state.db")
    now = int(time.time())
    con.execute("INSERT OR REPLACE INTO task_runs (id, recipe, status, cost_usd, created_at, "
                "updated_at, task_class, kickoff_path, workflow_version) VALUES (?,?,?,?,?,?,?,?,?)",
                (run_id, "code-fix", "published", 0.4, now, now, "code_fix",
                 str(run_dir / "kickoff.md") if kickoff else "", "latest"))
    con.commit()
    con.close()
    if workspace:
        wt = home / "worktrees" / run_id
        wt.mkdir(parents=True, exist_ok=True)
        (home / "worktrees" / f"{run_id}.json").write_text(json.dumps({
            "run_id": run_id, "path": str(wt), "branch": f"mini-ork/{run_id}",
            "base_branch": "main", "base_sha": "0" * 40, "project": str(home.parent),
            "adopted": adopted, "home": str(home),
        }), encoding="utf-8")
    return run_dir


def _race(home: Path, *run_ids: str, title: str = "Debounce the search box") -> None:
    for index, run_id in enumerate(run_ids):
        _contestant(home, run_id, title=title, lane=f"lane{index}")


def _spy(monkeypatch, *, merge: dict | None = None,
         discard_fail: set[str] | None = None) -> dict[str, list]:
    """Replace the two git seams and record how they were called."""
    calls: dict[str, list] = {"merge": [], "discard": []}

    def fake_merge(ws, message, *, approver=None):
        calls["merge"].append({"run_id": ws.run_id, "message": message, "approver": approver})
        return dict(merge or {"ok": True, "merged": "abc1234"})

    def fake_discard(ws, *, approver=None):
        calls["discard"].append({"run_id": ws.run_id, "approver": approver})
        if discard_fail and ws.run_id in discard_fail:
            return {"ok": False, "error": f"worktree busy: {ws.run_id}"}
        return {"ok": True}

    monkeypatch.setattr(workspaces, "merge", fake_merge)
    monkeypatch.setattr(workspaces, "discard", fake_discard)
    return calls


def _board(home: Path, *argv: str, capsys) -> tuple[int, dict]:
    rc = board_cmd.main([*argv, "--home", str(home)], "")
    return rc, json.loads(capsys.readouterr().out)


# ── the plan ───────────────────────────────────────────────────────────────

def test_dry_run_returns_the_plan_and_touches_nothing(home: Path, monkeypatch, capsys) -> None:
    _race(home, WINNER, LOSER)
    calls = _spy(monkeypatch)
    rc, payload = _board(home, "race-keep", WINNER, "--discard", LOSER, "--dry-run", capsys=capsys)
    assert rc == 0 and payload["ok"] is True and payload["dry_run"] is True
    assert payload["winner"] == WINNER
    assert [entry["run_id"] for entry in payload["discard"]] == [LOSER]
    # The numbers a confirm needs are the ones the page cannot compute itself.
    assert payload["frees"] == [str(home / "worktrees" / LOSER)]
    assert calls == {"merge": [], "discard": []}


def test_an_adopted_worktree_is_not_reported_as_freed(home: Path, monkeypatch, capsys) -> None:
    _race(home, WINNER)
    _contestant(home, LOSER, title="Debounce the search box", lane="glm", adopted=True)
    _spy(monkeypatch)
    _rc, payload = _board(home, "race-keep", WINNER, "--discard", LOSER, "--dry-run", capsys=capsys)
    assert payload["discard"] == [
        {"run_id": LOSER, "worktree": str(home / "worktrees" / LOSER), "frees_worktree": False}]
    assert payload["frees"] == []


def test_a_repeated_discard_is_one_worktree(home: Path, monkeypatch, capsys) -> None:
    _race(home, WINNER, LOSER)
    calls = _spy(monkeypatch)
    _rc, payload = _board(home, "race-keep", WINNER, "--discard", LOSER, "--discard", LOSER,
                          capsys=capsys)
    assert [entry["run_id"] for entry in payload["discard"]] == [LOSER]
    assert [call["run_id"] for call in calls["discard"]] == [LOSER]


# ── the refusals ───────────────────────────────────────────────────────────

def test_a_discard_from_another_kickoff_is_refused(home: Path, monkeypatch, capsys) -> None:
    _race(home, WINNER, LOSER)
    _contestant(home, OTHER, title="A completely different task", lane="glm")
    calls = _spy(monkeypatch)
    rc, payload = _board(home, "race-keep", WINNER, "--discard", OTHER, capsys=capsys)
    assert rc == 1 and payload["ok"] is False and payload["step"] == "plan"
    assert payload["run_id"] == OTHER
    assert "not a contestant of the same race" in payload["error"]
    # A refused plan is a refusal: neither half of the act happened.
    assert calls == {"merge": [], "discard": []}


def test_an_unreadable_kickoff_refuses_rather_than_matching_emptily(
        home: Path, monkeypatch, capsys) -> None:
    """Two unreadable kickoffs are both ``""`` — which must not read as "same"."""
    _race(home, WINNER, LOSER)
    (home / "runs" / WINNER / "kickoff.md").unlink()
    _contestant(home, LOSER, title="Debounce the search box")
    calls = _spy(monkeypatch)
    rc, payload = _board(home, "race-keep", WINNER, "--discard", LOSER, capsys=capsys)
    assert rc == 1 and payload["ok"] is False and payload["step"] == "plan"
    assert "cannot be read" in payload["error"]
    assert calls == {"merge": [], "discard": []}


def test_a_winner_without_a_workspace_is_refused(home: Path, monkeypatch, capsys) -> None:
    _race(home, WINNER, LOSER)
    (home / "worktrees" / f"{WINNER}.json").unlink()
    calls = _spy(monkeypatch)
    rc, payload = _board(home, "race-keep", WINNER, "--discard", LOSER, capsys=capsys)
    assert rc == 1 and payload["ok"] is False and payload["step"] == "winner"
    assert "has no open workspace" in payload["error"]
    assert calls == {"merge": [], "discard": []}


def test_discarding_the_winner_is_refused(home: Path, monkeypatch, capsys) -> None:
    _race(home, WINNER, LOSER)
    calls = _spy(monkeypatch)
    rc, payload = _board(home, "race-keep", WINNER, "--discard", WINNER, capsys=capsys)
    assert rc == 1 and payload["ok"] is False and payload["step"] == "plan"
    assert "names the winner" in payload["error"]
    assert calls == {"merge": [], "discard": []}


def test_a_run_id_is_required(capsys) -> None:
    assert board_cmd.main(["race-keep"], "") == 2


# ── the act ────────────────────────────────────────────────────────────────

def test_keep_merges_the_winner_then_discards_the_rest(home: Path, monkeypatch, capsys) -> None:
    _race(home, WINNER, LOSER)
    _contestant(home, OTHER, title="Debounce the search box", lane="glm")
    calls = _spy(monkeypatch)
    rc, payload = _board(home, "race-keep", WINNER, "--discard", LOSER, "--discard", OTHER,
                         capsys=capsys)
    assert rc == 0 and payload["ok"] is True and "step" not in payload
    assert [call["run_id"] for call in calls["merge"]] == [WINNER]
    assert [call["run_id"] for call in calls["discard"]] == [LOSER, OTHER]
    assert sorted(entry["run_id"] for entry in payload["discarded"]) == sorted([LOSER, OTHER])
    assert payload["failed"] == []


def test_without_force_the_merge_asserts_no_approval(home: Path, monkeypatch, capsys) -> None:
    _race(home, WINNER, LOSER)
    calls = _spy(monkeypatch)
    _board(home, "race-keep", WINNER, "--discard", LOSER, capsys=capsys)
    assert calls["merge"][0]["approver"] is None
    # The discard still records who gave the worktree up — that is an audit
    # line, not an approval, and discarding never needs one.
    assert str(calls["discard"][0]["approver"]).startswith("ide:")


def test_force_supplies_the_operator_identity_as_the_approver(
        home: Path, monkeypatch, capsys) -> None:
    _race(home, WINNER, LOSER)
    calls = _spy(monkeypatch)
    _board(home, "race-keep", WINNER, "--discard", LOSER, "--force", capsys=capsys)
    assert str(calls["merge"][0]["approver"]).startswith("ide:")


def test_the_merge_message_names_the_run(home: Path, monkeypatch, capsys) -> None:
    _race(home, WINNER, LOSER)
    calls = _spy(monkeypatch)
    _board(home, "race-keep", WINNER, "--discard", LOSER, capsys=capsys)
    assert WINNER in calls["merge"][0]["message"]


def test_a_refused_merge_stops_before_the_first_discard(
        home: Path, monkeypatch, capsys) -> None:
    """Dropping the runners-up when the winner did not land leaves nothing."""
    _race(home, WINNER, LOSER)
    calls = _spy(monkeypatch, merge={"ok": False, "error": "the project is on 'x'",
                                     "needs_approval": True})
    rc, payload = _board(home, "race-keep", WINNER, "--discard", LOSER, capsys=capsys)
    assert rc == 1 and payload["ok"] is False and payload["step"] == "merge"
    assert payload["needs_approval"] is True
    assert payload["merge"]["error"] == "the project is on 'x'"
    assert calls["discard"] == []
    # The plan still rides along, so a caller can retry with --force.
    assert payload["discard"][0]["run_id"] == LOSER


def test_a_partial_discard_failure_is_reported_not_masked(
        home: Path, monkeypatch, capsys) -> None:
    _race(home, WINNER, LOSER)
    _contestant(home, OTHER, title="Debounce the search box", lane="glm")
    calls = _spy(monkeypatch, discard_fail={LOSER})
    rc, payload = _board(home, "race-keep", WINNER, "--discard", LOSER, "--discard", OTHER,
                         capsys=capsys)
    assert rc == 1 and payload["ok"] is False and payload["step"] == "discard"
    assert [entry["run_id"] for entry in payload["discarded"]] == [OTHER]
    assert payload["failed"] == [{"run_id": LOSER, "error": f"worktree busy: {LOSER}"}]
    # It continued past the failure rather than stopping at the first one.
    assert [call["run_id"] for call in calls["discard"]] == [LOSER, OTHER]
    assert payload["merged"] is True


def test_the_reason_reaches_stderr(home: Path, monkeypatch, capsys) -> None:
    """A refusal a person cannot see is a button that silently does nothing."""
    _race(home, WINNER, LOSER)
    _contestant(home, OTHER, title="A completely different task", lane="glm")
    _spy(monkeypatch)
    rc = board_cmd.main(["race-keep", WINNER, "--discard", OTHER, "--home", str(home)], "")
    err = capsys.readouterr().err
    assert rc == 1 and "not a contestant of the same race" in err
