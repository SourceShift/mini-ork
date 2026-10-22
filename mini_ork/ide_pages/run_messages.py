"""A run's Messages tab: the concord mailbox of ``run:<id>``.

Concord keeps messages in ContextNest's ``coord.db``, never in ``state.db``. A
run registers as the principal ``run:<sanitized id>`` when it starts
(``orchestration/concord_run.py``), so that principal's inbox is the run's
mailbox. A message one run sends another arrives here with a ``run:`` sender —
concord is the only run-to-run channel mini-ork has. ContextNest lists a
principal's inbox but has no outbox query, so what this run *sent* is not shown.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from mini_ork import cn_client
from mini_ork.ide_pages import spec as S
from mini_ork.orchestration.concord_run import _sanitize_run_id

TITLE = "Concord messages"

# A live run's page re-fetches every five seconds: a hung ContextNest must cost
# each refresh half a second, not the coord helpers' 3 s default.
_READ_TIMEOUT_SEC = 0.5


@dataclass
class Mailbox:
    principal: str
    # ok | off | down | unregistered | refused | config
    state: str
    messages: list[dict[str, Any]] = field(default_factory=list)
    error: str = ""


def principal_for(run_id: str) -> str:
    return f"run:{_sanitize_run_id(run_id)}"


def read_mailbox(run_id: str) -> Mailbox:
    from mini_ork.dispatch.secrets import SecretStoreError

    principal = principal_for(run_id)
    if os.environ.get("MO_DISABLE_CN", "0") == "1":
        return Mailbox(principal, "off")
    try:
        # The ping is cached (CN_PING_TTL), so a stopped ContextNest costs a
        # file read per refresh rather than a connection attempt.
        if not cn_client.available():
            return Mailbox(principal, "down")
        data = cn_client.coord_inbox(principal, unacked=False, timeout=_READ_TIMEOUT_SEC)
    except SecretStoreError as exc:
        return Mailbox(principal, "config", error=str(exc))
    except cn_client.CoordUnavailable as exc:
        return Mailbox(principal, "down", error=str(exc))
    except cn_client.CoordHTTPError as exc:
        if exc.status == 404:
            return Mailbox(principal, "unregistered")
        return Mailbox(principal, "refused", error=f"HTTP {exc.status} {exc.body}".strip())
    messages = [m for m in (data.get("messages") or []) if isinstance(m, dict)]
    return Mailbox(principal, "ok", messages)


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


def _sender_run(sender: str) -> str | None:
    """The run id behind a ``run:`` sender, for the row's Open run button.

    The sanitiser only rewrites characters a run id never carries, so the
    principal name is the run id itself.
    """
    if not sender.startswith("run:"):
        return None
    return sender[len("run:"):] or None


def _row(message: dict[str, Any], principal: str) -> dict[str, Any]:
    sender = str(message.get("from") or "unknown sender")
    msg_id = str(message.get("msg_id") or "")
    if message.get("acked_at"):
        mark, colour = "✓", "green"
        state = f"acked by {message.get('acked_by') or 'someone'}"
    elif message.get("delivered_at"):
        # Delivery is the ContextNest turn hook handing the message to one of
        # the run's agent sessions; only an ack says someone acted on it.
        mark, colour = "✉", "blue"
        state = f"delivered to {str(message.get('delivered_to') or 'an agent')[:12]} · not acked"
    else:
        mark, colour = "✉", "yellow"
        state = "not yet delivered to an agent"
    acts: list[dict[str, Any]] = []
    if msg_id and not message.get("acked_at"):
        acts.append(S.btn("Ack", S.cli("concord", "ack", principal, msg_id, home=False), "ghost"))
    other_run = _sender_run(sender)
    if other_run and sender != principal:
        acts.append(S.btn("Open run", S.open_run(other_run), "ghost"))
    if sender != principal:
        acts.append(S.btn("Reply", S.thread(f"/concord send {sender} "), "ghost"))
    return S.item(f"{sender} · {state}", str(message.get("body") or ""), m=mark, mc=colour,
                  is_mono=True, when=_when(message.get("created_at")), acts=acts)


def _unavailable(mailbox: Mailbox) -> dict[str, Any]:
    if mailbox.state == "off":
        return S.dot("ContextNest is off",
                     "MO_DISABLE_CN=1 — concord messages live in ContextNest")
    if mailbox.state == "down":
        return S.warn("ContextNest is not reachable",
                      "Concord messages live in ContextNest (CN_BASE_URL); start it to "
                      "read this run's mailbox")
    if mailbox.state == "unregistered":
        return S.dot(f"This run has no concord mailbox ({mailbox.principal})",
                     "It never registered with concord: it ran with MO_CONCORD=0, before "
                     "concord existed, or while ContextNest was down")
    if mailbox.state == "config":
        return S.bad("ContextNest is misconfigured", mailbox.error)
    return S.bad("ContextNest refused the read", mailbox.error)


def messages_tab(run_id: str) -> list[dict[str, Any]]:
    mailbox = read_mailbox(run_id)
    if mailbox.state != "ok":
        return [S.lst(TITLE, [_unavailable(mailbox)], full=True)]
    messages = sorted(mailbox.messages, key=lambda m: str(m.get("created_at") or ""),
                      reverse=True)
    unacked = sum(1 for m in messages if not m.get("acked_at"))
    from_runs = sum(1 for m in messages
                    if str(m.get("from") or "").startswith("run:")
                    and m.get("from") != mailbox.principal)
    note = (f"Sent to {mailbox.principal}, newest first · {len(messages)} total · "
            f"{unacked} not acked · {from_runs} from other runs. Messages this run "
            "sent are not listed: ContextNest has no outbox query.")
    items = [_row(m, mailbox.principal) for m in messages] or [
        S.dot("No concord messages were sent to this run")]
    return [S.lst(TITLE, items, full=True, note=note,
                  actions=[S.btn("Send message", S.thread(f"/concord send {mailbox.principal} "))])]
