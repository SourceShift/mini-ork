"""The run page's Messages tab: the concord mailbox of ``run:<id>``.

ContextNest is stubbed at the ``cn_client`` boundary — the tab must never need a
live server to render, and each degraded state must say why it has no rows.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from mini_ork import cn_client
from mini_ork.ide_pages import build_page

_UNIT_DIR = Path(__file__).resolve().parent
if str(_UNIT_DIR) not in sys.path:
    sys.path.insert(0, str(_UNIT_DIR))
from test_ide_pages_run import RUN, _section, _seed, home as _home  # noqa: E402

home = _home

PRINCIPAL = f"run:{RUN}"
OTHER_RUN = "run-1791000500-def456"


def _messages() -> list[dict]:
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


@pytest.fixture
def level_two(monkeypatch) -> None:
    monkeypatch.setenv("MINI_ORK_IDE_SPEC", "2")
    monkeypatch.delenv("MO_DISABLE_CN", raising=False)


def _stub_inbox(monkeypatch, messages: list[dict] | Exception) -> list[tuple]:
    calls: list[tuple] = []

    def fake_inbox(principal_id: str, unacked: bool = True, *, timeout: float | None = None) -> dict:
        calls.append((principal_id, unacked, timeout))
        if isinstance(messages, Exception):
            raise messages
        return {"messages": messages}

    monkeypatch.setattr(cn_client, "available", lambda: True)
    monkeypatch.setattr(cn_client, "coord_inbox", fake_inbox)
    return calls


def test_lists_every_message_newest_first(home: Path, level_two, monkeypatch) -> None:
    calls = _stub_inbox(monkeypatch, _messages())
    _seed(home)
    page = build_page(home, "run", "messages", {"run": RUN})
    assert page["tab"] == "messages"
    # Acked messages included, so the read must ask for the whole mailbox.
    assert calls == [(PRINCIPAL, False, 0.5)]
    section = _section(page, "list", "Concord messages")
    titles = [row["t"] for row in section["items"]]
    assert titles == [
        "loop:s11 · not yet delivered to an agent",
        "human:amir · delivered to sess-0123456 · not acked",
        f"run:{OTHER_RUN} · acked by {PRINCIPAL}",
    ]
    assert [row["m"] for row in section["items"]] == ["✉", "✉", "✓"]
    assert [row["mc"] for row in section["items"]] == ["yellow", "blue", "green"]
    assert section["items"][1]["sub"] == "skip the flaky e2e suite\nit is red on main"
    assert "3 total · 2 not acked · 1 from other runs" in section["note"]
    assert section["actions"][0]["do"] == {"thread": f"/concord send {PRINCIPAL} "}


def test_row_actions_ack_reply_and_open_the_sending_run(home: Path, level_two, monkeypatch) -> None:
    _stub_inbox(monkeypatch, _messages())
    _seed(home)
    rows = _section(build_page(home, "run", "messages", {"run": RUN}), "list", "Concord messages")["items"]
    unacked_human = {a["label"]: a["do"] for a in rows[1]["acts"]}
    assert unacked_human["Ack"] == {"cli": ["concord", "ack", PRINCIPAL, "M-2"], "home": False}
    assert unacked_human["Reply"] == {"thread": "/concord send human:amir "}
    assert "Open run" not in unacked_human
    acked_from_run = {a["label"]: a["do"] for a in rows[2]["acts"]}
    assert "Ack" not in acked_from_run
    assert acked_from_run["Open run"]["run"] == OTHER_RUN


def test_empty_mailbox_says_so(home: Path, level_two, monkeypatch) -> None:
    _stub_inbox(monkeypatch, [])
    _seed(home)
    section = _section(build_page(home, "run", "messages", {"run": RUN}), "list", "Concord messages")
    assert [row["t"] for row in section["items"]] == ["No concord messages were sent to this run"]


def test_contextnest_down_is_named_not_empty(home: Path, level_two, monkeypatch) -> None:
    calls = _stub_inbox(monkeypatch, _messages())
    monkeypatch.setattr(cn_client, "available", lambda: False)
    _seed(home)
    section = _section(build_page(home, "run", "messages", {"run": RUN}), "list", "Concord messages")
    assert section["items"][0]["t"] == "ContextNest is not reachable"
    assert section["actions"] == []
    assert calls == []


def test_disabled_contextnest_never_probes(home: Path, level_two, monkeypatch) -> None:
    monkeypatch.setenv("MO_DISABLE_CN", "1")

    def no_probe() -> bool:
        raise AssertionError("probed ContextNest with MO_DISABLE_CN=1")

    monkeypatch.setattr(cn_client, "available", no_probe)
    _seed(home)
    section = _section(build_page(home, "run", "messages", {"run": RUN}), "list", "Concord messages")
    assert section["items"][0]["t"] == "ContextNest is off"


def test_unregistered_run_explains_the_missing_mailbox(home: Path, level_two, monkeypatch) -> None:
    _stub_inbox(monkeypatch, cn_client.CoordHTTPError(404, "not found"))
    _seed(home)
    section = _section(build_page(home, "run", "messages", {"run": RUN}), "list", "Concord messages")
    assert section["items"][0]["t"] == f"This run has no concord mailbox ({PRINCIPAL})"


def test_refused_read_shows_the_status(home: Path, level_two, monkeypatch) -> None:
    _stub_inbox(monkeypatch, cn_client.CoordHTTPError(401, "missing bearer token"))
    _seed(home)
    item = _section(build_page(home, "run", "messages", {"run": RUN}), "list", "Concord messages")["items"][0]
    assert item["t"] == "ContextNest refused the read"
    assert item["sub"] == "HTTP 401 missing bearer token"


def test_unexpected_failure_costs_the_tab_not_the_page(home: Path, level_two, monkeypatch) -> None:
    _stub_inbox(monkeypatch, ValueError("garbled payload"))
    _seed(home)
    page = build_page(home, "run", "messages", {"run": RUN})
    assert page["ok"] is True
    item = _section(page, "list", "Concord messages")["items"][0]
    assert item["sub"] == "ValueError: garbled payload"


def test_concord_alias_and_level_one_reach_the_tab(home: Path, monkeypatch) -> None:
    _stub_inbox(monkeypatch, [])
    _seed(home)
    monkeypatch.setenv("MINI_ORK_IDE_SPEC", "2")
    assert build_page(home, "run", "concord", {"run": RUN})["tab"] == "messages"
    monkeypatch.delenv("MINI_ORK_IDE_SPEC", raising=False)
    page = build_page(home, "run", "messages", {"run": RUN})
    assert page["tab"] == "messages"
    assert _section(page, "list", "Concord messages")


def test_coord_inbox_timeout_overrides_the_environment(monkeypatch) -> None:
    seen: list[float] = []

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self) -> bytes:
            return b'{"messages": []}'

    def fake_urlopen(request, timeout):
        seen.append(timeout)
        assert request.full_url.endswith("/messages?unacked=false")
        return _Response()

    monkeypatch.setenv("CN_COORD_TIMEOUT_SEC", "3")
    monkeypatch.setattr(cn_client.urllib.request, "urlopen", fake_urlopen)
    assert cn_client.coord_inbox(PRINCIPAL, unacked=False, timeout=0.5) == {"messages": []}
    assert cn_client.coord_inbox(PRINCIPAL, unacked=False) == {"messages": []}
    assert seen == [0.5, 3.0]
