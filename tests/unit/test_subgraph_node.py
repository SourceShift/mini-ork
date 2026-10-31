"""Subgraph nodes (mini_ork.orchestration.subgraph): one recipe as a node of another.

Hermetic: a fresh migrated state.db with a parent task_runs row, a project
recipe in a temporary MINI_ORK_HOME, and an injected runner standing in for the
child ``mini-ork run``. The spawn policy is the real recursive-orchestration one.
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import time
from pathlib import Path

import jsonschema
import pytest

from mini_ork.orchestration import subgraph
from mini_ork.stores import migrate as mig

REPO = Path(__file__).resolve().parents[2]
PARENT = "run-parent-1"


@pytest.fixture()
def world(tmp_path, monkeypatch):
    home = tmp_path / "home"
    run_dir = home / "runs" / PARENT
    run_dir.mkdir(parents=True)
    db = home / "state.db"
    rc, _, err = mig.init_db(db=str(db), root=str(REPO))
    assert rc == 0, err
    target = tmp_path / "target"
    target.mkdir()
    kickoff = target / "kickoff.md"
    kickoff.write_text("# parent kickoff\n")
    con = sqlite3.connect(db)
    now = int(time.time())
    con.execute("INSERT INTO task_runs (id, task_class, kickoff_path, created_at, updated_at) "
                "VALUES (?, 'parent_task', ?, ?, ?)", (PARENT, str(kickoff), now, now))
    con.commit()
    con.close()
    recipe = home / "recipes" / "child-recipe"
    recipe.mkdir(parents=True)
    (recipe / "workflow.yaml").write_text("version: '0.1.0'\ntask_class: child_task\nnodes: []\nedges: []\n")
    (recipe / "task_class.yaml").write_text("name: child_task\n")
    (recipe / "artifact_contract.yaml").write_text("source_artifact: result.md\noutputs: []\n")
    monkeypatch.setenv("MINI_ORK_DB", str(db))
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    return {"home": home, "run_dir": run_dir, "db": db, "target": target, "kickoff": kickoff}


def _base_env(world, **extra):
    return {"MINI_ORK_HOME": str(world["home"]), "MINI_ORK_DB": str(world["db"]),
            "MINI_ORK_RUN_ID": PARENT, "MINI_ORK_RUN_DIR": str(world["run_dir"]),
            "MO_NODE_ID": "child", "MO_SKILLS": "doc-writer", "PATH": "/usr/bin", **extra}


def fake_runner(world, *, rc=0, calls=None):
    def run(argv, *, env, cwd, capture_output, text, timeout):
        if calls is not None:
            calls.append({"argv": argv, "env": env, "cwd": cwd})
        child_dir = world["home"] / "runs" / env["MINI_ORK_RUN_ID"]
        child_dir.mkdir(parents=True, exist_ok=True)
        if rc == 0:
            (child_dir / "result.md").write_text("child result\n")
        return subprocess.CompletedProcess(argv, rc, stdout="child stdout\n", stderr="")
    return run


def _run(world, runner, **overrides):
    params = dict(parent_run_id=PARENT, node_id="child", recipe="child-recipe", kickoff="",
                  run_dir=str(world["run_dir"]), home=str(world["home"]), target=str(world["target"]),
                  db=str(world["db"]), base_env=_base_env(world), timeout_s=60, runner=runner)
    params.update(overrides)
    return subgraph.run_subgraph(**params)


def _spawn_status(world, child):
    con = sqlite3.connect(world["db"])
    try:
        row = con.execute("SELECT status FROM run_spawns WHERE child_run_id=?", (child,)).fetchone()
    finally:
        con.close()
    return row[0] if row else None


def test_the_child_recipe_runs_as_its_own_run_and_its_artifact_comes_back(world):
    calls = []
    record = _run(world, fake_runner(world, calls=calls))

    assert (record["rc"], record["error"]) == (0, "")
    assert record["child_run_id"] == f"{PARENT}--child"
    assert record["kickoff"] == str(world["kickoff"]), "defaults to the parent run's kickoff"
    assert Path(record["artifact"]).read_text() == "child result\n"
    assert Path(record["artifact"]).parent == world["run_dir"]
    assert _spawn_status(world, record["child_run_id"]) == "completed"
    assert (world["run_dir"] / "subgraph-child.log").read_text() == "child stdout\n"
    call = calls[0]
    assert call["argv"][-3:] == ["run", "child-recipe", str(world["kickoff"])]
    assert call["cwd"] == str(world["target"])


def test_the_child_inherits_no_run_or_node_scope_of_the_parent(world):
    env = subgraph.child_env(_base_env(world), child_run_id="c1", depth=1)
    assert "MINI_ORK_RUN_DIR" not in env and "MO_NODE_ID" not in env and "MO_SKILLS" not in env
    assert (env["MINI_ORK_RUN_ID"], env["MINI_ORK_PARENT_RUN_ID"]) == ("c1", PARENT)
    assert env[subgraph.DEPTH_ENV] == "1"
    assert env["MINI_ORK_HOME"] == str(world["home"]) and env["PATH"] == "/usr/bin"
    # Found by the live smoke: started from the target repo, the child could not
    # import mini_ork until the engine's package root was on its path.
    assert env["PYTHONPATH"].split(":")[0] == str(REPO)


def test_a_relative_parent_kickoff_is_resolved_against_the_target(world):
    con = sqlite3.connect(world["db"])
    con.execute("UPDATE task_runs SET kickoff_path='kickoff.md' WHERE id=?", (PARENT,))
    con.commit()
    con.close()
    record = _run(world, fake_runner(world))
    assert record["kickoff"] == str(world["target"] / "kickoff.md")


def test_a_failed_child_fails_the_node(world):
    record = _run(world, fake_runner(world, rc=3))
    assert record["rc"] == 3 and record["artifact"] is None
    assert _spawn_status(world, record["child_run_id"]) == "failed"


def test_the_spawn_policy_bounds_the_depth(world, monkeypatch):
    monkeypatch.setenv("MINI_ORK_RECURSIVE_MAX_DEPTH", "2")
    calls = []
    record = _run(world, fake_runner(world, calls=calls),
                  base_env=_base_env(world, **{subgraph.DEPTH_ENV: "2"}))
    assert "exceeds max_depth 2" in record["error"]
    assert calls == [], "a refused spawn never starts a child"


def test_an_explicit_kickoff_resolves_against_the_target(world):
    (world["target"] / "child-kickoff.md").write_text("# child\n")
    record = _run(world, fake_runner(world), kickoff="child-kickoff.md")
    assert record["kickoff"] == str(world["target"] / "child-kickoff.md")


def test_a_missing_kickoff_is_an_error_not_a_child_run(world):
    calls = []
    record = _run(world, fake_runner(world, calls=calls), kickoff="nope.md")
    assert record["error"].startswith("no kickoff for the child run") and calls == []


def test_node_fields_reads_the_workflow_entry(tmp_path):
    wf = tmp_path / "workflow.yaml"
    wf.write_text("nodes:\n  - {name: review, type: subgraph, recipe: code-fix, kickoff: k.md, dispatch_mode: serial}\n")
    assert subgraph.node_fields(str(wf), "review")["recipe"] == "code-fix"
    assert subgraph.node_fields(str(wf), "other") == {}


def test_the_schema_accepts_a_subgraph_node_and_requires_its_recipe():
    schema = json.loads((REPO / "schemas" / "workflow.schema.json").read_text())
    def workflow(node):
        return {"version": "0.1.0", "task_class": "t", "edges": [],
                "nodes": [{"name": "inner", "type": "subgraph", "dispatch_mode": "serial", **node}]}
    jsonschema.validate(workflow({"recipe": "code-fix"}), schema)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(workflow({}), schema)
