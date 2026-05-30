#!/usr/bin/env bash
# mini-ork Behavioral Contract — Phase A.4
# Implements a subset of the Agent Behavioral Contracts framework
# (arXiv 2602.22302) — formal specification + runtime enforcement of the
# per-epic contract C = (P, I, G, R):
#   Preconditions  — must hold BEFORE worker dispatches
#   Invariants     — must hold AFTER each worker iteration
#   Governance     — project-wide conventions
#   Recovery       — bounded retry with feedback (already implicit in iter loop)
#
# Each iter emits:
#   drift.json — count of invariant + governance violations
#
# Drift Bounds (paper §4.2, theorem): if recovery rate γ > drift α,
# expected drift D* = α/γ. Mini-ork's recovery rate = 1 - (failed iters /
# max iter). Drift α = avg invariant + governance violations per iter.
#
# Source from dispatch.sh.

set -uo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

# Caller exports: AGENTFLOW_DIR, REPO_ROOT, MINI_ORK_HOME

# ─── Preconditions (P) ─────────────────────────────────────────────────
# Args: epic worktree
# Returns 0 if all preconditions hold; non-zero on first failure.
# Writes: <run-dir>/contract-precond.json
mo_check_preconditions() {
  local epic="$1" worktree="$2"
  local run_dir
  run_dir="$(mo_run_dir "$epic")"
  mkdir -p "$run_dir"
  local report="$run_dir/contract-precond.json"
  local _db="${MINI_ORK_DB:-$AGENTFLOW_DIR/state.db}"
  local _scope_yaml="${AGENTFLOW_DIR:-$REPO_ROOT/${MINI_ORK_HOME:-.mini-ork}}/config/scope-patterns.yaml"

  local violations="[]"
  local pass=true

  # P1 — kickoff exists in state.db
  local kickoff_rel
  kickoff_rel=$(sqlite3 "$_db" \
    "SELECT kickoff_path FROM epics WHERE id='$epic';" 2>/dev/null)
  if [ -z "$kickoff_rel" ] || [ ! -f "$REPO_ROOT/$kickoff_rel" ]; then
    violations=$(echo "$violations" | jq -c \
      --arg id "P1" \
      --arg desc "kickoff missing or unreadable: $kickoff_rel" \
      '. + [{id: $id, severity: "error", description: $desc}]')
    pass=false
  fi

  # P2 — scope-patterns entry registered
  if [ -f "$_scope_yaml" ]; then
    if ! awk -v id="$epic" '
      /^epics:/ { in_e = 1; next }
      in_e && $0 ~ "^  " id ":" { found = 1; exit }
      END { exit (found ? 0 : 1) }
    ' "$_scope_yaml"; then
      violations=$(echo "$violations" | jq -c \
        --arg id "P2" \
        --arg desc "no scope-patterns.yaml entry for epic $epic" \
        '. + [{id: $id, severity: "error", description: $desc}]')
      pass=false
    fi
  fi

  # P3 — worktree exists and is a git checkout
  if [ ! -d "$worktree/.git" ] && ! git -C "$worktree" rev-parse --git-dir >/dev/null 2>&1; then
    violations=$(echo "$violations" | jq -c \
      --arg id "P3" \
      --arg desc "worktree not a git checkout: $worktree" \
      '. + [{id: $id, severity: "error", description: $desc}]')
    pass=false
  fi

  jq -n \
    --argjson pass "$pass" \
    --argjson violations "$violations" \
    --arg checked_at "$(date -u +%FT%TZ)" \
    '{pass: $pass, checked_at: $checked_at, violations: $violations}' \
    > "$report"

  $pass && return 0 || return 1
}

# ─── Invariants (I) ────────────────────────────────────────────────────
# Args: epic worktree iter
# Returns 0 if all invariants hold; non-zero on any failure.
# Writes: <iter-dir>/contract-invariants.json
mo_check_invariants() {
  local epic="$1" worktree="$2" iter="$3"
  local iter_dir="$(mo_run_dir "$epic")/iter-$iter"
  local report="$iter_dir/contract-invariants.json"
  local _scope_yaml="${AGENTFLOW_DIR:-$REPO_ROOT/${MINI_ORK_HOME:-.mini-ork}}/config/scope-patterns.yaml"
  mkdir -p "$iter_dir"

  local violations="[]"
  local pass=true

  # I1 — every commit on the worker's branch stays within scope-patterns.
  # Pull the patterns, build a regex, scan committed files since main.
  local patterns=""
  if [ -f "$_scope_yaml" ]; then
    patterns=$(awk -v id="$epic" '
      /^epics:/ { in_epics = 1; next }
      in_epics && $0 ~ "^  " id ":" { in_epic = 1; next }
      in_epic && /^    patterns:/ { in_pat = 1; next }
      in_pat && /^      - / {
        sub(/^      - /, ""); gsub(/^"|"$/, ""); print; next
      }
      in_pat && /^    [a-z]/ { in_pat = 0 }
      in_epic && /^  [A-Za-z]/ { in_epic = 0; in_pat = 0 }
    ' "$_scope_yaml")
  fi

  # Files changed on the branch vs main.
  local changed_files
  changed_files=$(git -C "$worktree" diff --name-only main..HEAD 2>/dev/null | sort -u)

  if [ -n "$changed_files" ] && [ -n "$patterns" ]; then
    local out_of_scope=()
    while IFS= read -r f; do
      [ -z "$f" ] && continue
      local matched=false
      while IFS= read -r pat; do
        [ -z "$pat" ] && continue
        # Convert glob to regex (simple ** → .*, * → [^/]*).
        local re
        re=$(printf '%s' "$pat" | sed -e 's|\.|\\.|g' -e 's|\*\*|::DOUBLESTAR::|g' -e 's|\*|[^/]*|g' -e 's|::DOUBLESTAR::|.*|g')
        if [[ "$f" =~ ^$re$ ]]; then matched=true; break; fi
      done <<< "$patterns"
      if ! $matched; then
        out_of_scope+=("$f")
      fi
    done <<< "$changed_files"

    if [ "${#out_of_scope[@]}" -gt 0 ]; then
      local list
      list=$(printf '%s, ' "${out_of_scope[@]}")
      violations=$(echo "$violations" | jq -c \
        --arg id "I1" \
        --arg desc "out-of-scope edits: ${list%, }" \
        '. + [{id: $id, severity: "error", description: $desc}]')
      pass=false
    fi
  fi

  # I2 — type-check green for the changed files only (cheap).
  local ts_files
  ts_files=$(echo "$changed_files" | grep -E '\.(ts|tsx)$' | head -5)
  if [ -n "$ts_files" ]; then
    if ! (cd "$worktree" && npx tsc --noEmit -p tsconfig.json 2>&1 | tail -3 | grep -qiE "error|errors"); then
      :  # tsc clean
    else
      # Run anyway to get exit code
      if ! (cd "$worktree" && npx tsc --noEmit -p tsconfig.json >/dev/null 2>&1); then
        violations=$(echo "$violations" | jq -c \
          --arg id "I2" \
          --arg desc "tsc --noEmit reported errors after worker iter" \
          '. + [{id: $id, severity: "warning", description: $desc}]')
        # Type errors are warning-only in invariant gate (existing reviewer
        # catches them as a blocker; we don't double-fail).
      fi
    fi
  fi

  jq -n \
    --argjson pass "$pass" \
    --argjson violations "$violations" \
    --arg checked_at "$(date -u +%FT%TZ)" \
    '{pass: $pass, iter: ($ARGS.named.iter | tonumber), checked_at: $checked_at, violations: $violations}' \
    --arg iter "$iter" \
    > "$report"

  $pass && return 0 || return 1
}

# ─── DELETE detector (MOX-X / P1.5) ─────────────────────────────────────
#
# Workers almost never need to delete files that existed at branch baseline.
# If they do, the spec must explicitly authorize it. Default behaviour:
# restore every deletion of a pre-existing file from the merge-base.
# Override per-epic by setting MO_ALLOW_SCOPE_DELETES=1.
#
# Runs BEFORE mo_revert_out_of_scope_files so the regular scope sweep
# sees the worker's intended ADD/MODIFY-only diff.
mo_restore_pre_existing_deletes() {
  [ "${MO_ALLOW_SCOPE_DELETES:-0}" -eq 1 ] && return 0
  [ "${MO_SKIP_SCOPE_REVERT:-0}" -eq 1 ] && return 0
  local epic="$1" worktree="$2" iter="$3"
  local iter_dir="$(mo_run_dir "$epic")/iter-$iter"
  mkdir -p "$iter_dir"
  local report="$iter_dir/scope-deletes.json"

  if declare -F mo_wait_for_worker_quiescence >/dev/null 2>&1; then
    if ! mo_wait_for_worker_quiescence "$(mo_run_dir "$epic")"; then
      echo "[mini-ork] scope-deletes epic=$epic iter=$iter — worker still active, proceeding" >&2
    fi
  fi

  local _baseline_sha
  _baseline_sha=$(git -C "$worktree" merge-base main HEAD 2>/dev/null || echo "main")

  # --diff-filter=D restricts to files DELETED on the branch.
  local deleted_files
  deleted_files=$(git -C "$worktree" diff --name-only --diff-filter=D "$_baseline_sha"..HEAD 2>/dev/null | sort -u)
  if [ -z "$deleted_files" ]; then
    printf '{"deletes_restored": [], "reason": "no deletes"}\n' > "$report"
    return 0
  fi

  local restored=()
  while IFS= read -r f; do
    [ -z "$f" ] && continue
    # Only restore if the file actually existed at baseline (defensive).
    if git -C "$worktree" cat-file -e "$_baseline_sha:$f" 2>/dev/null; then
      git -C "$worktree" checkout "$_baseline_sha" -- "$f" 2>/dev/null && restored+=("$f")
    fi
  done <<< "$deleted_files"

  if [ "${#restored[@]}" -gt 0 ]; then
    if [ -n "$(git -C "$worktree" status --porcelain)" ]; then
      git -C "$worktree" add -A 2>/dev/null
      git -C "$worktree" commit -m "chore(mini-ork): restore worker-deleted pre-existing files (${#restored[@]} files)" --no-verify 2>/dev/null || true
    fi
    echo "[mini-ork] scope-deletes epic=$epic iter=$iter — ${#restored[@]} file(s) restored from baseline (MO_ALLOW_SCOPE_DELETES=1 to override)" >&2
  fi

  printf '%s\n' "${restored[@]:-}" | jq -R -s -c 'split("\n") | map(select(length > 0)) | {deletes_restored: .}' > "$report"
  return 0
}

# ─── Scope auto-cleanup (Phase A.7) ────────────────────────────────────
# Reverts out-of-scope worker edits BEFORE reviewer fires.
# Disable: MO_SKIP_SCOPE_REVERT=1.
mo_revert_out_of_scope_files() {
  [ "${MO_SKIP_SCOPE_REVERT:-0}" -eq 1 ] && return 0
  local epic="$1" worktree="$2" iter="$3"
  local iter_dir="$(mo_run_dir "$epic")/iter-$iter"
  mkdir -p "$iter_dir"
  local report="$iter_dir/scope-revert.json"
  local _scope_yaml="${AGENTFLOW_DIR:-$REPO_ROOT/${MINI_ORK_HOME:-.mini-ork}}/config/scope-patterns.yaml"

  if declare -F mo_wait_for_worker_quiescence >/dev/null 2>&1; then
    if ! mo_wait_for_worker_quiescence "$(mo_run_dir "$epic")"; then
      echo "[mini-ork] scope-revert epic=$epic iter=$iter — worker still active, proceeding anyway (caller contract violation)" >&2
    fi
  fi

  # Read either `patterns` or `depends_on` for a single epic from
  # scope-patterns.yaml. Pass kind=patterns or kind=depends_on.
  _mo_read_epic_field() {
    local _id="$1" _kind="$2"
    [ -f "$_scope_yaml" ] || return 0
    awk -v id="$_id" -v kind="$_kind" '
      /^epics:/ { in_epics = 1; next }
      in_epics && $0 ~ "^  " id ":" { in_epic = 1; next }
      in_epic && $0 ~ "^    " kind ":" { in_field = 1; next }
      in_field && /^      - / { sub(/^      - /, ""); gsub(/^"|"$/, ""); print; next }
      in_field && /^    [a-z]/ { in_field = 0 }
      in_epic && /^  [A-Za-z]/ { in_epic = 0; in_field = 0 }
    ' "$_scope_yaml"
  }

  # Transitive scope walker. Builds the union of `epic`'s patterns and the
  # patterns of every epic in its `depends_on` graph.
  # Disable via MO_SCOPE_NO_TRANSITIVE_DEPS=1 to fall back to literal-only
  # patterns (legacy behaviour).
  _mo_collect_scope_patterns() {
    local _root="$1"
    local _seen_file _out_file
    _seen_file=$(mktemp)
    _out_file=$(mktemp)
    _mo_walk_epic() {
      local _id="$1"
      grep -qFx "$_id" "$_seen_file" 2>/dev/null && return 0
      echo "$_id" >> "$_seen_file"
      _mo_read_epic_field "$_id" patterns >> "$_out_file"
      if [ "${MO_SCOPE_NO_TRANSITIVE_DEPS:-0}" != "1" ]; then
        local _deps
        _deps=$(_mo_read_epic_field "$_id" depends_on)
        while IFS= read -r _d; do
          [ -z "$_d" ] && continue
          _mo_walk_epic "$_d"
        done <<< "$_deps"
      fi
    }
    _mo_walk_epic "$_root"
    sort -u "$_out_file"
    rm -f "$_seen_file" "$_out_file"
  }

  local patterns
  patterns=$(_mo_collect_scope_patterns "$epic")

  if [ -z "$patterns" ]; then
    printf '{"reverted": [], "reason": "no scope patterns"}\n' > "$report"
    return 0
  fi

  # Use merge-base as the diff baseline, NOT current main. When mid-run
  # rebase fails, the branch is stuck on its OLD base while main has advanced.
  local _baseline_sha
  _baseline_sha=$(git -C "$worktree" merge-base main HEAD 2>/dev/null || echo "main")

  local changed_files
  changed_files=$(git -C "$worktree" diff --name-only "$_baseline_sha"..HEAD 2>/dev/null | sort -u)
  [ -z "$changed_files" ] && { printf '{"reverted": []}\n' > "$report"; return 0; }

  local reverted=()
  while IFS= read -r f; do
    [ -z "$f" ] && continue
    local matched=false
    while IFS= read -r pat; do
      [ -z "$pat" ] && continue
      local re
      re=$(printf '%s' "$pat" | sed -e 's|\.|\\.|g' -e 's|\*\*|::DOUBLESTAR::|g' -e 's|\*|[^/]*|g' -e 's|::DOUBLESTAR::|.*|g')
      if [[ "$f" =~ ^$re$ ]]; then matched=true; break; fi
    done <<< "$patterns"
    if ! $matched; then
      if git -C "$worktree" cat-file -e "$_baseline_sha:$f" 2>/dev/null; then
        git -C "$worktree" checkout "$_baseline_sha" -- "$f" 2>/dev/null && reverted+=("$f")
      else
        rm -f "$worktree/$f" && reverted+=("$f (deleted, was new)")
      fi
    fi
  done <<< "$changed_files"

  if [ "${#reverted[@]}" -gt 0 ]; then
    if [ -n "$(git -C "$worktree" status --porcelain)" ]; then
      git -C "$worktree" add -A 2>/dev/null
      git -C "$worktree" commit -m "chore(mini-ork): auto-revert out-of-scope files (${#reverted[@]} files)" --no-verify 2>/dev/null || true
    fi
    echo "[mini-ork] scope-revert epic=$epic iter=$iter — ${#reverted[@]} file(s) reverted" >&2
  fi

  printf '%s\n' "${reverted[@]:-}" | jq -R -s -c 'split("\n") | map(select(length > 0)) | {reverted: .}' > "$report"
  return 0
}

# ─── --no-verify bypass detector (Phase A.7.5) ─────────────────────────
# Workers default to `git commit --no-verify` when pre-commit hooks fail.
# Bypassing masks env breaks. Detection: commit message or worker.log mentions --no-verify.
# Disable: MO_SKIP_NO_VERIFY_DETECT=1.
mo_check_no_verify_bypass() {
  [ "${MO_SKIP_NO_VERIFY_DETECT:-0}" = "1" ] && return 0
  local epic="$1" worktree="$2" iter="$3"
  local iter_dir="$(mo_run_dir "$epic")/iter-$iter"
  mkdir -p "$iter_dir"
  local report="$iter_dir/no-verify-bypass.json"
  local worker_log="$iter_dir/worker.log"
  local _inbox_dir="${AGENTFLOW_DIR:-$REPO_ROOT/${MINI_ORK_HOME:-.mini-ork}}/INBOX"

  local _baseline_sha
  _baseline_sha=$(git -C "$worktree" merge-base main HEAD 2>/dev/null || echo "main")

  # Signal 1: commit messages mentioning no-verify, excluding orch's own
  # auto-revert commits (which always use --no-verify by design).
  local cm_hits
  cm_hits=$(git -C "$worktree" log "$_baseline_sha"..HEAD \
    --grep="no.verify" --regexp-ignore-case \
    --format="%H|%s" 2>/dev/null \
    | grep -v "chore(mini-ork): auto-revert" || true)

  # Signal 2: worker.log contains literal `--no-verify` flag.
  local log_hit_count=0
  if [ -f "$worker_log" ]; then
    log_hit_count=$(grep -c -- "--no-verify" "$worker_log" 2>/dev/null || echo 0)
    log_hit_count=${log_hit_count//[^0-9]/}
    : "${log_hit_count:=0}"
  fi

  if [ -z "$cm_hits" ] && [ "$log_hit_count" -eq 0 ]; then
    printf '{"detected": false, "commits": [], "log_hits": 0}\n' > "$report"
    return 0
  fi

  local commits_json='[]'
  if [ -n "$cm_hits" ]; then
    commits_json=$(printf '%s\n' "$cm_hits" | jq -R -s -c '
      split("\n") | map(select(length > 0)) | map(split("|") | {sha: .[0], subject: .[1]})
    ')
  fi
  : "${commits_json:=[]}"

  local _mini_ork_home="${MINI_ORK_HOME:-.mini-ork}"
  printf '{"detected": true, "commits": %s, "log_hits": %d, "advisory": "Worker bypassed pre-commit hooks. If errors are baseline-rot (untouched files), file INBOX note at %s/INBOX/<EPIC>-baseline-rot-<ts>.md instead."}\n' \
    "$commits_json" "$log_hit_count" "$_mini_ork_home" > "$report"

  echo "[mini-ork] no-verify bypass epic=$epic iter=$iter — $(echo "$commits_json" | jq 'length') commit(s), $log_hit_count log hit(s)" >&2

  # Auto-emit INBOX note so healer + operator see it in normal scan paths.
  local ts
  ts=$(date -u +%Y%m%dT%H%M%SZ)
  mkdir -p "$_inbox_dir"
  local inbox_path="$_inbox_dir/${epic}-no-verify-bypass-${ts}.md"
  {
    echo "# Worker --no-verify bypass detected"
    echo
    echo "**Epic:** $epic"
    echo "**Iter:** $iter"
    echo "**Worktree:** $worktree"
    echo "**Detected:** $(date -u +%FT%TZ)"
    echo
    echo "## Signal"
    echo
    echo "- Commit-message hits: $(echo "$commits_json" | jq 'length')"
    echo "- worker.log --no-verify literal hits: $log_hit_count"
    if [ "$(echo "$commits_json" | jq 'length')" -gt 0 ]; then
      echo
      echo "## Commits"
      echo
      echo "$commits_json" | jq -r '.[] | "- `" + .sha[0:8] + "` " + .subject'
    fi
    echo
    echo "## Per scope-card protocol"
    echo
    echo "Workers must NOT bypass pre-commit hooks. The expected workflow when"
    echo "hooks fail on baseline rot:"
    echo
    echo "1. Verify error files are in untouched files (\`git diff --name-only main..HEAD\`)."
    echo "2. File a baseline-rot note: \`${_mini_ork_home}/INBOX/${epic}-baseline-rot-<ts>.md\`."
    echo "3. STOP. Operator / healer fixes env, then re-dispatches."
    echo
    echo "If errors are in worker-modified files, those are real — fix in code."
  } > "$inbox_path"
  echo "[mini-ork] INBOX note: $inbox_path" >&2
  return 0
}

# ─── Governance (G) ────────────────────────────────────────────────────
# Generic project-wide conventions. Static checks of the worker's diff.
# G1 — no .js files (project rule: pure TypeScript).
# G2 — no console.log in changed TS files.
# Args: epic worktree iter
mo_check_governance() {
  local epic="$1" worktree="$2" iter="$3"
  local iter_dir="$(mo_run_dir "$epic")/iter-$iter"
  local report="$iter_dir/contract-governance.json"

  local violations="[]"
  local pass=true

  local changed_files
  changed_files=$(git -C "$worktree" diff --name-only main..HEAD 2>/dev/null | sort -u)

  # G1 — no .js files (project rule: pure TypeScript).
  local js_files
  js_files=$(echo "$changed_files" | grep -E '\.js$' || true)
  if [ -n "$js_files" ]; then
    violations=$(echo "$violations" | jq -c \
      --arg id "G1" \
      --arg desc "new .js files violate TS-only rule: $(echo "$js_files" | tr '\n' ' ')" \
      '. + [{id: $id, severity: "error", description: $desc}]')
    pass=false
  fi

  # G2 — no console.log in changed TS files (project rule: use logger).
  local ts_changes
  ts_changes=$(echo "$changed_files" | grep -E '\.(ts|tsx)$' | grep -vE '^(e2e/)' || true)
  if [ -n "$ts_changes" ]; then
    local console_hits=0
    while IFS= read -r f; do
      [ -f "$worktree/$f" ] || continue
      if git -C "$worktree" diff main..HEAD -- "$f" 2>/dev/null | grep -E '^\+[^+].*console\.log' >/dev/null; then
        console_hits=$((console_hits + 1))
      fi
    done <<< "$ts_changes"
    if [ "$console_hits" -gt 0 ]; then
      violations=$(echo "$violations" | jq -c \
        --arg id "G2" \
        --arg desc "console.log added in $console_hits file(s); use a project logger" \
        '. + [{id: $id, severity: "error", description: $desc}]')
      pass=false
    fi
  fi

  jq -n \
    --argjson pass "$pass" \
    --argjson violations "$violations" \
    --arg checked_at "$(date -u +%FT%TZ)" \
    '{pass: $pass, checked_at: $checked_at, violations: $violations}' \
    > "$report"

  $pass && return 0 || return 1
}

# ─── Drift telemetry ────────────────────────────────────────────────────
# Computes per-iter drift score:
#   drift = (invariant_violations + governance_violations) / total_checks
# Recovery rate γ is computed by the orchestrator from final epic verdicts.
# Drift Bounds Theorem: D* = α/γ when γ > α.
mo_emit_drift_telemetry() {
  local epic="$1" iter="$2"
  local iter_dir="$(mo_run_dir "$epic")/iter-$iter"
  local out="$iter_dir/drift.json"

  local inv_violations=0 gov_violations=0
  if [ -f "$iter_dir/contract-invariants.json" ]; then
    inv_violations=$(jq -r '.violations | length' "$iter_dir/contract-invariants.json" 2>/dev/null || echo 0)
  fi
  if [ -f "$iter_dir/contract-governance.json" ]; then
    gov_violations=$(jq -r '.violations | length' "$iter_dir/contract-governance.json" 2>/dev/null || echo 0)
  fi

  # Total checks: I (2) + G (2) = 4 today; update if more added.
  local total_checks=4
  local violations=$((inv_violations + gov_violations))
  local drift
  drift=$(awk -v v="$violations" -v t="$total_checks" 'BEGIN{ printf "%.3f", v/t }')

  jq -n \
    --argjson drift "$drift" \
    --argjson inv "$inv_violations" \
    --argjson gov "$gov_violations" \
    --argjson total "$total_checks" \
    --arg epic "$epic" \
    --argjson iter "$iter" \
    --arg ts "$(date -u +%FT%TZ)" \
    '{
      epic: $epic,
      iter: $iter,
      drift: $drift,
      invariant_violations: $inv,
      governance_violations: $gov,
      total_checks: $total,
      ts: $ts
    }' > "$out"
  echo "[mini-ork] drift epic=$epic iter=$iter score=$drift inv=$inv_violations gov=$gov_violations" >&2
}
