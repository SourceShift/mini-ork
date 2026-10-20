"""Unit tests for RUN-BUGS RB-37 / RB-38 / RB-46 / RB-47.

Four small, independent hygiene fixes:

- RB-37: the planner repair loop overwrote the preserved first failing output
  (``plan-failure-<verdict>.raw.txt``) with each repaired reply, so the actual
  failure was lost. Repairs now land in ``plan-repair-<n>.raw.txt``.
- RB-38: a standalone ``mini-ork classify`` wrote a ``classified`` ``task_runs``
  row with no recipe/run dir (the board showed it live forever), and its
  ``ON CONFLICT`` could downgrade a live run's status. It now writes only from
  inside a run lifecycle and never touches an existing status.
- RB-46: the declared rollback strategy
  ``keep_judge_reports_discard_synthesis`` fell through to the generic
  "unhandled rollback_strategy" warning. It now keeps the judge-report outputs
  and removes the synthesis, reading the names from the recipe's artifact
  contract.
- RB-47: the epic-runner wave aggregator hard-coded ``"verdict":
  "in_progress"``, so a completed run still looked in progress.

Hermetic: tmp home/DB/run dirs, in-process seams, no network, no live DB.
"""
from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path

import yaml

from mini_ork.cli import classify, plan
from mini_ork.cli.execute_handlers import NodeDispatch, _handle_rollback

REPO = Path(__file__).resolve().parents[2]

_RUN_ENV_KEYS = (
    "MINI_ORK_RUN_ID",
    "MINI_ORK_RUN_DIR",
    "MINI_ORK_DB",
    "MINI_ORK_HOME",
    "MINI_ORK_ROOT",
    "MINI_ORK_RECIPE",
    "MINI_ORK_WORKFLOW",
    "MINI_ORK_TASK_CLASS",
    "MINI_ORK_DRY_RUN",
    "MINI_ORK_PROFILE_PATH",
    "MINI_ORK_PROFILE_GATE",
    "MO_GIVEN_PLAN",
    "MO_PLAN_ARTIFACT",
    "MO_PLAN_MAX_REPAIRS",
    "MO_PLAN_DETERMINISTIC_FALLBACK",
    "MO_STATIC_RECIPE_PLAN",
    "MO_FORCE_RECIPE_FALLBACK_PLAN",
    "MO_INJECT_LEARNINGS",
    "MO_AUTO_ANSWER_PROFILE",
    "MO_PROFILE_REQUIRE_ANSWERS",
    "MINI_ORK_ROLLBACK_KEEP_WORKTREE",
)


def _reset_run_env(monkeypatch, **setenv):
    for key in _RUN_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    for key, value in setenv.items():
        monkeypatch.setenv(key, value)


# ── RB-37: planner repair files ─────────────────────────────────────────────


def test_planner_repairs_get_their_own_files_and_keep_the_first_failure(
        tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    root = tmp_path / "root"  # bare root: built-in planner prompt
    root.mkdir()
    kickoff = tmp_path / "kickoff.md"
    kickoff.write_text("# kickoff\nfix the thing\n", encoding="utf-8")
    run_id = "run-rb37"

    outputs = [
        "FIRST ATTEMPT OUTPUT (not JSON at all)",
        "REPAIR ATTEMPT 1 OUTPUT (still not JSON)",
        "REPAIR ATTEMPT 2 OUTPUT (still not JSON)",
    ]
    calls: list[tuple[str, str]] = []

    def fake_dispatch(task_class, node, prompt):
        calls.append((task_class, node))
        return 0, outputs[len(calls) - 1]

    _reset_run_env(
        monkeypatch,
        MINI_ORK_HOME=str(home),
        MINI_ORK_DB=str(tmp_path / "state.db"),
        MINI_ORK_RUN_ID=run_id,
        MO_PLAN_MAX_REPAIRS="2",
        MO_INJECT_LEARNINGS="0",
    )

    plan.main([str(kickoff), "--task-class", "generic"],
              root=str(root), dispatch=fake_dispatch)

    assert len(calls) == 3

    run_dir = home / "runs" / run_id
    failures = sorted(run_dir.glob("plan-failure-*.raw.txt"))
    assert len(failures) == 1, [p.name for p in failures]
    assert failures[0].read_text(encoding="utf-8") == outputs[0], (
        "the preserved failure must stay the FIRST (unrepaired) output"
    )
    assert (run_dir / "plan-repair-1.raw.txt").read_text(encoding="utf-8") == outputs[1]
    assert (run_dir / "plan-repair-2.raw.txt").read_text(encoding="utf-8") == outputs[2]


# ── RB-38: classify only writes inside a run lifecycle ──────────────────────

_TASK_RUNS_DDL = """
CREATE TABLE IF NOT EXISTS task_runs (
    id TEXT PRIMARY KEY,
    task_class TEXT,
    recipe TEXT,
    workflow_version TEXT,
    kickoff_path TEXT,
    status TEXT,
    trace_id TEXT,
    created_at INTEGER,
    updated_at INTEGER
)
"""


def _make_home_and_db(tmp_path: Path):
    home = tmp_path / "home"
    home.mkdir()
    db_path = home / "state.db"
    con = sqlite3.connect(db_path)
    con.execute(_TASK_RUNS_DDL)
    con.commit()
    con.close()
    return home, db_path


def _kickoff(tmp_path: Path) -> str:
    kickoff = tmp_path / "kickoff.md"
    kickoff.write_text("# kickoff\nclassify me\n", encoding="utf-8")
    return str(kickoff)


def _rows(db_path: Path):
    con = sqlite3.connect(db_path)
    try:
        return con.execute("SELECT id, task_class, status FROM task_runs").fetchall()
    finally:
        con.close()


def test_standalone_classify_writes_no_row(tmp_path, monkeypatch, capsys):
    home, db_path = _make_home_and_db(tmp_path)
    _reset_run_env(
        monkeypatch,
        MINI_ORK_HOME=str(home),
        MINI_ORK_DB=str(db_path),
        MINI_ORK_ROOT=str(tmp_path),
    )

    rc = classify.main([_kickoff(tmp_path), "--task-class", "spec_class"])

    assert rc == 0
    assert "task_class=spec_class" in capsys.readouterr().out
    assert _rows(db_path) == []


def test_classify_run_id_without_run_dir_writes_no_row(tmp_path, monkeypatch):
    home, db_path = _make_home_and_db(tmp_path)
    _reset_run_env(
        monkeypatch,
        MINI_ORK_HOME=str(home),
        MINI_ORK_DB=str(db_path),
        MINI_ORK_ROOT=str(tmp_path),
        MINI_ORK_RUN_ID="run-never-created",
        MINI_ORK_RUN_DIR=str(home / "runs" / "run-never-created"),
    )

    rc = classify.main([_kickoff(tmp_path), "--task-class", "spec_class"])

    assert rc == 0
    assert _rows(db_path) == []


def test_classify_inside_run_records_class_but_leaves_status_alone(
        tmp_path, monkeypatch):
    home, db_path = _make_home_and_db(tmp_path)
    run_id = "run-rb38"
    run_dir = home / "runs" / run_id
    run_dir.mkdir(parents=True)
    con = sqlite3.connect(db_path)
    con.execute(
        "INSERT INTO task_runs (id, task_class, status, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (run_id, "old_class", "executing", 1, 1),
    )
    con.commit()
    con.close()

    _reset_run_env(
        monkeypatch,
        MINI_ORK_HOME=str(home),
        MINI_ORK_DB=str(db_path),
        MINI_ORK_ROOT=str(tmp_path),
        MINI_ORK_RUN_ID=run_id,
        MINI_ORK_RUN_DIR=str(run_dir),
    )

    rc = classify.main([_kickoff(tmp_path), "--task-class", "spec_class"])

    assert rc == 0
    rows = _rows(db_path)
    assert len(rows) == 1 and rows[0][0] == run_id
    assert rows[0][1] == "spec_class", "inside a run the classification is recorded"
    assert rows[0][2] == "executing", "a live status must not be downgraded"


# ── RB-46: keep judge reports, discard synthesis ────────────────────────────


def _rollback_ctx(tmp_path: Path, recipe_dir: Path, run_dir: Path) -> NodeDispatch:
    plan_doc = {
        "objective": "panel",
        "decomposition": [
            {"id": "opus_judge", "description": "judge", "node_type": "researcher", "depends_on": []},
            {"id": "minimax_judge", "description": "judge", "node_type": "researcher", "depends_on": []},
            {"id": "synthesizer", "description": "synthesize", "node_type": "reviewer", "depends_on": []},
        ],
        "artifact_contract": {"outputs": [], "success_verifiers": []},
        "verifier_contract": {"checks": [{"id": "c", "description": "d"}]},
    }
    (run_dir / "plan.json").write_text(json.dumps(plan_doc), encoding="utf-8")
    return NodeDispatch(
        node_id="rollback",
        node_type="rollback",
        node_desc="rollback",
        prompt_ref="",
        verifier_ref="",
        model_lane="",
        node_requires_capabilities="",
        root=str(tmp_path),
        run_dir=str(run_dir),
        plan_path=str(run_dir / "plan.json"),
        task_class="audit_judge_panel",
        db=str(tmp_path / "no-state.db"),
        run_id="run-rb46",
        recipe="audit-judge-panel",
        workflow=str(recipe_dir / "workflow.yaml"),
        lane="",
        run_dir_eff=str(run_dir),
        recipe_dir=str(recipe_dir),
        prompt_file="",
        plan_content=(run_dir / "plan.json").read_text(encoding="utf-8"),
        learned="",
        dispatch_fn=None,
        trace=None,
        charge=None,
        artifact_context=None,
        artifact_ledger=None,
        compiled_workflow=None,
        node_verdict="",
    )


def test_keep_judge_reports_discard_synthesis(tmp_path, monkeypatch, capsys):
    recipe_dir = REPO / "recipes" / "audit-judge-panel"
    contract = yaml.safe_load((recipe_dir / "artifact_contract.yaml").read_text(encoding="utf-8")) or {}
    outputs = [str(name) for name in (contract.get("outputs") or [])]
    synthesis = str(contract.get("source_artifact") or "").strip()
    judge_reports = [name for name in outputs if name != synthesis]

    run_dir = tmp_path / "runs" / "run-rb46"
    run_dir.mkdir(parents=True)
    for name in outputs:
        (run_dir / name).write_text(f"# {name}\n", encoding="utf-8")

    _reset_run_env(monkeypatch)
    rc, finish = _handle_rollback(_rollback_ctx(tmp_path, recipe_dir, run_dir))

    assert (rc, finish) == (0, "done")
    err = capsys.readouterr().err
    assert "unhandled rollback_strategy" not in err
    for name in judge_reports:
        assert (run_dir / name).is_file(), f"judge report {name!r} must be kept"
    assert not (run_dir / synthesis).exists(), f"synthesis {synthesis!r} must be removed"


# ── RB-47: wave aggregator verdict ─────────────────────────────────────────


def _aggregate(tmp_path, monkeypatch, statuses):
    run_dir = tmp_path / "runs" / "run-rb47"
    run_dir.mkdir(parents=True)
    plan_doc = {
        "epics": [{"id": epic_id, "depends_on": []} for epic_id in statuses],
        "waves": [list(statuses)],
    }
    (run_dir / "epic-runner-plan.json").write_text(json.dumps(plan_doc), encoding="utf-8")
    results = {"epics": [{"id": epic_id, "status": status}
                         for epic_id, status in statuses.items()]}
    (run_dir / "epic-results.json").write_text(json.dumps(results), encoding="utf-8")
    _reset_run_env(monkeypatch, MINI_ORK_RUN_DIR=str(run_dir))

    spec = importlib.util.spec_from_file_location(
        "rb47_wave_aggregator", REPO / "recipes" / "epic-runner" / "lib" / "wave_aggregator.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.aggregate() == 0
    return json.loads((run_dir / "wave-aggregate.json").read_text(encoding="utf-8"))


def test_aggregator_all_passed_is_pass(tmp_path, monkeypatch):
    payload = _aggregate(tmp_path, monkeypatch, {"e1": "passed", "e2": "passed"})
    assert payload["verdict"] == "pass"


def test_aggregator_one_failed_none_pending_is_fail(tmp_path, monkeypatch):
    payload = _aggregate(tmp_path, monkeypatch, {"e1": "passed", "e2": "failed"})
    assert payload["verdict"] == "fail"
