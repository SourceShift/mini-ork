"""The procedure recipe: the lightweight path for a fixed, step-by-step task.

Checks the recipe compiles like any other and that its deterministic report
check grades exactly the step lines the node wrote.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
RECIPE = REPO / "recipes" / "procedure"


def _grade():
    spec = importlib.util.spec_from_file_location("report_check", RECIPE / "verifiers" / "report-check.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.grade


def test_the_recipe_is_one_node_doing_the_work_plus_a_deterministic_check():
    from mini_ork.workflow.compiler import compile_workflow

    compiled = compile_workflow(str(RECIPE / "workflow.yaml"))
    types = {name: node.type for name, node in compiled.nodes.items()}
    assert types == {"procedure": "implementer", "report_check": "verifier",
                     "publisher": "publisher", "rollback": "rollback"}


@pytest.mark.parametrize("report, ok, reason", [
    ("## Steps\n- [x] 1. a — done\n- [x] 2. b — done\n", True, ""),
    ("## Steps\n- [x] 1. a — done\n- [ ] 2. b — blocked\n", False, "unfinished steps: [2]"),
    ("## Notes\nnothing\n", False, "no '## Steps' section"),
    ("## Steps\nI did everything.\n", False, "no step lines"),
])
def test_the_report_check_grades_each_step_line(tmp_path, report, ok, reason):
    path = tmp_path / "procedure-report.md"
    path.write_text(report)
    result = _grade()(str(path))
    assert (result["ok"], result["reason"]) == (ok, reason)


def test_a_missing_report_fails(tmp_path):
    assert _grade()(str(tmp_path / "procedure-report.md"))["reason"] == "procedure-report.md missing"
