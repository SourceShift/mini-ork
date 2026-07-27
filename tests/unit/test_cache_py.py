"""Unit tests for mini_ork.cache (Python port of lib/cache.sh).

Two things proven here:
1. The pure hashing helpers (input_hash, hash_bundle) implement the documented
   algorithm: sha256 of the input, with files inlined by content and each
   bundle part terminated by the 0x1e record separator.
2. The win #2 BEHAVIOR CHANGE: the Python cache hits across iterations on an
   identical input_hash (iter dropped from the lookup predicate) — the
   ×5-recursion recompute fix.
"""
from __future__ import annotations

import hashlib
import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork import cache  # noqa: E402


@pytest.fixture
def db(tmp_path):
    p = str(tmp_path / "state.db")
    cache.init_schema(p)
    return p


def test_input_hash_is_sha256():
    for s in ["hello", "abc123", "the-quick-brown-fox", "x1y2z3"]:
        assert cache.input_hash(s) == hashlib.sha256(s.encode("utf-8")).hexdigest(), s


def test_hash_bundle_algorithm(tmp_path):
    # Files are inlined by content; every part (literal or file content) is
    # followed by the 0x1e record separator.
    f = tmp_path / "frag.txt"
    f.write_text("some file content", encoding="utf-8")
    expected = hashlib.sha256(
        ("alpha" + "\x1e" + "some file content" + "\x1e" + "beta" + "\x1e").encode("utf-8")
    ).hexdigest()
    assert cache.hash_bundle("alpha", str(f), "beta") == expected


def test_same_iter_hit(db):
    cache.emit("worker", "E1", 1, "HASH1", "success", "/out/x", "/log/x", db=db)
    assert cache.lookup("worker", "E1", "HASH1", iter=1, db=db) == "/out/x"


def test_cross_iteration_hit_is_the_win2_fix(db):
    """The core win #2 proof: same input_hash emitted at iter 1, looked up at
    iter 2 → HIT (iter dropped from the lookup predicate), eliminating the
    ×5-recursion recompute."""
    cache.emit("worker", "E1", 1, "HASH1", "success", "/out/x", "/log/x", db=db)
    assert cache.lookup("worker", "E1", "HASH1", iter=2, db=db) == "/out/x"


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


def test_run_summary(db):
    """``cache.run_summary`` renders the per-stage reuse stats (stage,
    SUM(reused_count), SUM(cost*reused_count) rounded to 2dp) for rows of
    the job with reused_count > 0; jobs with no reused rows render empty."""
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

    out = cache.run_summary("job-A", db=db)
    lines = {ln.split()[0]: ln for ln in out.splitlines()
             if ln.strip() and not ln.startswith("-")}
    # header + one row per stage with reuses (the reused_count=0 row is excluded)
    assert set(lines) == {"stage", "reviewer", "worker"}
    # worker: reuses = 2 + 3 = 5; saved = 0.5*2 + 0.5*3 = 2.5
    assert "5" in lines["worker"].split() and "2.5" in lines["worker"].split()
    # reviewer: reuses = 1; saved = 1.25*1 = 1.25 (ROUND(...,2) → 1.25)
    assert "1" in lines["reviewer"].split() and "1.25" in lines["reviewer"].split()
    # job with no reused rows → empty output
    assert cache.run_summary("job-none", db=db).strip() == ""
