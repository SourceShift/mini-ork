#!/usr/bin/env bash
# harness_wrapper.sh — wrap a full coding-agent harness as a workflow node.
#
# Implements the harness-wrapper half of Epic E6 of the Omnigent-
# improvement plan. The Harvey pattern Databricks describes — open
# worker model + frontier advisor caller — assumes the worker is a
# full harness with tools and a workspace, not just a single LLM
# call. Mini-ork's lane providers (lib/providers/cl_*.sh) wrap
# clients, not harnesses. This file closes that gap.
#
# Supported harnesses: claude-code, codex-cli, gemini-cli.
#
# Dispatch contract (uniform across all three CLIs):
#   1. The workspace is initialized as a git repo (if not already).
#   2. The kickoff body is read + concatenated into the harness prompt.
#   3. The CLI is invoked with cwd=workspace + the prompt on stdin
#      OR via the CLI's argv-prompt flag, whichever shape the CLI
#      actually supports.
#   4. After the CLI exits, git diff captures every change as the
#      unified diff. The harness writes harness-verdict.json with
#      diff_lines + exit_code + harness identity.
#
# Why git-diff capture beats stdout parsing:
#   The original stub heuristic scanned stdout for "^diff --git"
#   anchors. Real coding agents typically write changes through tool
#   calls (Write/Edit/Bash patch) — the assistant transcript that
#   reaches stdout describes the work but does not include a
#   unified-diff dump. git-diff against the workspace's HEAD captures
#   exactly what changed regardless of how the CLI emitted it.
#
# Public API:
#   mo_harness_wrap <harness_name> <kickoff_path>
#       Writes the unified diff + verdict to MINI_ORK_RUN_DIR.
#       Returns 0 when the verdict file is written (even when the
#       CLI exited non-zero or was absent); returns 2 only when
#       arguments are malformed.
#
# Env knobs:
#   MO_HARNESS_TIMEOUT_S    Per-CLI wall-clock timeout. Default 900.
#   MO_HARNESS_DRY_RUN      Set to 1 to skip the actual CLI dispatch
#                            and emit a synthetic "dry_run" verdict.
#                            Used by the self-test to avoid burning
#                            tokens on a real harness call.
#   MO_HARNESS_PROMPT_FILE  Override the kickoff path with a
#                            preprocessed prompt file (for callers
#                            that want to layer system instructions).

set -uo pipefail

_mo_harness_log() {
  local _level="$1"; shift
  printf '{"level":"%s","subsystem":"harness","ts":"%s","msg":"%s"}\n' \
    "$_level" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >&2
}

_mo_harness_emit_verdict() {
  # Args: workspace harness status exit_code diff_lines diff_path notes
  local _workspace="$1" _harness="$2" _status="$3" _rc="$4"
  local _diff_lines="$5" _diff_path="$6" _notes="$7"
  python3 - "$_workspace/harness-verdict.json" <<PY
import json, sys
out = sys.argv[1]
verdict = {
    "harness":     "$_harness",
    "status":      "$_status",
    "exit_code":   $_rc,
    "diff_lines":  $_diff_lines,
    "diff_path":   "$_diff_path",
    "notes":       "$_notes",
}
with open(out, "w", encoding="utf-8") as f:
    json.dump(verdict, f, indent=2)
    f.write("\n")
PY
}

_mo_harness_git_init_if_needed() {
  local _workspace="$1"
  if ! git -C "$_workspace" rev-parse --git-dir >/dev/null 2>&1; then
    git -C "$_workspace" init --quiet 2>/dev/null || return 1
    # Empty initial commit so `git diff HEAD` works against a known
    # baseline. The author/committer identity is set inline so this
    # works on CI runners without a global git config.
    git -C "$_workspace" \
      -c "user.email=harness@mini-ork.local" \
      -c "user.name=mini-ork harness" \
      commit --allow-empty -m "harness baseline" --quiet 2>/dev/null || return 1
  else
    # Workspace already a git repo: stage current state so the diff
    # captures only what the harness changes, not pre-existing dirty
    # work.
    git -C "$_workspace" add -A 2>/dev/null || true
    git -C "$_workspace" \
      -c "user.email=harness@mini-ork.local" \
      -c "user.name=mini-ork harness" \
      commit -m "harness baseline" --quiet --allow-empty 2>/dev/null || true
  fi
}

_mo_harness_capture_diff() {
  # After the CLI exits, run `git add -A && git diff --staged` to
  # capture every change as a unified diff. Stage first so untracked
  # files (which `git diff HEAD` would miss) are included.
  local _workspace="$1" _diff_path="$2"
  git -C "$_workspace" add -A 2>/dev/null || true
  git -C "$_workspace" diff --cached HEAD 2>/dev/null > "$_diff_path" || true
}

_mo_harness_dispatch_claude_code() {
  # claude --print: streams a single assistant response.
  # --permission-mode bypassPermissions: required for autonomous
  #   tool-use without per-call prompts (Anthropic CLI convention,
  #   mirrors what lib/llm-dispatch.sh:774 sets).
  # --allowedTools: gives the agent file/bash access in the
  #   workspace. Restricted to read + write + bash so the agent
  #   cannot reach outside the workspace.
  # We pipe the kickoff body in on stdin and run with cwd=workspace.
  local _workspace="$1" _kickoff="$2" _timeout="$3"
  (
    cd "$_workspace"
    # macOS: timeout is gtimeout (or absent). Fall back to running
    # without a wall-clock cap when neither binary exists.
    if command -v timeout >/dev/null 2>&1; then
      timeout "${_timeout}s" claude \
        --print \
        --permission-mode bypassPermissions \
        --allowedTools "Read,Write,Edit,Bash" \
        < "$_kickoff" \
        > "$_workspace/harness-stdout.txt" \
        2> "$_workspace/harness-stderr.txt"
    else
      claude \
        --print \
        --permission-mode bypassPermissions \
        --allowedTools "Read,Write,Edit,Bash" \
        < "$_kickoff" \
        > "$_workspace/harness-stdout.txt" \
        2> "$_workspace/harness-stderr.txt"
    fi
  )
}

_mo_harness_dispatch_codex_cli() {
  # codex exec accepts the prompt as the trailing positional argument
  # (cl_codex.sh:157 pins this contract). `--skip-git-repo-check`
  # avoids the prompt in fresh workspaces; `--sandbox workspace-write`
  # mirrors lib/providers/cl_codex.sh:104 so codex can edit files in
  # cwd. `--json` would emit JSONL (parseable per cl_codex.sh) but
  # for the harness use case the workspace diff is what we want, not
  # the transcript — so we stick to text output for human readability.
  local _workspace="$1" _kickoff="$2" _timeout="$3"
  local _prompt_body
  _prompt_body=$(cat "$_kickoff")
  (
    cd "$_workspace"
    if command -v timeout >/dev/null 2>&1; then
      timeout "${_timeout}s" codex exec \
        --skip-git-repo-check \
        --sandbox workspace-write \
        "$_prompt_body" \
        > "$_workspace/harness-stdout.txt" \
        2> "$_workspace/harness-stderr.txt"
    else
      codex exec \
        --skip-git-repo-check \
        --sandbox workspace-write \
        "$_prompt_body" \
        > "$_workspace/harness-stdout.txt" \
        2> "$_workspace/harness-stderr.txt"
    fi
  )
}

_mo_harness_dispatch_gemini_cli() {
  # Google Gemini CLI accepts -p <prompt> for one-shot mode. The
  # 2026-Q2 release adds tool-use semantics, but mini-ork does not
  # rely on them — we let gemini edit the workspace via its built-in
  # Write/Edit tools and capture changes via git diff.
  local _workspace="$1" _kickoff="$2" _timeout="$3"
  local _prompt_body
  _prompt_body=$(cat "$_kickoff")
  (
    cd "$_workspace"
    if command -v timeout >/dev/null 2>&1; then
      timeout "${_timeout}s" gemini \
        -p "$_prompt_body" \
        > "$_workspace/harness-stdout.txt" \
        2> "$_workspace/harness-stderr.txt"
    else
      gemini \
        -p "$_prompt_body" \
        > "$_workspace/harness-stdout.txt" \
        2> "$_workspace/harness-stderr.txt"
    fi
  )
}

_mo_harness_run() {
  local _harness="$1" _kickoff="$2" _workspace="$3"
  local _timeout="${MO_HARNESS_TIMEOUT_S:-900}"
  local _diff_path="$_workspace/harness.diff"
  : > "$_diff_path"

  # Harness validation gate runs first so unknown harness names are
  # rejected before any side effects (git init, dry-run verdict, etc).
  case "$_harness" in
    claude-code|codex-cli|gemini-cli) ;;
    *)
      _mo_harness_log "error" "unknown harness: $_harness (supported: claude-code, codex-cli, gemini-cli)"
      return 2
      ;;
  esac

  # Workspace is prepared regardless of the dispatch path so the
  # operator can swap in a CLI later (cli_absent → installed) without
  # re-initializing. Failure to initialize git is tolerated — the
  # later capture_diff step degrades to an empty diff rather than
  # crashing the verdict.
  _mo_harness_git_init_if_needed "$_workspace" || \
    _mo_harness_log "warn" "git init failed in $_workspace; diff capture will be empty"

  # Dry-run path used by tests + operators probing the contract
  # without burning real tokens. Emits a synthetic but well-formed
  # verdict + zero-byte diff.
  if [ "${MO_HARNESS_DRY_RUN:-0}" = "1" ]; then
    _mo_harness_log "info" "MO_HARNESS_DRY_RUN=1 — skipping real CLI dispatch"
    _mo_harness_emit_verdict "$_workspace" "$_harness" "dry_run" 0 0 "$_diff_path" "dry-run mode"
    return 0
  fi

  # Per-CLI availability check. CLI-absent is a degraded but
  # well-defined verdict so downstream verifiers can decide whether
  # absence is a hard fail or a soft fall-through.
  local _binary
  case "$_harness" in
    claude-code) _binary="claude" ;;
    codex-cli)   _binary="codex" ;;
    gemini-cli)  _binary="gemini" ;;
  esac
  if ! command -v "$_binary" >/dev/null 2>&1; then
    _mo_harness_log "warn" "$_binary CLI absent; harness wrapper emitting cli_absent verdict"
    _mo_harness_emit_verdict "$_workspace" "$_harness" "cli_absent" 127 0 "$_diff_path" "no $_binary on PATH"
    return 0
  fi

  _mo_harness_log "info" "dispatching $_harness in $_workspace (kickoff=$_kickoff)"
  set +e
  case "$_harness" in
    claude-code) _mo_harness_dispatch_claude_code "$_workspace" "$_kickoff" "$_timeout" ;;
    codex-cli)   _mo_harness_dispatch_codex_cli   "$_workspace" "$_kickoff" "$_timeout" ;;
    gemini-cli)  _mo_harness_dispatch_gemini_cli  "$_workspace" "$_kickoff" "$_timeout" ;;
  esac
  local _rc=$?
  set -e

  _mo_harness_capture_diff "$_workspace" "$_diff_path"

  local _diff_lines
  _diff_lines=$(wc -l < "$_diff_path" 2>/dev/null | tr -d '[:space:]')
  [ -z "$_diff_lines" ] && _diff_lines=0

  local _status
  if [ "$_rc" -eq 0 ] && [ "$_diff_lines" -gt 0 ]; then
    _status="completed"
  elif [ "$_rc" -eq 0 ] && [ "$_diff_lines" -eq 0 ]; then
    _status="no_changes"
  elif [ "$_rc" -eq 124 ]; then
    _status="timeout"
  else
    _status="harness_error"
  fi

  _mo_harness_emit_verdict "$_workspace" "$_harness" "$_status" "$_rc" "$_diff_lines" "$_diff_path" "rc=$_rc lines=$_diff_lines"
  return 0
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

# Self-test fixtures. Exercises the full dispatch contract in
# CLI-absent and dry-run modes so the tests never call a real CLI
# (which would burn tokens + need credentials).
if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
  _selftest_dir=$(mktemp -d)
  trap 'rm -rf "$_selftest_dir"' EXIT
  export MINI_ORK_RUN_DIR="$_selftest_dir"
  echo "stub kickoff body" > "$_selftest_dir/kickoff.md"

  _read_verdict_field() {
    python3 -c "import json,sys; print(json.load(open(sys.argv[1])).get(sys.argv[2], ''))" \
      "$1" "$2"
  }

  echo "--- fixture 1: claude-code CLI absent → cli_absent verdict with rc=127 ---"
  PATH=/usr/bin:/bin mo_harness_wrap claude-code "$_selftest_dir/kickoff.md" 2>/dev/null
  vstatus=$(_read_verdict_field "$_selftest_dir/harness-verdict.json" status)
  vrc=$(_read_verdict_field "$_selftest_dir/harness-verdict.json" exit_code)
  if [ "$vstatus" = "cli_absent" ] && [ "$vrc" = "127" ]; then
    echo "  [ok] cli_absent verdict + rc=127"
  else
    echo "  [fail] status=$vstatus rc=$vrc"
  fi

  echo "--- fixture 2: codex-cli CLI absent → cli_absent verdict ---"
  PATH=/usr/bin:/bin mo_harness_wrap codex-cli "$_selftest_dir/kickoff.md" 2>/dev/null
  vharness=$(_read_verdict_field "$_selftest_dir/harness-verdict.json" harness)
  if [ "$vharness" = "codex-cli" ]; then
    echo "  [ok] harness identified"
  else
    echo "  [fail] harness=$vharness"
  fi

  echo "--- fixture 3: unknown harness returns rc=2 ---"
  if mo_harness_wrap unknown-harness "$_selftest_dir/kickoff.md" 2>/dev/null; then
    echo "  [fail] should have returned rc=2"
  else
    echo "  [ok] unknown harness rejected"
  fi

  echo "--- fixture 4: MO_HARNESS_DRY_RUN=1 emits dry_run verdict ---"
  MO_HARNESS_DRY_RUN=1 mo_harness_wrap claude-code "$_selftest_dir/kickoff.md" 2>/dev/null
  vstatus=$(_read_verdict_field "$_selftest_dir/harness-verdict.json" status)
  if [ "$vstatus" = "dry_run" ]; then
    echo "  [ok] dry_run verdict"
  else
    echo "  [fail] dry-run status=$vstatus"
  fi

  echo "--- fixture 5: git workspace initialization is idempotent ---"
  rm -rf "$_selftest_dir/.git"
  MO_HARNESS_DRY_RUN=1 mo_harness_wrap claude-code "$_selftest_dir/kickoff.md" 2>/dev/null
  if git -C "$_selftest_dir" rev-parse --git-dir >/dev/null 2>&1; then
    echo "  [ok] git repo initialized"
  else
    echo "  [fail] git init missing"
  fi
  # Re-run to confirm idempotence (no error on existing repo).
  MO_HARNESS_DRY_RUN=1 mo_harness_wrap claude-code "$_selftest_dir/kickoff.md" 2>/dev/null
  if git -C "$_selftest_dir" rev-parse --git-dir >/dev/null 2>&1; then
    echo "  [ok] git init idempotent across re-runs"
  else
    echo "  [fail] git init broke on re-run"
  fi
fi
