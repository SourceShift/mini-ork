"""Static node provider: the operator-run node-agent you already have.

``provider: static`` (or ``MO_NODE_PROVIDER=static``) is the default and the
zero-infrastructure tier of the cloud-sandbox axis: a node-agent the operator
started on a VM they control (``config/nodes.yaml`` / ``MO_NODE_URL``). Nothing
is created and nothing is destroyed — ``create`` re-selects the configured node,
``release`` is a no-op, and ``reap`` returns ``[]`` (a static node has no TTL to
sweep).

The endpoint's ``handle`` is deliberately EMPTY. That is what tells
:func:`mini_ork.remote.providers.base.acquire_run_node` not to record the node:
a static node is re-selected by every process of a run rather than re-attached,
exactly the behavior the legacy (no-provider) path already had. The registry
entry exists so a profile that names ``provider: static`` (or a run with
``MO_NODE_PROVIDER=static``) flows through the same provider seam as a cloud
backend without a special case in the workspace.

Import-time contract: stdlib only, no I/O, no env read. Selection happens in
``create`` (with the run's scoped ``env``), never at import.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from mini_ork.remote.nodes import select_node
from mini_ork.remote.providers.base import (
    NodeEndpoint,
    ProviderUnavailableError,
    register_node_provider,
)

__all__ = ["StaticProvider"]

NAME = "static"


def _selected_node(profile: Any, env: Mapping[str, str]) -> Any:
    """Resolve the configured node with the run's scoped ``env``.

    Mirrors the resolver in :func:`mini_ork.runtime.backends.remote._factory`:
    a profile's ``node:`` name pins ``MO_NODE`` for this selection. A missing
    node config is loud (:class:`ProviderUnavailableError`) — a static run must
    never silently fall back to the host.
    """
    src = dict(env or {})
    node_name = str(getattr(profile, "node", "") or "").strip()
    if node_name:
        src["MO_NODE"] = node_name
    try:
        return select_node(env=src)
    except RuntimeError as exc:
        raise ProviderUnavailableError(f"static node not configured: {exc}") from exc


class StaticProvider:
    """The default provider: re-select the operator's node, never create one."""

    name = NAME

    def create(self, *, run_id: str, profile: Any, env: Mapping[str, str],
               engine_root: str) -> NodeEndpoint:
        node = _selected_node(profile, env)
        return NodeEndpoint(
            name=str(getattr(node, "name", "") or NAME),
            url=str(getattr(node, "url", "")),
            token=str(getattr(node, "token", "") or ""),
            token_env=str(getattr(node, "token_env", "") or "MO_NODE_TOKEN"),
            max_sessions=int(getattr(node, "max_sessions", 1) or 1),
            handle="",   # nothing was created; nothing may be destroyed
            provider=NAME,
        )

    def attach(self, handle: str, *, env: Mapping[str, str]) -> NodeEndpoint | None:
        """A static node has no handle to re-find — always ``None``."""
        return None

    def release(self, handle: str, *, env: Mapping[str, str]) -> None:
        """A static node is the operator's to keep; release does nothing."""
        return None

    def reap(self, *, max_age_s: int, dry_run: bool,
             env: Mapping[str, str]) -> list[str]:
        """No TTL and no labels — a static provider has nothing to sweep."""
        return []


register_node_provider(NAME, StaticProvider)
