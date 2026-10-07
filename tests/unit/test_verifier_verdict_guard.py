"""Regression: the verifier hollow-run guard must not require the verdict the
verifiers are about to write.

framework-edit declares `${MINI_ORK_RUN_DIR}/verdict.json` as a required
artifact; its verifiers write it. The guard ran BEFORE the scripts, so every
framework-edit build (6/6 on 2026-10-05) failed both verifier nodes on its own
not-yet-written verdict, skipped the reviewer and rolled back.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

from mini_ork.cli import execute as ex
from mini_ork.cli import execute_handlers as eh


def _plan(tmp_path, run_dir):
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"artifact_contract": {
        "required_artifacts": [f"{run_dir}/framework-edit.diff", f"{run_dir}/verdict.json"],
        "outputs": [f"{run_dir}/framework-edit.diff", f"{run_dir}/verdict.json"],
    }}))
    return str(plan)


def _workflow(tmp_path):
    wf = tmp_path / "workflow.yaml"
    wf.write_text("nodes:\n  - {name: implementer, type: implementer}\n"
                  "  - {name: test_verifier, type: verifier}\n")
    return str(wf)


def test_guard_can_exempt_verifier_outputs(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    (run / "framework-edit.diff").write_text("diff --git a/x b/x\n")
    plan = _plan(tmp_path, run)
    assert ex._required_artifacts_ok(plan) is False                       # verdict missing
    assert ex._required_artifacts_ok(plan, skip_verifier_outputs=True) is True
    (run / "framework-edit.diff").write_text("")                         # hollow implementer
    assert ex._required_artifacts_ok(plan, skip_verifier_outputs=True) is False


def _ctx(tmp_path, run, script_body):
    recipe = tmp_path / "recipe"
    (recipe / "verifiers").mkdir(parents=True)
    (recipe / "verifiers" / "test.py").write_text(script_body)
    return SimpleNamespace(
        workflow=_workflow(tmp_path), node_id="test_verifier", plan_path=_plan(tmp_path, run),
        verifier_ref="verifiers/test.py", recipe_dir=str(recipe), run_dir=str(run),
        run_dir_eff=str(run), task_class="framework_edit", root=str(tmp_path),
        publish_declared_outputs=lambda: True, db="", run_id="",
    )


def test_verifier_that_writes_its_verdict_passes(tmp_path, monkeypatch):
    run = tmp_path / "run"
    run.mkdir()
    (run / "framework-edit.diff").write_text("diff --git a/x b/x\n")
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(run))
    body = ("import json, os\n"
            "open(os.path.join(os.environ['MINI_ORK_RUN_DIR'], 'verdict.json'), 'w')"
            ".write(json.dumps({'pass': True}))\n"
            "print(json.dumps({'pass': True}))\n")
    assert eh._handle_verifier(_ctx(tmp_path, run, body)) == (0, "done")


def test_verifier_that_never_writes_its_verdict_still_fails(tmp_path, monkeypatch, capsys):
    run = tmp_path / "run"
    run.mkdir()
    (run / "framework-edit.diff").write_text("diff --git a/x b/x\n")
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(run))
    body = "import json\nprint(json.dumps({'pass': True}))\n"
    assert eh._handle_verifier(_ctx(tmp_path, run, body)) == (1, "error")
    assert "did not produce" in capsys.readouterr().err


def test_hollow_implementer_still_fails_before_the_verifier_runs(tmp_path, monkeypatch):
    run = tmp_path / "run"
    run.mkdir()                                                          # no diff at all
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(run))
    marker = tmp_path / "ran"
    body = f"open({str(marker)!r}, 'w').write('x')\nprint('{{\"pass\": true}}')\n"
    assert eh._handle_verifier(_ctx(tmp_path, run, body)) == (1, "error")
    assert not marker.exists()
