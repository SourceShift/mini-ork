"""Unit tests: mini_ork.cn_client (bash parity halves removed; formerly vs lib/cn_client.sh).

Three surfaces:
  1. render_* markdown — run the port on JSON fixtures (incl. empty /
     truncation / missing-field edges) and assert the rendered sections
     semantically.
  2. HTTP round-trips — a mock CN server (health 200 + canned bodies); the
     port's `retrieve` / `inbox` return the served body verbatim.
  3. Fallbacks — MO_DISABLE_CN=1 and a dead port both yield {} on reads.
"""
from __future__ import annotations

import http.server
import json
import os
import sys
import threading
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork import cn_client as cn  # noqa: E402


# ---- 1. render_* markdown ----

_RETRIEVE = json.dumps({"hits": [
    {"similarity": 0.9123, "metadata": {"kind": "decision", "ts": "2026-07-05T10:00:00"},
     "session_id": "abcdef123456", "content": "chose sqlite over pg\nfor portability"},
    {"content": "x" * 400, "id": "zz"},
]})
_FEATURES = json.dumps({"features": [
    {"feature": "eval node", "layer": "backend", "how_to_test": "run pytest " + "y" * 200,
     "project_cwd": "/ps/mini-ork"},
    {"name": "no-test feat"},
]})
_INBOX = json.dumps({"items": [
    {"kind": "todo", "session_id": "deadbeefcafe", "subject": "z" * 200},
    {"kind": "user_action", "id": "short", "action": "restart"},
]})
_BASINS = json.dumps({"basins": [
    {"basin_id": "b1234567xx", "active_mass": 12, "representative": "w" * 200},
    {"id": "b2", "size": 3, "centroid_text": "schema work"},
]})


def test_render_atoms_md():
    rp = cn.render_atoms_md(_RETRIEVE, 5)
    assert rp.startswith("--- ContextNest atoms")
    # first hit: kind/sim/date/session prefix + content flattened to one line
    assert "- [decision sim=0.91 2026-07-05 sess=abcdef12] chose sqlite over pg for portability" in rp
    # second hit: missing metadata falls back to defaults; long content truncated
    assert "sess=zz" in rp and "..." in rp
    assert rp.rstrip().endswith("--- /ContextNest atoms ---")


def test_render_features_md():
    rp = cn.render_features_md(_FEATURES, "/ps/mini-ork", 6)
    assert rp.startswith("--- ContextNest features")
    assert "(backend) [this project] eval node" in rp
    assert "test: run pytest yyy" in rp
    # missing fields degrade to placeholders
    assert "(?) no-test feat" in rp


def test_render_inbox_md():
    rp = cn.render_inbox_md(_INBOX, 5)
    assert rp.startswith("--- ContextNest attention inbox ---")
    assert "[todo deadbeef]" in rp          # session_id truncated to 8 chars
    assert "..." in rp                       # long subject truncated
    assert "[user_action short] restart" in rp


def test_render_basins_md():
    rp = cn.render_basins_md(_BASINS, 5)
    assert rp.startswith("--- ContextNest topic clusters (basins) ---")
    assert "[b1234567 mass=12]" in rp        # basin_id truncated to 8 chars
    assert "..." in rp                       # long representative truncated
    assert "[b2 mass=3] schema work" in rp


def test_render_empty_and_bad_json():
    for fn in (cn.render_atoms_md, cn.render_inbox_md, cn.render_basins_md):
        for payload in ('{}', '{"hits":[]}', 'not json'):
            assert fn(payload, 5) == ""


# ---- 2. mock-server HTTP round-trips ----

class _Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):  # noqa: A002 — match base signature
        pass

    def _send(self, body: str):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(body.encode())

    def do_GET(self):
        if self.path.startswith("/api/v1/substrate/health"):
            self._send("")
        elif self.path.startswith("/api/v1/prompt-context/capsule"):
            self._send(_CAPSULE_MD)
        elif self.path.startswith("/api/v1/inbox"):
            self._send('{"items":[{"kind":"todo","id":"x"}]}')
        else:
            self._send("{}")

    def do_POST(self):
        ln = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(ln) if ln else b"{}"
        # cc/hook/<event> — record the fire-and-forget hook and 204 (mirrors CN).
        if self.path.startswith("/api/v1/cc/hook/"):
            event = self.path.rsplit("/", 1)[-1]
            try:
                body = json.loads(raw)
            except Exception:
                body = {}
            self.server.hook_events.append({"event": event, "body": body})
            self.send_response(204)
            self.end_headers()
            return
        self._send('{"hits":[{"content":"hi","similarity":0.5}]}')


# Markdown capsule body served at /api/v1/prompt-context/capsule — mirrors the
# kind-ordered sections the retired .sh stub emitted (## Risks / ## Decisions).
_CAPSULE_MD = (
    "# Prompt Context\n\n"
    "## Risks\n- [risk_flag x3] stub capsule risk\n\n"
    "## Decisions\n- [decision_made x2] stub capsule decision\n"
)


def _server():
    srv = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    srv.hook_events = []  # cc/hook POSTs land here (fire-and-forget, polled)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def _poll_hooks(srv, want: int, timeout: float = 5.0):
    """cn_hook_post is fire-and-forget (daemon thread) — poll the server until
    `want` events land or timeout."""
    import time as _t
    deadline = _t.time() + timeout
    while _t.time() < deadline:
        if len(srv.hook_events) >= want:
            break
        _t.sleep(0.05)
    return list(srv.hook_events)


def test_http_round_trips(tmp_path):
    srv, base = _server()
    try:
        old = dict(os.environ)
        os.environ.update({"CN_BASE_URL": base, "MINI_ORK_HOME": str(tmp_path / "h"),
                           "CN_TIMEOUT_SEC": "5"})
        try:
            rp_retrieve = cn.retrieve("q", 3)
            rp_inbox = cn.inbox(5)
        finally:
            os.environ.clear(); os.environ.update(old)
        assert json.loads(rp_retrieve) == {"hits": [{"content": "hi", "similarity": 0.5}]}
        assert json.loads(rp_inbox) == {"items": [{"kind": "todo", "id": "x"}]}
    finally:
        srv.shutdown()


# ---- 3. fallbacks ----

def test_disabled_and_down_fallback(tmp_path):
    # disabled → {} on reads
    env = {"MO_DISABLE_CN": "1", "MINI_ORK_HOME": str(tmp_path / "d")}
    old = dict(os.environ); os.environ.update(env)
    try:
        rp = cn.retrieve("q")
    finally:
        os.environ.clear(); os.environ.update(old)
    assert rp.strip() == "{}"
    # dead port → {} on reads (fast timeout)
    env = {"CN_BASE_URL": "http://127.0.0.1:1", "MINI_ORK_HOME": str(tmp_path / "x"),
           "CN_TIMEOUT_SEC": "1"}
    old = dict(os.environ); os.environ.update(env)
    try:
        rp = cn.retrieve("q")
    finally:
        os.environ.clear(); os.environ.update(old)
    assert rp.strip() == "{}"


# ---- 4. available / capsule / hook_post ----

def test_available_hook_capsule(tmp_path):
    srv, base = _server()
    try:
        old = dict(os.environ)
        os.environ.update({"CN_BASE_URL": base, "MINI_ORK_HOME": str(tmp_path / "a"),
                           "CN_TIMEOUT_SEC": "5", "CN_PING_TTL": "0"})
        try:
            assert cn.available() is True
            rp = cn.capsule("stub", "14d")
        finally:
            os.environ.clear(); os.environ.update(old)
        # capsule: kind-ordered markdown served by the stub comes back verbatim
        assert rp.strip() == _CAPSULE_MD.strip()
        assert "## Risks" in rp and "## Decisions" in rp

        # hook_post — fire-and-forget POST reaches /api/v1/cc/hook/session_start
        srv.hook_events.clear()
        old = dict(os.environ)
        os.environ.update({"CN_BASE_URL": base, "MINI_ORK_HOME": str(tmp_path / "hp"),
                           "CN_TIMEOUT_SEC": "5", "CN_HOOK_TIMEOUT_SEC": "5", "CN_PING_TTL": "0"})
        try:
            cn.hook_post("session_start", "sess-py", "/tmp", "")
        finally:
            os.environ.clear(); os.environ.update(old)
        got = _poll_hooks(srv, want=1, timeout=5.0)
        assert len(got) >= 1, f"expected >=1 hook POST, got {got}"
        sids = {e["body"].get("session_id") for e in got if e["event"] == "session_start"}
        assert "sess-py" in sids, f"hook session_ids={sids}"
        for e in got:
            assert e["event"] == "session_start"
            assert e["body"].get("hook_event_name") == "session_start"
    finally:
        srv.shutdown()
