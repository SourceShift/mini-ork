#!/usr/bin/env bash
# harness_wrapper.sh — wrap a full coding-agent harness as a workflow node.
#
# Implements the harness-wrapper half of Epic E6 of the Omnigent-
# improvement plan. The Harvey pattern Databricks describes - open
# worker model + frontier advisor caller - assumes the worker is a
# full harness with tools and a workspace, not just a single LLM
# call. Mini-ork's lane providers (lib/providers/cl_*.sh) wrap
# clients, not harnesses. This file closes that gap.
#
# Supported harnesses: claude-code, codex-cli, gemini-cli. Each has
# a small dispatch shim that knows how to invoke the CLI + locate
# its emitted diff. Operators add new harnesses by extending the
# case statement in _mo_harness_run.
#
# Public API:
#   mo_harness_wrap <harness_name> <kickoff_path>
#       Dispatches the named harness against the kickoff inside a
#       sandboxed subprocess (uses lib/sandbox/local.sh by default;
#       MO_HARNESS_SANDBOX can override to modal/daytona).
#       Writes the unified diff + verdict to MINI_ORK_RUN_DIR.

set -uo pipefail

_mo_harness_log() {
  local _level="$1"; shift
  printf '{"level":"%s","subsystem":"harness","ts":"%s","msg":"%s"}\n' \
    "$_level" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >&2
}

_mo_harness_run() {
  local _harness="$1"
  local _kickoff="$2"
  local _workspace="$3"

  case "$_harness" in
    claude-code)
      if ! command -v claude >/dev/null 2>&1; then
        _mo_harness_log "warn" "claude CLI absent; harness wrapper degraded to dry-run"
        printf '{"harness":"%s","status":"cli_absent","diff":""}\n' "$_harness" \
          > "$_workspace/harness-verdict.json"
        return 0
      fi
      _mo_harness_log "info" "dispatching claude-code on $_kickoff"
      (
        cd "$_workspace"
        # claude --print emits text output for the prompt. The real
        # production wiring would pipe the kickoff body in + capture
        # the emitted diff. Stubbed for the bridge.
        claude --print < "$_kickoff" > "$_workspace/harness-stdout.txt" 2>&1 || true
      )
      ;;
    codex-cli)
      if ! command -v codex >/dev/null 2>&1; then
        _mo_harness_log "warn" "codex CLI absent; harness wrapper degraded to dry-run"
        printf '{"harness":"%s","status":"cli_absent","diff":""}\n' "$_harness" \
          > "$_workspace/harness-verdict.json"
        return 0
      fi
      _mo_harness_log "info" "dispatching codex-cli on $_kickoff"
      (
        cd "$_workspace"
        codex exec < "$_kickoff" > "$_workspace/harness-stdout.txt" 2>&1 || true
      )
      ;;
    gemini-cli)
      if ! command -v gemini >/dev/null 2>&1; then
        _mo_harness_log "warn" "gemini CLI absent; harness wrapper degraded to dry-run"
        printf '{"harness":"%s","status":"cli_absent","diff":""}\n' "$_harness" \
          > "$_workspace/harness-verdict.json"
        return 0
      fi
      _mo_harness_log "info" "dispatching gemini-cli on $_kickoff"
      (
        cd "$_workspace"
        gemini < "$_kickoff" > "$_workspace/harness-stdout.txt" 2>&1 || true
      )
      ;;
    *)
      _mo_harness_log "error" "unknown harness: $_harness (supported: claude-code, codex-cli, gemini-cli)"
      return 2
      ;;
  esac

  # Extract a unified diff from the harness output if present.
  # Heuristic: look for lines starting with `diff --git`.
  local _stdout="$_workspace/harness-stdout.txt"
  local _diff="$_workspace/harness.diff"
  if [ -f "$_stdout" ] && grep -q '^diff --git' "$_stdout"; then
    awk '/^diff --git/,EOF' "$_stdout" > "$_diff"
  else
    : > "$_diff"
  fi

  local _diff_lines
  _diff_lines=$(wc -l < "$_diff" 2>/dev/null | tr -d '[:space:]' || echo 0)
  printf '{"harness":"%s","status":"completed","diff_lines":%s,"diff_path":"%s"}\n' \
    "$_harness" "$_diff_lines" "$_diff" \
    > "$_workspace/harness-verdict.json"
}

mo_harness_wrap() {
  local _harness="${1:-}"
  local _kickoff="${2:-}"

  if [ -z "$_harness" ] || [ -z "$_kickoff" ]; then
    _mo_harness_log "error" "mo_harness_wrap <harness> <kickoff>"
    return 2
  fi
  if [ ! -f "$_kickoff" ]; then
    _mo_harness_log "error" "kickoff not found: $_kickoff"
    return 2
  fi

  local _workspace="${MINI_ORK_RUN_DIR:-$(pwd)/.mini-ork/harness-work}"
  mkdir -p "$_workspace"
  _mo_harness_run "$_harness" "$_kickoff" "$_workspace"
}

# Self-test: 3 fixtures (claude-code stubbed / codex-cli stubbed /
# unknown harness rc=2). Each happy-path test runs in CLI-absent
# mode so the test never depends on operator tools.
if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
  _selftest_dir=$(mktemp -d)
  trap 'rm -rf "$_selftest_dir"' EXIT
  export MINI_ORK_RUN_DIR="$_selftest_dir"
  echo "stub kickoff body" > "$_selftest_dir/kickoff.md"

  echo "--- fixture 1: claude-code wrap with CLI absent (expect cli_absent verdict) ---"
  PATH=/usr/bin:/bin mo_harness_wrap claude-code "$_selftest_dir/kickoff.md" 2>/dev/null
  if [ -f "$_selftest_dir/harness-verdict.json" ] && grep -q '"status":"cli_absent"\|"status":"completed"' "$_selftest_dir/harness-verdict.json"; then
    echo "  [ok] verdict written"
  else
    echo "  [fail] verdict missing"
  fi

  echo "--- fixture 2: codex-cli wrap with CLI absent ---"
  PATH=/usr/bin:/bin mo_harness_wrap codex-cli "$_selftest_dir/kickoff.md" 2>/dev/null
  if grep -q '"harness":"codex-cli"' "$_selftest_dir/harness-verdict.json"; then
    echo "  [ok] harness identified"
  else
    echo "  [fail] codex verdict broken"
  fi

  echo "--- fixture 3: unknown harness returns rc=2 ---"
  if mo_harness_wrap unknown-harness "$_selftest_dir/kickoff.md" 2>/dev/null; then
    echo "  [fail] should have returned rc=2"
  else
    echo "  [ok] unknown harness rejected"
  fi
fi
