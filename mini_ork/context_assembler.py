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
import json
import os
import sqlite3
import sys
import time

from mini_ork.similarity import rank_raw

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
        "verified_emergent_patterns": verified_emergent,
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
        con2 = sqlite3.connect(dbp)
        con2.execute("PRAGMA busy_timeout=5000")
        try:
            emg = con2.execute("""
                SELECT cluster_label, feature_set_json, strength_score
                FROM emergent_patterns
                WHERE status='approved'
                ORDER BY strength_score DESC, detected_at DESC LIMIT ?
            """, (emg_limit,)).fetchall()
        except sqlite3.OperationalError:
            emg = []
        finally:
            con2.close()
        if emg:
            out.append("--- Verified emergent patterns (cross-run, judge-gate approved) ---")
            for cluster_label, feature_set_json, _strength in emg:
                try:
                    feats = json.loads(feature_set_json) if feature_set_json else []
                except Exception:
                    feats = []
                feat = feats[0] if feats else "emergent"
                out.append(f"- [{feat}] {(cluster_label or '').strip()}")
            out.append("--- /verified emergent patterns ---")

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


def operator_steering_md(role: str, db: str | None = None) -> str:
    """Consume and render operator guidance targeted at one agent role."""
    from mini_ork.steering import operator_steering

    rows = operator_steering.fetch_for(
        os.environ.get("MINI_ORK_RUN_ID", ""), role, db_path=db
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
