#!/usr/bin/env bash
# context_role_packs.sh — Role-tailored ContextNest pack builder.
#
# PR-3 of the agent-context-pack epic. Maps mini-ork's 8 workflow node
# types to specific CN endpoint combinations so each role gets the
# slice of substrate it actually needs:
#
#   planner            → strategic (risks + decisions + topics + blockers)
#   researcher         → wide recall (semantic + feature history)
#   implementer/worker → tactical (recent editors, adjacent deliveries, graph neighbours)
#   reviewer/verifier  → failure-shaped (capsule filtered to failures + verifications)
#   reflector          → narrative (prior-run trajectories + same-intent runs)
#   publisher/rollback → shipment + blocker awareness
#
# Pre-PR-3 every role got the same flat retrieve query. The result was
# universally mediocre — planner missed risk_flags, implementer missed
# adjacent feature deliveries, verifier missed prior failures. The
# role-pack split lets each role consume the substrate at the right
# angle without changing the substrate itself.
#
# Public API:
#   context_role_pack_md <role> <task_brief_path> [files_csv]
#       → emits a multi-section markdown block on stdout
#       → empty when CN unreachable or MO_DISABLE_CN=1 (silent)
#
# Each role's pack composes 2-4 CN calls. Calls run sequentially (not
# concurrent) to keep the bash simple; latency total is bounded by
# CN_TIMEOUT_SEC * num_calls (default 8s * 4 = 32s worst case, far
# under planner LLM wall-clock).
#
# Falls back to the native generic context-assembler output when:
#   - role is unknown (no error, just generic)
#   - CN is unreachable (silent empty)
#   - all CN calls return empty (silent empty)

[ "${0:-}" = "${BASH_SOURCE[0]:-}" ] && set -Eeuo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

# Source dependencies. Guarded so re-sourcing doesn't redefine.
if ! declare -f cn_available >/dev/null 2>&1; then
  # shellcheck source=cn_client.sh
  source "${MINI_ORK_ROOT}/lib/cn_client.sh"
fi

# desc: Extract a short concept token from a task brief — used as the
#       capsule substring filter. For JSON briefs reads title/objective.
#       For markdown briefs (kickoffs) strips heading markers, code
#       fences and structural boilerplate (Kickoff/Phase/Goal/Wire...)
#       before picking a concept token, and prefers domain-shaped
#       hyphenated/underscored tokens (e.g. "grounded-rejection") which
#       are almost always the real subject — so the capsule is
#       task-scoped instead of matching kickoff boilerplate substrate-wide.
_role_pack_extract_query() {
  local task_brief_path="$1"
  [ -f "$task_brief_path" ] || { echo ""; return 0; }
  python3 - "$task_brief_path" <<'PY' 2>/dev/null
import json, sys, re

# Structural / filler words that must never become the scoping token —
# they match unrelated atoms across the whole substrate.
STOP = {
    "kickoff", "phase", "goal", "task", "wire", "into", "from", "with",
    "this", "that", "then", "plain", "english", "objective", "summary",
    "step", "steps", "title", "intro", "overview", "context", "change",
    "changes", "implement", "implementation", "ship", "shipped", "fix",
    "fixes", "make", "adds", "added", "using", "when", "where", "what",
    "which", "should", "would", "will", "must", "each", "their", "they",
    "have", "been", "does", "doing", "done", "onto", "over", "under",
}

def pick(text):
    # First concept token (len>=4) that isn't structural boilerplate. The
    # stopword filter is the whole point: a kickoff starts with
    # "Kickoff: Wire grounded-rejection ..." — skipping kickoff/wire lands
    # on "grounded-rejection" (the real subject) instead of "Kickoff",
    # which would match kickoff atoms substrate-wide.
    for raw_tok in text.split()[:60]:
        tok = re.sub(r"^[^A-Za-z0-9]+|[^A-Za-z0-9_-]+$", "", raw_tok)
        if len(tok) >= 4 and tok.lower() not in STOP:
            return tok
    return ""

try:
    with open(sys.argv[1]) as f:
        raw = f.read()
    try:
        d = json.loads(raw)
        parts = []
        for k in ("title", "objective", "description", "task_class"):
            v = d.get(k)
            if isinstance(v, str) and v.strip():
                parts.append(v.strip())
        text = " ".join(parts)[:600] if parts else raw[:512].strip()
    except Exception:
        # Markdown brief: drop heading markers, code fences and inline
        # backticks before tokenising so '#'/'```'/'`x`' don't pollute.
        lines = []
        for ln in raw.splitlines():
            s = re.sub(r"^\s*#+\s*", "", ln)   # heading markers
            if s.strip().startswith("```"):     # fenced code start/end
                continue
            lines.append(s)
        text = re.sub(r"`+", " ", "\n".join(lines))[:512].strip()
    out = pick(text)
    if out:
        print(out)
except Exception:
    pass
PY
}

# desc: Extract task_class from brief JSON; empty when not present or
#       brief is markdown.
_role_pack_extract_task_class() {
  local task_brief_path="$1"
  [ -f "$task_brief_path" ] || { echo ""; return 0; }
  python3 - "$task_brief_path" <<'PY' 2>/dev/null
import json, sys
try:
    with open(sys.argv[1]) as f:
        d = json.load(f)
    tc = d.get("task_class", "")
    if tc:
        print(tc)
except Exception:
    pass
PY
}

# Sub-pack: planner — strategic context (risks + decisions + topics + blockers).
_role_pack_planner() {
  local task_brief_path="$1"
  local query
  query=$(_role_pack_extract_query "$task_brief_path")
  local task_class
  task_class=$(_role_pack_extract_task_class "$task_brief_path")
  local emitted=0

  # 1. Capsule — full kind-ordered substrate digest, scoped to last 14d.
  if declare -f cn_capsule >/dev/null 2>&1; then
    local capsule
    capsule=$(cn_capsule "$query" "14d" 2>/dev/null)
    if [ "${#capsule}" -gt 100 ]; then
      printf '%s\n%s\n%s\n\n' \
        "--- ContextNest planner pack — substrate digest (capsule) ---" \
        "$capsule" \
        "--- /capsule ---"
      emitted=1
    fi
  fi

  # 2. Sessions by intent — "have we planned this task class before?"
  if [ -n "$task_class" ] && declare -f cn_sessions_by_intent >/dev/null 2>&1; then
    local sess_json
    sess_json=$(cn_sessions_by_intent "$task_class" 2>/dev/null)
    local rendered
    rendered=$(python3 - "$sess_json" <<'PY' 2>/dev/null
import json, sys
try:
    d = json.loads(sys.argv[1])
except Exception:
    sys.exit(0)
sessions = d.get("sessions") or d.get("matches") or d.get("hits") or []
if not sessions:
    sys.exit(0)
print("--- ContextNest planner pack — prior sessions with same intent ---")
for s in sessions[:5]:
    sid = (s.get("session_id") or s.get("id", ""))[:8]
    ts = (s.get("last_seen") or s.get("ts") or "")[:10]
    title = (s.get("title") or s.get("intent") or "").strip()[:100]
    print(f"- {sid} ({ts}) {title}")
print("--- /prior sessions ---")
PY
)
    if [ -n "$rendered" ]; then
      printf '%s\n\n' "$rendered"
      emitted=1
    fi
  fi

  # 3. Urgent inbox items — "what's blocking the user RIGHT NOW".
  if declare -f cn_inbox_filtered >/dev/null 2>&1; then
    local inbox_json
    inbox_json=$(cn_inbox_filtered "now" 5 2>/dev/null)
    local rendered
    rendered=$(cn_render_inbox_md "$inbox_json" 5 2>/dev/null)
    if [ -n "$rendered" ]; then
      printf '%s\n\n' "$rendered"
      emitted=1
    fi
  fi

  # 4. Basins — topic clusters dominating recent work in this project.
  if declare -f cn_basins >/dev/null 2>&1; then
    local basins_json
    basins_json=$(cn_basins "$PWD" 5 2>/dev/null)
    local rendered
    rendered=$(cn_render_basins_md "$basins_json" 5 2>/dev/null)
    if [ -n "$rendered" ]; then
      printf '%s\n\n' "$rendered"
      emitted=1
    fi
  fi

  return 0
}

# Sub-pack: researcher — wide semantic + textual recall.
_role_pack_researcher() {
  local task_brief_path="$1"
  local query
  query=$(_role_pack_extract_query "$task_brief_path")
  [ -z "$query" ] && return 0

  # 1. Broad retrieve — semantic top-N from the native context assembler.
  if declare -f cn_retrieve >/dev/null 2>&1; then
    local hits
    hits=$(cn_retrieve "$query" 8 2>/dev/null)
    local rendered
    rendered=$(cn_render_atoms_md "$hits" 8 2>/dev/null)
    if [ -n "$rendered" ]; then
      printf '%s\n\n' "$rendered"
    fi
  fi

  # 2. Sessions by feature — textual match across recent feature deliveries.
  if declare -f cn_sessions_by_feature >/dev/null 2>&1; then
    local sess_json
    sess_json=$(cn_sessions_by_feature "$query" 2>/dev/null)
    local rendered
    rendered=$(python3 - "$sess_json" <<'PY' 2>/dev/null
import json, sys
try:
    d = json.loads(sys.argv[1])
except Exception:
    sys.exit(0)
sessions = d.get("sessions") or d.get("matches") or d.get("hits") or []
if not sessions:
    sys.exit(0)
print("--- ContextNest researcher pack — sessions matching feature ---")
for s in sessions[:5]:
    sid = (s.get("session_id") or s.get("id", ""))[:8]
    ts = (s.get("last_seen") or s.get("ts") or "")[:10]
    title = (s.get("title") or s.get("intent") or s.get("feature") or "").strip()[:100]
    print(f"- {sid} ({ts}) {title}")
print("--- /sessions by feature ---")
PY
)
    [ -n "$rendered" ] && printf '%s\n\n' "$rendered"
  fi

  return 0
}

# Sub-pack: implementer/worker — tactical (file editors, adjacent
# deliveries, graph neighbours of the top hit).
_role_pack_implementer() {
  local task_brief_path="$1"
  local files_csv="${2:-}"

  # 1. Recent sessions per file in scope.
  if [ -n "$files_csv" ]; then
    if declare -f context_contextnest_recent_sessions_md >/dev/null 2>&1; then
      local rendered
      rendered=$(context_contextnest_recent_sessions_md "$task_brief_path" 4 2>/dev/null)
      [ -n "$rendered" ] && printf '%s\n\n' "$rendered"
    else
      local rendered
      rendered=$(PYTHONPATH="${MINI_ORK_ROOT}:${PYTHONPATH:-}" \
        python3 -m mini_ork.context_assembler \
          contextnest-recent-sessions "$task_brief_path" 4 2>/dev/null || true)
      [ -n "$rendered" ] && printf '%s\n\n' "$rendered"
    fi
  fi

  # 2. Features delivered in the last 48h — avoid re-doing recent work.
  if declare -f cn_features_recent >/dev/null 2>&1; then
    local feats_json
    feats_json=$(cn_features_recent "48h" "" 2>/dev/null)
    local rendered
    rendered=$(cn_render_features_md "$feats_json" "$PWD" 6 2>/dev/null)
    [ -n "$rendered" ] && printf '%s\n\n' "$rendered"
  fi

  # 3. Graph neighbours of the TOP retrieve hit — adjacent context.
  local query
  query=$(_role_pack_extract_query "$task_brief_path")
  if [ -n "$query" ] && declare -f cn_retrieve >/dev/null 2>&1 && declare -f cn_connections_for >/dev/null 2>&1; then
    local hits
    hits=$(cn_retrieve "$query" 1 2>/dev/null)
    local top_id
    top_id=$(python3 -c '
import json, sys
try:
    d = json.loads(sys.argv[1])
    hits = d.get("hits", [])
    if hits:
        print(hits[0].get("id", ""))
except Exception:
    pass
' "$hits" 2>/dev/null)
    if [ -n "$top_id" ]; then
      local conn_json
      conn_json=$(cn_connections_for "$top_id" 6 2>/dev/null)
      local rendered
      rendered=$(python3 - "$conn_json" "$top_id" <<'PY' 2>/dev/null
import json, sys
try:
    d = json.loads(sys.argv[1])
except Exception:
    sys.exit(0)
edges = d.get("edges") or d.get("connections") or d.get("neighbours") or []
if not edges:
    sys.exit(0)
print(f"--- ContextNest implementer pack — graph neighbours of top hit {sys.argv[2][:12]} ---")
for e in edges[:6]:
    neighbor = (e.get("neighbor_id") or e.get("to") or e.get("id", ""))[:12]
    weight = e.get("weight") or e.get("similarity") or 0
    label = (e.get("label") or e.get("snippet") or "").strip()[:120]
    print(f"- [{neighbor} w={weight:.2f}] {label}" if isinstance(weight, float) else f"- [{neighbor} w={weight}] {label}")
print("--- /graph neighbours ---")
PY
)
      [ -n "$rendered" ] && printf '%s\n\n' "$rendered"
    fi
  fi

  return 0
}

# Sub-pack: reviewer/verifier — failures + verifications. Capsule has
# no kind filter, so render full capsule then post-hoc grep to keep
# only the relevant sections.
_role_pack_reviewer() {
  local task_brief_path="$1"
  local query
  query=$(_role_pack_extract_query "$task_brief_path")

  if declare -f cn_capsule >/dev/null 2>&1; then
    local capsule
    capsule=$(cn_capsule "$query" "30d" 2>/dev/null)
    if [ "${#capsule}" -gt 100 ]; then
      # Post-hoc filter: keep only Failures / Verifications / Risks sections.
      local filtered
      filtered=$(printf '%s\n' "$capsule" | awk '
        /^## Failures to avoid/ { keep=1; print; next }
        /^## Verifications run/ { keep=1; print; next }
        /^## Risks/ { keep=1; print; next }
        /^## / { keep=0; next }
        keep { print }
      ')
      if [ "${#filtered}" -gt 50 ]; then
        printf '%s\n%s\n%s\n\n' \
          "--- ContextNest reviewer pack — failures + verifications + risks ---" \
          "$filtered" \
          "--- /reviewer pack ---"
      fi
    fi
  fi

  return 0
}

# Sub-pack: reflector — narrative (prior runs same task_class +
# same-intent sessions). Heavy on cross-session reads.
_role_pack_reflector() {
  local task_brief_path="$1"
  local task_class
  task_class=$(_role_pack_extract_task_class "$task_brief_path")

  # 1. Sessions by intent (same task_class).
  if [ -n "$task_class" ] && declare -f cn_sessions_by_intent >/dev/null 2>&1; then
    local sess_json
    sess_json=$(cn_sessions_by_intent "$task_class" 2>/dev/null)
    local rendered
    rendered=$(python3 - "$sess_json" <<'PY' 2>/dev/null
import json, sys
try:
    d = json.loads(sys.argv[1])
except Exception:
    sys.exit(0)
sessions = d.get("sessions") or d.get("matches") or d.get("hits") or []
if not sessions:
    sys.exit(0)
print("--- ContextNest reflector pack — prior runs of this task class ---")
for s in sessions[:5]:
    sid = (s.get("session_id") or s.get("id", ""))[:8]
    ts = (s.get("last_seen") or s.get("ts") or "")[:10]
    title = (s.get("title") or s.get("intent") or "").strip()[:100]
    print(f"- {sid} ({ts}) {title}")
print("--- /prior runs ---")
PY
)
    [ -n "$rendered" ] && printf '%s\n\n' "$rendered"
  fi

  # 2. Capsule digest — full kind-ordered, longer window.
  if declare -f cn_capsule >/dev/null 2>&1; then
    local capsule
    capsule=$(cn_capsule "" "30d" 2>/dev/null)
    if [ "${#capsule}" -gt 100 ]; then
      printf '%s\n%s\n%s\n\n' \
        "--- ContextNest reflector pack — 30d substrate digest ---" \
        "$capsule" \
        "--- /reflector digest ---"
    fi
  fi

  return 0
}

# Sub-pack: publisher/rollback — shipment + blocker awareness.
_role_pack_publisher() {
  local task_brief_path="$1"

  # 1. Features shipped in last 24h.
  if declare -f cn_features_recent >/dev/null 2>&1; then
    local feats_json
    feats_json=$(cn_features_recent "24h" "" 2>/dev/null)
    local rendered
    rendered=$(cn_render_features_md "$feats_json" "$PWD" 10 2>/dev/null)
    [ -n "$rendered" ] && printf '%s\n\n' "$rendered"
  fi

  # 2. Inbox — all urgency tiers, publisher needs full picture.
  if declare -f cn_inbox_filtered >/dev/null 2>&1; then
    local inbox_json
    inbox_json=$(cn_inbox_filtered "" 10 2>/dev/null)
    local rendered
    rendered=$(cn_render_inbox_md "$inbox_json" 10 2>/dev/null)
    [ -n "$rendered" ] && printf '%s\n\n' "$rendered"
  fi

  return 0
}

# desc: PUBLIC ENTRY POINT. Dispatch table mapping workflow node type
#       to the appropriate sub-pack. Falls back to generic
#       the native context-assembler CLI for unknown roles.
#
#       Args:
#         $1  role            (planner|researcher|implementer|worker|reviewer|verifier|reflector|publisher|rollback)
#         $2  task_brief_path
#         $3  files_csv       (optional, used only by implementer pack)
#
#       Returns: stdout = multi-section markdown. Empty when CN
#       unreachable, MO_DISABLE_CN=1, or all sub-calls return empty.
#
#       Why bash dispatch table not configuration: each role's pack
#       composition is a deliberate design choice (which endpoints +
#       in what order). Configuration would let users compose
#       incoherent packs. Code dispatch keeps the role-to-endpoint
#       mapping in one place + reviewable.
context_role_pack_md() {
  local role="${1:?role required}"
  local task_brief_path="${2:?task_brief_path required}"
  local files_csv="${3:-}"

  [ "${MO_DISABLE_CN:-0}" = "1" ] && return 0
  [ -f "$task_brief_path" ] || return 0

  # Source CN client + render helpers (idempotent re-source).
  [ -f "${MINI_ORK_ROOT}/lib/cn_client.sh" ] || return 0
  # shellcheck source=cn_client.sh
  source "${MINI_ORK_ROOT}/lib/cn_client.sh"
  declare -f cn_available >/dev/null 2>&1 || return 0
  cn_available || return 0

  case "$role" in
    planner)
      _role_pack_planner "$task_brief_path"
      ;;
    researcher)
      _role_pack_researcher "$task_brief_path"
      ;;
    implementer|worker)
      _role_pack_implementer "$task_brief_path" "$files_csv"
      ;;
    reviewer|verifier)
      _role_pack_reviewer "$task_brief_path"
      ;;
    reflector)
      _role_pack_reflector "$task_brief_path"
      ;;
    publisher|rollback)
      _role_pack_publisher "$task_brief_path"
      ;;
    *)
      # Unknown role → fall back to the canonical native context helper.
      if declare -f context_contextnest_atoms_md >/dev/null 2>&1; then
        context_contextnest_atoms_md "$task_brief_path" 6
      else
        PYTHONPATH="${MINI_ORK_ROOT}:${PYTHONPATH:-}" \
          python3 -m mini_ork.context_assembler \
            contextnest-atoms "$task_brief_path" 6 2>/dev/null || true
      fi
      ;;
  esac
}

if [[ "${BASH_SOURCE[0]:-}" == "${0:-}" ]]; then
  echo "context_role_packs.sh — source me and call context_role_pack_md <role> <task_brief_path> [files_csv]"
fi
