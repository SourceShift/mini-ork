"""Unit tests for mini_ork.learning.role_evolver.

Each test seeds a temp DB via the native ``mini_ork.stores.migrate.init_db``,
drives the Python port (``re.propose`` / ``re.list_proposals`` /
``re.accept`` / ``re.reject``), and asserts the resulting
``role_evolver_log`` rows (excluding ``proposed_at`` which is wall-clock).

Cases:
  (a) propose_empty_db_returns_0
  (b) propose_retire_signal_seeded
  (c) propose_split_signal_seeded
  (d) propose_rename_signal_seeded
  (e) propose_idempotent_second_call_inserts_0
  (f) list_proposals_format
  (g) accept_reject_status_transitions
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.learning import role_evolver as re  # noqa: E402
from mini_ork.stores.migrate import init_db  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures / helpers
# ─────────────────────────────────────────────────────────────────────────────
@pytest.fixture
def db(tmp_path_factory):
    """Spin up a real mini-ork SQLite DB via the native init_db port so the
    schema matches what production runs against."""
    home = tmp_path_factory.mktemp("home")
    dbp = str(home / "state.db")
    rc, out, err = init_db(db=dbp, root=str(REPO))
    assert rc == 0, f"init_db failed rc={rc}\nstdout={out}\nstderr={err}"
    return dbp


def _seed_loser(con, lane: str, task_class: str, role: str, model: str,
                relative_advantage: float, runs_count: int) -> None:
    con.execute(
        """
        INSERT INTO agent_performance_memory
            (agent_version_id, role, model, task_class, runs_count,
             success_count, relative_advantage)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (lane, role, model, task_class, runs_count, 0, relative_advantage),
    )


def _seed_bug(con, agent_role: str, title: str, severity: str, frequency: int,
              now: int) -> None:
    con.execute(
        """
        INSERT INTO bug_reports
            (fingerprint, run_id, agent_role, task_class, observed_in,
             title, description, suggested_fix, severity, confidence,
             frequency, status, first_seen_at, last_seen_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (f"fp-{title}", "run-1", agent_role, "code_review", "general",
         title, "", None, severity, 0.5, frequency, "open",
         now, now, now),
    )


def _seed_gradient(con, target: str, confidence: float, gradient_id: str,
                   now: int) -> None:
    con.execute(
        """
        INSERT INTO gradient_records
            (gradient_id, target, signal, suggested_change, evidence,
             confidence, created_at, task_class)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (gradient_id, target, "rename_hint", "abstract the node name",
         "evidence blob", confidence, now, "__cross_class__"),
    )


def _all_log_rows(db: str) -> list[dict]:
    """Snapshot role_evolver_log for row-by-row assertions. ``proposed_at``
    is excluded because it is wall-clock and not part of the semantic
    surface."""
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """
        SELECT id, target_recipe, target_node_id, proposal_kind,
               rationale, evidence_json, proposed_change, status
          FROM role_evolver_log
         ORDER BY id
        """
    ).fetchall()
    con.close()
    return [
        {k: r[k] for k in ("target_recipe", "target_node_id", "proposal_kind",
                           "rationale", "evidence_json", "proposed_change", "status")}
        for r in rows
    ]


def _seed(con_fn, db):
    con = sqlite3.connect(db)
    try:
        con_fn(con)
        con.commit()
    finally:
        con.close()


# ─────────────────────────────────────────────────────────────────────────────
# (a) empty DB → 0 inserted
# ─────────────────────────────────────────────────────────────────────────────
def test_propose_empty_db_returns_0(db):
    """No signals present → propose returns 0 and writes nothing."""
    assert re.propose(db=db) == 0
    assert _all_log_rows(db) == []


# ─────────────────────────────────────────────────────────────────────────────
# (b) Signal 1 (retire): agent_performance_memory with negative advantage
# ─────────────────────────────────────────────────────────────────────────────
def test_propose_retire_signal_seeded(db):
    """Seed three lanes with relative_advantage<=-0.20 and runs>=3 → two
    'retire' proposals (the positive-advantage lane is not a loser)."""
    def seed(con):
        _seed_loser(con, "minimax", "code_review", "implementer", "minimax-m3", -0.42, 5)
        _seed_loser(con, "codex",   "code_review", "implementer", "codex-5",     -0.31, 7)
        _seed_loser(con, "kimi",    "code_review", "implementer", "kimi-k2",      0.10, 4)
    _seed(seed, db)

    assert re.propose(db=db, top=5) == 2

    rows = _all_log_rows(db)
    assert len(rows) == 2
    assert all(r["proposal_kind"] == "retire" for r in rows)
    assert all(r["status"] == "open" for r in rows)
    assert {r["target_node_id"] for r in rows} == {"minimax", "codex"}
    # the rationale embeds f"{ra:.2f}"
    rationale_texts = [r["rationale"] for r in rows]
    assert any("-0.42" in t and "minimax" in t for t in rationale_texts)
    assert any("-0.31" in t and "codex" in t for t in rationale_texts)


# ─────────────────────────────────────────────────────────────────────────────
# (c) Signal 2 (split): bug_reports clustered by agent_role
# ─────────────────────────────────────────────────────────────────────────────
def test_propose_split_signal_seeded(db):
    """Two open high-severity bug_reports for the same agent_role → one
    'split' proposal; a single-bug role does not trigger."""
    now = 1_700_000_000

    def seed(con):
        _seed_bug(con, "implementer", "lane X misses edge cases", "high", 3, now)
        _seed_bug(con, "implementer", "lane X retries infinitely", "critical", 5, now)
        # Decoy cluster that should NOT trigger (only 1 open bug per role).
        _seed_bug(con, "reviewer", "single reviewer bug", "high", 1, now)
    _seed(seed, db)

    assert re.propose(db=db, top=5) == 1

    rows = _all_log_rows(db)
    assert len(rows) == 1
    assert rows[0]["proposal_kind"] == "split"
    assert rows[0]["target_node_id"] == "implementer"
    # The correlated sub-select picked "critical" over "high", then by frequency DESC.
    assert "lane X retries infinitely" in rows[0]["rationale"]


# ─────────────────────────────────────────────────────────────────────────────
# (d) Signal 3 (rename): gradient_records cross_class
# ─────────────────────────────────────────────────────────────────────────────
def test_propose_rename_signal_seeded(db):
    """Cross_class gradient_records with confidence>=0.85 → one 'rename'
    proposal each; the node_name is the last dot-segment of the target."""
    now = 1_700_000_000

    def seed(con):
        _seed_gradient(con, "cross_class:workflow.node.coherence_check", 0.91, "g-1", now)
        _seed_gradient(con, "cross_class:workflow.node.validator", 0.86, "g-2", now)
        # Decoy below the 0.85 confidence threshold.
        _seed_gradient(con, "cross_class:workflow.node.should_skip", 0.50, "g-3", now)
        # Decoy with wrong task_class.
        _seed_gradient(con, "specific:workflow.node.skip_me", 0.99, "g-4", now)
    _seed(seed, db)

    assert re.propose(db=db, top=5) == 2

    rows = _all_log_rows(db)
    node_ids = {r["target_node_id"] for r in rows}
    assert node_ids == {"coherence_check", "validator"}
    assert all(r["proposal_kind"] == "rename" for r in rows)


# ─────────────────────────────────────────────────────────────────────────────
# (e) idempotence: second propose() insert count is 0
# ─────────────────────────────────────────────────────────────────────────────
def test_propose_idempotent_second_call_inserts_0(db):
    """Second propose() on the same DB returns 0 because the open proposals
    already exist. Row count stays the same."""
    def seed(con):
        _seed_loser(con, "minimax", "code_review", "implementer", "minimax-m3", -0.42, 5)
    _seed(seed, db)

    assert re.propose(db=db) == 1
    assert re.propose(db=db) == 0
    assert len(_all_log_rows(db)) == 1


# ─────────────────────────────────────────────────────────────────────────────
# (f) list_proposals printf format
# ─────────────────────────────────────────────────────────────────────────────
def test_list_proposals_format(db):
    """Seed three proposals with varying widths; verify the pipe-separated
    printf layout: id col width 4, status width 10, proposal_kind width 7,
    target_recipe width 18, target_node_id width 15, rationale truncated at
    80 chars."""
    def seed(con):
        _seed_loser(con, "minimax", "code_review", "implementer", "minimax-m3", -0.42, 5)
        _seed_bug(con, "reviewer", "skips stub assertions", "critical", 4, 1_700_000_000)
        _seed_bug(con, "reviewer", "skips stub assertions 2", "critical", 5, 1_700_000_000)
        _seed_gradient(con, "cross_class:workflow.node.guard", 0.95, "g-1", 1_700_000_000)
    _seed(seed, db)
    re.propose(db=db, top=5)

    lines = re.list_proposals(db=db).splitlines()
    assert len(lines) == 3
    for ln in lines:
        cols = ln.split(" | ")
        assert len(cols) == 6, f"expected 6 pipe-separated columns: {ln!r}"
        # id column is exactly 4 chars
        assert len(cols[0]) == 4
        # status column is exactly 10 chars (left-padded with spaces)
        assert len(cols[1]) == 10
        # proposal_kind column is exactly 7 chars
        assert len(cols[2]) == 7
        # target_recipe / target_node_id padded to 18 / 15
        assert len(cols[3]) == 18
        assert len(cols[4]) == 15
        # rationale truncated at 80 chars
        assert len(cols[5]) <= 80
    kinds = {ln.split(" | ")[2].strip() for ln in lines}
    assert kinds == {"retire", "split", "rename"}


# ─────────────────────────────────────────────────────────────────────────────
# (g) accept / reject status transitions
# ─────────────────────────────────────────────────────────────────────────────
def test_accept_reject_status_transitions(db):
    """accept() flips the row to 'accepted'; reject() to 'rejected'; a
    missing id raises ValueError."""
    def seed(con):
        _seed_loser(con, "minimax", "code_review", "implementer", "minimax-m3", -0.42, 5)
        _seed_loser(con, "codex", "code_review", "implementer", "codex-5", -0.50, 6)
    _seed(seed, db)
    re.propose(db=db, top=5)

    def _status(pid):
        con = sqlite3.connect(db)
        r = con.execute("SELECT status FROM role_evolver_log WHERE id=?",
                        (pid,)).fetchone()
        con.close()
        return r[0]

    re.accept(db, 1)
    assert _status(1) == "accepted"
    assert _status(2) == "open"

    re.reject(db, 2)
    assert _status(2) == "rejected"

    # Negative: missing id raises ValueError.
    with pytest.raises(ValueError):
        re.accept(db, 0)
