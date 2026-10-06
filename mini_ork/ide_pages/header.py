"""The IDE's always-visible numbers: title bar and status bar.

``board --json`` carries this as ``header``. It is polled every few seconds, so
everything here is a local read — the only network call is ContextNest's
cached ping, with a short timeout.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any


def _git(project: Path, *args: str) -> str:
    try:
        out = subprocess.run(["git", *args], cwd=project, capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return out.stdout if out.returncode == 0 else ""


def _version() -> str:
    try:
        import tomllib

        root = Path(__file__).resolve().parents[2]
        data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
        return str(data.get("project", {}).get("version") or "")
    except Exception:  # noqa: BLE001
        return ""


def _contextnest() -> bool:
    os.environ.setdefault("CN_TIMEOUT_SEC", "0.5")
    try:
        from mini_ork import cn_client

        return bool(cn_client.available())
    except Exception:  # noqa: BLE001
        return False


def _nodes() -> dict[str, Any]:
    try:
        from mini_ork.remote.nodes import load_registry

        names = sorted(load_registry())
    except Exception:  # noqa: BLE001
        names = []
    return {"configured": len(names), "names": names}


def header(home: Path, counts: dict[str, int]) -> dict[str, Any]:
    from mini_ork import cost_ledger

    home = home.absolute()
    project = home.parent
    os.environ.setdefault("MINI_ORK_HOME", str(home))
    worktrees = sum(1 for line in _git(project, "worktree", "list", "--porcelain").splitlines()
                    if line.startswith("worktree "))
    try:
        cap = float(os.environ.get("MO_DAILY_BUDGET_USD", "50") or 50)
    except ValueError:
        cap = 50.0
    try:
        from mini_ork import automations

        scheduler_on = bool(automations.scheduler_status(home).get("installed"))
    except Exception:  # noqa: BLE001
        scheduler_on = False
    db = home / "state.db"
    today_usd: float | None = (
        round(cost_ledger.spent_last_24h(db if db.is_file() else None), 2) if db.is_file() else None
    )
    return {
        "project": project.name,
        "branch": _git(project, "rev-parse", "--abbrev-ref", "HEAD").strip(),
        "worktrees": worktrees,
        "needs_you": int(counts.get("needs_you", 0)),
        "working": int(counts.get("working", 0)),
        "failed": int(counts.get("failed", 0)),
        "done": int(counts.get("done", 0)),
        "today_usd": today_usd,
        "cap_usd": round(cap, 2),
        "scheduler_on": scheduler_on,
        "contextnest": _contextnest(),
        "nodes": _nodes(),
        "version": _version(),
    }
