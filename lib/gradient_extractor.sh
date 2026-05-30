#!/usr/bin/env bash
# gradient_extractor.sh — TextualGradient extraction from execution traces.
#
# Public API:
#   gradient_extract <trace_id>         → emits 0..N gradient JSON objects, one per line
#   gradient_store   <json_payload>     → stores a gradient record
#
# Gradient schema:
#   { target, signal, suggested_change, evidence, confidence }
#   target  : "workflow.node.<name>" | "agent.<role>.prompt" | "workflow.edge.<name>"
#   signal  : free-text observation
#   suggested_change : free-text recommendation
#   evidence: trace_id
#   confidence: 0.0–1.0
#
# Override: set MINI_ORK_GRADIENT_EXTRACTOR_FN to a bash function name to
#   replace the default LLM-based extractor.

[ "${0:-}" = "${BASH_SOURCE[0]:-}" ] && set -Eeuo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

# Ensure gradient_records table exists.
_gradient_ensure_table() {
  python3 - "${MINI_ORK_DB:?MINI_ORK_DB unset}" <<'PY'
import sqlite3, sys
con = sqlite3.connect(sys.argv[1])
con.execute("""
    CREATE TABLE IF NOT EXISTS gradient_records (
        gradient_id   TEXT PRIMARY KEY,
        target        TEXT NOT NULL,
        signal        TEXT NOT NULL,
        suggested_change TEXT NOT NULL,
        evidence      TEXT NOT NULL,
        confidence    REAL NOT NULL DEFAULT 0.0
                          CHECK(confidence BETWEEN 0.0 AND 1.0),
        created_at    INTEGER NOT NULL
    )
""")
con.commit()
con.close()
PY
}

# Default LLM-based extractor prompt template (heredoc — not in prompts/).
_GRADIENT_EXTRACTOR_PROMPT_TEMPLATE='You are a workflow improvement analyst.

Given the execution trace below, extract 0 to 5 textual gradients — specific,
actionable improvement signals for workflow nodes, agent prompts, or edges.

TRACE:
<<<TRACE_JSON>>>

Respond ONLY with a JSON array of gradient objects. Each object must have:
  "target"          : string — "workflow.node.<name>" | "agent.<role>.prompt" | "workflow.edge.<name>"
  "signal"          : string — what was observed (1-2 sentences)
  "suggested_change": string — concrete recommendation (1-2 sentences)
  "confidence"      : number — 0.0 to 1.0

If no improvements are identifiable, respond with [].
No prose, no markdown fences, only the JSON array.'

# desc: Extract gradients from a trace via LLM (or custom override function).
#       Emits one JSON gradient object per stdout line; empty if none found.
gradient_extract() {
  local trace_id="${1:?trace_id required}"

  # Fetch the trace JSON
  local trace_json
  # shellcheck source=lib/trace_store.sh
  source "${MINI_ORK_ROOT}/lib/trace_store.sh" 2>/dev/null || true
  if ! declare -f trace_get > /dev/null 2>&1; then
    echo "gradient_extract: trace_store.sh not loaded" >&2
    return 1
  fi
  trace_json="$(trace_get "$trace_id")"
  if [[ "$trace_json" == "null" ]]; then
    echo "gradient_extract: trace_id $trace_id not found" >&2
    return 1
  fi

  # Use override function if set
  if [[ -n "${MINI_ORK_GRADIENT_EXTRACTOR_FN:-}" ]]; then
    if declare -f "${MINI_ORK_GRADIENT_EXTRACTOR_FN}" > /dev/null 2>&1; then
      "${MINI_ORK_GRADIENT_EXTRACTOR_FN}" "$trace_id" "$trace_json"
      return $?
    else
      echo "gradient_extract: override fn ${MINI_ORK_GRADIENT_EXTRACTOR_FN} not defined" >&2
      return 1
    fi
  fi

  # Default: call LLM via llm-dispatch.sh
  # shellcheck source=lib/llm-dispatch.sh
  source "${MINI_ORK_ROOT}/lib/llm-dispatch.sh" 2>/dev/null || true
  if ! declare -f mo_llm_dispatch > /dev/null 2>&1; then
    echo "gradient_extract: llm-dispatch.sh not loaded" >&2
    return 1
  fi

  local prompt="${_GRADIENT_EXTRACTOR_PROMPT_TEMPLATE/<<<TRACE_JSON>>>/${trace_json}}"
  local tmp_out
  tmp_out="$(mktemp -t gradient_extract.XXXXXX)"
  local model="${MINI_ORK_GRADIENT_MODEL:-sonnet}"

  if ! mo_llm_dispatch "$model" "$prompt" "$tmp_out" 120 5; then
    echo "gradient_extract: LLM dispatch failed" >&2
    rm -f "$tmp_out" "${tmp_out}.err.log"
    return 1
  fi

  # Parse LLM output — extract JSON array, emit one object per line
  python3 - "$tmp_out" "$trace_id" <<'PY'
import json, sys, re

out_file = sys.argv[1]
trace_id = sys.argv[2]

try:
    raw = open(out_file).read().strip()
except OSError as e:
    print(f"gradient_extract: cannot read tmp file: {e}", file=sys.stderr)
    sys.exit(1)

# Strip markdown fences if present
raw = re.sub(r'^```[a-z]*\n?', '', raw, flags=re.MULTILINE)
raw = re.sub(r'\n?```$', '', raw, flags=re.MULTILINE)
raw = raw.strip()

try:
    items = json.loads(raw)
    if not isinstance(items, list):
        items = []
except json.JSONDecodeError:
    # Try to find array inside response
    m = re.search(r'\[.*?\]', raw, re.DOTALL)
    if m:
        try:
            items = json.loads(m.group())
        except Exception:
            items = []
    else:
        items = []

for item in items:
    if not isinstance(item, dict):
        continue
    item.setdefault("evidence", trace_id)
    item.setdefault("confidence", 0.5)
    print(json.dumps(item))
PY

  rm -f "$tmp_out" "${tmp_out}.err.log"
}

# desc: Store a gradient record. payload must contain: target, signal,
#       suggested_change, evidence. Optional: confidence (default 0.5).
gradient_store() {
  local payload="${1:?json_payload required}"
  _gradient_ensure_table
  python3 - "${MINI_ORK_DB:?MINI_ORK_DB unset}" "$payload" <<'PY'
import sqlite3, json, sys, uuid, time

db = sys.argv[1]
try:
    p = json.loads(sys.argv[2])
except json.JSONDecodeError as e:
    print(f"gradient_store: invalid JSON: {e}", file=sys.stderr)
    sys.exit(1)

gid = p.get("gradient_id") or f"gr-{uuid.uuid4().hex[:12]}"
now = int(time.time())

required = ("target", "signal", "suggested_change", "evidence")
for f in required:
    if not p.get(f):
        print(f"gradient_store: missing required field '{f}'", file=sys.stderr)
        sys.exit(1)

con = sqlite3.connect(db)
con.execute("""
    INSERT INTO gradient_records (
        gradient_id, target, signal, suggested_change, evidence, confidence, created_at
    ) VALUES (?,?,?,?,?,?,?)
    ON CONFLICT(gradient_id) DO UPDATE SET
        signal=excluded.signal,
        suggested_change=excluded.suggested_change,
        confidence=excluded.confidence
""", (
    gid,
    p["target"],
    p["signal"],
    p["suggested_change"],
    p["evidence"],
    float(p.get("confidence", 0.5)),
    now,
))
con.commit()
con.close()
print(gid)
PY
}

if [[ "${BASH_SOURCE[0]:-}" == "${0:-}" ]]; then
  echo "gradient_extractor.sh — source me and call gradient_extract / gradient_store"
fi
