"""Tests for ``mini_ork.integrations.serve`` and the ``threads serve`` verb.

Every test is hermetic: no socket, no platform API, no process. The vault is a
plain object with ``resolve``/``describe``; a real Telegram poll reads through a
monkeypatched ``urlopen``; and the process launcher (``launch_run``) is a
recorder, so a "launch" is an assertion rather than a side effect.

What is pinned beyond the happy path: ``chat.yaml`` refuses anything but its
four keys (and never echoes a value), the allowlist gates a message *before*
the thread state machine, a missing secret stops the CLI before any network,
and the loop threads Telegram's cursor, backs off on errors and refuses to loop
Discord's Gateway.
"""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from mini_ork.integrations import discord, serve, slack, telegram  # noqa: E402
from mini_ork.integrations._base import PollResult  # noqa: E402
from mini_ork.stores import migrate as mig  # noqa: E402
from mini_ork.threads import cli as threads_cli, store  # noqa: E402
from mini_ork.threads.service import Delivery  # noqa: E402


class Env:
    """The vault stand-in: ``resolve(key)`` -> value or ``None``, ``describe()``."""

    def __init__(self, mapping=None):
        self._mapping = dict(mapping or {})

    def resolve(self, key):
        return self._mapping.get(key)

    def describe(self):
        return "the unit-test secret env"


class Recorder:
    def __init__(self, result=None):
        self.calls = []
        self.result = result

    def __call__(self, *args, **kwargs):
        self.calls.append({"args": args, "kwargs": kwargs})
        return self.result


class StubTransport:
    """``name`` + a scripted ``poll_once``: each entry is a result or exception."""

    def __init__(self, name, script=()):
        self.name = name
        self._script = list(script)
        self.calls = []

    def poll_once(self, **kwargs):
        self.calls.append(kwargs)
        step = self._script.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step


class _NoSleep:
    def __init__(self):
        self.calls = []

    def __call__(self, seconds):
        self.calls.append(seconds)


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch):
    for name in ("MINI_ORK_DB", "MINI_ORK_CHAT", "MINI_ORK_HOME"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def home(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    rc, _out, _err = mig.init_db(db=str(home / "state.db"), root=str(REPO))
    assert rc == 0
    return home


@pytest.fixture
def db(home):
    return str(home / "state.db")


@pytest.fixture
def launches(monkeypatch):
    import mini_ork.web.control as control

    rec = Recorder(result={"ok": True, "kickoff_path": None})
    monkeypatch.setattr(control, "launch_run", rec)
    monkeypatch.setattr(control, "steer_run", Recorder(result=None))
    return rec


def write_config(home, **doc):
    path = Path(home) / "config" / "chat.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    return path


def _config(home, **overrides):
    doc = {"transport": "telegram", "tier": "simple", "allow_channels": ["100"]}
    doc.update(overrides)
    write_config(home, **doc)
    return serve.load_chat_config(home)


def _delivery(thread_id="telegram-1", run_id="run-9", action="launched"):
    return Delivery(
        thread_id=thread_id, action=action, turn=1, message_id=1, run_id=run_id
    )


class _FakeResponse:
    def __init__(self, body):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


# -- config ----------------------------------------------------------------


def test_no_file_is_none_but_explicit_missing_path_raises(home, tmp_path):
    assert serve.load_chat_config(home) is None

    missing = tmp_path / "nope" / "chat.yaml"
    with pytest.raises(serve.ChatConfigError) as excinfo:
        serve.load_chat_config(home, environ={"MINI_ORK_CHAT": str(missing)})
    assert str(missing) in str(excinfo.value)
    assert "does not exist" in str(excinfo.value)


def test_valid_file_parses_and_integer_ids_become_str(home):
    write_config(
        home,
        transport="slack",
        tier="simple",
        allow_channels=[123456789, "C000111"],
    )
    config = serve.load_chat_config(home)
    assert config.transport == "slack"
    assert config.tier == "simple"
    assert config.recipe is None
    assert config.allow_channels == frozenset({"123456789", "C000111"})
    assert all(isinstance(item, str) for item in config.allow_channels)


@pytest.mark.parametrize(
    "doc",
    [
        {"transport": "irc", "tier": "simple", "allow_channels": ["1"]},
        {"transport": "telegram", "tier": "  secure ", "allow_channels": ["1"]},
        {"transport": "telegram", "tier": "simple"},
        {"transport": "telegram", "tier": "simple", "allow_channels": []},
        {"transport": "telegram", "tier": "simple", "allow_channels": ["   "]},
    ],
)
def test_bad_config_is_refused(home, doc):
    write_config(home, **doc)
    with pytest.raises(serve.ChatConfigError):
        serve.load_chat_config(home)


def test_unknown_key_names_the_key_never_the_value(home):
    write_config(
        home,
        transport="telegram",
        tier="simple",
        allow_channels=["1"],
        bot_token="s3cr3t",
    )
    with pytest.raises(serve.ChatConfigError) as excinfo:
        serve.load_chat_config(home)
    message = str(excinfo.value)
    assert "unknown key" in message
    assert "'bot_token'" in message
    assert "s3cr3t" not in message


# -- secrets and transport construction -----------------------------------


def test_missing_secret_names_key_and_fails_closed(home):
    config = _config(home)
    with pytest.raises(serve.ChatConfigError) as excinfo:
        serve.resolve_secrets(config, Env({}))
    message = str(excinfo.value)
    assert serve.TELEGRAM_BOT_TOKEN_KEY in message
    assert "never falls back to an empty secret" in message


def test_build_transport_telegram_passes_token_through(home, db):
    config = _config(home, recipe="code-fix")
    transport = serve.build_transport(
        config,
        environment=Env({serve.TELEGRAM_BOT_TOKEN_KEY: "800813:spec-token"}),
        home=home,
        db=db,
    )
    assert isinstance(transport, telegram.TelegramTransport)
    assert isinstance(transport, serve._GatedTelegram)
    assert transport.token == "800813:spec-token"
    assert "800813:spec-token" not in repr(transport)
    assert transport._recipe == "code-fix"
    assert transport._allow == frozenset({"100"})


def test_build_transport_slack_secure_requires_signing_secret(home, db):
    config = _config(home, transport="slack", tier="secure", allow_channels=["C1"])
    with pytest.raises(serve.ChatConfigError) as excinfo:
        serve.build_transport(
            config,
            environment=Env(
                {
                    slack.BOT_TOKEN_VAULT_KEY: "xoxb-supplied",
                    slack.APP_TOKEN_VAULT_KEY: "xapp-supplied",
                }
            ),
            home=home,
            db=db,
        )
    message = str(excinfo.value)
    assert slack.SIGNING_SECRET_VAULT_KEY in message
    assert "xoxb-supplied" not in message
    assert "xapp-supplied" not in message


@pytest.mark.parametrize("name", ["slack", "discord"])
def test_simple_tier_sets_socket_idle_timeout(home, db, name):
    keys = {
        "slack": {
            slack.BOT_TOKEN_VAULT_KEY: "xoxb-spec",
            slack.APP_TOKEN_VAULT_KEY: "xapp-spec",
        },
        "discord": {discord.BOT_TOKEN_VAULT_KEY: "Bot discord-spec"},
    }
    config = _config(home, transport=name, tier="simple", allow_channels=["C1"])
    transport = serve.build_transport(config, environment=Env(keys[name]), home=home, db=db)
    assert transport._ws_timeout == serve.SOCKET_IDLE_SECONDS


# -- allowlist -------------------------------------------------------------


def test_telegram_allowlist_through_a_real_poll(home, db, launches):
    config = _config(home, allow_channels=["100"])
    transport = serve.build_transport(
        config, environment=Env({serve.TELEGRAM_BOT_TOKEN_KEY: "8001:spec"}),
        home=home, db=db,
    )

    payload = {
        "ok": True,
        "result": [
            {
                "update_id": 5,
                "message": {
                    "message_id": 1,
                    "text": "please run the tests",
                    "chat": {"id": 100, "title": "allowed"},
                },
            },
            {
                "update_id": 6,
                "message": {"message_id": 2, "text": "let me in", "chat": {"id": 200}},
            },
        ],
    }
    original = telegram.urllib.request.urlopen
    telegram.urllib.request.urlopen = (
        lambda request, timeout=None: _FakeResponse(json.dumps(payload).encode())
    )
    try:
        result = transport.poll_once(offset=None, home=home, db=db)
    finally:
        telegram.urllib.request.urlopen = original

    assert len(launches.calls) == 1
    assert store.get_thread("telegram-100", db=db) is not None
    assert store.get_thread("telegram-200", db=db) is None
    assert result.next_offset == 7


def test_gated_discord_denied_channel(home, db, launches):
    gated = serve._GatedDiscord(allow=frozenset({"111"}), bot_token="Bot direct")
    result = gated._deliver({"id": "m1"}, channel="222", text="hi", home=home, db=db)
    assert result.action == "ignored"
    assert store.get_thread("discord-222", db=db) is None
    assert launches.calls == []


# -- the loop --------------------------------------------------------------


def test_once_writes_one_line_per_delivered_batch_result(home, db):
    ignored = PollResult(action="ignored", update_id=4)
    acted = PollResult(action="launched", update_id=5, delivery=_delivery())
    result = PollResult(
        action="launched", update_id=5, delivery=_delivery(), batch=(ignored, acted)
    )
    transport = StubTransport("telegram", [result])
    out, err = io.StringIO(), io.StringIO()
    rc = serve.serve(
        transport, home=home, db=db, once=True, sleep=_NoSleep(), out=out, err=err
    )
    assert rc == 0
    assert len(transport.calls) == 1
    assert out.getvalue() == "launched: thread telegram-1 run run-9\n"
    assert "chat: serving telegram (simple tier) into" in err.getvalue()


def test_telegram_cursor_threading(home, db):
    transport = StubTransport(
        "telegram",
        [PollResult(update_id=5), PollResult(update_id=9), KeyboardInterrupt()],
    )
    err = io.StringIO()
    rc = serve.serve(
        transport, home=home, db=db, sleep=_NoSleep(), out=io.StringIO(), err=err
    )
    assert rc == 0
    assert [call.get("offset") for call in transport.calls] == [None, 6, 10]
    assert "chat: stopped" in err.getvalue()


def test_backoff_schedule_and_timeout_is_quiet(home, db):
    transport = StubTransport(
        "telegram",
        [OSError("boom-a"), OSError("boom-b"), TimeoutError(), KeyboardInterrupt()],
    )
    sleeper = _NoSleep()
    err = io.StringIO()
    rc = serve.serve(
        transport, home=home, db=db, sleep=sleeper, out=io.StringIO(), err=err
    )
    assert rc == 0
    assert sleeper.calls == [1.0, 2.0]
    assert err.getvalue().count("tick failed") == 2


def test_once_error_returns_1_without_sleeping(home, db):
    transport = StubTransport("telegram", [OSError("boom-once")])
    sleeper = _NoSleep()
    rc = serve.serve(
        transport, home=home, db=db, once=True, sleep=sleeper,
        out=io.StringIO(), err=io.StringIO(),
    )
    assert rc == 1
    assert sleeper.calls == []


def test_discord_loop_is_refused(home, db):
    transport = StubTransport("discord", [KeyboardInterrupt()])
    err = io.StringIO()
    rc = serve.serve(
        transport, home=home, db=db, sleep=_NoSleep(), out=io.StringIO(), err=err
    )
    assert rc == 2
    assert transport.calls == []
    assert "the Discord Gateway tier opens a new session" in err.getvalue()
    assert "--once" in err.getvalue()


def test_slack_tick_passes_the_frame_limit(home, db):
    transport = StubTransport("slack", [PollResult()])
    serve.serve(
        transport, home=home, db=db, once=True, sleep=_NoSleep(),
        out=io.StringIO(), err=io.StringIO(),
    )
    assert transport.calls[0] == {
        "home": home, "db": db, "limit": serve.SOCKET_FRAMES_PER_TICK,
    }


# -- the CLI verb ----------------------------------------------------------


def _run_cli(monkeypatch, capsys, argv):
    monkeypatch.chdir("/")
    rc = threads_cli.main(argv)
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


def test_help_lists_the_serve_verb(monkeypatch, capsys):
    rc, out, _err = _run_cli(monkeypatch, capsys, ["help"])
    assert rc == 0
    assert "mini-ork threads serve [--transport T] [--once]" in out
    assert "docs/operator/chat.md" in out


def test_no_config_exit_2(monkeypatch, capsys, home):
    rc, _out, err = _run_cli(monkeypatch, capsys, ["serve", "--home", str(home)])
    assert rc == 2
    assert str(Path(home) / "config" / "chat.yaml") in err
    assert "config/chat.example.yaml" in err


def test_mismatched_transport_and_secure_tier_exit_2(
    monkeypatch, capsys, home
):
    write_config(home, transport="telegram", tier="simple", allow_channels=["1"])
    rc, _out, err = _run_cli(
        monkeypatch, capsys, ["serve", "--home", str(home), "--transport", "slack"]
    )
    assert rc == 2
    assert "telegram" in err and "slack" in err

    write_config(home, transport="telegram", tier="secure", allow_channels=["1"])
    rc, _out, err = _run_cli(monkeypatch, capsys, ["serve", "--home", str(home)])
    assert rc == 2
    assert "tier: secure" in err
    assert "/api/v1/chat/telegram" in err


def test_missing_secret_exits_2_before_serve(
    monkeypatch, capsys, home
):
    write_config(home, transport="telegram", tier="simple", allow_channels=["1"])
    monkeypatch.setattr(serve, "vault_environment", lambda: Env({}))
    recorder = Recorder(result=0)
    monkeypatch.setattr(serve, "serve", recorder)

    rc, _out, err = _run_cli(monkeypatch, capsys, ["serve", "--home", str(home)])
    assert rc == 2
    assert serve.TELEGRAM_BOT_TOKEN_KEY in err
    assert recorder.calls == []


def test_happy_path_wiring(monkeypatch, capsys, home):
    write_config(home, transport="telegram", tier="simple", allow_channels=["1"])
    monkeypatch.chdir("/")
    transport = StubTransport("telegram", [PollResult()])
    build = Recorder(result=transport)
    monkeypatch.setattr(serve, "build_transport", build)

    rc = threads_cli.main(["serve", "--once", "--home", str(home)])
    capsys.readouterr()

    assert rc == 0
    resolved_home = Path(home).resolve()
    assert len(build.calls) == 1
    assert Path(build.calls[0]["kwargs"]["home"]) == resolved_home
    assert Path(build.calls[0]["kwargs"]["db"]) == resolved_home / "state.db"
    assert transport.calls
    assert Path(transport.calls[0]["home"]) == resolved_home
    assert Path(transport.calls[0]["db"]) == resolved_home / "state.db"
