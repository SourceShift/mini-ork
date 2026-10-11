"""Threads: a durable, multi-turn unit of work.

The primitive the three external surfaces — work-intake, chat transports and
file attachments — all reduce to: *something opens a unit of work and can carry
follow-up messages about it*. A ticket opens a thread; a Slack message opens a
thread; an attachment hangs off one. Built once here, they are three adapters
rather than three incompatible notions of "a conversation".

Public surface:

* :mod:`mini_ork.threads.model` — the vocabulary (Thread, ThreadRun, …)
* :mod:`mini_ork.threads.store` — persistence (open_thread, append_message, …)
* :mod:`mini_ork.threads.kickoff` — pure markdown composition for the next turn
* :mod:`mini_ork.threads.service` — the state machine that launches, steers or
  continues a run

Importing this package pulls only the model and the store, both of which are
plain ``sqlite3``; :mod:`service` is what reaches ``web.control`` and is
imported by its callers directly.
"""

from __future__ import annotations

from mini_ork.threads.model import (
    IntakeEvent,
    MessageRole,
    RunStatus,
    Thread,
    ThreadMessage,
    ThreadOrigin,
    ThreadRun,
    ThreadState,
)
from mini_ork.threads import store

__all__ = [
    "IntakeEvent",
    "MessageRole",
    "RunStatus",
    "Thread",
    "ThreadMessage",
    "ThreadOrigin",
    "ThreadRun",
    "ThreadState",
    "store",
]
