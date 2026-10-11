"""The thread model: a durable, multi-turn unit of work.

A thread is what today's one-shot conversation shim cannot express. The
agent-server sidecar holds one DAG and answers every follow-up with a 409
(``start a new conversation``); a thread carries a transcript, a list of runs,
and a state that survives a run ending — so "follow up in the same thread" is a
turn on an existing row rather than a new conversation.

Three vocabularies meet here, and the module picks a side on each so nothing
downstream has to guess:

* :class:`ThreadState` is the OpenHands ``execution_status`` vocabulary exactly
  — ``idle``, ``running``, ``finished``, ``error``. The shim projects a thread's
  status onto that field by copy, not by mapping, so the canvas keeps seeing the
  words it already handles (``finished`` → ``running`` is a transition it draws
  when a real agent loops).

* :class:`ThreadOrigin` is a closed set of channels — the axis a UI groups a
  sidebar by. The provider's own key (``github:o/r#12``) is unbounded and lives
  in ``origin_ref`` beside it; the two are deliberately split, and neither is
  unique, because a ticket can legitimately yield a second thread later. Whether
  a run is *already working* a ticket is a liveness question answered by
  :class:`ThreadRun`, not a uniqueness constraint on the thread.

* ``turn`` numbers a *round*: the user message that opens it and every assistant
  message its run produces carry the same turn, ordered by row id. It is also the
  attempt number — a new turn is a new launch — which is why the attempt-2 run id
  is ``f"{thread_id}-t{turn}"`` and the allocator is ``threads.last_turn``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ThreadState(StrEnum):
    """Where a thread stands, in the OpenHands ``execution_status`` vocabulary.

    ``IDLE`` is a thread that has not launched yet — the state a just-opened
    ticket or a created-but-empty conversation is in. It is a cache of the
    latest :class:`ThreadRun`: :func:`mini_ork.threads.store.recompute_status`
    can rebuild it from ``thread_runs``, so a stale value is repairable.
    """

    IDLE = "idle"
    RUNNING = "running"
    FINISHED = "finished"
    ERROR = "error"


class ThreadOrigin(StrEnum):
    """Which surface opened the thread. A closed set — the sidebar groups by it."""

    IDE = "ide"        # the canvas: create_conversation
    CHAT = "chat"      # Slack / Telegram / Discord
    INTAKE = "intake"  # a work-intake ticket
    CLI = "cli"        # `mini-ork threads send …`


class RunStatus(StrEnum):
    """The lifecycle of one launch. Terminal values match ``ThreadState``."""

    RUNNING = "running"
    FINISHED = "finished"
    ERROR = "error"


class MessageRole(StrEnum):
    """Who wrote a message. ``FILE`` is an attachment (Stage 2) — a stored path
    rides in ``attachments`` and needs no prose of its own."""

    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
    FILE = "file"


@dataclass(frozen=True, slots=True)
class Thread:
    """One conversation. ``id`` is the key the SPA, the websocket and
    ``MINI_ORK_RUN_ID`` all already agree on — there is no translation table."""

    id: str
    title: str
    status: ThreadState
    origin: ThreadOrigin
    origin_ref: str | None
    recipe: str | None
    last_turn: int
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class ThreadMessage:
    """One row of the transcript.

    ``event_id`` is set when the row is a projection of a ``run_events`` row —
    the unique index on ``(thread_id, event_id)`` makes re-projecting it a
    no-op. ``external_id`` is set when a remote surface (Slack, Telegram)
    supplied the message, so a redelivery does not become a second turn.
    """

    id: int
    thread_id: str
    turn: int
    role: MessageRole
    text: str
    event_id: str | None
    external_id: str | None
    attachments: tuple[str, ...]
    created_at: str


@dataclass(frozen=True, slots=True)
class ThreadRun:
    """One launch within a thread. ``attempt`` 1 is the initial run; N>1 is a
    continuation in the same thread — the thing that replaced the 409."""

    id: int
    thread_id: str
    run_id: str
    attempt: int
    status: RunStatus
    recipe: str | None
    kickoff_path: str | None
    started_at: str
    ended_at: str | None


@dataclass(frozen=True, slots=True)
class IntakeEvent:
    """One thing a work-intake trigger acted on. The ledger that makes a
    webhook replay or a re-poll a no-op."""

    id: int
    provider: str
    delivery_id: str
    payload_hash: str
    issue_uid: str | None
    thread_id: str | None
    action: str | None
    detail: str | None
    created_at: str
