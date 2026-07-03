"""Bounded ContextPack builder — Python port of lib/context_assembler.sh (core).

Ported here: context_assemble (the ContextPack JSON builder with the rlm-6
slice-provider seam), failure_modes_md (the "Learned failure modes" prompt
block, incl. the 2026-06-13 project-scope filter), prior_runs_md (per-RUN
outcome memory block). The ContextNest capsule/retrieve wrappers and the
operator-steering block call an external service / another lib and are ported
separately (see tracker).

This module is the context-engine seam: what it emits is exactly what gets
injected into planner/worker prompts, so an evolvable-playbook loop (GEPA-style
weight-free improvement) plugs in here by scoring which emitted lessons help.
"""
from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import time
from collections import Counter

FRAMEWORK_INTERNAL_PREFIXES = (
    "workflow.", "verifier.", "gate.", "recipe.",
    "provenance.", "provider.", "cache.", "dispatcher.",
)


def _db_path(db: str | None) -> str:
    if db:
        return db
    env = os.environ.get("MINI_ORK_DB")
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


# ── the ContextPack builder ──────────────────────────────────────────────────

def context_assemble(task_brief_path: str, workflow_node: str,
                     db: str | None = None,
                     verifier_contract: dict | None = None) -> dict:
    """Build the bounded ContextPack (same shape/keys as bash context_assemble)."""
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
    cur_run = os.environ.get("MINI_ORK_RUN_ID", "")

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

    similar_lessons = []
    try:
        query_text = " ".join(filter(None, [
            brief.get("goal", "") if isinstance(brief, dict) else "",
            brief.get("title", "") if isinstance(brief, dict) else "",
            brief.get("description", "") if isinstance(brief, dict) else "",
            task_class]))

        def _stok(s):
            s = (s or "").lower()
            s = re.sub(r"[^\w./_-]+", " ", s)
            return [t for t in s.split() if len(t) >= 3]

        def _stf(toks):
            c = Counter(toks)
            total = sum(c.values()) or 1
            return {t: cnt / total for t, cnt in c.items()}

        def _scos(a, b):
            keys = set(a) | set(b)
            dot = sum(a.get(k, 0) * b.get(k, 0) for k in keys)
            na = math.sqrt(sum(v * v for v in a.values()))
            nb = math.sqrt(sum(v * v for v in b.values()))
            return dot / (na * nb) if na and nb else 0.0

        for tbl, col, kind in (("bug_reports", "title", "bug"),
                               ("gradient_records", "signal", "gradient"),
                               ("learning_record", "title", "learning")):
            try:
                rows = con.execute(
                    f"SELECT rowid AS rid, * FROM {tbl} LIMIT 2000").fetchall()
            except sqlite3.OperationalError:
                continue
            docs = [_stok((r[col] or "")) for r in rows]
            df = Counter()
            for d in docs:
                for t in set(d):
                    df[t] += 1
            n = max(len(docs), 1)
            idf = {t: math.log(1.0 + n / (1 + c)) for t, c in df.items()}

            def _svec(toks):
                return {t: w * idf.get(t, 0.0) for t, w in _stf(toks).items()}
            q_vec = _svec(_stok(query_text))
            scored = [(s, r) for r, d in zip(rows, docs)
                      if (s := _scos(q_vec, _svec(d))) >= 0.15]
            scored.sort(reverse=True, key=lambda p: p[0])
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
        cfg_path = os.path.join(os.environ.get("MINI_ORK_HOME", ".mini-ork"),
                                "config", "user_preferences.json")
        user_prefs = json.load(open(cfg_path, encoding="utf-8"))
        user_prefs["cite"] = cfg_path
    except Exception:
        pass

    constraints, forbidden_fallbacks = [], []
    try:
        cfg_path = os.path.join(os.environ.get("MINI_ORK_HOME", ".mini-ork"),
                                "config", "constraints.json")
        cfg = json.load(open(cfg_path, encoding="utf-8"))
        constraints = cfg.get("constraints", [])
        forbidden_fallbacks = cfg.get("forbidden_fallbacks", [])
    except Exception:
        pass
    con.close()

    pack = {
        "task_brief": {"content": brief, "cite": "task_brief_path"},
        "workflow_node": workflow_node,
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
    tgt, root = os.environ.get("MO_TARGET_CWD", ""), os.environ.get("MINI_ORK_ROOT", "")
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
    if not rows:
        return ""
    out = ["--- Learned failure modes (from prior runs of this task class) ---",
           "Avoid repeating these known issues:"]
    for target, signal, change in rows:
        out.append(f"- [{target}] {signal.strip()}")
        out.append(f"  Fix applied going forward: {change.strip()}")
    out.append("--- /learned failure modes ---")
    return "\n".join(out)


def prior_runs_md(task_class: str, limit: int = 5, db: str | None = None) -> str:
    """Per-RUN prior-outcome memory block; '' when no prior runs."""
    dbp = _db_path(db)
    if not os.path.isfile(dbp):
        return ""
    cur_run = os.environ.get("MINI_ORK_RUN_ID", "")
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
