"""Ticket write-back: the one gate every outbound mutation passes through.

A board that lets a person comment on a ticket or move it between columns is
promising the source will accept the write. Two things have to be true before
that promise is kept, and this module checks both, **in this order**:

1. **The operator opted the source in.** ``write_back: true`` in the source's
   ``config/pm.yaml`` stanza, read from :attr:`SourceConfig.settings` — the
   config layer already lands unknown keys there, so it needs no new field. A
   read-only credential is the ordinary case and a write needs a write-scoped
   one, which is a deliberate act rather than a default. The flag is read
   strictly: only a real YAML ``true`` opts in, so a typo or a stray string
   fails closed instead of quietly enabling writes.

2. **The adapter can do it.** The declared :class:`Capabilities` flag for the
   operation. That a method *exists* is not evidence it does anything — it can
   be a no-op — so this is the check ``hasattr`` cannot make. Reading the flag
   is exactly what ``Capabilities`` is for.

Either check failing raises :class:`Unsupported` naming the source, the
operation and which check failed, and **no adapter method is called**: a
refused write never reaches the network.

The three entry points mirror the adapter protocol — :func:`comment`,
:func:`transition`, :func:`update`. ``update`` takes a :class:`Patch`, never
``**kwargs``, so ``description=""`` (clear the field) stays distinguishable
from ``description=None`` (leave it alone).

Callers are out of scope in this slice: intake, and the post-verdict hooks that
post a run's result back to a ticket, come later. This module is the gated API
they will call.
"""

from __future__ import annotations

from mini_ork.pm import config as pm_config
from mini_ork.pm import registry
from mini_ork.pm.model import Comment, Issue, IssueState, split_uid
from mini_ork.pm.provider import Patch, Provider, Unsupported

#: The ``config/pm.yaml`` key that opts a source in to writes. Documented on the
#: linear and jira stanzas; absent means "no".
WRITE_BACK = "write_back"


def _opted_in(source_id: str) -> bool:
    """Whether ``config/pm.yaml`` opted ``source_id`` in to writes.

    A source the config never declares has no flag, so it is not opted in — the
    same answer an explicit ``false`` gives, and the safe one. Reads the live
    config each call rather than caching: an operator who flips the flag expects
    the next write to see it.
    """
    configuration = pm_config.load_config()
    try:
        source = configuration.source(source_id)
    except pm_config.PmConfigError:
        return False
    return source.settings.get(WRITE_BACK, False) is True


def _permitted(uid: str, operation: str) -> Provider:
    """The adapter allowed to perform ``operation`` on ``uid``, or a refusal.

    Resolves the source from the uid through the registry, then checks the two
    conditions in order — the ``write_back`` opt-in, then the adapter's declared
    capability — raising :class:`Unsupported` the moment one fails. The refused
    call touches neither the network nor the adapter.
    """
    provider_id, _key = split_uid(uid)
    provider = registry.get_provider(provider_id)
    if not _opted_in(provider_id):
        raise Unsupported(
            provider_id,
            f"{operation} (write_back is not enabled for this source)",
        )
    capabilities = provider.capabilities
    # An explicit table, not getattr/hasattr: the capability is a declared flag
    # to be read, never a method to be probed for existence.
    declared = {
        "comment": capabilities.comment,
        "transition": capabilities.transition,
        "update": capabilities.update,
    }[operation]
    if not declared:
        raise Unsupported(
            provider_id,
            f"{operation} (this source does not declare the capability)",
        )
    return provider


def comment(uid: str, body: str) -> Comment:
    """Post ``body`` as a comment on ``uid``, if the source allows it."""
    return _permitted(uid, "comment").comment(uid, body)


def transition(uid: str, state: IssueState) -> Issue:
    """Move ``uid`` to ``state``, returning the issue as it stands afterwards."""
    return _permitted(uid, "transition").transition(uid, state)


def update(uid: str, patch: Patch) -> Issue:
    """Apply ``patch`` to ``uid``, forwarding the :class:`Patch` verbatim."""
    return _permitted(uid, "update").update(uid, patch)
