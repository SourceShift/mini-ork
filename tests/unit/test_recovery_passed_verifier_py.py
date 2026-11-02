"""Tests for passed verifier recovery (option B).

When a verifier passed in a run, recovery should re-run it (cheap validation)
without making it a closure root. Verifiers that never ran or failed keep
today's behavior.
"""
from __future__ import annotations

import hashlib
import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from mini_ork.recovery import plan as plan_module
from mini_ork.recovery.plan import (
    _passed_verifiers,
    compute_recovery,
)


# ─────────────────────────────────────────────────────────────────────────────
# Database setup fixtures
# ─────────────────────────────────────────────────────────────────────────────


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS node_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    result TEXT NOT NULL,
    failure_class TEXT,
    ended_at REAL,
    checkpoint_produced INTEGER DEFAULT 0,
    UNIQUE(run_id, node_id, id)
);

CREATE TABLE IF NOT EXISTS node_checkpoints (
    run_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    attempt INTEGER,
    status TEXT,
    input_hash TEXT,
    recipe_version TEXT,
    config_hash TEXT,
    artifact_manifest_json TEXT,
    session_ref TEXT,
    failure_class TEXT,
    created_at REAL,
    UNIQUE(run_id, node_id)
);
"""


class TestDB:
    """Lightweight database fixture for recovery tests."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        con = sqlite3.connect(db_path)
        con.executescript(SCHEMA_SQL)
        con.close()

    def insert_node_attempt(
        self,
        run_id: str,
        node_id: str,
        result: str,
        failure_class: str | None = None,
        checkpoint_produced: int = 0,
    ) -> None:
        """Insert a node_attempts row."""
        con = sqlite3.connect(self.db_path)
        con.execute(
            "INSERT INTO node_attempts (run_id, node_id, result, failure_class, checkpoint_produced)"
            " VALUES (?, ?, ?, ?, ?)",
            (run_id, node_id, result, failure_class, checkpoint_produced),
        )
        con.commit()
        con.close()

    def insert_node_checkpoint(
        self,
        run_id: str,
        node_id: str,
        created_at: float | None = None,
    ) -> None:
        """Insert a node_checkpoints row."""
        import time
        con = sqlite3.connect(self.db_path)
        con.execute(
            "INSERT OR IGNORE INTO node_checkpoints (run_id, node_id, created_at)"
            " VALUES (?, ?, ?)",
            (run_id, node_id, created_at or time.time()),
        )
        con.commit()
        con.close()

    def insert_reusable_checkpoint(
        self,
        run_id: str,
        node_id: str,
        *,
        recipe: str,
        task_class: str,
    ) -> None:
        """Insert a checkpoint that ``is_node_reusable`` accepts.

        The hashes are the ones ``mini_ork.recovery.plan`` computes
        (``_current_input_hash_for_node`` / ``_current_config_hash``), and the
        manifest is empty, which ``is_node_reusable`` treats as reusable ("the
        node ran but emitted nothing to disk"). This is what makes the
        recovery planner reuse a producer instead of re-running it.
        """
        input_hash = hashlib.sha256(f"{run_id}|{node_id}|{recipe}".encode()).hexdigest()
        config_hash = hashlib.sha256(f"{task_class}|{recipe}|{run_id}".encode()).hexdigest()
        con = sqlite3.connect(self.db_path)
        con.execute(
            "INSERT OR REPLACE INTO node_checkpoints"
            " (run_id, node_id, attempt, status, input_hash, recipe_version,"
            "  config_hash, artifact_manifest_json, created_at)"
            " VALUES (?, ?, 1, 'success', ?, ?, ?, '[]', 0.0)",
            (run_id, node_id, input_hash, recipe, config_hash),
        )
        con.commit()
        con.close()


@pytest.fixture
def tmp_db(tmp_path: Path) -> TestDB:
    """Create a temporary database for recovery tests."""
    db_path = str(tmp_path / "state.db")
    return TestDB(db_path)


# ─────────────────────────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────────────────────────


def test_passed_verifiers_identifies_passed_verifiers(tmp_db):
    """A verifier with latest success attempt is identified as passed."""
    db_path = tmp_db.db_path
    run_id = "test-run"

    # Seed: verifier v1 with a success attempt (no_row reason)
    tmp_db.insert_node_attempt(
        run_id=run_id,
        node_id="v1",
        result="success",
        failure_class=None,
    )

    # Query: v1 should be in the passed set
    node_types = {"v1": "verifier"}
    reason = {"v1": "no_row"}
    passed = _passed_verifiers(db_path, run_id, node_types, reason)
    assert "v1" in passed


def test_passed_verifiers_excludes_failed_verifiers(tmp_db):
    """A verifier with latest failure attempt is NOT in passed set."""
    db_path = tmp_db.db_path
    run_id = "test-run"

    tmp_db.insert_node_attempt(
        run_id=run_id,
        node_id="v1",
        result="failure",
        failure_class="semantic_error",
    )

    node_types = {"v1": "verifier"}
    reason = {"v1": "no_row"}
    passed = _passed_verifiers(db_path, run_id, node_types, reason)
    assert "v1" not in passed


def test_passed_verifiers_latest_failure_wins(tmp_db):
    """A verifier with old success and newer failure is NOT passed."""
    db_path = tmp_db.db_path
    run_id = "test-run"

    # First attempt: success
    tmp_db.insert_node_attempt(
        run_id=run_id,
        node_id="v1",
        result="success",
        failure_class=None,
    )
    # Later attempt: failure
    tmp_db.insert_node_attempt(
        run_id=run_id,
        node_id="v1",
        result="failure",
        failure_class="semantic_error",
    )

    node_types = {"v1": "verifier"}
    reason = {"v1": "no_row"}
    passed = _passed_verifiers(db_path, run_id, node_types, reason)
    assert "v1" not in passed


def test_passed_verifiers_ignores_non_verifiers(tmp_db):
    """Non-verifier nodes are never in passed set."""
    db_path = tmp_db.db_path
    run_id = "test-run"

    tmp_db.insert_node_attempt(
        run_id=run_id,
        node_id="impl",
        result="success",
        failure_class=None,
    )

    node_types = {"impl": "implementer"}  # not a verifier
    reason = {"impl": "no_row"}
    passed = _passed_verifiers(db_path, run_id, node_types, reason)
    assert "impl" not in passed


def test_passed_verifiers_ignores_non_no_row_reason(tmp_db):
    """Verifiers with non-empty reason (e.g., already have checkpoint) are excluded."""
    db_path = tmp_db.db_path
    run_id = "test-run"

    tmp_db.insert_node_attempt(
        run_id=run_id,
        node_id="v1",
        result="success",
        failure_class=None,
    )

    node_types = {"v1": "verifier"}
    reason = {"v1": "reusable"}  # Has a checkpoint, not no_row
    passed = _passed_verifiers(db_path, run_id, node_types, reason)
    assert "v1" not in passed


def test_compute_recovery_passed_verifier_in_closure(tmp_db, tmp_path):
    """Recovery includes passed verifiers in closure without forcing downstream nodes."""
    db_path = tmp_db.db_path
    run_id = "test-run"
    run_dir = str(tmp_path / "run")

    # Workflow: v1 (verifier) -> llm (researcher) -> v2 (verifier) -> impl (implementer)
    workflow_content = """
version: "0.1.0"
task_class: test
nodes:
  - name: v1
    type: verifier
  - name: llm
    type: researcher
  - name: v2
    type: verifier
  - name: impl
    type: implementer
edges:
  - from: v1
    to: llm
  - from: llm
    to: v2
  - from: v2
    to: impl
"""
    workflow_path = tmp_path / "workflow.yaml"
    workflow_path.write_text(workflow_content)

    # Seed: v1 passed, llm has checkpoint, v2 and impl never ran
    tmp_db.insert_node_attempt(
        run_id=run_id,
        node_id="v1",
        result="success",
        failure_class=None,
    )
    tmp_db.insert_node_checkpoint(
        run_id=run_id,
        node_id="llm",
    )

    plan = compute_recovery(
        str(workflow_path), run_id, db_path, run_dir,
        recipe="test", task_class="test",
    )

    # v1 should be in closure (re-run) with reason "passed_verifier"
    assert "v1" in plan.closure
    assert plan.reason["v1"] == "passed_verifier"
    # The key behavior: v1 is NOT the failed_node (even though it's no_row).
    # The earliest non-reusable node (excluding v1) becomes the root.
    assert plan.failed_node != "v1"


def test_compute_recovery_never_ran_verifier_is_root(tmp_db, tmp_path):
    """A verifier that never ran is still a closure root."""
    db_path = tmp_db.db_path
    run_id = "test-run"
    run_dir = str(tmp_path / "run")

    workflow_content = """
version: "0.1.0"
task_class: test
nodes:
  - name: v1
    type: verifier
  - name: llm
    type: researcher
edges:
  - from: v1
    to: llm
"""
    workflow_path = tmp_path / "workflow.yaml"
    workflow_path.write_text(workflow_content)

    # v1 has no attempt row, llm never ran

    plan = compute_recovery(
        str(workflow_path), run_id, db_path, run_dir,
        recipe="test", task_class="test",
    )

    # v1 should be the failed_node (earliest non-reusable)
    assert plan.failed_node == "v1"
    # Closure should include v1 and llm
    assert plan.closure == {"v1", "llm"}


def test_compute_recovery_from_node_skips_passed_verifiers(tmp_db, tmp_path):
    """When from_node is set, passed verifiers are not queried."""
    db_path = tmp_db.db_path
    run_id = "test-run"
    run_dir = str(tmp_path / "run")

    workflow_content = """
version: "0.1.0"
task_class: test
nodes:
  - name: v1
    type: verifier
  - name: llm
    type: researcher
  - name: impl
    type: implementer
edges:
  - from: v1
    to: llm
  - from: llm
    to: impl
"""
    workflow_path = tmp_path / "workflow.yaml"
    workflow_path.write_text(workflow_content)

    # v1 passed, llm has checkpoint
    tmp_db.insert_node_attempt(
        run_id=run_id,
        node_id="v1",
        result="success",
        failure_class=None,
    )
    tmp_db.insert_node_checkpoint(
        run_id=run_id,
        node_id="llm",
    )

    # Override with from_node=llm
    plan = compute_recovery(
        str(workflow_path), run_id, db_path, run_dir,
        recipe="test", task_class="test",
        from_node="llm",
    )

    # v1 should NOT be in closure (from_node skips passed-verifier logic)
    assert "v1" not in plan.closure
    # Closure should be llm and impl
    assert plan.closure == {"llm", "impl"}
    # failed_node should be llm (the from_node)
    assert plan.failed_node == "llm"


def test_compute_recovery_failed_verifier_keeps_today_behavior(tmp_db, tmp_path):
    """A failed verifier remains a closure root (today's behavior)."""
    db_path = tmp_db.db_path
    run_id = "test-run"
    run_dir = str(tmp_path / "run")

    workflow_content = """
version: "0.1.0"
task_class: test
nodes:
  - name: v1
    type: verifier
  - name: llm
    type: researcher
  - name: impl
    type: implementer
edges:
  - from: v1
    to: llm
  - from: llm
    to: impl
"""
    workflow_path = tmp_path / "workflow.yaml"
    workflow_path.write_text(workflow_content)

    # v1 failed, llm has checkpoint
    tmp_db.insert_node_attempt(
        run_id=run_id,
        node_id="v1",
        result="failure",
        failure_class="semantic_error",
    )
    tmp_db.insert_node_checkpoint(
        run_id=run_id,
        node_id="llm",
    )

    plan = compute_recovery(
        str(workflow_path), run_id, db_path, run_dir,
        recipe="test", task_class="test",
    )

    # v1 should be the failed_node (invalidated by _invalidate_on_failed_verifiers)
    assert plan.failed_node == "v1"
    # Closure should include v1, llm (invalidated), impl (descendant)
    assert plan.closure == {"v1", "llm", "impl"}


def test_compute_recovery_closure_shrinks_when_producer_is_reusable(
    tmp_db, tmp_path, monkeypatch
):
    """THE point of option B: a passed verifier no longer drags a reusable producer back in.

    Every other test in this file seeds a checkpoint that ``is_node_reusable``
    rejects, so ``reuse`` is empty and the only observable difference is the
    ``passed_verifier`` reason label. With a genuinely reusable producer the
    closures must differ: BEFORE, the passed verifier is the earliest
    non-reusable node, so it becomes the root and its descendants — including
    the still-valid producer — re-run; AFTER, the verifier is re-run on its own
    and the producer's checkpoint is honoured.
    """
    run_id = "test-run"
    recipe, task_class = "fw-edit", "fw_edit"
    run_dir = str(tmp_path / "run")
    Path(run_dir).mkdir()

    workflow_content = """
version: "0.1.0"
task_class: fw_edit
nodes:
  - name: v1
    type: verifier
  - name: llm
    type: researcher
  - name: impl
    type: implementer
edges:
  - from: v1
    to: llm
  - from: llm
    to: impl
"""
    workflow_path = tmp_path / "workflow.yaml"
    workflow_path.write_text(workflow_content)

    # v1 ran and passed (no checkpoint — verifiers never write one);
    # llm has a checkpoint the planner can honour; impl never ran.
    tmp_db.insert_node_attempt(run_id=run_id, node_id="v1", result="success")
    tmp_db.insert_reusable_checkpoint(run_id, "llm", recipe=recipe, task_class=task_class)

    after = compute_recovery(
        str(workflow_path), run_id, tmp_db.db_path, run_dir,
        recipe=recipe, task_class=task_class,
    )

    # BEFORE: today's behaviour — no passed-verifier exception.
    monkeypatch.setattr(plan_module, "_passed_verifiers", lambda *_a, **_k: set())
    before = compute_recovery(
        str(workflow_path), run_id, tmp_db.db_path, run_dir,
        recipe=recipe, task_class=task_class,
    )

    # BEFORE: the passed verifier is the earliest non-reusable node, so it
    # roots the closure and swallows the reusable producer. `plan.reuse`
    # subtracts the closure, so a reusable node that gets re-run does NOT
    # appear there — that subtraction is itself the signal.
    assert before.failed_node == "v1"
    assert before.closure == {"v1", "llm", "impl"}
    assert "llm" not in before.reuse  # reusable, but dragged into the rerun

    # AFTER: the verifier is re-run, but it is not a root — the closure is the
    # genuinely-incomplete node plus the verifier, and `llm` is not re-run.
    assert after.failed_node == "impl"
    assert after.closure == {"v1", "impl"}
    assert "llm" not in after.closure
    assert "llm" in after.reuse  # checkpoint honoured — the win
    assert after.reason["v1"] == "passed_verifier"

    # The measurable win.
    assert len(after.closure) < len(before.closure)


def test_compute_recovery_all_done_includes_passed_verifiers(tmp_db, tmp_path):
    """When all nodes are done but verifiers passed, they are included in closure for re-run."""
    db_path = tmp_db.db_path
    run_id = "test-run"
    run_dir = str(tmp_path / "run")

    workflow_content = """
version: "0.1.0"
task_class: test
nodes:
  - name: v1
    type: verifier
  - name: v2
    type: verifier
  - name: llm
    type: researcher
  - name: impl
    type: implementer
edges:
  - from: v1
    to: llm
  - from: llm
    to: v2
  - from: v2
    to: impl
"""
    workflow_path = tmp_path / "workflow.yaml"
    workflow_path.write_text(workflow_content)

    # Everything done: v1 passed, llm has checkpoint, v2 passed, impl has checkpoint
    tmp_db.insert_node_attempt(
        run_id=run_id,
        node_id="v1",
        result="success",
        failure_class=None,
    )
    tmp_db.insert_node_checkpoint(
        run_id=run_id,
        node_id="llm",
    )
    tmp_db.insert_node_attempt(
        run_id=run_id,
        node_id="v2",
        result="success",
        failure_class=None,
    )
    tmp_db.insert_node_checkpoint(
        run_id=run_id,
        node_id="impl",
    )

    plan = compute_recovery(
        str(workflow_path), run_id, db_path, run_dir,
        recipe="test", task_class="test",
    )

    # Both verifiers should be in closure (re-run) with passed_verifier reason
    assert "v1" in plan.closure
    assert "v2" in plan.closure
    assert plan.reason["v1"] == "passed_verifier"
    assert plan.reason["v2"] == "passed_verifier"
    # No non-reusable non-verifier node, so failed_node may be None or a verifier
    # (the key point is that verifiers are re-run, not skipped)
