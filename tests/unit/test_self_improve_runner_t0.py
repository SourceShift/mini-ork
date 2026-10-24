"""Unit tests: RSI Tier-0 #8 (loop defects D4 + D5) in
``mini_ork/cli/self_improve.py``.

D5 — the per-iteration dispatch arms the run's own ``--deadline`` 120 s ahead of
the coreutils backstop, with a 60 s floor.
D4 — a successful iteration closes its ``learning_record`` rows: rank 1 is
``resolved``, lower ranks are ``deferred``, and ``record_success``'s supersede
sweep skips the current iteration.
D3 — the kickoff template no longer backticks the branch name.

Hermetic temp sqlite only: no subprocess dispatch, no real worktrees.
"""
from __future__ import annotations

import inspect
import re
import sqlite3
import time
from pathlib import Path

from mini_ork.cli import self_improve as si

REPO = Path(__file__).resolve().parents[2]
_MIGRATION = REPO / "db" / "migrations" / "0017_self_improve_learning.sql"


def _fresh_db(tmp_path):
    db = str(tmp_path / "state.db")
    con = sqlite3.connect(db)
    con.executescript(_MIGRATION.read_text())
    con.commit()
    con.close()
    return db


def _insert_learning(db, run_id, iter_, rank, outcome="open"):
    ts = int(time.time())
    con = sqlite3.connect(db)
    con.execute(
        "INSERT INTO learning_record (run_id, iter, rank, category, title,"
        " evidence_paths, arxiv_refs, patch_summary, outcome, severity, confidence,"
        " created_at, updated_at)"
        " VALUES (?,?,?,'meta',?,'[]','[]',NULL,?,'medium',0.5,?,?)",
        (run_id, int(iter_), int(rank), f"bottleneck {run_id} it{iter_} r{rank}",
         outcome, ts, ts),
    )
    con.commit()
    con.close()


def _outcomes(db, run_id, iter_):
    con = sqlite3.connect(db)
    rows = con.execute(
        "SELECT rank, outcome FROM learning_record WHERE run_id=? AND iter=?"
        " ORDER BY rank",
        (run_id, int(iter_)),
    ).fetchall()
    con.close()
    return dict(rows)


# ── D5: the dispatch argv ──

def test_build_dispatch_argv_exact():
    assert si.build_dispatch_argv("/r", "/k.md", 2160) == [
        "timeout", "2160", "/r/bin/mini-ork", "run", "--deadline", "2040",
        "recursive-self-improve", "/k.md",
    ]


def test_build_dispatch_argv_floor_60():
    def deadline(argv):
        return argv[argv.index("--deadline") + 1]

    assert deadline(si.build_dispatch_argv("/r", "/k.md", 100)) == "60"
    # exactly at the boundary the floor still binds
    assert deadline(si.build_dispatch_argv("/r", "/k.md", 180)) == "60"
    # just above it the subtraction governs again
    assert deadline(si.build_dispatch_argv("/r", "/k.md", 200)) == "80"


def test_build_dispatch_argv_int_coercion():
    argv = si.build_dispatch_argv("/r", "/k.md", "2160")
    assert argv[1] == "2160"
    assert argv[argv.index("--deadline") + 1] == "2040"


def test_default_dispatch_uses_builder():
    src = inspect.getsource(si._iter_run)
    assert re.search(
        r"build_dispatch_argv\(\s*root\s*,\s*kickoff\s*,\s*timeout_s\s*\)", src,
    ), "_default_dispatch must construct its argv via build_dispatch_argv"
    assert re.search(r"cwd\s*=\s*wt\b", src)
    assert re.search(r"stdout\s*=\s*fh\b", src)
    assert re.search(r"stderr\s*=\s*subprocess\.STDOUT\b", src)
    assert re.search(r"env\s*=\s*e\b", src)


# ── D4: the learning_record lifecycle ──

def test_close_iteration_findings_resolve_and_defer(tmp_path):
    db = _fresh_db(tmp_path)
    for rank in (1, 2, 3):
        _insert_learning(db, "run-1", 2, rank)
    _insert_learning(db, "run-0", 1, 1)

    before = int(time.time())
    assert si.close_iteration_findings(db, "run-1", 2) == (1, 2)

    assert _outcomes(db, "run-1", 2) == {1: "resolved", 2: "deferred", 3: "deferred"}
    assert _outcomes(db, "run-0", 1) == {1: "open"}

    # the UPDATEs are committed, with a fresh timestamp
    con = sqlite3.connect(db)
    touched = con.execute(
        "SELECT updated_at FROM learning_record WHERE run_id='run-1' AND iter=2"
    ).fetchall()
    con.close()
    assert touched and all(u >= before for (u,) in touched)


def test_close_iteration_findings_only_open_rows_match(tmp_path):
    db = _fresh_db(tmp_path)
    _insert_learning(db, "run-1", 5, 1)
    _insert_learning(db, "run-1", 5, 2)
    assert si.close_iteration_findings(db, "run-1", 5) == (1, 1)
    # the outcome='open' guard makes a second call a no-op
    assert si.close_iteration_findings(db, "run-1", 5) == (0, 0)
    assert _outcomes(db, "run-1", 5) == {1: "resolved", 2: "deferred"}


def test_current_iteration_not_superseded(tmp_path):
    db = _fresh_db(tmp_path)
    for rank in (1, 2, 3):
        _insert_learning(db, "run-1", 2, rank)
    si.close_iteration_findings(db, "run-1", 2)

    si.record_success(db, "run-1", 2, "self-improve/iter-2", "abc12345")

    # rank 0 is the success row record_success itself inserts; ranks 2-3 stay
    # deferred because the sweep excludes the current iteration
    assert _outcomes(db, "run-1", 2) == {
        0: "resolved", 1: "resolved", 2: "deferred", 3: "deferred",
    }


def test_later_iteration_supersedes(tmp_path):
    db = _fresh_db(tmp_path)
    for rank in (1, 2, 3):
        _insert_learning(db, "run-1", 2, rank)
    si.close_iteration_findings(db, "run-1", 2)
    si.record_success(db, "run-1", 2, "self-improve/iter-2", "abc12345")

    # a deferred row from a different run is still superseded
    si.record_success(db, "run-2", 3, "self-improve/iter-3", "def67890")

    outcomes = _outcomes(db, "run-1", 2)
    assert outcomes[2] == "superseded"
    assert outcomes[3] == "superseded"
    assert outcomes[1] == "resolved"


def test_record_success_sweep_skips_current_iteration_sql():
    src = re.sub(r"\s+", " ", inspect.getsource(si.record_success))
    assert "WHERE outcome='deferred' AND NOT (run_id=? AND iter=?)" in src


def test_success_branch_order_promote_close_record():
    src = inspect.getsource(si._iter_run)
    i_promote = src.find("promote_synthesis_findings(")
    i_close = src.find("close_iteration_findings(")
    i_record = src.find("record_success(")
    assert -1 not in (i_promote, i_close, i_record)
    assert i_promote < i_close < i_record


def test_close_iteration_findings_fail_open(tmp_path):
    not_db = tmp_path / "not-a-db.bin"
    not_db.write_bytes(b"this is definitely not a sqlite database\n" * 8)
    assert si.close_iteration_findings(str(not_db), "run-1", 2) == (0, 0)


# ── D3: the kickoff template wording ──

def test_kickoff_template_branch_not_backticked():
    branch = "self-improve/iter-9-x"
    text = si._kickoff_template(9, "/tmp/wt-iter-9", branch)
    assert "on branch " + branch in text
    assert "`" + branch not in text
    # nothing else in the template changed: the Target-repo path keeps its
    # backticks and the section anchors survive
    assert "`/tmp/wt-iter-9`" in text
    for anchor in ("## Goal", "## Scope", "## Success Criteria",
                   "## Verification Command", "an isolated git worktree",
                   "(the Python runtime is the only runtime)"):
        assert anchor in text


def test_module_source_no_backticked_branch_placeholder():
    mod_src = inspect.getsource(si)
    assert "`{branch}`" not in mod_src
    flat = re.sub(r"\s+", " ", mod_src)
    assert "on branch {branch}" in flat
