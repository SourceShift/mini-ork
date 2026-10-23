"""A run's Messages tab: the concord mail of ``run:<id>``, both directions.

Concord keeps messages in ContextNest's ``coord.db``, never in ``state.db``. A
run registers as the principal ``run:<sanitized id>`` when it starts
(``orchestration/concord_run.py``) and its workers send as that principal, so
the principal's inbox is what the run received and the messages sent *from* it
are what the run sent. A message between two ``run:`` principals is the only
run-to-run channel mini-ork has.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from mini_ork import cn_client
from mini_ork.ide_pages import spec as S
from mini_ork.orchestration.concord_run import _sanitize_run_id

RECEIVED_TITLE = "Messages to this run"
SENT_TITLE = "Messages this run sent"

# A live run's page re-fetches every five seconds: a hung ContextNest must cost
# each refresh half a second, not the coord helpers' 3 s default.
_READ_TIMEOUT_SEC = 0.5
_SENT_LIMIT = 200


@dataclass
class Mailbox:
    principal: str
    # ok | off | down | unregistered | refused | config | unsupported | error
    state: str
    messages: list[dict[str, Any]] = field(default_factory=list)
    error: str = ""


def principal_for(run_id: str) -> str:
    return f"run:{_sanitize_run_id(run_id)}"


def _read(principal: str, fetch, *, not_found: str) -> Mailbox:
    from mini_ork.dispatch.secrets import SecretStoreError

    if os.environ.get("MO_DISABLE_CN", "0") == "1":
        return Mailbox(principal, "off")
    try:
        # The ping is cached (CN_PING_TTL), so a stopped ContextNest costs a
        # file read per refresh rather than a connection attempt.
        if not cn_client.available():
            return Mailbox(principal, "down")
        data = fetch()
    except (SecretStoreError, cn_client.CoordConfigError) as exc:
        return Mailbox(principal, "config", error=str(exc))
    except cn_client.CoordUnavailable as exc:
        return Mailbox(principal, "down", error=str(exc))
    except cn_client.CoordHTTPError as exc:
        if exc.status == 404:
            return Mailbox(principal, not_found)
        return Mailbox(principal, "refused", error=f"HTTP {exc.status} {exc.body}".strip())
    rows = data.get("messages") if isinstance(data, dict) else None
    return Mailbox(principal, "ok", [m for m in (rows or []) if isinstance(m, dict)])


def read_mailbox(run_id: str) -> Mailbox:
    """What the run received. A 404 is an unknown principal: the run never registered."""
    principal = principal_for(run_id)
    return _read(principal, lambda: cn_client.coord_inbox(
        principal, unacked=False, timeout=_READ_TIMEOUT_SEC), not_found="unregistered")


def read_sent(run_id: str) -> Mailbox:
    """What the run sent. Listing by sender needs no registered principal, so a
    404 here means the route itself is missing: ContextNest predates it."""
    principal = principal_for(run_id)
    return _read(principal, lambda: cn_client.coord_outbox(
        principal, _SENT_LIMIT, timeout=_READ_TIMEOUT_SEC), not_found="unsupported")


def badge(mailbox: Mailbox) -> tuple[int, str] | None:
    """``(count, tone)`` for the tab bar: messages received, yellow while any is unacked.

    An unread mailbox has no badge at all — a zero would claim "no messages"
    when the truth is "could not look".
    """
    if mailbox.state != "ok":
        return None
    unacked = any(not m.get("acked_at") for m in mailbox.messages)
    return len(mailbox.messages), "yellow" if unacked else "sub"


def _parse(stamp: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(stamp or "").replace("Z", "+00:00")).astimezone()
    except ValueError:
        return None


def _when(stamp: Any) -> str:
    moment = _parse(stamp)
    if moment is None:
        return ""
    if moment.date() == datetime.now().astimezone().date():
        return moment.strftime("%H:%M:%S")
    return moment.strftime("%b %d %H:%M")


def _principal_run(principal: str) -> str | None:
    """The run id behind a ``run:`` principal, for an Open run button.

    The sanitiser only rewrites characters a run id never carries, so the
    principal name is the run id itself.
    """
    if not principal.startswith("run:"):
        return None
    return principal[len("run:"):] or None


def _delivery(message: dict[str, Any]) -> tuple[str, str, str]:
    """``(mark, colour, words)`` for where a message stands."""
    if message.get("acked_at"):
        return "✓", "green", f"acked by {message.get('acked_by') or 'someone'}"
    if message.get("delivered_at"):
        # Delivery is the ContextNest turn hook handing the message to one of
        # the recipient's agent sessions; only an ack says someone acted on it.
        to = str(message.get("delivered_to") or "an agent")[:12]
        return "✉", "blue", f"delivered to {to} · not acked"
    return "✉", "yellow", "not yet delivered to an agent"


def _sorted(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(messages, key=lambda m: str(m.get("created_at") or ""), reverse=True)


def _received_row(message: dict[str, Any], principal: str) -> dict[str, Any]:
    sender = str(message.get("from") or "unknown sender")
    msg_id = str(message.get("msg_id") or "")
    mark, colour, state = _delivery(message)
    acts: list[dict[str, Any]] = []
    if msg_id and not message.get("acked_at"):
        acts.append(S.btn("Ack", S.cli("concord", "ack", principal, msg_id, home=False), "ghost"))
    other_run = _principal_run(sender)
    if other_run and sender != principal:
        acts.append(S.btn("Open run", S.open_run(other_run), "ghost"))
    if sender != principal:
        acts.append(S.btn("Reply", S.thread(f"/concord send {sender} "), "ghost"))
    return S.item(f"{sender} · {state}", str(message.get("body") or ""), m=mark, mc=colour,
                  is_mono=True, when=_when(message.get("created_at")), acts=acts)


def _sent_row(message: dict[str, Any], principal: str) -> dict[str, Any]:
    recipient = str(message.get("principal_id") or "unknown recipient")
    mark, colour, state = _delivery(message)
    acts: list[dict[str, Any]] = []
    other_run = _principal_run(recipient)
    if other_run and recipient != principal:
        acts.append(S.btn("Open run", S.open_run(other_run), "ghost"))
    return S.item(f"→ {recipient} · {state}", str(message.get("body") or ""), m=mark,
                  mc=colour, is_mono=True, when=_when(message.get("created_at")), acts=acts)


def _unavailable(mailbox: Mailbox) -> dict[str, Any]:
    if mailbox.state == "off":
        return S.dot("ContextNest is off",
                     "MO_DISABLE_CN=1 — concord messages live in ContextNest")
    if mailbox.state == "down":
        return S.warn("ContextNest is not reachable",
                      "Concord messages live in ContextNest (CN_BASE_URL); start it to "
                      "read this run's messages")
    if mailbox.state == "unregistered":
        return S.dot(f"This run has no concord mailbox ({mailbox.principal})",
                     "It never registered with concord: it ran with MO_CONCORD=0, before "
                     "concord existed, or while ContextNest was down")
    if mailbox.state == "unsupported":
        return S.dot("This ContextNest cannot list sent messages",
                     "It predates GET /api/v1/coord/messages; restart it on a current "
                     "build to see what this run sent")
    if mailbox.state == "config":
        return S.bad("ContextNest is misconfigured", mailbox.error)
    if mailbox.state == "error":
        return S.bad("Could not read this run's messages", mailbox.error)
    return S.bad("ContextNest refused the read", mailbox.error)


def _received_section(mailbox: Mailbox) -> dict[str, Any]:
    if mailbox.state != "ok":
        return S.lst(RECEIVED_TITLE, [_unavailable(mailbox)], full=True)
    messages = _sorted(mailbox.messages)
    unacked = sum(1 for m in messages if not m.get("acked_at"))
    from_runs = sum(1 for m in messages
                    if _principal_run(str(m.get("from") or "")) and m.get("from") != mailbox.principal)
    note = (f"Sent to {mailbox.principal}, newest first · {len(messages)} total · "
            f"{unacked} not acked · {from_runs} from other runs.")
    items = [_received_row(m, mailbox.principal) for m in messages] or [
        S.dot("No concord messages were sent to this run")]
    return S.lst(RECEIVED_TITLE, items, full=True, note=note,
                 actions=[S.btn("Send message", S.thread(f"/concord send {mailbox.principal} "))])


def _sent_section(sent: Mailbox) -> dict[str, Any]:
    if sent.state != "ok":
        return S.lst(SENT_TITLE, [_unavailable(sent)], full=True)
    messages = _sorted(sent.messages)
    to_runs = sum(1 for m in messages
                  if _principal_run(str(m.get("principal_id") or ""))
                  and m.get("principal_id") != sent.principal)
    note = (f"Sent as {sent.principal}, newest first · {len(messages)} total · "
            f"{to_runs} to other runs.")
    if len(messages) >= _SENT_LIMIT:
        note += f" Only the latest {_SENT_LIMIT} are listed."
    items = [_sent_row(m, sent.principal) for m in messages] or [
        S.dot("This run sent no concord messages")]
    return S.lst(SENT_TITLE, items, full=True, note=note)


def messages_tab(run_id: str, mailbox: Mailbox | None = None) -> list[dict[str, Any]]:
    """Both directions. ``mailbox`` is the inbox the caller already read for the
    tab badge, so the tab does not ask ContextNest for it twice."""
    if mailbox is None:
        mailbox = read_mailbox(run_id)
    received = _received_section(mailbox)
    # Off or unreachable says everything already; a second identical line
    # under "sent" would only repeat it.
    if mailbox.state in ("off", "down", "config"):
        return [received]
    return [received, _sent_section(read_sent(run_id))]
