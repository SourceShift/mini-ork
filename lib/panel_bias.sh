#!/usr/bin/env bash
# panel_bias.sh — pure-mechanics panel-bias helpers (anonymize / rank / permute).
#
# Why this exists:
#   Adversarial panel review (panel-A) needs three independent controls
#   to keep lens-identity from leaking into the verdict:
#     (a) anonymize  → opaque response labels A/B/C/... so reviewers
#                     can't privilege a family by name (label bias).
#     (b) aggregate  → Borda sum + mean-rank to surface consensus
#                     winner from the now-anonymous rankings.
#     (c) permute    → randomized review order so position-of-
#                     presentation doesn't bias per-item scores.
#   These are STANDALONE mechanics — they take a directory in and emit
#   a directory + JSON file out, with no knowledge of mini-ork
#   recipes, gate libraries, or LLM dispatch. PANEL-b (out of scope
#   here) wires them into the panel-runner.
#
# Public API:
#   panel_anonymize <reports_dir> <out_dir> [seed=0]
#       Globs lens-*.md in reports_dir, sorts families deterministically
#       (LC_ALL=C), then shuffles via awk srand(seed). Assigns labels
#       A, B, C, ... in shuffled order. Copies each source to
#       <out_dir>/resp-<LABEL>.md with byte-for-byte content match.
#
#       label_map.json is written as a SIBLING file at:
#           ${out_dir}.label_map.json
#       (NEVER inside out_dir — keeps the de-anonymization key outside
#       the data plane so reviewers can't accidentally de-anonymize
#       by reading out_dir contents.)
#       JSON shape: { "A": "<family>", "B": "<family>", ... }
#
#   panel_rank_aggregate <xrank_dir> <label_map.json>
#       Reads each reviewer file in xrank_dir; extracts the
#       "^FINAL RANKING:" line; parses letter tokens. For each label:
#         borda     = Σ (N-1-p) where p=0-indexed position in a ranking
#                              of N items (best=N-1, last=0).
#         mean_rank = average 1-indexed position across reviewers.
#       De-anonymizes via `jq` lookup against label_map, emits
#       <xrank_dir>/panel-rank-aggregate.json as an array, sorted by
#       borda DESC, then mean_rank ASC, then family ASC.
#       JSON shape: [{"label":"A","family":"<f>","borda":<int>,
#                     "mean_rank":<float>}, ...]
#
#   panel_permute_order <reports_dir> [seed=0]
#       Lists *.md filenames in reports_dir (one level deep), shuffles
#       via seed, prints one filename per line to stdout.
#
# Conventions:
#   - All randomness is seed-deterministic via `awk 'BEGIN{srand(seed)}'`.
#     `$RANDOM` / `$SRANDOM` are FORBIDDEN — they re-seed from the
#     kernel each invocation, breaking same-seed-determinism.
#   - Default seed is 0 (fully deterministic) when unset.
#   - Input errors emit a message on stderr and return rc!=0.
#     No silent fallbacks: if the path is missing, fail loudly.
#   - label_map.json path is ${out_dir}.label_map.json (sibling).
#     Convention is documented in the function body and asserted
#     by tests/unit/test_panel_bias.sh.
#
# Requires: bash 4+, awk, find, sort, jq.

[ "${0:-}" = "${BASH_SOURCE[0]:-}" ] && set -Eeuo pipefail

# A..Z for label assignment (panel sizes are typically 3-7 families).
_PB_LABELS=(A B C D E F G H I J K L M N O P Q R S T U V W X Y Z)

# _pb_shuffle_lines <seed>: stdin shuffled to stdout by seed.
# Uses awk srand() — the only seedable RNG that survives across
# bash process boundaries. bash $RANDOM is not seedable.
# Width 20 + precision 18 gives stable zero-padded numerics so
# `sort -n` (lexicographic-as-numeric over the prefix) preserves
# the random-key ordering even when keys cluster.
_pb_shuffle_lines() {
  local seed="${1:-0}"
  awk -v seed="$seed" 'BEGIN{srand(seed)} {printf "%020.18f\t%s\n", rand(), $0}' \
    | LC_ALL=C sort -n \
    | cut -f2-
}

# _pb_extract_family  lens-<family>.md  →  <family>
# Multi-segment families (e.g., glm-4.5) preserved verbatim.
_pb_extract_family() {
  local f="${1#lens-}"
  printf '%s' "${f%.md}"
}

panel_anonymize() {
  local reports_dir="${1:?panel_anonymize: reports_dir required}"
  local out_dir="${2:?panel_anonymize: out_dir required}"
  local seed="${3:-0}"

  if [ ! -d "$reports_dir" ]; then
    echo "panel_anonymize: reports_dir not found: $reports_dir" >&2
    return 1
  fi
  if ! command -v jq >/dev/null 2>&1; then
    echo "panel_anonymize: jq not in PATH (required for label_map emission)" >&2
    return 1
  fi

  # Collect lens-*.md filenames, sorted deterministically (LC_ALL=C).
  local -a families=()
  local f
  while IFS= read -r f; do
    families+=("$f")
  done < <(LC_ALL=C ls -1 "$reports_dir" 2>/dev/null | grep -E '^lens-.*\.md$')

  if [ "${#families[@]}" -eq 0 ]; then
    echo "panel_anonymize: no lens-*.md in $reports_dir" >&2
    return 1
  fi

  # Shuffle by seed (single deterministic pipeline).
  local shuffled
  shuffled=$(printf '%s\n' "${families[@]}" | _pb_shuffle_lines "$seed")

  mkdir -p "$out_dir"
  local label_map="${out_dir}.label_map.json"

  # Build label_map.json incrementally as a pair of {label:family}
  # objects, then merge via `jq -s add`. Handles family names with
  # special chars (quotes etc.) safely.
  local tmp_pairs
  tmp_pairs=$(mktemp)

  local i=0
  local label family family_file
  while IFS= read -r family_file; do
    [ -z "$family_file" ] && continue
    if [ "$i" -ge "${#_PB_LABELS[@]}" ]; then
      echo "panel_anonymize: more than ${#_PB_LABELS[@]} families unsupported (got > ${i})" >&2
      rm -f "$tmp_pairs"
      return 1
    fi
    label="${_PB_LABELS[$i]}"
    family=$(_pb_extract_family "$family_file")
    if ! cp "$reports_dir/$family_file" "$out_dir/resp-${label}.md"; then
      echo "panel_anonymize: cp $reports_dir/$family_file -> $out_dir/resp-${label}.md failed" >&2
      rm -f "$tmp_pairs"
      return 1
    fi
    jq -n --arg l "$label" --arg f "$family" '{($l):$f}' >> "$tmp_pairs"
    i=$((i+1))
  done <<< "$shuffled"

  if ! jq -s 'add' "$tmp_pairs" > "$label_map"; then
    echo "panel_anonymize: jq merge of label pairs failed" >&2
    rm -f "$tmp_pairs"
    return 1
  fi
  rm -f "$tmp_pairs"
}

panel_rank_aggregate() {
  local xrank_dir="${1:?panel_rank_aggregate: xrank_dir required}"
  local label_map="${2:?panel_rank_aggregate: label_map required}"

  if [ ! -d "$xrank_dir" ]; then
    echo "panel_rank_aggregate: xrank_dir not found: $xrank_dir" >&2
    return 1
  fi
  if [ ! -f "$label_map" ]; then
    echo "panel_rank_aggregate: label_map not found: $label_map" >&2
    return 1
  fi
  if ! command -v jq >/dev/null 2>&1; then
    echo "panel_rank_aggregate: jq not in PATH" >&2
    return 1
  fi

  # Per-label accumulators (associative; default-to-zero on miss).
  declare -A label_borda=()
  declare -A label_pos_sum=()
  declare -A label_count=()

  local found_any=0
  local f ranking tok
  local -a tokens=()
  local N pos label

  for f in "$xrank_dir"/*; do
    [ -f "$f" ] || continue
    ranking=$(grep -h '^FINAL RANKING:' "$f" 2>/dev/null | head -n1 || true)
    [ -z "$ranking" ] && continue
    # Strip the "FINAL RANKING:" prefix; tokenize on whitespace.
    ranking="${ranking#FINAL RANKING:}"
    tokens=()
    for tok in $ranking; do
      tokens+=("$tok")
    done
    [ "${#tokens[@]}" -lt 1 ] && continue
    found_any=1
    N="${#tokens[@]}"
    pos=0
    for tok in "${tokens[@]}"; do
      label_borda[$tok]=$(( ${label_borda[$tok]:-0} + (N - 1 - pos) ))
      label_pos_sum[$tok]=$(( ${label_pos_sum[$tok]:-0} + (pos + 1) ))
      label_count[$tok]=$(( ${label_count[$tok]:-0} + 1 ))
      pos=$((pos + 1))
    done
  done

  if [ "$found_any" -eq 0 ]; then
    echo "panel_rank_aggregate: no reviewer file with '^FINAL RANKING:' line in $xrank_dir" >&2
    return 1
  fi

  local out_file="${xrank_dir}/panel-rank-aggregate.json"
  local tmp_entries
  tmp_entries=$(mktemp)

  for label in "${!label_borda[@]}"; do
    [ "${label_count[$label]:-0}" -gt 0 ] || continue
    local mean_rank family borda
    mean_rank=$(awk -v s="${label_pos_sum[$label]}" -v c="${label_count[$label]}" 'BEGIN{printf "%.4f", s/c}')
    family=$(jq -r --arg k "$label" '.[$k] // empty' "$label_map")
    [ -n "$family" ] || family="unknown"
    borda="${label_borda[$label]}"
    jq -n --arg l "$label" --arg f "$family" --argjson b "$borda" --argjson m "$mean_rank" \
      '{label:$l,family:$f,borda:$b,mean_rank:$m}' >> "$tmp_entries"
  done

  # Sort: borda DESC, mean_rank ASC, family ASC. jq's `sort_by`
  # sorts ascending, so negate borda for the DESC key.
  if ! jq -s 'sort_by(-.borda, .mean_rank, .family)' "$tmp_entries" > "$out_file"; then
    echo "panel_rank_aggregate: jq sort/emit failed" >&2
    rm -f "$tmp_entries"
    return 1
  fi
  rm -f "$tmp_entries"
}

panel_permute_order() {
  local reports_dir="${1:?panel_permute_order: reports_dir required}"
  local seed="${2:-0}"

  if [ ! -d "$reports_dir" ]; then
    echo "panel_permute_order: reports_dir not found: $reports_dir" >&2
    return 1
  fi

  # Shell glob is one-level (non-recursive) AND portable across BSD/GNU
  # find — `find -printf '%f\n'` is GNU-only. Filename basename via
  # parameter expansion (avoids forking basename).
  local f
  for f in "$reports_dir"/*.md; do
    [ -f "$f" ] || continue
    printf '%s\n' "${f##*/}"
  done | _pb_shuffle_lines "$seed"
}
