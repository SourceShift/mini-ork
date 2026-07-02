#!/usr/bin/env bash
# Regression: cl_codex.sh must refuse to dispatch codex when its resolved cwd
# is a mini-ork FRAMEWORK tree instead of the consumer's target repo. A codex
# turn runs `git reset --hard refs/codex/curated-sync` inside its cwd — if
# that cwd is the framework's own source clone (or a vendored install),
# codex clobbers the framework repo's working tree rather than the intended
# project. This is silent and easy to miss until the framework repo's own
# history looks corrupted. The guard fails fast instead.
set -uo pipefail
ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
PASS=0; FAIL=0
ok(){ echo "  [OK]   $1"; PASS=$((PASS+1)); }
bad(){ echo "  [FAIL] $1"; FAIL=$((FAIL+1)); }

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT

echo "── unit: cl_codex.sh cwd guard (cross-repo corruption prevention) ──"

# --- stub codex CLI (only reached when the guard correctly lets a dispatch through) ---
cat > "$TMP/codex" <<'STUB'
#!/usr/bin/env bash
while [ $# -gt 0 ]; do
  case "$1" in
    --output-last-message) last_msg="$2"; shift 2 ;;
    *) shift ;;
  esac
done
printf '{"type":"thread.started","thread_id":"th-1"}\n'
printf '{"type":"item.completed","item":{"type":"agent_message","text":"OK"}}\n'
[ -n "${last_msg:-}" ] && printf 'OK' > "$last_msg"
exit 0
STUB
chmod +x "$TMP/codex"

run_wrapper(){ # $1=MO_TARGET_CWD  $2=MO_ALLOW_FRAMEWORK_CWD (optional)
  MO_TARGET_CWD="$1" MO_ALLOW_FRAMEWORK_CWD="${2:-}" \
    PATH="$TMP:$PATH" bash "$ROOT/lib/providers/cl_codex.sh" --print --output-format text "do the thing"
}

# ── Scenario A: target cwd IS the framework install root (has bin/mini-ork) ──
FRAMEWORK_ROOT="$TMP/fake-framework"
mkdir -p "$FRAMEWORK_ROOT/bin"
touch "$FRAMEWORK_ROOT/bin/mini-ork"
OUT_A="$(run_wrapper "$FRAMEWORK_ROOT" 2>"$TMP/a.err")"
RC_A=$?
[ "$RC_A" -eq 2 ] && ok "guard refuses dispatch (exit 2) when cwd is a framework install root (A)" \
  || bad "expected exit 2, got $RC_A (A): $(cat "$TMP/a.err")"
grep -qi "cwd guard FAILED" "$TMP/a.err" && ok "guard error message present (A)" || bad "guard error message missing (A): $(cat "$TMP/a.err")"

# ── Scenario B: target cwd is a path literally named .mini-ork (vendored install, no bin/mini-ork needed) ──
DOTDIR_ROOT="$TMP/some-project/.mini-ork"
mkdir -p "$DOTDIR_ROOT"
OUT_B="$(run_wrapper "$DOTDIR_ROOT" 2>"$TMP/b.err")"
RC_B=$?
[ "$RC_B" -eq 2 ] && ok "guard refuses dispatch for a .mini-ork-named path (B)" \
  || bad "expected exit 2, got $RC_B (B): $(cat "$TMP/b.err")"

# ── Scenario C: normal target repo — guard must NOT fire ─────────────────────
TARGET_REPO="$TMP/my-actual-project"
mkdir -p "$TARGET_REPO"
OUT_C="$(run_wrapper "$TARGET_REPO" 2>"$TMP/c.err")"
RC_C=$?
[ "$RC_C" -eq 0 ] && ok "guard does not block a normal target repo (C)" \
  || bad "unexpected non-zero exit $RC_C for a normal target repo (C): $(cat "$TMP/c.err")"
printf '%s' "$OUT_C" | grep -q "OK" && ok "dispatch proceeds normally for a normal target repo (C)" || bad "dispatch output missing for normal target repo (C)"

# ── Scenario D: MO_ALLOW_FRAMEWORK_CWD=1 overrides the guard for a genuine self-edit ──
OUT_D="$(run_wrapper "$FRAMEWORK_ROOT" 1 2>"$TMP/d.err")"
RC_D=$?
[ "$RC_D" -eq 0 ] && ok "MO_ALLOW_FRAMEWORK_CWD=1 overrides the guard (D)" \
  || bad "override did not bypass guard, rc=$RC_D (D): $(cat "$TMP/d.err")"

echo "── Results: $PASS OK  $FAIL FAIL ──"
[ "$FAIL" -eq 0 ]
