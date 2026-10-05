"""Pure-logic helpers for the ``/race`` slash command (Zed S7a).

``/race <task>`` runs the thread's selected recipe on 2–3 models at once,
each in its own git worktree, and shows a table so the user can keep the
best one. The functions here are deliberately side-effect-light:

- ``known_lanes`` reads the effective provider registry (no hard-coding).
- ``parse_race_arg`` is pure: split the arg into lanes + task, surface
  errors as a string return value (the caller renders it).
- ``model_roles`` reads the recipe's workflow.yaml home-first.
- ``seed_run_config`` writes the run's lane-policy snapshot BEFORE
  ``launch_run`` is called, so ``bin/mini-ork run``'s idempotent
  ``snapshot_run_config`` is a no-op and the run's resolver sees the
  race lane for its whole life.
- ``render_race_table`` formats the per-lane outcome rows into the
  table the kickoff pins verbatim.

The dispatch-resolver pin (``seed_run_config`` + ``resolve_lane_model``)
is exercised by the test in ``tests/unit/test_acp_race.py`` so the
snap vs no-spy build is decided by a real verification rather than a
YAML-config-snap assertion.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any


DEFAULT_LANES: tuple[str, ...] = ("sonnet", "glm", "minimax")


def _registry_provider_names(home: Path) -> list[str]:
    """Lane keys the engine / home providers registry knows about.

    Reads ``mini_ork.dispatch.providers._load_providers_registry`` so
    ``/lanes`` and ``/race`` agree on the lane set. The function sets
    ``MINI_ORK_HOME`` to ``home`` for the read (the loader honours it),
    then restores the prior value.

    An empty / missing registry returns ``[]``; ``parse_race_arg`` then
    falls back to ``DEFAULT_LANES`` filtered to known lanes (which is
    empty → an error).
    """
    from mini_ork.dispatch import providers as _providers

    prior = os.environ.get("MINI_ORK_HOME")
    try:
        os.environ["MINI_ORK_HOME"] = str(home)
        try:
            reg = _providers._load_providers_registry()
        except Exception:
            return []
    finally:
        if prior is None:
            os.environ.pop("MINI_ORK_HOME", None)
        else:
            os.environ["MINI_ORK_HOME"] = prior
    return [str(k) for k in (reg or {}).keys()]


def known_lanes(home: Path) -> list[str]:
    """Return the lane names the effective provider registry exposes for ``home``.

    Stable order: ``sorted(...)`` over the loader's key set. Empty when
    the registry is missing or unreadable.
    """
    return sorted(_registry_provider_names(home))


def _is_known(lane: str, lanes: set[str]) -> bool:
    return lane in lanes


def _candidate_lanes_from_arg(first: str, known: set[str]) -> list[str] | None | str:
    """Classify the first token of a ``/race`` arg as a lane list or not.

    Returns:
      * the deduped lane list (in original order) when ``first`` is a
        2–4 element comma-separated list and every element is known;
      * the user-facing error string when the list mixes known and
        unknown lanes (kickoff: ``"Unknown lane <x>. Known: <list>."``);
      * ``None`` when ``first`` is not a lane list at all — the caller
        treats the whole arg as the task body.
    """
    if "," not in first:
        return None
    parts = [p.strip() for p in first.split(",") if p.strip()]
    if not parts or len(parts) > 4:
        return None
    known_hits = [p for p in parts if _is_known(p, known)]
    unknown_hits = [p for p in parts if not _is_known(p, known)]
    if known_hits and unknown_hits:
        known_list = ", ".join(sorted(known)) if known else "(none configured)"
        return f"Unknown lane {unknown_hits[0]}. Known: {known_list}."
    if not known_hits:
        # The first token merely contains a comma; the whole string is
        # the task and defaults apply (test: a task whose first word
        # merely contains a comma).
        return None
    seen: set[str] = set()
    out: list[str] = []
    for p in parts:
        if p in seen:
            continue
        seen.add(p)
        out.append(p)
    return out


def _default_lanes(known: set[str]) -> list[str]:
    """``MO_RACE_LANES`` → ``DEFAULT_LANES`` → filtered to ``known``, first 3.

    The env var wins (the operator can pin a default without rewriting
    the source); the literal default is the team suggestion; both are
    filtered to lanes the home's registry actually has.
    """
    env = os.environ.get("MO_RACE_LANES", "").strip()
    raw = [p.strip() for p in env.split(",") if p.strip()] if env else list(DEFAULT_LANES)
    seen: set[str] = set()
    out: list[str] = []
    for lane in raw:
        if lane in seen or lane not in known:
            continue
        seen.add(lane)
        out.append(lane)
        if len(out) >= 3:
            break
    return out


def parse_race_arg(arg: str, home: Path) -> tuple[list[str], str] | str:
    """``/race <task>`` or ``/race <lane,lane> <task>`` → ``(lanes, task)`` or error.

    Errors are returned as the user-facing string (the agent renders
    that as the message body, end turn). Lane count: 2–4 after dedup;
    fewer than 2 → ``"Racing needs at least two lanes..."``.
    """
    text = (arg or "").strip()
    if not text:
        return "What should they do? /race <task>"
    known = set(known_lanes(home))
    head, _, tail = text.partition(" ")
    lanes = _candidate_lanes_from_arg(head, known)
    if isinstance(lanes, str):
        # The first token mixes known + unknown lanes — propagate the
        # error message verbatim.
        return lanes
    if lanes is None:
        # The first token isn't a lane list — treat the WHOLE string as
        # the task and pick the default lane set.
        lanes = _default_lanes(known)
        task = text
    else:
        task = tail.strip()
        if not task:
            return "What should they do? /race <task>"
    if len(lanes) < 2:
        return "Racing needs at least two lanes — name them: /race sonnet,glm <task>."
    return lanes, task


def model_roles(home: Path, recipe: str) -> list[str]:
    """Roles to pin in the race run's agents.yaml.

    Reads the recipe's workflow.yaml via :func:`recipe_plan.recipe_dir`
    (home-first, then engine) and collects the ``model_lane`` of every
    ``implementer`` node. Always adds the literal roles ``"implementer"``
    and ``"worker"`` (the engine's dispatch resolver falls back through
    these when a recipe lane is unset). Order kept, duplicates removed.
    """
    roles: list[str] = ["implementer", "worker"]
    recipe_path = _resolve_recipe_dir(home, recipe)
    if recipe_path is None:
        return list(dict.fromkeys(roles))
    wf_path = recipe_path / "workflow.yaml"
    if not wf_path.is_file():
        return list(dict.fromkeys(roles))
    try:
        import yaml
        wf = yaml.safe_load(wf_path.read_text(encoding="utf-8")) or {}
    except Exception:
        return list(dict.fromkeys(roles))
    if not isinstance(wf, dict):
        return list(dict.fromkeys(roles))
    for node in wf.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        if str(node.get("type") or "") != "implementer":
            continue
        lane = str(node.get("model_lane") or "").strip()
        if lane:
            roles.append(lane)
    return list(dict.fromkeys(roles))


def _resolve_recipe_dir(home: Path, recipe: str) -> Path | None:
    """Recipe dir, resolved the way a run resolves it: home-first, then engine.

    Mirrors ``mini_ork.planning.recipe_plan.recipe_dir`` without taking
    on the import surface (race.py must not depend on cli modules). The
    caller passes ``home``; the engine root is read from
    ``MINI_ORK_ROOT`` or defaults to the package's parent.
    """
    if not recipe:
        return None
    from mini_ork.context import context_env

    home_str = str(home) if home else context_env("MINI_ORK_HOME", "")
    root_str = context_env("MINI_ORK_ROOT", "")
    for base in (home_str, root_str):
        if not base:
            continue
        for name in (recipe, recipe.replace("_", "-")):
            candidate = Path(base) / "recipes" / name
            if (candidate / "workflow.yaml").is_file() or (
                candidate / "artifact_contract.yaml"
            ).is_file():
                return candidate
    return None


def _load_effective_yaml(home: Path) -> dict[str, Any]:
    """The home's EFFECTIVE agents.yaml as a dict (or ``{}`` when none).

    Honours ``mini_ork.dispatch.agents_config.effective_path(home=home)``
    so the per-user overlay (if any) is merged in — race inherits the
    user's lane policy rather than the team template.
    """
    from mini_ork.dispatch import agents_config

    try:
        path = agents_config.effective_path(home=str(home))
    except ValueError:
        # Broken overlay — fall back to the tracked template (matches
        # web.recipes.load_lanes behaviour).
        try:
            path = agents_config.template_path(home=str(home))
        except Exception:
            return {}
    if not Path(path).is_file():
        return {}
    try:
        import yaml
        return yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def seed_run_config(home: Path, run_id: str, recipe: str, lane: str) -> Path:
    """Pin the race run's implementer lane; returns the overlay to pass as
    ``MINI_ORK_AGENTS`` in the run's env.

    Two files under ``<home>/runs/<run_id>/config/``, both = the home's
    effective policy with ``lanes[role] = lane`` for every role in
    :func:`model_roles`:

    - ``agents.race.yaml`` — the overlay. ``llm_dispatch.resolve_lane_model``
      (what a node's dispatch uses) reads ``<home>/config/agents.yaml``
      merged with ``$MINI_ORK_AGENTS``, never the run snapshot, so the pin
      only reaches dispatch through this env var.
    - ``agents.yaml`` — the run snapshot, read by the run-dir-aware
      resolvers (``steering.decision_service``). ``snapshot_run_config``
      never overwrites it.
    """
    doc = _load_effective_yaml(home)
    if not isinstance(doc, dict):
        doc = {}
    lanes = doc.get("lanes")
    if not isinstance(lanes, dict):
        lanes = {}
    for role in model_roles(home, recipe):
        lanes[role] = lane
    doc["lanes"] = lanes
    import yaml

    config = home / "runs" / run_id / "config"
    config.mkdir(parents=True, exist_ok=True)
    text = yaml.safe_dump(doc, sort_keys=True)
    (config / "agents.yaml").write_text(text, encoding="utf-8")
    overlay = config / "agents.race.yaml"
    overlay.write_text(text, encoding="utf-8")
    return overlay


_RESULT_WORDS = {"published": "verified", "failed": "failed", "rolled_back": "rolled back"}


def result_word(status: str | None) -> str:
    """``published`` → ``verified``; ``rolled_back`` → ``rolled back``; else the status."""
    return _RESULT_WORDS.get(str(status or ""), str(status or "running"))


def _format_seconds(seconds: Any) -> str:
    """``3m 12s`` (>=1m) or ``42s`` (under a minute); never raises."""
    try:
        s = max(0, int(float(seconds)))
    except (TypeError, ValueError):
        return "—"
    if s >= 60:
        return f"{s // 60}m {s % 60:02d}s"
    return f"{s}s"


def _format_cost(cost: Any) -> str:
    """``$0.42`` or ``$0.42`` with two decimals; ``—`` when missing/zero."""
    try:
        c = float(cost or 0.0)
    except (TypeError, ValueError):
        return "—"
    if c <= 0:
        return "—"
    return f"${c:.2f}"


def _format_change(added: Any, removed: Any) -> str:
    """``+12 −3`` or ``—`` when both zero/missing."""
    try:
        a = int(added or 0)
    except (TypeError, ValueError):
        a = 0
    try:
        r = int(removed or 0)
    except (TypeError, ValueError):
        r = 0
    if a == 0 and r == 0:
        return "—"
    return f"+{a} −{r}"


def _mark_for_status(status: str) -> str:
    """``✓`` / ``✗`` / ``●`` per the kickoff's table glyph vocabulary."""
    s = (status or "").lower()
    if s == "verified":
        return "✓"
    if s == "failed":
        return "✗"
    if s == "rolled back" or s == "rolled_back":
        return "✗"
    return "●"


def render_race_table(rows: list[dict[str, Any]]) -> str:
    """Render the race outcome table (verbatim per kickoff §Mechanism).

    Each row is ``{lane, run_id, status, added, removed, cost_usd, seconds}``.
    ``status`` is one of ``verified`` / ``failed`` / ``rolled back`` /
    anything-else (still running → ``●``).
    """
    header = "| | model | result | change | cost | time |"
    sep = "|---|---|---|---|---|---|"
    lines = [header, sep]
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        lane = str(row.get("lane") or "—")
        status = str(row.get("status") or "")
        mark = _mark_for_status(status)
        result = status or "running"
        change = _format_change(row.get("added"), row.get("removed"))
        cost = _format_cost(row.get("cost_usd"))
        time_s = _format_seconds(row.get("seconds"))
        lines.append(f"| {mark} | {lane} | {result} | {change} | {cost} | {time_s} |")
    return "\n".join(lines)


__all__ = [
    "DEFAULT_LANES",
    "known_lanes",
    "parse_race_arg",
    "model_roles",
    "seed_run_config",
    "render_race_table",
]


# Keep a thin wrapper around ``_load_providers_registry`` so the test
# surface can monkeypatch without reaching into ``mini_ork.dispatch.providers``.
def load_providers_registry(home: Path) -> dict[str, Any]:
    """Convenience: same as ``mini_ork.dispatch.providers._load_providers_registry``.

    Reads from the home's ``config/providers.yaml`` (or the engine's).
    Exposed here so ``known_lanes`` does not import the providers
    module at module load (deferred import keeps the test module
    hermetic).
    """
    from mini_ork.dispatch import providers as _providers

    prior = os.environ.get("MINI_ORK_HOME")
    try:
        os.environ["MINI_ORK_HOME"] = str(home)
        return dict(_providers._load_providers_registry() or {})
    finally:
        if prior is None:
            os.environ.pop("MINI_ORK_HOME", None)
        else:
            os.environ["MINI_ORK_HOME"] = prior