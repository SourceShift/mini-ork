"""Parity gate: mini_ork.ported.mini_ork_plan vs bin/mini-ork-plan.

The planner LLM dispatch is the one non-deterministic seam; MO_GIVEN_PLAN skips it
and flows a supplied plan through the SAME extraction → validation → fallback →
overlay → write → DB pipeline. We drive both the live bash and the port through
MO_GIVEN_PLAN (valid / markdown-fenced+z-insight-bleed / rejected shapes) plus
--dry-run, the profile gate, and flag errors — comparing plan.json + stdout +
exit code + the task_runs row.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.ported import mini_ork_plan as plan  # noqa: E402

BIN = REPO / "bin" / "mini-ork-plan"

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


def _run_bash(home, db, kickoff, out, given=None, extra=None):
    return subprocess.run(["bash", str(BIN), kickoff, "--out", out],
                          capture_output=True, text=True, env=_env(home, db, given, extra))


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


def _parity(tmp_path, name, given_text, *, extra=None):
    hb, db_b = _home(tmp_path, name + "b"); hp, db_p = _home(tmp_path, name + "p")
    k = _kick(tmp_path)
    g = _given(tmp_path, given_text, name + ".json")
    ob = str(tmp_path / (name + "b.json")); op = str(tmp_path / (name + "p.json"))
    cb = _run_bash(hb, db_b, k, ob, given=g, extra=extra)
    rc_p = _run_py(hp, db_p, k, op, given=g, extra=extra)
    assert cb.returncode == rc_p, f"{name}: rc bash={cb.returncode} py={rc_p}\n{cb.stderr}"
    fb = Path(ob).read_text() if Path(ob).exists() else ""
    fp = Path(op).read_text() if Path(op).exists() else ""
    assert fb == fp, f"{name}: plan.json differs\nBASH:{fb}\nPY:{fp}"
    assert _taskrow(db_b) == _taskrow(db_p), f"{name}: task_runs differ"
    return cb.returncode, fp


def test_valid_given_plan(tmp_path):
    rc, fp = _parity(tmp_path, "valid", _VALID)
    assert rc == 0 and json.loads(fp)["objective"] == "Ship widget"
    assert json.loads(fp)["task_class"] == "code_fix"    # overlay stamps it


def test_fenced_and_zinsight_bleed(tmp_path):
    # markdown fence + a trailing z-insight object; extraction must pick the plan
    raw = "```json\n" + json.dumps(_VALID) + "\n```\n<z-insight>{\"domain\":\"x\"}</z-insight>\n"
    rc, fp = _parity(tmp_path, "fenced", raw)
    assert rc == 0 and json.loads(fp)["objective"] == "Ship widget"


def test_missing_verifier_no_recipe_rejected(tmp_path):
    bad = {**_VALID, "verifier_contract": {"checks": []}}
    rc, _ = _parity(tmp_path, "noverif", bad)
    assert rc == 1


def test_bad_node_type_rejected(tmp_path):
    bad = {**_VALID, "decomposition": [{"id": "s1", "node_type": "wizard", "depends_on": []}]}
    rc, _ = _parity(tmp_path, "badnt", bad)
    assert rc == 1


def test_placeholder_rejected(tmp_path):
    bad = {**_VALID, "objective": "<fill in>"}
    rc, _ = _parity(tmp_path, "ph", bad)
    assert rc == 1


def test_parse_error_no_recipe_rejected(tmp_path):
    rc, _ = _parity(tmp_path, "parse", "this is not json at all")
    assert rc == 1


def test_dry_run_parity(tmp_path):
    hb, db_b = _home(tmp_path, "drb"); hp, db_p = _home(tmp_path, "drp")
    k = _kick(tmp_path)
    ob = str(tmp_path / "drb.json"); op = str(tmp_path / "drp.json")
    cb = subprocess.run(["bash", str(BIN), k, "--out", ob, "--dry-run"],
                        capture_output=True, text=True, env=_env(hb, db_b))
    old = dict(os.environ); os.environ.clear(); os.environ.update(_env(hp, db_p))
    try:
        rc = plan.main([k, "--out", op, "--dry-run"], root=str(REPO))
    finally:
        os.environ.clear(); os.environ.update(old)
    assert cb.returncode == rc == 0
    assert Path(ob).read_text() == Path(op).read_text()
    assert "dry-run placeholder" in Path(op).read_text()


def test_profile_gate_parity(tmp_path):
    hb, db_b = _home(tmp_path, "pgb"); hp, db_p = _home(tmp_path, "pgp")
    k = _kick(tmp_path)
    # a needs_answers profile → gate blocks
    prof = tmp_path / "prof.json"
    prof.write_text(json.dumps({"profile_status": "needs_answers", "confidence": 0.4,
                                "human_questions": ["what?"]}))
    ob = str(tmp_path / "pgb.json"); op = str(tmp_path / "pgp.json")
    extra = {"MINI_ORK_PROFILE_GATE": "1", "MINI_ORK_PROFILE_PATH": str(prof)}
    cb = subprocess.run(["bash", str(BIN), k, "--out", ob],
                        capture_output=True, text=True, env=_env(hb, db_b, extra=extra))
    old = dict(os.environ); os.environ.clear(); os.environ.update(_env(hp, db_p, extra=extra))
    try:
        rc = plan.main([k, "--out", op], root=str(REPO))
    finally:
        os.environ.clear(); os.environ.update(old)
    assert cb.returncode == rc == 0
    assert json.loads(Path(ob).read_text()) == json.loads(Path(op).read_text())
    assert json.loads(Path(op).read_text())["plan_status"] == "needs_answers"


def test_flag_errors_parity(tmp_path):
    hb, db_b = _home(tmp_path, "fe")
    e = _env(hb, db_b)
    assert subprocess.run(["bash", str(BIN), "--help"], capture_output=True, env=e).returncode == \
        plan.main(["--help"], root=str(REPO)) == 0
    assert subprocess.run(["bash", str(BIN), "--bogus"], capture_output=True, env=e).returncode == \
        plan.main(["--bogus"], root=str(REPO)) == 2
    assert subprocess.run(["bash", str(BIN), "/no/such.md"], capture_output=True, env=e).returncode == \
        plan.main(["/no/such.md"], root=str(REPO)) == 2


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
        "  - verifiers/test.sh\n"
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
