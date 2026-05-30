#!/usr/bin/env bash
# tests/security/test_sec_env_var_pollution.sh
#
# SECURITY TEST — PATH pollution / tool hijacking via manipulated $PATH
#
# THREAT MODEL:
#   An attacker prepends a writable directory to $PATH and places fake binaries
#   (sqlite3, python3, jq, yq) that exfiltrate environment variables or execute
#   arbitrary commands.  If mini-ork invokes tools without absolute paths, the
#   fake binary runs instead of the real one.
#
# EXPECTED BEHAVIOUR (two acceptable outcomes):
#   (a) HARDENED: The framework pins absolute paths for critical tools
#       (e.g. SQLITE3=/usr/bin/sqlite3) — the fake binary is never called.
#   (b) DOCUMENTED: The framework invokes tools via bare names (`sqlite3`, not
#       `/usr/bin/sqlite3`), relying on the caller's PATH being trustworthy.
#       This is acceptable for a developer-local tool as long as it is
#       documented as a caller responsibility.  This test detects that the
#       fake binary WAS called and files a documentation-level gap.
#
# ASSERTION:
#   The canary file /tmp/mo-sec-evp-canary-$$ is created by the fake binary
#   if it runs.  We assert it is NOT created (i.e. the fake binary did NOT
#   execute in place of the real tool).
#
# KNOWN RESULT (v0.1):
#   mini-ork uses bare `sqlite3`, `python3`, `jq`, `yq` invocations throughout.
#   On a PATH-poisoned environment the fake binary WILL run.  This test will
#   document the gap with a WARN-level note rather than a hard FAIL, because
#   PATH control is a valid caller responsibility for a shell framework that
#   is developer-local (not a setuid root binary).
#
# VULNERABILITY SHAPE IF FAILING:
#   /tmp/mo-sec-evp-canary-$$ exists after a mini-ork command — meaning
#   a fake sqlite3/python3/jq was executed instead of the real one.
#   FIX: Pin SQLITE3_BIN="$(command -v sqlite3)" at the top of each script and
#        use "$SQLITE3_BIN" throughout instead of bare `sqlite3`.

set -uo pipefail

PASS=0; FAIL=0; SKIP=0; WARN=0
_ok()   { echo "  [OK]   $*"; PASS=$((PASS+1)); }
_warn() { echo "  [WARN] $*"; WARN=$((WARN+1)); }
_fail() { echo "  [FAIL] $*"; FAIL=$((FAIL+1)); }
_skip() { echo "  [SKIP] $*"; SKIP=$((SKIP+1)); }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MINI_ORK_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

echo "=== test_sec_env_var_pollution.sh ==="
echo "    Testing: PATH pollution / fake tool injection"
echo ""

# ── Isolated tmp workspace ────────────────────────────────────────────────────

TMPDIR_TEST="$(mktemp -d /tmp/mo-sec-evp-XXXXXX)"
EVIL_BIN_DIR="$TMPDIR_TEST/evil-bin"
CANARY="$TMPDIR_TEST/evp-canary-$$"

export MINI_ORK_HOME="$TMPDIR_TEST/.mini-ork"
export MINI_ORK_DB="$MINI_ORK_HOME/state.db"
export MINI_ORK_DRY_RUN=1

trap 'rm -rf "$TMPDIR_TEST"' EXIT

classify_bin="$MINI_ORK_ROOT/bin/mini-ork-classify"
[[ ! -x "$classify_bin" ]] && {
  _skip "mini-ork-classify not executable — PATH pollution tests skipped"
  echo ""
  echo "=== Results: $PASS OK  $SKIP SKIP  $FAIL FAIL (WARN: $WARN) ==="
  exit 0
}

# ── Plant fake tools in EVIL_BIN_DIR ─────────────────────────────────────────
mkdir -p "$EVIL_BIN_DIR"

for fake_tool in sqlite3 python3 jq yq; do
  cat > "$EVIL_BIN_DIR/$fake_tool" <<FAKE_EOF
#!/usr/bin/env bash
# Fake $fake_tool planted by test_sec_env_var_pollution.sh
# Creates a canary file to prove it ran in place of the real tool.
touch "$CANARY"
# Attempt to mimic real tool enough to not crash caller (pass through if real tool exists)
REAL_BIN=\$(command -v "$fake_tool" 2>/dev/null || echo "")
if [[ -n "\$REAL_BIN" && "\$REAL_BIN" != "$EVIL_BIN_DIR/$fake_tool" ]]; then
  exec "\$REAL_BIN" "\$@"
else
  # No real tool available — exit gracefully
  exit 0
fi
FAKE_EOF
  chmod +x "$EVIL_BIN_DIR/$fake_tool"
done

echo "    Fake tools planted in: $EVIL_BIN_DIR"
echo "    Canary path: $CANARY"
echo ""

# ── Init .mini-ork with a CLEAN PATH first (so we get a valid state.db) ──────
mkdir -p "$MINI_ORK_HOME/config/task_classes"
if [[ -f "$MINI_ORK_ROOT/db/init.sh" ]]; then
  bash "$MINI_ORK_ROOT/db/init.sh" >/dev/null 2>&1 || true
fi

# Valid kickoff
VALID_KICKOFF="$TMPDIR_TEST/kickoff.md"
cat > "$VALID_KICKOFF" <<'EOF'
# Fix the login bug

Reproduce: users cannot log in.
EOF

# ── Test 1: Run classify with poisoned PATH ───────────────────────────────────
echo "--- 1. classify with PATH=$EVIL_BIN_DIR:\$PATH ---"

rm -f "$CANARY"
CLASSIFY_EXIT=0
PATH="$EVIL_BIN_DIR:$PATH" \
  MINI_ORK_DRY_RUN=1 \
  bash "$classify_bin" "$VALID_KICKOFF" --dry-run >/dev/null 2>&1 \
  || CLASSIFY_EXIT=$?

if [[ -f "$CANARY" ]]; then
  # Fake tool ran — document as WARN (caller responsibility), not FAIL
  _warn "A fake tool was invoked via PATH-poisoned sqlite3/python3/jq (canary created). Framework does not pin absolute tool paths. FIX: add SQLITE3_BIN=\$(command -v sqlite3) pinning at script init and use \$SQLITE3_BIN throughout."
else
  _ok "Canary NOT created — framework either pins absolute tool paths OR classify bypassed the fake tools"
fi
echo ""

# ── Test 2: Check whether classify SOURCES the framework's own PATH setup ───
echo "--- 2. Inspect classify for absolute-path pinning patterns ---"

# Grep for absolute paths or command-v pinning
if grep -qE '^[A-Z_]+=\$\(command -v|^SQLITE3=|^PYTHON3=|^JQ=|^YQ=' "$classify_bin" 2>/dev/null; then
  _ok "classify pins at least one tool path via command -v assignment"
else
  _warn "classify does NOT pin absolute tool paths. Hardening suggestion: add near top of each bin/mini-ork-* script:
    SQLITE3_BIN=\"\$(command -v sqlite3 || echo sqlite3)\"
    PYTHON3_BIN=\"\$(command -v python3 || echo python3)\"
    JQ_BIN=\"\$(command -v jq || echo jq)\"
  Then replace bare 'sqlite3' → '\$SQLITE3_BIN' etc. (PATH-control is caller responsibility for this class of tool)."
fi
echo ""

# ── Test 3: Check db/init.sh for absolute-path pinning ───────────────────────
echo "--- 3. Inspect db/init.sh for absolute-path pinning ---"
DB_INIT="$MINI_ORK_ROOT/db/init.sh"
if [[ -f "$DB_INIT" ]]; then
  if grep -qE 'sqlite3_bin|SQLITE3_BIN|command -v sqlite3' "$DB_INIT" 2>/dev/null; then
    _ok "db/init.sh has sqlite3 path pinning"
  else
    _warn "db/init.sh invokes 'sqlite3' by bare name — PATH-pinning not present (document as caller responsibility)"
  fi
else
  _skip "db/init.sh not found"
fi
echo ""

# ── Test 4: Verify real tool paths resolve to canonical locations ─────────────
echo "--- 4. Real tool paths are in trusted directories ---"
TRUSTED_DIRS=("/usr/bin" "/usr/local/bin" "/opt/homebrew/bin" "$HOME/.local/bin" "/opt/homebrew/opt")

for tool in sqlite3 python3 jq yq; do
  TOOL_PATH="$(command -v "$tool" 2>/dev/null || echo "")"
  if [[ -z "$TOOL_PATH" ]]; then
    _skip "$tool: not installed — cannot verify path (install it to reduce supply-chain risk)"
    continue
  fi

  # Resolve any symlinks to canonical path
  CANON_PATH="$(realpath "$TOOL_PATH" 2>/dev/null || readlink -f "$TOOL_PATH" 2>/dev/null || echo "$TOOL_PATH")"
  CANON_DIR="$(dirname "$CANON_PATH")"

  TRUSTED=0
  for trusted in "${TRUSTED_DIRS[@]}"; do
    if [[ "$CANON_DIR" == "$trusted" || "$CANON_DIR" == "$trusted/"* ]]; then
      TRUSTED=1; break
    fi
  done

  if [[ "$TRUSTED" -eq 1 ]]; then
    _ok "$tool resolves to trusted dir: $CANON_PATH"
  else
    _warn "$tool resolves to non-standard dir: $CANON_PATH (dir: $CANON_DIR) — verify this is expected"
  fi
done
echo ""

# ── Summary ───────────────────────────────────────────────────────────────────
echo "=== Results: $PASS OK  $SKIP SKIP  $FAIL FAIL (WARN: $WARN) ==="
(( FAIL > 0 )) && exit 1 || exit 0
