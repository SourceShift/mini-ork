"""Node registry for the remote ``Workspace`` backend (remote-nodes-06, §2).

Maps a logical node name to a URL + bearer token + max-sessions triple. The
default lookup is :func:`select_node`, which is called by the remote backend's
factory. Tokens are read from env **at call time** (see ``MINI_ORK_SECRETS_FOR_FOREIGN_HOME``
lesson), so a rotation does not require a restart and a process restart does
not pin a stale value.

Lookup order in :func:`select_node`:

  1. ``MO_NODE_URL`` + ``MO_NODE_TOKEN`` (one-off, no name, ignores the registry).
  2. ``MO_NODE`` -> look up the named entry in ``config/nodes.yaml``.
  3. If ``nodes.yaml`` declares a ``default_node``, fall back to that.

Registry resolution:

  * ``$MINI_ORK_HOME/config/nodes.yaml`` is the LIVE copy (operator-edited).
  * ``$MINI_ORK_ROOT/config/nodes.yaml`` is the TEMPLATE (committed).
  * A LIVE file MERGES PER NODE over the TEMPLATE: a live entry with the same
    name as a template entry overrides only the fields it sets. A live entry
    with a NEW name is appended. This avoids the "tabs of providers.yaml drops
    lanes" trap (memory feedback_shadow_providers_yaml_drops_lanes) — the
    template defaults (LANES) keep flowing through.

Tokens NEVER appear in ``config/nodes.yaml``. Only the NAME of the env var to
read (``token_env: MO_NODE_TOKEN``); the value lives in ``$MO_NODE_TOKEN``
in the operator's shell / secret store.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping, NamedTuple

import yaml

__all__ = ["Node", "select_node", "load_registry"]


class Node(NamedTuple):
    """One registered node-agent.

    ``token`` is the resolved bearer token (NEVER persisted to disk). The
    registry on disk only carries ``token_env`` — the name of the env var to
    read at call time. ``select_node`` resolves ``token_env`` -> ``token``
    here so the rest of the stack never sees env-var plumbing.
    """

    name: str
    url: str
    token: str
    max_sessions: int = 1
    # The NAME of the env var the token came from — the workspace re-reads it
    # at call time and records it in the session marker for kill_run.
    token_env: str = "MO_NODE_TOKEN"


def _config_paths(env: Mapping[str, str]) -> tuple[Path, Path]:
    """Return ``(template_path, live_path)`` for the nodes.yaml pair.

    ``MINI_ORK_HOME`` is the live config root (operator-edited, per
    ``mini_ork.runtime.context.RunContext``); ``MINI_ORK_ROOT`` is the engine
    checkout where the template ships. Falls back to ``os.getcwd()`` so an
    out-of-tree test that imports this module does not crash on import-time
    resolution (we do NOT read the files here — just compute the paths).
    """
    home = env.get("MINI_ORK_HOME") or os.getcwd()
    root = env.get("MINI_ORK_ROOT") or os.getcwd()
    return Path(root) / "config" / "nodes.yaml", Path(home) / "config" / "nodes.yaml"


def _merge_nodes(template: dict, live: dict) -> dict[str, dict]:
    """Per-node DEEP merge: a live entry overrides only the fields it sets.

    Why deep and not shallow: a live entry like ``{max_sessions: 4}`` must
    inherit the template's ``url`` + ``token_env``. A shallow ``update``
    would wipe the URL and leave the operator with a broken node. Verified
    by ``test_shadow_nodes_yaml_merge_preserves_template_lanes`` (memory
    feedback_shadow_providers_yaml_drops_lanes).
    """
    merged: dict[str, dict] = {}
    for name, entry in {**(template or {}), **(live or {})}.items():
        entry_dict = entry if isinstance(entry, dict) else {}
        if (
            name in template
            and name in live
            and isinstance(template.get(name), dict)
            and isinstance(live.get(name), dict)
        ):
            merged[name] = {**template[name], **live[name]}
        else:
            merged[name] = entry_dict
    return merged


def load_registry(*, env: Mapping[str, str] | None = None) -> dict[str, dict]:
    """Load and merge the nodes.yaml pair. Pure function over ``env``.

    Returns a ``{name: entry_dict}`` view. Missing files are non-fatal: a
    template-only deployment uses the committed defaults; a live-only
    deployment (no template) uses the operator file verbatim.
    """
    src = os.environ if env is None else env
    template_path, live_path = _config_paths(src)
    template: dict = {}
    live: dict = {}
    if template_path.is_file():
        template = (yaml.safe_load(template_path.read_text(encoding="utf-8")) or {}).get("nodes", {})
    if live_path.is_file():
        live = (yaml.safe_load(live_path.read_text(encoding="utf-8")) or {}).get("nodes", {})
    return _merge_nodes(template, live)


def select_node(*, env: Mapping[str, str] | None = None) -> Node:
    """Resolve the active node per the kickoff's §2 rules. Raises on miss.

    Order:

      1. ``MO_NODE_URL`` set (one-off): name is ``"<one-off>"``, token read
         from ``MO_NODE_TOKEN`` (BOTH required when URL is set).
      2. ``MO_NODE`` set: look up that name in the registry.
      3. Registry declares ``default_node: <name>``: use it.
      4. None of the above: raise ``RuntimeError`` (loud — never silently
         bind to a host execution).
    """
    src = os.environ if env is None else env
    one_off_url = (src.get("MO_NODE_URL") or "").strip()
    if one_off_url:
        token = src.get("MO_NODE_TOKEN") or ""
        return Node(name="<one-off>", url=one_off_url, token=token, max_sessions=1)

    registry = load_registry(env=src)
    if not registry:
        raise RuntimeError(
            "no node registry found; set MO_NODE_URL+MO_NODE_TOKEN for a "
            "one-off, or MO_NODE + a node in config/nodes.yaml"
        )

    chosen_raw = (src.get("MO_NODE") or "").strip()
    if chosen_raw:
        chosen: str = chosen_raw
    else:
        default = registry.get("default_node")
        chosen = default if isinstance(default, str) else ""
    if not chosen:
        raise RuntimeError(
            "no node selected; set MO_NODE=<name>, or MO_NODE_URL+MO_NODE_TOKEN, "
            "or 'default_node:' in config/nodes.yaml"
        )
    entry_raw = registry.get(chosen, {})
    entry: dict = entry_raw if isinstance(entry_raw, dict) else {}
    if not entry:
        raise RuntimeError(
            f"MO_NODE={chosen!r} is not in config/nodes.yaml; "
            f"known: {sorted(registry)}"
        )
    url = entry.get("url")
    if not isinstance(url, str) or not url:
        raise RuntimeError(
            f"node {chosen!r} in config/nodes.yaml has no 'url' field"
        )
    token_env_name = entry.get("token_env") or "MO_NODE_TOKEN"
    token = src.get(token_env_name) or ""
    if not token:
        raise RuntimeError(
            f"node {chosen!r} requires {token_env_name} in the environment; "
            "tokens are never stored in nodes.yaml"
        )
    max_sessions = int(entry.get("max_sessions") or 1)
    return Node(name=chosen, url=url, token=token, max_sessions=max_sessions,
                token_env=token_env_name)