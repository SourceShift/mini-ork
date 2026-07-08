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
import json
import math
import os
import re
import sqlite3
import subprocess
import sys
import time

_SEP = "\x1f"
_NODE_TYPE_ORDER = ("planner", "researcher", "implementer", "reviewer", "verifier",
                    "reflector", "publisher", "rollback")


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
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("--help", "-h"):
            sys.stdout.write(
                "Usage: mini-ork execute [<plan.json>] [--node-type <type>] "
                "[--dispatch-mode <mode>] [--dry-run]\n\n"
                "Dispatch plan steps to node-type handlers.\n\n"
                "Node types: planner | researcher | implementer | reviewer | verifier |\n"
                "            reflector | publisher | rollback\n\n"
                "Dispatch modes: serial | parallel | partitioned | speculative\n\n"
                "Options:\n"
                "  --node-type <type>        Execute only nodes of this type (filter)\n"
                "  --dispatch-mode <mode>    Override workflow dispatch mode\n"
                "  --dry-run                 Print what would be dispatched; no LLM calls\n"
                "  --help                    Show this help\n")
            return 0
        elif a == "--dry-run":
            dry_run = True; i += 1
        elif a == "--node-type":
            filter_node_type = argv[i + 1]; i += 2
        elif a == "--dispatch-mode":
            dispatch_mode_override = argv[i + 1]; i += 2
        elif a.startswith("-"):
            sys.stderr.write(f"Unknown flag: {a}. Try --help\n"); return 2
        else:
            if not plan_path:
                plan_path = a; i += 1
            else:
                sys.stderr.write(f"Unexpected argument: {a}\n"); return 2

    workflow = os.environ.get("MINI_ORK_WORKFLOW", "")
    if not workflow and os.environ.get("MINI_ORK_RECIPE"):
        workflow = os.path.join(root, "recipes", os.environ["MINI_ORK_RECIPE"], "workflow.yaml")
    run_dir = os.path.dirname(plan_path) if plan_path else "."

    # NODE_IDS: workflow.yaml source wins; else plan.json.decomposition.
    if workflow and os.path.isfile(workflow):
        node_source = "workflow.yaml"
        node_ids = nodes_from_workflow(workflow)
    else:
        node_source = "plan.json.decomposition"
        node_ids = nodes_from_plan(plan_path, workflow)
    print(f"    nodes:    {len(node_ids)} (from {node_source})")

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
    set_status(db, run_id, "executing")
    ordered = ([f for nt in _NODE_TYPE_ORDER for f in fields_list if f[1] == nt]
               if dispatch_mode == "partitioned" else fields_list)
    for f in ordered:
        if filter_node_type and f[1] != filter_node_type:
            continue
        if f[1] == "rollback" and fail_count == 0:
            print("  [skip] rollback — no failures (escalates_to edge not triggered)")
            continue
        rc, _fr = dispatch_node(f, root=root, run_dir=live_run_dir, plan_path=plan_path,
                                task_class=task_class, db=db, run_id=run_id,
                                dispatch_fn=llm, recipe=recipe, workflow=workflow)
        if rc != 0:
            fail_count += 1
    _emit_run_verdict(live_run_dir, fail_count, len(fields_list))
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


_REVIEW_PASS = {"pass", "approve", "approved"}
_REVIEW_REVISE = {"revise", "needs_revision", "request_changes"}
# unknown/other verdicts fall through to verdict_fail (matches bash catch-all)


def dispatch_node(fields, *, root, run_dir, plan_path, task_class, db, run_id,
                  dispatch_fn, recipe="", workflow="", trace_fn=None):
    """Live dispatch of one node. Returns (rc, finish_reason). rc!=0 → FAIL_COUNT++.
    dispatch_fn(task_class, node_type, prompt) -> (rc, text)."""
    node_id, node_type, node_desc, prompt_ref, _dmode, verifier_ref, model_lane, _req = \
        (list(fields) + [""] * 8)[:8]
    lane = model_lane or node_type
    trace = trace_fn or (lambda *a, **k: None)
    cost_sidecar = os.path.join(os.environ.get("MINI_ORK_RUN_DIR", run_dir), ".last-llm-cost")

    def _charge():
        charge_node_cost(db, run_id, cost_sidecar, root=root)

    # rollback only fires on an upstream failure (escalates_to edge) — the caller
    # gates that; here a reached rollback sets the terminal status.
    if node_type == "planner":
        print("  [skip] planner node handled by mini-ork-plan")
        return 0, "done"
    if node_type == "reflector":
        subprocess.run([os.path.join(root, "bin", "mini-ork-reflect")], capture_output=True)
        return 0, "done"

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
    os.environ["MO_NODE_ID"] = node_id

    if node_type == "researcher":
        norm = node_id[:-5] if node_id.endswith(("_lens", "-lens")) else node_id
        ctx = (os.path.join(run_dir, f"lens-{norm}.md") if node_id.endswith(("_lens", "-lens"))
               else os.path.join(run_dir, f"context-{node_id}.json"))
        prompt = f"{_prepend()}Task: {node_desc}\n\nPlan context:\n{plan_content}\n\nWrite your output to: {ctx}"
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
        impl_log = os.path.join(run_dir, f"impl-{node_id}.log")
        prompt = f"{_prepend()}Implement: {node_desc}\n\nPlan:\n{plan_content}"
        target = os.environ.get("MO_TARGET_CWD") or os.getcwd()
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
        is_synth = "synth" in node_id
        review_file = (os.path.join(run_dir, f"review-{node_id}.json") if not is_synth
                       else os.path.join(run_dir, "synthesis.md"))
        prompt = f"{_prepend()}Review: {node_desc}\n\nPlan:\n{plan_content}"
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
        if is_synth:
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
        artifact = ""
        try:
            ac = (json.load(open(plan_path)).get("artifact_contract") or {}) if plan_path else {}
            outs = ac.get("outputs") or [] if isinstance(ac, dict) else []
            artifact = outs[0] if outs else ""
        except Exception:
            artifact = ""
        if not artifact:
            return 1, "error"
        if verifier_ref and recipe_dir:
            script = os.path.join(recipe_dir, verifier_ref)
            if not os.path.isfile(script):
                print(f"  [fail] verifier_ref not found: {verifier_ref}", file=sys.stderr)
                return 1, "error"
            ev_dir = os.path.join(os.environ.get("MINI_ORK_RUN_DIR", run_dir), "evidence")
            os.makedirs(ev_dir, exist_ok=True)
            ev = os.path.join(ev_dir, os.path.basename(verifier_ref).replace(".sh", "") + ".log")
            rc = _run_verifier_ref(script, ev, plan_path=plan_path, artifact_path=artifact)
            return (0, "done") if rc == 0 else (1, "error")
        rc = subprocess.run([os.path.join(root, "bin", "mini-ork-verify"), "--plan", plan_path,
                             "--task-class", task_class, artifact]).returncode
        return (0, "done") if rc == 0 else (1, "error")

    if node_type == "publisher":
        set_status(db, run_id, "published")
        return 0, "done"

    if node_type == "rollback":
        set_status(db, run_id, "rolled_back")
        return 1, "rolled_back"

    return 0, "done"


if __name__ == "__main__":
    raise SystemExit(main())
