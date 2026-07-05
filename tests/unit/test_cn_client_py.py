"""Parity gate: mini_ork.cn_client vs lib/cn_client.sh.

Three surfaces:
  1. render_* markdown — run the LIVE bash renderer and the port on identical
     JSON fixtures (incl. empty / truncation / missing-field edges); byte-equal.
  2. HTTP round-trips — a mock CN server (health 200 + canned bodies); compare
     bash `cn_retrieve` / `cn_inbox` vs the port against the same server.
  3. Fallbacks — MO_DISABLE_CN=1 and a dead port both yield {} on reads, in both.
"""
from __future__ import annotations

import http.server
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork import cn_client as cn  # noqa: E402

LIB = REPO / "lib" / "cn_client.sh"


def _bash_fn(call: str, *args, env=None):
    """source cn_client.sh and invoke one function; return stdout."""
    script = f'source "{LIB}"; {call}'
    argv = ["bash", "-c", script, "_", *[str(a) for a in args]]
    return subprocess.run(argv, capture_output=True, text=True,
                          env={**os.environ, **(env or {})}).stdout


# ---- 1. render_* parity ----

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


def test_render_parity():
    cases = [
        ('cn_render_atoms_md "$1" "$2"', cn.render_atoms_md, _RETRIEVE, 5),
        ('cn_render_features_md "$1" "$2" "$3"', None, _FEATURES, None),  # 3-arg, handled below
        ('cn_render_inbox_md "$1" "$2"', cn.render_inbox_md, _INBOX, 5),
        ('cn_render_basins_md "$1" "$2"', cn.render_basins_md, _BASINS, 5),
    ]
    # atoms / inbox / basins (2-arg)
    for call, fn, payload, limit in cases:
        if fn is None:
            continue
        rb = _bash_fn(call, payload, limit)
        rp = fn(payload, limit)
        assert rb == rp, f"{call}\nBASH:{rb!r}\nPY:  {rp!r}"
    # features (3-arg: json, cwd, limit)
    rb = _bash_fn('cn_render_features_md "$1" "$2" "$3"', _FEATURES, "/ps/mini-ork", 6)
    rp = cn.render_features_md(_FEATURES, "/ps/mini-ork", 6)
    assert rb == rp, f"features\nBASH:{rb!r}\nPY:  {rp!r}"


def test_render_empty_and_bad_json():
    for call, fn in [('cn_render_atoms_md "$1" "$2"', cn.render_atoms_md),
                     ('cn_render_inbox_md "$1" "$2"', cn.render_inbox_md),
                     ('cn_render_basins_md "$1" "$2"', cn.render_basins_md)]:
        for payload in ('{}', '{"hits":[]}', 'not json'):
            assert _bash_fn(call, payload, 5) == fn(payload, 5) == ""


# ---- 2. mock-server HTTP parity ----

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
        elif self.path.startswith("/api/v1/inbox"):
            self._send('{"items":[{"kind":"todo","id":"x"}]}')
        else:
            self._send("{}")

    def do_POST(self):
        ln = int(self.headers.get("Content-Length", 0))
        self.rfile.read(ln)
        self._send('{"hits":[{"content":"hi","similarity":0.5}]}')


def _server():
    srv = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def test_http_parity(tmp_path):
    srv, base = _server()
    try:
        for i, (call, fn) in enumerate([
                ('cn_retrieve "$1" "$2"', lambda: cn.retrieve("q", 3)),
                ('cn_inbox "$1"', lambda: cn.inbox(5))]):
            hb = tmp_path / f"hb{i}"; hp = tmp_path / f"hp{i}"
            env_b = {"CN_BASE_URL": base, "MINI_ORK_HOME": str(hb), "CN_TIMEOUT_SEC": "5"}
            rb = _bash_fn(call, "q", 3 if i == 0 else 5, env=env_b)
            old = dict(os.environ)
            os.environ.update({"CN_BASE_URL": base, "MINI_ORK_HOME": str(hp), "CN_TIMEOUT_SEC": "5"})
            try:
                rp = fn()
            finally:
                os.environ.clear(); os.environ.update(old)
            assert rb.strip() == rp.strip(), f"{call}\nBASH:{rb!r}\nPY:{rp!r}"
    finally:
        srv.shutdown()


# ---- 3. fallback parity ----

def test_disabled_and_down_fallback(tmp_path):
    # disabled → {} on reads
    env = {"MO_DISABLE_CN": "1", "MINI_ORK_HOME": str(tmp_path / "d")}
    rb = _bash_fn('cn_retrieve "$1"', "q", env=env)
    old = dict(os.environ); os.environ.update(env)
    try:
        rp = cn.retrieve("q")
    finally:
        os.environ.clear(); os.environ.update(old)
    assert rb.strip() == rp.strip() == "{}"
    # dead port → {} on reads (fast timeout)
    env = {"CN_BASE_URL": "http://127.0.0.1:1", "MINI_ORK_HOME": str(tmp_path / "x"),
           "CN_TIMEOUT_SEC": "1"}
    rb = _bash_fn('cn_retrieve "$1"', "q", env=env)
    old = dict(os.environ); os.environ.update(env)
    try:
        rp = cn.retrieve("q")
    finally:
        os.environ.clear(); os.environ.update(old)
    assert rb.strip() == rp.strip() == "{}"
