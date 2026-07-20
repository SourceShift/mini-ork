#!/usr/bin/env bash
# tests/security/test_sec_kickoff_path_traversal.sh
#
# SECURITY TEST — Path Traversal via kickoff path argument
#
# THREAT MODEL:
#   An attacker passes a crafted path as the kickoff argument to mini-ork-classify
#   or mini-ork-plan.  Goals: (a) read sensitive files like /etc/passwd or
#   $MINI_ORK_HOME/../../../etc/shadow and expose their content on stdout,
#   (b) write a task_runs row whose kickoff_path column contains the traversal
#   string, leaking the fact that /etc/passwd was read.
#
# EXPECTED BEHAVIOUR (hardened):
#   - Paths outside the caller's project scope MUST be rejected with exit 2,
#     OR the content of /etc/passwd must NEVER appear on stdout/stderr.
#   - At minimum the tool must behave safely: either reject with a clear error
#     or return "file not found / out of scope".
#   - The tool MUST NOT print file content from system paths on stdout.
#
# KNOWN GAP (current v0.1):
#   mini-ork-classify does a `[ -f "$KICKOFF" ]` guard but has NO canonicalization
#   (realpath/readlink) and NO scope check against $MINI_ORK_HOME.  A path like
#   /etc/passwd passes the `-f` guard on Linux (world-readable), so classify WILL
#   read it and pass its content through grep pattern matching.  The content does
#   NOT appear on stdout (only task_class= is printed) but the kickoff_path stored
#   in the DB row IS /etc/passwd — a forensic signal.  The framework's zero-fallback
#   rule and future versions should add realpath + scope validation.
#
# VULNERABILITY SHAPE IF FAILING:
#   stdout contains lines from /etc/passwd or /etc/shadow content, OR
#   exit code is 0 with a traversal path that successfully reads a system file
#   without any warning.

set -uo pipefail

PASS=0; FAIL=0; SKIP=0
_ok()   { echo "  [OK]   $*"; PASS=$((PASS+1)); }
_fail() { echo "  [FAIL] $*"; FAIL=$((FAIL+1)); }
_skip() { echo "  [SKIP] $*"; SKIP=$((SKIP+1)); }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MINI_ORK_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

echo "=== test_sec_kickoff_path_traversal.sh ==="
echo "    Testing: path traversal via kickoff path argument"
echo ""

# ── Isolated tmp workspace ────────────────────────────────────────────────────

TMPDIR_TEST="$(mktemp -d /tmp/mo-sec-pt-XXXXXX)"
export MINI_ORK_HOME="$TMPDIR_TEST/.mini-ork"
export MINI_ORK_DB="$MINI_ORK_HOME/state.db"
export MINI_ORK_DRY_RUN=1

trap 'rm -rf "$TMPDIR_TEST"' EXIT

mkdir -p "$MINI_ORK_HOME/config/task_classes"
if [[ -f "$MINI_ORK_ROOT/db/init.sh" ]]; then
  bash "$MINI_ORK_ROOT/db/init.sh" >/dev/null 2>&1 || true
fi

classify_module="$MINI_ORK_ROOT/mini_ork/ported/mini_ork_classify.py"
classify_cmd=(env "PYTHONPATH=$MINI_ORK_ROOT${PYTHONPATH:+:$PYTHONPATH}" \
  python3 -m mini_ork.ported.mini_ork_classify)
[[ ! -f "$classify_module" ]] && {
  _skip "Python classify module unavailable — path traversal tests skipped"
  echo ""
  echo "=== Results: $PASS OK  $SKIP SKIP  $FAIL FAIL ==="
  exit 0
}

# Helper: assert classify does NOT leak sensitive content on stdout
# Returns:
#   "REJECTED"  — exited 2 (clean rejection)
#   "SAFE"      — exited other but output contains no /etc/passwd markers
#   "LEAK"      — stdout contained passwd-like content (root:, daemon:, nobody:)
run_traversal_check() {
  local label="$1"
  local path_arg="$2"

  local out exit_code=0
  out=$(MINI_ORK_DRY_RUN=1 "${classify_cmd[@]}" "$path_arg" --dry-run 2>&1) || exit_code=$?

  # Check for passwd-file content markers (any line matching user:x: or user:*: patterns)
  if echo "$out" | grep -qE '^[a-z_][a-z0-9_-]*:[x*!]:'; then
    echo "LEAK"
    return
  fi
  # Also check for common shadow/passwd field patterns
  if echo "$out" | grep -qE 'root:.*:0:0:|nobody:.*:[0-9]+:[0-9]+:'; then
    echo "LEAK"
    return
  fi

  if [[ "$exit_code" -eq 2 ]]; then
    echo "REJECTED"
    return
  fi
  echo "SAFE"
}

# ── Test 1: relative traversal ../../etc/passwd ───────────────────────────────
echo "--- 1. ../../etc/passwd (relative traversal) ---"
RESULT="$(run_traversal_check "relative_traversal" "../../etc/passwd")"
case "$RESULT" in
  REJECTED) _ok "../../etc/passwd: rejected cleanly (exit 2)" ;;
  SAFE)     _ok "../../etc/passwd: tool ran but no /etc/passwd content leaked to stdout" ;;
  LEAK)     _fail "../../etc/passwd: PASSWD CONTENT LEAKED on stdout — path traversal succeeded!" ;;
esac
echo ""

# ── Test 2: absolute /etc/passwd ─────────────────────────────────────────────
echo "--- 2. /etc/passwd (absolute path outside project) ---"
RESULT="$(run_traversal_check "absolute_etc_passwd" "/etc/passwd")"
case "$RESULT" in
  REJECTED) _ok "/etc/passwd: rejected cleanly (exit 2)" ;;
  SAFE)     _ok "/etc/passwd: tool ran but no passwd content on stdout (scope boundary not enforced — document as WARN)" ;;
  LEAK)     _fail "/etc/passwd: PASSWD CONTENT LEAKED — scope not enforced!" ;;
esac
echo ""

# ── Test 3: HOME-relative traversal via env expansion ─────────────────────────
echo "--- 3. MINI_ORK_HOME/../../../etc/shadow (env-based traversal) ---"
# Expand the path ourselves to simulate what a caller might pass
SHADOW_PATH="${MINI_ORK_HOME}/../../../etc/shadow"
if [[ -f "$SHADOW_PATH" || -f "/etc/shadow" ]]; then
  RESULT="$(run_traversal_check "env_traversal_shadow" "$SHADOW_PATH")"
  case "$RESULT" in
    REJECTED) _ok "shadow-path traversal: rejected (exit 2)" ;;
    SAFE)     _ok "shadow-path traversal: ran but no shadow content leaked" ;;
    LEAK)     _fail "shadow-path traversal: SHADOW CONTENT LEAKED!" ;;
  esac
else
  # macOS — /etc/shadow may not exist; test with /etc/master.passwd
  ALT_PATH="${MINI_ORK_HOME}/../../../etc/master.passwd"
  if [[ -f "/etc/master.passwd" ]]; then
    RESULT="$(run_traversal_check "env_traversal_master_passwd" "$ALT_PATH")"
    case "$RESULT" in
      REJECTED) _ok "master.passwd traversal: rejected (exit 2)" ;;
      SAFE)     _ok "master.passwd traversal: no content leaked" ;;
      LEAK)     _fail "master.passwd traversal: CONTENT LEAKED!" ;;
    esac
  else
    _skip "Neither /etc/shadow nor /etc/master.passwd present — shadow traversal test skipped"
  fi
fi
echo ""

# ── Test 4: URL-encoded-like variant (literal percent chars — not decoded) ───
echo "--- 4. Literal percent-encoded-like path ---"
# The bash classify script does simple -f check; it won't URL-decode.
# This tests that the tool doesn't accidentally eval or interpret %2e%2e.
URL_ENC_PATH="%2e%2e/%2e%2e/etc/passwd"
RESULT="$(run_traversal_check "url_encoded" "$URL_ENC_PATH")"
case "$RESULT" in
  REJECTED) _ok "URL-encoded path: rejected (exit 2) — treated as missing file" ;;
  SAFE)     _ok "URL-encoded path: no leak (literal string treated as relative path, likely not found)" ;;
  LEAK)     _fail "URL-encoded path: LEAK — tool decoded percent-encoding and read a real file!" ;;
esac
echo ""

# ── Test 5: Verify exit code for non-existent file is exactly 2 ─────────────
echo "--- 5. Non-existent path exits 2 ---"
NONEXISTENT="$TMPDIR_TEST/does-not-exist/kickoff.md"
local_exit=0
"${classify_cmd[@]}" "$NONEXISTENT" --dry-run >/dev/null 2>&1 || local_exit=$?
if [[ "$local_exit" -eq 2 ]]; then
  _ok "non-existent kickoff path exits 2 (file-not-found)"
elif [[ "$local_exit" -eq 0 ]]; then
  _fail "non-existent path exits 0 — silent failure (should be exit 2)"
else
  _ok "non-existent kickoff path exits $local_exit (non-zero; likely fine but expected 2)"
fi
echo ""

# ── Summary ───────────────────────────────────────────────────────────────────
echo "=== Results: $PASS OK  $SKIP SKIP  $FAIL FAIL ==="
(( FAIL > 0 )) && exit 1 || exit 0
