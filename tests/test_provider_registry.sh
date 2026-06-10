#!/usr/bin/env bash
# tests/test_provider_registry.sh — no-network test for the providers.yaml
# BYO registry (lib/providers/registry.sh + lib/llm-dispatch.sh routing).
#
# Covers:
#   1. registry field lookups (kind/model/base_url/api_key_env/extra_env)
#   2. wrapper-wins precedence — cl_<model>.sh beats a colliding registry entry
#   3. kind=executable dispatch via registry (script field, secrets sourcing)
#   4. kind=openai-compat routing through cl_codex.sh with MO_OAI_* env
#   5. _mo_registry_apply_env for anthropic-compat (env exports, missing-key error)
#   6. registry-aware gateway detection + provider family
#   7. unknown model with no wrapper and no registry entry → rc=2
#
# Never calls a real LLM. All dispatch targets are stub scripts that echo env.
#
# Usage: bash tests/test_provider_registry.sh

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

PASS=0
FAIL=0
check() {
  local desc="$1" expected="$2" actual="$3"
  if [ "$expected" = "$actual" ]; then
    echo "ok   - $desc"
    PASS=$((PASS + 1))
  else
    echo "FAIL - $desc"
    echo "       expected: $expected"
    echo "       actual:   $actual"
    FAIL=$((FAIL + 1))
  fi
}

# ── isolated fake MINI_ORK_ROOT ───────────────────────────────────────────
WORK=$(mktemp -d -t mini-ork-registry-test.XXXXXX)
trap 'rm -rf "$WORK"' EXIT
mkdir -p "$WORK/lib/providers" "$WORK/config" "$WORK/.mini-ork/config" "$WORK/scripts" "$WORK/out"

cp "$REPO_ROOT/lib/providers/registry.sh" "$WORK/lib/providers/registry.sh"

cat > "$WORK/config/providers.yaml" <<'YAML'
providers:
  codex:                       # collides with stub wrapper — wrapper must win
    kind: executable
    family: should-not-be-used
    script: scripts/registry_stub.sh
  stubexec:
    kind: executable
    family: local
    script: scripts/registry_stub.sh
  oaitest:
    kind: openai-compat
    family: openrouter
    model: test-model-7
    base_url: https://example.invalid/v1
    api_key_env: TEST_OAI_KEY
  gwtest:
    kind: anthropic-compat
    family: testgw
    model: gw-model-1
    base_url: https://gw.example.invalid/anthropic
    api_key_env: TEST_GW_KEY
    extra_env:
      ENABLE_TOOL_SEARCH: "false"
  gwstream:
    kind: anthropic-compat
    family: testgw
    model: gw-model-2
    base_url: https://gw2.example.invalid/anthropic
    api_key_env: TEST_GW_KEY
    gateway: false
YAML

# Stub wrapper colliding with the `codex` registry entry (executable lane)
cat > "$WORK/lib/providers/cl_codex.sh" <<'SH'
#!/usr/bin/env bash
# stub: echoes marker + any MO_OAI_* contract vars it received
echo "WRAPPER-WINS"
echo "MO_OAI_MODEL=${MO_OAI_MODEL:-}"
echo "MO_OAI_BASE_URL=${MO_OAI_BASE_URL:-}"
echo "MO_OAI_ENV_KEY=${MO_OAI_ENV_KEY:-}"
SH
chmod +x "$WORK/lib/providers/cl_codex.sh"

# Registry-routed executable stub — echoes a secrets-sourced var
cat > "$WORK/scripts/registry_stub.sh" <<'SH'
#!/usr/bin/env bash
echo "REGISTRY-STUB secret=${MY_TEST_SECRET:-unset}"
SH
chmod +x "$WORK/scripts/registry_stub.sh"

# Secrets file the dispatcher should source inside the subshell
cat > "$WORK/.mini-ork/config/secrets.local.sh" <<'SH'
export MY_TEST_SECRET="s3cret-ok"
export TEST_OAI_KEY="oai-key-ok"
SH

export MINI_ORK_ROOT="$WORK"
export MINI_ORK_HOME="$WORK/.mini-ork"
unset MINI_ORK_PROVIDERS MINI_ORK_SECRETS 2>/dev/null || true

# shellcheck disable=SC1091
source "$REPO_ROOT/lib/llm-dispatch.sh"
set +e  # llm-dispatch.sh enables errexit; keep asserting after failures

# ── 1. registry lookups ───────────────────────────────────────────────────
check "kind lookup"        "openai-compat"          "$(mo_provider_kind oaitest)"
check "model lookup"       "test-model-7"           "$(mo_provider_field oaitest model)"
check "base_url lookup"    "https://example.invalid/v1" "$(mo_provider_field oaitest base_url)"
check "api_key_env lookup" "TEST_OAI_KEY"           "$(mo_provider_field oaitest api_key_env)"
check "extra_env as KEY=VALUE lines" "ENABLE_TOOL_SEARCH=false" "$(mo_provider_field gwtest extra_env)"
mo_provider_exists no_such_provider
check "absent provider → rc=1" "1" "$?"
mo_provider_field gwtest no_such_field >/dev/null
check "absent field on existing provider → rc=0 (empty)" "0" "$?"

# ── 2. wrapper-wins precedence ────────────────────────────────────────────
mo_llm_dispatch codex "hello" "$WORK/out/codex.txt" 30
check "colliding model dispatch rc" "0" "$?"
check "cl_codex.sh wrapper beats registry entry" "WRAPPER-WINS" "$(head -1 "$WORK/out/codex.txt")"
check "no MO_OAI_* leakage on wrapper path" "MO_OAI_MODEL=" "$(grep '^MO_OAI_MODEL=' "$WORK/out/codex.txt")"

# ── 3. kind=executable via registry + secrets sourcing ────────────────────
mo_llm_dispatch stubexec "hello" "$WORK/out/stubexec.txt" 30
check "registry executable dispatch rc" "0" "$?"
check "registry script ran with secrets sourced" "REGISTRY-STUB secret=s3cret-ok" "$(head -1 "$WORK/out/stubexec.txt")"

# ── 4. kind=openai-compat routes through cl_codex.sh with MO_OAI_* env ────
mo_llm_dispatch oaitest "hello" "$WORK/out/oaitest.txt" 30
check "openai-compat dispatch rc" "0" "$?"
check "openai-compat → cl_codex.sh"        "WRAPPER-WINS"  "$(head -1 "$WORK/out/oaitest.txt")"
check "MO_OAI_MODEL propagated"    "MO_OAI_MODEL=test-model-7" "$(grep '^MO_OAI_MODEL=' "$WORK/out/oaitest.txt")"
check "MO_OAI_BASE_URL propagated" "MO_OAI_BASE_URL=https://example.invalid/v1" "$(grep '^MO_OAI_BASE_URL=' "$WORK/out/oaitest.txt")"
check "MO_OAI_ENV_KEY propagated"  "MO_OAI_ENV_KEY=TEST_OAI_KEY" "$(grep '^MO_OAI_ENV_KEY=' "$WORK/out/oaitest.txt")"

# ── 5. _mo_registry_apply_env (anthropic-compat) ──────────────────────────
_env_dump=$(
  export TEST_GW_KEY="gw-key-ok"
  _mo_registry_apply_env gwtest >/dev/null 2>&1 || { echo "APPLY-FAILED"; exit 0; }
  echo "AUTH=${ANTHROPIC_AUTH_TOKEN:-}"
  echo "BASE=${ANTHROPIC_BASE_URL:-}"
  echo "MODEL=${ANTHROPIC_MODEL:-}"
  echo "TOOLSEARCH=${ENABLE_TOOL_SEARCH:-}"
)
check "apply_env: auth token"  "AUTH=gw-key-ok" "$(grep '^AUTH=' <<<"$_env_dump")"
check "apply_env: base url"    "BASE=https://gw.example.invalid/anthropic" "$(grep '^BASE=' <<<"$_env_dump")"
check "apply_env: model pin"   "MODEL=gw-model-1" "$(grep '^MODEL=' <<<"$_env_dump")"
check "apply_env: extra_env"   "TOOLSEARCH=false" "$(grep '^TOOLSEARCH=' <<<"$_env_dump")"
( unset TEST_GW_KEY; _mo_registry_apply_env gwtest >/dev/null 2>&1 )
check "apply_env: missing key → rc=1" "1" "$?"

# ── 6. gateway detection + provider family ────────────────────────────────
_mo_llm_is_gateway gwtest
check "anthropic-compat defaults to gateway" "0" "$?"
_mo_llm_is_gateway gwstream
check "gateway: false respected" "1" "$?"
check "family from registry" "openrouter" "$(_mo_llm_provider_for_model oaitest)"

# ── 7. unknown model, no wrapper, no registry entry ───────────────────────
mo_llm_dispatch no_such_model "hello" "$WORK/out/none.txt" 30
check "unknown model → rc=2" "2" "$?"

echo
echo "passed: $PASS, failed: $FAIL"
[ "$FAIL" -eq 0 ]
