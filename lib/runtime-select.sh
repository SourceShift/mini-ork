#!/usr/bin/env bash
# runtime-select.sh — bash→Python runtime cutover switch.
#
# The strangler-fig migration ports every bin/mini-ork-* entrypoint to
# mini_ork/ported/mini_ork_<name>.py (bin/mini-ork → mini_ork_cli) behind live-bash
# parity gates. This shim lets a single env flag flip the runtime:
#
#   MINI_ORK_RUNTIME=bash    (default) — run the bash entrypoint as before.
#   MINI_ORK_RUNTIME=python            — exec the ported module instead.
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
  [ "${MINI_ORK_RUNTIME:-bash}" = "python" ] || return 0
  local _self="${1:-}"; shift || true
  local _base _module
  _base="$(basename "$_self")"
  if [ "$_base" = "mini-ork" ]; then
    _module="mini_ork_cli"
  else
    # mini-ork-plan → mini_ork_plan, mini-ork-self-improve → mini_ork_self_improve
    _module="$(printf '%s' "$_base" | tr '-' '_')"
  fi
  local _root="${MINI_ORK_ROOT:-}"
  [ -n "$_root" ] && [ -f "$_root/mini_ork/ported/${_module}.py" ] || return 0
  exec env PYTHONPATH="${_root}${PYTHONPATH:+:$PYTHONPATH}" \
    python3 -m "mini_ork.ported.${_module}" "$@"
}
