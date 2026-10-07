"""``needs-you`` tells the truth: one count, gates that close when their
worktree merges, no same-second spinner.

Covers ``kickoffs/auto/needs-you-truth.md``:

- ``run_mark`` counts a pending retry gate as needs_you — so the cheap tile
  count and the precise list agree ("one count").
- ``supersede_gates_for_target`` closes gates bound to a merged/removed
  worktree, idempotently.
- ``merge_worktree`` / ``clean_worktree`` call it once, fail-soft after the
  push / before the removal.
- ``derive_node_statuses`` never resurrects a same-second node to ``running``.

Hermetic: ``tmp_path`` homes, ``mig.init_db`` for the schema, no LLM, no
network, no live home.
"""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
import time
import types
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from mini_ork.acp import fleet as fleet_mod  # noqa: E402
from mini_ork.acp.task_state import MARKS, run_mark  # noqa: E402
from mini_ork.gates import oversight_inbox  # noqa: E402
from mini_ork.recovery import retry_notify  # noqa: E402
from mini_ork.stores import migrate as mig  # noqa: E402
from mini_ork.web.repositories import derive_node_statuses  # noqa: E402

WORKTREE_PY = REPO / "scripts" / "mini_ork_worktree.py"


# ── fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def home(tmp_path: Path) -> Path:
    h = tmp_path / "proj" / ".mini-ork"
    h.mkdir(parents=True)
    rc, _, err = mig.init_db(db=str(h / "state.db"), root=str(REPO))
    assert rc == 0, err
    return h


def _db(home: Path) -> str:
    return str(home / "state.db")


def _seed_run(home: Path, run_id: str, *, status: str = "failed",
              target_repo: str | Path | None = None) -> Path:
    """A run dir + one ``task_runs`` row, optionally naming a target repo."""
    run_dir = home / "runs" / run_id
    run_dir.mkdir(parents=True)
    ts = int(time.time())
    con = sqlite3.connect(home / "state.db")
    con.execute(
        "INSERT INTO task_runs (id, recipe, status, cost_usd, created_at, "
        "updated_at, ended_at, task_class, kickoff_path, workflow_version) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (run_id, "framework-edit", status, 0.5, ts, ts + 100, ts + 80,
         "framework_edit", str(home / "kickoffs" / "k.md"), "latest"))
    con.commit()
    con.close()
    profile: dict[str, Any] = {"recipe": "framework-edit"}
    if target_repo is not None:
        profile["target_repo"] = str(target_repo)
        profile["roots"] = {"target": str(target_repo), "home": str(home)}
    (run_dir / "run_profile.json").write_text(
        json.dumps(profile), encoding="utf-8")
    return run_dir


def _open_gate(home: Path, run_dir: Path, run_id: str) -> int:
    """A pending ``retry_precondition`` row + the pointer that names it."""
    iid = oversight_inbox.enqueue(
        retry_notify.GATE_ID, run_id, "retry",
        {"hint": {"needs_change": {"kind": "environment",
                                   "summary": "set FOO_BAR"}}},
        blocks_dispatch_for=run_id, db_path=_db(home))
    retry_notify._write_gate_pointer(run_dir, iid, run_id)
    return iid


# ── 1. one count: a pending gate is needs_you everywhere ─────────────────────


def test_pending_gate_is_needs_you_in_mark_fleet_and_task_state(home: Path) -> None:
    """The tile count and the list must agree — the whole bug."""
    run_dir = _seed_run(home, "run-gate-1", status="rolled_back")
    _open_gate(home, run_dir, "run-gate-1")

    assert run_mark("rolled_back", run_dir) == MARKS["needs_you"]

    rows, counts = fleet_mod.fleet_rows(home)
    shown = [r for r in rows if r.run_id == "run-gate-1"]
    assert len(shown) == 1
    assert shown[0].state == "needs_you"
    assert shown[0].mark == MARKS["needs_you"]
    # One count: the cheap tile bucket equals what the list calls needs_you.
    assert counts["needs_you"] == 1
    assert counts["needs_you"] == sum(1 for r in rows if r.state == "needs_you")


# ── 2. resolved / absent gate → failed, no DB touch when pointer absent ──────


def test_absent_pointer_is_failed_and_never_opens_the_db(
    home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = _seed_run(home, "run-nogate", status="failed")

    def boom(*_a: Any, **_k: Any) -> None:
        raise AssertionError("oversight_inbox.get called without a pointer file")

    monkeypatch.setattr(oversight_inbox, "get", boom)
    assert run_mark("failed", run_dir) == MARKS["failed"]


def test_resolved_gate_rows_back_to_failed(home: Path) -> None:
    run_dir = _seed_run(home, "run-resolved", status="failed")
    iid = _open_gate(home, run_dir, "run-resolved")
    oversight_inbox.resolve(iid, "approved", db_path=_db(home))
    assert run_mark("failed", run_dir) == MARKS["failed"]


# ── 3. supersede_gates_for_target ────────────────────────────────────────────


def test_supersede_closes_matching_target_and_leaves_others(
    home: Path, tmp_path: Path,
) -> None:
    wt = tmp_path / "mini-ork-worktrees" / "slug"
    wt.mkdir(parents=True)
    other = tmp_path / "elsewhere"
    other.mkdir()
    run_a = _seed_run(home, "run-a", status="failed", target_repo=wt)
    run_b = _seed_run(home, "run-b", status="failed", target_repo=other)
    iid_a = _open_gate(home, run_a, "run-a")
    iid_b = _open_gate(home, run_b, "run-b")

    closed = retry_notify.supersede_gates_for_target(
        home, wt, "superseded: merged to main as abc1234")
    assert closed == ["run-a"]

    row_a = oversight_inbox.get(iid_a, db_path=_db(home))
    assert row_a is not None and row_a["status"] == "rejected"
    assert row_a["review_note"] == "superseded: merged to main as abc1234"
    pointer = json.loads(
        (run_a / retry_notify.GATE_POINTER_FILENAME).read_text(encoding="utf-8"))
    assert pointer["abandoned"] is True
    assert pointer["superseded"] == "superseded: merged to main as abc1234"

    # The unrelated target's gate is untouched.
    row_b = oversight_inbox.get(iid_b, db_path=_db(home))
    assert row_b is not None and row_b["status"] == "pending"

    # Idempotent: a second call resolves nothing and returns [].
    assert retry_notify.supersede_gates_for_target(home, wt, "again") == []


def test_supersede_matches_a_realpath_equal_symlink(
    home: Path, tmp_path: Path,
) -> None:
    real = tmp_path / "real-wt"
    real.mkdir()
    link = tmp_path / "link-wt"
    link.symlink_to(real)
    run = _seed_run(home, "run-link", status="failed", target_repo=link)
    _open_gate(home, run, "run-link")

    closed = retry_notify.supersede_gates_for_target(
        home, real, "superseded: merged")
    assert closed == ["run-link"]


def test_supersede_never_raises_on_a_bad_file(home: Path, tmp_path: Path) -> None:
    run = _seed_run(home, "run-bad", status="failed", target_repo=str(tmp_path))
    (run / retry_notify.GATE_POINTER_FILENAME).write_text("{not json", encoding="utf-8")
    assert retry_notify.supersede_gates_for_target(home, tmp_path, "n") == []


# ── 4. merge_worktree / clean_worktree call sites ────────────────────────────


def _load_wt_script():
    spec = importlib.util.spec_from_file_location("mo_wt_under_test", WORKTREE_PY)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _stub_git(*_a: Any, **_k: Any) -> types.SimpleNamespace:
    return types.SimpleNamespace(stdout="", returncode=0)


def test_merge_calls_supersede_once_after_push(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    mod = _load_wt_script()
    wt_dir = tmp_path / "worktrees" / "slug"
    wt_dir.mkdir(parents=True)
    monkeypatch.setattr(mod, "WORKTREES_DIR", str(tmp_path / "worktrees"))
    calls: list[tuple[Any, Any, Any]] = []
    order: list[str] = []

    def supersede(home: Any, target: Any, note: Any) -> list[str]:
        order.append("supersede")
        calls.append((home, target, note))
        return ["run-a"]

    def git(*args: Any, **_k: Any) -> types.SimpleNamespace:
        if "push" in args:
            order.append("push")
        return _stub_git()

    monkeypatch.setattr(retry_notify, "supersede_gates_for_target", supersede)
    monkeypatch.setattr(mod, "git", git)
    monkeypatch.setattr(mod.subprocess, "run",
                        lambda *_a, **_k: types.SimpleNamespace(returncode=0))

    mod.merge_worktree(["slug"])

    assert len(calls) == 1
    assert calls[0][1] == str(wt_dir)
    assert "merged to main as" in calls[0][2]
    # The gate closes only once the work is actually on main.
    assert order == ["push", "supersede"]


def test_merge_survives_a_supersede_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    mod = _load_wt_script()
    (tmp_path / "worktrees" / "slug").mkdir(parents=True)
    monkeypatch.setattr(mod, "WORKTREES_DIR", str(tmp_path / "worktrees"))

    def boom(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("gate blew up")

    monkeypatch.setattr(retry_notify, "supersede_gates_for_target", boom)
    monkeypatch.setattr(mod, "git", _stub_git)
    monkeypatch.setattr(mod.subprocess, "run",
                        lambda *_a, **_k: types.SimpleNamespace(returncode=0))

    mod.merge_worktree(["slug"])  # must not raise — the push already landed
    assert "retry gates" in capsys.readouterr().err  # fail soft, but visibly


def test_clean_calls_supersede_before_removal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    mod = _load_wt_script()
    wt_dir = tmp_path / "worktrees" / "slug"
    wt_dir.mkdir(parents=True)
    monkeypatch.setattr(mod, "WORKTREES_DIR", str(tmp_path / "worktrees"))
    monkeypatch.setattr(mod, "ROOT", str(tmp_path / "root"))
    monkeypatch.setattr(mod, "assert_root", lambda: None)
    monkeypatch.setattr(mod, "release_ownership", lambda _slug: None)
    monkeypatch.setattr(mod, "_concord", lambda *_a, **_k: None)
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        retry_notify, "supersede_gates_for_target",
        lambda home, target, note: calls.append((target, note)) or [])
    monkeypatch.setattr(mod, "git", _stub_git)

    mod.clean_worktree("slug")

    assert calls == [(str(wt_dir), "superseded: worktree slug removed")]


# ── 5. same-second node events ───────────────────────────────────────────────


def _ev(node_id: str, kind: str, ts: int) -> dict[str, Any]:
    return {"event_type": kind, "created_at": ts,
            "payload_json": json.dumps({"node_id": node_id})}


def test_same_second_end_before_start_is_done() -> None:
    rows = [_ev("rollback", "node_end", 100), _ev("rollback", "node_start", 100)]
    assert derive_node_statuses(rows)["rollback"]["status"] == "done"


def test_same_second_start_before_end_is_done() -> None:
    rows = [_ev("rollback", "node_start", 100), _ev("rollback", "node_end", 100)]
    assert derive_node_statuses(rows)["rollback"]["status"] == "done"


def test_strictly_later_start_flips_a_finished_node_to_running() -> None:
    rows = [_ev("n", "node_start", 100), _ev("n", "node_end", 100),
            _ev("n", "node_start", 200)]
    assert derive_node_statuses(rows)["n"]["status"] == "running"
