"""Unit tests: ``workspaces.merge`` / ``discard`` merge-gate behaviour.

Covers DoD 1–7b from ``hr-merge-gate.md``. The fixture pattern mirrors
``test_merge_approval.py::_isolated`` so a stray read of the operator's
real ``state.db`` cannot happen — every test runs against a tmp home /
tmp DB with the in-run env keys scrubbed. ``MINI_ORK_RUN_ID`` is
deliberately unset because the surfaces this gate is meant for are the
human ones (board, Zed, /merge, /discard); a run subprocess would refuse
the approval.
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from mini_ork import workspaces as ws_mod  # noqa: E402
from mini_ork.cli import approvals_cmd  # noqa: E402
from mini_ork.gates import merge_approval  # noqa: E402


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )


def _init_repo(tmp_path: Path, *, name: str = "proj") -> Path:
    """A fresh temp git repo on ``main`` with one initial commit."""
    project = tmp_path / name
    project.mkdir()
    assert _git(["init", "-b", "main"], project).returncode == 0
    _git(["config", "user.name", "Test"], project)
    _git(["config", "user.email", "test@example.com"], project)
    (project / "README.md").write_text("hi\n", encoding="utf-8")
    assert _git(["add", "README.md"], project).returncode == 0
    assert _git(["commit", "-m", "init"], project).returncode == 0
    return project


def _init_home(tmp_path: Path) -> Path:
    """Return the path the fixture points ``MINI_ORK_HOME`` at.

    The CLI's ``_resolve_head`` reads the workspace record from this exact
    directory, so workspace creation and ``MINI_ORK_HOME`` must agree —
    returning ``tmp_path`` itself keeps them in lockstep without a
    subdirectory dance.
    """
    return tmp_path


_POLICY_KEYS = (
    "MO_REVIEW_BEFORE_MERGE",
    "MO_REVIEW_AFTER_NODES",
    "MO_REVIEW_TIER",
    "MO_REVIEW_SIGNING_KEY",
)
_RUN_KEYS = ("MINI_ORK_RUN_ID", "MINI_ORK_RUN_DIR")


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Pin ``$MINI_ORK_HOME`` / ``$MINI_ORK_DB`` to tmp. Scrub the run-id
    keys (the surfaces under test are always outside a run) and the policy
    knobs (each test sets exactly what it needs). ``MO_WORKTREE_DIR`` and
    ``ZED_SETTINGS`` are scrubbed so ``workspaces.create`` stays inside the
    tmp filesystem (``workspaces.py:150-180``)."""
    db = str(tmp_path / "state.db")
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path))
    monkeypatch.setenv("MINI_ORK_DB", db)
    monkeypatch.setenv("MO_WORKTREE_DIR", str(tmp_path / "wts"))
    monkeypatch.delenv("ZED_SETTINGS", raising=False)
    for key in _POLICY_KEYS:
        monkeypatch.delenv(key, raising=False)
    for key in _RUN_KEYS:
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def db(tmp_path: Path) -> str:
    return str(tmp_path / "state.db")


def _make_workspace(tmp_path: Path, run_id: str = "run-merge-gate") -> tuple[Path, ws_mod.Workspace, Path]:
    """Build a project + workspace + a committed worktree change.

    Returns ``(project_path, ws, worktree_path)``. The worktree branch
    already has one commit ahead of ``ws.base_sha`` — `merge` has
    something to merge, so Step 2 doesn't discard.
    """
    project = _init_repo(tmp_path)
    home = _init_home(tmp_path)
    ws = ws_mod.create(project, home, run_id)
    (ws.path / "edit.txt").write_text("change one\n", encoding="utf-8")
    assert _git(["add", "-A"], ws.path).returncode == 0
    assert _git(["commit", "-m", "edit"], ws.path).returncode == 0
    return project, ws, ws.path


def _row(db_path: str, run_id: str) -> dict | None:
    """The latest ``mo_inbox_gates`` row for ``merge:<run_id>``.

    Returns ``None`` if the DB has no ``mo_inbox_gates`` table — that
    happens on the policy-off happy path where ``merge`` never touches
    the ledger. The caller treats both as "no pending row".
    """
    conn = sqlite3.connect(db_path)
    try:
        try:
            row = conn.execute(
                "SELECT * FROM mo_inbox_gates WHERE gate_id = ? "
                "ORDER BY inbox_id DESC LIMIT 1",
                (f"merge:{run_id}",),
            ).fetchone()
        except sqlite3.OperationalError:
            return None  # table missing — policy off, no ledger writes
        if row is None:
            return None
        keys = [d[0] for d in conn.execute("SELECT * FROM mo_inbox_gates LIMIT 0").description]
    finally:
        conn.close()
    out = dict(zip(keys, row))
    try:
        out["context"] = json.loads(out.pop("context_json") or "{}")
    except (TypeError, ValueError):
        out["context"] = {}
    return out


def _head(project: Path) -> str:
    completed = _git(["rev-parse", "HEAD"], project)
    assert completed.returncode == 0
    return completed.stdout.strip()


def _branch_tree(workspace: ws_mod.Workspace) -> str:
    completed = _git(
        ["rev-parse", f"{workspace.branch}^{{tree}}"], workspace.path
    )
    assert completed.returncode == 0
    return completed.stdout.strip()


def _worktree_status(workspace: ws_mod.Workspace) -> str:
    completed = _git(["status", "--porcelain"], workspace.path)
    assert completed.returncode == 0
    return completed.stdout


def _task_runs_create(db_path: str) -> None:
    """Bootstrap a minimal ``task_runs`` table — awaits a publish row in DoD 7b.

    The mo_inbox_gates bootstrap happens via ``merge_approval._connect``;
    do that too so a tmp DB has the schema as a real lane would.
    """
    conn = merge_approval._connect(db_path)  # noqa: SLF001 — fixture helper
    conn.execute(
        "CREATE TABLE IF NOT EXISTS task_runs ("
        "id TEXT PRIMARY KEY, "
        "task_class TEXT NOT NULL, "
        "kickoff_path TEXT NOT NULL, "
        "status TEXT"
        ")"
    )
    conn.commit()
    conn.close()


# ── DoD 1: content_sha equals <branch>^{tree} after committing ──────────────


def test_dod1_content_sha_matches_branch_tree_after_commit(tmp_path: Path) -> None:
    """Dirty worktree (tracked edit + new file) → content_sha = ``<branch>^{tree}``
    after committing the same content. HEAD and ``git status --porcelain``
    are unchanged by ``content_sha`` itself.
    """
    project = _init_repo(tmp_path)
    home = _init_home(tmp_path)
    ws = ws_mod.create(project, home, "run-1")
    head_before = _head(ws.path)
    (ws.path / "README.md").write_text("hi\n+edit\n", encoding="utf-8")
    (ws.path / "new.txt").write_text("added\n", encoding="utf-8")
    status_before = _worktree_status(ws)
    assert status_before.strip()

    tree = ws_mod.content_sha(ws)

    # ``content_sha`` does not touch HEAD or the real index.
    assert _head(ws.path) == head_before
    assert _worktree_status(ws) == status_before

    # ``tree`` should be a hex sha. We then commit the same content and
    # assert equality with the post-commit branch tree.
    assert tree and len(tree) >= 7
    assert _git(["add", "-A"], ws.path).returncode == 0
    assert _git(["commit", "-m", "edit"], ws.path).returncode == 0
    assert tree == _branch_tree(ws)


def test_dod1_content_sha_returns_empty_on_git_error(tmp_path: Path) -> None:
    """``content_sha`` returns ``""`` for any git error — never raises."""
    project = _init_repo(tmp_path)
    home = _init_home(tmp_path)
    ws = ws_mod.create(project, home, "run-empty")
    # Wipe the worktree's git link to make every git command fail.
    worktree_git = ws.path / ".git"
    if worktree_git.is_file():
        worktree_git.unlink()
    elif worktree_git.is_dir():
        import shutil
        shutil.rmtree(worktree_git)
    assert ws_mod.content_sha(ws) == ""


# ── DoD 2: Required, no approver ────────────────────────────────────────────


def test_dod2_required_no_approver_refuses(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("MO_REVIEW_BEFORE_MERGE", "required")
    project, ws, _ = _make_workspace(tmp_path, "run-2")
    project_head_before = _head(project)
    result = ws_mod.merge(ws, "merge me")
    project_head_after = _head(project)

    assert result["ok"] is False
    assert result["needs_approval"] is True
    assert project_head_after == project_head_before  # project untouched

    # A pending ``merge:<run>`` row was written.
    row = _row(str(tmp_path / "state.db"), ws.run_id)
    assert row is not None
    assert row["status"] == "pending"
    # ws.branch^{tree} is the head_sha that was pending.
    assert row["context"]["head_sha"] == _branch_tree(ws)


# ── DoD 3: Required, human approver ─────────────────────────────────────────


def test_dod3_required_with_approver_merges(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("MO_REVIEW_BEFORE_MERGE", "required")
    project, ws, _ = _make_workspace(tmp_path, "run-3")
    result = ws_mod.merge(ws, "merge me", approver="ide:test")
    assert result.get("ok") is True
    assert result.get("merged"), "merge returned no commit sha"
    # File from the worktree branch is on the project HEAD.
    assert (project / "edit.txt").exists()
    # The ledger reports approved.
    info = merge_approval.status(ws.run_id, db_path=str(tmp_path / "state.db"))
    assert info["state"] == "approved"
    assert info["approver"] == "ide:test"


# ── DoD 4: CLI approval then merge with no approver ─────────────────────────


def test_dod4_cli_approve_then_merge_no_approver(tmp_path: Path, monkeypatch) -> None:
    """``approvals_cmd.main(['approve', run])`` uses ``workspaces.content_sha``
    as the default head. After the CLI approval, ``merge(ws, 'm')`` (no
    approver) succeeds because the recorded head matches what the merge
    will compute.
    """
    monkeypatch.setenv("MO_REVIEW_BEFORE_MERGE", "required")
    project, ws, _ = _make_workspace(tmp_path, "run-4")
    rc = approvals_cmd.main(
        ["approve", ws.run_id, "--note", "lgtm"],
        root=str(tmp_path),
    )
    assert rc == 0, "CLI approval refused"
    result = ws_mod.merge(ws, "merge me")
    assert result.get("ok") is True, result
    info = merge_approval.status(ws.run_id, db_path=str(tmp_path / "state.db"))
    assert info["state"] == "approved"


# ── DoD 5: Stale approval ──────────────────────────────────────────────────


def test_dod5_stale_approval_refuses_new_content(tmp_path: Path, monkeypatch) -> None:
    """Approve against content T1; modify the worktree; ``merge(ws, 'm')``
    with no approver → the gate sees T2 and refuses.
    """
    monkeypatch.setenv("MO_REVIEW_BEFORE_MERGE", "required")
    project, ws, _ = _make_workspace(tmp_path, "run-5")

    # First approval targets the current content.
    rc = approvals_cmd.main(["approve", ws.run_id], root=str(tmp_path))
    assert rc == 0

    # Mutate the worktree AFTER the CLI approval.
    (ws.path / "edit.txt").write_text("change two\n", encoding="utf-8")

    # Merge with no approver — gate refuses because T1 != T2.
    result = ws_mod.merge(ws, "merge me")
    assert result.get("ok") is False
    assert result.get("needs_approval") is True


# ── DoD 6: Policy off → no ledger I/O ───────────────────────────────────────


def test_dod6_policy_off_no_ledger_io(tmp_path: Path) -> None:
    project, ws, _ = _make_workspace(tmp_path, "run-6")
    result = ws_mod.merge(ws, "merge me")
    assert result.get("ok") is True
    assert result.get("merged")
    # No inbox row was written by the policy gate.
    assert _row(str(tmp_path / "state.db"), ws.run_id) is None


# ── DoD 7: Discard records a rejection ───────────────────────────────────────


def test_dod7_discard_marks_pending_as_rejected(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("MO_REVIEW_BEFORE_MERGE", "required")
    project, ws, _ = _make_workspace(tmp_path, "run-7")

    # A pending ``merge:<run>`` row exists (the refuse path from DoD 2).
    pending = ws_mod.merge(ws, "merge me")
    assert pending.get("needs_approval") is True
    pre = _row(str(tmp_path / "state.db"), ws.run_id)
    assert pre is not None and pre["status"] == "pending"

    # Discard with an approver rejects the pending row.
    result = ws_mod.discard(ws, approver="ide:test")
    assert result.get("ok") is True
    post = _row(str(tmp_path / "state.db"), ws.run_id)
    assert post is not None and post["status"] == "rejected"


# ── DoD 7b: Awaiting list shows published workspaces ────────────────────────


def test_dod7b_awaiting_list(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("MO_REVIEW_BEFORE_MERGE", "required")
    _task_runs_create(str(tmp_path / "state.db"))

    project = _init_repo(tmp_path)
    home = _init_home(tmp_path)
    ws = ws_mod.create(project, home, "run-await")
    (ws.path / "edit.txt").write_text("change\n", encoding="utf-8")
    assert _git(["add", "-A"], ws.path).returncode == 0
    assert _git(["commit", "-m", "edit"], ws.path).returncode == 0

    # Seed the published task row.
    con = sqlite3.connect(str(tmp_path / "state.db"))
    con.execute(
        "INSERT INTO task_runs (id, task_class, kickoff_path, status) "
        "VALUES (?, ?, ?, 'published')",
        (ws.run_id, "framework_edit", "/tmp/k.md"),
    )
    con.commit()
    con.close()

    rc = approvals_cmd.main(["list", "--json"], root=str(tmp_path))
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    awaiting = [r for r in payload if r.get("state") == "awaiting" and r.get("run") == ws.run_id]
    assert awaiting, payload
    entry = awaiting[0]
    assert entry["kind"] == "merge"
    assert entry["branch"] == ws.branch

    # Approve via CLI; the awaiting entry should disappear.
    rc_approve = approvals_cmd.main(["approve", ws.run_id], root=str(tmp_path))
    assert rc_approve == 0
    capsys.readouterr()  # clear approval output
    rc = approvals_cmd.main(["list", "--json"], root=str(tmp_path))
    assert rc == 0
    payload2 = json.loads(capsys.readouterr().out)
    awaiting2 = [r for r in payload2 if r.get("state") == "awaiting" and r.get("run") == ws.run_id]
    assert not awaiting2, payload2
