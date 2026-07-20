#!/usr/bin/env bash
# runtime-select.sh — bash→Python runtime cutover switch.
#
# The strangler-fig migration ports every bin/mini-ork-* entrypoint to
# mini_ork/ported/mini_ork_<name>.py (bin/mini-ork → mini_ork_cli) behind live-bash
# parity gates. This shim lets a single env flag flip the runtime:
#
#   MINI_ORK_RUNTIME=python  (default) — exec the ported module (the live runtime).
#   MINI_ORK_RUNTIME=bash              — run the legacy bash entrypoint (escape hatch).
#
# The default is python: the first flip attempt (#156) surfaced real divergences in
# the non-dispatch-engine ported flows (spawn child kickoff, recursive orchestration,
# execute no-plan exit codes, needs_answers gate) + a shim exec-on-source bug — all
# fixed here and re-gated by the full CI suite running under python. Fall back to bash
# per-command with MINI_ORK_RUNTIME=bash. The shim still only delegates when the ported
# module exists, so any not-yet-ported entrypoint transparently degrades to bash.
#
# Each entrypoint sources this and calls, right after MINI_ORK_ROOT is set:
#   mo_runtime_maybe_delegate "${BASH_SOURCE[0]}" "$@"
#
# When bash (the default) it is a no-op (returns 0, entrypoint continues). When
# python it execs `python3 -m mini_ork.ported.<module>` — process replacement,
# so the rest of the bash never runs. Only delegates when the ported module
# actually exists, so a partially-ported tree degrades to bash per-command.
#
# Env inherits across the exec, so a python-runtime `mini-ork run` cascades: its
# classify/plan/execute sub-invocations also delegate to Python.

mo_runtime_maybe_delegate() {
  [ "${MINI_ORK_RUNTIME:-python}" = "python" ] || return 0
  # Only delegate when the entrypoint is EXECUTED, not SOURCED. Bash unit tests
  # (and any code) `source bin/mini-ork-*` to reuse its functions; the exec below
  # would hijack that source and run python instead of loading the functions. The
  # caller passes its ${BASH_SOURCE[0]} as $1 — it equals $0 only when the entrypoint
  # is the top-level executed script, not when it is being sourced.
  [ "${1:-}" = "$0" ] || return 0
  local _self="${1:-}"; shift || true
  local _base _module
  _base="$(basename "$_self")"
  if [ "$_base" = "mini-ork" ]; then
    _module="mini_ork_cli"
  else
    # mini-ork-execute → mini_ork_execute, mini-ork-self-improve → mini_ork_self_improve
    _module="$(printf '%s' "$_base" | tr '-' '_')"
  fi
  local _root="${MINI_ORK_ROOT:-}"
  [ -n "$_root" ] && [ -f "$_root/mini_ork/ported/${_module}.py" ] || return 0
  exec env PYTHONPATH="${_root}${PYTHONPATH:+:$PYTHONPATH}" \
    python3 -m "mini_ork.ported.${_module}" "$@"
}
