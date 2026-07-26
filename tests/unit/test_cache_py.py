"""Integration + parity gate for mini_ork.cache (Python port of lib/cache.sh).

Two things proven here:
1. PARITY on the pure bits (input_hash, hash_bundle) vs the live bash functions.
2. The win #2 BEHAVIOR CHANGE: the Python cache hits across iterations on an
   identical input_hash, where the bash cache (iter in the predicate) misses —
   this is the ×5-recursion recompute fix, demonstrated against live bash.
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
from mini_ork import cache  # noqa: E402

CACHE_SH = REPO / "lib" / "cache.sh"


def _bash(db: str, snippet: str) -> str:
    env = {**os.environ, "MINI_ORK_DB": db, "JOB_ID": "test"}
    r = subprocess.run(
        ["bash", "-c", f'. "{CACHE_SH}" >/dev/null 2>&1; {snippet}'],
        capture_output=True, text=True, env=env,
    )
    return r.stdout.strip()


@pytest.fixture
def db(tmp_path):
    p = str(tmp_path / "state.db")
    cache.init_schema(p)
    return p


def test_input_hash_parity(db):
    for s in ["hello", "abc123", "the-quick-brown-fox", "x1y2z3"]:
        bash_h = _bash(db, f"printf %s {s!r} | mo_cache_input_hash")
        assert cache.input_hash(s) == bash_h, s


def test_hash_bundle_parity(db, tmp_path):
    f = tmp_path / "frag.txt"
    f.write_text("some file content", encoding="utf-8")
    bash_h = _bash(db, f'mo_cache_hash_bundle "alpha" "{f}" "beta"')
    assert cache.hash_bundle("alpha", str(f), "beta") == bash_h


def test_same_iter_hit_parity(db):
    # Both bash and python must HIT at the emitting iteration.
    cache.emit("worker", "E1", 1, "HASH1", "success", "/out/x", "/log/x", db=db)
    assert cache.lookup("worker", "E1", "HASH1", iter=1, db=db) == "/out/x"
    assert _bash(db, "mo_cache_lookup worker E1 1 HASH1") == "/out/x"


def test_cross_iteration_hit_is_the_win2_fix(db):
    """The core win #2 proof: same input_hash emitted at iter 1, looked up at
    iter 2. Python HITS (iter dropped from predicate); bash MISSES (iter in
    predicate) — the exact ×5-recursion recompute we're eliminating."""
    cache.emit("worker", "E1", 1, "HASH1", "success", "/out/x", "/log/x", db=db)
    # Python: cross-iteration HIT
    assert cache.lookup("worker", "E1", "HASH1", iter=2, db=db) == "/out/x"
    # Bash: cross-iteration MISS (demonstrates the bug being fixed)
    assert _bash(db, "mo_cache_lookup worker E1 2 HASH1") == ""


def test_new_verifier_stages_allowed(db):
    # win #2 widening: the recursion-loop stages are now cacheable.
    cache.emit("tier1", "E1", 3, "H2", "success", "/out/t1", "/log", db=db)
    assert cache.lookup("tier1", "E1", "H2", db=db) == "/out/t1"


def test_record_hit_increments(db):
    cache.emit("worker", "E1", 1, "H5", "success", "/o", "/l", db=db)
    cache.record_hit("worker", "E1", "H5", db=db)
    cache.record_hit("worker", "E1", "H5", db=db)
    con = sqlite3.connect(db)
    n = con.execute(
        "SELECT reused_count FROM mini_orch_sessions WHERE input_hash='H5'"
    ).fetchone()[0]
    con.close()
    assert n == 2


def test_expired_not_returned_and_gc(db):
    cache.emit("worker", "E2", 1, "H3", "success", "/o", "/l", db=db)
    con = sqlite3.connect(db)
    con.execute("UPDATE mini_orch_sessions SET expires_at='2000-01-01T00:00:00.000Z'")
    con.commit()
    con.close()
    assert cache.lookup("worker", "E2", "H3", db=db) is None
    assert cache.gc(db) >= 1


def test_run_summary_parity(db):
    """A/B (WS5): ``cache.run_summary`` vs live ``mo_cache_run_summary`` —
    the sqlite3 ``-column -header`` table must be byte-identical (same CLI,
    same SQL), including the empty-result case."""
    con = sqlite3.connect(db)
    rows = [
        ("u1", "job-A", "E1", 1, "worker", "h1", "success", 0.5, 2),
        ("u2", "job-A", "E1", 2, "reviewer", "h2", "success", 1.25, 1),
        ("u3", "job-A", "E2", 1, "worker", "h1", "success", 0.5, 3),
        ("u4", "job-B", "E1", 1, "worker", "h9", "success", 9.0, 5),
        ("u5", "job-A", "E1", 3, "worker", "h0", "success", 0.5, 0),  # no reuses
    ]
    for uuid, job, epic, it, stage, h, status, cost, reused in rows:
        con.execute(
            "INSERT INTO mini_orch_sessions "
            "(uuid, job_id, epic_id, iter, stage, input_hash, status, "
            " output_path, log_path, cost_usd, turns, duration_ms, "
            " expires_at, reused_count) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (uuid, job, epic, it, stage, h, status, "/o", "/l", cost, 1, 100,
             "2099-01-01T00:00:00.000Z", reused),
        )
    con.commit()
    con.close()

    py_out = cache.run_summary("job-A", db=db)
    bash_out = _bash(db, "mo_cache_run_summary job-A")
    assert py_out.strip() == bash_out.strip()
    assert "worker" in py_out and "reviewer" in py_out
    # job with no reused rows → both sides empty
    assert cache.run_summary("job-none", db=db).strip() == \
        _bash(db, "mo_cache_run_summary job-none").strip()
