#!/usr/bin/env bash
# mo-healer-bridge.sh — invoke healer.sh on ESCALATE, auto-apply
# low-risk recoveries within the mini-ork loop context.
#
# Self-healing closes the loop ONLY if it runs on every ESCALATE path.
# Mini-ork's `run.sh` wires mo-healer at its ESCALATE handler via this bridge.
#
# Function:
#   mo_run_healer_on_escalate <epic_id> <epic_run_dir>
#
# Behaviour:
#   1. Run healer.sh → get {recovery_action, lesson_id, matched}
#   2. Auto-apply LOW-RISK recoveries:
#        - cleaner-on-main: dispatch cleaner.sh against main worktree
#        - rebase-and-retry: git -C <worktree> rebase main; flip epic
#          back to PENDING so next loop iter re-dispatches
#        - wait-and-retry: sleep + flip to PENDING (rate-limit recovery)
#   3. Higher-risk recoveries (switch-agent / shrink-scope) emit a
#      brain hint via mo_event but leave epic at ESCALATE for the user.
#   4. Terminal recoveries (escalate-human / mark-wontfix / no-op)
#      keep epic at ESCALATE — same as before this bridge existed.
#
# Side effects:
#   - On successful recovery: caller should UN-set EPIC_STATUS to PENDING
#     so the outer loop re-tries. Return value signals this:
#       0  recovery applied, epic should be re-tried
#       1  no recovery / terminal — epic stays ESCALATE
#
# Disable globally: MO_HEALER_BRIDGE_DISABLED=1

set -uo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

# Caller exports: AGENTFLOW_DIR, WORKTREE, EPIC_STATUS, JOB_ID, JOB_RUN_DIR

mo_run_healer_on_escalate() {
  local epic="$1"
  local epic_run_dir="$2"

  # Escape hatch — disable entirely.
  if [ "${MO_HEALER_BRIDGE_DISABLED:-0}" = "1" ]; then
    return 1
  fi

  local healer="$MINI_ORK_ROOT/lib/healer.sh"
  if [ ! -x "$healer" ]; then
    echo "[mini-ork] mo-healer not executable at $healer — bridge skipped" >&2
    return 1
  fi

  echo "[mini-ork] mo-healer-bridge: classifying ESCALATE for $epic" >&2
  local healer_out
  healer_out=$(bash "$healer" "$epic" "$epic_run_dir" 2>/dev/null || echo "")
  if [ -z "$healer_out" ]; then
    echo "[mini-ork] mo-healer returned empty — no recovery" >&2
    return 1
  fi

  local recovery lesson_id matched
  recovery=$(echo "$healer_out" | jq -r '.recovery_action // ""' 2>/dev/null)
  lesson_id=$(echo "$healer_out" | jq -r '.lesson_id // "null"' 2>/dev/null)
  matched=$(echo "$healer_out" | jq -r '.matched // false' 2>/dev/null)

  echo "[mini-ork] healer -> recovery=$recovery lesson=$lesson_id matched=$matched" >&2

  # Emit observability event so this shows up in mo_events / dashboards.
  if type mo_emit >/dev/null 2>&1; then
    MO_EVENT_EPIC="$epic" \
    mo_emit "note" "mo-healer-bridge" \
      "$(printf '{"recovery_action":"%s","lesson_id":"%s","matched":%s}' \
            "$recovery" "$lesson_id" "$matched")" \
      "ok" "$epic_run_dir" "" >/dev/null 2>&1 || true
  fi

  local worktree="${WORKTREE[$epic]:-}"

  case "$recovery" in
    cleaner-on-main)
      _mo_bridge_apply_cleaner "$epic" "$epic_run_dir" "$lesson_id"
      return $?
      ;;
    rebase-and-retry)
      _mo_bridge_apply_rebase "$epic" "$worktree" "$lesson_id"
      return $?
      ;;
    wait-and-retry)
      _mo_bridge_apply_wait "$epic" "$lesson_id" "$healer_out"
      return $?
      ;;
    switch-agent|shrink-scope)
      # Higher-risk — these need scope-patterns / agents.yaml mutation,
      # which is out of scope for the mini-ork loop (the user reviews
      # the suggestion and applies on next dispatch). Emit a hint.
      echo "[mini-ork] healer suggests $recovery for $epic — brain hint emitted, no auto-apply" >&2
      return 1
      ;;
    escalate-human|mark-wontfix|no-op|"")
      return 1
      ;;
    *)
      echo "[mini-ork] mo-healer unknown recovery: '$recovery' — no auto-apply" >&2
      return 1
      ;;
  esac
}

# ── cleaner-on-main ─────────────────────────────────────────────────────
# Dispatch the cleaner against main so the next dispatch sees a clean
# baseline. On success, bump lesson success_count.
_mo_bridge_apply_cleaner() {
  local epic="$1" epic_run_dir="$2" lesson_id="$3"
  local cleaner="$MINI_ORK_ROOT/lib/cleaner.sh"
  if [ ! -x "$cleaner" ]; then
    echo "[mini-ork] cleaner.sh not executable — skip" >&2
    return 1
  fi

  local bridge_dir="$epic_run_dir/healer-cleaner"
  mkdir -p "$bridge_dir"
  jq -n --arg ep "$epic" --arg les "$lesson_id" \
    '{epic_id:$ep, classification:"baseline_rot", confidence:0.9, evidence:[],
      recommendation:"cleaner-on-main",
      cleaner_brief:"mo-healer-bridge recovery (lesson_id=\($les)). Restore main to a clean baseline so \($ep) can retry.",
      rationale:"mo-healer-bridge auto-recovery",
      source:"mo-healer-bridge",
      detected_at:(now|strftime("%Y-%m-%dT%H:%M:%SZ"))}' \
    > "$bridge_dir/detective.json"

  echo "[mini-ork] dispatching cleaner-on-main for $epic..." >&2
  local cleaner_rc=0
  bash "$cleaner" "$bridge_dir/detective.json" "$bridge_dir" 2>&1 | sed 's/^/      /' || cleaner_rc=$?

  if [ "$cleaner_rc" -eq 0 ]; then
    _mo_bridge_bump_lesson "$lesson_id" "success"
    echo "[mini-ork] cleaner-on-main succeeded for $epic — epic will retry" >&2
    return 0
  fi
  _mo_bridge_bump_lesson "$lesson_id" "failure"
  echo "[mini-ork] cleaner-on-main failed (rc=$cleaner_rc) for $epic" >&2
  return 1
}

# ── rebase-and-retry ────────────────────────────────────────────────────
_mo_bridge_apply_rebase() {
  local epic="$1" worktree="$2" lesson_id="$3"
  if [ -z "$worktree" ] || [ ! -d "$worktree" ]; then
    echo "[mini-ork] no worktree path for $epic — skip rebase" >&2
    return 1
  fi

  # Stash any uncommitted (worker may have left dirty WD).
  local stashed=0
  if ! git -C "$worktree" diff --quiet HEAD 2>/dev/null; then
    git -C "$worktree" stash push -m "mo-healer-bridge-rebase $epic $(date +%s)" >/dev/null 2>&1 \
      && stashed=1
  fi

  echo "[mini-ork] git rebase main in $worktree..." >&2
  local rebase_rc=0
  git -C "$worktree" rebase main 2>&1 | sed 's/^/      /' || rebase_rc=$?

  if [ "$rebase_rc" -ne 0 ]; then
    echo "[mini-ork] rebase produced conflicts — aborting + restoring stash" >&2
    git -C "$worktree" rebase --abort >/dev/null 2>&1 || true
    [ "$stashed" = "1" ] && git -C "$worktree" stash pop >/dev/null 2>&1 || true
    _mo_bridge_bump_lesson "$lesson_id" "failure"
    return 1
  fi

  [ "$stashed" = "1" ] && git -C "$worktree" stash pop >/dev/null 2>&1 || true
  _mo_bridge_bump_lesson "$lesson_id" "success"
  echo "[mini-ork] rebase succeeded for $epic — epic will retry" >&2
  return 0
}

# ── wait-and-retry ──────────────────────────────────────────────────────
_mo_bridge_apply_wait() {
  local epic="$1" lesson_id="$2" healer_out="$3"
  local wait_s
  wait_s=$(echo "$healer_out" | jq -r '.recovery_args.wait_s // 30' 2>/dev/null)
  # Clamp to [10s, 300s].
  case "$wait_s" in ''|*[!0-9]*) wait_s=30 ;; esac
  [ "$wait_s" -lt 10 ] && wait_s=10
  [ "$wait_s" -gt 300 ] && wait_s=300
  echo "[mini-ork] wait-and-retry for $epic: sleeping ${wait_s}s..." >&2
  sleep "$wait_s"
  _mo_bridge_bump_lesson "$lesson_id" "success"
  return 0
}

# ── lesson metrics ──────────────────────────────────────────────────────
_mo_bridge_bump_lesson() {
  local lesson_id="$1" kind="$2"
  [ "$lesson_id" = "null" ] || [ -z "$lesson_id" ] && return 0
  local helper="$MINI_ORK_ROOT/lib/memory-retrieve.sh"
  if [ ! -x "$helper" ]; then return 0; fi
  case "$kind" in
    success) bash "$helper" bump-success "$lesson_id" >/dev/null 2>&1 || true ;;
    failure) bash "$helper" bump-failure "$lesson_id" >/dev/null 2>&1 || true ;;
  esac
}

# Mark sourced.
export __MO_HEALER_BRIDGE_LOADED=1
