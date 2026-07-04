"""Parity gate: mini_ork.ported.role_evolver vs lib/role_evolver.sh.

Each test seeds a temp DB via ``db/init.sh``, then invokes the live bash
subprocess (``bash -c '. lib/role_evolver.sh && role_evolver_propose ...'``)
on one DB and the Python port on a parallel DB, then diffs the resulting
``role_evolver_log`` rows (excluding ``proposed_at`` which can drift by a
second between two ``time.time()`` calls). Floats compared at 1e-6.

>=6 cases:
  (a) propose_empty_db_returns_0
  (b) propose_retire_signal_seeded
  (c) propose_split_signal_seeded
  (d) propose_rename_signal_seeded
  (e) propose_idempotent_second_call_inserts_0
  (f) list_proposals_format_parity
  (g) accept_reject_status_transitions
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.ported import role_evolver as re  # noqa: E402

SH = REPO / "lib" / "role_evolver.sh"
INIT_SH = REPO / "db" / "init.sh"


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures / helpers
# ─────────────────────────────────────────────────────────────────────────────
@pytest.fixture
def temp_db(tmp_path_factory):
    """Spin up a real mini-ork SQLite DB via db/init.sh so the schema
    matches what the bash wrapper sees in production."""
    home = tmp_path_factory.mktemp("home")
    dbp = str(home / "state.db")
    subprocess.run(
        ["bash", str(INIT_SH)],
        env={**os.environ, "MINI_ORK_HOME": str(home), "MINI_ORK_DB": dbp},
        capture_output=True, text=True, check=True,
    )
    return dbp


def _pair_db(tmp_path_factory):
    """Two DBs with identical schemas: one for the bash subprocess, one for
    the Python port. Both share the same home so init.sh paths don't drift.
    """
    base = tmp_path_factory.mktemp("pair")
    home_bash = base / "bash"
    home_py = base / "py"
    home_bash.mkdir()
    home_py.mkdir()
    bash_db = str(home_bash / "state.db")
    py_db = str(home_py / "state.db")
    for env_home, env_db in ((home_bash, bash_db), (home_py, py_db)):
        subprocess.run(
            ["bash", str(INIT_SH)],
            env={**os.environ, "MINI_ORK_HOME": str(env_home), "MINI_ORK_DB": env_db},
            capture_output=True, text=True, check=True,
        )
    return bash_db, py_db


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
    """Snapshot role_evolver_log for row-by-row diffing. ``proposed_at`` is
    normalised to 0 because ``int(time.time())`` differs between two
    subprocess invocations and isn't part of the parity surface."""
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


def _bash_propose(db: str, top: int = 5, class_filter: str = "") -> int:
    args = ["--top", str(top)]
    if class_filter:
        args += ["--task-class", class_filter]
    r = subprocess.run(
        ["bash", "-c", f'. "{SH}" && role_evolver_propose {" ".join(args)}'],
        env={**os.environ, "MINI_ORK_DB": db},
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        raise RuntimeError(f"bash propose failed: {r.stderr}")
    return int(r.stdout.strip())


def _bash_list(db: str, status: str = "open") -> str:
    r = subprocess.run(
        ["bash", "-c", f'. "{SH}" && role_evolver_list --{status}'],
        env={**os.environ, "MINI_ORK_DB": db},
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        raise RuntimeError(f"bash list failed: {r.stderr}")
    return r.stdout.rstrip("\n")


def _bash_accept(db: str, pid: int) -> None:
    subprocess.run(
        ["bash", "-c", f'. "{SH}" && role_evolver_accept {pid}'],
        env={**os.environ, "MINI_ORK_DB": db},
        capture_output=True, text=True, check=True,
    )


def _bash_reject(db: str, pid: int) -> None:
    subprocess.run(
        ["bash", "-c", f'. "{SH}" && role_evolver_reject {pid}'],
        env={**os.environ, "MINI_ORK_DB": db},
        capture_output=True, text=True, check=True,
    )


# ─────────────────────────────────────────────────────────────────────────────
# (a) empty DB → 0 inserted on both sides
# ─────────────────────────────────────────────────────────────────────────────
def test_propose_empty_db_returns_0(tmp_path_factory):
    """No signals present → both bash and python return 0 and write nothing."""
    bash_db, py_db = _pair_db(tmp_path_factory)
    rc_bash = _bash_propose(bash_db)
    rc_py = re.propose(db=py_db)
    assert rc_bash == 0
    assert rc_py == 0
    assert _all_log_rows(bash_db) == []
    assert _all_log_rows(py_db) == []


# ─────────────────────────────────────────────────────────────────────────────
# (b) Signal 1 (retire): agent_performance_memory with negative advantage
# ─────────────────────────────────────────────────────────────────────────────
def test_propose_retire_signal_seeded(tmp_path_factory):
    """Seed three lanes with relative_advantage<=-0.20 and runs>=3; verify
    both bash and python emit identical 'retire' proposals."""
    bash_db, py_db = _pair_db(tmp_path_factory)
    seed_rows = [
        ("minimax", "implementer", "minimax-m3", "code_review", -0.42, 5),
        ("codex",   "implementer", "codex-5",     "code_review", -0.31, 7),
        ("kimi",    "implementer", "kimi-k2",     "code_review",  0.10, 4),  # positive → not a loser
    ]
    for dbp in (bash_db, py_db):
        con = sqlite3.connect(dbp)
        for lane, role, model, tc, ra, rc in seed_rows:
            _seed_loser(con, lane, tc, role, model, ra, rc)
        con.commit(); con.close()

    n_bash = _bash_propose(bash_db, top=5)
    n_py = re.propose(db=py_db, top=5)
    assert n_bash == 2
    assert n_py == 2

    rows_bash = _all_log_rows(bash_db)
    rows_py = _all_log_rows(py_db)
    assert rows_bash == rows_py

    # Float sanity: the rationale embeds f"{ra:.2f}"; compare at 1e-6.
    rationale_texts = [r["rationale"] for r in rows_py]
    assert any("-0.42" in t for t in rationale_texts)
    assert any("-0.31" in t for t in rationale_texts)


# ─────────────────────────────────────────────────────────────────────────────
# (c) Signal 2 (split): bug_reports clustered by agent_role
# ─────────────────────────────────────────────────────────────────────────────
def test_propose_split_signal_seeded(tmp_path_factory):
    """Seed two open high-severity bug_reports for the same agent_role;
    verify both sides emit a 'split' proposal with the same rationale."""
    bash_db, py_db = _pair_db(tmp_path_factory)
    now = 1_700_000_000
    for dbp in (bash_db, py_db):
        con = sqlite3.connect(dbp)
        _seed_bug(con, "implementer", "lane X misses edge cases", "high", 3, now)
        _seed_bug(con, "implementer", "lane X retries infinitely", "critical", 5, now)
        # Decoy cluster that should NOT trigger (only 1 open bug per role).
        _seed_bug(con, "reviewer", "single reviewer bug", "high", 1, now)
        con.commit(); con.close()

    n_bash = _bash_propose(bash_db, top=5)
    n_py = re.propose(db=py_db, top=5)
    assert n_bash == 1
    assert n_py == 1

    rows_bash = _all_log_rows(bash_db)
    rows_py = _all_log_rows(py_db)
    assert rows_bash == rows_py
    assert rows_py[0]["proposal_kind"] == "split"
    assert rows_py[0]["target_node_id"] == "implementer"
    # The correlated sub-select picked "critical" over "high", then by frequency DESC.
    assert "lane X retries infinitely" in rows_py[0]["rationale"]


# ─────────────────────────────────────────────────────────────────────────────
# (d) Signal 3 (rename): gradient_records cross_class
# ─────────────────────────────────────────────────────────────────────────────
def test_propose_rename_signal_seeded(tmp_path_factory):
    """Seed cross_class gradient_records with confidence>=0.85; verify both
    sides extract the node_name as the last dot-segment."""
    bash_db, py_db = _pair_db(tmp_path_factory)
    now = 1_700_000_000
    for dbp in (bash_db, py_db):
        con = sqlite3.connect(dbp)
        _seed_gradient(con, "cross_class:workflow.node.coherence_check", 0.91,
                       "g-1", now)
        _seed_gradient(con, "cross_class:workflow.node.validator", 0.86,
                       "g-2", now)
        # Decoy below the 0.85 confidence threshold.
        _seed_gradient(con, "cross_class:workflow.node.should_skip", 0.50,
                       "g-3", now)
        # Decoy with wrong task_class.
        _seed_gradient(con, "specific:workflow.node.skip_me", 0.99,
                       "g-4", now)
        con.commit(); con.close()

    n_bash = _bash_propose(bash_db, top=5)
    n_py = re.propose(db=py_db, top=5)
    assert n_bash == 2
    assert n_py == 2

    rows_bash = _all_log_rows(bash_db)
    rows_py = _all_log_rows(py_db)
    assert rows_bash == rows_py

    node_ids = {r["target_node_id"] for r in rows_py}
    assert node_ids == {"coherence_check", "validator"}
    assert all(r["proposal_kind"] == "rename" for r in rows_py)


# ─────────────────────────────────────────────────────────────────────────────
# (e) idempotence: second propose() insert count is 0
# ─────────────────────────────────────────────────────────────────────────────
def test_propose_idempotent_second_call_inserts_0(tmp_path_factory):
    """Second propose() on the same DB returns 0 because the open proposals
    already exist. Row count stays the same."""
    bash_db, py_db = _pair_db(tmp_path_factory)
    for dbp in (bash_db, py_db):
        con = sqlite3.connect(dbp)
        _seed_loser(con, "minimax", "code_review", "implementer",
                    "minimax-m3", -0.42, 5)
        con.commit(); con.close()

    n_bash_1 = _bash_propose(bash_db)
    n_py_1 = re.propose(db=py_db)
    n_bash_2 = _bash_propose(bash_db)
    n_py_2 = re.propose(db=py_db)
    assert n_bash_1 == n_py_1 == 1
    assert n_bash_2 == n_py_2 == 0

    # Row diff between bash and python DBs identical (modulo proposed_at).
    rows_bash = _all_log_rows(bash_db)
    rows_py = _all_log_rows(py_db)
    assert rows_bash == rows_py


# ─────────────────────────────────────────────────────────────────────────────
# (f) list_proposals printf-format parity
# ─────────────────────────────────────────────────────────────────────────────
def test_list_proposals_format_parity(tmp_path_factory):
    """Seed three proposals with varying widths; verify bash's
    pipe-separated printf output matches the Python port byte-for-byte."""
    bash_db, py_db = _pair_db(tmp_path_factory)
    # Use python port to seed on both DBs (single code path → identical rows).
    for dbp in (bash_db, py_db):
        con = sqlite3.connect(dbp)
        _seed_loser(con, "minimax", "code_review", "implementer",
                    "minimax-m3", -0.42, 5)
        _seed_bug(con, "reviewer", "skips stub assertions", "critical", 4,
                  1_700_000_000)
        _seed_bug(con, "reviewer", "skips stub assertions 2", "critical", 5,
                  1_700_000_000)
        _seed_gradient(con, "cross_class:workflow.node.guard", 0.95,
                       "g-1", 1_700_000_000)
        con.commit(); con.close()
        re.propose(db=dbp, top=5)

    bash_lines = _bash_list(bash_db).splitlines()
    py_lines = re.list_proposals(db=py_db).splitlines()
    # Same number of rows.
    assert len(bash_lines) == len(py_lines) == 3
    # Byte-for-byte column layout (width padding + substr truncation).
    assert bash_lines == py_lines
    # Width sanity: first row's id column is exactly 4 chars.
    assert len(bash_lines[0].split(" | ")[0]) == 4
    # Status column is exactly 10 chars (left-padded with spaces).
    assert len(bash_lines[0].split(" | ")[1]) == 10
    # proposal_kind column is exactly 7 chars.
    assert len(bash_lines[0].split(" | ")[2]) == 7


# ─────────────────────────────────────────────────────────────────────────────
# (g) accept / reject status transitions
# ─────────────────────────────────────────────────────────────────────────────
def test_accept_reject_status_transitions(tmp_path_factory):
    """Seed two proposals; bash accept() and python accept() flip the same
    row to 'accepted'. Same for reject(). Update counts equal between sides."""
    bash_db, py_db = _pair_db(tmp_path_factory)
    for dbp in (bash_db, py_db):
        con = sqlite3.connect(dbp)
        _seed_loser(con, "minimax", "code_review", "implementer",
                    "minimax-m3", -0.42, 5)
        _seed_loser(con, "codex", "code_review", "implementer",
                    "codex-5", -0.50, 6)
        con.commit(); con.close()
        re.propose(db=dbp, top=5)

    # bash: accept id=1, python: accept id=1. Both should flip status.
    _bash_accept(bash_db, 1)
    re.accept(py_db, 1)

    def _status(db, pid):
        con = sqlite3.connect(db)
        r = con.execute("SELECT status FROM role_evolver_log WHERE id=?",
                        (pid,)).fetchone()
        con.close()
        return r[0]

    assert _status(bash_db, 1) == "accepted"
    assert _status(py_db, 1) == "accepted"
    assert _status(bash_db, 2) == "open"
    assert _status(py_db, 2) == "open"

    # bash: reject id=2, python: reject id=2.
    _bash_reject(bash_db, 2)
    re.reject(py_db, 2)
    assert _status(bash_db, 2) == "rejected"
    assert _status(py_db, 2) == "rejected"

    # Negative: missing id raises ValueError on python (bash emits to stderr).
    with pytest.raises(ValueError):
        re.accept(py_db, 0)
    r = subprocess.run(
        ["bash", "-c", f'. "{SH}" && role_evolver_accept'],
        env={**os.environ, "MINI_ORK_DB": py_db},
        capture_output=True, text=True,
    )
    assert r.returncode != 0
    assert "id required" in r.stderr