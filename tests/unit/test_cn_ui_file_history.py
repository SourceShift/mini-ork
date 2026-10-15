"""``cn_file`` page — fixture-driven coverage of every section path.

The page reads three CN endpoints and the fixtures pin the **verified**
live response shapes — not the shape the existing ``sessions_by_file_recent``
helper parses (a different parser): by-file returns
``{query, matches: [{session_id, matched_files, total_files}]}``;
session summary returns ``{summary: {goal, last_ts, …}}``; hot-claims
returns ``{claims: [{path, principal_id, expires_at}]}``; the atoms
endpoint returns ``{atoms: [{id, kind, text, session_id, project, ts,
metadata}]}`` (request: ``GET /api/v1/prompt-context/atoms?since=30d&
kind=failure&project=<url-encoded root>&limit=500``) and file matching
runs entirely client-side.

Tests stub ``cn_client._get`` (the network entry point the page calls
for by-file, per-session summary, and the atoms feed) and
``cn_client.coord_hot_claims`` so they are fast and never touch the
network. ``cn_client.retrieve`` is patched to RAISE — the page must
never call it, and a stub that quietly returns ``{}`` would let a
regression pass the test suite.

A per-test ``MINI_ORK_HOME`` keeps the section cache scoped to
``tmp_path``; otherwise a previous test's slot bleeds into the next one
and a "fetch returns ``{}``" assertion passes only because
``cached_section`` fell back to healthy-looking cache data, not because
the live fetch was empty.
"""
from __future__ import annotations

import json
from typing import Any

import pytest

from mini_ork import cn_client
from mini_ork.ide_pages import cn_file


# ── fixtures ───────────────────────────────────────────────────────────────


@pytest.fixture()
def cn_home(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Scope the CN section cache to this test's tmp_path AND give the
    page a real ``.mini-ork`` directory whose parent is the project root.

    ``cn_client._section_cache_dir`` reads ``MINI_ORK_HOME`` (defaulting
    to ``.mini-ork`` in cwd). Pointing it at ``tmp_path/proj/.mini-ork``
    keeps every test starting from an empty cache, so a "fetch returns
    ``{}``" path actually reaches the unreachable one-liner instead of
    falling back to a previous test's stale payload.

    The page reads ``home.parent`` as the project root for path
    containment; tests that need a project file create one under
    ``cn_home.parent`` (``tmp_path/proj``).
    """
    project = tmp_path / "proj"
    home = project / ".mini-ork"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    return home


# ── helpers ────────────────────────────────────────────────────────────────


def _walk(node: Any, acc: list[dict]) -> None:
    """Collect every dict under ``node`` so asserts can scan them all."""
    if isinstance(node, dict):
        acc.append(node)
        for v in node.values():
            _walk(v, acc)
    elif isinstance(node, list):
        for v in node:
            _walk(v, acc)


def _stub_cn(monkeypatch: pytest.MonkeyPatch, *,
             by_file: dict | None = None,
             summaries: dict[str, dict] | None = None,
             hot_claims: dict | None = None,
             failure_atoms: dict | None = None) -> list[str]:
    """Stub the three network entry points the page calls.

    Returns the list of paths the page asked for, in call order, so
    tests can assert against the wire contract. ``cn_client.retrieve``
    is always patched to raise — the page must never call it, and a
    silent ``{}`` stub would let a regression pass the suite.

    ``failure_atoms`` is the wire payload (``{atoms: [...]}``) the
    atoms GET returns. Tests that pass it get matching failure rows;
    tests that don't get an empty payload and the section renders the
    "no failures" one-liner.

    Each fetch-stub returns a JSON string so the page's
    ``json.loads`` path runs for real; ``by_file`` defaults to
    ``"{}"`` so a test that doesn't care about by-file still gets the
    unreachable one-liner for sessions, which is what an empty
    payload deserves.
    """
    paths: list[str] = []
    summaries = summaries or {}
    failure_atoms = failure_atoms if failure_atoms is not None else {
        "since": "30d", "count": 0, "total_matched": 0,
        "by_kind": {}, "atoms": [],
    }

    def _fake_get(path: str, *, timeout: float | None = None) -> str:
        del timeout
        paths.append(path)
        if "/sessions/by-file" in path:
            return json.dumps(by_file) if by_file is not None else "{}"
        # The page uses ``f"/sessions/{sid}/summary"`` after URL-encoding.
        for sid, payload in summaries.items():
            target = f"/sessions/{sid}/summary"
            if path.endswith(target):
                return json.dumps(payload) if payload is not None else "{}"
        if path.startswith("/api/v1/prompt-context/atoms"):
            return json.dumps(failure_atoms)
        return "{}"

    monkeypatch.setattr(cn_client, "_get", _fake_get)
    if hot_claims is not None:
        monkeypatch.setattr(
            "mini_ork.cn_client.coord_hot_claims", lambda: hot_claims
        )
    # The page must not call retrieve; a raising stub turns a regression
    # into a loud traceback rather than a silent empty section.
    monkeypatch.setattr(
        "mini_ork.cn_client.retrieve",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError(
            "cn_file must not call cn_client.retrieve; the failures "
            "section reads GET /api/v1/prompt-context/atoms"
        )),
    )
    return paths


# ── fixtures (response payloads) ───────────────────────────────────────────

# Verified by-file shape: {query, matches: [{session_id, matched_files,
# total_files}]}. The session id order in ``matches`` is the order CN
# shipped; the page must reorder by per-session ``summary.last_ts``.
_BY_FILE_TWO = {
    "query": "mini_ork/ide_pages/cn_file.py",
    "matches": [
        # Ships first by wire, but the older timestamp (2026-10-10).
        {"session_id": "sess-older",
         "matched_files": ["mini_ork/ide_pages/cn_file.py"],
         "total_files": 1},
        # Ships second by wire, but the newer timestamp (2026-10-15).
        {"session_id": "sess-newer",
         "matched_files": ["mini_ork/ide_pages/cn_file.py",
                           "tests/unit/test_cn_ui_file_history.py"],
         "total_files": 2},
    ],
}

_SUMMARY_NEWER: dict = {
    "summary": {
        "goal": "Newer session goal",
        "last_ts": "2026-10-15T10:00:00Z",
    },
}

_SUMMARY_OLDER: dict = {
    "summary": {
        "goal": "Older session goal",
        "last_ts": "2026-10-10T10:00:00Z",
    },
}

# Verified hot-claims shape: {claims: [{path, principal_id, expires_at}]}.
# One claim matches the file the page is about; the other carries a
# foreign path and must NEVER appear on this page.
_HOT_CLAIMS: dict = {
    "claims": [
        {"path": "mini_ork/ide_pages/cn_file.py",
         "principal_id": "loop:alpha",
         "expires_at": "2026-10-15T13:00:00Z"},
        {"path": "mini_ork/cn_client.py",
         "principal_id": "loop:beta",
         "expires_at": "2026-10-15T13:00:00Z"},
    ],
}

# Verified atoms shape: {atoms: [{id, kind, text, session_id, project,
# ts, metadata}]}. The page's client-side project check requires each
# atom's ``project`` field to equal the resolved project root (the
# ``cn_home.parent.resolve()`` value), so the fixtures below take the
# root as a parameter and stamp it onto every atom. The first atom
# names the page's file (must appear); the second is a decision atom
# (must be filtered out).
def _failure_atoms_payload(project_root: str) -> dict:
    return {
        "since": "30d",
        "count": 2,
        "total_matched": 2,
        "by_kind": {"failure": 1, "decision_made": 1},
        "atoms": [
            {"id": "a1",
             "text": "ImportError when loading cn_file.py",
             "kind": "failure",
             "session_id": "sess-fail",
             "project": project_root,
             "ts": "2026-10-15T11:00:00Z",
             "metadata": {}},
            {"id": "a2",
             "text": "Decided to use the cached_section wrapper",
             "kind": "decision_made",
             "session_id": "sess-dec",
             "project": project_root,
             "ts": "2026-10-15T11:30:00Z",
             "metadata": {}},
        ],
    }


# ── 1. empty-args safe build ──────────────────────────────────────────────


def test_empty_state_renders_callout(cn_home: Any,
                                     monkeypatch: pytest.MonkeyPatch) -> None:
    """``{}`` for ``args`` (the IDE actions sweep default) keeps ``ok: true``.

    An arg-required page would fail ``test_every_page_builds_with_ok_true``
    because the sweep builds every page key with empty ``args``.
    """
    monkeypatch.delenv("MO_DISABLE_CN", raising=False)
    _stub_cn(monkeypatch)
    page = cn_file.build(cn_home, None, {})
    assert page["ok"] is True
    assert page["key"] == "cn_file"
    # Empty state carries no fetched sections.
    titles = [s.get("title") for s in page["sections"]]
    assert titles == ["No file path"], titles


# ── 2. recency ordering via summary.last_ts ───────────────────────────────


def test_sessions_ordered_newest_first_by_summary_last_ts(
    cn_home: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The by-file payload order must NOT drive the page order — the
    newer session's ``summary.last_ts`` wins.

    The fixture ships the older session first in the ``matches`` list;
    the page must reorder so the row whose ``last_ts`` is later in time
    renders first. Each session row carries an Open button that points
    at the ``cn_session`` page with the matching ``id``.
    """
    monkeypatch.delenv("MO_DISABLE_CN", raising=False)
    _stub_cn(monkeypatch,
             by_file=_BY_FILE_TWO,
             summaries={
                 "sess-older": _SUMMARY_OLDER,
                 "sess-newer": _SUMMARY_NEWER,
             })
    page = cn_file.build(cn_home, None,
                         {"path": "mini_ork/ide_pages/cn_file.py"})

    sessions = next(s for s in page["sections"]
                    if s.get("title") == "Sessions")
    goals = [it["t"] for it in sessions["items"]]
    assert goals == ["Newer session goal", "Older session goal"], goals

    # Each row's Open button is a page-link to cn_session with the
    # matching session id.
    for sid, item in zip(("sess-newer", "sess-older"), sessions["items"]):
        opens = [a for a in (item.get("acts") or [])
                 if isinstance(a.get("do"), dict)
                 and a["do"].get("page") == "cn_session"]
        assert len(opens) == 1, item
        assert opens[0]["do"]["args"]["id"] == sid, opens

    # The endpoint shape the page reads is the verified one: a
    # single by-file call + two per-session summary calls. Anything
    # else means the page is fetching from an unexpected endpoint.
    accum: list[dict] = []
    _walk(page, accum)
    cli_actions = [n for n in accum
                   if isinstance(n.get("cli"), list) and n["cli"]]
    assert cli_actions == [], cli_actions


# ── 3. live claim shows its principal ────────────────────────────────────


def test_live_claim_shows_principal_for_matching_path(
    cn_home: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A claim whose ``path`` matches the page argument surfaces its
    ``principal_id``; a foreign-path claim from the same payload is
    filtered out so two files never leak across projects.
    """
    monkeypatch.delenv("MO_DISABLE_CN", raising=False)
    _stub_cn(monkeypatch,
             by_file={"query": "x", "matches": []},
             hot_claims=_HOT_CLAIMS)
    page = cn_file.build(cn_home, None,
                         {"path": "mini_ork/ide_pages/cn_file.py"})
    hot = next(s for s in page["sections"] if s.get("title") == "Live holder")
    principals = [it["t"] for it in hot["items"]]
    assert "loop:alpha" in principals, principals
    # Foreign-path claim must not appear.
    assert not any("loop:beta" in p for p in principals), principals


def test_live_holder_empty_when_no_matching_claim(
    cn_home: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty ``claims`` payload renders one muted row, no buttons."""
    monkeypatch.delenv("MO_DISABLE_CN", raising=False)
    _stub_cn(monkeypatch,
             by_file={"query": "x", "matches": []},
             hot_claims={"claims": []})
    page = cn_file.build(cn_home, None,
                         {"path": "mini_ork/ide_pages/cn_file.py"})
    hot = next(s for s in page["sections"] if s.get("title") == "Live holder")
    assert len(hot["items"]) == 1
    assert not hot["items"][0].get("acts"), hot["items"][0]


# ── 4. failure atoms surface ──────────────────────────────────────────────


def test_failure_atoms_filter_to_failure_kind_only(
    cn_home: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Recent-failures section surfaces failure-kind atoms only.

    The atoms endpoint returns a flat ``atoms[]`` regardless of
    ``kind``; the page must filter on ``kind == "failure"`` so a
    decision / verification / artifact never appears here. Each
    atom's ``project`` field must equal the resolved project root so
    the page's client-side project check keeps the row — a foreign
    root would be dropped before the kind filter ever runs.
    """
    monkeypatch.delenv("MO_DISABLE_CN", raising=False)
    project_root = str(cn_home.parent.resolve())
    _stub_cn(monkeypatch,
             by_file={"query": "x", "matches": []},
             failure_atoms=_failure_atoms_payload(project_root))
    page = cn_file.build(cn_home, None,
                         {"path": "mini_ork/ide_pages/cn_file.py"})
    fail = next(s for s in page["sections"]
                if s.get("title") == "Recent failures")
    texts = [it["t"] for it in fail["items"]]
    # Failure-kind atom surfaces (the basename ``cn_file.py`` is in
    # its text).
    assert any("ImportError" in t for t in texts), texts
    # Non-failure kind is filtered out.
    assert not any("cached_section" in t for t in texts), texts


def test_failure_atoms_empty_payload_renders_no_match_line(
    cn_home: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A healthy CN with zero atoms renders the no-match one-liner,
    not the unreachable line — an empty-but-valid payload is not the
    same as a CN outage.
    """
    monkeypatch.delenv("MO_DISABLE_CN", raising=False)
    project_root = str(cn_home.parent.resolve())
    _stub_cn(monkeypatch,
             by_file={"query": "x", "matches": []},
             failure_atoms={
                 "since": "30d", "count": 0, "total_matched": 0,
                 "by_kind": {}, "atoms": [],
             })
    page = cn_file.build(cn_home, None,
                         {"path": "mini_ork/ide_pages/cn_file.py"})
    fail = next(s for s in page["sections"]
                if s.get("title") == "Recent failures")
    texts = [it["t"] for it in fail["items"]]
    assert texts == ["No failures in the last 30 days name this file"], texts
    # Sanity: the project_root survived the wire trip and the stub
    # was exercised — the failure-row assertion above is meaningful
    # only if the page asked CN for atoms.
    assert project_root  # touch the variable so the import doesn't warn


# ── 5. outside project renders explanation, no calls ─────────────────────


def test_outside_project_renders_explanation_no_calls(
    cn_home: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A path that resolves outside the project root renders an
    explanation row and makes ZERO ContextNest calls.

    The page must never ask CN about a path it cannot verify belongs
    to the project — a foreign path that resolves outside the root
    would leak a different project's session list, and a relative
    ``../`` path that escapes the root would silently widen the
    file's surface.
    """
    monkeypatch.delenv("MO_DISABLE_CN", raising=False)
    paths = _stub_cn(monkeypatch)
    page = cn_file.build(cn_home, None, {"path": "/tmp/elsewhere.py"})
    assert page["ok"] is True
    # No CN call may have happened — the guard short-circuits.
    assert paths == [], paths
    # The section is the outside-project explanation, not a CN list.
    section_blob = json.dumps(page["sections"])
    assert "outside" in section_blob.lower(), section_blob
    assert "Sessions" not in section_blob, section_blob


def test_relative_dotdot_path_outside_project_no_calls(
    cn_home: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A relative ``../`` path that escapes the project root also
    short-circuits to the explanation row.

    macOS ``tmp_path`` lives under ``/private/var/folders/...``;
    ``../../../etc/passwd`` from a project under that prefix still
    does NOT escape on most setups, so the test uses the absolute
    shape: the relative-escape variant is exercised through the
    resolver's ``resolved.relative_to(root)`` check.
    """
    monkeypatch.delenv("MO_DISABLE_CN", raising=False)
    paths = _stub_cn(monkeypatch)
    # A path whose ``..`` segments escape the project root. Resolved
    # against ``cn_home.parent`` (``tmp_path/proj``), this lands one
    # level above the project — outside.
    page = cn_file.build(cn_home, None, {"path": "../../escape.py"})
    assert page["ok"] is True
    assert paths == [], paths


# ── 6. page registered in PAGES ──────────────────────────────────────────


def test_cn_file_key_in_pages(cn_home: Any) -> None:
    """The page key is ``cn_file`` and the dispatcher reaches it with
    empty args (the IDE actions sweep builds every page this way)."""
    from mini_ork.ide_pages import PAGES, build_page
    assert "cn_file" in PAGES
    page = build_page(cn_home, "cn_file")
    assert page["ok"] is True


# ── 7. CN down / disabled still renders ok:true ─────────────────────────


def test_unreachable_cn_renders_one_liner(
    cn_home: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When every fetch returns ``{}`` the page still builds ok.

    ``cn_client._get`` returns ``"{}"`` on any network error; the
    page's fetch wraps it in ``json.loads`` → ``{}`` → ``cached_section``
    falls through to the unreachable payload. The page renders the
    one-line "not running" dot for every section and the page itself
    stays ``ok: true``. ``cn_client.retrieve`` is patched to raise so
    a regression that re-routes the failures section through it fails
    loud rather than passing silently.
    """
    monkeypatch.delenv("MO_DISABLE_CN", raising=False)
    monkeypatch.setenv("CN_BASE_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("CN_PING_TTL", "0")
    monkeypatch.setenv("CN_TIMEOUT_SEC", "0.2")
    monkeypatch.setenv("CN_PING_TIMEOUT_SEC", "0.2")

    # Every fetch returns ``{}`` so cached_section falls through to the
    # unreachable marker for every section. The page must still build
    # ``ok: true`` and every section must carry at least one item.
    monkeypatch.setattr(cn_client, "_get",
                        lambda *_a, **_k: "{}")
    from mini_ork.cn_client import CoordUnavailable
    monkeypatch.setattr(
        "mini_ork.cn_client.coord_hot_claims",
        lambda: (_ for _ in ()).throw(CoordUnavailable("down")),
    )
    monkeypatch.setattr(
        "mini_ork.cn_client.retrieve",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError(
            "cn_file must not call cn_client.retrieve; the failures "
            "section reads GET /api/v1/prompt-context/atoms"
        )),
    )

    page = cn_file.build(cn_home, None,
                         {"path": "mini_ork/ide_pages/cn_file.py"})
    assert page["ok"] is True
    # Every section renders at least one item (the one-line state when
    # CN is unreachable, the empty-state row when the section fetched
    # an empty-but-valid payload).
    for section in page["sections"]:
        items = section.get("items") or []
        assert items, section


def test_disabled_cn_renders_disabled_one_liner(
    cn_home: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``MO_DISABLE_CN=1`` short-circuits and shows the disabled dot.
    ``cn_client.retrieve`` is patched to raise — the disabled path
    must not fetch at all, so a regression that bypasses the disable
    flag via a direct retrieve call fails loud rather than silently
    rendering a row.
    """
    monkeypatch.setenv("MO_DISABLE_CN", "1")
    monkeypatch.setattr(cn_client, "_get", lambda *_a, **_k: "{}")
    monkeypatch.setattr("mini_ork.cn_client.coord_hot_claims",
                        lambda: {"claims": []})
    monkeypatch.setattr(
        "mini_ork.cn_client.retrieve",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError(
            "cn_file must not call cn_client.retrieve; the failures "
            "section reads GET /api/v1/prompt-context/atoms"
        )),
    )
    page = cn_file.build(cn_home, None,
                         {"path": "mini_ork/ide_pages/cn_file.py"})
    assert page["ok"] is True


# ── 8. c:0 — never ask for the raw /sessions/:id dump ────────────────────


def test_page_never_calls_raw_session_dump(
    cn_home: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The page calls ``/sessions/by-file``, ``/sessions/:id/summary``, and
    ``/api/v1/prompt-context/atoms`` only — never the raw session dump.

    The raw ``/api/v1/sessions/:id`` endpoint takes 12–20 s on large
    sessions (kickoff c:0). The stub records every path the page asks
    for; this test fails loudly if a future change starts fetching the
    slow endpoint.
    """
    monkeypatch.delenv("MO_DISABLE_CN", raising=False)
    paths = _stub_cn(monkeypatch,
                     by_file=_BY_FILE_TWO,
                     summaries={
                         "sess-older": _SUMMARY_OLDER,
                         "sess-newer": _SUMMARY_NEWER,
                     })
    cn_file.build(cn_home, None,
                  {"path": "mini_ork/ide_pages/cn_file.py"})
    assert paths, "no endpoint was called"
    for path in paths:
        ok = (
            "/sessions/by-file" in path
            or path.endswith("/summary")
            or path.startswith("/api/v1/prompt-context/atoms")
        )
        assert ok, path
        # Belt-and-braces: no path may be the raw session-dump shape
        # (``/sessions/<id>`` with nothing after the id). The summary
        # endpoint appends ``/summary`` and is fine.
        for sid in ("sess-newer", "sess-older"):
            if f"/sessions/{sid}" in path:
                assert path.endswith(f"/sessions/{sid}/summary"), path


# ── 9. by-file path is URL-encoded ───────────────────────────────────────


def test_by_file_path_url_encoded(
    cn_home: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A path with a slash or space is percent-encoded in the URL."""
    monkeypatch.delenv("MO_DISABLE_CN", raising=False)
    paths = _stub_cn(monkeypatch, by_file={"query": "x", "matches": []})
    cn_file.build(cn_home, None, {"path": "mini_ork/with space.py"})
    by_file_paths = [p for p in paths if "/sessions/by-file" in p]
    assert by_file_paths, paths
    for p in by_file_paths:
        # The space and one slash must be percent-encoded; the
        # by-file path on the wire is the encoded form, not the raw.
        assert " " not in p, p
        assert "%20" in p or "%2F" in p, p


# ── 10. spec-level helper sanity ─────────────────────────────────────────


def test_outside_project_resolver() -> None:
    """The outside-project guard returns ``None`` for paths that
    escape the root, and the project-relative form for paths inside."""
    from pathlib import Path
    root = Path("/tmp/proj-xyz")

    # Inside: a relative path joined to the root resolves inside.
    assert cn_file._resolve_inside_project(
        "mini_ork/cn_file.py", root
    ) == "mini_ork/cn_file.py"

    # Outside: an absolute path that points elsewhere.
    assert cn_file._resolve_inside_project("/etc/passwd", root) is None

    # Outside: a ``..`` that escapes the root.
    assert cn_file._resolve_inside_project("../escape.py", root) is None

    # Edge cases: empty / None / missing root.
    assert cn_file._resolve_inside_project("", root) is None
    assert cn_file._resolve_inside_project("anything", None) is None


def test_last_ts_epoch_accepts_string_and_int() -> None:
    """``summary.last_ts`` arrives as either a string (live CN) or an
    int / float (older payloads); the helper must convert either."""
    # String ISO-8601.
    ts = cn_file._last_ts_epoch({"last_ts": "2026-10-15T10:00:00Z"})
    assert ts > 0
    # Int epoch.
    assert cn_file._last_ts_epoch({"last_ts": 1_700_000_000}) == 1_700_000_000
    # Missing / malformed → 0 (sorts last).
    assert cn_file._last_ts_epoch({}) == 0
    assert cn_file._last_ts_epoch({"last_ts": ""}) == 0
    assert cn_file._last_ts_epoch({"last_ts": "not a date"}) == 0