"""ECOSYSTEM §6 · K11: a post-run verify failure after publish is marked.

Two mechanisms are covered here:

* ``mini_ork.cli.verify`` now persists its result to ``<run_dir>/verify-result.json``
  and, when the verdict is ``fail``/``partial`` against a run that is already
  ``published``, escalates that run (``verdict='ESCALATE'``, status untouched),
  records a note, writes ``post-verify-after-publish.json`` and enqueues one
  non-blocking oversight item.
* ``mini_ork.gates.step_rules.rule_named_test_path_exists`` expands the
  variables it can resolve, ignores words it cannot read (unset variables,
  ``<placeholders>``, globs) and resolves relative paths under the run dir.

Hermetic: tmp dirs, a template DB from ``init_db``, a faked ``gate_run_all``.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

import mini_ork.gates.step_rules as sr
from mini_ork.cli import verify as verify_cli
from mini_ork.stores.migrate import init_db

REPO = str(Path(__file__).resolve().parents[2])

#: The kickoff's autouse env scrub, so an outer mini-ork run's identity can
#: never leak into these tests.
_CLEAN_ENV = [
    "MINI_ORK_RUN_DIR", "MINI_ORK_RUN_ID", "MINI_ORK_TASK_RUN_ID",
    "MINI_ORK_DB", "MINI_ORK_HOME", "MINI_ORK_ROOT", "MINI_ORK_PLAN_PATH",
    "MINI_ORK_TASK_CLASS", "MINI_ORK_DRY_RUN", "MINI_ORK_RECIPE",
    "MINI_ORK_TARGET_REPO", "MO_TARGET_CWD", "MO_MUTATION_REPORT",
    "MO_MUTATION_TEST_CMD", "MO_VERIFY_RERUN", "MO_STEP_RULES", "RUN_ID",
]

# Copied from ``tests/unit/test_verify_partial_trace.py`` (the V6 bullet).
_TRACE_DDL = """
CREATE TABLE IF NOT EXISTS execution_traces (
  trace_id              TEXT PRIMARY KEY,
  run_id                INTEGER,
  workflow_version_id   TEXT,
  agent_version_id      TEXT NOT NULL DEFAULT '',
  task_class            TEXT NOT NULL,
  prompt_version_hash   TEXT NOT NULL DEFAULT '',
  context_bundle_hash   TEXT NOT NULL DEFAULT '',
  tool_calls            TEXT NOT NULL DEFAULT '[]',
  files_read            TEXT NOT NULL DEFAULT '[]',
  files_written         TEXT NOT NULL DEFAULT '[]',
  verifier_output       TEXT NOT NULL DEFAULT '{}',
  reviewer_verdict      TEXT,
  cost_usd              REAL NOT NULL DEFAULT 0.0,
  duration_ms           INTEGER NOT NULL DEFAULT 0,
  final_artifact_ref    TEXT,
  status                TEXT NOT NULL
                        CHECK (status IN ('success','failure','pending','running','vacuous','blocked')),
  created_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  process_reward        REAL DEFAULT NULL,
  objective_domain      TEXT NOT NULL DEFAULT 'code-delivery',
  segment               TEXT DEFAULT NULL,
  reward_primary_metric TEXT DEFAULT NULL,
  reward_direction      TEXT NOT NULL DEFAULT 'higher_is_better',
  reward_value          REAL DEFAULT NULL,
  reward_anchor         REAL DEFAULT NULL,
  reward_g              REAL DEFAULT NULL,
  reward_vector_json    TEXT DEFAULT NULL,
  reward_source         TEXT NOT NULL DEFAULT 'verifier@v1',
  validity              TEXT NOT NULL DEFAULT 'valid',
  code_region           TEXT DEFAULT NULL,
  route_source          TEXT DEFAULT NULL,
  route_explore         INTEGER DEFAULT NULL,
  route_score           REAL DEFAULT NULL,
  route_margin          REAL DEFAULT NULL,
  predicted_error       REAL DEFAULT NULL
)
"""


@pytest.fixture(autouse=True)
def _kickoff_env(monkeypatch, tmp_path):
    for key in _CLEAN_ENV:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("MO_MUTATION_ADVERSARY", "0")
    return tmp_path


@pytest.fixture(scope="module")
def template_db(tmp_path_factory):
    db = str(tmp_path_factory.mktemp("tpl") / "state.db")
    rc, _out, err = init_db(db=db, root=REPO)
    assert rc == 0, err
    return db


def _fresh_db(template_db, tmp_path):
    db = str(tmp_path / "state.db")
    src = sqlite3.connect(template_db)
    dst = sqlite3.connect(db)
    src.backup(dst)
    dst.close()
    src.close()
    return db


def _insert_run(db, status, verdict):
    con = sqlite3.connect(db)
    con.execute(
        "INSERT INTO task_runs (id, task_class, kickoff_path, status, verdict,"
        " created_at, updated_at) VALUES ('run-k11', 'k11', 'k.md', ?, ?, 1, 1)",
        (status, verdict),
    )
    con.commit()
    con.close()


class _Setup:
    """Verifiers under ``root/verifiers``, an existing ``rd``, a plan naming
    them, and a fresh DB with one ``task_runs`` row."""

    def __init__(self, tmp_path, template_db, verifiers, run_row):
        self.root = tmp_path / "root"
        (self.root / "verifiers").mkdir(parents=True)
        for name, lines in verifiers.items():
            (self.root / "verifiers" / name).write_text(
                "\n".join(lines) + "\n", encoding="utf-8")
        self.home = tmp_path / "home"
        self.home.mkdir()
        self.rd = tmp_path / "rd"
        self.rd.mkdir()
        self.plan = tmp_path / "plan.json"
        self.plan.write_text(json.dumps({
            "task_class": "k11",
            "artifact_contract": {"success_verifiers": list(verifiers)},
        }), encoding="utf-8")
        self.db = _fresh_db(template_db, tmp_path)
        if run_row is not None:
            _insert_run(self.db, *run_row)


def _verifier_scripts(which):
    scripts = {
        "vpass.py": ["print('pass evidence')"],
        "vfail.py": ["import sys", "print('fail evidence')", "sys.exit(1)"],
    }
    return {name: scripts[name] for name in which}


def _invoke(monkeypatch, capsys, setup, gates=None):
    """One ``verify_cli.main`` call, as the lifecycle makes it (no artifact)."""
    recorded = {}

    def fake_gate_run_all(db_path, task_class, ctx_json, mini_ork_root=None):
        recorded["ctx"] = json.loads(ctx_json)
        if gates is not None:
            return gates
        return {"any_fail": False, "gates": []}

    monkeypatch.setattr(verify_cli.gate_registry, "gate_run_all", fake_gate_run_all)
    monkeypatch.setenv("MINI_ORK_HOME", str(setup.home))
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(setup.rd))
    monkeypatch.setenv("MINI_ORK_RUN_ID", "run-k11")
    rc = verify_cli.main(["--plan", str(setup.plan), "--task-class", "k11"],
                         db=setup.db, root=str(setup.root))
    out = json.loads(capsys.readouterr().out)
    return out, rc, recorded


def _run(tmp_path, template_db, monkeypatch, capsys, which,
         gates=None, run_row=("published", "APPROVE")):
    setup = _Setup(tmp_path, template_db, _verifier_scripts(which), run_row)
    out, rc, recorded = _invoke(monkeypatch, capsys, setup, gates=gates)
    return out, rc, recorded, setup


def _row(db, sql, args=()):
    con = sqlite3.connect(db)
    try:
        return con.execute(sql, args).fetchall()
    finally:
        con.close()


def _result_rows(out):
    return [r if isinstance(r, dict) else json.loads(r) for r in out["results"]]


# ── V1–V10 ────────────────────────────────────────────────────────────────────


def test_v1_partial_after_publish_escalates(tmp_path, template_db, monkeypatch, capsys):
    out, rc, _recorded, setup = _run(tmp_path, template_db, monkeypatch, capsys,
                                     ["vpass.py", "vfail.py"])
    assert rc == 0
    assert out["verdict"] == "partial"

    rows = _row(setup.db, "SELECT status, verdict, notes FROM task_runs WHERE id='run-k11'")
    assert len(rows) == 1
    status, verdict, notes = rows[0]
    assert status == "published"
    assert verdict == "ESCALATE"
    assert "post-run verify partial after publish" in notes
    assert "vfail.py" in notes

    flag = json.loads((setup.rd / "post-verify-after-publish.json").read_text())
    assert flag["verdict"] == "partial"
    assert flag["trace_id"].startswith("tr-verify-")
    assert [r["verifier"] for r in flag["failing"]] == ["vfail.py"]
    assert flag["task_runs_verdict"] == "ESCALATE"
    assert isinstance(flag["inbox_id"], int)

    inbox = _row(setup.db, "SELECT gate_id, feature, phase, status, blocks_dispatch_for"
                           " FROM mo_inbox_gates")
    assert len(inbox) == 1
    gate_id, feature, phase, st, blocks = inbox[0]
    assert gate_id == "post_verify_after_publish"
    assert feature == "post-run verify failed after publish: run-k11"
    assert phase == "verify"
    assert st == "pending"
    assert blocks in ("", None)

    result = json.loads((setup.rd / "verify-result.json").read_text())
    assert result["verdict"] == "partial"
    assert result["trace_id"] == flag["trace_id"]


def test_v2_fail_after_publish_escalates(tmp_path, template_db, monkeypatch, capsys):
    out, rc, _recorded, setup = _run(tmp_path, template_db, monkeypatch, capsys,
                                     ["vfail.py"])
    assert rc == 1
    rows = _row(setup.db, "SELECT status, verdict FROM task_runs WHERE id='run-k11'")
    assert rows == [("published", "ESCALATE")]
    flag = json.loads((setup.rd / "post-verify-after-publish.json").read_text())
    assert flag["verdict"] == "fail"


def test_v3_twice_one_inbox_row_one_note(tmp_path, template_db, monkeypatch, capsys):
    setup = _Setup(tmp_path, template_db, _verifier_scripts(["vpass.py", "vfail.py"]),
                   ("published", "APPROVE"))
    out1, rc1, _ = _invoke(monkeypatch, capsys, setup)
    out2, rc2, _ = _invoke(monkeypatch, capsys, setup)
    assert rc1 == 0 and out1["verdict"] == "partial"
    assert rc2 == 0 and out2["verdict"] == "partial"
    assert len(_row(setup.db, "SELECT inbox_id FROM mo_inbox_gates")) == 1
    notes = _row(setup.db, "SELECT notes FROM task_runs WHERE id='run-k11'")[0][0]
    assert notes.count("post-run verify") == 1


def test_v4_not_published_no_flag(tmp_path, template_db, monkeypatch, capsys):
    out, rc, _recorded, setup = _run(tmp_path, template_db, monkeypatch, capsys,
                                     ["vpass.py", "vfail.py"],
                                     run_row=("executing", None))
    assert rc == 0
    assert out["verdict"] == "partial"
    assert _row(setup.db, "SELECT status, verdict, notes FROM task_runs WHERE id='run-k11'") \
        == [("executing", None, None)]
    assert not (setup.rd / "post-verify-after-publish.json").exists()
    assert len(_row(setup.db, "SELECT inbox_id FROM mo_inbox_gates")) == 0
    result = json.loads((setup.rd / "verify-result.json").read_text())
    assert result["verdict"] == "partial"


def test_v5_pass_writes_nothing(tmp_path, template_db, monkeypatch, capsys):
    out, rc, _recorded, setup = _run(tmp_path, template_db, monkeypatch, capsys,
                                     ["vpass.py"])
    assert rc == 0
    assert out["verdict"] == "pass"
    assert _row(setup.db, "SELECT status, verdict, notes FROM task_runs WHERE id='run-k11'") \
        == [("published", "APPROVE", None)]
    assert not (setup.rd / "post-verify-after-publish.json").exists()
    assert len(_row(setup.db, "SELECT inbox_id FROM mo_inbox_gates")) == 0


def test_v6_db_without_task_runs_does_not_raise(tmp_path, template_db, monkeypatch, capsys):
    setup = _Setup(tmp_path, template_db, _verifier_scripts(["vpass.py", "vfail.py"]),
                   run_row=None)
    con = sqlite3.connect(setup.db)
    for (name,) in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
            " AND name NOT LIKE 'sqlite_%' AND name != 'execution_traces'").fetchall():
        con.execute(f'DROP TABLE IF EXISTS "{name}"')
    con.commit()
    con.close()
    out, rc, _recorded = _invoke(monkeypatch, capsys, setup)
    assert rc == 0
    assert out["verdict"] == "partial"
    assert not (setup.rd / "post-verify-after-publish.json").exists()


def test_v7_crash_verdict_never_overwritten(tmp_path, template_db, monkeypatch, capsys):
    out, rc, _recorded, setup = _run(tmp_path, template_db, monkeypatch, capsys,
                                     ["vpass.py", "vfail.py"],
                                     run_row=("published", "CRASH"))
    assert rc == 0
    assert out["verdict"] == "partial"
    assert _row(setup.db, "SELECT status, verdict FROM task_runs WHERE id='run-k11'") \
        == [("published", "CRASH")]
    flag = json.loads((setup.rd / "post-verify-after-publish.json").read_text())
    assert flag["task_runs_verdict"] == "CRASH"
    assert len(_row(setup.db, "SELECT inbox_id FROM mo_inbox_gates")) == 1


def test_v8_gate_context_carries_run_dir(tmp_path, template_db, monkeypatch, capsys):
    _out, _rc, recorded, setup = _run(tmp_path, template_db, monkeypatch, capsys,
                                      ["vpass.py"])
    assert recorded["ctx"]["run_dir"] == str(setup.rd)


def test_v9_failing_gates_row_names_them(tmp_path, template_db, monkeypatch, capsys):
    out, rc, _recorded, _setup = _run(
        tmp_path, template_db, monkeypatch, capsys, ["vpass.py"],
        gates={"any_fail": True, "gates": [
            {"gate_id": "step-rules-gate", "verdict": "fail"},
            {"gate_id": "oracle-liveness", "verdict": "pass"},
        ]})
    assert rc == 0
    assert out["verdict"] == "partial"
    gates_rows = [r for r in _result_rows(out) if r.get("verifier") == "__gates__"]
    assert gates_rows == [{
        "verifier": "__gates__", "pass": False, "evidence_path": "gate_registry",
        "detail": "gate(s) failed: step-rules-gate",
    }]


def test_v10_default_gates_row_shape_unchanged(tmp_path, template_db, monkeypatch, capsys):
    out, _rc, _recorded, _setup = _run(tmp_path, template_db, monkeypatch, capsys,
                                       ["vpass.py"])
    gates_rows = [r for r in _result_rows(out) if r.get("verifier") == "__gates__"]
    assert len(gates_rows) == 1
    row = gates_rows[0]
    assert set(row) == {"verifier", "pass", "evidence_path"}
    assert row["pass"] is True


# ── M1 ────────────────────────────────────────────────────────────────────────


def test_m1_unflagged_verdicts_return_none(tmp_path, template_db):
    from mini_ork.verify import post_publish_flag

    rd = tmp_path / "rd"
    rd.mkdir()
    db = _fresh_db(template_db, tmp_path)
    _insert_run(db, "published", "APPROVE")
    assert post_publish_flag.mark(db, "run-k11", str(rd), "vacuous") is None
    assert post_publish_flag.mark(db, "run-k11", str(rd), "pass") is None
    assert _row(db, "SELECT status, verdict, notes FROM task_runs WHERE id='run-k11'") \
        == [("published", "APPROVE", None)]
    assert not (rd / "post-verify-after-publish.json").exists()
    assert len(_row(db, "SELECT inbox_id FROM mo_inbox_gates")) == 0


# ── Step rules ────────────────────────────────────────────────────────────────


def _dirs(tmp_path):
    ws = tmp_path / "ws"
    root = tmp_path / "root"
    rd = tmp_path / "rd"
    for d in (ws, root, rd):
        d.mkdir()
    return ws, root, rd


def _plan(tmp_path, *commands):
    path = tmp_path / "plan.json"
    path.write_text(json.dumps({
        "task_class": "code_fix",
        "verifier_contract": {
            "checks": [{"id": f"c{i + 1}", "command": cmd}
                       for i, cmd in enumerate(commands)]
        },
    }), encoding="utf-8")
    return str(path)


def test_step_rules_run_dir_var_expands(tmp_path):
    ws, root, rd = _dirs(tmp_path)
    (ws / "tool.py").write_text("", encoding="utf-8")
    (rd / "source-corpus.json").write_text("{}", encoding="utf-8")
    plan = _plan(tmp_path,
                 'python3 tool.py verify --corpus "$MINI_ORK_RUN_DIR/source-corpus.json"')
    assert sr.rule_named_test_path_exists(
        plan, str(ws), str(root), run_dir=str(rd)) == ("pass", "2 named path(s) resolved")


def test_step_rules_missing_expanded_path_names_the_file(tmp_path):
    ws, root, rd = _dirs(tmp_path)
    plan = _plan(tmp_path, 'jq -e . "${MINI_ORK_RUN_DIR}/source-corpus.json"')
    verdict, detail = sr.rule_named_test_path_exists(
        plan, str(ws), str(root), run_dir=str(rd))
    assert verdict == "fail"
    assert f"{rd}/source-corpus.json" in detail
    assert "MINI_ORK_RUN_DIR/" not in detail


def test_step_rules_root_var_expands(tmp_path):
    ws, root, rd = _dirs(tmp_path)
    vdir = root / "recipes" / "r1" / "verifiers"
    vdir.mkdir(parents=True)
    (vdir / "v.py").write_text("", encoding="utf-8")
    plan = _plan(tmp_path, "python3 ${MINI_ORK_ROOT}/recipes/r1/verifiers/v.py")
    assert sr.rule_named_test_path_exists(plan, str(ws), str(root))[0] == "pass"


def test_step_rules_placeholder_and_glob_defer(tmp_path):
    ws, root, rd = _dirs(tmp_path)
    plan = _plan(tmp_path, 'python3 -c "import sys" <run_dir>/*.sidecar.json')
    assert sr.rule_named_test_path_exists(
        plan, str(ws), str(root), run_dir=str(rd))[0] == "defer"


def test_step_rules_unset_var_defers(tmp_path):
    ws, root, rd = _dirs(tmp_path)
    plan = _plan(tmp_path, 'test -s ".mini-ork/runs/$RUN_ID/review.json"')
    assert sr.rule_named_test_path_exists(
        plan, str(ws), str(root), run_dir=str(rd))[0] == "defer"


def test_step_rules_recipe_glob_defers(tmp_path):
    ws, root, rd = _dirs(tmp_path)
    plan = _plan(tmp_path, "grep -rln path_globs recipes/*/task_class.yaml")
    assert sr.rule_named_test_path_exists(
        plan, str(ws), str(root), run_dir=str(rd))[0] == "defer"


def test_step_rules_redirect_and_flag_targets_are_outputs(tmp_path):
    ws, root, rd = _dirs(tmp_path)
    plan = _plan(tmp_path,
                 'python3 -m pytest -q > "$MINI_ORK_RUN_DIR/run.json"',
                 'python3 -m pytest -q --report="$MINI_ORK_RUN_DIR/r.json"')
    assert sr.rule_named_test_path_exists(
        plan, str(ws), str(root), run_dir=str(rd))[0] == "defer"


def test_step_rules_relative_token_resolves_under_run_dir(tmp_path):
    ws, root, rd = _dirs(tmp_path)
    (rd / "verifier-result-x.json").write_text("{}", encoding="utf-8")
    plan = _plan(tmp_path, "test -s verifier-result-x.json")
    assert sr.rule_named_test_path_exists(
        plan, str(ws), str(root), run_dir=str(rd))[0] == "pass"
    assert sr.rule_named_test_path_exists(plan, str(ws), str(root))[0] == "fail"


def test_step_rules_absent_named_test_fails_and_is_named(tmp_path):
    ws, root, rd = _dirs(tmp_path)
    plan = _plan(tmp_path, "python3 -m pytest tests/unit/test_ghost.py")
    verdict, detail = sr.rule_named_test_path_exists(plan, str(ws), str(root))
    assert verdict == "fail"
    assert "tests/unit/test_ghost.py" in detail


def test_step_rules_evaluate_passes_run_dir_from_context(tmp_path):
    ws, root, rd = _dirs(tmp_path)
    (ws / "tool.py").write_text("", encoding="utf-8")
    (rd / "source-corpus.json").write_text("{}", encoding="utf-8")
    plan = _plan(tmp_path,
                 'python3 tool.py verify --corpus "$MINI_ORK_RUN_DIR/source-corpus.json"')
    assert sr.evaluate(
        "",
        json.dumps({"plan_path": plan, "workspace": str(ws), "run_dir": str(rd)}),
        "",
        str(root),
    ) == "pass"
