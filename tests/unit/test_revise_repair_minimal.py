"""C9b — a revise round must not re-send the whole first-attempt context.

When a checker (verifier/reviewer) fails a node that carries a ``retries`` edge,
the runtime writes ``<run_dir>/revise/current.json`` and the implementer node
re-dispatches to fix the findings. The implementer used to be handed its FULL
first-attempt prompt again — recipe prompt + plan + artifact context — with the
findings merely appended. Fixing one finding therefore cost a fresh full session:
every background block the agent already had was re-sent and re-read.

A revise round now gets a MINIMAL repair prompt: the findings + the working tree
it must fix on top of. ``MO_REVISE_FULL_CONTEXT=1`` restores the old behaviour.

Measured acceptance: a repair round's prompt is < 25% of the original run's
implementer prompt.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from pathlib import Path

import yaml

from mini_ork.cli import execute as ex

REPO = Path(__file__).resolve().parents[2]

_RECIPE_PROMPT = "# Implementer\n\n" + ("Recipe convention line.\n" * 200)
_PLAN_BODY = "P" * 8000


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, check=True)


def _setup(tmp_path: Path, monkeypatch) -> Path:
    recipe_dir = tmp_path / "overlay" / "recipes" / "rounds-test"
    (recipe_dir / "prompts").mkdir(parents=True)
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
    # A recipe prompt + a plan make the first-attempt context the size it is in
    # production — this is what a full-context revise round would re-send.
    (recipe_dir / "prompts" / "implementer.md").write_text(_RECIPE_PROMPT, encoding="utf-8")
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
    plan.write_text(json.dumps({"objective": "o", "task_class": "code_fix", "body": _PLAN_BODY}))
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


def _drive(tmp_path, monkeypatch) -> list[str]:
    repo = _setup(tmp_path, monkeypatch)
    impl_prompts: list[str] = []
    state = {"n": 0}

    def fake_dispatch(task_class, lane, prompt):
        if "Implement:" in prompt:
            state["n"] += 1
            impl_prompts.append(prompt)
            (repo / "a.py").write_text(f"x = {state['n'] + 1}\n")
            return 0, "done"
        if "Review the implementation" in prompt:
            return 0, json.dumps({"verdict": "needs_revision",
                                  "notes": ["the single failing finding"]})
        return 0, "done"

    ex.main(argv=[], root=str(REPO), dispatch_fn=fake_dispatch)
    return impl_prompts


def test_revise_round_uses_a_minimal_repair_prompt(tmp_path, monkeypatch):
    prompts = _drive(tmp_path, monkeypatch)
    assert len(prompts) >= 2, f"expected a revise round; got {len(prompts)}"
    initial, repair = prompts[0], prompts[1]
    assert "## Revision round" not in initial
    assert "## Revision round" in repair

    # it still carries what the round is FOR
    assert "the single failing finding" in repair
    # structural: the first-attempt background is gone
    assert "--- Recipe prompt (system context) ---" not in repair
    assert _PLAN_BODY[:200] not in repair
    # quantitative: < 25% of the original implementer prompt
    assert len(repair) < 0.25 * len(initial), (len(repair), len(initial))


def test_full_context_knob_restores_the_old_revise_prompt(tmp_path, monkeypatch):
    monkeypatch.setenv("MO_REVISE_FULL_CONTEXT", "1")
    prompts = _drive(tmp_path, monkeypatch)
    repair = next(p for p in prompts if "## Revision round" in p)
    assert "--- Recipe prompt (system context) ---" in repair
    assert "the single failing finding" in repair
