"""Placement env resolution shared by the run lifecycle and ``recover``.

The lifecycle (``mini_ork/cli/main.py``) is the only place that used to turn a
run's placement into process env. A recovery re-enters ``execute`` directly, so
it never passes through that lifecycle — and a ``--placement remote`` run then
executes every node on the host (RB-06). This module holds the validation and
the exact env contract both entry points must share:

  ``MO_PLACEMENT=<local|remote>``
  ``MO_NODE_ENV=<profile>`` (when set)
  ``MO_REMOTE_SESSION_SCOPE=lifecycle`` when placement is ``remote``

Nothing here adopts a default placement: ``MO_DEFAULT_NODE_ENV`` is a
*fresh-run* policy (``cli.main._apply_default_placement``) and applying it to a
revival would turn a host run into a remote one. Recovery restores what the run
recorded, never a guess.
"""
from __future__ import annotations

import json
import os
from typing import Mapping

from mini_ork.context import publish_env

__all__ = [
    "placement_env_map",
    "publish_placement_env",
    "read_run_placement",
    "validate_placement_env",
]


def read_run_placement(run_dir: str) -> tuple[str, str]:
    """Return ``(placement, environment)`` recorded in ``run_profile.json``.

    The keys are exactly the ones ``gen_profile`` writes: ``placement`` from
    ``MO_PLACEMENT`` and ``environment`` from ``MO_NODE_ENV`` (each only when
    resolved). Fail-soft — a missing/corrupt profile, or a value of the wrong
    shape, reads as "no recorded placement" so a legacy run keeps today's
    host behaviour.
    """
    if not run_dir:
        return "", ""
    try:
        with open(os.path.join(run_dir, "run_profile.json"), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError, TypeError):
        return "", ""
    if not isinstance(data, dict):
        return "", ""
    placement = str(data.get("placement") or "").strip().lower()
    node_env = str(data.get("environment") or "").strip()
    return placement, node_env


def placement_env_map(placement: str, node_env: str) -> dict[str, str]:
    """The variables the lifecycle publishes for a resolved placement.

    Byte-identical to the lifecycle's publish tail: values are omitted when
    empty, and a remote placement also selects lifecycle-scoped sessions so
    execute keeps the node for the later steps and the lifecycle releases it.
    """
    overrides: dict[str, str] = {}
    if placement:
        overrides["MO_PLACEMENT"] = placement
    if node_env:
        overrides["MO_NODE_ENV"] = node_env
    if placement == "remote":
        # One node session for the whole run: execute keeps it for the later
        # steps; _release_remote_session ends it when the lifecycle returns.
        overrides["MO_REMOTE_SESSION_SCOPE"] = "lifecycle"
    return overrides


def publish_placement_env(placement: str, node_env: str) -> None:
    """Publish :func:`placement_env_map` through the canonical writer."""
    overrides = placement_env_map(placement, node_env)
    if overrides:
        publish_env(overrides)


def validate_placement_env(
    placement: str,
    node_env: str,
    *,
    env: Mapping[str, str],
    defaulted: bool = False,
) -> str:
    """Validate a resolved placement, returning a problem message or ``""``.

    Mirrors the lifecycle's validation (``main.py``): an unknown placement is
    rejected, and a remote placement needs a loadable environment profile.
    ``defaulted`` only changes the wording for a profile adopted from
    ``MO_DEFAULT_NODE_ENV`` — this helper itself never defaults.
    """
    if placement not in ("", "local", "remote"):
        return f"MO_PLACEMENT: expected local|remote (got '{placement}')"
    if placement != "remote":
        return ""

    from mini_ork.remote.environments import list_profiles, load_profile  # noqa: PLC0415

    problem = ""
    if not node_env:
        problem = "--placement remote requires --env <name> (or MO_NODE_ENV)"
    else:
        try:
            load_profile(node_env, env=env)
        except FileNotFoundError:
            problem = f"--env: environment profile '{node_env}' not found"
        except ValueError as exc:  # schema violation or a secret-looking env key
            if defaulted:
                problem = (f"default environment '{node_env}' (MO_DEFAULT_NODE_ENV) "
                           f"is invalid: {exc}")
            else:
                problem = f"--env: environment profile '{node_env}' is invalid: {exc}"
    if problem:
        names = list_profiles(env=env)
        avail = ", ".join(names) if names else (
            "none (add config/environments/<name>.yaml; see default.yaml.example)")
        problem = f"{problem}. Available profiles: {avail}"
    return problem
