#!/usr/bin/env bash
# mini-ork install script
# Idempotent — safe to re-run at any time.
# Usage: bash install.sh
set -Eeuo pipefail

MINI_ORK_REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MINI_ORK_BIN="$MINI_ORK_REPO/bin/mini-ork"

PASS=0; WARN=0; FAIL=0
_ok()   { echo "  [OK]   $*"; PASS=$((PASS+1)); }
_warn() { echo "  [WARN] $*"; WARN=$((WARN+1)); }
_fail() { echo "  [FAIL] $*"; FAIL=$((FAIL+1)); }

echo "=== mini-ork installer ==="
echo ""

# ── 1. Check required dependencies ───────────────────────────────────────────

echo "--- Checking dependencies ---"

MISSING_DEPS=()

_check_dep() {
  local name="$1"; local brew_name="${2:-$1}"; local apt_name="${3:-$1}"
  if command -v "$name" >/dev/null 2>&1; then
    _ok "$name found"
  else
    _warn "$name NOT found"
    echo "        Install:"
    echo "          macOS:  brew install $brew_name"
    echo "          Debian: sudo apt install $apt_name"
    MISSING_DEPS+=("$name")
  fi
}

# bash 4+ (macOS ships bash 3 — homebrew bash is required on Mac)
if (( BASH_VERSINFO[0] < 4 )); then
  _warn "bash 4+ required (you have $BASH_VERSION)"
  echo "        Install: brew install bash"
  MISSING_DEPS+=("bash4")
else
  _ok "bash $BASH_VERSION"
fi

_check_dep sqlite3 sqlite sqlite3
_check_dep jq     jq     jq
_check_dep git    git    git

# claude CLI — optional but needed for real delivery
if command -v claude >/dev/null 2>&1; then
  _ok "claude CLI found"
else
  _warn "claude CLI not found — you can install mini-ork now but 'mini-ork deliver' needs it"
  echo "        Install: npm install -g @anthropic-ai/claude-code"
fi

echo ""

# Abort if hard-required deps are missing
HARD_MISSING=0
for dep in sqlite3 jq git; do
  if [[ " ${MISSING_DEPS[*]:-} " == *" $dep "* ]]; then
    HARD_MISSING=1
  fi
done

if (( HARD_MISSING )); then
  echo "[ERROR] Hard-required dependencies missing: ${MISSING_DEPS[*]}"
  echo "        Install them then re-run: bash install.sh"
  exit 1
fi

# ── 2. Determine install target ───────────────────────────────────────────────

echo "--- Choosing install location ---"

LOCAL_BIN="$HOME/.local/bin"
SYSTEM_BIN="/usr/local/bin"
INSTALL_DIR=""

if [[ -w "$SYSTEM_BIN" ]]; then
  INSTALL_DIR="$SYSTEM_BIN"
  _ok "using $SYSTEM_BIN (writable)"
else
  mkdir -p "$LOCAL_BIN"
  INSTALL_DIR="$LOCAL_BIN"
  _ok "using $LOCAL_BIN (fallback — /usr/local/bin not writable)"

  # Remind user to add ~/.local/bin to PATH if not already present
  if [[ ":$PATH:" != *":$LOCAL_BIN:"* ]]; then
    _warn "$LOCAL_BIN is not in your PATH"
    echo "        Add to your shell profile (~/.zshrc / ~/.bashrc):"
    echo "          export PATH=\"\$HOME/.local/bin:\$PATH\""
  fi
fi

echo ""

# ── 3. Create / update symlink ────────────────────────────────────────────────

echo "--- Installing mini-ork binary ---"

TARGET="$INSTALL_DIR/mini-ork"

if [[ ! -f "$MINI_ORK_BIN" ]]; then
  _warn "bin/mini-ork not found at $MINI_ORK_BIN — skipping symlink"
  echo "        This is unexpected. Is the repo checkout complete?"
else
  # Remove stale symlink or binary at target (idempotent)
  if [[ -L "$TARGET" || -f "$TARGET" ]]; then
    rm -f "$TARGET"
  fi

  ln -s "$MINI_ORK_BIN" "$TARGET"
  chmod +x "$MINI_ORK_BIN"
  _ok "symlinked $MINI_ORK_BIN → $TARGET"
fi

# ── 4. Ensure mini-ork-init is executable ────────────────────────────────────

INIT_BIN="$MINI_ORK_REPO/bin/mini-ork-init"
if [[ -f "$INIT_BIN" ]]; then
  chmod +x "$INIT_BIN"
  _ok "chmod +x bin/mini-ork-init"
fi

# ── 5. Verify installation ────────────────────────────────────────────────────

echo ""
echo "--- Verification ---"

if command -v mini-ork >/dev/null 2>&1; then
  VER="$(mini-ork version 2>/dev/null || echo '(version unknown)')"
  _ok "mini-ork reachable: $VER"
elif [[ -f "$TARGET" ]]; then
  _warn "mini-ork installed to $TARGET but not yet in PATH (restart shell or source profile)"
else
  _warn "mini-ork not found in PATH — check $INSTALL_DIR is in PATH"
fi

echo ""

# ── Summary ───────────────────────────────────────────────────────────────────

if (( FAIL > 0 )); then
  echo "[ERROR] Installation had failures. Fix them and re-run."
  exit 1
elif (( WARN > 0 )); then
  echo "mini-ork installed with warnings — see above."
else
  echo "mini-ork installed."
fi

echo ""
echo "Next step: run 'mini-ork init' in your project."
echo ""
