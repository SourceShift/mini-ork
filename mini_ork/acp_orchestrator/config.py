"""Lane discovery + default selection for the orchestrator harness.

The orchestrator can only drive lanes whose ``command[0]`` is the ``claude``
CLI — codex / opencode / openai-compat lanes have their own argv shapes and
session models. We enumerate the provider registry (one-shot at call time,
never cached across edits) and let ``resolve_provider`` do the env + model
plumbing for each lane so credential handling stays in the dispatch layer.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from mini_ork.context import context_env
from mini_ork.dispatch.providers import (
    _load_providers_registry,
    resolve_provider,
)
from mini_ork.web.recipes import load_lanes

__all__ = ["default_lane", "orchestrator_lanes"]


_LANE_NAME_OVERRIDES: dict[str, str] = {
    "opus": "Opus (Claude subscription)",
    "sonnet": "Sonnet (Claude subscription)",
}


def _lane_display_name(lane_id: str, model: str) -> str:
    """Pick the most informative label for a lane.

    Order:
    1. ``opus`` / ``sonnet`` get the friendly "(Claude subscription)" suffix.
    2. If the registry entry has a ``model:`` field, that is usually a real
       model id (``MiniMax-M3``, ``GLM-5.3``, ``deepseek-v4-pro[1m]``) and is
       more informative than the lane id itself.
    3. Fall back to the lane id.
    """
    if lane_id in _LANE_NAME_OVERRIDES:
        return _LANE_NAME_OVERRIDES[lane_id]
    if model:
        return model
    return lane_id


def orchestrator_lanes(root: Path | None = None) -> list[dict[str, str]]:
    """Every Claude-CLI lane the orchestrator can drive.

    Walks the providers registry, calls ``resolve_provider`` for each name,
    keeps only the lanes whose command head is ``claude``, and applies the
    ordering the kickoff spec promises (``opus`` first, then ``sonnet`` if
    present, then the remainder sorted alphabetically).

    A malformed registry entry (missing kind, etc.) is silently skipped for
    each lane: ``orchestrator_lanes`` is a UI list, not a strict resolver,
    and one bad entry must not poison the whole picker.
    """
    registry = _load_providers_registry(root)
    result: list[tuple[int, str, dict[str, str]]] = []
    for name in registry.keys():
        try:
            spec = resolve_provider(str(name), root)
        except (ValueError, TypeError):
            continue
        command = list(spec.command)
        if not command or command[0] != "claude":
            continue
        entry = registry.get(name) or {}
        model = ""
        if isinstance(entry, dict):
            model_val = entry.get("model")
            if isinstance(model_val, str):
                model = model_val
        if name == "opus":
            order = 0
        elif name == "sonnet":
            order = 1
        else:
            order = 2
        result.append((order, name, {"id": name, "name": _lane_display_name(name, model)}))

    result.sort(key=lambda triple: (triple[0], triple[1]))
    return [payload for _, _, payload in result]


def default_lane(home: Path | None = None) -> str:
    """Pick the orchestrator's default lane.

    Precedence (highest first):
    1. ``MO_ORCHESTRATOR_LANE`` env var (``context_env`` honours the
       per-run contextvar layer, so concurrent runs do not clobber each
       other's overrides).
    2. The ``orchestrator`` role in the home's lane map (``load_lanes``
       already merges the per-user ``MINI_ORK_AGENTS`` overlay).
    3. ``"opus"``.

    An unresolvable / unknown value is NOT raised — it logs once to stderr
    and falls back to ``"opus"``. The orchestrator is a UI surface: a
    fast-typing user who sets ``MO_ORCHESTRATOR_LANE=garbage`` should see
    the picker keep working, not a stack trace.
    """
    candidate = context_env("MO_ORCHESTRATOR_LANE", "").strip()
    if not candidate:
        try:
            lanes = load_lanes(home)
        except Exception:  # noqa: BLE001 — lane map failures must not break the picker
            lanes = {}
        candidate = lanes.get("orchestrator", "") if isinstance(lanes, dict) else ""

    if not candidate:
        return "opus"

    available = {entry["id"] for entry in orchestrator_lanes()}
    if candidate in available:
        return candidate

    _warn_unknown(candidate)
    return "opus"


# ── per-process stderr dedup ────────────────────────────────────────────────
# A Zed user flipping ``MO_ORCHESTRATOR_LANE`` mid-session (a less common
# case) or a config-overlay typo (the common case) should NOT spam stderr
# once per orchestrator turn. Module-level set, keyed by the bad value.

_WARNED: set[str] = set()


def _warn_unknown(value: str) -> None:
    if value in _WARNED:
        return
    _WARNED.add(value)
    print(
        f"mini-ork.acp_orchestrator: unknown lane {value!r}; falling back to 'opus'",
        file=sys.stderr,
    )


# Suppress an unused-import warning if the symbol is only referenced via
# the public API elsewhere in the slice.
_ = Any