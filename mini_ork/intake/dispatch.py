"""From a ticket to a run — the one place a ticket becomes work.

Every intake path funnels through :func:`dispatch_issue`, whether the ticket
arrived by poll (tier A) or by signed webhook (tier B). What it does is the
plan's ticket → thread → run table, and the point of writing it once is that
the two tiers cannot drift into two different notions of "handled".

Three questions, three mechanisms, and the rule that ties them together:

* *has a run ever worked this ticket?* — ``pm_links`` ``kind='run'``, written
  by :func:`mini_ork.threads.service.deliver` from its ``issue_uid`` argument,
  so the link and the run are created in one place.
* *is one working it right now?* — :func:`mini_ork.threads.store.live_run_for_issue`,
  the ``pm_links`` → ``thread_runs`` join.
* *have I already handled this event?* — the ``intake_events`` row this module
  writes, keyed ``(provider, delivery_id)``.

**The rule is not "never twice". It is "never two at once, and the second one
continues the same thread."** So a ticket whose run is live is *steered* into
that run, and a ticket whose run has finished *continues* the same thread —
which is why this is one feature with the thread work rather than a poller
bolted next to it.

The thread id is derived from the issue uid, so intake is idempotent even
against a home whose threads table was wiped: the same ticket lands on the same
thread id. The authoritative identity is still ``(origin='intake',
origin_ref=issue_uid)`` — the id is only what the first arrival happened to
name it.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from mini_ork.pm.model import Comment, Issue, Priority, split_uid
from mini_ork.pm.provider import Provider, ProviderError
from mini_ork.pm.store import IssueRow
from mini_ork.threads import service, store
from mini_ork.threads.model import ThreadOrigin

#: The description is carried verbatim, but not without a ceiling: an issue
#: body can be a pasted log file, and the kickoff's ``## Your task`` block is
#: what the composer refuses to truncate. Over the ceiling the tail is dropped
#: *and said so* in the ticket itself — never silently.
MAX_BODY_CHARS = 8000

#: How many of the most recent comments ride along. A ticket's discussion can
#: be long; what the agent needs is where the ticket stands now, and the tail
#: is where that is.
MAX_COMMENTS = 3
MAX_COMMENT_CHARS = 1000

_SLUG_SEPARATORS = re.compile(r"[^a-z0-9]+")

#: What :attr:`Dispatch.started` counts. Named here rather than imported so the
#: poller and the webhook agree on the two actions that begin a run.
STARTED_ACTIONS = frozenset({"launched", "continued"})


@dataclass(frozen=True, slots=True)
class Dispatch:
    """What one ticket's arrival did."""

    issue_uid: str
    delivery_id: str
    thread_id: str
    action: str
    run_id: str | None = None
    detail: str | None = None
    payload_hash: str = ""

    @property
    def started(self) -> bool:
        """Whether this arrival put a run on the machine."""
        return self.action in STARTED_ACTIONS

    def to_dict(self) -> dict[str, object]:
        return {
            "issue_uid": self.issue_uid,
            "delivery_id": self.delivery_id,
            "thread_id": self.thread_id,
            "action": self.action,
            "run_id": self.run_id,
            "detail": self.detail,
            "started": self.started,
        }


def thread_id_for(issue_uid: str) -> str:
    """``github:acme/app#12`` → ``github-acme-app-12``.

    Deterministic, so a re-derivation after a wipe lands on the same thread, and
    readable, so a ``mini-ork threads list`` is scannable. It is *not* an
    identity: two uids whose non-alphanumerics differ the same way collide
    (``acme/app#12`` and ``acme-app#12``), and the lookup that decides whether a
    ticket already has a thread is ``(origin, origin_ref)``, which cannot.
    """
    provider, key = split_uid(issue_uid)
    slug = _SLUG_SEPARATORS.sub("-", key.lower()).strip("-")
    return f"{provider}-{slug}" if slug else provider


def delivery_id_for(issue: Issue | IssueRow) -> str:
    """What makes one arrival distinct from the next for the same ticket.

    ``uid@updated_at`` is the poller's spelling: an edit bumps ``updated_at``
    (GitHub, Linear) or the equivalent fingerprint, so "the ticket changed" and
    "this is a new arrival" are the same event. The webhook tier passes the
    provider's own delivery id instead — it has a better one.
    """
    return f"{issue.uid}@{issue.updated_at or ''}"


def payload_hash(text: str) -> str:
    """A fingerprint of what was delivered, for the ledger."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + f"… (+{len(text) - limit} characters)"


def _facts(issue: Issue) -> str:
    """The provenance line — what a person needs to find the ticket again."""
    state = str(issue.state)
    if issue.native_state and str(issue.native_state) != state:
        # The source's own word, kept because the normalized one is lossy: an
        # adapter that maps several native states onto one normalized state
        # would otherwise hide which of them this is.
        state = f"{state} ({issue.native_state})"
    parts = [
        f"State: {state}",
        f"Priority: {Priority(issue.priority).name.lower()}",
        f"Assignee: {issue.assignee or 'nobody'}",
    ]
    if issue.labels:
        parts.append("Labels: " + ", ".join(str(label) for label in issue.labels))
    line = " · ".join(parts)
    url = issue.identity.url
    return f"{line}\n{url}" if url else line


def ticket_markdown(
    issue: Issue,
    comments: Sequence[Comment] = (),
    *,
    comments_error: str | None = None,
) -> str:
    """The ticket as the agent reads it: provenance, body, latest discussion.

    Deliberately *not* a summary. The body is the ticket text verbatim up to
    :data:`MAX_BODY_CHARS`, because an agent asked to implement a ticket needs
    the ticket and not a paraphrase — and because a deterministic renderer can
    be unit-tested, where a summarizer would have to be inspected.

    ``comments_error`` exists so a source that cannot list comments — ``local``
    raises :class:`~mini_ork.pm.provider.Unsupported`, a network one can fail —
    says so *in the ticket* rather than quietly delivering a shorter one.
    """
    lines = [f"**{issue.uid}** — {issue.title}", _facts(issue), ""]
    body = (issue.description or "").strip()
    if not body:
        lines.append("_(no description)_")
    elif len(body) > MAX_BODY_CHARS:
        lines.append(body[:MAX_BODY_CHARS])
        lines.append("")
        lines.append(
            f"_(description truncated: {len(body) - MAX_BODY_CHARS} characters not shown)_"
        )
    else:
        lines.append(body)

    if comments_error is not None:
        lines += ["", f"_(comments unavailable: {comments_error})_"]
    elif comments:
        lines += ["", "### Latest comments"]
        for comment in comments:
            author = comment.author or "someone"
            when = f" ({comment.created_at})" if comment.created_at else ""
            lines.append(f"- **{author}**{when}: {_clip(comment.body.strip(), MAX_COMMENT_CHARS)}")
    return "\n".join(lines).rstrip() + "\n"


def _comments(provider: Provider, uid: str) -> tuple[tuple[Comment, ...], str | None]:
    """The tail of the discussion, or the reason there is none.

    Best-effort by design — a source that cannot list comments does not make
    the ticket undispatchable, and the ticket says which happened.
    """
    try:
        found = tuple(provider.comments(uid))
    except (ProviderError, KeyError) as exc:
        return (), str(exc)
    return found[-MAX_COMMENTS:], None


def dispatch_issue(
    home: Path | str,
    provider: Provider,
    issue: Issue,
    *,
    recipe: str | None = None,
    delivery_id: str | None = None,
    source: str = "intake",
    db: str | None = None,
) -> Dispatch:
    """Start, steer or continue the run that works ``issue``.

    The three branches are the plan's table:

    * a live run → ``deliver`` steers it, and no second run exists at any point;
    * a finished run → ``deliver`` continues *the same thread*, attempt N,
      with the ticket re-stated in full so the new attempt is self-contained;
    * neither → the deterministic id, and ``deliver`` launches attempt 1.

    Choosing the thread id is the only work this function does that ``deliver``
    does not: ``deliver`` will happily open a thread under whatever id it is
    handed, so the decision "does this ticket already have one" has to be made
    here, from the ledger and the origin ref rather than from the id.

    The ledger row is written here rather than by the caller because here is
    where both halves are known — the delivery id that identifies the arrival
    and the action it produced. A second call for the same arrival folds into
    the first row (:data:`~mini_ork.threads.store.record_intake_event` is
    ``INSERT OR IGNORE``), which is belt-and-braces: the poller folds earlier
    still, before it spends a network call on the ticket.
    """
    delivery_id = delivery_id or delivery_id_for(issue)
    live = store.live_run_for_issue(issue.uid, db=db)
    if live is not None:
        thread_id = live.thread_id
    else:
        existing = store.find_thread(ThreadOrigin.INTAKE, issue.uid, db=db)
        thread_id = existing.id if existing is not None else thread_id_for(issue.uid)

    comments, comments_error = _comments(provider, issue.uid)
    text = ticket_markdown(issue, comments, comments_error=comments_error)

    delivery = service.deliver(
        home,
        thread_id,
        text,
        title=issue.title,
        origin=ThreadOrigin.INTAKE,
        origin_ref=issue.uid,
        recipe=recipe,
        # No `ticket=` block: the ticket *is* the message, so a continuation
        # carries it in full. The composer truncates only the history block,
        # never the task, and a continuation's task must be the whole ticket.
        external_id=delivery_id,
        issue_uid=issue.uid,
        source=source,
        db=db,
    )

    store.record_intake_event(
        provider.id,
        delivery_id,
        payload_hash(text),
        issue_uid=issue.uid,
        thread_id=thread_id,
        action=delivery.action,
        detail=delivery.detail,
        # The ledger row is written here because here is where both halves are
        # known — ``delivery`` is what started or steered the run, so this is
        # the one place the arrival and its run meet.
        run_id=delivery.run_id,
        db=db,
    )
    return Dispatch(
        issue_uid=issue.uid,
        delivery_id=delivery_id,
        thread_id=thread_id,
        action=delivery.action,
        run_id=delivery.run_id,
        detail=delivery.detail,
        payload_hash=payload_hash(text),
    )
