"""Tests for ``mini_ork.integrations.telegram`` — the transport over the thread seam.

No socket and no run is ever opened. ``urllib.request.urlopen`` is
monkeypatched with a fake Bot API response, and ``control.launch_run`` /
``control.steer_run`` are stubbed with the same fixtures
``tests/unit/test_threads_service.py`` uses (the house pattern), so what is
pinned is *the decision*, not a real launch. The threads DB is a real migrated
``state.db`` under ``tmp_path``, because the state machine reads ``task_runs``
and settles a live run against it — an empty sqlite file would pass while
testing nothing.

What is pinned:

* a first message from a chat opens the thread ``(telegram, chat_id, None)``
  names and *launches* — the thread id is the key, derived the same way a second
  poll or a webhook would derive it;
* a follow-up on the same key *steers* (a run is live) or *continues* (it
  settled), and never launches a second time — the one invariant that makes a
  chat a conversation rather than a run per message;
* a redelivered update id is a *duplicate*: it lands on the ``external_id``
  dedupe, so the launcher is not reached twice even though the update arrived
  twice;
* a missing bot token *fails closed* — the error names the vault key, and the
  transport never falls back to an empty token;
* the inbound cursor advances past every update, including one it ignored.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from mini_ork.intake import webhooks  # noqa: E402
from mini_ork.integrations import telegram  # noqa: E402
from mini_ork.integrations.telegram import (  # noqa: E402
    TOKEN_VAULT_KEY,
    TelegramConfigError,
    TelegramSignatureError,
    TelegramTransport,
)
from mini_ork.stores import migrate as mig  # noqa: E402
from mini_ork.threads import store  # noqa: E402

CHAT_ID = 4242
TOKEN = "123456:test-token-not-a-real-one"
SECRET = "correct horse battery staple"


@pytest.fixture
def home(tmp_path: Path) -> Path:
    h = tmp_path / "proj" / ".mini-ork"
    h.mkdir(parents=True)
    rc, _, err = mig.init_db(db=str(h / "state.db"), root=str(REPO))
    assert rc == 0, err
    return h


@pytest.fixture
def db(home: Path) -> str:
    return str(home / "state.db")


@pytest.fixture
def launches(monkeypatch) -> list[dict]:
    """A ``control.launch_run`` that records instead of spawning."""
    import mini_ork.web.control as control

    calls: list[dict] = []

    def fake(home, recipe, kickoff, run_id=None):
        calls.append({"home": home, "recipe": recipe, "kickoff": kickoff, "run_id": run_id})
        return {
            "ok": True,
            "run_id": run_id,
            "recipe": recipe,
            "pid": 4242,
            "kickoff_path": str(Path(home) / "runs-inbox" / f"{run_id}.md"),
            "log_path": str(Path(home) / "logs" / f"{run_id}.log"),
        }

    monkeypatch.setattr(control, "launch_run", fake)
    return calls


@pytest.fixture
def steers(monkeypatch) -> list[dict]:
    """A ``control.steer_run`` that records instead of injecting."""
    import mini_ork.web.control as control

    calls: list[dict] = []

    def fake(db, task_run_id, message, **kwargs):
        calls.append({"run_id": task_run_id, "message": message, **kwargs})
        return {"ok": True, "steering_id": 7}

    monkeypatch.setattr(control, "steer_run", fake)
    return calls


class _FakeAPI:
    """A stand-in for ``urllib.request.urlopen`` against the Bot API.

    ``updates`` is the queue the next ``getUpdates`` will return; ``urls`` is
    every request that was made, so a test can assert the cursor and — just as
    importantly — that the token never had to be real for any of this.
    """

    def __init__(self) -> None:
        self.updates: list[dict] = []
        self.urls: list[str] = []

    def install(self, monkeypatch) -> "_FakeAPI":
        def fake_urlopen(request, timeout=None):  # noqa: ARG001 — timeout is the API's
            url = getattr(request, "full_url", str(request))
            self.urls.append(url)
            body = json.dumps({"ok": True, "result": list(self.updates)}).encode("utf-8")
            return _FakeResponse(body)

        monkeypatch.setattr(telegram.urllib.request, "urlopen", fake_urlopen)
        return self


class _FakeResponse:
    """The slice of an ``HTTPResponse`` the transport actually uses."""

    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


class _EmptyVault:
    """A vault with nothing in it — a missing key, not a broken one."""

    def resolve(self, key: str) -> None:  # noqa: ARG002 — the point is that it has none
        return None

    def describe(self) -> str:
        return "a test vault with nothing in it"


@pytest.fixture
def api(monkeypatch) -> _FakeAPI:
    return _FakeAPI().install(monkeypatch)


def _update(update_id: int, text: str, *, chat_id: int = CHAT_ID, thread: int | None = None) -> dict:
    message: dict[str, Any] = {"chat": {"id": chat_id, "title": "Ops"}, "text": text}
    if thread is not None:
        message["message_thread_id"] = thread
    return {"update_id": update_id, "message": message}


def _transport(**kwargs: Any) -> TelegramTransport:
    kwargs.setdefault("token", TOKEN)
    kwargs.setdefault("webhook_secret", SECRET)
    return TelegramTransport(**kwargs)


# -- the long-poll tier ----------------------------------------------------


def test_first_message_launches_the_thread(home: Path, db: str, launches, api) -> None:
    """A first message opens ``telegram-<chat>`` and launches attempt 1."""
    api.updates = [_update(101, "deploy is red")]
    result = _transport().poll_once(home=home, db=db)

    assert result.action == "launched"
    assert result.delivery is not None
    assert result.delivery.thread_id == telegram.thread_key("telegram", CHAT_ID, None)
    assert result.update_id == 101
    assert result.next_offset == 102
    assert len(launches) == 1
    # The run is the thread's own id (attempt 1 reuses it), the trigger the
    # canvas already routes on.
    assert launches[0]["run_id"] == result.delivery.thread_id


def test_followup_never_launches_a_second_run(
    home: Path, db: str, launches, steers, api
) -> None:
    """A second message on the same key steers — a chat is one conversation."""
    transport = _transport()

    api.updates = [_update(201, "first")]
    assert transport.poll_once(home=home, db=db).action == "launched"

    api.updates = [_update(202, "second")]
    second = transport.poll_once(home=home, db=db)

    assert second.action in ("steered", "continued")
    assert second.action != "launched"
    assert len(launches) == 1, "a follow-up spawned a second run"
    assert second.delivery is not None
    assert second.delivery.thread_id == telegram.thread_key("telegram", CHAT_ID, None)


def test_redelivered_update_is_a_duplicate(home: Path, db: str, launches, steers, api) -> None:
    """The same update id twice is a no-op — Telegram redelivers until acked."""
    transport = _transport()
    again = _update(301, "only once")

    api.updates = [again]
    assert transport.poll_once(home=home, db=db).action == "launched"

    api.updates = [dict(again)]  # byte-for-byte the same delivery
    redelivered = transport.poll_once(home=home, db=db)

    assert redelivered.action == "duplicate"
    assert len(launches) == 1, "a redelivery reached the launcher a second time"


def test_cursor_advances_past_an_ignored_update(home: Path, db: str, launches, api) -> None:
    """An update with no message still moves the cursor, or it is re-read forever."""
    api.updates = [{"update_id": 401, "edited_message": {"chat": {"id": CHAT_ID}}}]
    result = _transport().poll_once(home=home, db=db)

    assert result.action == "ignored"
    assert result.next_offset == 402
    assert launches == []


def test_poll_asks_for_the_offset_it_was_given(home: Path, db: str, launches, api) -> None:
    """``offset=`` reaches the wire — the cursor is not merely bookkeeping."""
    _transport().poll_once(offset=900, home=home, db=db)
    assert api.urls and "offset=900" in api.urls[-1]


def test_batch_is_delivered_and_reported_whole(home: Path, db: str, launches, api) -> None:
    """A ``getUpdates`` array is three updates, not "whatever landed last".

    Every update acts, the batch carries each outcome, and the cursor clears
    the *whole* batch rather than the final element.
    """
    transport = _transport()
    api.updates = [
        _update(1001, "first chat", chat_id=CHAT_ID),
        {"update_id": 1002, "message": {"chat": {"id": CHAT_ID + 1}}},  # no text
        _update(1003, "second chat", chat_id=CHAT_ID + 2),
    ]

    result = transport.poll_once(home=home, db=db)

    assert [r.action for r in result.batch] == ["launched", "ignored", "launched"]
    assert len(launches) == 2, "an update in the batch was dropped"
    # The representative outcome is the last update that acted, and the cursor
    # is one past the batch's highest id — not one past its last element.
    assert result.action == "launched"
    assert result.delivery is not None
    assert result.delivery.thread_id == telegram.thread_key("telegram", CHAT_ID + 2, None)
    assert result.update_id == 1003
    assert result.next_offset == 1004


def test_trailing_ignored_update_does_not_erase_the_delivery(
    home: Path, db: str, launches, api
) -> None:
    """A batch ending on a non-message still reports the message it carried."""
    api.updates = [
        _update(2001, "carried"),
        {"update_id": 2002, "message": {"chat": {"id": CHAT_ID}}},  # no text → ignored
    ]

    result = _transport().poll_once(home=home, db=db)

    assert result.action == "launched", "the trailing ignored update erased the delivery"
    assert result.delivery is not None
    assert result.batch[-1].action == "ignored"
    assert result.next_offset == 2003
    assert launches, "the carried message never launched"


def test_import_is_inert_and_carries_no_token() -> None:
    """``import mini_ork.integrations`` needs no token and opens nothing."""
    import mini_ork.integrations as integrations

    assert integrations.TelegramTransport is TelegramTransport
    assert integrations.thread_key("telegram", 7, None) == "telegram-7"
    # A sub-thread is part of the key; the top level is not.
    assert integrations.thread_key("telegram", 7, 55) == "telegram-7-55"


# -- fail-closed configuration --------------------------------------------


def test_missing_token_fails_closed(monkeypatch) -> None:
    """No token, no transport — and the error names the key a person must set."""
    monkeypatch.setattr(telegram, "vault_environment", lambda *a, **k: _EmptyVault())

    with pytest.raises(TelegramConfigError) as caught:
        TelegramTransport(token=None)

    assert TOKEN_VAULT_KEY in str(caught.value)
    assert "never falls back to an empty token" in str(caught.value)


def test_empty_token_is_not_a_token(monkeypatch) -> None:
    """An explicit ``""`` is refused rather than used — the empty-string footgun."""
    monkeypatch.setattr(telegram, "vault_environment", lambda *a, **k: _EmptyVault())

    with pytest.raises(TelegramConfigError):
        TelegramTransport(token="")


def test_missing_webhook_secret_fails_closed(monkeypatch) -> None:
    """A webhook with no resolvable secret refuses rather than accepting blind."""
    monkeypatch.setattr(telegram, "vault_environment", lambda *a, **k: _EmptyVault())
    transport = TelegramTransport(token=TOKEN)

    with pytest.raises(TelegramConfigError) as caught:
        transport.handle_webhook(b"{}", None)

    assert telegram.WEBHOOK_SECRET_VAULT_KEY in str(caught.value)


# -- the webhook tier ------------------------------------------------------


def test_webhook_verifies_then_launches(home: Path, db: str, launches) -> None:
    """A body signed with the shared secret is accepted and delivered."""
    transport = _transport()
    body = json.dumps(_update(501, "from a webhook")).encode("utf-8")

    delivery = transport.handle_webhook(
        body, webhooks.sign(SECRET, body), home=home, db=db
    )

    assert delivery is not None
    assert delivery.action == "launched"
    assert len(launches) == 1


def test_webhook_rejects_a_body_that_was_tampered_with(home: Path, db: str, launches) -> None:
    """The signature covers the bytes, so an edited body is a refusal."""
    transport = _transport()
    body = json.dumps(_update(601, "honest")).encode("utf-8")
    header = webhooks.sign(SECRET, body)
    tampered = json.dumps(_update(601, "malicious")).encode("utf-8")

    with pytest.raises(TelegramSignatureError):
        transport.handle_webhook(tampered, header, home=home, db=db)
    assert launches == []


def test_webhook_rejects_a_missing_signature(home: Path, db: str, launches) -> None:
    """No header is not "unsigned is fine" — it is a refusal."""
    body = json.dumps(_update(701, "anonymous")).encode("utf-8")

    with pytest.raises(TelegramSignatureError):
        _transport().handle_webhook(body, None, home=home, db=db)
    assert launches == []


def test_webhook_accepts_telegrams_own_secret_token(home: Path, db: str, launches) -> None:
    """A real Bot API deployment presents its secret-token header, not an HMAC."""
    transport = TelegramTransport(token=TOKEN, secret_token="s3cr3t")
    body = json.dumps(_update(801, "native header")).encode("utf-8")

    delivery = transport.handle_webhook(
        body, None, secret_token_header="s3cr3t", home=home, db=db
    )

    assert delivery is not None and delivery.action == "launched"


def test_wrong_secret_token_falls_back_to_the_hmac(home: Path, db: str, launches) -> None:
    """A presented-but-wrong native token is not the end of the check.

    Both proofs are configured, so the HMAC is still tried and a body it signs
    is accepted — the native header is an extra door, not a lock on the other.
    """
    transport = _transport(secret_token="s3cr3t")
    body = json.dumps(_update(4001, "hmac still works")).encode("utf-8")

    delivery = transport.handle_webhook(
        body, webhooks.sign(SECRET, body), secret_token_header="not-s3cr3t",
        home=home, db=db,
    )

    assert delivery is not None and delivery.action == "launched"


def test_secret_token_mismatch_is_a_signature_error_not_a_config_error(
    monkeypatch, home: Path, db: str, launches
) -> None:
    """A forged native header is refused *as forged*, even with no HMAC secret.

    With ``secret_token`` configured and nothing resolvable for
    ``integrations.telegram.webhook_secret``, the endpoint used to raise
    ``TelegramConfigError`` ("the secret is missing") at a caller who simply
    did not present the right token. Fail-closed either way, but the diagnosis
    belongs to the signature, not the configuration.
    """
    monkeypatch.setattr(telegram, "vault_environment", lambda *a, **k: _EmptyVault())
    transport = TelegramTransport(token=TOKEN, secret_token="s3cr3t")
    body = json.dumps(_update(5001, "forged")).encode("utf-8")

    with pytest.raises(TelegramSignatureError):
        transport.handle_webhook(
            body, None, secret_token_header="not-s3cr3t", home=home, db=db
        )
    assert launches == []


# -- outbound --------------------------------------------------------------


def test_send_projects_an_assistant_turn(home: Path, db: str, launches, api) -> None:
    """Outbound lands on the transcript, idempotently on ``event_id``."""
    transport = _transport()
    api.updates = [_update(901, "start")]
    launched = transport.poll_once(home=home, db=db)
    assert launched.delivery is not None
    thread_id = launched.delivery.thread_id

    first = transport.send(thread_id, "working on it", event_id="run-1-final", db=db)
    again = transport.send(thread_id, "working on it", event_id="run-1-final", db=db)

    assert first.id == again.id, "a retried send opened a second turn"
    roles = [m.role for m in store.messages(thread_id, db=db)]
    assert str(first.role) == "assistant"
    assert roles.count("assistant") == 1
