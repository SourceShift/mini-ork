"""Standalone golden and behavioral contracts for the Python plan runtime.

The planner LLM dispatch is the one non-deterministic seam; MO_GIVEN_PLAN skips it
and flows a supplied plan through the SAME extraction → validation → fallback →
overlay → write → DB pipeline. The Bash oracle was captured by the durable
pre-retirement parity report before its entrypoint was removed. These tests keep
the certified golden values and exercise MO_GIVEN_PLAN, dry-run, profile gates,
flag errors, repair limits, native dispatch capture, and DB writes directly.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.cli import plan

_VALID = {
    "objective": "Ship widget", "assumptions": ["a"],
    "decomposition": [{"id": "s1", "description": "do", "node_type": "implementer", "depends_on": []}],
    "dependencies": [], "risk_notes": [],
    "artifact_contract": {"outputs": ["x"], "success_verifiers": ["v"]},
    "verifier_contract": {"checks": [{"id": "c1", "description": "check it"}]},
}


def _home(tmp, name):
    h = tmp / name / ".mini-ork"; h.mkdir(parents=True)
    db = str(h / "state.db")
    subprocess.run(["bash", str(REPO / "db" / "init.sh")],
                   env={**os.environ, "MINI_ORK_HOME": str(h), "MINI_ORK_DB": db},
                   capture_output=True, text=True, check=True)
    return str(h), db


def _kick(tmp):
    k = tmp / "k.md"; k.write_text("# Do the thing\n\n## Success\n- works\n")
    return str(k)


def _env(home, db, given=None, extra=None):
    e = {**os.environ, "MINI_ORK_ROOT": str(REPO), "MINI_ORK_HOME": home, "MINI_ORK_DB": db,
         "MINI_ORK_TASK_CLASS": "code_fix", "MO_INJECT_LEARNINGS": "0",
         "MINI_ORK_PROFILE_GATE": "0", "MINI_ORK_PROFILE_PATH": "",
         "MINI_ORK_NONINTERACTIVE": "1", "MO_AUTO_ANSWER_PROFILE": "0",
         "MINI_ORK_RUN_ID": "run-fixed-1"}
    if given:
        e["MO_GIVEN_PLAN"] = given
    if extra:
        e.update(extra)
    return e


def _run_py(home, db, kickoff, out, given=None, extra=None):
    old = dict(os.environ)
    os.environ.clear(); os.environ.update(_env(home, db, given, extra))
    try:
        rc = plan.main([kickoff, "--out", out], root=str(REPO))
    finally:
        os.environ.clear(); os.environ.update(old)
    return rc


def _run_py_dispatch(home, db, kickoff, out, dispatch, extra=None):
    old = dict(os.environ)
    os.environ.clear(); os.environ.update(_env(home, db, extra=extra))
    try:
        rc = plan.main([kickoff, "--out", out], root=str(REPO), dispatch=dispatch)
    finally:
        os.environ.clear(); os.environ.update(old)
    return rc


def _fake_dispatch(*responses):
    calls = {"n": 0}

    def dispatch(_task_class, _node_type, _prompt):
        i = min(calls["n"], len(responses) - 1)
        calls["n"] += 1
        return 0, responses[i]

    dispatch.calls = calls
    return dispatch


def _taskrow(db):
    # plan_path is expected to differ (separate out files per side); compare
    # status + plan_hash — the content-derived fields that must match.
    return subprocess.run(["sqlite3", db, "SELECT status,plan_hash FROM task_runs WHERE id='run-fixed-1';"],
                          capture_output=True, text=True).stdout.strip()


def _given(tmp, obj_or_text, name="given.json"):
    p = tmp / name
    p.write_text(obj_or_text if isinstance(obj_or_text, str) else json.dumps(obj_or_text))
    return str(p)


def _contract(tmp_path, name, given_text, *, extra=None):
    home, db = _home(tmp_path, name)
    k = _kick(tmp_path)
    g = _given(tmp_path, given_text, name + ".json")
    out = str(tmp_path / (name + ".out.json"))
    rc = _run_py(home, db, k, out, given=g, extra=extra)
    rendered = Path(out).read_text() if Path(out).exists() else ""
    return rc, rendered, _taskrow(db)


def test_valid_given_plan(tmp_path):
    rc, fp, taskrow = _contract(tmp_path, "valid", _VALID)
    assert rc == 0 and json.loads(fp)["objective"] == "Ship widget"
    assert json.loads(fp)["task_class"] == "code_fix"    # overlay stamps it
    assert taskrow.startswith("planned|")


def test_fenced_and_zinsight_bleed(tmp_path):
    # markdown fence + a trailing z-insight object; extraction must pick the plan
    raw = "```json\n" + json.dumps(_VALID) + "\n```\n<z-insight>{\"domain\":\"x\"}</z-insight>\n"
    rc, fp, _ = _contract(tmp_path, "fenced", raw)
    assert rc == 0 and json.loads(fp)["objective"] == "Ship widget"


def test_missing_verifier_no_recipe_rejected(tmp_path):
    bad = {**_VALID, "verifier_contract": {"checks": []}}
    rc, _, _ = _contract(tmp_path, "noverif", bad)
    assert rc == 1


def test_bad_node_type_rejected(tmp_path):
    bad = {**_VALID, "decomposition": [{"id": "s1", "node_type": "wizard", "depends_on": []}]}
    rc, _, _ = _contract(tmp_path, "badnt", bad)
    assert rc == 1


def test_placeholder_rejected(tmp_path):
    bad = {**_VALID, "objective": "<fill in>"}
    rc, _, _ = _contract(tmp_path, "ph", bad)
    assert rc == 1


def test_parse_error_no_recipe_rejected(tmp_path):
    rc, _, _ = _contract(tmp_path, "parse", "this is not json at all")
    assert rc == 1


def test_dry_run_golden(tmp_path):
    home, db = _home(tmp_path, "dry-run")
    k = _kick(tmp_path)
    out = str(tmp_path / "dry-run.json")
    old = dict(os.environ); os.environ.clear(); os.environ.update(_env(home, db))
    try:
        rc = plan.main([k, "--out", out, "--dry-run"], root=str(REPO))
    finally:
        os.environ.clear(); os.environ.update(old)
    assert rc == 0
    assert Path(out).read_text() == plan._DRY_RUN_PLACEHOLDER


def test_profile_gate_golden(tmp_path):
    home, db = _home(tmp_path, "profile-gate")
    k = _kick(tmp_path)
    # a needs_answers profile → gate blocks
    prof = tmp_path / "prof.json"
    prof.write_text(json.dumps({"profile_status": "needs_answers", "confidence": 0.4,
                                "human_questions": ["what?"]}))
    out = str(tmp_path / "profile-gate.json")
    extra = {"MINI_ORK_PROFILE_GATE": "1", "MINI_ORK_PROFILE_PATH": str(prof)}
    old = dict(os.environ); os.environ.clear(); os.environ.update(_env(home, db, extra=extra))
    try:
        rc = plan.main([k, "--out", out], root=str(REPO))
    finally:
        os.environ.clear(); os.environ.update(old)
    assert rc == 0
    payload = json.loads(Path(out).read_text())
    assert payload["plan_status"] == "needs_answers"
    assert payload["blocked_by"] == "run_profile"
    assert payload["confidence"] == 0.4
    assert payload["human_questions"] == ["what?"]


def test_profile_zero_questions_normalizes_and_continues(tmp_path):
    home, db = _home(tmp_path, "profile-normalize")
    kickoff = _kick(tmp_path)
    profile = tmp_path / "normalize-profile.json"
    profile.write_text(json.dumps({
        "profile_status": "needs_answers",
        "confidence": 0.8,
        "human_questions": [],
        "recipe": "demo",
    }))
    given = _given(tmp_path, _VALID, "normalize-given.json")
    out = str(tmp_path / "normalize-plan.json")

    rc = _run_py(home, db, kickoff, out, given=given, extra={
        "MINI_ORK_PROFILE_GATE": "1",
        "MINI_ORK_PROFILE_PATH": str(profile),
    })

    normalized = json.loads(profile.read_text())
    assert rc == 0
    assert normalized["profile_status"] == "ready"
    assert normalized["profile_status_normalized"].startswith("needs_answers->ready")


def test_noninteractive_profile_auto_answer_uses_native_dispatch(tmp_path):
    home, db = _home(tmp_path, "profile-auto")
    kickoff = _kick(tmp_path)
    profile = tmp_path / "auto-profile.json"
    profile.write_text(json.dumps({
        "profile_status": "needs_answers",
        "confidence": 0.4,
        "human_questions": ["Which module?"],
    }))
    out = str(tmp_path / "auto-plan.json")
    calls = []

    def dispatch(task_class, node_type, prompt):
        calls.append((task_class, node_type, prompt))
        if node_type == "profile_answerer":
            return 0, json.dumps({
                "answers": [{"question": "Which module?", "answer": "planner"}],
                "auto_answered": True,
            })
        return 0, json.dumps(_VALID)

    rc = _run_py_dispatch(home, db, kickoff, out, dispatch, extra={
        "MINI_ORK_PROFILE_GATE": "1",
        "MINI_ORK_PROFILE_PATH": str(profile),
        "MO_AUTO_ANSWER_PROFILE": "1",
    })

    updated = json.loads(profile.read_text())
    assert rc == 0
    assert [call[1] for call in calls] == ["profile_answerer", "planner"]
    assert updated["profile_status"] == "ready"
    assert updated["confidence"] == 0.9
    assert updated["answers"] == {"Which module?": "planner"}
    assert json.loads((profile.parent / "profile-answers.json").read_text())["auto_answered"] is True


def test_interactive_profile_answers_continue_dispatch(tmp_path, monkeypatch):
    home, db = _home(tmp_path, "profile-interactive")
    kickoff = _kick(tmp_path)
    profile = tmp_path / "interactive-profile.json"
    profile.write_text(json.dumps({
        "profile_status": "needs_answers",
        "confidence": 0.2,
        "human_questions": ["Proceed?"],
    }))
    out = str(tmp_path / "interactive-plan.json")
    monkeypatch.setattr(plan, "_can_prompt_profile", lambda: True)

    def prompt(questions, profile_path):
        assert questions == ["Proceed?"]
        assert plan._apply_profile_answers(
            profile_path, {"answers": {"Proceed?": "yes"}, "auto_answered": False}
        )
        return str(Path(profile_path).parent / "profile-answers.json")

    monkeypatch.setattr(plan, "_prompt_profile_questions", prompt)
    dispatch = _fake_dispatch(json.dumps(_VALID))

    rc = _run_py_dispatch(home, db, kickoff, out, dispatch, extra={
        "MINI_ORK_PROFILE_GATE": "1",
        "MINI_ORK_PROFILE_PATH": str(profile),
        "MINI_ORK_NONINTERACTIVE": "0",
    })

    assert rc == 0
    assert dispatch.calls["n"] == 1
    assert json.loads(profile.read_text())["answers"] == {"Proceed?": "yes"}


def test_context_blocks_order_and_context_pack_persist(tmp_path, monkeypatch):
    home, db = _home(tmp_path, "context")
    kickoff = _kick(tmp_path)
    out = str(tmp_path / "context" / "plan.json")
    captured = {}

    from mini_ork import context_assembler
    from mini_ork.orchestration import active_state_index
    from mini_ork.steering import context_role_packs

    monkeypatch.setattr(context_assembler, "failure_modes_md", lambda *a, **k: "FAILURES")
    monkeypatch.setattr(context_assembler, "prior_runs_md", lambda *a, **k: "PRIOR")
    monkeypatch.setattr(context_assembler, "context_assemble",
                        lambda *a, **k: {"schema": "context-pack", "items": [1]})
    monkeypatch.setattr(context_role_packs, "role_pack_md", lambda *a, **k: "ROLE-PACK")
    monkeypatch.setattr(active_state_index, "render_active_state_block",
                        lambda *a, **k: "ACTIVE-STATE")
    monkeypatch.setattr(plan, "_contextnest_recent_sessions_md", lambda *a, **k: "RECENT")

    def dispatch(_task_class, _node_type, prompt):
        captured["prompt"] = prompt
        return 0, json.dumps(_VALID)

    rc = _run_py_dispatch(home, db, kickoff, out, dispatch, extra={
        "MO_INJECT_LEARNINGS": "1",
        "MO_USE_ROLE_PACKS": "1",
    })

    prompt = captured["prompt"]
    assert rc == 0
    assert prompt.index("FAILURES") < prompt.index("PRIOR") < prompt.index("ROLE-PACK")
    assert prompt.index("ROLE-PACK") < prompt.index("RECENT") < prompt.index("ACTIVE-STATE")
    assert json.loads((Path(out).parent / "context-pack.json").read_text()) == {
        "schema": "context-pack", "items": [1]
    }


def test_trace_lifecycle_records_success_and_blocked(tmp_path):
    home, db = _home(tmp_path, "trace")
    kickoff = _kick(tmp_path)
    given = _given(tmp_path, _VALID, "trace-given.json")
    success_out = str(tmp_path / "trace-success.json")

    assert _run_py(home, db, kickoff, success_out, given=given) == 0
    with sqlite3.connect(db) as con:
        success = con.execute(
            "SELECT status, final_artifact_ref FROM execution_traces "
            "WHERE trace_id LIKE 'tr-plan-%' ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
    assert success == ("success", success_out)

    profile = tmp_path / "trace-profile.json"
    profile.write_text(json.dumps({
        "profile_status": "needs_answers",
        "confidence": 0.1,
        "human_questions": ["Need input"],
    }))
    blocked_out = str(tmp_path / "trace-blocked.json")
    assert _run_py(home, db, kickoff, blocked_out, extra={
        "MINI_ORK_PROFILE_GATE": "1",
        "MINI_ORK_PROFILE_PATH": str(profile),
    }) == 0
    with sqlite3.connect(db) as con:
        blocked = con.execute(
            "SELECT status, reviewer_verdict FROM execution_traces "
            "WHERE trace_id LIKE 'tr-plan-%' ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
    assert blocked == ("blocked", "run_profile_needs_answers")


def test_flag_error_contracts():
    assert plan.main(["--help"], root=str(REPO)) == 0
    assert plan.main(["--bogus"], root=str(REPO)) == 2
    assert plan.main(["/no/such.md"], root=str(REPO)) == 2


def test_default_dispatch_uses_native_module_and_merges_streams(monkeypatch):
    calls = []

    def fake_native(argv, *, root):
        calls.append((argv, root))
        print("diagnostic", file=sys.stderr)
        print(json.dumps(_VALID))
        return 0

    from mini_ork.dispatch import llm_dispatch as native_dispatch
    monkeypatch.setattr(native_dispatch, "llm_dispatch", fake_native)

    rc, combined = plan._default_llm_dispatch(str(REPO))(
        "code_fix", "planner", "make a plan"
    )

    assert rc == 0
    assert combined == "diagnostic\n" + json.dumps(_VALID) + "\n"
    assert calls == [(["--task-class", "code_fix", "--node-type", "planner",
                       "--prompt-text", "make a plan"], str(REPO))]


def test_repair_recovers_parse_error(tmp_path):
    home, db = _home(tmp_path, "repair-ok")
    k = _kick(tmp_path)
    out = str(tmp_path / "repair-ok.json")
    dispatch = _fake_dispatch("not json", json.dumps(_VALID))

    rc = _run_py_dispatch(home, db, k, out, dispatch)

    assert rc == 0
    assert dispatch.calls["n"] == 2
    assert json.loads(Path(out).read_text())["objective"] == _VALID["objective"]


def test_repair_exhausted_hard_fail(tmp_path, capsys):
    home, db = _home(tmp_path, "repair-fail")
    k = _kick(tmp_path)
    out = str(tmp_path / "repair-fail.json")
    dispatch = _fake_dispatch("not json")

    rc = _run_py_dispatch(home, db, k, out, dispatch)

    captured = capsys.readouterr()
    assert rc == 1
    assert dispatch.calls["n"] == 3
    assert "PLAN REJECTED" in captured.err


def test_mo_plan_deterministic_fallback_opt_in(tmp_path):
    home, db = _home(tmp_path, "repair-fallback")
    k = _kick(tmp_path)
    out = str(tmp_path / "repair-fallback.json")
    recipe = tmp_path / "recipes" / "demo"
    recipe.mkdir(parents=True)
    workflow = recipe / "workflow.yaml"
    workflow.write_text(
        "nodes:\n"
        "  - name: implement\n"
        "    type: implementer\n"
        "edges: []\n"
        "outputs:\n"
        "  - plan.json\n"
        "success_verifiers:\n"
        "  - verifiers/test.py\n"
    )
    dispatch = _fake_dispatch("not json")

    rc = _run_py_dispatch(home, db, k, out, dispatch, extra={
        "MO_PLAN_DETERMINISTIC_FALLBACK": "1",
        "MINI_ORK_RECIPE": "demo",
        "MINI_ORK_WORKFLOW": str(workflow),
    })

    assert rc == 0
    assert dispatch.calls["n"] == 1
    assert json.loads(Path(out).read_text())["objective"].startswith("Execute recipe ")



def test_jsx_tags_are_not_placeholders():
    """A plan for a React/JSX codebase names component tags. Those are CODE, not stubs.

    Regression: the old check flagged ANY string shaped like `<...>`, anywhere in the plan.
    A legitimate plan mentioning "<ContentNodeCreationModal>" was therefore rejected as a
    dry-run placeholder -- which made mini-ork unable to plan work on any React/JSX repo.
    """
    jsx = {
        **_VALID,
        "objective": "Remove duplicate ContentNodeCreationModal mounts",
        "decomposition": [
            {"id": "s1", "node_type": "implementer", "depends_on": [],
             "description": "HighlightableText renders <ContentNodeCreationModal /> directly; "
                            "route it through the provider instead."},
            # the planner annotates non-editing steps this way -- also angle-bracketed
            {"id": "s2", "node_type": "verifier", "depends_on": ["s1"],
             "description": "<shell-only>"},
        ],
    }
    assert plan.validate_plan(json.dumps(jsx)) == "ok"


def test_dry_run_stub_still_rejected():
    """The thing the check actually exists for must still be caught."""
    assert plan.validate_plan(plan._DRY_RUN_PLACEHOLDER) == "placeholder_plan"

    # an objective that is an unfilled template value, with nothing to do
    stub = {**_VALID, "objective": "<TODO>", "decomposition": []}
    assert plan.validate_plan(json.dumps(stub)) == "placeholder_plan"

    # an empty shell: no objective, no steps
    empty = {**_VALID, "objective": "", "decomposition": []}
    assert plan.validate_plan(json.dumps(empty)) == "placeholder_plan"
