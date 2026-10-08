"""``mini_ork.ide_pages.run_flow`` — the run flow map model."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from mini_ork.ide_pages import run as run_page
from mini_ork.ide_pages import run_flow
from mini_ork.ide_pages.run import Node, Run
from mini_ork.stores import migrate as mig

REPO = Path(__file__).resolve().parents[2]
RUN = "run-1791000000-abc123"
T0 = 1_791_000_000
SHA = "abc1234def5678901234567890abcdef12345678"

# framework-edit-shaped: planner, 2 researchers, implementer, 2 verifiers,
# reviewer, publisher, rollback.
WORKFLOW = """\
version: 1
task_class: demo
nodes:
  - {name: planner, type: planner, model_lane: planner, prompt_ref: prompts/planner.md}
  - {name: code_impact_lens, type: researcher, model_lane: lens, prompt_ref: prompts/lens.md}
  - {name: prior_art_lens, type: researcher, model_lane: lens, prompt_ref: prompts/lens.md}
  - {name: implementer, type: implementer, model_lane: worker, prompt_ref: prompts/implementer.md}
  - {name: static_check_verifier, type: verifier, verifier_ref: verifiers/static-check.py}
  - {name: test_verifier, type: verifier, verifier_ref: verifiers/test.py}
  - {name: reviewer, type: reviewer, model_lane: reviewer, prompt_ref: prompts/reviewer.md}
  - {name: publisher, type: publisher}
  - {name: rollback, type: rollback}
edges:
  - {from: planner, to: code_impact_lens, edge_type: depends_on}
  - {from: planner, to: prior_art_lens, edge_type: depends_on}
  - {from: code_impact_lens, to: implementer, edge_type: depends_on}
  - {from: implementer, to: static_check_verifier, edge_type: verifies}
  - {from: implementer, to: test_verifier, edge_type: verifies}
  - {from: static_check_verifier, to: reviewer, edge_type: depends_on}
  - {from: test_verifier, to: reviewer, edge_type: depends_on}
  - {from: reviewer, to: publisher, edge_type: depends_on}
  - {from: test_verifier, to: reviewer, edge_type: retries}
  - {from: test_verifier, to: implementer, edge_type: retries}
  - {from: reviewer, to: implementer, edge_type: retries}
  - {from: reviewer, to: rollback, edge_type: escalates_to}
"""

# A research recipe: no implementer, so the researchers form the build lanes.
RESEARCH = """\
version: 1
task_class: research
nodes:
  - {name: planner, type: planner, model_lane: planner, prompt_ref: prompts/planner.md}
  - {name: lens_a, type: researcher, model_lane: lens, prompt_ref: prompts/lens.md}
  - {name: lens_b, type: researcher, model_lane: lens, prompt_ref: prompts/lens.md}
  - {name: lens_c, type: researcher, model_lane: lens, prompt_ref: prompts/lens.md}
  - {name: lens_d, type: researcher, model_lane: lens, prompt_ref: prompts/lens.md}
  - {name: reviewer, type: reviewer, model_lane: reviewer, prompt_ref: prompts/reviewer.md}
  - {name: test_verifier, type: verifier, verifier_ref: verifiers/test.py}
edges:
  - {from: planner, to: lens_a, edge_type: depends_on}
  - {from: planner, to: lens_b, edge_type: depends_on}
  - {from: planner, to: lens_c, edge_type: depends_on}
  - {from: planner, to: lens_d, edge_type: depends_on}
  - {from: lens_a, to: reviewer, edge_type: depends_on}
  - {from: reviewer, to: test_verifier, edge_type: depends_on}
"""

# The research recipe with a retries edge targeting a researcher: with no
# implementer the researcher is a build lane, so the loop belongs to build.
RESEARCH_RETRY = RESEARCH + """\
  - {from: reviewer, to: lens_a, edge_type: retries}
"""

# A plan critic (an eval downstream of the planner) and a run eval (downstream
# of the implementer) — only the former grades the plan.
PLAN_EVAL = """\
version: 1
task_class: demo
nodes:
  - {name: planner, type: planner, model_lane: planner}
  - {name: plan_critic, type: eval, model_lane: judge}
  - {name: implementer, type: implementer, model_lane: worker}
  - {name: run_eval, type: eval, model_lane: judge}
edges:
  - {from: planner, to: plan_critic, edge_type: depends_on}
  - {from: planner, to: implementer, edge_type: depends_on}
  - {from: implementer, to: run_eval, edge_type: depends_on}
"""


class _FakeWS:
    """The attribute surface ``workspaces.status`` reads."""

    def __init__(self, path: Path, branch: str = "wt/x", base_sha: str = "0" * 40) -> None:
        self.path = Path(path)
        self.branch = branch
        self.base_sha = base_sha


def _recipe(tmp_path: Path, workflow: str) -> Path:
    d = tmp_path / "recipes" / "demo-recipe"
    d.mkdir(parents=True, exist_ok=True)
    (d / "workflow.yaml").write_text(workflow)
    return d


def _run(tmp_path: Path, nodes: list[Node], cols: list[list[str]], *,
         workflow: str = WORKFLOW, workspace=None, card: dict | None = None) -> Run:
    home = tmp_path / ".mini-ork"
    run_dir = home / "runs" / "run-1"
    run_dir.mkdir(parents=True, exist_ok=True)
    return Run(id="run-1", home=home, run_dir=run_dir, row={},
               card=card if card is not None else {"title": "Make the demo pass"},
               nodes=nodes, cols=cols, calls=[], recipe_dir=_recipe(tmp_path, workflow),
               workspace=workspace)


def _framework_nodes() -> list[Node]:
    return [
        Node(id="planner", type="planner", state="done", family="glm"),
        Node(id="code_impact_lens", type="researcher", state="done", family="glm"),
        Node(id="prior_art_lens", type="researcher", state="done", family="glm"),
        Node(id="implementer", type="implementer", state="done", family="minimax"),
        Node(id="static_check_verifier", type="verifier", state="done",
             prompt="demo-recipe/verifiers/static-check.py"),
        Node(id="test_verifier", type="verifier", state="done",
             prompt="demo-recipe/verifiers/test.py"),
        Node(id="reviewer", type="reviewer", state="done", family="opus"),
        Node(id="publisher", type="publisher", state="done"),
        Node(id="rollback", type="rollback", state="pending", finish="skipped"),
    ]


_FRAMEWORK_COLS = [["planner"], ["code_impact_lens", "prior_art_lens"], ["implementer"],
                   ["static_check_verifier", "test_verifier"], ["reviewer"], ["publisher"],
                   ["rollback"]]


def _seed_framework_files(run: Run, *, publish: bool = True) -> None:
    d = run.run_dir
    (d / "verifier_static-check.json").write_text(json.dumps(
        {"verifier": "static-check", "checks": [{"name": "ruff", "pass": True, "rc": 0}]}))
    (d / "verifier_test.json").write_text(json.dumps(
        {"verifier": "test", "checks": [{"name": "pytest", "pass": True, "rc": 0}]}))
    (d / "review-reviewer.json").write_text(json.dumps({"verdict": "pass", "findings": []}))
    (d / "rubric.json").write_text(json.dumps({
        "items": [{"label": "Alignment", "verdict": "PASS"},
                  {"label": "Correctness", "verdict": "PASS"}],
        "score": 7,
    }))
    if publish:
        (d / "execute.log").write_text(
            "[publish] a code change\n  [publish] committed 3 file(s): " + SHA + "\n")


# ── the framework-edit-shaped run ───────────────────────────────────────────

def test_framework_edit_shape(tmp_path: Path) -> None:
    run = _run(tmp_path, _framework_nodes(), _FRAMEWORK_COLS)
    _seed_framework_files(run)
    flow = run_flow.build_flow(run)

    assert flow["ticket"] == {"id": "run-1", "title": "Make the demo pass", "state": "done"}

    # Design: the planner + the 2 researchers.
    design_ids = [s["id"] for s in flow["design"]["nodes"]]
    assert design_ids == ["planner", "code_impact_lens", "prior_art_lens"]
    assert flow["design"]["nodes"][0]["label"] == "Plan"
    assert flow["design"]["nodes"][0]["sub"] == "writes the plan"

    # Build: one implementer → a single unlabeled package with one lane.
    assert len(flow["build"]["packages"]) == 1
    pkg = flow["build"]["packages"][0]
    assert pkg["label"] == "" and len(pkg["lanes"]) == 1
    assert pkg["lanes"][0]["role"] == "Implementer"
    assert pkg["lanes"][0]["node"]["id"] == "implementer"
    # A single implementer is not a fan-out, so no critic is attached even
    # though both verifiers name it as their only source.
    assert pkg["lanes"][0]["critic"] is None

    # Integrate: one tier per verifier.
    assert len(flow["integrate"]["tiers"]) == 2
    assert flow["integrate"]["node"]["label"] == "Integrate"
    labels = {t["label"] for t in flow["integrate"]["tiers"]}
    assert labels == {"static checks", "tests"}
    assert all(t["passed"] == 1 and t["total"] == 1 for t in flow["integrate"]["tiers"])

    # Review: a hub plus the rubric spokes.
    assert flow["review"]["hub"]["id"] == "reviewer"
    assert flow["review"]["score"] == "7/8"
    rubric = [s for s in flow["review"]["spokes"] if s["kind"] == "rubric"]
    assert [s["label"] for s in rubric] == ["Alignment", "Correctness"]
    assert all(s["state"] == "approved" for s in rubric)

    # Merge: present, carrying the commit sha.
    assert flow["merge"] is not None
    assert flow["merge"]["commit"] == SHA
    assert {"label": f"Committed {SHA[:7]}", "ok": True} in flow["merge"]["checks"]
    # The run committed its own change in place: no human merge step.
    assert flow["merge"]["human"] is None

    # Six stages, intake … merge.
    assert [s["key"] for s in flow["stages"]] == [
        "intake", "design", "build", "integrate", "review", "merge"]
    assert [s["state"] for s in flow["stages"]] == ["done", "approved", "approved",
                                                    "approved", "approved", "approved"]
    assert flow["legend"][0] == {"key": "in_progress", "label": "in progress"}


def test_no_publish_sha_and_no_worktree_hides_merge(tmp_path: Path) -> None:
    run = _run(tmp_path, _framework_nodes(), _FRAMEWORK_COLS)
    _seed_framework_files(run, publish=False)
    flow = run_flow.build_flow(run)
    assert flow["merge"] is None
    assert [s["key"] for s in flow["stages"]] == [
        "intake", "design", "build", "integrate", "review"]


def test_worktree_present_and_unmerged_needs_the_human(tmp_path: Path, monkeypatch) -> None:
    wt = tmp_path / "worktree"
    wt.mkdir()
    run = _run(tmp_path, _framework_nodes(), _FRAMEWORK_COLS, workspace=_FakeWS(wt),
               card={"title": "Make the demo pass", "status": "published"})
    _seed_framework_files(run, publish=False)
    # task_state rule 3: a published run whose worktree still has commits
    # ahead of its base is waiting on the human to merge or discard.
    monkeypatch.setattr("mini_ork.workspaces.status",
                        lambda _ws: {"exists": True, "commits_ahead": 1, "uncommitted": []})
    flow = run_flow.build_flow(run)
    assert flow["merge"] is not None
    assert flow["merge"]["commit"] == ""
    assert flow["merge"]["human"]["state"] == "in_progress"
    assert flow["merge"]["state"] == "in_progress"


def test_rolled_back_run_with_a_kept_worktree_has_no_merge(tmp_path: Path, monkeypatch) -> None:
    wt = tmp_path / "worktree"
    wt.mkdir()
    run = _run(tmp_path, _framework_nodes(), _FRAMEWORK_COLS, workspace=_FakeWS(wt),
               card={"title": "Make the demo pass", "status": "rolled_back"})
    _seed_framework_files(run, publish=False)
    (run.run_dir / "rolled-back.json").write_text(json.dumps({"paths": ["a.py"]}))
    # Even a worktree with real commits ahead is not a merge: only a
    # *published* run gets the merge bar.
    monkeypatch.setattr("mini_ork.workspaces.status",
                        lambda _ws: {"exists": True, "commits_ahead": 2, "uncommitted": []})
    flow = run_flow.build_flow(run)
    assert flow["merge"] is None
    assert [s["key"] for s in flow["stages"]] == [
        "intake", "design", "build", "integrate", "review"]
    assert flow["rollback"] == {"state": "done", "paths": 1}


# ── multi-implementer fan-out ───────────────────────────────────────────────

# verifies edges run implementer → verifier (the real recipe direction).
FANOUT = """\
version: 1
task_class: demo
nodes:
  - {name: implementer_a, type: implementer, model_lane: worker}
  - {name: implementer_b, type: implementer, model_lane: worker}
  - {name: check_a, type: verifier, verifier_ref: verifiers/a.py}
  - {name: check_b, type: verifier, verifier_ref: verifiers/b.py}
  - {name: check_both, type: verifier, verifier_ref: verifiers/both.py}
edges:
  - {from: implementer_a, to: check_a, edge_type: verifies}
  - {from: implementer_b, to: check_b, edge_type: verifies}
  - {from: implementer_a, to: check_both, edge_type: verifies}
  - {from: implementer_b, to: check_both, edge_type: verifies}
"""


def test_two_implementers_make_two_packages(tmp_path: Path) -> None:
    nodes = [
        Node(id="implementer_a", type="implementer", state="done", family="minimax"),
        Node(id="implementer_b", type="implementer", state="done", family="minimax"),
    ]
    run = _run(tmp_path, nodes, [["implementer_a"], ["implementer_b"]])
    flow = run_flow.build_flow(run)
    assert [p["label"] for p in flow["build"]["packages"]] == ["PACKAGE 1", "PACKAGE 2"]
    assert all(len(p["lanes"]) == 1 for p in flow["build"]["packages"])


def test_fan_out_attaches_each_implementers_own_critic(tmp_path: Path) -> None:
    nodes = [
        Node(id="implementer_a", type="implementer", state="done", family="minimax"),
        Node(id="implementer_b", type="implementer", state="done", family="minimax"),
        Node(id="check_a", type="verifier", state="done", prompt="demo-recipe/verifiers/a.py"),
        Node(id="check_b", type="verifier", state="done", prompt="demo-recipe/verifiers/b.py"),
        Node(id="check_both", type="verifier", state="done",
             prompt="demo-recipe/verifiers/both.py"),
    ]
    cols = [["implementer_a", "implementer_b"], ["check_a", "check_b", "check_both"]]
    run = _run(tmp_path, nodes, cols, workflow=FANOUT)
    flow = run_flow.build_flow(run)
    lanes = {p["lanes"][0]["node"]["id"]: p["lanes"][0]["critic"]
             for p in flow["build"]["packages"]}
    # check_a verifies only implementer_a, check_b only implementer_b, so each
    # is that lane's critic; check_both verifies both, so it is no one's critic.
    assert lanes["implementer_a"]["id"] == "check_a"
    assert lanes["implementer_b"]["id"] == "check_b"


# ── revise rounds ───────────────────────────────────────────────────────────

def _write_round(run: Run, n: int, node: str, ntype: str) -> None:
    revise = run.run_dir / "revise"
    revise.mkdir(parents=True, exist_ok=True)
    (revise / f"round-{n}.md").write_text(
        f"Round {n} of 2: fixes\n\n## {node} ({ntype})\n\nplease fix\n")


def test_review_fix_counts_only_reviewer_rounds(tmp_path: Path) -> None:
    run = _run(tmp_path, _framework_nodes(), _FRAMEWORK_COLS)
    _seed_framework_files(run)
    _write_round(run, 1, "reviewer", "reviewer")
    _write_round(run, 2, "reviewer", "reviewer")
    flow = run_flow.build_flow(run)
    assert flow["review"]["fix"] == {"rounds": 2, "state": "approved"}


def test_build_revise_loop_counts_the_source_rounds(tmp_path: Path) -> None:
    run = _run(tmp_path, _framework_nodes(), _FRAMEWORK_COLS)
    _seed_framework_files(run)
    _write_round(run, 1, "test_verifier", "verifier")
    _write_round(run, 2, "test_verifier", "verifier")
    flow = run_flow.build_flow(run)
    loops = [lp for lp in flow["build"]["revise"]
             if lp["from"] == "test_verifier" and lp["to"] == "implementer"]
    assert len(loops) == 1
    assert loops[0]["rounds"] == 2 and loops[0]["state"] == "approved"


# ── research recipe: researchers are the build lanes ────────────────────────

def test_research_recipe_lanes_are_the_researchers(tmp_path: Path) -> None:
    run = _run(tmp_path, _research_nodes(), _RESEARCH_COLS, workflow=RESEARCH)
    flow = run_flow.build_flow(run)
    assert len(flow["build"]["packages"]) == 1
    lanes = flow["build"]["packages"][0]["lanes"]
    assert [lane["node"]["id"] for lane in lanes] == ["lens_a", "lens_b", "lens_c", "lens_d"]
    assert all(lane["role"] == "Lens" for lane in lanes)
    # Design holds only the planner; the researchers moved into build.
    assert [s["id"] for s in flow["design"]["nodes"]] == ["planner"]


def _research_nodes() -> list[Node]:
    nodes = [Node(id="planner", type="planner", state="done", family="glm")]
    nodes += [Node(id=f"lens_{c}", type="researcher", state="done", family="glm")
              for c in "abcd"]
    nodes += [Node(id="reviewer", type="reviewer", state="done", family="opus"),
              Node(id="test_verifier", type="verifier", state="done",
                   prompt="demo-recipe/verifiers/test.py")]
    return nodes


_RESEARCH_COLS = [["planner"], ["lens_a", "lens_b", "lens_c", "lens_d"],
                  ["reviewer"], ["test_verifier"]]


def test_researcher_revise_loop_lives_in_build_not_design(tmp_path: Path) -> None:
    run = _run(tmp_path, _research_nodes(), _RESEARCH_COLS, workflow=RESEARCH_RETRY)
    _write_round(run, 1, "reviewer", "reviewer")
    flow = run_flow.build_flow(run)
    # The researcher is a build lane, so its loop is in build — not also in
    # design, where the researchers no longer appear.
    assert flow["design"]["revise"] == []
    assert [(lp["from"], lp["to"]) for lp in flow["build"]["revise"]] == [("reviewer", "lens_a")]


# ── plan score ──────────────────────────────────────────────────────────────

def test_plan_score_comes_only_from_an_eval_grading_the_plan(tmp_path: Path) -> None:
    nodes = [Node(id="planner", type="planner", state="done"),
             Node(id="plan_critic", type="eval", state="done"),
             Node(id="implementer", type="implementer", state="done"),
             Node(id="run_eval", type="eval", state="done")]
    run = _run(tmp_path, nodes, [["planner"], ["plan_critic"], ["implementer"], ["run_eval"]],
               workflow=PLAN_EVAL)
    (run.run_dir / "review-plan_critic.json").write_text(json.dumps({"score": 9}))
    (run.run_dir / "review-run_eval.json").write_text(json.dumps({"score": 3}))
    flow = run_flow.build_flow(run)
    plan = next(s for s in flow["design"]["nodes"] if s["id"] == "planner")
    assert plan["score"] == "9/10"


def test_plan_score_is_blank_when_no_eval_grades_the_plan(tmp_path: Path) -> None:
    nodes = [Node(id="planner", type="planner", state="done"),
             Node(id="implementer", type="implementer", state="done"),
             Node(id="run_eval", type="eval", state="done")]
    run = _run(tmp_path, nodes, [["planner"], ["implementer"], ["run_eval"]],
               workflow=PLAN_EVAL)
    # run_eval grades the run (its edge is from the implementer), not the plan.
    (run.run_dir / "review-run_eval.json").write_text(json.dumps({"score": 3}))
    flow = run_flow.build_flow(run)
    plan = next(s for s in flow["design"]["nodes"] if s["id"] == "planner")
    assert plan["score"] == ""


# ── running node ────────────────────────────────────────────────────────────

def test_a_running_node_is_in_progress(tmp_path: Path) -> None:
    nodes = _framework_nodes()
    for n in nodes:
        if n.id == "reviewer":
            n.state = "running"
    run = _run(tmp_path, nodes, _FRAMEWORK_COLS)
    _seed_framework_files(run)
    flow = run_flow.build_flow(run)
    assert flow["review"]["hub"]["state"] == "in_progress"
    review_stage = next(s for s in flow["stages"] if s["key"] == "review")
    assert review_stage["state"] == "in_progress"


# ── fail-soft ───────────────────────────────────────────────────────────────

def test_a_broken_section_stays_empty_not_raised(tmp_path: Path, monkeypatch) -> None:
    run = _run(tmp_path, _framework_nodes(), _FRAMEWORK_COLS)
    _seed_framework_files(run)
    monkeypatch.setattr(run_flow, "_review", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    flow = run_flow.build_flow(run)
    assert flow["review"] == {"hub": None, "verdict": "", "score": "", "spokes": [],
                              "fix": {"rounds": 0, "state": "none"}}
    assert flow["build"]["packages"]  # the other sections still render


# ── integration with run.build (level 1 vs level 2) ─────────────────────────

@pytest.fixture
def home(tmp_path: Path) -> Path:
    h = tmp_path / "proj" / ".mini-ork"
    h.mkdir(parents=True)
    rc, _out, err = mig.init_db(db=str(h / "state.db"), root=str(REPO))
    assert rc == 0, err
    recipe = h / "recipes" / "demo-recipe"
    recipe.mkdir(parents=True)
    (recipe / "workflow.yaml").write_text(WORKFLOW)
    (recipe / "task_class.yaml").write_text("name: demo\ndescription: a demo recipe\n")
    return h


def _seed(home: Path) -> Path:
    run_dir = home / "runs" / RUN
    run_dir.mkdir(parents=True)
    kickoff = home / "kickoffs" / "demo.md"
    kickoff.parent.mkdir(parents=True)
    kickoff.write_text("# Make the demo pass\n\nDetails.\n")
    con = sqlite3.connect(home / "state.db")
    con.execute(
        "INSERT INTO task_runs (id, recipe, status, cost_usd, created_at, updated_at, ended_at, "
        "task_class, kickoff_path, workflow_version, trace_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (RUN, "demo-recipe", "published", 0.5, T0, T0 + 120, T0 + 100, "demo",
         str(kickoff), "latest", "tr-demo-1"))
    events = [("node_start", "planner", "planner", "planner", T0 + 1, None),
              ("node_end", "planner", "planner", "planner", T0 + 3, "done"),
              ("node_start", "implementer", "implementer", "worker", T0 + 10, None),
              ("node_end", "implementer", "implementer", "worker", T0 + 40, "done")]
    for i, (kind, node, ntype, lane, ts, fin) in enumerate(events):
        payload = {"node_id": node, "node_type": ntype, "model_lane": lane}
        if fin:
            payload["finish_reason"] = fin
        con.execute("INSERT INTO run_events (event_id, run_id, event_type, payload_json, created_at) "
                    "VALUES (?,?,?,?,?)", (f"ev-{i}", RUN, kind, json.dumps(payload), ts))
    con.commit()
    con.close()
    (run_dir / "verifier_static-check.json").write_text(json.dumps(
        {"checks": [{"name": "ruff", "pass": True, "rc": 0}]}))
    (run_dir / "verifier_test.json").write_text(json.dumps(
        {"checks": [{"name": "pytest", "pass": True, "rc": 0}]}))
    (run_dir / "review-reviewer.json").write_text(json.dumps({"verdict": "pass"}))
    (run_dir / "execute.log").write_text("  [publish] committed 1 file(s): " + SHA + "\n")
    return run_dir


_FLOW_KEYS = {"ticket", "stages", "design", "build", "integrate", "review", "merge",
              "rollback", "timeline", "legend"}


def test_build_attaches_flow_at_level_two_only(home: Path, monkeypatch) -> None:
    _seed(home)
    monkeypatch.setenv("MINI_ORK_IDE_SPEC", "2")
    page = run_page.build(home, None, {"run": RUN})
    assert set(page["flow"]) == _FLOW_KEYS
    assert [s["key"] for s in page["flow"]["stages"]][-1] == "merge"
    assert page["flow"]["merge"]["commit"] == SHA
    assert [n["node"] for n in page["flow"]["timeline"]] == [
        "planner", "planner", "implementer", "implementer"]
    assert page["flow"]["timeline"][0]["event"] == "start"
    assert page["flow"]["timeline"][1]["state"] == "approved"
    assert page["errors"] == {}

    monkeypatch.delenv("MINI_ORK_IDE_SPEC", raising=False)
    page1 = run_page.build(home, None, {"run": RUN})
    assert "flow" not in page1


def test_build_degrades_flow_to_null_on_error(home: Path, monkeypatch) -> None:
    _seed(home)
    monkeypatch.setenv("MINI_ORK_IDE_SPEC", "2")

    def boom(_run):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(run_flow, "build_flow", boom)
    page = run_page.build(home, None, {"run": RUN})
    assert page["flow"] is None
    assert "kaboom" in page["errors"]["flow"]
    # The rest of the page still renders.
    assert page["sections"][0]["type"] == "triage"


def test_reviewer_needs_revision_is_sent_back_not_failed() -> None:
    """A reviewer that asked for a revision sent the work back (orange), it did
    not crash (red) — reviewers are never `retries` targets, so the verdict decides."""
    from types import SimpleNamespace

    from mini_ork.ide_pages import run_flow

    node = SimpleNamespace(id="reviewer", type="reviewer", state="failed")
    assert run_flow._step_state(node, False, set(), "needs_revision") == "sent_back"
    assert run_flow._step_state(node, False, set(), "") == "failed"
    crashed = SimpleNamespace(id="implementer", type="implementer", state="failed")
    assert run_flow._step_state(crashed, False, set(), "needs_revision") == "failed"


def test_green_abstention_counts_as_a_pass() -> None:
    """A verifier that abstained over a green suite (build-only command, replay
    n/a) passed what it could measure — the integrate tier must not read 0/1."""
    from mini_ork.ide_pages import run_flow

    assert run_flow._verifier_pass({"pass": False, "status": "unverified", "suite_green": True}) is True
    assert run_flow._verifier_pass({"pass": False, "status": "unverified"}) is False
    assert run_flow._verifier_pass({"pass": False}) is False


def test_revived_run_that_published_drops_the_stale_rollback(tmp_path: Path) -> None:
    run = _run(tmp_path, _framework_nodes(), _FRAMEWORK_COLS)
    _seed_framework_files(run, publish=True)
    (run.run_dir / "rolled-back.json").write_text(json.dumps({"paths": ["a.py", "b.py"]}))
    flow = run_flow.build_flow(run)
    assert flow["merge"]["commit"] == SHA
    assert flow["rollback"] is None
