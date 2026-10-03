"""Unit tests: ``mini-ork concord`` (Concord P0 client — principals, run, mailbox).

The server half of Concord is built in parallel, so every test drives the CLI
against an in-process stub ``http.server.ThreadingHTTPServer`` implementing the
P0 endpoints in memory. ``CN_BASE_URL`` points at the stub via monkeypatch;
nothing depends on a live ContextNest. The threading server matters: ``run``
issues concurrent upserts from a daemon heartbeat thread, so a single-threaded
server would serialize and could starve the heartbeat assertions.
"""
from __future__ import annotations

import http.server
import json
import os
import shlex
import signal
import socket
import subprocess
import threading
import time
import urllib.parse

from mini_ork.orchestration import concord


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ---- in-memory P0 stub server ----

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

    def _route(self):
        url = urllib.parse.urlsplit(self.path)
        parts = [p for p in urllib.parse.unquote(url.path).split("/") if p]
        query = urllib.parse.parse_qs(url.query)
        return parts, query

    # endpoint handlers (paths already split; prefix parts[2:4] == ["coord", "principals"])

    def _upsert(self, pid, body):
        self.server.events.append(("PUT", pid, body))
        stored = self.server.principals.get(pid) or {}
        now = _now()
        principal = dict(stored)
        for key in ("harness", "host", "cwd", "worktree", "pgid", "pids",
                    "tmux_pane", "kill_recipe", "priority"):
            if key in body:
                principal[key] = body[key]
        if body.get("labels"):
            principal["labels"] = {**(stored.get("labels") or {}), **(body["labels"] or {})}
        principal["principal_id"] = pid
        principal["kind"] = pid.split(":", 1)[0]
        if not principal.get("started_at"):
            principal["started_at"] = now
        principal["last_seen"] = now
        principal["ended_at"] = None
        principal["status"] = "live"
        self.server.principals[pid] = principal
        unacked = sum(1 for m in self.server.messages
                      if m["principal_id"] == pid and not m["acked_at"])
        self._json(200, {"principal": principal, "unacked_messages": unacked})

    def _list(self, status):
        self.server.events.append(("GET", "__list__", None))
        principals = list(self.server.principals.values())
        if status != "all":
            principals = [p for p in principals if p.get("status") != "ended"]
        principals.sort(key=lambda p: p.get("last_seen") or "", reverse=True)
        self._json(200, {"count": len(principals), "principals": principals})

    def _get_principal(self, pid):
        principal = self.server.principals.get(pid)
        if principal is None:
            self._json(404, {"error": "not found"})
            return
        self._json(200, principal)

    def _end(self, pid):
        self.server.events.append(("DELETE", pid, None))
        principal = self.server.principals.get(pid)
        if principal is None:
            self._json(404, {"error": "not found"})
            return
        principal["status"] = "ended"
        principal["ended_at"] = _now()
        self._json(200, principal)

    def _send(self, pid, body):
        self.server.events.append(("POST", pid, body))
        if pid not in self.server.principals:
            self._json(404, {"error": "unknown principal"})
            return
        text = body.get("body") or ""
        if not text:
            self._json(400, {"error": "empty body"})
            return
        msg = {"msg_id": f"M-{self.server.next_id}", "principal_id": pid,
               "from": body.get("from"), "body": text, "created_at": _now(),
               "delivered_at": None, "delivered_to": None,
               "acked_at": None, "acked_by": None}
        self.server.next_id += 1
        self.server.messages.append(msg)
        self._json(201, msg)

    def _inbox(self, pid, unacked):
        if pid not in self.server.principals:
            self._json(404, {"error": "unknown principal"})
            return
        msgs = [m for m in self.server.messages if m["principal_id"] == pid]
        if unacked:
            msgs = [m for m in msgs if not m["acked_at"]]
        msgs.sort(key=lambda m: m.get("created_at") or "")
        self._json(200, {"messages": msgs})

    def _ack(self, pid, msg_id, body):
        for m in self.server.messages:
            if m["principal_id"] == pid and m["msg_id"] == msg_id:
                m["acked_at"] = _now()
                m["acked_by"] = body.get("by")
                self._json(200, m)
                return
        self._json(404, {"error": "not found"})

    # do_* dispatch

    def do_PUT(self):
        parts, _ = self._route()
        if len(parts) == 5 and parts[2:4] == ["coord", "principals"]:
            self._upsert(parts[4], self._read())
        else:
            self._json(404, {"error": "not found"})

    def do_DELETE(self):
        parts, _ = self._route()
        if len(parts) == 5 and parts[2:4] == ["coord", "principals"]:
            self._end(parts[4])
        else:
            self._json(404, {"error": "not found"})

    def do_GET(self):
        parts, query = self._route()
        if len(parts) == 4 and parts[2:4] == ["coord", "principals"]:
            self._list(query.get("status", ["active"])[0])
        elif len(parts) == 5 and parts[2:4] == ["coord", "principals"]:
            self._get_principal(parts[4])
        elif len(parts) == 6 and parts[2:4] == ["coord", "principals"] and parts[5] == "messages":
            self._inbox(parts[4], query.get("unacked", ["true"])[0] == "true")
        elif parts[2:4] == ["coord", "hot-claims"]:
            self._json(200, {"claims": getattr(self.server, "claims", [])})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        parts, _ = self._route()
        body = self._read()
        if len(parts) == 6 and parts[2:4] == ["coord", "principals"] and parts[5] == "messages":
            self._send(parts[4], body)
        elif (len(parts) == 8 and parts[2:4] == ["coord", "principals"]
              and parts[5] == "messages" and parts[7] == "ack"):
            self._ack(parts[4], parts[6], body)
        else:
            self._json(404, {"error": "not found"})


def _server():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    srv.principals = {}
    srv.messages = []
    srv.events = []
    srv.next_id = 1
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def _principal(pid, *, pgid=None, host=None):
    return {"principal_id": pid, "kind": pid.split(":", 1)[0],
            "harness": "shell", "host": host or socket.gethostname(),
            "cwd": "/tmp", "pgid": pgid, "pids": [pgid] if pgid else [],
            "started_at": _now(), "last_seen": _now(), "status": "live"}


def _puts(srv):
    return [e for e in srv.events if e[0] == "PUT"]


# ---- required tests ----

def test_run_registers_principal_and_ends_it(monkeypatch, tmp_path):
    srv, base = _server()
    monkeypatch.setenv("CN_BASE_URL", base)
    monkeypatch.setenv("CN_COORD_TIMEOUT_SEC", "5")
    marker = tmp_path / "principal.txt"
    cmd = f"printf '%s' \"$CONCORD_PRINCIPAL\" > {shlex.quote(str(marker))}; exit 7"
    try:
        rc = concord.main(["run", "--name", "test-run", "--", "sh", "-c", cmd])
    finally:
        srv.shutdown()
    assert rc == 7
    assert marker.read_text() == "loop:test-run"
    puts = _puts(srv)
    assert puts, "expected at least one PUT upsert"
    fields = puts[0][2]
    assert fields["pgid"] > 1
    assert isinstance(fields["pids"], list) and fields["pids"]
    assert fields["kill_recipe"] == [f"kill -TERM -{fields['pgid']}"]
    assert fields["cwd"] == os.getcwd()
    assert fields["harness"] == "shell"
    deletes = [e for e in srv.events if e[0] == "DELETE"]
    assert deletes and deletes[0][1] == "loop:test-run"


def test_run_fails_open_when_contextnest_down(monkeypatch, capsys):
    monkeypatch.setenv("CN_BASE_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("CN_COORD_TIMEOUT_SEC", "2")
    rc = concord.main(["run", "--name", "fail-open", "sh", "-c", "exit 7"])
    err = capsys.readouterr().err
    assert rc == 7
    assert err.count("warning:") == 1


def test_run_heartbeats(monkeypatch):
    srv, base = _server()
    monkeypatch.setenv("CN_BASE_URL", base)
    monkeypatch.setenv("CN_COORD_TIMEOUT_SEC", "5")
    try:
        rc = concord.main(["run", "--name", "hb", "--heartbeat-secs", "1",
                           "--", "sh", "-c", "sleep 2.5"])
    finally:
        srv.shutdown()
    assert rc == 0
    assert len(_puts(srv)) >= 2


def test_run_heartbeat_refreshes_live_pids(monkeypatch):
    # Loops spawn a new worker per step: each heartbeat must report the
    # CURRENT group membership, not the set captured at launch.
    calls = []

    def fake_group_pids(pgid, fallback):
        calls.append(pgid)
        return [pgid, 900000 + len(calls)]

    monkeypatch.setattr(concord, "_group_pids", fake_group_pids)
    srv, base = _server()
    monkeypatch.setenv("CN_BASE_URL", base)
    monkeypatch.setenv("CN_COORD_TIMEOUT_SEC", "5")
    try:
        rc = concord.main(["run", "--name", "hbpids", "--heartbeat-secs", "1",
                           "--", "sh", "-c", "sleep 2.5"])
    finally:
        srv.shutdown()
    assert rc == 0
    pid_sets = [tuple(e[2].get("pids") or ()) for e in _puts(srv) if len(e) > 2 and isinstance(e[2], dict)]
    assert len(set(pid_sets)) >= 2, pid_sets


def test_stop_on_tty_asks_and_honours_no(monkeypatch):
    srv, base = _server()
    monkeypatch.setenv("CN_BASE_URL", base)
    monkeypatch.setenv("CN_COORD_TIMEOUT_SEC", "5")
    monkeypatch.setattr(concord.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _prompt="": "n")
    proc = subprocess.Popen(["sleep", "30"], start_new_session=True)
    pgid = os.getpgid(proc.pid)
    try:
        srv.principals["loop:asked"] = _principal("loop:asked", pgid=pgid)
        assert concord.main(["stop", "loop:asked"]) == 4
        assert proc.poll() is None  # declined → still running
    finally:
        os.killpg(pgid, signal.SIGKILL)
        proc.wait(timeout=10)
        srv.shutdown()


def test_ps_json_lists_principals_and_ps_exits_3_when_down(monkeypatch, capsys):
    srv, base = _server()
    srv.principals["loop:alpha"] = _principal("loop:alpha", pgid=100)
    monkeypatch.setenv("CN_BASE_URL", base)
    monkeypatch.setenv("CN_COORD_TIMEOUT_SEC", "5")
    try:
        rc = concord.main(["ps", "--json"])
    finally:
        srv.shutdown()
    data = json.loads(capsys.readouterr().out)
    assert rc == 0
    ids = {p["principal_id"] for p in data.get("principals", [])}
    assert "loop:alpha" in ids

    monkeypatch.setenv("CN_BASE_URL", "http://127.0.0.1:1")
    assert concord.main(["ps"]) == 3


def test_stop_refusals_and_real_kill(monkeypatch):
    srv, base = _server()
    monkeypatch.setenv("CN_BASE_URL", base)
    monkeypatch.setenv("CN_COORD_TIMEOUT_SEC", "5")
    try:
        # refuse when the pgid is this process's own group
        srv.principals["loop:self"] = _principal("loop:self", pgid=os.getpgid(0))
        assert concord.main(["stop", "loop:self", "--yes"]) == 4

        # refuse when no pgid is recorded
        srv.principals["loop:nopgid"] = _principal("loop:nopgid", pgid=None)
        assert concord.main(["stop", "loop:nopgid", "--yes"]) == 4

        # succeed against a real sleep 30 started in its own session
        proc = subprocess.Popen(["sleep", "30"], start_new_session=True)
        try:
            pgid = os.getpgid(proc.pid)
            srv.principals["loop:sleeper"] = _principal("loop:sleeper", pgid=pgid)
            assert concord.main(["stop", "loop:sleeper", "--yes"]) == 0
            proc.wait(timeout=10)
            assert proc.returncode != 0  # killed by a signal, not a clean exit
        finally:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except (ProcessLookupError, NameError):
                pass
    finally:
        srv.shutdown()


def test_send_then_inbox_ack(monkeypatch, capsys):
    srv, base = _server()
    srv.principals["loop:box"] = _principal("loop:box", pgid=200)
    monkeypatch.setenv("CN_BASE_URL", base)
    monkeypatch.setenv("CN_COORD_TIMEOUT_SEC", "5")
    monkeypatch.setenv("CONCORD_PRINCIPAL", "human:tester")
    try:
        assert concord.main(["send", "loop:box", "hello", "world",
                             "--from", "human:tester"]) == 0

        assert concord.main(["inbox", "--principal", "loop:box", "--format", "json"]) == 0
        data = json.loads(capsys.readouterr().out)
        assert len(data["messages"]) == 1
        assert data["messages"][0]["body"] == "hello world"
        assert data["messages"][0]["from"] == "human:tester"

        assert concord.main(["inbox", "--principal", "loop:box",
                             "--format", "prompt", "--ack"]) == 0
        out = capsys.readouterr().out
        assert "[concord] 1 message(s) for loop:box" in out
        assert "from human:tester" in out
        assert "hello world" in out

        assert concord.main(["inbox", "--principal", "loop:box",
                             "--format", "prompt"]) == 0
        assert capsys.readouterr().out == ""
    finally:
        srv.shutdown()


def test_invalid_principal_id_exits_2(monkeypatch):
    monkeypatch.setenv("CN_BASE_URL", "http://127.0.0.1:1")
    for bad in ("foo", "loop:", "bad kind:x"):
        assert concord.main(["stop", bad, "--yes"]) == 2, bad


def test_claims_lists_live_hot_claims_and_exits_3_when_down(monkeypatch, capsys):
    srv, base = _server()
    srv.claims = [{"path": "/r/.mini-ork/config/agents.yaml", "principal_id": "loop:a",
                   "expires_at": "2026-10-03T08:00:00Z", "last_write_seq": 41}]
    monkeypatch.setenv("CN_BASE_URL", base)
    monkeypatch.setenv("CN_COORD_TIMEOUT_SEC", "5")
    try:
        assert concord.main(["claims"]) == 0
        out = capsys.readouterr().out
        assert "/r/.mini-ork/config/agents.yaml\tloop:a" in out
        srv.claims = []
        assert concord.main(["claims"]) == 0
        assert "(no live hot-file claims)" in capsys.readouterr().out
    finally:
        srv.shutdown()
    monkeypatch.setenv("CN_BASE_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("CN_COORD_TIMEOUT_SEC", "1")
    assert concord.main(["claims"]) == 3
