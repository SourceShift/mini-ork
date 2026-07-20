"""Small Python entrypoint for framework smoke checks."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from mini_ork.client import MiniOrk
from mini_ork.types import ProviderPolicy, RunRequest, SpawnRequest


def main() -> int:
    parser = argparse.ArgumentParser(description="Python facade for mini-ork")
    parser.add_argument("kickoff", nargs="?", help="Kickoff markdown file to run or spawn")
    parser.add_argument("--recipe", help="Force a recipe")
    parser.add_argument("--live", action="store_true", help="Run with MINI_ORK_DRY_RUN=0")
    parser.add_argument("--codex-only", action="store_true", help="Write a Codex-only provider policy before running")
    parser.add_argument("--spawn-parent", help="Parent run id; when set, run mini-ork spawn instead of mini-ork run")
    parser.add_argument("--child-run", help="Stable child run id for --spawn-parent")
    parser.add_argument("--allow-child-spawn", action="store_true", help="Permit the spawned child to spawn descendants")
    parser.add_argument("--no-execute", action="store_true", help="For --spawn-parent, approve only without executing the child")
    args = parser.parse_args()

    if not args.kickoff:
        print(json.dumps({"ok": True, "package": "mini_ork"}))
        return 0

    policy = ProviderPolicy.codex_only() if args.codex_only else None
    client = MiniOrk()
    if args.spawn_parent:
        result = client.spawn(
            SpawnRequest(
                parent_run_id=args.spawn_parent,
                kickoff=Path(args.kickoff),
                recipe=args.recipe,
                child_run_id=args.child_run,
                allow_child_spawn=args.allow_child_spawn,
                execute=not args.no_execute,
                mode="live" if args.live else "dry-run",
            )
        )
        print(json.dumps({
            "ok": result.ok,
            "returncode": result.returncode,
            "parent_run_id": result.parent_run_id,
            "child_run_id": result.child_run_id,
            "spawn_id": result.spawn_id,
            "child_workspace": str(result.child_workspace) if result.child_workspace else "",
            "child_kickoff": str(result.child_kickoff) if result.child_kickoff else "",
            "spawn_status": result.spawn_status,
            "command": list(result.command),
        }))
        return result.returncode

    result = client.run(
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
