"""Python port of bin/mini-ork-execute — the executor (INCREMENTAL).

bin/mini-ork-execute is the 3449-line node-dispatch engine. Its orchestration
core (_dispatch_node + the DAG loop) drives every run and warrants dedicated
harsh-critic review before its default flips; this module lands the deterministic
HELPER LAYER first, behind a live-bash parity gate, so the risky core ports onto
verified foundations.

Ported here (all pure / transcribed verbatim):
    reward_from_status(status, verdict)     — status/verdict → GRPO reward
    dispatch_chain(node_type, lead)         — role-aware fallback lane chain (deduped)
    learning_static_lane(node_type, lane)   — static lane synthesis for unpinned nodes
    finish_reason_for_failure(rc, text)     — rc/text → finish reason
    infer_trace_code_region(payload)        — files_written → top-level code region
    learning_update_conductor_outcomes(db)  — resolve pending conductor decisions
    write_grpo_advantages(db)               — GRPO group-relative advantage writeback
"""
from __future__ import annotations

import datetime
import json
import math
import os
import sqlite3


def reward_from_status(status: str = "", verdict: str = "") -> str:
    v = (verdict or "").lower()
    if v in ("approve", "approved", "pass", "passed", "success", "ok"):
        return "1.0"
    if v in ("reject", "rejected", "fail", "failed", "request_changes", "needs_revision", "escalate"):
        return "0.0"
    s = (status or "").lower()
    if s in ("success", "published", "done", "approve", "approved", "pass", "passed"):
        return "1.0"
    if s in ("failure", "failed", "rolled_back", "blocked", "crash", "escalated", "reject", "rejected"):
        return "0.0"
    return "0.5"


_CODING_ROLES = {"implementer", "worker", "spec_author", "healer", "planner", "researcher",
                 "reflector", "replanner", "synthesizer", "bdd_runner"}
_REVIEW_ROLES = {"reviewer", "spec_reviewer", "verifier", "brain"}


def dispatch_chain(node_type: str, lead: str) -> str:
    """Lead lane + role-category fallback tail, comma-joined, order-preserving dedup."""
    tail = ""
    if node_type in _CODING_ROLES:
        tail = os.environ.get("MO_FALLBACK_CODING", "minimax,codex,sonnet")
    elif node_type in _REVIEW_ROLES:
        tail = os.environ.get("MO_FALLBACK_REVIEW", "opus,kimi,sonnet")
    if not tail:
        return lead
    seen = set()
    out = []
    for x in (lead + "," + tail).split(","):
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return ",".join(out)


def learning_static_lane(node_type: str, current_lane: str) -> str:
    frontier = os.environ.get("MO_FRONTIER_LANE", "opus_lens")
    cheap = os.environ.get("MO_CHEAP_LANE", "kimi_lens")
    # A recipe-pinned lane (current_lane != node_type) is explicit author intent
    # + the learning loop's exploration arm — keep it.
    if current_lane != node_type:
        return current_lane
    if node_type == "reviewer":
        return frontier
    if node_type in ("researcher", "implementer"):
        return cheap
    return current_lane


def finish_reason_for_failure(rc, text: str = "") -> str:
    rc = int(rc) if str(rc).lstrip("-").isdigit() else 1
    if rc == 124:
        return "timeout"
    if rc == 43 or "lane_fuse_open" in (text or ""):
        return "error"
    if "cost_circuit_open" in (text or ""):
        return "cost_limit"
    return "error"


def infer_trace_code_region(payload: str) -> str:
    """files_written → the top-level dir of the first in-repo relative file
    ('(root)' for root-level files). Verbatim transcription of the bash's
    embedded python; returns '' when nothing maps (bash prints nothing)."""
    try:
        data = json.loads(payload or "{}")
    except json.JSONDecodeError:
        return ""
    run_dir = os.environ.get("MINI_ORK_RUN_DIR") or os.environ.get("RUN_DIR") or ""
    roots = [os.environ.get("MO_TARGET_CWD") or "", os.environ.get("MINI_ORK_ROOT") or "", os.getcwd()]
    roots = [os.path.abspath(r) for r in roots if r]

    def _decode_files(value):
        if isinstance(value, list):
            return value
        if isinstance(value, str):
            s = value.strip()
            if not s:
                return []
            try:
                decoded = json.loads(s)
            except json.JSONDecodeError:
                return [s]
            return decoded if isinstance(decoded, list) else []
        return []

    def _relativize(path):
        if not isinstance(path, str):
            return None
        p = path.strip()
        if not p or "://" in p:
            return None
        if run_dir:
            run_abs = os.path.abspath(run_dir)
            p_abs = os.path.abspath(p) if os.path.isabs(p) else os.path.abspath(os.path.join(os.getcwd(), p))
            try:
                if os.path.commonpath([run_abs, p_abs]) == run_abs:
                    return None
            except ValueError:
                pass
        if os.path.isabs(p):
            p_abs = os.path.abspath(p)
            for root in roots:
                try:
                    if os.path.commonpath([root, p_abs]) == root:
                        return os.path.relpath(p_abs, root)
                except ValueError:
                    continue
            return None
        return p

    for raw in _decode_files(data.get("files_written")):
        rel = _relativize(raw)
        if not rel:
            continue
        rel = rel.replace("\\", "/")
        while rel.startswith("./"):
            rel = rel[2:]
        if not rel or rel.startswith("../"):
            continue
        return rel.split("/", 1)[0] if "/" in rel else "(root)"
    return ""


# ── GRPO learning writeback (verbatim transcription of the embedded python) ──

def learning_update_conductor_outcomes(db) -> int:
    """Resolve pending conductor_decisions against their epic's terminal status."""
    if not (db and os.path.isfile(db)):
        return 0
    con = sqlite3.connect(db, timeout=5.0)
    con.execute("PRAGMA busy_timeout=5000")
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            "SELECT cd.id, e.status FROM conductor_decisions cd JOIN epics e ON e.id = cd.epic_id "
            "WHERE COALESCE(cd.outcome, 'pending') = 'pending' AND e.status IN ('done', 'escalated')"
        ).fetchall()
        for row in rows:
            success = row["status"] == "done"
            con.execute("UPDATE conductor_decisions SET outcome=?, realized_score=? WHERE id=?",
                        ("success" if success else "failure", 1.0 if success else 0.0, row["id"]))
        con.commit()
        return len(rows)
    except sqlite3.OperationalError:
        return 0
    finally:
        con.close()


_FAMILY_TOKENS = ("opus", "minimax", "glm", "kimi")
_APPROVE = {"approve", "approved", "pass", "success", "ok"}
_REJECT = {"reject", "rejected", "fail", "failed", "request_changes", "needs_revision", "escalate"}
_VERDICT_BAND = 0.10


def write_grpo_advantages(db) -> int:
    """Compute per-(agent_version_id, task_class) group-relative advantage from
    execution_traces and UPSERT into agent_performance_memory. Verbatim port."""
    if not (db and os.path.isfile(db)):
        return 0
    con = sqlite3.connect(db, timeout=5.0)
    con.execute("PRAGMA busy_timeout=5000")
    con.row_factory = sqlite3.Row
    cols = {r[1] for r in con.execute("PRAGMA table_info(agent_performance_memory)").fetchall()}
    if "relative_advantage" not in cols:
        con.close()
        return 0

    try:
        decay_alpha = float(os.environ.get("MO_LEARNING_DECAY_ALPHA", "0.30"))
    except ValueError:
        decay_alpha = 0.30
    decay_alpha = max(0.0, min(1.0, decay_alpha))
    try:
        halflife_days = float(os.environ.get("MO_LEARNING_HALFLIFE_DAYS", "14"))
    except ValueError:
        halflife_days = 14.0
    if halflife_days < 0.0:
        halflife_days = 0.0

    def _parse_iso(ts):
        if not ts:
            return None
        s = ts.strip()
        if not s:
            return None
        if s.endswith("Z"):
            s = s[:-1]
        try:
            return datetime.datetime.fromisoformat(s)
        except ValueError:
            return None

    _now = datetime.datetime.now(datetime.timezone.utc)
    _ln2 = math.log(2.0)

    def recency_weight(created_at):
        if halflife_days <= 0.0:
            return 1.0
        dt = _parse_iso(created_at)
        if dt is None:
            return 1.0
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        age_days = (_now - dt).total_seconds() / 86400.0
        if age_days <= 0.0:
            return 1.0
        return math.exp(-_ln2 * age_days / halflife_days)

    try:
        rows = con.execute(
            "SELECT trace_id, task_class, agent_version_id, verifier_output, status, "
            "reviewer_verdict, cost_usd, duration_ms, process_reward, created_at "
            "FROM execution_traces WHERE task_class IS NOT NULL AND task_class <> '' "
            "AND agent_version_id IS NOT NULL AND agent_version_id <> ''").fetchall()
    except sqlite3.OperationalError:
        con.close()
        return 0

    def _decode_verifier_output(raw):
        if isinstance(raw, dict):
            return raw
        if not isinstance(raw, str):
            return {}
        s = raw.strip()
        if not s or s in ("null", "None", "{}"):
            return {}
        try:
            decoded = json.loads(s)
        except (ValueError, TypeError):
            return {}
        if isinstance(decoded, dict):
            return decoded
        if isinstance(decoded, str):
            try:
                redecoded = json.loads(decoded)
            except (ValueError, TypeError):
                return {}
            return redecoded if isinstance(redecoded, dict) else {}
        return {}

    def node_type(row):
        payload = _decode_verifier_output(row["verifier_output"])
        value = payload.get("node_type") if isinstance(payload, dict) else None
        if value:
            return str(value)
        tc = row["task_class"] or ""
        return str(tc) if tc else "_unknown"

    def _lane_family(agent_version_id):
        if not agent_version_id:
            return None
        av = str(agent_version_id).lower()
        for tok in _FAMILY_TOKENS:
            if tok in av:
                return tok
        return None

    def reward(row):
        if row["process_reward"] is not None:
            return max(0.0, min(1.0, float(row["process_reward"])))
        verdict = (row["reviewer_verdict"] or "").lower()
        status = (row["status"] or "").lower()
        same_family = _lane_family(row["agent_version_id"]) is not None
        if status not in {"success", "failed"}:
            if verdict in _APPROVE:
                return 1.0
            if verdict in _REJECT:
                return 0.0
            return 1.0 if row["status"] == "success" else 0.0
        base = 0.85 if status == "success" else 0.15
        if same_family:
            delta = 0.0
        elif verdict in _APPROVE:
            delta = _VERDICT_BAND
        elif verdict in _REJECT:
            delta = -_VERDICT_BAND
        else:
            delta = 0.0
        return max(0.0, min(1.0, base + delta))

    weight_rows = [(row, reward(row), recency_weight(row["created_at"])) for row in rows]
    groups = {}
    for row, score, w in weight_rows:
        groups.setdefault((node_type(row), row["task_class"]), []).append((row, score, w))

    existing_adv = {}
    for er in con.execute("SELECT agent_version_id, task_class, relative_advantage "
                          "FROM agent_performance_memory").fetchall():
        try:
            existing_adv[(er["agent_version_id"], er["task_class"])] = float(er["relative_advantage"])
        except (TypeError, ValueError):
            pass

    written = 0
    for (role, task_class), items in groups.items():
        total_w = sum(w for _, _, w in items)
        if total_w > 0.0:
            mean = sum(w * s for _, s, w in items) / total_w
            variance = sum(w * (s - mean) ** 2 for _, s, w in items) / total_w
        else:
            scores = [s for _, s, _ in items]
            mean = sum(scores) / len(scores)
            variance = sum((s - mean) ** 2 for s in scores) / len(scores)
        std = math.sqrt(variance)
        try:
            tiebreak_enabled = int(os.environ.get("MO_LEARNING_TIEBREAK", "1")) != 0
        except ValueError:
            tiebreak_enabled = True
        if std == 0 and tiebreak_enabled:
            costs = sorted({round(float(r["cost_usd"] or 0.0), 6) for r, _, _ in items})
            cost_min, cost_max = (costs[0], costs[-1]) if costs else (0.0, 0.0)
            cost_span = cost_max - cost_min
        else:
            cost_min = cost_max = cost_span = 0.0
            if std == 0:
                tiebreak_enabled = False
        by_agent = {}
        for row, score, w in items:
            bucket = by_agent.setdefault(row["agent_version_id"], {
                "runs": 0, "success": 0, "cost": 0.0, "duration": 0.0, "adv": [], "w": []})
            bucket["runs"] += 1
            bucket["success"] += 1 if row["status"] == "success" else 0
            bucket["cost"] += float(row["cost_usd"] or 0.0)
            bucket["duration"] += float(row["duration_ms"] or 0.0)
            if std == 0:
                if tiebreak_enabled and cost_span > 0:
                    c = float(row["cost_usd"] or 0.0)
                    adv_tb = round(0.1 * (cost_max - c) / cost_span * 2.0 - 0.1, 6)
                    adv_tb = max(-0.1, min(0.1, adv_tb))
                    bucket["adv"].append(adv_tb)
                else:
                    bucket["adv"].append(0.0)
            else:
                bucket["adv"].append((score - mean) / std)
            bucket["w"].append(w)
        try:
            shrink_k = max(0, int(os.environ.get("MO_LEARNING_SHRINKAGE_K", "5")))
        except ValueError:
            shrink_k = 5
        for agent_id, bucket in by_agent.items():
            runs = bucket["runs"]
            bw_total = sum(bucket["w"])
            if bw_total > 0.0:
                raw_rel_adv = sum(w * a for w, a in zip(bucket["w"], bucket["adv"])) / bw_total
            else:
                raw_rel_adv = sum(bucket["adv"]) / len(bucket["adv"])
            shrink_factor = runs / (runs + shrink_k) if (runs + shrink_k) > 0 else 0.0
            batch_adv = raw_rel_adv * shrink_factor
            prior = existing_adv.get((agent_id, task_class))
            if prior is not None and decay_alpha < 1.0:
                rel_adv = decay_alpha * batch_adv + (1.0 - decay_alpha) * prior
            else:
                rel_adv = batch_adv
            con.execute(
                "INSERT INTO agent_performance_memory (agent_version_id, role, model, task_class, "
                "runs_count, success_count, avg_cost_usd, avg_duration_ms, top_failure_modes, "
                "relative_advantage, last_updated) VALUES (?,?,?,?,?,?,?,?,'[]',?,"
                "strftime('%Y-%m-%dT%H:%M:%fZ','now')) "
                "ON CONFLICT(agent_version_id, task_class) DO UPDATE SET role=excluded.role, "
                "model=excluded.model, runs_count=excluded.runs_count, "
                "success_count=excluded.success_count, avg_cost_usd=excluded.avg_cost_usd, "
                "avg_duration_ms=excluded.avg_duration_ms, "
                "relative_advantage=excluded.relative_advantage, last_updated=excluded.last_updated",
                (agent_id, role, agent_id, task_class, runs, bucket["success"],
                 bucket["cost"] / runs, bucket["duration"] / runs, round(rel_adv, 6)))
            written += 1
    con.commit()
    con.close()
    return written
