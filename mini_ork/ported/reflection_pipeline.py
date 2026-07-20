"""Native reflection pipeline sub-routines.

The public functions retain the Bash-era stdout, stderr, database, and exit
contracts. Pre-retirement parity remains covered against the Bash library while
the supported public reflect path uses this module in process.

The default gradient helpers call the native gradient extractor/store. Tests
may still inject deterministic callables through the same public seams.

Pipeline map (bash function → Python):
  reflection_extract_gradients  → reflection_extract_gradients
  reflection_deduplicate        → reflection_deduplicate
  reflection_link_failures      → reflection_link_failures
  reflection_detect_stale       → reflection_detect_stale
  reflection_summarize_patterns → reflection_summarize_patterns
  reflection_suggest_promotions → reflection_suggest_promotions
  reflection_persist_suggestions→ reflection_persist_suggestions
  reflection_apply_per_node_credit   → reflection_apply_per_node_credit
  reflection_restore_per_node_credit → reflection_restore_per_node_credit
  reflection_run                → reflection_run
"""
from __future__ import annotations

import json
import math
import os
import sqlite3
import sys
import time
from difflib import SequenceMatcher

__all__ = [
    "reflection_extract_gradients",
    "reflection_deduplicate",
    "reflection_link_failures",
    "reflection_detect_stale",
    "reflection_summarize_patterns",
    "reflection_suggest_promotions",
    "reflection_persist_suggestions",
    "reflection_verify_patterns",
    "reflection_apply_per_node_credit",
    "reflection_restore_per_node_credit",
    "reflection_run",
]


# ── Injection hooks ──────────────────────────────────────────────────────────
# Injectable seams remain for deterministic tests, but production defaults are
# real native operations.

def _default_gradient_extract(trace_id: str):
    from mini_ork.ported import gradient_extractor

    return [
        json.dumps(item)
        for item in gradient_extractor.extract(trace_id, emit=False)
    ]


def _default_gradient_store(gradient_json: str) -> None:
    import io
    from contextlib import redirect_stdout
    from mini_ork.ported import gradient_extractor

    with redirect_stdout(io.StringIO()):
        gradient_extractor.store(gradient_json)


def _default_gradient_ensure_table() -> None:
    from mini_ork.ported import gradient_extractor

    gradient_extractor.init_schema()


_gradient_extract = _default_gradient_extract
_gradient_store = _default_gradient_store
_gradient_ensure_table = _default_gradient_ensure_table


def set_gradient_extract(fn) -> None:
    """Inject a stub/replacement for lib/gradient_extractor.sh::gradient_extract.

    The callable receives a single `trace_id: str` argument and returns an
    iterable of gradient JSON strings (one per yielded gradient). Mirrors the
    bash convention where `gradient_extract "$tid"` emits one gradient per
    stdout line.
    """
    global _gradient_extract
    _gradient_extract = fn


def set_gradient_store(fn) -> None:
    """Inject a stub/replacement for lib/gradient_extractor.sh::gradient_store.

    The callable receives a single `gradient_json: str` argument and persists
    it. Bash convention: `gradient_store "$gradient" >/dev/null || true`
    (best-effort, no error propagation).
    """
    global _gradient_store
    _gradient_store = fn


def set_gradient_ensure_table(fn) -> None:
    """Inject a stub/replacement for lib/gradient_extractor.sh::_gradient_ensure_table.

    Called once at the top of `reflection_extract_gradients` before iterating
    trace_ids. Idempotent CREATE TABLE is fine; the table is also created by
    migration 0038 on fresh DBs.
    """
    global _gradient_ensure_table
    _gradient_ensure_table = fn


# ── Per-helper PRAGMA / DB helpers ──────────────────────────────────────────

def _connect(db_path: str) -> sqlite3.Connection:
    con = sqlite3.connect(db_path)
    # v0.2-pt7 (F-11): per-connection busy_timeout; WAL is persistent but
    # busy_timeout is not, so every hot-path open sets it.
    con.execute("PRAGMA busy_timeout=5000")
    return con


# ── Private SQL helpers (lifted from bash heredocs) ─────────────────────────

def _extract_trace_ids_sql(db_path: str, since_ts: int, batch: int) -> list[str]:
    """Mirror bash heredoc: SELECT trace_id FROM execution_traces
    WHERE CAST(strftime('%s', created_at) AS INTEGER) >= ?
    ORDER BY created_at LIMIT ?."""
    con = _connect(db_path)
    try:
        rows = con.execute(
            "SELECT trace_id FROM execution_traces "
            "WHERE CAST(strftime('%s', created_at) AS INTEGER) >= ? "
            "AND (task_class IS NULL OR substr(task_class, 1, 2) != '__') "
            "ORDER BY created_at LIMIT ?",
            (int(since_ts), int(batch)),
        ).fetchall()
    finally:
        con.close()
    return [r[0] for r in rows]


def _dedupe_pass1_exact(db_path: str, tbl: str, batch: int) -> tuple[list[str], list]:
    """Pass-1: identical (target, signal) merge. Returns (to_delete_gids, survivors)."""
    con = _connect(db_path)
    try:
        cols = {r[1] for r in con.execute(f"PRAGMA table_info({tbl})").fetchall()}
        task_class_expr = "task_class" if "task_class" in cols else "''"
        rows = con.execute(
            f"""
            SELECT gradient_id, target, signal, suggested_change, confidence,
                   COALESCE({task_class_expr},'')
            FROM {tbl}
            ORDER BY confidence DESC, created_at ASC
            LIMIT ?
            """,
            (batch,),
        ).fetchall()
    finally:
        con.close()

    to_delete: list[str] = []
    seen: dict[tuple, str] = {}
    survivors: list = []
    for row in rows:
        gid, tgt, sig = row[0], row[1], row[2]
        key = (tgt, sig)
        if key in seen:
            to_delete.append(gid)
        else:
            seen[key] = gid
            survivors.append(row)
    return to_delete, survivors


def _dedupe_pass2_fuzzy(survivors: list, fuzzy: float) -> tuple[list[str], int]:
    """Pass-2: fuzzy merge within (task_class, target) groups on signal text.

    Mirrors bash's SequenceMatcher three-stage ratio gate (real_quick_ratio,
    quick_ratio, ratio) — all three must clear the threshold for a hit.
    Returns (to_delete_gids, fuzzy_deleted_count).
    """
    groups: dict[tuple, list] = {}
    for row in survivors:
        groups.setdefault((row[5], row[1]), []).append(row)

    to_delete: list[str] = []
    fuzzy_deleted = 0
    for grp in groups.values():
        kept_texts: list[str] = []
        for row in grp:
            gid = row[0]
            text = row[2]  # row[2] is the `signal` column
            sm = SequenceMatcher(b=text, autojunk=False)
            dup = False
            for kt in kept_texts:
                sm.set_seq1(kt)
                if (
                    sm.real_quick_ratio() >= fuzzy
                    and sm.quick_ratio() >= fuzzy
                    and sm.ratio() >= fuzzy
                ):
                    dup = True
                    break
            if dup:
                to_delete.append(gid)
                fuzzy_deleted += 1
            else:
                kept_texts.append(text)
    return to_delete, fuzzy_deleted


def _link_failures_insert(db_path: str, ftbl: str) -> int:
    """Mirror bash link_failures heredoc. Returns inserted-pair count.

    Note: counter increments per (failure × gradient) pair even when INSERT
    OR IGNORE skipped the row — this matches bash's behaviour verbatim.
    """
    con = _connect(db_path)
    try:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS failure_links (
                link_id      TEXT PRIMARY KEY,
                trace_id     TEXT NOT NULL,
                gradient_id  TEXT,
                task_class   TEXT,
                linked_at    INTEGER NOT NULL
            )
            """
        )
        now = int(time.time())
        failures = con.execute(
            f"SELECT trace_id, task_class FROM {ftbl} WHERE status='failure'"
        ).fetchall()
        inserted = 0
        for tid, tc in failures:
            gradients = con.execute(
                "SELECT gradient_id FROM gradient_records WHERE evidence=?",
                (tid,),
            ).fetchall()
            for (gid,) in gradients:
                link_id = f"fl-{tid[:8]}-{gid[:8]}"
                con.execute(
                    """
                    INSERT OR IGNORE INTO failure_links
                        (link_id, trace_id, gradient_id, task_class, linked_at)
                    VALUES (?,?,?,?,?)
                    """,
                    (link_id, tid, gid, tc, now),
                )
                inserted += 1
        con.commit()
    finally:
        con.close()
    return inserted


def _detect_stale_select(db_path: str, tbl: str, days: int) -> dict:
    """Mirror bash detect_stale heredoc. Returns the dict that gets json.dumps'd."""
    cutoff = int(time.time()) - days * 86400
    con = _connect(db_path)
    try:
        cols = [r[1] for r in con.execute(f"PRAGMA table_info({tbl})").fetchall()]
        ts_col = next(
            (c for c in ["updated_at", "created_at", "reflection_last_check", "last_seen"] if c in cols),
            None,
        )
        if ts_col is None:
            print(
                f"reflection_detect_stale: no timestamp column found in {tbl}",
                file=sys.stderr,
            )
            return {"table": tbl, "stale_ids": [], "stale_before_epoch": cutoff}
        pk_col = next(
            (c for c in ["id", "gradient_id", "pattern_id", "trace_id", "adr_id"] if c in cols),
            cols[0],
        )
        rows = con.execute(
            f"SELECT {pk_col} FROM {tbl} WHERE {ts_col} < ?",
            (cutoff,),
        ).fetchall()
    finally:
        con.close()

    stale = [r[0] for r in rows]
    return {"table": tbl, "stale_ids": stale, "stale_before_epoch": cutoff}


def _summarize_patterns_query(db_path: str, cluster_id: str | None) -> dict:
    """Mirror bash summarize_patterns heredoc.

    cluster_id is falsy (empty string / None) → rows list is empty; matches
    bash's `(... ) if cid else []` ternary.
    """
    con = _connect(db_path)
    try:
        rows = (
            con.execute(
                """
                SELECT pattern_id, description, frequency, output_type, first_seen, last_seen
                FROM pattern_records
                WHERE cluster_id = ?
                ORDER BY frequency DESC
                """,
                (cluster_id,),
            ).fetchall()
            if cluster_id
            else []
        )
    finally:
        con.close()
    summary = {
        "cluster_id": cluster_id,
        "pattern_count": len(rows),
        "patterns": [
            {
                "pattern_id": r[0],
                "description": r[1],
                "frequency": r[2],
                "output_type": r[3],
                "first_seen": r[4],
                "last_seen": r[5],
            }
            for r in rows
        ],
        "dominant_output_type": rows[0][3] if rows else None,
        "total_frequency": sum(r[2] for r in rows),
    }
    return summary


def _suggest_promotions_query(db_path: str, tbl: str, min_freq: int) -> list[dict]:
    """Mirror bash suggest_promotions heredoc. Returns the suggestions array."""
    con = _connect(db_path)
    try:
        rows = con.execute(
            f"""
            SELECT pattern_id, description, frequency, output_type, evidence_trace_ids
            FROM {tbl}
            WHERE frequency >= ?
            ORDER BY frequency DESC
            """,
            (min_freq,),
        ).fetchall()
    finally:
        con.close()
    suggestions: list[dict] = []
    for r in rows:
        ev_raw = r[4]
        ev = json.loads(ev_raw) if ev_raw else []
        suggestions.append(
            {
                "pattern_id": r[0],
                "description": r[1],
                "frequency": r[2],
                "suggested_promotion_type": r[3],
                "evidence_trace_ids": ev,
                "rationale": (
                    f"Pattern observed {r[2]} times — meets promotion threshold of {min_freq}"
                ),
            }
        )
    return suggestions


def _persist_suggestions_upsert(db_path: str, suggestions_json: str) -> int:
    """Mirror bash persist_suggestions heredoc. Returns persisted count."""
    try:
        suggestions = json.loads(suggestions_json)
    except (json.JSONDecodeError, TypeError):
        print(0)
        return 0
    if not isinstance(suggestions, list):
        print(0)
        return 0

    con = _connect(db_path)
    try:
        # Schema mirror of db/migrations/0008_reflection_basins.sql. The
        # migration creates this table on `db/init.sh`; we mirror its
        # CREATE here so the port is self-sufficient on legacy DBs that
        # pre-date the migration.
        con.execute(
            """
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
            """
        )
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
            members = [
                {"item_table": "execution_traces", "item_id": tid}
                for tid in ev
                if tid
            ]
            features = [output_type] if output_type else []
            rationale = s.get("rationale") or None
            con.execute(
                """
                INSERT OR REPLACE INTO emergent_patterns
                    (pattern_id, cluster_label, member_item_ids_json,
                     feature_set_json, strength_score, suggested_meta_adr,
                     status, detected_at, resolved_at)
                VALUES (?,?,?,?,?,?,?,?,NULL)
                """,
                (
                    pid,
                    desc,
                    json.dumps(members),
                    json.dumps(features),
                    freq_f,
                    rationale,
                    "proposed",
                    now,
                ),
            )
            persisted += 1
        con.commit()
    finally:
        con.close()
    print(persisted)
    return persisted


def _list_distinct_cluster_ids(db_path: str) -> list[str]:
    """Mirror the inlined python3 heredoc in reflection_run step [5/6]."""
    con = _connect(db_path)
    try:
        try:
            rows = (
                con.execute(
                    "SELECT DISTINCT cluster_id FROM pattern_records WHERE cluster_id IS NOT NULL"
                ).fetchall()
                or []
            )
        except sqlite3.OperationalError:
            # bash `2>/dev/null` semantics: if pattern_records lacks cluster_id,
            # silently return empty so the orchestrator no-ops rather than
            # crashing (v0.2-pt11.5 D-044).
            rows = []
    finally:
        con.close()
    return [r[0] for r in rows]


# ── Public functions (mirror lib/reflection_pipeline.sh) ─────────────────────

def reflection_extract_gradients(since_ts: int = 0) -> None:
    """Mirror lib/reflection_pipeline.sh::reflection_extract_gradients.

    Selects execution_traces created on/after `since_ts` (unix epoch) via the
    bounded `_extract_trace_ids_sql` query, then for each trace_id calls the
    injected `gradient_extract` (yields gradient JSON lines) and persists each
    gradient via the injected `gradient_store`. Gradient JSON is echoed to
    stdout; the summary line goes to stderr.

    On Windows / db/init.sh-fresh DBs the `gradient_records` table is created by
    the injected `_gradient_ensure_table` hook (mirrors bash's defensive
    pre-create so dedupe / detect_stale / suggest_promotions don't crash on
    empty-gradient runs).

    Stdout: gradient JSON lines (one per gradient).
    Stderr: `reflection_extract_gradients: extracted N gradients since S`.
    """
    _gradient_ensure_table()
    trace_ids = _extract_trace_ids_sql(
        os.environ.get("MINI_ORK_DB", ""), int(since_ts), int(os.environ.get("MO_REFLECTION_BATCH", 500))
    )
    extracted = 0
    skipped_watermark = 0
    for tid in trace_ids:
        if not tid:
            continue
        from mini_ork.ported import gradient_extractor

        if gradient_extractor.has_watermark(tid):
            skipped_watermark += 1
            continue
        for gradient in _gradient_extract(tid):
            if not gradient:
                continue
            try:
                _gradient_store(gradient)
            except Exception:
                # bash mirrors `gradient_store "$gradient" >/dev/null || true`
                pass
            print(gradient)
            extracted += 1
    if skipped_watermark:
        print(
            "reflection_extract_gradients: skipped "
            f"{skipped_watermark} already-extracted trace(s) (watermark)",
            file=sys.stderr,
        )
    print(
        f"reflection_extract_gradients: extracted {extracted} gradients since {since_ts}",
        file=sys.stderr,
    )


def reflection_apply_per_node_credit(db_path: str | None = None) -> int:
    """Temporarily reweight ``reward_g`` using verifier process rewards.

    Originals are saved in a transient side table so the lane router can
    consume the adjusted values without changing the durable trace record.
    Returns the number of adjusted rows; all unsupported-schema cases are
    fail-open no-ops, matching the legacy reflection side channel.
    """
    if os.environ.get("MO_ROUTER_PER_NODE_CREDIT", "0") != "1":
        return 0
    resolved = db_path or os.environ.get("MINI_ORK_DB")
    if not resolved or not os.path.isfile(resolved):
        return 0
    try:
        gamma = float(os.environ.get("MO_ROUTER_PER_NODE_CREDIT_GAMMA", "1.0"))
    except (TypeError, ValueError):
        gamma = 1.0
    gamma = max(0.0, min(2.0, gamma))

    con = _connect(resolved)
    try:
        cols = {row[1] for row in con.execute("PRAGMA table_info(execution_traces)")}
        if not {"reward_g", "process_reward"}.issubset(cols):
            return 0
        con.execute(
            "CREATE TABLE IF NOT EXISTS per_node_credit_backup ("
            "id INTEGER PRIMARY KEY, original_reward_g REAL NOT NULL)"
        )
        con.execute(
            "INSERT OR REPLACE INTO per_node_credit_backup (id, original_reward_g) "
            "SELECT rowid, reward_g FROM execution_traces "
            "WHERE reward_g IS NOT NULL AND process_reward IS NOT NULL"
        )
        rows = con.execute(
            "SELECT rowid, reward_g, process_reward FROM execution_traces "
            "WHERE reward_g IS NOT NULL AND process_reward IS NOT NULL"
        ).fetchall()
        updated = 0
        for rowid, reward_g, process_reward in rows:
            try:
                weight = 1.0 + gamma * (float(process_reward) - 0.5)
                effective = max(-1.0, min(1.0, float(reward_g) * weight))
            except (TypeError, ValueError):
                continue
            con.execute(
                "UPDATE execution_traces SET reward_g=? WHERE rowid=?",
                (round(effective, 6), rowid),
            )
            updated += 1
        con.commit()
        return updated
    except sqlite3.OperationalError:
        return 0
    finally:
        con.close()


def reflection_restore_per_node_credit(db_path: str | None = None) -> int:
    """Restore rewards saved by :func:`reflection_apply_per_node_credit`."""
    resolved = db_path or os.environ.get("MINI_ORK_DB")
    if not resolved or not os.path.isfile(resolved):
        return 0
    con = _connect(resolved)
    try:
        try:
            rows = con.execute(
                "SELECT id, original_reward_g FROM per_node_credit_backup"
            ).fetchall()
        except sqlite3.OperationalError:
            return 0
        for rowid, original in rows:
            con.execute(
                "UPDATE execution_traces SET reward_g=? WHERE rowid=?",
                (original, rowid),
            )
        con.execute("DROP TABLE IF EXISTS per_node_credit_backup")
        con.commit()
        return len(rows)
    finally:
        con.close()


def reflection_deduplicate(gradients_table: str = "gradient_records") -> None:
    """Mirror lib/reflection_pipeline.sh::reflection_deduplicate.

    Two-pass dedupe:
      Pass 1 — exact (target, signal) merge, global.
      Pass 2 — fuzzy merge within (task_class, target) groups (difflib ratio on
               signal >= MO_DEDUP_FUZZY, default 0.55).
    Highest confidence wins in both passes (rows arrive confidence-desc).

    Stdout: empty.
    Stderr: `reflection_deduplicate: removed N duplicates (X exact, Y fuzzy@Z)`
            or `reflection_deduplicate: no duplicates found`.
    """
    db_path = os.environ["MINI_ORK_DB"]
    batch = int(os.environ.get("MO_DEDUP_BATCH", 10000))
    fuzzy = float(os.environ.get("MO_DEDUP_FUZZY", 0.55))

    to_delete_p1, survivors = _dedupe_pass1_exact(db_path, gradients_table, batch)
    to_delete_p2, fuzzy_deleted = _dedupe_pass2_fuzzy(survivors, fuzzy)
    to_delete = to_delete_p1 + to_delete_p2

    if to_delete:
        con = _connect(db_path)
        try:
            placeholders = ",".join("?" * len(to_delete))
            con.execute(
                f"DELETE FROM {gradients_table} WHERE gradient_id IN ({placeholders})",
                to_delete,
            )
            con.commit()
        finally:
            con.close()
        exact = len(to_delete) - fuzzy_deleted
        print(
            f"reflection_deduplicate: removed {len(to_delete)} duplicates "
            f"({exact} exact, {fuzzy_deleted} fuzzy@{fuzzy})",
            file=sys.stderr,
        )
    else:
        print("reflection_deduplicate: no duplicates found", file=sys.stderr)


def reflection_link_failures(failure_table: str = "execution_traces") -> None:
    """Mirror lib/reflection_pipeline.sh::reflection_link_failures.

    Correlate failure-status traces with gradient targets; update failure_links
    table. Stdout: empty. Stderr: `reflection_link_failures: N links
    created/verified`.
    """
    db_path = os.environ["MINI_ORK_DB"]
    inserted = _link_failures_insert(db_path, failure_table)
    print(
        f"reflection_link_failures: {inserted} links created/verified",
        file=sys.stderr,
    )


def reflection_detect_stale(memory_table: str) -> None:
    """Mirror lib/reflection_pipeline.sh::reflection_detect_stale.

    Detect memory entries not updated within MINI_ORK_STALE_DAYS (default 14).
    Stdout: JSON `{"table": ..., "stale_ids": [...], "stale_before_epoch": ...}`.
    Stderr: `reflection_detect_stale: N stale entries in <tbl>`.
    """
    db_path = os.environ["MINI_ORK_DB"]
    stale_days = int(os.environ.get("MINI_ORK_STALE_DAYS", 14))
    result = _detect_stale_select(db_path, memory_table, stale_days)
    print(json.dumps(result))
    print(
        f"reflection_detect_stale: {len(result['stale_ids'])} stale entries in {memory_table}",
        file=sys.stderr,
    )


def reflection_summarize_patterns(cluster_id: str) -> dict:
    """Mirror lib/reflection_pipeline.sh::reflection_summarize_patterns.

    Summarize all pattern_records belonging to a cluster. Returns the dict that
    is json.dumps'd to stdout; the function also prints it (matching bash's
    stdout emission) so direct callers get both the return value and the
    printed JSON.
    """
    db_path = os.environ["MINI_ORK_DB"]
    summary = _summarize_patterns_query(db_path, cluster_id)
    print(json.dumps(summary))
    return summary


def reflection_suggest_promotions(patterns_table: str = "pattern_records") -> list[dict]:
    """Mirror lib/reflection_pipeline.sh::reflection_suggest_promotions.

    Suggest promotion candidates where frequency >= MINI_ORK_PROMOTION_MIN_FREQ
    (default 3). Stdout: JSON array. Returns the parsed list for in-process
    callers.
    """
    db_path = os.environ["MINI_ORK_DB"]
    min_freq = int(os.environ.get("MINI_ORK_PROMOTION_MIN_FREQ", 3))
    suggestions = _suggest_promotions_query(db_path, patterns_table, min_freq)
    print(json.dumps(suggestions))
    return suggestions


def reflection_persist_suggestions(suggestions_json: str) -> int:
    """Mirror lib/reflection_pipeline.sh::reflection_persist_suggestions.

    Persist suggestions as durable rows in emergent_patterns (status='proposed').
    Idempotent upsert keyed by pattern_id (PK). Stdout: persisted count.
    Returns the count for in-process callers.

    Float-compare gate: `strength_score` is written via `freq_f` (float coerced
    from `frequency`). Within the 1e-6 tolerance enforced by the parity test.
    """
    db_path = os.environ["MINI_ORK_DB"]
    return _persist_suggestions_upsert(db_path, suggestions_json)


def reflection_verify_patterns() -> int:
    """Mirror lib/reflection_pipeline.sh::reflection_verify_patterns.

    Judge-gate (extract→distill→verify): transition emergent_patterns rows from
    status='proposed' → 'approved' when they clear the evidence/strength floor
    (strength_score >= MO_EMERGENT_VERIFY_MIN_STRENGTH AND member-evidence count
    >= MO_EMERGENT_VERIFY_MIN_EVIDENCE). Only approved rows are eligible to be
    read into routing/context — the guard against memory confabulation (Dixit
    2026). Opt-out MO_EMERGENT_VERIFY=0. Cold-safe: no-op on missing/empty
    table. Prints and returns the count of newly-approved rows.
    """
    if os.environ.get("MO_EMERGENT_VERIFY", "1") != "1":
        print(0)
        return 0
    min_strength = float(os.environ.get("MO_EMERGENT_VERIFY_MIN_STRENGTH", "3"))
    min_evidence = int(os.environ.get("MO_EMERGENT_VERIFY_MIN_EVIDENCE", "1"))
    db_path = os.environ["MINI_ORK_DB"]
    con = _connect(db_path)
    try:
        try:
            rows = con.execute(
                "SELECT pattern_id, member_item_ids_json, strength_score "
                "FROM emergent_patterns WHERE status='proposed'"
            ).fetchall()
        except sqlite3.OperationalError:
            print(0)
            return 0
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
    finally:
        con.close()
    print(approved)
    return approved


def reflection_run(since_ts: int | None = None) -> str:
    """Mirror lib/reflection_pipeline.sh::reflection_run.

    Orchestrate all 6 reflection steps sequentially. `since_ts` defaults to 24h
    ago (matching bash's `$(( $(_rfl_now) - 86400 ))`). Stdout: JSON suggestions
    array (the same JSON that bash's final `echo "$suggestions"` emits — one
    copy, captured via redirect_stdout to mirror bash command substitution).
    Stderr: banner + per-step progress + final summary.

    Returns the suggestions JSON string for in-process callers.
    """
    import io
    from contextlib import redirect_stdout

    if since_ts is None:
        since_ts = int(time.time()) - 86400
    print(f"reflection_run: starting pipeline since={since_ts}", file=sys.stderr)

    def _swallow_stdout(fn, *args, **kwargs):
        buf = io.StringIO()
        with redirect_stdout(buf):
            try:
                fn(*args, **kwargs)
            except Exception:
                # bash's `|| true` / `>/dev/null` semantics: inner step failures
                # don't abort the orchestrator.
                pass
        return buf.getvalue()

    # [1/6] extract_gradients — gated by MO_REFLECTION_EXTRACT_GRADIENTS=1
    if os.environ.get("MO_REFLECTION_EXTRACT_GRADIENTS", "1") == "1":
        print("  [1/6] extract_gradients", file=sys.stderr)
        _swallow_stdout(reflection_extract_gradients, since_ts)
    else:
        print(
            "  [1/6] extract_gradients skipped (MO_REFLECTION_EXTRACT_GRADIENTS=0)",
            file=sys.stderr,
        )
        try:
            _gradient_ensure_table()
        except Exception:
            pass

    print("  [2/6] deduplicate", file=sys.stderr)
    try:
        reflection_deduplicate("gradient_records")
    except Exception:
        pass

    print("  [3/6] link_failures", file=sys.stderr)
    try:
        reflection_link_failures("execution_traces")
    except Exception:
        pass

    print("  [4/6] detect_stale(gradient_records)", file=sys.stderr)
    try:
        reflection_detect_stale("gradient_records")
    except Exception:
        pass

    print("  [5/6] summarize_patterns (all clusters)", file=sys.stderr)
    db_path = os.environ["MINI_ORK_DB"]
    cluster_ids = _list_distinct_cluster_ids(db_path)
    for cid in cluster_ids:
        _swallow_stdout(reflection_summarize_patterns, cid)

    print("  [6/6] suggest_promotions + persist", file=sys.stderr)
    suggestions_json = _swallow_stdout(reflection_suggest_promotions, "pattern_records") or "[]"
    try:
        count = len(json.loads(suggestions_json))
    except Exception:
        count = 0
    persisted = 0
    try:
        # The persist step prints its count to stdout — we want to mirror bash's
        # `persisted="$(...)"`, which captures (consumes) the count without
        # re-emitting. Capture it and discard.
        _persisted_buf = io.StringIO()
        with redirect_stdout(_persisted_buf):
            persisted = reflection_persist_suggestions(suggestions_json)
    except Exception:
        persisted = 0
    # math.isclose gate: persisted is an int (no float drift), but if a future
    # port introduces a float coerce the parity test's 1e-6 tolerance still
    # holds.
    if not math.isclose(persisted, persisted, rel_tol=0, abs_tol=0):
        persisted = 0
    print(
        f"reflection_run: {count} promotion suggestions generated, {persisted} persisted",
        file=sys.stderr,
    )

    # [judge-gate] extract→distill→verify: promote only evidence-backed
    # emergent_patterns from 'proposed' → 'approved' so confabulated
    # self-diagnoses never reach routing/context (Dixit 2026).
    print("  [verify] judge-gate emergent_patterns", file=sys.stderr)
    approved = 0
    try:
        _approved_buf = io.StringIO()
        with redirect_stdout(_approved_buf):
            approved = reflection_verify_patterns()
    except Exception:
        approved = 0
    print(
        f"reflection_run: {approved} emergent_patterns approved by judge-gate",
        file=sys.stderr,
    )

    # Final stdout emission: bash's `echo "$suggestions"` — single copy.
    print(suggestions_json)
    return suggestions_json
