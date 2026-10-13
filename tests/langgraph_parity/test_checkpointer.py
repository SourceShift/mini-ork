"""LangGraph checkpointer parity tests (P1.2 of spec-driven-delivery).

Covers AC1 of /private/tmp/miniork-p1-specs/p1-2-checkpointer.md:

  - fresh state.db applies the 0071 migration cleanly
  - a node completing writes a completion record (F1 fields: run_id,
    step_id, node_id, status, artifact_path, artifact_sha256, rc,
    started_at, ended_at)
  - replaying a run SKIPS succeeded nodes (F2 — no second dispatch
    record, no artifact mtime change, sha256 unchanged)
  - failed (F3) and error (F4) nodes RE-ENTER the dispatch set on resume
  - corrupt / missing records on resume hard-error naming the node and
    run (Errors contract), never silently re-execute

Plus the C2 contract — opt-out recipes see the gate OFF and the record
write becomes a no-op (no behaviour change for any existing recipe).

Test isolation:
  - each test gets a fresh tmp state.db (the migrate_db fixture runs init_db
    against tmp_path, which applies every shipped migration INCLUDING
    0071_node_completion_checkpoints.sql — so the test also proves the
    migration is registered, lex-ordered, and applies cleanly end-to-end);
  - each test gets a fresh tmp run_dir (where artifact files live);
  - the module-level gate is forced ON at the start of every test (the
    module reads ``CHECKPOINTER_ENABLED`` at import time and some
    environments may flip it).
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

# Force the gate ON for these tests; ``mini_ork.checkpoints`` reads
# CHECKPOINTER_ENABLED at import time and we don't want a stray env var
# inherited from the shell to flip a whole parity suite into the no-op
# branch.
os.environ["CHECKPOINTER_ENABLED"] = "1"

from mini_ork import checkpoints as ck
from mini_ork.stores import migrate as mc_migrate


# ─── fixtures ──────────────────────────────────────────────────────────────

@pytest.fixture
def migrate_db(tmp_path: Path) -> str:
    """Fresh tmp state.db with every shipped migration applied, including
    0071_node_completion_checkpoints.sql.

    ``init_db`` mirrors ``db/init.sh``: applies migrations + views, sets
    PRAGMAs, validates >= 20 tables, returns 0 on success.
    """
    db = str(tmp_path / "state.db")
    rc, _out, _err = mc_migrate.init_db(db=db, root=str(REPO))
    assert rc == 0, f"init_db failed; rc={rc}"
    return db


@pytest.fixture
def run_dir(tmp_path: Path) -> str:
    """Fresh tmp run_dir (artifacts live here)."""
    d = tmp_path / "run"
    d.mkdir()
    return str(d)


@pytest.fixture(autouse=True)
def _gate_on():
    """Force the per-completion gate ON for every case here, regardless of
    the ambient env var. Mirrors the import-time reset above so a test
    that flips the gate OFF (the opt-out cases) cannot leak into siblings.
    """
    ck.enable(True)
    yield
    ck.enable(True)


# ─── migration ─────────────────────────────────────────────────────────────

def test_migration_0071_applies_cleanly_and_table_exists(migrate_db):
    """AC1 (a): a fresh state.db applies the new migration cleanly and the
    table is queryable by run_id (F6)."""
    con = sqlite3.connect(migrate_db)
    try:
        # The table itself must exist with the F5 column set.
        cols = {r[1] for r in con.execute(
            "SELECT * FROM pragma_table_info('node_completion_checkpoints')"
        ).fetchall()}
        assert {"run_id", "step_id", "node_id", "status",
                "artifact_path", "artifact_sha256", "rc",
                "started_at", "ended_at", "recorded_at"} <= cols, \
            f"missing F1 columns; got {sorted(cols)}"

        # And the migration is recorded in schema_migrations so future
        # ``init_db`` calls don't try to re-apply it.
        row = con.execute(
            "SELECT filename FROM schema_migrations "
            "WHERE filename = '0071_node_completion_checkpoints.sql'"
        ).fetchone()
        assert row is not None, "0071 migration not registered"
    finally:
        con.close()


def test_migration_uniqueness_constraint(migrate_db):
    """AC1 (a, F5): at most one row per (run_id, step_id) — enforced by the
    UNIQUE constraint that backs the resume lookup."""
    ck.record_completion(
        migrate_db,
        run_id="r1", step_id="n1", node_id="n1",
        status="succeeded", started_at=0, ended_at=1,
    )
    # A second write for the same pair must NOT crash and must NOT add a row
    # (INSERT OR REPLACE). INSERT OR REPLACE collapses to one; we just
    # assert the row count is 1 afterwards.
    ck.record_completion(
        migrate_db,
        run_id="r1", step_id="n1", node_id="n1",
        status="succeeded", started_at=2, ended_at=3,
    )
    con = sqlite3.connect(migrate_db)
    try:
        n_rows = con.execute(
            "SELECT COUNT(*) FROM node_completion_checkpoints"
        ).fetchone()[0]
        assert n_rows == 1
        # The latest write wins (REPLACE on UNIQUE).
        ended_at = con.execute(
            "SELECT ended_at FROM node_completion_checkpoints"
            " WHERE run_id='r1' AND step_id='n1'"
        ).fetchone()[0]
        assert ended_at == 3
    finally:
        con.close()


# ─── record shape (F1) ─────────────────────────────────────────────────────

def test_completion_record_writes_all_f1_fields(migrate_db, tmp_path):
    """AC1 (b): a node completing writes a completion record carrying every
    F1 field (node_id, status, artifact_path, artifact_sha256, rc,
    timestamps) keyed by run_id + step_id (F5)."""
    art = tmp_path / "out.md"
    art.write_bytes(b"hello world")
    sha = hashlib.sha256(art.read_bytes()).hexdigest()

    rc = ck.record_completion(
        migrate_db,
        run_id="r1", step_id="impl", node_id="impl",
        status="succeeded",
        artifact_path="out.md",
        artifact_sha256=sha,
        rc=0,
        started_at=1000, ended_at=2000,
    )
    assert rc == 0

    # F5: keyed by (run_id, step_id) — the only retrieval key the runtime
    # needs.
    rec = ck.get_completion(migrate_db, run_id="r1", step_id="impl")
    assert rec is not None
    assert rec.run_id == "r1"
    assert rec.step_id == "impl"
    assert rec.node_id == "impl"
    assert rec.status == "succeeded"
    assert rec.artifact_path == "out.md"
    assert rec.artifact_sha256 == sha
    assert rec.rc == 0
    assert rec.started_at == 1000
    assert rec.ended_at == 2000
    assert rec.recorded_at > 0


def test_completion_record_optional_artifact_when_no_output(migrate_db):
    """AC1 (b): a node that produced no on-disk output writes a row with
    artifact_path / artifact_sha256 both NULL and rc NULL — the SQL CHECK
    accepts that as long as both halves of the pair are absent (a half-
    populated pair is rejected — see store.py)."""
    rc = ck.record_completion(
        migrate_db,
        run_id="r2", step_id="noop", node_id="noop",
        status="succeeded", started_at=0, ended_at=1,
    )
    assert rc == 0
    rec = ck.get_completion(migrate_db, run_id="r2", step_id="noop")
    assert rec is not None
    assert rec.artifact_path is None
    assert rec.artifact_sha256 is None
    assert rec.rc is None


# ─── replay skip / re-dispatch (F2 / F3 / F4) ─────────────────────────────

class _DispatchTracker:
    """Tiny in-process dispatch-loop stub. The real one lives in
    ``mini_ork.dispatch``; this stub captures exactly what the AC1
    contract needs (no second dispatch record, no mtime change) without
    dragging the rest of the runtime in. Same idea as the
    ``record_completion`` write on the boundary."""

    def __init__(self, db: str, run_dir: str, run_id: str) -> None:
        self.db = db
        self.run_dir = run_dir
        self.run_id = run_id
        # dispatch_count[step_id] = how many times this step was dispatched
        # across this run's history. The AC1 replay assertion is on the
        # COUNTER, not on the list, so we just count.
        self.dispatch_count: dict[str, int] = {}
        self.artifact_mtime: dict[str, float] = {}
        self.artifact_sha: dict[str, str] = {}

    def dispatch(self, candidates: list[str], results: dict[str, str]
                 ) -> list[str]:
        """Run one pass: filter by completion, then "execute" each survivor.

        ``results`` maps step_id -> one of 'succeeded' / 'failed' / 'error'.
        A succeeded node writes an artifact file and a completion row.
        A failed node writes NO artifact (partial side effect) and a
        completion row with status='failed'. An error node behaves the
        same as 'failed' but with status='error' — the F4 control signal.
        """
        # F2 / F3 / F4 — this is the line the runtime needs.
        survivors = ck.filter_resume_set(
            self.db, run_id=self.run_id, candidates=candidates,
        )
        for step_id in survivors:
            self.dispatch_count[step_id] = self.dispatch_count.get(step_id, 0) + 1
            status = results[step_id]
            now = int(time.time())
            # The artifact file is the resolution on the cancel side-effect
            # check: succeeded nodes must not rewrite it on replay.
            art = Path(self.run_dir) / f"{step_id}.out"
            if status == "succeeded":
                art.write_text(f"output of {step_id} pass {self.dispatch_count[step_id]}")
                sha = hashlib.sha256(art.read_bytes()).hexdigest()
                ck.record_completion(
                    self.db,
                    run_id=self.run_id, step_id=step_id, node_id=step_id,
                    status="succeeded",
                    artifact_path=str(art.relative_to(self.run_dir)),
                    artifact_sha256=sha,
                    rc=0,
                    started_at=now, ended_at=now,
                )
                self.artifact_mtime[step_id] = art.stat().st_mtime
                self.artifact_sha[step_id] = sha
            else:
                # Failed / error nodes write no artifact (F3 / F4).
                ck.record_completion(
                    self.db,
                    run_id=self.run_id, step_id=step_id, node_id=step_id,
                    status=status,
                    started_at=now, ended_at=now,
                )
        return survivors


def test_replay_skips_succeeded_nodes(migrate_db, run_dir):
    """AC1 (c): replaying a run SKIPS succeeded nodes — no second dispatch
    record, no mtime change on the artifact, sha256 unchanged. F2."""
    run_id = "run-A"
    tr = _DispatchTracker(migrate_db, run_dir, run_id)

    # First pass: plan -> implement -> verify, all succeed.
    first = tr.dispatch(
        ["plan", "implement", "verify"],
        results={"plan": "succeeded", "implement": "succeeded", "verify": "succeeded"},
    )
    assert sorted(first) == ["implement", "plan", "verify"]
    # Exactly one dispatch per node.
    assert tr.dispatch_count == {"plan": 1, "implement": 1, "verify": 1}

    # Snapshot artifact state.
    mtimes_before = {k: v for k, v in tr.artifact_mtime.items()}
    shas_before = {k: v for k, v in tr.artifact_sha.items()}

    # Second pass (REPLAY): same candidates. Succeeded nodes MUST be
    # dropped before re-execution (F2).
    second = tr.dispatch(
        ["plan", "implement", "verify"],
        results={"plan": "succeeded", "implement": "succeeded", "verify": "succeeded"},
    )
    # No survivors: every candidate is already succeeded.
    assert second == []
    # Dispatch counter unchanged for succeeded nodes.
    assert tr.dispatch_count == {"plan": 1, "implement": 1, "verify": 1}
    # Artifact mtime unchanged (no write on replay).
    assert tr.artifact_mtime == mtimes_before
    # sha256 unchanged (no rewrite on replay).
    assert tr.artifact_sha == shas_before


def test_replay_redispatches_failed_nodes(migrate_db, run_dir):
    """AC1 (d, F3): a node that completed with status='failed' MUST re-enter
    the dispatch set on the next resume — never silently skipped."""
    run_id = "run-B"
    tr = _DispatchTracker(migrate_db, run_dir, run_id)

    # First pass: implement fails; plan succeeds.
    first = tr.dispatch(
        ["plan", "implement"],
        results={"plan": "succeeded", "implement": "failed"},
    )
    assert sorted(first) == ["implement", "plan"]
    assert tr.dispatch_count == {"plan": 1, "implement": 1}

    # Second pass: replay the full candidate list. plan stays skipped
    # (succeeded); implement MUST re-enter (failed → re-dispatch).
    second = tr.dispatch(
        ["plan", "implement"],
        results={"plan": "succeeded", "implement": "succeeded"},
    )
    # Only implement should have re-entered.
    assert second == ["implement"]
    assert tr.dispatch_count == {"plan": 1, "implement": 2}


def test_replay_redispatches_error_nodes(migrate_db, run_dir):
    """AC1 (d, F4): a node that completed with status='error' (LangGraph's
    ERROR control signal) MUST re-enter the dispatch set on the next resume
    — never silently skipped, mirroring LangGraph's rule that errors stay
    eligible for re-execution."""
    run_id = "run-C"
    tr = _DispatchTracker(migrate_db, run_dir, run_id)

    # First pass: implement throws ERROR; plan succeeds.
    tr.dispatch(
        ["plan", "implement"],
        results={"plan": "succeeded", "implement": "error"},
    )
    assert tr.dispatch_count == {"plan": 1, "implement": 1}

    # Second pass: ERROR node MUST re-enter the dispatch set.
    second = tr.dispatch(
        ["plan", "implement"],
        results={"plan": "succeeded", "implement": "succeeded"},
    )
    assert second == ["implement"]
    assert tr.dispatch_count == {"plan": 1, "implement": 2}


def test_replay_mixed_statuses_run(migrate_db, run_dir):
    """AC1 (c + d) integrated: a replay with mixed terminal statuses only
    re-dispatches the failed / ERROR / never-ran nodes. Succeeded stays
    skipped, failed / ERROR re-enter."""
    run_id = "run-D"
    tr = _DispatchTracker(migrate_db, run_dir, run_id)

    # First pass.
    tr.dispatch(
        ["ok1", "fail1", "err1", "ok2"],
        results={"ok1": "succeeded", "fail1": "failed",
                 "err1": "error", "ok2": "succeeded"},
    )
    assert tr.dispatch_count == {"ok1": 1, "fail1": 1, "err1": 1, "ok2": 1}

    # Second pass — same candidates, all succeed this pass. fail1 + err1
    # must re-enter; ok1 + ok2 must stay skipped.
    second = tr.dispatch(
        ["ok1", "fail1", "err1", "ok2", "new1"],
        results={"ok1": "succeeded", "fail1": "succeeded",
                 "err1": "succeeded", "ok2": "succeeded",
                 "new1": "succeeded"},
    )
    assert sorted(second) == ["err1", "fail1", "new1"]
    assert tr.dispatch_count == {"ok1": 1, "fail1": 2, "err1": 2,
                                 "ok2": 1, "new1": 1}


# ─── filter_resume_set ordering ─────────────────────────────────────────────

def test_filter_resume_set_preserves_input_order(migrate_db):
    """The filter preserves the candidate order (so the runtime sees the
    same topological intent it built for the original pass)."""
    run_id = "run-E"
    # Pre-seed: middle is done, edges are not.
    ck.record_completion(
        migrate_db, run_id=run_id, step_id="middle", node_id="middle",
        status="succeeded", started_at=0, ended_at=1,
    )
    survivors = ck.filter_resume_set(
        migrate_db, run_id=run_id,
        candidates=["first", "middle", "last", "first"],  # dup of first
    )
    # middle is dropped; "first" deduplicated; order preserved.
    assert survivors == ["first", "last"]


# ─── Errors contract ──────────────────────────────────────────────────────

def test_write_side_shape_violation_returns_rc3(migrate_db):
    """Layer 1 of the F1 shape defence: ``record_completion_db`` refuses a
    malformed record BEFORE the SQL round-trip and returns a typed rc=3
    rather than raising a generic ``sqlite3.Error``. A status outside the
    terminal vocabulary is the canonical case (a future migration added a
    name this code path does not know)."""
    from mini_ork.checkpoints.store import record_completion_db
    rc = record_completion_db(
        migrate_db,
        run_id="r-x", step_id="n-x", node_id="n-x",
        status="mystery", started_at=0, ended_at=1,
    )
    assert rc == 3, f"unknown status should fail F1 shape; got rc={rc}"
    # Nothing got written.
    assert ck.get_completion(migrate_db, run_id="r-x", step_id="n-x") is None


def test_missing_record_is_not_corrupt_and_redispatches(migrate_db):
    """A MISSING row is the normal "never ran" case, NOT corruption:
    ``filter_resume_set`` keeps the candidate so the node re-dispatches.
    Only a row that EXISTS and cannot be parsed is corruption (next test)."""
    assert ck.filter_resume_set(
        migrate_db, run_id="run-missing", candidates=["never-ran"],
    ) == ["never-ran"]


def test_filter_resume_set_hard_errors_on_corrupt_row_naming_node_and_run(
    migrate_db, monkeypatch,
):
    """The Errors contract in the SpecCard, exercised end to end on the
    resume path: a checkpoint row that cannot be parsed MUST hard-error
    naming the node and the run, never silently re-execute.

    The table's SQL CHECK makes it impossible to *write* a bad status
    through the sanctioned path, so we corrupt the read seam the way a real
    one occurs — a row the current schema no longer matches (an older
    writer, a hand-edited DB, a schema downgrade). Monkeypatching the store
    reader that ``filter_resume_set`` consumes drives the failure through
    the exact production call path:
    ``filter_resume_set`` -> ``list_completions`` -> ``from_row``.
    """
    from mini_ork.checkpoints.store import CompletionRow
    bad = CompletionRow(
        run_id="run-corrupt", step_id="step-7", node_id="node-7",
        status="mystery", artifact_path=None, artifact_sha256=None,
        rc=1, started_at=0, ended_at=1, recorded_at=2,
    )
    monkeypatch.setattr(ck, "list_completions_db", lambda db, *, run_id: [bad])
    with pytest.raises(ck.CheckpointCorrupt) as excinfo:
        ck.filter_resume_set(
            migrate_db, run_id="run-corrupt", candidates=["step-6", "step-7"],
        )
    err = excinfo.value
    assert err.run_id == "run-corrupt"
    assert err.step_id == "step-7"
    assert err.node_id == "node-7"
    # The message must NAME both the run and the node so the operator can
    # locate the bad row.
    assert "run-corrupt" in str(err)
    assert "step-7" in str(err)
    assert "node-7" in str(err)


def test_from_row_raises_on_malformed_artifact_pairing():
    """``from_row`` is the one funnel every reader shares, so its shape gate
    must catch a violation the SQL CHECK does not cover: an ``artifact_path``
    with no ``artifact_sha256`` — a row the writer's F1 contract would have
    refused — reads back as corrupt, not as a valid completion."""
    from mini_ork.checkpoints.store import CompletionRow
    bad = CompletionRow(
        run_id="r", step_id="s", node_id="n", status="succeeded",
        artifact_path="out/thing.json", artifact_sha256=None,
        rc=0, started_at=0, ended_at=1, recorded_at=2,
    )
    with pytest.raises(ck.CheckpointCorrupt) as excinfo:
        ck.CompletionRecord.from_row(bad)
    assert excinfo.value.run_id == "r"
    assert "artifact_path" in str(excinfo.value)


# ─── C2: opt-out gate ─────────────────────────────────────────────────────

def test_opt_out_gate_makes_record_a_noop(migrate_db):
    """C2 — no behavior change for any existing recipe: a recipe that opts
    out (sets CHECKPOINTER_ENABLED=0 or calls ``enable(False)``) sees the
    record write become a no-op, so the dispatch loop's surface area stays
    identical to the pre-checkpointer behaviour."""
    ck.enable(False)
    try:
        rc = ck.record_completion(
            migrate_db,
            run_id="opt-1", step_id="n", node_id="n",
            status="succeeded", started_at=0, ended_at=1,
        )
        # No-op rc — the gate is OFF, so the caller knows no row was
        # written and the rest of the dispatch path continues unchanged.
        assert rc == 1
        # And the DB has no row (the durable-DAG ledger is the source of
        # truth, exactly as before this module).
        assert ck.get_completion(migrate_db, run_id="opt-1", step_id="n") is None
    finally:
        ck.enable(True)


def test_opt_out_does_not_change_read_paths(migrate_db):
    """C2 — the read paths (list / filter_resume_set) keep working when the
gate is OFF. Only the WRITE paths no-op. This is the property that lets an
opt-out recipe resume a run the older way (read the durable-DAG ledger,
not this one)."""
    # Seed a row WHILE the gate is on, then flip the gate OFF and read.
    ck.record_completion(
        migrate_db,
        run_id="r", step_id="n", node_id="n",
        status="succeeded", started_at=0, ended_at=1,
    )
    ck.enable(False)
    try:
        rec = ck.get_completion(migrate_db, run_id="r", step_id="n")
        assert rec is not None
        assert rec.status == "succeeded"
    finally:
        ck.enable(True)


# ─── legacy / additive ────────────────────────────────────────────────────

def test_legacy_db_without_node_completion_checkpoints_handles_migration(tmp_path):
    """A DB that predates 0071 (e.g., a state.db left over from a
    pre-migration run) accepts the new migration on next ``init_db``
    without breaking the existing schema. This is the additive property
    the migration's preamble relies on."""
    db = str(tmp_path / "legacy.db")
    con = sqlite3.connect(db)
    # Pretend a pre-existing run wrote its durable-DAG row under the E1
    # contract. The new migration must NOT touch this table.
    con.executescript(
        """
        CREATE TABLE node_checkpoints (
            run_id TEXT NOT NULL,
            node_id TEXT NOT NULL,
            attempt INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL,
            artifact_manifest_json TEXT NOT NULL DEFAULT '[]',
            created_at INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (run_id, node_id)
        );
        INSERT INTO node_checkpoints
            VALUES ('r1', 'preexisting', 1, 'success', '[]', 0);
        """
    )
    con.commit()
    con.close()

    rc, _out, _err = mc_migrate.init_db(db=db, root=str(REPO))
    assert rc == 0

    con = sqlite3.connect(db)
    try:
        # E1 row survives.
        e1 = con.execute(
            "SELECT status FROM node_checkpoints"
            " WHERE run_id='r1' AND node_id='preexisting'"
        ).fetchone()
        assert e1 == ("success",)
        # New table exists.
        new = con.execute(
            "SELECT name FROM sqlite_master"
            " WHERE type='table' AND name='node_completion_checkpoints'"
        ).fetchone()
        assert new is not None
    finally:
        con.close()