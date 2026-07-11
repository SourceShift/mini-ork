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

# desc: Decide whether task_class identifies a framework-internal agent
#       (e.g. `__reflect__`). Framework self-traces add no learning signal
#       and only bloat the gradient table — callers should skip them before
#       paying the LLM dispatch cost. Returns 0 (skip) for task_class
#       beginning with `__`, 1 (extract) otherwise. Pure string match on
#       the task_class string — no DB hit, no schema dependency.
#       In scope: any agent whose name is wrapped in double underscores.
#       Out of scope: regular task_classes (no leading `__`).
_gradient_is_framework_agent() {
  local task_class="${1:-}"
  [[ -z "$task_class" ]] && return 1
  case "$task_class" in
    __*) return 0 ;;
    *)    return 1 ;;
  esac
}

# desc: Per-trace watermark check. Returns 0 (skip — already linked) when
#       at least one gradient_records row exists whose evidence equals the
#       given trace_id; returns 1 (extract) otherwise. Idempotency contract:
#       re-running `reflection_extract_gradients` over an overlapping
#       `--since` window must not duplicate rows for traces that already
#       produced a gradient in an earlier run. Scoped per-(task_class, trace)
#       — a trace_id already linked under a different task_class counts as
#       "seen" (per risk_notes: don't widen across task_class to avoid
#       silently dropping legitimate same-trace, different-class gradients).
_gradient_check_watermark() {
  local trace_id="${1:?trace_id required}"
  python3 - "${MINI_ORK_DB:?MINI_ORK_DB unset}" "$trace_id" <<'PY' >/dev/null 2>&1
import sqlite3, sys
db, tid = sys.argv[1], sys.argv[2]
con = sqlite3.connect(db)
try:
    row = con.execute(
        "SELECT 1 FROM gradient_records WHERE evidence = ? LIMIT 1",
        (tid,),
    ).fetchone()
except sqlite3.OperationalError:
    row = None
con.close()
sys.exit(0 if row else 1)
PY
}

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
  if ! declare -f llm_dispatch > /dev/null 2>&1; then
    echo "gradient_extract: llm-dispatch.sh not loaded" >&2
    return 1
  fi

  local prompt="${_GRADIENT_EXTRACTOR_PROMPT_TEMPLATE/<<<TRACE_JSON>>>/${trace_json}}"
  local tmp_out
  tmp_out="$(mktemp -t gradient_extract.XXXXXX)"
  local model="${MINI_ORK_GRADIENT_MODEL:-codex}"

  # Route via the llm_dispatch shim (not mo_llm_dispatch directly) so this
  # path emits llm_calls ledger rows + respects the cost circuit breaker.
  # Shim cats the out-file to stdout; silence it — we parse $tmp_out below.
  if ! llm_dispatch --model "$model" --node-type gradient-extract \
         --prompt-text "$prompt" --out "$tmp_out" \
         --timeout 120 --max-turns 5 >/dev/null; then
    echo "gradient_extract: LLM dispatch failed" >&2
    rm -f "$tmp_out" "${tmp_out}.err.log" "${tmp_out}.shim.err"
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
import sqlite3, json, sys, uuid, time, os, re

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

# --- Cross-target dedup ---------------------------------------------------
# The LLM extractor often mints the SAME underlying issue under several
# targets (e.g. workflow.node.X and workflow.node.Y both flagging missing
# run_id stamps). Each copy passes the confidence floor and gets injected
# into prompts — redundant tokens, no extra signal. Before inserting,
# compare against same-task_class records by stopword-filtered token
# CONTAINMENT (overlap / smaller set — robust to length asymmetry, unlike
# plain Jaccard which saturates low on multi-sentence text); on a hit,
# absorb into the existing record (keep max confidence) instead of
# inserting. Measured on live data: true dups score >=0.69, the ambiguous
# plateau sits at ~0.58, so the 0.65 default only merges unambiguous
# copies — a false merge permanently loses a learning, a miss just costs
# tokens. MO_GRADIENT_DEDUP_SIM tunes the threshold; 0 disables.
_STOP = set(
    "the a an of to in on for and or is are was were be been it its this"
    " that with not no when even though so as by from at into our we you"
    " their".split()
)

def _tokens(s):
    return set(re.findall(r"[a-z0-9_]+", s.lower())) - _STOP

_thresh = float(os.environ.get("MO_GRADIENT_DEDUP_SIM", "0.65"))
if _thresh > 0 and not p.get("gradient_id"):
    new_tok = _tokens(f"{p['signal']} {p['suggested_change']}")
    try:
        rows = con.execute(
            "SELECT gradient_id, target, signal, suggested_change, confidence"
            " FROM gradient_records WHERE task_class = ?",
            (task_class,),
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    for gid_e, target_e, sig_e, chg_e, conf_e in rows:
        if target_e == p["target"]:
            continue
        old_tok = _tokens(f"{sig_e} {chg_e}")
        if not new_tok or not old_tok:
            continue
        sim = len(new_tok & old_tok) / min(len(new_tok), len(old_tok))
        if sim >= _thresh:
            con.execute(
                "UPDATE gradient_records SET confidence = ? WHERE gradient_id = ?",
                (max(float(conf_e or 0), float(p.get("confidence", 0.5))), gid_e),
            )
            con.commit()
            con.close()
            print(
                f"gradient_store: dedup sim={sim:.2f} target={p['target']}"
                f" absorbed into {target_e} [{gid_e}]",
                file=sys.stderr,
            )
            print(gid_e)
            sys.exit(0)

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
