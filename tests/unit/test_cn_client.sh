#!/usr/bin/env bash
# tests/unit/test_cn_client.sh — unit tests for lib/cn_client.sh
# Usage: bash tests/unit/test_cn_client.sh
# Exit 0 = all assertions pass.  Exit 1 = any assertion failed.
#
# Strategy: spawn a tiny python http.server stub that mimics CN's HTTP
# surface for the endpoints we use. No real CN required.
set -uo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
export MINI_ORK_ROOT
LIB="$MINI_ORK_ROOT/lib/cn_client.sh"

PASS=0; FAIL=0; SKIP=0
_ok()   { echo "  [OK]   $*"; PASS=$((PASS+1)); }
_fail() { echo "  [FAIL] $*"; FAIL=$((FAIL+1)); }
_skip() { echo "  [SKIP] $*"; SKIP=$((SKIP+1)); }

echo "── unit: cn_client.sh ──"

[ -f "$LIB" ] || { _skip "lib/cn_client.sh missing"; exit 0; }

# Pick a random high port; tear down the stub on exit.
PORT=$(python3 -c 'import socket; s=socket.socket(); s.bind(("",0)); print(s.getsockname()[1]); s.close()')
STUB_LOG=$(mktemp /tmp/cn-stub-XXXXXX.log)
STUB_PIDFILE=$(mktemp /tmp/cn-stub-pid-XXXXXX)

# Isolated state dir so the reachability cache doesn't bleed across runs.
MINI_ORK_HOME=$(mktemp -d /tmp/mo-test-XXXXXX)
export MINI_ORK_HOME

cleanup() {
  if [ -f "$STUB_PIDFILE" ]; then
    local pid; pid=$(cat "$STUB_PIDFILE" 2>/dev/null || true)
    [ -n "$pid" ] && kill "$pid" 2>/dev/null || true
  fi
  rm -f "$STUB_LOG" "$STUB_PIDFILE"
  rm -rf "$MINI_ORK_HOME"
}
trap cleanup EXIT

# Tiny CN stub.
python3 - "$PORT" "$STUB_LOG" "$STUB_PIDFILE" <<'PY' &
import json, os, sys, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

port, log_path, pid_path = int(sys.argv[1]), sys.argv[2], sys.argv[3]
hook_events = []
retrieve_calls = []

class H(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args): pass

    def _send_json(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/api/v1/substrate/health":
            self._send_json(200, {"ok": True}); return
        if self.path.startswith("/api/v1/sessions/by-file"):
            self._send_json(200, {"sessions": [
                {"session_id": "sess-aaaaaaaa", "last_seen": "2026-06-10T00:00:00Z", "title": "fixture-session"}
            ]}); return
        if self.path.startswith("/api/v1/inbox"):
            self._send_json(200, {"items": [
                {"kind": "user_action", "session_id": "sess-bbbbbbbb", "content": "stub inbox item"}
            ]}); return
        if self.path.startswith("/api/v1/features"):
            self._send_json(200, {"features": [
                {"feature": "stub feature", "layer": "backend", "how_to_test": "curl localhost"}
            ]}); return
        self._send_json(404, {"error": "not found"})

    def do_POST(self):
        ln = int(self.headers.get("Content-Length", "0") or 0)
        body_raw = self.rfile.read(ln) if ln else b"{}"
        try: body = json.loads(body_raw)
        except Exception: body = {}
        if self.path == "/api/v1/tools/retrieve":
            retrieve_calls.append(body)
            with open(log_path, "a") as f:
                f.write(f"retrieve {json.dumps(body)}\n")
            self._send_json(200, {"hits": [
                {"id": "cn-stub-1", "content": "stub hit one", "similarity": 0.9,
                 "session_id": "sess-cccccccc",
                 "metadata": {"kind": "learning", "ts": "2026-06-15T00:00:00Z"}},
                {"id": "cn-stub-2", "content": "stub hit two", "similarity": 0.8,
                 "session_id": "sess-cccccccc",
                 "metadata": {"kind": "decision_made", "ts": "2026-06-14T00:00:00Z"}},
            ]})
            return
        if self.path.startswith("/api/v1/cc/hook/"):
            event = self.path.rsplit("/", 1)[-1]
            hook_events.append({"event": event, "body": body})
            with open(log_path, "a") as f:
                f.write(f"hook {event} {json.dumps(body)}\n")
            self.send_response(204); self.end_headers(); return
        self._send_json(404, {"error": "not found"})

srv = ThreadingHTTPServer(("127.0.0.1", port), H)
with open(pid_path, "w") as f: f.write(str(os.getpid()))
srv.serve_forever()
PY

# Wait for stub to come up
for _ in $(seq 1 20); do
  if curl -s -o /dev/null -w '%{http_code}' --max-time 1 "http://127.0.0.1:$PORT/api/v1/substrate/health" 2>/dev/null | grep -q 200; then
    break
  fi
  sleep 0.1
done

export CN_BASE_URL="http://127.0.0.1:$PORT"
export CN_TIMEOUT_SEC=2
export CN_HOOK_TIMEOUT_SEC=2
export CN_PING_TTL=0   # force re-ping each call so the test isn't order-dependent

# shellcheck source=/dev/null
source "$LIB"

echo ""
echo "--- cn_available reaches the stub ---"
if cn_available; then _ok "cn_available returns 0 against stub"
else _fail "cn_available returned non-zero against stub"; fi

echo ""
echo "--- cn_retrieve returns JSON with hits ---"
out=$(cn_retrieve "schema audit" 3)
hits=$(echo "$out" | python3 -c 'import json,sys; print(len(json.load(sys.stdin).get("hits",[])))' 2>/dev/null || echo "0")
if [ "$hits" -eq 2 ]; then _ok "cn_retrieve returns 2 hits from stub"
else _fail "cn_retrieve hits expected 2, got $hits — out='$out'"; fi

echo ""
echo "--- cn_render_atoms_md emits markdown block ---"
rendered=$(cn_render_atoms_md "$out" 3)
if echo "$rendered" | grep -q "ContextNest atoms"; then _ok "render header present"
else _fail "render missing header — rendered='$rendered'"; fi
if echo "$rendered" | grep -q "stub hit one"; then _ok "stub hit one rendered"
else _fail "stub hit one missing"; fi

echo ""
echo "--- cn_render_atoms_md is silent on empty hits ---"
rendered_empty=$(cn_render_atoms_md '{"hits":[]}' 3)
if [ -z "$rendered_empty" ]; then _ok "empty hits → empty render"
else _fail "expected empty render, got '$rendered_empty'"; fi

echo ""
echo "--- cn_hook_post fires (async; check log) ---"
cn_hook_post "session_start" "test-session-XYZ" "/tmp" ""
# fire-and-forget — give it a moment
sleep 0.3
if grep -q "hook session_start" "$STUB_LOG"; then _ok "stub received session_start"
else _fail "stub did NOT receive session_start — log: $(cat "$STUB_LOG")"; fi

echo ""
echo "--- MO_DISABLE_CN=1 short-circuits everything ---"
export MO_DISABLE_CN=1
out_disabled=$(cn_retrieve "anything" 3)
if [ "$out_disabled" = "{}" ]; then _ok "cn_retrieve returns {} when disabled"
else _fail "expected {}, got '$out_disabled'"; fi
unset MO_DISABLE_CN

echo ""
echo "--- CN-down path: cn_retrieve returns {} silently ---"
# Stop the stub
kill "$(cat "$STUB_PIDFILE")" 2>/dev/null || true
# Wipe ping cache + bypass TTL
rm -f "$MINI_ORK_HOME/state/cn_ping.cache"
out_down=$(cn_retrieve "anything" 3)
if [ "$out_down" = "{}" ]; then _ok "cn_retrieve returns {} when CN unreachable"
else _fail "expected {} when CN down, got '$out_down'"; fi

echo ""
echo "── Results: ${PASS} OK  ${SKIP} SKIP  ${FAIL} FAIL ──"
[ "$FAIL" -eq 0 ]
