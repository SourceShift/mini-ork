"""Pages for the mini-ork IDE — one module per activity-bar page.

``mini-ork board page <key> [--tab T] [--arg k=v ...]`` returns
``build_page(home, key, tab, args)``; the shapes are in :mod:`.spec`. Each page
module exposes ``build(home: Path, tab: str | None, args: dict[str, str]) ->
dict`` and is imported only when asked for, so one broken page never costs the
others.
"""
from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

# key → the IDE tab label. ``run`` is one run's tab (``args["run"]``).
PAGES: dict[str, str] = {
    "run": "Run",
    "orch": "Orchestrator",
    "runs": "Runs",
    "changes": "Changes",
    "verify": "Verify & safety",
    "recipes": "Recipes & epics",
    "autos": "Automations",
    "lanes": "Lanes & cost",
    "learn": "Learning & memory",
    "context": "Context",
    "nodes": "Nodes",
    "setup": "Setup & health",
}


def build_page(home: Path, key: str, tab: str | None = None,
               args: dict[str, str] | None = None) -> dict[str, Any]:
    if key not in PAGES:
        return {"ok": False, "error": f"no page {key!r} (pages: {', '.join(PAGES)})"}
    try:
        module = importlib.import_module(f"mini_ork.ide_pages.{key}")
        out = module.build(home, tab or None, dict(args or {}))
    except Exception as exc:  # noqa: BLE001 — the IDE shows the error in the tab
        return {"ok": False, "key": key, "error": f"{type(exc).__name__}: {exc}"}
    out.setdefault("label", PAGES[key])
    return out
