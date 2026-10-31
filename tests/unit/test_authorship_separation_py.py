"""I2a authorship separation: the probe author's model family must not be the implementer's.

Unit tests for ``mini_ork.verify.authorship_separation`` plus the ``execute.main``
wiring, driven like ``test_revise_loop.py::_run`` (a recipe overlay via
``MINI_ORK_RECIPE_ROOT`` and a fake LLM seam, so nothing spends provider credits).
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.cli import execute as ex  # noqa: E402
from mini_ork.dispatch import agents_config  # noqa: E402
from mini_ork.verify import authorship_separation as sep  # noqa: E402

GATE_FILE = "authorship-separation-gate.json"


@pytest.fixture(autouse=True)
def _isolate_run_environment(monkeypatch):
    for name in (
        "MINI_ORK_RUN_DIR", "MINI_ORK_RECIPE", "MINI_ORK_PLAN_PATH",
        "MINI_ORK_RUN_ID", "MO_TARGET_CWD", "MO_APPLY_IMPL_OUTPUT",
        "MINI_ORK_WORKFLOW", "MINI_ORK_RECIPE_ROOT", "MINI_ORK_AGENTS",
        "MO_GRADE_RUN_REWARD", "MO_LEARNING_WRITEBACK", "MO_LANE_ROUTER",
        sep.FLAG,
    ):
        monkeypatch.delenv(name, raising=False)


# ── mode ──────────────────────────────────────────────────────────────────────

def test_mode_unset_is_off(monkeypatch):
    monkeypatch.delenv(sep.FLAG, raising=False)
    assert sep.mode() == "off"
    assert sep.mode({}) == "off"


@pytest.mark.parametrize("value", ["", "0", "off"])
def test_mode_empty_and_explicit_off_are_off(value):
    assert sep.mode({sep.FLAG: value}) == "off"


def test_mode_shadow_and_enforce():
    assert sep.mode({sep.FLAG: "shadow"}) == "shadow"
    assert sep.mode({sep.FLAG: "1"}) == "enforce"


def test_mode_reads_the_process_env_by_default(monkeypatch):
    monkeypatch.setenv(sep.FLAG, "shadow")
    assert sep.mode() == "shadow"
    monkeypatch.setenv(sep.FLAG, "1")
    assert sep.mode() == "enforce"


@pytest.mark.parametrize("value", ["2", "true", "enforce", "shadows"])
def test_mode_unknown_value_raises_naming_the_flag(value):
    with pytest.raises(ValueError, match=sep.FLAG):
        sep.mode({sep.FLAG: value})


# ── probe_author_nodes ────────────────────────────────────────────────────────

def _wf(*nodes: dict) -> dict:
    return {"nodes": list(nodes)}


def test_probe_author_matched_by_type():
    node = {"name": "anything", "type": "probe_author", "model_lane": "glm_lens"}
    assert sep.probe_author_nodes(_wf(node)) == [node]


@pytest.mark.parametrize("basename", ["spec-test-author.md", "test-author.md"])
def test_probe_author_matched_by_prompt_basename_under_a_renamed_node(basename):
    node = {"name": "blind_probes", "type": "researcher", "model_lane": "glm_lens",
            "prompt_ref": f"prompts/{basename}"}
    assert sep.probe_author_nodes(_wf(node)) == [node]


def test_node_named_spec_test_author_with_unrelated_prompt_is_not_matched():
    node = {"name": "spec_test_author", "type": "researcher", "model_lane": "glm_lens",
            "prompt_ref": "prompts/code-impact-lens.md"}
    assert sep.probe_author_nodes(_wf(node)) == []


def test_probe_author_tolerates_null_prompt_and_malformed_workflows():
    assert sep.probe_author_nodes(_wf({"name": "v", "type": "verifier", "prompt_ref": None})) == []
    assert sep.probe_author_nodes({}) == []
    assert sep.probe_author_nodes({"nodes": "nope"}) == []
    assert sep.probe_author_nodes({"nodes": ["not-a-dict"]}) == []


# ── check_separation ──────────────────────────────────────────────────────────

AUTHOR = {"name": "spec_test_author", "type": "researcher", "model_lane": "glm_lens",
          "prompt_ref": "prompts/spec-test-author.md"}
IMPL = {"name": "implementer", "type": "implementer", "model_lane": "implementer"}


def _fam(**aliases: str) -> dict:
    return {a: {"lane": a, "model": a, "family": f} for a, f in aliases.items()}


def test_distinct_families_are_ok():
    ok, reason, detail = sep.check_separation(
        _wf(AUTHOR, IMPL), _fam(glm_lens="zai", implementer="minimax"))
    assert ok is True and reason == ""
    assert detail == {
        "probe_author_lanes": ["glm_lens"],
        "implementer_lanes": ["implementer"],
        "author_families": {"glm_lens": "zai"},
        "implementer_families": {"implementer": "minimax"},
    }


def test_same_family_is_refused_with_author_family_is_implementer():
    ok, reason, detail = sep.check_separation(
        _wf(AUTHOR, IMPL), _fam(glm_lens="zai", implementer="zai"))
    assert ok is False
    assert reason == "author_family_is_implementer: glm_lens/implementer -> zai"
    assert detail["author_families"] == {"glm_lens": "zai"}
    assert detail["implementer_families"] == {"implementer": "zai"}


def test_alias_missing_from_the_map_resolves_to_itself():
    # Neither lane is in the map: each is its own family, so they differ ...
    ok, _, detail = sep.check_separation(_wf(AUTHOR, IMPL), {})
    assert ok is True
    assert detail["author_families"] == {"glm_lens": "glm_lens"}
    assert detail["implementer_families"] == {"implementer": "implementer"}
    # ... and the SAME unmapped alias on both sides collides with itself.
    same = {**IMPL, "model_lane": "glm_lens"}
    ok, reason, _ = sep.check_separation(_wf(AUTHOR, same), {})
    assert ok is False
    assert reason == "author_family_is_implementer: glm_lens/glm_lens -> glm_lens"
    # An alias that is unmapped can still collide with a mapped family named like it.
    ok, reason, _ = sep.check_separation(_wf(AUTHOR, IMPL), _fam(implementer="glm_lens"))
    assert ok is False and "-> glm_lens" in reason


def test_no_probe_author_node_passes():
    ok, reason, detail = sep.check_separation(_wf(IMPL), _fam(implementer="zai"))
    assert (ok, reason) == (True, "no_probe_author_node")
    assert detail["probe_author_lanes"] == [] and detail["implementer_lanes"] == ["implementer"]


def test_no_implementer_node_passes():
    ok, reason, detail = sep.check_separation(_wf(AUTHOR), _fam(glm_lens="zai"))
    assert (ok, reason) == (True, "no_implementer_node")
    assert detail["probe_author_lanes"] == ["glm_lens"] and detail["implementer_lanes"] == []


def test_every_author_is_compared_with_every_implementer():
    second_author = {"name": "t2", "type": "probe_author", "model_lane": "codex_lens"}
    second_impl = {"name": "impl2", "type": "implementer", "model_lane": "worker"}
    fams = _fam(glm_lens="zai", codex_lens="openai", implementer="minimax", worker="openai")
    ok, reason, _ = sep.check_separation(_wf(AUTHOR, second_author, IMPL, second_impl), fams)
    assert ok is False
    assert reason == "author_family_is_implementer: codex_lens/worker -> openai"


def test_node_without_model_lane_uses_its_type_like_dispatch_does():
    bare_impl = {"name": "i", "type": "implementer"}
    ok, reason, detail = sep.check_separation(
        _wf(AUTHOR, bare_impl), _fam(glm_lens="zai", implementer="zai"))
    assert ok is False and reason.endswith("glm_lens/implementer -> zai")
    assert detail["implementer_lanes"] == ["implementer"]


# ── execute.main wiring ───────────────────────────────────────────────────────

def _workflow() -> dict:
    return {
        "version": "0.1.0",
        "task_class": "code_fix",
        "dispatch_mode": "serial",
        "nodes": [
            {"name": "planner", "type": "planner", "dispatch_mode": "serial"},
            {"name": "blind_probes", "type": "researcher", "model_lane": "glm_lens",
             "prompt_ref": "prompts/spec-test-author.md", "dispatch_mode": "serial"},
            {"name": "implementer", "type": "implementer", "model_lane": "implementer",
             "dispatch_mode": "serial"},
        ],
        "edges": [
            {"from": "planner", "to": "blind_probes", "edge_type": "depends_on"},
            {"from": "blind_probes", "to": "implementer", "edge_type": "depends_on"},
        ],
    }


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, check=True)


def _drive(tmp_path, monkeypatch, *, flag, colliding, lanes_overlay=None):
    """Drive ``ex.main`` once and return ``(rc, dispatched_lanes, run_dir, db)``.

    ``colliding`` True/False pins the family map to a collision / a clean split;
    None leaves ``lane_families`` untouched (real overlay resolution, or the off path).
    """
    recipe_dir = tmp_path / "overlay" / "recipes" / "authorship-test"
    recipe_dir.mkdir(parents=True)
    wf_path = recipe_dir / "workflow.yaml"
    wf_path.write_text(yaml.safe_dump(_workflow()), encoding="utf-8")

    repo = tmp_path / "target"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
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
    subprocess.run(
        ["sqlite3", db,
         "INSERT INTO task_runs (id,task_class,workflow_version,kickoff_path,status,"
         "cost_usd,created_at,updated_at) VALUES ('r','code_fix','v1','k.md','planned',"
         "0,strftime('%s','now','-60 seconds'),strftime('%s','now'));"],
        capture_output=True, text=True, check=True)
    if lanes_overlay is not None:
        (home / "config").mkdir()
        (home / "config" / "agents.local.yaml").write_text(
            yaml.safe_dump({"lanes": lanes_overlay}), encoding="utf-8")

    monkeypatch.setenv("MO_TARGET_CWD", str(repo))
    monkeypatch.setenv("MINI_ORK_WORKFLOW", str(wf_path))
    monkeypatch.setenv("MINI_ORK_PLAN_PATH", str(plan))
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(run_dir))
    monkeypatch.setenv("MINI_ORK_RUN_ID", "r")
    monkeypatch.setenv("MINI_ORK_DB", db)
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    monkeypatch.setenv("MINI_ORK_RECIPE_ROOT", str(tmp_path / "overlay"))
    monkeypatch.setenv("MINI_ORK_RECIPE", "authorship-test")
    monkeypatch.setenv("MO_GRADE_RUN_REWARD", "0")
    monkeypatch.setenv("MO_LEARNING_WRITEBACK", "0")
    monkeypatch.setenv("MO_LANE_ROUTER", "0")
    if flag is not None:
        monkeypatch.setenv(sep.FLAG, flag)

    if colliding is not None:
        fams = (_fam(glm_lens="zai", implementer="zai") if colliding
                else _fam(glm_lens="zai", implementer="minimax"))
        monkeypatch.setattr(agents_config, "lane_families", lambda agents: fams)

    dispatched: list[str] = []

    def fake_dispatch(task_class, lane, prompt):
        dispatched.append(lane)
        if "Implement:" in prompt:
            (repo / "a.py").write_text("x = 2\n")
        return 0, "done"

    rc = ex.main(argv=[], root=str(REPO), dispatch_fn=fake_dispatch)
    return rc, dispatched, run_dir, db


def _notes(db: str) -> str:
    con = sqlite3.connect(db)
    try:
        return con.execute("SELECT COALESCE(notes, '') FROM task_runs WHERE id = 'r'").fetchone()[0]
    finally:
        con.close()


def test_main_enforce_refuses_a_colliding_family_before_dispatching(tmp_path, monkeypatch, capsys):
    rc, dispatched, run_dir, db = _drive(tmp_path, monkeypatch, flag="1", colliding=True)

    assert rc == 2
    assert dispatched == []
    err = capsys.readouterr().err
    assert ("execute: authorship separation refused: "
            "author_family_is_implementer: glm_lens/implementer -> zai") in err
    assert ("authorship_separation_refused: "
            "author_family_is_implementer: glm_lens/implementer -> zai") in _notes(db)
    assert not (run_dir / GATE_FILE).exists()


def test_main_enforce_lets_distinct_families_through(tmp_path, monkeypatch, capsys):
    rc, dispatched, run_dir, db = _drive(tmp_path, monkeypatch, flag="1", colliding=False)

    assert rc == 0
    assert dispatched                                    # the nodes actually ran
    assert "authorship separation refused" not in capsys.readouterr().err
    assert "authorship_separation_refused" not in _notes(db)
    assert not (run_dir / GATE_FILE).exists()            # enforce records nothing when ok


def test_main_shadow_records_would_block_and_proceeds(tmp_path, monkeypatch, capsys):
    rc, dispatched, run_dir, db = _drive(tmp_path, monkeypatch, flag="shadow", colliding=True)

    assert rc == 0
    assert dispatched                                    # shadow never blocks
    assert capsys.readouterr().err.count("authorship separation refused") == 0
    gate = json.loads((run_dir / GATE_FILE).read_text())
    assert gate["mode"] == "shadow"
    assert gate["would_block"] is True
    assert gate["reason"] == "author_family_is_implementer: glm_lens/implementer -> zai"
    assert gate["probe_author_lanes"] == ["glm_lens"]
    assert gate["implementer_lanes"] == ["implementer"]
    assert gate["author_families"] == {"glm_lens": "zai"}
    assert gate["implementer_families"] == {"implementer": "zai"}
    assert "[shadow] authorship separation would block: author_family_is_implementer" in _notes(db)


def test_main_shadow_with_distinct_families_records_no_block(tmp_path, monkeypatch):
    rc, dispatched, run_dir, db = _drive(tmp_path, monkeypatch, flag="shadow", colliding=False)

    assert rc == 0 and dispatched
    gate = json.loads((run_dir / GATE_FILE).read_text())
    assert gate["would_block"] is False and gate["mode"] == "shadow"
    assert "authorship separation would block" not in _notes(db)


def test_main_flag_unset_writes_no_gate_file(tmp_path, monkeypatch):
    rc, dispatched, run_dir, db = _drive(tmp_path, monkeypatch, flag=None, colliding=None)

    assert rc == 0 and dispatched
    assert not (run_dir / GATE_FILE).exists()
    assert "authorship" not in _notes(db)


def test_main_flag_unset_never_enters_the_gate(tmp_path, monkeypatch):
    # (`lane_families` is also called by dispatch itself, so the gate helper is the tripwire.)
    calls: list[object] = []
    monkeypatch.setattr(ex, "_authorship_separation_gate", lambda *a, **k: calls.append(k))

    rc, dispatched, _run_dir, _db = _drive(tmp_path, monkeypatch, flag=None, colliding=None)

    assert rc == 0 and dispatched
    assert calls == []


def test_main_unknown_flag_value_fails_closed(tmp_path, monkeypatch, capsys):
    rc, dispatched, run_dir, _db = _drive(tmp_path, monkeypatch, flag="enforce", colliding=None)

    assert rc == 2
    assert dispatched == []
    assert sep.FLAG in capsys.readouterr().err
    assert not (run_dir / GATE_FILE).exists()


def test_main_enforce_resolves_families_from_the_home_agents_overlay(tmp_path, monkeypatch, capsys):
    # No monkeypatch: the author and implementer aliases point at one lane in the
    # operator's overlay, so they are one family whatever the providers registry says.
    rc, dispatched, _run_dir, _db = _drive(
        tmp_path, monkeypatch, flag="1", colliding=None,
        lanes_overlay={"glm_lens": "shared-lane-x", "implementer": "shared-lane-x"})

    assert rc == 2
    assert dispatched == []
    err = capsys.readouterr().err
    assert "authorship separation refused: author_family_is_implementer: glm_lens/implementer" in err
