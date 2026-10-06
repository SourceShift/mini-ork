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


def _git_common_dir(project: Path) -> Path | None:
    """The shared git dir for ``project`` — its worktrees' ``.git/worktrees/<name>/``
    siblings live under this path. ``None`` when ``git rev-parse`` fails or returns
    a relative path (avoids CWD lookups)."""
    out = _git(project, "rev-parse", "--git-common-dir").strip()
    if not out:
        return None
    p = Path(out)
    if not p.is_absolute():
        return None
    return p


def _worktree_count(project: Path) -> int:
    """Count linked worktrees from the common git dir — no subprocess for the count.

    ``git worktree list --porcelain`` returns the same number as one main repo
    (its own ``.git`` is also a worktree entry on disk) plus the entries under
    ``<git-common-dir>/worktrees/``. We read the directory listing instead of
    shelling out — ``git worktree list`` cost 3.4 s on a 1,115-worktree home.
    """
    common = _git_common_dir(project)
    if common is None:
        return 0
    worktrees_dir = common / "worktrees"
    n = 1  # the main checkout itself
    if worktrees_dir.is_dir():
        try:
            n += sum(1 for entry in os.scandir(worktrees_dir) if entry.is_dir())
        except OSError:
            pass
    return n


def _branch(project: Path) -> str:
    """Branch name — read ``.git/HEAD`` (or the worktree's HEAD file) directly.

    The file's first line is either ``ref: refs/heads/<name>`` (a branch) or a
    raw commit sha (detached HEAD). The cheap path skips ``git rev-parse``; we
    fall back to it only when the file is missing or malformed.
    """
    candidates = [project / ".git", project]
    for base in candidates:
        head = base / "HEAD"
        if not head.is_file():
            continue
        try:
            line = head.read_text(encoding="utf-8", errors="replace").splitlines()[0]
        except (OSError, IndexError):
            continue
        if line.startswith("ref: refs/heads/"):
            return line[len("ref: refs/heads/"):].strip()
        if line and not line.startswith("ref:"):
            return line.strip()[:12]  # detached: short SHA is enough for the header
    return _git(project, "rev-parse", "--abbrev-ref", "HEAD").strip()


def _version() -> str:
    try:
        import tomllib

        root = Path(__file__).resolve().parents[2]
        data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
        return str(data.get("project", {}).get("version") or "")
    except Exception:  # noqa: BLE001
        return ""


def _contextnest() -> bool:
    # Pass the timeout explicitly. ``setdefault`` lets an ambient CN_TIMEOUT_SEC
    # of 8 s win and stall the poll, so override for the call only — restore
    # whatever was there before (no process-wide mutation).
    prev = os.environ.get("CN_TIMEOUT_SEC")
    os.environ["CN_TIMEOUT_SEC"] = "0.5"
    try:
        from mini_ork import cn_client

        return bool(cn_client.available())
    except Exception:  # noqa: BLE001
        return False
    finally:
        if prev is None:
            os.environ.pop("CN_TIMEOUT_SEC", None)
        else:
            os.environ["CN_TIMEOUT_SEC"] = prev


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
    worktrees = _worktree_count(project)
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
        "branch": _branch(project),
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
