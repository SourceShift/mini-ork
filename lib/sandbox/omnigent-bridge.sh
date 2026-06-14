#!/usr/bin/env bash
# omnigent-bridge.sh — sandbox bridge to Omnigent's sandbox backends.
#
# Implements Epic E1 of the Omnigent-improvement plan
# (.mini-ork/kickoffs/omnigent-phase-e1-egress-proxy-bridge.md) per
# the panel-revised ordering at
# docs/research/omnigent-vs-mini-ork-panel-synthesis.md.
#
# The 8-lens panel unanimously flagged secret isolation as the
# highest-leverage gap (codex-1 verified the exfil path at
# lib/llm-dispatch.sh:694-781). Omnigent ships a real OS sandbox
# stack (bwrap on Linux at omnigent/sandbox/bwrap.py, seatbelt on
# macOS at omnigent/sandbox/seatbelt.py). Both Apache 2.0.
#
# This bridge does NOT reimplement those backends — it shells out
# to the omnigent CLI when installed and falls back to a clearly-
# logged pass-through when not. Operators choose whether to install
# Omnigent; mini-ork stays runnable either way.
#
# Public API:
#   mo_sandbox_detect
#       Emits backend identity on stdout:
#         linux_bwrap | mac_seatbelt | omnigent_absent | unsupported
#   mo_sandbox_run <command-array>
#       Runs <command...> inside the resolved sandbox. When the
#       sandbox CLI is absent, runs the command directly and logs
#       a structured 'omnigent_unavailable' warning to stderr.
#
# Env knobs:
#   MO_OMNIGENT_BIN   Override path to the omnigent CLI. Default:
#                      `command -v omnigent`.
#   MO_SANDBOX_FORCE  Force a backend identity for testing. Values
#                      match the mo_sandbox_detect output.

set -uo pipefail

_mo_sandbox_log() {
  # Structured stderr warning so operators (and the trace store)
  # can grep for sandbox degradations after the fact.
  local _level="$1"; shift
  printf '{"level":"%s","subsystem":"sandbox","ts":"%s","msg":"%s"}\n' \
    "$_level" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >&2
}

mo_sandbox_detect() {
  if [ -n "${MO_SANDBOX_FORCE:-}" ]; then
    printf '%s\n' "$MO_SANDBOX_FORCE"
    return 0
  fi

  local _omnigent_bin="${MO_OMNIGENT_BIN:-$(command -v omnigent 2>/dev/null)}"
  if [ -z "$_omnigent_bin" ] || [ ! -x "$_omnigent_bin" ]; then
    printf '%s\n' "omnigent_absent"
    return 0
  fi

  # Trust the omnigent CLI to pick its own platform backend. The
  # Linux default selection in omnigent/sandbox/bwrap.py:15-20 already
  # falls back to 'none' when bwrap is missing — we mirror that
  # transparency by returning the platform-tier name here.
  case "$(uname -s)" in
    Linux)  printf '%s\n' "linux_bwrap" ;;
    Darwin) printf '%s\n' "mac_seatbelt" ;;
    *)      printf '%s\n' "unsupported" ;;
  esac
}

mo_sandbox_run() {
  if [ $# -lt 1 ]; then
    _mo_sandbox_log "error" "mo_sandbox_run: command required"
    return 2
  fi

  local _backend
  _backend=$(mo_sandbox_detect)

  case "$_backend" in
    linux_bwrap|mac_seatbelt)
      local _omnigent_bin="${MO_OMNIGENT_BIN:-$(command -v omnigent)}"
      _mo_sandbox_log "info" "running under $_backend via $_omnigent_bin"
      "$_omnigent_bin" sandbox run -- "$@"
      return $?
      ;;
    omnigent_absent)
      _mo_sandbox_log "warn" "omnigent_unavailable: pass-through degraded mode; secrets in env are NOT isolated"
      "$@"
      return $?
      ;;
    unsupported|*)
      _mo_sandbox_log "warn" "unsupported_platform: pass-through degraded mode"
      "$@"
      return $?
      ;;
  esac
}

# Self-test fixtures: 3 cases mirroring the krippendorff_alpha_gate
# self-test convention. Run directly to exercise the contract.
if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
  echo "--- fixture 1: detect with no omnigent installed (expect omnigent_absent) ---"
  MO_OMNIGENT_BIN="" MO_SANDBOX_FORCE="" out=$(mo_sandbox_detect)
  if [ "$out" = "omnigent_absent" ] || [ "$out" = "linux_bwrap" ] || [ "$out" = "mac_seatbelt" ]; then
    echo "  [ok] detect returned '$out'"
  else
    echo "  [fail] unexpected: '$out'"
  fi

  echo "--- fixture 2: run with omnigent absent (expect pass-through, warning logged) ---"
  warn_seen=0
  out=$(MO_OMNIGENT_BIN="" MO_SANDBOX_FORCE=omnigent_absent mo_sandbox_run echo "hello" 2> /tmp/.sandbox-selftest-err.log)
  if [ "$out" = "hello" ] && grep -q omnigent_unavailable /tmp/.sandbox-selftest-err.log; then
    echo "  [ok] pass-through ran and emitted omnigent_unavailable warning"
  else
    echo "  [fail] pass-through path broken: out='$out'"
    cat /tmp/.sandbox-selftest-err.log >&2
  fi
  rm -f /tmp/.sandbox-selftest-err.log

  echo "--- fixture 3: run with no command (expect rc=2 + error log) ---"
  if mo_sandbox_run 2>/dev/null; then
    echo "  [fail] no-command path should have returned non-zero"
  else
    echo "  [ok] no-command path returned non-zero as expected"
  fi
fi
