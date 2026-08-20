"""GRPO learning writeback and reward anchoring (extracted from cli/execute.py).

Owns the reward contract (status-anchored, reviewer-veto-only) and the
group-relative-advantage writeback into agent_performance_memory. Pure DB +
env-knob logic; no CLI, no dispatch. Re-exported from mini_ork.cli.execute
for backward compatibility.
"""
from __future__ import annotations

import datetime
import json
import math
import os
import sqlite3

# Anti-Goodhart reward anchor (per arXiv 2601.18533 chain-veto semantics):
# execution status is the PRIMARY (verified) anchor; the reviewer verdict can
# only VETO (downgrade), never fabricate a positive. The prior implementation
# was judge-anchored — verdict checked first, status only as fallback — which
# let a self-improving loop learn to GAME the reviewer instead of writing
# correct code. This rewrite makes the loop reward *verified execution*, with
# the reviewer as a one-way downgrade gate.
_REWARD_SUCCESS_STATUSES = ("success", "published", "done", "pass")
_REWARD_FAILURE_STATUSES = ("failure", "failed", "rolled_back", "blocked",
                            "crash", "escalated", "reject")
_REWARD_VETO_VERDICTS = ("reject", "needs_revision", "request_changes",
                         "escalate")
_REWARD_APPROVE_VERDICTS = ("approve", "approved", "pass", "passed",
                            "success", "ok")


def reward_from_status(status: str = "", verdict: str = "") -> str:
    s = (status or "").lower()
    v = (verdict or "").lower()
    if s in _REWARD_FAILURE_STATUSES:
        return "0.0"
    if s in _REWARD_SUCCESS_STATUSES:
        return "0.0" if v in _REWARD_VETO_VERDICTS else "1.0"
    if s == "":
        return "1.0" if v in _REWARD_APPROVE_VERDICTS else "0.5"
    return "0.5"



# ── GRPO learning writeback (verbatim transcription of the embedded python) ──

def learning_update_conductor_outcomes(db) -> int:
    """Write back what the conductor ACTUALLY got, so its predictions become falsifiable.

    THE BUG THIS FIXES. This function used to reconcile only against an EPIC's terminal
    status. But `bin/mini-ork run` completes a TASK_RUN and does not necessarily advance any
    epic — on the live db every conductor decision pointed at an epic still marked
    `not started`, so the join matched nothing and `realized_score` was NULL on 10 of 10 rows.
    The conductor predicted a score every single time and never once learned whether it was
    right. Uncalibrated by construction.

    A prediction nobody scores is not a prediction, it is a claim. This closes that.

    Two reconciliation paths now, in priority order:

      1. task_run_id (migration 0050) — the run the decision actually produced. This is the
         path that fires for run-driven work, i.e. essentially all of it.
      2. epic_id — the original path, kept for epic-driven work.

    `verdict` is preferred over `status` because it is the *judged* outcome; `status` merely
    says the process finished. A run that completes while failing its verifiers is a failure,
    and scoring it 1.0 because it exited cleanly is precisely the false-completion this whole
    system exists to prevent.
    """
    if not (db and os.path.isfile(db)):
        return 0
    con = sqlite3.connect(db, timeout=5.0)
    con.execute("PRAGMA busy_timeout=5000")
    con.row_factory = sqlite3.Row
    updated = 0
    try:
        has_run_col = any(
            r["name"] == "task_run_id"
            for r in con.execute("PRAGMA table_info(conductor_decisions)").fetchall()
        )

        # ── path 1: the run the decision produced (migration 0050) ──────────────
        #
        # GROUND TRUTH, read off the live schema and data — not assumed:
        #
        #   task_runs.status CHECK IN ('classified','planned','executing','verifying',
        #                              'reviewing','published','rolled_back','failed')
        #
        # So the terminal success state is `published`, NOT `done`. An earlier version of this
        # function checked `status in ('done','success','pass')` — values that CANNOT occur —
        # and its unit tests passed only because they used the same invented status. Tests that
        # encode the author's assumption instead of the system's schema prove nothing.
        #
        # And `verdict` is NOT a reliable signal: on the live db it is EMPTY on 242 of 278
        # completed runs; the only value ever observed is 'CRASH'. So verdict is used only as
        # a negative override, never as the positive one.
        #
        # `ended_at IS NOT NULL` is also NOT a terminal test — 8 rows sit in `reviewing` with
        # ended_at set. Gate on the status set, which is what actually means "finished".
        if has_run_col:
            rows = con.execute(
                "SELECT cd.id, tr.status, tr.verdict "
                "FROM conductor_decisions cd JOIN task_runs tr ON tr.id = cd.task_run_id "
                "WHERE COALESCE(cd.outcome, 'pending') = 'pending' "
                "  AND tr.status IN ('published', 'failed', 'rolled_back')"
            ).fetchall()
            for row in rows:
                status = (row["status"] or "").strip().lower()
                verdict = (row["verdict"] or "").strip().lower()
                # Published AND not crashed = the only way to score 1.0. A crash is a failure
                # no matter what the status column says.
                success = status == "published" and verdict != "crash"
                con.execute(
                    "UPDATE conductor_decisions SET outcome=?, realized_score=? WHERE id=?",
                    ("success" if success else "failure", 1.0 if success else 0.0, row["id"]),
                )
                updated += 1

        # ── path 2: epic-driven work (the original path) ────────────────────────
        rows = con.execute(
            "SELECT cd.id, e.status FROM conductor_decisions cd JOIN epics e ON e.id = cd.epic_id "
            "WHERE COALESCE(cd.outcome, 'pending') = 'pending' AND e.status IN ('done', 'escalated')"
        ).fetchall()
        for row in rows:
            success = row["status"] == "done"
            con.execute("UPDATE conductor_decisions SET outcome=?, realized_score=? WHERE id=?",
                        ("success" if success else "failure", 1.0 if success else 0.0, row["id"]))
            updated += 1

        con.commit()
        return updated
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
            "reviewer_verdict, cost_usd, duration_ms, process_reward, created_at, "
            "COALESCE(validity, 'valid') AS validity "
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

    def _row_is_valid(row):
        # Reward hygiene: rows stamped non-'valid' are INFRA exits (timeout /
        # cost-circuit) the agent never controlled. Drop them ENTIRELY — not
        # zero-weight — so they perturb neither the group mean/variance nor the
        # cost-span tie-break. NULL/empty (legacy rows predating the stamp) count
        # as valid so the historical corpus stays in the learning signal.
        v = row["validity"]
        return not v or str(v).strip().lower() == "valid"

    weight_rows = [(row, reward(row), recency_weight(row["created_at"]))
                   for row in rows if _row_is_valid(row)]
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


