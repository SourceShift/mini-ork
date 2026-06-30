#!/usr/bin/env bash
# Regression / contract test for T1.0 — per-run config isolation.
# Proves concurrent mini-ork runs don't share the mutable global agents.yaml:
# a run freezes its lane policy into its run-dir at launch, and the dispatch
# resolvers read run-dir-first, so editing the global agents.yaml mid-run can't
# perturb an in-flight run, and two runs hold independent frozen policies.
set -uo pipefail
ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
export MINI_ORK_ROOT="$ROOT"
PASS=0; FAIL=0
ok(){ echo "  [OK]   $1"; PASS=$((PASS+1)); }
bad(){ echo "  [FAIL] $1"; FAIL=$((FAIL+1)); }

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
echo "── unit: per-run config isolation (T1.0) ──"

# read the implementer lane from whatever agents.yaml the resolvers would pick
_lane_via_resolver(){ python3 - "$(mo_resolve_agents_yaml)" <<'PY'
import sys, yaml
cfg = yaml.safe_load(open(sys.argv[1], encoding="utf-8")) or {}
print((cfg.get("lanes") or {}).get("implementer", ""))
PY
}

# --- fixtures: a global HOME config with implementer: minimax -----------------
export MINI_ORK_HOME="$TMP/home"
mkdir -p "$MINI_ORK_HOME/config"
printf 'lanes:\n  implementer: minimax\n' > "$MINI_ORK_HOME/config/agents.yaml"

# shellcheck source=/dev/null
. "$ROOT/lib/config_resolve.sh"

# --- Scenario: launch run A, freeze policy, then mutate the GLOBAL ------------
RUN_A="$TMP/home/runs/run-A"; mkdir -p "$RUN_A"
( export MINI_ORK_RUN_DIR="$RUN_A"; mo_snapshot_run_config "$RUN_A" )
[ -f "$RUN_A/config/agents.yaml" ] && ok "snapshot froze agents.yaml into run-dir" || bad "snapshot missing"

# operator (or run B) mutates the global lane policy mid-flight
printf 'lanes:\n  implementer: kimi\n' > "$MINI_ORK_HOME/config/agents.yaml"

# resolver WITH run-dir set must return the FROZEN lane (minimax), not kimi
frozen="$(MINI_ORK_RUN_DIR="$RUN_A" bash -c '. "'"$ROOT"'/lib/config_resolve.sh"; '"$(declare -f _lane_via_resolver)"'; _lane_via_resolver')"
[ "$frozen" = "minimax" ] && ok "in-flight run keeps frozen lane despite global edit (got minimax)" || bad "run not isolated (got '$frozen', expected minimax)"

# resolver WITHOUT a run-dir snapshot sees the new global (kimi) — proves the
# isolation is the run-dir tier, not an accident
nofreeze="$(MINI_ORK_RUN_DIR="$TMP/home/runs/run-NONE" bash -c '. "'"$ROOT"'/lib/config_resolve.sh"; '"$(declare -f _lane_via_resolver)"'; _lane_via_resolver')"
[ "$nofreeze" = "kimi" ] && ok "un-snapshotted resolution sees the live global (kimi)" || bad "global resolution wrong (got '$nofreeze')"

# --- Two concurrent runs hold independent frozen policies ---------------------
RUN_B="$TMP/home/runs/run-B"; mkdir -p "$RUN_B/config"
printf 'lanes:\n  implementer: codex\n' > "$RUN_B/config/agents.yaml"   # B frozen earlier with codex
a="$(MINI_ORK_RUN_DIR="$RUN_A" bash -c '. "'"$ROOT"'/lib/config_resolve.sh"; '"$(declare -f _lane_via_resolver)"'; _lane_via_resolver')"
b="$(MINI_ORK_RUN_DIR="$RUN_B" bash -c '. "'"$ROOT"'/lib/config_resolve.sh"; '"$(declare -f _lane_via_resolver)"'; _lane_via_resolver')"
[ "$a" = "minimax" ] && [ "$b" = "codex" ] && ok "two runs resolve independently (A=minimax B=codex)" || bad "runs not independent (A=$a B=$b)"

# --- Idempotency: re-snapshot must NOT overwrite the launch-time policy -------
( export MINI_ORK_RUN_DIR="$RUN_A"; mo_snapshot_run_config "$RUN_A" )   # global is kimi now
again="$(MINI_ORK_RUN_DIR="$RUN_A" bash -c '. "'"$ROOT"'/lib/config_resolve.sh"; '"$(declare -f _lane_via_resolver)"'; _lane_via_resolver')"
[ "$again" = "minimax" ] && ok "re-snapshot is idempotent (keeps launch-time minimax)" || bad "re-snapshot clobbered policy (got '$again')"

# --- Integration: the real dispatch resolver honors the snapshot --------------
ds_lane="$(MINI_ORK_RUN_DIR="$RUN_A" MINI_ORK_HOME="$MINI_ORK_HOME" \
  bash -c '. "'"$ROOT"'/lib/decision_service.sh" 2>/dev/null; decision_service_default_lane implementer' 2>/dev/null)"
if [ -n "$ds_lane" ]; then
  [ "$ds_lane" = "minimax" ] && ok "decision_service_default_lane honors the run-dir snapshot (minimax)" || bad "decision_service ignored snapshot (got '$ds_lane')"
else
  echo "  [SKIP] decision_service.sh did not load in isolation (heavy deps) — core contract still proven above"
fi

echo "── Results: $PASS OK  $FAIL FAIL ──"
[ "$FAIL" -eq 0 ]
