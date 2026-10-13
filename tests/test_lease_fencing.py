"""Unit tests for the single-writer lease + fencing (E3).

Pins design §7 + the kickoff acceptance:
  * one acquirer wins; a second (different owner) is blocked while the lease is live
  * an expired lease can be stolen; the stale owner then cannot publish
  * a stale worker's checkpoint publish is REJECTED once a newer recovery holds
    the lease (end-to-end through write_checkpoint's fence guard)
  * legacy publishes (no owner_token) are unaffected

Time is injected via ``now=`` for the lease-level cases so expiry is
deterministic. The end-to-end checkpoint-fence case uses a real ``now`` for
the surviving lease (write_checkpoint's fence reads the wall clock) and a
far-past ``now`` for the stale one.
"""
from __future__ import annotations

import hashlib
import sqlite3
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from mini_ork.stores import lease
from mini_ork.stores import checkpoints as mc

# E3 schema — mirror of db/migrations/0052_run_leases_recovery_requests.sql.
LEASE_SCHEMA = """
CREATE TABLE IF NOT EXISTS run_leases (
    run_id        TEXT    PRIMARY KEY,
    owner_token   TEXT    NOT NULL,
    acquired_at   INTEGER NOT NULL,
    expires_at    INTEGER NOT NULL,
    renewed_at    INTEGER NOT NULL,
    check (expires_at >= acquired_at)
);
CREATE TABLE IF NOT EXISTS recovery_requests (
    request_id           TEXT    PRIMARY KEY,
    run_id               TEXT    NOT NULL,
    from_node            TEXT    NOT NULL,
    strategy             TEXT    NOT NULL,
    status               TEXT    NOT NULL CHECK (status IN ('pending','dispatched','completed','failed')),
    failure_class        TEXT,
    budget_usd           REAL    NOT NULL DEFAULT 5.00,
    cost_usd             REAL    NOT NULL DEFAULT 0.0,
    dispatch_count       INTEGER NOT NULL DEFAULT 0,
    owner_token          TEXT,
    created_at           INTEGER NOT NULL,
    last_dispatched_at   INTEGER,
    closed_at            INTEGER,
    payload_json         TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_recovery_requests_idem
    ON recovery_requests(run_id, from_node, strategy);
CREATE INDEX IF NOT EXISTS idx_recovery_requests_status
    ON recovery_requests(run_id, status);
"""

# E1 schema (node_checkpoints + node_attempts) — needed for the end-to-end
# checkpoint-fence case. Mirror of db/migrations/0050_node_dag_checkpoints.sql.
CHECKPOINT_SCHEMA = """
CREATE TABLE IF NOT EXISTS node_checkpoints (
    run_id                TEXT    NOT NULL,
    node_id               TEXT    NOT NULL,
    attempt               INTEGER NOT NULL DEFAULT 1,
    status                TEXT    NOT NULL CHECK (status IN ('success','failure','skipped')),
    input_hash            TEXT    NOT NULL,
    recipe_version        TEXT    NOT NULL,
    config_hash           TEXT    NOT NULL,
    artifact_manifest_json TEXT   NOT NULL,
    session_ref           TEXT,
    failure_class         TEXT,
    created_at            INTEGER NOT NULL,
    PRIMARY KEY (run_id, node_id)
);
CREATE TABLE IF NOT EXISTS node_attempts (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id                TEXT    NOT NULL,
    node_id               TEXT    NOT NULL,
    attempt_no            INTEGER NOT NULL,
    node_type             TEXT,
    started_at            INTEGER NOT NULL,
    ended_at              INTEGER NOT NULL,
    result                TEXT    NOT NULL CHECK (result IN ('success','failure','skipped','error')),
    failure_class         TEXT,
    checkpoint_used       INTEGER NOT NULL DEFAULT 0,
    checkpoint_produced   INTEGER NOT NULL DEFAULT 0,
    cost_usd              REAL,
    provider_session_id   TEXT,
    initiator             TEXT
);
"""


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    p = tmp_path / "state.db"
    con = sqlite3.connect(p)
    con.executescript(LEASE_SCHEMA)
    con.executescript(CHECKPOINT_SCHEMA)
    con.commit()
    con.close()
    return str(p)


# ── acquire / block / steal / refresh / release ────────────────────────────
def test_acquire_on_empty_run_creates_row(db_path):
    tok = lease.acquire_lease(db_path, "run1", now=1000)
    assert tok is not None
    row = lease.current_lease(db_path, "run1")
    assert row["owner_token"] == tok
    assert row["expires_at"] == 1000 + lease.DEFAULT_LEASE_TTL_S


def test_second_owner_blocked_while_live(db_path):
    a = lease.acquire_lease(db_path, "run1", owner_token="A", ttl_s=100, now=1000)
    assert a == "A"
    # different owner, lease still live → blocked
    b = lease.acquire_lease(db_path, "run1", owner_token="B", now=1050)
    assert b is None
    assert lease.current_lease(db_path, "run1")["owner_token"] == "A"


def test_reentrant_same_owner_refreshes(db_path):
    lease.acquire_lease(db_path, "run1", owner_token="A", ttl_s=100, now=1000)
    again = lease.acquire_lease(db_path, "run1", owner_token="A", ttl_s=100, now=1050)
    assert again == "A"
    assert lease.current_lease(db_path, "run1")["expires_at"] == 1150


def test_expired_lease_can_be_stolen(db_path):
    lease.acquire_lease(db_path, "run1", owner_token="A", ttl_s=10, now=1000)  # expires 1010
    stolen = lease.acquire_lease(db_path, "run1", owner_token="B", ttl_s=100, now=2000)
    assert stolen == "B"
    assert lease.current_lease(db_path, "run1")["owner_token"] == "B"


def test_refresh_live_and_lapsed_both_extend(db_path):
    lease.acquire_lease(db_path, "run1", owner_token="A", ttl_s=100, now=1000)
    assert lease.refresh_lease(db_path, "run1", "A", ttl_s=100, now=1050) is True
    # a lapsed-but-never-stolen lease is still OURS: a recovery closure can
    # outlive the TTL (2026-10-10 session-task-judge recover lost its
    # synthesizer checkpoint to exactly that lapse), so refresh is keyed on
    # token identity, not wall-clock liveness. A steal rewrites owner_token —
    # pinned separately below.
    assert lease.refresh_lease(db_path, "run1", "A", ttl_s=100, now=5000) is True
    assert lease.is_lease_holder(db_path, "run1", "A", now=5050) is True


def test_refresh_after_steal_returns_false(db_path):
    lease.acquire_lease(db_path, "run1", owner_token="A", ttl_s=10, now=1000)  # expires 1010
    assert lease.acquire_lease(db_path, "run1", owner_token="B", ttl_s=100, now=2000) == "B"
    # the stale worker cannot resurrect itself over the live recovery
    assert lease.refresh_lease(db_path, "run1", "A", ttl_s=100, now=2010) is False
    assert lease.current_lease(db_path, "run1")["owner_token"] == "B"


def test_release_only_by_holder(db_path):
    lease.acquire_lease(db_path, "run1", owner_token="A", ttl_s=100, now=1000)
    assert lease.release_lease(db_path, "run1", "B") is False   # not the holder
    assert lease.release_lease(db_path, "run1", "A") is True
    assert lease.current_lease(db_path, "run1") is None


def test_is_lease_holder(db_path):
    lease.acquire_lease(db_path, "run1", owner_token="A", ttl_s=100, now=1000)
    assert lease.is_lease_holder(db_path, "run1", "A", now=1050) is True
    assert lease.is_lease_holder(db_path, "run1", "B", now=1050) is False   # wrong token
    assert lease.is_lease_holder(db_path, "run1", "A", now=5000) is False   # expired


def test_fence_or_reject_raises_for_non_holder(db_path):
    lease.acquire_lease(db_path, "run1", owner_token="A", ttl_s=100, now=1000)
    lease.fence_or_reject(db_path, "run1", "A", now=1050)  # holder → no raise
    with pytest.raises(lease.FenceError):
        lease.fence_or_reject(db_path, "run1", "B", now=1050)


# ── scenario: a stale worker cannot publish a checkpoint after a new
#    recovery acquires the lease (end-to-end through write_checkpoint) ───────
def _hashes(run_id, node_id, recipe="test-recipe"):
    return (
        hashlib.sha256(f"{run_id}|{node_id}|{recipe}".encode()).hexdigest(),
        recipe,
        hashlib.sha256(f"tc|{recipe}|{run_id}".encode()).hexdigest(),
    )


def test_stale_worker_cannot_publish_checkpoint(db_path, tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    ih, rv, ch = _hashes("run1", "critic")
    now = int(time.time())

    # stale worker A held the lease long ago (already expired vs wall clock)
    a = lease.acquire_lease(db_path, "run1", owner_token="A", ttl_s=1, now=now - 10_000)
    assert a == "A"
    # a new recovery B acquires (steals the expired lease) with a live TTL
    b = lease.acquire_lease(db_path, "run1", owner_token="B", ttl_s=lease.DEFAULT_LEASE_TTL_S, now=now)
    assert b == "B"

    common = dict(
        db=db_path, run_id="run1", node_id="critic", status="success",
        input_hash=ih, recipe_version=rv, config_hash=ch,
        artifact_paths=[], run_dir=str(run_dir), started_at=now, ended_at=now,
    )

    # stale A tries to publish → fence rejects (rc=2), no row written
    rc_stale = mc.write_checkpoint(**common, owner_token="A")
    assert rc_stale == 2
    con = sqlite3.connect(db_path)
    assert con.execute("SELECT COUNT(*) FROM node_checkpoints WHERE run_id='run1'").fetchone()[0] == 0
    con.close()

    # live holder B publishes → success (rc=0)
    rc_live = mc.write_checkpoint(**common, owner_token="B")
    assert rc_live == 0

    # legacy path (no token) is unaffected by fencing
    ih2, rv2, ch2 = _hashes("run2", "impl")
    rc_legacy = mc.write_checkpoint(
        db=db_path, run_id="run2", node_id="impl", status="success",
        input_hash=ih2, recipe_version=rv2, config_hash=ch2,
        artifact_paths=[], run_dir=str(run_dir), started_at=now, ended_at=now,
    )
    assert rc_legacy == 0


# ── token binding: a recovered parent's token must not fence its children ──


def _checkpoint_rows(db_path: str, run_id: str) -> int:
    con = sqlite3.connect(db_path)
    try:
        return con.execute(
            "SELECT COUNT(*) FROM node_checkpoints WHERE run_id=?", (run_id,)).fetchone()[0]
    finally:
        con.close()


def test_child_run_inheriting_parent_token_still_checkpoints(db_path, tmp_path, monkeypatch):
    """2026-10-14: `recover` of an SDD parent exported its lease token, every
    child the dispatcher spawned inherited it, and each child checkpoint was
    fenced ("token … is not the live lease holder of run <child>") — so the
    child ended with no checkpoints and could only be restarted, not revived."""
    from mini_ork.cli import execute

    now = int(time.time())
    assert lease.acquire_lease(db_path, "parent", owner_token="P", now=now) == "P"
    monkeypatch.setenv("MINI_ORK_LEASE_TOKEN", "P")
    monkeypatch.setenv("MINI_ORK_LEASE_RUN_ID", "parent")

    run_dir = tmp_path / "child"
    run_dir.mkdir()
    (run_dir / "implementer.md").write_text("done")
    checkpoint = execute._make_checkpoint_fn(
        db_path, "parent--spec--D1", str(run_dir), "recursive-validate-impl", "tc")
    checkpoint("implementer", "success", "implementer", "implementer.md")

    assert _checkpoint_rows(db_path, "parent--spec--D1") == 1


def test_bound_token_is_still_fenced_for_its_own_run(db_path, tmp_path, monkeypatch):
    from mini_ork.cli import execute

    now = int(time.time())
    assert lease.acquire_lease(db_path, "parent", owner_token="P", ttl_s=1, now=now - 10_000) == "P"
    assert lease.acquire_lease(db_path, "parent", owner_token="Q", now=now) == "Q"
    monkeypatch.setenv("MINI_ORK_LEASE_TOKEN", "P")
    monkeypatch.setenv("MINI_ORK_LEASE_RUN_ID", "parent")

    run_dir = tmp_path / "parent"
    run_dir.mkdir()
    (run_dir / "planner.md").write_text("done")
    checkpoint = execute._make_checkpoint_fn(
        db_path, "parent", str(run_dir), "spec-driven-delivery", "tc")
    checkpoint("planner", "success", "planner", "planner.md")

    assert _checkpoint_rows(db_path, "parent") == 0
