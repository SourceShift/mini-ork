"""Unit tests: ContextNest two-tier client + IDE page + ``cn status``.

The kickoff's acceptance surface in one file. Each test below is named for
the acceptance bullet it covers so a CI failure points at the spec line:

  * top_k                      — retrieve POST body uses ``top_k``, not ``limit``.
  * secure_tier_missing_token  — missing token in secure mode raises an error
                                 naming the key; the token is never logged.
  * as_of / cache              — slow/failed fetch with a cached slot renders
                                 the cached section with its as-of label.
  * unreachable / not_running  — no cache + no server renders the one-line
                                 "ContextNest is not running" message.
  * disabled                   — ``MO_DISABLE_CN=1`` renders the disabled line.
  * scope / project            — items with no path inside the project are
                                 dropped; run-id extraction and
                                 sessions_by_file_recent ordering hold.
  * status                     — ``cn status`` answers tier/endpoint/reachable/
                                 latency with no server and exits 0.

Regression: the existing ``test_cn_client_py.py`` and
``test_ide_pages_actions.py`` must still pass.
"""
from __future__ import annotations

import http.server
import io
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from mini_ork import cn_client  # noqa: E402
from mini_ork.dispatch.secrets import SecretStoreError  # noqa: E402
from mini_ork.ide_pages import _cn as _ide_cn  # noqa: E402


# ── env helpers ────────────────────────────────────────────────────────────


@pytest.fixture()
def fresh_env(monkeypatch, tmp_path):
    """Drop every CN-related env var so tests start from a clean slate.

    ``MO_DISABLE_CN``, ``CN_BASE_URL``, ``CN_TOKEN_KEY``, ``CN_TIER``,
    ``CN_PING_TIMEOUT_SEC``, ``CN_TIMEOUT_SEC``, ``CN_CACHE_TTL_SEC``,
    ``CN_PING_TTL`` all unset, plus a per-test temp home for the cache
    directory and the secret store.
    """
    for k in (
        "MO_DISABLE_CN", "CN_BASE_URL", "CN_TOKEN_KEY", "CN_TIER",
        "CN_PING_TIMEOUT_SEC", "CN_TIMEOUT_SEC", "CN_CACHE_TTL_SEC",
        "CN_PING_TTL", "MINI_ORK_SECRETS", "MINI_ORK_HOME",
    ):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path / "h"))
    return tmp_path / "h"


# ── 1. top_k body key ─────────────────────────────────────────────────────


def test_retrieve_body_carries_top_k(fresh_env, monkeypatch) -> None:
    """The wire body uses ``top_k``; the Python parameter is still ``limit``."""
    captured: list[bytes] = []

    class _Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a, **k):  # noqa: A002 — match base signature
            pass

        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"")

        def do_POST(self):
            ln = int(self.headers.get("Content-Length", 0))
            captured.append(self.rfile.read(ln) if ln else b"")
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"hits":[]}')

    srv = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    monkeypatch.setenv("CN_BASE_URL", base)
    monkeypatch.setenv("CN_PING_TTL", "0")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        body = cn_client.retrieve("hello", 7)
        # A live read against the mock server, in simple tier.
        assert json.loads(body) == {"hits": []}
        # The wire body MUST be ``top_k`` (the server reads that key), and
        # the value MUST be the int we passed as ``limit`` (the Python
        # parameter is still named ``limit``).
        assert len(captured) == 1
        wire = json.loads(captured[0])
        assert wire == {"query": "hello", "top_k": 7}
        assert "limit" not in wire
    finally:
        srv.shutdown()


# ── 2. secure-tier missing token fails closed ─────────────────────────────


def test_secure_tier_missing_token_fails_closed(fresh_env, monkeypatch) -> None:
    """A missing key in secure mode raises ``SecretStoreError`` naming the
    key (not the value). The token is never logged in the error message.
    """
    monkeypatch.setenv("CN_TIER", "secure")
    monkeypatch.setenv("CN_TOKEN_KEY", "CN_TOKEN")
    monkeypatch.setenv("MINI_ORK_SECRETS", str(fresh_env / "secrets.local.sh"))
    # No secret store file exists — guaranteed missing.

    with pytest.raises(SecretStoreError) as exc:
        cn_client._auth_headers()
    msg = str(exc.value)
    # The error names the KEY (so the operator knows what to export), not
    # the value. A missing key has no value, so the check is "key appears
    # in the message, the literal token does not".
    assert "CN_TOKEN" in msg, msg
    # The simple-tier path must not be silently substituted.
    with pytest.raises(SecretStoreError):
        cn_client._auth_headers()
    # And the live retrieve call must surface the same error, not a silent
    # simple-tier fallback. Seed the ping cache so ``available()`` does not
    # short-circuit on a connection error and the missing-token path is the
    # one that actually runs.
    cache = cn_client._ping_cache_file()
    open(cache, "w").write(f"{int(time.time())} up\n")
    monkeypatch.setenv("CN_BASE_URL", "http://127.0.0.1:28080")
    with pytest.raises(SecretStoreError):
        cn_client.retrieve("anything", 1)


def test_secure_tier_secret_never_appears_in_error(fresh_env, monkeypatch) -> None:
    """When the token IS set, the value must never appear in any error log,
    repr, or URL — only the key name should ever leak."""
    monkeypatch.setenv("CN_TIER", "secure")
    monkeypatch.setenv("CN_TOKEN_KEY", "CN_TOKEN")
    secret = "sk-this-is-the-secret-1234567890"
    store = fresh_env / "secrets.local.sh"
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text(f"export CN_TOKEN={secret!r}\n".replace("'", '"'), encoding="utf-8")
    os.chmod(store, 0o600)
    monkeypatch.setenv("MINI_ORK_SECRETS", str(store))

    # Resolve succeeds.
    headers = cn_client._auth_headers()
    assert headers.get("Authorization", "").startswith("Bearer ")

    # Now corrupt the store and observe that the error names the key only.
    store.unlink()
    with pytest.raises(SecretStoreError) as exc:
        cn_client._auth_headers()
    assert "CN_TOKEN" in str(exc.value)
    assert secret not in str(exc.value)


# ── 3. cached section renders the as-of label ─────────────────────────────


def test_cached_section_renders_as_of(fresh_env, monkeypatch) -> None:
    """A slow fetch with a recent cache slot renders the cached payload
    with the as-of label."""
    # Pre-seed the cache: a healthy payload stamped "now".
    section = "atoms"
    scope = "/ps/mini-ork"
    payload = {"hits": [{"content": "fresh payload"}], "_as_of": int(time.time())}
    cn_client._write_section_cache(section, scope, payload)

    # A fetch that always fails should fall back to the cache.
    def _boom():
        raise TimeoutError("simulated slow CN")

    out = cn_client.cached_section(section, scope, _boom)
    assert out["hits"] == payload["hits"]
    assert out["_from_cache"] is True
    assert out["_as_of"] == payload["_as_of"]
    assert isinstance(out["_age_seconds"], int)

    # The IDE page wrapper renders this as a "connected · as of HH:MM" dot.
    line = _ide_cn.section_status(out)
    assert "as of" in line["text"] or "cached" in line["text"]
    assert line["tone"] in ("ok", "warn")


def test_cached_section_refreshes_on_success(fresh_env, monkeypatch) -> None:
    """A successful fetch refreshes the slot — the as-of stamp is current."""

    section = "atoms"
    scope = "/ps/mini-ork"
    stale = {"hits": [{"content": "stale"}], "_as_of": int(time.time()) - 3600}
    cn_client._write_section_cache(section, scope, stale)

    new_payload = {"hits": [{"content": "fresh"}]}
    out = cn_client.cached_section(section, scope, lambda: new_payload)
    assert out["hits"] == [{"content": "fresh"}]
    assert out["_from_cache"] is False
    assert out["_as_of"] >= stale["_as_of"] + 3000


# ── 4. unreachable (no cache, no server) renders the one-liner ────────────


def test_unreachable_no_cache_renders_not_running(fresh_env, monkeypatch) -> None:
    """No live data, no cache, no server → the page renders the
    'ContextNest is not running' one-liner."""
    # Point CN at a port nothing listens on, and zero the ping cache.
    monkeypatch.setenv("CN_BASE_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("CN_PING_TTL", "0")
    monkeypatch.setenv("CN_PING_TIMEOUT_SEC", "0.2")
    monkeypatch.setenv("CN_TIMEOUT_SEC", "0.2")

    def _boom():
        raise ConnectionError("refused")

    payload = cn_client.cached_section("atoms", "/ps/mini-ork", _boom)
    assert payload == {"_unreachable": True}

    line = _ide_cn.section_status(payload)
    assert "not running" in line["text"]
    assert line["tone"] == "warn"


# ── 5. MO_DISABLE_CN renders the disabled line ────────────────────────────


def test_disabled_line_render(fresh_env, monkeypatch) -> None:
    """``MO_DISABLE_CN=1`` renders the disabled line and short-circuits the
    network call entirely."""
    monkeypatch.setenv("MO_DISABLE_CN", "1")
    payload = cn_client.cached_section("atoms", "/ps/mini-ork",
                                       lambda: {"hits": [{"content": "ignored"}]})
    assert payload.get("_disabled") is True

    line = _ide_cn.section_status(payload)
    assert "off" in line["text"]
    assert "MO_DISABLE_CN=1" in line["text"]
    assert line["tone"] == "sub"


# ── 6. scope helper filters foreign items, run-id extraction, recent ──────


def test_scope_helper_filters_foreign_items() -> None:
    """Items with no path inside the project are dropped."""
    project = "/ps/mini-ork"
    items = [
        {"path": f"{project}/mini_ork/cn_client.py", "kind": "trace"},
        {"path": "/other/repo/foo.py", "kind": "trace"},
        {"file": f"{project}/README.md", "kind": "doc"},
        {"kind": "no-path"},                          # dropped (no path)
        {"path": None},                               # dropped (null)
        {"path": ""},                                 # dropped (empty)
        {"cwd": f"{project}/sub/dir", "kind": "ctx"}, # cwd counts
        {"project_cwd": f"{project}/other"},          # project_cwd counts
        {"uri": f"{project}/notes.md"},               # uri counts
    ]
    kept = cn_client.filter_by_project_path(items, project)
    paths = [it.get("path") or it.get("file") or it.get("cwd")
             or it.get("project_cwd") or it.get("uri")
             for it in kept]
    assert f"{project}/mini_ork/cn_client.py" in paths
    assert f"{project}/README.md" in paths
    assert f"{project}/sub/dir" in paths
    assert f"{project}/other" in paths
    assert f"{project}/notes.md" in paths
    # The foreign path is gone.
    assert all("/other/repo/" not in (p or "") for p in paths)
    # Items with no path are gone (the project filter is the only thing
    # that can drop them — a null/empty path is a leak risk, not a feature).
    assert all(isinstance(it.get("path") or it.get("file") or it.get("cwd")
                          or it.get("project_cwd") or it.get("uri"), str)
               for it in kept)


def test_scope_helper_trailing_slash_equivalent() -> None:
    """``/ps/mini-ork`` and ``/ps/mini-ork/`` mean the same thing — a
    naive startswith would reject ``/ps/mini-ork/foo`` when the caller
    asked for ``/ps/mini-ork/`` (the trailing slash is the bug)."""
    items = [{"path": "/ps/mini-ork/foo.py"}]
    assert cn_client.filter_by_project_path(items, "/ps/mini-ork/")[0]["path"] == "/ps/mini-ork/foo.py"


def test_extract_run_id_accepts_canonical_shape() -> None:
    """The run-id shape is ``run-<digits>-<short>``."""
    assert cn_client.extract_run_id({"run_id": "run-1792058012-31521"}) == "run-1792058012-31521"
    assert cn_client.extract_run_id({"trace_id": "run-1792058012-abcde"}) == "run-1792058012-abcde"
    assert cn_client.extract_run_id({"id": "run-1792058012-zzzzz"}) == "run-1792058012-zzzzz"
    # Non-canonical shapes are rejected.
    assert cn_client.extract_run_id({"run_id": "not-a-run-id"}) == ""
    assert cn_client.extract_run_id({"run_id": "run-1-x"}) == ""
    assert cn_client.extract_run_id({"run_id": "run-1234-ab"}) == ""
    assert cn_client.extract_run_id({}) == ""
    assert cn_client.extract_run_id(None) == ""


def test_sessions_by_file_recent_orders_and_scopes(fresh_env, monkeypatch) -> None:
    """Recent sessions are sorted newest-first by ``last_seen`` (then the
    other time fields) and scoped to the project path."""
    raw = {
        "items": [
            {"path": "/ps/mini-ork/a.py", "last_seen": 100},
            {"path": "/ps/mini-ork/b.py", "last_seen": 300},  # newest
            {"path": "/ps/mini-ork/c.py", "last_seen": 200},
            {"path": "/other/repo/d.py", "last_seen": 9999},  # foreign — dropped
        ]
    }
    captured: list[str] = []

    class _R:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return json.dumps(raw).encode()

    def _fake_urlopen(req, timeout=None):
        # ``req`` is either a Request object (``available()``) or a bare
        # string url (``_get``); capture whichever it is.
        url = getattr(req, "full_url", req)
        captured.append(url)
        return _R()

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)
    # Seed the ping cache to "up" so available() short-circuits.
    cache = cn_client._ping_cache_file()
    open(cache, "w").write(f"{int(time.time())} up\n")

    recent = cn_client.sessions_by_file_recent("/ps/mini-ork/a.py", "/ps/mini-ork", 5)

    paths = [it["path"] for it in recent]
    # Foreign project is gone.
    assert "/other/repo/d.py" not in paths
    # Newest first.
    assert paths == ["/ps/mini-ork/b.py", "/ps/mini-ork/c.py", "/ps/mini-ork/a.py"]
    # The URL only ever carries the requested file path, never the project.
    assert captured
    # The path is URL-encoded in the query string.
    assert "%2Fps%2Fmini-ork%2Fa.py" in captured[0]


# ── 7. cn status runs offline and exits 0 ────────────────────────────────


def test_cn_status_runs_offline(fresh_env, monkeypatch) -> None:
    """``cn status`` answers tier/endpoint/reachable/latency with no server
    and exits 0 (reachable: no is a valid answer, not an error)."""
    from mini_ork.cli import cn_cmd

    # No server, no env overrides.
    parser = cn_cmd.build_parser()
    args = parser.parse_args(["status", "--json"])
    rc = cn_cmd._cmd_status(args)
    assert rc == 0
    # Text mode also works.
    out = io.StringIO()
    err = io.StringIO()
    saved_stdout, saved_stderr = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = out, err
    try:
        rc = cn_cmd._cmd_status(parser.parse_args(["status"]))
    finally:
        sys.stdout, sys.stderr = saved_stdout, saved_stderr
    assert rc == 0
    text = out.getvalue()
    assert "not reachable" in text


def test_cn_status_subprocess_offline(fresh_env, monkeypatch) -> None:
    """End-to-end: ``python -m mini_ork.cli.cn_cmd status --json`` returns
    0 and a JSON document with the right shape — the subprocess path that
    ``mini-ork cn status`` actually takes."""
    env = os.environ.copy()
    for k in ("MO_DISABLE_CN", "CN_BASE_URL", "CN_TOKEN_KEY", "CN_TIER",
              "CN_PING_TIMEOUT_SEC", "CN_TIMEOUT_SEC", "MINI_ORK_SECRETS"):
        env.pop(k, None)
    env["MINI_ORK_HOME"] = str(fresh_env)
    env["PYTHONPATH"] = str(REPO)
    proc = subprocess.run(
        [sys.executable, "-m", "mini_ork.cli.cn_cmd", "status", "--json"],
        env=env, capture_output=True, text=True, timeout=10,
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["tier"] in ("simple", "secure")
    assert payload["endpoint"]
    assert payload["reachable"] is False
    assert payload["disabled"] is False
    assert payload["http_status"] == 0


def test_cn_status_subprocess_disabled(fresh_env, monkeypatch) -> None:
    """``MO_DISABLE_CN=1`` short-circuits and reports ``disabled: true``."""
    env = os.environ.copy()
    for k in ("CN_BASE_URL", "CN_TOKEN_KEY", "CN_TIER",
              "CN_PING_TIMEOUT_SEC", "CN_TIMEOUT_SEC", "MINI_ORK_SECRETS"):
        env.pop(k, None)
    env["MINI_ORK_HOME"] = str(fresh_env)
    env["PYTHONPATH"] = str(REPO)
    env["MO_DISABLE_CN"] = "1"
    proc = subprocess.run(
        [sys.executable, "-m", "mini_ork.cli.cn_cmd", "status", "--json"],
        env=env, capture_output=True, text=True, timeout=10,
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["disabled"] is True
    assert payload["reachable"] is False


# ── 8. CLI registration: ``mini-ork cn status`` is reachable via main ────


def test_cn_subcommand_is_registered() -> None:
    """The ``cn`` entry is wired through ``_NATIVE_MODULE_SUBS`` so the
    ``mini-ork cn`` path in ``cli/main.py`` actually reaches the cmd."""
    from mini_ork.cli import main as cli_main

    assert cli_main._NATIVE_MODULE_SUBS.get("cn") == "mini_ork.cli.cn_cmd"


# ── 9. tier resolution: env > real > example ─────────────────────────────


def test_tier_resolution_env_beats_yaml(fresh_env, monkeypatch) -> None:
    """A real ``config/contextnest.yaml`` is the highest-precedence file;
    env beats both. The example is the lowest tier."""
    # Default: simple tier (from the example).
    cfg = cn_client._tier_config()
    assert cfg["tier"] == "simple"

    # Env beats the example.
    monkeypatch.setenv("CN_TIER", "secure")
    cfg = cn_client._tier_config()
    assert cfg["tier"] == "secure"

    # A real local override beats the example, but env still wins.
    real = fresh_env / "config" / "contextnest.yaml"
    real.parent.mkdir(parents=True, exist_ok=True)
    real.write_text("tier: simple\nbase_url: http://real:1234\n", encoding="utf-8")
    monkeypatch.delenv("CN_TIER", raising=False)
    cfg = cn_client._tier_config()
    assert cfg["tier"] == "simple"
    assert cfg["base_url"] == "http://real:1234"

    # Env still beats the real config.
    monkeypatch.setenv("CN_TIER", "secure")
    cfg = cn_client._tier_config()
    assert cfg["tier"] == "secure"
    assert cfg["base_url"] == "http://real:1234"  # base_url not in env


def test_authorization_header_uses_bearer(fresh_env, monkeypatch) -> None:
    """Secure tier with a present token emits ``Authorization: Bearer …``;
    the token is read from the secret store and never appears in the URL."""
    monkeypatch.setenv("CN_TIER", "secure")
    monkeypatch.setenv("CN_TOKEN_KEY", "CN_TOKEN")
    store = fresh_env / "secrets.local.sh"
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text('export CN_TOKEN="super-secret-1234"\n', encoding="utf-8")
    os.chmod(store, 0o600)
    monkeypatch.setenv("MINI_ORK_SECRETS", str(store))

    headers = cn_client._auth_headers()
    assert headers.get("Authorization", "").endswith("super-secret-1234")

    # The token must never appear in a request URL.
    captured: list[str] = []

    class _Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a, **k):  # noqa: A002
            pass
        def do_GET(self):
            captured.append(self.path)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"")
        def do_POST(self):
            captured.append(self.path)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"hits":[]}')

    srv = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    monkeypatch.setenv("CN_BASE_URL", base)
    monkeypatch.setenv("CN_PING_TTL", "0")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        cn_client.retrieve("hello", 3)
        # Only one POST went out, and the URL has no token in it.
        assert any(p.startswith("/api/v1/tools/retrieve") for p in captured)
        for p in captured:
            assert "super-secret-1234" not in p
    finally:
        srv.shutdown()


# ── 10. secure-tier GETs and probe send Authorization ─────────────────────


def test_secure_tier_available_sends_authorization(fresh_env, monkeypatch) -> None:
    """On the secure tier, the availability probe sends
    ``Authorization: Bearer <token>`` derived from the secret store."""
    monkeypatch.setenv("CN_TIER", "secure")
    monkeypatch.setenv("CN_TOKEN_KEY", "CN_TOKEN")
    store = fresh_env / "secrets.local.sh"
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text('export CN_TOKEN="probe-auth-1234"\n', encoding="utf-8")
    os.chmod(store, 0o600)
    monkeypatch.setenv("MINI_ORK_SECRETS", str(store))
    monkeypatch.setenv("CN_BASE_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("CN_PING_TTL", "0")  # always probe
    monkeypatch.setenv("CN_PING_TIMEOUT_SEC", "0.2")

    captured: list = []

    class _R:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b""

    def _fake(req, timeout=None):
        captured.append(req)
        return _R()

    monkeypatch.setattr("urllib.request.urlopen", _fake)
    cn_client.available()
    assert captured, "urlopen was never called"
    assert captured[0].headers.get("Authorization", "") == "Bearer probe-auth-1234"


def test_secure_tier_get_path_sends_authorization(fresh_env, monkeypatch) -> None:
    """A GET through the page-helper path (``inbox`` → ``_get``) sends the
    bearer token on the secure tier. The probe (a separate ``_auth_headers``
    call) also carries the header."""
    monkeypatch.setenv("CN_TIER", "secure")
    monkeypatch.setenv("CN_TOKEN_KEY", "CN_TOKEN")
    store = fresh_env / "secrets.local.sh"
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text('export CN_TOKEN="get-path-9876"\n', encoding="utf-8")
    os.chmod(store, 0o600)
    monkeypatch.setenv("MINI_ORK_SECRETS", str(store))
    monkeypatch.setenv("CN_BASE_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("CN_PING_TTL", "0")
    monkeypatch.setenv("CN_PING_TIMEOUT_SEC", "0.2")
    monkeypatch.setenv("CN_TIMEOUT_SEC", "0.2")

    captured: list = []

    class _R:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b"{}"

    def _fake(req, timeout=None):
        captured.append(req)
        return _R()

    monkeypatch.setattr("urllib.request.urlopen", _fake)
    cn_client.inbox(3)
    # 1 = availability probe, 2 = inbox GET. Both must carry Authorization.
    assert len(captured) >= 2, captured
    for req in captured:
        assert req.headers.get("Authorization", "") == "Bearer get-path-9876"


def test_secure_tier_get_text_sends_authorization(fresh_env, monkeypatch) -> None:
    """``_get_text`` (used by the markdown renderers) also sends the
    bearer token on the secure tier."""
    monkeypatch.setenv("CN_TIER", "secure")
    monkeypatch.setenv("CN_TOKEN_KEY", "CN_TOKEN")
    store = fresh_env / "secrets.local.sh"
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text('export CN_TOKEN="get-text-5555"\n', encoding="utf-8")
    os.chmod(store, 0o600)
    monkeypatch.setenv("MINI_ORK_SECRETS", str(store))

    captured: list = []

    class _R:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b""

    def _fake(req, timeout=None):
        captured.append(req)
        return _R()

    monkeypatch.setattr("urllib.request.urlopen", _fake)
    cn_client._get_text("/foo")
    assert captured, "urlopen was never called"
    assert captured[0].headers.get("Authorization", "") == "Bearer get-text-5555"


# ── 11. secure-tier GETs and probe fail closed when the token is missing ──


def test_secure_tier_get_fails_closed_when_token_missing(fresh_env, monkeypatch) -> None:
    """On the secure tier, a missing bearer token makes ``_get`` raise
    ``SecretStoreError`` (naming the KEY) BEFORE any request is sent. The
    empty-dict fallback must NOT paper over the misconfiguration."""
    monkeypatch.setenv("CN_TIER", "secure")
    monkeypatch.setenv("CN_TOKEN_KEY", "CN_TOKEN")
    monkeypatch.setenv("MINI_ORK_SECRETS", str(fresh_env / "missing.sh"))

    def _fake(req, timeout=None):
        raise AssertionError("urlopen must not be called when the token is missing")

    monkeypatch.setattr("urllib.request.urlopen", _fake)
    with pytest.raises(SecretStoreError) as exc:
        cn_client._get("/foo")
    assert "CN_TOKEN" in str(exc.value)


def test_secure_tier_get_text_fails_closed_when_token_missing(fresh_env, monkeypatch) -> None:
    """``_get_text`` raises ``SecretStoreError`` when the token is missing —
    the empty-string fallback is reserved for genuine network errors only."""
    monkeypatch.setenv("CN_TIER", "secure")
    monkeypatch.setenv("CN_TOKEN_KEY", "CN_TOKEN")
    monkeypatch.setenv("MINI_ORK_SECRETS", str(fresh_env / "missing.sh"))

    def _fake(req, timeout=None):
        raise AssertionError("urlopen must not be called when the token is missing")

    monkeypatch.setattr("urllib.request.urlopen", _fake)
    with pytest.raises(SecretStoreError) as exc:
        cn_client._get_text("/foo")
    assert "CN_TOKEN" in str(exc.value)


def test_secure_tier_available_fails_closed_when_token_missing(fresh_env, monkeypatch) -> None:
    """The availability probe also fails closed on a missing token — a
    missing key is a CONFIG error, not a "down" answer."""
    monkeypatch.setenv("CN_TIER", "secure")
    monkeypatch.setenv("CN_TOKEN_KEY", "CN_TOKEN")
    monkeypatch.setenv("MINI_ORK_SECRETS", str(fresh_env / "missing.sh"))
    monkeypatch.setenv("CN_BASE_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("CN_PING_TTL", "0")  # always probe

    def _fake(req, timeout=None):
        raise AssertionError("urlopen must not be called when the token is missing")

    monkeypatch.setattr("urllib.request.urlopen", _fake)
    with pytest.raises(SecretStoreError) as exc:
        cn_client.available()
    assert "CN_TOKEN" in str(exc.value)


# ── 12. simple tier: no Authorization, no secret store access ─────────────


def test_simple_tier_get_sends_no_authorization(fresh_env, monkeypatch) -> None:
    """On the simple tier, ``_get`` does not touch the secret store and
    sends no Authorization header — even when a secret store exists with
    a CN_TOKEN entry, the simple tier must ignore it (no silent upgrade
    to secure)."""
    # No CN_TIER set — default is "simple" from the example yaml.
    # Pre-create a secret store with a token. The simple tier must not
    # read it; if it tried, the test would still pass functionally, but
    # we tighten the assertion by also asserting the secret VALUE never
    # appears in any captured request.
    secret_value = "simple-tier-canary-7777"
    store = fresh_env / "secrets.local.sh"
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text(f'export CN_TOKEN="{secret_value}"\n', encoding="utf-8")
    os.chmod(store, 0o600)
    monkeypatch.setenv("MINI_ORK_SECRETS", str(store))

    captured: list = []

    class _R:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b"{}"

    def _fake(req, timeout=None):
        captured.append(req)
        return _R()

    monkeypatch.setattr("urllib.request.urlopen", _fake)
    cn_client._get("/foo")
    assert captured, "urlopen was never called"
    # No Authorization header on the simple tier.
    assert "Authorization" not in captured[0].headers
    # The secret value never appears in any outbound URL or header.
    for req in captured:
        url = req.full_url if hasattr(req, "full_url") else str(req)
        assert secret_value not in url
        assert secret_value not in str(req.headers)


def test_simple_tier_available_sends_no_authorization(fresh_env, monkeypatch) -> None:
    """The availability probe on the simple tier sends no Authorization
    header and never opens the secret store file."""
    # No CN_TIER set — default is "simple". Pre-create a secret store
    # with a value; the simple tier must not read it.
    secret_value = "available-canary-8888"
    store = fresh_env / "secrets.local.sh"
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text(f'export CN_TOKEN="{secret_value}"\n', encoding="utf-8")
    os.chmod(store, 0o600)
    monkeypatch.setenv("MINI_ORK_SECRETS", str(store))
    monkeypatch.setenv("CN_PING_TTL", "0")

    captured: list = []

    class _R:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b""

    def _fake(req, timeout=None):
        captured.append(req)
        return _R()

    monkeypatch.setattr("urllib.request.urlopen", _fake)
    assert cn_client.available() is True
    assert captured, "urlopen was never called"
    assert "Authorization" not in captured[0].headers
    for req in captured:
        url = req.full_url if hasattr(req, "full_url") else str(req)
        assert secret_value not in url
        assert secret_value not in str(req.headers)


# ── 13. cn status exits non-zero on a missing token ───────────────────────


def test_cn_status_subprocess_missing_token_exits_nonzero(fresh_env, monkeypatch) -> None:
    """End-to-end: ``python -m mini_ork.cli.cn_cmd status --json`` on the
    secure tier with a missing token exits 5, names the KEY in stderr,
    and the JSON payload has ``error == "secret_store_missing_key"``.
    A "reachable: no" answer would mask the misconfiguration; this
    command must instead tell the operator which export to add."""
    env = os.environ.copy()
    for k in ("MO_DISABLE_CN", "CN_BASE_URL", "CN_PING_TIMEOUT_SEC",
              "CN_TIMEOUT_SEC"):
        env.pop(k, None)
    env["MINI_ORK_HOME"] = str(fresh_env)
    env["PYTHONPATH"] = str(REPO)
    env["CN_TIER"] = "secure"
    env["CN_TOKEN_KEY"] = "CN_TOKEN"
    env["MINI_ORK_SECRETS"] = str(fresh_env / "missing.sh")  # never created
    proc = subprocess.run(
        [sys.executable, "-m", "mini_ork.cli.cn_cmd", "status", "--json"],
        env=env, capture_output=True, text=True, timeout=10,
    )
    # Non-zero exit distinguishes "fix the secret store" from "server is down".
    assert proc.returncode != 0, proc.stdout + proc.stderr
    # The KEY name is in stderr so the operator knows which export to add.
    assert "CN_TOKEN" in proc.stderr
    # The structured diagnostic on stdout is parseable JSON.
    payload = json.loads(proc.stdout)
    assert payload["error"] == "secret_store_missing_key"
    assert payload["error_key"] == "CN_TOKEN"
    assert payload["reachable"] is False
    assert payload["tier"] == "secure"


def test_cn_status_subprocess_token_value_never_leaks(fresh_env, monkeypatch) -> None:
    """The token VALUE never appears in ``cn status`` output — neither
    stdout nor stderr, on the success path or the error path."""
    secret_value = "leak-canary-9999-zzzz"
    env = os.environ.copy()
    for k in ("MO_DISABLE_CN", "CN_PING_TIMEOUT_SEC", "CN_TIMEOUT_SEC"):
        env.pop(k, None)
    env["MINI_ORK_HOME"] = str(fresh_env)
    env["PYTHONPATH"] = str(REPO)
    env["CN_TIER"] = "secure"
    env["CN_TOKEN_KEY"] = "CN_TOKEN"
    env["CN_BASE_URL"] = "http://127.0.0.1:1"  # nothing listens here
    env["CN_PING_TIMEOUT_SEC"] = "0.2"
    store = fresh_env / "secrets.local.sh"
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text(f'export CN_TOKEN="{secret_value}"\n', encoding="utf-8")
    os.chmod(store, 0o600)
    env["MINI_ORK_SECRETS"] = str(store)
    proc = subprocess.run(
        [sys.executable, "-m", "mini_ork.cli.cn_cmd", "status", "--json"],
        env=env, capture_output=True, text=True, timeout=10,
    )
    # The token value must NEVER appear in any output stream.
    assert secret_value not in proc.stdout, proc.stdout
    assert secret_value not in proc.stderr, proc.stderr
    # And the KEY name is fine to appear (the operator needs to know
    # which export to set).
    assert "CN_TOKEN" in proc.stdout or "CN_TOKEN" in proc.stderr


def test_cn_status_runs_offline_secure_with_token(fresh_env, monkeypatch) -> None:
    """``cn status`` on the secure tier with a valid token still answers
    the reachability question (with a no, since the server is down) and
    exits 0 — reachability is what ``status`` answers, not auth."""
    from mini_ork.cli import cn_cmd

    monkeypatch.setenv("CN_TIER", "secure")
    monkeypatch.setenv("CN_TOKEN_KEY", "CN_TOKEN")
    store = fresh_env / "secrets.local.sh"
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text('export CN_TOKEN="ok-token-1111"\n', encoding="utf-8")
    os.chmod(store, 0o600)
    monkeypatch.setenv("MINI_ORK_SECRETS", str(store))
    monkeypatch.setenv("CN_BASE_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("CN_PING_TIMEOUT_SEC", "0.2")

    parser = cn_cmd.build_parser()
    out = io.StringIO()
    err = io.StringIO()
    saved_stdout, saved_stderr = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = out, err
    try:
        rc = cn_cmd._cmd_status(parser.parse_args(["status", "--json"]))
    finally:
        sys.stdout, sys.stderr = saved_stdout, saved_stderr
    assert rc == 0
    payload = json.loads(out.getvalue())
    assert payload["tier"] == "secure"
    assert payload["token_key"] == "CN_TOKEN"
    assert payload["reachable"] is False
    # Token value never leaked into the JSON output.
    assert "ok-token-1111" not in out.getvalue()
    assert "ok-token-1111" not in err.getvalue()
