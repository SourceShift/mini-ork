"""Reviewer round awareness (MO_REVIEW_ROUND_AWARE) and the one-complete-pass prompt rule."""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]

from mini_ork.cli import execute as ex  # noqa: E402
from mini_ork.cli import execute_handlers as eh  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    for name in ("MINI_ORK_RUN_DIR", "MINI_ORK_RECIPE", "MINI_ORK_PLAN_PATH", "MINI_ORK_RUN_ID",
                 "MO_TARGET_CWD", "MO_APPLY_IMPL_OUTPUT", "MINI_ORK_WORKFLOW", "MINI_ORK_RECIPE_ROOT",
                 "MO_REVISE_ROUNDS", "MO_GRADE_RUN_REWARD", "MO_LEARNING_WRITEBACK", "MO_LANE_ROUTER",
                 "MO_REVIEW_ROUND_AWARE", "MO_REVIEW_ROUND_AWARE_HOLDOUT", "MO_CONTEXT_V2"):
        monkeypatch.delenv(name, raising=False)


# ── the block itself ────────────────────────────────────────────────────────

def _revise(run_dir: Path, round_no: int, max_rounds: int, text: str) -> None:
    d = run_dir / "revise"
    d.mkdir(parents=True, exist_ok=True)
    fb = d / f"round-{round_no}.md"
    fb.write_text(text, encoding="utf-8")
    (d / "current.json").write_text(json.dumps(
        {"round": round_no, "max_rounds": max_rounds, "feedback": str(fb)}))


def test_off_by_default_returns_nothing_and_records_nothing(tmp_path):
    assert eh._reviewer_round_block(str(tmp_path), "run-1") == ""
    assert not (tmp_path / "review-round-aware.json").exists()


def test_first_attempt_states_the_round_and_the_severity_bar(tmp_path, monkeypatch):
    monkeypatch.setenv("MO_REVIEW_ROUND_AWARE", "on")
    monkeypatch.setenv("MO_REVIEW_ROUND_AWARE_HOLDOUT", "0")
    block = eh._reviewer_round_block(str(tmp_path), "run-1")
    assert "## Review attempt 1 of 3" in block
    assert "HIGH-severity" in block and "FINAL" not in block
    rec = json.loads((tmp_path / "review-round-aware.json").read_text())
    assert rec[-1]["arm"] == "on" and rec[-1]["attempt"] == 1 and rec[-1]["final"] is False


def test_final_attempt_says_it_discards_and_carries_the_previous_findings(tmp_path, monkeypatch):
    monkeypatch.setenv("MO_REVIEW_ROUND_AWARE", "on")
    monkeypatch.setenv("MO_REVIEW_ROUND_AWARE_HOLDOUT", "0")
    _revise(tmp_path, 2, 2, "the cache key ignores the file mtime")
    block = eh._reviewer_round_block(str(tmp_path), "run-1")
    assert "## Review attempt 3 of 3" in block
    assert "FINAL attempt it discards the whole delivery" in block
    assert "last attempt" in block
    assert "the cache key ignores the file mtime" in block


def test_holdout_arm_keeps_the_plain_reviewer_but_is_recorded(tmp_path, monkeypatch):
    monkeypatch.setenv("MO_REVIEW_ROUND_AWARE", "on")
    monkeypatch.setenv("MO_REVIEW_ROUND_AWARE_HOLDOUT", "1")
    assert eh._reviewer_round_block(str(tmp_path), "run-1") == ""
    assert json.loads((tmp_path / "review-round-aware.json").read_text())[-1]["arm"] == "holdout"


def test_arm_is_deterministic_per_run_and_independent_of_context_v2():
    import mini_ork.context_v2 as cv
    ids = [f"run-{i}" for i in range(300)]
    os.environ["MO_REVIEW_ROUND_AWARE"] = "on"
    try:
        arms = [eh._review_round_arm(r) for r in ids]
        assert arms == [eh._review_round_arm(r) for r in ids]
        held = [r for r, a in zip(ids, arms) if a == "holdout"]
        assert 0.1 < len(held) / len(ids) < 0.3
        # a different salt than context v2's holdout: the two arms are not the same runs
        assert held != [r for r in ids if cv.in_holdout(r, 0.2)]
    finally:
        os.environ.pop("MO_REVIEW_ROUND_AWARE", None)


def test_reviewer_prompts_require_one_complete_pass():
    for recipe in ("framework-edit", "code-fix"):
        text = (REPO / "recipes" / recipe / "prompts" / "reviewer.md").read_text(encoding="utf-8")
        assert "## One complete pass" in text
        assert "every changed file" in text


# ── through the real executor (fake LLM seam) ───────────────────────────────

def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, check=True)


def _setup(tmp_path: Path, monkeypatch) -> Path:
    recipe_dir = tmp_path / "overlay" / "recipes" / "rounds-test"
    recipe_dir.mkdir(parents=True)
    (recipe_dir / "workflow.yaml").write_text(yaml.safe_dump({
        "version": "0.1.0", "task_class": "code_fix", "dispatch_mode": "parallel",
        "nodes": [
            {"name": "planner", "type": "planner", "dispatch_mode": "serial"},
            {"name": "implementer", "type": "implementer", "model_lane": "worker", "dispatch_mode": "serial"},
            {"name": "reviewer", "type": "reviewer", "model_lane": "reviewer", "dispatch_mode": "serial"},
            {"name": "rollback", "type": "rollback", "dispatch_mode": "serial"},
        ],
        "edges": [
            {"from": "planner", "to": "implementer", "edge_type": "depends_on"},
            {"from": "implementer", "to": "reviewer", "edge_type": "depends_on"},
            {"from": "reviewer", "to": "rollback", "edge_type": "escalates_to"},
            {"from": "reviewer", "to": "implementer", "edge_type": "retries", "max_rounds": 2},
        ],
    }), encoding="utf-8")
    repo = tmp_path / "target"
    repo.mkdir()
    for args in (("init", "-q"), ("config", "user.email", "t@t"), ("config", "user.name", "t")):
        _git(repo, *args)
    (repo / "a.py").write_text("x = 1\n")
    _git(repo, "add", "a.py")
    _git(repo, "commit", "-qm", "base")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    plan = run_dir / "plan.json"
    plan.write_text(json.dumps({"objective": "o", "task_class": "code_fix"}))
    home = tmp_path / "db" / ".mini-ork"
    home.mkdir(parents=True)
    db = str(home / "state.db")
    subprocess.run(["bash", str(REPO / "db" / "init.sh")],
                   env={**os.environ, "MINI_ORK_HOME": str(home), "MINI_ORK_DB": db},
                   capture_output=True, text=True, check=True)
    con = sqlite3.connect(db)
    con.execute("INSERT INTO task_runs (id,task_class,workflow_version,kickoff_path,status,cost_usd,"
                "created_at,updated_at) VALUES ('r','code_fix','v1','k.md','planned',0,"
                "strftime('%s','now','-60 seconds'),strftime('%s','now'))")
    con.commit()
    con.close()
    for name, value in {"MO_TARGET_CWD": str(repo), "MINI_ORK_WORKFLOW": str(recipe_dir / "workflow.yaml"),
                        "MINI_ORK_PLAN_PATH": str(plan), "MINI_ORK_RUN_DIR": str(run_dir),
                        "MINI_ORK_RUN_ID": "r", "MINI_ORK_DB": db, "MINI_ORK_HOME": str(home),
                        "MINI_ORK_RECIPE_ROOT": str(tmp_path / "overlay"), "MINI_ORK_RECIPE": "rounds-test",
                        "MO_GRADE_RUN_REWARD": "0", "MO_LEARNING_WRITEBACK": "0", "MO_LANE_ROUTER": "0",
                        "MO_CONTEXT_V2": "off"}.items():
        monkeypatch.setenv(name, value)
    return repo


def _drive(tmp_path, monkeypatch):
    repo = _setup(tmp_path, monkeypatch)
    reviewer_prompts: list[str] = []
    impl = {"n": 0}

    def fake_dispatch(task_class, lane, prompt):
        if "Implement:" in prompt:
            impl["n"] += 1
            (repo / "a.py").write_text(f"x = {impl['n'] + 1}\n")
            return 0, "done"
        if "Review the implementation" in prompt:
            reviewer_prompts.append(prompt)
            return 0, json.dumps({"verdict": "needs_revision", "notes": ["missing test"]})
        return 0, "done"

    ex.main(argv=[], root=str(REPO), dispatch_fn=fake_dispatch)
    return reviewer_prompts


def test_executor_tells_each_review_round_which_attempt_it_is(tmp_path, monkeypatch):
    monkeypatch.setenv("MO_REVIEW_ROUND_AWARE", "on")
    monkeypatch.setenv("MO_REVIEW_ROUND_AWARE_HOLDOUT", "0")
    prompts = _drive(tmp_path, monkeypatch)
    assert len(prompts) == 3
    assert "## Review attempt 1 of 3" in prompts[0]
    assert "## Review attempt 2 of 3" in prompts[1] and "missing test" in prompts[1]
    assert "## Review attempt 3 of 3" in prompts[2] and "FINAL attempt" in prompts[2]


def test_executor_reviewer_prompt_is_unchanged_with_the_flag_off(tmp_path, monkeypatch):
    prompts = _drive(tmp_path, monkeypatch)
    assert len(prompts) == 3
    assert not any("## Review attempt" in p for p in prompts)
