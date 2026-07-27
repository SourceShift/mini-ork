#!/usr/bin/env bash
# tests/unit/test_typecheck_detect.sh — gate tsc on a project marker, not global presence.
#
# Regression: on bash/Python repos with a globally-installed tsc, the verifier
# short-circuited on bare `tsc --noEmit`, printing the --help banner and exiting
# non-zero → verifier reported pass:false on every code_fix run. See kickoff
# kickoffs/issue-fixes/typecheck-detect-project-marker.md.
#
# Usage: bash tests/unit/test_typecheck_detect.sh
set -uo pipefail

MINI_ORK_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TYPECHECK="$MINI_ORK_ROOT/recipes/code-fix/verifiers/typecheck.py"

PASS=0; FAIL=0
_ok()   { echo "  [OK]   $*"; PASS=$((PASS+1)); }
_fail() { echo "  [FAIL] $*"; FAIL=$((FAIL+1)); }

# Build a fake tsc in $bindir that records its invocation to $sentinel and exits 0.
_make_fake_tsc() {
  local bindir="$1" sentinel="$2"
  mkdir -p "$bindir"
  cat > "$bindir/tsc" <<EOF
#!/usr/bin/env bash
echo "fake-tsc-invoked: \$*" >> "$sentinel"
exit 0
EOF
  chmod +x "$bindir/tsc"
}

# Build a fake mypy that records its invocation to $sentinel and exits 0.
_make_fake_mypy() {
  local bindir="$1" sentinel="$2"
  mkdir -p "$bindir"
  cat > "$bindir/mypy" <<EOF
#!/usr/bin/env bash
echo "fake-mypy-invoked: \$*" >> "$sentinel"
exit 0
EOF
  chmod +x "$bindir/mypy"
}

# Minimal PATH: fake bin + system bins. Strips mypy/cargo/go/pyenv shims so the
# test is hermetic — only what we set up in the tmp cwd is visible to the
# verifier's command -v probes.
MIN_PATH="/usr/bin:/bin"

echo "== typecheck.py: project-marker gating =="

# (a) empty tmp + fake tsc on PATH → skip(pass), tsc NOT invoked
T1=$(mktemp -d)
SENT1="$T1/sentinel"
B1="$T1/bin"
_make_fake_tsc "$B1" "$SENT1"
OUT1=$(cd "$T1" && PATH="$B1:$MIN_PATH" MINI_ORK_HOME="$T1/.mini-ork" \
       MINI_ORK_RUN_ID="t1-$$" python3 "$TYPECHECK" 2>&1) || true
RC1=$?
if [ "$RC1" -eq 0 ] \
   && echo "$OUT1" | grep -q '"pass":true' \
   && echo "$OUT1" | grep -q 'no typecheck tool detected' \
   && [ ! -f "$SENT1" ]; then
  _ok "(a) empty tmp + fake tsc → skip(pass), tsc NOT invoked"
else
  _fail "(a): rc=$RC1, sent_exists=$([ -f "$SENT1" ] && echo y || echo n), out=$OUT1"
fi
rm -rf "$T1"

# (b) tsconfig.json + fake tsc on PATH → tsc selected and invoked
T2=$(mktemp -d)
SENT2="$T2/sentinel"
echo '{}' > "$T2/tsconfig.json"
B2="$T2/bin"
_make_fake_tsc "$B2" "$SENT2"
OUT2=$(cd "$T2" && PATH="$B2:$MIN_PATH" MINI_ORK_HOME="$T2/.mini-ork" \
       MINI_ORK_RUN_ID="t2-$$" python3 "$TYPECHECK" 2>&1) || true
RC2=$?
if [ "$RC2" -eq 0 ] \
   && [ -f "$SENT2" ] \
   && grep -q "fake-tsc-invoked" "$SENT2"; then
  _ok "(b) tsconfig.json + fake tsc → tsc selected and invoked"
else
  _fail "(b): rc=$RC2, sent_exists=$([ -f "$SENT2" ] && echo y || echo n), sent=$(cat $SENT2 2>/dev/null), out=$OUT2"
fi
rm -rf "$T2"

# (c) MINI_ORK_TYPECHECK_CMD override wins regardless of markers
T3=$(mktemp -d)
SENT3="$T3/sentinel"
B3="$T3/bin"
_make_fake_tsc "$B3" "$SENT3"
# Override writes to sentinel via shell expansion inside the verifier's eval.
OUT3=$(cd "$T3" && PATH="$B3:$MIN_PATH" MINI_ORK_HOME="$T3/.mini-ork" \
       MINI_ORK_RUN_ID="t3-$$" \
       MINI_ORK_TYPECHECK_CMD="echo override-ran > $SENT3" \
       python3 "$TYPECHECK" 2>&1) || true
RC3=$?
if [ "$RC3" -eq 0 ] \
   && [ -f "$SENT3" ] \
   && grep -q "override-ran" "$SENT3"; then
  _ok "(c) override → MINI_ORK_TYPECHECK_CMD runs regardless of markers"
else
  _fail "(c): rc=$RC3, sent_exists=$([ -f "$SENT3" ] && echo y || echo n), sent=$(cat $SENT3 2>/dev/null), out=$OUT3"
fi
rm -rf "$T3"

# (d) pyproject.toml only + fake tsc on PATH (mypy absent via minimal PATH) → skip(pass), not tsc
T4=$(mktemp -d)
SENT4="$T4/sentinel"
printf '[project]\nname = "x"\n' > "$T4/pyproject.toml"
B4="$T4/bin"
_make_fake_tsc "$B4" "$SENT4"
OUT4=$(cd "$T4" && PATH="$B4:$MIN_PATH" MINI_ORK_HOME="$T4/.mini-ork" \
       MINI_ORK_RUN_ID="t4-$$" python3 "$TYPECHECK" 2>&1) || true
RC4=$?
if [ "$RC4" -eq 0 ] \
   && echo "$OUT4" | grep -q '"pass":true' \
   && [ ! -f "$SENT4" ]; then
  _ok "(d) pyproject.toml + fake tsc (mypy absent) → skip(pass), tsc NOT invoked"
else
  _fail "(d): rc=$RC4, sent_exists=$([ -f "$SENT4" ] && echo y || echo n), out=$OUT4"
fi
rm -rf "$T4"

# (e) pyproject.toml WITHOUT [tool.mypy] + fake mypy on PATH → skip(pass), mypy NOT invoked
T5=$(mktemp -d)
SENT5="$T5/sentinel"
printf '[project]\nname = "x"\n' > "$T5/pyproject.toml"
B5="$T5/bin"
_make_fake_mypy "$B5" "$SENT5"
OUT5=$(cd "$T5" && PATH="$B5:$MIN_PATH" MINI_ORK_HOME="$T5/.mini-ork" \
       MINI_ORK_RUN_ID="t5-$$" python3 "$TYPECHECK" 2>&1) || true
RC5=$?
if [ "$RC5" -eq 0 ] \
   && echo "$OUT5" | grep -q '"pass":true' \
   && [ ! -f "$SENT5" ]; then
  _ok "(e) pyproject.toml w/o [tool.mypy] + fake mypy → skip(pass), mypy NOT invoked"
else
  _fail "(e): rc=$RC5, sent_exists=$([ -f "$SENT5" ] && echo y || echo n), out=$OUT5"
fi
rm -rf "$T5"

# (f) pyproject.toml WITH [tool.mypy] + fake mypy → mypy selected and invoked
T6=$(mktemp -d)
SENT6="$T6/sentinel"
printf '[project]\nname = "x"\n\n[tool.mypy]\nstrict = true\n' > "$T6/pyproject.toml"
B6="$T6/bin"
_make_fake_mypy "$B6" "$SENT6"
OUT6=$(cd "$T6" && PATH="$B6:$MIN_PATH" MINI_ORK_HOME="$T6/.mini-ork" \
       MINI_ORK_RUN_ID="t6-$$" python3 "$TYPECHECK" 2>&1) || true
RC6=$?
if [ "$RC6" -eq 0 ] \
   && [ -f "$SENT6" ] \
   && grep -q "fake-mypy-invoked" "$SENT6"; then
  _ok "(f) pyproject.toml w/ [tool.mypy] + fake mypy → mypy selected and invoked"
else
  _fail "(f): rc=$RC6, sent_exists=$([ -f "$SENT6" ] && echo y || echo n), sent=$(cat $SENT6 2>/dev/null), out=$OUT6"
fi
rm -rf "$T6"

echo
echo "PASS=$PASS FAIL=$FAIL"
[ "$FAIL" -eq 0 ]
