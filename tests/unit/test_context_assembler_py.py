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


def test_context_assemble_shape(db, tmp_path, monkeypatch):
    brief = tmp_path / "brief.json"
    brief.write_text(json.dumps({"task_class": "code-fix", "goal": "fix auth tests"}))
    monkeypatch.setenv("MINI_ORK_DB", db)
    py_pack = ca.context_assemble(str(brief), "implementer", db=db)
    assert py_pack["workflow_node"] == "implementer"
    assert py_pack["task_brief"]["content"]["task_class"] == "code-fix"
    assert py_pack["known_failure_modes"]
    assert py_pack["prior_similar_runs"]


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
    from mini_ork.ported import operator_steering

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
    from mini_ork.ported import active_state_index

    calls = []
    monkeypatch.setattr(
        active_state_index,
        "render_active_state_block",
        lambda task_class, days, db_path: calls.append((task_class, days, db_path)) or "ACTIVE",
    )
    assert ca.active_state_md("code-fix", 14, db=db) == "ACTIVE"
    assert calls == [("code-fix", 14, db)]
