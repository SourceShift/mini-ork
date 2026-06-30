#!/usr/bin/env bash
# lib/runtime/bubblewrap.sh — bubblewrap sandbox backend for lib/runtime/contract.sh.
#
# Filesystem-isolation backend: only the cwd passed to mo_runtime_exec is
# read-write inside the sandbox; everything else (/usr, /bin, /lib, /lib64,
# /etc) is read-only via --ro-bind. The agent cannot reach or modify a
# sibling repo's .git — structurally preventing the cross-repo HEAD-clobber
# corruption pattern from recurring.
#
# Opt-in only: default backend stays 'local'. Activated by:
#   MO_RUNTIME_BACKEND=bubblewrap source lib/runtime/contract.sh
#
# Degrade, never fail: if bwrap is missing or the kernel is not Linux
# (e.g. macOS dev hosts), the backend emits a one-line WARN and dispatches
# to the local backend — mirroring the fall-back pattern in
# lib/sandbox/{modal,daytona}.sh. Never abort a run because isolation
# is unavailable.
#
# Reference: mini-swe-agent's BubblewrapEnvironment — same flag shape, same
# --unshare-user-try portability trick.

# `set -u` (strict) but NOT `set -e`: callers depend on rc propagation; bogus
# paths and missing files should surface as rc != 0 from the spawned command,
# not abort the harness mid-loop. Same rationale as local.sh.
set -u
set -o pipefail 2>/dev/null || true

# Source local.sh once at load-time so the fall-back path can call
# mo_runtime_local_exec without re-sourcing on every invocation. Bash
# redefines functions harmlessly on re-source, but loading once avoids
# the per-call fs hit and prevents any drift between local.sh revisions
# that bubblewrap.sh's behaviour depends on (e.g. pgid-kill semantics).
_MO_BWRAP_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "$_MO_BWRAP_LIB_DIR/local.sh"

# Pick a pgid-spawner at source-time. Mirrors local.sh exactly: array
# literal preserves the single-quoted perl source verbatim — unquoted
# $var word-splitting would pass the quotes as LITERAL characters to
# perl, breaking `-e`. Either form gives a child whose PID equals its
# PGID, so `kill -- -<pid>` signals the whole group on timeout.
if command -v setsid >/dev/null 2>&1; then
  _MO_RUNTIME_BUBBLEWRAP_SPAWNER=(setsid --)
else
  _MO_RUNTIME_BUBBLEWRAP_SPAWNER=(perl -e 'use POSIX; POSIX::setpgid(0, 0); exec @ARGV;')
fi

_mo_runtime_bubblewrap_log() {
  local _level="$1"; shift
  printf '{"level":"%s","subsystem":"runtime.bubblewrap","ts":"%s","msg":"%s"}\n' \
    "$_level" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >&2
}

# bubblewrap_available: returns 0 iff bwrap is on PATH AND the kernel is
# Linux. On Darwin (macOS), BSDs, or WSL without user-ns, returns 1 and
# the caller falls back to local with a WARN. Tested by being invoked
# from mo_runtime_bubblewrap_exec at the top of every dispatch.
bubblewrap_available() {
  command -v bwrap >/dev/null 2>&1 || return 1
  [ "$(uname -s 2>/dev/null)" = "Linux" ] || return 1
  return 0
}

mo_runtime_bubblewrap_exec() {
  local cmd="${1-}"
  local cwd="${2-}"
  local timeout_s="${3-0}"
  local env_assigns=("${@:4}")

  if ! bubblewrap_available; then
    _mo_runtime_bubblewrap_log "warn" "bubblewrap unavailable, falling back to local"
    mo_runtime_local_exec "$cmd" "$cwd" "$timeout_s" "${env_assigns[@]+"${env_assigns[@]}"}"
    return $?
  fi

  # Build bwrap args. Each --ro-bind target is conditional: bwrap exits
  # non-zero when handed a nonexistent host path, so /lib64 / etc must be
  # guarded. --unshare-user-try (vs --unshare-user) gracefully degrades on
  # kernels that lack user-ns support (some hardened CI runners) instead
  # of aborting the run — matches mini-swe-agent's BubblewrapEnvironment.
  local bwrap_args=(--unshare-user-try --tmpfs /tmp --proc /proc --dev /dev --new-session)
  local _p
  for _p in /usr /bin /lib /lib64 /etc; do
    if [ -d "$_p" ]; then
      bwrap_args+=(--ro-bind "$_p" "$_p")
    fi
  done

  # The cwd passed in IS the writable surface. Empty cwd falls back to
  # tmpfs /tmp inside the sandbox (an explicit, lossy degradation) so
  # the contract's "cwd may be empty (= inherit)" semantics still hold —
  # the agent sees /tmp, not the host's real /tmp.
  local mount_target
  if [ -n "${cwd}" ]; then
    mount_target="$cwd"
  else
    _mo_runtime_bubblewrap_log "warn" "no cwd provided; using tmpfs /tmp as workspace"
    mount_target="/tmp"
  fi

  # --bind (not --ro-bind) so the agent can write inside $WORKSPACE.
  # --chdir lands the child inside that mount. The inner cd is belt-and-
  # braces for the case where mount_target is /tmp (a symlink on macOS,
  # tmpfs here) — chdir'ing by the canonicalized path inside the child
  # ensures writes really do land on the writable bind.
  bwrap_args+=(--bind "$mount_target" "$mount_target" --chdir "$mount_target")
  local inner_cmd="cd $(printf '%q' "$mount_target") || { echo \"mo_runtime_bubblewrap_exec: cd failed: ${mount_target}\" >&2; exit 126; }; ${cmd}"
  bwrap_args+=(-- bash -c "$inner_cmd")

  local outfile
  outfile="$(mktemp -t mo_runtime_bubblewrap.XXXXXX)"
  local rc=0

  # Spawn bwrap in its own pgid (same model as local.sh) so the timeout
  # path can `kill -- -$pgid` and reap bwrap + every descendant. env_assigns
  # passed only when present (avoids `env` argv-position ambiguity under
  # set -u).
  local child_pid
  if [ "${#env_assigns[@]}" -gt 0 ]; then
    env "${env_assigns[@]}" "${_MO_RUNTIME_BUBBLEWRAP_SPAWNER[@]}" bwrap "${bwrap_args[@]}" >"$outfile" 2>&1 &
    child_pid=$!
  else
    "${_MO_RUNTIME_BUBBLEWRAP_SPAWNER[@]}" bwrap "${bwrap_args[@]}" >"$outfile" 2>&1 &
    child_pid=$!
  fi

  # Timer-based wait, identical to local.sh semantics: 50ms cadence, LC_ALL=C
  # so awk parses decimals as dots (locale-aware EPOCHREALTIME mis-parses
  # on macOS bash 5.3), TERM-then-KILL grace window. If the contract changes
  # for the local backend, mirror the change here.
  if LC_ALL=C awk 'BEGIN { exit !('"${timeout_s:-0}"' > 0) }'; then
    local deadline_s
    deadline_s="$(LC_ALL=C awk 'BEGIN { printf "%.6f", '$(date +%s.%N)' + '"${timeout_s}"' }')"
    while kill -0 "$child_pid" 2>/dev/null; do
      local now_s
      now_s="$(date +%s.%N)"
      if LC_ALL=C awk 'BEGIN { exit !('"$now_s"' >= '"$deadline_s"') }'; then
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
    wait "$child_pid"
    rc=$?
  fi

  cat "$outfile"
  rm -f "$outfile"
  return "$rc"
}

# put / get: bubblewrap's isolation is per-exec, so cp straight to/from
# the workspace is the same-view semantics as local.sh. Mirrors the
# "remote_path" naming even though there is no remote.
mo_runtime_bubblewrap_put() {
  local src="${1-}" dst="${2-}"
  if [ -z "$src" ] || [ -z "$dst" ]; then
    echo "mo_runtime_bubblewrap_put: src and dst required" >&2
    return 2
  fi
  cp -- "$src" "$dst" 2>/dev/null || cp "$src" "$dst"
  printf '%s\n' "$dst"
}

mo_runtime_bubblewrap_get() {
  local src="${1-}" dst="${2-}"
  if [ -z "$src" ] || [ -z "$dst" ]; then
    echo "mo_runtime_bubblewrap_get: src and dst required" >&2
    return 2
  fi
  cp -- "$src" "$dst" 2>/dev/null || cp "$src" "$dst"
  printf '%s\n' "$dst"
}

mo_runtime_bubblewrap_start() { return 0; }
mo_runtime_bubblewrap_stop() { return 0; }
mo_runtime_bubblewrap_alive() { printf '1\n'; return 0; }