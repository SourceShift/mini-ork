"""Small Python entrypoint for framework smoke checks."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .client import MiniOrk
from .types import ProviderPolicy, RunRequest


def main() -> int:
    parser = argparse.ArgumentParser(description="Python facade for mini-ork")
    parser.add_argument("kickoff", nargs="?", help="Kickoff markdown file to run")
    parser.add_argument("--recipe", help="Force a recipe")
    parser.add_argument("--live", action="store_true", help="Run with MINI_ORK_DRY_RUN=0")
    parser.add_argument("--codex-only", action="store_true", help="Write a Codex-only provider policy before running")
    args = parser.parse_args()

    if not args.kickoff:
        print(json.dumps({"ok": True, "package": "mini_ork"}))
        return 0

    policy = ProviderPolicy.codex_only() if args.codex_only else None
    result = MiniOrk().run(
        RunRequest(
            kickoff=Path(args.kickoff),
            recipe=args.recipe,
            mode="live" if args.live else "dry-run",
            provider_policy=policy,
        )
    )
    print(json.dumps({
        "ok": result.ok,
        "returncode": result.returncode,
        "task_class": result.task_class,
        "run_id": result.run_id,
        "plan_path": str(result.plan_path) if result.plan_path else "",
        "verdict": result.verdict,
        "command": list(result.command),
        "init_ran": result.init_ran,
        "retained_home": str(result.retained_home) if result.retained_home else "",
    }))
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
