"""A delegating run's empty-outputs publisher is a no-commit pass, not a failure.

``epic-runner``'s artifact contract declares ``outputs: []`` and says why:
"outputs: [] so the publisher takes the no-commit path while verifiers still
confirm recipe shape". The parent never edits a repository — its
implementer-typed nodes (``epic_dispatcher``, ``wave_aggregator``) spawn children
that do. So ``resolve_authored_patch`` correctly abstains, and the M1
empty-outputs branch turned that abstain into ``failed``: a run that passed
``epic_verifier`` and ``final_reviewer`` (23/23 checks) was reported as a
delivery failure (run-1791785967-43108, 2026-10-12).

The distinction the branch now draws is delegation, read from ``run_spawns`` —
the child-spawn ledger — not the absence of a patch, which is also how a
code-fix run that promised a change and wrote none presents. That run still
fails; only a run that provably delegated, under a contract declaring nothing to
deliver, is finalized as published.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from mini_ork.cli import publisher  # noqa: E402


# ── _delegated_to_children ──────────────────────────────────────────────────


def _db_with_spawns(tmp_path, *, rows):
    db = tmp_path / "state.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE run_spawns (spawn_id TEXT, parent_run_id TEXT, child_run_id TEXT)")
    for parent, child in rows:
        con.execute("INSERT INTO run_spawns VALUES (?, ?, ?)", (f"sp-{child}", parent, child))
    con.commit()
    con.close()
    return str(db)


def test_a_spawn_row_proves_delegation(tmp_path):
    db = _db_with_spawns(tmp_path, rows=[("run-parent", "run-parent-epic")])
    assert publisher._delegated_to_children(db, "run-parent") is True


def test_another_runs_spawn_does_not_count(tmp_path):
    db = _db_with_spawns(tmp_path, rows=[("run-other", "run-other-epic")])
    assert publisher._delegated_to_children(db, "run-parent") is False


def test_no_spawns_is_not_delegation(tmp_path):
    db = _db_with_spawns(tmp_path, rows=[])
    assert publisher._delegated_to_children(db, "run-parent") is False


def test_a_missing_db_is_not_delegation(tmp_path):
    """Fail closed: an unreadable ledger must not license a publish."""
    assert publisher._delegated_to_children(str(tmp_path / "nope.db"), "run-parent") is False
    assert publisher._delegated_to_children("", "run-parent") is False
    assert publisher._delegated_to_children(str(tmp_path / "db"), "") is False


def test_a_home_predating_run_spawns_is_not_delegation(tmp_path):
    db = tmp_path / "old.db"
    sqlite3.connect(db).close()  # no run_spawns table at all
    assert publisher._delegated_to_children(str(db), "run-parent") is False


# ── publisher_node: the outcome the branch chooses ─────────────────────────


def _delegated_run(tmp_path, monkeypatch, *, spawns):
    """An epic-runner parent shape: outputs: [] contract, an APPROVE verdict, a
    pre-implementer-ref baseline and no implementer transcript — i.e. exactly the
    shape whose abstain the branch must classify."""
    root = tmp_path / "engine"
    contract = root / "recipes" / "epic-runner" / "artifact_contract.yaml"
    contract.parent.mkdir(parents=True)
    contract.write_text("outputs: []\nsource_artifact: ${MINI_ORK_RUN_DIR}/delivery.json\n")

    run_dir = tmp_path / "runs" / "run-parent"
    run_dir.mkdir(parents=True)
    (run_dir / "panel-verdict.json").write_text(json.dumps({"verdict": "APPROVE"}))
    (run_dir / "pre-implementer-ref").write_text("e28d17be3765\n")
    (run_dir / "run_profile.json").write_text(json.dumps({"roots": {"target": str(tmp_path)}}))

    db = _db_with_spawns(tmp_path, rows=spawns)
    monkeypatch.setenv("MINI_ORK_RECIPE_ROOT", str(root))
    monkeypatch.setenv("MO_ORACLE_GATES_AUTO", "0")
    monkeypatch.setenv("MO_LEVEL_VECTOR", "0")
    return root, run_dir, db


def _status(db, run_id):
    """The row as the publisher's ``set_status``/``finalize_passed`` leave it.

    The stub carries only the columns those two writers touch, so the test does
    not have to reproduce the engine's whole migration history.
    """
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE IF NOT EXISTS task_runs ("
        "id TEXT PRIMARY KEY, status TEXT, verdict TEXT, notes TEXT, "
        "created_at INTEGER, updated_at INTEGER, ended_at INTEGER, duration_ms INTEGER)")
    con.execute("INSERT OR IGNORE INTO task_runs "
                "(id, status, notes, created_at, duration_ms) VALUES (?, 'executing', '', 1, 0)",
                (run_id,))
    con.commit()
    row = con.execute("SELECT status, notes FROM task_runs WHERE id = ?", (run_id,)).fetchone()
    con.close()
    return row


def test_a_delegating_run_is_published_not_failed(tmp_path, monkeypatch):
    run_id = "run-parent"
    _, run_dir, db = _delegated_run(tmp_path, monkeypatch, spawns=[(run_id, "run-parent-epic")])
    _status(db, run_id)

    rc, finish = publisher.publisher_node(str(tmp_path / "engine"), str(run_dir), db, run_id,
                                          "epic-runner", "epic_runner_delivery")

    assert (rc, finish) == (0, "done")
    status, notes = _status(db, run_id)
    assert status == "published", notes
    assert "delegated its implementation" in notes
    # The abstain record survives: the run still committed nothing.
    assert json.loads((run_dir / "publish-abstain.json").read_text())["reason"] == \
        "publish-no-authored-patch"


def test_a_non_delegating_run_that_authored_nothing_still_fails(tmp_path, monkeypatch):
    """The guard the abstain exists for: no children, no authored patch."""
    run_id = "run-solo"
    _, run_dir, db = _delegated_run(tmp_path, monkeypatch, spawns=[("run-other", "run-other-epic")])
    _status(db, run_id)

    rc, finish = publisher.publisher_node(str(tmp_path / "engine"), str(run_dir), db, run_id,
                                          "epic-runner", "epic_runner_delivery")

    assert (rc, finish) == (0, "publish_abstain")
    assert _status(db, run_id)[0] == "failed"
