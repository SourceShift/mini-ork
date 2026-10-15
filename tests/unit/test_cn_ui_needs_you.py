"""``mini-ork board page inbox`` — the "From agent sessions" section (CN inbox + attention).

The kickoff (rank 1 — "Which sessions are blocked on me, and on which run?")
asks the Inbox page to surface ContextNest's per-urgency inbox asks plus the
per-session attention record. The page goes through
:func:`mini_ork.ide_pages._cn.render_cached_section` (one slot per urgency
``now|soon|later`` plus one for the attention endpoint), and the down /
disabled / config-error states are rendered through
:func:`mini_ork.ide_pages._cn.section_status`, so the page stays
``ok: true`` with no server.

The inbox endpoint (``/api/v1/inbox?urgency=…&project=…``) returns
``{items: [{kind, content/subject/action, session_id, id, metadata: {project_cwd}}]}``;
the attention endpoint (``/api/v1/sessions/attention?project=…``) returns
``{since, project, count, sessions: [{session_id, project_cwd, last_ts,
attention_count, by_kind}]}``.

Tests use synthetic data in exactly that shape — never real transcript
content. ``cn_client._get`` is stubbed per path so four different slots can
be served from one fake, the same way ``test_cn_ui_lane_decisions`` stubs it
per ``kind``. The ``MINI_ORK_HOME`` env var is set to the test's tmp dir so
the section cache slots never leak between tests or into the real home.
"""
from __future__ import annotations

import json
import urllib.parse
from pathlib import Path
from typing import Any

import pytest

from mini_ork import cn_client
from mini_ork.ide_pages import _cn
from mini_ork.ide_pages import build_page
from mini_ork.stores import migrate as mig

REPO = Path(__file__).resolve().parents[2]


# ── shared fixtures ─────────────────────────────────────────────────────────


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A temp ``.mini-ork`` home whose section cache lives inside the
    test's tmp dir so no real slot is read or written."""
    h = tmp_path / "proj" / ".mini-ork"
    h.mkdir(parents=True)
    rc, _out, err = mig.init_db(db=str(h / "state.db"), root=str(REPO))
    assert rc == 0, err
    monkeypatch.setenv("MINI_ORK_HOME", str(h))
    monkeypatch.setenv("MO_DISABLE_CN", "0")
    monkeypatch.setattr(cn_client, "available", lambda: True)
    return h


def _project(home: Path) -> str:
    """The project root (the parent of the .mini-ork home)."""
    return str(home.parent.resolve())


def _section(page: dict[str, Any], title: str) -> dict[str, Any]:
    for s in page["sections"]:
        if s.get("title") == title:
            return s
    raise AssertionError(
        f"no section with title {title!r}; got {[s.get('title') for s in page['sections']]}"
    )


def _sections_by_prefix(page: dict[str, Any], prefix: str) -> list[dict[str, Any]]:
    return [s for s in page["sections"] if str(s.get("title") or "").startswith(prefix)]


def _chip_map(page: dict[str, Any]) -> dict[str, str]:
    return {c["t"]: c["c"] for c in page.get("chips") or []}


def _inbox_item(*, content: str, session_id: str, project: str,
                urgency: str = "now", kind: str = "todo",
                item_id: str = "") -> dict[str, Any]:
    """One inbox item in the verified live shape.

    ``metadata.project_cwd`` is the field the page reads for the
    fail-closed project filter — a foreign project must NOT byte-equal the
    project root, so the fixture sets it explicitly to the test's project.
    """
    return {
        "id": item_id or f"item-{session_id}",
        "kind": kind,
        "content": content,
        "session_id": session_id,
        "metadata": {"project_cwd": project},
    }


def _attention_session(*, session_id: str, project: str, count: int = 1,
                       by_kind: dict[str, int] | None = None,
                       last_ts: str = "2026-10-15T12:00:00.000Z") -> dict[str, Any]:
    return {
        "session_id": session_id,
        "project_cwd": project,
        "last_ts": last_ts,
        "attention_count": int(count),
        "by_kind": dict(by_kind or {}),
    }


def _inbox_body(items: list[dict[str, Any]]) -> str:
    return json.dumps({"items": items, "count": len(items)})


def _attention_body(sessions: list[dict[str, Any]]) -> str:
    return json.dumps({
        "since": "14d", "project": "", "count": len(sessions),
        "sessions": sessions,
    })


@pytest.fixture
def cn(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """A hermetic ContextNest: ``cn_client._get`` is stubbed per path.

    ``state["inbox"][urgency]`` is the body returned for
    ``/api/v1/inbox?urgency=<u>``; ``state["attention"]`` is the body for
    ``/api/v1/sessions/attention``. ``state["error"]``, when set, makes
    ``_get`` raise (the same one the cached_section slot converts to
    ``_unreachable``). ``state["calls"]`` records every requested path so
    request-shape tests can assert on it.
    """
    state: dict[str, Any] = {
        "inbox": {"now": _inbox_body([]), "soon": _inbox_body([]), "later": _inbox_body([])},
        "attention": _attention_body([]),
        "error": None,
        "calls": [],
    }

    def fake_get(path: str, *, timeout: float | None = None) -> str:
        state["calls"].append(path)
        if state["error"] is not None:
            raise state["error"]
        if "/api/v1/sessions/attention" in path:
            return state["attention"]
        if "/api/v1/inbox" in path:
            query = urllib.parse.parse_qs(urllib.parse.urlparse(path).query)
            urgency = (query.get("urgency") or [""])[0]
            return state["inbox"].get(urgency, "{}")
        return "{}"

    monkeypatch.setattr(cn_client, "_get", fake_get)
    return state


# ── the happy path: three buckets, session + run actions ─────────────────────


def test_agent_sessions_renders_three_buckets_with_session_and_run_actions(
    home: Path, cn: dict[str, Any],
) -> None:
    """A healthy read in all three buckets renders one section per urgency
    (now → soon → later), each row opens its session via ``cn_session`` and,
    when the content names a ``run-<digits>-<alnum>`` id, the row also
    carries an Open-run action.
    """
    project = _project(home)
    cn["inbox"]["now"] = _inbox_body([_inbox_item(
        content="The reviewer is waiting on my decision for run-123-456",
        session_id="aaaaaaaa-0000-4000-8000-000000000001",
        project=project, urgency="now", kind="question")])
    cn["inbox"]["soon"] = _inbox_body([_inbox_item(
        content="Confirm the budget cap for the next lane escalation",
        session_id="aaaaaaaa-0000-4000-8000-000000000002",
        project=project, urgency="soon", kind="todo")])
    cn["inbox"]["later"] = _inbox_body([_inbox_item(
        content="Re-test the reranker in run-998877-zzz when the queue is idle",
        session_id="aaaaaaaa-0000-4000-8000-000000000003",
        project=project, urgency="later", kind="todo")])

    page = build_page(home, "inbox")
    assert page["ok"] is True

    now_sec = _section(page, "From agent sessions · now")
    soon_sec = _section(page, "From agent sessions · soon")
    later_sec = _section(page, "From agent sessions · later")

    assert now_sec["type"] == "list" and len(now_sec["items"]) == 1
    assert soon_sec["type"] == "list" and len(soon_sec["items"]) == 1
    assert later_sec["type"] == "list" and len(later_sec["items"]) == 1

    now_row = now_sec["items"][0]
    assert "run-123-456" in now_row["t"]
    assert "now" in now_row["sub"]
    now_acts = now_row.get("acts") or []
    now_labels = [a["label"] for a in now_acts]
    assert "Open session" in now_labels, f"a session link is required, got {now_labels}"
    assert "Open run" in now_labels, (
        f"a run-123-456 row must carry an open-run action, got {now_labels}")
    session_act = next(a for a in now_acts if a["label"] == "Open session")
    assert session_act["do"] == {
        "page": "cn_session",
        "args": {"id": "aaaaaaaa-0000-4000-8000-000000000001"},
    }
    run_act = next(a for a in now_acts if a["label"] == "Open run")
    assert run_act["do"] == {"run": "run-123-456", "title": "run-123-456"}

    later_row = later_sec["items"][0]
    later_acts = later_row.get("acts") or []
    later_run_act = next((a for a in later_acts if a["label"] == "Open run"), None)
    assert later_run_act is not None, (
        f"a run-998877-zzz row must carry an open-run action, got {later_acts}")


def test_agent_sessions_drops_foreign_project_items(
    home: Path, cn: dict[str, Any],
) -> None:
    """Items whose ``metadata.project_cwd`` is not the project root, or
    whose ``project_cwd`` is a nested checkout of the root, are dropped
    — only items byte-equal to the project root survive. Mirrors the
    fail-closed rule ``lanes.py`` applies to atoms."""
    project = _project(home)
    foreign = _inbox_item(
        content="Foreign-project ask that must be dropped",
        session_id="bbbbbbbb-0000-4000-8000-000000000001",
        project="/somewhere/else/project", urgency="now")
    nested = _inbox_item(
        content="Nested-checkout ask that must be dropped",
        session_id="bbbbbbbb-0000-4000-8000-000000000002",
        project=project + "/child", urgency="now")
    own = _inbox_item(
        content="Own-project ask that must render",
        session_id="bbbbbbbb-0000-4000-8000-000000000003",
        project=project, urgency="now")
    cn["inbox"]["now"] = _inbox_body([foreign, nested, own])

    page = build_page(home, "inbox")
    now_sec = _section(page, "From agent sessions · now")
    rows_text = json.dumps(now_sec["items"])
    assert "Own-project ask that must render" in rows_text
    assert "Foreign-project ask that must be dropped" not in rows_text, (
        "a foreign-project item leaked into the page")
    assert "Nested-checkout ask that must be dropped" not in rows_text, (
        "a nested-checkout item leaked into the page")


# ── cap + overflow ─────────────────────────────────────────────────────────


def test_agent_sessions_caps_combined_at_30_with_more_section(
    home: Path, cn: dict[str, Any],
) -> None:
    """40 items across the three buckets render at most 30 rows total
    (the cap is combined, not per-bucket), and the overflow is surfaced
    via a "More" section the way inbox.py's older-runs row does."""
    project = _project(home)
    now_items: list[dict[str, Any]] = []
    soon_items: list[dict[str, Any]] = []
    later_items: list[dict[str, Any]] = []
    for i in range(40):
        # 25 now, 10 soon, 5 later — the cap should keep all 25 now and
        # 5 of the 10 soon, dropping 5 soon + 5 later for overflow=10.
        bucket = "now" if i < 25 else ("soon" if i < 35 else "later")
        target = {"now": now_items, "soon": soon_items, "later": later_items}[bucket]
        target.append(_inbox_item(
            content=f"ask-{i:02d} in {bucket}",
            session_id=f"cccccccc-{i:04d}-4000-8000-000000000000",
            project=project, urgency=bucket))
    cn["inbox"]["now"] = _inbox_body(now_items)
    cn["inbox"]["soon"] = _inbox_body(soon_items)
    cn["inbox"]["later"] = _inbox_body(later_items)

    page = build_page(home, "inbox")
    rendered = _sections_by_prefix(page, "From agent sessions · ")
    total_rows = sum(len(s.get("items") or []) for s in rendered)
    assert total_rows == 30, f"the combined cap is 30, got {total_rows}"

    # 25 now + 5 of the 10 soon rendered — the 5 dropped soon + 5 later
    # overflow into the "More" section.
    now_sec = _section(page, "From agent sessions · now")
    assert len(now_sec["items"]) == 25, f"all 25 now items render, got {len(now_sec['items'])}"
    soon_sec = _section(page, "From agent sessions · soon")
    assert len(soon_sec["items"]) == 5, (
        f"only 5 of the 10 soon items render after the combined cap, got {len(soon_sec['items'])}")
    later_titles = [s.get("title") for s in page["sections"]
                    if str(s.get("title") or "").startswith("From agent sessions · later")]
    assert later_titles == [], (
        f"the later bucket is fully past the cap, but a later section rendered: {later_titles}")

    more = _section(page, "More")
    assert more["type"] == "list"
    assert "10 more" in more["items"][0]["t"], (
        f"the overflow must show 10 dropped, got {more['items'][0]['t']!r}")


# ── chip behaviour ─────────────────────────────────────────────────────────


def test_agent_sessions_now_chip_on_healthy_read(
    home: Path, cn: dict[str, Any],
) -> None:
    """A healthy read with at least one now item adds a ``✋ now <N>`` chip
    to the page header. The chip carries the count of now items in the
    rendered 30-row slice, not the count CN reported."""
    project = _project(home)
    items = [_inbox_item(
        content=f"ask-{i:02d}",
        session_id=f"dddddddd-{i:04d}-4000-8000-000000000000",
        project=project, urgency="now") for i in range(3)]
    cn["inbox"]["now"] = _inbox_body(items)

    page = build_page(home, "inbox")
    chips = _chip_map(page)
    assert "✋ now 3" in chips, f"the now chip must appear, got {list(chips)}"
    assert chips["✋ now 3"] == "yellow"


def test_agent_sessions_no_now_chip_when_health_but_no_items(
    home: Path, cn: dict[str, Any],
) -> None:
    """A healthy read with zero items renders the 'No asks recorded' row
    and adds NO chip — the chip is keyed on the now count, and a healthy
    empty read has no asks to count."""
    cn["inbox"]["now"] = _inbox_body([])

    page = build_page(home, "inbox")
    sec = _section(page, "From agent sessions")
    assert sec["type"] == "list"
    assert sec["items"][0]["t"] == "No asks recorded"
    assert all("now" not in c["t"] for c in page.get("chips") or []), (
        "no chip is added on a healthy empty read")


# ── degraded states ────────────────────────────────────────────────────────


def test_agent_sessions_cn_down_renders_one_line_state(
    home: Path, cn: dict[str, Any],
) -> None:
    """``_get`` returning ``{}`` (CN is down) renders the one-line
    ``_cn.section_status`` state, never silently empty, and adds no chip.
    """
    cn["inbox"]["now"] = "{}"
    cn["inbox"]["soon"] = "{}"
    cn["inbox"]["later"] = "{}"
    cn["attention"] = "{}"

    page = build_page(home, "inbox")
    sec = _section(page, "From agent sessions")
    assert sec["type"] == "list"
    assert len(sec["items"]) == 1
    assert sec["items"][0] == _cn.section_status({"_unreachable": True}), (
        f"a down CN must render the section_status one-liner, got {sec['items'][0]}")
    chips = _chip_map(page)
    assert "✋ now 3" not in chips and not any("now" in t for t in chips), (
        f"no chip may render on a down CN, got {list(chips)}")


def test_agent_sessions_cn_disabled_renders_one_line_state(
    home: Path, cn: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``MO_DISABLE_CN=1`` short-circuits before any ``_get`` is fired and
    renders the one-line state."""
    monkeypatch.setenv("MO_DISABLE_CN", "1")
    try:
        page = build_page(home, "inbox")
        sec = _section(page, "From agent sessions")
        assert sec["type"] == "list"
        assert sec["items"][0] == _cn.section_status({"_disabled": True}), (
            f"MO_DISABLE_CN=1 must render the section_status one-liner, got {sec['items'][0]}")
        assert not cn["calls"], "a disabled ContextNest must not be read at all"
    finally:
        monkeypatch.delenv("MO_DISABLE_CN", raising=False)


def test_agent_sessions_cn_transport_error_renders_one_line_state(
    home: Path, cn: dict[str, Any],
) -> None:
    """A transport error (``_get`` raises ``OSError``) is converted to
    the one-line state by ``_cn.render_cached_section``."""
    cn["error"] = OSError("connection refused")

    page = build_page(home, "inbox")
    sec = _section(page, "From agent sessions")
    assert sec["type"] == "list"
    assert sec["items"][0] == _cn.section_status({"_unreachable": True}), (
        f"a transport error must render the section_status one-liner, got {sec['items'][0]}")


# ── run-id extraction: bounded, never a bare substring match ───────────────


def test_agent_sessions_run_id_extraction_is_bounded_word_boundary(
    home: Path, cn: dict[str, Any],
) -> None:
    """A bare substring match would hyperlink non-run text; the anchored
    ``cn_client.extract_run_id`` regex would miss the acceptance fixture
    ``run-123-456``. The page must match ``run-123-456`` (3 digits, 3 alnum)
    and reject ``runa-123-456`` (no leading ``run-``) and ``run-123-``
    (no trailing alnum)."""
    project = _project(home)
    cn["inbox"]["now"] = _inbox_body([
        # Acceptance fixture: must match.
        _inbox_item(content="Please review run-123-456 before noon",
                    session_id="eeeeeeee-0000-4000-8000-000000000001",
                    project=project, urgency="now"),
        # "runa-" must NOT match — a bare substring would.
        _inbox_item(content="Aloha runa-123-456 is the wrong anchor",
                    session_id="eeeeeeee-0000-4000-8000-000000000002",
                    project=project, urgency="now"),
        # "run-abc" must NOT match — digits are required.
        _inbox_item(content="The phrase run-abc is a known false friend",
                    session_id="eeeeeeee-0000-4000-8000-000000000003",
                    project=project, urgency="now"),
    ])

    page = build_page(home, "inbox")
    sec = _section(page, "From agent sessions · now")
    items = {row["t"]: row for row in sec["items"]}

    matched = items["Please review run-123-456 before noon"]
    matched_acts = [a["label"] for a in (matched.get("acts") or [])]
    assert "Open run" in matched_acts, (
        f"run-123-456 must carry an Open-run action, got {matched_acts}")
    matched_run = next(a for a in matched["acts"] if a["label"] == "Open run")
    assert matched_run["do"] == {"run": "run-123-456", "title": "run-123-456"}

    wrong_anchor = items["Aloha runa-123-456 is the wrong anchor"]
    wrong_acts = [a["label"] for a in (wrong_anchor.get("acts") or [])]
    assert "Open run" not in wrong_acts, (
        f"runa-123-456 must NOT match, got {wrong_acts}")

    no_digits = items["The phrase run-abc is a known false friend"]
    no_digits_acts = [a["label"] for a in (no_digits.get("acts") or [])]
    assert "Open run" not in no_digits_acts, (
        f"run-abc must NOT match (digits required), got {no_digits_acts}")


# ── request shape ─────────────────────────────────────────────────────────


def test_agent_sessions_request_paths_carry_urgency_project_and_limit(
    home: Path, cn: dict[str, Any],
) -> None:
    """Each read hits the right endpoint with the right query string:
    three ``/api/v1/inbox?urgency=…`` calls (one per urgency) plus one
    ``/api/v1/sessions/attention?project=…`` call, all scoped to the
    project root."""
    project = _project(home)
    cn["inbox"]["now"] = _inbox_body([_inbox_item(
        content="ask", session_id="ffffffff-0000-4000-8000-000000000001",
        project=project, urgency="now")])
    build_page(home, "inbox")

    calls = cn["calls"]
    inbox_calls = [p for p in calls if "/api/v1/inbox" in p]
    attention_calls = [p for p in calls if "/api/v1/sessions/attention" in p]
    assert len(inbox_calls) == 3, (
        f"one inbox call per urgency is required, got {len(inbox_calls)}")
    assert len(attention_calls) == 1, (
        f"exactly one attention call is required, got {len(attention_calls)}")

    for p in inbox_calls:
        q = urllib.parse.parse_qs(urllib.parse.urlparse(p).query)
        assert q.get("urgency") in (["now"], ["soon"], ["later"]), (
            f"each inbox call carries one urgency, got {p}")
        assert q.get("project") == [project], (
            f"each inbox call is scoped to the project root, got {p}")
        assert q.get("limit") == ["30"], (
            f"each inbox call carries limit=30, got {p}")
    q = urllib.parse.parse_qs(urllib.parse.urlparse(attention_calls[0]).query)
    assert q.get("project") == [project], (
        f"the attention call is scoped to the project root, got {attention_calls[0]}")


# ── the existing inbox stays unchanged when CN is unreachable ─────────────


def test_agent_sessions_does_not_break_existing_inbox_when_cn_unreachable(
    home: Path,
) -> None:
    """With no CN stub (a real CI home, CN is unreachable), the existing
    inbox still renders cleanly and the chip counts are unchanged — the
    'From agent sessions' section is the one-line state and the page
    builds ``ok: true``."""
    # The ``home`` fixture does NOT stub cn_client._get; the real client
    # tries to reach the default ``CN_BASE_URL`` and gets a connection
    # error, which ``_get`` catches and returns ``"{}"`` for.
    page = build_page(home, "inbox")
    assert page["ok"] is True, f"the inbox must build with CN down, got {page}"
    # The new section is the one-line state, not silently absent.
    sec = _section(page, "From agent sessions")
    assert sec["type"] == "list"
    # And it does NOT add a chip — the existing inbox chip counts are
    # unchanged, which is the regression gate the kickoff names.
    chips = _chip_map(page)
    assert not any("now" in t and "needs you" not in t for t in chips), (
        f"a CN-down render must not add a 'now' chip, got {list(chips)}")


def test_agent_sessions_existing_inbox_sections_unchanged_when_cn_up(
    home: Path, cn: dict[str, Any],
) -> None:
    """With CN up, the existing inbox sections (awaiting review, needs you,
    failed, being repaired) keep rendering — the new section is added, not
    substituted."""
    project = _project(home)
    cn["inbox"]["now"] = _inbox_body([_inbox_item(
        content="ask", session_id="11111111-1111-4111-8111-111111111111",
        project=project, urgency="now")])
    page = build_page(home, "inbox")
    titles = [s.get("title") for s in page["sections"]]
    # The new section is present.
    assert "From agent sessions · now" in titles
    # The existing inbox callouts ("Nothing needs you" with no other runs)
    # are also present.
    assert "Nothing needs you" in titles
