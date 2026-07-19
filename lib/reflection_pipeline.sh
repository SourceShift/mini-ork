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
  # AC2: skip framework-internal traces (task_class starts with `__`,
  # e.g. `__reflect__`). These carry only "what reflect did" payload,
  # not a real signal to learn from — and the Python reflect entrypoint already
  # counts `task_class != '__reflect__'` itself, so we must agree here.
  trace_ids="$(python3 - "${MINI_ORK_DB:?MINI_ORK_DB unset}" "$since_ts" "$_batch" <<'PY'
import sqlite3, json, sys
con = sqlite3.connect(sys.argv[1])
con.execute("PRAGMA busy_timeout=5000")  # v0.2-pt7 F-11
# v0.2-pt11 (D-039 follow): execution_traces.created_at is TEXT (ISO-8601
# per migration 0010 default), caller passes unix-ts INT — compare via
# strftime('%s', created_at) cast to int.
# Gr1 fix (kickoff): exclude framework-self traces (task_class LIKE
# '\_\_%' ESCAPE '\\' — the only `__*` corpus in the repo is `__reflect__`
# today, but the pattern generalizes if more appear). Lifted out of the
# LIKE clause inline so future maintainers see the policy.
rows = con.execute(
    "SELECT trace_id, task_class FROM execution_traces"
    " WHERE CAST(strftime('%s', created_at) AS INTEGER) >= ?"
    "   AND (task_class IS NULL OR task_class NOT LIKE '\\_%' ESCAPE '\\')"
    " ORDER BY created_at LIMIT ?",
    (int(sys.argv[2]), int(sys.argv[3])),
).fetchall()
con.close()
for r in rows:
    print(r[0])
PY
)"

  local extracted=0 skipped_watermark=0
  while IFS= read -r tid; do
    [[ -z "$tid" ]] && continue
    # AC1: idempotent re-extract. If a gradient_records row already names
    # this trace_id as evidence, skip — re-running with an overlapping
    # `--since` window must not double-insert (kickoff: per-trace watermark
    # against `gradient_records.evidence`, no schema migration).
    if declare -f _gradient_check_watermark >/dev/null 2>&1; then
      if _gradient_check_watermark "$tid" 2>/dev/null; then
        (( skipped_watermark++ )) || true
        continue
      fi
    fi
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

  if [ "$skipped_watermark" -gt 0 ]; then
    echo "reflection_extract_gradients: skipped ${skipped_watermark} already-extracted trace(s) (watermark)" >&2
  fi

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
import sqlite3, re, sys
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

# Gr2 fix: legacy (target, signal) dedup missed same-target reviewers
# whose only difference was per-trace token noise (durations like
# "2.7min" vs "8.9min", costs like "$1.62" vs "$5.10", trace ids).
# SequenceMatcher on raw signal chars scored such pairs at ~0.40 — far
# below the 0.55 fuzzy floor — so the table grew ~5x per lifecycle.
# Replace lexical signal with a normalized SEMANTIC SIGNATURE that
# strips the noisy token classes while leaving prose untouched, then
# key merge on (target, semantic_signature). Calibration tokens:
#   - numerals: any run of digits, optionally with decimal point
#   - currency: $<digits>
#   - durations: <n>s | <n>min | <n>ms | <n>h
#   - ISO timestamps: YYYY-MM-DD[ HH:MM:SS]
#   - UUIDs: 8-4-4-4-12 hex with dashes
#   - trace ids: trace_<hex> (>=8 hex chars) and tr-<hex>
#   - bare hex runs >= 8 chars (catches tail snippets from truncated ids)
# `suggested_change` is concatenated for context — the same prescription
# phrased against different signals is one lesson, not many.
_NUM = r"\d+(?:\.\d+)?"
_CUR = r"\$\d+(?:\.\d+)?"
_DUR = r"\d+(?:\.\d+)?\s*(?:ms|s|sec|secs|min|mins|h|hr|hrs|hours?)"
_ISO = r"\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}(?::\d{2})?)?"
_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_TRACE = r"(?:trace|tr)_[0-9a-f]{8,}"
_HEXLONG = r"\b[0-9a-f]{8,}\b"
_NOISE = re.compile(
    rf"({_CUR}|{_DUR}|{_ISO}|{_UUID}|{_TRACE}|{_HEXLONG}|{_NUM})",
    re.IGNORECASE,
)
_WS = re.compile(r"\s+")

def _semantic_signature(text):
    """Lower-cased, noise-stripped text suitable for dedup keying."""
    if not text:
        return ""
    s = _NOISE.sub(" ", text.lower())
    s = _WS.sub(" ", s).strip()
    return s

to_delete = []

# Pass 1 — exact (target, semantic_signature) merge.
# Rows arrive confidence-desc so the first seen per key is the keeper.
# The semantic signature folds trace-token noise (durations/costs/ids)
# so reviewer rows from different traces with the same target collapse
# to a single representative. Prose (suggested_change intent) is
# preserved verbatim, so distinct intents still key apart.
seen = {}
survivors = []
for row in rows:
    gid, tgt, sig, chg = row[0], row[1], row[2], row[3]
    sig_key = _semantic_signature(f"{sig} {chg}")
    key = (tgt, sig_key)
    if key in seen:
        to_delete.append(gid)
    else:
        seen[key] = gid
        survivors.append(row)

# Pass 2 — fuzzy merge within (task_class, target) groups. Operates on
# the SAME semantic_signature so the difflib ratio compares normalized
# text (no trace-token blow-up). Greedy confidence-desc scan: a row
# whose signature is similar to an already-kept representative is
# deleted. Same-target pairs MUST be compared (kickoff: prior pass
# tokenized only `signal`, missing cross-trace same-target reviewers).
groups = {}
for row in survivors:
    groups.setdefault((row[5], row[1]), []).append(row)

fuzzy_deleted = 0
for grp in groups.values():
    kept_texts = []
    for gid, tgt, sig, chg, _conf, _tc in grp:
        text = _semantic_signature(f"{sig} {chg}")
        if not text:
            kept_texts.append(text or "")
            continue
        sm = SequenceMatcher(b=text, autojunk=False)
        dup = False
        for kt in kept_texts:
            if not kt:
                continue
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

# desc: Judge-gate (extract→distill→verify). Transition emergent_patterns rows
#       from status='proposed' → 'approved' when they clear an evidence/strength
#       floor already present in the schema:
#         strength_score >= MO_EMERGENT_VERIFY_MIN_STRENGTH (default 3), AND
#         evidence member count (member_item_ids_json) >= MO_EMERGENT_VERIFY_MIN_EVIDENCE (default 1).
#       ONLY rows that clear this gate become eligible to be read into
#       routing/context (see context_assembler). This is the guard against
#       memory confabulation (Dixit 2026): raw self-diagnosed patterns stay
#       'proposed' and never reach the rail; only evidence-backed ones are
#       promoted to 'approved'. Opt-out MO_EMERGENT_VERIFY=0. Cold-safe: no-op
#       on missing/empty table. Emits count of newly-approved rows on stdout.
reflection_verify_patterns() {
  if [ "${MO_EMERGENT_VERIFY:-1}" != "1" ]; then echo 0; return 0; fi
  local min_strength="${MO_EMERGENT_VERIFY_MIN_STRENGTH:-3}"
  local min_evidence="${MO_EMERGENT_VERIFY_MIN_EVIDENCE:-1}"
  python3 - "${MINI_ORK_DB:?MINI_ORK_DB unset}" "$min_strength" "$min_evidence" <<'PY'
import sqlite3, json, sys, time
db, min_strength, min_evidence = sys.argv[1], float(sys.argv[2]), int(sys.argv[3])
con = sqlite3.connect(db)
con.execute("PRAGMA busy_timeout=5000")
try:
    rows = con.execute(
        "SELECT pattern_id, member_item_ids_json, strength_score "
        "FROM emergent_patterns WHERE status='proposed'"
    ).fetchall()
except sqlite3.OperationalError:
    print(0); con.close(); sys.exit(0)
now = int(time.time())
approved = 0
for pid, members_json, strength in rows:
    try:
        n_evidence = len(json.loads(members_json)) if members_json else 0
    except (json.JSONDecodeError, TypeError):
        n_evidence = 0
    try:
        s = float(strength)
    except (TypeError, ValueError):
        s = 0.0
    if s >= min_strength and n_evidence >= min_evidence:
        con.execute(
            "UPDATE emergent_patterns SET status='approved', resolved_at=? "
            "WHERE pattern_id=? AND status='proposed'",
            (now, pid),
        )
        approved += 1
con.commit()
con.close()
print(approved)
PY
}

# desc: D5 — per-node credit from the single outcome.
#   reflection_apply_per_node_credit
#       Reweight each trace's reward_g by its per-node process_reward so
#       decisive nodes (high process_reward vs run mean) carry more credit
#       into recompute_advantages, while consensus nodes (process_reward
#       near 0.5) carry less. Falls back to uniform (no reweight) when a
#       trace's process_reward is NULL or when MO_ROUTER_PER_NODE_CREDIT=0.
#       Idempotent: re-running with no state carries zero net effect
#       because the original reward_g is restored from per_node_credit_backup
#       in reflection_restore_per_node_credit. Best-effort: any failure
#       in the apply path triggers an automatic restore so the caller
#       never sees a partially-weighted DB.
#
#   Math: per_node_weight = 1 + gamma * (process_reward - 0.5)
#         effective_reward_g = reward_g * per_node_weight
#         (clamped to [-1.0, +1.0] to match reward_g's documented range).
#         gamma defaults to 1.0 (full amplification). Set
#         MO_ROUTER_PER_NODE_CREDIT_GAMMA=0 for the uniform limit (no
#         reweight, only the backup/restore overhead remains).
#
#   State: a side-table `per_node_credit_backup` (id PRIMARY KEY,
#         original_reward_g REAL) is created in the same DB. It is
#         dropped by reflection_restore_per_node_credit at the end of
#         the reflect cycle. No ALTER TABLE on execution_traces — the
#         plan's out_of_scope rule is respected.
#
#   Cold-start: a fresh DB (no reward_g rows) is a no-op — empty backup
#               table is still created and immediately dropped so the
#               restore path stays symmetric.
reflection_apply_per_node_credit() {
  [ "${MO_ROUTER_PER_NODE_CREDIT:-0}" = "1" ] || return 0
  [ -n "${MINI_ORK_DB:-}" ] || return 0
  [ -f "${MINI_ORK_DB}" ] || return 0

  local _gamma="${MO_ROUTER_PER_NODE_CREDIT_GAMMA:-1.0}"
  python3 - "${MINI_ORK_DB}" "$_gamma" <<'PY' 2>/dev/null || true
import os, sqlite3, sys

db, gamma_s = sys.argv[1], sys.argv[2]
try:
    gamma = float(gamma_s)
except (TypeError, ValueError):
    gamma = 1.0
# Hard cap so a runaway env knob can't drive weight to 0 / negative;
# the kickoff's contract is a multiplier around 1.0 (range [0.5, 1.5]).
if gamma < 0.0:
    gamma = 0.0
if gamma > 2.0:
    gamma = 2.0

con = sqlite3.connect(db)
con.execute("PRAGMA busy_timeout=5000")
con.row_factory = sqlite3.Row
try:
    cols = {r[1] for r in con.execute(
        "PRAGMA table_info(execution_traces)").fetchall()}
except sqlite3.OperationalError:
    con.close(); sys.exit(0)
if "reward_g" not in cols or "process_reward" not in cols:
    con.close(); sys.exit(0)

# CREATE TABLE — keep idempotent + in the same DB so the restore path
# (reflection_restore_per_node_credit) sees the originals. NOT a
# migration — this is a transient staging table scoped to one reflect
# cycle.
con.execute(
    "CREATE TABLE IF NOT EXISTS per_node_credit_backup ("
    "  id INTEGER PRIMARY KEY,"
    "  original_reward_g REAL NOT NULL"
    ")")

# Snapshot original reward_g BEFORE we mutate. Only rows with both a
# non-NULL reward_g AND a non-NULL process_reward get a backup; rows
# with NULL process_reward keep their uniform fallback (no reweight,
# no backup row either).
# execution_traces has no integer 'id' column (PK is trace_id, a TEXT hash);
# use sqlite's implicit rowid as the stable integer key for the backup.
con.execute(
    "INSERT OR REPLACE INTO per_node_credit_backup (id, original_reward_g) "
    "SELECT rowid, reward_g FROM execution_traces "
    "  WHERE reward_g IS NOT NULL "
    "    AND process_reward IS NOT NULL")

cur = con.execute(
    "SELECT rowid AS id, reward_g, process_reward FROM execution_traces "
    "  WHERE reward_g IS NOT NULL "
    "    AND process_reward IS NOT NULL")
updated = 0
for row in cur:
    try:
        rg = float(row["reward_g"])
        pr = float(row["process_reward"])
    except (TypeError, ValueError):
        continue
    weight = 1.0 + gamma * (pr - 0.5)
    # Clamp to keep effective reward_g inside the documented [-1, +1] range.
    eff = max(-1.0, min(1.0, rg * weight))
    con.execute(
        "UPDATE execution_traces SET reward_g = ? WHERE rowid = ?",
        (round(eff, 6), row["id"]))
    updated += 1
con.commit()
con.close()
sys.stderr.write(
    f"  [d5] per-node credit: gamma={gamma:g} updated={updated} traces\n")
PY
}

# desc: D5 — restore reward_g from per_node_credit_backup after
#       lane_router_recompute_advantages has consumed the reweighted
#       values. No-op when per_node_credit_backup is absent or empty
#       (e.g. MO_ROUTER_PER_NODE_CREDIT=0, fresh DB, or restore already
#       ran in this reflect cycle). Tolerates failures silently —
#       a stale backup row only risks a future reflect cycle seeing
#       slightly-different reward_g values on the same trace ids, never
#       a crash.
reflection_restore_per_node_credit() {
  [ -n "${MINI_ORK_DB:-}" ] || return 0
  [ -f "${MINI_ORK_DB}" ] || return 0
  python3 - "${MINI_ORK_DB}" <<'PY' 2>/dev/null || true
import sqlite3, sys

db = sys.argv[1]
con = sqlite3.connect(db)
con.execute("PRAGMA busy_timeout=5000")
try:
    cur = con.execute(
        "SELECT id, original_reward_g FROM per_node_credit_backup")
    rows = cur.fetchall()
except sqlite3.OperationalError:
    con.close(); sys.exit(0)
if not rows:
    # Empty table — drop it and exit. Keeps the DB clean even when
    # MO_ROUTER_PER_NODE_CREDIT=0 (the apply path is a no-op).
    con.execute("DROP TABLE IF EXISTS per_node_credit_backup")
    con.commit()
    con.close(); sys.exit(0)

restored = 0
for backup_rowid, original in rows:
    con.execute(
        "UPDATE execution_traces SET reward_g = ? WHERE rowid = ?",
        (original, backup_rowid))
    restored += 1
con.execute("DROP TABLE IF EXISTS per_node_credit_backup")
con.commit()
con.close()
sys.stderr.write(
    f"  [d5] per-node credit restored: {restored} traces reverted\n")
PY
}

# desc: Orchestrate all reflection steps sequentially. since_ts is unix epoch;
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

  # [judge-gate] extract→distill→verify: promote only evidence-backed
  # emergent_patterns from 'proposed' → 'approved' so confabulated
  # self-diagnoses never reach routing/context (Dixit 2026).
  echo "  [verify] judge-gate emergent_patterns" >&2
  local approved
  approved="$(reflection_verify_patterns 2>/dev/null || echo 0)"
  echo "reflection_run: ${approved:-0} emergent_patterns approved by judge-gate" >&2

  echo "$suggestions"
}

if [[ "${BASH_SOURCE[0]:-}" == "${0:-}" ]]; then
  echo "reflection_pipeline.sh — source me and call reflection_run / individual step functions"
fi
