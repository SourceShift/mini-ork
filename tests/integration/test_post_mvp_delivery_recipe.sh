#!/usr/bin/env bash
# Integration coverage for the post-mvp-delivery discovery-first recipe.
set -uo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
export MINI_ORK_ROOT
export PATH="$MINI_ORK_ROOT/bin:$PATH"
export MINI_ORK_DRY_RUN=1

TMPROOT="$(mktemp -d /tmp/mini-ork-post-mvp-recipe-XXXXXX)"
trap 'rm -rf "$TMPROOT"' EXIT
cd "$TMPROOT" || exit 1
git init -q

export MINI_ORK_HOME="$TMPROOT/.mini-ork"
export MINI_ORK_DB="$MINI_ORK_HOME/state.db"

PASS=0
FAIL=0
_ok() { echo "  [OK]   $*"; PASS=$((PASS + 1)); }
_fail() { echo "  [FAIL] $*"; FAIL=$((FAIL + 1)); }
_assert() {
  local label="$1"
  shift
  if "$@"; then _ok "$label"; else _fail "$label"; fi
}

mini-ork init >/dev/null 2>&1

RECIPE_DIR="$MINI_ORK_ROOT/recipes/post-mvp-delivery"
WORKFLOW="$RECIPE_DIR/workflow.yaml"
OPTIONS_VERIFIER="$RECIPE_DIR/verifiers/options-completeness.sh"
DECISION_VERIFIER="$RECIPE_DIR/verifiers/selected-option-gate.sh"

cat > "$TMPROOT/kickoff.md" <<'MD'
# Deliver Post-MVP Admin Console

## Goal
Deliver a post-MVP product capability that requires research before implementation.

## Success Criteria
- Research deep implementation details.
- Provide options to the user.
- Ask the user to choose before extending implementation.

## Scope
- The workflow must use mini-ork agents for product, architecture, integration,
  and validation research.
MD

echo "── integration: post-mvp-delivery recipe ──"

_assert "recipe task_class.yaml exists" test -f "$RECIPE_DIR/task_class.yaml"
_assert "recipe workflow.yaml exists" test -f "$WORKFLOW"
_assert "options verifier exists" test -f "$OPTIONS_VERIFIER"
_assert "selected-option gate exists" test -f "$DECISION_VERIFIER"

OUT="$(PYTHONPATH="$MINI_ORK_ROOT${PYTHONPATH:+:$PYTHONPATH}" \
  python3 -m mini_ork.ported.mini_ork_classify \
  --dry-run "$TMPROOT/kickoff.md" 2>/dev/null || true)"
CLASS="$(printf '%s\n' "$OUT" | grep -E '^task_class=' | head -1 | cut -d= -f2)"
if [ "$CLASS" = "post_mvp_delivery" ]; then
  _ok "post-MVP kickoff classifies to post_mvp_delivery"
else
  _fail "post-MVP kickoff classified as ${CLASS:-missing}"
fi

python3 - "$WORKFLOW" <<'PY'
import sys
import yaml

with open(sys.argv[1], encoding="utf-8") as f:
    wf = yaml.safe_load(f) or {}

nodes = wf.get("nodes") or []
names = [n.get("name") for n in nodes]
required = [
    "discovery_planner",
    "product_lens",
    "architecture_lens",
    "integration_lens",
    "validation_lens",
    "options_synthesizer",
    "options_completeness",
    "delivery_planner",
    "selected_option_gate",
    "implementer",
]
missing = [name for name in required if name not in names]

def index(name):
    return names.index(name)

ordered = not missing and (
    index("options_completeness")
    < index("delivery_planner")
    < index("selected_option_gate")
    < index("implementer")
)

lenses = {
    n.get("name"): n.get("model_lane")
    for n in nodes
    if n.get("name") in {"product_lens", "architecture_lens", "integration_lens", "validation_lens"}
}
expected_lanes = {
    "product_lens": "glm_lens",
    "architecture_lens": "codex_lens",
    "integration_lens": "kimi_lens",
    "validation_lens": "minimax_lens",
}

edges = wf.get("edges") or []
has_decision_edge = any(
    e.get("from") == "options_completeness"
    and e.get("to") == "delivery_planner"
    and e.get("edge_type") == "human_decision_gate"
    for e in edges
)
has_selected_gate = any(
    e.get("from") == "selected_option_gate"
    and e.get("to") == "implementer"
    for e in edges
)

if missing:
    print("missing_nodes=" + ",".join(missing))
    raise SystemExit(1)
if not ordered:
    print("bad_order=" + ",".join(names))
    raise SystemExit(2)
if lenses != expected_lanes:
    print("bad_lanes=" + repr(lenses))
    raise SystemExit(3)
if not has_decision_edge or not has_selected_gate:
    print("missing_decision_edges")
    raise SystemExit(4)
PY
case "$?" in
  0) _ok "workflow has discovery lenses, decision gate, and correct ordering" ;;
  *) _fail "workflow topology check failed" ;;
esac

export MINI_ORK_RUN_DIR="$TMPROOT/run"
mkdir -p "$MINI_ORK_RUN_DIR"
cat > "$MINI_ORK_RUN_DIR/options.md" <<'MD'
# Options

## Option A
Ship a focused collaboration dashboard.

## Recommendation
Choose Option A.

## Tradeoff
Lower breadth, faster delivery.

## Risk
May miss some admin workflows.

## Validation
Run user-flow and integration tests.

## Decision
Pending user choice.
MD

if bash "$OPTIONS_VERIFIER" >/dev/null 2>&1; then
  _ok "options verifier accepts complete options.md"
else
  _fail "options verifier rejected complete options.md"
fi

rm -f "$MINI_ORK_RUN_DIR/options.md"
cat > "$MINI_ORK_RUN_DIR/options.md" <<'MD'
# Options

## Option A
Ship a focused collaboration dashboard.

## Recommended Default
Choose Option A.

## Tradeoffs
Lower breadth, faster delivery.

## Risks
May miss some admin workflows.

## Validation Plan
Run user-flow and integration tests.

## User Decision Required
Pending user choice.
MD

if bash "$OPTIONS_VERIFIER" >/dev/null 2>&1; then
  _ok "options verifier accepts recommended-default wording from live runs"
else
  _fail "options verifier rejected recommended-default wording"
fi

rm -f "$MINI_ORK_RUN_DIR/options.md"
cat > "$MINI_ORK_RUN_DIR/options.md" <<'MD'
# Options

## Option A
Incomplete package.
MD

if bash "$OPTIONS_VERIFIER" >/dev/null 2>&1; then
  _fail "options verifier accepted incomplete options.md"
else
  _ok "options verifier rejects incomplete options.md"
fi

cat > "$MINI_ORK_RUN_DIR/options.md" <<'MD'
# Options

## Option A
Ship a focused collaboration dashboard.

## Recommendation
Choose Option A.

## Tradeoff
Lower breadth, faster delivery.

## Risk
May miss some admin workflows.

## Validation
Run user-flow and integration tests.

## Decision
Pending user choice.
MD

rm -f "$MINI_ORK_RUN_DIR/selected-option.md"
if bash "$DECISION_VERIFIER" >/dev/null 2>&1; then
  _fail "selected-option gate passed without a user choice"
else
  _ok "selected-option gate blocks implementation without user choice"
fi

cat > "$MINI_ORK_RUN_DIR/selected-option.md" <<'MD'
# Selected Option

Option A, because it is the smallest post-MVP delivery with clear validation.
MD

if bash "$DECISION_VERIFIER" >/dev/null 2>&1; then
  _ok "selected-option gate passes with selected-option.md"
else
  _fail "selected-option gate rejected selected-option.md"
fi

cat > "$TMPROOT/plan.json" <<'JSON'
{
  "task_class": "post_mvp_delivery",
  "objective": "dry-run lane routing check",
  "decomposition": [],
  "artifact_contract": {"outputs": ["options.md"], "success_verifiers": []},
  "verifier_contract": {"checks": []}
}
JSON

DRY_OUT="$(
  MINI_ORK_WORKFLOW="$WORKFLOW" \
  MINI_ORK_RECIPE="post-mvp-delivery" \
  MINI_ORK_PLAN_PATH="$TMPROOT/plan.json" \
  mini-ork execute --dry-run 2>&1
)"

for expected in \
  "node_id=product_lens node_type=researcher model_lane=glm_lens" \
  "node_id=architecture_lens node_type=researcher model_lane=codex_lens" \
  "node_id=integration_lens node_type=researcher model_lane=kimi_lens" \
  "node_id=validation_lens node_type=researcher model_lane=minimax_lens" \
  "node_id=options_synthesizer node_type=reviewer model_lane=reviewer"
do
  if printf '%s\n' "$DRY_OUT" | grep -q "$expected"; then
    _ok "execute dry-run preserves $expected"
  else
    _fail "execute dry-run missing lane marker: $expected"
  fi
done

echo
echo "-- Results: ${PASS} OK  ${FAIL} FAIL --"
[ "$FAIL" -eq 0 ] || exit 1
exit 0
