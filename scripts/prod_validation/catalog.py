"""Scenario catalog and seed-file definitions."""

from __future__ import annotations

from pathlib import Path

from .model import Scenario


def scenarios(root: Path) -> list[Scenario]:
    base = root / "docs" / "production-validation" / "kickoffs"
    return [
        Scenario("code-fix", base / "code-fix-real-bug.md"),
        Scenario(
            "docs",
            base / "docs-real-edit.md",
            (
                "examples",
                "docs/production-validation/mini-ork-production-scenarios.md",
            ),
        ),
        Scenario("bdd-first-delivery", base / "bdd-settings-page.md"),
        Scenario("refactor-audit", base / "refactor-audit-provider-roster.md"),
        Scenario("research-synthesis", base / "research-synthesis-heterogeneous-review.md"),
        Scenario("blog-post", base / "blog-post-launch.md"),
        Scenario("db-migration", base / "db-migration-user-profile.md"),
        Scenario("ops-runbook", base / "ops-runbook-symlink-hang.md"),
        Scenario("ui-audit", base / "ui-audit-readme-cli.md"),
    ]


def expected_task_class(root: Path, recipe: str) -> str:
    task_class_yaml = root / "recipes" / recipe / "task_class.yaml"
    if task_class_yaml.exists():
        # Avoid adding a hard runtime dependency on PyYAML for this helper.
        for line in task_class_yaml.read_text(encoding="utf-8").splitlines():
            if line.startswith("name:"):
                return line.split(":", 1)[1].strip().strip('"').strip("'").replace("-", "_")
    return recipe.replace("-", "_")


def select_scenarios(root: Path, recipe_filter: str | None) -> tuple[list[Scenario], int]:
    selected = []
    skipped = 0
    for scenario in scenarios(root):
        if recipe_filter and scenario.recipe != recipe_filter:
            skipped += 1
            continue
        selected.append(scenario)
    return selected, skipped

