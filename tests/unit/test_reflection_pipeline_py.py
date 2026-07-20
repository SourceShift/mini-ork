"""Standalone contracts for the native reflection pipeline.

8 cases (matching the bash public API):
  (1) reflection_deduplicate        — pass-1 exact merge against seeded gradient_records
  (2) reflection_deduplicate        — pass-2 fuzzy merge with calibrated MO_DEDUP_FUZZY
  (3) reflection_link_failures      — row count + failure_links row content via DB row-diff
  (4) reflection_detect_stale       — JSON shape (table/stale_ids/stale_before_epoch)
  (5) reflection_summarize_patterns — JSON shape for populated cluster_id (ALTER TABLE adds col)
  (6) reflection_suggest_promotions — JSON array shape with frequency-filter + rationale
  (7) reflection_persist_suggestions— INSERT OR REPLACE into emergent_patterns (idempotent)
  (8) reflection_extract_gradients  — SQL trace_id selection + injected gradient_extract stub

All cases use a temp DB initialized by ``db/init.sh`` and assert durable output
and database contracts without retaining a second runtime implementation.
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
from mini_ork.learning import reflection_pipeline as rp
from mini_ork.stores import pattern_store

INIT_SH = REPO / "db" / "init.sh"


# ── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture
def temp_db(tmp_path_factory, monkeypatch):
    """Spin up a real mini-ork SQLite DB via db/init.sh AND point the in-process
    Python port at it via `os.environ["MINI_ORK_DB"]`."""
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


# ─────────────────────────────────────────────────────────────────────────────
# (1) reflection_deduplicate — pass-1 exact (target, signal) merge
# ─────────────────────────────────────────────────────────────────────────────
def test_reflection_deduplicate_pass1_exact(temp_db):
    """Exact duplicate pairs keep the highest-confidence row.
    gradient_records: 5 rows, two pairs of identical (target, signal); the lower-
    confidence row in each pair is deleted."""
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

    _seed()
    py_stderr = _capture_py_stderr(lambda: rp.reflection_deduplicate("gradient_records"))
    assert py_stderr == (
        "reflection_deduplicate: removed 2 duplicates "
        "(2 exact, 0 fuzzy@0.55)\n"
    )

    con = sqlite3.connect(temp_db)
    survivors = sorted(r[0] for r in con.execute(
        "SELECT gradient_id FROM gradient_records ORDER BY gradient_id"
    ).fetchall())
    con.close()
    assert survivors == ["g-a-high", "g-b-high", "g-c-unique"], (
        f"unexpected survivors: {survivors!r}"
    )

    py_idem = _capture_py_stderr(lambda: rp.reflection_deduplicate("gradient_records"))
    assert py_idem == "reflection_deduplicate: no duplicates found\n"


def _capture_py_stderr(fn) -> str:
    """Capture stderr from a Python callable that uses print(..., file=sys.stderr)."""
    import io
    from contextlib import redirect_stderr
    buf = io.StringIO()
    with redirect_stderr(buf):
        fn()
    return buf.getvalue()


def test_extract_excludes_framework_traces_and_honors_watermark(temp_db):
    """Internal traces never dispatch; previously-linked traces never repeat."""
    con = sqlite3.connect(temp_db)
    con.execute(
        "INSERT OR IGNORE INTO epics(id, title, status) "
        "VALUES ('e-native-extract', 't', 'in progress')"
    )
    con.execute(
        "INSERT INTO runs(epic_id, run_dir, branch, baseline_sha, agent) "
        "VALUES ('e-native-extract', 'run-native-extract', 'main', 'sha', 'glm')"
    )
    run_id = con.execute("SELECT last_insert_rowid()").fetchone()[0]
    for trace_id, task_class, created_at in (
        ("trace-native", "code_review", "2026-07-04T00:00:00.000Z"),
        ("trace-seen", "code_review", "2026-07-04T00:00:01.000Z"),
        ("trace-internal", "__reflect__", "2026-07-04T00:00:02.000Z"),
    ):
        con.execute(
            "INSERT INTO execution_traces(trace_id, run_id, task_class, status, created_at) "
            "VALUES (?, ?, ?, 'success', ?)",
            (trace_id, run_id, task_class, created_at),
        )
    con.execute(
        "INSERT INTO gradient_records(gradient_id, target, signal, suggested_change, "
        "evidence, confidence, created_at, task_class) "
        "VALUES ('gr-seen', 'workflow.node.verify', 's', 'c', 'trace-seen', "
        "0.8, 1700000000, 'code_review')"
    )
    con.commit()
    con.close()

    dispatched: list[str] = []

    def _stub_extract(trace_id: str):
        dispatched.append(trace_id)
        yield json.dumps({
            "gradient_id": f"gr-{trace_id}",
            "target": "workflow.node.verify",
            "signal": "s",
            "suggested_change": "c",
            "evidence": trace_id,
            "confidence": 0.5,
        })

    rp.set_gradient_extract(_stub_extract)
    try:
        stderr = _capture_py_stderr(lambda: rp.reflection_extract_gradients(0))
    finally:
        rp.set_gradient_extract(rp._default_gradient_extract)

    assert dispatched == ["trace-native"]
    assert "skipped 1 already-extracted trace(s) (watermark)" in stderr
    assert "extracted 1 gradients since 0" in stderr


def test_per_node_credit_apply_restore_and_off(temp_db, monkeypatch):
    con = sqlite3.connect(temp_db)
    con.execute(
        "INSERT OR IGNORE INTO epics(id, title, status) "
        "VALUES ('e-credit', 't', 'in progress')"
    )
    con.execute(
        "INSERT INTO runs(epic_id, run_dir, branch, baseline_sha, agent) "
        "VALUES ('e-credit', 'run-credit', 'main', 'sha', 'glm')"
    )
    run_id = con.execute("SELECT last_insert_rowid()").fetchone()[0]
    con.execute(
        "INSERT INTO execution_traces(trace_id, run_id, task_class, status, "
        "reward_g, process_reward, created_at) "
        "VALUES ('trace-credit-high', ?, 'code_review', 'success', 0.8, 1.0, "
        "'2026-07-04T00:00:00.000Z')",
        (run_id,),
    )
    con.execute(
        "INSERT INTO execution_traces(trace_id, run_id, task_class, status, "
        "reward_g, process_reward, created_at) "
        "VALUES ('trace-credit-low', ?, 'code_review', 'success', 0.8, 0.3, "
        "'2026-07-04T00:00:01.000Z')",
        (run_id,),
    )
    con.commit()
    con.close()

    monkeypatch.setenv("MO_ROUTER_PER_NODE_CREDIT", "1")
    monkeypatch.setenv("MO_ROUTER_PER_NODE_CREDIT_GAMMA", "1.0")
    assert rp.reflection_apply_per_node_credit(temp_db) == 2
    con = sqlite3.connect(temp_db)
    adjusted = dict(con.execute(
        "SELECT trace_id, reward_g FROM execution_traces "
        "WHERE trace_id LIKE 'trace-credit-%'"
    ).fetchall())
    con.close()
    assert adjusted == {"trace-credit-high": 1.0, "trace-credit-low": 0.64}

    assert rp.reflection_restore_per_node_credit(temp_db) == 2
    con = sqlite3.connect(temp_db)
    restored = dict(con.execute(
        "SELECT trace_id, reward_g FROM execution_traces "
        "WHERE trace_id LIKE 'trace-credit-%'"
    ).fetchall())
    backup_exists = con.execute(
        "SELECT COUNT(*) FROM sqlite_master "
        "WHERE type='table' AND name='per_node_credit_backup'"
    ).fetchone()[0]
    con.close()
    assert restored == {"trace-credit-high": 0.8, "trace-credit-low": 0.8}
    assert backup_exists == 0

    monkeypatch.setenv("MO_ROUTER_PER_NODE_CREDIT", "0")
    assert rp.reflection_apply_per_node_credit(temp_db) == 0


# ─────────────────────────────────────────────────────────────────────────────
# (2) reflection_deduplicate — pass-2 fuzzy merge (difflib signal ratio)
# ─────────────────────────────────────────────────────────────────────────────
def test_reflection_deduplicate_pass2_fuzzy(temp_db):
    """Fuzzy duplicates keep the highest-confidence row.

    Seed two rows whose `signal` strings are rephrasings of the same lesson
    (similarity > 0.55). Different signal so pass-1 doesn't catch them; same
    (task_class, target) so pass-2 groups them.
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

    assert py_stderr == (
        "reflection_deduplicate: removed 1 duplicates "
        "(0 exact, 1 fuzzy@0.55)\n"
    )

    con = sqlite3.connect(temp_db)
    survivors = [r[0] for r in con.execute(
        "SELECT gradient_id FROM gradient_records"
    ).fetchall()]
    con.close()
    assert survivors == ["g-fuzzy-hi"], f"unexpected survivors: {survivors!r}"


def test_semantic_noise_collapses_but_distinct_intents_survive(temp_db):
    """Trace-local timing/cost noise collapses without merging distinct advice."""
    now = int(time.time())
    rows = [
        (
            "gr-trace-1", "agent.reviewer.prompt",
            "verifier spent 2.7min and cost $1.62 on the empty-object fix",
            "add a guard against empty verifier_output before re-extracting",
            "tr-aaaa1111", 0.55,
        ),
        (
            "gr-trace-2", "agent.reviewer.prompt",
            "verifier spent 8.9min and cost $5.10 on the empty-object fix",
            "add a guard against empty verifier_output before re-extracting",
            "tr-bbbb2222", 0.62,
        ),
        (
            "gr-trace-3", "agent.reviewer.prompt",
            "verifier spent 633s and cost $3.53 on the empty-object fix",
            "add a guard against empty verifier_output before re-extracting",
            "tr-cccc3333", 0.50,
        ),
        (
            "gr-intent-A", "agent.planner.prompt",
            "planner skipped the verifier link step in the original trace",
            "inject the verifier_output schema before the planner prompt",
            "tr-1111aaaa", 0.7,
        ),
        (
            "gr-intent-B", "agent.planner.prompt",
            "planner emitted the wrong aggregation for the panel review",
            "switch the lens-count aggregator from sum to majority_vote",
            "tr-2222bbbb", 0.8,
        ),
    ]
    con = sqlite3.connect(temp_db)
    con.executemany(
        "INSERT INTO gradient_records(gradient_id, target, signal, suggested_change, "
        "evidence, confidence, created_at, task_class) VALUES (?,?,?,?,?,?,?,'framework_edit')",
        [(*row, now) for row in rows],
    )
    con.commit()
    con.close()

    rp.reflection_deduplicate("gradient_records")
    con = sqlite3.connect(temp_db)
    reviewer = con.execute(
        "SELECT gradient_id FROM gradient_records "
        "WHERE target='agent.reviewer.prompt'"
    ).fetchall()
    planner = {
        row[0] for row in con.execute(
            "SELECT gradient_id FROM gradient_records "
            "WHERE target='agent.planner.prompt'"
        )
    }
    con.close()
    assert reviewer == [("gr-trace-2",)]
    assert planner == {"gr-intent-A", "gr-intent-B"}


# ─────────────────────────────────────────────────────────────────────────────
# (3) reflection_link_failures — row count + failure_links row content
# ─────────────────────────────────────────────────────────────────────────────
def test_reflection_link_failures(temp_db):
    """Failure gradients create stable trace links.
    rows linking each (failure-status trace, gradient with that trace_id as
    evidence) pair. Counter increments per pair regardless of INSERT OR IGNORE.

    Note on link_id collisions: IDs truncate to `fl-<tid[:8]>-<gid[:8]>`,
    so 3 different (trace, gradient) pairs share the same link_id
    `fl-trace-fa-gid-fail`. INSERT OR IGNORE keeps only the first; the bash
    counter still says "3 created/verified"."""
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

    assert py_stderr == "reflection_link_failures: 3 links created/verified\n"

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
    """Stale detection emits the documented JSON and stderr summary."""
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

    import io
    from contextlib import redirect_stdout, redirect_stderr
    py_out_buf = io.StringIO()
    py_err_buf = io.StringIO()
    with redirect_stdout(py_out_buf), redirect_stderr(py_err_buf):
        rp.reflection_detect_stale("gradient_records")
    py_out = py_out_buf.getvalue()
    py_err = py_err_buf.getvalue()

    py_json = json.loads(py_out.strip())
    assert py_json["table"] == "gradient_records"
    assert sorted(py_json["stale_ids"]) == [
        "g-stale-1", "g-stale-2",
    ]
    expected_cutoff = int(time.time()) - 14 * 86400
    assert math.isclose(
        py_json["stale_before_epoch"], expected_cutoff, rel_tol=0, abs_tol=2
    )
    assert py_err == "reflection_detect_stale: 2 stale entries in gradient_records\n"


# ─────────────────────────────────────────────────────────────────────────────
# (5) reflection_summarize_patterns — JSON shape for populated cluster_id
# ─────────────────────────────────────────────────────────────────────────────
def test_reflection_summarize_patterns(temp_db):
    """Pattern summaries retain ordering, totals, and empty-cluster shape.

    The pattern_records schema (migration 0011) does NOT include cluster_id —
    the query's `WHERE cluster_id = ?` would otherwise fail. We ALTER
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

    import io
    from contextlib import redirect_stdout
    py_buf = io.StringIO()
    with redirect_stdout(py_buf):
        rp.reflection_summarize_patterns("cluster-A")
    py_out = py_buf.getvalue()

    py_summary = json.loads(py_out.strip())
    assert py_summary["cluster_id"] == "cluster-A"
    assert py_summary["pattern_count"] == 2
    assert py_summary["total_frequency"] == 8  # 5 + 3
    # dominant_output_type = first row by frequency DESC = p-1's 'verifier_addition'.
    assert py_summary["dominant_output_type"] == "verifier_addition"
    assert len(py_summary["patterns"]) == 2
    assert {p["pattern_id"] for p in py_summary["patterns"]} == {"p-1", "p-2"}

    # Also exercise the missing-cluster path: empty patterns list.
    py_buf2 = io.StringIO()
    with redirect_stdout(py_buf2):
        rp.reflection_summarize_patterns("cluster-Z")
    assert json.loads(py_buf2.getvalue().strip())["pattern_count"] == 0


# ─────────────────────────────────────────────────────────────────────────────
# (6) reflection_suggest_promotions — JSON array shape with frequency-filter
# ─────────────────────────────────────────────────────────────────────────────
def test_reflection_suggest_promotions(temp_db):
    """Promotion suggestions honor frequency, ordering, and JSON shape.

    Seed 4 patterns: 2 above the min_freq threshold and 2 below.
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

    import io
    from contextlib import redirect_stdout
    py_buf = io.StringIO()
    with redirect_stdout(py_buf):
        rp.reflection_suggest_promotions("pattern_records")
    py_out = py_buf.getvalue()

    py_arr = json.loads(py_out.strip())
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
    """Suggestions persist by pattern id and remain idempotent."""
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

    # Idempotent re-run keeps two proposed rows with no resolution timestamp.
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


def test_learning_loop_writeback_from_trace_cluster(temp_db):
    """Trace mining produces an evidence-backed, idempotent promotion row."""
    from datetime import datetime, timedelta, timezone

    con = sqlite3.connect(temp_db)
    run_id = _seed_epic_run(con, "epic-writeback", "run-writeback")
    recent = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z"
    )
    con.executemany(
        "INSERT INTO execution_traces(trace_id, run_id, task_class, status, created_at) "
        "VALUES (?, ?, 'code_fix', ?, ?)",
        [
            ("tr-fix-fail-1", run_id, "failure", recent),
            ("tr-fix-fail-2", run_id, "failure", recent),
            ("tr-fix-fail-3", run_id, "failure", recent),
            ("tr-fix-fail-4", run_id, "failure", recent),
            ("tr-fix-ok-1", run_id, "success", recent),
            ("tr-fix-ok-2", run_id, "success", recent),
        ],
    )
    con.commit()
    con.close()

    assert pattern_store.mine_from_traces(
        db_path=temp_db, window="7d", min_cluster=3
    ) == 1
    con = sqlite3.connect(temp_db)
    pattern = con.execute(
        "SELECT pattern_id, description, frequency, output_type, evidence_trace_ids "
        "FROM pattern_records WHERE frequency >= 3"
    ).fetchone()
    con.close()
    assert pattern is not None
    evidence = json.loads(pattern[4])
    assert len(evidence) == 4
    suggestions = json.dumps([{
        "pattern_id": pattern[0],
        "description": pattern[1],
        "frequency": pattern[2],
        "suggested_promotion_type": pattern[3],
        "evidence_trace_ids": evidence,
        "rationale": f"observed {pattern[2]} times",
    }])

    assert rp.reflection_persist_suggestions(suggestions) == 1
    assert rp.reflection_persist_suggestions(suggestions) == 1
    con = sqlite3.connect(temp_db)
    rows = con.execute(
        "SELECT member_item_ids_json, status FROM emergent_patterns "
        "WHERE pattern_id=?",
        (pattern[0],),
    ).fetchall()
    con.close()
    assert len(rows) == 1
    assert rows[0][1] == "proposed"
    assert len(json.loads(rows[0][0])) == 4


# ─────────────────────────────────────────────────────────────────────────────
# (9) reflection_verify_patterns — judge-gate: proposed → approved on floor
# ─────────────────────────────────────────────────────────────────────────────
def _seed_emergent(temp_db, rows):
    """rows: list of (pattern_id, members_list, strength_score, status)."""
    now = int(time.time())
    con = sqlite3.connect(temp_db)
    con.execute("PRAGMA busy_timeout=5000")
    con.execute("DELETE FROM emergent_patterns")
    for pid, members, strength, status in rows:
        con.execute(
            "INSERT INTO emergent_patterns (pattern_id, cluster_label, "
            "member_item_ids_json, feature_set_json, strength_score, status, detected_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (pid, f"label-{pid}", json.dumps(members), json.dumps(["verifier_addition"]),
             strength, status, now),
        )
    con.commit()
    con.close()


def test_reflection_verify_patterns_gate(temp_db):
    """Judge-gate promotes only evidence-backed 'proposed' rows to 'approved'.

    Floor (defaults): strength_score >= 3 AND member-evidence count >= 1.
    Rows below either bound stay 'proposed'."""
    seed = [
        ("p-strong",   [{"item_table": "execution_traces", "item_id": "t1"}], 5.0, "proposed"),  # pass
        ("p-weak-str", [{"item_table": "execution_traces", "item_id": "t2"}], 2.0, "proposed"),  # fail: strength
        ("p-no-ev",    [],                                                     9.0, "proposed"),  # fail: evidence
        ("p-already",  [{"item_table": "execution_traces", "item_id": "t3"}], 8.0, "approved"),  # not proposed
    ]

    _seed_emergent(temp_db, seed)
    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    with redirect_stdout(buf):
        n = rp.reflection_verify_patterns()
    assert n == 1
    assert buf.getvalue().strip() == "1"

    # Row-diff after Python ran: only p-strong flipped to approved; p-already
    # stays approved; the two failing rows stay proposed.
    con = sqlite3.connect(temp_db)
    statuses = dict(con.execute(
        "SELECT pattern_id, status FROM emergent_patterns"
    ).fetchall())
    con.close()
    assert statuses["p-strong"] == "approved"
    assert statuses["p-weak-str"] == "proposed"
    assert statuses["p-no-ev"] == "proposed"
    assert statuses["p-already"] == "approved"


def test_reflection_verify_patterns_optout(temp_db, monkeypatch):
    """MO_EMERGENT_VERIFY=0 is a hard opt-out: nothing is promoted, count 0."""
    seed = [("p-strong", [{"item_table": "execution_traces", "item_id": "t1"}], 5.0, "proposed")]
    _seed_emergent(temp_db, seed)
    monkeypatch.setenv("MO_EMERGENT_VERIFY", "0")
    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    with redirect_stdout(buf):
        n = rp.reflection_verify_patterns()
    assert n == 0
    con = sqlite3.connect(temp_db)
    st = con.execute("SELECT status FROM emergent_patterns WHERE pattern_id='p-strong'").fetchone()[0]
    con.close()
    assert st == "proposed"  # untouched


def test_reflection_verify_patterns_cold(temp_db):
    """Cold-safe: no emergent patterns returns zero without crashing."""
    con = sqlite3.connect(temp_db)
    con.execute("PRAGMA busy_timeout=5000")
    con.execute("DELETE FROM emergent_patterns")
    con.commit()
    con.close()
    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    with redirect_stdout(buf):
        assert rp.reflection_verify_patterns() == 0
    assert buf.getvalue().strip() == "0"


# ─────────────────────────────────────────────────────────────────────────────
# (8) reflection_extract_gradients — SQL trace_id selection + injected stub
# ─────────────────────────────────────────────────────────────────────────────
def test_reflection_extract_gradients(temp_db):
    """Extraction selects bounded trace ids, calls the injected extractor,
    emits one stdout line per gradient, writes the summary line to stderr.

    Extract/store/schema hooks are stubbed to keep this contract deterministic.
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

    py_lines = [ln for ln in py_out.splitlines() if ln]
    assert len(py_lines) == 4
    # Each line is JSON-decodable.
    for ln in py_lines:
        d = json.loads(ln)
        assert "gradient_id" in d and "evidence" in d

    assert py_err.strip() == "reflection_extract_gradients: extracted 4 gradients since 0", (
        f"unexpected summary: {py_err!r}"
    )

    # gradient_store was invoked once per gradient (4 total).
    assert len(py_stored) == 4, f"gradient_store invocation count: {len(py_stored)}"
