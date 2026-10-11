"""The PM source registry: one dict, one registration call, one catalogue.

Deliberately the same shape as ``mini_ork/features/registry.py`` — a module-level
dict filled by :func:`register_provider` at import time — because that pattern
has already paid for itself once in this codebase: adding a source is a new file
plus one line in ``mini_ork/pm/providers/__init__.py``, and every surface (the
``pm`` CLI, the ``issues`` IDE page, the catalogue the IDE reads) sees the same
list without anyone remembering to update a second one.

Name note: "provider" here means a *project-management source* — Linear, Jira,
mini-ork's own epics. ``config/providers.yaml`` in this repo is a different
thing (the LLM lane registry). Every symbol in this package is reachable only
as ``mini_ork.pm.*``, and the CLI surface is ``mini-ork pm``, so the collision
stays out of sight in both directions.
"""

from __future__ import annotations

import json
from typing import Any, Sequence

from mini_ork.pm.provider import Capabilities, Provider

_PROVIDERS: dict[str, Provider] = {}


class UnknownProvider(KeyError):
    """A provider id nothing registered. Carries the ids that would have worked."""

    def __init__(self, provider_id: str, known: Sequence[str]) -> None:
        listing = ", ".join(known) if known else "(none registered)"
        super().__init__(f"unknown pm provider {provider_id!r}; known: {listing}")
        self.provider_id = provider_id
        self.known = tuple(known)


def register_provider(provider: Provider, *, replace: bool = False) -> Provider:
    """Add one source to the registry. Returns it, so a module can register and
    export in one line.

    An empty id is refused rather than accepted: it would mint uids of the form
    ``:KEY``, which ``model.split_uid`` rejects, so the failure would surface
    far from its cause.
    """
    provider_id = getattr(provider, "id", "")
    if not provider_id:
        raise ValueError(f"{type(provider).__name__} has no id; refusing to register")
    if ":" in provider_id:
        raise ValueError(f"pm provider id may not contain ':': {provider_id!r}")
    if provider_id in _PROVIDERS and not replace:
        raise ValueError(
            f"pm provider {provider_id!r} is already registered "
            "(pass replace=True to override)"
        )
    _PROVIDERS[provider_id] = provider
    return provider


def unregister_provider(provider_id: str) -> None:
    """Drop a source. For tests, and for one whose configuration failed to load.

    Quiet when the id is not registered: the caller is removing a source that
    it wants gone, and it already is.
    """
    _PROVIDERS.pop(provider_id, None)


def get_provider(provider_id: str) -> Provider:
    """The registered source with this id. Raises :class:`UnknownProvider`."""
    try:
        return _PROVIDERS[provider_id]
    except KeyError:
        raise UnknownProvider(provider_id, provider_ids()) from None


def providers() -> tuple[Provider, ...]:
    """Every registered source, ordered by id so pages and menus are stable."""
    return tuple(_PROVIDERS[key] for key in sorted(_PROVIDERS))


def provider_ids() -> tuple[str, ...]:
    return tuple(sorted(_PROVIDERS))


def capabilities(provider_id: str) -> Capabilities:
    return get_provider(provider_id).capabilities


def catalogue() -> list[dict[str, Any]]:
    """The registry as JSON-safe data, for a page, a menu or a CLI listing."""
    return [
        {
            "id": provider.id,
            "label": provider.label,
            "read_only": provider.capabilities.read_only,
            "capabilities": provider.capabilities.as_dict(),
        }
        for provider in providers()
    ]


def catalogue_json() -> str:
    return json.dumps({"providers": catalogue()}, indent=2, sort_keys=True)
