"""Unit tests: ``mini_ork`` run principals (Concord P1a — ``concord_run``).

Every ``mini-ork run`` registers itself as a Concord principal so its node
workers inherit ``CONCORD_PRINCIPAL``. The server half of Concord is built in
parallel, so these tests drive ``concord_run.start``/``stop`` against an
in-process stub ``http.server.ThreadingHTTPServer`` implementing only the P0
upsert (PUT) and end (DELETE) endpoints. ``CN_BASE_URL`` points at the stub via
monkeypatch; nothing depends on a live ContextNest. The threading server is
load-bearing: the daemon heartbeat fires concurrent upserts, so a
single-threaded server would serialize and could starve the heartbeat
assertions. (Pattern reused from ``test_concord_cli.py`` — not imported.)
"""
from __future__ import annotations

import http.server
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.parse

from mini_ork.orchestration import concord_run


# ---- in-memory P0 stub server (upsert + end only) ----

class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):  # noqa: A002 — match base signature
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read(self):
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw)
        except Exception:
            return {}

    def _principal(self):
        parts = [p for p in urllib.parse.unquote(urllib.parse.urlsplit(self.path).path).split("/") if p]
        if len(parts) == 5 and parts[2:4] == ["coord", "principals"]:
            return parts[4]
        return None

    def do_PUT(self):
        pid = self._principal()
        body = self._read()
        if pid is None:
            self._json(404, {"error": "not found"})
            return
        self.server.events.append(("PUT", pid, body))
        self._json(200, {"principal": {"principal_id": pid}, "unacked_messages": 0})

    def do_DELETE(self):
        pid = self._principal()
        if pid is None:
            self._json(404, {"error": "not found"})
            return
        self.server.events.append(("DELETE", pid, None))
        self._json(200, {"principal": {"principal_id": pid, "status": "ended"}})


def _server():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    srv.events = []
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def _puts(srv):
    return [e for e in srv.events if e[0] == "PUT"]


# ---- required tests ----

def test_start_registers_principal_and_child_inherits_env(monkeypatch):
    srv, base = _server()
    monkeypatch.setenv("CN_BASE_URL", base)
    monkeypatch.setenv("CN_COORD_TIMEOUT_SEC", "5")
    monkeypatch.setenv("MO_TARGET_CWD", "/tmp/wt")
    monkeypatch.delenv("CONCORD_PRINCIPAL", raising=False)
    h = None
    try:
        h = concord_run.start("my-run", "recipe-x", "/abs/kickoff.md")
        assert h is not None
        pid = "run:my-run"
        puts = _puts(srv)
        assert puts and puts[0][1] == pid
        fields = puts[0][2]
        assert fields["harness"] == "mini-ork"
        assert fields["host"] == socket.gethostname()
        assert fields["cwd"] == os.getcwd()
        assert fields["worktree"] == "/tmp/wt"
        assert fields["pids"] == [os.getpid()]
        assert fields["kill_recipe"] == [f"kill -TERM {os.getpid()}"]
        assert fields["labels"]["recipe"] == "recipe-x"
        assert fields["labels"]["kickoff"] == "kickoff.md"
        assert "parent" not in fields["labels"]
        assert "pgid" not in fields
        out = subprocess.run(
            [sys.executable, "-c", "import os;print(os.environ['CONCORD_PRINCIPAL'])"],
            capture_output=True, text=True)
        assert out.stdout.strip() == pid
    finally:
        if h is not None:
            concord_run.stop(h)
        srv.shutdown()


def test_outer_principal_becomes_parent_and_stop_restores(monkeypatch):
    srv, base = _server()
    monkeypatch.setenv("CN_BASE_URL", base)
    monkeypatch.setenv("CN_COORD_TIMEOUT_SEC", "5")
    monkeypatch.setenv("CONCORD_PRINCIPAL", "loop:outer")
    h = None
    try:
        h = concord_run.start("inner", "r", "k.md")
        fields = _puts(srv)[0][2]
        assert fields["labels"]["parent"] == "loop:outer"
        assert os.environ["CONCORD_PRINCIPAL"] == "run:inner"
        concord_run.stop(h)
        h = None
        assert os.environ["CONCORD_PRINCIPAL"] == "loop:outer"
    finally:
        if h is not None:
            concord_run.stop(h)
        srv.shutdown()


def test_unreachable_fails_open_with_one_line_and_no_heartbeat(monkeypatch, capsys):
    monkeypatch.setenv("CN_BASE_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("CN_COORD_TIMEOUT_SEC", "1")
    monkeypatch.delenv("CONCORD_PRINCIPAL", raising=False)
    h = concord_run.start("fail-open", "r", "k.md")
    assert h is not None
    assert h.thread is None
    assert os.environ["CONCORD_PRINCIPAL"] == "run:fail-open"
    err = capsys.readouterr().err
    assert err.count("[concord] ContextNest unavailable — run not registered") == 1
    concord_run.stop(h)  # must not raise


def test_heartbeat_then_stop_ends_principal(monkeypatch):
    srv, base = _server()
    monkeypatch.setenv("CN_BASE_URL", base)
    monkeypatch.setenv("CN_COORD_TIMEOUT_SEC", "5")
    monkeypatch.setenv("MO_CONCORD_HEARTBEAT_S", "1")
    monkeypatch.delenv("CONCORD_PRINCIPAL", raising=False)
    h = concord_run.start("hb", "r", "k.md")
    try:
        time.sleep(2.5)
        assert len(_puts(srv)) >= 2
        concord_run.stop(h)
        h = None
        assert any(e[0] == "DELETE" and e[1] == "run:hb" for e in srv.events)
    finally:
        if h is not None:
            concord_run.stop(h)
        srv.shutdown()


def test_mo_concord_zero_disables_and_leaves_env_untouched(monkeypatch):
    srv, base = _server()
    monkeypatch.setenv("CN_BASE_URL", base)
    monkeypatch.setenv("MO_CONCORD", "0")
    monkeypatch.delenv("CONCORD_PRINCIPAL", raising=False)
    h = concord_run.start("whatever", "r", "k.md")
    assert h is None
    assert "CONCORD_PRINCIPAL" not in os.environ
    assert srv.events == []
    srv.shutdown()


def test_run_id_sanitized_to_valid_principal(monkeypatch):
    srv, base = _server()
    monkeypatch.setenv("CN_BASE_URL", base)
    monkeypatch.setenv("CN_COORD_TIMEOUT_SEC", "5")
    monkeypatch.delenv("CONCORD_PRINCIPAL", raising=False)
    handles = []
    try:
        handles.append(concord_run.start("bad!id@with#chars", "r", "k.md"))
        pid = _puts(srv)[0][1]
        assert re.fullmatch(r"^(loop|run|session|human|agent):[A-Za-z0-9._@/-]{1,128}$", pid)
        assert pid == "run:bad_id@with_chars"

        handles.append(concord_run.start("y" * 200, "r", "k.md"))
        long_pid = _puts(srv)[-1][1]
        assert long_pid == "run:" + "y" * 128
    finally:
        for h in handles:
            concord_run.stop(h)
        srv.shutdown()
