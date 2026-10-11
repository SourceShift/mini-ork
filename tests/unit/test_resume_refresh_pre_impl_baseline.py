"""On a resume that re-runs the implementer, refresh a stale pre-implementer-ref.

A framework-edit run records ``pre-implementer-ref`` ONCE (the tree state before
the implementer edits, via non-destructive ``git stash create``). If an external
actor advances the target HEAD after that snapshot — a merge onto the shared
tree, a sibling run's base — the baseline becomes a stale proper ancestor of HEAD
and ``_implementer_moved_base`` false-fails the resumed implementer with
``impl_moved_base`` even though it never moved anything. The recover dispatch
path now drops the stale snapshot (``_refresh_stale_pre_impl_baseline``) so the
pre-dispatch capture re-snapshots CURRENT HEAD.

Invariants pinned here:

  1. Within-attempt idempotence — a plain retry (HEAD == the recorded baseline,
     or the stash-WIP first-parent shape) is left byte-identical. No
     unconditional refresh; the run never absorbs its own uncommitted delta.
  2. No laundering — when the run's own ledger records that the prior implementer
     failed ``impl_moved_base``, the HEAD movement is attributable to the run's
     own commit, so the refresh is refused and the guard keeps failing loudly.
  3. Orphaned baselines (rewritten main) refresh like any other stale baseline.
  4. Unreadable ledger → fail closed (never refresh blindly).
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.cli import execute as ex  # noqa: E402
from mini_ork.cli import execute_handlers as exh  # noqa: E402
from mini_ork.recovery import planner as rp  # noqa: E402


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                          text=True, check=True).stdout.strip()


def _init_repo(repo: Path) -> str:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "A.txt").write_text("A original\n")
    (repo / "B.txt").write_text("B original\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    return _git(repo, "rev-parse", "HEAD")


def _external_advance(repo: Path) -> str:
    """A commit the run did not make (an external actor merging onto the tree)."""
    (repo / "C.txt").write_text("EXTERNAL ADVANCE\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "external advance")
    return _git(repo, "rev-parse", "HEAD")


def _divergent_baseline(repo: Path, base: str) -> str:
    """A commit that still exists but is NOT an ancestor of HEAD (a baseline on
    a divergent line, e.g. a rewritten branch the run no longer descends from)."""
    (repo / "SIDE.txt").write_text("side line\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "side line")
    side = _git(repo, "rev-parse", "HEAD")
    _git(repo, "reset", "-q", "--hard", base)
    (repo / "MAIN.txt").write_text("main line\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "main line")
    return side


def _ledger(tmp_path: Path, notes: str, run_id: str = "run-r1") -> Path:
    db = tmp_path / "state.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE task_runs (id TEXT PRIMARY KEY, notes TEXT)")
    con.execute("INSERT INTO task_runs VALUES (?, ?)", (run_id, notes))
    con.commit()
    con.close()
    return db


def _run_dir(tmp_path: Path, baseline: str) -> Path:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "pre-implementer-ref").write_text(baseline + "\n")
    return run_dir


def _plan(closure: set, node_types: dict, run_id: str = "run-r1") -> SimpleNamespace:
    return SimpleNamespace(closure=closure, node_types=node_types, run_id=run_id)


# ── (a) the false-fail the refresh exists to fix ────────────────────────────

def test_resume_refreshes_a_stale_baseline_to_current_head(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    base = _init_repo(repo)
    head = _external_advance(repo)          # HEAD moved past the recorded base
    run_dir = _run_dir(tmp_path, base)
    db = _ledger(tmp_path, "")              # no impl_moved_base recorded

    # Pre-fix shape: the stale baseline trips the guard.
    assert exh._implementer_moved_base(str(run_dir), str(repo)) != ""

    assert ex._refresh_stale_pre_impl_baseline(
        str(run_dir), str(repo), db=str(db), run_id="run-r1") is True

    # The pre-dispatch capture now re-snapshots CURRENT HEAD.
    monkeypatch.setenv("MO_TARGET_CWD", str(repo))
    ex._capture_pre_impl_baseline(str(run_dir))
    assert (run_dir / "pre-implementer-ref").read_text().strip() == head
    assert exh._implementer_moved_base(str(run_dir), str(repo)) == ""


def test_resume_harvest_diffs_from_current_head_not_the_stale_baseline(
        tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    base = _init_repo(repo)
    _external_advance(repo)                 # committed change between base and HEAD
    run_dir = _run_dir(tmp_path, base)
    db = _ledger(tmp_path, "")

    assert ex._refresh_stale_pre_impl_baseline(
        str(run_dir), str(repo), db=str(db), run_id="run-r1") is True

    monkeypatch.setenv("MO_TARGET_CWD", str(repo))
    ex._capture_pre_impl_baseline(str(run_dir))   # baseline = current HEAD (clean)
    (repo / "B.txt").write_text("B IMPLEMENTER EDIT\n")

    ok, fr = ex._harvest_framework_edit_ground_truth(str(run_dir), str(repo))
    assert (ok, fr) == (True, "")
    diff = (run_dir / "framework-edit.diff").read_text()
    assert "B IMPLEMENTER EDIT" in diff, "implementer's own change must be harvested"
    # Diffing against the stale base would sweep the external commit in.
    assert "EXTERNAL ADVANCE" not in diff
    assert "C.txt" not in diff


# ── (b) orphaned baseline (rewritten main) ──────────────────────────────────

def test_resume_refreshes_an_orphaned_baseline(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_repo(repo)
    head = _git(repo, "rev-parse", "HEAD")
    run_dir = _run_dir(tmp_path, "f" * 40)   # object no longer exists
    db = _ledger(tmp_path, "")

    assert exh._implementer_moved_base(str(run_dir), str(repo)) != ""
    assert ex._refresh_stale_pre_impl_baseline(
        str(run_dir), str(repo), db=str(db), run_id="run-r1") is True

    monkeypatch.setenv("MO_TARGET_CWD", str(repo))
    ex._capture_pre_impl_baseline(str(run_dir))
    assert (run_dir / "pre-implementer-ref").read_text().strip() == head
    assert exh._implementer_moved_base(str(run_dir), str(repo)) == ""


# ── (c) laundering refusal (invariant 2) ────────────────────────────────────

def test_laundering_refused_when_the_prior_implementer_moved_the_base(
        tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    base = _init_repo(repo)
    _external_advance(repo)
    run_dir = _run_dir(tmp_path, base)
    # The run's own ledger pins the movement on the prior implementer attempt.
    db = _ledger(tmp_path, f"impl_moved_base: HEAD {base[:12]} is not the run's "
                           "starting commit; the implementer committed.")

    assert ex._refresh_stale_pre_impl_baseline(
        str(run_dir), str(repo), db=str(db), run_id="run-r1") is False
    # Refused: the stale baseline is left in place, so the guard still fires.
    assert (run_dir / "pre-implementer-ref").read_text().strip() == base
    assert exh._implementer_moved_base(str(run_dir), str(repo)) != ""
    # And the pre-dispatch capture does NOT re-snapshot over it.
    monkeypatch.setenv("MO_TARGET_CWD", str(repo))
    ex._capture_pre_impl_baseline(str(run_dir))
    assert (run_dir / "pre-implementer-ref").read_text().strip() == base


# ── (d) within-attempt idempotence (invariant 1) ────────────────────────────

def test_within_attempt_retry_does_not_resnapshot(tmp_path):
    repo = tmp_path / "repo"
    base = _init_repo(repo)
    run_dir = _run_dir(tmp_path, base)
    db = _ledger(tmp_path, "")
    (repo / "B.txt").write_text("uncommitted implementer edit\n")  # HEAD == base

    assert ex._refresh_stale_pre_impl_baseline(
        str(run_dir), str(repo), db=str(db), run_id="run-r1") is False
    assert (run_dir / "pre-implementer-ref").read_text().strip() == base


def test_stash_shape_baseline_is_not_refreshed(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "A.txt").write_text("pre-existing dirt\n")
    stash = _git(repo, "stash", "create")
    assert stash                                    # 2-parent WIP commit
    run_dir = _run_dir(tmp_path, stash)
    db = _ledger(tmp_path, "")

    # HEAD is the stash's first parent → the base never moved (run-1791543223).
    assert exh._implementer_moved_base(str(run_dir), str(repo)) == ""
    assert ex._refresh_stale_pre_impl_baseline(
        str(run_dir), str(repo), db=str(db), run_id="run-r1") is False
    assert (run_dir / "pre-implementer-ref").read_text().strip() == stash


# ── (e) fail-closed on an unreadable ledger (invariant 4) ───────────────────

def test_unreadable_ledger_fails_closed(tmp_path):
    repo = tmp_path / "repo"
    base = _init_repo(repo)
    _external_advance(repo)
    run_dir = _run_dir(tmp_path, base)

    # No db file at all → the signal is unreadable → refuse to refresh.
    assert ex._refresh_stale_pre_impl_baseline(
        str(run_dir), str(repo), db=str(tmp_path / "missing.db"),
        run_id="run-r1") is False
    assert (run_dir / "pre-implementer-ref").read_text().strip() == base

    # A db without a task_runs row for this run is equally unreadable.
    empty = tmp_path / "empty.db"
    con = sqlite3.connect(empty)
    con.execute("CREATE TABLE task_runs (id TEXT PRIMARY KEY, notes TEXT)")
    con.commit()
    con.close()
    assert ex._refresh_stale_pre_impl_baseline(
        str(run_dir), str(repo), db=str(empty), run_id="run-r1") is False
    assert (run_dir / "pre-implementer-ref").read_text().strip() == base


def test_orphaned_baseline_ignores_a_recorded_impl_moved_base(tmp_path, monkeypatch):
    """A baseline that is NOT an ancestor of HEAD cannot contribute a commit to
    HEAD, so the run's own impl_moved_base ledger note must not block the
    refresh: only a forward-ancestor baseline can launder a committed change."""
    repo = tmp_path / "repo"
    head = _init_repo(repo)
    recorded = (f"impl_moved_base: HEAD {head[:12]} is not the run's starting "
                "commit; the implementer committed.")

    # (a) the baseline object no longer exists (rewritten/gc'd main) → exit 128.
    d_gone = tmp_path / "gone"
    d_gone.mkdir()
    run_dir = _run_dir(d_gone, "f" * 40)
    db = _ledger(d_gone, recorded)
    assert exh._implementer_moved_base(str(run_dir), str(repo)) != ""
    assert ex._refresh_stale_pre_impl_baseline(
        str(run_dir), str(repo), db=str(db), run_id="run-r1") is True
    assert not (run_dir / "pre-implementer-ref").exists()

    # (b) the baseline object still exists but diverged from HEAD → exit 1.
    side = _divergent_baseline(repo, head)
    d_side = tmp_path / "side"
    d_side.mkdir()
    run_dir = _run_dir(d_side, side)
    db = _ledger(d_side, recorded)
    assert exh._implementer_moved_base(str(run_dir), str(repo)) != ""
    assert ex._refresh_stale_pre_impl_baseline(
        str(run_dir), str(repo), db=str(db), run_id="run-r1") is True
    assert not (run_dir / "pre-implementer-ref").exists()

    # The pre-dispatch capture re-snapshots the CURRENT HEAD, not the orphan.
    monkeypatch.setenv("MO_TARGET_CWD", str(repo))
    ex._capture_pre_impl_baseline(str(run_dir))
    assert (run_dir / "pre-implementer-ref").read_text().strip() == \
        _git(repo, "rev-parse", "HEAD")


def test_forward_ancestor_with_recorded_failure_still_refused(tmp_path):
    """The complement: a proper-ancestor baseline IS the laundering shape, so the
    ancestry probe classifies it as an ancestor and the ledger refusal decides."""
    repo = tmp_path / "repo"
    base = _init_repo(repo)
    _external_advance(repo)
    (repo / "D.txt").write_text("SECOND EXTERNAL ADVANCE\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "second external advance")
    run_dir = _run_dir(tmp_path, base)
    db = _ledger(tmp_path, f"impl_moved_base: HEAD {base[:12]} is not the run's "
                           "starting commit; the implementer committed.")

    assert ex._baseline_is_ancestor_of_head(str(run_dir), str(repo)) is True
    assert ex._refresh_stale_pre_impl_baseline(
        str(run_dir), str(repo), db=str(db), run_id="run-r1") is False
    # Refused: the stale baseline is left in place, so the guard still fires.
    assert (run_dir / "pre-implementer-ref").read_text().strip() == base
    assert exh._implementer_moved_base(str(run_dir), str(repo)) != ""


# ── (f) the planner gate: only an implementer rerun triggers a refresh ───────

def test_planner_gate_only_refreshes_when_an_implementer_reruns(tmp_path):
    repo = tmp_path / "repo"
    base = _init_repo(repo)
    head = _external_advance(repo)
    run_dir = _run_dir(tmp_path, base)
    db = _ledger(tmp_path, "")
    # Persisted roots are what the original capture resolved the target from.
    (run_dir / "run_profile.json").write_text(json.dumps(
        {"roots": {"target": str(repo), "run_dir": str(run_dir),
                   "home": "", "engine": ""}}))

    types = {"researcher": "researcher", "implementer": "implementer",
             "verifier": "verifier"}
    # A verifier-only closure has no in-place editor → no refresh.
    assert rp._refresh_stale_baseline_for_resume(
        _plan({"verifier"}, types), str(run_dir), str(db)) is False
    assert (run_dir / "pre-implementer-ref").read_text().strip() == base

    # An implementer in the closure refreshes to the current HEAD.
    assert rp._refresh_stale_baseline_for_resume(
        _plan({"implementer", "verifier"}, types), str(run_dir), str(db)) is True
    assert not (run_dir / "pre-implementer-ref").exists()

    import os
    os.environ["MO_TARGET_CWD"] = str(repo)
    try:
        ex._capture_pre_impl_baseline(str(run_dir))
    finally:
        os.environ.pop("MO_TARGET_CWD", None)
    assert (run_dir / "pre-implementer-ref").read_text().strip() == head
