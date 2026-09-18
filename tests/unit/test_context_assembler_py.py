"""Standalone contracts for the native context assembler."""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork import context_assembler as ca  # noqa: E402
from mini_ork import trace_store  # noqa: E402

@pytest.fixture
def db(tmp_path_factory):
    home = tmp_path_factory.mktemp("home")
    dbp = str(home / "state.db")
    subprocess.run(["bash", str(REPO / "db" / "init.sh")],
                   env={**os.environ, "MINI_ORK_HOME": str(home), "MINI_ORK_DB": dbp},
                   capture_output=True, text=True, check=True)
    con = sqlite3.connect(dbp)
    now = int(time.time())
    grads = [
        ("g1", "auth.middleware", "tests skipped silently", "run pytest -x", "e", 0.9, now, "code-fix"),
        ("g2", "workflow.gate", "framework-internal lesson", "fix gate", "e", 0.8, now, "code-fix"),
        ("g3", "db.migration", "cross-class lesson", "always backup", "e", 0.95, now, "__cross_class__"),
        ("g4", "lowconf.target", "below floor", "ignore", "e", 0.3, now, "code-fix"),
    ]
    con.executemany(
        "INSERT INTO gradient_records (gradient_id, target, signal, suggested_change,"
        " evidence, confidence, created_at, task_class) VALUES (?,?,?,?,?,?,?,?)", grads)
    con.commit()
    con.close()
    for i, (run, status) in enumerate([("r1", "success"), ("r1", "success"),
                                       ("r2", "failure"), ("r2", "success")]):
        trace_store.trace_write(
            {"trace_id": f"t{i}", "run_id": run, "task_class": "code-fix",
             "status": status, "cost_usd": 0.5, "duration_ms": 2000,
             "agent_version_id": "codex"}, db=dbp)
    return dbp


def test_failure_modes_md(db, monkeypatch):
    monkeypatch.setenv("MINI_ORK_DB", db)
    monkeypatch.delenv("MO_TARGET_CWD", raising=False)
    py = ca.failure_modes_md("code-fix", 5, db=db)
    assert "auth.middleware" in py and "lowconf.target" not in py


def test_failure_modes_project_scope_filter(db, monkeypatch, tmp_path):
    # Foreign MO_TARGET_CWD strips framework-internal targets (workflow.*).
    monkeypatch.setenv("MINI_ORK_DB", db)
    monkeypatch.setenv("MINI_ORK_ROOT", str(REPO))
    monkeypatch.setenv("MO_TARGET_CWD", str(tmp_path))
    py = ca.failure_modes_md("code-fix", 5, db=db)
    assert "workflow.gate" not in py and "auth.middleware" in py


def test_prior_runs_md(db, monkeypatch):
    monkeypatch.setenv("MINI_ORK_DB", db)
    monkeypatch.delenv("MINI_ORK_RUN_ID", raising=False)
    py = ca.prior_runs_md("code-fix", 5, db=db)
    assert "r1: success" in py and "1/2 nodes failed" in py


def _seed_failure_links(db):
    """DDL mirrors reflection_pipeline._link_failures_insert — the table is
    created on demand there, never by db/init.sh."""
    con = sqlite3.connect(db)
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS failure_links (
            link_id      TEXT PRIMARY KEY,
            trace_id     TEXT NOT NULL,
            gradient_id  TEXT,
            task_class   TEXT,
            linked_at    INTEGER NOT NULL
        )
        """
    )
    now = int(time.time())
    con.executemany(
        "INSERT OR IGNORE INTO failure_links "
        "(link_id, trace_id, gradient_id, task_class, linked_at) VALUES (?,?,?,?,?)",
        [("fl-1", "t2", "g1", "code-fix", now),
         ("fl-2", "t2", "g1", "code-fix", now),
         ("fl-3", "t2", "g2", "code-fix", now)])
    con.commit()
    con.close()


def test_graph_context_md_join_and_link_count_ordering(db, monkeypatch):
    monkeypatch.setenv("MINI_ORK_DB", db)
    monkeypatch.delenv("MO_TARGET_CWD", raising=False)
    monkeypatch.delenv("MO_GRAPH_CONTEXT", raising=False)
    _seed_failure_links(db)

    py = ca.graph_context_md("code-fix", 5, db=db)

    assert "Learned graph context" in py and "/learned graph context" in py
    assert "auth.middleware" in py
    assert "workflow.gate" in py
    # g1 has two links, g2 one → link_count DESC puts auth.middleware first.
    assert py.index("auth.middleware") < py.index("workflow.gate")
    # g3/g4 have no failure_links row: the INNER JOIN must drop them.
    assert "db.migration" not in py and "lowconf.target" not in py


def test_graph_context_md_cold_safe_zero_cases(db, monkeypatch):
    monkeypatch.setenv("MINI_ORK_DB", db)
    monkeypatch.delenv("MO_TARGET_CWD", raising=False)
    monkeypatch.delenv("MO_GRAPH_CONTEXT", raising=False)

    # Stock fixture has no failure_links table: missing-table path, not a raise.
    assert ca.graph_context_md("code-fix", 5, db=db) == ""
    assert ca.graph_context_md("code-fix", 5, db="/nonexistent/state.db") == ""

    _seed_failure_links(db)
    monkeypatch.setenv("MO_GRAPH_CONTEXT", "0")
    assert ca.graph_context_md("code-fix", 5, db=db) == ""
    monkeypatch.delenv("MO_GRAPH_CONTEXT", raising=False)


def test_context_assemble_shape(db, tmp_path, monkeypatch):
    brief = tmp_path / "brief.json"
    brief.write_text(json.dumps({"task_class": "code-fix", "goal": "fix auth tests"}))
    monkeypatch.setenv("MINI_ORK_DB", db)
    py_pack = ca.context_assemble(str(brief), "implementer", db=db)
    assert py_pack["workflow_node"] == "implementer"
    assert py_pack["task_brief"]["content"]["task_class"] == "code-fix"
    assert py_pack["known_failure_modes"]
    assert py_pack["prior_similar_runs"]
    assert "graph_context" in py_pack


def _seed_bug_lessons(db):
    con = sqlite3.connect(db)
    now = int(time.time())
    rows = [
        (f"similar-{index}", "auth middleware failure", f"fix-{index}")
        for index in range(1, 5)
    ]
    con.executemany(
        """INSERT INTO bug_reports (
               fingerprint, agent_role, title, description, suggested_fix,
               first_seen_at, last_seen_at, updated_at
           ) VALUES (?, 'reviewer', ?, '', ?, ?, ?, ?)""",
        [(fingerprint, title, fix, now, now, now) for fingerprint, title, fix in rows],
    )
    con.execute(
        """INSERT INTO bug_reports (
               fingerprint, agent_role, title, description, suggested_fix,
               first_seen_at, last_seen_at, updated_at
           ) VALUES ('unrelated', 'reviewer', 'database backup rotation', '',
                     'not relevant', ?, ?, ?)""",
        (now, now, now),
    )
    con.commit()
    con.close()


def test_similar_lessons_shape_threshold_top_three_and_stable_ties(
        db, tmp_path, monkeypatch):
    _seed_bug_lessons(db)
    brief = tmp_path / "brief.json"
    brief.write_text(json.dumps({
        "task_class": "code-fix",
        "goal": "auth middleware failure",
    }))
    monkeypatch.setenv("MINI_ORK_DB", db)

    pack = ca.context_assemble(str(brief), "implementer", db=db)
    bugs = [lesson for lesson in pack["similar_lessons"] if lesson["kind"] == "bug"]

    assert len(bugs) == 3
    assert [lesson["suggested_fix"] for lesson in bugs] == ["fix-1", "fix-2", "fix-3"]
    assert all(set(lesson) == {"cite", "kind", "score", "title", "suggested_fix"}
               for lesson in bugs)
    assert all(lesson["score"] >= 0.15 for lesson in bugs)
    assert all(lesson["title"] == "auth middleware failure" for lesson in bugs)
    assert all("unrelated" not in lesson["title"] for lesson in bugs)


def test_similar_lessons_skip_missing_source_table(db, tmp_path, monkeypatch):
    con = sqlite3.connect(db)
    con.execute("DROP TABLE bug_reports")
    con.commit()
    con.close()
    brief = tmp_path / "brief.json"
    brief.write_text(json.dumps({
        "task_class": "code-fix",
        "goal": "tests skipped silently",
    }))
    monkeypatch.setenv("MINI_ORK_DB", db)

    pack = ca.context_assemble(str(brief), "implementer", db=db)

    assert isinstance(pack["similar_lessons"], list)
    assert any(lesson["kind"] == "gradient" for lesson in pack["similar_lessons"])


def _seed_emergent(db, rows):
    """rows: (pattern_id, cluster_label, features_list, strength, status)."""
    con = sqlite3.connect(db)
    now = int(time.time())
    for pid, label, feats, strength, status in rows:
        con.execute(
            "INSERT INTO emergent_patterns (pattern_id, cluster_label, "
            "member_item_ids_json, feature_set_json, strength_score, "
            "suggested_meta_adr, status, detected_at) VALUES (?,?,?,?,?,?,?,?)",
            (pid, label, "[]", json.dumps(feats), strength,
             "meta-adr text", status, now))
    con.commit()
    con.close()


def test_verified_emergent_md_readback(db, monkeypatch):
    """failure_modes_md surfaces judge-gate 'approved' emergent patterns and
    hides 'proposed' ones."""
    monkeypatch.setenv("MINI_ORK_DB", db)
    monkeypatch.delenv("MO_TARGET_CWD", raising=False)
    _seed_emergent(db, [
        ("emg-ok",  "empty verifier output means silent failure",
         ["verifier_addition"], 7.0, "approved"),
        ("emg-raw", "unverified confabulated self-diagnosis",
         ["adr"], 9.0, "proposed"),
    ])
    py = ca.failure_modes_md("code-fix", 5, db=db)
    assert "Verified emergent patterns" in py
    assert "empty verifier output means silent failure" in py
    # The 'proposed' (unverified) pattern must NOT reach the prompt.
    assert "confabulated" not in py


def test_verified_emergent_optout_and_json(db, monkeypatch):
    """MO_EMERGENT_INJECT=0 suppresses the block; JSON pack carries approved rows
    under verified_emergent_patterns."""
    monkeypatch.setenv("MINI_ORK_DB", db)
    _seed_emergent(db, [
        ("emg-ok", "cross-run lesson", ["verifier_addition"], 6.0, "approved"),
    ])
    # opt-out hides the markdown block.
    monkeypatch.setenv("MO_EMERGENT_INJECT", "0")
    py_off = ca.failure_modes_md("code-fix", 5, db=db)
    assert "Verified emergent patterns" not in py_off
    monkeypatch.delenv("MO_EMERGENT_INJECT", raising=False)

    # JSON path: verified_emergent_patterns populated.
    import tempfile
    brief = os.path.join(tempfile.mkdtemp(), "brief.json")
    with open(brief, "w") as f:
        f.write(json.dumps({"task_class": "code-fix", "goal": "x"}))
    pack = ca.context_assemble(brief, "implementer", db=db)
    ids = [e["cite"] for e in pack["verified_emergent_patterns"]]
    assert "emergent_patterns/emg-ok" in ids


def test_truncation_budget(db, tmp_path, monkeypatch):
    brief = tmp_path / "brief.json"
    brief.write_text(json.dumps({"task_class": "code-fix"}))
    monkeypatch.setenv("MINI_ORK_DB", db)
    monkeypatch.setenv("MINI_ORK_CTX_BUDGET_TOKENS", "120")
    pack = ca.context_assemble(str(brief), "implementer", db=db)
    monkeypatch.delenv("MINI_ORK_CTX_BUDGET_TOKENS")
    assert pack.get("_truncated") is True
    assert "_truncation_summary" in pack


def test_graph_context_survives_truncation(db, tmp_path, monkeypatch):
    """graph_context is bounded by SQL LIMIT only — slice_provider_default must
    never pop it under budget pressure."""
    brief = tmp_path / "brief.json"
    brief.write_text(json.dumps({"task_class": "code-fix"}))
    monkeypatch.setenv("MINI_ORK_DB", db)
    monkeypatch.setenv("MINI_ORK_CTX_BUDGET_TOKENS", "120")
    pack = ca.context_assemble(str(brief), "implementer", db=db)
    monkeypatch.delenv("MINI_ORK_CTX_BUDGET_TOKENS")
    assert pack.get("_truncated") is True
    assert "graph_context" in pack


class _FakeContextNest:
    def __init__(self, capsule_text="", retrieved=None, sessions=None):
        self.capsule_text = capsule_text
        self.retrieved = retrieved or {"hits": []}
        self.sessions = sessions or {}
        self.calls = []

    def available(self):
        return True

    def capsule(self, query, since):
        self.calls.append(("capsule", query, since))
        return self.capsule_text

    def retrieve(self, query, limit):
        self.calls.append(("retrieve", query, limit))
        return json.dumps(self.retrieved)

    def render_atoms_md(self, payload, limit):
        from mini_ork import cn_client
        return cn_client.render_atoms_md(payload, limit)

    def sessions_by_file(self, path):
        self.calls.append(("sessions", path))
        return json.dumps(self.sessions.get(path, {}))


def test_contextnest_atoms_capsule_and_retrieve_fallback(tmp_path, monkeypatch):
    brief = tmp_path / "brief.json"
    brief.write_text(json.dumps({
        "title": "Authentication migration",
        "description": "Repair session middleware",
        "task_class": "code-fix",
    }))
    capsule = "# Prompt Context\n\n## Risks\n- expired sessions" + (" x" * 60)
    client = _FakeContextNest(capsule_text=capsule)
    rendered = ca.contextnest_atoms_md(str(brief), 4, client=client)
    assert rendered.startswith("--- ContextNest capsule")
    assert "expired sessions" in rendered
    assert client.calls == [("capsule", "Authentication", "14d")]

    fallback = _FakeContextNest(retrieved={"hits": [{
        "similarity": 0.9,
        "metadata": {"kind": "risk", "ts": "2026-07-20T00:00:00Z"},
        "session_id": "session-1234",
        "content": "session cookies can expire during migration",
    }]})
    rendered = ca.contextnest_atoms_md(str(brief), 4, client=fallback)
    assert "ContextNest atoms" in rendered
    assert "session cookies" in rendered
    assert [call[0] for call in fallback.calls] == ["capsule", "retrieve"]

    monkeypatch.setenv("MO_DISABLE_CN", "1")
    assert ca.contextnest_atoms_md(str(brief), client=fallback) == ""


def test_contextnest_recent_sessions_from_file_hints(tmp_path):
    brief = tmp_path / "brief.json"
    brief.write_text(json.dumps({
        "files": ["src/auth.py", {"path": "tests/test_auth.py"}],
    }))
    client = _FakeContextNest(sessions={
        "src/auth.py": {"sessions": [{
            "session_id": "abcdef123456",
            "last_seen": "2026-07-19T10:00:00Z",
            "title": "Auth middleware repair",
        }]},
    })
    rendered = ca.contextnest_recent_sessions_md(str(brief), 2, client=client)
    assert "src/auth.py" in rendered
    assert "abcdef12" in rendered
    assert "tests/test_auth.py" not in rendered


def test_operator_steering_render_and_consume(db, monkeypatch):
    from mini_ork.steering import operator_steering

    monkeypatch.setenv("MINI_ORK_RUN_ID", "run-context")
    monkeypatch.setattr(operator_steering, "fetch_for", lambda run_id, role, db_path=None: [{
        "severity": "critical",
        "source": "operator",
        "message": "Do not change the public schema",
    }])
    rendered = ca.operator_steering_md("planner", db=db)
    assert "1 message(s)" in rendered
    assert "[CRITICAL] (from operator) Do not change the public schema" in rendered


def test_active_state_delegates_to_native_owner(db, monkeypatch):
    from mini_ork.orchestration import active_state_index

    calls = []
    monkeypatch.setattr(
        active_state_index,
        "render_active_state_block",
        lambda task_class, days, db_path: calls.append((task_class, days, db_path)) or "ACTIVE",
    )
    assert ca.active_state_md("code-fix", 14, db=db) == "ACTIVE"
    assert calls == [("code-fix", 14, db)]


# ── semantic read-back: utility-ranked emergent patterns ────────────────────


def _mirrored(db, needle):
    """The id of the memory mirrored from a seeded pattern, found by text.

    Mirrors are created on read, so this emits once to materialise them before
    looking any up — otherwise a caller seeding a track record has no id to
    seed it against.
    """
    ca.semantic_lessons_md("code-fix", 3, db=db)
    con = sqlite3.connect(db)
    try:
        rows = con.execute(
            "SELECT id, text FROM semantic_memory WHERE scope = 'code-fix'",
        ).fetchall()
    finally:
        con.close()
    matches = [mid for mid, text in rows if needle in text]
    assert len(matches) == 1, f"expected one mirror of {needle!r}: {rows}"
    return matches[0]


def _ledger(db):
    con = sqlite3.connect(db)
    try:
        return con.execute(
            "SELECT run_id, outcome FROM semantic_memory_uses",
        ).fetchall()
    finally:
        con.close()


def test_semantic_lessons_cold_store_matches_the_static_block(db, monkeypatch):
    """The non-regression property the whole tranche rests on: with no track
    record anywhere, the utility-ranked block IS the strength-ordered one. A
    cold install must not see different lessons than it saw before."""
    monkeypatch.setenv("MINI_ORK_DB", db)
    monkeypatch.delenv("MINI_ORK_RUN_ID", raising=False)
    _seed_emergent(db, [
        ("a", "strongest pattern", ["adr"], 9.0, "approved"),
        ("b", "middling pattern", ["adr"], 5.0, "approved"),
        ("c", "weakest pattern", ["adr"], 1.0, "approved"),
    ])

    warm = ca.semantic_lessons_md("code-fix", 3, db=db)
    assert warm == ca._static_emergent_block(db, 3)
    assert warm.index("strongest") < warm.index("middling") < warm.index("weakest")
    assert "(helped" not in warm, "nothing proven yet, so no credit to report"


def _seed_strength_ladder(db, count=20, top=9.0, step=0.05):
    """`count` approved patterns on a fine strength grid, strongest first.

    A fine grid is what makes reordering observable: with a coarse one the
    normalised prior gaps are so large that no record and no exploration bonus
    can cross them, and a test would pass whether or not the ranking worked.
    """
    _seed_emergent(db, [
        (f"p{index:02d}", f"pattern-{index:02d} says something", ["adr"],
         top - index * step, "approved")
        for index in range(count)
    ])


def test_semantic_lessons_rank_by_earned_utility(db, monkeypatch):
    """The point of the tranche: a pattern that has actually worked outranks
    equally-prior'd patterns that have not. With no prior to separate them,
    the record is the entire ranking."""
    monkeypatch.setenv("MINI_ORK_DB", db)
    monkeypatch.delenv("MINI_ORK_RUN_ID", raising=False)
    _seed_emergent(db, [
        ("emg-a", "first pattern", ["adr"], 7.0, "approved"),
        ("emg-b", "second pattern that works", ["adr"], 7.0, "approved"),
        ("emg-c", "third pattern", ["adr"], 7.0, "approved"),
    ])

    from mini_ork import memory as semantic
    mid = _mirrored(db, "second pattern that works")
    for i in range(3):
        semantic.record_retrievals([mid], scope="code-fix", run_id=f"r{i}", db_path=db)
        semantic.record_outcome(f"r{i}", True, db_path=db)

    py = ca.semantic_lessons_md("code-fix", 3, db=db)
    bullets = [ln for ln in py.splitlines() if ln.startswith("- ")]
    assert bullets[0].startswith("- [adr] second pattern that works"), (
        f"the pattern with a record did not lead its equals: {py!r}"
    )
    assert "(helped 3/3 retrievals)" in py
    assert "first pattern" in py and "third pattern" in py, "no peer was dropped"


def test_semantic_lessons_demote_a_pattern_that_never_helps(db, monkeypatch):
    """A pattern at the top of the prior order that has been retrieved twenty
    times and helped none of them loses its place to untried peers. The prior
    gates — it chose the pool — but inside the pool the record overrules it."""
    monkeypatch.setenv("MINI_ORK_DB", db)
    monkeypatch.delenv("MINI_ORK_RUN_ID", raising=False)
    _seed_strength_ladder(db, count=20)
    from mini_ork import memory as semantic
    failed = _mirrored(db, "pattern-00 says something")
    # Forty, not a handful: the penalty is bounded, so a long losing record is
    # what makes the demotion decisive rather than a knife-edge tie with the
    # peer it has to fall behind.
    for i in range(40):
        semantic.record_retrievals([failed], scope="code-fix", run_id=f"r{i}", db_path=db)
        semantic.record_outcome(f"r{i}", False, db_path=db)

    py = ca.semantic_lessons_md("code-fix", 3, db=db)

    assert "pattern-00 says something" not in py, (
        f"a pattern that never once helped held its place: {py!r}"
    )
    assert py.count("- [") == 3
    # The ones that displaced it are its near-prior peers, not distant ones —
    # the gate still bounds how far the record can reach.
    assert all(f"pattern-{i:02d}" in py for i in (1, 2, 3)), py


def test_semantic_lessons_gate_keeps_low_prior_patterns_out(db, monkeypatch):
    """Utility may reorder the pool; it may not reach past it. A pattern far
    down the prior order cannot buy its way in with a perfect record — the
    gate is the only thing standing between a measured ranking and a single
    lucky streak."""
    monkeypatch.setenv("MINI_ORK_DB", db)
    monkeypatch.delenv("MINI_ORK_RUN_ID", raising=False)
    _seed_strength_ladder(db, count=20)
    from mini_ork import memory as semantic
    outsider = _mirrored(db, "pattern-19 says something")  # weakest prior
    for i in range(20):
        semantic.record_retrievals([outsider], scope="code-fix", run_id=f"r{i}", db_path=db)
        semantic.record_outcome(f"r{i}", True, db_path=db)

    py = ca.semantic_lessons_md("code-fix", 3, db=db)

    assert "pattern-19 says something" not in py, (
        f"a perfect record reached past the prior gate: {py!r}"
    )
    assert len(py.splitlines()) == 5, "block shape unchanged"


def test_semantic_lessons_optout_restores_the_static_order(db, monkeypatch):
    """MO_SEMANTIC_INJECT=0 is the way back, not the way in: the block still
    ships, in the ordering it had before this channel existed."""
    monkeypatch.setenv("MINI_ORK_DB", db)
    monkeypatch.delenv("MINI_ORK_RUN_ID", raising=False)
    _seed_emergent(db, [
        ("emg-strong", "broad vague advice", ["adr"], 9.0, "approved"),
        ("emg-weak", "specific narrow fix", ["verifier_addition"], 1.0, "approved"),
    ])
    from mini_ork import memory as semantic
    ca.semantic_lessons_md("code-fix", 3, db=db)  # mirror-on-read creates the row
    mid = _mirrored(db, "specific narrow")
    semantic.record_retrievals([mid], scope="code-fix", run_id="r0", db_path=db)
    semantic.record_outcome("r0", True, db_path=db)

    monkeypatch.setenv("MO_SEMANTIC_INJECT", "0")
    assert ca.semantic_lessons_md("code-fix", 3, db=db) == ""
    py = ca.failure_modes_md("code-fix", 3, db=db)
    assert "broad vague" in py and "specific narrow" in py
    assert py.index("broad vague") < py.index("specific narrow"), "static order back"
    assert "Verified emergent patterns" in py, "the block must not disappear"


def test_semantic_lessons_log_retrieval_only_inside_a_run(db, monkeypatch):
    """A retrieval with no run to attribute it to can never resolve to a win,
    so writing one would depress the memory's utility for nothing. Outside a
    run the block is emitted and nothing is logged."""
    monkeypatch.setenv("MINI_ORK_DB", db)
    _seed_emergent(db, [("emg-1", "a lesson", ["adr"], 5.0, "approved")])

    monkeypatch.delenv("MINI_ORK_RUN_ID", raising=False)
    assert ca.semantic_lessons_md("code-fix", 3, db=db) != ""
    assert _ledger(db) == [], "no run → nothing to attribute → nothing logged"

    monkeypatch.setenv("MINI_ORK_RUN_ID", "run-x")
    assert ca.semantic_lessons_md("code-fix", 3, db=db) != ""
    assert _ledger(db) == [("run-x", "pending")]


def test_semantic_lessons_close_the_loop_across_injections(db, monkeypatch):
    """End to end over two injections of the same run: the first logs a
    retrieval, the run finishes, and the second injection's sweep resolves it
    before ranking — so the prompt that follows reports credit the first one
    earned."""
    monkeypatch.setenv("MINI_ORK_DB", db)
    monkeypatch.setenv("MINI_ORK_RUN_ID", "run-loop")
    _seed_emergent(db, [("emg-1", "lesson that helped", ["adr"], 1.0, "approved")])

    first = ca.semantic_lessons_md("code-fix", 3, db=db)
    assert "lesson that helped" in first and "(helped" not in first
    assert _ledger(db) == [("run-loop", "pending")]

    trace_store.trace_write(
        {"trace_id": "tl-1", "run_id": "run-loop", "task_class": "code-fix",
         "status": "success", "cost_usd": 0.1, "duration_ms": 10,
         "agent_version_id": "codex"}, db=db)

    second = ca.semantic_lessons_md("code-fix", 3, db=db)
    assert second == first.replace("- [adr] lesson that helped",
                                  "- [adr] lesson that helped  (helped 1/1 retrievals)"), (
        f"the completed run's win was not reflected: {second!r}"
    )


def test_semantic_lessons_hold_a_still_running_run_pending(db, monkeypatch):
    """The sweep must not credit a run whose traces are still live."""
    monkeypatch.setenv("MINI_ORK_DB", db)
    monkeypatch.setenv("MINI_ORK_RUN_ID", "run-live")
    _seed_emergent(db, [("emg-1", "a lesson", ["adr"], 5.0, "approved")])
    ca.semantic_lessons_md("code-fix", 3, db=db)

    trace_store.trace_write(
        {"trace_id": "tn-1", "run_id": "run-live", "task_class": "code-fix",
         "status": "running", "cost_usd": 0.0, "duration_ms": 0,
         "agent_version_id": "codex"}, db=db)
    py = ca.semantic_lessons_md("code-fix", 3, db=db)

    assert "(helped" not in py, f"an unfinished run earned credit: {py!r}"
    assert [o for _r, o in _ledger(db)] == ["pending", "pending"]


def test_semantic_lessons_cold_safe_without_the_patterns_table(db, monkeypatch):
    monkeypatch.setenv("MINI_ORK_DB", db)
    con = sqlite3.connect(db)
    con.execute("DROP TABLE emergent_patterns")
    con.commit()
    con.close()

    assert ca.semantic_lessons_md("code-fix", 5, db=db) == ""
    assert ca._static_emergent_block(db, 5) == ""
    assert "Verified emergent patterns" not in ca.failure_modes_md("code-fix", 5, db=db)


def test_semantic_lessons_do_not_leak_proposed_patterns(db, monkeypatch):
    """The confabulation guard survives the channel change: only judge-gated
    'approved' rows are ever mirrored, so a 'proposed' self-diagnosis cannot
    reach the prompt by being ranked."""
    monkeypatch.setenv("MINI_ORK_DB", db)
    monkeypatch.delenv("MINI_ORK_RUN_ID", raising=False)
    _seed_emergent(db, [
        ("emg-raw", "unverified confabulated self-diagnosis", ["adr"], 99.0, "proposed"),
    ])

    assert ca.semantic_lessons_md("code-fix", 5, db=db) == ""
    assert "confabulated" not in ca.failure_modes_md("code-fix", 5, db=db)
    con = sqlite3.connect(db)
    try:
        n = con.execute("SELECT COUNT(*) FROM semantic_memory").fetchone()[0]
    finally:
        con.close()
    assert n == 0, "a proposed pattern must not even be mirrored"
