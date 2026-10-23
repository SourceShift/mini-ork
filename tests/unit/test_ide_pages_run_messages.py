"""The run page's Messages tab: the concord mail of ``run:<id>``, both directions.

ContextNest is stubbed at the ``cn_client`` boundary — the tab must never need a
live server to render, and each degraded state must say why it has no rows.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from mini_ork import cn_client
from mini_ork.dispatch.secrets import SecretStoreError
from mini_ork.ide_pages import build_page
from mini_ork.orchestration import concord_run

_UNIT_DIR = Path(__file__).resolve().parent
if str(_UNIT_DIR) not in sys.path:
    sys.path.insert(0, str(_UNIT_DIR))
from test_ide_pages_run import RUN, _section, _seed, home as _home  # noqa: E402

home = _home

PRINCIPAL = f"run:{RUN}"
OTHER_RUN = "run-1791000500-def456"
RECEIVED = "Messages to this run"
SENT = "Messages this run sent"


def _received() -> list[dict]:
    return [
        {"msg_id": "M-1", "principal_id": PRINCIPAL, "from": f"run:{OTHER_RUN}",
         "body": "I own mini_ork/foo.py until 14:00", "created_at": "2026-10-01T10:00:00.000000Z",
         "delivered_at": "2026-10-01T10:00:05.000000Z", "delivered_to": "sess-abcdef1234567890",
         "acked_at": "2026-10-01T10:01:00.000000Z", "acked_by": PRINCIPAL},
        {"msg_id": "M-2", "principal_id": PRINCIPAL, "from": "human:amir",
         "body": "skip the flaky e2e suite\nit is red on main", "created_at": "2026-10-01T11:00:00.000000Z",
         "delivered_at": "2026-10-01T11:00:02.000000Z", "delivered_to": "sess-0123456789abcdef",
         "acked_at": None, "acked_by": None},
        {"msg_id": "M-3", "principal_id": PRINCIPAL, "from": "loop:s11",
         "body": "rebase before publishing", "created_at": "2026-10-01T12:00:00.000000Z",
         "delivered_at": None, "delivered_to": None, "acked_at": None, "acked_by": None},
    ]


def _sent() -> list[dict]:
    return [
        {"msg_id": "M-9", "principal_id": f"run:{OTHER_RUN}", "from": PRINCIPAL,
         "body": "done with mini_ork/foo.py", "created_at": "2026-10-01T13:00:00.000000Z",
         "delivered_at": None, "delivered_to": None, "acked_at": None, "acked_by": None},
        {"msg_id": "M-4", "principal_id": "human:amir", "from": PRINCIPAL,
         "body": "need a decision on the e2e suite", "created_at": "2026-10-01T09:00:00.000000Z",
         "delivered_at": "2026-10-01T09:00:03.000000Z", "delivered_to": "sess-x",
         "acked_at": "2026-10-01T09:05:00.000000Z", "acked_by": "human:amir"},
    ]


@pytest.fixture
def level_two(monkeypatch) -> None:
    monkeypatch.setenv("MINI_ORK_IDE_SPEC", "2")
    monkeypatch.delenv("MO_DISABLE_CN", raising=False)


def _stub(monkeypatch, received: list[dict] | Exception,
          sent: list[dict] | Exception | None = None) -> list[tuple]:
    calls: list[tuple] = []

    def answer(value):
        if isinstance(value, Exception):
            raise value
        return {"messages": value}

    def fake_inbox(principal_id: str, unacked: bool = True, *, timeout: float | None = None) -> dict:
        calls.append(("inbox", principal_id, unacked, timeout))
        return answer(received)

    def fake_outbox(sender: str, limit: int = 200, *, timeout: float | None = None) -> dict:
        calls.append(("outbox", sender, limit, timeout))
        return answer([] if sent is None else sent)

    monkeypatch.setattr(cn_client, "available", lambda: True)
    monkeypatch.setattr(cn_client, "coord_inbox", fake_inbox)
    monkeypatch.setattr(cn_client, "coord_outbox", fake_outbox)
    return calls


def _messages_page(home: Path, tab: str = "messages") -> dict:
    _seed(home)
    return build_page(home, "run", tab, {"run": RUN})


def test_received_lists_every_message_newest_first(home: Path, level_two, monkeypatch) -> None:
    calls = _stub(monkeypatch, _received())
    page = _messages_page(home)
    assert page["tab"] == "messages"
    # One inbox read feeds both the badge and the tab; acked messages included.
    assert [c for c in calls if c[0] == "inbox"] == [("inbox", PRINCIPAL, False, 0.5)]
    section = _section(page, "list", RECEIVED)
    assert [row["t"] for row in section["items"]] == [
        "loop:s11 · not yet delivered to an agent",
        "human:amir · delivered to sess-0123456 · not acked",
        f"run:{OTHER_RUN} · acked by {PRINCIPAL}",
    ]
    assert [row["m"] for row in section["items"]] == ["✉", "✉", "✓"]
    assert [row["mc"] for row in section["items"]] == ["yellow", "blue", "green"]
    assert section["items"][1]["sub"] == "skip the flaky e2e suite\nit is red on main"
    assert "3 total · 2 not acked · 1 from other runs" in section["note"]
    assert section["actions"][0]["do"] == {"thread": f"/concord send {PRINCIPAL} "}


def test_received_row_actions(home: Path, level_two, monkeypatch) -> None:
    _stub(monkeypatch, _received())
    rows = _section(_messages_page(home), "list", RECEIVED)["items"]
    unacked_human = {a["label"]: a["do"] for a in rows[1]["acts"]}
    assert unacked_human["Ack"] == {"cli": ["concord", "ack", PRINCIPAL, "M-2"], "home": False}
    assert unacked_human["Reply"] == {"thread": "/concord send human:amir "}
    assert "Open run" not in unacked_human
    acked_from_run = {a["label"]: a["do"] for a in rows[2]["acts"]}
    assert "Ack" not in acked_from_run
    assert acked_from_run["Open run"]["run"] == OTHER_RUN


def test_sent_lists_what_the_run_wrote(home: Path, level_two, monkeypatch) -> None:
    calls = _stub(monkeypatch, _received(), _sent())
    page = _messages_page(home)
    assert ("outbox", PRINCIPAL, 200, 0.5) in calls
    section = _section(page, "list", SENT)
    assert [row["t"] for row in section["items"]] == [
        f"→ run:{OTHER_RUN} · not yet delivered to an agent",
        "→ human:amir · acked by human:amir",
    ]
    assert "2 total · 1 to other runs" in section["note"]
    to_run = {a["label"]: a["do"] for a in section["items"][0]["acts"]}
    # The recipient acks its own mail; the sender can only go and look.
    assert list(to_run) == ["Open run"] and to_run["Open run"]["run"] == OTHER_RUN
    assert section["items"][1]["acts"] == []


def test_old_contextnest_without_the_outbox_route_says_so(home: Path, level_two, monkeypatch) -> None:
    _stub(monkeypatch, _received(), cn_client.CoordHTTPError(404, ""))
    page = _messages_page(home)
    assert len(_section(page, "list", RECEIVED)["items"]) == 3
    assert _section(page, "list", SENT)["items"][0]["t"] == "This ContextNest cannot list sent messages"


def test_empty_mail_says_so(home: Path, level_two, monkeypatch) -> None:
    _stub(monkeypatch, [], [])
    page = _messages_page(home)
    assert [r["t"] for r in _section(page, "list", RECEIVED)["items"]] == [
        "No concord messages were sent to this run"]
    assert [r["t"] for r in _section(page, "list", SENT)["items"]] == [
        "This run sent no concord messages"]


def test_tab_badge_counts_received_and_flags_unacked(home: Path, level_two, monkeypatch) -> None:
    _stub(monkeypatch, _received())
    tabs = {t["key"]: t for t in _messages_page(home, "dag")["tabs"]}
    assert (tabs["messages"]["count"], tabs["messages"]["tone"]) == (3, "yellow")


def test_tab_badge_is_quiet_once_everything_is_acked(home: Path, level_two, monkeypatch) -> None:
    _stub(monkeypatch, _received()[:1])
    tabs = {t["key"]: t for t in _messages_page(home, "dag")["tabs"]}
    assert (tabs["messages"]["count"], tabs["messages"]["tone"]) == (1, "sub")


def test_no_badge_when_the_mailbox_could_not_be_read(home: Path, level_two, monkeypatch) -> None:
    _stub(monkeypatch, _received())
    monkeypatch.setattr(cn_client, "available", lambda: False)
    tabs = {t["key"]: t for t in _messages_page(home, "dag")["tabs"]}
    assert "count" not in tabs["messages"] and "tone" not in tabs["messages"]


def test_contextnest_down_is_named_once(home: Path, level_two, monkeypatch) -> None:
    calls = _stub(monkeypatch, _received(), _sent())
    monkeypatch.setattr(cn_client, "available", lambda: False)
    page = _messages_page(home)
    lists = [s for s in page["sections"] if s["type"] == "list"]
    assert [s["title"] for s in lists] == [RECEIVED]
    assert lists[0]["items"][0]["t"] == "ContextNest is not reachable"
    assert lists[0]["actions"] == []
    assert calls == []


def test_disabled_contextnest_never_probes(home: Path, level_two, monkeypatch) -> None:
    monkeypatch.setenv("MO_DISABLE_CN", "1")

    def no_probe() -> bool:
        raise AssertionError("probed ContextNest with MO_DISABLE_CN=1")

    monkeypatch.setattr(cn_client, "available", no_probe)
    assert _section(_messages_page(home), "list", RECEIVED)["items"][0]["t"] == "ContextNest is off"


def test_unregistered_run_explains_the_missing_mailbox(home: Path, level_two, monkeypatch) -> None:
    _stub(monkeypatch, cn_client.CoordHTTPError(404, "not found"))
    item = _section(_messages_page(home), "list", RECEIVED)["items"][0]
    assert item["t"] == f"This run has no concord mailbox ({PRINCIPAL})"


def test_refused_read_shows_the_status(home: Path, level_two, monkeypatch) -> None:
    _stub(monkeypatch, cn_client.CoordHTTPError(401, "missing bearer token"))
    item = _section(_messages_page(home), "list", RECEIVED)["items"][0]
    assert item["t"] == "ContextNest refused the read"
    assert item["sub"] == "HTTP 401 missing bearer token"


def test_missing_secure_tier_token_names_the_key(home: Path, level_two, monkeypatch) -> None:
    _stub(monkeypatch, cn_client.CoordConfigError(
        "secret store is missing required key 'CN_TOKEN' for the secure CN tier"))
    item = _section(_messages_page(home), "list", RECEIVED)["items"][0]
    assert item["t"] == "ContextNest is misconfigured"
    assert "'CN_TOKEN'" in item["sub"]


def test_unexpected_failure_costs_the_tab_not_the_page(home: Path, level_two, monkeypatch) -> None:
    _stub(monkeypatch, ValueError("garbled payload"))
    page = _messages_page(home)
    assert page["ok"] is True
    item = _section(page, "list", RECEIVED)["items"][0]
    assert item["t"] == "Could not read this run's messages"
    assert item["sub"] == "ValueError: garbled payload"
    # Every other tab still builds, without a messages badge.
    dag = build_page(home, "run", "dag", {"run": RUN})
    assert dag["ok"] is True
    assert "count" not in {t["key"]: t for t in dag["tabs"]}["messages"]


def test_concord_alias_and_level_one_reach_the_tab(home: Path, monkeypatch) -> None:
    _stub(monkeypatch, [], [])
    _seed(home)
    monkeypatch.setenv("MINI_ORK_IDE_SPEC", "2")
    assert build_page(home, "run", "concord", {"run": RUN})["tab"] == "messages"
    monkeypatch.delenv("MINI_ORK_IDE_SPEC", raising=False)
    page = build_page(home, "run", "messages", {"run": RUN})
    assert page["tab"] == "messages"
    assert _section(page, "list", RECEIVED) and _section(page, "list", SENT)


# ── cn_client: the coord seam ──────────────────────────────────────────────


class _Response:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self) -> bytes:
        return b'{"messages": []}'


def _capture_urlopen(monkeypatch) -> list:
    seen: list = []

    def fake_urlopen(request, timeout):
        seen.append((request, timeout))
        return _Response()

    monkeypatch.setattr(cn_client.urllib.request, "urlopen", fake_urlopen)
    return seen


def test_coord_inbox_timeout_overrides_the_environment(monkeypatch) -> None:
    seen = _capture_urlopen(monkeypatch)
    monkeypatch.setenv("CN_COORD_TIMEOUT_SEC", "3")
    assert cn_client.coord_inbox(PRINCIPAL, unacked=False, timeout=0.5) == {"messages": []}
    assert cn_client.coord_inbox(PRINCIPAL, unacked=False) == {"messages": []}
    assert [timeout for _, timeout in seen] == [0.5, 3.0]
    assert seen[0][0].full_url.endswith("/messages?unacked=false")


def test_coord_outbox_lists_by_sender(monkeypatch) -> None:
    seen = _capture_urlopen(monkeypatch)
    cn_client.coord_outbox(PRINCIPAL, 50, timeout=0.5)
    request, timeout = seen[0]
    assert request.full_url.endswith(f"/api/v1/coord/messages?from=run%3A{RUN}&limit=50")
    assert request.get_method() == "GET" and timeout == 0.5


def test_coord_requests_carry_the_secure_tier_bearer(monkeypatch) -> None:
    seen = _capture_urlopen(monkeypatch)
    monkeypatch.setattr(cn_client, "_auth_headers", lambda: {"Authorization": "Bearer t0k"})
    cn_client.coord_send(PRINCIPAL, "human:amir", "hello")
    request, _ = seen[0]
    assert request.get_header("Authorization") == "Bearer t0k"
    assert request.get_header("Content-type") == "application/json"


def test_missing_token_raises_a_config_error_the_callers_already_catch(monkeypatch) -> None:
    _capture_urlopen(monkeypatch)

    def missing() -> dict:
        raise SecretStoreError("secret store is missing required key 'CN_TOKEN'")

    monkeypatch.setattr(cn_client, "_auth_headers", missing)
    with pytest.raises(cn_client.CoordConfigError, match="'CN_TOKEN'") as caught:
        cn_client.coord_inbox(PRINCIPAL)
    assert isinstance(caught.value, cn_client.CoordUnavailable)


def test_run_registration_names_the_missing_token_and_fails_open(monkeypatch, capsys) -> None:
    def refuse(principal_id: str, fields: dict) -> dict:
        raise cn_client.CoordConfigError("secret store is missing required key 'CN_TOKEN'")

    monkeypatch.setattr(cn_client, "coord_upsert_principal", refuse)
    monkeypatch.delenv("CONCORD_PRINCIPAL", raising=False)
    monkeypatch.delenv("MO_CONCORD", raising=False)
    handle = concord_run.start("my-run", "recipe-x", "/abs/kickoff.md")
    try:
        assert handle is not None and handle.thread is None
        assert "missing required key 'CN_TOKEN' — run not registered" in capsys.readouterr().err
    finally:
        concord_run.stop(handle)
