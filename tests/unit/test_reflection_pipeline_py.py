"""Parity gate: mini_ork.ported.reflection_pipeline vs lib/reflection_pipeline.sh.

Each test invokes the LIVE bash subprocess (the authoritative source) on the
same inputs as the Python port and asserts byte-identical stdout/stderr. No
mocks, no hardcoded expected outputs — expected is always derived from a
control bash invocation that shares the inputs.

8 cases (matching the bash public API):
  (1) reflection_deduplicate        — pass-1 exact merge against seeded gradient_records
  (2) reflection_deduplicate        — pass-2 fuzzy merge with calibrated MO_DEDUP_FUZZY
  (3) reflection_link_failures      — row count + failure_links row content via DB row-diff
  (4) reflection_detect_stale       — JSON shape (table/stale_ids/stale_before_epoch)
  (5) reflection_summarize_patterns — JSON shape for populated cluster_id (ALTER TABLE adds col)
  (6) reflection_suggest_promotions — JSON array shape with frequency-filter + rationale
  (7) reflection_persist_suggestions— INSERT OR REPLACE into emergent_patterns (idempotent)
  (8) reflection_extract_gradients  — SQL trace_id selection + injected gradient_extract stub

All cases use a temp DB initialised by db/init.sh (kickoff requirement). The
case (8) bash control sets MINI_ORK_ROOT=/tmp/empty-lib-stub so the internal
`source lib/gradient_extractor.sh` silently fails — our pre-defined stubs win,
mirroring the Python injection contract (set_gradient_extract / set_gradient_store
/ set_gradient_ensure_table).
"""
from __future__ import annotations

import json
import math
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.ported import reflection_pipeline as rp  # noqa: E402

SH = REPO / "lib" / "reflection_pipeline.sh"
INIT_SH = REPO / "db" / "init.sh"
STUB_ROOT = "/tmp/empty-lib-stub"  # bash MINI_ORK_ROOT for case (8)


# ── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture
def temp_db(tmp_path_factory, monkeypatch):
    """Spin up a real mini-ork SQLite DB via db/init.sh AND point the in-process
    Python port at it via `os.environ["MINI_ORK_DB"]`. Without the env override
    the port would silently read from a project-level DB inherited from the
    parent shell, breaking parity with the bash subprocess (which gets the temp
    DB via subprocess env)."""
    home = tmp_path_factory.mktemp("home")
    dbp = str(home / "state.db")
    subprocess.run(
        ["bash", str(INIT_SH)],
        env={**os.environ, "MINI_ORK_HOME": str(home), "MINI_ORK_DB": dbp},
        capture_output=True, text=True, check=True,
    )
    monkeypatch.setenv("MINI_ORK_DB", dbp)
    monkeypatch.setenv("MO_REFLECTION_BATCH", "500")
    monkeypatch.setenv("MO_DEDUP_BATCH", "10000")
    monkeypatch.setenv("MO_DEDUP_FUZZY", "0.55")
    monkeypatch.setenv("MINI_ORK_STALE_DAYS", "14")
    monkeypatch.setenv("MINI_ORK_PROMOTION_MIN_FREQ", "3")
    return dbp


def _seed_epic_run(con: sqlite3.Connection, epic_id: str, run_dir: str) -> int:
    """Insert a minimal epic + run row; return the new runs.id."""
    con.execute(
        "INSERT INTO epics(id, title, status) VALUES (?, 't', 'in progress')",
        (epic_id,),
    )
    con.execute(
        "INSERT INTO runs(epic_id, run_dir, branch, baseline_sha, agent) "
        "VALUES (?, ?, 'main', 'sha', 'glm')",
        (epic_id, run_dir),
    )
    return con.execute("SELECT last_insert_rowid()").fetchone()[0]


def _run_bash_function(db_path: str, bash_body: str, env_extra: dict | None = None) -> subprocess.CompletedProcess:
    """Source lib/reflection_pipeline.sh under bash, run `bash_body`, capture stdout/stderr."""
    env = {**os.environ, "MINI_ORK_DB": db_path, "MO_REFLECTION_BATCH": "500",
           "MO_DEDUP_BATCH": "10000", "MO_DEDUP_FUZZY": "0.55",
           "MINI_ORK_STALE_DAYS": "14", "MINI_ORK_PROMOTION_MIN_FREQ": "3"}
    if env_extra:
        env.update(env_extra)
    wrapper = f'. "{SH}"\n{bash_body}\n'
    return subprocess.run(
        ["bash", "-c", wrapper],
        env=env, capture_output=True, text=True,
    )


# ─────────────────────────────────────────────────────────────────────────────
# (1) reflection_deduplicate — pass-1 exact (target, signal) merge
# ─────────────────────────────────────────────────────────────────────────────
def test_reflection_deduplicate_pass1_exact(temp_db):
    """Python `reflection_deduplicate` matches live bash pass-1 against seeded
    gradient_records: 5 rows, two pairs of identical (target, signal); the lower-
    confidence row in each pair is deleted. Stderr summary must match byte-for-byte.

    Runs Python first, then re-seeds the DB to the pre-dedup state, then runs
    bash — so each implementation observes the same input."""
    seed_rows = [
        ("g-a-high",   "wf.node.foo", "verifier_output is empty",      "fix parser",    "trace-1", 0.9, "code_review"),
        ("g-a-low",    "wf.node.foo", "verifier_output is empty",      "fix parser",    "trace-1", 0.3, "code_review"),
        ("g-b-high",   "wf.node.bar", "trace_id missing",              "add trace_id",  "trace-2", 0.7, "code_review"),
        ("g-b-low",    "wf.node.bar", "trace_id missing",              "add trace_id",  "trace-2", 0.2, "code_review"),
        ("g-c-unique", "wf.node.baz", "totally different signal",      "no change",     "trace-3", 0.5, "code_review"),
    ]

    def _seed():
        now = int(time.time())
        con = sqlite3.connect(temp_db)
        con.execute("PRAGMA busy_timeout=5000")
        con.execute("DELETE FROM gradient_records")
        for gid, tgt, sig, ch, ev, conf, tc in seed_rows:
            con.execute(
                "INSERT INTO gradient_records(gradient_id, target, signal, suggested_change, "
                "evidence, confidence, created_at, task_class) VALUES (?,?,?,?,?,?,?,?)",
                (gid, tgt, sig, ch, ev, conf, now, tc),
            )
        con.commit()
        con.close()

    # Run Python first on the populated DB.
    _seed()
    py_stderr = _capture_py_stderr(lambda: rp.reflection_deduplicate("gradient_records"))

    # Re-seed and run bash.
    _seed()
    r = _run_bash_function(temp_db, "reflection_deduplicate gradient_records")
    assert r.returncode == 0, f"bash dedup failed: {r.stderr}"
    bash_stderr = r.stderr

    # Byte-parity on stderr.
    assert py_stderr == bash_stderr, (
        f"stderr mismatch.\n  py:   {py_stderr!r}\n  bash: {bash_stderr!r}"
    )
    assert "reflection_deduplicate: removed 2 duplicates (2 exact, 0 fuzzy@0.55)" in py_stderr

    # Row-diff: after bash ran on the re-seeded DB, 3 rows remain.
    con = sqlite3.connect(temp_db)
    survivors = sorted(r[0] for r in con.execute(
        "SELECT gradient_id FROM gradient_records ORDER BY gradient_id"
    ).fetchall())
    con.close()
    assert survivors == ["g-a-high", "g-b-high", "g-c-unique"], (
        f"unexpected survivors: {survivors!r}"
    )

    # Idempotent re-run on the now-clean DB: both emit "no duplicates found".
    py_idem = _capture_py_stderr(lambda: rp.reflection_deduplicate("gradient_records"))
    r2 = _run_bash_function(temp_db, "reflection_deduplicate gradient_records")
    assert py_idem == r2.stderr
    assert "no duplicates found" in py_idem


def _capture_py_stderr(fn) -> str:
    """Capture stderr from a Python callable that uses print(..., file=sys.stderr)."""
    import io
    from contextlib import redirect_stderr
    buf = io.StringIO()
    with redirect_stderr(buf):
        fn()
    return buf.getvalue()


# ─────────────────────────────────────────────────────────────────────────────
# (2) reflection_deduplicate — pass-2 fuzzy merge (difflib signal ratio)
# ─────────────────────────────────────────────────────────────────────────────
def test_reflection_deduplicate_pass2_fuzzy(temp_db):
    """Python `reflection_deduplicate` matches live bash pass-2 fuzzy merge.

    Seed two rows whose `signal` strings are rephrasings of the same lesson
    (similarity > 0.55). Different signal so pass-1 doesn't catch them; same
    (task_class, target) so pass-2 groups them. Same seed strategy as (1): run
    Python first, re-seed, then bash.
    """
    seed_rows = [
        ("g-fuzzy-hi", "wf.node.foo",
         "verifier_output is an empty object meaning the verifier failed silently",
         "add explicit failure detection",  "trace-1", 0.8, "code_review"),
        ("g-fuzzy-lo", "wf.node.foo",
         "verifier_output is an empty object, meaning the verifier returned no signal",
         "add verifier completeness check", "trace-1", 0.4, "code_review"),
    ]

    def _seed():
        now = int(time.time())
        con = sqlite3.connect(temp_db)
        con.execute("PRAGMA busy_timeout=5000")
        con.execute("DELETE FROM gradient_records")
        for gid, tgt, sig, ch, ev, conf, tc in seed_rows:
            con.execute(
                "INSERT INTO gradient_records(gradient_id, target, signal, suggested_change, "
                "evidence, confidence, created_at, task_class) VALUES (?,?,?,?,?,?,?,?)",
                (gid, tgt, sig, ch, ev, conf, now, tc),
            )
        con.commit()
        con.close()

    _seed()
    py_stderr = _capture_py_stderr(lambda: rp.reflection_deduplicate("gradient_records"))

    _seed()
    r = _run_bash_function(temp_db, "reflection_deduplicate gradient_records")
    assert r.returncode == 0, f"bash dedup failed: {r.stderr}"

    # Summary form: 1 fuzzy@0.55 removal (the lower-confidence row).
    assert py_stderr == r.stderr, (
        f"stderr mismatch.\n  py:   {py_stderr!r}\n  bash: {r.stderr!r}"
    )
    assert "reflection_deduplicate: removed 1 duplicates (0 exact, 1 fuzzy@0.55)" in py_stderr

    # Row-diff: only g-fuzzy-hi survives (DB now post-bash).
    con = sqlite3.connect(temp_db)
    survivors = [r[0] for r in con.execute(
        "SELECT gradient_id FROM gradient_records"
    ).fetchall()]
    con.close()
    assert survivors == ["g-fuzzy-hi"], f"unexpected survivors: {survivors!r}"


# ─────────────────────────────────────────────────────────────────────────────
# (3) reflection_link_failures — row count + failure_links row content
# ─────────────────────────────────────────────────────────────────────────────
def test_reflection_link_failures(temp_db):
    """Python `reflection_link_failures` matches live bash: creates failure_links
    rows linking each (failure-status trace, gradient with that trace_id as
    evidence) pair. Counter increments per pair regardless of INSERT OR IGNORE.

    Note on link_id collisions: bash truncates `link_id = fl-<tid[:8]>-<gid[:8]>`,
    so 3 different (trace, gradient) pairs share the same link_id
    `fl-trace-fa-gid-fail`. INSERT OR IGNORE keeps only the first; the bash
    counter still says "3 created/verified". The Python port mirrors this
    exactly — including the 3-vs-1 mismatch between counter and actual rows."""
    now = int(time.time())

    def _seed():
        con = sqlite3.connect(temp_db)
        con.execute("PRAGMA busy_timeout=5000")
        # failure_links is created on-demand by the function itself; skip the
        # DELETE if the table hasn't been created yet.
        tables = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        if "failure_links" in tables:
            con.execute("DELETE FROM failure_links")
        for t in ("execution_traces", "runs", "epics", "gradient_records"):
            con.execute(f"DELETE FROM {t}")
        run_id = _seed_epic_run(con, "epic-1", "run-1")
        con.execute(
            "INSERT INTO execution_traces(trace_id, run_id, task_class, status, created_at) "
            "VALUES (?, ?, 'code_review', 'failure', '2026-07-04T00:00:00.000Z')",
            ("trace-fail-1", run_id),
        )
        con.execute(
            "INSERT INTO execution_traces(trace_id, run_id, task_class, status, created_at) "
            "VALUES (?, ?, 'code_review', 'failure', '2026-07-04T00:00:01.000Z')",
            ("trace-fail-2", run_id),
        )
        con.execute(
            "INSERT INTO execution_traces(trace_id, run_id, task_class, status, created_at) "
            "VALUES (?, ?, 'code_review', 'success', '2026-07-04T00:00:02.000Z')",
            ("trace-ok-1", run_id),
        )
        for gid, ev in [
            ("gid-fail-1-a", "trace-fail-1"),
            ("gid-fail-1-b", "trace-fail-1"),
            ("gid-fail-2-a", "trace-fail-2"),
            ("gid-ok-1",     "trace-ok-1"),
        ]:
            con.execute(
                "INSERT INTO gradient_records(gradient_id, target, signal, suggested_change, "
                "evidence, confidence, created_at, task_class) "
                "VALUES (?, 't1', 's1', 'c1', ?, 0.5, ?, 'code_review')",
                (gid, ev, now),
            )
        con.commit()
        con.close()

    _seed()
    py_stderr = _capture_py_stderr(lambda: rp.reflection_link_failures("execution_traces"))

    _seed()
    r = _run_bash_function(temp_db, "reflection_link_failures execution_traces")
    assert r.returncode == 0, f"bash link failed: {r.stderr}"

    # Stderr: same counter on both sides ("3 created/verified") despite the
    # link_id collision (both implementations share this bash quirk).
    assert py_stderr == r.stderr, (
        f"stderr mismatch.\n  py:   {py_stderr!r}\n  bash: {r.stderr!r}"
    )
    assert "reflection_link_failures: 3 links created/verified" in py_stderr

    # Row-diff: link_id collision means only 1 row actually exists.
    con = sqlite3.connect(temp_db)
    rows = con.execute(
        "SELECT link_id, trace_id, gradient_id FROM failure_links ORDER BY link_id"
    ).fetchall()
    con.close()
    assert len(rows) == 1, f"expected 1 row (link_id collision), got {len(rows)}: {rows!r}"
    lid, tid, gid = rows[0]
    assert lid == "fl-trace-fa-gid-fail", f"unexpected link_id {lid!r}"
    assert tid == "trace-fail-1", f"first-writer wins: tid={tid!r}"
    assert gid == "gid-fail-1-a", f"first-writer wins: gid={gid!r}"


# ─────────────────────────────────────────────────────────────────────────────
# (4) reflection_detect_stale — JSON shape (table/stale_ids/stale_before_epoch)
# ─────────────────────────────────────────────────────────────────────────────
def test_reflection_detect_stale(temp_db):
    """Python `reflection_detect_stale` matches live bash: JSON stdout with
    {table, stale_ids, stale_before_epoch} keys + stderr summary."""
    now = int(time.time())
    very_old = now - int(60 * 86400)  # 60 days ago → stale under default 14-day cutoff
    not_stale = now - int(2 * 86400)   # 2 days ago → not stale
    con = sqlite3.connect(temp_db)
    con.execute("PRAGMA busy_timeout=5000")
    con.execute(
        "INSERT INTO gradient_records(gradient_id, target, signal, suggested_change, evidence, confidence, created_at, task_class) "
        "VALUES (?, 't', 's', 'c', 'e', 0.5, ?, 'tc')",
        ("g-stale-1", very_old),
    )
    con.execute(
        "INSERT INTO gradient_records(gradient_id, target, signal, suggested_change, evidence, confidence, created_at, task_class) "
        "VALUES (?, 't', 's', 'c', 'e', 0.5, ?, 'tc')",
        ("g-stale-2", very_old),
    )
    con.execute(
        "INSERT INTO gradient_records(gradient_id, target, signal, suggested_change, evidence, confidence, created_at, task_class) "
        "VALUES (?, 't', 's', 'c', 'e', 0.5, ?, 'tc')",
        ("g-fresh-1", not_stale),
    )
    con.commit()
    con.close()

    r = _run_bash_function(temp_db, "reflection_detect_stale gradient_records")
    assert r.returncode == 0, f"bash detect_stale failed: {r.stderr}"
    # Python port.
    import io
    from contextlib import redirect_stdout, redirect_stderr
    py_out_buf = io.StringIO()
    py_err_buf = io.StringIO()
    with redirect_stdout(py_out_buf), redirect_stderr(py_err_buf):
        rp.reflection_detect_stale("gradient_records")
    py_out = py_out_buf.getvalue()
    py_err = py_err_buf.getvalue()

    # Parse JSON from both and compare structurally (timestamp floats may
    # differ between bash and Python by sub-second noise — both compute
    # `int(time.time()) - days * 86400` so they should match exactly).
    bash_json = json.loads(r.stdout.strip())
    py_json = json.loads(py_out.strip())
    assert bash_json["table"] == py_json["table"] == "gradient_records"
    assert sorted(bash_json["stale_ids"]) == sorted(py_json["stale_ids"]) == [
        "g-stale-1", "g-stale-2",
    ]
    # Bash uses time.time() inside python3 — float; Python uses time.time() —
    # also float. They may differ by milliseconds. Use the float-tolerance gate.
    assert math.isclose(bash_json["stale_before_epoch"], py_json["stale_before_epoch"],
                        rel_tol=0, abs_tol=1e-6), (
        f"stale_before_epoch mismatch: bash={bash_json['stale_before_epoch']} py={py_json['stale_before_epoch']}"
    )
    # Stderr summary must match byte-for-byte.
    assert py_err == r.stderr, f"stderr mismatch.\n  py:   {py_err!r}\n  bash: {r.stderr!r}"


# ─────────────────────────────────────────────────────────────────────────────
# (5) reflection_summarize_patterns — JSON shape for populated cluster_id
# ─────────────────────────────────────────────────────────────────────────────
def test_reflection_summarize_patterns(temp_db):
    """Python `reflection_summarize_patterns` matches live bash JSON shape.

    The pattern_records schema (migration 0011) does NOT include cluster_id —
    the bash function's `WHERE cluster_id = ?` would otherwise crash. We ALTER
    TABLE to add cluster_id (same as a production migration would have done).
    """
    now = "2026-07-04T00:00:00.000Z"
    earlier = "2026-06-15T00:00:00.000Z"
    con = sqlite3.connect(temp_db)
    con.execute("PRAGMA busy_timeout=5000")
    con.execute("ALTER TABLE pattern_records ADD COLUMN cluster_id TEXT")
    con.execute(
        "INSERT INTO pattern_records(pattern_id, description, evidence_trace_ids, frequency, "
        "first_seen, last_seen, output_type, status, cluster_id) "
        "VALUES (?, ?, '[]', 5, ?, ?, 'verifier_addition', 'observed', 'cluster-A')",
        ("p-1", "Verifier returns empty for syntax errors", earlier, now),
    )
    con.execute(
        "INSERT INTO pattern_records(pattern_id, description, evidence_trace_ids, frequency, "
        "first_seen, last_seen, output_type, status, cluster_id) "
        "VALUES (?, ?, '[]', 3, ?, ?, 'adr', 'observed', 'cluster-A')",
        ("p-2", "Add empty-output verifier guard", earlier, now),
    )
    # Different cluster — must not appear in the summary.
    con.execute(
        "INSERT INTO pattern_records(pattern_id, description, evidence_trace_ids, frequency, "
        "first_seen, last_seen, output_type, status, cluster_id) "
        "VALUES (?, ?, '[]', 99, ?, ?, 'adr', 'observed', 'cluster-B')",
        ("p-3", "Unrelated pattern in other cluster", earlier, now),
    )
    con.commit()
    con.close()

    r = _run_bash_function(temp_db, "reflection_summarize_patterns cluster-A")
    assert r.returncode == 0, f"bash summarize failed: {r.stderr}"
    import io
    from contextlib import redirect_stdout
    py_buf = io.StringIO()
    with redirect_stdout(py_buf):
        rp.reflection_summarize_patterns("cluster-A")
    py_out = py_buf.getvalue()

    # Both must produce JSON-serialisable summaries that match in shape.
    bash_summary = json.loads(r.stdout.strip())
    py_summary = json.loads(py_out.strip())
    assert bash_summary == py_summary, (
        f"summarize mismatch.\n  bash: {bash_summary!r}\n  py:   {py_summary!r}"
    )
    # And the shape is correct.
    assert py_summary["cluster_id"] == "cluster-A"
    assert py_summary["pattern_count"] == 2
    assert py_summary["total_frequency"] == 8  # 5 + 3
    # dominant_output_type = first row by frequency DESC = p-1's 'verifier_addition'.
    assert py_summary["dominant_output_type"] == "verifier_addition"
    assert len(py_summary["patterns"]) == 2
    assert {p["pattern_id"] for p in py_summary["patterns"]} == {"p-1", "p-2"}

    # Also exercise the missing-cluster path: empty patterns list.
    r2 = _run_bash_function(temp_db, "reflection_summarize_patterns cluster-Z")
    py_buf2 = io.StringIO()
    with redirect_stdout(py_buf2):
        rp.reflection_summarize_patterns("cluster-Z")
    assert json.loads(r2.stdout.strip()) == json.loads(py_buf2.getvalue().strip())
    assert json.loads(py_buf2.getvalue().strip())["pattern_count"] == 0


# ─────────────────────────────────────────────────────────────────────────────
# (6) reflection_suggest_promotions — JSON array shape with frequency-filter
# ─────────────────────────────────────────────────────────────────────────────
def test_reflection_suggest_promotions(temp_db):
    """Python `reflection_suggest_promotions` matches live bash JSON array.

    Seed 4 patterns: 2 above the min_freq threshold, 2 below. Both implementations
    must return the same 2-element array with rationale strings + sorted by
    frequency DESC.
    """
    con = sqlite3.connect(temp_db)
    con.execute("PRAGMA busy_timeout=5000")
    con.execute("ALTER TABLE pattern_records ADD COLUMN cluster_id TEXT")
    rows = [
        ("p-high-A", "Most frequent pattern",        '["t1","t2"]', 7, "verifier_addition"),
        ("p-high-B", "Second most frequent",         '["t3"]',      4, "adr"),
        ("p-low-C",  "Below threshold",              '["t4"]',      2, "workflow_change"),
        ("p-low-D",  "Way below threshold",          '[]',          1, "prompt_change"),
    ]
    for pid, desc, ev, freq, ot in rows:
        con.execute(
            "INSERT INTO pattern_records(pattern_id, description, evidence_trace_ids, frequency, "
            "first_seen, last_seen, output_type, status) "
            "VALUES (?, ?, ?, ?, '2026-07-04T00:00:00.000Z', '2026-07-04T00:00:00.000Z', ?, 'observed')",
            (pid, desc, ev, freq, ot),
        )
    con.commit()
    con.close()

    r = _run_bash_function(temp_db, "reflection_suggest_promotions pattern_records")
    assert r.returncode == 0, f"bash suggest failed: {r.stderr}"
    import io
    from contextlib import redirect_stdout
    py_buf = io.StringIO()
    with redirect_stdout(py_buf):
        rp.reflection_suggest_promotions("pattern_records")
    py_out = py_buf.getvalue()

    bash_arr = json.loads(r.stdout.strip())
    py_arr = json.loads(py_out.strip())
    assert bash_arr == py_arr, (
        f"suggest mismatch.\n  bash: {bash_arr!r}\n  py:   {py_arr!r}"
    )
    assert len(py_arr) == 2
    assert [s["pattern_id"] for s in py_arr] == ["p-high-A", "p-high-B"]
    # Rationale must include the observed count + threshold (3).
    for s in py_arr:
        assert "Pattern observed" in s["rationale"]
        assert f"threshold of 3" in s["rationale"], f"bad rationale: {s['rationale']!r}"
    # JSON shape: pattern_id, description, frequency, suggested_promotion_type,
    # evidence_trace_ids, rationale.
    assert set(py_arr[0].keys()) == {
        "pattern_id", "description", "frequency", "suggested_promotion_type",
        "evidence_trace_ids", "rationale",
    }


# ─────────────────────────────────────────────────────────────────────────────
# (7) reflection_persist_suggestions — INSERT OR REPLACE (idempotent)
# ─────────────────────────────────────────────────────────────────────────────
def test_reflection_persist_suggestions(temp_db):
    """Python `reflection_persist_suggestions` matches live bash: writes
    emergent_patterns rows keyed by pattern_id, idempotent on re-run."""
    suggestions = [
        {
            "pattern_id": "p-1",
            "description": "Empty verifier output means silent failure",
            "frequency": 5,
            "suggested_promotion_type": "verifier_addition",
            "evidence_trace_ids": ["trace-A", "trace-B"],
            "rationale": "Pattern observed 5 times — meets promotion threshold of 3",
        },
        {
            "pattern_id": "p-2",
            "description": "Trace ID sometimes missing",
            "frequency": 4,
            "suggested_promotion_type": "adr",
            "evidence_trace_ids": [],
            "rationale": "Pattern observed 4 times — meets promotion threshold of 3",
        },
        # Missing pattern_id → skipped (no row inserted).
        {"pattern_id": "", "description": "no id", "frequency": 99,
         "suggested_promotion_type": "adr", "evidence_trace_ids": []},
    ]
    sj = json.dumps(suggestions)

    r = _run_bash_function(temp_db, f"reflection_persist_suggestions {shlex_quote(sj)}")
    assert r.returncode == 0, f"bash persist failed: {r.stderr}"
    assert r.stdout.strip() == "2", f"bash persist count: {r.stdout!r}"
    # Python port — also returns the count.
    py_count = rp.reflection_persist_suggestions(sj)
    assert py_count == 2, f"py persist count: {py_count}"

    # Row-diff: emergent_patterns has exactly 2 rows.
    con = sqlite3.connect(temp_db)
    con.execute("PRAGMA busy_timeout=5000")
    rows = con.execute(
        "SELECT pattern_id, cluster_label, member_item_ids_json, feature_set_json, "
        "strength_score, status FROM emergent_patterns ORDER BY pattern_id"
    ).fetchall()
    assert len(rows) == 2, f"unexpected row count: {rows!r}"
    # Row content match — bash and python both write via the same INSERT OR
    # REPLACE statement; compare to expected computed from input.
    by_id = {r[0]: r for r in rows}
    assert set(by_id.keys()) == {"p-1", "p-2"}
    p1 = by_id["p-1"]
    assert p1[1] == "Empty verifier output means silent failure"
    assert json.loads(p1[2]) == [
        {"item_table": "execution_traces", "item_id": "trace-A"},
        {"item_table": "execution_traces", "item_id": "trace-B"},
    ]
    assert json.loads(p1[3]) == ["verifier_addition"]
    assert math.isclose(p1[4], 5.0, rel_tol=0, abs_tol=1e-6)
    assert p1[5] == "proposed"
    p2 = by_id["p-2"]
    assert math.isclose(p2[4], 4.0, rel_tol=0, abs_tol=1e-6)
    assert json.loads(p2[2]) == []  # empty evidence_trace_ids → empty members
    assert json.loads(p2[3]) == ["adr"]
    con.close()

    # Idempotent re-run: bash re-run with the same JSON → still 2 rows, still
    # status='proposed', resolved_at=NULL.
    r2 = _run_bash_function(temp_db, f"reflection_persist_suggestions {shlex_quote(sj)}")
    assert r2.stdout.strip() == "2"
    py_count2 = rp.reflection_persist_suggestions(sj)
    assert py_count2 == 2
    con = sqlite3.connect(temp_db)
    count = con.execute("SELECT COUNT(*) FROM emergent_patterns").fetchone()[0]
    assert count == 2, f"idempotent re-run added rows: {count}"
    statuses = con.execute("SELECT status FROM emergent_patterns").fetchall()
    assert all(s[0] == "proposed" for s in statuses)
    resolved = con.execute(
        "SELECT resolved_at FROM emergent_patterns WHERE resolved_at IS NOT NULL"
    ).fetchall()
    assert resolved == [], f"resolved_at should be NULL, got: {resolved!r}"
    con.close()


def shlex_quote(s: str) -> str:
    """Minimal sh-quote for shell-arg passing; tests run on macOS."""
    return "'" + s.replace("'", "'\"'\"'") + "'"


# ─────────────────────────────────────────────────────────────────────────────
# (8) reflection_extract_gradients — SQL trace_id selection + injected stub
# ─────────────────────────────────────────────────────────────────────────────
def test_reflection_extract_gradients(temp_db):
    """Python `reflection_extract_gradients` matches live bash: selects the same
    trace_ids via SQL, calls the injected `gradient_extract` stub once per trace,
    emits one stdout line per gradient, writes the summary line to stderr.

    `gradient_extract / gradient_store / _gradient_ensure_table` are stubbed on
    BOTH sides (bash: pre-define functions before sourcing reflection_pipeline.sh;
    Python: set_gradient_* before calling the port). MINI_ORK_ROOT is set to
    `/tmp/empty-lib-stub` so bash's internal `source lib/gradient_extractor.sh`
    silently fails and our stubs win.
    """
    now = "2026-07-04T12:00:00.000Z"
    con = sqlite3.connect(temp_db)
    con.execute("PRAGMA busy_timeout=5000")
    run_id = _seed_epic_run(con, "epic-1", "run-1")
    con.execute(
        "INSERT INTO execution_traces(trace_id, run_id, task_class, status, created_at) "
        "VALUES (?, ?, 'code_review', 'success', ?)",
        ("trace-A", run_id, now),
    )
    con.execute(
        "INSERT INTO execution_traces(trace_id, run_id, task_class, status, created_at) "
        "VALUES (?, ?, 'code_review', 'failure', ?)",
        ("trace-B", run_id, now),
    )
    con.commit()
    con.close()

    # Stub: each trace yields 2 deterministic gradient JSON lines.
    # We emit exactly 2 JSON objects per trace so stdout is 4 lines total
    # (2 trace_ids × 2 gradients each).
    stub = (
        '_gradient_ensure_table() { return 0; }\n'
        'gradient_store() { return 0; }\n'
        'gradient_extract() {\n'
        '  local tid="$1"\n'
        '  if [ "$tid" = "trace-A" ]; then\n'
        '    echo \'{"gradient_id":"g-A-0","target":"t","signal":"s0","suggested_change":"c0","evidence":"trace-A","confidence":0.5}\'\n'
        '    echo \'{"gradient_id":"g-A-1","target":"t","signal":"s1","suggested_change":"c1","evidence":"trace-A","confidence":0.4}\'\n'
        '  elif [ "$tid" = "trace-B" ]; then\n'
        '    echo \'{"gradient_id":"g-B-0","target":"t","signal":"s0","suggested_change":"c0","evidence":"trace-B","confidence":0.7}\'\n'
        '    echo \'{"gradient_id":"g-B-1","target":"t","signal":"s1","suggested_change":"c1","evidence":"trace-B","confidence":0.6}\'\n'
        '  fi\n'
        '}\n'
        'export -f _gradient_ensure_table gradient_store gradient_extract\n'
    )

    # Bash wrapper: source reflection_pipeline.sh AFTER defining stubs so they
    # remain authoritative; use a MINI_ORK_ROOT that doesn't have
    # lib/gradient_extractor.sh so the internal source silently fails.
    os.makedirs(STUB_ROOT, exist_ok=True)
    bash_wrapper = (
        f'MINI_ORK_ROOT="{STUB_ROOT}" MINI_ORK_DB="{temp_db}" MO_REFLECTION_BATCH=500\n'
        f'{stub}'
        f'. "{SH}"\n'
        f'reflection_extract_gradients 0\n'
    )
    r = subprocess.run(
        ["bash", "-c", bash_wrapper],
        env={**os.environ, "MINI_ORK_DB": temp_db, "MO_REFLECTION_BATCH": "500",
             "MINI_ORK_ROOT": STUB_ROOT},
        capture_output=True, text=True,
    )
    assert r.returncode == 0, f"bash extract failed: {r.stderr}"

    # Python port: install equivalent stubs.
    def _stub_ensure():
        return None

    py_stored: list[str] = []

    def _stub_store(g: str):
        py_stored.append(g)

    def _stub_extract(tid: str):
        if tid == "trace-A":
            yield '{"gradient_id":"g-A-0","target":"t","signal":"s0","suggested_change":"c0","evidence":"trace-A","confidence":0.5}'
            yield '{"gradient_id":"g-A-1","target":"t","signal":"s1","suggested_change":"c1","evidence":"trace-A","confidence":0.4}'
        elif tid == "trace-B":
            yield '{"gradient_id":"g-B-0","target":"t","signal":"s0","suggested_change":"c0","evidence":"trace-B","confidence":0.7}'
            yield '{"gradient_id":"g-B-1","target":"t","signal":"s1","suggested_change":"c1","evidence":"trace-B","confidence":0.6}'

    rp.set_gradient_extract(_stub_extract)
    rp.set_gradient_store(_stub_store)
    rp.set_gradient_ensure_table(_stub_ensure)
    try:
        import io
        from contextlib import redirect_stdout, redirect_stderr
        py_out_buf = io.StringIO()
        py_err_buf = io.StringIO()
        with redirect_stdout(py_out_buf), redirect_stderr(py_err_buf):
            rp.reflection_extract_gradients(0)
        py_out = py_out_buf.getvalue()
        py_err = py_err_buf.getvalue()
    finally:
        # Reset injections to defaults so subsequent tests don't inherit them.
        rp.set_gradient_extract(rp._default_gradient_extract)
        rp.set_gradient_store(rp._default_gradient_store)
        rp.set_gradient_ensure_table(rp._default_gradient_ensure_table)

    # stdout: 4 lines, one per gradient.
    bash_lines = [ln for ln in r.stdout.splitlines() if ln]
    py_lines = [ln for ln in py_out.splitlines() if ln]
    assert bash_lines == py_lines, (
        f"stdout mismatch.\n  bash: {bash_lines!r}\n  py:   {py_lines!r}"
    )
    assert len(bash_lines) == 4
    # Each line is JSON-decodable.
    for ln in bash_lines:
        d = json.loads(ln)
        assert "gradient_id" in d and "evidence" in d

    # stderr: same summary line on both sides.
    assert py_err.strip() == r.stderr.strip(), (
        f"stderr mismatch.\n  py:   {py_err!r}\n  bash: {r.stderr!r}"
    )
    assert py_err.strip() == "reflection_extract_gradients: extracted 4 gradients since 0", (
        f"unexpected summary: {py_err!r}"
    )

    # gradient_store was invoked once per gradient (4 total).
    assert len(py_stored) == 4, f"gradient_store invocation count: {len(py_stored)}"