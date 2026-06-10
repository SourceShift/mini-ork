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
  # v0.2-pt10 G-003 DDL session guard
  [ "${_MO_GRADIENT_SCHEMA_INIT:-0}" = "1" ] && return 0
  python3 - "${MINI_ORK_DB:?MINI_ORK_DB unset}" <<'PY'
import sqlite3, sys
con = sqlite3.connect(sys.argv[1])
con.execute("PRAGMA busy_timeout=5000")
con.execute("""
    CREATE TABLE IF NOT EXISTS gradient_records (
        gradient_id   TEXT PRIMARY KEY,
        target        TEXT NOT NULL,
        signal        TEXT NOT NULL,
        suggested_change TEXT NOT NULL,
        evidence      TEXT NOT NULL,
        confidence    REAL NOT NULL DEFAULT 0.0
                          CHECK(confidence BETWEEN 0.0 AND 1.0),
        created_at    INTEGER NOT NULL,
        task_class    TEXT
    )
""")
# Idempotent upgrade for DBs created before task_class existed. The old
# `target LIKE %task_class%` join in context_assembler almost never
# matched (targets are "workflow.node.plan" etc.) — learnings were
# unreachable at injection time without a real task_class column.
cols = [r[1] for r in con.execute("PRAGMA table_info(gradient_records)").fetchall()]
if "task_class" not in cols:
    con.execute("ALTER TABLE gradient_records ADD COLUMN task_class TEXT")
con.commit()
con.close()
PY
  _MO_GRADIENT_SCHEMA_INIT=1
  export _MO_GRADIENT_SCHEMA_INIT
}

# Default LLM-based extractor prompt template (heredoc — not in prompts/).
#
# v0.2-pt36 (D-048 fix, 2026-06-05): the original prompt asked "what
# algorithm needs fixing?" against traces that are mostly COORDINATION-
# SHAPED (audit recipes dispatching 4 lens nodes + a synthesizer; panel
# reviewers debating; doc-edit recipes). On those traces the extractor
# returned [] every cycle because the trace doesn't describe an
# algorithm at all — it describes who-dispatched-what.
#
# Reframed to ask "what about THIS RECIPE'S design would have made the
# OUTCOME better?" with a 5-target taxonomy that covers both
# algorithmic shapes (per-node improvements) AND coordination shapes
# (lens choices, synthesizer aggregation, verifier contract,
# recipe-level dispatch shape).
_GRADIENT_EXTRACTOR_PROMPT_TEMPLATE='You are a recipe-design improvement analyst.

Given the execution trace below, extract 0 to 5 textual gradients — specific,
actionable improvement signals. The trace may describe an algorithmic
workflow (planner → implementer → verifier) OR a coordination workflow
(4 lenses → synthesizer → publisher). Find what about THIS RECIPE design
would have made the outcome better.

Five target families (pick the most specific that fits each gradient):

  1. "workflow.node.<name>"           — algorithmic improvement to one node
                                        (e.g. planner missed a step, verifier
                                        accepted a bad artifact)
  2. "agent.<role>.prompt"            — prompt-level improvement (e.g. the
                                        lens prompt produced shallow output;
                                        the synthesizer prompt missed an axis)
  3. "workflow.edge.<name>"           — dependency / sequencing refinement
                                        (e.g. depends_on should be
                                        supplies_context_to; this edge needs
                                        a retries policy)
  4. "verifier.<name>"                — verifier-script logic gap
                                        (e.g. the grep-assert missed a
                                        boundary condition)
  5. "workflow.recipe.<recipe_name>"  — RECIPE-LEVEL shape suggestion when
                                        the issue is the dispatch topology
                                        itself, not any single node (e.g.
                                        the 4-lens panel has 2 lenses in the
                                        same family; the synthesizer should
                                        be a different family from any lens;
                                        a missing publisher node lets
                                        artifacts vanish from the audit)

TRACE:
<<<TRACE_JSON>>>

Important: if the trace is from a coordination-shaped recipe (audit,
synthesis, multi-lens debate), prefer "workflow.recipe.<name>" or
"agent.<role>.prompt" gradients — those are where the leverage actually
lives. Do NOT respond with [] just because no algorithmic bug stands out;
coordination shapes ALWAYS have a recipe-design improvement to surface
(family-diversity, verifier contract, synthesis aggregation, etc).

Respond ONLY with a JSON array of gradient objects. Each object must have:
  "target"          : string — one of the 5 target families above
  "signal"          : string — what was observed (1-2 sentences)
  "suggested_change": string — concrete recommendation (1-2 sentences)
  "confidence"      : number — 0.0 to 1.0

If after honest analysis you genuinely find nothing to improve (rare —
audit yourself before defaulting), respond with []. No prose, no markdown
fences, only the JSON array.'

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

def _extract_objects_balanced(text):
    """v0.2-pt24: brace-balanced extraction from possibly-truncated JSON
    array output. Walks the buffer object-by-object via raw_decode so a
    missing closing ] doesn't lose every gradient. Observed 2026-06-01:
    MiniMax-M3 produced 4 valid gradients but stopped mid-array before
    emitting ']' — the previous non-greedy regex /\[.*?\]/ failed to
    match and we lost ALL of them."""
    decoder = json.JSONDecoder()
    objs = []
    # Locate the first '[' or '{' — start of array or naked-objects stream.
    i = text.find('[')
    if i < 0:
        i = text.find('{')
    if i < 0:
        return objs
    # If we found a '[', advance past it.
    if text[i] == '[':
        i += 1
    n = len(text)
    while i < n:
        # Skip whitespace + commas
        while i < n and text[i] in ' \t\n\r,':
            i += 1
        if i >= n or text[i] == ']':
            break
        try:
            obj, end = decoder.raw_decode(text, i)
        except json.JSONDecodeError:
            break
        if isinstance(obj, dict):
            objs.append(obj)
        i = end
    return objs

try:
    items = json.loads(raw)
    if not isinstance(items, list):
        items = []
except json.JSONDecodeError:
    # First try the legacy non-greedy regex (still works for properly-closed arrays)
    m = re.search(r'\[.*?\]', raw, re.DOTALL)
    if m:
        try:
            items = json.loads(m.group())
        except Exception:
            items = _extract_objects_balanced(raw)
    else:
        # No closing ] anywhere — fall through to brace-balanced extraction.
        items = _extract_objects_balanced(raw)

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
con.execute("PRAGMA busy_timeout=5000")

# evidence is a trace_id — resolve task_class from the source trace so
# context_assembler can join learnings back to runs of the same class.
task_class = p.get("task_class") or ""
if not task_class:
    try:
        row = con.execute(
            "SELECT task_class FROM execution_traces WHERE trace_id = ?",
            (p["evidence"],),
        ).fetchone()
        task_class = row[0] if row and row[0] else ""
    except sqlite3.OperationalError:
        task_class = ""

con.execute("""
    INSERT INTO gradient_records (
        gradient_id, target, signal, suggested_change, evidence, confidence, created_at, task_class
    ) VALUES (?,?,?,?,?,?,?,?)
    ON CONFLICT(gradient_id) DO UPDATE SET
        signal=excluded.signal,
        suggested_change=excluded.suggested_change,
        confidence=excluded.confidence,
        task_class=COALESCE(NULLIF(excluded.task_class,''), gradient_records.task_class)
""", (
    gid,
    p["target"],
    p["signal"],
    p["suggested_change"],
    p["evidence"],
    float(p.get("confidence", 0.5)),
    now,
    task_class,
))
con.commit()
con.close()
print(gid)
PY
}

if [[ "${BASH_SOURCE[0]:-}" == "${0:-}" ]]; then
  echo "gradient_extractor.sh — source me and call gradient_extract / gradient_store"
fi
