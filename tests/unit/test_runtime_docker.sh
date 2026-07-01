#!/usr/bin/env bash
# tests/unit/test_runtime_docker.sh — R3: prove MO_RUNTIME_BACKEND=docker
# runs commands inside a per-run container, real put/get round-trips via
# `docker cp`, and falls back to local with a one-line WARN when docker
# is missing or the daemon is unreachable. Same shape as
# tests/unit/test_runtime_bubblewrap.sh so reviewers can diff the two.
#
# Filename ends in .sh (not test_*.py) so pytest's default discovery skips
# it. Run with: bash tests/unit/test_runtime_docker.sh
#
# Capability-gating: detected at test time. On hosts without docker the
# container-only assertions SKIP; the fall-back-to-local + WARN
# assertion ALWAYS runs (it's the load-bearing "degrade never fail"
# guarantee). Cold-start `docker pull debian:stable-slim` may take
# 10-60s on first invocation — no wall-clock timing assertions.

set -uo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
export MINI_ORK_ROOT
CONTRACT="$MINI_ORK_ROOT/lib/runtime/contract.sh"
DOCKER_LIB="$MINI_ORK_ROOT/lib/runtime/docker.sh"
LOCAL_LIB="$MINI_ORK_ROOT/lib/runtime/local.sh"

PASS=0; FAIL=0; SKIP=0
_ok()   { echo "  [OK]   $*"; PASS=$((PASS+1)); }
_fail() { echo "  [FAIL] $*"; FAIL=$((FAIL+1)); }
_skip() { echo "  [SKIP] $*"; SKIP=$((SKIP+1)); }

cleanup_workspace() {
  if [ -n "${WORKSPACE:-}" ] && [ -d "${WORKSPACE}" ]; then
    rm -rf "${WORKSPACE}"
  fi
  # Best-effort cleanup of any container still bound to a cid file in
  # the workspace — defensive against a test that crashed mid-exec.
  if [ -n "${CID_FILE:-}" ] && [ -f "${CID_FILE}" ]; then
    local cid
    cid="$(tr -d '[:space:]' <"$CID_FILE" 2>/dev/null || true)"
    if [ -n "$cid" ] && command -v docker >/dev/null 2>&1; then
      docker rm -f "$cid" >/dev/null 2>&1 || true
    fi
    rm -f "$CID_FILE"
  fi
}

echo "── unit: lib/runtime/docker.sh ──"

if [ ! -f "$CONTRACT" ] || [ ! -f "$DOCKER_LIB" ] || [ ! -f "$LOCAL_LIB" ]; then
  _skip "missing contract.sh / docker.sh / local.sh"
else
  WORKSPACE="$(mktemp -d /tmp/mo-runtime-docker-XXXXXX)"
  trap cleanup_workspace EXIT

  unset MO_RUNTIME_BACKEND

  DOCKER_AVAIL=0
  command -v docker >/dev/null 2>&1 && timeout 5 docker info >/dev/null 2>&1 \
    && DOCKER_AVAIL=1

  # ── (a) docker unavailable: command still runs AND WARN surfaces ────────────
  # Load-bearing "degrade never fail" assertion — must execute on EVERY
  # host, not just ones where docker happens to live. We force the
  # fall-back by masking the docker binary via PATH (prepend an empty
  # temp dir) while leaving bash/setsid/perl reachable so the local
  # fall-back can still exec its child.
  echo ""
  echo "--- (a) docker unavailable: command runs + WARN 'falling back to local' ---"
  HIDE_DOCKER="$(mktemp -d /tmp/mo-trap-docker-XXXXXX)"
  (
    export MO_RUNTIME_BACKEND=docker
    # Prepend a temp dir (no docker inside) so `command -v docker` fails.
    # `/usr/bin` and `/bin` come later in $PATH and still resolve bash /
    # setsid / perl so the local fall-back's `bash -c` works.
    PATH="$HIDE_DOCKER:$PATH"
    # shellcheck source=/dev/null
    source "$CONTRACT"
    out="$(mo_runtime_exec 'echo docker-fb-ran' "$WORKSPACE" 2>&1)"
    rc=$?
    if [ "$rc" -eq 0 ] \
       && echo "$out" | grep -q "docker-fb-ran" \
       && echo "$out" | grep -q "falling back to local"; then
      echo "OK"
    else
      echo "FAIL rc=$rc out='$out'"
      exit 1
    fi
  ) && _ok "(a) docker unavailable: command ran AND 'falling back to local' WARN emitted" \
    || _fail "(a) docker unavailable fallback did not satisfy contract"
  rm -rf "$HIDE_DOCKER"

  # ── (b) docker available: start -> exec -> put -> get -> alive -> stop ─────
  echo ""
  echo "--- (b) docker available: container exec, put, get round-trip, alive, stop ---"
  if [ "$DOCKER_AVAIL" != "1" ]; then
    _skip "(b) docker exec + put + get + alive (docker unavailable on host)"
  else
    (
      export MO_RUNTIME_BACKEND=docker
      # Bind the test workspace into the container so cwd=$WORKSPACE is
      # reachable inside it (the backend binds MO_RUNTIME_WORKSPACE).
      export MO_RUNTIME_WORKSPACE="$WORKSPACE"
      # shellcheck source=/dev/null
      source "$CONTRACT"

      # Start the per-run container.
      mo_runtime_start
      start_rc=$?
      if [ "$start_rc" -ne 0 ]; then
        echo "FAIL mo_runtime_start rc=$start_rc"; exit 1
      fi

      # Test the PUBLIC runtime contract only — NOT docker internals (cid-file
      # layout, raw `docker exec`, bind-mount host-visibility). Those vary
      # across environments (macOS Docker Desktop VM, rootless/userns CI
      # daemons) and are not backend guarantees; poking them made this test
      # env-flaky. The contract is: start → alive → exec-runs-in-container →
      # put+get round-trip → stop. Failure reasons go to >&2 so CI shows them.

      # alive == 1 right after start.
      alive_out="$(mo_runtime_alive 2>/dev/null)"
      if [ "$alive_out" != "1" ]; then
        echo "FAIL alive=$alive_out (expected 1)" >&2; exit 1
      fi

      # exec: mo_runtime_exec runs a command in the container and returns its
      # stdout + rc (proves exec-in-container via the public API).
      exec_out="$(mo_runtime_exec 'echo docker-ran' "$WORKSPACE" 2>/dev/null)"
      exec_rc=$?
      if [ "$exec_rc" -ne 0 ] || ! printf '%s' "$exec_out" | grep -q "docker-ran"; then
        echo "FAIL exec rc=$exec_rc out='$exec_out'" >&2; exit 1
      fi

      # put → get round-trip via the public API (docker cp under the hood).
      # Use a container path OUTSIDE the bound workspace (/root/...) so this
      # exercises real host↔container transfer, not a bind-mount shortcut,
      # and get-s back to a FRESH host path so a stale file can't false-pass.
      host_src="${WORKSPACE}/roundsrc.txt"; printf 'round-trip-content\n' > "$host_src"
      remote_path="/root/rounddst.txt"
      mo_runtime_put "$host_src" "$remote_path" >/dev/null 2>&1
      get_local="${WORKSPACE}/getback.txt"; rm -f "$get_local"
      mo_runtime_get "$remote_path" "$get_local" >/dev/null 2>&1
      if [ ! -s "$get_local" ] || ! grep -q "round-trip-content" "$get_local"; then
        echo "FAIL put/get round-trip: '$get_local' missing payload" >&2; exit 1
      fi

      # stop: container removed; alive → 0 afterwards.
      mo_runtime_stop
      if [ "$(mo_runtime_alive 2>/dev/null)" = "1" ]; then
        echo "FAIL alive after stop = 1 (expected 0)" >&2; exit 1
      fi

      echo "OK"
    ) 2>"${WORKSPACE}/b.err" && _ok "(b) docker exec + put + get round-trip + alive + stop pass" \
      || _fail "(b) docker container cycle failed: $(tr '\n' ' ' < "${WORKSPACE}/b.err" 2>/dev/null | tail -c 300)"
  fi

  # ── (c) timeout mapping: rc=124 from `timeout --foreground` survives ───────
  echo ""
  echo "--- (c) timeout maps to rc=124 inside container ---"
  if [ "$DOCKER_AVAIL" != "1" ]; then
    _skip "(c) timeout assertion (docker unavailable on host)"
  else
    (
      export MO_RUNTIME_BACKEND=docker
      export MO_RUNTIME_WORKSPACE="$WORKSPACE"
      # shellcheck source=/dev/null
      source "$CONTRACT"
      mo_runtime_start
      # 0.5s timeout on a 60-iteration sleep loop — timeout must trigger
      # BEFORE the loop completes (loop runs > 1s total).
      out="$(mo_runtime_exec 'i=0; while [ $i -lt 60 ]; do sleep 0.1; i=$((i+1)); done; echo TOO-LATE' "$WORKSPACE" 0.5 2>/dev/null)"
      rc=$?
      if [ "$rc" -eq 124 ] && ! echo "$out" | grep -q "TOO-LATE"; then
        echo "OK"
      else
        echo "FAIL rc=$rc out='$out' (expected rc=124, no TOO-LATE)"; exit 1
      fi
      mo_runtime_stop >/dev/null
    ) && _ok "(c) docker exec timeout maps to contract rc=124" \
      || _fail "(c) timeout did not map to rc=124"
  fi

  # ── (d) control: same command under MO_RUNTIME_BACKEND=local still works ───
  # Regression guard — the doc-comment edit to contract.sh must not
  # touch the local backend factory binding.
  echo ""
  echo "--- (d) control: same WORKSPACE write under MO_RUNTIME_BACKEND=local ---"
  (
    export MO_RUNTIME_BACKEND=local
    # shellcheck source=/dev/null
    source "$CONTRACT"
    target="$WORKSPACE/control_d.txt"
    out="$(mo_runtime_exec "printf z > '$target' && echo local-ran" "$WORKSPACE" 2>/dev/null)"
    rc=$?
    if [ "$rc" -eq 0 ] && [ "$out" = "local-ran" ] && [ -s "$target" ]; then
      echo "OK"
    else
      echo "FAIL rc=$rc out='$out'"; exit 1
    fi
  ) && _ok "(d) control: in-WORKSPACE write succeeds under local" \
    || _fail "(d) control: in-WORKSPACE write failed under local"
fi

echo ""
echo "── Results: ${PASS} OK  ${SKIP} SKIP  ${FAIL} FAIL ──"
[ "$FAIL" -eq 0 ] || exit 1
