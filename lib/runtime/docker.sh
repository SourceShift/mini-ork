#!/usr/bin/env bash
# lib/runtime/docker.sh — docker-managed-runtime backend for lib/runtime/contract.sh.
#
# Process-isolation backend: each run gets its own ephemeral container
# (`docker run -d ... debian:stable-slim`) with the workspace bind-mounted
# at the same path inside the container. Real put/get cross the
# container-host boundary via `docker cp`, so file round-trips round-trip
# (not "the host already saw this because it's a bind-mount").
#
# Opt-in only: default backend stays 'local'. Activated by:
#   MO_RUNTIME_BACKEND=docker source lib/runtime/contract.sh
#
# Degrade, never fail: if `docker` is missing OR `docker info` errors
# (daemon down, perm denied, hung), the backend emits a one-line WARN
# and dispatches to the local backend — mirroring the fall-back pattern
# in lib/runtime/bubblewrap.sh and lib/sandbox/{modal,daytona}.sh.
# Never abort a run because isolation is unavailable.
#
# Concurrency: the container-id lives at
#   ${MINI_ORK_RUN_DIR:-/tmp/mo-runtime-docker-$$-$RANDOM}.cid
# so ~29 simultaneous MO_RUNTIME_BACKEND=docker runs don't clobber each
# other's cid (and don't accidentally `docker rm` a sibling container).
#
# Default image: MO_RUNTIME_DOCKER_IMAGE default 'debian:stable-slim'.
# Small (~75 MB), ships /bin/bash, cold `docker pull` is paid once per
# host. Test must NOT assert wall-clock timing — see risk_notes.
#
# Reference: ruflo's modal/daytona sandbox dispatch shape; same "warn +
# local" degradation contract.

# `set -u` (strict) but NOT `set -e`: callers depend on rc propagation;
# bogus paths and missing files should surface as rc != 0 from the
# spawned command, not abort the harness mid-loop. Same rationale as
# local.sh and bubblewrap.sh.
set -u
set -o pipefail 2>/dev/null || true

# Source local.sh once at load-time so the fall-back path can call
# mo_runtime_local_exec without re-sourcing on every invocation. Bash
# redefines functions harmlessly on re-source, but loading once avoids
# the per-call fs hit and prevents any drift between local.sh revisions
# that docker.sh's behaviour depends on.
_MO_DOCKER_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "$_MO_DOCKER_LIB_DIR/local.sh"

_mo_runtime_docker_log() {
  local _level="$1"; shift
  printf '{"level":"%s","subsystem":"runtime.docker","ts":"%s","msg":"%s"}\n' \
    "$_level" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >&2
}

# Per-run path resolver for the cid file. $$ + $RANDOM so ~29 concurrent
# runs each get a unique suffix; PID catches long-running idle sessions
# that wrap into a new random epoch.
_mo_runtime_docker_cid_path() {
  local _dir="${MINI_ORK_RUN_DIR:-/tmp}"
  printf '%s/mo-runtime-docker-%s-%s.cid\n' "$_dir" "$$" "$RANDOM"
}

# docker_available: returns 0 iff `docker` is on PATH AND `docker info`
# exits 0 within 5 seconds. The 5s ceiling matters because `docker info`
# can hang on a misconfigured daemon (broken context, auth socket gone);
# letting it stall a node dispatch would burn a 10s+ budget per run.
docker_available() {
  command -v docker >/dev/null 2>&1 || return 1
  timeout 5 docker info >/dev/null 2>&1 || return 1
  return 0
}

mo_runtime_docker_start() {
  if ! docker_available; then
    _mo_runtime_docker_log "warn" "docker unavailable, falling back to local"
    mo_runtime_local_start
    return $?
  fi

  local cid_file
  cid_file="$(_mo_runtime_docker_cid_path)"
  export MO_RUNTIME_DOCKER_CID_FILE="$cid_file"

  local image="${MO_RUNTIME_DOCKER_IMAGE:-debian:stable-slim}"
  local name
  name="mo-runtime-docker-${$}-${RANDOM}"
  # Re-using a stale name from a prior crashed run would let `docker run`
  # fail silently with "name in use" — sweep first. Best-effort; failure
  # here is not actionable (the run is going to fail at `docker run` with
  # a clearer message either way).
  docker rm -f "$name" >/dev/null 2>&1 || true

  # Bind-mount the RUN WORKSPACE (the cwd that exec will actually use) at the
  # SAME host path inside the container, so cwd-relative paths Just Work. The
  # exec cwd is the node's edit surface (MO_TARGET_CWD) or the run dir — NOT
  # necessarily $PWD — so binding $PWD alone left /tmp/<workspace> unreachable
  # (docker exec -w → chdir ENOENT). Resolve precedence:
  #   MO_RUNTIME_WORKSPACE → MO_TARGET_CWD → MINI_ORK_RUN_DIR → $PWD.
  # Record it so exec can verify a requested cwd is actually reachable.
  local _ws="${MO_RUNTIME_WORKSPACE:-${MO_TARGET_CWD:-${MINI_ORK_RUN_DIR:-$PWD}}}"
  export MO_RUNTIME_DOCKER_WS="$_ws"
  # Mount RO would block the agent's writes — bind (rw). `--security-opt
  # seccomp=unconfined` is paranoia for dev hosts running colima.
  local rc=0
  docker run -d \
    --name "$name" \
    --label "mo-runtime=docker" \
    --label "mo-runtime-pid=${$}" \
    --security-opt seccomp=unconfined \
    -v "$_ws:$_ws" \
    -w "$_ws" \
    "$image" \
    sleep infinity >"$cid_file" 2>&1 || rc=$?

  if [ "$rc" -ne 0 ] || [ ! -s "$cid_file" ]; then
    _mo_runtime_docker_log "warn" "docker run failed rc=$rc; falling back to local"
    mo_runtime_local_start
    return $?
  fi

  # The cid file currently holds the full container ID written by
  # `docker run` to stdout. Persist it (overwrite with trimmed value)
  # so subsequent exec/put/get call this same path.
  local cid
  cid="$(tr -d '[:space:]' <"$cid_file")"
  printf '%s\n' "$cid" >"$cid_file"
  return 0
}

mo_runtime_docker_stop() {
  if ! docker_available; then
    mo_runtime_local_stop
    return $?
  fi

  local cid_file="${MO_RUNTIME_DOCKER_CID_FILE:-$(_mo_runtime_docker_cid_path)}"
  if [ ! -f "$cid_file" ]; then
    return 0
  fi

  local cid
  cid="$(tr -d '[:space:]' <"$cid_file" 2>/dev/null)"
  if [ -n "$cid" ]; then
    # Best-effort: a stale cid (container already gone) is fine to skip.
    docker rm -f "$cid" >/dev/null 2>&1 || true
  fi
  rm -f "$cid_file" 2>/dev/null || true
  unset MO_RUNTIME_DOCKER_CID_FILE
  return 0
}

mo_runtime_docker_alive() {
  if ! docker_available; then
    mo_runtime_local_alive
    return $?
  fi

  local cid_file="${MO_RUNTIME_DOCKER_CID_FILE:-$(_mo_runtime_docker_cid_path)}"
  if [ ! -f "$cid_file" ]; then
    printf '0\n'
    return 1
  fi

  local cid
  cid="$(tr -d '[:space:]' <"$cid_file" 2>/dev/null)"
  if [ -z "$cid" ]; then
    printf '0\n'
    return 1
  fi

  local running
  running="$(docker inspect --format '{{.State.Running}}' "$cid" 2>/dev/null)"
  if [ "$running" = "true" ]; then
    printf '1\n'
    return 0
  fi
  printf '0\n'
  return 1
}

mo_runtime_docker_exec() {
  local cmd="${1-}"
  local cwd="${2-}"
  local timeout_s="${3-0}"
  local env_assigns=("${@:4}")

  if ! docker_available; then
    _mo_runtime_docker_log "warn" "docker unavailable, falling back to local"
    mo_runtime_local_exec "$cmd" "$cwd" "$timeout_s" "${env_assigns[@]+"${env_assigns[@]}"}"
    return $?
  fi

  local cid_file="${MO_RUNTIME_DOCKER_CID_FILE:-$(_mo_runtime_docker_cid_path)}"
  if [ ! -f "$cid_file" ]; then
    _mo_runtime_docker_log "warn" "no cid file ($cid_file); falling back to local"
    mo_runtime_local_exec "$cmd" "$cwd" "$timeout_s" "${env_assigns[@]+"${env_assigns[@]}"}"
    return $?
  fi
  local cid
  cid="$(tr -d '[:space:]' <"$cid_file" 2>/dev/null)"
  if [ -z "$cid" ]; then
    _mo_runtime_docker_log "warn" "empty cid; falling back to local"
    mo_runtime_local_exec "$cmd" "$cwd" "$timeout_s" "${env_assigns[@]+"${env_assigns[@]}"}"
    return $?
  fi

  # Validate cwd if supplied: `docker exec -w` requires the path to exist
  # INSIDE the container, which is true ONLY for paths under the bound
  # workspace (MO_RUNTIME_DOCKER_WS). A host-only existence check is not
  # enough — /tmp/<x> exists on the host but is unreachable in the container
  # unless it was the bind target — so verify the cwd is the workspace or a
  # descendant of it; otherwise fall back to local (the agent should not see
  # an unreachable-cwd as a hard backend failure).
  if [ -n "$cwd" ]; then
    local _ws="${MO_RUNTIME_DOCKER_WS:-}"
    if [ ! -d "$cwd" ]; then
      _mo_runtime_docker_log "warn" "cwd '$cwd' missing on host; falling back to local"
      mo_runtime_local_exec "$cmd" "$cwd" "$timeout_s" "${env_assigns[@]+"${env_assigns[@]}"}"
      return $?
    fi
    if [ -n "$_ws" ] && [ "$cwd" != "$_ws" ] && [ "${cwd#"$_ws"/}" = "$cwd" ]; then
      _mo_runtime_docker_log "warn" "cwd '$cwd' is outside bound workspace '$_ws' (not mounted in container); falling back to local"
      mo_runtime_local_exec "$cmd" "$cwd" "$timeout_s" "${env_assigns[@]+"${env_assigns[@]}"}"
      return $?
    fi
  fi

  # `timeout --foreground` is mandatory: plain `timeout` backgrounds the
  # child, which leaks the docker-exec wrapper when the dispatch moves
  # on. Mapping rc=124 to the contract's 124 keeps parity with local.sh's
  # timeout semantics.
  local timeout_arg=""
  if LC_ALL=C awk 'BEGIN { exit !('"${timeout_s:-0}"' > 0) }'; then
    timeout_arg="timeout --foreground ${timeout_s}"
  fi

  local docker_args=(exec -i)
  if [ -n "$cwd" ]; then
    docker_args+=(-w "$cwd")
  fi
  docker_args+=("$cid" bash -lc "$cmd")

  local outfile
  outfile="$(mktemp -t mo_runtime_docker.XXXXXX)"
  local rc=0

  # shellcheck disable=SC2086
  if [ "${#env_assigns[@]}" -gt 0 ]; then
    env "${env_assigns[@]}" $timeout_arg docker "${docker_args[@]}" >"$outfile" 2>&1
    rc=$?
  else
    $timeout_arg docker "${docker_args[@]}" >"$outfile" 2>&1
    rc=$?
  fi

  # Map timeout's 124 to the contract's 124 — already true, made explicit.
  if [ "$rc" -eq 124 ]; then
    rc=124
  fi

  cat "$outfile"
  rm -f "$outfile"
  return "$rc"
}

mo_runtime_docker_put() {
  local src="${1-}" dst="${2-}"
  if [ -z "$src" ] || [ -z "$dst" ]; then
    echo "mo_runtime_docker_put: src and dst required" >&2
    return 2
  fi

  if ! docker_available; then
    mo_runtime_local_put "$src" "$dst"
    return $?
  fi

  local cid_file="${MO_RUNTIME_DOCKER_CID_FILE:-$(_mo_runtime_docker_cid_path)}"
  if [ ! -f "$cid_file" ]; then
    mo_runtime_local_put "$src" "$dst"
    return $?
  fi
  local cid
  cid="$(tr -d '[:space:]' <"$cid_file" 2>/dev/null)"
  if [ -z "$cid" ]; then
    mo_runtime_local_put "$src" "$dst"
    return $?
  fi

  docker cp "$src" "$cid:$dst"
  printf '%s\n' "$dst"
}

mo_runtime_docker_get() {
  local src="${1-}" dst="${2-}"
  if [ -z "$src" ] || [ -z "$dst" ]; then
    echo "mo_runtime_docker_get: src and dst required" >&2
    return 2
  fi

  if ! docker_available; then
    mo_runtime_local_get "$src" "$dst"
    return $?
  fi

  local cid_file="${MO_RUNTIME_DOCKER_CID_FILE:-$(_mo_runtime_docker_cid_path)}"
  if [ ! -f "$cid_file" ]; then
    mo_runtime_local_get "$src" "$dst"
    return $?
  fi
  local cid
  cid="$(tr -d '[:space:]' <"$cid_file" 2>/dev/null)"
  if [ -z "$cid" ]; then
    mo_runtime_local_get "$src" "$dst"
    return $?
  fi

  docker cp "$cid:$src" "$dst"
  printf '%s\n' "$dst"
}
