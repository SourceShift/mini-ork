"""Contract tests for the Python port of ``mini-ork apply``
(``mini_ork/cli/apply.py``).

The bash implementation (``bin/mini-ork-apply`` + ``lib/apply.sh``) is the
reference; these tests pin the ported behaviour against tmp sqlite fixtures:

  1. pick_candidate: pattern_records priority + LIKE matching
  2. pick_candidate: emergent_patterns fallback
  3. pick_candidate: gradient_records last resort
  4. score_candidate: deterministic mock + forced-regression seam
  5. evaluate_gate: equal / regression / improvement (bash self-test cases)
  6. evaluate_gate: per-task no-regression gate (2607.14004) + tolerance seam
  7. evaluate_gate: the retired human-approval flag is inert
  8. apply_run: no_candidate path (audit row + summary line)
  9. apply_run: full dry-run promote path (candidate + promotion + attempt rows,
     no file write while MO_APPLY_ENABLED is off)
 10. apply_run: enabled promote rewrites the target file + the version_registry
     rows (promoted + baseline) that make it reversible
 11. apply_run: forced regression quarantines (file untouched)
 12. CLI: --help / usage errors / missing-flag-value exit codes
 13. Native integration: apply is in _NATIVE_SUBS, native dispatch (_EXEC_SUBS deleted), and the
     SUBCOMMAND_REGISTRY handler dispatches ``python -m mini_ork.cli.apply``.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.cli import apply as ap
from mini_ork.cli import main as cli_main

# ── Minimal fixture schema (mirrors the canonical migrations) ────────────────
FIXTURE_DDL = """
CREATE TABLE pattern_records (
  pattern_id            TEXT PRIMARY KEY,
  description           TEXT NOT NULL,
  evidence_trace_ids    TEXT NOT NULL DEFAULT '[]',
  frequency             INTEGER NOT NULL DEFAULT 1,
  first_seen            TEXT NOT NULL DEFAULT '',
  last_seen             TEXT NOT NULL DEFAULT '',
  output_type           TEXT NOT NULL,
  promoted_to           TEXT,
  status                TEXT NOT NULL DEFAULT 'observed'
);
CREATE TABLE emergent_patterns (
  pattern_id           TEXT PRIMARY KEY,
  cluster_label        TEXT NOT NULL,
  member_item_ids_json TEXT NOT NULL,
  feature_set_json     TEXT NOT NULL,
  strength_score       REAL NOT NULL,
  suggested_meta_adr   TEXT,
  status               TEXT NOT NULL DEFAULT 'proposed',
  detected_at          INTEGER NOT NULL,
  resolved_at          INTEGER
);
CREATE TABLE gradient_records (
    gradient_id      TEXT PRIMARY KEY,
    target           TEXT NOT NULL,
    signal           TEXT NOT NULL,
    suggested_change TEXT NOT NULL,
    evidence         TEXT NOT NULL,
    confidence       REAL NOT NULL DEFAULT 0.0,
    created_at       INTEGER NOT NULL,
    task_class       TEXT
);
CREATE TABLE workflow_memory (
  workflow_version_id       TEXT PRIMARY KEY,
  workflow_name             TEXT NOT NULL,
  base_version_id           TEXT,
  yaml_hash                 TEXT NOT NULL,
  yaml_blob                 TEXT NOT NULL,
  mutations                 TEXT NOT NULL DEFAULT '[]',
  created_at                TEXT NOT NULL DEFAULT '',
  status                    TEXT NOT NULL DEFAULT 'candidate',
  previous_stable_version_id TEXT
);
CREATE TABLE workflow_candidates (
  candidate_id              TEXT PRIMARY KEY,
  base_workflow_version_id  TEXT NOT NULL,
  mutations                 TEXT NOT NULL DEFAULT '[]',
  status                    TEXT NOT NULL DEFAULT 'candidate',
  benchmark_summary_id      TEXT,
  utility_delta             REAL NOT NULL DEFAULT 0.0,
  created_by                TEXT NOT NULL DEFAULT 'evolution_engine',
  created_at                TEXT NOT NULL DEFAULT ''
);
CREATE TABLE promotion_records (
  promotion_id          TEXT PRIMARY KEY,
  candidate_id          TEXT NOT NULL,
  from_version_id       TEXT NOT NULL,
  to_version_id         TEXT NOT NULL,
  utility_before        REAL NOT NULL DEFAULT 0.0,
  utility_after         REAL NOT NULL DEFAULT 0.0,
  benchmark_run_id      TEXT,
  rationale             TEXT NOT NULL DEFAULT '',
  decision              TEXT NOT NULL,
  decided_at            TEXT NOT NULL DEFAULT '',
  decided_by            TEXT NOT NULL
);
-- Canonical db/migrations/0048_apply_attempts.sql DDL. Production DBs get the
-- table from the migration (the lib's idempotent guard is then a no-op), so
-- the fixture mirrors the migration — including source_kind='none'.
CREATE TABLE apply_attempts (
    attempt_id              TEXT PRIMARY KEY,
    task_class              TEXT NOT NULL,
    target_kind             TEXT NOT NULL
                            CHECK (target_kind IN ('workflow_node','workflow_edge','agent_prompt','prompt_file')),
    target_name             TEXT NOT NULL,
    source_kind             TEXT NOT NULL
                            CHECK (source_kind IN ('pattern_records','emergent_patterns','gradient_records','synthesis_gate_verdict','none')),
    source_id               TEXT,
    candidate_id            TEXT REFERENCES workflow_candidates(candidate_id) ON DELETE SET NULL,
    promotion_id            TEXT REFERENCES promotion_records(promotion_id) ON DELETE SET NULL,
    base_workflow_version_id TEXT,
    utility_before          REAL,
    utility_after           REAL,
    utility_delta           REAL,
    decision                TEXT NOT NULL
                            CHECK (decision IN ('promoted','quarantined','rejected','pending_human_approval','dry_run','no_candidate')),
    rationale               TEXT NOT NULL DEFAULT '',
    dry_run                 INTEGER NOT NULL DEFAULT 0 CHECK (dry_run IN (0,1)),
    apply_enabled           INTEGER NOT NULL DEFAULT 0 CHECK (apply_enabled IN (0,1)),
    created_at              TEXT NOT NULL DEFAULT ''
);
"""

# Env vars the apply module reads — scrubbed for isolation.
_APPLY_ENV = [
    "MINI_ORK_DB", "MINI_ORK_HOME", "MINI_ORK_ROOT",
    "MO_APPLY_ENABLED", "MO_APPLY_DRY_RUN", "MO_APPLY_SCORER",
    "MO_APPLY_MODE",
    "MO_APPLY_NONREGRESSION_DELTA", "MO_APPLY_MIN_EXAMPLES",
    "MO_APPLY_REGRESSION_TOLERANCE", "MO_APPLY_PERTASK_JSON",
    "MO_APPLY_MOCK_BASELINE", "MO_APPLY_MOCK_DELTA",
    "MO_APPLY_FORCE_REGRESSION",
    "MO_AUTO_APPLY", "MO_AUTO_APPLY_MAX_TARGETS",
    "MO_APPLY_PROBE_MAX_TASKS", "MO_APPLY_PROBE_BUDGET_USD",
    "MO_APPLY_PROBE_TIMEOUT_S",
]


@pytest.fixture()
def envscrub(monkeypatch):
    for var in _APPLY_ENV:
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


@pytest.fixture()
def db(tmp_path, envscrub):
    """A tmp sqlite DB with the minimal fixture schema; MINI_ORK_DB set."""
    path = tmp_path / "state.db"
    con = sqlite3.connect(path)
    con.executescript(FIXTURE_DDL)
    con.commit()
    con.close()
    envscrub.setenv("MINI_ORK_DB", str(path))
    ap._SCHEMA_INIT = False
    yield str(path)
    ap._SCHEMA_INIT = False


def _rows(db_path, table):
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(f"SELECT * FROM {table}").fetchall()]
    finally:
        con.close()


def _seed_pattern(db_path, pattern_id="pat-1", description="improve prompts/reviewer.md wording",
                  frequency=7, status="observed", output_type="prompt_change"):
    con = sqlite3.connect(db_path)
    con.execute(
        "INSERT INTO pattern_records (pattern_id, description, frequency, output_type, status)"
        " VALUES (?,?,?,?,?)",
        (pattern_id, description, frequency, output_type, status),
    )
    con.commit()
    con.close()


# ── 1-3. pick_candidate source priority ──────────────────────────────────────

def test_pick_candidate_pattern_records_priority(db):
    _seed_pattern(db)
    picked = json.loads(ap.pick_candidate("reviewer", "prompt_file", "prompts/reviewer.md", db=db))
    assert picked["source_kind"] == "pattern_records"
    assert picked["source_id"] == "pat-1"
    assert picked["confidence"] == 1.0  # only row → frequency / max(frequency)
    assert picked["suggested_change"] == "improve prompts/reviewer.md wording"
    assert picked["frequency"] == 7


def test_pick_candidate_skips_promoted_and_falls_to_emergent(db):
    _seed_pattern(db, status="promoted")  # excluded by the picker
    con = sqlite3.connect(db)
    con.execute(
        "INSERT INTO emergent_patterns"
        " (pattern_id, cluster_label, member_item_ids_json, feature_set_json,"
        "  strength_score, suggested_meta_adr, status, detected_at)"
        " VALUES ('ep-1','reviewer cluster','[]','[]',0.7,'tighten prompts/reviewer.md','proposed',100)",
    )
    con.commit()
    con.close()
    picked = json.loads(ap.pick_candidate("reviewer", "prompt_file", "prompts/reviewer.md", db=db))
    assert picked["source_kind"] == "emergent_patterns"
    assert picked["source_id"] == "ep-1"
    assert picked["confidence"] == 0.7
    assert picked["suggested_change"] == "tighten prompts/reviewer.md"


def test_pick_candidate_gradient_last_resort(db):
    con = sqlite3.connect(db)
    con.execute(
        "INSERT INTO gradient_records"
        " (gradient_id, target, signal, suggested_change, evidence, confidence, created_at, task_class)"
        " VALUES ('gr-1','prompts/reviewer.md','rubric','be more specific','[]',0.42,100,'reviewer')",
    )
    con.commit()
    con.close()
    picked = json.loads(ap.pick_candidate("reviewer", "prompt_file", "prompts/reviewer.md", db=db))
    assert picked["source_kind"] == "gradient_records"
    assert picked["confidence"] == pytest.approx(0.42)
    assert picked["suggested_change"] == "be more specific"
    # No rows at all → empty line (bash echoes "").
    assert ap.pick_candidate("other_class", "prompt_file", "x", db=db) == ""


# ── 4. score_candidate ───────────────────────────────────────────────────────

def test_score_candidate_mock_deterministic(db, envscrub):
    # mock is no longer the default (probe is); select it explicitly.
    envscrub.setenv("MO_APPLY_SCORER", "mock")
    first = ap.score_candidate("cand-abc")
    second = ap.score_candidate("cand-abc")
    assert first == second
    score_s, n_s = first.split()
    assert n_s == "5"
    # baseline 0.5 + delta 0.05 ± hash jitter → well above the 0.0 gate baseline
    assert 0.5 <= float(score_s) <= 0.6

    envscrub.setenv("MO_APPLY_FORCE_REGRESSION", "1")
    reg = ap.score_candidate("cand-abc").split()[0]
    assert float(reg) == pytest.approx(0.35)  # max(0, 0.5 - 0.10 - 0.05)

    envscrub.setenv("MO_APPLY_SCORER", "nonsense")
    assert ap.score_candidate("cand-abc") == "0.5 1"  # unknown → neutral


# ── 5-7. evaluate_gate (mirrors the bash self-test battery) ──────────────────

def test_evaluate_gate_scalar_paths(db):
    # equal scores → promoted (delta >= dt fires before the ambiguity branch)
    assert json.loads(ap.evaluate_gate("cand-test", 0.5, 0.5))["decision"] == "promoted"
    # regression → quarantined
    reg = json.loads(ap.evaluate_gate("cand-test", 0.7, 0.5))
    assert reg["decision"] == "quarantined"
    assert reg["utility_delta"] == pytest.approx(-0.2)
    # improvement → promoted
    assert json.loads(ap.evaluate_gate("cand-test", 0.5, 0.7))["decision"] == "promoted"
    # legacy scalar path reports regressed_tasks == -1
    assert json.loads(ap.evaluate_gate("cand-test", 0.5, 0.7))["regressed_tasks"] == -1


def test_evaluate_gate_pertask_no_regression(db, envscrub):
    # aggregate UP (0.5→0.7) but a previously-solved task regressed → quarantined
    out = json.loads(ap.evaluate_gate("cand-test", 0.5, 0.7,
                                      '{"before":[1,1,1],"after":[1,0,1]}'))
    assert out["decision"] == "quarantined"
    assert out["regressed_tasks"] == 1
    # aggregate up with no pass→fail (a 0→1 recovery is fine) → promoted
    clean = json.loads(ap.evaluate_gate("cand-test", 0.5, 0.7,
                                        '{"before":[1,0,1],"after":[1,1,1]}'))
    assert clean["decision"] == "promoted"
    assert clean["regressed_tasks"] == 0
    # tolerance seam: 1 regression tolerated → promoted despite the break
    envscrub.setenv("MO_APPLY_REGRESSION_TOLERANCE", "1")
    tol = json.loads(ap.evaluate_gate("cand-test", 0.5, 0.7,
                                      '{"before":[1,1,1],"after":[1,0,1]}'))
    assert tol["decision"] == "promoted"


def test_evaluate_gate_measured_path_requires_strict_improvement(db):
    """A real held-out measurement must show a GAIN, not merely the absence of
    a regression.

    On the scalar-only path (no per-task vectors) delta == dt promotes — that
    is the legacy "no regression" rule, pinned by
    test_evaluate_gate_scalar_paths. Once per-task vectors exist, a real probe
    run has happened, and a candidate whose publish rate equals the baseline's
    is indistinguishable from a directive that does nothing — as is a probe
    harness where every probe fails in BOTH arms. Both used to promote.
    """
    # Every probe publishes in both arms: the measurement found no difference.
    flat = json.loads(ap.evaluate_gate(
        "cand-test", 1.0, 1.0, '{"before":[1,1],"after":[1,1],"ids":["p1","p2"]}'))
    assert flat["decision"] == "quarantined"
    assert flat["rationale"].startswith("no measured improvement")
    assert flat["regressed_tasks"] == 0

    # Every probe fails in both arms (broken harness): also no evidence.
    dead = json.loads(ap.evaluate_gate(
        "cand-test", 0.0, 0.0, '{"before":[0,0],"after":[0,0],"ids":["p1","p2"]}'))
    assert dead["decision"] == "quarantined"
    assert dead["rationale"].startswith("no measured improvement")

    # One probe recovered (0→1), none regressed: a measured gain.
    gain = json.loads(ap.evaluate_gate(
        "cand-test", 0.5, 1.0, '{"before":[0,1],"after":[1,1],"ids":["p1","p2"]}'))
    assert gain["decision"] == "promoted"

    # The scalar path is untouched: no vectors ⇒ delta >= dt still promotes.
    assert json.loads(ap.evaluate_gate("cand-test", 0.5, 0.5))["decision"] == "promoted"


def test_evaluate_gate_human_approval_flag_is_inert(db, envscrub):
    """The old human gate is gone, and its env var cannot bring it back.

    ``MINI_ORK_REQUIRE_HUMAN_APPROVAL`` used to divert every gate decision to
    ``pending_human_approval``. The gate is now a measurement verdict with no
    approval branch, so the variable has no reader: setting it must not change
    the outcome, and ``needs_human`` must not be part of the payload.
    """
    envscrub.setenv("MINI_ORK_REQUIRE_HUMAN_APPROVAL", "true")
    out = json.loads(ap.evaluate_gate("cand-test", 0.5, 0.9))
    assert out["decision"] == "promoted"       # the measurement verdict, unchanged
    assert "needs_human" not in out


# ── 8. apply_run: no_candidate ───────────────────────────────────────────────

def test_apply_run_no_candidate(db, capsys):
    rc = ap.apply_run("reviewer", "prompt_file", "prompts/reviewer.md", db=db)
    assert rc == 0
    out = capsys.readouterr().out
    lines = out.strip().splitlines()
    # bash leaks the attempt_record id line (no > /dev/null on this path)
    assert lines[0].startswith("apply-")
    summary = json.loads(lines[1])
    assert summary == {"decision": "no_candidate", "task_class": "reviewer",
                       "target": "prompts/reviewer.md"}
    attempts = _rows(db, "apply_attempts")
    assert len(attempts) == 1
    assert attempts[0]["decision"] == "no_candidate"
    assert attempts[0]["source_kind"] == "none"


# ── 9-11. apply_run full pipelines ───────────────────────────────────────────

def test_apply_run_mock_score_quarantines_and_writes_no_file(db, tmp_path, capsys, envscrub):
    _seed_pattern(db)
    target = tmp_path / "reviewer.md"
    target.write_text("ORIGINAL PROMPT\n")
    # Master gate OFF (default) → stage + score + audit, but never write.
    # The mock scorer fabricates utility, so even a "passing" score is refused:
    # the candidate is quarantined, not promoted.
    envscrub.setenv("MO_APPLY_SCORER", "mock")
    rc = ap.apply_run("reviewer", "prompt_file", "prompts/reviewer.md",
                      str(target), db=db)
    assert rc == 0
    summary = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert summary["decision"] == "quarantined"
    assert summary["candidate_id"].startswith("cand-")
    assert summary["promotion_id"].startswith("pr-")
    assert summary["version_id"] == ""  # no write while disabled
    assert target.read_text() == "ORIGINAL PROMPT\n"

    cands = _rows(db, "workflow_candidates")
    assert len(cands) == 1
    mutations = json.loads(cands[0]["mutations"])
    assert mutations[0]["kind"] == "prompt_change"
    assert mutations[0]["node_name"] == "prompts/reviewer.md"
    assert mutations[0]["new_val"] == "improve prompts/reviewer.md wording"
    assert mutations[0]["source_kind"] == "pattern_records"

    promos = _rows(db, "promotion_records")
    assert len(promos) == 1
    assert promos[0]["decision"] == "quarantined"
    assert promos[0]["decided_by"] == "gate"
    assert "fabricates utility" in promos[0]["rationale"]

    attempts = _rows(db, "apply_attempts")
    assert len(attempts) == 1
    assert attempts[0]["decision"] == "quarantined"
    assert attempts[0]["dry_run"] == 0
    assert attempts[0]["apply_enabled"] == 0


def test_apply_run_enabled_rewrites_file_and_registers_version(db, tmp_path, capsys, envscrub, monkeypatch):
    from mini_ork.learning import probe_scorer as ps
    _seed_pattern(db)
    target = tmp_path / "reviewer.md"
    target.write_text("ORIGINAL PROMPT\n")
    envscrub.setenv("MO_APPLY_ENABLED", "1")
    # A measured GAIN (one probe recovered, none regressed) — the only shape
    # that promotes now that the human gate is gone: see
    # test_evaluate_gate_measured_path_requires_strict_improvement.
    probe_out = {"before": 0.5, "after": 1.0, "n": 2,
                 "pertask_json": json.dumps({"before": [0, 1], "after": [1, 1],
                                             "ids": ["probe-1.md", "probe-2.md"]}),
                 "runs": [], "cost_usd": 0.08}
    monkeypatch.setattr(ps, "probe_score", lambda *a, **k: dict(probe_out))
    rc = ap.apply_run("reviewer", "prompt_file", "prompts/reviewer.md",
                      str(target), db=db)
    assert rc == 0
    summary = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert summary["decision"] == "promoted"
    # F3 append semantics: the original prompt survives, the change lands as
    # an idempotent directive block below it.
    applied = target.read_text()
    assert applied.startswith("ORIGINAL PROMPT\n")
    assert "<!-- applied:pattern_records:pat-1 -->" in applied
    assert "- Directive: improve prompts/reviewer.md wording" in applied
    # The rollback handle is in the registry payload, not a
    # `<target>.apply-rollback-<pid>` sidecar: the sidecar survived only as long
    # as nobody cleaned the worktree, and rollback() could not locate it anyway.
    assert list(tmp_path.glob("reviewer.md.apply-rollback-*")) == []
    versions = _rows(db, "version_registry")
    # Two rows: the promoted version, plus the baseline row minted for the
    # pre-mutation text so this first promotion has somewhere to roll back to.
    assert len(versions) == 2
    promoted = [v for v in versions if not json.loads(v["payload"]).get("baseline")]
    baseline = [v for v in versions if json.loads(v["payload"]).get("baseline")]
    assert len(promoted) == 1 and len(baseline) == 1
    assert promoted[0]["kind"] == "agent"
    assert promoted[0]["name"] == str(target)
    assert promoted[0]["promoted_at"] is not None
    assert promoted[0]["previous_stable_version"] == baseline[0]["version_id"]
    payload = json.loads(promoted[0]["payload"])
    assert payload["rollback_hash"]
    assert payload["content"] == applied
    assert payload["candidate_id"] == summary["candidate_id"]
    assert json.loads(baseline[0]["payload"])["content"] == "ORIGINAL PROMPT\n"
    assert summary["version_id"] == promoted[0]["version_id"]
    assert _rows(db, "apply_attempts")[0]["apply_enabled"] == 1
    # the promote is measured, not fabricated
    rationale = _rows(db, "promotion_records")[0]["rationale"]
    assert "probe: n=2" in rationale
    assert "fabricates utility" not in rationale


def test_apply_run_forced_regression_quarantines(db, tmp_path, capsys, envscrub):
    _seed_pattern(db)
    target = tmp_path / "reviewer.md"
    target.write_text("ORIGINAL PROMPT\n")
    envscrub.setenv("MO_APPLY_ENABLED", "1")
    envscrub.setenv("MO_APPLY_SCORER", "mock")
    # before=0.5 (mock baseline), after=max(0, 0.5-0.10-0.05)=0.35 → regression
    envscrub.setenv("MO_APPLY_MOCK_BASELINE", "0.5")
    envscrub.setenv("MO_APPLY_FORCE_REGRESSION", "1")
    rc = ap.apply_run("reviewer", "prompt_file", "prompts/reviewer.md",
                      str(target), db=db)
    assert rc == 0  # quarantine is success — the gate ENFORCED itself
    summary = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert summary["decision"] == "quarantined"
    assert summary["version_id"] == ""
    assert target.read_text() == "ORIGINAL PROMPT\n"
    assert not list(tmp_path.glob("reviewer.md.apply-rollback-*"))
    # quarantine still writes the promotion audit row explaining the decision
    promos = _rows(db, "promotion_records")
    assert promos[0]["decision"] == "quarantined"
    assert "regression" in promos[0]["rationale"]
    assert _rows(db, "apply_attempts")[0]["decision"] == "quarantined"


# ── 11b. F3 enable semantics (append + unvetted honesty + target filter) ─────

def test_apply_run_mock_never_promotes(db, tmp_path, capsys, envscrub):
    """A fabricating scorer cannot promote, and no flag restores the ability.

    This is the invariant that makes removing the human approval gate safe:
    the audit trail can never claim a measured improvement that was not measured.
    """
    _seed_pattern(db)
    target = tmp_path / "reviewer.md"
    target.write_text("ORIGINAL PROMPT\n")
    envscrub.setenv("MO_APPLY_ENABLED", "1")
    envscrub.setenv("MO_APPLY_SCORER", "mock")
    # The retired escape hatch: setting it must change nothing.
    envscrub.setenv("MO_APPLY_UNVETTED", "1")
    rc = ap.apply_run("reviewer", "prompt_file", "prompts/reviewer.md",
                      str(target), db=db)
    assert rc == 0
    summary = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert summary["decision"] == "quarantined"
    assert summary["version_id"] == ""
    assert target.read_text() == "ORIGINAL PROMPT\n"
    assert not list(tmp_path.glob("reviewer.md.apply-rollback-*"))
    assert "fabricates utility" in _rows(db, "promotion_records")[0]["rationale"]
    assert _rows(db, "apply_attempts")[0]["decision"] == "quarantined"


def test_apply_mutation_append_idempotent(db, tmp_path, envscrub):
    target = tmp_path / "reviewer.md"
    target.write_text("ORIGINAL PROMPT\n")
    envscrub.setenv("MO_APPLY_ENABLED", "1")
    kw = {"source_ref": "gradient_records:gr-9", "context": "empty traces"}
    v1 = ap.apply_mutation("cand-1", str(target), "emit an action ledger", db=db, **kw)
    assert v1  # version registered
    applied = target.read_text()
    assert applied.startswith("ORIGINAL PROMPT\n")
    assert "<!-- applied:gradient_records:gr-9 -->" in applied
    assert "- Observation: empty traces" in applied
    assert "- Directive: emit an action ledger" in applied
    # Re-apply of the same source is a no-op (idempotency marker).
    v2 = ap.apply_mutation("cand-2", str(target), "emit an action ledger", db=db, **kw)
    assert v2 == ""
    assert target.read_text() == applied  # byte-identical


def test_apply_mutation_replace_mode_restores_whole_file(db, tmp_path, envscrub):
    target = tmp_path / "reviewer.md"
    target.write_text("ORIGINAL PROMPT\n")
    envscrub.setenv("MO_APPLY_ENABLED", "1")
    envscrub.setenv("MO_APPLY_MODE", "replace")
    ap.apply_mutation("cand-1", str(target), "FULL NEW PROMPT", db=db)
    assert target.read_text() == "FULL NEW PROMPT\n"


def test_pick_candidate_gradient_filters_by_target(db):
    con = sqlite3.connect(db)
    con.executemany(
        "INSERT INTO gradient_records"
        " (gradient_id, target, signal, suggested_change, evidence, confidence, created_at, task_class)"
        " VALUES (?,?,?,?,?,?,?,?)",
        [("gr-a", "agent.reviewer.prompt", "sig-a", "change-a", "[]", 0.9, 100, "framework_edit"),
         ("gr-b", "workflow.node.verify", "sig-b", "change-b", "[]", 0.99, 101, "framework_edit")],
    )
    con.commit()
    con.close()
    # Must pick THIS target's gradient (gr-a), not the task_class-wide
    # highest-confidence one (gr-b lives on a different target).
    picked = json.loads(ap.pick_candidate("framework_edit", "prompt_file",
                                          "agent.reviewer.prompt", db=db))
    assert picked["source_id"] == "gr-a"
    assert picked["suggested_change"] == "change-a"
    # Unknown target → no candidate, not a mismatched borrow.
    assert ap.pick_candidate("framework_edit", "prompt_file",
                             "agent.planner.prompt", db=db) == ""


# ── 12. CLI surface (bin/mini-ork-apply parity) ──────────────────────────────

def test_cli_help_and_usage_errors(db, capsys, envscrub):
    assert ap.main(["--help"]) == 0
    out = capsys.readouterr().out
    assert out == ap.help_text()
    assert out.startswith("Usage: bin/mini-ork apply --task-class <name> --target <file>\n")

    # missing required flags → rc 2, message + usage on stderr
    assert ap.main(["--target", "x.md"]) == 2
    err = capsys.readouterr().err
    assert err.startswith("--task-class is required\n")
    assert "--task-class is required\n" + ap.help_text() == err

    assert ap.main(["--task-class", "reviewer"]) == 2
    assert capsys.readouterr().err.startswith("--target is required\n")

    # unknown flag → rc 2 + usage; positional → rc 2 without usage
    assert ap.main(["--bogus"]) == 2
    err = capsys.readouterr().err
    assert err == "Unknown flag: --bogus\n" + ap.help_text()
    assert ap.main(["positional"]) == 2
    assert capsys.readouterr().err == "Unexpected argument: positional\n"

    # missing flag value → rc 1 (bash `${2:?msg}` abort)
    assert ap.main(["--task-class"]) == 1
    assert capsys.readouterr().err == "--task-class requires a value\n"


def test_cli_main_end_to_end_no_candidate(db, tmp_path, capsys, envscrub):
    envscrub.setenv("MINI_ORK_ROOT", str(tmp_path))
    rc = ap.main(["--task-class", "reviewer", "--target", "prompts/reviewer.md"])
    assert rc == 0
    out = capsys.readouterr().out
    assert out.startswith(
        "=== mini-ork apply ===\n"
        "    task_class: reviewer\n"
        "    target:     prompts/reviewer.md\n"
        "    target_kind:prompt_file\n"
        "    scorer:     probe\n"
        "    apply_enabled: 0\n"
        "    dry_run:    0\n"
        "\n"
    )
    summary = json.loads(out.strip().splitlines()[-1])
    assert summary["decision"] == "no_candidate"
    # --enable / --dry-run env flow: header reflects the exported values
    rc = ap.main(["--task-class", "reviewer", "--target", "prompts/reviewer.md",
                  "--enable", "--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "    apply_enabled: 1\n" in out
    assert "    dry_run:    1\n" in out


# ── 12b. Probe scorer (#18): frozen probe set, two arms, vetted gate ─────────

def _probe_fixture(tmp_path, monkeypatch):
    """A fake recipe with a frozen probe set; probe_scorer._ROOT pinned to it."""
    from mini_ork.learning import probe_scorer as ps
    recipe = tmp_path / "recipes" / "obs-smoke"
    (recipe / "prompts").mkdir(parents=True)
    (recipe / "prompts" / "tiny-researcher.md").write_text("BASE PROMPT\n")
    for i in (1, 2, 3):  # 3 probes; default cap keeps 2
        (recipe / "probes").mkdir(exist_ok=True)
        (recipe / "probes" / f"probe-{i}.md").write_text(f"probe {i}\n")
    monkeypatch.setattr(ps, "_ROOT", str(tmp_path))
    return ps, recipe


def test_probe_scorer_no_probes_returns_none(tmp_path, monkeypatch):
    ps, _ = _probe_fixture(tmp_path, monkeypatch)
    shutil.rmtree(tmp_path / "recipes" / "obs-smoke" / "probes")
    assert ps.probe_score("obs_smoke", "prompts/tiny-researcher.md", "d") is None


def test_probe_scorer_missing_directive_or_target_returns_none(tmp_path, monkeypatch):
    ps, _ = _probe_fixture(tmp_path, monkeypatch)
    assert ps.probe_score("obs_smoke", "", "d") is None
    assert ps.probe_score("obs_smoke", "prompts/tiny-researcher.md", "") is None


def test_probe_scorer_two_arms_vectors_and_cleanup(tmp_path, monkeypatch):
    ps, recipe = _probe_fixture(tmp_path, monkeypatch)
    launches = []
    outcomes = {}

    def fake_launch(recipe_name, kickoff, target_cwd=None):
        arm = "cand" if "__probe_" in recipe_name and recipe_name.endswith("_1") else "base"
        rid = f"run-{arm}-{len(launches)}"
        # Prove the candidate arm actually carries the directive block.
        mut = tmp_path / "recipes" / recipe_name / "prompts" / "tiny-researcher.md"
        if arm == "cand":
            assert "<!-- applied:gradient_records:gr-9 -->" in mut.read_text()
        else:
            assert mut.read_text() == "BASE PROMPT\n"
        assert not (tmp_path / "recipes" / recipe_name / "probes").exists()
        # obs-smoke's probes declare no fixture: they write only into their own
        # run dir, so there is no target to hand over.
        assert target_cwd is None
        launches.append((recipe_name, kickoff, arm))
        return f"mini_ork_result={{\"run_id\": \"{rid}\"}}\n", rid, 0.01

    def fake_outcome(run_id):
        return outcomes.get(run_id, 1.0)

    monkeypatch.setattr(ps, "_launch_run", fake_launch)
    monkeypatch.setattr(ps, "_run_outcome", fake_outcome)
    outcomes.update({"run-base-0": 1.0, "run-base-2": 1.0,
                     "run-cand-1": 0.0, "run-cand-3": 1.0})

    out = ps.probe_score("obs_smoke", "prompts/tiny-researcher.md",
                         "emit an action ledger",
                         source_ref="gradient_records:gr-9", context="sig")
    assert out["n"] == 2  # 3 probes on disk, cap 2
    assert out["before"] == 1.0
    assert out["after"] == 0.5
    pertask = json.loads(out["pertask_json"])
    assert pertask == {"before": [1, 1], "after": [0, 1],
                       "ids": ["probe-1.md", "probe-2.md"]}
    assert len(out["runs"]) == 4
    assert out["cost_usd"] == pytest.approx(0.04)
    # Both arms ran per probe, base first.
    arms = [(probe.rsplit("/", 1)[-1], arm) for _r, probe, arm in launches]
    assert arms == [("probe-1.md", "base"), ("probe-1.md", "cand"),
                    ("probe-2.md", "base"), ("probe-2.md", "cand")]
    # Temp recipes removed; only the original recipe remains.
    assert [p.name for p in (tmp_path / "recipes").iterdir()] == ["obs-smoke"]


def test_probe_scorer_absolute_target_lands_in_temp_copy(tmp_path, monkeypatch):
    """auto_sweep passes ABSOLUTE target paths: the directive must land in
    the TEMP candidate recipe, never the original — a quarantine must leave
    the live recipe untouched."""
    ps, recipe = _probe_fixture(tmp_path, monkeypatch)
    seen = {}

    def fake_launch(recipe_name, kickoff, target_cwd=None):
        mut = tmp_path / "recipes" / recipe_name / "prompts" / "tiny-researcher.md"
        if "__probe_" in recipe_name and recipe_name.endswith("_1"):
            seen["cand"] = mut.read_text()
        return f'mini_ork_result={{"run_id": "r-{recipe_name}"}}\n', f"r-{recipe_name}", 0.0

    monkeypatch.setattr(ps, "_launch_run", fake_launch)
    monkeypatch.setattr(ps, "_run_outcome", lambda rid: 1.0)
    abs_target = str(tmp_path / "recipes" / "obs-smoke" / "prompts" / "tiny-researcher.md")
    out = ps.probe_score("obs_smoke", abs_target, "directive text",
                         source_ref="gradient_records:gr-x")
    assert out["n"] == 2 and out["after"] == 1.0
    assert "- Directive: directive text" in seen["cand"]
    assert (recipe / "prompts" / "tiny-researcher.md").read_text() == "BASE PROMPT\n"
    # An absolute target OUTSIDE the recipe dir measures nothing → None.
    assert ps.probe_score("obs_smoke", str(tmp_path / "elsewhere" / "x.md"), "d") is None


def test_probe_scorer_gives_each_arm_a_fresh_target_copy(tmp_path, monkeypatch):
    """A file-editing recipe needs somewhere to edit that is not the framework
    tree, and each (probe, arm) launch needs its OWN copy.

    Without a target, both arms run in MINI_ORK_ROOT — refused by
    providers.cwd_guard, and shared between arms even where it is not, so the
    candidate would start from the baseline's edits. Fresh per (probe, arm),
    not per arm: the recipe arms are materialized once and reused across the
    whole probe loop, so one probe's edits would otherwise be visible to the
    next launch.
    """
    ps, recipe = _probe_fixture(tmp_path, monkeypatch)
    # Give both probes a fixture: probes/fixtures/<stem>/
    for i in (1, 2, 3):
        fx = recipe / "probes" / "fixtures" / f"probe-{i}"
        fx.mkdir(parents=True)
        (fx / "tally.py").write_text("ORIGINAL\n")

    targets = []

    def fake_launch(recipe_name, kickoff, target_cwd=None):
        assert target_cwd is not None, "fixture probe must hand over a target"
        # Hand each arm a mutated copy, then prove the next launch is untouched.
        p = Path(target_cwd) / "tally.py"
        assert p.read_text() == "ORIGINAL\n"
        p.write_text(f"MUTATED BY {recipe_name}\n")
        targets.append(target_cwd)
        rid = f"run-{len(targets)}"
        return f'mini_ork_result={{"run_id": "{rid}"}}\n', rid, 0.0

    monkeypatch.setattr(ps, "_launch_run", fake_launch)
    monkeypatch.setattr(ps, "_run_outcome", lambda rid: 1.0)
    out = ps.probe_score("obs_smoke", "prompts/tiny-researcher.md", "d")

    # 2 probes × 2 arms == 4 launches, each in its own directory.
    assert len(targets) == 4
    assert len(set(targets)) == 4
    assert out["n"] == 2
    # Every scratch target is removed on the way out.
    assert not any(Path(t).exists() for t in targets)
    # The frozen fixture is untouched.
    assert (recipe / "probes" / "fixtures" / "probe-1" / "tally.py").read_text() == "ORIGINAL\n"


def test_probe_scorer_budget_zero_truncates_to_nothing(tmp_path, monkeypatch, envscrub):
    ps, _ = _probe_fixture(tmp_path, monkeypatch)
    envscrub.setenv("MO_APPLY_PROBE_BUDGET_USD", "0")
    monkeypatch.setattr(ps, "_launch_run", lambda *a: pytest.fail("must not launch"))
    out = ps.probe_score("obs_smoke", "prompts/tiny-researcher.md", "d")
    assert out["n"] == 0 and out["truncated_by_budget"] is True
    assert [p.name for p in (tmp_path / "recipes").iterdir()] == ["obs-smoke"]


def test_run_id_from_stdout_sink_and_fallback():
    from mini_ork.learning import probe_scorer as ps
    sink = 'noise\nmini_ork_result={"run_id": "run-123-456", "status": "published"}\n'
    assert ps._run_id_from_stdout(sink) == "run-123-456"
    assert ps._run_id_from_stdout("banner run-999-1 mid run-999-2 end") == "run-999-2"
    assert ps._run_id_from_stdout("nothing here") is None
    assert ps._run_id_from_stdout('mini_ork_result=not-json') is None


def test_launch_run_scrubs_run_scoped_env(tmp_path, monkeypatch, envscrub):
    """Run-scoped env must never leak into the nested probe launch: an
    inherited MINI_ORK_RUN_ID makes the nested run REUSE the parent's
    task_runs row (outcome attribution reads the wrong status), and an
    inherited MO_AUTO_APPLY fires a sweep inside every probe run —
    unbounded recursion."""
    from mini_ork.learning import probe_scorer as ps
    captured = {}

    class FakeProc:
        returncode = 0

        def __init__(self, _cmd, **kw):
            captured.update(kw)
            self.pid = 4242

        def poll(self):
            return 0  # the arm's process is already gone

        def communicate(self, timeout=None):
            return ('mini_ork_result={"run_id": "run-1-1"}\n', "")

    monkeypatch.setattr(ps.subprocess, "Popen", FakeProc)
    monkeypatch.setattr(ps, "_ROOT", str(tmp_path))
    for var, val in (("MINI_ORK_RUN_ID", "run-parent-99"),
                     ("MINI_ORK_TASK_RUN_ID", "run-parent-99"),
                     ("MINI_ORK_RUN_DIR", "/tmp/parent"),
                     ("MINI_ORK_PLAN_PATH", "/tmp/parent/plan.json"),
                     ("MINI_ORK_WORKFLOW", "/tmp/parent/workflow.yaml"),
                     ("MINI_ORK_RECIPE", "parent-recipe"),
                     ("MO_AUTO_APPLY", "1")):
        envscrub.setenv(var, val)
    envscrub.setenv("MINI_ORK_DB", str(tmp_path / "no.db"))  # _run_cost → 0.0
    stdout, run_id, cost = ps._launch_run("obs_smoke", "probe-1.md")
    assert run_id == "run-1-1"
    assert cost == 0.0
    env = captured["env"]
    for leak in ("MINI_ORK_RUN_ID", "MINI_ORK_TASK_RUN_ID", "MINI_ORK_RUN_DIR",
                 "MINI_ORK_PLAN_PATH", "MINI_ORK_WORKFLOW", "MINI_ORK_RECIPE",
                 "MO_AUTO_APPLY"):
        assert leak not in env, f"{leak} leaked into the probe launch env"
    assert env["MINI_ORK_ROOT"] == str(tmp_path)
    assert env["MINI_ORK_NONINTERACTIVE"] == "1"
    assert env["MO_STATIC_RECIPE_PLAN"] == "1"  # frozen planning, no LLM planner
    assert captured["cwd"] == str(tmp_path)


def _seed_gradient(db_path, gradient_id="gr-1", target="agent.reviewer.prompt",
                   change="be more specific", confidence=0.42, task_class="reviewer",
                   signal="rubric"):
    con = sqlite3.connect(db_path)
    con.execute(
        "INSERT INTO gradient_records"
        " (gradient_id, target, signal, suggested_change, evidence, confidence, created_at, task_class)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (gradient_id, target, signal, change, "[]", confidence, 100, task_class),
    )
    con.commit()
    con.close()


def test_apply_run_probe_no_probe_set_never_promotes(db, tmp_path, capsys, envscrub, monkeypatch):
    """probe scorer with nothing measured must NOT ride the delta>=0 gate."""
    from mini_ork.learning import probe_scorer as ps
    _seed_gradient(db, target="prompts/reviewer.md", task_class="reviewer")
    target = tmp_path / "reviewer.md"
    target.write_text("ORIGINAL PROMPT\n")
    envscrub.setenv("MO_APPLY_SCORER", "probe")
    envscrub.setenv("MO_APPLY_ENABLED", "1")
    monkeypatch.setattr(ps, "probe_score", lambda *a, **k: None)
    rc = ap.apply_run("reviewer", "prompt_file", "prompts/reviewer.md",
                      str(target), db=db)
    assert rc == 0
    summary = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert summary["decision"] == "quarantined"
    assert summary["version_id"] == ""
    assert target.read_text() == "ORIGINAL PROMPT\n"
    att = _rows(db, "apply_attempts")[0]
    assert att["decision"] == "quarantined"
    assert "measured nothing" in att["rationale"]


def test_apply_run_probe_vetted_promote_without_unvetted(db, tmp_path, capsys, envscrub, monkeypatch):
    """A probe-measured no-regression candidate promotes VETTED — no
    MO_APPLY_UNVETTED needed (that flag exists only for fabricated scorers)."""
    from mini_ork.learning import probe_scorer as ps
    _seed_gradient(db, target="prompts/reviewer.md", task_class="reviewer",
                   change="improve wording")
    target = tmp_path / "reviewer.md"
    target.write_text("ORIGINAL PROMPT\n")
    envscrub.setenv("MO_APPLY_SCORER", "probe")
    envscrub.setenv("MO_APPLY_ENABLED", "1")
    # A measured GAIN (one probe recovered, none regressed) — the only shape
    # that promotes now that the human gate is gone: see
    # test_evaluate_gate_measured_path_requires_strict_improvement.
    probe_out = {"before": 0.5, "after": 1.0, "n": 2,
                 "pertask_json": json.dumps({"before": [0, 1], "after": [1, 1],
                                             "ids": ["probe-1.md", "probe-2.md"]}),
                 "runs": [], "cost_usd": 0.08}
    monkeypatch.setattr(ps, "probe_score", lambda *a, **k: dict(probe_out))
    rc = ap.apply_run("reviewer", "prompt_file", "prompts/reviewer.md",
                      str(target), db=db)
    assert rc == 0
    summary = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert summary["decision"] == "promoted"
    assert summary["version_id"]  # file + version_registry written
    applied = target.read_text()
    assert applied.startswith("ORIGINAL PROMPT\n")
    assert "- Directive: improve wording" in applied
    promo = _rows(db, "promotion_records")[0]
    assert promo["decision"] == "promoted"
    assert "UNVETTED" not in promo["rationale"]
    assert "probe: n=2" in promo["rationale"]
    assert "cost=$0.08" in promo["rationale"]
    att = _rows(db, "apply_attempts")[0]
    assert att["utility_before"] == pytest.approx(0.5)
    assert att["utility_after"] == pytest.approx(1.0)


def test_apply_run_probe_regression_quarantines(db, tmp_path, capsys, envscrub, monkeypatch):
    from mini_ork.learning import probe_scorer as ps
    _seed_gradient(db, target="prompts/reviewer.md", task_class="reviewer",
                   change="break it")
    target = tmp_path / "reviewer.md"
    target.write_text("ORIGINAL PROMPT\n")
    envscrub.setenv("MO_APPLY_SCORER", "probe")
    envscrub.setenv("MO_APPLY_ENABLED", "1")
    probe_out = {"before": 1.0, "after": 0.0, "n": 2,
                 "pertask_json": json.dumps({"before": [1, 1], "after": [0, 0],
                                             "ids": ["probe-1.md", "probe-2.md"]}),
                 "runs": [], "cost_usd": 0.08}
    monkeypatch.setattr(ps, "probe_score", lambda *a, **k: dict(probe_out))
    rc = ap.apply_run("reviewer", "prompt_file", "prompts/reviewer.md",
                      str(target), db=db)
    assert rc == 0
    summary = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert summary["decision"] == "quarantined"
    assert target.read_text() == "ORIGINAL PROMPT\n"
    assert _rows(db, "apply_attempts")[0]["decision"] == "quarantined"


def test_apply_run_probe_dead_arms_refuse_promote(db, tmp_path, capsys, envscrub, monkeypatch):
    """Live-smoke finding (2026-09-16): every probe launch failing for an
    infra reason yields n>0 with 0.0-vs-0.0 utilities — the scalar gate would
    read that as non-regression and promote on a dead harness. Both arms
    entirely dead must refuse; only 0→positive stays promotable."""
    from mini_ork.learning import probe_scorer as ps
    _seed_gradient(db, target="prompts/reviewer.md", task_class="reviewer")
    target = tmp_path / "reviewer.md"
    target.write_text("ORIGINAL PROMPT\n")
    envscrub.setenv("MO_APPLY_SCORER", "probe")
    envscrub.setenv("MO_APPLY_ENABLED", "1")
    dead = {"before": 0.0, "after": 0.0, "n": 2,
            "pertask_json": json.dumps({"before": [0, 0], "after": [0, 0],
                                        "ids": ["probe-1.md", "probe-2.md"]}),
            "runs": [], "cost_usd": 0.0}
    monkeypatch.setattr(ps, "probe_score", lambda *a, **k: dict(dead))
    rc = ap.apply_run("reviewer", "prompt_file", "prompts/reviewer.md",
                      str(target), db=db)
    assert rc == 0
    summary = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert summary["decision"] == "quarantined"
    assert summary["version_id"] == ""
    assert target.read_text() == "ORIGINAL PROMPT\n"
    att = _rows(db, "apply_attempts")[0]
    assert "dead harness" in att["rationale"]

    # 0 → positive is a genuine improvement and stays promotable. A FRESH
    # directive is needed: the dead-harness refusal is a recorded verdict, so
    # edit memory would reject a re-proposal of gr-1 outright (that is the
    # spend containment — an unmeasured attempt is not re-run forever). Seed
    # a higher-confidence one so pick_candidate selects it over gr-1.
    _seed_gradient(db, gradient_id="gr-2", target="prompts/reviewer.md",
                   change="revive idea", confidence=0.9, task_class="reviewer")
    revive = {"before": 0.0, "after": 0.5, "n": 2,
              "pertask_json": json.dumps({"before": [0, 0], "after": [1, 0],
                                          "ids": ["probe-1.md", "probe-2.md"]}),
              "runs": [], "cost_usd": 0.1}
    monkeypatch.setattr(ps, "probe_score", lambda *a, **k: dict(revive))
    ap.apply_run("reviewer", "prompt_file", "prompts/reviewer.md",
                 str(target), db=db)
    summary = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert summary["decision"] == "promoted"


def test_apply_run_launch_failure_is_caught_not_fatal(db, tmp_path, capsys, envscrub, monkeypatch):
    from mini_ork.learning import probe_scorer as ps
    _seed_gradient(db, target="prompts/reviewer.md", task_class="reviewer")
    envscrub.setenv("MO_APPLY_SCORER", "probe")
    envscrub.setenv("MO_APPLY_ENABLED", "1")

    def boom(*a, **k):
        raise RuntimeError("probe launch reported no run_id (rc=1)")

    monkeypatch.setattr(ps, "probe_score", boom)
    rc = ap.apply_run("reviewer", "prompt_file", "prompts/reviewer.md",
                      str(tmp_path / "reviewer.md"), db=db)
    assert rc == 0
    summary = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert summary["decision"] == "quarantined"


# ── 12c. Edit memory (#19 prereq) + auto_sweep (#19) + execute wiring ────────

def test_edit_memory_never_reproposes_failed_directive(db, tmp_path, capsys, envscrub):
    _seed_gradient(db, target="prompts/reviewer.md", task_class="reviewer")
    target = tmp_path / "reviewer.md"
    target.write_text("ORIGINAL PROMPT\n")
    envscrub.setenv("MO_APPLY_ENABLED", "1")
    envscrub.setenv("MO_APPLY_SCORER", "mock")
    envscrub.setenv("MO_APPLY_MOCK_BASELINE", "0.5")
    envscrub.setenv("MO_APPLY_FORCE_REGRESSION", "1")
    args = ("reviewer", "prompt_file", "prompts/reviewer.md", str(target))
    # 1st attempt: gate quarantines the regression.
    ap.apply_run(*args, db=db)
    summary = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert summary["decision"] == "quarantined"
    # 2nd attempt with the SAME source: edit memory rejects instantly —
    # no re-score, no re-spend (GRASP/GRAO: never re-propose a failed edit).
    envscrub.setenv("MO_APPLY_FORCE_REGRESSION", "0")
    ap.apply_run(*args, db=db)
    summary = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert summary["decision"] == "rejected"
    attempts = _rows(db, "apply_attempts")
    assert [a["decision"] for a in attempts] == ["quarantined", "rejected"]
    assert "2604.20714" in attempts[1]["rationale"]
    assert attempts[1]["utility_after"] is None  # nothing was re-measured
    # A DIFFERENT source on the same target is unaffected.
    _seed_gradient(db, gradient_id="gr-2", target="prompts/reviewer.md",
                   change="another idea", confidence=0.9)
    ap.apply_run(*args, db=db)
    summary = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert summary["decision"] != "rejected"


def test_auto_sweep_top1_per_target_through_gate(db, tmp_path, capsys, envscrub, monkeypatch):
    recipe = tmp_path / "recipes" / "obs-smoke" / "prompts"
    recipe.mkdir(parents=True)
    (recipe / "tiny-researcher.md").write_text("R\n")
    (recipe / "tiny-reviewer.md").write_text("V\n")
    monkeypatch.setattr(ap, "_resolve_root", lambda: str(tmp_path))
    _seed_gradient(db, gradient_id="gr-hi", target="agent.tiny-researcher.prompt",
                   change="directive one", confidence=0.9, task_class="obs_smoke")
    _seed_gradient(db, gradient_id="gr-lo", target="agent.tiny-researcher.prompt",
                   change="directive one-dup", confidence=0.5, task_class="obs_smoke")
    _seed_gradient(db, gradient_id="gr-rev", target="agent.tiny-reviewer.prompt",
                   change="directive two", confidence=0.8, task_class="obs_smoke")
    _seed_gradient(db, gradient_id="gr-x", target="cross_class:whatever",
                   change="cross-class", confidence=0.99, task_class="obs_smoke")
    envscrub.setenv("MO_APPLY_ENABLED", "1")
    # Pin the fabricating scorer explicitly. The default is now `probe`, and
    # `recipes/obs-smoke/probes/` exists in the real tree, so leaving it unset
    # would launch real held-out runs from a unit test.
    envscrub.setenv("MO_APPLY_SCORER", "mock")
    results = ap.auto_sweep("obs_smoke", db=db, max_targets=2)
    # top-1 per target, confidence-ordered, cross_class excluded
    assert [r["target"] for r in results] == ["agent.tiny-researcher.prompt",
                                              "agent.tiny-reviewer.prompt"]
    # mock fabricates utility (after≈0.55 against a 0.0 baseline), so the
    # fabrication guard refuses the promote: quarantined, never promoted.
    assert all(r["decision"] == "quarantined" for r in results)
    attempts = _rows(db, "apply_attempts")
    assert len(attempts) == 2
    assert {a["source_id"] for a in attempts} == {"gr-hi", "gr-rev"}
    # nothing written (gate refused)
    assert (recipe / "tiny-researcher.md").read_text() == "R\n"
    capsys.readouterr()  # drain


def test_auto_sweep_no_recipe_skips(db, tmp_path, envscrub, monkeypatch):
    monkeypatch.setattr(ap, "_resolve_root", lambda: str(tmp_path))
    _seed_gradient(db, target="agent.ghost.prompt", task_class="obs_smoke")
    results = ap.auto_sweep("obs_smoke", db=db)
    assert results == [{"target": "agent.ghost.prompt", "skipped": "no prompt file"}]


def test_post_run_learning_auto_apply_wiring(db, envscrub, monkeypatch):
    from mini_ork.cli import execute as ex
    calls = []
    monkeypatch.setattr(ap, "auto_sweep",
                        lambda tc, db=None, max_targets=None: calls.append(tc) or [])
    envscrub.setenv("MO_AUTO_APPLY", "1")
    envscrub.setenv("MO_APPLY_ENABLED", "1")
    ex._post_run_learning(db, "/nonexistent-run-dir", "run-x", "obs_smoke")
    assert calls == ["obs_smoke"]
    # master gate off → no sweep even with MO_AUTO_APPLY=1
    envscrub.delenv("MO_APPLY_ENABLED")
    ex._post_run_learning(db, "/nonexistent-run-dir", "run-y", "obs_smoke")
    assert calls == ["obs_smoke"]


# ── 13. Native integration through the dispatcher ────────────────────────────

def test_apply_is_native_in_dispatcher(tmp_path, envscrub):
    assert "apply" in cli_main._NATIVE_SUBS
    assert not hasattr(cli_main, "_EXEC_SUBS"), "apply must dispatch natively — the bash trampoline set is gone"

    handler = cli_main.SUBCOMMAND_REGISTRY["apply"]
    # The handler must be a native-module handler (python -m mini_ork.cli.apply),
    # not the retired bin/mini-ork-apply bash trampoline.
    assert not hasattr(cli_main, "_bash_entrypoint_handler")
    envscrub.setenv("MINI_ORK_DB", str(tmp_path / "state.db"))
    rc = handler(["--help"], str(REPO))
    assert rc == 0


def test_module_invocation_help():
    """`python -m mini_ork.cli.apply --help` runs the ported module (rc 0)."""
    run = subprocess.run(
        [sys.executable, "-m", "mini_ork.cli.apply", "--help"],
        capture_output=True, text=True, cwd=str(REPO), check=False,
    )
    assert run.returncode == 0
    assert run.stdout == ap.help_text()
