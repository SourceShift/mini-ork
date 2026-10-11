"""What a project-management source implements, and how it says what it cannot do.

:class:`Capabilities` is the reason this module exists rather than a bare
``Protocol``. mini-ork's own epics are the motivating case: their status moves
through ``scheduler._settle``, and the DB trigger ``trg_epics_no_revert_done``
refuses a done epic going back to not-done — so a board that let you drag a
card out of Done would be lying about a write it cannot make. The local adapter
declares ``transition=False``, and the surface that would have offered the drag
never draws it. ``hasattr`` cannot answer that question: the method could exist
and still be a no-op, and a capability check that rots silently is worse than
none.

Every write operation takes an explicit :class:`Draft` or :class:`Patch`. There
is no ``update(uid, **kwargs)``, because "field absent" and "field set to
empty" have to be distinguishable: clearing a description and leaving it alone
are different intentions, and a kwargs API cannot tell them apart.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Sequence, runtime_checkable

from mini_ork.pm.model import (
    Comment,
    Issue,
    IssueState,
    Link,
    LinkKind,
    Priority,
)


class ProviderError(RuntimeError):
    """A source failed: the network, the credential, the payload, the config.

    The message is shown to a person, so it names the source and the operation
    and never the secret behind them.
    """


class Unsupported(ProviderError):
    """The source is reachable but does not do this — see its ``Capabilities``.

    Raised instead of returning a plausible-looking empty result, so a caller
    that skipped the capability check fails loudly at the first call rather
    than showing a board full of nothing.
    """

    def __init__(self, provider: str, operation: str) -> None:
        super().__init__(
            f"{provider} does not support {operation}; check its Capabilities first"
        )
        self.provider = provider
        self.operation = operation


@dataclass(frozen=True, slots=True)
class Capabilities:
    """What one source can do. Read it before offering an action.

    ``link`` covers the :class:`LinkKind` values the source can represent, not
    all of them: mini-ork has blocked-by edges and nothing else, so it raises
    :class:`Unsupported` for ``duplicates`` while ``link`` is still true. A
    source that can do none of them declares false.
    """

    create: bool = False
    update: bool = False
    transition: bool = False
    comment: bool = False
    link: bool = False
    search: bool = False
    states: bool = True
    priorities: bool = True
    labels: bool = False
    assignees: bool = False
    pagination: bool = False

    @property
    def read_only(self) -> bool:
        return not (self.create or self.update or self.transition or self.comment or self.link)

    @property
    def writes(self) -> tuple[str, ...]:
        """The write operations this source claims, in a stable order."""
        return tuple(
            name
            for name in ("create", "update", "transition", "comment", "link")
            if getattr(self, name)
        )

    def as_dict(self) -> dict[str, bool]:
        return {
            name: bool(getattr(self, name))
            for name in (
                "create",
                "update",
                "transition",
                "comment",
                "link",
                "search",
                "states",
                "priorities",
                "labels",
                "assignees",
                "pagination",
            )
        }


@dataclass(frozen=True, slots=True)
class Query:
    """A read request. Empty tuples and ``None`` mean "do not filter"."""

    text: str = ""
    states: tuple[IssueState, ...] = ()
    priorities: tuple[Priority, ...] = ()
    labels: tuple[str, ...] = ()
    assignee: str | None = None
    project: str | None = None
    limit: int = 50
    cursor: str | None = None


@dataclass(frozen=True, slots=True)
class Draft:
    """A new issue. ``state`` is a request, not a promise — a source may open
    everything in its own first column and say so through the returned Issue."""

    title: str
    description: str = ""
    state: IssueState = IssueState.TODO
    priority: Priority = Priority.NONE
    labels: tuple[str, ...] = ()
    assignee: str | None = None
    project: str | None = None
    parent_uid: str | None = None


@dataclass(frozen=True, slots=True)
class Patch:
    """A change to an existing issue. ``None`` means "leave it as it is".

    An empty string is a real value: it clears the field. That distinction is
    why this is a dataclass rather than keyword arguments.
    """

    title: str | None = None
    description: str | None = None
    priority: Priority | None = None
    labels: tuple[str, ...] | None = None
    assignee: str | None = None
    project: str | None = None
    parent_uid: str | None = None


@dataclass(frozen=True, slots=True)
class Page:
    """One page of issues. ``cursor`` is opaque — hand it back as
    :attr:`Query.cursor` for the next page, and do not parse it."""

    issues: tuple[Issue, ...] = ()
    has_more: bool = False
    cursor: str | None = None
    total: int | None = None


@runtime_checkable
class Provider(Protocol):
    """A project-management source. Implementations live in ``pm/providers/``.

    Read methods are required and must work; write methods are required and
    must raise :class:`Unsupported` when the matching capability is false —
    never silently succeed. ``comments`` follows the same rule.
    """

    #: Registry id: a lowercase slug, no colon (see ``model.make_uid``).
    id: str
    #: What a person calls this source ("Linear", "mini-ork epics").
    label: str
    capabilities: Capabilities

    def list_issues(self, query: Query) -> Page:
        """Issues matching ``query``, most useful first. Never raises for an
        empty result; raises :class:`ProviderError` when the source fails."""
        ...

    def get(self, uid: str) -> Issue:
        """One issue by uid. Raises ``KeyError`` when it does not exist."""
        ...

    def comments(self, uid: str) -> Sequence[Comment]:
        """Notes on an issue, oldest first."""
        ...

    def create(self, draft: Draft) -> Issue:
        ...

    def update(self, uid: str, patch: Patch) -> Issue:
        """Apply ``patch`` and return the issue as it stands afterwards."""
        ...

    def transition(self, uid: str, state: IssueState) -> Issue:
        """Move the issue to a normalized state. The source may resolve the
        request to a different one of its own statuses; the returned Issue says
        which actually happened."""
        ...

    def comment(self, uid: str, body: str) -> Comment:
        ...

    def link(self, from_uid: str, to_uid: str, kind: LinkKind) -> Link:
        ...
