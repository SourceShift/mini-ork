"""``board page orch · coord`` — ContextNest coord surfaces on the IDE tab.

The kickoff (rank 4 — "Is another run editing my files, and who yields?") asks
the Coordination tab to render the live state of Concord alongside the existing
principals, hot claims and --owns violations:

* Overlaps table from ``/api/v1/coord/overlaps`` — ``{id, kind, a, b,
  state, count, first_seen}``, sorted open → escalated → acked (newest
  first within each bucket), capped at 30 rows with a 'Showing N of M'
  note; one confirm-gated Ack button per open / escalated row that calls
  ``mini-ork cn ack-overlap <id>`` (never on acked rows).
* Active path freezes from ``/api/v1/coord/freezes``.
* Duplicate-work pairs from ``/api/v1/coord/topic-pairs`` — ``{a, a_text,
  b, b_text, similarity}``, sorted by similarity descending, each pair
  shows both ids and both intents (cut to 120 characters), capped at 30
  rows with a 'Showing N of M' note.

The ack verb's POST happens through :func:`mini_ork.cn_client._coord_request`,
which raises :class:`CoordUnavailable` on a connection error and
:class:`CoordHTTPError` on a 4xx/5xx. The verb maps those to exit codes 3 and
4 respectively, so a CI failure on the IDE-side button has a precise cause.

Every test talks to the in-scope modules only. ``mini_ork.cn_client`` is
read-only, so the tests reach ``_coord_request`` through the same private seam
the page builder uses — monkeypatching it lets one fake serve three sections
(overlaps/freezes/topic-pairs) without spinning up a real ContextNest, and
gives the verb's POST path a single dispatch point.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

from mini_ork.cli import cn_cmd
from mini_ork.cn_client import CoordHTTPError, CoordUnavailable
from mini_ork.ide_pages import orch

REPO = Path(__file__).resolve().parents[2]


# ── shared fixtures ─────────────────────────────────────────────────────────


@pytest.fixture
def home(tmp_path: Path) -> Path:
    from mini_ork.stores import migrate as mig

    h = tmp_path / "proj" / ".mini-ork"
    h.mkdir(parents=True)
    rc, _out, err = mig.init_db(db=str(h / "state.db"), root=str(REPO))
    assert rc == 0, err
    return h


def _walk_cli_actions(node: dict | list | object) -> list[dict]:
    """Every ``{"cli": [...]}`` dict anywhere under ``node`` — borrowed from
    :mod:`tests.unit.test_ide_pages_actions` so the new file does not depend
    on that test module being importable in a non-action-sweep context.

    Walks the entire tree because a page puts cli actions under buttons,
    table rows, list items, chips, and section actions; filtering on a
    specific shape here would skip the section actions of the new
    Overlaps / Freezes / Topic-pairs sections.
    """
    out: list[dict] = []
    if isinstance(node, dict):
        do = node.get("do")
        if isinstance(do, dict) and isinstance(do.get("cli"), list):
            out.append(do)
        for value in node.values():
            out.extend(_walk_cli_actions(value))
    elif isinstance(node, list):
        for value in node:
            out.extend(_walk_cli_actions(value))
    return out


def _coord_table(page: dict[str, Any], title_substr: str) -> dict[str, Any]:
    """One table section whose title contains ``title_substr`` (case-insensitive)."""
    needle = title_substr.lower()
    for s in page["sections"]:
        if s.get("type") == "table" and needle in str(s.get("title", "")).lower():
            return s
    raise AssertionError(
        f"no table section with title containing {title_substr!r}; "
        f"got {[s.get('title') for s in page['sections']]}"
    )


def _coord_list(page: dict[str, Any], title_substr: str) -> dict[str, Any]:
    needle = title_substr.lower()
    for s in page["sections"]:
        if s.get("type") == "list" and needle in str(s.get("title", "")).lower():
            return s
    raise AssertionError(
        f"no list section with title containing {title_substr!r}; "
        f"got {[s.get('title') for s in page['sections']]}"
    )


# ── fake ``_coord_request`` ─────────────────────────────────────────────────


def _make_fake(overlaps=None, freezes=None, topic_pairs=None, *,
               calls: list[tuple[str, str, dict | None]] | None = None):
    """Build a fake ``_coord_request`` that serves the three new endpoints.

    ``calls`` lets a test inspect ``(method, path, body)`` tuples in dispatch
    order; the fake always records into the same list it returns, so passing
    ``calls=seen`` makes both the assertion and the dispatch visible at once.

    Each fake is factory-shaped because monkeypatching replaces the symbol on
    the module; a closure captures the local lists and avoids a stale state
    leak between tests.
    """
    seen = calls if calls is not None else []

    def _fake(method: str, path: str, body: dict | None = None) -> dict:
        seen.append((method, path, body))
        if path == "/api/v1/coord/overlaps":
            return {"overlaps": list(overlaps or [])}
        if path == "/api/v1/coord/freezes":
            return {"freezes": list(freezes or [])}
        if path == "/api/v1/coord/topic-pairs":
            return {"pairs": list(topic_pairs or [])}
        return {}

    return _fake, seen


# ── section rendering ───────────────────────────────────────────────────────


def test_overlaps_section_renders_open_first_with_ack_action(
    home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CN-up + three overlaps (open / escalated / acked): the open row
    sorts first, its Ack button wires to ``cn ack-overlap <id>`` with
    ``confirm`` and ``home=False``, the acked row carries no Ack."""
    monkeypatch.setattr("mini_ork.cn_client.available", lambda: True)
    now = int(time.time())
    fake, calls = _make_fake(
        overlaps=[
            {"id": "ov-acked", "kind": "file", "a": "p3", "b": "p4",
             "state": "acked", "count": 2, "first_seen": now - 1200},
            {"id": "ov-escalated", "kind": "topic", "a": "p2", "b": "p5",
             "state": "escalated", "count": 5, "first_seen": now - 300},
            {"id": "ov-open", "kind": "topic", "a": "p1", "b": "p2",
             "state": "open", "count": 7, "first_seen": now - 60},
        ],
        freezes=[],
        topic_pairs=[],
    )
    monkeypatch.setattr("mini_ork.cn_client._coord_request", fake)
    # The existing principals/claims/violations helpers go through module-level
    # symbols; stub them out so this test does not care about them.
    monkeypatch.setattr("mini_ork.cn_client.coord_list_principals",
                        lambda status="active": {"principals": []})
    monkeypatch.setattr("mini_ork.cn_client.coord_hot_claims", lambda: {"claims": []})
    monkeypatch.setattr("mini_ork.cn_client.coord_owns_violations",
                        lambda since=0: {"violations": []})

    page = orch.build(home, "coord", {})
    section = _coord_table(page, "overlaps")
    rows = section["rows"]
    assert len(rows) == 3
    # Open is first even though "ov-acked" appears earlier in the payload;
    # the escalated row sits ahead of the acked one.
    assert rows[0]["cells"][0]["t"] == "ov-open"
    assert rows[1]["cells"][0]["t"] == "ov-escalated"
    assert rows[2]["cells"][0]["t"] == "ov-acked"
    # The "a ↔ b" column carries both principals.
    assert rows[0]["cells"][2]["t"] == "p1 ↔ p2"
    assert rows[1]["cells"][2]["t"] == "p2 ↔ p5"
    assert rows[2]["cells"][2]["t"] == "p3 ↔ p4"
    # State column carries the colour the design calls for.
    assert rows[0]["cells"][3] == {"t": "open", "c": "yellow",
                                   "mono": False, "b": False}
    assert rows[1]["cells"][3]["t"] == "escalated"
    assert rows[1]["cells"][3]["c"] == "orange"
    assert rows[2]["cells"][3]["t"] == "acked"
    # Count column reads the numeric payload (and "—" is the value for
    # a missing count, exercised in a separate test).
    assert rows[0]["cells"][4]["t"] == "7"
    assert rows[1]["cells"][4]["t"] == "5"
    assert rows[2]["cells"][4]["t"] == "2"
    # Open AND escalated rows carry an Ack button; acked does not.
    ack_open = next(a for a in rows[0]["acts"] if a["label"] == "Ack")
    assert ack_open["kind"] == "warn"
    assert ack_open["do"]["cli"] == ["cn", "ack-overlap", "ov-open"]
    assert ack_open["do"]["home"] is False
    assert ack_open["do"]["confirm"]
    ack_esc = next(a for a in rows[1]["acts"] if a["label"] == "Ack")
    assert ack_esc["do"]["cli"] == ["cn", "ack-overlap", "ov-escalated"]
    # The acked row has no row-level acts (the section keeps the
    # action header; the cell-level "no button" is the gate's job).
    assert not any(a["label"] == "Ack" for a in rows[2]["acts"])
    # The end-of-section note announces the cap and the sort order.
    note = section["note"].lower()
    assert "showing 3 of 3" in note
    assert "open" in note and "escalated" in note and "acked" in note
    # The page walked the three GET endpoints through the seam.
    methods = [m for m, _p, _b in calls]
    assert "GET" in methods and methods.count("GET") >= 3


def test_overlaps_cap_at_30_and_open_first(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """35 overlaps (10 open + 10 escalated + 15 acked, interleaved): the
    30-row cap holds, the note says 'Showing 30 of 35', and every open
    row precedes every escalated which precedes every acked."""
    monkeypatch.setattr("mini_ork.cn_client.available", lambda: True)
    now = int(time.time())
    items = []
    # Interleave 15 acked + 10 open + 10 escalated, oldest first within
    # each bucket so a naive stable sort would scatter them through the
    # table.
    for i in range(15):
        items.append({"id": f"ov-acked-{i:02d}", "kind": "file",
                      "a": f"p{i}", "b": f"q{i}",
                      "state": "acked", "count": 1,
                      "first_seen": now - (200 + i)})
    for i in range(10):
        items.append({"id": f"ov-open-{i:02d}", "kind": "topic",
                      "a": f"p{i}", "b": f"q{i}",
                      "state": "open", "count": 1,
                      "first_seen": now - (100 + i)})
    for i in range(10):
        items.append({"id": f"ov-esc-{i:02d}", "kind": "topic",
                      "a": f"p{i}", "b": f"q{i}",
                      "state": "escalated", "count": 1,
                      "first_seen": now - (50 + i)})
    fake, _ = _make_fake(overlaps=items, freezes=[], topic_pairs=[])
    monkeypatch.setattr("mini_ork.cn_client._coord_request", fake)
    monkeypatch.setattr("mini_ork.cn_client.coord_list_principals",
                        lambda status="active": {"principals": []})
    monkeypatch.setattr("mini_ork.cn_client.coord_hot_claims", lambda: {"claims": []})
    monkeypatch.setattr("mini_ork.cn_client.coord_owns_violations",
                        lambda since=0: {"violations": []})

    page = orch.build(home, "coord", {})
    section = _coord_table(page, "overlaps")
    rows = section["rows"]
    assert len(rows) == 30
    # The 30-row cap note rides on the section.
    assert "Showing 30 of 35" in section["note"]
    # The open bucket is preserved; the last open row sits before any
    # escalated, and the last escalated sits before any acked.
    last_open_idx = max(i for i, r in enumerate(rows) if r["cells"][3]["t"] == "open")
    first_non_open = min(
        i for i, r in enumerate(rows) if r["cells"][3]["t"] != "open"
    )
    last_esc_idx = max(
        i for i, r in enumerate(rows) if r["cells"][3]["t"] == "escalated"
    )
    first_acked_idx = min(
        i for i, r in enumerate(rows) if r["cells"][3]["t"] == "acked"
    )
    assert last_open_idx < first_non_open
    assert last_esc_idx < first_acked_idx


def test_freezes_section_renders_empty_state(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty freezes payload renders the 'No active path freezes' row."""
    monkeypatch.setattr("mini_ork.cn_client.available", lambda: True)
    fake, _ = _make_fake(freezes=[])
    monkeypatch.setattr("mini_ork.cn_client._coord_request", fake)
    monkeypatch.setattr("mini_ork.cn_client.coord_list_principals",
                        lambda status="active": {"principals": []})
    monkeypatch.setattr("mini_ork.cn_client.coord_hot_claims", lambda: {"claims": []})
    monkeypatch.setattr("mini_ork.cn_client.coord_owns_violations",
                        lambda since=0: {"violations": []})

    page = orch.build(home, "coord", {})
    section = _coord_table(page, "freezes")
    assert section["rows"][0]["cells"][0]["t"] == "No active path freezes"


def test_freezes_section_renders_rows(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("mini_ork.cn_client.available", lambda: True)
    fake, _ = _make_fake(freezes=[
        {"path": "mini_ork/x.py", "principal_id": "p9", "expires_at": "soon",
         "started_at": int(time.time()) - 30},
        {"path": "mini_ork/y.py", "holder": "p8", "until": "later",
         "ts": int(time.time()) - 600},
    ])
    monkeypatch.setattr("mini_ork.cn_client._coord_request", fake)
    monkeypatch.setattr("mini_ork.cn_client.coord_list_principals",
                        lambda status="active": {"principals": []})
    monkeypatch.setattr("mini_ork.cn_client.coord_hot_claims", lambda: {"claims": []})
    monkeypatch.setattr("mini_ork.cn_client.coord_owns_violations",
                        lambda since=0: {"violations": []})

    page = orch.build(home, "coord", {})
    section = _coord_table(page, "freezes")
    rows = section["rows"]
    assert len(rows) == 2
    assert rows[0]["cells"][0]["t"] == "mini_ork/x.py"
    assert rows[0]["cells"][1]["t"] == "p9"
    assert rows[1]["cells"][1]["t"] == "p8"


def test_topic_pairs_section_renders_new_shape(
    home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each pair shows the two principal ids and both intents (cut to
    120 characters), with the similarity in the detail line. Strongest
    pair sorts first; at most 30 pairs render with 'Showing N of M'."""
    monkeypatch.setattr("mini_ork.cn_client.available", lambda: True)
    long_intent = "very long session intent " * 30
    fake, _ = _make_fake(topic_pairs=[
        {"a": "p1", "b": "p2",
         "a_text": "fix login flow", "b_text": "rewrite auth handler",
         "similarity": 0.42},
        {"a": "p3", "b": "p4",
         "a_text": long_intent, "b_text": long_intent,
         "similarity": 0.91},
        {"a": "p5", "b": "p6",
         "a_text": "refactor scheduler", "b_text": "rewrite scheduler",
         "similarity": 0.77},
    ])
    monkeypatch.setattr("mini_ork.cn_client._coord_request", fake)
    monkeypatch.setattr("mini_ork.cn_client.coord_list_principals",
                        lambda status="active": {"principals": []})
    monkeypatch.setattr("mini_ork.cn_client.coord_hot_claims", lambda: {"claims": []})
    monkeypatch.setattr("mini_ork.cn_client.coord_owns_violations",
                        lambda since=0: {"violations": []})

    page = orch.build(home, "coord", {})
    section = _coord_list(page, "duplicate")
    items = section["items"]
    assert len(items) == 3

    # Similarity descending → 0.91 first, then 0.77, then 0.42.
    assert items[0]["t"] == "p3 ↔ p4"
    assert items[0]["sub"].endswith("similarity 0.91")
    assert items[1]["t"] == "p5 ↔ p6"
    assert items[1]["sub"].endswith("similarity 0.77")
    assert items[2]["t"] == "p1 ↔ p2"
    assert items[2]["sub"].endswith("similarity 0.42")

    # The 120-char cut: both sides of the long pair carry at most 120
    # characters of intent text in the detail line.
    expected_cut = (long_intent[:120] + "  ×  " + long_intent[:120]
                    + "  ·  similarity 0.91")
    assert items[0]["sub"] == expected_cut
    # The two short intents render in full (no ellipsis, no cut).
    assert "fix login flow" in items[2]["sub"]
    assert "rewrite auth handler" in items[2]["sub"]

    # No actions: the pair's per-side open verbs were removed with the
    # rewrite — the OLD shape (Open A / Open B → run tab, thread, CN CLI)
    # is dead code now that the payloads carry plain ids instead.
    for it in items:
        assert not it["acts"]
    # The section's note says "Showing 3 of 3".
    assert "Showing 3 of 3" in section["note"]


def test_topic_pairs_section_cap_at_30(
    home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """40 pairs: the cap holds at 30; the note says 'Showing 30 of 40'."""
    monkeypatch.setattr("mini_ork.cn_client.available", lambda: True)
    pairs = [
        {"a": f"p{i}", "b": f"q{i}",
         "a_text": "intent a", "b_text": "intent b",
         "similarity": float(i) / 100.0}
        for i in range(40)
    ]
    fake, _ = _make_fake(topic_pairs=pairs)
    monkeypatch.setattr("mini_ork.cn_client._coord_request", fake)
    monkeypatch.setattr("mini_ork.cn_client.coord_list_principals",
                        lambda status="active": {"principals": []})
    monkeypatch.setattr("mini_ork.cn_client.coord_hot_claims", lambda: {"claims": []})
    monkeypatch.setattr("mini_ork.cn_client.coord_owns_violations",
                        lambda since=0: {"violations": []})

    page = orch.build(home, "coord", {})
    section = _coord_list(page, "duplicate")
    assert len(section["items"]) == 30
    assert "Showing 30 of 40" in section["note"]


# ── CN-down fallback ────────────────────────────────────────────────────────


def test_coordination_down_keeps_one_line_fallback_only(
    home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CN down → the three new sections are NOT rendered; the existing
    one-line "ContextNest is not reachable" fallback is preserved."""
    monkeypatch.setattr("mini_ork.cn_client.available", lambda: False)
    page = orch.build(home, "coord", {})
    titles = [s["title"] for s in page["sections"]]
    # The fallback rides inside the Principals · concord list section's first
    # item, matching the assertion shape ``test_ide_pages_orch``
    # ``test_coordination_without_contextnest`` already pins.
    assert page["sections"][0]["items"][0]["t"] == "ContextNest is not reachable"
    # None of the new sections sneak in when CN is down.
    assert not any("overlaps" in t.lower() for t in titles)
    assert not any("freeze" in t.lower() for t in titles)
    assert not any("duplicate" in t.lower() for t in titles)


def test_coordination_section_failure_yields_guarded_section(
    home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CN up but ``/coord/overlaps`` blows up: the section is rendered as a
    'Could not read this' guard item instead of blanking the page."""
    monkeypatch.setattr("mini_ork.cn_client.available", lambda: True)

    def fake(method: str, path: str, body: dict | None = None) -> dict:
        if path == "/api/v1/coord/overlaps":
            raise RuntimeError("kaboom")
        if path == "/api/v1/coord/freezes":
            return {"freezes": []}
        if path == "/api/v1/coord/topic-pairs":
            return {"pairs": []}
        return {}

    monkeypatch.setattr("mini_ork.cn_client._coord_request", fake)
    monkeypatch.setattr("mini_ork.cn_client.coord_list_principals",
                        lambda status="active": {"principals": []})
    monkeypatch.setattr("mini_ork.cn_client.coord_hot_claims", lambda: {"claims": []})
    monkeypatch.setattr("mini_ork.cn_client.coord_owns_violations",
                        lambda since=0: {"violations": []})

    page = orch.build(home, "coord", {})
    section = _coord_list(page, "overlaps")
    # S.guarded renders one list-style row with the failure message in a "✗"
    # marker ("Could not read this"). The page keeps building despite the error,
    # and the Spawn tree still anchors the bottom (the section failure costs
    # only one section, never the page).
    assert section["items"][0]["t"].startswith("Could not read this")
    titles = [s["title"] for s in page["sections"]]
    assert "Spawn tree" in titles


# ── ``mini-ork cn ack-overlap`` ──────────────────────────────────────────────


def test_cn_ack_overlap_verb_posts_through_coord_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``mini-ork cn ack-overlap <id>`` POSTs through ``_coord_request`` to
    ``/api/v1/coord/overlaps/<id>/ack`` with the body the spec asks for."""
    seen: list[tuple[str, str, dict | None]] = []

    def fake(method: str, path: str, body: dict | None = None) -> dict:
        seen.append((method, path, body))
        return {"id": "ov-7", "status": "acknowledged"}

    monkeypatch.setattr("mini_ork.cn_client._coord_request", fake)
    monkeypatch.delenv("CONCORD_PRINCIPAL", raising=False)
    rc = cn_cmd.main(["ack-overlap", "ov-7"], root="")
    assert rc == 0
    assert len(seen) == 1
    method, path, body = seen[0]
    assert method == "POST"
    assert path == "/api/v1/coord/overlaps/ov-7/ack"
    assert isinstance(body, dict) and "by" in body and body["by"].startswith("human:")


def test_cn_ack_overlap_verb_url_encodes_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """An overlap id carrying a reserved character is URL-escaped so the path
    stays a single segment after ``/overlaps/`` and ``/ack``."""
    seen: list[tuple[str, str, dict | None]] = []

    def fake(method: str, path: str, body: dict | None = None) -> dict:
        seen.append((method, path, body))
        return {"id": "ov/x"}

    monkeypatch.setattr("mini_ork.cn_client._coord_request", fake)
    rc = cn_cmd.main(["ack-overlap", "ov/x"], root="")
    assert rc == 0
    _, path, _ = seen[0]
    assert path == "/api/v1/coord/overlaps/ov%2Fx/ack"


def test_cn_ack_overlap_returns_3_on_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake(method: str, path: str, body: dict | None = None) -> dict:
        raise CoordUnavailable("connection refused")

    monkeypatch.setattr("mini_ork.cn_client._coord_request", fake)
    rc = cn_cmd.main(["ack-overlap", "ov-1"], root="")
    assert rc == cn_cmd.EXIT_COORD_UNREACHABLE
    assert rc == 3


def test_cn_ack_overlap_returns_4_on_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake(method: str, path: str, body: dict | None = None) -> dict:
        raise CoordHTTPError(404, "no such overlap")

    monkeypatch.setattr("mini_ork.cn_client._coord_request", fake)
    rc = cn_cmd.main(["ack-overlap", "ov-1"], root="")
    assert rc == cn_cmd.EXIT_COORD_HTTP
    assert rc == 4


def test_cn_ack_overlap_uses_supplied_by(monkeypatch: pytest.MonkeyPatch) -> None:
    """``--by`` overrides the env-derived default and rides the POST body."""
    seen: list[tuple[str, str, dict | None]] = []

    def fake(method: str, path: str, body: dict | None = None) -> dict:
        seen.append((method, path, body))
        return {}

    monkeypatch.setattr("mini_ork.cn_client._coord_request", fake)
    monkeypatch.delenv("CONCORD_PRINCIPAL", raising=False)
    rc = cn_cmd.main(["ack-overlap", "ov-7", "--by", "run:abc"], root="")
    assert rc == 0
    assert seen[0][2] == {"by": "run:abc"}


# ── actions sweep over the Coordination tab ────────────────────────────────


def _overlap_action_under_section(page: dict[str, Any]) -> dict[str, Any]:
    """The Ack action on the open row of the Overlaps table."""
    section = _coord_table(page, "overlaps")
    for row in section["rows"]:
        for act in row.get("acts") or []:
            if act.get("label") == "Ack":
                return act["do"]
    raise AssertionError("no Ack action on the Overlaps table")


def test_coordination_ack_action_parses_in_actions_sweep(
    home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Ack button's argv ``['ack-overlap', <id>]`` parses against the
    ``mini_ork.cn`` parser; ``home=False`` keeps the IDE from appending
    ``--home`` to a verb whose parser rejects it."""
    monkeypatch.setattr("mini_ork.cn_client.available", lambda: True)
    now = int(time.time())
    fake, _ = _make_fake(
        overlaps=[{"id": "ov-7", "kind": "file", "a": "p1", "b": "p2",
                   "state": "open", "count": 3,
                   "first_seen": now - 60}],
    )
    monkeypatch.setattr("mini_ork.cn_client._coord_request", fake)
    monkeypatch.setattr("mini_ork.cn_client.coord_list_principals",
                        lambda status="active": {"principals": []})
    monkeypatch.setattr("mini_ork.cn_client.coord_hot_claims", lambda: {"claims": []})
    monkeypatch.setattr("mini_ork.cn_client.coord_owns_violations",
                        lambda since=0: {"violations": []})

    page = orch.build(home, "coord", {})
    ack = _overlap_action_under_section(page)
    assert ack["cli"][:2] == ["cn", "ack-overlap"]
    overlap_id = ack["cli"][2]
    # The CLI parser accepts the argv AS-IS (home=False means the IDE will
    # not append ``--home`` later).
    parser = cn_cmd.build_parser()
    try:
        parser.parse_known_args(["ack-overlap", overlap_id])
    except SystemExit as exc:
        pytest.fail(f"cn parser rejected Ack argv: exit {exc.code}")

    # And the same walk used by test_ide_pages_actions — drive the page tree
    # directly — must NOT trip on the Ack button.
    failures: list[str] = []
    for action in _walk_cli_actions(page):
        cli = action.get("cli") or []
        if not cli:
            continue
        head = str(cli[0])
        if head != "cn":
            # Other verbs (e.g. ``intake``) on other tabs are out of scope here.
            continue
        argv = [str(a) for a in cli[1:]]
        if action.get("home", True) is True:
            argv += ["--home", str(home)]
        try:
            parser.parse_known_args(argv)
        except SystemExit as exc:
            failures.append(f"{argv!r}: exit {exc.code}")
    assert not failures, failures


def test_coordination_section_titles_preserve_existing_order(
    home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The kickoff relies on the titles[:3] ordering the existing
    ``test_coordination_with_contextnest`` pins: append-after-violations
    without renaming the original three."""
    monkeypatch.setattr("mini_ork.cn_client.available", lambda: True)
    monkeypatch.setattr("mini_ork.cn_client.coord_list_principals",
                        lambda status="active": {"principals": []})
    monkeypatch.setattr("mini_ork.cn_client.coord_hot_claims", lambda: {"claims": []})
    monkeypatch.setattr("mini_ork.cn_client.coord_owns_violations",
                        lambda since=0: {"violations": []})
    fake, _ = _make_fake(overlaps=[], freezes=[], topic_pairs=[])
    monkeypatch.setattr("mini_ork.cn_client._coord_request", fake)

    page = orch.build(home, "coord", {})
    titles = [s["title"] for s in page["sections"]]
    assert titles[:3] == [
        "Principals · concord", "Claims on hot files", "Scope violations (--owns)",
    ], titles
    assert "Overlaps · concord" in titles
    assert "Active path freezes" in titles
    assert "Duplicate work · concord" in titles


# ── cn subcommand registration ──────────────────────────────────────────────


def test_cn_subcommand_is_registered_for_ack_overlap() -> None:
    """``mini-ork`` must reach ``cn_cmd.build_parser`` for the cn verb so the
    actions sweep in ``test_ide_pages_actions.py`` can resolve its parser.
    """
    from mini_ork.cli import main as cli_main

    assert cli_main._NATIVE_MODULE_SUBS.get("cn") == "mini_ork.cli.cn_cmd"

    parser = cn_cmd.build_parser()
    args = parser.parse_args(["ack-overlap", "ov-x"])
    assert args.subcommand == "ack-overlap"
    assert args.overlap_id == "ov-x"
