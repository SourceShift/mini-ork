"""Tests for ``POST /api/v1/chat/<transport>`` — the secure webhook tier.

Driven through a real ``TestClient`` because what matters here only exists at
the HTTP layer: a transport that is not enabled must look *absent* (404, not
403), a missing secret or a vault protection gap must fail closed (503, never a
silent empty-string secret), a bad signature must be a 401, and none of those
may write a ``threads`` row or start a run.

Hermetic, like ``test_chat_serve.py``: one temp home per test with a migrated
``state.db``, a stub vault patched onto ``serve.vault_environment``, launch and
steer recorders on ``mini_ork.web.control``, and ``webhooks.protection_gap``
lifted except in the test that needs a gap. Discord's Ed25519 check is stubbed
at the class level because the transport is built inside the route.

What is pinned beyond the status codes: a redelivery starts **one** run, a
delivery whose launch fails is still a 200 (so the platform does not retry
forever), and no secret value reaches a response body or the server's stderr.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from fastapi.testclient import TestClient  # noqa: E402

from mini_ork.intake import webhooks  # noqa: E402
from mini_ork.integrations import discord, serve, slack, telegram  # noqa: E402
from mini_ork.stores import migrate as mig  # noqa: E402
from mini_ork.threads import store as thread_store  # noqa: E402
from mini_ork.web import control as web_control  # noqa: E402
from mini_ork.web.app import create_app  # noqa: E402

TG_VALUES = {
    serve.TELEGRAM_BOT_TOKEN_KEY: "tg-bot-token-7a1e",
    serve.TELEGRAM_WEBHOOK_SECRET_KEY: "tg-webhook-secret-3c9f",
}
SLACK_VALUES = {
    slack.BOT_TOKEN_VAULT_KEY: "xoxb-test-token-not-a-real-one",
    slack.SIGNING_SECRET_VAULT_KEY: "slack-signing-0e18ab",
}
DISCORD_VALUES = {
    discord.BOT_TOKEN_VAULT_KEY: "discord-bot-unit-9d4",
    discord.PUBLIC_KEY_VAULT_KEY: "cd" * 32,
}

TG_SECRET = TG_VALUES[serve.TELEGRAM_WEBHOOK_SECRET_KEY]


class _StubVault:
    """The vault seam: resolve by name, describe without leaking values."""

    def __init__(self, values):
        self._values = dict(values)

    def resolve(self, key):
        return self._values.get(key)

    def describe(self):
        return "the unit-test stub vault"


class _Launches:
    """Recorder for ``control.launch_run`` — no process is ever spawned."""

    def __init__(self, ok=True, error="unit: launch refused"):
        self.ok = ok
        self.error = error
        self.calls = []

    def __call__(self, home, recipe, kickoff_markdown, run_id=None, **_):
        self.calls.append(
            {
                "home": str(home),
                "recipe": recipe,
                "run_id": run_id,
                "kickoff": kickoff_markdown,
            }
        )
        if not self.ok:
            return {"ok": False, "error": self.error}
        return {
            "ok": True,
            "run_id": run_id,
            "recipe": recipe,
            "pid": 4242,
            "kickoff_path": f"{home}/runs-inbox/{run_id}.md",
            "log_path": f"{home}/runs-inbox/{run_id}.launch.log",
        }


class _Steers:
    def __init__(self):
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch):
    for name in ("MINI_ORK_DB", "MINI_ORK_HOME", "MINI_ORK_CHAT", "MO_INTAKE_WEBHOOKS"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def home(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    rc, _out, err = mig.init_db(db=str(home / "state.db"), root=str(REPO))
    assert rc == 0, err
    return home


def _write_chat_config(home, transport, tier, allow, extra=None):
    config = home / "config"
    config.mkdir(parents=True, exist_ok=True)
    lines = [f"transport: {transport}", f"tier: {tier}", "allow_channels:"]
    lines += [f'  - "{item}"' for item in allow]
    if extra:
        lines += list(extra)
    (config / "chat.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _stack(monkeypatch, home, *, transport, values, allow, tier="secure"):
    """One hermetic server: chat.yaml, stub vault, recorders, TestClient."""
    _write_chat_config(home, transport, tier, allow)
    monkeypatch.setattr(serve, "vault_environment", lambda: _StubVault(values))
    monkeypatch.setattr(webhooks, "protection_gap", lambda: None)
    launches = _Launches()
    steers = _Steers()
    monkeypatch.setattr(web_control, "launch_run", launches)
    monkeypatch.setattr(web_control, "steer_run", steers)
    client = TestClient(create_app(home=home, dev_cors=False))
    return client, str(home / "state.db"), launches


def _patch_discord_verify(monkeypatch):
    """Ed25519 passes only for the signature header ``"good"``."""

    def fake(self, key_hex, timestamp, body, signature_hex):
        if str(signature_hex).strip() != "good":
            raise discord.DiscordSignatureError("unit: bad signature")
        return None

    monkeypatch.setattr(discord.DiscordTransport, "verify_ed25519", fake)


def _tg_body(update_id=1001, chat_id=4242, text="fix the build"):
    return json.dumps(
        {
            "update_id": update_id,
            "message": {
                "message_id": 7,
                "chat": {"id": chat_id, "title": "Unit Room"},
                "text": text,
            },
        }
    ).encode()


def _slack_sign(values, body, age=0):
    ts = str(int(time.time()) - age)
    base = b"v0:" + ts.encode() + b":" + body
    digest = hmac.new(
        values[slack.SIGNING_SECRET_VAULT_KEY].encode(), base, hashlib.sha256
    ).hexdigest()
    return {slack.TIMESTAMP_HEADER: ts, slack.SIGNATURE_HEADER: f"v0={digest}"}


def _slack_event_body(channel="C42", event_id="ev-unit-1"):
    return json.dumps(
        {
            "type": "event_callback",
            "event_id": event_id,
            "event": {
                "type": "message",
                "channel": channel,
                "text": "fix the build",
                "ts": "1729900000.000100",
            },
        }
    ).encode()


def _thread_ids(db):
    return [t.id for t in thread_store.list_threads(db=db)]


# -- 1. gating ----------------------------------------------------------------


def test_telegram_404_without_chat_yaml(monkeypatch, home):
    monkeypatch.setattr(serve, "vault_environment", lambda: _StubVault(TG_VALUES))
    monkeypatch.setattr(webhooks, "protection_gap", lambda: None)
    monkeypatch.setattr(web_control, "launch_run", _Launches())
    client = TestClient(create_app(home=home, dev_cors=False))
    resp = client.post("/api/v1/chat/telegram", content=_tg_body(), headers={})
    assert resp.status_code == 404
    assert resp.json() == {
        "ok": False,
        "error": "chat webhooks for 'telegram' are not enabled on this server",
    }
    assert _thread_ids(str(home / "state.db")) == []


def test_telegram_404_when_tier_simple(monkeypatch, home):
    client, db, launches = _stack(
        monkeypatch, home, transport="telegram", values=TG_VALUES,
        allow=["4242"], tier="simple",
    )
    resp = client.post("/api/v1/chat/telegram", content=_tg_body(), headers={})
    assert resp.status_code == 404
    assert _thread_ids(db) == []
    assert launches.calls == []


def test_telegram_404_when_another_transport_configured(monkeypatch, home):
    client, db, launches = _stack(
        monkeypatch, home, transport="slack", values=SLACK_VALUES, allow=["C42"]
    )
    resp = client.post("/api/v1/chat/telegram", content=_tg_body(), headers={})
    assert resp.status_code == 404
    assert _thread_ids(db) == []
    assert launches.calls == []


def test_body_over_max_bytes_is_413(monkeypatch, home):
    client, db, launches = _stack(
        monkeypatch, home, transport="telegram", values=TG_VALUES, allow=["4242"]
    )
    resp = client.post(
        "/api/v1/chat/telegram",
        content=b"x" * (webhooks.MAX_BODY_BYTES + 1),
        headers={telegram.SECRET_TOKEN_HEADER: TG_SECRET},
    )
    assert resp.status_code == 413
    assert _thread_ids(db) == []
    assert launches.calls == []


# -- 2. telegram secure refusals ----------------------------------------------


def test_telegram_503_on_protection_gap(monkeypatch, home):
    client, db, launches = _stack(
        monkeypatch, home, transport="telegram", values=TG_VALUES, allow=["4242"]
    )
    monkeypatch.setattr(
        webhooks, "protection_gap", lambda: "unit: the vault is unprotected"
    )
    resp = client.post(
        "/api/v1/chat/telegram",
        content=_tg_body(),
        headers={telegram.SECRET_TOKEN_HEADER: TG_SECRET},
    )
    assert resp.status_code == 503
    assert resp.json()["error"] == "unit: the vault is unprotected"
    assert _thread_ids(db) == []
    assert launches.calls == []


def test_telegram_503_when_webhook_secret_missing(monkeypatch, home, capsys):
    values = {serve.TELEGRAM_BOT_TOKEN_KEY: TG_VALUES[serve.TELEGRAM_BOT_TOKEN_KEY]}
    client, db, launches = _stack(
        monkeypatch, home, transport="telegram", values=values, allow=["4242"]
    )
    resp = client.post(
        "/api/v1/chat/telegram",
        content=_tg_body(),
        headers={telegram.SECRET_TOKEN_HEADER: "anything"},
    )
    assert resp.status_code == 503
    assert resp.json()["error"] == "chat is not configured to accept 'telegram' webhooks"
    assert _thread_ids(db) == []
    assert launches.calls == []
    # No secret value reaches the response or the server's own log line.
    captured = capsys.readouterr()
    assert TG_VALUES[serve.TELEGRAM_BOT_TOKEN_KEY] not in resp.text
    assert TG_VALUES[serve.TELEGRAM_BOT_TOKEN_KEY] not in captured.err


def test_telegram_401_on_wrong_secret_token(monkeypatch, home):
    client, db, launches = _stack(
        monkeypatch, home, transport="telegram", values=TG_VALUES, allow=["4242"]
    )
    resp = client.post(
        "/api/v1/chat/telegram",
        content=_tg_body(),
        headers={telegram.SECRET_TOKEN_HEADER: "definitely-not-the-secret"},
    )
    assert resp.status_code == 401
    assert resp.json()["error"] == "the signature does not match"
    assert _thread_ids(db) == []
    assert launches.calls == []
    assert TG_SECRET not in resp.text


def test_telegram_unparseable_body_is_400(monkeypatch, home):
    client, db, launches = _stack(
        monkeypatch, home, transport="telegram", values=TG_VALUES, allow=["4242"]
    )
    resp = client.post(
        "/api/v1/chat/telegram",
        content=b"this is not json",
        headers={telegram.SECRET_TOKEN_HEADER: TG_SECRET},
    )
    assert resp.status_code == 400
    assert _thread_ids(db) == []
    assert launches.calls == []


# -- 3. telegram secure, valid secret token -----------------------------------


def test_telegram_valid_secret_launches_thread(monkeypatch, home):
    client, db, launches = _stack(
        monkeypatch, home, transport="telegram", values=TG_VALUES, allow=["4242"]
    )
    resp = client.post(
        "/api/v1/chat/telegram",
        content=_tg_body(chat_id=4242),
        headers={telegram.SECRET_TOKEN_HEADER: TG_SECRET},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["action"] == "launched"
    assert body["thread_id"] == "telegram-4242"
    for key in ("turn", "message_id", "run_id"):
        assert key in body

    thread = thread_store.get_thread("telegram-4242", db=db)
    assert thread is not None
    assert thread.origin == "chat"
    assert len(launches.calls) == 1


def test_telegram_same_body_twice_is_duplicate(monkeypatch, home):
    client, db, launches = _stack(
        monkeypatch, home, transport="telegram", values=TG_VALUES, allow=["4242"]
    )
    headers = {telegram.SECRET_TOKEN_HEADER: TG_SECRET}
    first = client.post(
        "/api/v1/chat/telegram", content=_tg_body(update_id=55), headers=headers
    )
    assert first.status_code == 200
    assert first.json()["action"] == "launched"

    second = client.post(
        "/api/v1/chat/telegram", content=_tg_body(update_id=55), headers=headers
    )
    assert second.status_code == 200
    body = second.json()
    assert body["ok"] is True
    assert body["action"] == "duplicate"
    assert len(launches.calls) == 1
    # The thread row legitimately exists; a redelivery adds no second launch.
    assert _thread_ids(db) == ["telegram-4242"]


def test_telegram_denied_chat_is_ignored(monkeypatch, home):
    client, db, launches = _stack(
        monkeypatch, home, transport="telegram", values=TG_VALUES, allow=["4242"]
    )
    resp = client.post(
        "/api/v1/chat/telegram",
        content=_tg_body(chat_id=9999),
        headers={telegram.SECRET_TOKEN_HEADER: TG_SECRET},
    )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "action": "ignored"}
    assert _thread_ids(db) == []
    assert launches.calls == []


def test_telegram_failed_launch_is_200_with_ok_false(monkeypatch, home):
    client, _db, launches = _stack(
        monkeypatch, home, transport="telegram", values=TG_VALUES, allow=["4242"]
    )
    launches.ok = False
    resp = client.post(
        "/api/v1/chat/telegram",
        content=_tg_body(update_id=77),
        headers={telegram.SECRET_TOKEN_HEADER: TG_SECRET},
    )
    # A launch failure stays a 200 so the platform does not retry forever.
    assert resp.status_code == 200
    assert resp.json()["ok"] is False
    assert resp.json()["action"] == "error"


# -- 4. slack -----------------------------------------------------------------


def test_slack_url_verification_returns_challenge(monkeypatch, home):
    client, db, launches = _stack(
        monkeypatch, home, transport="slack", values=SLACK_VALUES, allow=["C42"]
    )
    body = json.dumps(
        {"type": "url_verification", "challenge": "unit-challenge-1"}
    ).encode()
    resp = client.post(
        "/api/v1/chat/slack", content=body, headers=_slack_sign(SLACK_VALUES, body)
    )
    assert resp.status_code == 200
    assert resp.json()["challenge"] == "unit-challenge-1"
    assert _thread_ids(db) == []
    assert launches.calls == []


def test_slack_signed_event_callback_launches(monkeypatch, home):
    client, db, launches = _stack(
        monkeypatch, home, transport="slack", values=SLACK_VALUES, allow=["C42"]
    )
    body = _slack_event_body(channel="C42")
    resp = client.post(
        "/api/v1/chat/slack", content=body, headers=_slack_sign(SLACK_VALUES, body)
    )
    assert resp.status_code == 200
    assert resp.json()["action"] == "launched"
    assert resp.json()["ok"] is True
    assert len(launches.calls) == 1
    thread = thread_store.get_thread("slack-C42", db=db)
    assert thread is not None
    assert thread.origin == "chat"


def test_slack_stale_timestamp_is_401(monkeypatch, home):
    client, db, launches = _stack(
        monkeypatch, home, transport="slack", values=SLACK_VALUES, allow=["C42"]
    )
    body = _slack_event_body(channel="C42")
    resp = client.post(
        "/api/v1/chat/slack",
        content=body,
        headers=_slack_sign(SLACK_VALUES, body, age=10_000),
    )
    assert resp.status_code == 401
    assert resp.json()["error"] == "the signature does not match"
    assert _thread_ids(db) == []
    assert launches.calls == []


# -- 5. discord ---------------------------------------------------------------


def test_discord_ping_gets_the_pong_unchanged(monkeypatch, home):
    client, db, launches = _stack(
        monkeypatch, home, transport="discord", values=DISCORD_VALUES, allow=["99"]
    )
    _patch_discord_verify(monkeypatch)
    resp = client.post(
        "/api/v1/chat/discord",
        content=json.dumps({"type": 1}).encode(),
        headers={discord.SIGNATURE_HEADER: "good", discord.TIMESTAMP_HEADER: "1"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"type": 1}
    assert _thread_ids(db) == []
    assert launches.calls == []


def test_discord_application_command_launches(monkeypatch, home):
    client, db, launches = _stack(
        monkeypatch, home, transport="discord", values=DISCORD_VALUES, allow=["99"]
    )
    _patch_discord_verify(monkeypatch)
    body = json.dumps(
        {
            "type": 2,
            "id": "i-unit-1",
            "channel_id": "99",
            "user": {"id": "u1"},
            "data": {
                "name": "ork",
                "options": [{"name": "task", "value": "fix the build"}],
            },
        }
    ).encode()
    resp = client.post(
        "/api/v1/chat/discord",
        content=body,
        headers={discord.SIGNATURE_HEADER: "good", discord.TIMESTAMP_HEADER: "1"},
    )
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["type"] == 4
    assert payload["data"]["flags"] == 64
    assert payload["data"]["content"].startswith("launched")
    assert "thread discord-99" in payload["data"]["content"]
    assert len(launches.calls) == 1
    thread = thread_store.get_thread("discord-99", db=db)
    assert thread is not None
    assert thread.origin == "chat"


def test_discord_bad_signature_is_401(monkeypatch, home):
    client, db, launches = _stack(
        monkeypatch, home, transport="discord", values=DISCORD_VALUES, allow=["99"]
    )
    _patch_discord_verify(monkeypatch)
    resp = client.post(
        "/api/v1/chat/discord",
        content=json.dumps({"type": 1}).encode(),
        headers={discord.SIGNATURE_HEADER: "nope", discord.TIMESTAMP_HEADER: "1"},
    )
    assert resp.status_code == 401
    assert resp.json()["error"] == "the signature does not match"
    assert _thread_ids(db) == []
    assert launches.calls == []


def test_discord_missing_public_key_is_503(monkeypatch, home):
    values = {discord.BOT_TOKEN_VAULT_KEY: DISCORD_VALUES[discord.BOT_TOKEN_VAULT_KEY]}
    client, db, launches = _stack(
        monkeypatch, home, transport="discord", values=values, allow=["99"]
    )
    _patch_discord_verify(monkeypatch)
    resp = client.post(
        "/api/v1/chat/discord",
        content=json.dumps({"type": 1}).encode(),
        headers={discord.SIGNATURE_HEADER: "good", discord.TIMESTAMP_HEADER: "1"},
    )
    assert resp.status_code == 503
    assert resp.json()["error"] == "chat is not configured to accept 'discord' webhooks"
    assert _thread_ids(db) == []
    assert launches.calls == []


def test_discord_nothing_to_deliver(monkeypatch, home):
    client, db, launches = _stack(
        monkeypatch, home, transport="discord", values=DISCORD_VALUES, allow=["99"]
    )
    _patch_discord_verify(monkeypatch)
    resp = client.post(
        "/api/v1/chat/discord",
        content=json.dumps(
            {
                "type": 2,
                "id": "i-unit-2",
                "channel_id": "99",
                "user": {"id": "u1"},
                "data": {"name": ""},
            }
        ).encode(),
        headers={discord.SIGNATURE_HEADER: "good", discord.TIMESTAMP_HEADER: "1"},
    )
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["type"] == 4
    assert payload["data"]["content"] == "nothing to deliver"
    assert payload["data"]["flags"] == 64
    assert _thread_ids(db) == []
    assert launches.calls == []


# -- 6. a broken config fails closed ------------------------------------------


def test_misconfigured_chat_yaml_is_503_and_logged(monkeypatch, home, capsys):
    # chat.yaml holds names, never values — an unknown key is refused at load.
    _write_chat_config(
        home, "telegram", "secure", ["4242"], extra=["bot_token: pasted-value"]
    )
    monkeypatch.setattr(serve, "vault_environment", lambda: _StubVault(TG_VALUES))
    monkeypatch.setattr(webhooks, "protection_gap", lambda: None)
    monkeypatch.setattr(web_control, "launch_run", _Launches())
    client = TestClient(create_app(home=home, dev_cors=False))
    resp = client.post(
        "/api/v1/chat/telegram",
        content=_tg_body(),
        headers={telegram.SECRET_TOKEN_HEADER: TG_SECRET},
    )
    assert resp.status_code == 503
    assert resp.json()["error"] == "chat is misconfigured on this server"
    assert capsys.readouterr().err.strip() != ""
    assert _thread_ids(str(home / "state.db")) == []
