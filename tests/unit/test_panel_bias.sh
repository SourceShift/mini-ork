#!/usr/bin/env bash
# tests/unit/test_panel_bias.sh — unit tests for lib/panel_bias.sh
# Usage: bash tests/unit/test_panel_bias.sh
# Exit 0 = all assertions pass.  Exit 1 = any assertion failed.
#
# Covers three public helpers:
#   - panel_anonymize     : glob lens-*.md → byte-copy to resp-<LABEL>.md +
#                           write label_map.json as sibling of out_dir.
#                           Three families (collision prob between seeds:
#                           1/6 ≈ 17%; a single re-roll on a stable hit
#                           cheaps out the small collision risk).
#   - panel_rank_aggregate: Borda = Σ (N-1-p) over rankings, mean_rank
#                           = mean 1-indexed position. For 3 reviewers
#                           ('A C B', 'B A C', 'A B C') with N=3 this
#                           is hand-computed at the assertion site.
#   - panel_permute_order : same seed must yield identical order across
#                           invocations (awk srand is the only RNG that
#                           survives across process boundaries).
set -uo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
export MINI_ORK_ROOT
LIB="$MINI_ORK_ROOT/lib/panel_bias.sh"

PASS=0; FAIL=0; SKIP=0
_ok()   { echo "  [OK]   $*"; PASS=$((PASS+1)); }
_fail() { echo "  [FAIL] $*"; FAIL=$((FAIL+1)); }
_skip() { echo "  [SKIP] $*"; SKIP=$((SKIP+1)); }

if ! command -v jq >/dev/null 2>&1; then
  echo "── SKIPPED: jq not installed ──"
  echo "── Results: ${PASS} OK  ${SKIP} SKIP  ${FAIL} FAIL ──"
  exit 0
fi

echo "── unit: panel_bias.sh ──"

if [ ! -f "$LIB" ]; then
  _skip "lib/panel_bias.sh missing"
  echo ""
  echo "── Results: ${PASS} OK  ${SKIP} SKIP  ${FAIL} FAIL ──"
  exit 0
fi

TMP_DIR="$(mktemp -d /tmp/mo-panel-bias-test-XXXXXX)"
cleanup() { rm -rf "$TMP_DIR" /tmp/anon_err /tmp/rank_err /tmp/perm_err 2>/dev/null || true; }
trap cleanup EXIT

# shellcheck source=/dev/null
source "$LIB"

# ---------------------------------------------------------------------------
echo ""
echo "--- panel_anonymize: 3 families, seed=42 ---"
# ---------------------------------------------------------------------------
test_anonymize() {
  local R="$TMP_DIR/an_reports_42"
  local O="$TMP_DIR/an_out_42"
  mkdir -p "$R"
  printf 'GLM LENS OUTPUT\nverdict: ok\n'   > "$R/lens-glm.md"
  printf 'KIMI LENS OUTPUT\nverdict: ok\n'  > "$R/lens-kimi.md"
  printf 'OPUS LENS OUTPUT\nverdict: ok\n'  > "$R/lens-opus.md"

  if ! panel_anonymize "$R" "$O" 42 2>/tmp/anon_err; then
    _fail "panel_anonymize seed=42 returned non-zero: $(cat /tmp/anon_err)"
    return
  fi

  # 1. label_map.json sibling file exists at ${O}.label_map.json.
  if [ -f "$O.label_map.json" ]; then
    _ok "anonymize: label_map.json written as SIBLING of out_dir (\$O.label_map.json)"
  else
    _fail "anonymize: label_map.json not at sibling path $O.label_map.json"
    return
  fi
  if [ ! -f "$O/label_map.json" ]; then
    _ok "anonymize: label_map.json NOT inside out_dir (sibling convention honored)"
  else
    _fail "anonymize: label_map.json found inside out_dir — violates sibling convention"
  fi

  # 2. resp-{A,B,C}.md exist and match sources byte-for-byte.
  local all_match=1
  local label family resp
  for label in A B C; do
    resp="$O/resp-${label}.md"
    if [ ! -f "$resp" ]; then
      _fail "anonymize seed=42 missing resp-${label}.md"; all_match=0; continue
    fi
    family=$(jq -r --arg L "$label" '.[$L]' "$O.label_map.json")
    if ! cmp -s "$resp" "$R/lens-${family}.md"; then
      _fail "anonymize seed=42 resp-${label}.md (family=$family) not byte-equal to source"
      all_match=0
    fi
  done
  if [ "$all_match" -eq 1 ]; then
    _ok "anonymize: resp-{A,B,C}.md byte-match source via label_map round-trip"
  fi

  # 3. label_map keys are exactly A,B,C and values are drawn from {glm,kimi,opus}.
  local keys families
  keys=$(jq -r 'keys | sort | join(",")' "$O.label_map.json")
  families=$(jq -r '[.[]] | sort | join(",")' "$O.label_map.json")
  if [ "$keys" = "A,B,C" ]; then
    _ok "anonymize: label_map keys are exactly A,B,C"
  else
    _fail "anonymize: expected keys 'A,B,C', got '$keys'"
  fi
  if [ "$families" = "glm,kimi,opus" ]; then
    _ok "anonymize: label_map covers {glm,kimi,opus} exactly (no orphans)"
  else
    _fail "anonymize: expected families 'glm,kimi,opus', got '$families'"
  fi

  # 4. seed=99 mapping must differ from seed=42 (cheap collision retry once).
  local R2="$TMP_DIR/an_reports_99"
  mkdir -p "$R2"
  cp "$R"/*.md "$R2/"
  local O2="$TMP_DIR/an_out_99"
  if ! panel_anonymize "$R2" "$O2" 99 2>/tmp/anon_err; then
    _fail "panel_anonymize seed=99 returned non-zero: $(cat /tmp/anon_err)"
    return
  fi

  local map42 map99
  map42=$(jq -S '.' "$O.label_map.json")
  map99=$(jq -S '.' "$O2.label_map.json")
  local tries=0
  while [ "$map42" = "$map99" ] && [ "$tries" -lt 2 ]; do
    tries=$((tries + 1))
    O2="$TMP_DIR/an_out_99_$tries"
    if ! panel_anonymize "$R2" "$O2" $((99 + tries)) 2>/tmp/anon_err; then
      break
    fi
    map99=$(jq -S '.' "$O2.label_map.json")
  done

  if [ "$map42" = "$map99" ]; then
    _fail "anonymize: seed=42 and seed=$((99 + tries)) produced identical mapping (bad luck × $((tries+1)))"
  else
    _ok "anonymize: different seeds produced different mappings (re-rolls used: $tries)"
  fi
}
test_anonymize

# ---------------------------------------------------------------------------
echo ""
echo "--- panel_rank_aggregate: 3 reviewers, hand-computed Borda ---"
# ---------------------------------------------------------------------------
test_rank_aggregate() {
  local X="$TMP_DIR/rank_xrank"
  mkdir -p "$X"
  cat > "$X/r1.md" <<'EOF'
# Reviewer 1
some preamble...
FINAL RANKING: A C B
EOF
  cat > "$X/r2.md" <<'EOF'
# Reviewer 2
FINAL RANKING: B A C
EOF
  cat > "$X/r3.md" <<'EOF'
# Reviewer 3
FINAL RANKING: A B C
EOF
  local LM="$TMP_DIR/rank_label_map.json"
  printf '{"A":"glm","B":"kimi","C":"opus"}\n' > "$LM"

  if ! panel_rank_aggregate "$X" "$LM" 2>/tmp/rank_err; then
    _fail "panel_rank_aggregate returned non-zero: $(cat /tmp/rank_err)"
    return
  fi

  local out="$X/panel-rank-aggregate.json"
  [ -f "$out" ] || { _fail "rank_aggregate: output file missing at $out"; return; }

  # Hand-computed expectations for ('A C B', 'B A C', 'A B C'), N=3:
  #   Borda scale: p=0 → 2, p=1 → 1, p=2 → 0.
  #     A: r1=2 (p=0), r2=1 (p=1), r3=2 (p=0)        → Σ = 5
  #     B: r1=0 (p=2), r2=2 (p=0), r3=1 (p=1)        → Σ = 3
  #     C: r1=1 (p=1), r2=0 (p=2), r3=0 (p=2)        → Σ = 1
  #   mean_rank (1-indexed average position):
  #     A: (1+2+1)/3 = 1.3333
  #     B: (3+1+2)/3 = 2.0000
  #     C: (2+3+3)/3 = 2.6667
  local a_borda b_borda c_borda a_mr b_mr c_mr
  a_borda=$(jq '.[] | select(.label=="A") | .borda'     "$out")
  b_borda=$(jq '.[] | select(.label=="B") | .borda'     "$out")
  c_borda=$(jq '.[] | select(.label=="C") | .borda'     "$out")
  a_mr=$(jq    '.[] | select(.label=="A") | .mean_rank' "$out")
  b_mr=$(jq    '.[] | select(.label=="B") | .mean_rank' "$out")
  c_mr=$(jq    '.[] | select(.label=="C") | .mean_rank' "$out")

  if [ "$a_borda" = "5" ] && [ "$b_borda" = "3" ] && [ "$c_borda" = "1" ]; then
    _ok "rank_aggregate: Borda totals A=5 B=3 C=1 (N-1-p with N=3)"
  else
    _fail "rank_aggregate: Borda mismatch — got A=$a_borda B=$b_borda C=$c_borda (expected 5/3/1)"
  fi

  if [ "$a_mr" = "1.3333" ] && [ "$b_mr" = "2.0000" ] && [ "$c_mr" = "2.6667" ]; then
    _ok "rank_aggregate: mean_rank A=1.3333 B=2.0000 C=2.6667"
  else
    _fail "rank_aggregate: mean_rank mismatch — got A=$a_mr B=$b_mr C=$c_mr (expected 1.3333/2.0000/2.6667)"
  fi

  # Sort: borda DESC → A(5) > B(3) > C(1). Top must be glm.
  local top_family
  top_family=$(jq -r '.[0].family' "$out")
  if [ "$top_family" = "glm" ]; then
    _ok "rank_aggregate: top entry by borda DESC is glm (highest Borda=5)"
  else
    _fail "rank_aggregate: top family='$top_family', expected 'glm'"
  fi

  # Output sort order: glm > kimi > opus (Borda DESC).
  local order
  order=$(jq -r '[.[].family] | join(",")' "$out")
  if [ "$order" = "glm,kimi,opus" ]; then
    _ok "rank_aggregate: output order glm,kimi,opus (borda DESC)"
  else
    _fail "rank_aggregate: order='$order', expected 'glm,kimi,opus'"
  fi
}
test_rank_aggregate

# ---------------------------------------------------------------------------
echo ""
echo "--- panel_rank_aggregate: tie-break (Borda tie → family alphabetical) ---"
# ---------------------------------------------------------------------------
test_rank_aggregate_tiebreak() {
  local X="$TMP_DIR/rank_xrank_tb"
  mkdir -p "$X"
  # N=2 items, 2 reviewers with anti-symmetric ranking forces Borda
  # TIE between both items, AND mean_rank TIE (by linearity: borda_i =
  # R*(N-1) - Σpos, mean_rank = (Σpos)/R + 1). So secondary+tertiary keys
  # both reduce to family alphabetical.
  #   r1: 'A B' → A=1, B=0
  #   r2: 'B A' → B=1, A=0
  #   A: borda=1, mean_rank=1.5;  B: borda=1, mean_rank=1.5.  Tied.
  cat > "$X/r1.md" <<'EOF'
FINAL RANKING: A B
EOF
  cat > "$X/r2.md" <<'EOF'
FINAL RANKING: B A
EOF
  local LM="$TMP_DIR/rank_label_map_tb.json"
  # Note: ordering by family name (bravo first) to prove the sort is
  # alphabetical and NOT insertion-order.
  printf '{"A":"bravo","B":"alpha"}\n' > "$LM"

  if ! panel_rank_aggregate "$X" "$LM" 2>/tmp/rank_err; then
    _fail "rank_aggregate (tie-break) returned non-zero: $(cat /tmp/rank_err)"
    return
  fi

  local out="$X/panel-rank-aggregate.json"
  local order
  order=$(jq -r '[.[].family] | join(",")' "$out")
  if [ "$order" = "alpha,bravo" ]; then
    _ok "rank_aggregate: Borda+mean_rank tie broken by family alphabetical (alpha < bravo)"
  else
    _fail "rank_aggregate: tie-break order='$order', expected 'alpha,bravo'"
  fi
}
test_rank_aggregate_tiebreak

# ---------------------------------------------------------------------------
echo ""
echo "--- panel_permute_order: 5 files, seed determinism ---"
# ---------------------------------------------------------------------------
test_permute_order() {
  local P="$TMP_DIR/perm_reports"
  mkdir -p "$P"
  for f in alpha beta gamma delta epsilon; do
    : > "$P/${f}.md"
  done

  local ord1="$TMP_DIR/perm_order1.txt"
  local ord1_again="$TMP_DIR/perm_order1_again.txt"
  if ! panel_permute_order "$P" 1 > "$ord1" 2>/tmp/perm_err; then
    _fail "panel_permute_order seed=1 returned non-zero: $(cat /tmp/perm_err)"
    return
  fi
  if ! panel_permute_order "$P" 1 > "$ord1_again" 2>/tmp/perm_err; then
    _fail "panel_permute_order seed=1 second invocation returned non-zero"
    return
  fi

  if diff -q "$ord1" "$ord1_again" >/dev/null 2>&1; then
    _ok "permute_order: same seed (1) produced identical order across invocations"
  else
    _fail "permute_order: seed=1 NOT deterministic across invocations (awk srand broken?)"
  fi

  local ord2="$TMP_DIR/perm_order2.txt"
  if ! panel_permute_order "$P" 2 > "$ord2" 2>/tmp/perm_err; then
    _fail "panel_permute_order seed=2 returned non-zero"
    return
  fi

  if ! diff -q "$ord1" "$ord2" >/dev/null 2>&1; then
    _ok "permute_order: seed=1 vs seed=2 produced different orders"
  else
    _fail "permute_order: seed=1 and seed=2 produced identical orders (extremely unlikely)"
  fi

  # Output must be a permutation of the source filenames (preserves count + multiset).
  local src_list out_list
  src_list=$(printf '%s\n' alpha.md beta.md gamma.md delta.md epsilon.md | sort)
  out_list=$(sort "$ord1")
  if [ "$src_list" = "$out_list" ]; then
    _ok "permute_order: output is a permutation of source filenames"
  else
    _fail "permute_order: output is NOT a permutation of source filenames"
      _fail "  source: $src_list"
      _fail "  output: $out_list"
  fi
}
test_permute_order

echo ""
echo "── Results: ${PASS} OK  ${SKIP} SKIP  ${FAIL} FAIL ──"
[ "$FAIL" -eq 0 ] || exit 1
exit 0
