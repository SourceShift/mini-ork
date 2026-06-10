#!/usr/bin/env bash
# registry.sh — providers.yaml registry for mini-ork's LLM dispatcher.
#
# Lets developers bring their own Anthropic / OpenAI-compatible API keys
# without writing a cl_*.sh wrapper. A provider entry declares HOW to reach
# a model; secrets stay in secrets.local.sh (or the ambient environment).
#
# Resolution order for the registry file:
#   1. $MINI_ORK_PROVIDERS            (explicit override, absolute path)
#   2. $MINI_ORK_HOME/config/providers.yaml
#   3. $MINI_ORK_ROOT/config/providers.yaml   (repo default, committed)
#
# Schema (config/providers.yaml):
#   providers:
#     <name>:                  # referenced by agents.yaml lanes.<lane>
#       kind: anthropic-native | anthropic-compat | openai-compat | executable
#       family: anthropic      # provider family for telemetry (llm_calls.provider)
#       model: claude-opus-4-7 # model id pin (optional for anthropic-native)
#       base_url: https://...  # anthropic-compat / openai-compat endpoints
#       api_key_env: MY_KEY    # NAME of env var holding the key (never the key)
#       script: path/to/x.sh   # kind=executable only; dispatcher contract
#       gateway: true          # force json output (no stream-json support)
#       extra_env:             # optional extra env exports (map)
#         ENABLE_TOOL_SEARCH: "false"
#
# Lookups are cached per-session via exported env vars (same pattern as the
# _MO_LANE_ cache in llm_dispatch) so parallel node subshells inherit them
# instead of re-forking python3 + yaml.safe_load per dispatch.

_mo_registry_file() {
  if [ -n "${MINI_ORK_PROVIDERS:-}" ] && [ -f "${MINI_ORK_PROVIDERS}" ]; then
    printf '%s\n' "$MINI_ORK_PROVIDERS"
    return 0
  fi
  local _home_yaml="${MINI_ORK_HOME:-.mini-ork}/config/providers.yaml"
  if [ -f "$_home_yaml" ]; then
    printf '%s\n' "$_home_yaml"
    return 0
  fi
  local _root_yaml="${MINI_ORK_ROOT:-.}/config/providers.yaml"
  if [ -f "$_root_yaml" ]; then
    printf '%s\n' "$_root_yaml"
    return 0
  fi
  return 1
}

# mo_provider_field <provider-name> <field> → value on stdout (empty if unset)
# Returns 1 when the provider has no registry entry at all.
mo_provider_field() {
  local name="${1:?provider name required}"
  local field="${2:?field required}"
  local _cache_key="_MO_PROV_${name^^}_${field^^}"
  _cache_key="${_cache_key//-/_}"
  # Cached values are stored with a sentinel prefix so "cached empty" and
  # "not cached yet" are distinguishable.
  local _cached="${!_cache_key:-}"
  if [ -n "$_cached" ]; then
    [ "$_cached" = "__MO_ABSENT__" ] && return 1
    [ "$_cached" = "__MO_EMPTY__" ] && { printf '\n'; return 0; }
    printf '%s\n' "$_cached"
    return 0
  fi

  local _yaml
  _yaml=$(_mo_registry_file) || { export "$_cache_key=__MO_ABSENT__"; return 1; }

  local _val
  _val=$(python3 - "$_yaml" "$name" "$field" 2>/dev/null <<'PY'
import sys, yaml
path, name, field = sys.argv[1:4]
try:
    d = yaml.safe_load(open(path)) or {}
except Exception:
    sys.exit(1)
entry = (d.get('providers') or {}).get(name)
if not isinstance(entry, dict):
    sys.exit(1)
v = entry.get(field)
if v is None:
    print('')
elif isinstance(v, bool):
    print('true' if v else 'false')
elif isinstance(v, dict):
    # extra_env maps → one KEY=VALUE per line, consumable by `while read`
    for k, val in v.items():
        print(f"{k}={val}")
else:
    print(v)
PY
  ) || { export "$_cache_key=__MO_ABSENT__"; return 1; }

  if [ -z "$_val" ]; then
    export "$_cache_key=__MO_EMPTY__"
    printf '\n'
  else
    export "$_cache_key=$_val"
    printf '%s\n' "$_val"
  fi
  return 0
}

# mo_provider_exists <provider-name> — 0 when registry has an entry
mo_provider_exists() {
  mo_provider_field "$1" kind >/dev/null 2>&1
}

# mo_provider_kind <provider-name> → kind on stdout, 1 if absent/invalid
mo_provider_kind() {
  local _kind
  _kind=$(mo_provider_field "$1" kind) || return 1
  case "$_kind" in
    anthropic-native|anthropic-compat|openai-compat|executable)
      printf '%s\n' "$_kind" ;;
    *)
      return 1 ;;
  esac
}
