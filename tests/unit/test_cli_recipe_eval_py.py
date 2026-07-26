"""Unit + native-integration tests for ``mini_ork.cli.recipe_eval``.

The unit tests drive the ported scoring logic against tmp recipe fixtures
and assert the exact stdout/exit-code contract (human table and ``--json``)
of the retired ``bin/mini-ork-recipe-eval``. The integration tests prove
``mini-ork recipe-eval`` is dispatched natively — explicitly registered
against ``mini_ork.cli.recipe_eval`` (the dash is not importable in a module
name, so it cannot live in ``_NATIVE_SUBS``) and absent from ``_EXEC_SUBS``
— and that ``python3 -m mini_ork.cli.recipe_eval --help`` exits 0 without
any ``bin/mini-ork-*`` entrypoint.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from mini_ork.cli import main as cli
from mini_ork.cli import recipe_eval

REPO = Path(__file__).resolve().parents[2]


# ── fixtures / helpers ────────────────────────────────────────────────────────

def _full_recipe(root: Path, name: str = "demo") -> Path:
    """A recipe with no static findings (score 100)."""
    d = root / "recipes" / name
    (d / "prompts").mkdir(parents=True)
    (d / "examples").mkdir()
    (d / "task_class.yaml").write_text(
        "name: demo\ndescription: demo recipe\n", encoding="utf-8")
    (d / "workflow.yaml").write_text("nodes:\n  - id: plan\n", encoding="utf-8")
    (d / "artifact_contract.yaml").write_text(
        "success_verifiers:\n  - pytest\n", encoding="utf-8")
    (d / "examples" / "kickoff.md").write_text("# demo\n", encoding="utf-8")
    return d


@pytest.fixture
def env(tmp_path, monkeypatch):
    root = tmp_path / "root"
    (root / "recipes").mkdir(parents=True)
    monkeypatch.setenv("MINI_ORK_ROOT", str(root))
    return root


# ── usage / arg parsing ───────────────────────────────────────────────────────

def test_help_to_stdout(capsys):
    assert recipe_eval.main(["--help"]) == 0
    assert capsys.readouterr().out == recipe_eval.HELP_TEXT
    assert recipe_eval.main(["-h"]) == 0
    assert capsys.readouterr().out == recipe_eval.HELP_TEXT


def test_usage_errors(capsys):
    assert recipe_eval.main(["--nope"]) == 2
    assert capsys.readouterr().err == f"Unknown flag: --nope\n{recipe_eval.HELP_TEXT}"
    assert recipe_eval.main(["one", "two"]) == 2
    assert capsys.readouterr().err == f"Unexpected argument: two\n{recipe_eval.HELP_TEXT}"


# ── scoring logic ─────────────────────────────────────────────────────────────

def test_full_recipe_scores_100_with_no_findings(tmp_path):
    _full_recipe(tmp_path)
    result = recipe_eval.eval_recipe(tmp_path, "demo")
    assert result == {"recipe": "demo", "score": 100, "findings": []}


def test_missing_everything_scores_40(tmp_path):
    # 100 - 3*15 (manifests) - 10 (no success_verifiers) - 5 (no example) = 40.
    # Empty dicts from missing YAML files skip the name/desc/nodes checks
    # (bash heredoc: `if tc and ...` — falsy {} short-circuits).
    result = recipe_eval.eval_recipe(tmp_path, "ghost")
    assert result["score"] == 40
    sevs = [(f["sev"], f["msg"]) for f in result["findings"]]
    assert sevs == [
        ("error", "missing task_class.yaml"),
        ("error", "missing workflow.yaml"),
        ("error", "missing artifact_contract.yaml"),
        ("warn", "no success_verifiers"),
        ("warn", "no example kickoff"),
    ]
    fixes = [f["fix"] for f in result["findings"]]
    assert "create recipes/ghost/task_class.yaml" in fixes
    assert "add success_verifiers: to recipes/ghost/artifact_contract.yaml" in fixes
    assert "add recipes/ghost/examples/<name>/kickoff.md" in fixes


def test_missing_name_and_description_and_nodes(tmp_path):
    d = tmp_path / "recipes" / "demo"
    d.mkdir(parents=True)
    (d / "task_class.yaml").write_text("other: x\n", encoding="utf-8")
    (d / "workflow.yaml").write_text("entry: plan\n", encoding="utf-8")
    (d / "artifact_contract.yaml").write_text(
        "success_verifiers:\n  - pytest\n", encoding="utf-8")
    (d / "examples").mkdir()
    (d / "examples" / "k.md").write_text("# k\n", encoding="utf-8")
    result = recipe_eval.eval_recipe(tmp_path, "demo")
    # 100 - 10 (name) - 5 (description) - 10 (nodes) = 75
    assert result["score"] == 75
    msgs = [f["msg"] for f in result["findings"]]
    assert msgs == ["task_class.yaml missing name",
                    "task_class.yaml missing description",
                    "workflow.yaml missing nodes"]


def test_unparseable_yaml_counts_as_present_but_empty(tmp_path):
    d = tmp_path / "recipes" / "demo"
    d.mkdir(parents=True)
    for fn in ("task_class.yaml", "workflow.yaml", "artifact_contract.yaml"):
        (d / fn).write_text("not: [valid\n", encoding="utf-8")
    result = recipe_eval.eval_recipe(tmp_path, "demo")
    # load_yaml → None → {} → no missing-manifest errors, but verifiers and
    # examples deductions still fire: 100 - 10 (verifiers) - 5 (examples) = 85.
    assert result["score"] == 85
    assert [f["msg"] for f in result["findings"]] == [
        "no success_verifiers", "no example kickoff"]


def test_oversize_prompt_and_workflow_deductions(tmp_path):
    d = _full_recipe(tmp_path)
    (d / "prompts" / "big.md").write_bytes(b"x" * (33 * 1024 + 1))
    # Valid YAML (a bare 17 KiB scalar would crash the scorer exactly as it
    # crashes the bash heredoc — wf would be a str, not a dict).
    (d / "workflow.yaml").write_text(
        "nodes:\n  - id: plan\n# " + "y" * (17 * 1024), encoding="utf-8")
    result = recipe_eval.eval_recipe(tmp_path, "demo")
    # 100 - 5 (prompt) - 5 (workflow) = 90
    assert result["score"] == 90
    msgs = [f["msg"] for f in result["findings"]]
    assert "prompt big.md is 33 KiB (cap 32)" in msgs
    assert "workflow.yaml is 17 KiB (cap 16)" in msgs


def test_example_directory_also_satisfies_example_check(tmp_path):
    d = _full_recipe(tmp_path)
    (d / "examples" / "kickoff.md").unlink()
    (d / "examples" / "sample").mkdir()
    assert recipe_eval.eval_recipe(tmp_path, "demo")["score"] == 100


def test_list_recipes_sorted_dirs_only(env):
    _full_recipe(env, "b-recipe")
    _full_recipe(env, "a-recipe")
    (env / "recipes" / "not-a-dir.md").write_text("x\n", encoding="utf-8")
    assert recipe_eval._list_recipes(env) == ["a-recipe", "b-recipe"]


# ── CLI output modes ──────────────────────────────────────────────────────────

def test_human_output_format(env, capsys):
    _full_recipe(env, "demo")
    (env / "recipes" / "ghost").mkdir()
    assert recipe_eval.main([]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out.startswith("# RecipeEval (static)\n\n")
    assert "## demo — 100/100 (A)\n- No static findings.\n\n" in captured.out
    assert "## ghost — 40/100 (F)\n" in captured.out
    assert "- [error] missing task_class.yaml\n  Fix: create recipes/ghost/task_class.yaml\n" \
        in captured.out
    assert captured.out.endswith("\n\n")


def test_human_output_single_recipe_arg(env, capsys):
    _full_recipe(env, "demo")
    (env / "recipes" / "other").mkdir()
    assert recipe_eval.main(["demo"]) == 0
    out = capsys.readouterr().out
    assert "## demo — 100/100 (A)\n" in out
    assert "other" not in out


def test_json_output(env, capsys):
    _full_recipe(env, "demo")
    assert recipe_eval.main(["--json", "demo"]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out) == [
        {"recipe": "demo", "score": 100, "findings": []}]
    assert captured.out == json.dumps(
        [{"recipe": "demo", "score": 100, "findings": []}], indent=2) + "\n"


def test_empty_recipes_dir_still_exits_zero(env, capsys):
    assert recipe_eval.main([]) == 0
    assert capsys.readouterr().out == "# RecipeEval (static)\n\n"


# ── native integration ────────────────────────────────────────────────────────

def test_recipe_eval_is_registered_natively():
    # The dash is not importable in a module name, so recipe-eval lives in the
    # explicit registry (mapped to mini_ork.cli.recipe_eval), not _NATIVE_SUBS.
    assert not hasattr(cli, "_EXEC_SUBS"), "recipe-eval must dispatch natively — the bash trampoline set is gone"
    assert "recipe-eval" in cli.SUBCOMMAND_REGISTRY


def test_recipe_eval_dispatches_via_python_m(monkeypatch):
    calls = []

    class _Done:
        returncode = 0

    monkeypatch.setattr(cli.subprocess, "run", lambda argv, **kw: (calls.append(argv), _Done())[1])
    assert cli.main(["recipe-eval", "--json"], root=str(REPO)) == 0
    assert calls == [[sys.executable, "-m", "mini_ork.cli.recipe_eval", "--json"]]


def test_module_help_exits_zero_without_bin_entrypoint(tmp_path):
    """``python3 -m mini_ork.cli.recipe_eval --help`` runs the module
    directly — no bin/mini-ork-recipe-eval involved (cwd is an empty
    tmp dir)."""
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("MINI_ORK_", "MO_"))}
    env["PYTHONPATH"] = str(REPO) + (os.pathsep + env["PYTHONPATH"]
                                     if env.get("PYTHONPATH") else "")
    run = subprocess.run(
        [sys.executable, "-m", "mini_ork.cli.recipe_eval", "--help"],
        capture_output=True, text=True, cwd=tmp_path, env=env, check=False)
    assert run.returncode == 0
    assert run.stdout == recipe_eval.HELP_TEXT
    assert run.stderr == ""
