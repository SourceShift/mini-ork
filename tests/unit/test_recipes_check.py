"""`mini-ork recipes check` — recipe integrity, not recipe style.

recipe-eval scores a recipe and always exits 0, which is why a renamed prompt
or a deleted verifier can sit in the tree unnoticed until a paid run fails on
it. These tests pin the gate's contract: a dangling reference and an
uncompilable workflow fail, an incomplete recipe is visible but does not by
itself fail, and the real repo is clean.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from mini_ork.cli import recipes

WORKFLOW = """\
version: "0.1.0"
task_class: demo
nodes:
  - name: implementer
    type: implementer
    prompt_ref: prompts/implementer.md
    dispatch_mode: serial
  - name: publisher
    type: publisher
    prompt_ref: null
    dispatch_mode: serial
  - name: rollback
    type: rollback
    prompt_ref: null
    dispatch_mode: serial
edges:
  - { from: implementer, to: publisher, edge_type: depends_on }
  - { from: implementer, to: rollback, edge_type: escalates_to }
"""


def _recipe(tmp_path: Path, *, workflow: str = WORKFLOW,
            prompt: bool = True, extra: bool = True) -> Path:
    rd = tmp_path / "recipes" / "demo"
    (rd / "prompts").mkdir(parents=True)
    (rd / "workflow.yaml").write_text(workflow, encoding="utf-8")
    if prompt:
        (rd / "prompts" / "implementer.md").write_text("do it\n", encoding="utf-8")
    if extra:
        (rd / "task_class.yaml").write_text("name: demo\n", encoding="utf-8")
        (rd / "artifact_contract.yaml").write_text(
            "task_class: demo\nexpected_artifact: patch\n", encoding="utf-8")
    return tmp_path


def _run(tmp_path: Path, monkeypatch, *args: str) -> int:
    monkeypatch.setenv("MINI_ORK_ROOT", str(tmp_path))
    return recipes.main(["check", *args])


def test_clean_recipe_has_no_findings(tmp_path: Path) -> None:
    root = _recipe(tmp_path)
    assert recipes.check_recipe(root, "demo") == []


def test_dangling_prompt_ref_is_an_error_and_fails_the_run(tmp_path: Path, monkeypatch, capsys) -> None:
    root = _recipe(tmp_path, prompt=False)
    findings = recipes.check_recipe(root, "demo")
    assert [f["kind"] for f in findings] == ["dangling-ref"]
    assert "prompts/implementer.md" in findings[0]["detail"]
    assert _run(root, monkeypatch) == 1
    assert "dangling-ref" in capsys.readouterr().out


def test_unparsable_workflow_is_an_error(tmp_path: Path, monkeypatch) -> None:
    root = _recipe(tmp_path, workflow="nodes: [\n")
    kinds = [f["kind"] for f in recipes.check_recipe(root, "demo")]
    assert kinds == ["unparsable"]
    assert _run(root, monkeypatch) == 1


def test_workflow_that_is_not_a_mapping_is_an_error(tmp_path: Path, monkeypatch) -> None:
    """A YAML list parses fine and then has no `.get` — a finding, not a crash."""
    root = _recipe(tmp_path, workflow="- just\n- a list\n")
    findings = recipes.check_recipe(root, "demo")
    assert [f["kind"] for f in findings] == ["unparsable"]
    assert "expected a mapping" in findings[0]["detail"]
    assert _run(root, monkeypatch) == 1


def test_dangling_artifact_contract_ref_is_an_error(tmp_path: Path, monkeypatch) -> None:
    """task_class.yaml names files too; a rename dangles it the same way."""
    root = _recipe(tmp_path)
    (root / "recipes" / "demo" / "task_class.yaml").write_text(
        "name: demo\nartifact_contract_ref: contracts/main.yaml\n", encoding="utf-8")
    findings = recipes.check_recipe(root, "demo")
    assert [f["kind"] for f in findings] == ["dangling-ref"]
    assert "artifact_contract_ref" in findings[0]["detail"]
    assert _run(root, monkeypatch) == 1


def test_missing_compiler_dependency_is_a_usage_error_not_52_findings(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """An unimportable compiler must not be reported as "every recipe is broken"."""
    root = _recipe(tmp_path)
    monkeypatch.setenv("MINI_ORK_ROOT", str(root))
    monkeypatch.setitem(sys.modules, "mini_ork.workflow.compiler", None)
    assert recipes.main(["check"]) == 2
    assert "pyyaml" in capsys.readouterr().err


def test_no_recipes_directory_is_a_usage_error(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("MINI_ORK_ROOT", str(tmp_path))
    assert recipes.main(["check"]) == 2


def test_uncompilable_workflow_is_an_error(tmp_path: Path, monkeypatch) -> None:
    broken = WORKFLOW.replace(
        "{ from: implementer, to: publisher, edge_type: depends_on }",
        "{ from: implementer, to: nobody, edge_type: depends_on }",
    )
    root = _recipe(tmp_path, workflow=broken)
    kinds = [f["kind"] for f in recipes.check_recipe(root, "demo")]
    assert kinds == ["uncompilable"]
    assert _run(root, monkeypatch) == 1


def test_incomplete_recipe_warns_but_passes_unless_strict(tmp_path: Path, monkeypatch) -> None:
    root = _recipe(tmp_path, extra=False)
    findings = recipes.check_recipe(root, "demo")
    assert [f["severity"] for f in findings] == ["warning", "warning"]
    assert _run(root, monkeypatch) == 0
    assert _run(root, monkeypatch, "--strict") == 1


def test_named_recipe_that_does_not_exist_is_an_error(tmp_path: Path, monkeypatch) -> None:
    root = _recipe(tmp_path)
    assert [f["kind"] for f in recipes.check_recipe(root, "nope")] == ["missing-recipe"]
    assert _run(root, monkeypatch, "nope") == 1


def test_json_reports_verdict_and_findings(tmp_path: Path, monkeypatch, capsys) -> None:
    root = _recipe(tmp_path, prompt=False)
    assert _run(root, monkeypatch, "--json") == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["verdict"] == "FAIL"
    assert payload["errors"] == 1
    assert payload["findings"][0]["recipe"] == "demo"


def test_real_repo_passes() -> None:
    """The gate ships green: every shipped recipe's references resolve."""
    assert recipes.main(["check"]) == 0
