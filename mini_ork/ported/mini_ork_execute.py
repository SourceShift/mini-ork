"""Python port of bin/mini-ork-execute — the executor (INCREMENTAL).

bin/mini-ork-execute is the 3449-line node-dispatch engine. This port covers it
in increments: deterministic helpers + GRPO writeback + orchestration backbone
(NODE_IDS + --dry-run, all live-bash parity-gated) + the live per-node routing
(dispatch_node), whose ONE LLM call is an injectable seam (dispatch_fn) — the live
path is functionally verified (fake dispatch → ported-helper wiring), not
bash-parity, since real model output can't be parity-tested. The default flip
(bash→Python runtime) + a real-LLM integration harness remain.

Ported here (all pure / transcribed verbatim):
    reward_from_status(status, verdict)     — status/verdict → GRPO reward
    dispatch_chain(node_type, lead)         — role-aware fallback lane chain (deduped)
    learning_static_lane(node_type, lane)   — static lane synthesis for unpinned nodes
    finish_reason_for_failure(rc, text)     — rc/text → finish reason
    infer_trace_code_region(payload)        — files_written → top-level code region
    learning_update_conductor_outcomes(db)  — resolve pending conductor decisions
    write_grpo_advantages(db)               — GRPO group-relative advantage writeback
    set_status / charge_node_cost           — per-node DB status + cost writes
    apply_impl_output                       — 'capture coin-flip' diff/fenced-block applier
    dispatch_node(...)                      — LIVE per-node routing (LLM = seam)
    main(..., dispatch_fn=)                 — full run: dry-run OR live per-node dispatch
"""
from __future__ import annotations

import datetime
import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid

_SEP = "\x1f"
_NODE_TYPE_ORDER = ("planner", "researcher", "implementer", "reviewer", "verifier",
                    "reflector", "publisher", "rollback")


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


def learning_governed_lane(node_type: str, current_lane: str, *, root=None) -> str:
    """Port of bash `_mo_learning_governed_lane`: delegate the routing read to the
    canonical NATIVE `decide()` in ``mini_ork.ported.decision_service`` — the same
    brain every consumer uses. No state DB → static fallback (decide can't consult
    GRPO tables). Byte-parity with bash `decide` (verified deterministic, EPSILON=0);
    the .route field carries the lane, empty falls back to current_lane.

    2026-07-18: rewired from `bash -c 'source decision_service.sh; decide'` to the
    in-process native port — routing no longer shells out per dispatch."""
    db = os.environ.get("MINI_ORK_DB", "")
    if not db or not os.path.isfile(db):
        return learning_static_lane(node_type, current_lane)
    task_class = os.environ.get("TASK_CLASS") or os.environ.get("MINI_ORK_TASK_CLASS") or "generic"
    objective_domain = (os.environ.get("MINI_ORK_OBJECTIVE_DOMAIN")
                        or os.environ.get("MO_OBJECTIVE_DOMAIN") or "code-delivery")
    try:
        from mini_ork.ported import decision_service
        route = decision_service.decide(
            node_type, task_class, objective_domain, db=db).get("route", "")
        return route or current_lane
    except Exception:
        return current_lane


def policy_route_lane(node_type: str, current_lane: str, *, dry_run=False, root=None) -> str:
    """Port of bash `_mo_policy_route_lane`. Applied to every live node BEFORE dispatch
    so the routed lane (not the raw node_type/workflow lane) reaches --node-type. Dry-run
    preserves the recipe's explicit lane (workflow-shape preview, not a policy preview)."""
    if dry_run:
        return current_lane
    policy = os.environ.get("MO_ROUTING_POLICY") or "learning_governed"
    frontier = os.environ.get("MO_FRONTIER_LANE", "opus_lens")
    cheap = os.environ.get("MO_CHEAP_LANE", "kimi_lens")
    llm_types = ("researcher", "implementer", "reviewer")
    if policy in ("", "workflow_default"):
        return current_lane
    if policy == "frontier_only":
        return frontier if node_type in llm_types else current_lane
    if policy == "cheap_only":
        return cheap if node_type in llm_types else current_lane
    if policy == "static_hybrid":
        return learning_static_lane(node_type, current_lane)
    if policy == "learning_governed":
        # Router-monoculture fix: a recipe-pinned lane (current_lane != node_type) is a
        # deliberate author choice — cross-family panel diversity (glm/kimi/codex/opus
        # lenses) or a model-strength pin. The governed router must NOT override it with
        # the single global-slice winner: that collapses every same-node-type panel node
        # (4 researchers) onto ONE lane, destroying the diversity the recipe designed.
        # Learning governs only UNPINNED nodes (current_lane == node_type); pinned nodes
        # keep their lane — consistent with learning_static_lane's pin-preservation.
        if current_lane != node_type:
            return current_lane
        return learning_governed_lane(node_type, learning_static_lane(node_type, current_lane), root=root)
    if policy == "trace_governed":
        fail_count = int(os.environ.get("FAIL_COUNT", "0") or "0")
        if node_type == "reviewer":
            return frontier
        if node_type in ("researcher", "implementer"):
            return frontier if fail_count > 0 else cheap
        return current_lane
    sys.stderr.write(f"  [warn] unknown MO_ROUTING_POLICY={policy} — using workflow lane {current_lane}\n")
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


def _target_repo_changed_files() -> list[str]:
    """Repo-relative paths git sees as changed in the TARGET repo, so an
    implementer trace's code_region reflects the edited source — not the
    .mini-ork run-log path passed as output_file (which relativizes to
    '.mini-ork' when MINI_ORK_RUN_DIR is unset). Covers unstaged tracked
    edits (`git diff --name-only`) plus new untracked files (`ls-files
    --others --exclude-standard`, which honours .gitignore so .mini-ork/runs
    artifacts never leak in). Best-effort: any git failure yields [] and the
    caller falls back to the impl.log path (prior behaviour)."""
    target = os.environ.get("MO_TARGET_CWD") or ""
    if not target or not os.path.isdir(target):
        return []
    files: list[str] = []
    for args in (["diff", "--name-only"], ["ls-files", "--others", "--exclude-standard"]):
        try:
            r = subprocess.run(["git", "-C", target, *args],
                               capture_output=True, text=True, timeout=10)
        except Exception:
            continue
        if r.returncode != 0:
            continue
        for line in r.stdout.splitlines():
            p = line.strip()
            if p and p not in files:
                files.append(p)
    return files


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


# ── orchestration backbone (NODE_IDS assembly + DAG loop + dry-run) ──
#
# The live per-node LLM execution (_dispatch_node's non-dry-run branches) is the
# remaining integration-gated increment; main() below fully ports the
# deterministic orchestration — node assembly, dispatch-mode routing, the
# dry-run dispatch plan, verdict.json + status — all parity-gated against the
# live bash --dry-run. A live dispatch raises NotImplementedError unless a
# dispatch_fn seam is supplied.

def nodes_from_workflow(wf_path: str) -> list[str]:
    """workflow.yaml → NODE_IDS tuples (8 SEP-joined fields). Verbatim."""
    import yaml
    with open(wf_path) as f:
        wf = yaml.safe_load(f) or {}
    out = []
    for n in wf.get("nodes", []) or []:
        name = n.get("name", "")
        typ = n.get("type", "")
        desc = n.get("description", "") or name
        pref = n.get("prompt_ref", "") or ""
        dmode = n.get("dispatch_mode", "") or "serial"
        vref = n.get("verifier_ref", "") or ""
        mlane = n.get("model_lane", "") or typ
        requires = n.get("requires_capabilities", []) or []
        requires_csv = requires if isinstance(requires, str) else ",".join(str(x) for x in requires)
        if not name or not typ:
            continue
        desc = desc.replace(_SEP, " ")
        pref = pref.replace(_SEP, " ")
        vref = vref.replace(_SEP, " ")
        mlane = mlane.replace(_SEP, " ")
        requires_csv = requires_csv.replace(_SEP, " ")
        out.append(_SEP.join([name, typ, desc, pref, dmode, vref, mlane, requires_csv]))
    return out


def nodes_from_plan(plan_path: str, wf_path: str = "") -> list[str]:
    """plan.json.decomposition (+ optional workflow.yaml lane/prompt lift) → NODE_IDS. Verbatim."""
    try:
        import yaml
    except ImportError:
        yaml = None
    with open(plan_path) as f:
        p = json.load(f)
    wf_by_name = {}
    if wf_path and yaml is not None and os.path.isfile(wf_path):
        try:
            with open(wf_path) as wf:
                wf_data = yaml.safe_load(wf) or {}
            for node in (wf_data.get("nodes") or []):
                name = str(node.get("name") or "")
                if not name:
                    continue
                wf_by_name[name] = {
                    "model_lane": str(node.get("model_lane") or "") or None,
                    "prompt_ref": str(node.get("prompt_ref") or "") or None,
                    "verifier_ref": str(node.get("verifier_ref") or "") or None,
                    "dispatch_mode": str(node.get("dispatch_mode") or "serial")}
        except Exception:
            wf_by_name = {}

    def _wf_lookup(nid):
        if nid in wf_by_name:
            return wf_by_name[nid]
        u = nid.replace("-", "_")
        if u in wf_by_name:
            return wf_by_name[u]
        d = nid.replace("_", "-")
        if d in wf_by_name:
            return wf_by_name[d]
        return None

    out = []
    for step in p.get("decomposition", []):
        nid = step.get("id", "")
        ntyp = step.get("node_type") or "implementer"
        if not nid or not ntyp:
            continue
        desc = (step.get("description", "") or "").replace(_SEP, " ")
        wf = _wf_lookup(nid) or {}
        model_lane = step.get("model_lane") or (wf.get("model_lane") or ntyp)
        prompt_ref = step.get("prompt_ref") or wf.get("prompt_ref") or ""
        verifier_ref = step.get("verifier_ref") or wf.get("verifier_ref") or ""
        dispatch_mode = step.get("dispatch_mode") or wf.get("dispatch_mode") or "serial"
        out.append(_SEP.join([nid, ntyp, desc, prompt_ref, dispatch_mode, verifier_ref, model_lane, ""]))
    return out


def _dry_dispatch_node(fields, filter_node_type, fail_count, out):
    """The dry-run branch of _dispatch_node: gates + the plan line. Appends to
    `out`. Returns whether it counted as dispatched (for the plan line count)."""
    node_id, node_type, node_desc, model_lane = fields[0], fields[1], fields[2], fields[6]
    if filter_node_type and node_type != filter_node_type:
        return
    if node_type == "rollback" and fail_count == 0:
        out.append("  [skip] rollback — no failures (escalates_to edge not triggered)")
        return
    # dry-run: _mo_policy_route_lane returns current_lane unchanged
    out.append(f"[dry-run] would dispatch node_id={node_id} node_type={node_type} "
               f"model_lane={model_lane}: {node_desc}")


def _resolve_dispatch_mode(override, wf_path) -> str:
    if override:
        return override
    if wf_path and os.path.isfile(wf_path):
        try:
            import yaml
            return (yaml.safe_load(open(wf_path)) or {}).get("dispatch_mode") or "serial"
        except Exception:
            return "serial"
    return "serial"


def _emit_run_verdict(run_dir, fail_count, dispatched):
    if not (run_dir and os.path.isdir(run_dir)):
        return
    if os.path.isfile(os.path.join(run_dir, "verdict.json")) or \
            os.path.isfile(os.path.join(run_dir, "panel-verdict.json")):
        return
    verdict = "fail" if fail_count > 0 else "pass"
    try:
        open(os.path.join(run_dir, "verdict.json"), "w").write(
            '{"verdict":"%s","failed_nodes":%d,"dispatched":%d,"source":"execute@run-level"}\n'
            % (verdict, fail_count, dispatched))
    except OSError:
        return
    print(f"  [verdict] run-level verdict.json: {verdict} (failed_nodes={fail_count})")


def main(argv=None, *, root=None, dispatch_fn=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    root = root or os.environ.get("MINI_ORK_ROOT") or os.getcwd()
    os.environ["MINI_ORK_ROOT"] = root

    dry_run = os.environ.get("MINI_ORK_DRY_RUN", "0") == "1"
    filter_node_type = ""
    dispatch_mode_override = ""
    plan_path = os.environ.get("MINI_ORK_PLAN_PATH", "")
    from_node = ""
    recovery_active = False
    repair_budget = ""
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("--help", "-h"):
            sys.stdout.write(
                "Usage: mini-ork execute [<plan.json>] [--node-type <type>] "
                "[--dispatch-mode <mode>] [--dry-run]\n"
                "                       [--from-node <id>] [--recovery] [--repair-budget <usd>]\n\n"
                "Dispatch plan steps to node-type handlers.\n\n"
                "Node types: planner | researcher | implementer | reviewer | verifier |\n"
                "            reflector | publisher | rollback\n\n"
                "Dispatch modes: serial | parallel | partitioned | speculative\n\n"
                "Options:\n"
                "  --node-type <type>        Execute only nodes of this type (filter)\n"
                "  --dispatch-mode <mode>    Override workflow dispatch mode\n"
                "  --dry-run                 Print what would be dispatched; no LLM calls\n"
                "  --from-node <id>          Enter the loop at this node (recovery)\n"
                "  --recovery                Same as --from-node + closure filter\n"
                "                            (set by `mini-ork recover`; honors\n"
                "                            MINI_ORK_RECOVERY_CLOSURE env var)\n"
                "  --repair-budget <usd>     Bound the recovery cost ceiling\n"
                "                            (strategy=repair). Without it, the\n"
                "                            default is $5.00 (env MO_REPAIR_BUDGET_USD)\n"
                "  --help                    Show this help\n")
            return 0
        elif a == "--dry-run":
            dry_run = True; i += 1
        elif a == "--node-type":
            filter_node_type = argv[i + 1]; i += 2
        elif a == "--dispatch-mode":
            dispatch_mode_override = argv[i + 1]; i += 2
        elif a == "--from-node":
            if i + 1 >= len(argv):
                sys.stderr.write("--from-node requires <id>\n"); return 2
            from_node = argv[i + 1]; i += 2
        elif a.startswith("--from-node="):
            from_node = a.split("=", 1)[1].strip(); i += 1
        elif a == "--recovery":
            recovery_active = True; i += 1
        elif a == "--repair-budget":
            if i + 1 >= len(argv):
                sys.stderr.write("--repair-budget requires <usd>\n"); return 2
            repair_budget = argv[i + 1]; i += 2
        elif a.startswith("--repair-budget="):
            repair_budget = a.split("=", 1)[1].strip(); i += 1
        elif a.startswith("-"):
            sys.stderr.write(f"Unknown flag: {a}. Try --help\n"); return 2
        else:
            if not plan_path:
                plan_path = a; i += 1
            else:
                sys.stderr.write(f"Unexpected argument: {a}\n"); return 2

    # Resolve plan path (bash :957-973): empty → newest plan.json in $MINI_ORK_HOME/runs,
    # then REQUIRE it. A missing or nonexistent plan must exit 2 with a message, not a
    # Python traceback (nodes_from_plan would open('') / a bad path). bash requires a
    # plan even in workflow mode (it's used for run_dir / task_run_id / plan_content).
    home = os.environ.get("MINI_ORK_HOME") or os.path.join(os.getcwd(), ".mini-ork")
    if not plan_path:
        newest, newest_mtime = "", -1.0
        for dirpath, _dirs, files in os.walk(os.path.join(home, "runs")):
            if "plan.json" in files:
                p = os.path.join(dirpath, "plan.json")
                try:
                    m = os.path.getmtime(p)
                except OSError:
                    continue
                if m > newest_mtime:
                    newest, newest_mtime = p, m
        plan_path = newest
    # E2 recovery: a from-workflow recovery derives node_ids from MINI_ORK_WORKFLOW
    # and its run_dir from MINI_ORK_RUN_DIR, so it does NOT require a plan.json — the
    # original plan may be gone, or recovery may be driven purely from the workflow +
    # E1 checkpoints. Only require a plan for a normal, non-recovery run.
    _wf_env = os.environ.get("MINI_ORK_WORKFLOW", "")
    _recovery_ctx = bool(
        from_node
        or os.environ.get("MINI_ORK_RECOVERY_CLOSURE", "").strip()
        or os.environ.get("MINI_ORK_RECOVERY_FROM", "").strip()
        or recovery_active
    )
    _recovery_no_plan = (
        not plan_path and _recovery_ctx and bool(_wf_env) and os.path.isfile(_wf_env)
    )
    if not plan_path and not _recovery_no_plan:
        sys.stderr.write("No plan.json found. Run: mini-ork plan <kickoff.md>\n")
        return 2
    if plan_path and not os.path.isfile(plan_path):
        sys.stderr.write(f"plan not found: {plan_path}\n")
        return 2

    workflow = os.environ.get("MINI_ORK_WORKFLOW", "")
    if not workflow and os.environ.get("MINI_ORK_RECIPE"):
        workflow = os.path.join(root, "recipes", os.environ["MINI_ORK_RECIPE"], "workflow.yaml")
    run_dir = (os.path.dirname(plan_path) if plan_path
               else (os.environ.get("MINI_ORK_RUN_DIR") or "."))

    # Pre-dispatch execute gate (bash :1136-1203): refuse to dispatch a
    # needs_answers plan (exit 6). Needs a plan.json; a from-workflow recovery
    # has none → nothing to gate on, so skip it.
    if plan_path and _execute_gate_check(plan_path, run_dir, dry_run):
        return 6

    # NODE_IDS: workflow.yaml source wins; else plan.json.decomposition.
    if workflow and os.path.isfile(workflow):
        node_source = "workflow.yaml"
        node_ids = nodes_from_workflow(workflow)
    else:
        node_source = "plan.json.decomposition"
        node_ids = nodes_from_plan(plan_path, workflow)
    print(f"    nodes:    {len(node_ids)} (from {node_source})")

    # ── E2 recovery-context filter ──────────────────────────────────────────
    # If `mini-ork recover` set MINI_ORK_RECOVERY_FROM (the closure root)
    # and MINI_ORK_RECOVERY_CLOSURE (the closure node_ids), restrict the
    # dispatch set to ONLY those nodes. Ancestors of the closure root are
    # SKIPPED entirely — they have valid E1 checkpoints (the planner
    # verified each one via is_node_reusable before emitting the env),
    # so dispatching them again would burn LLM calls for no reason.
    # CLI flags (`--from-node`, `--recovery`) take precedence over the
    # env vars; both forms produce the same filter shape.
    closure_env = os.environ.get("MINI_ORK_RECOVERY_CLOSURE", "").strip()
    closure_from_env = os.environ.get("MINI_ORK_RECOVERY_FROM", "").strip()
    if recovery_active and not closure_env and not closure_from_env and not from_node:
        # Operator passed --recovery with no plan context: refuse rather
        # than silently run the whole DAG. This is the "drop into recovery
        # mode but the planner hasn't computed a plan" footgun.
        sys.stderr.write(
            "execute: --recovery requires MINI_ORK_RECOVERY_FROM or "
            "--from-node (use `mini-ork recover <run_id>` to compute the plan)\n"
        )
        return 2
    effective_from = from_node or closure_from_env
    effective_closure = (
        set(closure_env.split()) if closure_env else set()
    )
    if effective_from or effective_closure:
        # Repair-budget wiring: surface the budget as MO_REPAIR_BUDGET_USD
        # so the cost_pause seam (or any future cost-aware router) can
        # honor it without depending on a new env contract.
        if repair_budget:
            try:
                v = float(repair_budget)
                if v > 0:
                    os.environ["MO_REPAIR_BUDGET_USD"] = f"{v:.2f}"
            except ValueError:
                sys.stderr.write(
                    f"execute: --repair-budget must be a positive number, got {repair_budget!r}\n"
                )
                return 2
        if not effective_closure and effective_from:
            # Operator override only (--from-node, no closure set): trust
            # the operator and include every node downstream of from_node.
            # Use the planner's DAG loader so the semantics stay identical
            # to `mini-ork recover` (edges, escalates_to exclusion).
            from mini_ork.ported.recovery_planner import load_dag
            dag = load_dag(workflow)
            effective_closure = dag.descendants(effective_from)
        # Track the closure set BEFORE mutating node_ids; the original
        # ``node_ids`` list is SEP-joined strings (the format
        # ``_resolve_dispatch_mode`` and the dispatch-loop below both
        # consume). We keep ``node_ids`` as strings and filter by name
        # here so the downstream ``fields_list = [... e.split(...) ...]``
        # keeps working unchanged.
        before_count = len(node_ids)
        node_ids = [
            e for e in node_ids
            if e.split(_SEP, 1)[0] in effective_closure
        ]
        # Mark the run as a recovery dispatch so downstream trace / cost
        # seams can stamp the metadata without re-deriving the closure.
        os.environ["MINI_ORK_RECOVERY_ACTIVE"] = "1"
        if effective_from:
            os.environ["MINI_ORK_RECOVERY_FROM"] = effective_from
        print(
            f"    recovery: from_node={effective_from or '<unset>'} "
            f"closure={len(node_ids)}/{before_count} nodes"
        )

    dispatch_mode = _resolve_dispatch_mode(dispatch_mode_override, workflow)
    fields_list = [tuple((e.split(_SEP) + [""] * 8)[:8]) for e in node_ids]

    fail_count = 0
    out: list[str] = []
    if dry_run:
        # partitioned reorders by node_type group; others keep NODE_IDS order.
        if dispatch_mode == "partitioned":
            ordered = [f for nt in _NODE_TYPE_ORDER for f in fields_list if f[1] == nt]
        else:
            ordered = fields_list
        for f in ordered:
            _dry_dispatch_node(f, filter_node_type, fail_count, out)
        for line in out:
            print(line)
        dispatched = sum(1 for line in out if line.startswith("[dry-run] would dispatch"))
        _emit_run_verdict(run_dir, fail_count, dispatched)
        print("")
        print("execute: all nodes complete")
        return 0

    # ── live per-node execution ──
    # dispatch_fn is the LLM seam (task_class, node_type, prompt) -> (rc, text);
    # defaults to the ported llm_dispatch. dispatch_node wires the ported helpers
    # (apply_impl_output, charge_node_cost, set_status, verdict gate) around it.
    task_class = os.environ.get("MINI_ORK_TASK_CLASS", "generic")
    db = os.environ.get("MINI_ORK_DB") or os.path.join(
        os.environ.get("MINI_ORK_HOME", ".mini-ork"), "state.db")
    run_id = os.environ.get("MINI_ORK_RUN_ID", "")
    recipe = os.environ.get("MINI_ORK_RECIPE", "")
    live_run_dir = os.environ.get("MINI_ORK_RUN_DIR") or run_dir
    llm = dispatch_fn or _default_llm_dispatch(root)
    # F3: without a trace_fn the live path writes zero execution_traces rows and the
    # GRPO/reflect learning loop is inert. Wire the real writer (reward-stamped rows).
    trace_writer = _make_trace_fn(task_class, db, run_id)
    # F4 (durable-dag E1): parallel writer that publishes node_checkpoints
    # rows at every node success. The runtime seam (trace wrapper inside
    # dispatch_node) calls BOTH; absence of a node_checkpoints row after
    # a success means the writer failed best-effort and the runtime will
    # treat the node as not-reusable on the next attempt (design §4).
    checkpoint_writer = _make_checkpoint_fn(db, run_id, live_run_dir, recipe, task_class)
    set_status(db, run_id, "executing")
    ordered = ([f for nt in _NODE_TYPE_ORDER for f in fields_list if f[1] == nt]
               if dispatch_mode == "partitioned" else fields_list)
    for f in ordered:
        if filter_node_type and f[1] != filter_node_type:
            continue
        if f[1] == "rollback" and fail_count == 0:
            print("  [skip] rollback — no failures (escalates_to edge not triggered)")
            continue
        # D1: bash keeps FAIL_COUNT as a shell var visible to _mo_policy_route_lane's
        # trace_governed branch (:2014). Export it so the port's policy_route_lane sees
        # the live prefix-failure count (else trace_governed never escalates).
        os.environ["FAIL_COUNT"] = str(fail_count)
        rc, _fr = dispatch_node(f, root=root, run_dir=live_run_dir, plan_path=plan_path,
                                task_class=task_class, db=db, run_id=run_id,
                                dispatch_fn=llm, recipe=recipe, workflow=workflow,
                                trace_fn=trace_writer, checkpoint_fn=checkpoint_writer)
        if rc != 0:
            fail_count += 1
    _emit_run_verdict(live_run_dir, fail_count, len(fields_list))
    # Close the eval loop (bash mo_grade_run_reward, :3271): feed the rubric's GRADED
    # 0-8 run score into reward_g on every trace of this run. When rubric.json exists it
    # overwrites the per-node status-map reward with the graded value; absent → no-op,
    # leaving the reward_from_status fallback the trace_fn already stamped. Best-effort.
    if os.environ.get("MO_GRADE_RUN_REWARD", "1") == "1":
        try:
            from mini_ork import trace_store  # noqa: PLC0415
            trace_store.grade_run_reward(live_run_dir, run_id, db=db)
        except Exception:
            pass
    if fail_count > 0:
        set_status(db, run_id, "failed")
        sys.stderr.write(f"execute: {fail_count} node(s) failed\n")
        return 1
    print("\nexecute: all nodes complete")
    return 0


# ── per-node live-path support helpers (deterministic; increment 4) ──
#
# These are the deterministic operations _dispatch_node's live (non-dry-run)
# branches wire around the LLM call: DB status/cost writes and the "capture
# coin-flip" output applier. Ported + parity-gated ahead of the live routing
# (whose LLM dispatch is integration territory).

def set_status(db, run_id, new_status, *, dry_run=False):
    """Verbatim port of _d021_set_status: retrying task_runs status write;
    terminal states stamp ended_at + duration_ms."""
    if dry_run or not db or not run_id or not os.path.isfile(db):
        return
    terminal = {"published", "rolled_back", "failed"}
    last_err = None
    for attempt in range(3):
        try:
            con = sqlite3.connect(db, timeout=15.0)
            con.execute("PRAGMA busy_timeout = 15000")
            con.execute("PRAGMA journal_mode=WAL")
            try:
                if new_status in terminal:
                    now = int(time.time())
                    con.execute(
                        "UPDATE task_runs SET status = ?, updated_at = ?, ended_at = COALESCE(ended_at, ?), "
                        "duration_ms = CASE WHEN COALESCE(duration_ms, 0) = 0 "
                        "THEN MAX(COALESCE(ended_at, ?) - created_at, 0) * 1000 "
                        "ELSE duration_ms END WHERE id = ?",
                        (new_status, now, now, now, run_id))
                else:
                    con.execute("UPDATE task_runs SET status = ?, updated_at = ? WHERE id = ?",
                                (new_status, int(time.time()), run_id))
                con.commit()
                last_err = None
                break
            finally:
                con.close()
        except sqlite3.OperationalError as e:
            last_err = e
            time.sleep(0.5 * (attempt + 1))
    if last_err is not None:
        sys.stderr.write(f"[warn] set_status({new_status}) failed after retries: {last_err}\n")


def charge_node_cost(db, run_id, cost_file="", *, dry_run=False, root=None):
    """Verbatim port of _d022_charge_node_cost: charge the node's real LLM cost
    (from the .last-llm-cost sidecar; $0.01 placeholder otherwise), then the
    reactive cost-pause check (bash lib seam — sets MO_NODE_FINISH_REASON)."""
    if dry_run or not db or not run_id or not os.path.isfile(db):
        return
    cost = "0.01"
    if cost_file and os.path.isfile(cost_file):
        raw = open(cost_file).read().strip()
        try:
            v = float(raw)
            if 0 < v < 10:
                cost = raw
        except ValueError:
            pass
    try:
        con = sqlite3.connect(db)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA busy_timeout=5000")
        con.execute("UPDATE task_runs SET cost_usd = COALESCE(cost_usd,0) + ?, updated_at = ? WHERE id = ?",
                    (float(cost), int(time.time()), run_id))
        con.commit(); con.close()
    except Exception:
        pass
    root = root or os.environ.get("MINI_ORK_ROOT", "")
    pause = os.path.join(root, "lib", "cost_pause.sh") if root else ""
    if pause and os.path.isfile(pause):
        r = subprocess.run(["bash", "-c", f'source "{pause}"; mo_cost_pause_check "$1" "$2"',
                            "_", run_id, cost], capture_output=True)
        if r.returncode != 0:
            os.environ["MO_NODE_FINISH_REASON"] = "paused_for_approval"


def apply_impl_output(impl_log, target):
    """Verbatim port of mo_apply_impl_output (the 'capture coin-flip' fix): when
    the implementer applied NOTHING to the tree, parse its text output for a
    unified diff (git apply) or fenced file blocks with a path marker (write the
    files). Path-safe: rejects absolute / .. / out-of-target paths."""
    if os.environ.get("MO_APPLY_IMPL_OUTPUT", "1") != "1":
        return
    if not (impl_log and os.path.isfile(impl_log) and os.path.getsize(impl_log) > 0
            and os.path.isdir(target)):
        return
    porc = subprocess.run(["git", "-C", target, "status", "--porcelain"],
                          capture_output=True, text=True).stdout
    if porc.splitlines()[:1]:
        return
    text = open(impl_log, encoding="utf-8", errors="replace").read()
    target_real = os.path.realpath(target)

    def safe_path(p):
        p = p.strip().strip('`"\'')
        if not p or os.path.isabs(p) or ".." in p.split("/"):
            return None
        full = os.path.realpath(os.path.join(target_real, p))
        if not full.startswith(target_real + os.sep):
            return None
        return p

    applied = []
    if re.search(r"^--- (a/|/dev/null)", text, re.M) and re.search(r"^\+\+\+ b/", text, re.M):
        m = re.search(r"(^--- .*?)(?=\n```|\Z)", text, re.S | re.M)
        if m:
            try:
                subprocess.run(["git", "-C", target, "apply", "--whitespace=nowarn", "-"],
                               input=m.group(1), text=True, capture_output=True, check=True)
                applied.append("<unified-diff>")
            except subprocess.CalledProcessError:
                pass
    if not applied:
        lines = text.splitlines()
        i = 0
        while i < len(lines):
            line = lines[i]
            fm = re.match(r"^```[\w+-]*\s+(?:file=|path=)?([\w./_-]+\.[\w]+)\s*$", line)
            path = safe_path(fm.group(1)) if fm else None
            if not path and line.startswith("```") and line.strip() != "```":
                path = None
            if not path and line.startswith("```"):
                for back in range(1, 4):
                    if i - back < 0:
                        break
                    pm = re.match(
                        r"^\s*(?:#{2,4}\s*)?(?:\*\*)?(?:FILE:|File:|file:)?\s*`?"
                        r"([\w./_-]+\.(?:py|sh|md|yaml|yml|json|toml|txt|cfg|ini))`?:?(?:\*\*)?\s*$",
                        lines[i - back])
                    if pm:
                        path = safe_path(pm.group(1))
                        break
            if line.startswith("```") and path:
                body = []
                i += 1
                while i < len(lines) and not lines[i].startswith("```"):
                    body.append(lines[i])
                    i += 1
                if body:
                    full = os.path.join(target_real, path)
                    os.makedirs(os.path.dirname(full) or ".", exist_ok=True)
                    with open(full, "w", encoding="utf-8") as fh:
                        fh.write("\n".join(body) + "\n")
                    applied.append(path)
            i += 1
    if applied:
        print("  [apply-impl-output] applied from implementer text: " + ", ".join(applied))


# ── live per-node routing (increment 5) ──
#
# The live (non-dry-run) counterpart of _dry_dispatch_node. The LLM call is an
# injectable seam (dispatch_fn(task_class, node_type, prompt) -> (rc, text));
# the deterministic wiring around it — output-file naming, preserve-agent-Write,
# apply_impl_output, the reviewer verdict gate, cost charge, status — is ported.
# Trace writes + heartbeats + context assembly + oracle gates are best-effort
# seams (the run's pass/fail result does not depend on them). Recipe-specific
# dispatchers (per_feature/epic/minimal-scaffold) and the publisher commit
# delegate to their existing scripts. This makes main()'s live path functional
# with the LLM as the one integration seam.

def _extract_verdict(root, review_file) -> str:
    p = os.path.join(root, "lib", "extract_verdict.py")
    if not os.path.isfile(p):
        return "unknown"
    r = subprocess.run(["python3", p, review_file], capture_output=True, text=True)
    return (r.stdout.strip() or "unknown") if r.returncode == 0 else "unknown"


def _required_artifacts_ok(plan_path) -> bool:
    """Hollow-run guard for the verifier node (parity: bash _mo_required_artifacts_ok).
    A recipe that declares a concrete, run-local artifact (an ABSOLUTE, env-expanded
    artifact_contract path such as ``${MINI_ORK_RUN_DIR}/framework-edit.diff``) but
    produces nothing — missing OR zero-byte — fails. Relative canonical outputs are
    publish-targets (exempt), so a genuine artifact is never false-failed. Returns
    True when all required artifacts exist + are non-empty (or none apply)."""
    if not plan_path or not os.path.isfile(plan_path):
        return True
    try:
        ac = json.load(open(plan_path, encoding="utf-8")).get("artifact_contract", {})
    except Exception:
        return True
    if not isinstance(ac, dict):
        return True
    ok = True
    seen: set[str] = set()
    for key in ("required_artifacts", "outputs"):
        for raw in ac.get(key, []) or []:
            p = os.path.expandvars(str(raw))
            if not os.path.isabs(p) or p in seen:
                continue
            seen.add(p)
            if not (os.path.isfile(p) and os.path.getsize(p) > 0):
                sys.stderr.write(f"  [fail] required artifact missing or empty: {p}\n")
                ok = False
    return ok


def _run_verifier_ref(script, evidence_path, *, plan_path="", artifact_path="", cwd=None):
    """Port of _run_verifier_ref (minus the mo_runtime_exec seam): run the
    verifier script, capture evidence, and treat {"pass": true} as success."""
    cwd = cwd or os.environ.get("MO_TARGET_CWD") or os.getcwd()
    with open(evidence_path, "wb") as fh:
        rc = subprocess.run(["bash", script], cwd=cwd, stdout=fh, stderr=subprocess.STDOUT,
                            env={**os.environ, "MINI_ORK_PLAN_PATH": plan_path,
                                 "ARTIFACT_PATH": artifact_path}).returncode
    if not os.path.getsize(evidence_path):
        open(evidence_path, "w").write(f"vacuous pass: verifier exited {rc} but wrote no evidence")
        return 1
    try:
        payload = json.load(open(evidence_path))
    except Exception:
        return rc  # non-JSON evidence → propagate the script's rc
    if not isinstance(payload, dict) or "pass" not in payload:
        return rc
    return 0 if payload.get("pass") is True else 1


def _default_llm_dispatch(root):
    """The real LLM seam: shell to llm_dispatch, capturing stdout+stderr as the
    node result — mirrors bash's
    RESULT=$(llm_dispatch --task-class X --node-type Y --prompt-text Z 2>&1)."""
    lib = os.path.join(root, "lib", "llm-dispatch.sh")

    def d(task_class, node_type, prompt):
        r = subprocess.run(
            ["bash", "-c", f'source "{lib}"; llm_dispatch --task-class "$1" '
             '--node-type "$2" --prompt-text "$3" 2>&1', "_", task_class, node_type, prompt],
            capture_output=True, text=True)
        return r.returncode, r.stdout
    return d


def _watchdog_stale_heartbeat(root, db, run_id):
    """Port of bash `_mo_watchdog_check_stale_heartbeats` (embedded python). Returns
    '<node>\\t<ts>' for the first node whose last heartbeat is older than the timeout
    and not covered by a node_end, else '' (also '' on any error — best-effort)."""
    db = os.environ.get("MINI_ORK_DB", db)
    if not run_id or not db or not os.path.isfile(db):
        return ""
    try:
        timeout_ms = int(float(os.environ.get("MO_HEARTBEAT_TIMEOUT_S", "300")) * 1000)
    except ValueError:
        timeout_ms = 300000
    now_ms = int(time.time() * 1000)
    cutoff = now_ms - timeout_ms
    try:
        con = sqlite3.connect(db, timeout=2.0)
        con.execute("PRAGMA busy_timeout = 2000")
        try:
            cols = {r[1] for r in con.execute("PRAGMA table_info(run_events)").fetchall()}
            if "last_heartbeat_at" not in cols:
                return ""
            rows = con.execute(
                "SELECT event_id, event_type, payload_json, last_heartbeat_at, created_at "
                "FROM run_events WHERE run_id = ? AND event_type IN "
                "('node_start','node_heartbeat','node_end') ORDER BY created_at ASC",
                (run_id,)).fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return ""
    latest, ended_at = {}, {}
    for event_id, event_type, payload_raw, last_hb, created_at in rows:
        try:
            payload = json.loads(payload_raw or "{}")
        except json.JSONDecodeError:
            payload = {}
        node = payload.get("node_id") or event_id
        if event_type in ("node_start", "node_heartbeat") and last_hb is not None:
            if latest.get(node) is None or int(last_hb) > latest[node]:
                latest[node] = int(last_hb)
        elif event_type == "node_end":
            ended_ms = (int(created_at or 0) * 1000) + 999
            ended_at[node] = max(ended_ms, ended_at.get(node, 0))
    for node, last_hb in latest.items():
        if last_hb < cutoff and ended_at.get(node, 0) < last_hb:
            return f"{node}\t{last_hb}"
    return ""


def _synth_artifact_name(root, recipe):
    """Bash _dispatch_node:2710-2723 — the synth output file is the recipe's
    artifact_contract.yaml `source_artifact` (default synthesis.md)."""
    default = "synthesis.md"
    contract = os.path.join(root, "recipes", recipe, "artifact_contract.yaml") if recipe else ""
    if not contract or not os.path.isfile(contract):
        return default
    try:
        import yaml  # noqa: PLC0415 — lazy, matches bash's inline python
        d = yaml.safe_load(open(contract, encoding="utf-8")) or {}
        return (d.get("source_artifact") or default) if isinstance(d, dict) else default
    except Exception:
        return default


def _resolve_target_cwd(run_dir_eff):
    """Port of bash _dispatch_node:2633-2641. Derive the implementer edit-surface cwd
    from the run's kickoff git-toplevel; fall back to $MO_TARGET_CWD or cwd. This is
    the CWT-A corruption fix — pins codex to the TARGET repo, not MINI_ORK_ROOT."""
    kickoff = ""
    prof = os.path.join(run_dir_eff, "run_profile.json") if run_dir_eff else ""
    if prof and os.path.isfile(prof):
        try:
            kickoff = json.load(open(prof)).get("kickoff_path", "") or ""
        except Exception:
            kickoff = ""
    if kickoff and os.path.isfile(kickoff):
        kdir = os.path.dirname(kickoff)
        try:
            r = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                               cwd=kdir, capture_output=True, text=True)
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout.strip()
        except Exception:
            pass
        # NEW-2: bash's `$(cd dirname && git … || pwd)` returns dirname(kickoff) on
        # git failure (the subshell already cd'd) — NOT the executor cwd. Returning
        # os.getcwd() would re-open the CWT-A corruption path this fix exists to close.
        return kdir
    return os.environ.get("MO_TARGET_CWD") or os.getcwd()


def _assert_lane_capability(root, lane, required):
    """Shell to lib/lane-helpers.sh mo_assert_lane_capability (rc 0 = satisfiable).
    Not re-implemented — the capability taxonomy lives in the bash lib."""
    lib = os.path.join(root, "lib", "lane-helpers.sh")
    if not os.path.isfile(lib) or not required:
        return True
    try:
        r = subprocess.run(
            ["bash", "-c", f'source "{lib}"; MO_LANE_REQUIRES_CAPABILITY="$2" '
             'mo_assert_lane_capability "$1"', "_", lane, required],
            capture_output=True, text=True, timeout=15)
        return r.returncode == 0
    except Exception:
        return True


_JUDGE_LENS_FILES = {
    "opus_scalability_lens": "judge-opus-scalability.md",
    "opus_llm_safety_lens": "judge-opus-llm-safety.md",
    "kimi_correctness_lens": "judge-kimi-correctness.md",
    "codex_codebase_lens": "judge-codex-codebase.md",
    "minimax_perf_lens": "judge-minimax-performance.md",
}
_TIER4_LENS_FILES = {
    "tier4_glm": "tier4-glm.md", "tier4_kimi": "tier4-kimi.md",
    "tier4_codex": "tier4-codex.md", "tier4_minimax": "tier4-minimax.md",
}


def _researcher_output_file(run_dir, recipe, node_id):
    """F1-B (bash _dispatch_node:2403-2437): recipe-specific researcher output names.
    schema-judge-panel + recursive-validate-impl map non-_lens node_ids to the exact
    judge-*.md / tier4-*.md files their synthesizer + verifier glob for. Without these
    the panel gate reads context-<id>.json → zero lens inputs → theater verdict."""
    if recipe == "schema-judge-panel" and node_id in _JUDGE_LENS_FILES:
        return os.path.join(run_dir, _JUDGE_LENS_FILES[node_id])
    if recipe == "recursive-validate-impl" and node_id in _TIER4_LENS_FILES:
        return os.path.join(run_dir, _TIER4_LENS_FILES[node_id])
    if node_id.endswith(("_lens", "-lens")):
        return os.path.join(run_dir, f"lens-{node_id[:-5]}.md")
    return os.path.join(run_dir, f"context-{node_id}.json")


def _capture_pre_impl_baseline(run_dir):
    """Snapshot the working-tree state BEFORE the implementer edits, so the
    reviewer diff (below) captures ONLY the implementer's delta — never
    pre-existing dirt from a concurrent session sharing this in-place tree.

    Why this exists: framework-edit's implementer edits MO_TARGET_CWD in place,
    and the reviewer diff was `git diff` (working tree vs HEAD). With any
    unrelated uncommitted change already present, that diff swept it into
    review-diff.patch — so the run could review, and publish, another session's
    work. (Observed repeatedly; a concurrent session hit the same confound.)

    `git stash create` records the current tracked modifications as a commit
    object WITHOUT touching the working tree, index, or stash list — a purely
    non-destructive snapshot. Empty output means a clean tree, so the baseline is
    HEAD. The ref is persisted to <run_dir>/pre-implementer-ref and read back by
    _assemble_reviewer_inputs. Idempotent: only the first call (before the first
    implementer iteration) writes it.
    """
    if not run_dir:
        return
    ref_path = os.path.join(run_dir, "pre-implementer-ref")
    if os.path.isfile(ref_path):
        return
    cwd = os.environ.get("MO_TARGET_CWD") or os.getcwd()
    try:
        if subprocess.run(["git", "-C", cwd, "rev-parse", "--git-dir"],
                          capture_output=True).returncode != 0:
            return
        created = subprocess.run(["git", "-C", cwd, "stash", "create"],
                                 capture_output=True, text=True)
        ref = (created.stdout or "").strip()
        if not ref:
            head = subprocess.run(["git", "-C", cwd, "rev-parse", "HEAD"],
                                  capture_output=True, text=True)
            ref = (head.stdout or "").strip()
        if ref:
            os.makedirs(run_dir, exist_ok=True)
            with open(ref_path, "w") as fh:
                fh.write(ref + "\n")
    except Exception:
        pass


def _assemble_reviewer_inputs(run_dir):
    """F2-B (bash _mo_assemble_reviewer_inputs:182-275). Build the reviewer input block:
    implementer-summary.json + verifier_{typecheck,test}.json + a generated
    review-diff.patch, with the REVIEWER NOTE. Without this the classic reviewer reviews
    blind and hard-abstains ('inputs missing') — the gate becomes theater."""
    if not run_dir:
        return ""
    try:
        os.makedirs(run_dir, exist_ok=True)
    except OSError:
        pass
    summary = os.path.join(run_dir, "implementer-summary.json")
    worktree, files = "", []
    if os.path.isfile(summary):
        try:
            d = json.load(open(summary))
            worktree = d.get("worktree_path") or ""
            fc = d.get("files_changed") or []
            files = [str(x) for x in fc] if isinstance(fc, list) else []
        except Exception:
            pass
    if not worktree or not os.path.isdir(worktree):
        worktree = os.environ.get("MO_TARGET_CWD") or os.getcwd()
    diff_path = os.path.join(run_dir, "review-diff.patch")
    # Diff against the pre-implementer baseline (captured at run start by
    # _capture_pre_impl_baseline) so the reviewer sees ONLY the implementer's
    # delta, never pre-existing dirt from a concurrent session sharing this
    # in-place working tree. Falls back to a plain working-tree diff only when no
    # baseline was recorded (e.g. an isolated worktree that started clean).
    baseline = ""
    ref_path = os.path.join(run_dir, "pre-implementer-ref")
    if os.path.isfile(ref_path):
        try:
            baseline = open(ref_path).read().strip()
        except OSError:
            baseline = ""
    try:
        if os.path.isdir(worktree) and subprocess.run(
                ["git", "-C", worktree, "rev-parse", "--git-dir"],
                capture_output=True).returncode == 0:
            args = ["git", "-C", worktree, "diff", "--no-color"]
            if baseline:
                args.append(baseline)
            if files:
                args += ["--", *files]
            with open(diff_path, "w") as fh:
                subprocess.run(args, stdout=fh, stderr=subprocess.DEVNULL)
        if not (os.path.isfile(diff_path) and os.path.getsize(diff_path) > 0):
            open(diff_path, "w").close()
    except Exception:
        open(diff_path, "w").close()

    def _sec(title, path):
        if os.path.isfile(path) and os.path.getsize(path) > 0:
            return f"\n# {title}\n{open(path).read()}\n"
        return f"\n# {title}\n(not available)\n"

    block = "--- Reviewer inputs (assembled by mini-ork-execute) ---\n"
    block += _sec("implementer-summary.json", summary)
    block += _sec("verifier_typecheck.json", os.path.join(run_dir, "verifier_typecheck.json"))
    block += _sec("verifier_test.json", os.path.join(run_dir, "verifier_test.json"))
    if os.path.isfile(diff_path) and os.path.getsize(diff_path) > 0:
        block += f"\n# review-diff.patch\n{open(diff_path).read()}\n"
    else:
        block += "\n# review-diff.patch\n(no diff)\n"
    block += ("\n--- End reviewer inputs ---\n\n"
              "REVIEWER NOTE: The four inputs above are required for a real verdict. If any "
              "input is marked '(not available)' or '(no diff)', review what IS present. Only "
              "hard-abstain (verdict=needs_revision with reason 'inputs missing') when BOTH the "
              "diff and the summary are absent — that is the only genuine no-op case. A missing "
              "verifier verdict is a real failure signal, not an abstention excuse.\n")
    return block


def _learned_block(root, task_class, node_type):
    """F5-B (bash _dispatch_node:2357-2382): inject reflect-learned failure modes +
    unconsumed operator-steering messages into LLM node prompts — the READ side of
    the learning loop. Shells to lib/context_assembler.sh (the same code the bash path
    runs). Empty when opt-out (MO_INJECT_LEARNINGS=0), non-LLM node, or lib absent."""
    if os.environ.get("MO_INJECT_LEARNINGS", "1") != "1":
        return ""
    if node_type not in ("researcher", "implementer", "reviewer"):
        return ""
    lib = os.path.join(root, "lib", "context_assembler.sh")
    if not os.path.isfile(lib):
        return ""
    block = ""
    try:
        fm = subprocess.run(
            ["bash", "-c", 'source "$1"; declare -f context_failure_modes_md >/dev/null 2>&1 '
             '&& context_failure_modes_md "$2" 5 || true', "_", lib, task_class or "generic"],
            capture_output=True, text=True, timeout=30).stdout.strip()
        if fm:
            block = "\n\n" + fm + "\n"
        st = subprocess.run(
            ["bash", "-c", 'source "$1"; declare -f context_operator_steering_md >/dev/null 2>&1 '
             '&& context_operator_steering_md "$2" || true', "_", lib, node_type],
            capture_output=True, text=True, timeout=30).stdout.strip()
        if st:
            block = block + "\n" + st + "\n"
    except Exception:
        pass
    return block


def _intervention_gate_check(root, node_id, node_type, lane, node_desc):
    """Shell to lib/intervention_gate.sh intervention_gate_check (rc 0 = proceed).
    Absent lib → proceed (matches bash `[ -f "$gate_lib" ] || return 0`)."""
    lib = os.path.join(root, "lib", "intervention_gate.sh")
    if not os.path.isfile(lib):
        return True
    try:
        r = subprocess.run(
            ["bash", "-c", f'source "{lib}"; intervention_gate_check "$1" "$2" "$3" "$4"',
             "_", node_id, node_type, lane, node_desc],
            capture_output=True, text=True, timeout=15)
        return r.returncode == 0
    except Exception:
        return True


def _envsubst(s):
    """B2-C: envsubst-equivalent — expand $VAR / ${VAR} from the environment, BLANKING
    unset vars (os.path.expandvars leaves them literal, which commits garbage
    ${VAR}-in-path files, the OSS-leak-garbage class the bash guard prevents)."""
    return re.sub(r'\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)',
                  lambda m: os.environ.get(m.group(1) or m.group(2), ''), s)


def _execute_gate_check(plan_path, run_dir, dry_run):
    """Port of bash execute pre-dispatch gate (:1142-1203). A plan with
    plan_status=needs_answers AND real human_questions must NOT dispatch: print the
    refusal, write blocked.json, mark the run failed/BLOCKED, emit an execute_blocked
    run_event, and signal exit 6. Returns True when blocked. Opt out via
    MINI_ORK_EXECUTE_GATE=0; skipped under dry-run."""
    if os.environ.get("MINI_ORK_EXECUTE_GATE", "1") != "1" or dry_run:
        return False
    try:
        p = json.load(open(plan_path))
    except Exception:
        return False
    status = p.get("plan_status") or ""
    questions = p.get("human_questions") or []
    # A needs_answers plan with ZERO questions is a contradiction — do not block.
    if status != "needs_answers" or not questions:
        return False
    gate_info = {"plan_status": status, "blocked_by": p.get("blocked_by") or "unknown",
                 "human_questions": questions}
    print("[blocked] plan_status=needs_answers — refusing to dispatch "
          "(MINI_ORK_EXECUTE_GATE=0 to override)")
    print(f"  blocked_by: {gate_info['blocked_by']}")
    for q in questions:
        print(f"  question: {q}")
    try:
        with open(os.path.join(run_dir, "blocked.json"), "w") as f:
            f.write(json.dumps(gate_info) + "\n")
    except OSError:
        pass
    # Resolve db with the same MINI_ORK_HOME/state.db fallback bash uses (:958) —
    # callers set MINI_ORK_HOME but not always MINI_ORK_DB.
    home = os.environ.get("MINI_ORK_HOME") or os.path.join(os.getcwd(), ".mini-ork")
    db = os.environ.get("MINI_ORK_DB") or os.path.join(home, "state.db")
    run_id = (os.environ.get("MINI_ORK_RUN_ID") or os.environ.get("MINI_ORK_TASK_RUN_ID")
              or os.path.basename(run_dir))
    if db and os.path.isfile(db) and run_id:
        try:
            now = int(time.time())
            con = sqlite3.connect(db, timeout=5.0)
            con.execute("PRAGMA busy_timeout = 5000")
            try:
                con.execute(
                    "UPDATE task_runs SET status='failed', verdict=COALESCE(verdict,'BLOCKED'), "
                    "updated_at=?, ended_at=COALESCE(ended_at,?), "
                    "notes=COALESCE(notes || '; ','') || "
                    "'execute gate: plan_status=needs_answers — nothing dispatched' "
                    "WHERE id=? AND status NOT IN ('published','rolled_back','failed')",
                    (now, now, run_id))
                con.execute(
                    "INSERT INTO run_events(event_id, run_id, event_type, payload_json, created_at) "
                    "VALUES (?,?,?,?,?)",
                    (f"evt-execute_blocked-{now}", run_id, "execute_blocked",
                     json.dumps(gate_info), now))
                con.commit()
            finally:
                con.close()
        except sqlite3.Error:
            pass
    return True


def _make_trace_fn(task_class, db, run_id):
    """F3: build the trace_fn that reproduces bash `_trace_write_node_rich` (:1786) —
    without it the live path writes ZERO execution_traces rows and the whole GRPO /
    reflect learning loop is inert under the python runtime. Each node success/failure
    writes a row with a reward stamp (reward_from_status) + code_region so
    lane_router_recompute_advantages has real signal to learn from.
    Signature matches dispatch_node's `trace(node_id, status, node_type, output_file,
    verdict, finish_reason, lane)`."""
    from mini_ork import trace_store  # noqa: PLC0415

    def _tf(node_id, status, node_type, output_file="", verdict="", finish_reason="", lane=""):
        extra = {
            "trace_id": f"tr-{node_type}-{node_id}-{uuid.uuid4().hex[:8]}",
            "run_id": run_id,
            # objective_domain is the GRPO slice key (bash obj stamp, :1839). Stamp it
            # from the run's env so the feature-partition column populates per-run;
            # default code-delivery ONLY when unset, matching trace_store's fallback.
            "objective_domain": (os.environ.get("MINI_ORK_OBJECTIVE_DOMAIN")
                                 or os.environ.get("MO_OBJECTIVE_DOMAIN") or "code-delivery"),
            "verifier_output": {"node_type": node_type, "finish_reason": finish_reason or None},
        }
        # agent_version_id = the resolved dispatch lane (bash passes ${dispatch_lane:-}
        # into the payload, :1878). Without it lane attribution is lost on every trace,
        # so lane_router_recompute_advantages can't group rows by lane.
        if lane:
            extra["agent_version_id"] = lane
        # Implementer code_region must reflect the TARGET repo's edited source,
        # not the .mini-ork run-log path. Seed files_written from git-visible
        # target-repo changes FIRST so infer_trace_code_region resolves the
        # region from the edited repo; output_file (impl.log) stays as the
        # fallback consumed only when there are no target-repo changes.
        files_written = _target_repo_changed_files() if node_type == "implementer" else []
        if output_file:
            extra["final_artifact_ref"] = output_file
            files_written.append(output_file)
            # tool-summary sidecar (bash _trace_write_node_rich, :1786): llm-dispatch
            # emits "${output_file}.tool-summary" from its stream-json post-process when
            # MO_TRACE_RICH=1. When present, merge tool_calls + files_read (+ any extra
            # files_written) so reflect's gradient_extract sees real tool/file signal
            # instead of empty arrays — the D-048 fix, mirrored from bash. Best-effort:
            # a missing/garbled sidecar is a silent no-op (bash reads with `|| true`).
            sidecar = f"{output_file}.tool-summary"
            if os.path.isfile(sidecar):
                try:
                    with open(sidecar) as fh:
                        ts = json.load(fh)
                    tool_calls = ts.get("tool_calls") or []
                    files_read = ts.get("files_read") or []
                    if tool_calls:
                        extra["tool_calls"] = tool_calls
                    if files_read:
                        extra["files_read"] = files_read
                    for fw in (ts.get("files_written") or []):
                        if fw and fw not in files_written:
                            files_written.append(fw)
                except Exception:
                    pass  # best-effort (bash: python3 … 2>/dev/null)
        if files_written:
            extra["files_written"] = files_written
        if verdict:
            extra["reviewer_verdict"] = verdict
        if finish_reason:
            extra["finish_reason"] = finish_reason
        # Reward stamp (bash:1812-1815): activates the GRPO shared-brain loop.
        if os.environ.get("MO_REWARD_STAMP", "1") == "1":
            rv = reward_from_status(status, verdict)
            if rv:
                try:
                    extra["reward_value"] = float(rv)
                    extra["reward_anchor"] = float(os.environ.get("MO_REWARD_ANCHOR", "0.5"))
                    extra["reward_direction"] = "higher_is_better"
                except ValueError:
                    pass
        try:
            payload = trace_store.trace_write_node(task_class, status, extra)
            trace_id = trace_store.trace_write(payload, db=db)
            # code_region UPDATE (bash _mo_update_trace_code_region:1744) — the GRPO
            # grouping key alongside (objective_domain, task_class, node_type).
            region = infer_trace_code_region(json.dumps(payload))
            if trace_id and region and db and os.path.isfile(db):
                con = sqlite3.connect(db, timeout=5.0)
                con.execute("PRAGMA busy_timeout=5000")
                try:
                    cols = {r[1] for r in con.execute("PRAGMA table_info(execution_traces)").fetchall()}
                    if "code_region" in cols:
                        con.execute("UPDATE execution_traces SET code_region=? WHERE trace_id=?",
                                    (region, trace_id))
                        con.commit()
                finally:
                    con.close()
        except Exception:
            pass  # trace writes are best-effort (bash uses 2>/dev/null || true)

    return _tf


def _make_checkpoint_fn(db, run_id, run_dir, recipe, task_class):
    """F4 (durable-dag E1): closure that publishes a ``node_checkpoints`` row
    at every node success. Mirrors ``_make_trace_fn``'s role — a single
    closure the dispatch_node trace wrapper calls. Best-effort: failures
    are logged to stderr but NEVER raise, so a transient DB hiccup cannot
    crash a live run. The runtime treats the absence of a row as
    ``not reusable → rerun`` (the fail-closed contract from design §4).

    E1 placeholder semantics (E2 will tighten these):
      - ``input_hash`` is a stable per-(run,node) sha256 — E1 has no
        upstream-input resolution; this keeps the column populated and
        the validity check wired so the schema, write, and read paths
        are all exercised. E2 replaces this with a real upstream-hash.
      - ``recipe_version`` is the resolved recipe name (workflow.yaml
        is the source of truth in E2; recipe is a stable proxy in E1).
      - ``config_hash`` is a stable per-(task_class, recipe, run_id)
        sha256 — the resolved config slice mini-ork already knows.
    """
    from mini_ork.ported import mini_ork_checkpoints as mc  # noqa: PLC0415
    started_at = int(time.time())
    recipe_eff = recipe or "unknown"
    # E3 fencing: during a recovery dispatch, mini-ork-recover acquires the
    # run's single-writer lease and exports MINI_ORK_LEASE_TOKEN. Threading it
    # here makes every checkpoint publish present the token, so a stale worker
    # whose lease was re-acquired by a newer recovery is rejected at the write
    # (design §7). On a normal run the env is unset → owner_token=None → no
    # fencing (preserves E1's contract).
    owner_token = os.environ.get("MINI_ORK_LEASE_TOKEN") or None
    # Pre-compute the stable input/config hashes; both depend only on
    # resolved run-level fields so they are constant for a given (run,
    # recipe, task_class) and only vary by node_id (input_hash) — which
    # is exactly the per-node reuse key the validity check compares.
    config_hash = hashlib.sha256(
        f"{task_class}|{recipe_eff}|{run_id}".encode()).hexdigest()

    def _cp(node_id: str, status: str, node_type: str = "", output_file: str = ""):
        if status != "success":
            return  # only success → reusable checkpoint (design §3 rule 0)
        if not output_file:
            return  # nothing on disk to checkpoint; no row = rerun is correct
        # input_hash is per-(run, node) in E1; E2 will fold in upstream
        # input sha256s so a config change invalidates exactly the right
        # subtree.
        input_hash = hashlib.sha256(
            f"{run_id}|{node_id}|{recipe_eff}".encode()).hexdigest()
        # (E4) recover the node's claude session id from the dispatch sidecar
        # so write_checkpoint records it + persists the transcript. Empty for
        # non-claude lanes / when no dispatch happened.
        provider_session_id = ""
        _sc = os.path.join(run_dir, ".sessions", f"{node_id}.session")
        if os.path.isfile(_sc):
            try:
                provider_session_id = open(_sc).read().strip()
            except OSError:
                provider_session_id = ""
        mc.write_checkpoint(
            db, run_id, node_id,
            status="success", input_hash=input_hash,
            recipe_version=recipe_eff, config_hash=config_hash,
            artifact_paths=[output_file], run_dir=run_dir,
            node_type=node_type or "", started_at=started_at,
            ended_at=int(time.time()), initiator="python",
            owner_token=owner_token, provider_session_id=provider_session_id,
        )

    return _cp


def _publisher_try_commit_files(root, target_repo, run_dir, review_file, verdict_env,
                                recipe, node_desc, run_id):
    """Port of bash `_publisher_try_commit_files` (embedded python, :824-949). Commit
    the implementer's in-place edits on reviewer APPROVE. Strict-child path validation
    (the OSS-leak guard — only `git add --` files proven inside the target repo, never
    `-A`). Returns True on commit, False on skip."""
    def log(msg):
        print(msg, file=sys.stderr, flush=True)
    summary_path = os.path.join(run_dir, "implementer-summary.json") if run_dir else ""
    verdict = ""
    candidates = []
    if run_dir:
        for name in ("panel-verdict.json", "review-verdict.json"):
            p = os.path.join(run_dir, name)
            if os.path.isfile(p):
                candidates.append(p)
    if review_file and os.path.isfile(review_file):
        candidates.append(review_file)
    for p in candidates:
        try:
            data = json.load(open(p, encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        if data.get("pass") is True or str(data.get("verdict", "")).strip().lower() in {"approve", "approved", "pass"}:
            verdict = "approve"
            break
    if verdict != "approve" and review_file and root:
        try:
            out = subprocess.check_output(
                ["python3", os.path.join(root, "lib", "extract_verdict.py"), review_file],
                stderr=subprocess.DEVNULL).decode("utf-8", "replace").strip().lower()
            if out in {"approve", "approved", "pass"}:
                verdict = "approve"
        except Exception:
            pass
    if verdict != "approve" and (verdict_env or "").strip().lower() in {"approve", "approved", "pass"}:
        verdict = "approve"
    if verdict != "approve":
        disp = verdict or (verdict_env or "").strip() or "<none>"
        log(f"  [skip-publish] reviewer verdict (resolved: '{disp}') is not APPROVE — no commit")
        return False
    files = []
    if summary_path and os.path.isfile(summary_path):
        try:
            data = json.load(open(summary_path, encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("files_changed"), list):
                files = [e for e in data["files_changed"] if isinstance(e, str) and e]
        except Exception:
            pass
    if not files:
        log(f"  [skip-publish] no files_changed in {summary_path or '<unset>'} — no commit")
        return False
    if not target_repo:
        log("  [skip-publish] no target_repo resolved (MO_TARGET_CWD empty and git toplevel failed)")
        return False
    real_root = os.path.realpath(target_repo)
    valid = []
    for raw in files:
        ap = raw if os.path.isabs(raw) else os.path.abspath(raw)
        if not os.path.exists(ap):
            log(f"  [reject-publish] file does not exist: {raw}")
            continue
        real = os.path.realpath(ap)
        if real != real_root and not real.startswith(real_root + os.sep):
            log(f"  [reject-publish] file escapes target repo toplevel: {raw} -> {real} not under {real_root}")
            continue
        valid.append(real)
    if not valid:
        log(f"  [skip-publish] no valid files inside target_repo={real_root} — no commit")
        return False
    try:
        subprocess.run(["git", "add", "--", *valid], cwd=target_repo, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        msg = f"mini-ork({recipe}): {node_desc} [run {run_id}]"
        subprocess.run(["git", "-c", "user.email=mini-ork@local", "-c", "user.name=mini-ork",
                        "commit", "-q", "-m", msg], cwd=target_repo, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        sha = subprocess.check_output(["git", "-C", target_repo, "rev-parse", "HEAD"]).decode("utf-8", "replace").strip()
        log(f"  [publish] committed {len(valid)} file(s): {sha}")
        return True
    except subprocess.CalledProcessError as e:
        log(f"  [skip-publish] git add/commit failed in {target_repo}: rc={e.returncode}")
        return False


def publisher_node(root, run_dir, db, run_id, recipe, task_class, review_file="", verdict_env=""):
    """Port of bash publisher branch (:2909-3200). The panel found the port stubbed
    this to `set_status('published')` — this restores the two BLOCKING gates + delivery:
      1. oracle gates (block on safety_violation);
      2. recursive-validate-impl panel-verdict approval gate;
      3. artifact-contract copy source_artifact→outputs + git commit, OR the M1
         empty-outputs in-place implementer commit.
    Returns (rc, finish_reason). Template ${VAR} paths resolved via expandvars
    (envsubst-equivalent)."""
    # ── oracle gates: fire once pre-publish; only a definitive safety_violation blocks
    if (os.environ.get("MO_ORACLE_GATES_AUTO", "1") == "1" and run_id and db
            and os.path.isfile(os.path.join(root, "lib", "gate_bootstrap.sh"))):
        verdict_file = os.path.join(run_dir, "panel-verdict.json")
        ctx = json.dumps({"panel_run_id": run_id, "recipe": recipe or "unknown",
                          "task_class": task_class or "generic",
                          "verdict_file": verdict_file, "current_round": 1})
        try:
            r = subprocess.run(
                ["bash", "-c",
                 'source "$1/lib/gate_bootstrap.sh" 2>/dev/null || true; '
                 'declare -f mo_bootstrap_oracle_gates >/dev/null 2>&1 && mo_bootstrap_oracle_gates 2>/dev/null || true; '
                 'source "$1/lib/gate_registry.sh" 2>/dev/null || true; '
                 'declare -f gate_run_all >/dev/null 2>&1 && gate_run_all "$2" "$3" 2>/dev/null || echo "{}"',
                 "_", root, task_class or "generic", ctx],
                capture_output=True, text=True, timeout=120)
            try:
                sv = json.loads(r.stdout.strip() or "{}").get("safety_violation", False)
            except Exception:
                sv = False
            if sv is True or str(sv) == "True":
                print("  [BLOCK] oracle-gates: safety_violation — publish refused (COALITION_ABORT or equivalent)")
                return 1, "safety_violation"
            print("  [ok] oracle-gates: pre-publish pass")
        except Exception:
            pass
    # ── recursive-validate-impl requires an approved panel verdict
    if recipe == "recursive-validate-impl":
        pvf = os.path.join(run_dir, "panel-verdict.json")
        if not (os.path.isfile(pvf) and os.path.getsize(pvf) > 0):
            print(f"  [BLOCK] publisher: missing panel verdict at {pvf}", file=sys.stderr)
            return 1, "verdict_fail"
        try:
            data = json.load(open(pvf, encoding="utf-8"))
            ok = data.get("pass") is True or str(data.get("verdict", "")).strip().lower() in {"approve", "approved", "pass"}
        except Exception:
            ok = False
        if not ok:
            print("  [BLOCK] publisher: panel verdict is not approved — publish refused", file=sys.stderr)
            return 1, "verdict_fail"
    # ── artifact contract
    contract = os.path.join(root, "recipes", recipe, "artifact_contract.yaml") if recipe else ""
    if not contract or not os.path.isfile(contract):
        print(f"  [warn] publisher: no artifact_contract.yaml at {contract} — skipping", file=sys.stderr)
        return 0, "done"
    src_name, outputs = "synthesis.md", []
    try:
        import yaml  # noqa: PLC0415
        d = yaml.safe_load(open(contract, encoding="utf-8")) or {}
        if isinstance(d, dict):
            src_name = d.get("source_artifact") or "synthesis.md"
            for o in (d.get("outputs") or []):
                if isinstance(o, dict) and o.get("path"):
                    outputs.append(o["path"])
                elif isinstance(o, str):
                    outputs.append(o)
    except Exception:
        pass
    if not outputs:
        # M1 empty-outputs: commit the implementer's in-place edits (code-fix recipes)
        target_repo = os.environ.get("MO_TARGET_CWD", "")
        if not target_repo:
            try:
                target_repo = subprocess.check_output(
                    ["git", "-C", root or ".", "rev-parse", "--show-toplevel"],
                    stderr=subprocess.DEVNULL).decode().strip()
            except Exception:
                target_repo = root or "."
        print("  [warn] publisher: artifact_contract.yaml has no outputs[] — skipping publish", file=sys.stderr)
        _publisher_try_commit_files(root, target_repo, run_dir, review_file, verdict_env,
                                    recipe or "code-fix", os.environ.get("MINI_ORK_NODE_DESC", "implementer"),
                                    run_id or "local")
        set_status(db, run_id, "published")
        return 0, "done"
    # ── copy source_artifact → outputs[] + git-commit each.
    # B2-C: recipe-creator meta-recipe references ${MINI_ORK_DERIVED_RECIPE_NAME}; read
    # it from chosen/recipe_name and export BEFORE resolving templates (bash :3075-3079).
    if not os.environ.get("MINI_ORK_DERIVED_RECIPE_NAME"):
        chosen = os.path.join(run_dir, "chosen", "recipe_name")
        if os.path.isfile(chosen):
            try:
                os.environ["MINI_ORK_DERIVED_RECIPE_NAME"] = "".join(open(chosen).read().split())
            except OSError:
                pass
    src = os.path.join(run_dir, _envsubst(src_name))
    src_is_dir = os.path.isdir(src)
    if not src_is_dir and not os.path.isfile(src):
        print(f"  [warn] publisher: expected source artifact missing: {src}", file=sys.stderr)
        return 1, "error"
    # B1: track copy/commit failures like bash (:3111-3184). A failed copy, or a
    # copy-OK-but-commit-failed with a dirty tree, is a real failure — return 1 and
    # do NOT mark 'published'. A commit that fails with nothing-to-commit (dst already
    # matches) is an OK no-op. Was: swallowed failures + unconditional 'published'.
    published_count = 0
    failed_count = 0
    for out in outputs:
        out = _envsubst(out)
        dst = os.path.join(root, out)
        copied = False
        try:
            if src_is_dir:
                dst_norm = dst.rstrip("/")
                os.makedirs(dst_norm, exist_ok=True)
                copied = subprocess.run(["cp", "-R", src + "/.", dst_norm + "/"],
                                        capture_output=True).returncode == 0
            else:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy(src, dst)
                copied = True
        except Exception as e:
            print(f"  [fail] publisher: cp failed for {out}: {e}", file=sys.stderr)
        if not copied:
            print(f"  [fail] publisher: cp failed for {out}", file=sys.stderr)
            failed_count += 1
            continue
        subprocess.run(["git", "add", out], cwd=root, check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        commit = subprocess.run(
            ["git", "-c", "user.email=mini-ork@local", "-c", "user.name=mini-ork",
             "commit", "-q", "-m",
             f"audit({recipe or 'unknown'}): publish synthesis from {run_id}\n\n"
             f"Run: {run_id}\nRecipe: {recipe or 'unknown'}\nOutput: {out}\n"
             "Dispatched by mini-ork-execute publisher node (D-037 v0.2-pt9)."],
            cwd=root, capture_output=True)
        if commit.returncode == 0:
            print(f"  [ok] publisher: published {out} (committed)")
            published_count += 1
        else:
            # nothing-to-commit (dst unchanged) is an OK no-op, not a failure.
            status = subprocess.run(["git", "status", "--porcelain", out], cwd=root,
                                    capture_output=True, text=True)
            if not status.stdout.strip():
                print(f"  [ok] publisher: {out} unchanged from prior cycle (no-op commit)")
                published_count += 1
            else:
                print(f"  [warn] publisher: copy OK but commit failed for {out}", file=sys.stderr)
                failed_count += 1
    if failed_count > 0:
        print(f"  [fail] publisher: {failed_count} of {published_count + failed_count} outputs failed",
              file=sys.stderr)
        return 1, "error"
    print(f"  [ok] publisher: {published_count} artifact(s) published")
    set_status(db, run_id, "published")
    return 0, "done"


_REVIEW_PASS = {"pass", "approve", "approved"}
_REVIEW_REVISE = {"revise", "needs_revision", "request_changes"}
# unknown/other verdicts fall through to verdict_fail (matches bash catch-all)


def dispatch_node(fields, *, root, run_dir, plan_path, task_class, db, run_id,
                  dispatch_fn, recipe="", workflow="", trace_fn=None,
                  checkpoint_fn=None):
    """Live dispatch of one node. Returns (rc, finish_reason). rc!=0 → FAIL_COUNT++.
    dispatch_fn(task_class, node_type, prompt) -> (rc, text)."""
    node_id, node_type, node_desc, prompt_ref, _dmode, verifier_ref, model_lane, node_requires_capabilities = \
        (list(fields) + [""] * 8)[:8]
    # F1: apply the learning policy router BEFORE dispatch (bash _dispatch_node:2219)
    # so the routed lane — not the raw workflow/node_type lane — reaches --node-type.
    # Without this the whole GRPO/learning-governed router is inert (panel finding 1).
    workflow_lane = model_lane or node_type
    lane = policy_route_lane(node_type, workflow_lane, dry_run=False, root=root)
    _base_trace = trace_fn or (lambda *a, **k: None)
    # F4: durable DAG checkpoint writer (E1). Single seam — the trace
    # wrapper below — so every node completion site publishes a row in
    # exactly one place. Best-effort: the writer returns non-zero on
    # failure but never raises; absence of a row means "not reusable",
    # which the runtime treats as rerun (design §4 fail-closed).
    _base_checkpoint = checkpoint_fn or (lambda *a, **k: None)

    # Bind the resolved lane into every trace() call so agent_version_id is stamped
    # (bash passes the shell var dispatch_lane into _trace_write_node_rich's payload).
    def trace(node_id, status, node_type, output_file="", verdict="", finish_reason=""):
        _base_trace(node_id, status, node_type, output_file, verdict, finish_reason, lane=lane)
        # F4: publish the durable checkpoint at the SAME single seam as the
        # trace write. The wrapper unifies node-completion side effects so
        # E2's recovery code can rely on every success also having a row.
        _base_checkpoint(node_id, status, node_type, output_file)
    run_dir_eff = os.environ.get("MINI_ORK_RUN_DIR", run_dir)
    # Snapshot the tree BEFORE any implementer node edits it, so the reviewer
    # diff captures only the implementer's delta (not pre-existing dirt from a
    # concurrent session sharing this in-place working tree). Non-destructive.
    _capture_pre_impl_baseline(run_dir_eff)
    cost_sidecar = os.path.join(run_dir_eff, ".last-llm-cost")

    def _charge():
        charge_node_cost(db, run_id, cost_sidecar, root=root)

    # Export the role-aware fallback chain (lead = resolved lane) so a python
    # dispatch backend routes around a hung/flaky lead lane (bash:2224-2225, NEW-5).
    os.environ["MO_DISPATCH_CHAIN"] = dispatch_chain(node_type, lane)

    # ── Pre-dispatch gates, in bash _dispatch_node order (:2231-2318). These run
    # for every real dispatch; the dry-run preview path is _dry_dispatch_node. ──
    # Cooperative soft-stop: UI POST /stop touches .stop-requested; bail BEFORE the
    # next node so an in-flight node finishes naturally (Stop=soft vs Kill=hard).
    if os.path.isfile(os.path.join(run_dir_eff, ".stop-requested")):
        print(f"  [stop] .stop-requested present — skipping node_id={node_id}", file=sys.stderr)
        return 1, "interrupted"

    # Intervention gate (bash:2258-2262): runs FIRST, for every node type (including
    # planner/reflector), before the type-specific handling.
    if not _intervention_gate_check(root, node_id, node_type, lane, node_desc):
        return 1, "blocked"

    # planner/reflector don't dispatch an LLM — handled after the intervention gate
    # (bash routes them through the same gate then falls to their case).
    if node_type == "planner":
        print("  [skip] planner node handled by mini-ork-plan")
        return 0, "done"
    if node_type == "reflector":
        # NEW-3: guard like bash's `… || true` (:2906) — a missing/non-exec binary
        # must degrade gracefully, not crash the whole live run with FileNotFoundError.
        try:
            subprocess.run([os.path.join(root, "bin", "mini-ork-reflect")], capture_output=True)
        except OSError:
            pass
        return 0, "done"

    # Capability assert (bash:2296-2306): a node's requires_capabilities must be
    # satisfiable by the resolved lane, else fail 'config' rather than dispatch to
    # an incapable lane.
    if node_requires_capabilities and not _assert_lane_capability(root, lane, node_requires_capabilities):
        print(f"  [config] lane={lane} missing required capability for node_id={node_id}: "
              f"{node_requires_capabilities}", file=sys.stderr)
        return 1, "config"
    # Stale-heartbeat watchdog (bash:2308-2315) for the LLM node types.
    if node_type in ("researcher", "implementer", "reviewer"):
        stale = _watchdog_stale_heartbeat(root, db, run_id)
        if stale:
            print(f"  [timeout] stale heartbeat detected before node_id={node_id}: {stale}",
                  file=sys.stderr)
            return 1, "timeout"

    recipe_dir = os.path.join(root, "recipes", recipe) if recipe else ""
    prompt_file = ""
    if prompt_ref and recipe_dir and os.path.isfile(os.path.join(recipe_dir, prompt_ref)):
        prompt_file = os.path.join(recipe_dir, prompt_ref)
    elif recipe_dir and os.path.isfile(os.path.join(recipe_dir, "prompts", f"{node_type}.md")):
        prompt_file = os.path.join(recipe_dir, "prompts", f"{node_type}.md")
    elif os.path.isfile(os.path.join(root, "prompts", f"{node_type}.md")):
        prompt_file = os.path.join(root, "prompts", f"{node_type}.md")

    def _prepend():
        return (f"\n\n--- Recipe prompt (system context) ---\n{open(prompt_file).read()}"
                f"\n--- /recipe prompt ---\n\n") if prompt_file and os.path.isfile(prompt_file) else ""

    def _write_preserving_agent(out_file, marker, result):
        # preserve the agent's own tool-call Write when it touched out_file
        if os.path.isfile(out_file) and os.path.getmtime(out_file) > os.path.getmtime(marker):
            open(out_file + ".stdout.md", "w").write(result)
        else:
            open(out_file, "w").write(result)

    plan_content = open(plan_path).read() if plan_path and os.path.isfile(plan_path) else ""
    # F5-B: reflect-learned failure modes + operator steering, injected after node_desc
    # in the LLM prompts (the read side of the learning loop). Empty for non-LLM nodes.
    learned = _learned_block(root, task_class, node_type)
    os.environ["MO_NODE_ID"] = node_id

    # (E4 turn-resume) During an active recovery, restore this node's persisted
    # transcript and export MO_RESUME_SESSION_ID so a claude lane continues the
    # interrupted conversation (`--resume <id>`, via providers.dispatch_model)
    # instead of starting the node over. Strictly recovery-scoped and fail-soft:
    # off recovery, for codex/gemini, or with no session it is a no-op and the
    # node runs normally.
    os.environ.pop("MO_RESUME_SESSION_ID", None)
    if (os.environ.get("MINI_ORK_RECOVERY_CLOSURE", "").strip()
            or os.environ.get("MINI_ORK_RECOVERY_FROM", "").strip()):
        try:
            from mini_ork.ported.resume_prep import prepare_node_resume  # noqa: PLC0415
            _resume_sid = prepare_node_resume(
                db, run_id, node_id, run_dir=run_dir, model=lane,
                cwd=os.environ.get("MO_TARGET_CWD") or None,
            )
            if _resume_sid:
                os.environ["MO_RESUME_SESSION_ID"] = _resume_sid
                print(f"  [resume] node_id={node_id} continuing session "
                      f"{_resume_sid[:12]}… via --resume", file=sys.stderr)
        except Exception as e:  # noqa: BLE001 — resume is best-effort
            print(f"  [resume] skipped for node_id={node_id}: {e}", file=sys.stderr)

    if node_type == "researcher":
        ctx = _researcher_output_file(run_dir, recipe or os.environ.get("MINI_ORK_RECIPE", ""), node_id)
        prompt = f"{_prepend()}Task: {node_desc}{learned}\n\nPlan context:\n{plan_content}\n\nWrite your output to: {ctx}"
        marker = os.path.join(run_dir, f".dispatch-marker-{node_id}"); open(marker, "w").write("")
        rc, result = dispatch_fn(task_class, lane, prompt)
        if rc != 0:
            fr = finish_reason_for_failure(rc, result)
            trace(node_id, "failure", "researcher", ctx, "", fr)
            return 1, fr
        _write_preserving_agent(ctx, marker, result)
        try:
            os.remove(marker)
        except OSError:
            pass
        trace(node_id, "success", "researcher", ctx, "", "done")
        _charge()
        return 0, "done"

    if node_type == "implementer":
        # F6-B: implementer sub-mode dispatchers (bash :2493-2555). Orchestration
        # recipes replace the single-LLM implementer with a python fan-out dispatcher.
        recipe_eff = recipe or os.environ.get("MINI_ORK_RECIPE", "")
        _submode = {
            ("doc-to-features-loop", "per_feature_dispatcher"):
                ("child-runs/_summary.json", "doc-to-features-loop/lib/per_feature_dispatcher.py"),
            ("epic-runner", "epic_dispatcher"):
                ("epic-results.json", "epic-runner/lib/epic_dispatcher.py"),
            ("epic-runner", "wave_aggregator"):
                ("wave-aggregate.json", "epic-runner/lib/wave_aggregator.py"),
        }.get((recipe_eff, node_id))
        if _submode:
            impl_rel, script_rel = _submode
            sub_log = os.path.join(run_dir, impl_rel)
            script = os.path.join(root, "recipes", script_rel)
            if not os.path.isfile(script):
                print(f"dispatcher script missing: {script}", file=sys.stderr)
                trace(node_id, "failure", "implementer", sub_log, "", "error")
                return 1, "error"
            os.makedirs(os.path.dirname(sub_log), exist_ok=True)
            rc = subprocess.run(["python3", script]).returncode
            if rc == 0:
                print(f"  [ok] dispatcher results → {sub_log}")
                trace(node_id, "success", "implementer", sub_log, "", "done")
                return 0, "done"
            print("dispatcher failed", file=sys.stderr)
            trace(node_id, "failure", "implementer", sub_log, "", "error")
            return 1, "error"
        impl_log = os.path.join(run_dir, f"impl-{node_id}.log")
        prompt = f"{_prepend()}Implement: {node_desc}{learned}\n\nPlan:\n{plan_content}"
        # F4: pin the codex/gemini edit surface to the TARGET repo (kickoff's git
        # toplevel), not os.getcwd(). Without this the implementer diff/writes land
        # in mini-ork's own tree when cwd != target — the CWT-A corruption hazard
        # (bash _dispatch_node:2626-2642). Export so cl_codex.sh reads it.
        target = _resolve_target_cwd(run_dir_eff)
        os.environ["MO_TARGET_CWD"] = target
        print(f"  [cwd] codex target: {target}", file=sys.stderr)
        rc, result = dispatch_fn(task_class, lane, prompt)
        if rc != 0:
            fr = finish_reason_for_failure(rc, result)
            trace(node_id, "failure", "implementer", impl_log, "", fr)
            return 1, fr
        open(impl_log, "w").write(result)
        apply_impl_output(impl_log, target)   # ported "capture coin-flip" applier
        trace(node_id, "success", "implementer", impl_log, "", "done")
        _charge()
        return 0, "done"

    if node_type == "reviewer":
        # F3/F6: three-way classification matching bash _dispatch_node:2704-2727.
        #  - recursive-validate-impl/tier4_synth is a PANEL GATE, not a synth: it
        #    writes panel-verdict.json and MUST run the verdict gate (approval gate).
        #  - other *synth* nodes are informational: write the artifact_contract
        #    source_artifact (default synthesis.md) and never gate.
        #  - everything else is a classic reviewer → review-<id>.json + gate.
        recipe_eff = recipe or os.environ.get("MINI_ORK_RECIPE", "")
        is_panel_gate = (recipe_eff == "recursive-validate-impl" and node_id == "tier4_synth")
        if is_panel_gate:
            review_file = os.path.join(run_dir, "panel-verdict.json")
            is_synth = False
        elif "synth" in node_id:
            review_file = os.path.join(run_dir, _synth_artifact_name(root, recipe_eff))
            is_synth = True
        else:
            review_file = os.path.join(run_dir, f"review-{node_id}.json")
            is_synth = False
        # F2-B: per-case prompt matching bash :2739-2756. The classic reviewer gets the
        # assembled inputs (summary + verifier verdicts + diff) AND the JSON envelope —
        # without the envelope the LLM emits prose → verdict=unknown → false rollback.
        if is_panel_gate:
            prompt = (f"{_prepend()}Synthesize panel verdict for: {node_desc}{learned}\n\n"
                      f"Plan:\n{plan_content}\n\nWrite strict JSON to: {review_file}")
        elif is_synth:
            prompt = (f"{_prepend()}Synthesize for: {node_desc}{learned}\n\n"
                      f"Plan:\n{plan_content}\n\nWrite your synthesis to: {review_file}")
        else:
            reviewer_inputs = _assemble_reviewer_inputs(run_dir_eff)
            prompt = (f"{_prepend()}Review the implementation for: {node_desc}{learned}\n\n"
                      f"Plan:\n{plan_content}\n\n{reviewer_inputs}\n"
                      'Respond with JSON: {"verdict": "pass|fail|needs_revision", "notes": []}')
        marker = os.path.join(run_dir, f".dispatch-marker-{node_id}"); open(marker, "w").write("")
        rc, result = dispatch_fn(task_class, lane, prompt)
        if rc != 0:
            fr = finish_reason_for_failure(rc, result)
            trace(node_id, "failure", "reviewer", review_file, "", fr)
            return 1, fr
        _write_preserving_agent(review_file, marker, result)
        try:
            os.remove(marker)
        except OSError:
            pass
        verdict = _extract_verdict(root, review_file)
        print(f"  [info] reviewer verdict={verdict} → {review_file}")
        vn = verdict.lower()
        if is_synth:  # true synth only — panel gate falls through to the verdict gate
            trace(node_id, "success", "reviewer", review_file, verdict, "done")
            _charge()
            return 0, "done"
        if vn in _REVIEW_PASS:
            trace(node_id, "success", "reviewer", review_file, verdict, "done")
            _charge()
            return 0, "done"
        fr = "verdict_revise" if vn in _REVIEW_REVISE else "verdict_fail"
        trace(node_id, "failure", "reviewer", review_file, verdict, fr)
        _charge()
        return 1, fr

    if node_type == "verifier":
        # Hollow-run guard: fail before any verifier runs if the recipe declares a
        # concrete run-local artifact (absolute contract path) that is missing or
        # zero-byte. Covers the verifier_ref branch (which bypasses mini-ork-verify).
        if not _required_artifacts_ok(plan_path):
            print("  [fail] verifier node: required artifact(s) missing or empty", file=sys.stderr)
            return 1, "error"
        artifact = ""
        try:
            ac = (json.load(open(plan_path)).get("artifact_contract") or {}) if plan_path else {}
            outs = ac.get("outputs") or [] if isinstance(ac, dict) else []
            artifact = outs[0] if outs else ""
        except Exception:
            artifact = ""
        if not artifact:
            # NEW-1: bash (:2899-2902) warns + sets error finish_reason but does NOT
            # return 1 — a verifier node with no artifact_contract outputs does not
            # fail the run. Return rc 0 to match (finish_reason error is informational).
            print("  [warn] verifier node: no outputs in artifact_contract")
            return 0, "error"
        if verifier_ref and recipe_dir:
            script = os.path.join(recipe_dir, verifier_ref)
            if not os.path.isfile(script):
                print(f"  [fail] verifier_ref not found: {verifier_ref}", file=sys.stderr)
                return 1, "error"
            ev_dir = os.path.join(os.environ.get("MINI_ORK_RUN_DIR", run_dir), "evidence")
            os.makedirs(ev_dir, exist_ok=True)
            ev = os.path.join(ev_dir, os.path.basename(verifier_ref).replace(".sh", "") + ".log")
            rc = _run_verifier_ref(script, ev, plan_path=plan_path, artifact_path=artifact)
            # F2-B: persist evidence to verifier_<stem>.json (bash :2886-2888) so the
            # reviewer input assembly can read the typecheck/test verdicts. Before the
            # rc return so failures are visible too (a missing verifier is real signal).
            vstem = verifier_ref[len("verifiers/"):] if verifier_ref.startswith("verifiers/") else verifier_ref
            vstem = vstem[:-3] if vstem.endswith(".sh") else vstem
            persist_dir = os.environ.get("MINI_ORK_RUN_DIR", run_dir)
            if persist_dir and os.path.isfile(ev) and os.path.getsize(ev) > 0:
                try:
                    shutil.copy(ev, os.path.join(persist_dir, f"verifier_{vstem}.json"))
                except OSError:
                    pass
            return (0, "done") if rc == 0 else (1, "error")
        rc = subprocess.run([os.path.join(root, "bin", "mini-ork-verify"), "--plan", plan_path,
                             "--task-class", task_class, artifact]).returncode
        return (0, "done") if rc == 0 else (1, "error")

    if node_type == "publisher":
        recipe_eff = recipe or os.environ.get("MINI_ORK_RECIPE", "")
        return publisher_node(root, run_dir_eff, db, run_id, recipe_eff, task_class,
                              review_file=os.environ.get("REVIEW_FILE", ""),
                              verdict_env=os.environ.get("VERDICT", ""))

    if node_type == "rollback":
        # F4: bash (:3205-3223) does a best-effort version_rollback (workflow then
        # agent), succeeds regardless of whether a prior version exists, sets
        # finish_reason=done and returns 0 — it does NOT set task_runs.status. Was:
        # set_status('rolled_back') + return 1 (a no-op that also mis-set status and
        # double-counted the failure). The upstream failure already failed the run.
        from mini_ork.ported import version_registry as _vr  # noqa: PLC0415
        reverted = False
        for kind, name in (("workflow", recipe or "default"), ("agent", "default")):
            try:
                _vr.rollback(kind, name, db=db)
                reverted = True
                break
            except Exception:
                continue
        if not reverted:
            print("  [ok] rollback: nothing to revert (no prior promoted version)", file=sys.stderr)
        print("  [ok] rollback complete")
        # NOTE: bash traces NO rollback node (:3205-3223 has no _trace_write_node_rich).
        # Tracing it with status=success would write a spurious +1-reward execution_traces
        # row — semantically inverted (rollback fires because the run FAILED) — that
        # poisons GRPO/reflect. Deliberately no trace() here to stay faithful.
        return 0, "done"

    return 0, "done"


if __name__ == "__main__":
    raise SystemExit(main())
