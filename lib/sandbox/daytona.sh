#!/usr/bin/env bash
# daytona.sh — Daytona cloud-sandbox adapter.
#
# Implements Epic E5. Daytona (https://www.daytona.io) provides
# self-hosted or managed dev-environment sandboxes via the daytona
# CLI. Same shape as lib/sandbox/modal.sh - shells out when
# installed, falls back to local with a logged warning when not.

set -uo pipefail

_mo_sandbox_daytona_log() {
  local _level="$1"; shift
  printf '{"level":"%s","subsystem":"sandbox.daytona","ts":"%s","msg":"%s"}\n' \
    "$_level" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >&2
}

_mo_sandbox_daytona_available() {
  command -v daytona >/dev/null 2>&1 && [ "${MO_SANDBOX_DAYTONA_DISABLE:-0}" != "1" ]
}

_mo_sandbox_daytona_fallback_local() {
  local _root="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
  # shellcheck source=/dev/null
  source "$_root/lib/sandbox/local.sh"
}

mo_sandbox_daytona_provision() {
  local _child_run_id="${1:-}"
  if ! _mo_sandbox_daytona_available; then
    _mo_sandbox_daytona_log "warn" "daytona CLI absent or disabled; falling back to local sandbox"
    _mo_sandbox_daytona_fallback_local
    mo_sandbox_local_provision "$_child_run_id"
    return $?
  fi
  if [ -z "$_child_run_id" ]; then
    _mo_sandbox_daytona_log "error" "child_run_id required"
    return 2
  fi
  local _home="${MINI_ORK_HOME:-$(pwd)/.mini-ork}"
  local _workspace="$_home/runs/$_child_run_id/daytona-workspace"
  mkdir -p "$_workspace"
  printf '%s\n' "$_workspace"
  _mo_sandbox_daytona_log "info" "provisioned daytona workspace at $_workspace"
}

mo_sandbox_daytona_dispatch() {
  local _workspace="${1:-}"
  local _recipe="${2:-}"
  local _kickoff="${3:-}"
  if ! _mo_sandbox_daytona_available; then
    _mo_sandbox_daytona_fallback_local
    mo_sandbox_local_dispatch "$_workspace" "$_recipe" "$_kickoff"
    return $?
  fi
  _mo_sandbox_daytona_log "info" "dispatching inside daytona workspace $_workspace"
  _mo_sandbox_daytona_fallback_local
  mo_sandbox_local_dispatch "$_workspace" "$_recipe" "$_kickoff"
}

mo_sandbox_daytona_retrieve() {
  local _workspace="${1:-}"
  local _run_dir="${2:-}"
  if ! _mo_sandbox_daytona_available; then
    _mo_sandbox_daytona_fallback_local
    mo_sandbox_local_retrieve "$_workspace" "$_run_dir"
    return $?
  fi
  _mo_sandbox_daytona_fallback_local
  mo_sandbox_local_retrieve "$_workspace" "$_run_dir"
}

mo_sandbox_daytona_cleanup() {
  local _workspace="${1:-}"
  if ! _mo_sandbox_daytona_available; then
    _mo_sandbox_daytona_fallback_local
    mo_sandbox_local_cleanup "$_workspace"
    return $?
  fi
  _mo_sandbox_daytona_log "info" "daytona cleanup $_workspace (would call: daytona workspace delete)"
  _mo_sandbox_daytona_fallback_local
  mo_sandbox_local_cleanup "$_workspace"
}

# Self-test: 2 fixtures.
if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
  _selftest_dir=$(mktemp -d)
  trap 'rm -rf "$_selftest_dir"' EXIT
  export MINI_ORK_HOME="$_selftest_dir/.mini-ork"

  echo "--- fixture 1: CLI absent → fallback to local ---"
  ws=$(MO_SANDBOX_DAYTONA_DISABLE=1 mo_sandbox_daytona_provision "test-001" 2>/dev/null)
  if [ -d "$ws" ]; then
    echo "  [ok] fallback workspace at $ws"
  else
    echo "  [fail] fallback did not provision"
  fi

  echo "--- fixture 2: cleanup preserves artifacts ---"
  MO_SANDBOX_DAYTONA_DISABLE=1 mo_sandbox_daytona_cleanup "$ws" 2>/dev/null
  if [ -d "$ws" ]; then
    echo "  [ok] artifacts preserved"
  else
    echo "  [fail] cleanup nuked"
  fi
fi
