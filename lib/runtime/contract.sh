#!/usr/bin/env bash
# lib/runtime/contract.sh — single-source-of-truth interface for host execution.
#
# Backends live under lib/runtime/<name>.sh and expose functions named
# `mo_runtime_<name>_<op>`. The six contract entry points below are thin
# forwarders to whichever backend is currently active. Swapping backends
# is a single `MO_RUNTIME_BACKEND=<name> source lib/runtime/contract.sh`
# away — no `unset`, no monkey-patching.
#
# Contract (all backends MUST implement):
#   mo_runtime_exec <cmd> [cwd] [timeout] [env_kv]...
#     Echo command stdout. Return command's exit code.
#     `cwd` may be empty (= inherit). `timeout` is seconds; 0 = wait forever.
#     Extra args (after cmd/cwd/timeout) are passed as KEY=VAL env to the child.
#   mo_runtime_put <local_path> <remote_path>
#     Copy local -> remote (within the same backend's view). Echoes remote.
#   mo_runtime_get <remote_path> <local_path>
#     Copy remote -> local. Echoes local.
#   mo_runtime_start
#     Begin a session (no-op for backends without one).
#   mo_runtime_stop
#     End a session (no-op for backends without one).
#   mo_runtime_alive
#     Print "1" if the session is up, "0" otherwise. Exit code is the same.
#
# Activation: the factory below runs at source-time. There is no opt-out.

# Locate this lib's directory even when sourced through a symlink/path munging.
# shellcheck source=/dev/null
_MO_RUNTIME_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export _MO_RUNTIME_LIB_DIR

# Public: load a backend by name. Sources the backend file into the CURRENT
# shell so function definitions persist into the caller's environment.
# Required so tests/Recipes can `MO_RUNTIME_BACKEND=foo source contract.sh`
# and immediately call the contract symbols. Side-effect: defines the
# six mo_runtime_<op> forwarders below.
mo_runtime_load_backend() {
  local name="${1:-${MO_RUNTIME_BACKEND:-local}}"
  local path="${_MO_RUNTIME_LIB_DIR}/${name}.sh"
  if [ ! -f "$path" ]; then
    echo "mo_runtime_load_backend: unknown backend '${name}' (expected ${_MO_RUNTIME_LIB_DIR}/${name}.sh)" >&2
    return 2
  fi
  # shellcheck source=/dev/null
  source "$path"
  export MO_RUNTIME_BACKEND="${name}"
}

# Forwarders. Each resolves the backend-prefixed symbol on every call so a
# caller can `eval`-redefine a single operation without unsetting the rest.
mo_runtime_exec() {
  local _op=exec _backend="${MO_RUNTIME_BACKEND:-local}"
  "mo_runtime_${_backend}_${_op}" "$@"
}
mo_runtime_put() {
  local _op=put _backend="${MO_RUNTIME_BACKEND:-local}"
  "mo_runtime_${_backend}_${_op}" "$@"
}
mo_runtime_get() {
  local _op=get _backend="${MO_RUNTIME_BACKEND:-local}"
  "mo_runtime_${_backend}_${_op}" "$@"
}
mo_runtime_start() {
  local _op=start _backend="${MO_RUNTIME_BACKEND:-local}"
  "mo_runtime_${_backend}_${_op}" "$@"
}
mo_runtime_stop() {
  local _op=stop _backend="${MO_RUNTIME_BACKEND:-local}"
  "mo_runtime_${_backend}_${_op}" "$@"
}
mo_runtime_alive() {
  local _op=alive _backend="${MO_RUNTIME_BACKEND:-local}"
  "mo_runtime_${_backend}_${_op}" "$@"
}

# Activate the configured backend at source-time. A bogus backend value
# (e.g. MO_RUNTIME_BACKEND=bogus) MUST surface as a clear stderr + non-zero
# exit, so a wrong env var fails loudly instead of silently no-oping.
mo_runtime_load_backend "${MO_RUNTIME_BACKEND:-local}"
