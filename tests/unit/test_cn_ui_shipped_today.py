"""``mini-ork board page inbox`` — the "Shipped since yesterday" section.

The kickoff (rank 4) asks the Inbox page to surface features shipped in the
last 24 hours (``GET /api/v1/features?since=24h``), scoped to the project by
filtering ``files[]`` client-side against the project root (the parent of the
``.mini-ork`` home). The endpoint takes no server-side ``project=`` query
parameter — that key filters by session cwd, not by files — so client-side
``files[]`` containment is the only correct gate. A feature with no file that
resolves inside the project root is dropped even when its session cwd matches
the project (explicit acceptance).

The verified 2026-10-15 wire shape is::

    {
        "count": N, "since": "24h", "layer": "",
        "features": [
            {"feature": "<name>", "files": ["..."], "how_to_test": "...",
             "layer": "", "refs": [], "defs": [],
             "session_id": "...", "ts": 1730000000}
        ]
    }

The page reads ``feature`` first (the live key), falling back to ``name`` /
``label`` (older payloads) — the same defensive fallback ``cn_session``
applies.

The section goes through :func:`mini_ork.ide_pages._cn.render_cached_section`
with its own slot (``inbox-shipped-today:<project>``) so a slow
trajectory/summary read on the cn_session page can never poison this one
and vice-versa. Down / disabled / config-error states render the one-line
``_cn.section_status`` line, never silently drop the section, and add NO
page chip — exactly the way ``_agent_sessions_section`` adds its chip only
on a healthy read with at least one now item. The existing inbox (and
``test_ide_pages_inbox_kanban``, which does not stub CN) therefore renders
unchanged on every degraded path.

Tests use synthetic data in the verified wire shapes — never real
transcript content. ``cn_client._get`` is stubbed per path so the
``/api/v1/features`` slot is served from one fake. The ``MINI_ORK_HOME``
env var is set to the test's tmp dir so the section cache slots never
leak between tests or into the real home.
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
    # Lay down one real file inside the project so at least one of the
    # synthetic ``files[]`` paths resolves on disk. The client-side filter
    # does NOT require the file to exist (path containment is the gate),
    # but a real file makes the section's "Open session" link unambiguous.
    (h.parent / "README.md").write_text("# Project\n")
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


def _chip_map(page: dict[str, Any]) -> dict[str, str]:
    return {c["t"]: c["c"] for c in page.get("chips") or []}


def _feature(*, feature: str, files: list[str], session_id: str,
             how_to_test: str = "", ts: int = 0, name: str = "") -> dict[str, Any]:
    """One feature in the verified 2026-10-15 wire shape."""
    payload: dict[str, Any] = {
        "feature": feature,
        "files": files,
        "how_to_test": how_to_test,
        "layer": "",
        "refs": [],
        "defs": [],
        "session_id": session_id,
        "ts": ts,
    }
    if name:
        payload["name"] = name
    return payload


def _features_body(features: list[dict[str, Any]]) -> str:
    """A features payload in the verified wire shape: ``{count, since, layer, features}``."""
    return json.dumps({
        "count": len(features),
        "since": "24h",
        "layer": "",
        "features": features,
    })


@pytest.fixture
def cn(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """A hermetic ContextNest: ``cn_client._get`` is stubbed per path.

    ``state["features"]`` is the body returned for ``/api/v1/features``;
    ``state["calls"]`` records every requested path so request-shape tests
    can assert on it. ``state["error"]`` makes ``_get`` raise (the same one
    the cached_section slot converts to ``_unreachable``).
    """
    state: dict[str, Any] = {
        "features": _features_body([]),
        "error": None,
        "calls": [],
    }

    def fake_get(path: str, *, timeout: float | None = None) -> str:
        state["calls"].append(path)
        if state["error"] is not None:
            raise state["error"]
        if "/api/v1/features" in path:
            return state["features"]
        return "{}"

    monkeypatch.setattr(cn_client, "_get", fake_get)
    return state


# ── 1. happy path: one row per in-project feature ──────────────────────────


def test_shipped_today_renders_one_row_per_in_project_feature(
    home: Path, cn: dict[str, Any],
) -> None:
    """Three features, two in-project and one out-of-project. The section
    renders exactly the two in-project ones; each row carries the feature
    name, the verbatim files list as the sub cell, an Open session action,
    and a Re-test action whose draft-thread seed carries ``how_to_test``
    verbatim as the first prompt — the page never executes the recipe.
    """
    in_proj_1 = _feature(
        feature="shipped-1", session_id="aaaaaaaa-0000-4000-8000-000000000001",
        files=["README.md"], how_to_test="curl http://localhost/ping",
        ts=10)
    in_proj_2 = _feature(
        feature="shipped-2", session_id="aaaaaaaa-0000-4000-8000-000000000002",
        files=["README.md", "new/path.py"], how_to_test="POST /api/run",
        ts=20)
    out_proj = _feature(
        feature="shipped-outside", session_id="aaaaaaaa-0000-4000-8000-000000000003",
        files=["../../elsewhere/unrelated.py"], how_to_test="n/a",
        ts=15)
    cn["features"] = _features_body([in_proj_1, in_proj_2, out_proj])

    page = build_page(home, "inbox")
    assert page["ok"] is True

    sec = _section(page, "Shipped since yesterday")
    assert sec["type"] == "list" and sec["full"] is True
    rows = {row["t"]: row for row in sec["items"]}
    assert set(rows) == {"shipped-1", "shipped-2"}, (
        f"only in-project features must render, got {sorted(rows)}")
    assert "shipped-outside" not in rows, (
        "the out-of-project feature leaked into the page")

    row1 = rows["shipped-1"]
    assert "README.md" in row1["sub"]
    actions = {a["label"]: a for a in row1.get("acts") or []}
    assert "Open session" in actions, f"a session link is required, got {list(actions)}"
    assert "Re-test" in actions, f"a Re-test button is required, got {list(actions)}"
    session_do = actions["Open session"]["do"]
    assert session_do == {"page": "cn_session", "args": {"id": "aaaaaaaa-0000-4000-8000-000000000001"}}
    test_do = actions["Re-test"]["do"]
    assert "thread" in test_do
    assert "shipped-1" in test_do["thread"]
    assert "curl http://localhost/ping" in test_do["thread"], (
        f"the thread seed must carry how_to_test verbatim, got {test_do['thread']!r}")

    row2 = rows["shipped-2"]
    test2 = next(a for a in row2["acts"] if a["label"] == "Re-test")
    assert "POST /api/run" in test2["do"]["thread"], (
        "the second feature's how_to_test must appear in its thread seed")
    assert "README.md" in row2["sub"] and "new/path.py" in row2["sub"]


# ── 2. out-of-project filter is the only correct gate ──────────────────────


def test_shipped_today_drops_features_with_no_in_project_file_even_when_session_matches(
    home: Path, cn: dict[str, Any],
) -> None:
    """A feature whose session_id/cwd matches the project but whose
    ``files[]`` all point outside the project root (absolute paths outside
    it, or ``..``-escapes) is dropped. The filter is files-based, not
    session-based — explicit acceptance."""
    project = _project(home)
    # Absolute path outside the project root.
    out_abs = _feature(
        feature="out-by-abs-path",
        session_id="aaaaaaaa-0000-4000-8000-000000000010",
        files=["/other/repo/foo.py"], how_to_test="x",
        ts=10)
    # ``..``-escape from a relative path resolves outside the project root.
    out_dotdot = _feature(
        feature="out-by-dotdot",
        session_id="aaaaaaaa-0000-4000-8000-000000000011",
        files=["../../../etc/passwd"], how_to_test="x",
        ts=20)
    # A nested-checkout absolute path (sibling of the project root) is also dropped.
    out_nested = _feature(
        feature="out-by-nested",
        session_id="aaaaaaaa-0000-4000-8000-000000000012",
        files=[str(Path(project).parent / "sibling" / "x.py")],
        how_to_test="x",
        ts=30)
    # One in-project control row.
    in_proj = _feature(
        feature="in-project-control",
        session_id="aaaaaaaa-0000-4000-8000-000000000013",
        files=["README.md"], how_to_test="y",
        ts=40)
    cn["features"] = _features_body([out_abs, out_dotdot, out_nested, in_proj])

    page = build_page(home, "inbox")
    sec = _section(page, "Shipped since yesterday")
    titles = [r["t"] for r in sec["items"]]
    assert "in-project-control" in titles, (
        f"the in-project feature must render, got {titles}")
    for dropped in ("out-by-abs-path", "out-by-dotdot", "out-by-nested"):
        assert dropped not in titles, (
            f"{dropped!r} must be dropped (files live outside the project root), got {titles}")


# ── 3. wire-shape tolerance: feature / name / label ─────────────────────────


def test_shipped_today_reads_feature_falling_back_to_name_and_label(
    home: Path, cn: dict[str, Any],
) -> None:
    """The verified 2026-10-15 key is ``feature``; older payloads used
    ``name`` or ``label``. All three must render the row's text on the
    page; an empty name drops the row (silently, the way cn_session._features
    does it)."""
    cn["features"] = _features_body([
        # ``feature`` (live wire key) wins.
        _feature(feature="via-feature-key", files=["README.md"],
                 session_id="s-1", how_to_test="x", ts=10),
        # Older payload: only ``name``.
        {"name": "via-name-key", "files": ["README.md"],
         "session_id": "s-2", "how_to_test": "y", "ts": 5},
        # Even older payload: only ``label``.
        {"label": "via-label-key", "files": ["README.md"],
         "session_id": "s-3", "how_to_test": "z", "ts": 1},
        # Empty name: the row is dropped (cn_session convention).
        _feature(feature="", files=["README.md"],
                 session_id="s-4", how_to_test="empty", ts=0),
    ])

    page = build_page(home, "inbox")
    sec = _section(page, "Shipped since yesterday")
    titles = {r["t"] for r in sec["items"]}
    assert "via-feature-key" in titles
    assert "via-name-key" in titles
    assert "via-label-key" in titles
    assert "" not in titles and not any(not t for t in titles), (
        f"a blank-named row leaked into the page, got {titles!r}")


# ── 4. empty payload — friendly one-line state ─────────────────────────────


def test_shipped_today_empty_features_returns_one_line_no_features(
    home: Path, cn: dict[str, Any],
) -> None:
    """An empty ``features`` payload is a *healthy* read with zero in-project
    rows — the section renders the 'No features shipped' one-line state and
    adds NO chip (the chip path requires a healthy read with at least one
    shipped item)."""
    cn["features"] = _features_body([])

    page = build_page(home, "inbox")
    sec = _section(page, "Shipped since yesterday")
    assert sec["type"] == "list" and len(sec["items"]) == 1
    row = sec["items"][0]
    assert "No features shipped" in row["t"], (
        f"the empty-state line must say 'No features shipped', got {row['t']!r}")
    chips = _chip_map(page)
    assert not any("shipped" in t.lower() for t in chips), (
        f"a healthy-but-empty read must add no chip, got {list(chips)}")


def test_shipped_today_only_out_of_project_features_returns_one_line(
    home: Path, cn: dict[str, Any],
) -> None:
    """A payload where every feature is out-of-project renders the same
    one-line 'No features shipped' state — the filter drops them all."""
    cn["features"] = _features_body([_feature(
        feature="only-out-of-project",
        files=["/tmp/elsewhere.py"],
        session_id="s-only-out",
        how_to_test="x",
    )])

    page = build_page(home, "inbox")
    sec = _section(page, "Shipped since yesterday")
    assert sec["type"] == "list" and len(sec["items"]) == 1
    assert "No features shipped" in sec["items"][0]["t"]


# ── 5. missing how_to_test — Re-test still works ───────────────────────────


def test_shipped_today_renders_re_test_button_when_how_to_test_is_missing(
    home: Path, cn: dict[str, Any],
) -> None:
    """A feature with no ``how_to_test`` still gets a Re-test button — the
    seed text just stays shorter (the same rule ``cn_session._features``
    follows). The page must never synthesize a fake recipe."""
    cn["features"] = _features_body([_feature(
        feature="shipped-no-recipe",
        files=["README.md"],
        session_id="aaaaaaaa-0000-4000-8000-000000000099",
        how_to_test="",
        ts=10,
    )])

    page = build_page(home, "inbox")
    sec = _section(page, "Shipped since yesterday")
    assert sec["type"] == "list" and len(sec["items"]) == 1
    row = sec["items"][0]
    acts = {a["label"]: a for a in row.get("acts") or []}
    assert "Re-test" in acts, (
        f"a feature with no how_to_test still gets a Re-test button, got {list(acts)}")
    thread = acts["Re-test"]["do"].get("thread") or ""
    assert "shipped-no-recipe" in thread, (
        "the thread seed still names the feature even without how_to_test")
    # And the seed does NOT contain a fabricated recipe — cn_session's
    # empty-recipe seed is exactly "Re-test the **<name>** feature."
    assert "How to test" not in thread, (
        f"the empty-recipe seed must not carry a fabricated 'How to test:' block, got {thread!r}")


# ── 6. degraded states — one-line, no chip, never silently blank ──────────


def test_shipped_today_cn_down_renders_one_line_state(
    home: Path, cn: dict[str, Any],
) -> None:
    """``_get`` returning ``{}`` (CN is down) renders the one-line
    ``_cn.section_status`` state, never silently empty, and adds no chip.
    The existing inbox keeps rendering unchanged on this degraded path so
    ``test_ide_pages_inbox_kanban`` keeps passing without a CN stub."""
    cn["features"] = "{}"  # server returned an empty body, / HTTP error

    page = build_page(home, "inbox")
    sec = _section(page, "Shipped since yesterday")
    assert sec["type"] == "list"
    assert len(sec["items"]) == 1
    assert sec["items"][0] == _cn.section_status({"_unreachable": True}), (
        f"a down CN must render the section_status one-liner, got {sec['items'][0]}")
    chips = _chip_map(page)
    assert not any("shipped" in t.lower() for t in chips), (
        f"no chip may render on a down CN, got {list(chips)}")


def test_shipped_today_cn_disabled_renders_one_line_state(
    home: Path, cn: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``MO_DISABLE_CN=1`` short-circuits before any ``_get`` is fired and
    renders the one-line state. A disabled CN must NOT make the request."""
    monkeypatch.setenv("MO_DISABLE_CN", "1")
    try:
        page = build_page(home, "inbox")
        sec = _section(page, "Shipped since yesterday")
        assert sec["type"] == "list"
        assert sec["items"][0] == _cn.section_status({"_disabled": True}), (
            f"MO_DISABLE_CN=1 must render the section_status one-liner, got {sec['items'][0]}")
        # The features endpoint must not have been called.
        assert not any("/api/v1/features" in p for p in cn["calls"]), (
            f"a disabled CN must not be read, got {cn['calls']}")
    finally:
        monkeypatch.delenv("MO_DISABLE_CN", raising=False)


def test_shipped_today_cn_transport_error_renders_one_line_state(
    home: Path, cn: dict[str, Any],
) -> None:
    """A transport error (``_get`` raises ``OSError``) is converted to the
    one-line state by ``_cn.render_cached_section``. The page stays
    ``ok: true`` and adds no chip."""
    cn["error"] = OSError("connection refused")

    page = build_page(home, "inbox")
    sec = _section(page, "Shipped since yesterday")
    assert sec["type"] == "list"
    assert sec["items"][0] == _cn.section_status({"_unreachable": True}), (
        f"a transport error must render the section_status one-liner, got {sec['items'][0]}")


# ── 7. Re-test seed must not execute the recipe ─────────────────────────────


def test_shipped_today_re_test_seeds_a_draft_thread_and_never_executes(
    home: Path, cn: dict[str, Any],
) -> None:
    """The Re-test button carries a ``thread`` action (draft-thread seed),
    NOT a ``cli`` action. The page never executes ``how_to_test``; an IDE
    that the operator clicks the button sees a draft agent thread whose
    first prompt is the recipe text — the operator submits it, not the
    page."""
    cn["features"] = _features_body([_feature(
        feature="must-seed-not-execute",
        files=["README.md"],
        session_id="aaaaaaaa-0000-4000-8000-000000000888",
        how_to_test="This text must ride the thread seed, not run.",
        ts=10,
    )])

    page = build_page(home, "inbox")
    sec = _section(page, "Shipped since yesterday")
    row = sec["items"][0]
    test_action = next(a for a in row["acts"] if a["label"] == "Re-test")
    do = test_action["do"]
    assert "thread" in do and isinstance(do["thread"], str), (
        f"the Re-test action must be a thread action, got {do}")
    assert "cli" not in do, (
        f"the Re-test action must never be a cli action, got {do}")
    assert "This text must ride the thread seed, not run." in do["thread"], (
        f"how_to_test must appear verbatim in the thread seed, got {do['thread']!r}")


# ── 8. request shape — one call, since=24h, no project= ─────────────────────


def test_shipped_today_request_path_carries_since_24h_and_no_project(
    home: Path, cn: dict[str, Any],
) -> None:
    """The fetched path is exactly ``/api/v1/features?since=24h`` — no
    ``project=`` (that key would filter by session cwd, defeating the
    client-side ``files[]`` containment filter). The path is requested
    exactly once per page build (cache hits don't issue a second call)."""
    cn["features"] = _features_body([_feature(
        feature="x", files=["README.md"],
        session_id="s-only-1", how_to_test="y", ts=10,
    )])
    build_page(home, "inbox")

    feature_calls = [p for p in cn["calls"] if "/api/v1/features" in p]
    assert len(feature_calls) == 1, (
        f"exactly one features call per page build, got {len(feature_calls)}: {feature_calls}")
    q = urllib.parse.parse_qs(urllib.parse.urlparse(feature_calls[0]).query)
    assert q.get("since") == ["24h"], (
        f"the request must carry since=24h, got {feature_calls[0]}")
    assert "project" not in q, (
        f"the request must NOT carry a project= query (client-side filter "
        f"is files-based, not session-cwd-based), got {feature_calls[0]}")


# ── 9. the existing inbox stays unchanged when CN is unreachable ───────────


def test_shipped_today_does_not_break_existing_inbox_when_cn_unreachable(
    home: Path,
) -> None:
    """With no CN stub (a real CI home, CN is unreachable), the existing
    inbox still renders cleanly and the chip counts are unchanged. The
    'Shipped since yesterday' section is the one-line state and the page
    builds ``ok: true`` — the no-chip invariant rules out a regression on
    the kanban page's tests, which do not stub CN."""
    # ``home`` does NOT stub cn_client._get; the real client tries the
    # default ``CN_BASE_URL`` and gets a connection error, which ``_get``
    # swallows to ``"{}"``.
    page = build_page(home, "inbox")
    assert page["ok"] is True, f"the inbox must build with CN down, got {page}"
    sec = _section(page, "Shipped since yesterday")
    assert sec["type"] == "list"
    chips = _chip_map(page)
    assert not any("shipped" in t.lower() for t in chips), (
        f"a CN-down render must not add a 'shipped' chip, got {list(chips)}")
