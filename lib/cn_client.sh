#!/usr/bin/env bash
# cn_client.sh — ContextNest HTTP client (read + hook push).
#
# Public API:
#   cn_available                            → 0 if reachable, 1 otherwise (cached for $CN_PING_TTL secs)
#   cn_retrieve <query> [limit]             → JSON {hits[]} from POST /api/v1/tools/retrieve
#   cn_sessions_by_file <path>              → JSON sessions touching path
#   cn_sessions_by_feature <text>           → JSON sessions matching feature text
#   cn_sessions_by_intent <text>            → JSON sessions matching intent text
#   cn_inbox [limit]                        → JSON inbox items
#   cn_features_recent [since] [layer]      → JSON features list (since defaults 24h)
#   cn_hook_post <event> <session_id> [cwd] [transcript_path]
#                                           → fire-and-forget POST to /api/v1/cc/hook/<event>
#   cn_render_atoms_md <retrieve_json> [limit]
#                                           → compact markdown block (callers append unconditionally)
#
# Env:
#   CN_BASE_URL        Default http://127.0.0.1:28080
#   CN_TIMEOUT_SEC     Default 2 (read), 1 (ping/hook). Tight on purpose — never block plan.
#   CN_PING_TTL        Default 30 (seconds reachability cache)
#   MO_DISABLE_CN      If "1" → every read function returns empty {}; hook_post is a no-op.
#
# Design rules:
#   - NEVER block mini-ork on CN being down. Every call has a timeout + fallback to {} or "".
#   - NEVER write to CN via the tools/store endpoint from mini-ork. Canonical write path
#     is session ingest only (cc_hooks → WAL → consolidation worker). We push events,
#     not memories. This keeps CN's substrate pipeline single-entry.
#   - Cite-tag every atom we surface so the planner prompt can attribute it.

[ "${0:-}" = "${BASH_SOURCE[0]:-}" ] && set -Eeuo pipefail

CN_BASE_URL="${CN_BASE_URL:-http://127.0.0.1:28080}"
CN_TIMEOUT_SEC="${CN_TIMEOUT_SEC:-8}"  # bumped from 2 → 8 in PR-1: 2s silently empties retrieve on ~500-char planner briefs (cn_retrieve curl times out → fallback `echo "{}"` → planner gets no atoms with no error). 8s comfortably covers embedder fan-out for typical brief sizes against a populated substrate. Override lower (e.g. CN_TIMEOUT_SEC=2) only in tests.
CN_HOOK_TIMEOUT_SEC="${CN_HOOK_TIMEOUT_SEC:-3}"  # bumped from 1 → 3 in PR-1: 1s silently flips cn_available to "down" intermittently when CN's health endpoint takes 0.8-1s under load (consolidation worker active on a populated substrate). 3s comfortably covers the steady-state health-check latency without making the fire-and-forget hook posts feel slow. The hook curls themselves still set --max-time CN_HOOK_TIMEOUT_SEC so a truly dead CN doesn't block Claude's hook loop.
CN_PING_TTL="${CN_PING_TTL:-30}"

_cn_disabled() { [ "${MO_DISABLE_CN:-0}" = "1" ]; }

_cn_ping_cache_file() {
  local dir="${MINI_ORK_HOME:-.mini-ork}/state"
  mkdir -p "$dir" 2>/dev/null || true
  printf '%s/cn_ping.cache' "$dir"
}

# desc: 0 if CN reachable (cached); 1 otherwise. Cache TTL = CN_PING_TTL.
cn_available() {
  _cn_disabled && return 1
  local cache; cache=$(_cn_ping_cache_file)
  local now; now=$(date +%s)
  if [ -f "$cache" ]; then
    local ts state
    read -r ts state < "$cache" 2>/dev/null || true
    if [ -n "${ts:-}" ] && [ $((now - ts)) -lt "$CN_PING_TTL" ]; then
      [ "${state:-down}" = "up" ]; return $?
    fi
  fi
  local code
  # Reachability probe uses CN_TIMEOUT_SEC (read budget) not CN_HOOK_TIMEOUT_SEC
  # (fire-and-forget budget). PR-1 evidence: under populated substrate +
  # consolidation worker contention, /health latency varies 0.5-3s and a 3s
  # cap times out 30% of the time. Read-call budget (8s) is the right shape
  # for "is the substrate reachable enough to trust a read?" — hook posts
  # remain on the tighter CN_HOOK_TIMEOUT_SEC budget because losing a hook
  # post is cheaper than blocking the planner.
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time "$CN_TIMEOUT_SEC" \
    "$CN_BASE_URL/api/v1/substrate/health" 2>/dev/null || echo "000")
  if [ "$code" = "200" ]; then
    printf '%s up\n' "$now" > "$cache" 2>/dev/null
    return 0
  fi
  printf '%s down\n' "$now" > "$cache" 2>/dev/null
  return 1
}

_cn_post_json() {
  local path="$1" body="$2"
  curl -s --max-time "$CN_TIMEOUT_SEC" \
    -X POST "$CN_BASE_URL$path" \
    -H 'Content-Type: application/json' \
    -d "$body" 2>/dev/null || echo "{}"
}

_cn_get() {
  local path="$1"
  curl -s --max-time "$CN_TIMEOUT_SEC" "$CN_BASE_URL$path" 2>/dev/null || echo "{}"
}

# desc: GET raw text response (no JSON-shape fallback). Used for the
#       capsule endpoint which returns text/markdown. Empty string on
#       failure so callers can distinguish "no content" from JSON {} hits.
_cn_get_text() {
  local path="$1"
  curl -s --max-time "$CN_TIMEOUT_SEC" "$CN_BASE_URL$path" 2>/dev/null || echo ""
}

# desc: Fetch the deterministic kind-ordered prompt-context capsule.
#       CN clusters atoms by kind, orders by what an agent most needs
#       to know first (risks → decisions → failures → directives →
#       verifications → evidence → reads → artifacts → assumptions),
#       and renders ready-to-paste markdown. Strictly better signal than
#       flat retrieve for planner consumption.
#
#       Args:
#         $1  query     Optional substring filter (case-insensitive over
#                       cluster normalized text). Empty = no filter.
#         $2  since     Duration like "14d", "48h", "30m". Default "14d".
#         $3  project   Optional cwd substring to scope clusters to.
#
#       Returns: markdown text (multi-line) or empty string. Empty when
#       CN is down/disabled OR substrate has no atoms matching filters
#       OR capsule renderer dropped all clusters (caller's responsibility
#       to fall back to cn_retrieve if needed).
cn_capsule() {
  local query="${1:-}"
  local since="${2:-14d}"
  local project="${3:-}"
  _cn_disabled && { echo ""; return 0; }
  cn_available || { echo ""; return 0; }
  local qs="since=$since"
  if [ -n "$query" ]; then
    local encoded_q
    encoded_q=$(python3 -c 'import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1], safe=""))' "$query")
    qs="$qs&query=$encoded_q"
  fi
  if [ -n "$project" ]; then
    local encoded_p
    encoded_p=$(python3 -c 'import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1], safe=""))' "$project")
    qs="$qs&project=$encoded_p"
  fi
  _cn_get_text "/api/v1/prompt-context/capsule?$qs"
}

# desc: Semantic retrieve. Returns CN's raw JSON ({"hits":[...]} or {}).
cn_retrieve() {
  local query="${1:?query required}"
  local limit="${2:-8}"
  _cn_disabled && { echo "{}"; return 0; }
  cn_available || { echo "{}"; return 0; }
  local body
  body=$(python3 -c 'import json,sys; print(json.dumps({"query":sys.argv[1],"limit":int(sys.argv[2])}))' \
    "$query" "$limit")
  _cn_post_json "/api/v1/tools/retrieve" "$body"
}

# desc: Sessions touching a file path.
cn_sessions_by_file() {
  local path="${1:?path required}"
  _cn_disabled && { echo "{}"; return 0; }
  cn_available || { echo "{}"; return 0; }
  local encoded
  encoded=$(python3 -c 'import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1], safe=""))' "$path")
  _cn_get "/api/v1/sessions/by-file?path=$encoded"
}

# desc: Sessions matching a feature text (substring match server-side).
cn_sessions_by_feature() {
  local q="${1:?query required}"
  _cn_disabled && { echo "{}"; return 0; }
  cn_available || { echo "{}"; return 0; }
  local encoded
  encoded=$(python3 -c 'import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1], safe=""))' "$q")
  _cn_get "/api/v1/sessions/by-feature?q=$encoded"
}

# desc: Sessions matching an intent text.
cn_sessions_by_intent() {
  local q="${1:?query required}"
  _cn_disabled && { echo "{}"; return 0; }
  cn_available || { echo "{}"; return 0; }
  local encoded
  encoded=$(python3 -c 'import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1], safe=""))' "$q")
  _cn_get "/api/v1/sessions/by-intent?q=$encoded"
}

# desc: Attention inbox items.
cn_inbox() {
  local limit="${1:-10}"
  _cn_disabled && { echo "{}"; return 0; }
  cn_available || { echo "{}"; return 0; }
  _cn_get "/api/v1/inbox?limit=$limit"
}

# desc: Recent delivered features (since=24h default; optional layer filter).
cn_features_recent() {
  local since="${1:-24h}"
  local layer="${2:-}"
  _cn_disabled && { echo "{}"; return 0; }
  cn_available || { echo "{}"; return 0; }
  local q="since=$since"
  [ -n "$layer" ] && q="$q&layer=$layer"
  _cn_get "/api/v1/features?$q"
}

# desc: Fire-and-forget hook POST to CN. event is one of session_start,
#       user_prompt_submit, stop, subagent_stop, task_completed.
#       Never blocks; never fails the caller.
cn_hook_post() {
  local event="${1:?event required}"
  local session_id="${2:?session_id required}"
  local cwd="${3:-${PWD:-}}"
  local transcript="${4:-}"
  _cn_disabled && return 0
  cn_available || return 0
  local body
  body=$(python3 -c '
import json, sys
p = {"session_id": sys.argv[1], "hook_event_name": sys.argv[2]}
if sys.argv[3]: p["cwd"] = sys.argv[3]
if sys.argv[4]: p["transcript_path"] = sys.argv[4]
print(json.dumps(p))
' "$session_id" "$event" "$cwd" "$transcript" 2>/dev/null) || return 0
  curl -s -o /dev/null --max-time "$CN_HOOK_TIMEOUT_SEC" \
    -X POST "$CN_BASE_URL/api/v1/cc/hook/$event" \
    -H 'Content-Type: application/json' \
    -d "$body" >/dev/null 2>&1 &
  return 0
}

# desc: Render a compact markdown block from a cn_retrieve JSON payload.
#       Returns nothing when no hits — callers can append unconditionally.
cn_render_atoms_md() {
  local json="${1:?json required}"
  local limit="${2:-5}"
  python3 - "$json" "$limit" <<'PY' 2>/dev/null || true
import json, sys
try:
    data = json.loads(sys.argv[1])
except Exception:
    sys.exit(0)
hits = data.get("hits") or []
if not hits:
    sys.exit(0)
limit = int(sys.argv[2])
hits = hits[:limit]
print("--- ContextNest atoms (fresh substrate retrieval) ---")
print("Cross-session memory the planner should weigh before deciding:")
for h in hits:
    sim = h.get("similarity", 0)
    meta = h.get("metadata") or {}
    kind = meta.get("kind", "atom")
    ts = (meta.get("ts") or "")[:10]
    sid = h.get("session_id") or h.get("id", "")
    content = (h.get("content") or "").strip().replace("\n", " ")
    if len(content) > 280:
        content = content[:277] + "..."
    print(f"- [{kind} sim={sim:.2f} {ts} sess={sid[:8]}] {content}")
print("--- /ContextNest atoms ---")
PY
}
