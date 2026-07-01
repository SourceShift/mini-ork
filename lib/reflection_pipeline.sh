#!/usr/bin/env bash
# reflection_pipeline.sh — Background reflection orchestrator.
#
# Each step is independently callable so users can override any one.
#
# Public API:
#   reflection_extract_gradients  <since_ts>
#   reflection_deduplicate        <gradients_table>
#   reflection_link_failures      <failure_table>
#   reflection_detect_stale       <memory_table>
#   reflection_summarize_patterns <cluster_id>
#   reflection_suggest_promotions <patterns_table>
#   reflection_run                <since_ts>   ← orchestrates all 6

[ "${0:-}" = "${BASH_SOURCE[0]:-}" ] && set -Eeuo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

_rfl_now() { date +%s; }

# desc: Extract gradients from all execution_traces created after since_ts
#       (unix epoch). Emits gradient JSON objects on stdout via gradient_extractor.
reflection_extract_gradients() {
  local since_ts="${1:-0}"
  # shellcheck source=lib/gradient_extractor.sh
  source "${MINI_ORK_ROOT}/lib/gradient_extractor.sh" 2>/dev/null || true
  # shellcheck source=lib/trace_store.sh
  source "${MINI_ORK_ROOT}/lib/trace_store.sh" 2>/dev/null || true

  # v0.2-pt11.5 (D-043): defensive table-ensure. `_gradient_ensure_table`
  # was only fired lazily by `gradient_store`. If LLM extracts 0
  # gradients (legitimate when traces are sparse), `gradient_store` is
  # never called, table never created, and downstream pipeline steps
  # (deduplicate / detect_stale / suggest_promotions) crash with
  # "no such table: gradient_records". Pre-create at extract start
  # so the pipeline can traverse cleanly even on empty-gradient runs.
  if declare -f _gradient_ensure_table >/dev/null 2>&1; then
    _gradient_ensure_table 2>/dev/null || true
  fi

  local trace_ids
  # v0.2-pt7 (R6/F-17): bounded fetchall — unbounded SELECT * FROM
  # execution_traces with no LIMIT was an O(N) memory bomb at 10M
  # rows/day. Default cap MO_REFLECTION_BATCH=500 traces/run; rerun
  # reflection_extract_gradients with newer since_ts to process more.
  local _batch="${MO_REFLECTION_BATCH:-500}"
  trace_ids="$(python3 - "${MINI_ORK_DB:?MINI_ORK_DB unset}" "$since_ts" "$_batch" <<'PY'
import sqlite3, json, sys
con = sqlite3.connect(sys.argv[1])
con.execute("PRAGMA busy_timeout=5000")  # v0.2-pt7 F-11
# v0.2-pt11 (D-039 follow): execution_traces.created_at is TEXT (ISO-8601
# per migration 0010 default), caller passes unix-ts INT — compare via
# strftime('%s', created_at) cast to int.
rows = con.execute(
    "SELECT trace_id FROM execution_traces WHERE CAST(strftime('%s', created_at) AS INTEGER) >= ? ORDER BY created_at LIMIT ?",
    (int(sys.argv[2]), int(sys.argv[3]))
).fetchall()
con.close()
for r in rows:
    print(r[0])
PY
)"

  local extracted=0
  while IFS= read -r tid; do
    [[ -z "$tid" ]] && continue
    while IFS= read -r gradient; do
      [[ -z "$gradient" ]] && continue
      gradient_store "$gradient" >/dev/null || true
      echo "$gradient"
      (( extracted++ )) || true
    # </dev/null: the claude CLI inside gradient_extract slurps inherited
    # stdin (the herestring feeding the outer loop) — without this the
    # first LLM call eats all remaining trace ids and the loop stops
    # after one trace.
    done < <(gradient_extract "$tid" </dev/null || true)
  done <<< "$trace_ids"

  echo "reflection_extract_gradients: extracted ${extracted} gradients since ${since_ts}" >&2
}

# desc: Deduplicate gradient_records table. Pass 1 merges identical
#       target+signal pairs; pass 2 fuzzy-merges semantically-similar signals
#       within (task_class, target) groups (difflib ratio on signal >=
#       MO_DEDUP_FUZZY, default 0.55). Highest confidence wins in both passes.
#       gradients_table defaults to "gradient_records".
reflection_deduplicate() {
  local gradients_table="${1:-gradient_records}"
  # v0.2-pt7 (R6/F-18): bounded fetchall to prevent OOM at 10M+ rows.
  # Default cap MO_DEDUP_BATCH=10000; oldest-first ordering ensures
  # repeated runs eventually process the whole table without OOM.
  local _batch="${MO_DEDUP_BATCH:-10000}"
  local _fuzzy="${MO_DEDUP_FUZZY:-0.55}"
  python3 - "${MINI_ORK_DB:?MINI_ORK_DB unset}" "$gradients_table" "$_batch" "$_fuzzy" <<'PY'
import sqlite3, sys
from difflib import SequenceMatcher

db, tbl, batch, fuzzy = sys.argv[1], sys.argv[2], int(sys.argv[3]), float(sys.argv[4])
con = sqlite3.connect(db)
con.execute("PRAGMA busy_timeout=5000")  # v0.2-pt7 F-11
cols = {r[1] for r in con.execute(f"PRAGMA table_info({tbl})").fetchall()}
task_class_expr = "task_class" if "task_class" in cols else "''"
# LIMIT prevents O(table) memory bomb; repeated runs process all rows.
rows = con.execute(f"""
    SELECT gradient_id, target, signal, suggested_change, confidence,
           COALESCE({task_class_expr},'')
    FROM {tbl}
    ORDER BY confidence DESC, created_at ASC
    LIMIT ?
""", (batch,)).fetchall()

to_delete = []

# Pass 1 — exact (target, signal) merge, global across task_classes.
# Rows arrive confidence-desc so the first seen per key is the keeper.
seen = {}
survivors = []
for row in rows:
    gid, tgt, sig = row[0], row[1], row[2]
    key = (tgt, sig)
    if key in seen:
        to_delete.append(gid)
    else:
        seen[key] = gid
        survivors.append(row)

# Pass 2 — fuzzy merge within (task_class, target) groups. Gradients from
# repeated reflect runs phrase the same lesson slightly differently
# ("verifier_output is an empty object '{}'" vs "verifier_output is an
# empty object, meaning..."); exact-match dedup never catches these so the
# table grows by ~5 near-identical rows per lifecycle. Greedy
# confidence-desc scan: a row whose SIGNAL is similar to an already-kept
# representative is deleted. Calibrated on live data 2026-06-10:
# signal-only char ratio separates cleanly (cross-lesson pairs max 0.48,
# same-lesson rephrases >= 0.55); including suggested_change in the
# comparison muddied it (same-lesson pairs dropped to ~0.60 because
# prescription phrasing diverges more than diagnosis phrasing).
groups = {}
for row in survivors:
    groups.setdefault((row[5], row[1]), []).append(row)

fuzzy_deleted = 0
for grp in groups.values():
    kept_texts = []
    for gid, _tgt, sig, _change, _conf, _tc in grp:
        text = sig
        sm = SequenceMatcher(b=text, autojunk=False)
        dup = False
        for kt in kept_texts:
            sm.set_seq1(kt)
            if sm.real_quick_ratio() >= fuzzy and sm.quick_ratio() >= fuzzy \
               and sm.ratio() >= fuzzy:
                dup = True
                break
        if dup:
            to_delete.append(gid)
            fuzzy_deleted += 1
        else:
            kept_texts.append(text)

if to_delete:
    placeholders = ",".join("?" * len(to_delete))
    con.execute(f"DELETE FROM {tbl} WHERE gradient_id IN ({placeholders})", to_delete)
    con.commit()
    exact = len(to_delete) - fuzzy_deleted
    print(f"reflection_deduplicate: removed {len(to_delete)} duplicates "
          f"({exact} exact, {fuzzy_deleted} fuzzy@{fuzzy})", file=sys.stderr)
else:
    print("reflection_deduplicate: no duplicates found", file=sys.stderr)
con.close()
PY
}

# desc: Correlate failure-status traces with gradient targets; update a
#       failure_links table. failure_table defaults to "execution_traces".
reflection_link_failures() {
  local failure_table="${1:-execution_traces}"
  python3 - "${MINI_ORK_DB:?MINI_ORK_DB unset}" "$failure_table" <<'PY'
import sqlite3, json, sys, time
db, ftbl = sys.argv[1], sys.argv[2]
con = sqlite3.connect(db)
con.execute("""
    CREATE TABLE IF NOT EXISTS failure_links (
        link_id      TEXT PRIMARY KEY,
        trace_id     TEXT NOT NULL,
        gradient_id  TEXT,
        task_class   TEXT,
        linked_at    INTEGER NOT NULL
    )
""")
now = int(time.time())
failures = con.execute(
    f"SELECT trace_id, task_class FROM {ftbl} WHERE status='failure'"
).fetchall()
inserted = 0
for tid, tc in failures:
    gradients = con.execute(
        "SELECT gradient_id FROM gradient_records WHERE evidence=?", (tid,)
    ).fetchall()
    for (gid,) in gradients:
        link_id = f"fl-{tid[:8]}-{gid[:8]}"
        con.execute("""
            INSERT OR IGNORE INTO failure_links (link_id, trace_id, gradient_id, task_class, linked_at)
            VALUES (?,?,?,?,?)
        """, (link_id, tid, gid, tc, now))
        inserted += 1
con.commit()
con.close()
print(f"reflection_link_failures: {inserted} links created/verified", file=sys.stderr)
PY
}

# desc: Detect stale memory entries (not updated within MINI_ORK_STALE_DAYS,
#       default 14). memory_table is the table name to inspect.
reflection_detect_stale() {
  local memory_table="${1:?memory_table required}"
  local stale_days="${MINI_ORK_STALE_DAYS:-14}"
  python3 - "${MINI_ORK_DB:?MINI_ORK_DB unset}" "$memory_table" "$stale_days" <<'PY'
import sqlite3, json, sys, time
db, tbl, days = sys.argv[1], sys.argv[2], int(sys.argv[3])
cutoff = int(time.time()) - days * 86400
con = sqlite3.connect(db)
# Attempt to find a timestamp column — try common names in order.
cols = [r[1] for r in con.execute(f"PRAGMA table_info({tbl})").fetchall()]
ts_col = next((c for c in ["updated_at","created_at","reflection_last_check","last_seen"] if c in cols), None)
if ts_col is None:
    print(f"reflection_detect_stale: no timestamp column found in {tbl}", file=sys.stderr)
    con.close()
    sys.exit(0)

# Find a primary-key-like column
pk_col = next((c for c in ["id","gradient_id","pattern_id","trace_id","adr_id"] if c in cols), cols[0])
rows = con.execute(f"SELECT {pk_col} FROM {tbl} WHERE {ts_col} < ?", (cutoff,)).fetchall()
con.close()

stale = [r[0] for r in rows]
print(json.dumps({"table": tbl, "stale_ids": stale, "stale_before_epoch": cutoff}))
print(f"reflection_detect_stale: {len(stale)} stale entries in {tbl}", file=sys.stderr)
PY
}

# desc: Summarize all pattern_records belonging to a cluster and emit a
#       consolidated summary JSON on stdout.
reflection_summarize_patterns() {
  local cluster_id="${1:?cluster_id required}"
  python3 - "${MINI_ORK_DB:?MINI_ORK_DB unset}" "$cluster_id" <<'PY'
import sqlite3, json, sys
db, cid = sys.argv[1], sys.argv[2]
con = sqlite3.connect(db)
rows = con.execute("""
    SELECT pattern_id, description, frequency, output_type, first_seen, last_seen
    FROM pattern_records
    WHERE cluster_id = ?
    ORDER BY frequency DESC
""", (cid,)).fetchall() if cid else []
con.close()
summary = {
    "cluster_id": cid,
    "pattern_count": len(rows),
    "patterns": [
        {"pattern_id": r[0], "description": r[1], "frequency": r[2],
         "output_type": r[3], "first_seen": r[4], "last_seen": r[5]}
        for r in rows
    ],
    "dominant_output_type": rows[0][3] if rows else None,
    "total_frequency": sum(r[2] for r in rows),
}
print(json.dumps(summary))
PY
}

# desc: Suggest promotion candidates from patterns_table where frequency >= threshold.
#       Emits JSON array of promotion suggestions on stdout.
reflection_suggest_promotions() {
  local patterns_table="${1:-pattern_records}"
  local min_freq="${MINI_ORK_PROMOTION_MIN_FREQ:-3}"
  python3 - "${MINI_ORK_DB:?MINI_ORK_DB unset}" "$patterns_table" "$min_freq" <<'PY'
import sqlite3, json, sys
db, tbl, mf = sys.argv[1], sys.argv[2], int(sys.argv[3])
con = sqlite3.connect(db)
rows = con.execute(f"""
    SELECT pattern_id, description, frequency, output_type, evidence_trace_ids
    FROM {tbl}
    WHERE frequency >= ?
    ORDER BY frequency DESC
""", (mf,)).fetchall()
con.close()
suggestions = []
for r in rows:
    suggestions.append({
        "pattern_id": r[0],
        "description": r[1],
        "frequency": r[2],
        "suggested_promotion_type": r[3],
        "evidence_trace_ids": json.loads(r[4]) if r[4] else [],
        "rationale": f"Pattern observed {r[2]} times — meets promotion threshold of {mf}",
    })
print(json.dumps(suggestions))
PY
}

# desc: Persist promotion suggestions as durable, queryable, evidence-linked
#       rows. Reuses the existing emergent_patterns table (status='proposed')
#       rather than promotion_records (which is the heavy benchmark-gated
#       APPLY path). Idempotent upsert keyed by pattern_id (PK), so repeated
#       reflect runs do not duplicate rows. Args:
#         $1 = JSON array of suggestions (output of reflection_suggest_promotions)
#       Emits count of suggestions persisted on stdout.
reflection_persist_suggestions() {
  local suggestions_json="${1:?suggestions_json required}"
  python3 - "${MINI_ORK_DB:?MINI_ORK_DB unset}" "$suggestions_json" <<'PY'
import sqlite3, json, sys, time

db, suggestions_json = sys.argv[1], sys.argv[2]
try:
    suggestions = json.loads(suggestions_json)
except (json.JSONDecodeError, TypeError):
    print(0)
    sys.exit(0)
if not isinstance(suggestions, list):
    print(0)
    sys.exit(0)

con = sqlite3.connect(db)
con.execute("PRAGMA busy_timeout=5000")
# Schema mirror of db/migrations/0008_reflection_basins.sql (idempotent CREATE).
con.execute("""
    CREATE TABLE IF NOT EXISTS emergent_patterns (
        pattern_id           TEXT PRIMARY KEY,
        cluster_label        TEXT NOT NULL,
        member_item_ids_json TEXT NOT NULL,
        feature_set_json     TEXT NOT NULL,
        strength_score       REAL NOT NULL,
        suggested_meta_adr   TEXT,
        status               TEXT NOT NULL DEFAULT 'proposed'
                             CHECK(status IN ('proposed','approved','rejected','superseded')),
        detected_at          INTEGER NOT NULL,
        resolved_at          INTEGER
    )
""")
now = int(time.time())
persisted = 0
for s in suggestions:
    pid = s.get("pattern_id") or ""
    if not pid:
        continue
    desc = (s.get("description") or "")[:500]
    freq = s.get("frequency", 1)
    try:
        freq_f = float(freq)
    except (TypeError, ValueError):
        freq_f = 1.0
    output_type = s.get("suggested_promotion_type") or "other"
    ev = s.get("evidence_trace_ids", [])
    if isinstance(ev, str):
        try:
            ev = json.loads(ev)
        except Exception:
            ev = []
    if not isinstance(ev, list):
        ev = []
    members = [{"item_table": "execution_traces", "item_id": tid} for tid in ev if tid]
    features = [output_type] if output_type else []
    rationale = s.get("rationale") or None
    # Idempotent upsert keyed by pattern_id (PK). INSERT OR REPLACE resets
    # resolved_at to NULL and status to 'proposed' on each reflect run, which
    # matches the kickoff's "no duplicates on re-run" requirement.
    con.execute("""
        INSERT OR REPLACE INTO emergent_patterns
            (pattern_id, cluster_label, member_item_ids_json,
             feature_set_json, strength_score, suggested_meta_adr,
             status, detected_at, resolved_at)
        VALUES (?,?,?,?,?,?,?,?,NULL)
    """, (
        pid,
        desc,
        json.dumps(members),
        json.dumps(features),
        freq_f,
        rationale,
        "proposed",
        now,
    ))
    persisted += 1
con.commit()
con.close()
print(persisted)
PY
}

# desc: Orchestrate all 6 reflection steps sequentially. since_ts is unix epoch;
#       defaults to 24 hours ago.
reflection_run() {
  local since_ts="${1:-$(( $(_rfl_now) - 86400 ))}"
  echo "reflection_run: starting pipeline since=${since_ts}" >&2

  # Always-on by project policy: the learning system extracts gradients every
  # reflect cycle (set MO_REFLECTION_EXTRACT_GRADIENTS=0 to opt out for cost).
  if [ "${MO_REFLECTION_EXTRACT_GRADIENTS:-1}" = "1" ]; then
    echo "  [1/6] extract_gradients" >&2
    reflection_extract_gradients "$since_ts" >/dev/null
  else
    echo "  [1/6] extract_gradients skipped (MO_REFLECTION_EXTRACT_GRADIENTS=0)" >&2
    # Ensure downstream SQL steps have the table even when extraction is
    # skipped for foreground delivery runs.
    source "${MINI_ORK_ROOT}/lib/gradient_extractor.sh" 2>/dev/null || true
    if declare -f _gradient_ensure_table >/dev/null 2>&1; then
      _gradient_ensure_table 2>/dev/null || true
    fi
  fi

  echo "  [2/6] deduplicate" >&2
  reflection_deduplicate "gradient_records"

  echo "  [3/6] link_failures" >&2
  reflection_link_failures "execution_traces"

  echo "  [4/6] detect_stale(gradient_records)" >&2
  reflection_detect_stale "gradient_records" >/dev/null

  echo "  [5/6] summarize_patterns (all clusters)" >&2
  python3 - "${MINI_ORK_DB:?MINI_ORK_DB unset}" <<'PY' | while IFS= read -r cid; do
import sqlite3, sys
con = sqlite3.connect(sys.argv[1])
try:
    rows = con.execute("SELECT DISTINCT cluster_id FROM pattern_records WHERE cluster_id IS NOT NULL").fetchall() or []
except sqlite3.OperationalError:
    rows = []  # v0.2-pt11.5 (D-044): bash `2>/dev/null` syntax was leaked into Python heredoc
con.close()
for r in rows:
    print(r[0])
PY
    reflection_summarize_patterns "$cid" >/dev/null
  done || true

  echo "  [6/6] suggest_promotions + persist" >&2
  local suggestions
  suggestions="$(reflection_suggest_promotions "pattern_records" 2>/dev/null || echo '[]')"
  local count
  count="$(python3 -c "import json,sys; print(len(json.loads(sys.argv[1])))" "$suggestions" 2>/dev/null || echo 0)"
  local persisted
  persisted="$(reflection_persist_suggestions "$suggestions" 2>/dev/null || echo 0)"
  echo "reflection_run: ${count} promotion suggestions generated, ${persisted:-0} persisted" >&2
  echo "$suggestions"
}

if [[ "${BASH_SOURCE[0]:-}" == "${0:-}" ]]; then
  echo "reflection_pipeline.sh — source me and call reflection_run / individual step functions"
fi
