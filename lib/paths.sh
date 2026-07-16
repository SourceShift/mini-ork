#!/usr/bin/env bash
# lib/paths.sh — single source of truth for mini-ork path resolution.
#
# Source this file at the top of every entrypoint script. It exports three
# contracts and back-compat aliases:
#
#   MINI_ORK_ENGINE_ROOT   read-only engine installation (bin/, lib/, recipes/)
#   MINI_ORK_PROJECT_HOME  project state: .mini-ork/ (state.db, config, runs)
#   MINI_ORK_TARGET_REPO   tree being mutated by the current run
#   MINI_ORK_ROOT          alias for MINI_ORK_ENGINE_ROOT (legacy)
#   MINI_ORK_HOME          alias for MINI_ORK_PROJECT_HOME (legacy)
#
# Resolution rules (in order):
#   1. Explicit env var wins: MINI_ORK_ENGINE_ROOT / MINI_ORK_PROJECT_HOME /
#      MINI_ORK_TARGET_REPO.
#   2. Pointer file `.mini-ork/engine` (relative to CWD or MINI_ORK_HOME) gives
#      the engine root, enabling "install mini-ork into any repo".
#   3. Fallback: derive engine root from this script's location.
#   4. PROJECT_HOME defaults to $(pwd)/.mini-ork if unset.
#   5. TARGET_REPO defaults to $(pwd) if unset.
#
# After sourcing, scripts must NOT compute their own defaults from BASH_SOURCE.

set -Eeuo pipefail

# ── helper: make a path absolute ──────────────────────────────────────────────
_mo_abspath() {
  local p="$1"
  if [ -d "$p" ]; then
    (cd "$p" && pwd -P)
  else
    # File path: cd to parent, then append basename.
    (cd "$(dirname "$p")" && printf '%s/%s\n' "$(pwd -P)" "$(basename "$p")")
  fi
}

# ── ENGINE_ROOT ───────────────────────────────────────────────────────────────
if [ -n "${MINI_ORK_ENGINE_ROOT:-}" ]; then
  MINI_ORK_ENGINE_ROOT="$(_mo_abspath "$MINI_ORK_ENGINE_ROOT")"
else
  # Look for a project-local pointer to a separately-installed engine.
  _mo_engine_pointer=""
  if [ -n "${MINI_ORK_HOME:-}" ] && [ -f "$MINI_ORK_HOME/engine" ]; then
    _mo_engine_pointer="$MINI_ORK_HOME/engine"
  elif [ -f "$(pwd)/.mini-ork/engine" ]; then
    _mo_engine_pointer="$(pwd)/.mini-ork/engine"
  fi

  if [ -n "$_mo_engine_pointer" ]; then
    _mo_pointer_target="$(cat "$_mo_engine_pointer" | head -1 | tr -d '[:space:]')"
    if [ -z "$_mo_pointer_target" ]; then
      echo "mini-ork: empty engine pointer: $_mo_engine_pointer" >&2
      exit 2
    fi
    if [ -d "$_mo_pointer_target" ]; then
      MINI_ORK_ENGINE_ROOT="$(_mo_abspath "$_mo_pointer_target")"
    else
      # Resolve relative to the pointer file's directory.
      _mo_pointer_dir="$(dirname "$_mo_engine_pointer")"
      MINI_ORK_ENGINE_ROOT="$(_mo_abspath "$_mo_pointer_dir/$_mo_pointer_target")"
    fi
    unset _mo_pointer_target _mo_pointer_dir
  else
    # Fallback: this script lives at <engine>/lib/paths.sh.
    MINI_ORK_ENGINE_ROOT="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd -P)"
  fi
  unset _mo_engine_pointer
fi
export MINI_ORK_ENGINE_ROOT

# ── PROJECT_HOME ──────────────────────────────────────────────────────────────
if [ -n "${MINI_ORK_PROJECT_HOME:-}" ]; then
  MINI_ORK_PROJECT_HOME="$(_mo_abspath "$MINI_ORK_PROJECT_HOME")"
elif [ -n "${MINI_ORK_HOME:-}" ]; then
  MINI_ORK_PROJECT_HOME="$(_mo_abspath "$MINI_ORK_HOME")"
else
  MINI_ORK_PROJECT_HOME="$(_mo_abspath "$(pwd)/.mini-ork")"
fi
export MINI_ORK_PROJECT_HOME

# ── TARGET_REPO ───────────────────────────────────────────────────────────────
if [ -n "${MINI_ORK_TARGET_REPO:-}" ]; then
  MINI_ORK_TARGET_REPO="$(_mo_abspath "$MINI_ORK_TARGET_REPO")"
else
  MINI_ORK_TARGET_REPO="$(_mo_abspath "$(pwd)")"
fi
export MINI_ORK_TARGET_REPO

# ── legacy aliases ────────────────────────────────────────────────────────────
MINI_ORK_ROOT="$MINI_ORK_ENGINE_ROOT"
export MINI_ORK_ROOT
MINI_ORK_HOME="$MINI_ORK_PROJECT_HOME"
export MINI_ORK_HOME

unset -f _mo_abspath
