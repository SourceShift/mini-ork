"""Console reporting for production scenario validation."""

from __future__ import annotations

from pathlib import Path

from .model import RunConfig, ScenarioResult


def print_header(root: Path, config: RunConfig, recipe_filter: str | None) -> None:
    print("mini-ork production scenarios")
    print(f"root: {root}")
    print(f"mode: {config.mode}")
    print(f"provider_policy: {config.provider_policy}")
    if config.md_only:
        print("entrypoint: mini-ork run <kickoff.md>")
    if recipe_filter:
        print(f"filter: {recipe_filter}")
    print()


def print_result(result: ScenarioResult, config: RunConfig) -> None:
    if result.ok:
        print("  [OK] rc=0 and verification evidence emitted")
        print(f"  task_class: {result.actual_task_class}")
        if result.plan_path:
            print(f"  run_dir: {result.plan_path.parent}")
    else:
        if result.error and result.returncode is None:
            print(f"  [FAIL] {result.error}")
        else:
            print(
                f"  [FAIL] rc={result.returncode}, "
                f"task_class={result.actual_task_class or 'missing'} "
                f"expected={result.expected_task_class}, "
                "or missing verification evidence"
            )
            for line in result.output.splitlines()[:80]:
                print(f"    {line}")

    if result.tmp_project and (config.mode != "dry-run" or config.keep):
        print(f"  retained: {result.tmp_project}")


def print_summary(passed: int, skipped: int, failed: int) -> None:
    print(f"Results: {passed} OK  {skipped} SKIP  {failed} FAIL")

