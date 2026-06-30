#!/usr/bin/env bash
# lib/runtime/local.sh — local-host backend for lib/runtime/contract.sh.
#
# Models mini-swe-agent's LocalEnvironment._run: every command runs in its own
# process group, so a timeout can `kill -- -$pgid` to reap not just the child
# but every descendant (subshells, sleeps, redirects). No pseudo-terminal,
# no job control wrappers — the parent shell's $PWD is preserved.

# `set -u` (strict) but NOT `set -e`: callers depend on rc propagation; bogus
# paths and missing files should surface as rc != 0 from the spawned command,
# not abort the harness mid-loop.
set -u
set -o pipefail 2>/dev/null || true

# Pick a pgid-spawner at source-time. Array literal form preserves single-
# quoted perl source verbatim — unquoted $var word-splitting would otherwise
# pass the quotes as LITERAL characters to perl, breaking `-e`. The `setsid --`
# form is GNU-only; portable systems fall back to a perl one-liner that calls
# POSIX::setpgrp(0,0) before exec, making the child's PID equal to its PGID.
# Either way, `kill -- -<pid>` signals the whole group.
if command -v setsid >/dev/null 2>&1; then
  _MO_RUNTIME_LOCAL_SPAWNER=(setsid --)
else
  _MO_RUNTIME_LOCAL_SPAWNER=(perl -e 'use POSIX; POSIX::setpgid(0, 0); exec @ARGV;')
fi

mo_runtime_local_exec() {
  local cmd="${1-}"
  local cwd="${2-}"
  local timeout_s="${3-0}"
  # Anything position-4 onward is treated as KEY=VAL env passing. `${@:4}`
  # handles missing positions cleanly (returns empty if $# < 4), unlike a
  # `shift 3` which errors without shifting when $# < 3 — that exact bug
  # silently re-fed positional 1 back into env_assigns on shorter calls.
  local env_assigns=("${@:4}")

  # Build child prefix so cwd is honored INSIDE the child (don't mutate $PWD).
  local prefix=""
  if [ -n "${cwd}" ]; then
    prefix="cd $(printf '%q' "$cwd") || { echo \"mo_runtime_local_exec: cd failed: ${cwd}\" >&2; exit 126; }; "
  fi
  local full_cmd="${prefix}${cmd}"

  local outfile
  outfile="$(mktemp -t mo_runtime_local.XXXXXX)"
  local rc=0

  # Spawn in new pgid, capturing stdout+stderr to tmpfile. env_assigns passed
  # only when present (avoids `env` argv-position ambiguity under set -u).
  # NOTE: do NOT `disown $child_pid` — empirically, when combined with the
  # spawner's `setpgid(0,0)`, `disown` reorders the parent's stdout/stderr
  # FDs and the redirect-to-outfile is silently lost (output ends up nowhere).
  # The harness call chain is non-interactive (bash -c → wait), so SIGHUP
  # propagation is not a concern; `disown` was defensive over-engineering.
  local child_pid
  if [ "${#env_assigns[@]}" -gt 0 ]; then
    env "${env_assigns[@]}" "${_MO_RUNTIME_LOCAL_SPAWNER[@]}" bash -c "$full_cmd" >"$outfile" 2>&1 &
    child_pid=$!
  else
    "${_MO_RUNTIME_LOCAL_SPAWNER[@]}" bash -c "$full_cmd" >"$outfile" 2>&1 &
    child_pid=$!
  fi

  # Timer-based wait. Polling at 50ms resolution: 6 ticks per 300ms timeout
  # gives plenty of head-room without burning CPU. Sub-second timeouts need
  # fractional seconds — `date +%s` is whole-second only and would silently
  # skip 0.3s timeouts (the deadline straddles a second boundary).
  #
  # Pitfalls avoided here:
  # - bash `[ N -gt 0 ]` is integer-only — fractional timeouts take the
  #   `else` branch. Use awk (with LC_ALL=C so decimals parse as dots).
  # - `EPOCHREALTIME` is locale-aware — on macOS bash 5.3 the decimal
  #   separator is a comma, which makes awk syntax-error. Stick with
  #   `date +%s.%N` (GNU coreutils, present on macOS 14) for predictable
  #   dot-decimal output.
  if LC_ALL=C awk 'BEGIN { exit !('"${timeout_s:-0}"' > 0) }'; then
    local deadline_s
    deadline_s="$(LC_ALL=C awk 'BEGIN { printf "%.6f", '$(date +%s.%N)' + '"${timeout_s}"' }')"
    while kill -0 "$child_pid" 2>/dev/null; do
      local now_s
      now_s="$(date +%s.%N)"
      if LC_ALL=C awk 'BEGIN { exit !('"$now_s"' >= '"$deadline_s"') }'; then
        # TERM the whole group, grace 500ms (10 polls × 50ms), then KILL.
        kill -TERM -- "-${child_pid}" 2>/dev/null \
          || kill -TERM "-${child_pid}" 2>/dev/null || true
        local grace=0
        while kill -0 "$child_pid" 2>/dev/null && [ "$grace" -lt 10 ]; do
          sleep 0.05
          grace=$((grace + 1))
        done
        kill -KILL -- "-${child_pid}" 2>/dev/null \
          || kill -KILL "-${child_pid}" 2>/dev/null || true
        wait "$child_pid" 2>/dev/null || true
        rc=124
        break
      fi
      sleep 0.05
    done
    if [ "$rc" -ne 124 ]; then
      wait "$child_pid" 2>/dev/null
      rc=$?
    fi
  else
    # No timeout: block until child exits. RC propagation is the contract.
    wait "$child_pid"
    rc=$?
  fi

  cat "$outfile"
  rm -f "$outfile"
  return "$rc"
}

mo_runtime_local_put() {
  # Same-host: cp. Mirrors the contract's "remote_path" naming without an
  # actual remote target.
  local src="${1-}" dst="${2-}"
  if [ -z "$src" ] || [ -z "$dst" ]; then
    echo "mo_runtime_local_put: src and dst required" >&2
    return 2
  fi
  cp -- "$src" "$dst" 2>/dev/null || cp "$src" "$dst"
  printf '%s\n' "$dst"
}

mo_runtime_local_get() {
  local src="${1-}" dst="${2-}"
  if [ -z "$src" ] || [ -z "$dst" ]; then
    echo "mo_runtime_local_get: src and dst required" >&2
    return 2
  fi
  cp -- "$src" "$dst" 2>/dev/null || cp "$src" "$dst"
  printf '%s\n' "$dst"
}

mo_runtime_local_start() { return 0; }
mo_runtime_local_stop() { return 0; }
mo_runtime_local_alive() { printf '1\n'; return 0; }
