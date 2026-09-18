"""Canonical bounded ContextPack builder and prompt-context helpers.

Owns context_assemble (the ContextPack JSON builder with the rlm-6
slice-provider seam), failure_modes_md (the "Learned failure modes" prompt
block, incl. the 2026-06-13 project-scope filter), prior_runs_md (per-RUN
outcome memory block). The ContextNest capsule/retrieve wrappers and the
operator-steering and active-state blocks delegate to their native owners.

This module is the context-engine seam: what it emits is exactly what gets
injected into planner/worker prompts, so an evolvable-playbook loop (GEPA-style
weight-free improvement) plugs in here by scoring which emitted lessons help.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sqlite3
import sys
import time

from mini_ork.context import context_env
from mini_ork.similarity import rank_raw

FRAMEWORK_INTERNAL_PREFIXES = (
    "workflow.", "verifier.", "gate.", "recipe.",
    "provenance.", "provider.", "cache.", "dispatcher.",
)


def _db_path(db: str | None) -> str:
    if db:
        return db
    env = context_env("MINI_ORK_DB")
    if not env:
        raise RuntimeError("MINI_ORK_DB unset")
    return env


def approx_tokens(s: str) -> int:
    """Rough estimate: 1 token ~ 4 chars (parity with bash)."""
    return max(1, len(s) // 4)


# ── slice providers (rlm-6 seam) ─────────────────────────────────────────────

def slice_provider_default(pack: dict, budget: int) -> dict:
    """Legacy 64K-truncate: trim prior_runs then failure_modes, tag summary."""
    tokens_used = approx_tokens(json.dumps(pack))
    if tokens_used > budget:
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
            f"oldest prior_runs and low-confidence failure_modes removed.")
    return pack


def slice_provider_paged(pack: dict, budget: int) -> dict:
    pack = slice_provider_default(pack, budget)
    pack["_slice_provider"] = "paged"
    pack["_next_slice_hint"] = (
        "Fetch additional slices via context_assemble with the same "
        "MINI_ORK_SLICE_PROVIDER=paged and a follow-on cursor; this "
        "stub only emits the first slice.")
    return pack


SLICE_PROVIDERS = {"default": slice_provider_default, "paged": slice_provider_paged}


# ── per-call prompt-block cap (F6a) ───────────────────────────────────────────

DEFAULT_SECTION_MAX_CHARS = 80_000


def section_max_chars() -> int:
    """MO_CTX_SECTION_MAX_CHARS, default 80K chars (~20K tokens) per inlined
    section. <=0 disables capping."""
    try:
        return int(os.environ.get("MO_CTX_SECTION_MAX_CHARS",
                                  str(DEFAULT_SECTION_MAX_CHARS)))
    except ValueError:
        return DEFAULT_SECTION_MAX_CHARS


def cap_block(text: str, max_chars: int | None = None, *, label: str = "") -> str:
    """F6a: bound one prompt-inlined section. Live-DB receipts (audit F6a):
    reviewer/panel prompts inlined whole verifier JSONs, ledgers and diffs —
    multi-MB sections rode every agentic round-trip (worst measured lens call:
    14.77M input tokens, $2.89, 27 sibling calls with zero output). Keeps the
    head (setup/manifest) and tail (verdict/result) around an omission marker
    so both ends of a section stay reviewable."""
    if max_chars is None:
        max_chars = section_max_chars()
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    head = int(max_chars * 0.6)
    tail = max_chars - head
    omitted = len(text) - head - tail
    name = f" of {label!r}" if label else ""
    return (f"{text[:head]}\n"
            f"…[context cap: omitted {omitted} chars{name}; "
            f"read the full file at the path above if needed]\n"
            f"{text[len(text) - tail:]}")


# ── the ContextPack builder ──────────────────────────────────────────────────

def context_assemble(task_brief_path: str, workflow_node: str,
                     db: str | None = None,
                     verifier_contract: dict | None = None) -> dict:
    """Build the canonical bounded ContextPack."""
    with open(task_brief_path, encoding="utf-8") as fh:
        brief_raw = fh.read()
    budget = int(os.environ.get("MINI_ORK_CTX_BUDGET_TOKENS", "64000"))
    try:
        brief = json.loads(brief_raw)
    except (ValueError, TypeError):
        brief = {"raw": brief_raw}
    task_class = brief.get("task_class", "") if isinstance(brief, dict) else ""
    verifier_contract = verifier_contract or {}

    con = sqlite3.connect(_db_path(db))
    con.row_factory = sqlite3.Row
    cur_run = context_env("MINI_ORK_RUN_ID", "")

    prior_runs = []
    try:
        for r in con.execute("""
            SELECT trace_id, task_class, status, cost_usd, duration_ms, created_at
            FROM execution_traces
            WHERE task_class = ? AND (? = '' OR run_id IS NULL OR run_id != ?)
            ORDER BY created_at DESC LIMIT 10
        """, (task_class, cur_run, cur_run)).fetchall():
            prior_runs.append({
                "cite": f"execution_traces/{r['trace_id']}",
                "trace_id": r["trace_id"], "status": r["status"],
                "cost_usd": r["cost_usd"], "duration_ms": r["duration_ms"],
                "created_at": r["created_at"]})
    except Exception:
        pass

    failure_modes = []
    try:
        for r in con.execute("""
            SELECT target, signal, suggested_change, confidence,
                   (task_class = '__cross_class__') AS is_cross_class
            FROM gradient_records
            WHERE ((task_class = ? OR target LIKE ?) OR task_class = '__cross_class__')
              AND confidence >= 0.6
            ORDER BY is_cross_class DESC, confidence DESC LIMIT 10
        """, (task_class, f"%{task_class}%")).fetchall():
            failure_modes.append({
                "cite": f"gradient_records/{r['target']}",
                "target": r["target"], "signal": r["signal"],
                "suggested_change": r["suggested_change"],
                "confidence": r["confidence"],
                "scope": "cross_class" if r["is_cross_class"] else task_class})
    except Exception:
        pass

    # Verified emergent patterns (judge-gate approved) — read-back of the
    # reflection judge-gate. ONLY status='approved' rows (those that cleared the
    # evidence/strength floor in reflection_verify_patterns); 'proposed' rows are
    # unverified self-diagnoses and are excluded to avoid memory confabulation
    # (Dixit 2026). Sibling of known_failure_modes. Opt-out MO_EMERGENT_INJECT=0;
    # cold-safe (empty/missing table → []).
    verified_emergent = []
    if os.environ.get("MO_EMERGENT_INJECT", "1") == "1":
        try:
            emg_limit = int(os.environ.get("MO_EMERGENT_INJECT_LIMIT", "3"))
        except ValueError:
            emg_limit = 3
        try:
            for r in con.execute("""
                SELECT pattern_id, cluster_label, feature_set_json,
                       strength_score, suggested_meta_adr
                FROM emergent_patterns
                WHERE status='approved'
                ORDER BY strength_score DESC, detected_at DESC LIMIT ?
            """, (emg_limit,)).fetchall():
                try:
                    feats = json.loads(r["feature_set_json"]) if r["feature_set_json"] else []
                except Exception:
                    feats = []
                verified_emergent.append({
                    "cite": f"emergent_patterns/{r['pattern_id']}",
                    "feature": feats[0] if feats else "emergent",
                    "cluster_label": r["cluster_label"],
                    "suggested_change": r["suggested_meta_adr"] or "",
                    "strength_score": r["strength_score"],
                    "scope": "emergent"})
        except Exception:
            pass

    similar_lessons = []
    try:
        query_text = " ".join(filter(None, [
            brief.get("goal", "") if isinstance(brief, dict) else "",
            brief.get("title", "") if isinstance(brief, dict) else "",
            brief.get("description", "") if isinstance(brief, dict) else "",
            task_class]))

        for tbl, col, kind in (("bug_reports", "title", "bug"),
                               ("gradient_records", "signal", "gradient"),
                               ("learning_record", "title", "learning")):
            try:
                rows = con.execute(
                    f"SELECT rowid AS rid, * FROM {tbl} LIMIT 2000").fetchall()
            except sqlite3.OperationalError:
                continue
            docs = [(r[col] or "") for r in rows]
            scored = [
                (score, rows[index])
                for score, index in rank_raw(query_text, docs)
                if score >= 0.15
            ]
            for s, r in scored[:3]:
                similar_lessons.append({
                    "cite": f"{tbl}/{r['rid']}", "kind": kind,
                    "score": round(s, 4), "title": (r[col] or "")[:200],
                    "suggested_fix": (r["suggested_fix"] if "suggested_fix" in r.keys()
                                      else r["suggested_change"]
                                      if "suggested_change" in r.keys() else "") or ""})
    except Exception:
        pass

    user_prefs = {}
    try:
        cfg_path = os.path.join(context_env("MINI_ORK_HOME", ".mini-ork"),
                                "config", "user_preferences.json")
        user_prefs = json.load(open(cfg_path, encoding="utf-8"))
        user_prefs["cite"] = cfg_path
    except Exception:
        pass

    constraints, forbidden_fallbacks = [], []
    try:
        cfg_path = os.path.join(context_env("MINI_ORK_HOME", ".mini-ork"),
                                "config", "constraints.json")
        cfg = json.load(open(cfg_path, encoding="utf-8"))
        constraints = cfg.get("constraints", [])
        forbidden_fallbacks = cfg.get("forbidden_fallbacks", [])
    except Exception:
        pass
    con.close()

    # graph_context — failure-linked evidence for the same three sources the
    # prompt block reads. Init-before-try so the key survives any DB failure.
    graph_context = {"linked_gradients": [], "failure_hotspots": [],
                     "outstanding_blame": []}
    try:
        gc_db = _db_path(db)
        if os.environ.get("MO_GRAPH_CONTEXT", "1") == "1" and os.path.isfile(gc_db):
            gc_linked, gc_hotspots, gc_blame = _graph_context_rows(task_class, 5, gc_db)
            graph_context["linked_gradients"] = [
                {"cite": f"gradient_records/{r[0]}", "target": r[0],
                 "signal": r[1], "suggested_change": r[2], "confidence": r[3],
                 "target_class": r[4], "link_count": r[5]}
                for r in gc_linked]
            graph_context["failure_hotspots"] = [
                {"cite": f"failure_memory/{r[0]}/{r[1]}", "workflow_stage": r[0],
                 "failure_category": r[1], "count": r[2], "last_at": r[3],
                 "last_error": r[4]}
                for r in gc_hotspots]
            graph_context["outstanding_blame"] = [
                {"cite": f"defect_attributions/{r[0]}/{r[1]}", "lane": r[0],
                 "code_region": r[1], "task_class": r[2], "severity": r[3],
                 "penalty": r[4], "decay_halflife_days": r[5], "ts": r[6]}
                for r in gc_blame]
    except Exception:
        pass

    pack = {
        "task_brief": {"content": brief, "cite": "task_brief_path"},
        "workflow_node": workflow_node,
        "verifier_contract": {"content": verifier_contract, "cite": "artifact_contract"},
        "prior_similar_runs": prior_runs,
        "known_failure_modes": failure_modes,
        "verified_emergent_patterns": verified_emergent,
        "similar_lessons": similar_lessons,
        "graph_context": graph_context,
        "user_preferences": user_prefs,
        "constraints": constraints,
        "forbidden_fallbacks": forbidden_fallbacks,
        "assembled_at": int(time.time()),
        "budget_tokens": budget,
    }
    provider = os.environ.get("MINI_ORK_SLICE_PROVIDER", "default")
    pack = SLICE_PROVIDERS.get(provider, slice_provider_default)(pack, budget)
    pack["tokens_estimated"] = approx_tokens(json.dumps(pack))
    return pack


# ── prompt-block emitters ────────────────────────────────────────────────────

def failure_modes_md(task_class: str, limit: int = 5, db: str | None = None) -> str:
    """The "Learned failure modes" block; '' when no learnings. Includes the
    project-scope filter: framework-internal targets are stripped when
    MO_TARGET_CWD is set and differs from MINI_ORK_ROOT."""
    dbp = _db_path(db)
    if not os.path.isfile(dbp):
        return ""
    strip_framework = False
    tgt, root = context_env("MO_TARGET_CWD", ""), context_env("MINI_ORK_ROOT", "")
    if tgt and root:
        try:
            strip_framework = os.path.realpath(tgt) != os.path.realpath(root)
        except OSError:
            strip_framework = False
    con = sqlite3.connect(dbp)
    con.execute("PRAGMA busy_timeout=5000")
    try:
        rows = con.execute("""
            SELECT target, signal, suggested_change
            FROM gradient_records
            WHERE (task_class = ? OR target LIKE ?) AND confidence >= 0.6
            ORDER BY confidence DESC, created_at DESC LIMIT ?
        """, (task_class, f"%{task_class}%",
              limit * 4 if strip_framework else limit)).fetchall()
    except sqlite3.OperationalError:
        rows = []
    finally:
        con.close()
    if strip_framework:
        rows = [r for r in rows
                if not r[0].startswith(FRAMEWORK_INTERNAL_PREFIXES)][:limit]
    else:
        rows = rows[:limit]
    out = []
    if rows:
        out.append("--- Learned failure modes (from prior runs of this task class) ---")
        out.append("Avoid repeating these known issues:")
        for target, signal, change in rows:
            out.append(f"- [{target}] {signal.strip()}")
            out.append(f"  Fix applied going forward: {change.strip()}")
        out.append("--- /learned failure modes ---")

    # Verified emergent patterns (judge-gate approved) — read-back into the
    # prompt. ONLY status='approved' rows (cleared the evidence/strength floor
    # in reflection_verify_patterns); 'proposed' self-diagnoses excluded
    # (memory-confabulation guard, Dixit 2026). Opt-out MO_EMERGENT_INJECT=0;
    # cold-safe (empty/missing table → nothing).
    if os.environ.get("MO_EMERGENT_INJECT", "1") == "1":
        try:
            emg_limit = int(os.environ.get("MO_EMERGENT_INJECT_LIMIT", "3"))
        except ValueError:
            emg_limit = 3
        # The semantic channel owns this block when it can serve it: it ranks
        # the same approved patterns by earned utility rather than the static
        # strength_score, and closes the retrieval loop while it is there. An
        # unavailable or opted-out channel degrades to the static ordering —
        # never to nothing, because the lessons are still evidence.
        block = semantic_lessons_md(task_class, emg_limit, db=dbp)
        if not block:
            block = _static_emergent_block(dbp, emg_limit)
        if block:
            out.append(block)

    return "\n".join(out)


# ── emergent-pattern read-back (static + utility-ranked) ─────────────────────

def _approved_emergent_rows(dbp: str) -> list[tuple]:
    """Approved (judge-gated) emergent patterns, strongest first.

    Cold-safe: a missing table, or one predating any of these columns, is an
    empty list and not an error — this runs on the prompt-injection path.
    """
    con = sqlite3.connect(dbp)
    con.execute("PRAGMA busy_timeout=5000")
    try:
        return con.execute("""
            SELECT pattern_id, cluster_label, feature_set_json, strength_score
            FROM emergent_patterns
            WHERE status='approved'
            ORDER BY strength_score DESC, detected_at DESC
        """).fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        con.close()


def _emergent_text(feature_set_json: str | None, cluster_label: str | None) -> str:
    """The one-line form of a pattern — `[feat] label`.

    Shared by the static block and the semantic mirror so a memory reads
    exactly as the pattern it came from.
    """
    try:
        feats = json.loads(feature_set_json) if feature_set_json else []
    except Exception:
        feats = []
    feat = feats[0] if feats else "emergent"
    return f"[{feat}] {(cluster_label or '').strip()}"


def _static_emergent_block(dbp: str, limit: int) -> str:
    """Strength-ordered read-back — the pre-semantic behaviour, kept as the
    degradation path. Ordering is unchanged; only the LIMIT moved to Python.
    """
    rows = _approved_emergent_rows(dbp)[:limit]
    if not rows:
        return ""
    lines = ["--- Verified emergent patterns (cross-run, judge-gate approved) ---"]
    lines.extend(
        f"- {_emergent_text(feats, label)}" for _pid, label, feats, _strength in rows
    )
    lines.append("--- /verified emergent patterns ---")
    return "\n".join(lines)


def semantic_lessons_md(task_class: str, limit: int = 5, db: str | None = None) -> str:
    """The emergent-pattern block, ranked by earned utility (SimUtil-UCB).

    Same rows, same shape as `_static_emergent_block` — different order. The
    channel mirrors each approved pattern into the semantic store under a key
    derived from its `pattern_id` (so a pattern is one memory, however alike it
    reads to another), sweeps finished runs to resolve retrievals already in
    flight, then ranks the mirrors by `strength_score` adjusted for what each
    has actually earned.

    `strength_score` is the prior, not the query: it is a measured frequency,
    so it still decides *which* patterns are eligible — but it is the only
    thing doing so today, and it cannot know whether a pattern helped. Utility
    and exploration reorder inside that gate. An untried scope reproduces the
    static block exactly, so this is a non-regressive default; from then on
    the block also samples eligible patterns nobody has tried, which is the
    only way they ever get evidence.

    Returns '' when the channel is opted out, unavailable, or has nothing to
    say — the caller decides what to fall back to.
    """
    from mini_ork import memory as semantic

    if os.environ.get("MO_SEMANTIC_INJECT", "1") != "1":
        return ""
    dbp = _db_path(db)
    if not os.path.isfile(dbp):
        return ""
    rows = _approved_emergent_rows(dbp)
    if not rows:
        return ""

    scope = task_class or "generic"
    try:
        # Close the loop before reading. A run that already finished has its
        # verdict sitting in execution_traces, and a retrieval left pending
        # counts toward `uses` forever with no chance of a win — so failing to
        # sweep here would silently decay every memory the run used.
        semantic.resolve_finished_runs(db_path=dbp)

        candidates = []
        for pattern_id, label, feats, strength in rows:
            memory_id = semantic.upsert(
                _emergent_text(feats, label),
                scope=scope,
                key=str(pattern_id),
                db_path=dbp,
            )
            candidates.append((int(memory_id), float(strength or 0.0)))

        ordered = semantic.rank_with_prior(
            candidates, scope=scope, top_k=limit, db_path=dbp,
        )

        # Log the retrieval only when the caller is inside a run: an
        # unattributable retrieval can never be resolved to a win, so writing
        # one would depress this memory's utility for no information gained.
        run_id = context_env("MINI_ORK_RUN_ID", "")
        if run_id:
            semantic.record_retrievals(
                [hit["memory_id"] for hit in ordered],
                scope=scope,
                run_id=run_id,
                task_class=task_class or "",
                db_path=dbp,
            )

        lines = ["--- Verified emergent patterns (cross-run, judge-gate approved) ---"]
        for hit in ordered:
            # Credit is only ever claimed, never speculated: a retrieval whose
            # run has not reported yet says nothing about this memory, so it
            # gets no suffix rather than an implied "helped 0/N".
            suffix = ""
            if hit["wins"] > 0:
                suffix = f"  (helped {hit['wins']}/{hit['uses']} retrievals)"
            lines.append(f"- {hit['text']}{suffix}")
        lines.append("--- /verified emergent patterns ---")
        return "\n".join(lines)
    except Exception:
        # Any failure here degrades to the static block upstream. The prompt
        # path must never break because the ranking layer could not run.
        return ""


def prior_runs_md(task_class: str, limit: int = 5, db: str | None = None) -> str:
    """Per-RUN prior-outcome memory block; '' when no prior runs."""
    dbp = _db_path(db)
    if not os.path.isfile(dbp):
        return ""
    cur_run = context_env("MINI_ORK_RUN_ID", "")
    con = sqlite3.connect(dbp)
    con.execute("PRAGMA busy_timeout=5000")
    try:
        rows = con.execute("""
            SELECT COALESCE(run_id, trace_id) AS run_key,
                   COUNT(*) AS nodes,
                   SUM(CASE WHEN status NOT IN ('success','running') THEN 1 ELSE 0 END) AS failed_nodes,
                   SUM(COALESCE(cost_usd, 0)) AS total_cost,
                   SUM(COALESCE(duration_ms, 0)) AS total_dur_ms,
                   MAX(created_at) AS last_at
            FROM execution_traces
            WHERE task_class = ? AND (? = '' OR run_id IS NULL OR run_id != ?)
            GROUP BY run_key ORDER BY last_at DESC LIMIT ?
        """, (task_class, cur_run, cur_run, limit)).fetchall()
    except sqlite3.OperationalError:
        rows = []
    finally:
        con.close()
    if not rows:
        return ""
    n_ok = sum(1 for r in rows if (r[2] or 0) == 0)
    out = ["--- Prior runs of this task class (memory) ---",
           f"{len(rows)} most recent: {n_ok} clean / {len(rows) - n_ok} with failures. "
           "Calibrate plan scope and verifier strictness against these outcomes:"]
    for run_key, nodes, failed, cost, dur_ms, _last_at in rows:
        outcome = "success" if (failed or 0) == 0 else f"{failed}/{nodes} nodes failed"
        cost_s = f"${cost:.2f}" if isinstance(cost, (int, float)) else "?"
        dur_s = f"{int(dur_ms) // 1000}s" if isinstance(dur_ms, (int, float)) else "?"
        out.append(f"- {run_key}: {outcome} ({nodes} nodes, cost {cost_s}, {dur_s})")
    out.append("--- /prior runs ---")
    return "\n".join(out)


def _graph_context_rows(task_class: str, limit: int, dbp: str,
                        strip_framework: bool = False) -> tuple[list, list, list]:
    """The three graph-context sources over one cold-safe connection.

    Shared by graph_context_md and context_assemble so the prompt block and the
    pack entry can never disagree. Each query degrades to [] on its own
    sqlite3.OperationalError — a missing table is the expected case for
    failure_links, which reflection_pipeline creates on demand rather than
    db/init.sh. The failure_links JOIN is deliberately INNER: gradient_id is
    nullable, and a LEFT JOIN would surface null-target rows.
    """
    linked: list = []
    hotspots: list = []
    blame: list = []
    con = sqlite3.connect(dbp)
    con.execute("PRAGMA busy_timeout=5000")
    try:
        try:
            linked = con.execute("""
                SELECT g.target, g.signal, g.suggested_change, g.confidence,
                       g.task_class AS target_class, COUNT(*) AS link_count
                  FROM failure_links fl
                  JOIN gradient_records g ON g.gradient_id = fl.gradient_id
                 WHERE fl.task_class = ?
                 GROUP BY g.target, g.signal, g.suggested_change, g.confidence, g.task_class
                 ORDER BY link_count DESC, g.confidence DESC
                 LIMIT ?
            """, (task_class, limit * 4 if strip_framework else limit)).fetchall()
        except sqlite3.OperationalError:
            linked = []
        try:
            hotspots = con.execute("""
                SELECT fm.workflow_stage, fm.failure_category, COUNT(*) AS n,
                       MAX(fm.occurred_at) AS last_at,
                       (SELECT fm2.error_message FROM failure_memory fm2
                         WHERE fm2.workflow_stage = fm.workflow_stage
                           AND fm2.failure_category = fm.failure_category
                         ORDER BY fm2.occurred_at DESC LIMIT 1) AS last_error
                  FROM failure_memory fm
                 GROUP BY fm.workflow_stage, fm.failure_category
                 ORDER BY n DESC, last_at DESC
                 LIMIT ?
            """, (limit,)).fetchall()
        except sqlite3.OperationalError:
            hotspots = []
        try:
            blame = con.execute("""
                SELECT lane, code_region, task_class, severity, penalty,
                       decay_halflife_days, ts
                  FROM defect_attributions
                 WHERE task_class = ?
                 ORDER BY ts DESC
                 LIMIT ?
            """, (task_class, limit)).fetchall()
        except sqlite3.OperationalError:
            blame = []
    finally:
        con.close()
    return linked, hotspots, blame


def graph_context_md(task_class: str, limit: int = 5, db: str | None = None) -> str:
    """Failure-linked graph context block; '' when gated off, the DB is missing,
    or all three sub-sections are empty. Includes the same project-scope filter
    as failure_modes_md: framework-internal targets are stripped when MO_TARGET_CWD
    is set and differs from MINI_ORK_ROOT. Never raises, for any DB state."""
    if os.environ.get("MO_GRAPH_CONTEXT", "1") != "1":
        return ""
    dbp = _db_path(db)
    if not os.path.isfile(dbp):
        return ""
    strip_framework = False
    tgt, root = context_env("MO_TARGET_CWD", ""), context_env("MINI_ORK_ROOT", "")
    if tgt and root:
        try:
            strip_framework = os.path.realpath(tgt) != os.path.realpath(root)
        except OSError:
            strip_framework = False
    linked, hotspots, blame = _graph_context_rows(task_class, limit, dbp, strip_framework)
    if strip_framework:
        linked = [r for r in linked
                  if not r[0].startswith(FRAMEWORK_INTERNAL_PREFIXES)][:limit]
    else:
        linked = linked[:limit]

    now_utc = datetime.datetime.utcnow()
    blame_lines = []
    for lane, region, _task_class, severity, penalty, halflife, ts in blame:
        try:
            pen = float(penalty)
            hlf = float(halflife) if halflife is not None else 30.0
        except (TypeError, ValueError):
            continue
        if hlf <= 0:
            continue
        tsv = str(ts).strip().rstrip("Z").replace("T", " ")
        parsed = None
        for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
            try:
                parsed = datetime.datetime.strptime(tsv, fmt)
                break
            except ValueError:
                parsed = None
        if parsed is None:
            continue
        age_days = max((now_utc - parsed).total_seconds() / 86400.0, 0.0)
        decayed = pen * (0.5 ** (age_days / hlf))
        blame_lines.append(
            f"- [{lane}/{region}] severity={severity} decayed_penalty={decayed:.3f}")

    if not linked and not hotspots and not blame_lines:
        return ""
    out = ["--- Learned graph context (failure-linked) ---"]
    if linked:
        out.append("Gradient targets evidenced by real failures (INNER JOIN):")
        for target, signal, change, _confidence, target_class, link_count in linked:
            scope = (f" [class: {target_class}]"
                     if target_class and target_class != task_class else "")
            out.append(f"- [{target}]{scope} {link_count} failure link(s): "
                       f"{(signal or '').strip()}")
            out.append(f"  Fix applied going forward: {(change or '').strip()}")
    if hotspots:
        out.append("Recurring failure hotspots:")
        for stage, category, n, last_at, last_error in hotspots:
            detail = (last_error or "").strip().replace("\n", " ")
            detail = f" — {detail[:200]}" if detail else ""
            out.append(f"- [{stage}/{category}] {n}x (last {last_at}){detail}")
    if blame_lines:
        out.append("Outstanding defect blame (decay-weighted):")
        out.extend(blame_lines)
    out.append("--- /learned graph context ---")
    return "\n".join(out)


def operator_steering_md(role: str, db: str | None = None) -> str:
    """Consume and render operator guidance targeted at one agent role."""
    from mini_ork.steering import operator_steering

    rows = operator_steering.fetch_for(
        context_env("MINI_ORK_RUN_ID", ""), role, db_path=db
    )
    if not rows:
        return ""
    out = [
        "--- Operator steering (injected supervisor guidance) ---",
        f"{len(rows)} message(s) targeted at this node. Treat as load-bearing:",
    ]
    for row in rows:
        severity = str(row.get("severity", "info")).upper()
        source = row.get("source") or "unknown"
        out.append(f"- [{severity}] (from {source}) {row.get('message', '')}")
    out.append("--- /operator steering ---")
    return "\n".join(out)


def _contextnest_query(task_brief_path: str) -> str:
    try:
        with open(task_brief_path, encoding="utf-8") as fh:
            raw = fh.read()
    except OSError:
        return ""
    try:
        data = json.loads(raw)
    except Exception:
        return raw[:512].strip()
    if not isinstance(data, dict):
        return raw[:512].strip()
    parts = [
        value.strip()
        for key in ("title", "objective", "description", "task_class")
        if isinstance((value := data.get(key)), str) and value.strip()
    ]
    return " ".join(parts)[:600] if parts else raw[:512].strip()


def _capsule_query(query: str) -> str:
    for raw_token in query.split()[:5]:
        token = raw_token.strip("`#*.,:;!?()[]{}\"'")
        if len(token) >= 4 and any(char.isalnum() for char in token):
            return token
    return ""


def contextnest_atoms_md(
    task_brief_path: str,
    limit: int = 5,
    *,
    client=None,
) -> str:
    """Render ContextNest capsule content, falling back to retrieved atoms."""
    if os.environ.get("MO_DISABLE_CN", "0") == "1" or not os.path.isfile(task_brief_path):
        return ""
    from mini_ork import cn_client

    client = client or cn_client
    if not client.available():
        return ""
    query = _contextnest_query(task_brief_path)
    if not query:
        return ""
    capsule = client.capsule(_capsule_query(query), "14d")
    try:
        min_chars = int(os.environ.get("CN_CAPSULE_MIN_CHARS", "100"))
    except ValueError:
        min_chars = 100
    if len(capsule) > min_chars and any(
        line.startswith("## ") for line in capsule.splitlines()
    ):
        return (
            "--- ContextNest capsule (kind-ordered substrate digest) ---\n"
            f"{capsule}\n"
            "--- /ContextNest capsule ---\n"
        )
    return client.render_atoms_md(client.retrieve(query, int(limit)), int(limit))


def contextnest_recent_sessions_md(
    task_brief_path: str,
    max_files: int = 3,
    *,
    client=None,
) -> str:
    """Render recent ContextNest sessions for file hints in a task brief."""
    if os.environ.get("MO_DISABLE_CN", "0") == "1" or not os.path.isfile(task_brief_path):
        return ""
    from mini_ork import cn_client

    client = client or cn_client
    if not client.available():
        return ""
    try:
        with open(task_brief_path, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return ""
    candidates: list[str] = []
    if isinstance(data, dict):
        for key in ("files", "paths", "relevant_files", "targets"):
            value = data.get(key)
            if not isinstance(value, list):
                continue
            for item in value:
                if isinstance(item, str):
                    candidates.append(item)
                elif isinstance(item, dict):
                    path = item.get("path") or item.get("file") or item.get("name")
                    if isinstance(path, str):
                        candidates.append(path)
    sections: list[str] = []
    for path in candidates[: int(max_files)]:
        try:
            payload = json.loads(client.sessions_by_file(path))
        except Exception:
            continue
        sessions = payload.get("sessions") or payload.get("hits") or []
        if not sessions:
            continue
        lines = [f"- File `{path}` recently touched in:"]
        for session in sessions[:3]:
            session_id = session.get("session_id") or session.get("id", "")
            timestamp = (session.get("last_seen") or session.get("ts") or "")[:10]
            title = (session.get("title") or session.get("intent") or "").strip()[:80]
            lines.append(f"  - {session_id[:8]} ({timestamp}) {title}")
        sections.extend(lines)
    if not sections:
        return ""
    return (
        "--- ContextNest: recent sessions for relevant files ---\n"
        + "\n".join(sections)
        + "\n--- /ContextNest: recent sessions ---\n"
    )


def active_state_md(task_class: str = "__any__", days: int = 30, db: str | None = None) -> str:
    """Render the native active-state index for prompt injection."""
    if os.environ.get("MO_DISABLE_ACTIVE_STATE", "0") == "1":
        return ""
    from mini_ork.orchestration.active_state_index import render_active_state_block

    return render_active_state_block(task_class, days, db_path=db)


def main(argv: list[str] | None = None) -> int:
    """CLI used by shell integration fixtures while their owners remain Bash."""
    parser = argparse.ArgumentParser(prog="python -m mini_ork.context_assembler")
    sub = parser.add_subparsers(dest="command", required=True)
    assemble = sub.add_parser("assemble")
    assemble.add_argument("task_brief_path")
    assemble.add_argument("workflow_node")
    atoms = sub.add_parser("contextnest-atoms")
    atoms.add_argument("task_brief_path")
    atoms.add_argument("limit", nargs="?", type=int, default=5)
    recent = sub.add_parser("contextnest-recent-sessions")
    recent.add_argument("task_brief_path")
    recent.add_argument("max_files", nargs="?", type=int, default=3)
    args = parser.parse_args(argv)
    if args.command == "assemble":
        print(json.dumps(context_assemble(args.task_brief_path, args.workflow_node)))
    elif args.command == "contextnest-atoms":
        sys.stdout.write(contextnest_atoms_md(args.task_brief_path, args.limit))
    else:
        sys.stdout.write(
            contextnest_recent_sessions_md(args.task_brief_path, args.max_files)
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
