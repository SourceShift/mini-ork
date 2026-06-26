#!/usr/bin/env bash
# context_assembler.sh — Bounded ContextPack builder.
#
# Public API:
#   context_assemble <task_brief_path> <workflow_node_name>
#       → emits ContextPack JSON on stdout
#
# ContextPack fields:
#   task_brief, relevant_files[], prior_similar_runs[],
#   known_failure_modes[], user_preferences, verifier_contract,
#   constraints[], forbidden_fallbacks[]
#
# Token budget: MINI_ORK_CTX_BUDGET_TOKENS (default 64000). Prefers
# recent/high-confidence items; truncates with summary marker.
# Every included item carries a cite: <source> field.
#
# Slice-provider seam (rlm-6 context-paging):
#   MINI_ORK_SLICE_PROVIDER selects which truncation policy runs.
#     default (unset) — legacy 64K-truncate, byte-for-byte compatible.
#     paged           — emits the first slice and tags the pack with
#                       _slice_provider / _next_slice_hint so the
#                       book-gen Body can fetch the next slice. Future
#                       work; this seam is the only thing rlm-6 ships.
#   Unknown provider names fall back to "default" so a misconfigured
#   caller cannot regress the eng-team consumer.

[ "${0:-}" = "${BASH_SOURCE[0]:-}" ] && set -Eeuo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

# desc: Build a bounded ContextPack for task_brief_path at workflow_node_name.
#       Queries task_memory and failure_memory tables in MINI_ORK_DB.
#       Emits JSON ContextPack on stdout.
context_assemble() {
  local task_brief_path="${1:?task_brief_path required}"
  local workflow_node="${2:?workflow_node_name required}"

  if [[ ! -f "$task_brief_path" ]]; then
    echo "context_assemble: task_brief_path not found: $task_brief_path" >&2
    return 1
  fi

  local brief_content
  brief_content="$(< "$task_brief_path")"
  local budget="${MINI_ORK_CTX_BUDGET_TOKENS:-64000}"

  # Load artifact contract if available
  local verifier_contract="{}"
  if declare -f artifact_contract_load > /dev/null 2>&1; then
    local task_class
    task_class="$(python3 -c "
import json, sys
try:
    d = json.loads(sys.argv[1])
    print(d.get('task_class',''))
except Exception:
    print('')
" "$brief_content" 2>/dev/null || echo "")"
    if [[ -n "$task_class" ]]; then
      verifier_contract="$(artifact_contract_load "$task_class" 2>/dev/null || echo '{}')"
    fi
  fi

  # Slice-provider seam (rlm-6 context-paging).
  # Default provider reproduces the legacy 64K-truncate behavior
  # byte-for-byte; paged provider is a stub for the book-gen Body to bind
  # groundChapterQuery's "next slice" fetch against. Selected via the
  # MINI_ORK_SLICE_PROVIDER env var; unknown names fall through to the
  # default so a misconfigured caller never regresses the eng-team consumer.
  local slice_provider="${MINI_ORK_SLICE_PROVIDER:-default}"
  python3 - \
    "${MINI_ORK_DB:?MINI_ORK_DB unset}" \
    "$brief_content" \
    "$workflow_node" \
    "$budget" \
    "$verifier_contract" \
    "$slice_provider" \
    <<'PY'
import sqlite3, json, sys, re, time

db          = sys.argv[1]
brief_raw   = sys.argv[2]
node_name   = sys.argv[3]
budget      = int(sys.argv[4])
vc_raw      = sys.argv[5]

def approx_tokens(s):
    """Rough estimate: 1 token ~ 4 chars."""
    return max(1, len(s) // 4)

try:
    brief = json.loads(brief_raw)
except Exception:
    brief = {"raw": brief_raw}

task_class = brief.get("task_class", "")
try:
    verifier_contract = json.loads(vc_raw)
except Exception:
    verifier_contract = {}

con = sqlite3.connect(db)
con.row_factory = sqlite3.Row

# --- Prior similar runs from execution_traces ---------------------------
# Exclude the CURRENT run's own traces (same guard as context_prior_runs_md)
# — the plan trace written moments earlier is not "prior" memory.
import os as _os
cur_run = _os.environ.get("MINI_ORK_RUN_ID", "")
prior_runs = []
try:
    rows = con.execute("""
        SELECT trace_id, task_class, status, cost_usd, duration_ms, created_at
        FROM execution_traces
        WHERE task_class = ?
          AND (? = '' OR run_id IS NULL OR run_id != ?)
        ORDER BY created_at DESC LIMIT 10
    """, (task_class, cur_run, cur_run)).fetchall()
    for r in rows:
        prior_runs.append({
            "cite": f"execution_traces/{r['trace_id']}",
            "trace_id": r["trace_id"],
            "status": r["status"],
            "cost_usd": r["cost_usd"],
            "duration_ms": r["duration_ms"],
            "created_at": r["created_at"],
        })
except Exception:
    pass

# --- Known failure modes from gradient_records -------------------------
# task_class column is the primary join (populated by gradient_store);
# the legacy target-substring match stays as fallback for rows stored
# before the column existed.
# E7: also merge __cross_class__ gradients — universal lessons fanned out
# from recurring targets across multiple task_classes. These get a higher
# weight in the assembler since they generalize.
failure_modes = []
try:
    rows = con.execute("""
        SELECT target, signal, suggested_change, confidence,
               (task_class = '__cross_class__') AS is_cross_class
        FROM gradient_records
        WHERE ((task_class = ? OR target LIKE ?)
               OR task_class = '__cross_class__')
          AND confidence >= 0.6
        ORDER BY is_cross_class DESC, confidence DESC LIMIT 10
    """, (task_class, f"%{task_class}%")).fetchall()
    for r in rows:
        scope = "cross_class" if r["is_cross_class"] else task_class
        failure_modes.append({
            "cite": f"gradient_records/{r['target']}",
            "target": r["target"],
            "signal": r["signal"],
            "suggested_change": r["suggested_change"],
            "confidence": r["confidence"],
            "scope": scope,
        })
except Exception:
    pass

# --- Similarity-retrieved prior observations (Track A item 1) -----------
# TF-IDF cosine over bug_reports + gradient_records + learning_record text
# columns, scored against the incoming task_brief. Pulls in lessons that
# don't match by exact task_class — exactly what "agent encounters a
# similar problem next time -> already knows the fix" needs.
similar_lessons = []
try:
    import math as _math, re as _re
    from collections import Counter as _Counter
    try:
        query_text = " ".join(filter(None, [
            brief.get("goal", "") if isinstance(brief, dict) else "",
            brief.get("title", "") if isinstance(brief, dict) else "",
            brief.get("description", "") if isinstance(brief, dict) else "",
            task_class,
        ]))
    except Exception:
        query_text = task_class
    def _stok(s):
        s = (s or "").lower()
        s = _re.sub(r"[^\w./_-]+", " ", s)
        return [t for t in s.split() if len(t) >= 3]
    def _stf(toks):
        c = _Counter(toks); total = sum(c.values()) or 1
        return {t: cnt/total for t, cnt in c.items()}
    def _scos(a, b):
        keys = set(a) | set(b)
        dot = sum(a.get(k,0)*b.get(k,0) for k in keys)
        na = _math.sqrt(sum(v*v for v in a.values()))
        nb = _math.sqrt(sum(v*v for v in b.values()))
        return dot/(na*nb) if na and nb else 0.0
    for tbl, col, kind in (
        ("bug_reports",      "title",  "bug"),
        ("gradient_records", "signal", "gradient"),
        ("learning_record",  "title",  "learning"),
    ):
        try:
            rows = con.execute(f"SELECT rowid AS rid, * FROM {tbl} LIMIT 2000").fetchall()
        except sqlite3.OperationalError:
            continue
        docs = [_stok((r[col] or "")) for r in rows]
        df = _Counter()
        for d in docs:
            for t in set(d): df[t] += 1
        N = max(len(docs), 1)
        idf = {t: _math.log(1.0 + N/(1+c)) for t,c in df.items()}
        def _svec(toks): return {t: w*idf.get(t,0.0) for t,w in _stf(toks).items()}
        q_vec = _svec(_stok(query_text))
        scored = []
        for r, d in zip(rows, docs):
            s = _scos(q_vec, _svec(d))
            if s >= 0.15: scored.append((s, r))
        scored.sort(reverse=True, key=lambda p: p[0])
        for s, r in scored[:3]:
            similar_lessons.append({
                "cite": f"{tbl}/{r['rid']}",
                "kind": kind,
                "score": round(s, 4),
                "title": (r[col] or "")[:200],
                "suggested_fix": (r["suggested_fix"]    if "suggested_fix"    in r.keys() else
                                  r["suggested_change"] if "suggested_change" in r.keys() else "") or "",
            })
except Exception:
    pass

# --- User preferences (from config if present) -------------------------
user_prefs = {}
try:
    import os
    cfg_path = os.environ.get("MINI_ORK_HOME", ".mini-ork") + "/config/user_preferences.json"
    with open(cfg_path) as f:
        user_prefs = json.load(f)
    user_prefs["cite"] = cfg_path
except Exception:
    pass

# --- Constraints and forbidden fallbacks from config -------------------
constraints = []
forbidden_fallbacks = []
try:
    import os
    cfg_path = os.environ.get("MINI_ORK_HOME", ".mini-ork") + "/config/constraints.json"
    with open(cfg_path) as f:
        cfg = json.load(f)
    constraints = cfg.get("constraints", [])
    forbidden_fallbacks = cfg.get("forbidden_fallbacks", [])
except Exception:
    pass

con.close()

# --- Token budget enforcement ------------------------------------------
# Slice-provider seam (rlm-6 context-paging). The default provider is the
# legacy 64K-truncate behavior extracted verbatim; the paged provider is a
# stub for the book-gen Body to bind groundChapterQuery's "next slice"
# fetch against. Both mutate `pack` in place and return it so callers can
# extend (e.g. tag metadata) without re-implementing the trim loops.
def slice_provider_default(pack, budget):
    """Trim pack to fit budget — legacy 64K-truncate behavior.

    Stable contract: when MINI_ORK_SLICE_PROVIDER is unset or "default"
    this function's output is byte-for-byte identical to the pre-rlm-6
    inline truncation. Dict insertion order is preserved so json.dumps
    produces the same key sequence. Do not reorder the keys below
    without rerunning tests/fixtures/slice_provider_smoke.sh.
    """
    serialized = json.dumps(pack)
    tokens_used = approx_tokens(serialized)

    if tokens_used > budget:
        # Trim prior_runs first, then failure_modes
        while tokens_used > budget and pack["prior_similar_runs"]:
            pack["prior_similar_runs"].pop()
            pack["_truncated"] = True
            tokens_used = approx_tokens(json.dumps(pack))

        while tokens_used > budget and pack["known_failure_modes"]:
            pack["known_failure_modes"].pop()
            pack["_truncated"] = True
            tokens_used = approx_tokens(json.dumps(pack))

        pack["_truncation_summary"] = (
            f"Context truncated to fit {budget} token budget; "
            f"oldest prior_runs and low-confidence failure_modes removed."
        )
    return pack

def slice_provider_paged(pack, budget):
    """On-demand slice provider stub.

    Falls through to the default trim, then tags the pack so a downstream
    caller (book-gen Body's groundChapterQuery) can fetch the next slice
    without re-running the assembler. The next-slice fetching itself is
    deliberately out of scope for rlm-6 — this task only exposes the
    seam. The marker keys (_slice_provider, _next_slice_hint) make the
    paged path observable so callers and tests can detect it.
    """
    pack = slice_provider_default(pack, budget)
    pack["_slice_provider"] = "paged"
    pack["_next_slice_hint"] = (
        "Fetch additional slices via context_assemble with the same "
        "MINI_ORK_SLICE_PROVIDER=paged and a follow-on cursor; this "
        "stub only emits the first slice."
    )
    return pack

# Dispatch: unknown provider names fall through to default so a
# misconfigured caller never regresses the eng-team consumer (plan risk
# note: "fail closed to existing behavior").
slice_provider_name = sys.argv[6] if len(sys.argv) > 6 else "default"
slice_providers = {
    "default": slice_provider_default,
    "paged":   slice_provider_paged,
}
slice_provider_fn = slice_providers.get(slice_provider_name, slice_provider_default)

pack = {
    "task_brief": {"content": brief, "cite": "task_brief_path"},
    "workflow_node": node_name,
    "verifier_contract": {"content": verifier_contract, "cite": "artifact_contract"},
    "prior_similar_runs": prior_runs,
    "known_failure_modes": failure_modes,
    "similar_lessons": similar_lessons,
    "user_preferences": user_prefs,
    "constraints": constraints,
    "forbidden_fallbacks": forbidden_fallbacks,
    "assembled_at": int(time.time()),
    "budget_tokens": budget,
}

pack = slice_provider_fn(pack, budget)
pack["tokens_estimated"] = approx_tokens(json.dumps(pack))
print(json.dumps(pack))
PY
}

# desc: Emit learned failure modes for task_class as a compact markdown block
#       suitable for direct prompt injection. Prints NOTHING when no learnings
#       exist (callers can append output unconditionally). Confidence floor 0.6.
context_failure_modes_md() {
  local task_class="${1:?task_class required}"
  local limit="${2:-5}"
  [ -n "${MINI_ORK_DB:-}" ] && [ -f "${MINI_ORK_DB:-}" ] || return 0
  # Project-scope filter (2026-06-13 fix). gradient_records collects lessons
  # from ALL prior runs across ALL projects in this mini-ork install. Without
  # filtering, mini-ork-self-referential lessons (target prefixes like
  # `workflow.`, `verifier.`, `gate.`, `recipe.`) leak into every project's
  # implementer prompt — diagnosed during CWT-A dispatch where codex went off
  # to fix mini-ork's own workflow components instead of the researcher
  # target. Skip framework-internal targets when MO_TARGET_CWD is set and
  # does NOT match MINI_ORK_ROOT (i.e. we're working on a non-mini-ork
  # project). When MO_TARGET_CWD is unset OR matches MINI_ORK_ROOT, fall
  # through to the legacy behavior so mini-ork's own self-improvement runs
  # still see their lessons.
  local _strip_framework_internal=0
  if [ -n "${MO_TARGET_CWD:-}" ] && [ -n "${MINI_ORK_ROOT:-}" ] && \
     [ "$(cd "${MO_TARGET_CWD}" 2>/dev/null && pwd -P)" != "$(cd "${MINI_ORK_ROOT}" && pwd -P)" ]; then
    _strip_framework_internal=1
  fi
  python3 - "$MINI_ORK_DB" "$task_class" "$limit" "$_strip_framework_internal" <<'PY'
import sqlite3, sys

db, task_class, limit, strip_framework = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
FRAMEWORK_INTERNAL_PREFIXES = (
    "workflow.", "verifier.", "gate.", "recipe.",
    "provenance.", "provider.", "cache.", "dispatcher.",
)
con = sqlite3.connect(db)
con.execute("PRAGMA busy_timeout=5000")
try:
    rows = con.execute("""
        SELECT target, signal, suggested_change
        FROM gradient_records
        WHERE (task_class = ? OR target LIKE ?) AND confidence >= 0.6
        ORDER BY confidence DESC, created_at DESC LIMIT ?
    """, (task_class, f"%{task_class}%", limit * 4 if strip_framework else limit)).fetchall()
except sqlite3.OperationalError:
    rows = []
finally:
    con.close()

if strip_framework:
    rows = [r for r in rows if not r[0].startswith(FRAMEWORK_INTERNAL_PREFIXES)][:limit]
else:
    rows = rows[:limit]

if rows:
    print("--- Learned failure modes (from prior runs of this task class) ---")
    print("Avoid repeating these known issues:")
    for target, signal, change in rows:
        print(f"- [{target}] {signal.strip()}")
        print(f"  Fix applied going forward: {change.strip()}")
    print("--- /learned failure modes ---")
PY
}

# desc: Emit operator-injected steering messages as a markdown block.
#       Reads unconsumed, unexpired rows from operator_steering targeted at
#       this run + role (or "any") and marks them consumed so the agent does
#       not see the same steering twice in a single dispatch. Prints
#       NOTHING when no steering is queued.
#
#       Args:
#         $1  role  e.g. "planner" | "implementer" | "reviewer" — agent role
#                   the calling node represents
context_operator_steering_md() {
  local role="${1:?role required}"
  [ -n "${MINI_ORK_DB:-}" ] && [ -f "${MINI_ORK_DB:-}" ] || return 0
  [ -f "$(dirname "${BASH_SOURCE[0]}")/operator_steering.sh" ] || return 0
  # shellcheck source=lib/operator_steering.sh
  . "$(dirname "${BASH_SOURCE[0]}")/operator_steering.sh"

  local jsonl
  jsonl="$(operator_steering_fetch_for "${MINI_ORK_RUN_ID:-}" "$role" 2>/dev/null)"
  [ -n "$jsonl" ] || return 0

  python3 - "$jsonl" <<'PY'
import json, sys
lines = [l for l in sys.argv[1].splitlines() if l.strip()]
if not lines:
    sys.exit(0)
print("--- Operator steering (injected supervisor guidance) ---")
print(f"{len(lines)} message(s) targeted at this node. Treat as load-bearing:")
for line in lines:
    try:
        r = json.loads(line)
        sev = r.get("severity","info").upper()
        src = r.get("source","unknown")
        print(f"- [{sev}] (from {src}) {r.get('message','')}")
    except Exception:
        continue
print("--- /operator steering ---")
PY
}

# desc: Emit prior same-task_class run outcomes as a compact markdown block
#       suitable for direct prompt injection (the prior_similar_runs slice of
#       the ContextPack, without the full context_assemble JSON envelope).
#       Excludes the CURRENT run's own traces via $MINI_ORK_RUN_ID — without
#       that, the classify trace written moments earlier shows up as its own
#       "prior" memory. Prints NOTHING when no prior runs exist.
context_prior_runs_md() {
  local task_class="${1:?task_class required}"
  local limit="${2:-5}"
  [ -n "${MINI_ORK_DB:-}" ] && [ -f "${MINI_ORK_DB:-}" ] || return 0
  python3 - "$MINI_ORK_DB" "$task_class" "$limit" "${MINI_ORK_RUN_ID:-}" <<'PY'
import sqlite3, sys

db, task_class, limit, cur_run = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4]
con = sqlite3.connect(db)
con.execute("PRAGMA busy_timeout=5000")
try:
    # One line per RUN, not per node trace — 5 node traces of one run are
    # noise, 5 runs are memory. Legacy rows with run_id NULL degrade to one
    # group per trace. Run status: failure if ANY node failed.
    rows = con.execute("""
        SELECT COALESCE(run_id, trace_id) AS run_key,
               COUNT(*) AS nodes,
               SUM(CASE WHEN status NOT IN ('success','running') THEN 1 ELSE 0 END) AS failed_nodes,
               SUM(COALESCE(cost_usd, 0)) AS total_cost,
               SUM(COALESCE(duration_ms, 0)) AS total_dur_ms,
               MAX(created_at) AS last_at
        FROM execution_traces
        WHERE task_class = ?
          AND (? = '' OR run_id IS NULL OR run_id != ?)
        GROUP BY run_key
        ORDER BY last_at DESC LIMIT ?
    """, (task_class, cur_run, cur_run, limit)).fetchall()
except sqlite3.OperationalError:
    rows = []
finally:
    con.close()

if rows:
    n_ok = sum(1 for r in rows if (r[2] or 0) == 0)
    print("--- Prior runs of this task class (memory) ---")
    print(f"{len(rows)} most recent: {n_ok} clean / {len(rows) - n_ok} with failures. "
          "Calibrate plan scope and verifier strictness against these outcomes:")
    for run_key, nodes, failed, cost, dur_ms, last_at in rows:
        outcome = "success" if (failed or 0) == 0 else f"{failed}/{nodes} nodes failed"
        cost_s = f"${cost:.2f}" if isinstance(cost, (int, float)) else "?"
        dur_s = f"{int(dur_ms) // 1000}s" if isinstance(dur_ms, (int, float)) else "?"
        print(f"- {run_key}: {outcome} ({nodes} nodes, cost {cost_s}, {dur_s})")
    print("--- /prior runs ---")
PY
}

# desc: Emit ContextNest atoms as a compact markdown block suitable for
#       direct prompt injection. PR-1 swap (2026-06-17): prefers CN's
#       /api/v1/prompt-context/capsule (kind-ordered clusters, ready
#       markdown) over the flat /tools/retrieve hit list. Falls back to
#       retrieve when capsule returns empty (substrate too fresh for
#       cluster aggregation, or filters drop all candidates). Degrades
#       silently when CN is down or MO_DISABLE_CN=1 (caller can append
#       unconditionally).
#
#       Args:
#         $1  task_brief_path  Path to brief JSON or markdown.
#         $2  limit            Max atoms to surface in retrieve fallback
#                              (default 5). Capsule has its own server-side
#                              cap controlled via /capsule?max_per_kind=.
#
#       Why capsule first: capsule deduplicates atoms into clusters and
#       orders them by what a planning agent most needs to know first
#       (risks → decisions → failures → directives → verifications →
#       evidence → reads → artifacts → assumptions). Same substrate,
#       strictly better signal for prompt injection.
#
#       Why retrieve fallback: capsule depends on atoms having `kind`
#       metadata; some legacy atoms don't. On a freshly-ingested or
#       under-populated substrate the capsule may return nothing where
#       retrieve still finds nearest-neighbour hits.
#
#       Why exists at all: mini-ork's planner has only ever seen local
#       sqlite memory (task_memory + failure_memory). ContextNest carries
#       the cross-session substrate fed by all Claude Code sessions in
#       this install. Without this fetch, the planner bakes outdated
#       assumptions into plans (see chapter-anchor drift audit, 2026-06-15).
context_contextnest_atoms_md() {
  local task_brief_path="${1:?task_brief_path required}"
  local limit="${2:-5}"
  [ "${MO_DISABLE_CN:-0}" = "1" ] && return 0
  [ -f "$task_brief_path" ] || return 0
  [ -f "${MINI_ORK_ROOT}/lib/cn_client.sh" ] || return 0
  # shellcheck source=cn_client.sh
  source "${MINI_ORK_ROOT}/lib/cn_client.sh"
  declare -f cn_retrieve >/dev/null 2>&1 || return 0
  cn_available || return 0

  # Build query from brief: prefer 'title' + first 200 chars of 'description'
  # or 'objective'. Fall back to whole file content. The query is what
  # CN sees as the substring filter (capsule) AND as the embedder input
  # (retrieve fallback), so more semantic context = better filtering.
  local query
  query=$(python3 - "$task_brief_path" <<'PY'
import json, sys
try:
    with open(sys.argv[1]) as f:
        raw = f.read()
    try:
        d = json.loads(raw)
    except Exception:
        print(raw[:512].strip())
        sys.exit(0)
    parts = []
    for k in ("title", "objective", "description", "task_class"):
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            parts.append(v.strip())
    if not parts:
        parts.append(raw[:512].strip())
    print(" ".join(parts)[:600])
except Exception:
    pass
PY
)
  [ -z "$query" ] && return 0

  # Try capsule first (kind-ordered markdown, strictly better signal).
  # Capsule's substring filter is case-insensitive over cluster normalized
  # text; passing the full multi-sentence brief query usually under-matches,
  # so feed it the first concept-shaped token for a more permissive filter.
  # Empty query also valid — returns the full deterministic capsule.
  #
  # Concept-shaped means: alphanumeric + dash/underscore, length >=4.
  # This skips markdown headers (`#`, `##`), bullets (`-`, `*`), code fences,
  # and short stopword-like tokens. Falls back to empty filter (full capsule)
  # when no qualifying token exists in the first 5 words of the brief.
  local capsule_query
  capsule_query=$(printf '%s' "$query" | awk '
    {
      for (i=1; i<=NF && i<=5; i++) {
        tok = $i
        # Strip leading + trailing non-alphanum
        gsub(/^[^A-Za-z0-9]+|[^A-Za-z0-9_-]+$/, "", tok)
        if (length(tok) >= 4) { print tok; exit }
      }
    }')
  local capsule_md
  if declare -f cn_capsule >/dev/null 2>&1; then
    capsule_md=$(cn_capsule "$capsule_query" "14d" 2>/dev/null)
    # Two-gate "is this capsule worth surfacing" check, then fall through
    # to retrieve if it fails either gate:
    #
    #   1. char floor (CN_CAPSULE_MIN_CHARS, default 100). The renderer
    #      always emits the "# Prompt Context" header even with zero
    #      clusters, so a near-header-only response is effectively empty.
    #   2. cluster presence. Each kind cluster renders a "## <heading>"
    #      line (prompt_context.rs). A capsule padded past the char floor
    #      by a long header/preamble but carrying NO "## " section is still
    #      contentless for a planner — retrieve's nearest-neighbour hits
    #      beat it. Without this gate such capsules were emitted as if
    #      substantive, starving the planner of the retrieve fallback.
    local min_chars="${CN_CAPSULE_MIN_CHARS:-100}"
    local section_count
    # grep -c already prints 0 on no match but exits 1; `|| true` swallows
    # that exit without the `|| echo 0` antipattern (which would append a
    # second "0" and corrupt the integer compare).
    section_count=$(printf '%s' "$capsule_md" | grep -c '^## ' || true)
    if [ "${#capsule_md}" -gt "$min_chars" ] && [ "${section_count:-0}" -ge 1 ]; then
      printf '%s\n%s\n%s\n' \
        "--- ContextNest capsule (kind-ordered substrate digest) ---" \
        "$capsule_md" \
        "--- /ContextNest capsule ---"
      return 0
    fi
  fi

  # Fallback: flat retrieve hits (pre-PR-1 behaviour). Kept so capsule-empty
  # cases (legacy atoms without kind metadata, freshly-ingested substrate)
  # still surface something.
  local hits_json
  hits_json=$(cn_retrieve "$query" "$limit" 2>/dev/null) || return 0
  cn_render_atoms_md "$hits_json" "$limit"
}

# desc: Emit a compact markdown block listing CN sessions that recently
#       touched files relevant to the brief. Helps planner notice "this
#       module was last edited 3 sessions ago for reason X" without
#       reading git log. Args: $1 = brief path, $2 = max files to probe.
context_contextnest_recent_sessions_md() {
  local task_brief_path="${1:?task_brief_path required}"
  local max_files="${2:-3}"
  [ "${MO_DISABLE_CN:-0}" = "1" ] && return 0
  [ -f "$task_brief_path" ] || return 0
  [ -f "${MINI_ORK_ROOT}/lib/cn_client.sh" ] || return 0
  # shellcheck source=cn_client.sh
  source "${MINI_ORK_ROOT}/lib/cn_client.sh"
  declare -f cn_sessions_by_file >/dev/null 2>&1 || return 0
  cn_available || return 0

  # Pull file hints from brief's 'files' or 'paths' array.
  local files_csv
  files_csv=$(python3 - "$task_brief_path" "$max_files" <<'PY'
import json, sys
try:
    with open(sys.argv[1]) as f:
        d = json.load(f)
    max_files = int(sys.argv[2])
    candidates = []
    for k in ("files", "paths", "relevant_files", "targets"):
        v = d.get(k)
        if isinstance(v, list):
            for item in v:
                if isinstance(item, str):
                    candidates.append(item)
                elif isinstance(item, dict):
                    p = item.get("path") or item.get("file") or item.get("name")
                    if isinstance(p, str):
                        candidates.append(p)
    if candidates:
        print(",".join(candidates[:max_files]))
except Exception:
    pass
PY
)
  [ -z "$files_csv" ] && return 0
  local any_output=0
  local IFS=','
  local f
  for f in $files_csv; do
    [ -z "$f" ] && continue
    local resp
    resp=$(cn_sessions_by_file "$f" 2>/dev/null) || continue
    local rendered
    rendered=$(python3 - "$resp" "$f" <<'PY' 2>/dev/null
import json, sys
try:
    d = json.loads(sys.argv[1])
except Exception:
    sys.exit(0)
sessions = d.get("sessions") or d.get("hits") or []
if not sessions:
    sys.exit(0)
print(f"- File `{sys.argv[2]}` recently touched in:")
for s in sessions[:3]:
    sid = s.get("session_id") or s.get("id", "")
    ts = (s.get("last_seen") or s.get("ts") or "")[:10]
    title = (s.get("title") or s.get("intent") or "").strip()[:80]
    print(f"  - {sid[:8]} ({ts}) {title}")
PY
)
    if [ -n "$rendered" ]; then
      if [ "$any_output" -eq 0 ]; then
        printf '%s\n' "--- ContextNest: recent sessions for relevant files ---"
        any_output=1
      fi
      printf '%s\n' "$rendered"
    fi
  done
  [ "$any_output" -eq 1 ] && printf '%s\n' "--- /ContextNest: recent sessions ---"
  return 0
}

# Active-State Index helper — HarnessBridge Technique 1 (arxiv:2606.12882).
# Wraps lib/active_state_index.sh:1 mo_active_state_block so the planner
# block at bin/mini-ork-plan:176 can call it through the same
# convention as the other context_*_md helpers.
context_active_state_md() {
  local task_class="${1:-__any__}"
  local days="${2:-30}"
  [ "${MO_DISABLE_ACTIVE_STATE:-0}" = "1" ] && return 0
  [ -f "${MINI_ORK_ROOT}/lib/active_state_index.sh" ] || return 0
  # shellcheck source=active_state_index.sh
  source "${MINI_ORK_ROOT}/lib/active_state_index.sh"
  declare -f mo_active_state_block >/dev/null 2>&1 || return 0
  mo_active_state_block "$task_class" "$days"
}

if [[ "${BASH_SOURCE[0]:-}" == "${0:-}" ]]; then
  echo "context_assembler.sh — source me and call context_assemble <task_brief_path> <node_name>"
fi
