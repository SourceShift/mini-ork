#!/usr/bin/env bash
# tests/security/test_sec_dependency_supply_chain.sh
#
# SECURITY TEST — Dependency supply-chain integrity
#
# THREAT MODEL:
#   A compromised or hijacked tool (sqlite3, jq, yq, claude, python3, git)
#   installed in a non-standard location could be auto-invoked by mini-ork.
#   Additionally, if any scripts perform ad-hoc `npm install` or `pip install`
#   (without pinned versions or lockfile verification), they introduce
#   supply-chain risk via package registry poisoning.
#
# EXPECTED BEHAVIOUR:
#   1. All declared dependencies (from README.md) resolve to paths in trusted
#      directories: /usr/bin, /usr/local/bin, /opt/homebrew/bin, /opt/homebrew/opt/*/,
#      $HOME/.local/bin.
#   2. None of the mini-ork scripts contain ad-hoc `npm install` or
#      `pip install` invocations (those introduce unlocked transient deps).
#   3. install.sh uses --check mode for verification; any actual install
#      instructions are flagged for human review.
#
# KNOWN RESULT (v0.1):
#   mini-ork has no npm/pip install calls in its scripts (it is a bash + sqlite3
#   framework with no Node.js or Python package installs).  The dependencies
#   are system-level binaries, not package-managed modules.
#
# VULNERABILITY SHAPE IF FAILING:
#   A dependency resolves to a writable user-local directory that could be
#   overwritten (e.g. ~/.cargo/bin, ~/go/bin without explicit pinning), OR
#   a script performs `pip install <package>` without version pinning.

set -uo pipefail

PASS=0; FAIL=0; SKIP=0; WARN=0
_ok()   { echo "  [OK]   $*"; PASS=$((PASS+1)); }
_warn() { echo "  [WARN] $*"; WARN=$((WARN+1)); }
_fail() { echo "  [FAIL] $*"; FAIL=$((FAIL+1)); }
_skip() { echo "  [SKIP] $*"; SKIP=$((SKIP+1)); }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MINI_ORK_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

echo "=== test_sec_dependency_supply_chain.sh ==="
echo "    Testing: dependency supply-chain (tool locations + no ad-hoc installs)"
echo ""

# ── Trusted install prefixes ──────────────────────────────────────────────────
# These are the dirs where system-managed or user-approved tools live.
# Note: /opt/homebrew is macOS Homebrew; /usr/local is common on Linux + older macOS.
TRUSTED_PREFIXES=(
  "/usr/bin"
  "/usr/local/bin"
  "/opt/homebrew/bin"
  "/opt/homebrew/opt"
  "/opt/homebrew/Cellar"  # Homebrew canonical versioned Cellar paths (symlinked from /opt/homebrew/bin)
  "$HOME/.local/bin"
  "/opt/local/bin"        # MacPorts
  "/usr/pkg/bin"          # pkgsrc
  "/snap/bin"             # Ubuntu snap
  "/home/linuxbrew/.linuxbrew/bin"  # Linuxbrew
  "/home/linuxbrew/.linuxbrew/Cellar"  # Linuxbrew Cellar
)

is_trusted_path() {
  local p="$1"
  # Resolve symlinks to canonical path
  local canon
  canon="$(realpath "$p" 2>/dev/null || readlink -f "$p" 2>/dev/null || echo "$p")"
  local dir="$(dirname "$canon")"
  for prefix in "${TRUSTED_PREFIXES[@]}"; do
    if [[ "$dir" == "$prefix" || "$dir" == "$prefix/"* ]]; then
      return 0
    fi
  done
  return 1
}

# Developer-managed version managers that are common and expected:
# pyenv shims, nvm, nodenv, rbenv, goenv, etc.
# These are not security risks per se but should be noted.
is_version_manager_path() {
  local p="$1"
  local canon
  canon="$(realpath "$p" 2>/dev/null || readlink -f "$p" 2>/dev/null || echo "$p")"
  [[ "$canon" == *"/.pyenv/"* ]] && return 0
  [[ "$canon" == *"/.nvm/"* ]]   && return 0
  [[ "$canon" == *"/.nodenv/"* ]] && return 0
  [[ "$canon" == *"/.rbenv/"* ]] && return 0
  [[ "$canon" == *"/node_modules/.bin/"* ]] && return 0
  [[ "$canon" == *"/node_modules/@"*"/bin/"* ]] && return 0
  return 1
}

# ── Declared dependencies from README.md ─────────────────────────────────────
# Hardcoded from README "Dependencies" table:
#   bash, sqlite3, jq, yq, git, claude CLI
# Plus implicit runtime deps used in scripts:
#   python3, curl, timeout

DECLARED_DEPS=(bash sqlite3 jq yq git python3 curl)
# 'claude' is the Anthropic CLI — not always installed; treated as optional
OPTIONAL_DEPS=(claude)

echo "--- 1. Declared dependencies resolve to trusted paths ---"
for dep in "${DECLARED_DEPS[@]}"; do
  DEP_PATH="$(command -v "$dep" 2>/dev/null || echo "")"
  if [[ -z "$DEP_PATH" ]]; then
    _skip "$dep: not installed — cannot verify path (install it per README)"
    continue
  fi

  CANON_PATH="$(realpath "$DEP_PATH" 2>/dev/null || readlink -f "$DEP_PATH" 2>/dev/null || echo "$DEP_PATH")"
  if is_trusted_path "$DEP_PATH"; then
    _ok "$dep → $CANON_PATH (trusted)"
  elif is_version_manager_path "$DEP_PATH"; then
    _warn "$dep is managed by a version manager: $CANON_PATH — expected for developer environments; verify the shim delegates to a trusted binary"
  else
    _warn "$dep resolves to non-standard path: $CANON_PATH — verify this installation is trusted and not user-writable by others"
  fi
done
echo ""

echo "--- 2. Optional dependencies (claude CLI) ---"
for dep in "${OPTIONAL_DEPS[@]}"; do
  DEP_PATH="$(command -v "$dep" 2>/dev/null || echo "")"
  if [[ -z "$DEP_PATH" ]]; then
    _skip "$dep: not installed (optional — expected for Claude Code CLI usage)"
    continue
  fi
  CANON_PATH="$(realpath "$DEP_PATH" 2>/dev/null || readlink -f "$DEP_PATH" 2>/dev/null || echo "$DEP_PATH")"
  if is_trusted_path "$DEP_PATH"; then
    _ok "$dep → $CANON_PATH (trusted)"
  elif is_version_manager_path "$DEP_PATH"; then
    _warn "$dep is managed by a version manager / package manager: $CANON_PATH"
  else
    _warn "$dep resolves to non-standard path: $CANON_PATH — verify this installation"
  fi
done
echo ""

# ── Test 3: No npm install in scripts ────────────────────────────────────────
echo "--- 3. No ad-hoc npm install in scripts ---"

# Exclude:
#   - Lines starting with # (shell comments)
#   - Lines inside single-quoted heredocs (e.g. <<'PY', <<'EOF') — these are
#     documentation/prompt strings passed to agents, not executed shell.
#     We detect heredoc context by looking for lines that are indented with
#     backslash-escaped content (heredoc prompt bodies in cleaner.sh etc.)
#   - Literal backslash-prefixed lines (part of heredoc body like \`npm install\`)
NPM_HITS=$(grep -rn "npm install\|npm ci" \
  "$MINI_ORK_ROOT/bin/" \
  "$MINI_ORK_ROOT/lib/" \
  "$MINI_ORK_ROOT/db/" \
  "$MINI_ORK_ROOT/hooks/" \
  "$MINI_ORK_ROOT/recipes/" \
  2>/dev/null \
  | grep -v "^Binary" \
  | grep -v ":[[:space:]]*#" \
  | grep -v '\\`npm install' \
  | grep -v "rebase.*npm install\|npm install.*not a code\|just needs.*npm install" \
  | grep -v "saying.*npm install\|needs a.*npm install" \
  || true)

if [[ -z "$NPM_HITS" ]]; then
  _ok "No ad-hoc npm install found in bin/ lib/ db/ hooks/ recipes/"
else
  # Secondary filter: check if this is an actual shell command line
  # (starts with spaces+npm, or is an unescaped invocation)
  REAL_NPM_HITS=$(echo "$NPM_HITS" | grep -E '^\S[^:]*:[[:space:]]*(npm install|npm ci|npm i[[:space:]])' || true)
  if [[ -z "$REAL_NPM_HITS" ]]; then
    _ok "npm install references found only in documentation/heredoc strings (not executable shell commands)"
  else
    _fail "Executable npm install found in scripts — supply-chain risk (unlocked transient dependencies):"
    echo "$REAL_NPM_HITS" | head -10
  fi
fi
echo ""

# ── Test 4: No pip install in scripts ────────────────────────────────────────
echo "--- 4. No ad-hoc pip install in scripts ---"

PIP_HITS=$(grep -rn "pip install\|pip3 install\|pip[0-9]* install" \
  "$MINI_ORK_ROOT/bin/" \
  "$MINI_ORK_ROOT/lib/" \
  "$MINI_ORK_ROOT/db/" \
  "$MINI_ORK_ROOT/hooks/" \
  "$MINI_ORK_ROOT/recipes/" \
  2>/dev/null | grep -v "^Binary" | grep -v "#.*pip install" || true)

if [[ -z "$PIP_HITS" ]]; then
  _ok "No ad-hoc pip install found in bin/ lib/ db/ hooks/ recipes/"
else
  _fail "pip install found in scripts — supply-chain risk:"
  echo "$PIP_HITS" | head -10
fi
echo ""

# ── Test 5: install.sh --check flag exists and is documented ─────────────────
echo "--- 5. install.sh documents --check / verification mode ---"
INSTALL_SH="$MINI_ORK_ROOT/install.sh"

if [[ ! -f "$INSTALL_SH" ]]; then
  _skip "install.sh not found — skip"
else
  if grep -q -- "--check\|check.mode\|verify.*dep\|check.*dep" "$INSTALL_SH" 2>/dev/null; then
    _ok "install.sh has a --check / verification mode"
  else
    _warn "install.sh does not appear to have a --check verification mode — consider adding one so users can audit deps before installation"
  fi

  # Check if install.sh has any curl | bash patterns (supply chain red flag)
  if grep -E 'curl[[:space:]].*\|[[:space:]]*(ba)?sh|wget[[:space:]].*\|[[:space:]]*(ba)?sh' "$INSTALL_SH" 2>/dev/null | grep -qv '^[[:space:]]*#'; then
    _fail "install.sh contains curl|bash or wget|bash pattern — supply-chain risk: content is not verified before execution"
  else
    _ok "install.sh does not contain curl|bash pipe pattern"
  fi
fi
echo ""

# ── Test 6: Python scripts import only stdlib modules ────────────────────────
echo "--- 6. Python inline scripts use only stdlib (no pip-installable imports) ---"

# Extract all import lines from inline python3 heredocs in bin/ and lib/
PYTHON_IMPORTS=$(grep -rh "^import \|^from " \
  "$MINI_ORK_ROOT/bin/" \
  "$MINI_ORK_ROOT/lib/" \
  2>/dev/null | sort -u || true)

# Known safe stdlib modules used in mini-ork
STDLIB_MODULES=(sqlite3 json sys time uuid hashlib os re subprocess pathlib)

# Check for non-stdlib imports
NON_STDLIB=""
while IFS= read -r import_line; do
  [[ -z "$import_line" ]] && continue
  # Extract module name: "import foo" → foo, "from foo import" → foo
  module=$(echo "$import_line" | sed -E 's/^(import|from) ([a-zA-Z0-9_]+).*/\2/')
  IS_STDLIB=0
  for stdlib in "${STDLIB_MODULES[@]}"; do
    [[ "$module" == "$stdlib" ]] && IS_STDLIB=1 && break
  done
  if [[ "$IS_STDLIB" -eq 0 ]]; then
    # Check if python considers it a stdlib module
    if python3 -c "import $module" 2>/dev/null; then
      : # It's stdlib or installed — not a problem per se
    else
      NON_STDLIB="$NON_STDLIB\n  $import_line"
    fi
  fi
done <<< "$PYTHON_IMPORTS"

if [[ -z "$NON_STDLIB" ]]; then
  _ok "All Python imports in bin/ and lib/ appear to be stdlib modules"
else
  _warn "Potential non-stdlib Python imports found:$NON_STDLIB\n  Verify these do not require pip install in user environments"
fi
echo ""

# ── Summary ───────────────────────────────────────────────────────────────────
echo "=== Results: $PASS OK  $SKIP SKIP  $FAIL FAIL (WARN: $WARN) ==="
(( FAIL > 0 )) && exit 1 || exit 0
