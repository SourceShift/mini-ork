#!/usr/bin/env bash
# mini-ork scope-overlap detector — prevent shared-trunk collisions.
#
# Reads scope patterns from ${MINI_ORK_HOME}/config/scope-patterns.yaml,
# materializes globs via `git ls-files`, builds pairwise intersections,
# and classifies overlaps as shared-trunk (SERIALIZE) or epic-private
# (WARN only).
#
# Source from dispatch.sh; not meant to run alone.

set -uo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

# ─── shared-trunk denylist ──────────────────────────────────────────────
# Any overlap on these paths forces serialization when
# MO_SERIALIZE_ON_OVERLAP=1 (default).
mo_is_shared_trunk() {
  local file="$1"
  case "$file" in
    shared/types/*)
      return 0
      ;;
    server/routes/*)
      return 0
      ;;
    package*.json)
      return 0
      ;;
    tsconfig*.json)
      return 0
      ;;
    .gitignore)
      return 0
      ;;
    .mini-ork/config/*)
      return 0
      ;;
    server/migrations/*)
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}

# ─── YAML pattern extraction ────────────────────────────────────────────
# Args: epic_id yaml_path
# Returns: one pattern per line (empty if epic not found)
mo_get_epic_patterns() {
  local epic="$1" yaml="$2"
  awk -v epic="$epic" '
    BEGIN { in_epic=0; in_patterns=0 }
    $0 ~ "^  " epic ":" { in_epic=1; next }
    in_epic && /^  [A-Za-z0-9_-]+:/ { exit }
    in_epic && /^  default:/ { exit }
    in_epic && /^    patterns:/ { in_patterns=1; next }
    in_patterns && /^    [a-z]+:/ { in_patterns=0; next }
    in_patterns {
      gsub(/^      - "/, "")
      gsub(/"$/, "")
      print
    }
  ' "$yaml"
}

# ─── Symbol-level claim extraction (Specification Gap §4) ───────────────
# Optional schema extension to scope-patterns.yaml:
#
#   EPIC_X:
#     patterns: [...]
#     shared_trunk_symbols:
#       "shared/types/promptSettings.ts":
#         - PROMPT_KEYS
#         - PromptKey
#
# When BOTH epics declare disjoint symbol sets for a shared-trunk file
# they happen to share, the overlap is downgraded SERIALIZE → WARN.
#
# Citation: Specification Gap (arXiv:2603.24284) §4 — AST-based conflict
# detector achieves 97% precision when each agent's claimed-symbol set
# is provided pre-dispatch.
#
# Args: epic_id file_path yaml_path
# Returns: one symbol per line (empty if no claims declared)
mo_get_epic_symbols_for_file() {
  local epic="$1" file="$2" yaml="$3"
  awk -v epic="$epic" -v file="$file" '
    BEGIN { in_epic=0; in_symbols=0; in_file=0 }
    $0 ~ "^  " epic ":"        { in_epic=1; in_symbols=0; in_file=0; next }
    in_epic && /^  [A-Za-z0-9_-]+:/ { exit }
    in_epic && /^  default:/   { exit }
    in_epic && /^    shared_trunk_symbols:/ { in_symbols=1; in_file=0; next }
    in_symbols && /^    [a-z_]+:/ && !/^    shared_trunk_symbols:/ { in_symbols=0; in_file=0 }
    in_symbols && /^      "[^"]+":/ {
      match($0, /"[^"]+"/)
      cur_file=substr($0, RSTART+1, RLENGTH-2)
      if (cur_file == file) { in_file=1 } else { in_file=0 }
      next
    }
    in_file && /^        - / {
      gsub(/^        - "?/, "")
      gsub(/"?[[:space:]]*$/, "")
      print
    }
  ' "$yaml"
}

# Returns 0 if epic_a's claimed symbols ∩ epic_b's claimed symbols is empty
# for $file (safe to PARALLEL); 1 if intersection non-empty OR either side
# did not declare symbols (FALL BACK to serialize).
mo_symbols_disjoint_for_file() {
  local file="$1" epic_a="$2" epic_b="$3"
  local yaml="${AGENTFLOW_DIR:-${MINI_ORK_HOME:-.mini-ork}}/config/scope-patterns.yaml"
  [ -f "$yaml" ] || return 1

  local syms_a syms_b
  syms_a=$(mo_get_epic_symbols_for_file "$epic_a" "$file" "$yaml" | sort -u | grep -v '^$' || true)
  syms_b=$(mo_get_epic_symbols_for_file "$epic_b" "$file" "$yaml" | sort -u | grep -v '^$' || true)

  [ -z "$syms_a" ] && return 1
  [ -z "$syms_b" ] && return 1

  local common
  common=$(comm -12 <(printf '%s\n' "$syms_a") <(printf '%s\n' "$syms_b") | grep -v '^$' || true)
  [ -z "$common" ] && return 0
  return 1
}

# ─── Scope-overlap checker ──────────────────────────────────────────────
# Globals: EPICS (array), REPO_ROOT, AGENTFLOW_DIR, JOB_RUN_DIR
# Returns:
#   0 — no shared-trunk overlap (or MO_SERIALIZE_ON_OVERLAP=0)
#   1 — shared-trunk overlap detected AND MO_SERIALIZE_ON_OVERLAP=1
#
# Side effects:
#   Writes <JOB_RUN_DIR>/scope-overlap.json when overlap found.
#   Prints overlap warnings to stderr.
mo_check_scope_overlap() {
  local yaml="${AGENTFLOW_DIR:-${MINI_ORK_HOME:-.mini-ork}}/config/scope-patterns.yaml"
  if [ ! -f "$yaml" ]; then
    echo "[mini-ork] WARN: scope-patterns.yaml not found — skipping overlap check" >&2
    return 0
  fi

  local job_run_dir="${JOB_RUN_DIR:-}"
  if [ -z "$job_run_dir" ]; then
    echo "[mini-ork] WARN: JOB_RUN_DIR unset — skipping overlap check" >&2
    return 0
  fi

  local overlap_json="$job_run_dir/scope-overlap.json"

  # Build file sets per epic
  local -a epics_list=()
  local -A epic_files

  for epic in "${EPICS[@]}"; do
    epics_list+=("$epic")
    local patterns=""
    patterns=$(mo_get_epic_patterns "$epic" "$yaml")
    if [ -z "$patterns" ]; then
      epic_files["$epic"]=""
      continue
    fi

    local files=""
    while IFS= read -r pat; do
      [ -z "$pat" ] && continue
      local matched=""
      matched=$(git -C "$REPO_ROOT" ls-files -- ":(glob)$pat" 2>/dev/null)
      if [ -n "$matched" ]; then
        if [ -z "$files" ]; then
          files="$matched"
        else
          files="$files"$'\n'"$matched"
        fi
      fi
    done <<< "$patterns"

    files=$(printf '%s\n' "$files" | sort -u | grep -v '^$')
    epic_files["$epic"]="$files"
  done

  # Pairwise intersection
  local -a overlap_pairs=()
  local count=${#epics_list[@]}

  for ((i = 0; i < count; i++)); do
    for ((j = i + 1; j < count; j++)); do
      local a="${epics_list[$i]}"
      local b="${epics_list[$j]}"
      local files_a="${epic_files[$a]:-}"
      local files_b="${epic_files[$b]:-}"

      [ -z "$files_a" ] || [ -z "$files_b" ] && continue

      local common=""
      common=$(comm -12 <(printf '%s\n' "$files_a") <(printf '%s\n' "$files_b") | grep -v '^$')
      [ -z "$common" ] && continue

      local shared_trunk_files=""
      local private_files=""
      local downgraded_files=""
      while IFS= read -r f; do
        [ -z "$f" ] && continue
        if mo_is_shared_trunk "$f"; then
          if mo_symbols_disjoint_for_file "$f" "$a" "$b"; then
            if [ -z "$downgraded_files" ]; then
              downgraded_files="$f"
            else
              downgraded_files="$downgraded_files"$'\n'"$f"
            fi
          else
            if [ -z "$shared_trunk_files" ]; then
              shared_trunk_files="$f"
            else
              shared_trunk_files="$shared_trunk_files"$'\n'"$f"
            fi
          fi
        else
          if [ -z "$private_files" ]; then
            private_files="$f"
          else
            private_files="$private_files"$'\n'"$f"
          fi
        fi
      done <<< "$common"

      if [ -n "$downgraded_files" ]; then
        local dg_count=""
        dg_count=$(printf '%s\n' "$downgraded_files" | grep -c '.')
        echo "[mini-ork] SCOPE OVERLAP (symbol-disjoint downgrade): $a ↔ $b — $dg_count file(s) [WARN, declared symbols disjoint]" >&2
      fi

      if [ -n "$shared_trunk_files" ]; then
        local file_count=""
        file_count=$(printf '%s\n' "$shared_trunk_files" | grep -c '.')
        local files_json=""
        files_json=$(printf '%s\n' "$shared_trunk_files" | sed 's/"/\\"/g;s/^/"/;s/$/"/' | paste -sd ',' -)
        overlap_pairs+=("{\"a\":\"$a\",\"b\":\"$b\",\"files\":[$files_json]}")
        echo "[mini-ork] SCOPE OVERLAP (shared-trunk): $a ↔ $b — $file_count file(s)" >&2
      elif [ -n "$private_files" ]; then
        local file_count=""
        file_count=$(printf '%s\n' "$private_files" | grep -c '.')
        echo "[mini-ork] SCOPE OVERLAP (epic-private): $a ↔ $b — $file_count file(s) [WARN, proceeding parallel]" >&2
      fi
    done
  done

  if [ ${#overlap_pairs[@]} -gt 0 ]; then
    local pairs_json=""
    pairs_json=$(printf '%s\n' "${overlap_pairs[@]}" | paste -sd ',' -)

    # ── Partition graph (OptiMA-style transaction partitioning) ────────
    # Build connected-components on the conflict graph so independent
    # epic-groups can run in parallel even when one pair conflicts.
    # Citation: OptiMA (arXiv:2511.03761) §3 transaction partitioning.
    local partitions_json="[]"
    partitions_json=$(_mo_compute_partitions "$pairs_json" "${epics_list[@]}")

    printf '%s\n' "{\"pairs\":[$pairs_json],\"partitions\":$partitions_json}" > "$overlap_json"

    local partition_count="" total_epics=""
    partition_count=$(echo "$partitions_json" | jq -r 'length')
    total_epics="${#epics_list[@]}"
    echo "[mini-ork] SERIALIZE recommendation: shared-trunk overlap → $partition_count partition(s) across $total_epics epic(s) (see $overlap_json)" >&2

    if [ "${MO_SERIALIZE_ON_OVERLAP:-1}" -eq 1 ]; then
      return 1
    else
      echo "[mini-ork] MO_SERIALIZE_ON_OVERLAP=0 — overlap logged, serialization bypassed" >&2
      return 0
    fi
  fi

  rm -f "$overlap_json"
  return 0
}

# ─── Partition graph helper ─────────────────────────────────────────────
# Compute connected components of the conflict graph via union-find.
# Args:
#   $1 — pairs_json (JSON array element string, no surrounding [])
#   $2..$N — all epic ids (as separate args)
# Returns: JSON array of partitions, e.g. [["X","Y","Z"],["W"]]
_mo_compute_partitions() {
  local pairs_json="$1"; shift
  local -a all_epics=("$@")

  local -A parent
  local epic
  for epic in "${all_epics[@]}"; do
    parent["$epic"]="$epic"
  done

  _mo_uf_find() {
    local x="$1"
    while [ "${parent[$x]}" != "$x" ]; do
      parent[$x]="${parent[${parent[$x]}]}"
      x="${parent[$x]}"
    done
    echo "$x"
  }

  _mo_uf_union() {
    local a="$1" b="$2"
    local ra rb
    ra=$(_mo_uf_find "$a")
    rb=$(_mo_uf_find "$b")
    [ "$ra" = "$rb" ] && return
    parent[$ra]="$rb"
  }

  if [ -n "$pairs_json" ]; then
    while IFS=$'\t' read -r a b; do
      [ -z "$a" ] || [ -z "$b" ] && continue
      _mo_uf_union "$a" "$b"
    done < <(echo "[$pairs_json]" | jq -r '.[] | [.a,.b] | @tsv' 2>/dev/null)
  fi

  local -A groups
  for epic in "${all_epics[@]}"; do
    local root=""
    root=$(_mo_uf_find "$epic")
    if [ -z "${groups[$root]:-}" ]; then
      groups[$root]="$epic"
    else
      groups[$root]="${groups[$root]}|$epic"
    fi
  done

  local first=1
  printf '['
  local k
  for k in $(printf '%s\n' "${!groups[@]}" | sort); do
    if [ "$first" -eq 1 ]; then first=0; else printf ','; fi
    printf '%s' "${groups[$k]}" | tr '|' '\n' | jq -R -s -c 'split("\n") | map(select(length>0)) | sort'
  done
  printf ']\n'
}

# ─── Public read-side accessor ─────────────────────────────────────────
# Returns the partition count from the most-recent scope-overlap.json,
# or 1 if no file / no overlap.
mo_partition_count() {
  local job_run_dir="${JOB_RUN_DIR:-}"
  [ -z "$job_run_dir" ] && { echo 1; return; }
  local overlap_json="$job_run_dir/scope-overlap.json"
  [ ! -f "$overlap_json" ] && { echo 1; return; }
  local n=""
  n=$(jq -r '.partitions | length // 1' "$overlap_json" 2>/dev/null)
  if [ -z "$n" ] || ! [[ "$n" =~ ^[0-9]+$ ]]; then n=1; fi
  [ "$n" -lt 1 ] && n=1
  echo "$n"
}
