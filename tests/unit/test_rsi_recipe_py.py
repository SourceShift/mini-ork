"""The RSI recipes, and the seam that hands their artifacts to the cycle.

Two things are worth proving here, and neither needs a model:

* **The recipes are structurally whole.** Every ``verifier_ref``/``prompt_ref``
  exists, every edge connects a real output to a real input, and both workflows
  compile. A recipe that fails this only fails at dispatch time — after a cycle
  has claimed a candidate and started spending — so it is checked here instead.
* **The seam fails closed.** No recipe directory, a crashed run, or an artifact
  that is not JSON must leave the cycle in a state where G10 quarantines. The
  recipe is the only source of a proposal and of critic verdicts; a seam that
  invented either would be a merge path with no evidence behind it.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from mini_ork.rsi import cycle as cycle_mod  # noqa: E402
from mini_ork.rsi.candidates import Candidate  # noqa: E402
from mini_ork.rsi.config import from_mapping  # noqa: E402
from mini_ork.rsi.cycle import (  # noqa: E402
    CriticsResult,
    CycleFailure,
    CycleRequest,
    Implementation,
    _child_env,
    _default_critics,
    _default_propose,
    _dispatch_child,
    _recipe_critics,
    _recipe_propose,
    _run_recipe,
    _with_critics,
)
from mini_ork.rsi.gates import CriticVerdict, GateEvidence, evaluate  # noqa: E402
from mini_ork.rsi.select import Selection  # noqa: E402
from mini_ork.verify.panel_heterogeneity import lane_family  # noqa: E402
from mini_ork.workflow.compiler import compile_workflow  # noqa: E402

RECIPES = ("rsi-cycle", "rsi-critic")
NOW = 1_760_000_000
FILE = "mini_ork/rsi/cycle.py"


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


def _cfg(**overrides):
    raw = {
        "budget": {"daily_cap_usd": 50, "per_cycle_cap_usd": 20},
        "critic": {"lanes": ["glm_lens", "deepseek_lens"]},
        "gates": {"pytest_paths": ["tests/unit/test_x.py"]},
        "decide": {"auto_merge": True},
    }
    raw.update(overrides)
    return from_mapping(raw, env={}, strict=True)


def _req(tmp_path: Path, *, root: Path | None = None) -> CycleRequest:
    return CycleRequest(root=root or tmp_path, db=tmp_path / "state.db",
                        config=_cfg(), cycle_id="rsi-ctest")


def _cand() -> Candidate:
    return Candidate.build(
        domain="verification",
        source="db:V1",
        kind="data_path_dead",
        title="confidence is never persisted",
        evidence_key="table:verifier_results:confidence-null",
        files=(FILE,),
        success_command="db:V1",
        now=NOW,
    )


def _impl(cand: Candidate) -> Implementation:
    return Implementation(
        worktree=None,
        changed_files=cand.files,
        diff="diff --git a/x b/x\n+one line\n",
        verdict={"pass": True, "files_changed": 1},
        child_run_id="child-run",
        cost_usd=1.0,
    )


def _selection(cand: Candidate) -> Selection:
    return Selection(candidate_id=cand.candidate_id, accepted=True,
                     files=cand.files, verification_command=cand.success_command,
                     kickoff="# kickoff\n")


def _fake_run(monkeypatch, files: dict[str, object], *, rc: int = 0) -> dict:
    """Replace the dispatch with one that writes the given artifacts."""
    seen: dict = {}

    def _run(req, recipe, brief, kickoff, *, timeout_s, brief_env):
        seen.update(recipe=recipe, brief=brief, kickoff=kickoff,
                    timeout_s=timeout_s, brief_env=brief_env)
        home = req.home / "rsi" / req.cycle_id / recipe / "home"
        run_dir = home / "runs" / f"{req.cycle_id}-{recipe}"
        run_dir.mkdir(parents=True, exist_ok=True)
        for name, payload in files.items():
            (run_dir / name).write_text(json.dumps(payload), encoding="utf-8")
        return rc, home, run_dir

    monkeypatch.setattr(cycle_mod, "_run_recipe", _run)
    return seen


# --------------------------------------------------------------------------
# The recipes are structurally whole
# --------------------------------------------------------------------------


@pytest.mark.parametrize("recipe", RECIPES)
def test_the_workflow_compiles(recipe):
    compile_workflow(REPO / "recipes" / recipe / "workflow.yaml")


@pytest.mark.parametrize("recipe", RECIPES)
def test_every_verifier_prompt_and_edge_reference_resolves(recipe):
    recipe_dir = REPO / "recipes" / recipe
    wf = yaml.safe_load((recipe_dir / "workflow.yaml").read_text(encoding="utf-8"))
    nodes = {n["name"]: n for n in wf["nodes"]}
    outputs = {name: {o["name"] for o in n.get("outputs") or ()} for name, n in nodes.items()}
    inputs = {name: set((n.get("inputs") or {}).keys()) for name, n in nodes.items()}

    for node in wf["nodes"]:
        if node.get("verifier_ref"):
            assert (recipe_dir / node["verifier_ref"]).is_file(), node["name"]
        if node.get("prompt_ref"):
            assert (recipe_dir / node["prompt_ref"]).is_file(), node["name"]

    for edge in wf["edges"]:
        assert edge["from"] in nodes and edge["to"] in nodes, edge
        if edge["edge_type"] == "escalates_to":
            continue
        assert edge["from_output"] in outputs[edge["from"]], edge
        assert edge["to_input"] in inputs[edge["to"]], edge


@pytest.mark.parametrize("recipe", RECIPES)
def test_the_artifact_contract_names_its_success_verifiers(recipe):
    recipe_dir = REPO / "recipes" / recipe
    contract = yaml.safe_load(
        (recipe_dir / "artifact_contract.yaml").read_text(encoding="utf-8"))
    verifiers = contract.get("success_verifiers") or []
    assert verifiers, "a contract with no success verifier ships unchecked"
    for ref in verifiers:
        assert (recipe_dir / ref).is_file(), ref
    # Contract paths carry the run-dir prefix; the workflow declares them relative.
    promised = {o["path"].replace("${MINI_ORK_RUN_DIR}/", "")
                for o in contract.get("outputs") or ()}
    wf = yaml.safe_load((recipe_dir / "workflow.yaml").read_text(encoding="utf-8"))
    declared = {o["path"] for n in wf["nodes"] for o in (n.get("outputs") or ())}
    assert promised <= declared, promised - declared


def test_the_task_class_names_are_globally_unique():
    seen: dict[str, Path] = {}
    for task_class in sorted((REPO / "recipes").glob("*/task_class.yaml")):
        name = yaml.safe_load(task_class.read_text(encoding="utf-8")).get("name")
        assert name, task_class
        assert name not in seen, f"duplicate task class {name!r}: {seen.get(name)} and {task_class}"
        seen[name] = task_class
    assert {"rsi_cycle", "rsi_critic"} <= set(seen)


def _example_config():
    from mini_ork.rsi.config import load_config

    return load_config(REPO / "config" / "rsi-cycle.example.yaml", env={}, strict=True)


def test_the_critics_are_the_config_lanes_two_families_neither_the_implementers():
    """The recipe, the config and G10 must agree on who the critics are.

    A recipe that dispatches lanes the config does not name makes `doctor`'s
    family check grade a panel that never ran — and G10 then rejects the run
    after it has been paid for.
    """
    cfg = _example_config()
    wf = yaml.safe_load(
        (REPO / "recipes" / "rsi-critic" / "workflow.yaml").read_text(encoding="utf-8"))
    lanes = [n["model_lane"] for n in wf["nodes"] if n["type"] == "researcher"]
    assert sorted(lanes) == sorted(cfg.critic.lanes), (
        f"recipe dispatches {lanes}, config names {list(cfg.critic.lanes)}")
    families = [lane_family(lane) for lane in lanes]
    assert "" not in families, lanes
    assert len(set(families)) == 2, families
    assert lane_family(cfg.implement.lane) not in families, (
        f"a critic in the implementer's family ({cfg.implement.lane}) is not exogenous")


def test_the_suggest_workflow_runs_three_lenses_on_three_families():
    wf = yaml.safe_load(
        (REPO / "recipes" / "rsi-cycle" / "workflow.yaml").read_text(encoding="utf-8"))
    lenses = [n for n in wf["nodes"] if n["name"].startswith("lens_")]
    assert len(lenses) == 3, [n["name"] for n in lenses]
    families = [lane_family(n["model_lane"]) for n in lenses]
    assert len(set(families)) == 3, families


# --------------------------------------------------------------------------
# The verifier scripts run against the runtime's documented env contract
# --------------------------------------------------------------------------


def _script(recipe: str, name: str) -> Path:
    return REPO / "recipes" / recipe / "verifiers" / name


def _run(recipe: str, name: str, *, run_dir: Path, input_dir: Path,
         extra_env: dict[str, str]) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(filter(None, (str(REPO), os.environ.get("PYTHONPATH")))),
        "MINI_ORK_RUN_DIR": str(run_dir),
        "MINI_ORK_NODE_INPUT_DIR": str(input_dir),
        "MINI_ORK_RUN_ID": "rsi-ctest",
        **extra_env,
    }
    return subprocess.run([sys.executable, str(_script(recipe, name))],
                          env=env, capture_output=True, text=True)


def _brief_file(tmp_path: Path, *, candidate_id: str = "rsi-abc") -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    (repo / Path(FILE).parent).mkdir(parents=True)
    (repo / FILE).write_text("# target\n", encoding="utf-8")
    brief = {
        "version": 1,
        "cycle_id": "rsi-ctest",
        "candidate": {
            "candidate_id": candidate_id,
            "domain": "verification",
            "kind": "data_path_dead",
            "severity": 68,
            "title": "confidence is never persisted",
            "detail": "the producer writes nothing",
            "files": [FILE],
            "evidence": {"table": "verifier_results", "source": "db:V1"},
            "fix_hint": "call the writer",
            "success_metric": "rows with confidence",
            "success_command": "db:V1",
        },
        "constraints": {"repo_root": str(repo), "verification_command": "db:V1", "max_files": 5},
        "candidates": [],
    }
    path = tmp_path / "brief.json"
    path.write_text(json.dumps(brief), encoding="utf-8")
    return path, repo


def test_brief_then_proposal_accepts_a_well_formed_synthesis(tmp_path):
    brief, _ = _brief_file(tmp_path)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    inputs = tmp_path / "inputs"
    (inputs / "suggestion").mkdir(parents=True)
    (inputs / "suggestion" / "suggestion.json").write_text(json.dumps({
        "candidate_id": "rsi-abc",
        "files": [FILE, FILE],
        "change_summary": "persist the column",
        "root_cause": "the producer is unwired",
        "verification_command": "db:V1",
    }), encoding="utf-8")

    first = _run("rsi-cycle", "brief.py", run_dir=run_dir, input_dir=inputs,
                 extra_env={"MO_RSI_SUGGEST_BRIEF": str(brief)})
    assert first.returncode == 0, first.stderr
    rendered = (run_dir / "brief.md").read_text(encoding="utf-8")
    assert "db:V1" in rendered
    machine = json.loads((run_dir / "candidates.json").read_text())
    assert machine["candidate"]["evidence"]["source"] == "db:V1"

    second = _run("rsi-cycle", "proposal.py", run_dir=run_dir, input_dir=inputs,
                  extra_env={"MO_RSI_SUGGEST_BRIEF": str(brief)})
    assert second.returncode == 0, second.stderr
    proposal = json.loads((run_dir / "proposal.json").read_text())
    assert proposal["files"] == [FILE], "a duplicated path is one file, not a wider diff"
    assert proposal["candidate_id"] == "rsi-abc"
    assert proposal["verification_command"] == "db:V1"


def test_brief_strips_provenance_before_a_lens_sees_it(tmp_path):
    brief, _ = _brief_file(tmp_path)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    first = _run("rsi-cycle", "brief.py", run_dir=run_dir, input_dir=tmp_path,
                 extra_env={"MO_RSI_SUGGEST_BRIEF": str(brief)})
    assert first.returncode == 0, first.stderr
    # candidates.json is the machine copy the cycle re-reads; it keeps provenance.
    machine = json.loads((run_dir / "candidates.json").read_text())
    assert machine["candidate"]["evidence"]["source"] == "db:V1"
    # The rendered brief does not.
    assert "db:V1" not in (run_dir / "brief.md").read_text(encoding="utf-8").split("## Evidence")[1].split("## What")[0]


def test_proposal_carries_a_new_file_instead_of_dropping_it(tmp_path):
    """The builder must not silently turn a two-part fix into half a fix.

    Cycle 7's synthesis declared the migration under ``new_files`` and this node
    dropped the key, so the kickoff named no schema change and G8 could not pass.
    """
    brief, _ = _brief_file(tmp_path)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    inputs = tmp_path / "inputs"
    (inputs / "suggestion").mkdir(parents=True)
    created = str(Path(FILE).parent / "0085_new_thing.sql")
    (inputs / "suggestion" / "suggestion.json").write_text(json.dumps({
        "candidate_id": "rsi-abc",
        "files": [FILE],
        "new_files": [created],
        "change_summary": "add the column",
        "verification_command": "db:V1",
    }), encoding="utf-8")

    out = _run("rsi-cycle", "proposal.py", run_dir=run_dir, input_dir=inputs,
               extra_env={"MO_RSI_SUGGEST_BRIEF": str(brief)})
    assert out.returncode == 0, out.stderr
    proposal = json.loads((run_dir / "proposal.json").read_text())
    assert proposal["files"] == [FILE]
    assert proposal["new_files"] == [created]


def test_proposal_counts_new_files_against_the_limit(tmp_path):
    brief, _ = _brief_file(tmp_path)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    inputs = tmp_path / "inputs"
    (inputs / "suggestion").mkdir(parents=True)
    parent = Path(FILE).parent
    (inputs / "suggestion" / "suggestion.json").write_text(json.dumps({
        "candidate_id": "rsi-abc",
        "files": [FILE],
        "new_files": [str(parent / f"new_{i}.py") for i in range(5)],
        "verification_command": "db:V1",
    }), encoding="utf-8")

    out = _run("rsi-cycle", "proposal.py", run_dir=run_dir, input_dir=inputs,
               extra_env={"MO_RSI_SUGGEST_BRIEF": str(brief)})
    assert out.returncode != 0
    assert "the limit is" in out.stderr


def test_proposal_fails_on_a_new_file_under_a_missing_directory(tmp_path):
    brief, _ = _brief_file(tmp_path)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    inputs = tmp_path / "inputs"
    (inputs / "suggestion").mkdir(parents=True)
    (inputs / "suggestion" / "suggestion.json").write_text(json.dumps({
        "candidate_id": "rsi-abc",
        "files": [FILE],
        "new_files": ["db/no_such_dir/0085_x.sql"],
        "verification_command": "db:V1",
    }), encoding="utf-8")

    out = _run("rsi-cycle", "proposal.py", run_dir=run_dir, input_dir=inputs,
               extra_env={"MO_RSI_SUGGEST_BRIEF": str(brief)})
    assert out.returncode != 0
    assert "does not exist" in out.stderr


def test_proposal_rejects_a_directory_in_files(tmp_path):
    brief, _ = _brief_file(tmp_path)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    inputs = tmp_path / "inputs"
    (inputs / "suggestion").mkdir(parents=True)
    (inputs / "suggestion" / "suggestion.json").write_text(json.dumps({
        "candidate_id": "rsi-abc",
        "files": [str(Path(FILE).parent)],
        "verification_command": "db:V1",
    }), encoding="utf-8")

    out = _run("rsi-cycle", "proposal.py", run_dir=run_dir, input_dir=inputs,
               extra_env={"MO_RSI_SUGGEST_BRIEF": str(brief)})
    assert out.returncode != 0
    assert "is a directory" in out.stderr


def test_proposal_fails_on_a_path_that_does_not_exist(tmp_path):
    brief, _ = _brief_file(tmp_path)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    inputs = tmp_path / "inputs"
    (inputs / "suggestion").mkdir(parents=True)
    (inputs / "suggestion" / "suggestion.json").write_text(json.dumps({
        "candidate_id": "rsi-abc",
        "files": ["mini_ork/invented.py"],
    }), encoding="utf-8")

    out = _run("rsi-cycle", "proposal.py", run_dir=run_dir, input_dir=inputs,
               extra_env={"MO_RSI_SUGGEST_BRIEF": str(brief)})
    assert out.returncode != 0
    assert "does not exist" in out.stderr


def test_proposal_fails_when_the_synthesis_answers_another_candidate(tmp_path):
    brief, _ = _brief_file(tmp_path)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    inputs = tmp_path / "inputs"
    (inputs / "suggestion").mkdir(parents=True)
    (inputs / "suggestion" / "suggestion.json").write_text(json.dumps({
        "candidate_id": "rsi-someone-else",
        "files": [FILE],
    }), encoding="utf-8")

    out = _run("rsi-cycle", "proposal.py", run_dir=run_dir, input_dir=inputs,
               extra_env={"MO_RSI_SUGGEST_BRIEF": str(brief)})
    assert out.returncode != 0
    assert "dispatched for" in out.stderr


def test_critic_merge_carries_every_verdict_and_refuses_an_abstention(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    inputs = tmp_path / "inputs"
    (inputs / "verdicts").mkdir(parents=True)
    (inputs / "verdicts" / "glm_lens.json").write_text(json.dumps({
        "approve": True, "blocking_findings": [], "confidence": 0.8, "why": "traced"}),
        encoding="utf-8")
    (inputs / "verdicts" / "deepseek_lens.json").write_text(json.dumps({
        "approve": False, "blocking_findings": ["the test was deleted"]}),
        encoding="utf-8")

    out = _run("rsi-critic", "merge.py", run_dir=run_dir, input_dir=inputs, extra_env={})
    assert out.returncode == 0, out.stderr
    merged = json.loads((run_dir / "critics.json").read_text())
    by_alias = {c["alias"]: c for c in merged["critics"]}
    assert sorted(by_alias) == ["deepseek_lens", "glm_lens"]
    assert by_alias["deepseek_lens"]["blocking_findings"] == ["the test was deleted"]

    (inputs / "verdicts" / "glm_lens.json").write_text(json.dumps({"approve": "no"}), encoding="utf-8")
    out = _run("rsi-critic", "merge.py", run_dir=run_dir, input_dir=inputs, extra_env={})
    assert out.returncode != 0, "a string 'no' must never coerce to approval"
    assert "JSON boolean" in out.stderr


# --------------------------------------------------------------------------
# The panel grades its own lanes, not every actor that logged a call
# --------------------------------------------------------------------------


def _llm_db(tmp_path: Path, rows: list[tuple[str, str, str, str]]) -> Path:
    """A minimal ``llm_calls`` the panel verifier can grade."""
    import sqlite3

    db = tmp_path / "state.db"
    con = sqlite3.connect(db)
    try:
        con.execute("CREATE TABLE llm_calls (run_id TEXT, actor TEXT, model_id TEXT, status TEXT)")
        con.executemany("INSERT INTO llm_calls VALUES (?,?,?,?)", rows)
        con.commit()
    finally:
        con.close()
    return db


def test_the_suggest_panel_grades_only_the_three_declared_lenses(tmp_path):
    """The synthesiser and the engine's own lanes are not panel members.

    The first live cycle graded them too and reported
    ``same_family:codex_lens,deepseek_lens`` on a recipe that was correct, which
    would quarantine every cycle.
    """
    db = _llm_db(tmp_path, [
        ("rsi-ctest", "deepseek_lens", "deepseek-v3", "success"),
        ("rsi-ctest", "glm_lens", "glm-4.6", "success"),
        ("rsi-ctest", "minimax_lens", "minimax-m2", "success"),
        ("rsi-ctest", "codex_lens", "deepseek-v3", "success"),
        ("rsi-ctest", "planner", "glm-4.6", "success"),
        ("rsi-ctest", "profile_answerer", "minimax-m2", "success"),
    ])
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    out = _run("rsi-cycle", "panel.py", run_dir=run_dir, input_dir=tmp_path,
               extra_env={"MINI_ORK_DB": str(db)})

    assert out.returncode == 0, out.stderr
    panel = json.loads((run_dir / "panel-composition.json").read_text())
    assert panel["ok"] is True, panel["problems"]
    assert panel["lanes"] == ["deepseek_lens", "glm_lens", "minimax_lens"]
    assert sorted(panel["families"]) == ["deepseek_lens", "glm_lens", "minimax_lens"]


def test_the_suggest_panel_still_fails_when_two_lenses_share_a_family(tmp_path):
    db = _llm_db(tmp_path, [
        ("rsi-ctest", "deepseek_lens", "deepseek-v3", "success"),
        ("rsi-ctest", "glm_lens", "deepseek-v3", "success"),
        ("rsi-ctest", "minimax_lens", "minimax-m2", "success"),
    ])
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    out = _run("rsi-cycle", "panel.py", run_dir=run_dir, input_dir=tmp_path,
               extra_env={"MINI_ORK_DB": str(db)})

    assert out.returncode != 0
    assert "same_family" in out.stderr


def test_the_critic_panel_grades_only_the_two_declared_critics(tmp_path):
    brief, _ = _brief_file(tmp_path)
    brief.write_text(json.dumps({**json.loads(brief.read_text()),
                                 "implementer_lane": "codex_lens"}), encoding="utf-8")
    db = _llm_db(tmp_path, [
        ("rsi-ctest", "glm_lens", "glm-4.6", "success"),
        ("rsi-ctest", "minimax_lens", "minimax-m2", "success"),
        ("rsi-ctest", "codex_lens", "deepseek-v3", "success"),
        ("rsi-ctest", "planner", "glm-4.6", "success"),
        ("rsi-ctest", "profile_answerer", "minimax-m2", "success"),
    ])
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    out = _run("rsi-critic", "panel.py", run_dir=run_dir, input_dir=tmp_path,
               extra_env={"MINI_ORK_DB": str(db), "MO_RSI_CRITIC_BRIEF": str(brief)})

    assert out.returncode == 0, out.stderr
    panel = json.loads((run_dir / "panel-composition.json").read_text())
    assert panel["lanes"] == ["glm_lens", "minimax_lens"]
    assert sorted(panel["families"]) == ["glm_lens", "minimax_lens"]


# --------------------------------------------------------------------------
# SUGGEST: the seam
# --------------------------------------------------------------------------


def test_default_propose_refuses_when_the_recipe_is_not_installed(tmp_path):
    with pytest.raises(CycleFailure, match="recipes/rsi-cycle"):
        _default_propose(_req(tmp_path), _cand())


def test_recipe_propose_parses_the_panels_proposal(tmp_path, monkeypatch):
    cand = _cand()
    seen = _fake_run(monkeypatch, {"proposal.json": {
        "candidate_id": cand.candidate_id,
        "files": [FILE],
        "summary": "persist the column",
        "root_cause": "the producer is unwired",
        "ac_ids": ["V1"],
        "verification_command": cand.success_command,
    }})

    proposal = _recipe_propose(_req(tmp_path), cand)

    assert proposal.candidate_id == cand.candidate_id
    assert proposal.files == (FILE,)
    assert proposal.verification_command == cand.success_command
    assert seen["recipe"] == "rsi-cycle"
    assert seen["brief_env"] == "MO_RSI_SUGGEST_BRIEF"
    assert seen["brief"]["candidate"]["candidate_id"] == cand.candidate_id
    assert seen["brief"]["constraints"]["verification_command"] == cand.success_command


def test_recipe_propose_fails_cleanly_without_an_artifact(tmp_path, monkeypatch):
    _fake_run(monkeypatch, {}, rc=1)
    with pytest.raises(CycleFailure, match="without a proposal.json"):
        _recipe_propose(_req(tmp_path), _cand())


def test_recipe_propose_refuses_a_proposal_from_a_failed_contract(tmp_path, monkeypatch):
    """The artifact existing does not make it admissible.

    Cycle 3's synthesizer wrote ``proposal.json`` and the panel verifier then
    failed; the cycle carried the panel's false "three families" claim into
    IMPLEMENT. The run's own receipt names the reason and must reach the error.
    """
    cand = _cand()
    _fake_run(monkeypatch, {
        "proposal.json": {
            "candidate_id": cand.candidate_id,
            "files": [FILE],
            "summary": "persist the column",
            "root_cause": "the producer is unwired",
            "ac_ids": ["V1"],
            "verification_command": cand.success_command,
        },
        "verify-result.json": {
            "verdict": "fail",
            "results": [{"verifier": "verifiers/verify.py", "pass": False, "rc": 1,
                         "detail": "the panel recorded problems: "
                                   "['same_family:deepseek_lens,minimax_lens:minimax']"}],
        },
    }, rc=1)

    with pytest.raises(CycleFailure, match="failed contract verify.*same_family"):
        _recipe_propose(_req(tmp_path), cand)


def test_recipe_propose_still_accepts_a_proposal_from_a_clean_contract(tmp_path, monkeypatch):
    cand = _cand()
    _fake_run(monkeypatch, {
        "proposal.json": {
            "candidate_id": cand.candidate_id,
            "files": [FILE],
            "summary": "persist the column",
            "root_cause": "the producer is unwired",
            "ac_ids": ["V1"],
            "verification_command": cand.success_command,
        },
        "verify-result.json": {"verdict": "pass", "results": [
            {"verifier": "verifiers/verify.py", "pass": True, "rc": 0}]},
    }, rc=0)

    assert _recipe_propose(_req(tmp_path), cand).candidate_id == cand.candidate_id


def test_recipe_propose_rejects_an_artifact_that_is_not_json(tmp_path, monkeypatch):
    def _run(req, recipe, brief, kickoff, *, timeout_s, brief_env):
        home = req.home / "rsi" / req.cycle_id / recipe / "home"
        run_dir = home / "runs" / f"{req.cycle_id}-{recipe}"
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "proposal.json").write_text("not json", encoding="utf-8")
        return 0, home, run_dir

    monkeypatch.setattr(cycle_mod, "_run_recipe", _run)
    with pytest.raises(CycleFailure, match="is not JSON"):
        _recipe_propose(_req(tmp_path), _cand())


# --------------------------------------------------------------------------
# Critics: the seam
# --------------------------------------------------------------------------


def test_default_critics_returns_nothing_without_the_recipe(tmp_path):
    result = _default_critics(_req(tmp_path), _cand(), _impl(_cand()), _selection(_cand()),
                              evaluate(GateEvidence()))
    assert tuple(result) == ()
    assert getattr(result, "panel", None) is None


def test_recipe_critics_returns_the_verdicts_and_the_served_panel(tmp_path, monkeypatch):
    cand = _cand()
    panel = {"ok": True, "problems": [], "served": {"glm_lens": "glm-4.6"},
             "families": {"glm_lens": ["zai"], "deepseek_lens": ["anthropic"]}}
    seen = _fake_run(monkeypatch, {
        "critics.json": {"critics": [
            {"alias": "glm_lens", "lane": "glm_lens", "approve": True, "blocking_findings": []},
            {"alias": "deepseek_lens", "lane": "deepseek_lens", "approve": True, "blocking_findings": []},
        ], "count": 2},
        "panel-composition.json": panel,
    })

    result = _recipe_critics(_req(tmp_path), cand, _impl(cand), _selection(cand),
                             evaluate(GateEvidence()))

    assert isinstance(result, CriticsResult)
    assert [v.alias for v in result] == ["glm_lens", "deepseek_lens"]
    assert all(v.approve for v in result)
    assert result.panel == panel
    assert seen["recipe"] == "rsi-critic"
    assert seen["brief_env"] == "MO_RSI_CRITIC_BRIEF"
    assert seen["brief"]["diff"].startswith("diff --git")
    assert seen["brief"]["implementer_lane"] == "codex_lens"


def test_recipe_critics_quarantines_instead_of_raising_when_the_run_fails(tmp_path, monkeypatch):
    _fake_run(monkeypatch, {}, rc=1)
    cand = _cand()
    result = _recipe_critics(_req(tmp_path), cand, _impl(cand), _selection(cand),
                             evaluate(GateEvidence()))
    assert tuple(result) == ()
    assert result.panel is None


def test_recipe_critics_treats_an_unreadable_panel_as_unmeasured(tmp_path, monkeypatch):
    def _run(req, recipe, brief, kickoff, *, timeout_s, brief_env):
        home = req.home / "rsi" / req.cycle_id / recipe / "home"
        run_dir = home / "runs" / f"{req.cycle_id}-{recipe}"
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "critics.json").write_text(json.dumps({"critics": [
            {"alias": "glm_lens", "lane": "glm_lens", "approve": True},
        ]}), encoding="utf-8")
        (run_dir / "panel-composition.json").write_text("{", encoding="utf-8")
        return 0, home, run_dir

    monkeypatch.setattr(cycle_mod, "_run_recipe", _run)
    cand = _cand()
    result = _recipe_critics(_req(tmp_path), cand, _impl(cand), _selection(cand),
                             evaluate(GateEvidence()))
    assert len(result) == 1
    assert result.panel is None, "an unparseable panel must not become a passing one"


# --------------------------------------------------------------------------
# A dispatched run can still reach the operator's credentials
# --------------------------------------------------------------------------


def test_child_env_points_a_foreign_home_at_the_operators_secret_store(tmp_path, monkeypatch):
    monkeypatch.delenv("MINI_ORK_SECRETS", raising=False)
    req = _req(tmp_path)
    store = tmp_path / "config" / "secrets.local.sh"
    store.parent.mkdir()
    store.write_text("export OPENROUTER_API_KEY=x\n", encoding="utf-8")
    home = tmp_path / "rsi" / "rsi-ctest" / "child-home"

    env = _child_env(req, home)

    assert env["MINI_ORK_SECRETS"] == str(store)
    assert env["MINI_ORK_HOME"] == str(home)
    # The launcher prefers PROJECT_HOME and the cycle inherits its own; leaving
    # it stale sent the first child's run to the primary .mini-ork/runs.
    assert env["MINI_ORK_PROJECT_HOME"] == str(home)
    assert env["MINI_ORK_ROOT"] == str(tmp_path)
    assert env["MINI_ORK_DB"] == str(tmp_path / "state.db")


def test_child_env_never_names_a_store_that_does_not_exist(tmp_path, monkeypatch):
    """An explicit-but-wrong path fails where the default would have resolved."""
    monkeypatch.delenv("MINI_ORK_SECRETS", raising=False)

    env = _child_env(_req(tmp_path), tmp_path / "child-home")

    assert "MINI_ORK_SECRETS" not in env


def test_child_env_keeps_a_store_the_operator_chose(tmp_path, monkeypatch):
    chosen = tmp_path / "elsewhere.sh"
    chosen.write_text("export OPENROUTER_API_KEY=y\n", encoding="utf-8")
    monkeypatch.setenv("MINI_ORK_SECRETS", str(chosen))
    local = tmp_path / "config" / "secrets.local.sh"
    local.parent.mkdir()
    local.write_text("export OPENROUTER_API_KEY=x\n", encoding="utf-8")

    env = _child_env(_req(tmp_path), tmp_path / "child-home")

    assert env["MINI_ORK_SECRETS"] == str(chosen)


def test_child_env_hands_the_child_the_operators_lane_overlay(tmp_path, monkeypatch):
    """A fresh child home has no ``agents.local.yaml``: without the pointer the
    child silently runs the *stock* alias map (2026-10-25)."""
    monkeypatch.delenv("MINI_ORK_AGENTS", raising=False)
    req = _req(tmp_path)
    overlay = tmp_path / "config" / "agents.local.yaml"
    overlay.parent.mkdir()
    overlay.write_text("lanes:\n  deepseek_lens: deepseek_flash\n", encoding="utf-8")

    env = _child_env(req, tmp_path / "rsi" / "rsi-ctest" / "child-home")

    assert env["MINI_ORK_AGENTS"] == str(overlay)


def test_child_env_never_names_an_overlay_that_does_not_exist(tmp_path, monkeypatch):
    """``agents_config`` raises on a missing MINI_ORK_AGENTS — worse than none."""
    monkeypatch.delenv("MINI_ORK_AGENTS", raising=False)

    env = _child_env(_req(tmp_path), tmp_path / "child-home")

    assert "MINI_ORK_AGENTS" not in env


def test_child_env_keeps_an_overlay_the_operator_chose(tmp_path, monkeypatch):
    chosen = tmp_path / "elsewhere.yaml"
    chosen.write_text("lanes:\n  deepseek_lens: deepseek_flash\n", encoding="utf-8")
    monkeypatch.setenv("MINI_ORK_AGENTS", str(chosen))
    local = tmp_path / "config" / "agents.local.yaml"
    local.parent.mkdir()
    local.write_text("lanes:\n  deepseek_lens: glm\n", encoding="utf-8")

    env = _child_env(_req(tmp_path), tmp_path / "child-home")

    assert env["MINI_ORK_AGENTS"] == str(chosen)


def test_every_lane_the_rsi_recipes_declare_resolves_from_the_tracked_map():
    """The portable tier's guard: a fresh home reads only ``config/agents.yaml``.

    An alias a recipe names but the tracked map omits is not a *missing* lane —
    dispatch treats it as a literal provider name, raises "unknown lane", and the
    node falls back to whatever the health filter picks. The panel then reports
    a family collapse the recipe never caused.
    """
    tracked = yaml.safe_load((REPO / "config" / "agents.yaml").read_text(encoding="utf-8"))
    lanes = tracked["lanes"]
    missing: list[str] = []
    for recipe in ("rsi-cycle", "rsi-critic"):
        workflow = yaml.safe_load(
            (REPO / "recipes" / recipe / "workflow.yaml").read_text(encoding="utf-8"))
        for node in workflow.get("nodes") or []:
            lane = str((node or {}).get("model_lane") or "").strip()
            if lane and lane not in lanes:
                missing.append(f"{recipe}:{node.get('name')} -> {lane}")

    assert not missing, f"lanes a fresh home cannot resolve: {missing}"


def test_run_recipe_hands_the_store_to_the_child(tmp_path, monkeypatch):
    monkeypatch.delenv("MINI_ORK_SECRETS", raising=False)
    store = tmp_path / "config" / "secrets.local.sh"
    store.parent.mkdir()
    store.write_text("export OPENROUTER_API_KEY=x\n", encoding="utf-8")
    seen: dict = {}

    def _run(argv, **kwargs):
        seen.update(argv=argv, env=kwargs["env"])
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(cycle_mod.subprocess, "run", _run)

    rc, home, run_dir = _run_recipe(_req(tmp_path), "rsi-cycle", {"candidate": {}},
                                    "# kickoff\n", timeout_s=600,
                                    brief_env="MO_RSI_SUGGEST_BRIEF")

    assert rc == 0
    assert seen["env"]["MINI_ORK_SECRETS"] == str(store)
    assert seen["env"]["MINI_ORK_HOME"] == str(home)
    assert seen["env"]["MO_RSI_SUGGEST_BRIEF"].endswith("brief.json")
    assert run_dir == home / "runs" / "rsi-ctest-rsi-cycle"


def test_child_env_runs_the_engine_the_caller_names(tmp_path, monkeypatch):
    """A child that edits must run the tree it edits, not the primary checkout."""
    worktree = tmp_path / "wt"
    worktree.mkdir()

    env = _child_env(_req(tmp_path), tmp_path / "child-home", root=worktree)

    assert env["MINI_ORK_ROOT"] == str(worktree)
    # First in the launcher's lookup: left inherited, the child imports the
    # primary package while its argv and cwd are the worktree.
    assert env["MINI_ORK_ENGINE_ROOT"] == str(worktree)


def test_child_env_drops_a_stale_target_repo(tmp_path, monkeypatch):
    """An inherited target names the primary checkout from inside a worktree."""
    monkeypatch.setenv("MINI_ORK_TARGET_REPO", str(tmp_path / "primary"))
    worktree = tmp_path / "wt"
    worktree.mkdir()

    env = _child_env(_req(tmp_path), tmp_path / "child-home", root=worktree)

    assert "MINI_ORK_TARGET_REPO" not in env


def test_the_implementer_child_runs_the_worktrees_engine(tmp_path, monkeypatch):
    monkeypatch.delenv("MINI_ORK_SECRETS", raising=False)
    req = _req(tmp_path)
    wt = SimpleNamespace(path=tmp_path / "wt")
    child_home = tmp_path / "rsi" / "rsi-ctest" / "child-home"
    child_home.mkdir(parents=True)
    seen: dict = {}

    def _run(argv, **kwargs):
        seen.update(argv=argv, cwd=kwargs["cwd"], env=kwargs["env"])
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(cycle_mod.subprocess, "run", _run)

    run_id, rc, _out = _dispatch_child(req, wt, "# kickoff\n", child_home)

    assert (run_id, rc) == ("rsi-ctest-fe", 0)
    assert seen["env"]["MINI_ORK_ROOT"] == str(wt.path)
    assert seen["cwd"] == str(wt.path)
    assert seen["argv"][2] == str(wt.path / "bin" / "mini-ork")


# --------------------------------------------------------------------------
# G10 reads what the seam produced
# --------------------------------------------------------------------------


def _gate(report, gate_id: str):
    return next(r for r in report.results if r.gate == gate_id)


def _approving() -> tuple[CriticVerdict, ...]:
    return (CriticVerdict("glm_lens", lane="glm_lens", approve=True),
            CriticVerdict("deepseek_lens", lane="deepseek_lens", approve=True))


def test_a_panel_without_verdicts_is_not_attached():
    evidence = _with_critics(GateEvidence(), CriticsResult((), panel={"ok": True}))
    assert evidence.critics == ()
    assert evidence.critic_panel is None


def test_a_plain_tuple_carries_no_panel():
    evidence = _with_critics(GateEvidence(), _approving())
    assert len(evidence.critics) == 2
    assert evidence.critic_panel is None


def test_the_served_panel_travels_with_the_verdicts():
    panel = {"ok": True, "problems": []}
    evidence = _with_critics(GateEvidence(), CriticsResult(_approving(), panel=panel))
    assert evidence.critic_panel == panel


def test_g10_fails_when_the_served_panel_has_problems():
    panel = {"ok": False, "problems": ["same_family:glm_lens,deepseek_lens"]}
    evidence = GateEvidence(critics=_approving(), implementer_family="codex_lens",
                            critic_panel=panel)
    result = _gate(evaluate(evidence), "G10")
    assert not result.ok
    assert result.code == "critic_not_exogenous"
    assert "same_family" in result.detail


def test_g10_passes_on_two_exogenous_approvals_and_an_ok_panel():
    evidence = GateEvidence(critics=_approving(), implementer_family="codex_lens",
                            critic_panel={"ok": True, "problems": []})
    assert _gate(evaluate(evidence), "G10").ok


def test_g10_rejects_a_split_panel():
    verdicts = (CriticVerdict("glm_lens", lane="glm_lens", approve=True),
                CriticVerdict("deepseek_lens", lane="deepseek_lens", approve=False))
    result = _gate(evaluate(GateEvidence(critics=verdicts,
                                         implementer_family="codex_lens")), "G10")
    assert not result.ok
    assert result.code == "critic_split"


def test_g10_rejects_a_critic_in_the_implementers_family():
    verdicts = (CriticVerdict("glm_lens", lane="glm_lens", approve=True),
                CriticVerdict("codex_lens", lane="codex_lens", approve=True))
    result = _gate(evaluate(GateEvidence(critics=verdicts,
                                         implementer_family="codex_lens")), "G10")
    assert not result.ok
    assert result.code == "critic_not_exogenous"
