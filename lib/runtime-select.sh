#!/usr/bin/env bash
# runtime-select.sh — bash→Python runtime cutover switch.
#
# The public bin/mini-ork launcher is Python-owned. This shim remains only for
# suffixed bin/mini-ork-* entrypoints whose individual forks are still behind
# live-Bash parity gates. It lets a single env flag flip those runtimes:
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
  # mini-ork-self-improve → mini_ork_self_improve
  _module="$(printf '%s' "$_base" | tr '-' '_')"
  local _root="${MINI_ORK_ROOT:-}"
  [ -n "$_root" ] || return 0
  # ported/ was reorganized into domain packages; map the basename-derived
  # module id to its dotted home. Unmapped ids degrade to bash (return 0).
  local _mod=""
  case "$_module" in
    mini_ork_bug_collector) _mod="mini_ork.observability.bug_collector" ;;
    mini_ork_bugs) _mod="mini_ork.cli.bugs" ;;
    mini_ork_checkpoints) _mod="mini_ork.stores.checkpoints" ;;
    mini_ork_classify) _mod="mini_ork.cli.classify" ;;
    mini_ork_cli) _mod="mini_ork.cli.main" ;;
    mini_ork_conductor) _mod="mini_ork.orchestration.conductor" ;;
    mini_ork_coord) _mod="mini_ork.orchestration.coord" ;;
    mini_ork_epics) _mod="mini_ork.cli.epics" ;;
    mini_ork_eval) _mod="mini_ork.cli.eval" ;;
    mini_ork_execute) _mod="mini_ork.cli.execute" ;;
    mini_ork_improve) _mod="mini_ork.cli.improve" ;;
    mini_ork_init) _mod="mini_ork.cli.init" ;;
    mini_ork_inject) _mod="mini_ork.cli.inject" ;;
    mini_ork_invoke_prompt) _mod="mini_ork.cli.invoke_prompt" ;;
    mini_ork_lifetime) _mod="mini_ork.orchestration.lifetime" ;;
    mini_ork_mcp_steering) _mod="mini_ork.steering.mcp_server" ;;
    mini_ork_metrics) _mod="mini_ork.cli.metrics" ;;
    mini_ork_plan) _mod="mini_ork.cli.plan" ;;
    mini_ork_promote) _mod="mini_ork.cli.promote" ;;
    mini_ork_reflect) _mod="mini_ork.cli.reflect" ;;
    mini_ork_resume) _mod="mini_ork.cli.resume" ;;
    mini_ork_rollback) _mod="mini_ork.cli.rollback" ;;
    mini_ork_self_improve) _mod="mini_ork.cli.self_improve" ;;
    mini_ork_serve) _mod="mini_ork.cli.serve" ;;
    mini_ork_spawn) _mod="mini_ork.cli.spawn" ;;
    mini_ork_topology) _mod="mini_ork.cli.topology" ;;
    mini_ork_traceotter) _mod="mini_ork.cli.traceotter" ;;
    mini_ork_update) _mod="mini_ork.cli.update" ;;
    mini_ork_usage_report) _mod="mini_ork.observability.usage_report" ;;
    mini_ork_verify) _mod="mini_ork.cli.verify" ;;
    mini_ork_watchdog) _mod="mini_ork.orchestration.watchdog" ;;
    *) return 0 ;;
  esac
  local _rel="${_mod#mini_ork.}"; _rel="mini_ork/${_rel//.//}.py"
  [ -f "$_root/$_rel" ] || return 0
  exec env PYTHONPATH="${_root}${PYTHONPATH:+:$PYTHONPATH}" \
    python3 -m "$_mod" "$@"
}
