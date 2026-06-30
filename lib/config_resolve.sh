#!/usr/bin/env bash
# config_resolve.sh — per-run config isolation (roadmap T1.0).
#
# Problem: concurrent mini-ork runs cannot run safely because the lane policy
# (config/agents.yaml) is read LIVE, per-dispatch, by the dispatch resolvers
# (lib/decision_service.sh, lib/lane-helpers.sh). Editing the global
# agents.yaml while a run is in flight mutates that run's routing on its next
# node — so two parallel runs (or one run + an operator editing lanes) collide.
#
# Fix: a run FREEZES its effective lane policy into its run-dir at launch
# (mo_snapshot_run_config), and every dispatch-time resolver reads run-dir-FIRST
# (mo_resolve_agents_yaml). Consequences:
#   - editing the global agents.yaml never perturbs an in-flight run;
#   - concurrent runs hold independent frozen policies;
#   - a run is reproducible — its lane policy travels with its run-dir.
#
# This is the first concrete slice of the actor-per-run isolation epic and is a
# design pattern (not a band-aid): it survives the Bash→Python migration.
#
# Not meant to run alone — source from the bins + dispatch libs.

# mo_resolve_agents_yaml — echo the EFFECTIVE agents.yaml path, run-dir first.
# Precedence: $MINI_ORK_RUN_DIR/config → $MINI_ORK_HOME/config → $MINI_ORK_ROOT/config.
# Always echoes a path (callers still `[ -f ]`-guard the "not configured" case).
mo_resolve_agents_yaml() {
  if [ -n "${MINI_ORK_RUN_DIR:-}" ] && [ -f "${MINI_ORK_RUN_DIR}/config/agents.yaml" ]; then
    printf '%s\n' "${MINI_ORK_RUN_DIR}/config/agents.yaml"
    return 0
  fi
  local p="${MINI_ORK_HOME:-.mini-ork}/config/agents.yaml"
  [ -f "$p" ] || p="${MINI_ORK_ROOT:-.}/config/agents.yaml"
  printf '%s\n' "$p"
}

# mo_snapshot_run_config <run_dir> — freeze the effective agents.yaml into the
# run-dir ONCE, at launch. Idempotent: never overwrites an existing snapshot, so
# a re-entrant execute keeps the launch-time policy. Best-effort: any failure
# returns 0 (the resolvers fall back to the global file) so it can never break a
# run. The source is the global home-or-root file (NOT a run-dir, to avoid a
# snapshot freezing itself on re-entry).
mo_snapshot_run_config() {
  local run_dir="${1:-${MINI_ORK_RUN_DIR:-}}"
  [ -n "$run_dir" ] || return 0
  local dest="${run_dir}/config/agents.yaml"
  [ -f "$dest" ] && return 0  # already frozen — keep launch-time policy
  local src="${MINI_ORK_HOME:-.mini-ork}/config/agents.yaml"
  [ -f "$src" ] || src="${MINI_ORK_ROOT:-.}/config/agents.yaml"
  [ -f "$src" ] || return 0   # nothing to snapshot
  mkdir -p "${run_dir}/config" 2>/dev/null || return 0
  cp "$src" "$dest" 2>/dev/null || return 0
  return 0
}
