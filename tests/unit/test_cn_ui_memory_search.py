"""``cn search`` + the Context IDE Search tab.

Two co-designed surfaces — the ``mini-ork cn search`` CLI verb that writes
the cache slot, and the Search tab on the Context page that reads it
verbatim (never re-hits ContextNest on a render path).

Fixtures cover the four modes (memories/sessions/features/files) in the
exact wire shapes verified on 2026-10-15. The tests never reach a real
CN: the verb's helper seams are stubbed at the
:mod:`mini_ork.cn_client` boundary so they run offline and fast. The
page tests read the slot file and assert render correctness without
making HTTP calls — ``_make_no_cn_calls`` stubs the read helpers and
then asserts ``NOT called``.

Each test pins the project scope (``--all-projects`` is off by
default) so a foreign CN row never leaks into the IDE.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from mini_ork import cn_client
from mini_ork.cli import cn_cmd
from mini_ork.ide_pages import context
from mini_ork.stores import migrate as mig

REPO = Path(__file__).resolve().parents[2]


# ── fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An empty mini-ork home (no run, no context pack, no slot file).

    Pins ``MINI_ORK_HOME`` to ``tmp_path/proj/.mini-ork`` so the cn
    search slot file lands in a per-test directory and never leaks
    from one test to the next. The page's project root resolves to
    ``tmp_path/proj`` so ``args["all_projects"]`` semantics align with
    the CLI verb's ``git rev-parse --show-toplevel``.
    """
    h = tmp_path / "proj" / ".mini-ork"
    h.mkdir(parents=True)
    rc, _out, err = mig.init_db(db=str(h / "state.db"), root=str(REPO))
    assert rc == 0, err
    for k in ("MO_DISABLE_CN", "CN_BASE_URL", "CN_TIER", "CN_TOKEN_KEY",
              "CN_PING_TIMEOUT_SEC", "CN_TIMEOUT_SEC", "CN_CACHE_TTL_SEC",
              "CN_PING_TTL", "MINI_ORK_SECRETS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("MINI_ORK_HOME", str(h))
    return h


@pytest.fixture()
def _no_cn_calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Stub every cn_client HTTP helper — assert NOT called in the test body.

    Returns the per-helper call list so each test can ask specific
    helpers whether they were reached. Default is each helper is
    stubbed to return its fail-soft default (``{}`` / ``""``); a
    helper that was *never invoked* keeps its list empty. CN being
    disabled / unreachable is the default state the page must
    tolerate.
    """
    calls = {
        "retrieve": 0,
        "sessions_by_feature": 0,
        "sessions_by_file": 0,
        "features_recent": 0,
        "_get": 0,
        "_post_json": 0,
    }

    def _bump(name: str, stub):
        def _wrapped(*args, **kwargs):
            calls[name] += 1
            return stub(*args, **kwargs)
        return _wrapped

    monkeypatch.setattr(cn_client, "retrieve",
                        _bump("retrieve", lambda *a, **k: "{}"))
    monkeypatch.setattr(cn_client, "sessions_by_feature",
                        _bump("sessions_by_feature", lambda *a, **k: "{}"))
    monkeypatch.setattr(cn_client, "sessions_by_file",
                        _bump("sessions_by_file", lambda *a, **k: "{}"))
    monkeypatch.setattr(cn_client, "features_recent",
                        _bump("features_recent", lambda *a, **k: "{}"))
    monkeypatch.setattr(cn_client, "_get",
                        _bump("_get", lambda *a, **k: "{}"))
    monkeypatch.setattr(cn_client, "_post_json",
                        _bump("_post_json", lambda *a, **k: "{}"))
    monkeypatch.setattr(cn_client, "available", lambda: False)
    return calls


# ── helpers ───────────────────────────────────────────────────────────────


def _write_slot(home: Path, *, mode: str, query: str = "test",
                all_projects: bool = False, project: str = "/proj",
                result: dict | None = None, ts: int | None = None) -> dict:
    """Persist a slot envelope for the page to read.

    Returns the written envelope so a test can reference its ``ts``
    in render-time ``as of HH:MM`` assertions. The directory resolves
    via ``cn_client._section_cache_dir`` so the verb and the page
    agree on the path.
    """
    if ts is None:
        ts = int(time.time())
    if result is None:
        if mode == "memories":
            result = {"hits": []}
        elif mode == "sessions":
            result = {"hits": []}
        elif mode == "features":
            result = {"features": []}
        elif mode == "files":
            result = {"matches": []}
    envelope = {
        "mode": mode,
        "query": query,
        "all_projects": all_projects,
        "project": project,
        "ts": ts,
        "count": len(result.get("hits", result.get("features", result.get("matches", [])))),
        "result": result,
    }
    slot = cn_client._section_cache_dir()
    os.makedirs(slot, exist_ok=True)
    path = os.path.join(slot, "context.search__latest.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(envelope, fh)
    return envelope


def _section(page: dict, title: str) -> dict[str, Any]:
    for s in page["sections"]:
        if s["title"] == title:
            return s
    raise AssertionError(
        f"no section {title!r} in {[s['title'] for s in page['sections']]}"
    )


def _results_section(page: dict) -> dict[str, Any]:
    """The Results section: titled ``Results`` when empty, the mode name
    (Memories / Sessions / Features / Files) when rows render."""
    for s in page["sections"]:
        if s["title"] in {"Results", "Memories", "Sessions",
                          "Features", "Files"}:
            return s
    raise AssertionError(
        f"no Results section in {[s['title'] for s in page['sections']]}"
    )


def _list_items(page: dict, title: str) -> list[dict[str, Any]]:
    return list(_section(page, title).get("items") or [])


def _results_items(page: dict) -> list[dict[str, Any]]:
    return list(_results_section(page).get("items") or [])


# ── 1. Parser ─────────────────────────────────────────────────────────────


def test_search_parser_parses_minimal_query() -> None:
    parser = cn_cmd.build_parser()
    args = parser.parse_args(["search", "memories", "fix bug"])
    assert args.subcommand == "search"
    assert args.mode == "memories"
    assert args.query == "fix bug"
    assert args.all_projects is False
    assert args.json is False


def test_search_parser_accepts_all_modes() -> None:
    parser = cn_cmd.build_parser()
    for mode in ("memories", "sessions", "features", "files"):
        args = parser.parse_args(["search", mode, "q"])
        assert args.mode == mode


def test_search_parser_rejects_unknown_mode() -> None:
    parser = cn_cmd.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["search", "totally_unknown", "q"])


def test_search_parser_all_projects_flag() -> None:
    parser = cn_cmd.build_parser()
    args = parser.parse_args(["search", "files", "x.py",
                              "--all-projects", "--limit", "5"])
    assert args.all_projects is True
    assert args.limit == 5


def test_search_parser_default_limit() -> None:
    parser = cn_cmd.build_parser()
    args = parser.parse_args(["search", "features", "x"])
    assert args.limit == 20


def test_cn_subparser_has_no_home_flag() -> None:
    """The homeless-verb set the IDE sweeps for requires ``"cn"`` to have
    no ``--home`` literal anywhere in its source. The :file:`search`
    parser does not declare it; the composer therefore MUST carry
    ``home=False`` so the IDE does not append a flag the verb rejects.
    """
    src = Path(cn_cmd.__file__).read_text(encoding="utf-8")
    assert '"--home"' not in src and "'--home'" not in src, (
        "cn_cmd.py must not declare --home; the IDE appends it to "
        "every subcommand unless the action says home=False"
    )


# ── 2. CLI verb — project resolution ──────────────────────────────────────


def test_project_root_prefers_mini_ork_home_parent(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """``MINI_ORK_HOME``'s resolved parent wins over git toplevel.

    The verb scopes CN rows to the same project root the IDE Context page
    uses (``home.parent``). When ``MINI_ORK_HOME`` is set, that resolves
    the scope even when cwd sits inside a different git checkout (the
    test runner's working tree).
    """
    monkeypatch.setenv("MINI_ORK_HOME", "/x/proj/.mini-ork")
    assert cn_cmd._project_root() == "/x/proj"


def test_project_root_falls_back_to_git_when_home_unset(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """No ``MINI_ORK_HOME`` → ``git rev-parse --show-toplevel`` of cwd."""
    monkeypatch.delenv("MINI_ORK_HOME", raising=False)
    monkeypatch.setattr(cn_cmd.subprocess, "run",
                        mock.Mock(return_value=mock.Mock(stdout="/git/repo\n")))
    assert cn_cmd._project_root() == "/git/repo"


def test_project_root_ignores_mini_ork_root(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """``MINI_ORK_ROOT`` is the engine install, never the user project.

    A previous revision fell back to ``MINI_ORK_ROOT`` when no git
    toplevel resolved. The revised verb never reads ``MINI_ORK_ROOT``
    for project scoping — it is reserved for the engine layout.
    """
    monkeypatch.setenv("MINI_ORK_ROOT", "/engine/install")
    monkeypatch.setattr(cn_cmd.subprocess, "run",
                        mock.Mock(side_effect=FileNotFoundError))
    assert cn_cmd._project_root() == ""


def test_project_root_returns_empty_when_unresolved(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """No ``MINI_ORK_HOME`` AND no git toplevel → ``""``.

    The verb treats ``""`` as the fail-closed sentinel; ``_cmd_search``
    converts that into a scope-resolution error envelope (exit 3)
    unless ``--all-projects`` is the opt-in to the unfiltered state.
    """
    monkeypatch.delenv("MINI_ORK_HOME", raising=False)
    monkeypatch.setattr(cn_cmd.subprocess, "run",
                        mock.Mock(side_effect=FileNotFoundError))
    assert cn_cmd._project_root() == ""


# ── 3. CLI verb — mode handlers ───────────────────────────────────────────


def _stub_post_json(monkeypatch: pytest.MonkeyPatch,
                    canned: str = '{"hits": []}') -> dict[str, Any]:
    """Patch ``cn_client._post_json`` (and ``_disabled`` / ``available``)
    so ``_do_memories`` reaches the stub without ever calling
    ContextNest. Returns the call-record dict so the test body can
    inspect path, body, and timeout. ``_disabled`` / ``available`` are
    stubbed so the new ``_disabled() / available()`` gate inside
    ``_do_memories`` lets the call through; tests that need the
    unavailable path stub them back to False or True directly.
    """
    captured: dict[str, Any] = {}

    def _record(path: str, payload: str,
                *, timeout: float | None = None) -> str:
        captured["path"] = path
        captured["body"] = payload
        captured["timeout"] = timeout
        return canned

    monkeypatch.setattr(cn_client, "_disabled", lambda: False)
    monkeypatch.setattr(cn_client, "available", lambda: True)
    monkeypatch.setattr(cn_client, "_post_json", _record)
    return captured


def test_do_memories_calls_post_json_with_query_top_k_and_budget(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """``_do_memories`` POSTs to ``/api/v1/tools/retrieve`` with
    ``{"query", "top_k"}`` and the ``CN_SEARCH_TIMEOUT_SEC`` budget.

    The verb reaches into :func:`cn_client._post_json` directly so the
    read budget is read from ``CN_SEARCH_TIMEOUT_SEC`` (default ``90``)
    rather than the legacy ``CN_TIMEOUT_SEC`` set/restore around
    :func:`cn_client.retrieve`. The body keeps the ``top_k`` key
    because that is what ``tools/retrieve`` reads on the wire.
    """
    captured = _stub_post_json(monkeypatch, canned=json.dumps({
        "hits": [{"id": "h1", "metadata": {"cwd": "/proj/foo.py"}}],
    }))
    monkeypatch.delenv("CN_SEARCH_TIMEOUT_SEC", raising=False)
    out = cn_cmd._do_memories("foo", "/proj", False, 5)
    assert captured["path"] == "/api/v1/tools/retrieve"
    assert json.loads(captured["body"]) == {"query": "foo", "top_k": 5}
    assert captured["timeout"] == pytest.approx(90.0)
    assert isinstance(out, list) and out and out[0]["id"] == "h1"


def test_do_memories_honours_cn_search_timeout_sec(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """``CN_SEARCH_TIMEOUT_SEC`` overrides the default 90 s read budget."""
    captured = _stub_post_json(monkeypatch,
                               canned=json.dumps({"hits": [{"id": "h1"}]}))
    monkeypatch.setenv("CN_SEARCH_TIMEOUT_SEC", "12.5")
    cn_cmd._do_memories("foo", "/proj", False, 5)
    assert captured["timeout"] == pytest.approx(12.5)


def test_do_memories_does_not_mutate_cn_timeout_sec(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """``_do_memories`` never reads or writes ``CN_TIMEOUT_SEC``.

    A legacy revision set/restore ``CN_TIMEOUT_SEC="30"`` around the
    call. The revised verb reads ``CN_SEARCH_TIMEOUT_SEC`` only — the
    legacy shared read budget is no longer mutated by search. ``os.environ``
    is byte-identical before and after the call, and ``CN_TIMEOUT_SEC``
    keeps its pre-existing value (set to ``"8"`` here) verbatim.
    """
    monkeypatch.setenv("CN_TIMEOUT_SEC", "8")
    before = dict(os.environ)
    captured: dict[str, Any] = {}

    def _fake_post_json(path: str, body: str,
                        *, timeout: float | None = None) -> str:
        captured["env"] = dict(os.environ)
        return json.dumps({"hits": [{"id": "h1"}]})

    monkeypatch.setattr(cn_client, "_disabled", lambda: False)
    monkeypatch.setattr(cn_client, "available", lambda: True)
    monkeypatch.setattr(cn_client, "_post_json", _fake_post_json)
    cn_cmd._do_memories("foo", "/proj", False, 5)
    assert os.environ.get("CN_TIMEOUT_SEC") == "8"
    assert captured["env"].get("CN_TIMEOUT_SEC") == "8"
    # And os.environ itself is unchanged.
    assert dict(os.environ) == before


def test_do_memories_raises_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """``MO_DISABLE_CN=1`` → ``ContextNest is not reachable`` (no POST)."""
    monkeypatch.setattr(cn_client, "_disabled", lambda: True)
    monkeypatch.setattr(cn_client, "available", lambda: False)
    called: list[str] = []
    monkeypatch.setattr(cn_client, "_post_json",
                        lambda *a, **k: (called.append("post"), "{}")[1])
    with pytest.raises(cn_cmd.RetrieveFailed) as exc:
        cn_cmd._do_memories("q", "/proj", False, 5)
    assert "not reachable" in str(exc.value)
    assert called == []  # never reached the wire


def test_do_memories_raises_when_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """``available()`` returning False → same envelope, same exit code path."""
    monkeypatch.setattr(cn_client, "_disabled", lambda: False)
    monkeypatch.setattr(cn_client, "available", lambda: False)
    monkeypatch.setattr(cn_client, "_post_json", lambda *a, **k: "{}")
    with pytest.raises(cn_cmd.RetrieveFailed) as exc:
        cn_cmd._do_memories("q", "/proj", False, 5)
    assert str(exc.value) == "ContextNest is not reachable"


def test_do_memories_raises_when_body_has_no_hits(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """A body without a ``hits`` list is a failed retrieve (exit 4 path).

    ``cn_client._post_json`` swallows timeouts and connection errors
    into ``"{}"``. From the verb's perspective that is a body without
    ``hits`` — the budget is the documented rationale (``"within
    <budget> s"``). The verb turns this into :class:`RetrieveFailed`
    so the centralised envelope + exit-4 path in :func:`_cmd_search`
    fires.
    """
    monkeypatch.setattr(cn_client, "_disabled", lambda: False)
    monkeypatch.setattr(cn_client, "available", lambda: True)
    monkeypatch.setattr(cn_client, "_post_json",
                        lambda *a, **k: "{}")
    monkeypatch.setenv("CN_SEARCH_TIMEOUT_SEC", "12.5")
    with pytest.raises(cn_cmd.RetrieveFailed) as exc:
        cn_cmd._do_memories("q", "/proj", False, 5)
    msg = str(exc.value)
    assert msg.startswith("ContextNest retrieve did not answer within"), msg
    assert "12.5" in msg


def test_do_memories_filters_by_project_path(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Project filter keeps only hits whose path lies under ``project``.

    With a non-empty ``project`` and ``all_projects=False`` (the verb's
    fail-closed scope guarantee), only hits with a ``metadata.cwd``
    under the project are kept.
    """
    payload = json.dumps({
        "hits": [
            {"id": "keep", "metadata": {"cwd": "/proj/foo.py"}},
            {"id": "drop", "metadata": {"cwd": "/other/bar.py"}},
            {"id": "no_path"},
        ],
    })
    monkeypatch.setattr(cn_client, "_disabled", lambda: False)
    monkeypatch.setattr(cn_client, "available", lambda: True)
    monkeypatch.setattr(cn_client, "_post_json", lambda *a, **k: payload)
    out = cn_cmd._do_memories("q", "/proj", False, 10)
    ids = [h["id"] for h in out]
    assert ids == ["keep"]


def test_do_memories_all_projects_skips_filter(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """``all_projects=True`` returns the unfiltered list."""
    payload = json.dumps({
        "hits": [
            {"id": "keep", "metadata": {"cwd": "/proj/a.py"}},
            {"id": "other", "metadata": {"cwd": "/other/b.py"}},
        ],
    })
    monkeypatch.setattr(cn_client, "_disabled", lambda: False)
    monkeypatch.setattr(cn_client, "available", lambda: True)
    monkeypatch.setattr(cn_client, "_post_json", lambda *a, **k: payload)
    out = cn_cmd._do_memories("q", "/proj", True, 10)
    assert {h["id"] for h in out} == {"keep", "other"}


def test_do_sessions_filters_by_project(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = json.dumps({
        "hits": [
            {"session_id": "sa", "feature": "kept",
             "files": ["/proj/a.py"]},
            {"session_id": "sb", "feature": "drop",
             "files": ["/other/b.py"]},
        ],
    })
    monkeypatch.setattr(cn_client, "sessions_by_feature",
                        lambda *a, **k: payload)
    out = cn_cmd._do_sessions("q", "/proj", False, 10)
    ids = [h["session_id"] for h in out]
    assert ids == ["sa"]


def test_do_features_passes_project_when_scoped(monkeypatch: pytest.MonkeyPatch) -> None:
    """The features endpoint honours ``project=``; the verb builds it
    onto the URL so the response is small + already filtered."""
    captured: dict[str, str] = {}

    def _fake_get(path: str, *, timeout: float | None = None) -> str:
        captured["path"] = path
        return json.dumps({
            "count": 1, "since": "30d", "layer": "core",
            "features": [
                {"feature": "k", "session_id": "sa",
                 "files": ["/proj/foo"], "layer": "core",
                 "how_to_test": ""},
            ],
        })

    monkeypatch.setattr(cn_client, "_disabled", lambda: False)
    monkeypatch.setattr(cn_client, "_get", _fake_get)
    out = cn_cmd._do_features("k", "/proj", False, 5)
    assert "project=" in captured["path"]
    assert isinstance(out, list) and out[0]["feature"] == "k"


def test_do_features_omits_project_when_all_projects(
        monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, str] = {}

    def _fake_get(path: str, *, timeout: float | None = None) -> str:
        captured["path"] = path
        return json.dumps({"features": []})

    monkeypatch.setattr(cn_client, "_disabled", lambda: False)
    monkeypatch.setattr(cn_client, "_get", _fake_get)
    cn_cmd._do_features("k", "/proj", True, 5)
    assert "project=" not in captured["path"]


def test_do_files_sorts_by_last_ts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Wire payload has NO timestamp; the verb enriches via
    /sessions/:id/summary and sorts newest first."""
    by_file = json.dumps({
        "query": "x.py",
        "matches": [
            {"session_id": "s-old", "matched_files": ["x.py"], "total_files": 1},
            {"session_id": "s-new", "matched_files": ["x.py"], "total_files": 2},
        ],
    })
    summaries = {
        "s-old": json.dumps({"summary": {"last_ts": 1000}}),
        "s-new": json.dumps({"summary": {"last_ts": 2000}}),
    }

    def _fake_sessions_by_file(*a, **k):
        return by_file

    def _fake_get(path, *, timeout=None):
        for sid, payload in summaries.items():
            if f"/api/v1/sessions/{sid}/summary" in path:
                return payload
        return "{}"

    monkeypatch.setattr(cn_client, "_disabled", lambda: False)
    monkeypatch.setattr(cn_client, "sessions_by_file", _fake_sessions_by_file)
    monkeypatch.setattr(cn_client, "_get", _fake_get)
    out = cn_cmd._do_files("x.py", "", True, 10)
    ids = [m["session_id"] for m in out]
    assert ids == ["s-new", "s-old"]


# ── 4. CLI verb — JSON / slot write ──────────────────────────────────────


def test_cmd_search_json_prints_envelope(
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture) -> None:
    monkeypatch.setattr(cn_cmd, "_project_root", lambda: "/proj")
    monkeypatch.setattr(cn_cmd, "_do_memories",
                        lambda *a, **k: [{"id": "h1", "content": "x"}])
    rc = cn_cmd._cmd_search(cn_cmd.build_parser().parse_args(
        ["search", "memories", "fix bug", "--json"]))
    assert rc == cn_cmd.EXIT_OK
    captured = capsys.readouterr()
    payload = json.loads(captured.out.strip())
    assert payload["mode"] == "memories"
    assert payload["query"] == "fix bug"
    assert payload["all_projects"] is False
    assert payload["project"] == "/proj"
    assert payload["count"] == 1
    assert payload["result"] == {"hits": [{"id": "h1", "content": "x"}]}


def test_cmd_search_writes_slot_to_cache_dir(
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture, home: Path) -> None:
    monkeypatch.setattr(cn_cmd, "_project_root", lambda: "/proj")
    monkeypatch.setattr(cn_cmd, "_do_memories",
                        lambda *a, **k: [{"id": "h1", "content": "x"}])
    rc = cn_cmd._cmd_search(cn_cmd.build_parser().parse_args(
        ["search", "memories", "fix bug"]))
    assert rc == cn_cmd.EXIT_OK
    slot_path = os.path.join(cn_client._section_cache_dir(),
                             "context.search__latest.json")
    assert os.path.isfile(slot_path)
    with open(slot_path, encoding="utf-8") as fh:
        env = json.load(fh)
    assert env["mode"] == "memories"
    assert env["query"] == "fix bug"
    assert env["count"] == 1
    assert env["result"] == {"hits": [{"id": "h1", "content": "x"}]}


def test_cmd_search_default_project_scope_in_envelope(
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture, home: Path) -> None:
    monkeypatch.setattr(cn_cmd, "_project_root", lambda: "/my-project")
    monkeypatch.setattr(cn_cmd, "_do_sessions", lambda *a, **k: [])
    cn_cmd._cmd_search(cn_cmd.build_parser().parse_args(
        ["search", "sessions", "x"]))
    captured = capsys.readouterr()
    assert "all_projects" not in captured.out
    assert "/my-project" in captured.out


def test_cmd_search_all_projects_in_envelope(
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture, home: Path) -> None:
    monkeypatch.setattr(cn_cmd, "_project_root", lambda: "/proj")
    monkeypatch.setattr(cn_cmd, "_do_sessions", lambda *a, **k: [])
    cn_cmd._cmd_search(cn_cmd.build_parser().parse_args(
        ["search", "sessions", "x", "--all-projects"]))
    captured = capsys.readouterr()
    assert "all projects" in captured.out


# ── 5. Page — default view unchanged ─────────────────────────────────────


def test_default_tab_does_not_render_search_sections(
        home: Path, _no_cn_calls) -> None:
    """``tab=None`` (and legacy ``tab="active"`` / ``tab="library"``)
    render the original view — no Search-tab sections."""
    for tab in (None, "active", "library"):
        page = context.build(home, tab, {})
        titles = [s["title"] for s in page["sections"]]
        for search_title in ("Search mode", "Search", "Results"):
            assert search_title not in titles, (
                f"default tab {tab!r} leaked Search section {search_title!r}"
            )


def test_default_tab_keeps_existing_sections(
        home: Path, _no_cn_calls) -> None:
    page = context.build(home, None, {})
    titles = [s["title"] for s in page["sections"]]
    assert "ContextNest status" in titles
    assert "Capsule preview (14d)" in titles
    assert "Context pack" in titles


def test_default_tab_has_no_tabs_set_on_page(
        home: Path, _no_cn_calls) -> None:
    """Default tab carries the tab list (the IDE renders them).
    The TABS tuple lists active/library/search."""
    page = context.build(home, None, {})
    assert isinstance(page.get("tabs"), list)
    keys = [t["key"] for t in page["tabs"]]
    assert keys == ["active", "library", "search"]
    assert page["tab"] == "active"


# ── 6. Page — Search tab empty state ─────────────────────────────────────


def test_search_tab_with_no_slot_shows_empty_state(
        home: Path, _no_cn_calls) -> None:
    page = context.build(home, "search", {"mode": "memories"})
    assert page["ok"] is True
    items = _list_items(page, "Results")
    assert len(items) == 1
    text = (items[0].get("t") or "").lower()
    assert "no search yet" in text


def test_search_tab_default_mode_is_memories(
        home: Path, _no_cn_calls) -> None:
    """No ``mode`` arg → memories mode chip is on."""
    page = context.build(home, "search", {})
    chips = _section(page, "Search mode")
    on_chips = [c["t"] for c in chips["items"] if c.get("on")]
    assert on_chips == ["memories"]


def test_search_tab_known_mode_chip_on(
        home: Path, _no_cn_calls) -> None:
    page = context.build(home, "search", {"mode": "features"})
    chips = _section(page, "Search mode")
    on_chips = [c["t"] for c in chips["items"] if c.get("on")]
    assert on_chips == ["features"]


def test_search_tab_unknown_mode_falls_back_to_memories(
        home: Path, _no_cn_calls) -> None:
    """An ide-launched tab with a stale mode arg falls back to memories."""
    page = context.build(home, "search", {"mode": "nonsense"})
    chips = _section(page, "Search mode")
    on_chips = [c["t"] for c in chips["items"] if c.get("on")]
    assert on_chips == ["memories"]


# ── 7. Page — all-projects chip ──────────────────────────────────────────


def test_search_tab_default_chip_is_this_project(
        home: Path, _no_cn_calls) -> None:
    page = context.build(home, "search", {"mode": "memories"})
    chips = _section(page, "Search mode")
    ap_chips = [c for c in chips["items"] if c["t"] == "all projects"]
    assert ap_chips and ap_chips[0]["on"] is False


def test_search_tab_all_projects_chip_on(
        home: Path, _no_cn_calls) -> None:
    page = context.build(home, "search",
                         {"mode": "memories", "all_projects": "1"})
    chips = _section(page, "Search mode")
    ap_chips = [c for c in chips["items"] if c["t"] == "all projects"]
    assert ap_chips and ap_chips[0]["on"] is True


def test_search_tab_chip_text_defaults_to_this_project(
        home: Path, _no_cn_calls) -> None:
    page = context.build(home, "search", {"mode": "memories"})
    chips = _section(page, "Search mode")
    assert "project scope:" in chips["note"]
    assert "all projects" not in chips["note"]


# ── 8. Page — composer contract ──────────────────────────────────────────


def _composer(page: dict) -> dict[str, Any]:
    for s in page["sections"]:
        if s.get("type") == "composer" or s.get("title") == "Search":
            # The :func:`S.composer` helper emits ``type: "composer"`` AND
            # ``title: ...`` (the latter is the section title).
            return s
    raise AssertionError(
        f"no composer in {[s.get('type') for s in page['sections']]}"
    )


def test_search_composer_cn_prefix(home: Path, _no_cn_calls) -> None:
    """The composer prefix parses against ``cn_cmd.build_parser()``.

    The test passes `--home` here because parse_known_args accepts it
    — the point is the ``cn search <mode>`` argv is well-formed,
    not the IDE's home=False handling (which the next test pins).
    """
    page = context.build(home, "search", {"mode": "memories"})
    composer = _composer(page)
    assert composer.get("type") == "composer"
    cli = composer["cli"]
    assert cli[0] == "cn"
    parser = cn_cmd.build_parser()
    # Calling parse_known_args with the prefix + a fake query must
    # recognise the verb. With the placeholder query appended this
    # MUST pass without SystemExit.
    argv = cli[1:] + ["--home", str(home), "fakequery"]
    parser.parse_known_args(argv)


def test_search_composer_carries_home_false(home: Path, _no_cn_calls) -> None:
    """cn_cmd's parser has no ``--home``; the composer must say so or
    the IDE's ``board.rs run_action`` appends ``--home`` and the verb
    exits 2."""
    page = context.build(home, "search", {"mode": "memories"})
    composer = _composer(page)
    assert composer.get("home") is False


def test_search_composer_all_projects_in_prefix(
        home: Path, _no_cn_calls) -> None:
    page = context.build(home, "search",
                         {"mode": "sessions", "all_projects": "1"})
    composer = _composer(page)
    cli = composer["cli"]
    assert "--all-projects" in cli


def test_search_composer_no_all_projects_in_prefix_by_default(
        home: Path, _no_cn_calls) -> None:
    page = context.build(home, "search", {"mode": "memories"})
    composer = _composer(page)
    assert "--all-projects" not in composer["cli"]


# ── 9. Page — render rows from the slot ─────────────────────────────────


def test_search_tab_renders_memory_rows(home: Path, _no_cn_calls) -> None:
    _write_slot(home, mode="memories", query="k", project="/proj",
                result={"hits": [
                    {"id": "h1", "content": "match this content",
                     "similarity": 0.93, "session_id": "sess-abc"},
                ]})
    page = context.build(home, "search", {"mode": "memories"})
    items = _results_items(page)
    assert items
    # The first row's t carries the hit content; sub carries sim + session.
    head = items[0]
    assert "match this content" in head["t"]
    sub = head.get("sub", "")
    assert "sim=" in sub and "sess" in sub
    # The Open action points at the cn_session page via page_link, no
    # CLI -- so a row click never triggers a CN re-fetch.
    acts = head.get("acts") or []
    assert acts and acts[0]["do"].get("page") == "cn_session"


def test_search_tab_session_rows(home: Path, _no_cn_calls) -> None:
    _write_slot(home, mode="sessions", query="k",
                result={"hits": [
                    {"session_id": "sa", "feature": "kept",
                     "files": ["/proj/a.py", "/proj/b.py"]},
                ]})
    page = context.build(home, "search", {"mode": "sessions"})
    items = _results_items(page)
    assert items and "kept" in items[0]["t"]
    assert "2 file(s)" in items[0]["sub"]


def test_search_tab_feature_rows(home: Path, _no_cn_calls) -> None:
    _write_slot(home, mode="features", query="k",
                result={"features": [
                    {"feature": "feature-x", "session_id": "sa",
                     "layer": "core", "how_to_test": "pytest -k x"},
                ]})
    page = context.build(home, "search", {"mode": "features"})
    items = _results_items(page)
    assert items and "feature-x" in items[0]["t"]
    assert "layer=core" in items[0]["sub"]
    assert "pytest -k x" in items[0]["sub"]


def test_search_tab_file_rows(home: Path, _no_cn_calls) -> None:
    _write_slot(home, mode="files", query="x.py",
                result={"matches": [
                    {"session_id": "sa", "matched_files": ["x.py", "y.py"],
                     "total_files": 5, "last_ts": int(time.time())},
                ]})
    page = context.build(home, "search", {"mode": "files"})
    items = _results_items(page)
    assert items and "x.py" in items[0]["t"]
    assert "2 file(s)" in items[0]["sub"]


def test_search_tab_stale_mode_renders_stale_message(
        home: Path, _no_cn_calls) -> None:
    """The chip says ``features``, the slot is ``memories`` → page renders
    an explicit 'stale slot' line so the operator isn't misled."""
    _write_slot(home, mode="memories", query="k",
                result={"hits": [{"id": "h1"}]})
    page = context.build(home, "search", {"mode": "features"})
    items = _results_items(page)
    text = (items[0].get("t") or "").lower()
    assert "stale slot" in text
    assert "memories" in items[0].get("sub", "")


def test_search_tab_empty_results_shows_no_rows(home: Path,
                                                _no_cn_calls) -> None:
    _write_slot(home, mode="memories",
                result={"hits": []}, project="/proj")
    page = context.build(home, "search", {"mode": "memories"})
    items = _results_section(page).get("items") or []
    assert items and "no rows" in items[0]["t"].lower()


def test_search_tab_results_note_carries_as_of_and_project(
        home: Path, _no_cn_calls) -> None:
    ts = int(time.time())
    _write_slot(home, mode="memories", ts=ts, project="/proj",
                result={"hits": []})
    page = context.build(home, "search", {"mode": "memories"})
    note = _results_section(page).get("note", "")
    assert "as of" in note
    assert "/proj" in note


# ── 10. Page — render path makes NO CN calls ─────────────────────────────


def test_search_tab_makes_no_cn_http_calls(home: Path,
                                           _no_cn_calls) -> None:
    _write_slot(home, mode="memories",
                result={"hits": [{"id": "h1", "session_id": "sa"}]})
    context.build(home, "search", {"mode": "memories"})
    for name, n in _no_cn_calls.items():
        assert n == 0, f"{name!r} called {n} time(s) from the page"


def test_search_tab_no_slot_no_cn_calls(home: Path,
                                        _no_cn_calls) -> None:
    """The page renders the empty state without consulting CN; an empty
    home (no slot file, CN disabled) is the default render path."""
    context.build(home, "search", {"mode": "memories"})
    for name, n in _no_cn_calls.items():
        assert n == 0, f"{name!r} called {n} time(s) from the page"


def test_search_tab_source_does_not_call_retrieve() -> None:
    """The verdict's grep-test: ``context.py`` never references retrieve
    on the render path. (The verb does — and only the verb.)"""
    src = (REPO / "mini_ork" / "ide_pages" / "context.py").read_text(
        encoding="utf-8"
    )
    assert "retrieve(" not in src, (
        "context.py must not call retrieve(); the Search tab reads "
        "the cache slot the verb wrote."
    )


def test_search_tab_source_does_not_reference_tools_retrieve() -> None:
    """The literal ``tools/retrieve`` never appears in the page source.
    The render path is cache-slot only.
    """
    src = (REPO / "mini_ork" / "ide_pages" / "context.py").read_text(
        encoding="utf-8"
    )
    assert "tools/retrieve" not in src
