#!/usr/bin/env python3
"""Live recursive mini-ork validation without Anthropic-family provider calls.

This runs the real CLI/Python facade against an isolated temp project with
MINI_ORK_DRY_RUN=1. It validates the recursive control plane end to end:
root run -> child spawn -> grandchild spawn -> lineage/event queries.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import tempfile
from pathlib import Path

from mini_ork import MiniOrk, RunRequest, SpawnRequest


ROOT = Path(__file__).resolve().parents[2]


def write(path: Path, title: str) -> None:
    path.write_text(
        f"""# {title}
## Problem
Validate recursive mini-ork delegation in a temp project.
## Definition of Done
- Plan artifacts exist.
## Scope
- ONLY temp validation artifacts may be created.
""",
        encoding="utf-8",
    )


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="mini-ork-live-recursive-") as tmp:
        project = Path(tmp)
        subprocess.run(["git", "init", "-q"], cwd=project, check=True)
        home = project / ".mini-ork"
        db = home / "state.db"

        root_md = project / "root.md"
        child_md = project / "child.md"
        grandchild_md = project / "grandchild.md"
        write(root_md, "Live recursive root")
        write(child_md, "Live recursive child")
        write(grandchild_md, "Live recursive grandchild")

        env = {
            "MINI_ORK_RECURSIVE_MAX_DEPTH": "2",
            "MINI_ORK_RECURSIVE_MAX_CHILDREN": "4",
            "MINI_ORK_RECURSIVE_MAX_DESCENDANTS": "16",
        }
        client = MiniOrk(root=ROOT, home=home, db=db)
        root = client.run(
            RunRequest(
                kickoff=root_md,
                recipe="code-fix",
                mode="dry-run",
                cwd=project,
                extra_env={**env, "MINI_ORK_RUN_ID": "live-root-recursive"},
            )
        )
        if not root.ok:
            print(root.output)
            return 1
        con = sqlite3.connect(db)
        con.execute(
            """
            INSERT OR IGNORE INTO task_runs(id, task_class, recipe, kickoff_path, status, created_at, updated_at)
            VALUES (?, 'code_fix', 'code-fix', ?, 'classified', strftime('%s','now'), strftime('%s','now'))
            """,
            ("live-root-recursive", str(root_md)),
        )
        con.commit()
        con.close()

        child = client.spawn(
            SpawnRequest(
                parent_run_id="live-root-recursive",
                kickoff=child_md,
                recipe="code-fix",
                child_run_id="live-child-recursive",
                allow_child_spawn=True,
                mode="dry-run",
                cwd=project,
                extra_env=env,
            )
        )
        if not child.ok:
            print(child.output)
            return 1

        grandchild = client.spawn(
            SpawnRequest(
                parent_run_id="live-child-recursive",
                kickoff=grandchild_md,
                recipe="code-fix",
                child_run_id="live-grandchild-recursive",
                depth=2,
                mode="dry-run",
                cwd=project,
                extra_env=env,
            )
        )
        if not grandchild.ok:
            print(grandchild.output)
            return 1

        con = sqlite3.connect(db)
        spawns = con.execute("SELECT COUNT(*) FROM run_spawns WHERE root_run_id=?", ("live-root-recursive",)).fetchone()[0]
        events = con.execute("SELECT COUNT(*) FROM run_events WHERE event_type='child.completed'").fetchone()[0]
        con.close()

        result = {
            "ok": spawns == 2 and events == 2,
            "root_run": root.run_id or "live-root-recursive",
            "child_run": child.child_run_id,
            "grandchild_run": grandchild.child_run_id,
            "spawn_count": spawns,
            "completed_events": events,
            "home": str(home),
        }
        print(json.dumps(result, indent=2))
        return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
