#!/usr/bin/env python3
"""Integration smoke tests for the mini_ork Python framework facade."""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from mini_ork import MiniOrk, NodeSpec, ProviderPolicy, RecipeBuilder, RunRequest


ROOT = Path(__file__).resolve().parents[2]


def assert_true(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def test_recipe_builder() -> None:
    recipe = (
        RecipeBuilder("docs-lite", "docs_lite", "Tiny docs flow")
        .keywords("docs", "markdown")
        .node(NodeSpec(name="planner", type="planner", model_lane="planner"))
        .node(NodeSpec(name="verifier", type="verifier", verifier_ref="verifiers/check.sh"))
        .edge("planner", "verifier", "verifies")
        .build()
    )
    assert_true(recipe.name == "docs-lite", "recipe name is preserved")
    assert_true(recipe.workflow.nodes[1].verifier_ref == "verifiers/check.sh", "verifier_ref is preserved")
    assert_true(recipe.workflow.to_dict()["edges"][0]["edge_type"] == "verifies", "edge materializes")


def test_codex_policy_writer(tmp_path: Path) -> None:
    client = MiniOrk(root=ROOT, home=tmp_path / ".mini-ork")
    path = client.write_provider_policy(ProviderPolicy.codex_only(), tmp_path / ".mini-ork")
    text = path.read_text(encoding="utf-8")
    assert_true("planner: codex" in text, "planner lane written")
    assert_true("opus_lens: codex" in text, "opus lens can be remapped by policy")


def test_dry_run_client(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    kickoff = tmp_path / "kickoff.md"
    kickoff.write_text(
        "# Docs update\n\n"
        "## Goal\nUpdate docs.\n\n"
        "## Scope allow\n- README.md\n\n"
        "## Success criteria\n- README mentions python framework.\n",
        encoding="utf-8",
    )

    result = MiniOrk(root=ROOT, home=tmp_path / ".mini-ork").run(
        RunRequest(kickoff=kickoff, recipe="docs", mode="dry-run")
    )
    assert_true(result.ok, f"dry-run failed:\n{result.output}")
    assert_true(result.task_class == "docs", "task_class parsed")
    assert_true(result.plan_path is not None, "plan path parsed")
    assert_true(result.command[:2] == (str(ROOT / "bin" / "mini-ork"), "run"), "command preserved")
    assert_true(result.init_ran, "client auto-initialized the project")
    assert_true("=== mini-ork init ===" in result.init_output, "init output preserved")


def main() -> int:
    test_recipe_builder()
    with tempfile.TemporaryDirectory(prefix="mini-ork-py-fw-") as d:
        test_codex_policy_writer(Path(d))
    with tempfile.TemporaryDirectory(prefix="mini-ork-py-fw-") as d:
        test_dry_run_client(Path(d))
    print("python framework: 3 OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
