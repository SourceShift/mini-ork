"""``board page lanes · past decisions`` — ContextNest atoms on the Lanes tab.

The kickoff (rank 8 — "What did I already decide for this kind of work?")
asks the Lanes tab to render ContextNest decision and failure atoms from
the last 14 days, scoped to the project, each opening its session. The
page goes through :func:`mini_ork.ide_pages._cn.render_cached_section`
(two cached slots, one per ``kind`` — ``decision_made`` and ``failure``),
and :func:`mini_ork.ide_pages._cn.section_status` renders the
down / disabled / cached / config-error states so the page stays
``ok: true`` with no server.

The atoms endpoint (``/api/v1/prompt-context/atoms``) returns JSON with
``since`` / ``count`` / ``total_matched`` / ``atoms``; each atom carries
``kind``, ``text``, ``session_id``, ``project`` and ``ts``
(ISO-8601 UTC with milliseconds). Tests use synthetic data in exactly
that shape, never invented JSON and never real transcript content.

The tests reach :func:`mini_ork.cn_client._get` through the same module
seam :mod:`tests.unit.test_cn_ui_coordination` uses for ``_coord_request``
— monkeypatching the symbol lets one fake serve both cached-section
slots without spinning up a real ContextNest, and keeps the test
hermetic. Stubbing ``capsule`` would silently keep testing the removed
code path, which is why this module stubs ``_get`` only.
"""
from __future__ import annotations

import datetime as _dt
import json
import urllib.parse
from pathlib import Path
from typing import Any

import pytest

from mini_ork import cn_client
from mini_ork.ide_pages import _cn
from mini_ork.ide_pages import lanes

REPO = Path(__file__).resolve().parents[2]


# ── shared fixtures ─────────────────────────────────────────────────────────


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A temp ``.mini-ork`` home whose section cache lives inside the
    test's tmp dir so no real slot is read or written."""
    from mini_ork.stores import migrate as mig

    h = tmp_path / "proj" / ".mini-ork"
    h.mkdir(parents=True)
    rc, _out, err = mig.init_db(db=str(h / "state.db"), root=str(REPO))
    assert rc == 0, err
    monkeypatch.setenv("MINI_ORK_HOME", str(h))
    monkeypatch.setenv("MO_DISABLE_CN", "0")
    # The Past decisions section is a level-2 surface (env unset keeps the old
    # page byte-identical), so these tests build the current IDE's page.
    monkeypatch.setenv("MINI_ORK_IDE_SPEC", "2")
    monkeypatch.setattr(cn_client, "available", lambda: True)
    return h


def _project(home: Path) -> str:
    """The project root (the parent of the .mini-ork home)."""
    return str(home.parent.resolve())


def _section(page: dict[str, Any], title: str) -> dict[str, Any]:
    for s in page["sections"]:
        if s["title"] == title:
            return s
    raise AssertionError(
        f"no section with title {title!r}; got {[s['title'] for s in page['sections']]}"
    )


def _ts(seconds_ago: float) -> str:
    """An ISO-8601 UTC ts with milliseconds, the kickoff's verified live shape."""
    when = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=seconds_ago)
    return when.strftime("%Y-%m-%dT%H:%M:%S.") + f"{when.microsecond // 1000:03d}Z"


def _atom(text: str, project: str, *, kind: str = "decision_made",
          sid: str, ts: str, i: int = 0) -> dict[str, Any]:
    return {"id": f"cn-atom{i:04d}", "kind": kind, "text": text,
            "session_id": sid, "project": project, "ts": ts, "metadata": {}}


def _body(atoms: list[dict[str, Any]], total_matched: int | None = None) -> str:
    return json.dumps({"since": "14d", "count": len(atoms),
                       "total_matched": len(atoms) if total_matched is None else total_matched,
                       "by_kind": {}, "atoms": atoms})


@pytest.fixture
def cn(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """A hermetic ContextNest: ``cn_client._get`` is stubbed per kind.

    ``state["decision_made"]`` / ``state["failure"]`` hold the body ``_get``
    returns for each kind query (a JSON string); ``state["error"]``, when
    set, makes ``_get`` raise; ``state["calls"]`` records every requested
    path so request-shape tests can assert on it.
    """
    state: dict[str, Any] = {"decision_made": _body([]), "failure": _body([]),
                             "error": None, "calls": []}

    def fake_get(path: str, *, timeout: float | None = None) -> str:
        state["calls"].append(path)
        if state["error"] is not None:
            raise state["error"]
        query = urllib.parse.parse_qs(urllib.parse.urlparse(path).query)
        kind = (query.get("kind") or [""])[0]
        return state.get(kind, "{}")

    monkeypatch.setattr(cn_client, "_get", fake_get)
    return state


# ── section rendering ───────────────────────────────────────────────────────


def test_past_decisions_renders_atoms_with_session_link(
    home: Path, cn: dict[str, Any],
) -> None:
    """Healthy atoms from both kinds render as rows: each carries its kind
    label (``decision`` / ``failure``), the (truncated) text, and a
    :func:`S.page_link` to the session page."""
    project = _project(home)
    decision = _atom("Escalate the reviewer lane to opus after two failures",
                     project, sid="f5671eaa-1111-4222-8333-444444444444",
                     ts=_ts(3600), i=1)
    failure = _atom("kimi_lens returned an empty artifact; the run routed to glm",
                    project, kind="failure",
                    sid="0c31d9bb-9999-4888-8777-666666666666",
                    ts=_ts(7200), i=2)
    cn["decision_made"] = _body([decision])
    cn["failure"] = _body([failure])

    page = lanes.build(home, "lanes", {})
    section = _section(page, "Past decisions")
    assert section["type"] == "table"
    rows = section["rows"]
    assert len(rows) == 2, f"expected 2 rendered atoms, got {len(rows)}"

    def _texts(row: dict[str, Any]) -> list[str]:
        return [str(c.get("t")) for c in row.get("cells") or [] if isinstance(c, dict)]

    def _action(row: dict[str, Any]) -> dict[str, Any] | None:
        do = row.get("do")
        return do if isinstance(do, dict) else None

    def _row_with(rows: list[dict[str, Any]], needle: str) -> dict[str, Any] | None:
        for row in rows:
            if any(needle in t for t in _texts(row)):
                return row
        return None

    drow = _row_with(rows, "Escalate the reviewer lane")
    assert drow is not None, "decision atom text not rendered"
    assert "decision" in _texts(drow), f"kind must render as 'decision', got {_texts(drow)}"
    assert "Escalate the reviewer lane to opus after two failures" in _texts(drow)
    assert _action(drow) == {
        "page": "cn_session",
        "args": {"id": "f5671eaa-1111-4222-8333-444444444444"},
    }, "the row action must open the session page, never execute atom text"

    frow = _row_with(rows, "kimi_lens returned an empty artifact")
    assert frow is not None, "failure atom text not rendered"
    assert "failure" in _texts(frow), f"kind must render as 'failure', got {_texts(frow)}"
    assert "kimi_lens returned an empty artifact; the run routed to glm" in _texts(frow)
    assert _action(frow) == {
        "page": "cn_session",
        "args": {"id": "0c31d9bb-9999-4888-8777-666666666666"},
    }


def test_past_decisions_drops_atoms_from_another_project(
    home: Path, cn: dict[str, Any],
) -> None:
    """An atom whose ``project`` is foreign, and an atom whose ``project``
    is a nested checkout of the root, are both dropped — only the atom
    whose ``project`` byte-equals the project root survives. Mirrors the
    fail-closed rule ``mini_ork.cn_client.filter_by_project_path`` keeps."""
    project = _project(home)
    foreign = _atom("lane routing budget for another project entirely",
                    "/somewhere/else/project",
                    sid="aaaaaaaa-0000-4000-8000-000000000001", ts=_ts(600), i=1)
    nested = _atom("cost cap decided for a nested checkout of the root",
                   project + "/child",
                   sid="aaaaaaaa-0000-4000-8000-000000000002", ts=_ts(700), i=2)
    own = _atom("provider fallback order decided for this root",
                project, sid="aaaaaaaa-0000-4000-8000-000000000003",
                ts=_ts(800), i=3)
    cn["decision_made"] = _body([foreign, nested, own])

    page = lanes.build(home, "lanes", {})
    section = _section(page, "Past decisions")
    rows = section["rows"]

    def _texts(row: dict[str, Any]) -> list[str]:
        return [str(c.get("t")) for c in row.get("cells") or [] if isinstance(c, dict)]

    def _row_with(rows: list[dict[str, Any]], needle: str) -> dict[str, Any] | None:
        for row in rows:
            if any(needle in t for t in _texts(row)):
                return row
        return None

    assert len(rows) == 1, f"only the root's own atom may render, got {len(rows)} rows"
    assert _row_with(rows, "provider fallback order decided") is not None
    assert _row_with(rows, "another project") is None, "a foreign-project atom leaked in"
    assert _row_with(rows, "nested checkout") is None, (
        "an atom whose project is inside but not equal to the root must be dropped")


def test_past_decisions_drops_atoms_without_vocabulary(
    home: Path, cn: dict[str, Any],
) -> None:
    """Atoms whose ``text`` carries none of the lane / routing / budget
    vocabulary are dropped, so the section stays on-topic."""
    project = _project(home)
    off_topic = _atom("Renamed the documentation index chapters and fixed two typos",
                      project, sid="bbbbbbbb-0000-4000-8000-000000000001",
                      ts=_ts(600), i=1)
    on_topic = _atom("Rebalanced the cost cap across provider lanes",
                     project, sid="bbbbbbbb-0000-4000-8000-000000000002",
                     ts=_ts(700), i=2)
    footnote_failure = _atom("The chapter renderer clipped a footnote",
                             project, kind="failure",
                             sid="bbbbbbbb-0000-4000-8000-000000000003",
                             ts=_ts(800), i=3)
    cn["decision_made"] = _body([off_topic, on_topic])
    cn["failure"] = _body([footnote_failure])

    page = lanes.build(home, "lanes", {})
    section = _section(page, "Past decisions")
    rows = section["rows"]

    def _texts(row: dict[str, Any]) -> list[str]:
        return [str(c.get("t")) for c in row.get("cells") or [] if isinstance(c, dict)]

    def _row_with(rows: list[dict[str, Any]], needle: str) -> dict[str, Any] | None:
        for row in rows:
            if any(needle in t for t in _texts(row)):
                return row
        return None

    assert len(rows) == 1, f"only vocabulary-matching atoms may render, got {len(rows)} rows"
    assert _row_with(rows, "Rebalanced the cost cap") is not None
    assert _row_with(rows, "documentation index") is None
    assert _row_with(rows, "footnote") is None


def test_past_decisions_vocabulary_match_is_whole_word_and_case_insensitive(
    home: Path, cn: dict[str, Any],
) -> None:
    """``cap`` inside ``capability`` and ``route`` inside ``router`` are
    substring-only hits and must NOT match; ``CAP`` and ``OPUS`` must
    match because the match is case-insensitive on the full token."""
    project = _project(home)
    capability = _atom("The capability matrix was re-derived for the docs",
                       project, sid="cccccccc-0000-4000-8000-000000000001",
                       ts=_ts(600), i=1)
    router = _atom("Restarted the router daemon before the deploy",
                   project, sid="cccccccc-0000-4000-8000-000000000002",
                   ts=_ts(700), i=2)
    upper = _atom("CAP raised for the OPUS lane on deepseek days",
                  project, sid="cccccccc-0000-4000-8000-000000000003",
                  ts=_ts(800), i=3)
    cn["decision_made"] = _body([capability, router, upper])

    page = lanes.build(home, "lanes", {})
    section = _section(page, "Past decisions")
    rows = section["rows"]

    def _texts(row: dict[str, Any]) -> list[str]:
        return [str(c.get("t")) for c in row.get("cells") or [] if isinstance(c, dict)]

    def _row_with(rows: list[dict[str, Any]], needle: str) -> dict[str, Any] | None:
        for row in rows:
            if any(needle in t for t in _texts(row)):
                return row
        return None

    assert len(rows) == 1, (
        f"'capability' and 'router' are substring-only hits and must be dropped; got {len(rows)} rows")
    assert _row_with(rows, "CAP raised") is not None, (
        "case-insensitive vocabulary ('CAP', 'OPUS') must match")


def test_past_decisions_rows_newest_first_cap_30_with_showing_note(
    home: Path, cn: dict[str, Any],
) -> None:
    """The merged list sorts newest ts first, caps at 30 rows, and the
    table note carries ``Showing 30 of N`` when capped."""
    project = _project(home)
    atoms = []
    for i in range(45):
        atoms.append(_atom(f"lane marker {i:02d} budget cap decided",
                           project,
                           kind="failure" if i % 9 == 0 else "decision_made",
                           sid=f"dddddddd-{i:04d}-4000-8000-000000000000",
                           ts=_ts((i + 1) * 100), i=i))
    cn["decision_made"] = _body([a for a in atoms if a["kind"] == "decision_made"],
                                total_matched=500)
    cn["failure"] = _body([a for a in atoms if a["kind"] == "failure"],
                          total_matched=496)

    page = lanes.build(home, "lanes", {})
    section = _section(page, "Past decisions")
    rows = section["rows"]
    assert len(rows) == 30, f"the merged list must cap at 30 rows, got {len(rows)}"

    def _texts(row: dict[str, Any]) -> list[str]:
        return [str(c.get("t")) for c in row.get("cells") or [] if isinstance(c, dict)]

    def _marker(row: dict[str, Any]) -> str | None:
        import re as _re
        for t in _texts(row):
            m = _re.search(r"lane marker \d\d", t)
            if m:
                return m.group(0)
        return None

    got = [_marker(r) for r in rows]
    assert got == [f"lane marker {i:02d}" for i in range(30)], (
        "rows must be the 30 NEWEST atoms (smallest seconds-ago first), newest first")
    assert "failure" in _texts(rows[0]), "atom 00 is a failure; the newest overall must lead the merge"
    note = str(section.get("note", ""))
    assert "Showing 30 of 45" in note, (
        f"the capped table must say 'Showing N of M' over the 45 matched atoms; note={note!r}")


def test_past_decisions_atom_text_truncated_at_160_chars(
    home: Path, cn: dict[str, Any],
) -> None:
    """An atom whose ``text`` is 255 chars renders only the first 160 —
    the kickoff's truncation rule."""
    project = _project(home)
    text = "lane " + "abcdefghij" * 25  # 255 chars, vocabulary in the first word
    cn["decision_made"] = _body([_atom(text, project,
                                       sid="eeeeeeee-0000-4000-8000-000000000001",
                                       ts=_ts(600), i=1)])

    page = lanes.build(home, "lanes", {})
    section = _section(page, "Past decisions")
    rows = section["rows"]
    assert len(rows) == 1

    texts = [str(c.get("t")) for c in rows[0].get("cells") or [] if isinstance(c, dict)]
    assert text[:160] in texts, "the rendered text must be exactly the first 160 chars"
    assert text not in texts, "the full 255-char text must not render untruncated"


def test_past_decisions_request_paths_carry_kinds_and_project(
    home: Path, cn: dict[str, Any],
) -> None:
    """The two GETs hit ``/api/v1/prompt-context/atoms`` with
    ``since=14d``, ``kind=decision_made`` / ``kind=failure``, the project
    root, and ``limit=500`` — one per kind, both going through
    :func:`mini_ork.cn_client._get`."""
    project = _project(home)
    cn["decision_made"] = _body([_atom("lane budget decision", project,
                                       sid="ffffffff-0000-4000-8000-000000000001",
                                       ts=_ts(600), i=1)])
    lanes.build(home, "lanes", {})

    calls = cn["calls"]
    assert calls, "_past_decisions must read its atoms through cn_client._get"
    endpoint = "/api/v1/prompt-context/atoms"
    assert all(p.startswith(endpoint) for p in calls), (
        f"every request must hit the atoms endpoint, got {calls}")
    queries = [urllib.parse.parse_qs(urllib.parse.urlparse(p).query) for p in calls]
    kinds = sorted(q.get("kind", [""])[0] for q in queries)
    assert "decision_made" in kinds and "failure" in kinds, (
        f"one list per kind must be requested, got kinds {kinds}")
    for q in queries:
        assert q.get("since") == ["14d"], f"since=14d must be carried, got {q}"
        assert q.get("limit") == ["500"], f"limit=500 must be carried, got {q}"
        assert q.get("project") == [project], (
            f"the project root {project!r} must be carried, got {q}")


def test_past_decisions_cn_down_renders_one_line_state(
    home: Path, cn: dict[str, Any],
) -> None:
    """CN up but returning ``{}`` is a failed read — the section renders
    the established one-line state from
    :func:`mini_ork.ide_pages._cn.section_status`, never silently empty."""
    cn["decision_made"] = "{}"
    cn["failure"] = "{}"

    page = lanes.build(home, "lanes", {})
    section = _section(page, "Past decisions")
    assert section["type"] == "list"
    items = section["items"]
    assert len(items) == 1
    assert items[0] == _cn.section_status({"_unreachable": True}), (
        f"the failed read must render the section_status one-liner, got {items[0]}")


def test_past_decisions_cn_disabled_renders_one_line_state(
    home: Path, cn: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``MO_DISABLE_CN=1`` short-circuits before any ``_get`` is fired."""
    monkeypatch.setenv("MO_DISABLE_CN", "1")
    try:
        page = lanes.build(home, "lanes", {})
        section = _section(page, "Past decisions")
        assert section["type"] == "list"
        item = section["items"][0]
        assert item == _cn.section_status({"_disabled": True}), (
            f"MO_DISABLE_CN=1 must render the section_status one-liner, got {item}")
        assert not cn["calls"], "a disabled ContextNest must not be read at all"
    finally:
        monkeypatch.delenv("MO_DISABLE_CN", raising=False)


def test_past_decisions_cn_transport_error_renders_one_line_state(
    home: Path, cn: dict[str, Any],
) -> None:
    """A transport error (``_get`` raises ``OSError``) renders the
    one-line state via ``section_status``."""
    cn["error"] = OSError("connection refused")

    page = lanes.build(home, "lanes", {})
    section = _section(page, "Past decisions")
    assert section["type"] == "list"
    items = section["items"]
    assert len(items) == 1
    assert items[0] == _cn.section_status({"_unreachable": True}), (
        f"a transport error must render the section_status one-liner, got {items[0]}")


def test_past_decisions_section_is_only_on_lanes_tab(home: Path) -> None:
    """The kickoff asks for one 'Past decisions' section on the default
    'lanes' tab — not on routing, not on cost."""
    page_lanes = lanes.build(home, "lanes", {})
    page_routing = lanes.build(home, "routing", {})
    page_cost = lanes.build(home, "cost", {})

    assert "Past decisions" in [s["title"] for s in page_lanes["sections"]]
    assert "Past decisions" not in [s["title"] for s in page_routing["sections"]]
    assert "Past decisions" not in [s["title"] for s in page_cost["sections"]]


def test_past_decisions_no_matches_renders_one_plain_row(
    home: Path, cn: dict[str, Any],
) -> None:
    """CN up with no atoms for this project: a single 'No decisions
    recorded' row replaces the table — never a silently empty table
    and never the down state."""
    cn["decision_made"] = _body([])
    cn["failure"] = _body([])

    page = lanes.build(home, "lanes", {})
    section = _section(page, "Past decisions")
    assert isinstance(section, dict)
    if section.get("type") == "table":
        rows = list(section.get("rows") or [])
        assert len(rows) == 1, f"a healthy empty read renders ONE plain row, got {len(rows)}"
        text = " ".join(str(c.get("t")) for c in rows[0].get("cells") or [] if isinstance(c, dict))
    else:
        items = list(section.get("items") or [])
        assert len(items) == 1, f"a healthy empty read renders ONE plain row, got {len(items)}"
        text = " ".join(str(v) for v in items[0].values() if isinstance(v, str))
    assert "not running" not in text, "an empty-but-healthy read is not the down state"
    assert "no" in text.lower(), f"the plain row must say there are none, got {text!r}"
    assert "cn_session" not in json.dumps(section), "no atom row may render"


def test_past_decisions_section_does_not_break_page(
    home: Path, cn: dict[str, Any],
) -> None:
    """Even when ContextNest raises mid-render, the page still builds
    with ``ok: true`` and the existing sections are untouched. The
    kickoff relies on the page continuing to build for the
    ``test_ide_pages_actions`` action sweep."""
    cn["error"] = RuntimeError("kapow")

    page = lanes.build(home, "lanes", {})
    assert page["ok"] is True
    titles = [s["title"] for s in page["sections"]]
    # Level 2 renames the Lanes table section to "All · Lanes" (the agents board
    # goes above it); either title proves the existing section survived.
    assert "All · Lanes" in titles
    assert "Past decisions" in titles
    assert "Credentials" in titles
    assert "Bring your own provider" in titles


def test_past_decisions_section_uses_only_existing_action_kinds(
    home: Path, cn: dict[str, Any],
) -> None:
    """Each row's ``do`` action is :func:`S.page_link` (or ``None`` when
    the atom carries no ``session_id``); no new action kind is invented
    for the section. The ``test_ide_pages_actions`` action sweep walks
    every action on every page, so an unknown action kind would break it."""
    project = _project(home)
    cn["decision_made"] = _body([
        _atom("provider fallback lane at the project root", project,
              sid="11111111-1111-4111-8111-111111111111", ts=_ts(600), i=1),
        _atom("budget cost cap at the project root", project,
              sid="", ts=_ts(700), i=2),
    ])

    page = lanes.build(home, "lanes", {})
    section = _section(page, "Past decisions")
    rows = section["rows"]
    assert len(rows) == 2
    # First row carries a session id — its action opens the cn_session page.
    assert isinstance(rows[0]["do"], dict)
    assert rows[0]["do"].get("page") == "cn_session"
    assert rows[0]["do"].get("args", {}).get("id") == "11111111-1111-4111-8111-111111111111"
    # Second row carries no session id — the action is ``None`` (a plain row).
    assert rows[1]["do"] is None