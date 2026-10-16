"""``mini-ork board page kanban`` — ContextNest attention badges on the Board.

The Board (``mini_ork/ide_pages/kanban.py``) renders two tab surfaces:
``tasks`` (epics + mirrored tickets) and ``runs`` (fleet rows). The
acceptance matrix the kickoff pins is:

1. **Open asks badge.** On both tabs, a card whose run is named by an open
   ContextNest ask (a fresh inbox cache slot) gets a ``✋ ask`` meta item;
   cards whose run no ask names render byte-identical to the pre-change meta.

2. **Open overlaps badge.** A card whose run id matches one of the ``a`` /
   ``b`` principal ids on an open or escalated overlap gets a ``↔ overlap``
   meta item. Acked overlaps badge nothing. Overlaps carry no ``paths``
   field — the matcher must derive "on its paths" from principal ids alone.

3. **Three urgency scopes.** The inbox cache slots cover ``now``,
   ``soon``, ``later`` (the verified wire shape carries no ``urgency``
   field on items). An ask in ``later`` is still an open ask and still
   badges the run.

4. **No network on build.** The board reads cache slots via
   ``cn_client._read_section_cache`` only; it never calls ``_get``,
   ``_get_text`` or ``_post_json``. A ``no_network`` fixture patches those
   three to raise and asserts the call list stays empty.

5. **Cold / corrupt cache renders exactly as before.** A missing
   ``state.db`` or a malformed cache slot must cost the board nothing —
   no badge, no ``errors`` entry, no rendering change.

6. **Existing keys stay unchanged.** Meta additions stay inside the
   ``meta`` list — the card's documented top-level keys are untouched
   (``test_ticket_cards_carry_only_documented_keys``-style contract).

Fixtures are synthetic; no real transcript content, no real credentials,
no live CN — every read goes through cache slots on a temp home the test
controls.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

import pytest

from mini_ork import cn_client
from mini_ork.acp.fleet import FleetRow
from mini_ork.ide_pages import kanban
from mini_ork.ide_pages import spec as S
from mini_ork.stores import migrate as mig

REPO = Path(__file__).resolve().parents[2]

# Run ids in the canonical ``run-<digits>-<alnum>`` shape — every run id
# matcher in the tree accepts these.
RUN_A = "run-1792090100-a1b2c3"
RUN_B = "run-1792090200-z9y8x7"
SESS = "sess-3f9c21aa-77c0"
EPIC_A = "epic-board-badge-a"
EPIC_B = "epic-board-badge-b"

NOW = int(time.time())

# The exact pre-change meta of a fleet card with no diff and no cost:
# ``[S.meta_item(S.money(0.0), mono=True)]``.
RUN_META_BASE = [S.meta_item(S.money(0.0), mono=True)]
# ``_task_card`` ships ``"meta": []`` until badges are added.
EPIC_META_BASE: list[dict[str, Any]] = []


# ── fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A temp mini-ork home with migrations run and CN env pinned.

    The existing kanban-tests' home setups do ``ensure_priority_column`` and
    ``ensure_retry_schema`` — the latter is what adds ``epics.last_run_id``
    and the ``epic_attempts`` table the board reads to resolve an epic's
    linked run ids.
    """
    h = tmp_path / "proj" / ".mini-ork"
    h.mkdir(parents=True)
    rc, _out, err = mig.init_db(db=str(h / "state.db"), root=str(REPO))
    assert rc == 0, err
    from mini_ork import scheduler
    scheduler.ensure_priority_column(str(h / "state.db"))
    scheduler.ensure_retry_schema(str(h / "state.db"))
    # CN reads go through cache slots under MINI_ORK_HOME/cache/cn/… — the
    # monkeypatched env vars strip any inherited tier / base url so the
    # test cannot reach a developer's real CN.
    monkeypatch.setenv("MINI_ORK_HOME", str(h))
    monkeypatch.setenv("MINI_ORK_ROOT", str(tmp_path / "proj"))
    for var in ("MO_DISABLE_CN", "CN_CACHE_TTL_SEC", "CN_BASE_URL",
                "CN_TOKEN_KEY", "CN_TIER", "MINI_ORK_PM"):
        monkeypatch.delenv(var, raising=False)
    return h


@pytest.fixture
def project(home: Path) -> str:
    """The project root the inbox epic scopes its cache slots by.

    ``str(home.parent.resolve())`` — exactly the convention inbox.py
    uses to derive it, so the board and the inbox read the same slot.
    """
    return str(home.parent.resolve())


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Patch the three CN network functions to raise, counting every attempt.

    The kickoff's own acceptance mechanism: ``patch ``cn_client._get``,
    ``_get_text`` and ``_post_json`` to raise. The recorded list lets a
    test assert the board attempted no call at all — not merely that it
    survived one.
    """
    calls: list[str] = []

    def _boom(name, path, *a, **kw):
        calls.append(f"{name} {path}")
        raise AssertionError(f"network call attempted during board build: {name} {path}")

    monkeypatch.setattr(cn_client, "_get", lambda p, **k: _boom("_get", p))
    monkeypatch.setattr(cn_client, "_get_text", lambda p, **k: _boom("_get_text", p))
    monkeypatch.setattr(cn_client, "_post_json",
                        lambda p, b, **k: _boom("_post_json", p, b))
    return calls


@pytest.fixture
def fleet(monkeypatch: pytest.MonkeyPatch) -> list[FleetRow]:
    """Synthetic fleet rows for the Runs tab (no run dirs, no history db).

    ``kanban._fleet`` imports :func:`mini_ork.acp.fleet.fleet_rows` lazily
    inside the function — replacing it on the module itself intercepts the
    read without touching the on-disk state.
    """
    rows = [
        FleetRow(run_id=RUN_A, title="Fixture run A", recipe="code-fix",
                 state="working", mark="●", step="", started_at=NOW,
                 ended_at=None, cost_usd=0.0, added=0, removed=0),
        FleetRow(run_id=RUN_B, title="Fixture run B", recipe="code-fix",
                 state="working", mark="●", step="", started_at=NOW - 10,
                 ended_at=None, cost_usd=0.0, added=0, removed=0),
    ]

    def _fleet_rows(home, *, state="all", limit=50):
        selected = [r for r in rows if state in ("all", r.state)]
        counts = {s: sum(1 for r in rows if r.state == s)
                  for s in ("working", "needs_you", "failed", "done")}
        return selected, counts

    from mini_ork.acp import fleet
    monkeypatch.setattr(fleet, "fleet_rows", _fleet_rows)
    return rows


@pytest.fixture
def epic_rows(home: Path) -> Path:
    """Two epics in the ``epics`` table, each linked to its run both ways.

    ``epics.last_run_id`` (the scheduler's recorded latest) and an
    ``epic_attempts`` row are the two routes the board's
    :func:`_epic_run_ids` reads to resolve an epic's run ids. The seeded
    pair lets the matcher match ``EPIC_A`` to ``RUN_A`` and ``EPIC_B`` to
    ``RUN_B`` — one positive case (the ask names ``RUN_A``), one negative
    (``RUN_B``).
    """
    con = sqlite3.connect(home / "state.db")
    con.execute("PRAGMA foreign_keys = ON")
    for epic_id, run_id in ((EPIC_A, RUN_A), (EPIC_B, RUN_B)):
        kickoff = f"kickoffs/{epic_id}.md"
        con.execute(
            "INSERT INTO epics (id, title, status, kickoff_path, updated_at,"
            " priority, attempts, recipe, last_run_id)"
            " VALUES (?, ?, 'in progress', ?, "
            " strftime('%Y-%m-%dT%H:%M:%fZ','now'),"
            " 2, 1, 'code-fix', ?)",
            (epic_id, f"Fixture epic {epic_id}", kickoff, run_id),
        )
        con.execute(
            "INSERT INTO task_runs (id, task_class, recipe, kickoff_path,"
            " status, created_at, updated_at) VALUES (?, 'code_fix', 'code-fix',"
            " ?, 'executing', ?, ?)",
            (run_id, kickoff, NOW, NOW),
        )
        con.execute(
            "INSERT INTO epic_attempts (epic_id, attempt, run_id, recipe,"
            " kickoff_path) VALUES (?, 1, ?, 'code-fix', ?)",
            (epic_id, run_id, kickoff),
        )
    con.commit(); con.close()
    return home / "state.db"


# ── synthetic payloads in the live-verified shapes ──────────────────────────


def ask_item(run_id: str, project: str, urgency_text: str = "") -> dict:
    """One inbox ask in the kickoff's verified wire shape.

    ``GET /api/v1/inbox`` → ``{items: [{id, session_id, content, importance,
    metadata{kind, project_cwd, src_session, task_id, task_status, ts}}]}``.
    The item carries **no urgency field** — the bucket is the cache scope —
    and names ``run_id`` both in ``metadata.task_id`` and in the content
    text. ``urgency_text`` is opt-in: a string appended to content so a
    test can prove the matcher ignores the bucket-level "when".
    """
    return {
        "id": f"ask-{run_id}",
        "session_id": SESS,
        "content": f"Operator asked about {run_id}: publish once green"
                   + (f" ({urgency_text})" if urgency_text else ""),
        "importance": 3,
        "metadata": {
            "kind": "ask",
            "project_cwd": project,
            "src_session": SESS,
            "task_id": run_id,
            "task_status": "open",
            "ts": 1792090000,
        },
    }


def overlap(run_id: str, state: str) -> dict:
    """One overlap in the kickoff's verified shape: ``a``/``b`` are principal
    ids, there is **no** ``paths``/``status`` field — the badge must derive
    "on its paths" from the principal ids alone."""
    return {
        "id": "ov-117",
        "kind": "edit",
        "a": run_id,
        "b": "sess-77c0-deadbeef",
        "state": state,
        "count": 3,
        "first_seen": NOW - 600,
        "last_seen": NOW - 60,
        "escalated_at": None,
        "ack_by": None,
        "ack_decision": None,
        "ack_note": None,
    }


def write_slot(home: Path, section: str, scope: str, payload: dict) -> None:
    """Write one section-cache slot where ``cn_client``'s cache resolver puts it.

    The slot is stamped fresh (``_as_of`` = now) and ``_from_cache`` = False
    so a reader that consults the TTL the way orch's overlapping cache does
    sees a fresh write.
    """
    path = cn_client._section_cache_path(section, scope)
    assert path, "section cache dir unavailable under MINI_ORK_HOME"
    data = dict(payload)
    data["_as_of"] = int(time.time())
    data["_from_cache"] = False
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


def write_slot_aged(home: Path, section: str, scope: str, payload: dict,
                    age_s: int) -> None:
    """Like :func:`write_slot` but stamped ``age_s`` seconds in the past.

    The board peeks the slot directly, so it must apply the same TTL bound
    ``render_cached_section`` does (``CN_CACHE_TTL_SEC``, default 300 s): a
    slot aged past the TTL reads as "no data" exactly like a missing one.
    """
    path = cn_client._section_cache_path(section, scope)
    assert path, "section cache dir unavailable under MINI_ORK_HOME"
    data = dict(payload)
    data["_as_of"] = int(time.time()) - int(age_s)
    data["_from_cache"] = False
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


def write_ask_slot(home: Path, project: str, urgency: str, items: list) -> None:
    """Write the asks slot for one urgency scope — the slot the inbox epic's
    per-urgency read writes (section ``inbox-needs-you``, scope
    ``<urgency>:<project>``)."""
    write_slot(home, "inbox-needs-you", f"{urgency}:{project}", {"items": items})


def write_overlap_slot(home: Path, project: str, overlaps: list) -> None:
    """Write the overlaps slot — the slot the orch coord section writes."""
    write_slot(home, "coord-overlaps", "", {"overlaps": overlaps})


# ── page walkers ────────────────────────────────────────────────────────────


def cards_by_id(page: dict) -> dict:
    """Every card in a page, keyed by card id — shape-agnostic.

    A card is the dict carrying both ``id`` and ``meta``; the walk nests
    through sections → columns → cards naturally.
    """
    out: dict = {}

    def walk(node):
        if isinstance(node, dict):
            if "id" in node and isinstance(node.get("meta"), list):
                out[node["id"]] = node
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(page)
    return out


def meta_texts(card: dict) -> list[str]:
    return [str(m.get("t")) for m in card.get("meta", [])
            if isinstance(m, dict)]


def meta_is_s_meta_item(card: dict) -> bool:
    """Every meta item is an ``S.meta_item`` shape — used by spec tests."""
    for m in card.get("meta", []):
        if not isinstance(m, dict):
            return False
        if not isinstance(m.get("t"), str):
            return False
    return True


def dump(page: dict) -> str:
    return json.dumps(page, sort_keys=True, default=str)


# ── Runs tab: asks ──────────────────────────────────────────────────────────


def test_run_named_by_ask_gets_badge(home, project, fleet, no_network):
    """A run named by a fixture ask gets the badge; unnamed runs do not."""
    write_ask_slot(home, project, "now", [ask_item(RUN_A, project)])

    page = kanban.build(home, "runs", {})
    cards = cards_by_id(page)
    assert page.get("ok") is True and not page.get("errors")

    named = cards.get(RUN_A)
    assert named is not None, "fixture run A must render on the Runs board"
    assert named["meta"] != RUN_META_BASE, (
        "the run named by the cached ask must carry an ask badge meta item "
        "beyond its cost meta")
    assert meta_is_s_meta_item(named), (
        "badges must be meta items (S.meta_item shape)")
    # The badge carries a "ask" verb the operator can read at a glance.
    assert any("ask" in str(m.get("t") or "").lower() for m in named["meta"]), (
        f"expected an ask badge in meta, got {[m.get('t') for m in named['meta']]}")

    unnamed = cards.get(RUN_B)
    assert unnamed is not None, "fixture run B must render on the Runs board"
    assert unnamed["meta"] == RUN_META_BASE, (
        "a run the ask does not name must render exactly its pre-change meta — "
        "asks that name the card's run badge only that card")


def test_ask_in_soon_bucket_still_badges(home, project, fleet, no_network):
    """The matcher buckets by the cache scope, not by an item-level field.

    The verified wire shape carries no ``urgency`` field on items — the
    bucket is the per-urgency cache scope. An ask in ``soon`` must still
    badge the run it names; cold would mean the scope is unreadable.
    """
    write_ask_slot(home, project, "soon", [ask_item(RUN_A, project)])

    cards = cards_by_id(kanban.build(home, "runs", {}))
    assert cards[RUN_A]["meta"] != RUN_META_BASE, (
        "an open ask in the soon urgency scope is still an open ContextNest "
        "ask naming the run — the badge must not depend on the now bucket")


def test_ask_in_later_bucket_still_badges(home, project, fleet, no_network):
    """``later`` scope is still an open ask — even the least urgent bucket."""
    write_ask_slot(home, project, "later", [ask_item(RUN_A, project)])

    cards = cards_by_id(kanban.build(home, "runs", {}))
    assert cards[RUN_A]["meta"] != RUN_META_BASE, (
        "an open ask in the later urgency scope is still an open ContextNest "
        "ask naming the run — the badge must not depend on the now bucket")


def test_unrelated_ask_does_not_badge_uninvolved_runs(home, project, fleet, no_network):
    """An ask that names ``RUN_C`` badges nothing on a board holding only A and B."""
    write_ask_slot(home, project, "now", [ask_item("run-1792999999-other", project)])

    cards = cards_by_id(kanban.build(home, "runs", {}))
    assert cards[RUN_A]["meta"] == RUN_META_BASE
    assert cards[RUN_B]["meta"] == RUN_META_BASE


# ── Runs tab: overlaps ──────────────────────────────────────────────────────


def test_open_overlap_badges_runs_card(home, project, fleet, no_network):
    """An open overlap whose principal matches a run ids badges that card."""
    write_overlap_slot(home, project, [overlap(RUN_A, "open")])

    cards = cards_by_id(kanban.build(home, "runs", {}))
    assert cards[RUN_A]["meta"] != RUN_META_BASE, (
        "an open overlap whose principal id is the run must badge the run's "
        "card, derived from a/b alone — overlaps carry no paths field")
    assert any("overlap" in str(m.get("t") or "").lower() for m in cards[RUN_A]["meta"]), (
        f"expected an overlap badge in meta, got {[m.get('t') for m in cards[RUN_A]['meta']]}")
    assert cards[RUN_B]["meta"] == RUN_META_BASE, (
        "an overlap not touching a run's principals must not badge that run")


def test_escalated_overlap_badges_runs_card(home, project, fleet, no_network):
    """An escalated overlap is an open overlap — same badge condition."""
    write_overlap_slot(home, project, [overlap(RUN_A, "escalated")])

    cards = cards_by_id(kanban.build(home, "runs", {}))
    assert cards[RUN_A]["meta"] != RUN_META_BASE
    assert cards[RUN_B]["meta"] == RUN_META_BASE


def test_acked_overlap_badges_nothing(home, project, fleet, no_network):
    """An acked overlap is not an open overlap — no badge may appear."""
    write_overlap_slot(home, project, [overlap(RUN_A, "acked")])

    cards = cards_by_id(kanban.build(home, "runs", {}))
    assert cards[RUN_A]["meta"] == RUN_META_BASE, (
        "an acked overlap must not fabricate an attention badge")


def test_b_side_principal_also_badges(home, project, fleet, no_network):
    """An overlap whose ``b`` (not ``a``) is the card's principal still badges."""
    write_overlap_slot(home, project, [{
        "id": "ov-200", "kind": "edit",
        "a": "sess-aaaaaaaa-bbbb", "b": RUN_A,
        "state": "open", "count": 1, "first_seen": NOW - 60,
        "last_seen": NOW - 30,
        "escalated_at": None, "ack_by": None,
        "ack_decision": None, "ack_note": None,
    }])
    cards = cards_by_id(kanban.build(home, "runs", {}))
    assert cards[RUN_A]["meta"] != RUN_META_BASE, (
        "an overlap naming the run on side ``b`` is still 'on its paths' "
        "for the badge — the matcher must check both endpoints")


# ── Tasks tab ───────────────────────────────────────────────────────────────


def test_ask_naming_epic_run_badges_tasks_card(home, project, epic_rows, no_network):
    """On Tasks, an epic whose linked run is named by an ask gets the badge.

    The match keys on the epic's run ids (sourced from ``epics.last_run_id``
    and the ``epic_attempts.run_id`` row). An epic whose run no ask names
    keeps the pre-change empty meta.
    """
    write_ask_slot(home, project, "now", [ask_item(RUN_A, project)])

    page = kanban.build(home, "tasks", {})
    cards = cards_by_id(page)
    assert page.get("ok") is True and not page.get("errors")

    epic_a = cards.get(EPIC_A)
    assert epic_a is not None, "fixture epic A must render on the Tasks board"
    assert epic_a["meta"] != EPIC_META_BASE, (
        "an ask naming the epic's run must put a badge meta item on the epic's "
        "Tasks card")

    epic_b = cards.get(EPIC_B)
    assert epic_b is not None, "fixture epic B must render on the Tasks board"
    assert epic_b["meta"] == EPIC_META_BASE, (
        "an epic whose run no ask names must keep its pre-change empty meta")


def test_open_overlap_badges_tasks_card(home, project, epic_rows, no_network):
    """An open overlap on the epic's run badges the epic's Tasks card."""
    write_overlap_slot(home, project, [overlap(RUN_A, "open")])

    page = kanban.build(home, "tasks", {})
    cards = cards_by_id(page)
    assert page.get("ok") is True and not page.get("errors")

    assert cards[EPIC_A]["meta"] != EPIC_META_BASE, (
        "an open overlap on the epic's run must badge the epic's Tasks card")
    assert cards[EPIC_B]["meta"] == EPIC_META_BASE


def test_task_card_keeps_only_documented_keys(home, project, epic_rows, no_network):
    """The badge additions stay inside ``meta``; top-level keys are untouched."""
    write_ask_slot(home, project, "now", [ask_item(RUN_A, project)])
    write_overlap_slot(home, project, [overlap(RUN_A, "open")])

    page = kanban.build(home, "tasks", {})
    cards = cards_by_id(page)
    card = cards.get(EPIC_A)
    assert set(card) == {"id", "title", "sub", "state", "mark", "meta", "do"}, (
        f"Tasks card must keep exactly the documented top-level keys; got {sorted(card)}")
    assert meta_is_s_meta_item(card), "every meta item is an S.meta_item shape"
    json.dumps(page)  # stays JSON-serialisable


def test_run_card_keeps_only_documented_keys(home, project, fleet, no_network):
    """Same shape contract on the Runs side."""
    write_ask_slot(home, project, "now", [ask_item(RUN_A, project)])
    write_overlap_slot(home, project, [overlap(RUN_A, "open")])

    page = kanban.build(home, "runs", {})
    cards = cards_by_id(page)
    card = cards.get(RUN_A)
    assert set(card) == {"id", "title", "sub", "state", "mark", "meta", "do"}, (
        f"Runs card must keep exactly the documented top-level keys; got {sorted(card)}")
    assert meta_is_s_meta_item(card)
    json.dumps(page)


# ── ContextNest down / no network ───────────────────────────────────────────


def test_cn_down_renders_exactly_as_before(home, project, fleet, epic_rows,
                                           no_network, monkeypatch):
    """With CN down the board renders exactly as before.

    No cache slot exists in this test; the board's peeks return ``(None, 0)``
    and the cards render with the pre-change meta. The disabled-CN render
    must equal the CN-down render byte-for-byte.
    """
    down = {tab: kanban.build(home, tab, {}) for tab in ("runs", "tasks")}

    for tab, page in down.items():
        assert page.get("ok") is True, f"{tab} page must build with CN down"
        assert page.get("errors") == {}, (
            f"{tab} page must record no new errors with CN down: "
            f"{page.get('errors')}")

    cards = cards_by_id(down["runs"])
    assert cards[RUN_A]["meta"] == RUN_META_BASE, (
        "a cold cache must leave a run card's meta byte-identical")
    assert cards[RUN_B]["meta"] == RUN_META_BASE
    tcards = cards_by_id(down["tasks"])
    assert tcards[EPIC_A]["meta"] == EPIC_META_BASE, (
        "a cold cache must leave an epic card's meta byte-identical (empty)")
    assert tcards[EPIC_B]["meta"] == EPIC_META_BASE

    for card in list(cards.values()) + list(tcards.values()):
        for text in meta_texts(card):
            assert "ask" not in text.lower() and "overlap" not in text.lower(), (
                "no badge text may appear on any card with CN down")

    # "Exactly as before" — the down board must equal the board with CN
    # switched off entirely: neither may add, remove or change anything.
    monkeypatch.setenv("MO_DISABLE_CN", "1")
    for tab in ("runs", "tasks"):
        disabled = kanban.build(home, tab, {})
        assert dump(disabled) == dump(down[tab]), (
            f"{tab} board with CN down must render exactly as with CN off — "
            "the badges add nothing when there is no cached data to read")


def test_board_build_makes_no_network_call(home, project, fleet, epic_rows,
                                           no_network):
    """The acceptance seal: building the board never makes a CN network call."""
    write_ask_slot(home, project, "now", [ask_item(RUN_A, project)])

    for tab in ("runs", "tasks"):
        page = kanban.build(home, tab, {})
        assert page.get("ok") is True, (
            f"{tab} page must build with the CN network functions raising")
        assert page.get("errors") == {}, (
            f"{tab} page errors with network patched to raise: {page.get('errors')}")

    assert no_network == [], (
        "building the board attempted a ContextNest network call — values must "
        "come from the cached sections, never a live fetch")

    cards = cards_by_id(kanban.build(home, "runs", {}))
    assert cards[RUN_A]["meta"] != RUN_META_BASE, (
        "the badge must be servable from the cache slot alone — no network, "
        "still badged")


def test_corrupt_cache_slot_renders_as_before(home, project, fleet, epic_rows,
                                              no_network):
    """A malformed cache slot costs the board nothing — no badge, no error."""
    path = cn_client._section_cache_path("inbox-needs-you", f"now:{project}")
    with open(path, "w", encoding="utf-8") as f:
        f.write("not json {")

    for tab in ("runs", "tasks"):
        page = kanban.build(home, tab, {})
        assert page.get("ok") is True, f"{tab} page must build past a corrupt slot"
        assert page.get("errors") == {}, (
            f"a malformed cache slot must cost no section and no error: "
            f"{page.get('errors')}")

    cards = cards_by_id(kanban.build(home, "runs", {}))
    assert cards[RUN_A]["meta"] == RUN_META_BASE, (
        "a malformed slot is 'no data' — no badge, no rendering change")
    tcards = cards_by_id(kanban.build(home, "tasks", {}))
    assert tcards[EPIC_A]["meta"] == EPIC_META_BASE


# ── TTL bound on the peeked slots ───────────────────────────────────────────


def test_stale_ask_slot_badges_nothing(home, project, fleet, epic_rows, no_network):
    """A slot older than the CN cache TTL is "no data" — no badge.

    The board peeks the section cache directly (never
    ``render_cached_section``), so it applies the same TTL bound itself: an
    hour-old slot must not badge a run with an ask the operator may have
    answered since.
    """
    write_slot_aged(home, "inbox-needs-you", f"now:{project}",
                    {"items": [ask_item(RUN_A, project)]}, 3600)

    cards = cards_by_id(kanban.build(home, "runs", {}))
    assert cards[RUN_A]["meta"] == RUN_META_BASE, (
        "a stale ask slot must not badge a run")
    tcards = cards_by_id(kanban.build(home, "tasks", {}))
    assert tcards[EPIC_A]["meta"] == EPIC_META_BASE, (
        "a stale ask slot must not badge the epic either")


def test_stale_overlap_slot_badges_nothing(home, project, fleet, epic_rows,
                                           no_network):
    """The same TTL bound applies to the coord-overlaps slot."""
    write_slot_aged(home, "coord-overlaps", "",
                    {"overlaps": [overlap(RUN_A, "open")]}, 3600)

    cards = cards_by_id(kanban.build(home, "runs", {}))
    assert cards[RUN_A]["meta"] == RUN_META_BASE, (
        "a stale overlap slot must not badge a run")


def test_slot_within_ttl_still_badges(home, project, fleet, no_network):
    """A slot a second old is fresh — the badge still shows.

    Pins the other side of the TTL bound so the freshness check cannot be a
    silent always-false.
    """
    write_slot_aged(home, "inbox-needs-you", f"now:{project}",
                    {"items": [ask_item(RUN_A, project)]}, 1)

    cards = cards_by_id(kanban.build(home, "runs", {}))
    assert cards[RUN_A]["meta"] != RUN_META_BASE, (
        "a within-TTL slot is still an open ask — it must badge")


# ── both badges on one card ────────────────────────────────────────────────


def test_run_with_both_ask_and_overlap_gets_both_badges(home, project, fleet,
                                                        no_network):
    """A run named by an ask AND touched by an open overlap carries both badges.

    Order matters: ask first, overlap second — the same order the kickoff
    ranks them in prose and the helper itself reads the maps in.
    """
    write_ask_slot(home, project, "now", [ask_item(RUN_A, project)])
    write_overlap_slot(home, project, [overlap(RUN_A, "open")])

    cards = cards_by_id(kanban.build(home, "runs", {}))
    badges = [str(m.get("t") or "").lower() for m in cards[RUN_A]["meta"]]
    assert "ask" in badges[0] or len(badges) >= 2, (
        f"both badges expected; got {badges!r}")
    # The badges are both present (in any order on the card).
    assert any("ask" in t for t in badges)
    assert any("overlap" in t for t in badges), (
        f"expected both badges on the named+overlapped run, got {badges!r}")
