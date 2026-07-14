#!/usr/bin/env bash
# tests/unit/test_tool_grants.sh
#
# End-to-end + unit coverage for node-scoped tool grants
# (kickoff/tool-grant-hermetic-dispatch.md).
#
# The previous attempt shipped a consumer-only implementation and shipped
# 12+ recipes' implementer nodes without Write/Edit because the producer was
# missing. This test covers BOTH sides:
#
#   (A) Producer: parses a real workflow.yaml containing a `tools:` block and
#       produces the expected native|mcp csv.
#   (B) Consumer: feeds the resolved grant through the argv builder and
#       asserts --allowedTools / --strict-mcp-config / --mcp-config reach the
#       claude invocation. Tested via a stub claude binary that prints its
#       argv to a file.
#   (C) Type-aware defaults: undeclared nodes fall through to the per-type
#       default (implementer → Read,Write,Edit,Bash; planner/reviewer →
#       Read,Bash) and STILL receive Write/Edit for implementer (regression
#       bar the prior attempt broke).
#   (D) MCP grant rendering: `mcp: [codegraph]` becomes `mcp__codegraph` in
#       --allowedTools argv.
#   (E) Dead Context7 instruction is gone from bin/_worker-launcher.sh.

set -uo pipefail

ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
export MINI_ORK_ROOT="$ROOT"

OK=0
FAIL=0
_ok()   { OK=$((OK + 1));   echo "  [OK]   $1"; }
_fail() { FAIL=$((FAIL + 1)); echo "  [FAIL] $1"; }

TMP=$(mktemp -d -t mo-tool-grants.XXXXXX)
trap 'rm -rf "$TMP"' EXIT

# ── Source lib/llm-dispatch.sh in source-only mode (no auto-execution) ───
MINI_ORK_LLM_SOURCE_ONLY=1 source "$ROOT/lib/llm-dispatch.sh" 2>/dev/null || true

# ─── Producer tests: parse real workflow.yaml + bespoke fixtures ─────────

echo "── Producer: parse workflow.yaml tools: block ──"

# End-to-end: real recipes/code-fix/workflow.yaml MUST yield the profiles we
# declared. This is the test the prior attempt was missing.
RESOLVED_PLANNER=$(
  MINI_ORK_LLM_SOURCE_ONLY=1 MO_WORKFLOW_YAML="$ROOT/recipes/code-fix/workflow.yaml" \
  bash -c 'source "'"$ROOT/lib/llm-dispatch.sh"'" 2>/dev/null; \
           MO_NODE_ID=planner MO_NODE_TYPE=planner _mo_resolve_node_tools'
)
if [ "$RESOLVED_PLANNER" = "Read|codegraph" ]; then
  _ok "planner (real workflow.yaml) resolves to native=Read mcp=codegraph"
else
  _fail "planner (real workflow.yaml) resolved to '$RESOLVED_PLANNER', expected 'Read|codegraph'"
fi

RESOLVED_IMPL=$(
  MINI_ORK_LLM_SOURCE_ONLY=1 MO_WORKFLOW_YAML="$ROOT/recipes/code-fix/workflow.yaml" \
  bash -c 'source "'"$ROOT/lib/llm-dispatch.sh"'" 2>/dev/null; \
           MO_NODE_ID=implementer MO_NODE_TYPE=implementer _mo_resolve_node_tools'
)
if [ "$RESOLVED_IMPL" = "Read,Write,Edit,Bash|codegraph" ]; then
  _ok "implementer (real workflow.yaml) resolves to native=Read,Write,Edit,Bash mcp=codegraph"
else
  _fail "implementer (real workflow.yaml) resolved to '$RESOLVED_IMPL', expected 'Read,Write,Edit,Bash|codegraph'"
fi

RESOLVED_REV=$(
  MINI_ORK_LLM_SOURCE_ONLY=1 MO_WORKFLOW_YAML="$ROOT/recipes/code-fix/workflow.yaml" \
  bash -c 'source "'"$ROOT/lib/llm-dispatch.sh"'" 2>/dev/null; \
           MO_NODE_ID=reviewer MO_NODE_TYPE=reviewer _mo_resolve_node_tools'
)
if [ "$RESOLVED_REV" = "Read,Bash|" ]; then
  _ok "reviewer (real workflow.yaml) resolves to native=Read,Bash mcp=(none)"
else
  _fail "reviewer (real workflow.yaml) resolved to '$RESOLVED_REV', expected 'Read,Bash|'"
fi

# ─── Type-aware fail-closed defaults ─────────────────────────────────────
echo "── Type-aware fail-closed defaults ──"

# Fixture: workflow.yaml whose implementer node has NO tools: block. The
# default MUST still be Read,Write,Edit,Bash (no MCP). This is the regression
# bar the prior attempt broke (it shipped a blanket Read,Bash default).
FIXTURE_UNDECL_IMPL="$TMP/workflow-undeclared-impl.yaml"
cat > "$FIXTURE_UNDECL_IMPL" <<'YAML'
version: "0.1.0"
task_class: code_fix
nodes:
  - name: planner
    type: planner
  - name: implementer
    type: implementer
    # NO tools: block — must fall through to type-aware default
  - name: reviewer
    type: reviewer
YAML

RESOLVED=$(
  MINI_ORK_LLM_SOURCE_ONLY=1 MO_WORKFLOW_YAML="$FIXTURE_UNDECL_IMPL" \
  bash -c 'source "'"$ROOT/lib/llm-dispatch.sh"'" 2>/dev/null; \
           MO_NODE_ID=implementer MO_NODE_TYPE=implementer _mo_resolve_node_tools 2>/dev/null'
)
if [ "$RESOLVED" = "Read,Write,Edit,Bash|" ]; then
  _ok "undeclared implementer defaults to Read,Write,Edit,Bash (no MCP) — regression bar"
else
  _fail "undeclared implementer resolved to '$RESOLVED', expected 'Read,Write,Edit,Bash|'"
fi

RESOLVED=$(
  MINI_ORK_LLM_SOURCE_ONLY=1 MO_WORKFLOW_YAML="$FIXTURE_UNDECL_IMPL" \
  bash -c 'source "'"$ROOT/lib/llm-dispatch.sh"'" 2>/dev/null; \
           MO_NODE_ID=planner MO_NODE_TYPE=planner _mo_resolve_node_tools 2>/dev/null'
)
if [ "$RESOLVED" = "Read,Bash|" ]; then
  _ok "undeclared planner defaults to Read,Bash (no MCP, no Write/Edit)"
else
  _fail "undeclared planner resolved to '$RESOLVED', expected 'Read,Bash|'"
fi

RESOLVED=$(
  MINI_ORK_LLM_SOURCE_ONLY=1 MO_WORKFLOW_YAML="$FIXTURE_UNDECL_IMPL" \
  bash -c 'source "'"$ROOT/lib/llm-dispatch.sh"'" 2>/dev/null; \
           MO_NODE_ID=reviewer MO_NODE_TYPE=reviewer _mo_resolve_node_tools 2>/dev/null'
)
if [ "$RESOLVED" = "Read,Bash|" ]; then
  _ok "undeclared reviewer defaults to Read,Bash (no MCP, no Write/Edit)"
else
  _fail "undeclared reviewer resolved to '$RESOLVED', expected 'Read,Bash|'"
fi

# Pure default fn parity (no yaml at all)
RESOLVED=$(
  MINI_ORK_LLM_SOURCE_ONLY=1 bash -c 'source "'"$ROOT/lib/llm-dispatch.sh"'" 2>/dev/null; \
                                    MO_NODE_ID=unknown MO_NODE_TYPE=implementer _mo_resolve_node_tools 2>/dev/null'
)
if [ "$RESOLVED" = "Read,Write,Edit,Bash|" ]; then
  _ok "no-yaml implementer falls through to default Read,Write,Edit,Bash"
else
  _fail "no-yaml implementer resolved to '$RESOLVED', expected 'Read,Write,Edit,Bash|'"
fi

# ─── MCP grant rendering ────────────────────────────────────────────────
echo "── MCP grant rendering (mcp__<server>) ──"

ALLOWED=$(
  MINI_ORK_LLM_SOURCE_ONLY=1 bash -c 'source "'"$ROOT/lib/llm-dispatch.sh"'" 2>/dev/null; \
                                     _mo_build_allowed_tools_arg "Read,Write,Edit,Bash" "codegraph"'
)
if [ "$ALLOWED" = "Read,Write,Edit,Bash,mcp__codegraph" ]; then
  _ok "MCP grant 'codegraph' renders as mcp__codegraph in --allowedTools"
else
  _fail "MCP grant rendered as '$ALLOWED', expected 'Read,Write,Edit,Bash,mcp__codegraph'"
fi

ALLOWED=$(
  MINI_ORK_LLM_SOURCE_ONLY=1 bash -c 'source "'"$ROOT/lib/llm-dispatch.sh"'" 2>/dev/null; \
                                     _mo_build_allowed_tools_arg "Read" "codegraph,context7"'
)
if [ "$ALLOWED" = "Read,mcp__codegraph,mcp__context7" ]; then
  _ok "multiple MCP grants render as mcp__<server> each"
else
  _fail "multi-MCP rendered as '$ALLOWED', expected 'Read,mcp__codegraph,mcp__context7'"
fi

ALLOWED=$(
  MINI_ORK_LLM_SOURCE_ONLY=1 bash -c 'source "'"$ROOT/lib/llm-dispatch.sh"'" 2>/dev/null; \
                                     _mo_build_allowed_tools_arg "Read,Bash" ""'
)
if [ "$ALLOWED" = "Read,Bash" ]; then
  _ok "no MCP grant → no mcp__ entries in argv"
else
  _fail "empty MCP rendered as '$ALLOWED', expected 'Read,Bash'"
fi

# ─── argv injection: stub claude binary, dispatch real run dir ───────────
echo "── argv injection: --allowedTools / --strict-mcp-config / --mcp-config reach claude ──"

STUB_BIN="$TMP/bin"
mkdir -p "$STUB_BIN"
cat > "$STUB_BIN/claude" <<'STUB'
#!/usr/bin/env bash
# stub claude — prints its argv as a JSON array, exits 0
python3 - "$@" <<'PY'
import json, sys
print(json.dumps(sys.argv[1:]))
PY
STUB
chmod +x "$STUB_BIN/claude"

# Real workflow.yaml + implementer node + run dir → dispatch via stub
FIXTURE_RUN_DIR="$TMP/run-impl"
mkdir -p "$FIXTURE_RUN_DIR"
cp "$ROOT/recipes/code-fix/workflow.yaml" "$FIXTURE_RUN_DIR/workflow.yaml"

PATH="$STUB_BIN:$PATH" \
MINI_ORK_RUN_DIR="$FIXTURE_RUN_DIR" \
MO_NODE_ID=implementer \
MO_NODE_TYPE=implementer \
MO_TOOL_GRANTS_DISABLED=0 \
MO_DISPATCH_BACKEND=bash \
MO_LANE_TIER=default \
MO_LLM_FORMAT=text \
MO_TRACE_RICH=0 \
MINI_ORK_LLM_SOURCE_ONLY=1 \
bash -c '
  source "'"$ROOT/lib/llm-dispatch.sh"'"
  # Source a stub provider so the bash sourceable branch is exercised.
  mkdir -p "'"$TMP"'/providers"
  cat > "'"$TMP"'/providers/cl_test.sh" <<EOF
#!/usr/bin/env bash
unset ANTHROPIC_AUTH_TOKEN ANTHROPIC_API_KEY ANTHROPIC_BASE_URL
EOF
  chmod +x "'"$TMP"'/providers/cl_test.sh"
  SCRIPTS_DIR="'"$TMP"'/providers" mo_llm_dispatch test "PONG" "'"$FIXTURE_RUN_DIR/out.txt"'" 60 5
' >/dev/null 2>"$FIXTURE_RUN_DIR/err.log" || true

# The stub prints its argv as JSON on stdout — read it back
if [ -f "$FIXTURE_RUN_DIR/out.txt" ]; then
  STUB_ARGV=$(head -c 8192 "$FIXTURE_RUN_DIR/out.txt")
else
  STUB_ARGV=""
fi

echo "    stub argv (first 200 chars): ${STUB_ARGV:0:200}"

if echo "$STUB_ARGV" | grep -q '"--allowedTools"'; then
  _ok "stub claude invocation received --allowedTools"
else
  _fail "stub claude invocation did NOT receive --allowedTools (argv: $STUB_ARGV)"
fi

if echo "$STUB_ARGV" | grep -q '"--strict-mcp-config"'; then
  _ok "stub claude invocation received --strict-mcp-config"
else
  _fail "stub claude invocation did NOT receive --strict-mcp-config (argv: $STUB_ARGV)"
fi

if echo "$STUB_ARGV" | grep -q '"--mcp-config"'; then
  _ok "stub claude invocation received --mcp-config"
else
  _fail "stub claude invocation did NOT receive --mcp-config (argv: $STUB_ARGV)"
fi

# The implementer's allowedTools value MUST contain Write and Edit AND mcp__codegraph
if echo "$STUB_ARGV" | grep -qE '"--allowedTools",\s*"[^"]*Write'; then
  _ok "implementer --allowedTools contains Write (regression bar)"
else
  _fail "implementer --allowedTools missing Write (argv: $STUB_ARGV)"
fi
if echo "$STUB_ARGV" | grep -qE '"--allowedTools",\s*"[^"]*Edit'; then
  _ok "implementer --allowedTools contains Edit (regression bar)"
else
  _fail "implementer --allowedTools missing Edit (argv: $STUB_ARGV)"
fi
if echo "$STUB_ARGV" | grep -qE '"--allowedTools",\s*"[^"]*mcp__codegraph'; then
  _ok "implementer --allowedTools contains mcp__codegraph (MCP grant rendered)"
else
  _fail "implementer --allowedTools missing mcp__codegraph (argv: $STUB_ARGV)"
fi

# The node-scoped MCP config file MUST exist and contain only the granted server
if [ -f "$FIXTURE_RUN_DIR/.mcp-config.json" ]; then
  _ok "node-scoped .mcp-config.json written to run dir"
  if grep -q '"codegraph"' "$FIXTURE_RUN_DIR/.mcp-config.json"; then
    _ok "node-scoped .mcp-config.json contains the granted codegraph server"
  else
    _fail "node-scoped .mcp-config.json missing the granted server (content: $(cat "$FIXTURE_RUN_DIR/.mcp-config.json"))"
  fi
else
  _fail "node-scoped .mcp-config.json was NOT written"
fi

# ─── Source-level greps (verifier contract) ─────────────────────────────
echo "── source-level greps (verifier contract) ──"

# The producer must exist in source (proof: `tools:` appears in producer code)
if grep -rq "tools:" lib/ bin/; then
  _ok "producer exists in source (grep 'tools:' lib/ bin/ non-empty)"
else
  _fail "producer not in source (grep 'tools:' lib/ bin/ empty)"
fi

# Both claude invocations must have --allowedTools and --strict-mcp-config
COUNT_ALLOWED=$(grep -c -- '--allowedTools' lib/llm-dispatch.sh || true)
if [ "$COUNT_ALLOWED" -ge 2 ]; then
  _ok "--allowedTools reaches both claude invocations (count=$COUNT_ALLOWED)"
else
  _fail "--allowedTools count=$COUNT_ALLOWED (expected >=2)"
fi

COUNT_STRICT=$(grep -c -- '--strict-mcp-config' lib/llm-dispatch.sh || true)
if [ "$COUNT_STRICT" -ge 2 ]; then
  _ok "--strict-mcp-config reaches both claude invocations (count=$COUNT_STRICT)"
else
  _fail "--strict-mcp-config count=$COUNT_STRICT (expected >=2)"
fi

# ─── Dead Context7 instruction removed ──────────────────────────────────
echo "── dead Context7 instruction resolved ──"

if grep -q 'Context7 MCP' "$ROOT/bin/_worker-launcher.sh"; then
  _fail "bin/_worker-launcher.sh still references 'Context7 MCP' — dead instruction not removed"
else
  _ok "bin/_worker-launcher.sh no longer references 'Context7 MCP'"
fi

if grep -q 'ctx7' "$ROOT/bin/_worker-launcher.sh"; then
  _ok "bin/_worker-launcher.sh references the ctx7 CLI (live instruction)"
else
  _fail "bin/_worker-launcher.sh does not reference the ctx7 CLI"
fi

# ─── Structural invariant: implementer cannot reach comms/web MCP ───────
echo "── structural invariant: no comms/web MCP on implementer ──"

IMPL_TOOLS="$RESOLVED_IMPL"
if echo "$IMPL_TOOLS" | grep -qE 'gmail|web|fetch|http|comms|slack'; then
  _fail "implementer reference profile includes a comms/web MCP grant (forbidden): $IMPL_TOOLS"
else
  _ok "implementer reference profile has no comms/web MCP grant"
fi

# ─── Summary ─────────────────────────────────────────────────────────────
echo ""
echo "=== Results: $OK OK  $FAIL FAIL ==="
if [ "$FAIL" -gt 0 ]; then
  exit 1
fi
exit 0