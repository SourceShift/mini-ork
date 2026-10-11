"""The provider-neutral project-management model.

Everything mini-ork knows about a piece of work — its own ``epics`` rows, a
Linear issue, a Jira ticket — is expressed as an :class:`Issue` here.
Adapters bend onto this shape; the shape does not bend to an adapter.

Two places where the module deliberately picks a side, because leaving them
ambiguous is what makes a cross-tool board lie:

* :class:`IssueState` is the union of the states the sources can be in, and it
  is closed — seven values, no more. Normalizing must never lose the source's
  own word, so every :class:`Issue` carries ``native_state`` beside it: a card
  that reads "In review" still has Jira's ``In Review`` behind it when a
  mapping turns out to be wrong. A state that maps to nothing lands in
  :attr:`IssueState.BACKLOG`, the one state that claims nothing about the work.

* :class:`Priority` counts UP — ``URGENT`` is the highest number. mini-ork's
  ``epics.priority`` already reads that way (``scheduler.pick_ready`` orders by
  effective priority ``DESC``), while Linear numbers DOWN from 1 (urgent) to 4
  (low). Two opposite orderings under one word, so the inversion is written
  down once, in :meth:`Priority.from_linear`, instead of living in the head of
  every caller that renders a priority next to one from the other system.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum, StrEnum
from typing import Any, Mapping


class IssueState(StrEnum):
    """Where a piece of work stands, in every source's vocabulary but one.

    ``BACKLOG`` is the least committal value: not yet promised to anyone. It is
    also where a native status the adapters do not recognize lands, so a new
    status in a remote tool degrades to "unplanned" rather than crashing a page
    or silently disappearing.
    """

    BACKLOG = "backlog"
    TODO = "todo"
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"
    IN_REVIEW = "in_review"
    DONE = "done"
    CANCELED = "canceled"


#: States that mean "still needs someone". The board groups by these.
OPEN_STATES: frozenset[IssueState] = frozenset(
    {
        IssueState.BACKLOG,
        IssueState.TODO,
        IssueState.IN_PROGRESS,
        IssueState.BLOCKED,
        IssueState.IN_REVIEW,
    }
)

#: States that mean "out of play".
CLOSED_STATES: frozenset[IssueState] = frozenset({IssueState.DONE, IssueState.CANCELED})


class Priority(IntEnum):
    """Urgency, higher is more urgent. Not any one tool's numbering."""

    NONE = 0
    LOW = 1
    MEDIUM = 2
    HIGH = 3
    URGENT = 4

    @classmethod
    def from_linear(cls, value: int | None) -> Priority:
        """Linear numbers urgency downward: 1 urgent, 2 high, 3 medium, 4 low."""
        return {
            0: cls.NONE,
            1: cls.URGENT,
            2: cls.HIGH,
            3: cls.MEDIUM,
            4: cls.LOW,
        }.get(int(value or 0), cls.NONE)

    @classmethod
    def from_mini_ork(cls, value: int | None) -> Priority:
        """mini-ork's ``epics.priority`` counts upward and is unbounded above.

        The scheduler treats anything above the 0–3 range as "as soon as
        possible", so it clamps here rather than inventing tiers.
        """
        number = int(value or 0)
        if number <= 0:
            return cls.NONE
        if number >= int(cls.URGENT):
            return cls.URGENT
        return cls(number)


def make_uid(provider: str, key: str) -> str:
    """``provider:key`` — the one string that identifies an issue anywhere.

    The provider id may not contain a colon (registry ids are slugs), so
    splitting on the FIRST colon is unambiguous even when the native key has
    one — Linear's ``ENG-123`` does not, but a Jira key or a URL might.
    """
    return f"{provider}:{key}"


def split_uid(uid: str) -> tuple[str, str]:
    """``provider:key`` → ``(provider, key)``. Raises on a uid with no provider."""
    provider, _, key = uid.partition(":")
    if not provider or not key:
        raise ValueError(f"not a provider uid: {uid!r} (want '<provider>:<key>')")
    return provider, key


@dataclass(frozen=True, slots=True)
class ProviderIdentity:
    """Which source an issue lives in, and what that source calls it.

    Identity only — no operations. Every operation takes the identity back to
    the adapter that minted it, so nothing has to guess which source a bare
    ``ENG-123`` belongs to.
    """

    provider: str
    key: str
    project: str | None = None
    url: str | None = None

    @property
    def uid(self) -> str:
        return make_uid(self.provider, self.key)


@dataclass(frozen=True, slots=True)
class Issue:
    """One piece of work, as the sources can agree on it.

    ``native_state`` and ``native_priority`` are not debug fields: they are what
    the detail view shows when the normalized value is not enough, and what a
    write-back needs in order to leave the remote side exactly as it was.

    ``native`` is the source's own payload for the fields this model does not
    carry (mini-ork's ``recipe``, ``worker_default``, ``attempts``; Jira's ADF
    description). Anything the UI needs and this model does not name is still
    reachable through it, which is why nothing here is lossy.
    """

    identity: ProviderIdentity
    title: str
    state: IssueState
    native_state: str
    description: str = ""
    priority: Priority = Priority.NONE
    native_priority: int | None = None
    labels: tuple[str, ...] = ()
    assignee: str | None = None
    parent_uid: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    blocked_reason: str | None = None
    native: Mapping[str, Any] = field(default_factory=dict)

    @property
    def uid(self) -> str:
        return self.identity.uid

    @property
    def is_open(self) -> bool:
        return self.state in OPEN_STATES


@dataclass(frozen=True, slots=True)
class Comment:
    """One note on an issue. ``author`` is a display name, never an id we act on."""

    issue_uid: str
    author: str
    body: str
    created_at: str | None = None
    url: str | None = None
    native_id: str | None = None


class LinkKind(StrEnum):
    """How two pieces of work relate.

    ``BLOCKS`` is the only one mini-ork can express natively (it is
    ``epic_dependencies`` with kind ``hard``); the adapters raise
    :class:`mini_ork.pm.provider.Unsupported` for a kind their source cannot
    represent rather than silently downgrading it to a note.
    """

    BLOCKS = "blocks"
    RELATES = "relates"
    DUPLICATES = "duplicates"
    PARENT = "parent"
    #: A mini-ork run working on an issue. ``to_uid`` is ``run:<run id>``.
    RUN = "run"


@dataclass(frozen=True, slots=True)
class Link:
    """A directed edge between two uids, either of which may be a run."""

    from_uid: str
    to_uid: str
    kind: LinkKind = LinkKind.RELATES
    created_at: str | None = None
