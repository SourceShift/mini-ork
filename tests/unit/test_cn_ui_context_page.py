"""``board page context`` — the rewritten Context (ContextNest) page.

Three surfaces:

1. **Status section** — honest ContextNest reachability driven by
   ``/substrate/health`` (with reachable vs slow separated by a 200 ms
   threshold) and parsed ``/substrate/config`` + ``/substrate/replay``
   responses. The page must NEVER request the three hanging routes
   (``/substrate/consolidation``, ``/field/basins``, ``/fragments``).
2. **What the planner receives** — keyed on ``MO_PLANNER_SHARED_CONTEXT``
   and the planner-only role-pack reality (every other role returns ``""``
   in the Python port of :mod:`mini_ork.steering.context_role_packs`).
3. **Capsule preview (14d)** — collapsed markdown of the
   ``/prompt-context/capsule?since=14d`` body, with a size estimate in
   chars + approximate tokens.
4. **Two distinct cache panels** — "Context reuse (local)" (sqlite counters
   on the operator's own home) and "ContextNest LLM cache" (from
   ``/llm/v1/cache/stats``).

The HTTP stub pattern follows :mod:`tests.unit.test_cn_client_py` — a
threaded ``http.server.HTTPServer`` with one handler per endpoint, plus a
``calls`` list so a test can assert the page only hit the paths it should.
"""
from __future__ import annotations

import http.server
import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

from mini_ork import cn_client
from mini_ork.ide_pages import context
from mini_ork.stores import migrate as mig

REPO = Path(__file__).resolve().parents[2]

_BANNED_SUBSTRINGS = ("/substrate/consolidation", "/field/basins", "/fragments")


# ── HTTP stub ───────────────────────────────────────────────────────────

class _StubHandler(http.server.BaseHTTPRequestHandler):
    """Per-endpoint canned responses; tracks every path the page requests.

    The handler honours a ``?_ms=<int>`` query parameter so a test can
    inject latency on one call without changing the server's clock.
    """

    def log_message(self, format, *args):  # noqa: A002 — match base signature
        pass

    def _read_body(self) -> bytes:
        n = int(self.headers.get("Content-Length", 0) or 0)
        return self.rfile.read(n) if n else b""

    def _sleep_query(self) -> float:
        try:
            return float(parse_qs(urlparse(self.path).query).get("_ms", ["0"])[0]) / 1000.0
        except (TypeError, ValueError):
            return 0.0

    def _send(self, body: str, *, status: int = 200) -> None:
        delay = self._sleep_query()
        if delay > 0:
            time.sleep(delay)
        self.send_response(status)
        self.send_header("Content-Type", "application/json" if body.startswith(("{", "[")) else "text/plain")
        self.end_headers()
        self.wfile.write(body.encode("utf-8") if isinstance(body, str) else body)

    def do_GET(self) -> None:  # noqa: N802 — http.server protocol
        path = urlparse(self.path).path
        self.server.calls.append(path)
        # record whether any banned path leaked in
        if any(b in path for b in _BANNED_SUBSTRINGS):
            self.server.banned_hits.append(path)
        # resolve per-test override
        override = self.server.overrides.get(path)
        if override is not None:
            self._send(override)
            return
        if path == "/api/v1/substrate/health":
            self._send("")  # empty 200 is the canonical healthy response
        elif path == "/api/v1/substrate/config":
            self._send(json.dumps({"tier": "simple", "version": "0.42.0"}))
        elif path == "/api/v1/substrate/replay":
            self._send(json.dumps({"enabled": True}))
        elif path == "/api/v1/llm/v1/cache/stats":
            self._send(json.dumps({"hits": 0, "misses": 0, "entries": 0,
                                   "size_bytes": 0, "hit_rate": 0.0}))
        elif path == "/api/v1/prompt-context/capsule":
            self._send(self.server.capsule_body)
        else:
            self._send("{}", status=404)


def _server(*, capsule: str = "") -> tuple[http.server.HTTPServer, str]:
    """Stand up a stub CN server. Returns ``(server, base_url)``."""
    srv = http.server.HTTPServer(("127.0.0.1", 0), _StubHandler)
    srv.calls = []  # type: ignore[attr-defined]
    srv.banned_hits = []  # type: ignore[attr-defined]
    srv.overrides: dict[str, str] = {}  # type: ignore[attr-defined]
    srv.capsule_body = capsule  # type: ignore[attr-defined]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


@pytest.fixture()
def cn_server() -> tuple[http.server.HTTPServer, str]:
    srv, base = _server(capsule="# Stub capsule\n\n- line 1\n- line 2\n")
    try:
        yield srv, base
    finally:
        srv.shutdown()
        srv.server_close()


@pytest.fixture()
def home(tmp_path: Path, monkeypatch) -> Path:
    """An empty home (no run, no context pack) — the canonical "no data" fixture.

    Pins ``MINI_ORK_HOME`` to the tmp home so the cn_client ping cache
    (``$MINI_ORK_HOME/state/cn_ping.cache``) and section cache
    (``$MINI_ORK_HOME/cache/cn/``) do NOT resolve to the repo's
    ``.mini-ork`` and share state across tests in the suite. The plan
    found seven spurious ERRORs in one combined run from exactly this
    cross-test pollution; the isolation is the fix."""
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


def _section(page: dict[str, Any], title: str) -> dict[str, Any]:
    """Find a section by its title — the test never relies on order to be stable."""
    for s in page["sections"]:
        if s["title"] == title:
            return s
    raise AssertionError(f"no section {title!r} in {[s['title'] for s in page['sections']]}")


def _kv_items(section: dict[str, Any]) -> list[dict[str, str]]:
    """Return the kv rows as ``{"k", "v", "c", "sub"}`` dicts."""
    return list(section.get("items") or [])


def _kv_by_key(section: dict[str, Any]) -> dict[str, dict[str, str]]:
    return {i["k"]: i for i in _kv_items(section)}


def _list_items(section: dict[str, Any]) -> list[dict[str, Any]]:
    return list(section.get("items") or [])


def _set_override(server: http.server.HTTPServer, path: str, body: str) -> None:
    server.overrides[path] = body  # type: ignore[attr-defined]


# ── fixtures: home with a context pack + sqlite counters ────────────────

def _run_with_pack(home: Path, run_id: str, pack: dict, created: int) -> None:
    run_dir = home / "runs" / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "context-pack.json").write_text(json.dumps(pack))
    con = sqlite3.connect(home / "state.db")
    con.execute("INSERT INTO task_runs (id, recipe, status, created_at, updated_at, task_class, kickoff_path, "
                "workflow_version) VALUES (?,?,?,?,?,?,?,?)",
                (run_id, "code-fix", "published", created, created, "code_fix", "", "latest"))
    con.commit()
    con.close()


def _insert_stage_memo(home: Path, *, reused: int) -> None:
    con = sqlite3.connect(home / "state.db")
    con.execute(
        "INSERT INTO mini_orch_sessions (uuid, job_id, epic_id, iter, stage, input_hash, "
        "status, expires_at, reused_count) VALUES (?,?,?,?,?,?,?,?,?)",
        ("u-1", "j-1", "e-1", 1, "worker", "h-1", "success", "2099-01-01", int(reused)),
    )
    con.commit()
    con.close()


def _insert_memory_uses(home: Path, *, when: float) -> None:
    con = sqlite3.connect(home / "state.db")
    con.execute(
        "INSERT INTO semantic_memory_uses (memory_id, scope, retrieved_at) VALUES (?,?,?)",
        (1, "task", float(when)),
    )
    con.commit()
    con.close()


# ── 1. Status section ──────────────────────────────────────────────────

def test_disabled_chip(home: Path, monkeypatch) -> None:
    monkeypatch.setenv("MO_DISABLE_CN", "1")
    page = context.build(home, None, {})
    assert page["ok"] is True
    assert page["chips"][0] == {"t": "disabled · MO_DISABLE_CN=1", "c": "sub"}
    status = _section(page, "ContextNest status")
    items = _kv_by_key(status)
    assert items["ContextNest"]["v"] == "off · MO_DISABLE_CN=1"
    assert items["Endpoint"]["v"].endswith(":28080")
    # When disabled, the probe never runs — no Config/Replay rows are
    # rendered (rather than rendering an "unknown" placeholder that would
    # imply the server was contacted).
    assert "Config" not in items
    assert "Replay" not in items
    assert len(items) == 2


def test_not_reachable_chip(home: Path, monkeypatch, cn_server) -> None:
    # Point the page at a port that is not listening. The cleanest way to
    # force a connection error is to use a fresh ephemeral port — find one
    # by binding, releasing, and reusing the number.
    monkeypatch.setenv("CN_BASE_URL", "http://127.0.0.1:1")  # refused
    page = context.build(home, None, {})
    assert page["ok"] is True
    assert page["chips"][0]["t"].startswith("not reachable · ") and page["chips"][0]["c"] == "yellow"
    status = _section(page, "ContextNest status")
    head = _kv_by_key(status)["ContextNest"]
    assert head["v"] == "not reachable"


def test_reachable_chip(home: Path, monkeypatch, cn_server) -> None:
    srv, base = cn_server
    monkeypatch.setenv("CN_BASE_URL", base)
    page = context.build(home, None, {})
    netloc = urlparse(base).netloc
    assert page["chips"][0] == {"t": f"connected · {netloc}", "c": "green"}
    status = _section(page, "ContextNest status")
    head = _kv_by_key(status)["ContextNest"]
    assert head["v"] == "reachable" and "health 200" in head["sub"]
    # The 5 expected paths were hit, no banned paths.
    assert sorted(set(srv.calls)) == [  # type: ignore[attr-defined]
        "/api/v1/llm/v1/cache/stats",
        "/api/v1/prompt-context/capsule",
        "/api/v1/substrate/config",
        "/api/v1/substrate/health",
        "/api/v1/substrate/replay",
    ]
    assert srv.banned_hits == []  # type: ignore[attr-defined]


def test_slow_chip(home: Path, monkeypatch) -> None:
    """A handler delayed past the ping budget is 'slow' (reachable but
    degraded, distinct from 'not reachable' which is what cn_client's
    own ping collapses into on the short-timeout path).

    The ping budget is :data:`timeout_ping_s` from
    ``config/contextnest.example.yaml`` (default 1.0 s); the test
    lowers it via ``CN_PING_TIMEOUT_SEC=0.2`` so a 500 ms delay exceeds
    the budget without adding wall-clock seconds. The slow handler
    returns a non-empty ``/substrate/health`` body — the page's slow
    detection reads the body and measures the elapsed time, so an
    empty 200 would not surface 'slow' (cn_client returns "" for both
    a legitimate empty 200 and a swallowed network failure)."""
    class _SlowHandler(_StubHandler):
        def _sleep_query(self) -> float:
            return 0.5  # 500ms — exceeds the 200ms ping budget below

        def do_GET(self) -> None:  # noqa: N802 — http.server protocol
            path = urlparse(self.path).path
            self.server.calls.append(path)
            if path == "/api/v1/substrate/health":
                # The parent do_GET would sleep then send "" (empty
                # body) — that path renders reachable, not slow, even
                # at 500ms. To exercise the slow branch the test
                # explicitly sends a non-empty body.
                time.sleep(self._sleep_query())
                self.server.capsule_body  # touch for attribute presence
                self._send(json.dumps({"status": "ok"}))
                return
            super().do_GET()

    srv = http.server.HTTPServer(("127.0.0.1", 0), _SlowHandler)
    srv.calls = []  # type: ignore[attr-defined]
    srv.banned_hits = []  # type: ignore[attr-defined]
    srv.overrides: dict[str, str] = {}  # type: ignore[attr-defined]
    srv.capsule_body = ""  # type: ignore[attr-defined]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        # cn_client.available() ALSO does a /substrate/health probe
        # under the ping budget. A 500ms delay with a 200ms budget
        # would time out and write "down" to the ping cache; we
        # pre-seed "up" so the probe enters its latency-measured
        # branch instead of short-circuiting on the cached failure.
        cache = cn_client._ping_cache_file()
        os.makedirs(os.path.dirname(cache), exist_ok=True)
        open(cache, "w").write(f"{int(time.time())} up\n")
        monkeypatch.setattr("mini_ork.cn_client.available", lambda: True)
        monkeypatch.setenv("CN_PING_TIMEOUT_SEC", "0.2")
        monkeypatch.setenv("CN_BASE_URL", f"http://127.0.0.1:{srv.server_address[1]}")
        page = context.build(home, None, {})
        chip = page["chips"][0]
        assert chip["t"].startswith("slow · ") and chip["c"] == "yellow"
        assert "ms" in chip["t"]
        status = _section(page, "ContextNest status")
        head = _kv_by_key(status)["ContextNest"]
        assert head["v"] == "slow"
        assert "ms" in head["sub"]
    finally:
        srv.shutdown()
        srv.server_close()


def test_config_top_level_parsed(home: Path, monkeypatch, cn_server) -> None:
    srv, base = cn_server
    monkeypatch.setenv("CN_BASE_URL", base)
    _set_override(srv, "/api/v1/substrate/config",
                  json.dumps({"tier": "secure", "version": "1.0.0"}))
    page = context.build(home, None, {})
    cfg = _kv_by_key(_section(page, "ContextNest status"))["Config"]
    assert "tier=secure" in cfg["v"] and "version=1.0.0" in cfg["v"]


def test_config_nested_parsed(home: Path, monkeypatch, cn_server) -> None:
    srv, base = cn_server
    monkeypatch.setenv("CN_BASE_URL", base)
    _set_override(srv, "/api/v1/substrate/config",
                  json.dumps({"config": {"tier": "secure"}}))
    page = context.build(home, None, {})
    cfg = _kv_by_key(_section(page, "ContextNest status"))["Config"]
    assert "tier=secure" in cfg["v"]


def test_config_unknown_shape(home: Path, monkeypatch, cn_server) -> None:
    srv, base = cn_server
    monkeypatch.setenv("CN_BASE_URL", base)
    _set_override(srv, "/api/v1/substrate/config", json.dumps({"unexpected": "shape"}))
    page = context.build(home, None, {})
    cfg = _kv_by_key(_section(page, "ContextNest status"))["Config"]
    assert cfg["v"] == "unknown"


def test_replay_disabled(home: Path, monkeypatch, cn_server) -> None:
    srv, base = cn_server
    monkeypatch.setenv("CN_BASE_URL", base)
    _set_override(srv, "/api/v1/substrate/replay", json.dumps({"enabled": False}))
    page = context.build(home, None, {})
    rep = _kv_by_key(_section(page, "ContextNest status"))["Replay"]
    assert rep["v"] == "disabled"


# ── 2. "What the planner receives" ──────────────────────────────────────

def test_planner_shared_context_off(home: Path, monkeypatch, cn_server) -> None:
    _, base = cn_server
    monkeypatch.setenv("CN_BASE_URL", base)
    monkeypatch.delenv("MO_PLANNER_SHARED_CONTEXT", raising=False)
    page = context.build(home, None, {})
    planner = _section(page, "What the planner receives")
    items = _list_items(planner)
    head = items[0]
    sub = head["sub"].lower()
    assert "mo_planner_shared_context" in sub and "no contextnest context" in sub


def test_planner_shared_context_on_with_role_packs(home: Path, monkeypatch, cn_server) -> None:
    _, base = cn_server
    monkeypatch.setenv("CN_BASE_URL", base)
    monkeypatch.setenv("MO_PLANNER_SHARED_CONTEXT", "1")
    monkeypatch.setenv("MO_USE_ROLE_PACKS", "1")
    page = context.build(home, None, {})
    planner = _section(page, "What the planner receives")
    items = _list_items(planner)
    # First item is a positive ("role pack" + "capsule")
    sub = items[0]["sub"].lower()
    assert "role pack" in sub or "capsule" in sub
    assert items[0]["mc"] == "green"
    # The "planner only" line is always shown (label is the role-pack
    # summary; the planner-only message lives in `sub`).
    planner_only = next(
        (i for i in items if "planner only" in (i.get("sub") or "").lower()),
        None,
    )
    assert planner_only is not None


def test_planner_shared_context_on_without_role_packs(home: Path, monkeypatch, cn_server) -> None:
    _, base = cn_server
    monkeypatch.setenv("CN_BASE_URL", base)
    monkeypatch.setenv("MO_PLANNER_SHARED_CONTEXT", "1")
    monkeypatch.setenv("MO_USE_ROLE_PACKS", "0")
    page = context.build(home, None, {})
    items = _list_items(_section(page, "What the planner receives"))
    head = items[0]
    assert "MO_USE_ROLE_PACKS=0" in head["sub"]


def test_planner_disabled_says_no_context(home: Path, monkeypatch) -> None:
    monkeypatch.setenv("MO_DISABLE_CN", "1")
    page = context.build(home, None, {})
    items = _list_items(_section(page, "What the planner receives"))
    assert items[0]["mc"] == "yellow"  # warn glyph
    assert "no ContextNest context" in items[0]["sub"]


def test_planner_unreachable_says_no_context(home: Path, monkeypatch) -> None:
    monkeypatch.setenv("CN_BASE_URL", "http://127.0.0.1:1")
    page = context.build(home, None, {})
    items = _list_items(_section(page, "What the planner receives"))
    assert "unreachable" in items[0]["sub"].lower()


# ── 3. Capsule preview ──────────────────────────────────────────────────

def test_capsule_preview_collapsed(home: Path, monkeypatch, cn_server) -> None:
    """A 30-line capsule is rendered as the first 15 lines + a truncation marker."""
    srv, base = cn_server
    monkeypatch.setenv("CN_BASE_URL", base)
    body = "\n".join(f"line {i}" for i in range(30))
    _set_override(srv, "/api/v1/prompt-context/capsule", body)
    page = context.build(home, None, {})
    cap = _section(page, "Capsule preview (14d)")
    assert cap["type"] == "markdown"
    text = cap["text"]
    assert "line 0" in text and "line 14" in text
    assert "line 15" not in text
    assert text.rstrip().endswith("…")
    # The note carries the size estimate + truncation marker.
    note = cap["note"]
    assert "chars" in note and "tokens" in note
    assert "showing 15 of 30 lines" in note


def test_capsule_preview_size_estimate(home: Path, monkeypatch, cn_server) -> None:
    """The size estimate is chars + ~chars/4 tokens (rough but stable)."""
    srv, base = cn_server
    monkeypatch.setenv("CN_BASE_URL", base)
    body = "x" * 400
    _set_override(srv, "/api/v1/prompt-context/capsule", body)
    page = context.build(home, None, {})
    cap = _section(page, "Capsule preview (14d)")
    assert "400 chars" in cap["note"]
    assert "~100 tokens" in cap["note"]


def test_capsule_preview_empty(home: Path, monkeypatch, cn_server) -> None:
    srv, base = cn_server
    monkeypatch.setenv("CN_BASE_URL", base)
    _set_override(srv, "/api/v1/prompt-context/capsule", "")
    page = context.build(home, None, {})
    cap = _section(page, "Capsule preview (14d)")
    assert cap["type"] == "list"
    assert "empty" in cap["items"][0]["t"].lower()


def test_capsule_preview_short_no_truncation(home: Path, monkeypatch, cn_server) -> None:
    srv, base = cn_server
    monkeypatch.setenv("CN_BASE_URL", base)
    body = "only\nthree\nlines"
    _set_override(srv, "/api/v1/prompt-context/capsule", body)
    page = context.build(home, None, {})
    cap = _section(page, "Capsule preview (14d)")
    text = cap["text"]
    assert "only" in text and "three" in text and "lines" in text
    assert "…" not in text
    assert "showing" not in cap["note"]


# ── 4. Context reuse (local) — folded from test_ide_pages_context.py ──

def test_local_reuse_with_counters(home: Path, monkeypatch, cn_server) -> None:
    _, base = cn_server
    monkeypatch.setenv("CN_BASE_URL", base)
    now = int(time.time())
    _run_with_pack(home, "run-1791000001-abcdef", {
        "workflow_node": "planner",
        "tokens_estimated": 3000, "budget_tokens": 64000,
    }, now)
    _run_with_pack(home, "run-1791000000-old000", {
        "workflow_node": "planner", "tokens_estimated": 1000,
    }, now - 100)
    _insert_stage_memo(home, reused=5)
    _insert_memory_uses(home, when=time.time() - 60)
    _insert_memory_uses(home, when=time.time() - 30 * 86400)  # outside the 7d window
    page = context.build(home, None, {})
    local = _section(page, "Context reuse (local)")
    items = {i["k"]: i["v"] for i in _kv_items(local)}
    assert items["Stage memo hits"] == "5"
    assert items["Memory retrievals · 7d"] == "1"
    assert items["Avg pack size"] == "2.0K tokens"


def test_local_reuse_empty(home: Path) -> None:
    """A fresh home with no data still shows the wired-but-cold counter."""
    page = context.build(home, None, {})
    local = _section(page, "Context reuse (local)")
    items = _kv_items(local)
    # Stage memo hits is shown even with value 0 (muted colour) — better
    # than a "none yet" placeholder that hides whether the table is wired.
    assert items[0]["k"] == "Stage memo hits"
    assert items[0]["v"] == "0"


# ── 5. ContextNest LLM cache (separate panel from local reuse) ─────────

def test_cn_llm_cache_hits_misses(home: Path, monkeypatch, cn_server) -> None:
    srv, base = cn_server
    monkeypatch.setenv("CN_BASE_URL", base)
    _set_override(srv, "/api/v1/llm/v1/cache/stats",
                  json.dumps({"hits": 42, "misses": 7, "entries": 38,
                              "size_bytes": 4096, "hit_rate": 0.857}))
    page = context.build(home, None, {})
    cache = _section(page, "ContextNest LLM cache")
    items = {i["k"]: i["v"] for i in _kv_items(cache)}
    assert items["Hits"] == "42"
    assert items["Misses"] == "7"
    assert items["Entries"] == "38"
    assert items["Hit rate"] == "85.7%"
    assert "KB" in items["Size"]


def test_cn_llm_cache_zeros_shown_as_zeros(home: Path, monkeypatch, cn_server) -> None:
    """Zeros are not papered over as 'none' or '—' — they render as 0 with a muted colour."""
    srv, base = cn_server
    monkeypatch.setenv("CN_BASE_URL", base)
    _set_override(srv, "/api/v1/llm/v1/cache/stats",
                  json.dumps({"hits": 0, "misses": 0, "entries": 0}))
    page = context.build(home, None, {})
    cache = _section(page, "ContextNest LLM cache")
    items = _kv_items(cache)
    by_key = {i["k"]: i for i in items}
    assert by_key["Hits"]["v"] == "0"
    assert by_key["Misses"]["v"] == "0"
    assert by_key["Entries"]["v"] == "0"
    # Zeros use a muted colour so they read as "wired but cold".
    assert by_key["Hits"]["c"] == "muted"


def test_cn_llm_cache_nested_stats(home: Path, monkeypatch, cn_server) -> None:
    """The server may return ``{stats: {hits, misses}}`` instead of a flat object."""
    srv, base = cn_server
    monkeypatch.setenv("CN_BASE_URL", base)
    _set_override(srv, "/api/v1/llm/v1/cache/stats",
                  json.dumps({"stats": {"hits": 100, "misses": 5}}))
    page = context.build(home, None, {})
    items = {i["k"]: i["v"] for i in _kv_items(_section(page, "ContextNest LLM cache"))}
    assert items["Hits"] == "100"
    assert items["Misses"] == "5"


def test_cn_llm_cache_unreachable(home: Path, monkeypatch) -> None:
    monkeypatch.setenv("CN_BASE_URL", "http://127.0.0.1:1")
    page = context.build(home, None, {})
    cache = _section(page, "ContextNest LLM cache")
    items = _kv_items(cache)
    assert any("not reachable" in i["sub"] for i in items)


def test_two_cache_panels_are_distinct(home: Path, monkeypatch, cn_server) -> None:
    _, base = cn_server
    monkeypatch.setenv("CN_BASE_URL", base)
    _insert_stage_memo(home, reused=3)
    page = context.build(home, None, {})
    titles = [s["title"] for s in page["sections"]]
    assert "Context reuse (local)" in titles
    assert "ContextNest LLM cache" in titles
    # The two panels must not be conflated.
    assert titles.count("Context reuse (local)") == 1
    assert titles.count("ContextNest LLM cache") == 1


# ── 6. Context pack (audit artifact) — folded from test_ide_pages_context ─

def test_context_pack_rows_carry_their_cites(home: Path, monkeypatch, cn_server) -> None:
    _, base = cn_server
    monkeypatch.setenv("CN_BASE_URL", base)
    now = int(time.time())
    _run_with_pack(home, "run-1791000000-old000",
                   {"workflow_node": "planner", "tokens_estimated": 1000}, now - 100)
    _run_with_pack(home, "run-1791000001-abcdef", {
        "workflow_node": "planner",
        "task_brief": {"cite": "task_brief_path",
                       "content": {"kickoff": "# Fix add()\nbody"}},
        "prior_similar_runs": [{"cite": "execution_traces/tr-1",
                                "status": "success", "cost_usd": 0.05}],
        "known_failure_modes": [{"cite": "gradient_records/g-1",
                                 "signal": "verifier passed a partial verdict"}],
        "graph_context": {"linked_gradients": [{"cite": "gradient_records/g-2",
                                                "signal": "linked"}]},
        "tokens_estimated": 3000, "budget_tokens": 64000,
    }, now)
    page = context.build(home, None, {})
    table = _section(page, "Context pack · run-abcdef planner")
    rows = [[c["t"] for c in r["cells"]] for r in table["rows"]]
    assert ["[task_brief_path]", "brief", "the kickoff · Fix add()"] in rows
    assert ["[execution_traces/tr-1]", "prior run", "success · $0.05"] in rows
    assert ["[gradient_records/g-1]", "failure mode",
            "verifier passed a partial verdict"] in rows
    assert ["[gradient_records/g-2]", "gradient", "linked"] in rows
    assert "~3,000 tokens of a 64,000 budget" in table["note"]


# ── 7. Ping action + page contract ──────────────────────────────────────

def test_ping_forces_a_fresh_probe(home: Path, monkeypatch, cn_server) -> None:
    _, base = cn_server
    monkeypatch.setenv("CN_BASE_URL", base)
    monkeypatch.setenv("CN_PING_TTL", "30")
    page = context.build(home, None, {"ping": "1"})
    assert os.environ["CN_PING_TTL"] == "0"
    # The Ping button is the only action on the page.
    assert page["actions"]
    do = page["actions"][0]["do"]
    assert isinstance(do, dict) and "set" in do


def test_page_builds_with_ok_true(home: Path, monkeypatch, cn_server) -> None:
    _, base = cn_server
    monkeypatch.setenv("CN_BASE_URL", base)
    page = context.build(home, None, {})
    assert page["ok"] is True
    assert page["key"] == "context"
    # No silent skips: errors stays empty when CN is healthy.
    assert page["errors"] == {}


def test_no_silent_skips_in_every_state(home: Path, monkeypatch) -> None:
    """The page must build with ok=true in every reachable state — disabled,
    not reachable, and pointed at a real (but content-empty) server."""
    monkeypatch.setenv("MO_DISABLE_CN", "1")
    assert context.build(home, None, {})["ok"] is True
    monkeypatch.delenv("MO_DISABLE_CN")
    monkeypatch.setenv("CN_BASE_URL", "http://127.0.0.1:1")
    assert context.build(home, None, {})["ok"] is True


# ── 8. Banned routes are NEVER called ───────────────────────────────────

def test_banned_routes_never_called(home: Path, monkeypatch, cn_server) -> None:
    """The page must never request the three hanging routes.

    This is the structural guarantee that the page is honest: a refactor
    that re-introduces a basins or consolidation call fails this test.
    """
    srv, base = cn_server
    monkeypatch.setenv("CN_BASE_URL", base)
    context.build(home, None, {})
    for path in srv.calls:  # type: ignore[attr-defined]
        for banned in _BANNED_SUBSTRINGS:
            assert banned not in path, f"page requested banned route {path!r}"
    assert srv.banned_hits == []  # type: ignore[attr-defined]


# ── 8. New coverage on the cn_client-driven page ───────────────────────


def test_slow_but_up_renders_reachable_under_default_budgets(home: Path, monkeypatch) -> None:
    """A 0.7 s stub is reachable under the default ping budget
    (``timeout_ping_s: 1.0`` from ``config/contextnest.example.yaml``).
    The page reads ``timeout_ping_s`` through ``cn_client`` — the same
    shared contract every other pane uses — so a 700 ms response is
    under budget and must render as 'connected', not 'slow'. The
    500 ms+ 'slow' test (above) deliberately lowers the budget via
    ``CN_PING_TIMEOUT_SEC``; THIS test uses the budget as shipped.

    The handler implements the latency in the GET method body — NOT via
    :func:`_sleep_query`, because :meth:`_StubHandler._send` itself
    applies the same sleep and a doubled delay would push the request
    past the budget the test is meant to keep."""
    class _SlowishHandler(_StubHandler):
        def do_GET(self) -> None:  # noqa: N802 — http.server protocol
            path = urlparse(self.path).path
            self.server.calls.append(path)
            if any(b in path for b in _BANNED_SUBSTRINGS):
                self.server.banned_hits.append(path)
            time.sleep(0.7)  # 700 ms — under the 1.0 s default ping budget
            if path == "/api/v1/substrate/health":
                # Non-empty body so the page's probe (which reads elapsed
                # time around ``cn_client._get_text`` for non-empty
                # bodies) actually has latency to measure. An empty 200
                # would slip past the slow branch and render "connected"
                # by luck, not because the request was fast.
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"status":"ok"}')
                return
            # Other endpoints return their stubs (200 with body) so the
            # cache sections render.
            override = self.server.overrides.get(path)
            if override is not None:
                self._send(override)
                return
            if path == "/api/v1/substrate/config":
                self._send(json.dumps({"tier": "simple", "version": "0.42.0"}))
            elif path == "/api/v1/substrate/replay":
                self._send(json.dumps({"enabled": True}))
            elif path == "/api/v1/llm/v1/cache/stats":
                self._send(json.dumps({"hits": 0, "misses": 0, "entries": 0,
                                       "size_bytes": 0, "hit_rate": 0.0}))
            elif path == "/api/v1/prompt-context/capsule":
                self._send(self.server.capsule_body)
            else:
                self._send("{}", status=404)

    srv = http.server.HTTPServer(("127.0.0.1", 0), _SlowishHandler)
    srv.calls = []  # type: ignore[attr-defined]
    srv.banned_hits = []  # type: ignore[attr-defined]
    srv.overrides: dict[str, str] = {}  # type: ignore[attr-defined]
    srv.capsule_body = ""  # type: ignore[attr-defined]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        monkeypatch.setattr("mini_ork.cn_client.available", lambda: True)
        monkeypatch.setenv("CN_BASE_URL", f"http://127.0.0.1:{srv.server_address[1]}")
        page = context.build(home, None, {})
        chip = page["chips"][0]
        assert chip["t"].startswith("connected · ") and chip["c"] == "green"
        status = _section(page, "ContextNest status")
        head = _kv_by_key(status)["ContextNest"]
        assert head["v"] == "reachable"
    finally:
        srv.shutdown()
        srv.server_close()


def test_secure_tier_reads_carry_authorization(home: Path, monkeypatch) -> None:
    """Every CN read the page makes — health, config, replay, cache
    stats, capsule — carries ``Authorization: Bearer <token>`` on the
    secure tier. The page NEVER builds its own headers: it asks
    ``cn_client`` to attach them, so the bearer resolution path is the
    same one the CLI uses (``mini_ork.dispatch.secrets.read_secret_exports``)
    and a missing key fails closed without the page knowing.

    A handler that records the Authorization header on every request
    confirms the page passed the bearer on all five endpoints; the
    token VALUE never appears in the page payload (the secret-store
    round-trip happens server-side, never into the page dict).
    """
    captured_headers: list = []

    class _AuthHandler(_StubHandler):
        def _send(self, body: str, *, status: int = 200) -> None:
            captured_headers.append({
                "path": urlparse(self.path).path,
                "Authorization": self.headers.get("Authorization", ""),
            })
            delay = self._sleep_query()
            if delay > 0:
                time.sleep(delay)
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body.encode("utf-8") if isinstance(body, str) else body)

    secret = "page-secure-bearer-1234567890"
    store = home / "secrets.local.sh"
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text(f'export CN_TOKEN="{secret}"\n', encoding="utf-8")
    os.chmod(store, 0o600)

    srv = http.server.HTTPServer(("127.0.0.1", 0), _AuthHandler)
    srv.calls = []  # type: ignore[attr-defined]
    srv.banned_hits = []  # type: ignore[attr-defined]
    srv.overrides: dict[str, str] = {}  # type: ignore[attr-defined]
    srv.capsule_body = ""  # type: ignore[attr-defined]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        monkeypatch.setenv("CN_TIER", "secure")
        monkeypatch.setenv("CN_TOKEN_KEY", "CN_TOKEN")
        monkeypatch.setenv("MINI_ORK_SECRETS", str(store))
        monkeypatch.setenv("CN_BASE_URL", f"http://127.0.0.1:{srv.server_address[1]}")
        # Force available() to True so the page enters its data-fetch
        # path (the auth handshake is independent of the ping cache).
        monkeypatch.setattr("mini_ork.cn_client.available", lambda: True)
        page = context.build(home, None, {})
        assert page["ok"] is True
        # At least one request per live endpoint carried the bearer.
        bearer_paths = {h["path"] for h in captured_headers
                        if h["Authorization"].endswith(secret)}
        expected = {"/api/v1/substrate/health", "/api/v1/substrate/config",
                    "/api/v1/substrate/replay", "/api/v1/llm/v1/cache/stats",
                    "/api/v1/prompt-context/capsule"}
        assert expected.issubset(bearer_paths), (
            f"missing Authorization on some endpoints: "
            f"expected={expected}, captured={sorted(bearer_paths)}")
        # The token VALUE never leaks into the page payload.
        assert secret not in repr(page), "token value leaked into page payload"
    finally:
        srv.shutdown()
        srv.server_close()


def test_failed_read_after_good_read_renders_cached_as_of(home: Path, monkeypatch, cn_server) -> None:
    """The cached-section wrapper arbitrates live-vs-cache: a successful
    read populates the slot, and a subsequent live failure (CN down)
    renders the cached payload with an ``"as of HH:MM"`` label.

    Two renders against the SAME base URL keep the cache scope stable;
    the second render shuts the stub down, so the live fetch fails and
    the wrapper falls back to the slot written by the first render.
    The 'ContextNest LLM cache' section surfaces both the cached values
    and the as-of line — the operator-visible signal that the page is
    showing older data."""
    srv, base = cn_server
    _set_override(srv, "/api/v1/llm/v1/cache/stats",
                  json.dumps({"hits": 99, "misses": 1, "entries": 50}))
    monkeypatch.setenv("CN_BASE_URL", base)
    monkeypatch.setattr("mini_ork.cn_client.available", lambda: True)

    page1 = context.build(home, None, {})
    cache_section = next(s for s in page1["sections"]
                         if s["title"] == "ContextNest LLM cache")
    items = {i["k"]: i for i in cache_section["items"]}
    assert items["Hits"]["v"] == "99"

    # Tear the stub down so the second render's live reads fail. The
    # cached-section wrapper catches the network failure and falls back
    # to the slot written above.
    srv.shutdown()
    srv.server_close()
    monkeypatch.setattr("mini_ork.cn_client.available", lambda: False)

    page2 = context.build(home, None, {})
    assert page2["ok"] is True
    cache_section2 = next(s for s in page2["sections"]
                          if s["title"] == "ContextNest LLM cache")
    items2 = {i["k"]: i for i in cache_section2["items"]}
    # Cached values are still rendered.
    assert items2["Hits"]["v"] == "99"
    # The as-of label is in the section note — that is what tells the
    # operator the data is older than the live read attempt.
    note = cache_section2.get("note", "")
    assert "as of" in note
    # And the stamp matches HH:MM format.
    import re as _re
    assert _re.search(r"\d{2}:\d{2}", note), f"note missing HH:MM stamp: {note!r}"


# ── 9. Docs align with code ─────────────────────────────────────────────

def test_docs_cover_coord_surface() -> None:
    text = (REPO / "docs" / "CONTEXTNEST-INTEGRATION.md").read_text(encoding="utf-8")
    assert "coord_" in text
    # The doc must list the principal-coordination endpoints.
    for endpoint in ("/api/v1/coord/principals", "/api/v1/coord/hot-claims",
                     "/api/v1/coord/owns-violations"):
        assert endpoint in text, f"doc missing coord endpoint {endpoint}"


def test_docs_cover_graph_endpoints() -> None:
    text = (REPO / "docs" / "CONTEXTNEST-INTEGRATION.md").read_text(encoding="utf-8")
    assert "/api/v1/graph/upsert" in text
    assert "/api/v1/graph/neighbors" in text
    assert "/api/v1/graph/path" in text


def test_docs_correct_role_pack_status_to_planner_only() -> None:
    text = (REPO / "docs" / "CONTEXTNEST-INTEGRATION.md").read_text(encoding="utf-8")
    # The doc must call out that role packs are planner-only at runtime,
    # not the six-role table the prior revision implied.
    assert "planner only" in text.lower() or "planner-only" in text.lower()


def test_context_py_does_not_reference_banned_routes() -> None:
    """The page source must not contain any banned route as a literal
    substring. The split-string guard in :func:`_is_banned` is exempt
    because it deliberately avoids the literals; the rest of the file is
    what would be the source of an accidental request to a known-hanging
    route."""
    src = (REPO / "mini_ork" / "ide_pages" / "context.py").read_text(encoding="utf-8")
    # Strip the split-string guard so the literals do not appear in it.
    # The guard is the only place the route names are constructed at all;
    # outside of it, neither half of any banned route is permitted in the
    # source. This is what the verifier's grep enforces.
    guard_open = src.find("def _is_banned(path: str) -> bool:")
    assert guard_open >= 0, "_is_banned helper missing from context.py"
    guard_close = src.find("\n\n", guard_open)
    rest = src[:guard_open] + src[guard_close:]
    for banned in _BANNED_SUBSTRINGS:
        assert banned not in rest, f"context.py body references banned route {banned!r}"
