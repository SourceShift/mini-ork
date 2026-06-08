#!/usr/bin/env python3
"""Live validation: mini-ork builds a TypeScript mini-ork from one markdown file.

This is intentionally not a unit test. It creates a blank temporary repository,
passes one markdown kickoff to mini-ork, requires the allowed provider roster
(MiniMax, Codex, GLM, Kimi), exercises recursive child spawning, and then checks
whether the generated TypeScript system builds, tests, runs, and emits learning
signals.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from mini_ork import MiniOrk, RunRequest, SpawnRequest


ROOT = Path(__file__).resolve().parents[2]
KICKOFF = ROOT / "docs/production-validation/kickoffs/typescript-mini-ork-from-scratch.md"
DEFAULT_SECRETS = Path("/Volumes/docker-ssd/ps/mini-ork/.mini-ork/config/secrets.local.sh")
REPORT_PATH = ROOT / "docs/production-validation/runs/20260608-typescript-mini-ork-from-scratch-live.json"
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def strip_terminal_controls(text: str) -> str:
    text = ANSI_RE.sub("", text)
    return "".join(ch for ch in text if ch == "\n" or ch == "\t" or ord(ch) >= 32)


def run(cmd: list[str], cwd: Path, env: dict[str, str], timeout: int = 180) -> dict[str, object]:
    started = time.time()
    completed = subprocess.run(
        cmd,
        cwd=cwd,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
    )
    return {
        "cmd": cmd,
        "cwd": str(cwd),
        "returncode": completed.returncode,
        "output": completed.stdout[-4000:],
        "duration_seconds": round(time.time() - started, 2),
    }


def source_secret_presence(secrets: Path) -> dict[str, bool]:
    script = (
        "set +u; "
        f"source {str(secrets)!r} 2>/dev/null || true; "
        "for v in MINIMAX_API_KEY GLM_API_KEY KIMI_API_KEY; do "
        "if [ -n \"${!v:-}\" ]; then echo \"$v=present\"; else echo \"$v=missing\"; fi; "
        "done"
    )
    out = subprocess.check_output(["bash", "-lc", script], text=True)
    presence: dict[str, bool] = {}
    for raw_line in out.splitlines():
        line = strip_terminal_controls(raw_line).strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        presence[key.strip()] = value.strip() == "present"
    return presence


def write_provider_policy(home: Path) -> None:
    config = home / "config"
    config.mkdir(parents=True, exist_ok=True)
    (config / "agents.yaml").write_text(
        """lanes:
  planner: glm
  researcher: kimi
  implementer: codex
  worker: codex
  reviewer: minimax
  verifier: glm
  reflector: glm
  publisher: codex
  rollback: glm
  glm_lens: glm
  kimi_lens: kimi
  codex_lens: codex
  minimax_lens: minimax
budget:
  per_epic_usd: 20.00
  per_run_usd: 10.00
  daily_cap_usd: 100.00
""",
        encoding="utf-8",
    )


def seed_example(project: Path) -> None:
    examples = project / "examples"
    examples.mkdir(exist_ok=True)
    (examples / "kickoff.md").write_text(
        """# Example generated-system kickoff

## Goal
Run a tiny recursive validation task.

## Success Criteria
- The orchestrator emits a final JSON summary.
- Learning signals include recursive_spawn.

## Scope
- In-memory execution only.

## Verification
- npm test
""",
        encoding="utf-8",
    )


def count_db(db: Path, sql: str) -> int:
    con = sqlite3.connect(db)
    try:
        return int(con.execute(sql).fetchone()[0])
    except sqlite3.OperationalError:
        return 0
    finally:
        con.close()


def read_tail(path: Path, limit: int = 6000) -> str:
    try:
        return strip_terminal_controls(path.read_text(encoding="utf-8", errors="replace"))[-limit:]
    except Exception as exc:
        return f"<failed to read {path}: {exc}>"


def collect_diagnostics(project: Path, limit: int = 40) -> list[dict[str, str]]:
    patterns = (
        ".mini-ork/runs/**/*.err.log",
        ".mini-ork/runs/**/*.shim.err",
        ".mini-ork/runs/**/*.raw",
        ".mini-ork/runs/**/*.out",
        ".mini-ork/runs/**/run_profile.json",
        ".mini-ork/runs/**/plan.json",
        ".mini-ork/runs/**/*.cost",
    )
    seen: set[Path] = set()
    diagnostics: list[dict[str, str]] = []
    for pattern in patterns:
        for path in sorted(project.glob(pattern)):
            if path in seen or not path.is_file():
                continue
            seen.add(path)
            diagnostics.append(
                {
                    "path": str(path.relative_to(project)),
                    "bytes": str(path.stat().st_size),
                    "tail": read_tail(path),
                }
            )
            if len(diagnostics) >= limit:
                return diagnostics
    return diagnostics


def write_markdown_report(result: dict[str, object]) -> Path:
    md_path = REPORT_PATH.with_suffix(".md")
    steps = result.get("steps", {})
    parent = steps.get("parent_run", {}) if isinstance(steps, dict) else {}
    artifacts = result.get("artifacts", {})
    db = result.get("db", {})
    diagnostics = result.get("diagnostics", [])
    lines = [
        "# TypeScript Mini-Ork From Scratch Live Validation",
        "",
        f"- ok: `{result.get('ok')}`",
        f"- preflight_ok: `{result.get('preflight_ok')}`",
        f"- project: `{result.get('project')}`",
        f"- workspace_preserved: `{result.get('workspace_preserved')}`",
        f"- report_json: `{REPORT_PATH}`",
        "",
        "## Outcome",
        "",
    ]
    if isinstance(parent, dict):
        lines.extend(
            [
                f"- parent_ok: `{parent.get('ok')}`",
                f"- parent_returncode: `{parent.get('returncode')}`",
                "",
                "```text",
                str(parent.get("output_tail", ""))[-1200:],
                "```",
                "",
            ]
        )
    lines.extend(["## Artifacts", ""])
    if isinstance(artifacts, dict):
        for path, exists in artifacts.items():
            lines.append(f"- `{path}`: `{exists}`")
    lines.extend(["", "## Database", ""])
    if isinstance(db, dict):
        for key, value in db.items():
            lines.append(f"- `{key}`: `{value}`")
    lines.extend(["", "## Diagnostics", ""])
    if isinstance(diagnostics, list) and diagnostics:
        for item in diagnostics[:8]:
            if not isinstance(item, dict):
                continue
            lines.extend(
                [
                    f"### `{item.get('path')}`",
                    "",
                    "```text",
                    str(item.get("tail", ""))[-2000:],
                    "```",
                    "",
                ]
            )
    else:
        lines.append("- No diagnostic files captured.")
    md_path.write_text("\n".join(lines), encoding="utf-8")
    return md_path


def main() -> int:
    secrets = Path(os.environ.get("MINI_ORK_SECRETS", str(DEFAULT_SECRETS)))
    provider_presence = source_secret_presence(secrets)
    cli_presence = {
        "codex": shutil.which("codex") is not None,
        "claude": shutil.which("claude") is not None,
        "node": shutil.which("node") is not None,
        "npm": shutil.which("npm") is not None,
        "tsc": shutil.which("tsc") is not None,
    }
    preflight_ok = all(provider_presence.values()) and all(cli_presence.values())

    project = Path(tempfile.mkdtemp(prefix="mini-ork-ts-from-scratch-"))
    keep_workspace = os.environ.get("MO_TS_FROM_SCRATCH_KEEP", "0") == "1"
    try:
        home = project / ".mini-ork"
        db = home / "state.db"
        subprocess.run(["git", "init", "-q"], cwd=project, check=True)
        seed_example(project)

        env = os.environ.copy()
        env.update(
            {
                "MINI_ORK_ROOT": str(ROOT),
                "MINI_ORK_HOME": str(home),
                "MINI_ORK_DB": str(db),
                "MINI_ORK_SECRETS": str(secrets),
                "MINI_ORK_DRY_RUN": "0",
                "MINI_ORK_NO_COLOR": "1",
                "MINI_ORK_RUN_ID": "live-ts-root",
                "MINI_ORK_GRADIENT_MODEL": "glm",
                "MINI_ORK_TYPECHECK_CMD": "tsc --noEmit",
                "MINI_ORK_TEST_CMD": "npm test",
                "MO_TRACE_RICH": "0",
                "MO_DAILY_BUDGET_USD": "100",
                "CODEX_SANDBOX": "workspace-write",
            }
        )

        result: dict[str, object] = {
            "ok": False,
            "preflight_ok": preflight_ok,
            "provider_presence": provider_presence,
            "cli_presence": cli_presence,
            "project": str(project),
            "home": str(home),
            "workspace_preserved": True,
            "run_id": "live-ts-root",
            "steps": {},
            "artifacts": {},
            "db": {},
            "diagnostics": [],
            "blockers": [],
        }

        if not preflight_ok:
            result["blockers"] = ["missing provider key or required CLI"]
            result["diagnostics"] = collect_diagnostics(project)
            REPORT_PATH.write_text(json.dumps(result, indent=2), encoding="utf-8")
            result["report_path"] = str(REPORT_PATH)
            result["markdown_report_path"] = str(write_markdown_report(result))
            print(json.dumps(result, indent=2))
            return 2

        client = MiniOrk(root=ROOT, home=home, db=db)
        write_provider_policy(home)

        parent = client.run(
            RunRequest(
                kickoff=KICKOFF,
                recipe="code-fix",
                mode="live",
                cwd=project,
                auto_init=True,
                timeout_seconds=int(os.environ.get("MO_TS_FROM_SCRATCH_TIMEOUT", "1800")),
                extra_env=env,
            )
        )
        result["steps"]["parent_run"] = {
            "ok": parent.ok,
            "returncode": parent.returncode,
            "task_class": parent.task_class,
            "plan_path": str(parent.plan_path) if parent.plan_path else "",
            "output_tail": parent.output[-4000:],
        }

        if not parent.ok:
            required = [
                "package.json",
                "tsconfig.json",
                "src/types.ts",
                "src/profile.ts",
                "src/planner.ts",
                "src/orchestrator.ts",
                "src/learning.ts",
                "src/cli.ts",
                "tests/orchestrator.test.ts",
            ]
            result["artifacts"] = {path: (project / path).exists() for path in required}
            if db.exists():
                result["db"] = {
                    "task_runs": count_db(db, "SELECT COUNT(*) FROM task_runs"),
                    "execution_traces": count_db(db, "SELECT COUNT(*) FROM execution_traces"),
                    "run_spawns": count_db(db, "SELECT COUNT(*) FROM run_spawns"),
                    "gradient_records": count_db(db, "SELECT COUNT(*) FROM gradient_records"),
                    "workflow_candidates": count_db(db, "SELECT COUNT(*) FROM workflow_candidates"),
                }
            result["steps"]["recursive_spawns"] = {
                "architecture_child": {"ok": False, "status": "blocked_parent_failed"},
                "learning_grandchild": {"ok": False, "status": "blocked_parent_failed"},
            }
            result["steps"]["generated_project_checks"] = {
                "typecheck": {"returncode": None, "output": "blocked: parent planner failed"},
                "test": {"returncode": None, "output": "blocked: parent planner failed"},
                "cli": {"returncode": None, "output": "blocked: parent planner failed"},
            }
            result["steps"]["learning_validation"] = {
                "reflect": {"returncode": None, "output": "blocked: parent planner failed"},
                "improve": {"returncode": None, "output": "blocked: parent planner failed"},
            }
            result["blockers"] = ["parent planner dispatch failed; recursive generation not attempted"]
            result["diagnostics"] = collect_diagnostics(project)
            REPORT_PATH.write_text(json.dumps(result, indent=2), encoding="utf-8")
            result["report_path"] = str(REPORT_PATH)
            result["markdown_report_path"] = str(write_markdown_report(result))
            print(json.dumps(result, indent=2))
            return 1

        write_provider_policy(home)

        child_arch = client.spawn(
            SpawnRequest(
                parent_run_id="live-ts-root",
                kickoff=KICKOFF,
                recipe="code-fix",
                child_run_id="live-ts-child-architecture",
                allow_child_spawn=True,
                mode="live",
                cwd=project,
                timeout_seconds=900,
                extra_env={**env, "MINI_ORK_RUN_ID": "live-ts-child-architecture"},
            )
        )
        child_learning = client.spawn(
            SpawnRequest(
                parent_run_id="live-ts-child-architecture",
                kickoff=KICKOFF,
                recipe="code-fix",
                child_run_id="live-ts-grandchild-learning",
                depth=2,
                mode="live",
                cwd=project,
                timeout_seconds=900,
                extra_env={**env, "MINI_ORK_RUN_ID": "live-ts-grandchild-learning"},
            )
        )
        result["steps"]["recursive_spawns"] = {
            "architecture_child": {"ok": child_arch.ok, "status": child_arch.spawn_status, "output_tail": child_arch.output[-2000:]},
            "learning_grandchild": {"ok": child_learning.ok, "status": child_learning.spawn_status, "output_tail": child_learning.output[-2000:]},
        }

        checks = {}
        for name, cmd in {
            "typecheck": ["tsc", "--noEmit"],
            "test": ["npm", "test"],
            "cli": ["node", "dist/cli.js", "run", "examples/kickoff.md"],
        }.items():
            try:
                checks[name] = run(cmd, project, env, timeout=240)
            except Exception as exc:
                checks[name] = {"cmd": cmd, "returncode": None, "output": str(exc)}
        result["steps"]["generated_project_checks"] = checks

        reflect = run([str(ROOT / "bin/mini-ork"), "reflect", "--since", "0"], project, env, timeout=300)
        improve = run([str(ROOT / "bin/mini-ork"), "improve", "--task-class", "code_fix", "--limit", "1"], project, env, timeout=300)
        result["steps"]["learning_validation"] = {"reflect": reflect, "improve": improve}

        required = [
            "package.json",
            "tsconfig.json",
            "src/types.ts",
            "src/profile.ts",
            "src/planner.ts",
            "src/orchestrator.ts",
            "src/learning.ts",
            "src/cli.ts",
            "tests/orchestrator.test.ts",
        ]
        result["artifacts"] = {path: (project / path).exists() for path in required}

        if db.exists():
            result["db"] = {
                "task_runs": count_db(db, "SELECT COUNT(*) FROM task_runs"),
                "execution_traces": count_db(db, "SELECT COUNT(*) FROM execution_traces"),
                "run_spawns": count_db(db, "SELECT COUNT(*) FROM run_spawns"),
                "gradient_records": count_db(db, "SELECT COUNT(*) FROM gradient_records"),
                "workflow_candidates": count_db(db, "SELECT COUNT(*) FROM workflow_candidates"),
            }

        generated_ok = all(result["artifacts"].values()) and all(
            isinstance(v, dict) and v.get("returncode") == 0 for v in checks.values()
        )
        recursive_ok = bool(result["db"].get("run_spawns", 0) >= 2)
        learning_ok = bool(result["db"].get("execution_traces", 0) > 0) and (
            result["db"].get("gradient_records", 0) > 0 or reflect.get("returncode") == 0
        )
        result["ok"] = bool(parent.ok and child_arch.ok and child_learning.ok and generated_ok and recursive_ok and learning_ok)

        result["diagnostics"] = collect_diagnostics(project)
        if result["ok"] and not keep_workspace:
            shutil.rmtree(project, ignore_errors=True)
            result["workspace_preserved"] = False
        REPORT_PATH.write_text(json.dumps(result, indent=2), encoding="utf-8")
        result["report_path"] = str(REPORT_PATH)
        result["markdown_report_path"] = str(write_markdown_report(result))
        print(json.dumps(result, indent=2))
        return 0 if result["ok"] else 1
    except Exception:
        print(f"live validation crashed; preserved workspace: {project}", file=sys.stderr)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
