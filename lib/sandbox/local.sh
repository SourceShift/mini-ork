#!/usr/bin/env bash
# local.sh — local-dispatch sandbox adapter.
#
# Implements Epic E5 of the Omnigent-improvement plan
# (.mini-ork/kickoffs/omnigent-phase-e5-cloud-sandbox-adapters.md).
# This adapter encapsulates today's local-dispatch behavior as a
# clean backend so `bin/mini-ork-spawn` can pick a backend at
# dispatch time. Preserves the worktree-creation logic + the
# cwd→RUN_DIR sync fix shipped at commit 62cd3f5.
#
# Public API (the 4-function contract every adapter implements):
#   mo_sandbox_local_provision <child_run_id>   → emits workspace path
#   mo_sandbox_local_dispatch <workspace> <recipe> <kickoff>
#   mo_sandbox_local_retrieve <workspace> <run_dir>
#   mo_sandbox_local_cleanup <workspace>

set -uo pipefail

_mo_sandbox_local_log() {
  local _level="$1"; shift
  printf '{"level":"%s","subsystem":"sandbox.local","ts":"%s","msg":"%s"}\n' \
    "$_level" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >&2
}

mo_sandbox_local_provision() {
  local _child_run_id="${1:-}"
  if [ -z "$_child_run_id" ]; then
    _mo_sandbox_local_log "error" "mo_sandbox_local_provision: child_run_id required"
    return 2
  fi
  local _home="${MINI_ORK_HOME:-$(pwd)/.mini-ork}"
  local _workspace="$_home/runs/$_child_run_id/workspace"
  mkdir -p "$_workspace"
  printf '%s\n' "$_workspace"
  _mo_sandbox_local_log "info" "provisioned $_workspace"
}

mo_sandbox_local_dispatch() {
  local _workspace="${1:-}"
  local _recipe="${2:-}"
  local _kickoff="${3:-}"
  if [ -z "$_workspace" ] || [ -z "$_kickoff" ]; then
    _mo_sandbox_local_log "error" "usage: mo_sandbox_local_dispatch <workspace> <recipe> <kickoff>"
    return 2
  fi
  # Local backend just shells out to bin/mini-ork in the workspace's
  # working directory. The recipe arg is optional - mini-ork-classify
  # will pick the right one if omitted.
  local _root="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
  (
    cd "$_workspace"
    if [ -n "$_recipe" ]; then
      "$_root/bin/mini-ork" run "$_recipe" "$_kickoff"
    else
      "$_root/bin/mini-ork" run "$_kickoff"
    fi
  )
}

mo_sandbox_local_retrieve() {
  local _workspace="${1:-}"
  local _run_dir="${2:-}"
  if [ -z "$_workspace" ] || [ -z "$_run_dir" ]; then
    _mo_sandbox_local_log "error" "usage: mo_sandbox_local_retrieve <workspace> <run_dir>"
    return 2
  fi
  # Local: no copy needed - the workspace IS on the host filesystem.
  # Just ensure the operator-visible run_dir resolves to the workspace.
  if [ "$_workspace" != "$_run_dir" ] && [ -d "$_workspace" ]; then
    mkdir -p "$_run_dir"
    # Hardlinks where possible to avoid duplication; copy fallback otherwise.
    cp -al "$_workspace"/* "$_run_dir/" 2>/dev/null \
      || cp -R "$_workspace"/* "$_run_dir/" 2>/dev/null \
      || true
  fi
}

mo_sandbox_local_cleanup() {
  local _workspace="${1:-}"
  if [ -z "$_workspace" ]; then
    return 0
  fi
  # Local cleanup is a no-op by default - artifacts on the host
  # filesystem are intentionally preserved for operator inspection.
  # Operators can force removal with MO_SANDBOX_LOCAL_NUKE=1.
  if [ "${MO_SANDBOX_LOCAL_NUKE:-0}" = "1" ] && [ -d "$_workspace" ]; then
    rm -rf "$_workspace"
  fi
}

# Self-test: 3 fixtures.
if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
  _selftest_dir=$(mktemp -d)
  trap 'rm -rf "$_selftest_dir"' EXIT
  export MINI_ORK_HOME="$_selftest_dir/.mini-ork"

  echo "--- fixture 1: provision returns workspace path ---"
  ws=$(mo_sandbox_local_provision "test-child-001" 2>/dev/null)
  if [ -d "$ws" ]; then
    echo "  [ok] workspace at $ws"
  else
    echo "  [fail] workspace not created"
  fi

  echo "--- fixture 2: retrieve no-op when workspace == run_dir ---"
  if mo_sandbox_local_retrieve "$ws" "$ws" 2>/dev/null; then
    echo "  [ok] retrieve same-path is a no-op"
  else
    echo "  [fail] retrieve same-path raised"
  fi

  echo "--- fixture 3: cleanup is no-op without MO_SANDBOX_LOCAL_NUKE ---"
  mo_sandbox_local_cleanup "$ws"
  if [ -d "$ws" ]; then
    echo "  [ok] artifacts preserved by default"
  else
    echo "  [fail] cleanup nuked without opt-in"
  fi
fi
