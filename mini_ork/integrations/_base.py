"""Transports: the seam between a chat surface and a thread.

A *transport* is one external conversation surface — Telegram today, and the
same shape is what Slack, Discord or a webhook catch-all would wear tomorrow.
What every one of them has in common is not its wire format but its *reduction*:
an inbound event names a conversation and carries text, and that text is a
message on a durable :mod:`mini_ork.threads` thread. Everything transport-shaped
stays in the transport module; everything thread-shaped — the state machine, the
transcript, the launch — is the one seam this package rides
(:func:`mini_ork.threads.service.deliver`).

Three things live here and nothing else:

:func:`thread_key`
    The mapping from a transport's own addressing — ``(transport, channel,
    thread_ts)`` — to the stable thread id the store and the canvas already
    understand. It is a pure function on purpose: the *identity* of a
    conversation is the one decision that must not be re-derived differently in
    a poll loop and a webhook handler, or a redelivered update would land on a
    second thread and the thread's whole reason for existing (carrying a
    multi-turn conversation) would be lost.

:class:`PollResult`
    What one poll tick did. The Telegram long-poll tier is a loop, so it needs a
    value that carries both the delivery it produced *and* the cursor to poll
    from next; a bare ``Delivery`` would make the caller re-derive the offset
    from the payload, which is exactly the coupling this package avoids.

:class:`Transport`
    The protocol those two imply, so a caller (a poll loop, a route, a test) can
    hold any transport without importing it. It is a ``Protocol`` rather than a
    base class because there is nothing to inherit: a Telegram transport is not
    a *kind* of transport, it is one.

Nothing here imports a transport. ``import mini_ork.integrations`` pulls this
module and the transports it re-exports; this module itself is transport-free
and side-effect free.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol, Sequence, runtime_checkable

from mini_ork.threads.model import ThreadMessage, ThreadOrigin
from mini_ork.threads.service import Delivery

#: Every chat transport opens :attr:`ThreadOrigin.CHAT` threads. The origin is
#: what the sidebar groups by, and every transport here is a chat surface — a
#: transport that wanted its own origin would be a work-intake (``INTAKE``) or
#: the canvas (``IDE``), neither of which is a chat.
TRANSPORT_ORIGIN = ThreadOrigin.CHAT

#: A thread id is an opaque key, but it is also a *filename-adjacent* string the
#: store writes into sqlite and the canvas puts in a URL. Anything outside this
#: set is replaced rather than escaped: a transport channel id is attacker-
#: influenced (a chat id is remote input), and refusing to carry a ``/`` or a
#: ``..`` into a key that reaches a path join is cheaper than auditing every
#: consumer for one.
_UNSAFE = re.compile(r"[^A-Za-z0-9_-]+")


def _slug(value: object) -> str:
    """``value`` as a key-safe token, or ``"x"`` when nothing survives.

    Never returns ``""``: an empty part would collapse two different channels
    onto the same key (``("", "")`` and ``("x", "")`` both becoming ``"-"``),
    which is a collision between two conversations — the one failure this
    helper exists to prevent.
    """
    return _UNSAFE.sub("_", str(value)).strip("_") or "x"


def thread_key(
    transport: str, channel: object, thread_ts: object | None = None
) -> str:
    """The stable thread id for one conversation on one transport.

    ``channel`` is the transport's own container — a Telegram chat id, a Slack
    channel id — and ``thread_ts`` is the sub-thread within it, or ``None`` for
    the top-level conversation. The pair is the identity: a Discord thread and a
    Telegram chat that happened to share an id are two conversations, and
    ``transport`` is what keeps them apart.

    ``thread_ts`` is *included only when it is meaningful*. A transport that
    reports the channel's own id as the thread timestamp (some do) would open a
    fresh thread per message under a naive join; the caller passes ``None``
    when there is no sub-thread, and the same conversation keeps its key.
    """
    key = f"{_slug(transport)}-{_slug(channel)}"
    if thread_ts is None or str(thread_ts).strip() == "":
        return key
    return f"{key}-{_slug(thread_ts)}"


@dataclass(frozen=True, slots=True)
class PollResult:
    """What one poll tick did with one update.

    ``action`` mirrors :attr:`mini_ork.threads.service.Delivery.action` —
    ``launched``, ``steered``, ``continued``, ``duplicate``, ``error`` — plus
    ``ignored`` for an update that carried no message to deliver (a join, a
    reaction, an edit this stage does not act on). ``ignored`` is a *deliberate*
    value rather than a dropped update: the cursor still has to advance past it,
    and a loop that only advanced on a delivery would re-read the same unacted
    update forever.

    ``update_id`` is the transport's own monotonic id for the update, and
    :attr:`next_offset` is the one thing a loop needs from it. ``delivery`` is
    the thread-side outcome, or ``None`` when this tick delivered nothing.

    One ``getUpdates`` answer is an *array*, so a tick can carry several updates
    across several chats. The three fields above are then the *representative*
    outcome — the last update that actually acted, and a cursor that clears the
    whole batch (:func:`merge_poll_results`) — and :attr:`batch` is the per-update
    truth they were derived from, in wire order. Without it a multi-update tick
    would report whichever update happened to land last and silently drop the
    rest from view, even though each one already had its side effect.
    """

    action: str = "ignored"
    update_id: int | None = None
    delivery: Delivery | None = None
    batch: tuple["PollResult", ...] = ()

    @property
    def next_offset(self) -> int | None:
        """The cursor to poll from next, or ``None`` when nothing was seen.

        ``None`` means "do not move the cursor", which is the correct answer for
        an empty poll: Telegram's ``offset`` is exclusive and its ``getUpdates``
        acknowledges everything below it, so advancing on an empty response
        would ack nothing and is at best a no-op.
        """
        return None if self.update_id is None else int(self.update_id) + 1


def merge_poll_results(results: Sequence[PollResult]) -> PollResult:
    """One representative :class:`PollResult` for a tick's whole batch.

    A poll loop threads a single cursor, so the batch has to collapse to one
    value — but the two fields that matter collapse by *different* rules:

    * the cursor must clear the **whole** batch. ``offset`` is exclusive and
      acknowledges everything below it, so it is one past the highest
      ``update_id`` seen — not one past the last element's. A batch whose
      replies arrive out of order would otherwise replay every update above
      the final element;
    * the outcome is the **last update that acted**, so a trailing *ignored*
      update (a join, an edit this stage does not act on) cannot erase the
      delivery that preceded it.

    Every per-update result is preserved on :attr:`PollResult.batch`, so
    nothing observed is dropped. An empty batch yields the default result —
    the "do not move the cursor" tick.
    """
    results = tuple(results)
    if not results:
        return PollResult()
    ids = [int(r.update_id) for r in results if r.update_id is not None]
    acted = [r for r in results if r.action != "ignored"] or list(results)
    last = acted[-1]
    return PollResult(
        action=last.action,
        update_id=max(ids) if ids else None,
        delivery=last.delivery,
        batch=results,
    )


@runtime_checkable
class Transport(Protocol):
    """The surface a transport module offers. See the module docstring.

    The methods take ``home``/``db`` per call rather than in the constructor
    because one transport instance is long-lived (a poll loop holds it for the
    life of the process) while the home a given run should write to is a
    property of *the request*, not of the transport. Passing the target per call
    is what lets one transport serve a test's ``tmp_path`` and a real home in
    the same process without a hidden global.
    """

    name: str

    def poll_once(
        self,
        *,
        offset: int | None = None,
        timeout: float | None = None,
        home: object | None = None,
        db: str | None = None,
    ) -> PollResult: ...

    def handle_webhook(
        self,
        body: bytes,
        header: str | None,
        *,
        home: object | None = None,
        db: str | None = None,
    ) -> Delivery | None: ...

    def send(
        self,
        thread_id: str,
        text: str,
        *,
        turn: int | None = None,
        event_id: str | None = None,
        home: object | None = None,
        db: str | None = None,
    ) -> ThreadMessage: ...


__all__ = [
    "PollResult",
    "TRANSPORT_ORIGIN",
    "Transport",
    "merge_poll_results",
    "thread_key",
]
