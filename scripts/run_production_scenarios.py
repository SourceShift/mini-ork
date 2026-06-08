#!/usr/bin/env python3
"""Run production-style mini-ork scenarios through real markdown kickoffs."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from prod_validation.catalog import select_scenarios
from prod_validation.executor import ScenarioExecutor
from prod_validation.model import RunConfig
from prod_validation.reporting import print_header, print_result, print_summary


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("filter", nargs="?", help="Optional recipe name to run")
    parser.add_argument("--md-only", action="store_true", help="Use `mini-ork run <kickoff.md>`")
    parser.add_argument("--mode", choices=["dry-run", "live"], default=os.environ.get("MO_PROD_SCENARIO_MODE", "dry-run"))
    parser.add_argument(
        "--provider-policy",
        choices=["default", "codex-only"],
        default=os.environ.get("MO_PROD_PROVIDER_POLICY", "default"),
    )
    parser.add_argument("--timeout-seconds", type=int, default=int(os.environ.get("MO_PROD_SCENARIO_TIMEOUT", "1800")))
    parser.add_argument("--keep", action="store_true", help="Retain dry-run temp projects")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = repo_root()
    config = RunConfig(
        mode=args.mode,
        provider_policy=args.provider_policy,
        md_only=args.md_only,
        timeout_seconds=args.timeout_seconds,
        keep=args.keep,
    )

    print_header(root, config, args.filter)
    selected, skipped = select_scenarios(root, args.filter)
    executor = ScenarioExecutor(root, config)

    passed = failed = 0
    for scenario in selected:
        print(f"==> {scenario.recipe}")
        result = executor.run(scenario)
        print_result(result, config)
        if result.ok:
            passed += 1
        else:
            failed += 1
        print()

    print_summary(passed, skipped, failed)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
