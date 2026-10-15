"""remote-early-death-hardening AC3 — no stale node-provider lock outlives a run.

Kickoff: *"A run that reached a terminal state must not leave
`<run_dir>/.node-provider.lock` behind. Do this where it cannot race a live
provisioner (a terminal-state cleanup, not inside `_RunLock.__exit__` while
another process may hold the flock); `_RunLock`'s exclusive `flock` +
`MO_NODE_PROVISION_WAIT_S` path must keep working unchanged."*

48h triage §8.3 found 0-byte locks (``remote/providers/base.py:73``) released but
never unlinked, sitting in the run dirs of dead runs.
"""
from __future__ import annotations

import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from mini_ork.orchestration import run_reaper
from mini_ork.remote.providers import base
from mini_ork.remote.providers.base import (
    _LOCK_NAME,
    NodeEndpoint,
    ProviderUnavailableError,
    _RunLock,
    acquire_run_node,
    register_node_provider,
    release_run_node,
)
from mini_ork.stores import migrate as mig

REPO = Path(__file__).resolve().parents[2]
RUN_ID = "run-1791911811-5826"
PROVIDER_NAME = "unit_stub_lock_provider"


@pytest.fixture
def home(tmp_path: Path) -> Path:
    h = tmp_path / "proj" / ".mini-ork"
    h.mkdir(parents=True)
    rc, _out, err = mig.init_db(db=str(h / "state.db"), root=str(REPO))
    assert rc == 0, err
    return h


def _seed(home: Path, status: str = "executing") -> Path:
    now = int(time.time())
    con = sqlite3.connect(home / "state.db")
    con.execute(
        "INSERT INTO task_runs (id, task_class, kickoff_path, recipe, status, workflow_version, "
        "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
        (RUN_ID, "framework_edit", "/kickoff.md", "code-fix", status, "latest", now - 60, now))
    con.commit()
    con.close()
    run_dir = home / "runs" / RUN_ID
    run_dir.mkdir(parents=True)
    return run_dir


def _status(home: Path) -> str:
    con = sqlite3.connect(home / "state.db")
    try:
        return con.execute("SELECT status FROM task_runs WHERE id = ?", (RUN_ID,)).fetchone()[0]
    finally:
        con.close()


def _stale_lock(run_dir: Path) -> Path:
    """The 0-byte artefact §8.3 found in the dead runs' dirs."""
    lock = run_dir / _LOCK_NAME
    lock.write_bytes(b"")
    return lock


def _gone_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


class _StubProvider:
    name = PROVIDER_NAME

    def create(self, *, run_id, profile, env, engine_root):
        return NodeEndpoint(name="unit-node", url="http://127.0.0.1:1", handle="unit-handle",
                            provider=PROVIDER_NAME)

    def attach(self, handle, *, env):
        return NodeEndpoint(name="unit-node", url="http://127.0.0.1:1", handle=handle,
                            provider=PROVIDER_NAME)

    def release(self, handle, *, env):
        pass

    def reap(self, *, max_age_s, dry_run, env):
        return []


# ── the cleanup fires at terminal state ─────────────────────────────────────

def test_a_failed_run_leaves_no_provider_lock(home: Path) -> None:
    run_dir = _seed(home)
    lock = _stale_lock(run_dir)

    assert run_reaper.close_run_record(home / "state.db", RUN_ID, run_dir, crashed=True) == "executing"

    assert _status(home) == "failed"
    assert not lock.exists()


def test_a_published_run_leaves_no_provider_lock(home: Path) -> None:
    run_dir = _seed(home)
    lock = _stale_lock(run_dir)
    (run_dir / "verdict.json").write_text('{"pass": true}', encoding="utf-8")

    assert run_reaper.close_run_record(home / "state.db", RUN_ID, run_dir,
                                       crashed=False, rc=0) == "executing"

    assert _status(home) == "published"
    assert not lock.exists()


def test_an_already_published_row_still_leaves_no_provider_lock(home: Path) -> None:
    """The publisher writes ``published`` *mid-workflow*, before the executor's
    ``finally`` calls :func:`close_run_record` (``publisher.py`` → ``cli/main.py``
    teardown). That call then sees a terminal row and early-returns — and nothing
    later compensates: ``release_run_node`` removes only ``.node-provider.json``,
    and :func:`run_reaper.reap` visits non-terminal rows only. So the dominant
    remote path (a run that published normally) must drop the lock in that early
    return, or AC3's 0-byte artefact outlives every good run."""
    run_dir = _seed(home, status="published")
    lock = _stale_lock(run_dir)

    assert run_reaper.close_run_record(home / "state.db", RUN_ID, run_dir,
                                       crashed=False, rc=0) is None

    assert _status(home) == "published", "a terminal status is not ours to rewrite"
    assert not lock.exists()


def test_an_already_failed_row_still_leaves_no_provider_lock(home: Path) -> None:
    """The publisher's abstain path (``failed``) writes the same terminal status
    before teardown; the early return must treat it the same as ``published``."""
    run_dir = _seed(home, status="failed")
    lock = _stale_lock(run_dir)

    assert run_reaper.close_run_record(home / "state.db", RUN_ID, run_dir,
                                       crashed=False, rc=1) is None

    assert _status(home) == "failed"
    assert not lock.exists()


def test_an_unknown_run_row_touches_nothing(home: Path) -> None:
    """No row means no run — there is no terminal state to justify removing a
    lock, and no lock for this run id to remove anyway."""
    run_dir = _seed(home)
    lock = _stale_lock(run_dir)

    assert run_reaper.close_run_record(home / "state.db", "run-0000000000-00000",
                                       run_dir, crashed=True) is None

    assert _status(home) == "executing"
    assert lock.exists(), "a missing row must not be read as terminal"


def test_a_reaped_run_leaves_no_provider_lock(home: Path) -> None:
    """§8.3's locks sat in exactly the dirs of dispatchers that died without
    their ``finally``; the reaper is the only process that closes those."""
    run_dir = _seed(home)
    lock = _stale_lock(run_dir)
    (run_dir / ".pid").write_text(f"{_gone_pid()}\n", encoding="utf-8")

    assert [entry["run_id"] for entry in run_reaper.reap(home)] == [RUN_ID]

    assert _status(home) == "failed"
    assert not lock.exists()


def test_a_non_terminal_run_keeps_its_lock(home: Path) -> None:
    """Cost-paused waits on ``mini-ork resume`` — the run is not over, so its
    provisioning rendezvous is not ours to remove."""
    run_dir = _seed(home)
    lock = _stale_lock(run_dir)
    (run_dir / ".cost-pause").write_text("", encoding="utf-8")

    assert run_reaper.close_run_record(home / "state.db", RUN_ID, run_dir, crashed=False) is None

    assert _status(home) == "executing"
    assert lock.exists(), "a paused run is not terminal"


# ── _RunLock itself is untouched ────────────────────────────────────────────

def test_run_lock_release_does_not_unlink_the_lock_file(tmp_path: Path) -> None:
    """The lock is a rendezvous, not a lease to delete: unlinking on release
    would hand a peer already waiting a lock on a fresh inode while the exiting
    process still holds the old one."""
    run_dir = tmp_path / "runs" / RUN_ID
    run_dir.mkdir(parents=True)

    with _RunLock(run_dir, {}):
        inode = (run_dir / _LOCK_NAME).stat().st_ino
    lock = run_dir / _LOCK_NAME
    assert lock.exists()
    assert lock.stat().st_ino == inode, "the lock file must not be replaced either"


def test_run_lock_still_excludes_and_releases(tmp_path: Path) -> None:
    run_dir = tmp_path / "runs" / RUN_ID
    run_dir.mkdir(parents=True)
    no_wait = {"MO_NODE_PROVISION_WAIT_S": "0"}

    holder = _RunLock(run_dir, no_wait)
    holder.__enter__()
    try:
        with pytest.raises(ProviderUnavailableError, match="timed out"):
            _RunLock(run_dir, no_wait).__enter__()
    finally:
        holder.__exit__(None, None, None)

    again = _RunLock(run_dir, no_wait)
    again.__enter__()
    again.__exit__(None, None, None)


def test_acquire_run_node_records_the_node_and_keeps_the_lock_file(tmp_path: Path) -> None:
    """Provisioning still serializes on the lock and records the node; removing
    the file is the terminal teardown's job, not the acquire path's."""
    register_node_provider(PROVIDER_NAME, _StubProvider)
    run_dir = tmp_path / "runs" / RUN_ID
    run_dir.mkdir(parents=True)

    node = acquire_run_node(_StubProvider(), run_id=RUN_ID, run_dir=str(run_dir),
                            profile=None, env={}, engine_root="/opt/mini-ork")

    assert node.handle == "unit-handle"
    record = run_dir / ".node-provider.json"
    assert record.is_file() and f'"{PROVIDER_NAME}"' in record.read_text(encoding="utf-8")
    assert (run_dir / _LOCK_NAME).exists()
    assert release_run_node(str(run_dir), env={}) is True
    assert not record.exists()


def test_clearing_a_run_lock_is_idempotent(tmp_path: Path) -> None:
    run_dir = tmp_path / "runs" / RUN_ID
    run_dir.mkdir(parents=True)
    _stale_lock(run_dir)

    assert base.clear_stale_run_lock(run_dir) is True
    assert base.clear_stale_run_lock(run_dir) is False, "a missing lock is not an error"
    assert not (run_dir / _LOCK_NAME).exists()
    # A run dir that was never provisioned must not be an error either.
    assert base.clear_stale_run_lock(run_dir / "does-not-exist") is False
