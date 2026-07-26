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
  7. evaluate_gate: human-approval override
  8. apply_run: no_candidate path (audit row + summary line)
  9. apply_run: full dry-run promote path (candidate + promotion + attempt rows,
     no file write while MO_APPLY_ENABLED is off)
 10. apply_run: enabled promote rewrites the target file + version_registry row
 11. apply_run: forced regression quarantines (file untouched)
 12. CLI: --help / usage errors / missing-flag-value exit codes
 13. Native integration: apply is in _NATIVE_SUBS, native dispatch (_EXEC_SUBS deleted), and the
     SUBCOMMAND_REGISTRY handler dispatches ``python -m mini_ork.cli.apply``.
"""
from __future__ import annotations

import json
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
    "MO_APPLY_NONREGRESSION_DELTA", "MO_APPLY_MIN_EXAMPLES",
    "MO_APPLY_REGRESSION_TOLERANCE", "MO_APPLY_PERTASK_JSON",
    "MO_APPLY_MOCK_BASELINE", "MO_APPLY_MOCK_DELTA",
    "MO_APPLY_FORCE_REGRESSION", "MINI_ORK_REQUIRE_HUMAN_APPROVAL",
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


def test_evaluate_gate_human_approval_override(db, envscrub):
    envscrub.setenv("MINI_ORK_REQUIRE_HUMAN_APPROVAL", "true")
    out = json.loads(ap.evaluate_gate("cand-test", 0.5, 0.9))
    assert out["decision"] == "pending_human_approval"
    assert out["needs_human"] is True
    assert out["rationale"] == "human approval required (MINI_ORK_REQUIRE_HUMAN_APPROVAL=true)"


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

def test_apply_run_dry_run_promote_writes_no_file(db, tmp_path, capsys):
    _seed_pattern(db)
    target = tmp_path / "reviewer.md"
    target.write_text("ORIGINAL PROMPT\n")
    # Master gate OFF (default) → stage + score + audit, but never write.
    rc = ap.apply_run("reviewer", "prompt_file", "prompts/reviewer.md",
                      str(target), db=db)
    assert rc == 0
    summary = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert summary["decision"] == "promoted"  # mock beats the 0.0 baseline
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
    assert promos[0]["decision"] == "promoted"
    assert promos[0]["decided_by"] == "gate"

    attempts = _rows(db, "apply_attempts")
    assert len(attempts) == 1
    assert attempts[0]["decision"] == "promoted"
    assert attempts[0]["dry_run"] == 0
    assert attempts[0]["apply_enabled"] == 0


def test_apply_run_enabled_rewrites_file_and_registers_version(db, tmp_path, capsys, envscrub):
    _seed_pattern(db)
    target = tmp_path / "reviewer.md"
    target.write_text("ORIGINAL PROMPT\n")
    envscrub.setenv("MO_APPLY_ENABLED", "1")
    rc = ap.apply_run("reviewer", "prompt_file", "prompts/reviewer.md",
                      str(target), db=db)
    assert rc == 0
    summary = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert summary["decision"] == "promoted"
    # printf '%s\n' semantics: content + trailing newline
    assert target.read_text() == "improve prompts/reviewer.md wording\n"
    # rollback snapshot next to the target (bash: <file>.apply-rollback-$$)
    rollbacks = list(tmp_path.glob("reviewer.md.apply-rollback-*"))
    assert len(rollbacks) == 1
    assert rollbacks[0].read_text() == "ORIGINAL PROMPT\n"
    # version_registry row exists and the summary carries its id
    versions = _rows(db, "version_registry")
    assert len(versions) == 1
    assert versions[0]["kind"] == "agent"
    assert versions[0]["name"] == str(target)
    payload = json.loads(versions[0]["payload"])
    assert payload["rollback_hash"]
    assert payload["candidate_id"] == summary["candidate_id"]
    assert summary["version_id"] == versions[0]["version_id"]
    assert _rows(db, "apply_attempts")[0]["apply_enabled"] == 1


def test_apply_run_forced_regression_quarantines(db, tmp_path, capsys, envscrub):
    _seed_pattern(db)
    target = tmp_path / "reviewer.md"
    target.write_text("ORIGINAL PROMPT\n")
    envscrub.setenv("MO_APPLY_ENABLED", "1")
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
        "    scorer:     mock\n"
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
