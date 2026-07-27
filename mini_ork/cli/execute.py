"""The sole mini-ork executor implementation.

This module owns the node lifecycle orchestration: workflow selection, bounded
process-isolated dispatch, verification, checkpointing, and failure
propagation. Cohesive concerns live in dedicated modules and are re-exported
here for backward compatibility:

    mini_ork.dispatch.routing     — lane fallback chains + routing policy registry
    mini_ork.learning.writeback   — reward contract + GRPO advantage writeback
    mini_ork.cli.publisher        — publish gates + artifact delivery + commit
    mini_ork.context              — the MINI_ORK_*/MO_* environment contract

The LLM call remains an injectable external boundary through ``dispatch_fn``;
native tests use deterministic dispatchers and never spend provider credits.

Key public contracts:
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

import contextlib
import concurrent.futures
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from typing import Callable

from mini_ork.context import (
    ENV_DISPATCH_CHAIN,
    ENV_RESUME_SESSION_ID,
    ENV_RUN_DIR,
    ENV_TARGET_CWD,
    RunContext,
    apply_env_overrides,
    node_env_overrides,
)


# ── extracted modules (re-exported contracts; definitions live in the modules) ──
from mini_ork.learning.writeback import (  # noqa: F401
    learning_update_conductor_outcomes,
    reward_from_status,
    write_grpo_advantages,
)
from mini_ork.dispatch.routing import (  # noqa: F401
    dispatch_chain,
    learning_governed_lane,
    learning_static_lane,
    policy_route_lane,
)
from mini_ork.cli.publisher import (  # noqa: F401
    _envsubst,
    _publisher_try_commit_files,
    publisher_node,
)

_SEP = "\x1f"
_NODE_TYPE_ORDER = ("planner", "researcher", "transform", "implementer", "reviewer", "verifier",
                    "reflector", "publisher", "rollback")


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


# ── orchestration backbone (NODE_IDS assembly + DAG loop + dry-run) ──
#
# The live per-node LLM execution (_dispatch_node's non-dry-run branches) is the
# remaining integration-gated increment; main() below fully ports the
# deterministic orchestration — node assembly, dispatch-mode routing, the
# dry-run dispatch plan, verdict.json + status — all parity-gated against the
# live bash --dry-run. A live dispatch raises NotImplementedError unless a
# dispatch_fn seam is supplied.

def nodes_from_workflow(wf_path: str) -> list[str]:
    """Compile workflow.yaml into the executor's legacy 8-field node tuples.

    Legacy workflows retain declaration order. A workflow that opts into
    explicit artifact ports is scheduled in its compiler-validated topological
    order, so a consumer cannot race a producer just because its YAML position
    happened to be convenient.
    """
    from mini_ork.workflow import compile_workflow

    compiled = compile_workflow(wf_path)
    order = compiled.topological_order if compiled.bindings else compiled.declared_order
    return [compiled.nodes[node_id].dispatch_fields(_SEP) for node_id in order]


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
    if os.path.isfile(os.path.join(run_dir, "panel-verdict.json")):
        return
    verdict = "fail" if fail_count > 0 else "pass"
    verdict_path = os.path.join(run_dir, "verdict.json")
    if os.path.isfile(verdict_path):
        try:
            existing = json.load(open(verdict_path, encoding="utf-8"))
        except Exception:
            existing = {}
        if isinstance(existing, dict) and existing.get("source") == "execute@run-level":
            return
        # A recipe may own verdict.json as a detailed deliverable. Keep that
        # evidence intact and put executor bookkeeping beside it.
        verdict_path = os.path.join(run_dir, "run-verdict.json")
    try:
        open(verdict_path, "w").write(
            '{"verdict":"%s","failed_nodes":%d,"dispatched":%d,"source":"execute@run-level"}\n'
            % (verdict, fail_count, dispatched))
    except OSError:
        return
    print(f"  [verdict] run-level {os.path.basename(verdict_path)}: {verdict} "
          f"(failed_nodes={fail_count})")


def _max_parallel() -> int:
    """Return the bounded worker count (Bash default: 4, minimum: 1)."""
    try:
        return max(1, int(os.environ.get("MINI_ORK_MAX_PARALLEL", "4")))
    except ValueError:
        return 4


def _isolated_dispatch_worker(payload):
    """Run one native node in a process-isolated environment.

    Provider routing mutates process environment variables, so live concurrent
    nodes cannot safely share threads. The worker recreates best-effort trace
    and checkpoint writers locally and returns captured output to the parent.
    """
    (field, root, run_dir, plan_path, task_class, db, run_id,
     recipe, workflow) = payload
    stdout = io.StringIO()
    stderr = io.StringIO()
    try:
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            rc, finish_reason = dispatch_node(
                field,
                root=root,
                run_dir=run_dir,
                plan_path=plan_path,
                task_class=task_class,
                db=db,
                run_id=run_id,
                dispatch_fn=_default_llm_dispatch(root),
                recipe=recipe,
                workflow=workflow,
                trace_fn=_make_trace_fn(task_class, db, run_id),
                checkpoint_fn=_make_checkpoint_fn(
                    db, run_id, run_dir, recipe, task_class
                ),
            )
    except Exception as exc:
        rc, finish_reason = 1, "error"
        stderr.write(f"native parallel worker failed: {exc}\n")
    return rc, finish_reason, stdout.getvalue(), stderr.getvalue()


def _run_parallel_batch(
    fields,
    *,
    root,
    run_dir,
    plan_path,
    task_class,
    db,
    run_id,
    recipe,
    workflow,
):
    """Dispatch a bounded batch and return each node's outcome in field order."""
    if not fields:
        return []
    payloads = [
        (field, root, run_dir, plan_path, task_class, db, run_id, recipe, workflow)
        for field in fields
    ]
    try:
        with concurrent.futures.ProcessPoolExecutor(
            max_workers=min(_max_parallel(), len(payloads))
        ) as pool:
            results = list(pool.map(_isolated_dispatch_worker, payloads))
    except Exception as exc:
        print(f"  [warn] parallel worker pool unavailable; falling back to serial: {exc}",
              file=sys.stderr)
        results = [_isolated_dispatch_worker(payload) for payload in payloads]
    outcomes = []
    for field, (rc, finish_reason, out, err) in zip(fields, results):
        sys.stdout.write(out)
        sys.stderr.write(err)
        outcomes.append((field, rc, finish_reason))
    return outcomes


_USAGE = (
    "Usage: mini-ork execute [<plan.json>] [--node-type <type>] "
    "[--dispatch-mode <mode>] [--dry-run]\n"
    "                       [--from-node <id>] [--recovery] [--repair-budget <usd>]\n\n"
    "Dispatch plan steps to node-type handlers.\n\n"
    "Node types: planner | researcher | transform | implementer | reviewer | verifier |\n"
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


@dataclass(frozen=True)
class ExecuteArgs:
    """Parsed `mini-ork execute` argv (defaults honor the env contract)."""

    dry_run: bool
    filter_node_type: str
    dispatch_mode_override: str
    plan_path: str
    from_node: str
    recovery_active: bool
    repair_budget: str


def _parse_execute_argv(argv: list[str]) -> tuple[ExecuteArgs | None, int]:
    """Parse execute argv. Returns (args, 0) on success, (None, 0) after
    --help, (None, 2) on a usage error (stderr already written)."""
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
            sys.stdout.write(_USAGE)
            return None, 0
        elif a == "--dry-run":
            dry_run = True; i += 1
        elif a == "--node-type":
            filter_node_type = argv[i + 1]; i += 2
        elif a == "--dispatch-mode":
            dispatch_mode_override = argv[i + 1]; i += 2
        elif a == "--from-node":
            if i + 1 >= len(argv):
                sys.stderr.write("--from-node requires <id>\n"); return None, 2
            from_node = argv[i + 1]; i += 2
        elif a.startswith("--from-node="):
            from_node = a.split("=", 1)[1].strip(); i += 1
        elif a == "--recovery":
            recovery_active = True; i += 1
        elif a == "--repair-budget":
            if i + 1 >= len(argv):
                sys.stderr.write("--repair-budget requires <usd>\n"); return None, 2
            repair_budget = argv[i + 1]; i += 2
        elif a.startswith("--repair-budget="):
            repair_budget = a.split("=", 1)[1].strip(); i += 1
        elif a.startswith("-"):
            sys.stderr.write(f"Unknown flag: {a}. Try --help\n"); return None, 2
        else:
            if not plan_path:
                plan_path = a; i += 1
            else:
                sys.stderr.write(f"Unexpected argument: {a}\n"); return None, 2
    return ExecuteArgs(dry_run, filter_node_type, dispatch_mode_override,
                       plan_path, from_node, recovery_active, repair_budget), 0


def _resolve_plan_path(plan_path: str, home: str, *, from_node: str,
                       recovery_active: bool) -> tuple[str, int]:
    """Resolve plan path (bash :957-973): empty → newest plan.json in
    $MINI_ORK_HOME/runs, then REQUIRE it. A missing or nonexistent plan must
    exit 2 with a message, not a Python traceback (nodes_from_plan would
    open('') / a bad path). bash requires a plan even in workflow mode (it's
    used for run_dir / task_run_id / plan_content)."""
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
        return "", 2
    if plan_path and not os.path.isfile(plan_path):
        sys.stderr.write(f"plan not found: {plan_path}\n")
        return "", 2
    return plan_path, 0


def _apply_recovery_filter(node_ids: list[str], *, from_node: str,
                           recovery_active: bool, repair_budget: str,
                           workflow: str) -> tuple[list[str], int]:
    """E2 recovery-context filter: restrict the dispatch set to the closure
    computed by `mini-ork recover` (or every node downstream of --from-node).
    Ancestors of the closure root are SKIPPED — they have valid E1
    checkpoints, so dispatching them again would burn LLM calls for nothing.
    CLI flags take precedence over the env vars; both produce the same filter
    shape. Returns (filtered node_ids, 0) or (node_ids, 2) on a usage error."""
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
        return node_ids, 2
    effective_from = from_node or closure_from_env
    effective_closure = (
        set(closure_env.split()) if closure_env else set()
    )
    if not (effective_from or effective_closure):
        return node_ids, 0
    # Repair-budget wiring: surface the budget as MO_REPAIR_BUDGET_USD
    # so the cost_pause seam (or any future cost-aware router) can
    # honor it without depending on a new env contract.
    if repair_budget:
        try:
            v = float(repair_budget)
            if v > 0:
                apply_env_overrides({"MO_REPAIR_BUDGET_USD": f"{v:.2f}"})
        except ValueError:
            sys.stderr.write(
                f"execute: --repair-budget must be a positive number, got {repair_budget!r}\n"
            )
            return node_ids, 2
    if not effective_closure and effective_from:
        # Operator override only (--from-node, no closure set): trust
        # the operator and include every node downstream of from_node.
        # Use the planner's DAG loader so the semantics stay identical
        # to `mini-ork recover` (edges, escalates_to exclusion).
        from mini_ork.recovery.planner import load_dag
        dag = load_dag(workflow)
        effective_closure = dag.descendants(effective_from)
    # Filter by name; node_ids entries are SEP-joined strings, the format
    # _resolve_dispatch_mode and the dispatch loop both consume.
    before_count = len(node_ids)
    node_ids = [
        e for e in node_ids
        if e.split(_SEP, 1)[0] in effective_closure
    ]
    # Mark the run as a recovery dispatch so downstream trace / cost
    # seams can stamp the metadata without re-deriving the closure.
    apply_env_overrides({"MINI_ORK_RECOVERY_ACTIVE": "1"})
    if effective_from:
        apply_env_overrides({"MINI_ORK_RECOVERY_FROM": effective_from})
    print(
        f"    recovery: from_node={effective_from or '<unset>'} "
        f"closure={len(node_ids)}/{before_count} nodes"
    )
    return node_ids, 0


def main(argv=None, *, root=None, dispatch_fn=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    root = root or os.environ.get("MINI_ORK_ROOT") or os.getcwd()
    RunContext(root=root).apply()

    args, rc = _parse_execute_argv(argv)
    if args is None:
        return rc
    dry_run = args.dry_run
    filter_node_type = args.filter_node_type

    home = os.environ.get("MINI_ORK_HOME") or os.path.join(os.getcwd(), ".mini-ork")
    plan_path, rc = _resolve_plan_path(args.plan_path, home, from_node=args.from_node,
                                       recovery_active=args.recovery_active)
    if rc != 0:
        return rc

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

    node_ids, rc = _apply_recovery_filter(
        node_ids, from_node=args.from_node, recovery_active=args.recovery_active,
        repair_budget=args.repair_budget, workflow=workflow)
    if rc != 0:
        return rc

    dispatch_mode = _resolve_dispatch_mode(args.dispatch_mode_override, workflow)
    fields_list = [tuple((e.split(_SEP) + [""] * 8)[:8]) for e in node_ids]
    control_parents: dict[str, tuple[str, ...]] = {}
    if workflow and os.path.isfile(workflow):
        from mini_ork.workflow import compile_workflow

        control_parents = compile_workflow(workflow).control_parents

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
    task_class = ""
    if plan_path:
        try:
            with open(plan_path, encoding="utf-8") as handle:
                task_class = str((json.load(handle) or {}).get("task_class") or "")
        except (OSError, ValueError, TypeError):
            task_class = ""
    ctx = RunContext.from_env()
    task_class = task_class or ctx.task_class_or_default()
    db = ctx.db_or_default()
    run_id = ctx.run_id
    recipe = ctx.recipe
    live_run_dir = ctx.run_dir or run_dir
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
    selected = [f for f in fields_list if not filter_node_type or f[1] == filter_node_type]

    def _dispatch_serial(field):
        # D1: bash keeps FAIL_COUNT as a shell var visible to _mo_policy_route_lane's
        # trace_governed branch (:2014). Publish it so the port's policy_route_lane sees
        # the live prefix-failure count (else trace_governed never escalates).
        apply_env_overrides({"FAIL_COUNT": str(fail_count)})
        return dispatch_node(field, root=root, run_dir=live_run_dir, plan_path=plan_path,
                                task_class=task_class, db=db, run_id=run_id,
                                dispatch_fn=llm, recipe=recipe, workflow=workflow,
                                trace_fn=trace_writer, checkpoint_fn=checkpoint_writer)

    def _parallel(batch):
        # Injected dispatchers stay in-process and serial for deterministic,
        # provider-free tests. Production's default dispatcher gets real,
        # process-isolated concurrency.
        if dispatch_fn is not None:
            outcomes = []
            for field in batch:
                rc, finish_reason = _dispatch_serial(field)
                outcomes.append((field, rc, finish_reason))
            return outcomes
        apply_env_overrides({"FAIL_COUNT": str(fail_count)})
        return _run_parallel_batch(
            batch,
            root=root,
            run_dir=live_run_dir,
            plan_path=plan_path,
            task_class=task_class,
            db=db,
            run_id=run_id,
            recipe=recipe,
            workflow=workflow,
        )

    rollback_fields = [field for field in selected if field[1] == "rollback"]
    work_fields = [field for field in selected if field[1] != "rollback"]

    def _count_failures(outcomes):
        return sum(1 for _field, rc, _finish_reason in outcomes if rc != 0)

    def _dispatch_dependency_graph():
        """Dispatch control/data dependencies in readiness waves.

        ``compile_workflow`` has already proven the graph acyclic. This runtime
        pass adds the missing operational half: a child starts only after every
        selected parent succeeded; failed parents block descendants without
        executing a publisher or consumer against partial state.
        """
        nonlocal fail_count
        pending = {field[0]: field for field in work_fields}
        statuses: dict[str, str] = {}
        order = {field[0]: index for index, field in enumerate(work_fields)}
        selected_ids = set(pending)

        while pending:
            blocked = []
            for node_id in pending:
                parents = set(control_parents.get(node_id, ())) & selected_ids
                failed = sorted(
                    parent for parent in parents
                    if statuses.get(parent) in {"failed", "blocked"}
                )
                if failed:
                    blocked.append((node_id, failed))
            for node_id, failed in blocked:
                pending.pop(node_id)
                statuses[node_id] = "blocked"
                fail_count += 1
                print(
                    f"  [skip] node_id={node_id} blocked by failed parent(s): {', '.join(failed)}",
                    file=sys.stderr,
                )
            if not pending:
                break

            ready = []
            for node_id, field in pending.items():
                parents = set(control_parents.get(node_id, ())) & selected_ids
                if all(statuses.get(parent) == "success" for parent in parents):
                    ready.append(field)
            ready.sort(key=lambda field: order[field[0]])
            if not ready:
                # Defensive fail-closed fallback. Compiler cycle validation makes
                # this unreachable unless a filtered/externally mutated graph is
                # inconsistent with the field list.
                for node_id in sorted(pending, key=order.__getitem__):
                    pending.pop(node_id)
                    statuses[node_id] = "blocked"
                    fail_count += 1
                    print(
                        f"  [skip] node_id={node_id} has unresolved workflow parents",
                        file=sys.stderr,
                    )
                break

            if dispatch_mode == "parallel":
                batch = ready
            elif dispatch_mode == "partitioned":
                batch = []
                for node_type in _NODE_TYPE_ORDER:
                    batch = [field for field in ready if field[1] == node_type]
                    if batch:
                        break
                if not batch:
                    batch = [ready[0]]
            elif ready[0][4] == "parallel" and dispatch_fn is None:
                batch = [field for field in ready if field[4] == "parallel"]
            else:
                batch = [ready[0]]

            for field in batch:
                pending.pop(field[0])
            for field, rc, _finish_reason in _parallel(batch):
                if rc == 0:
                    statuses[field[0]] = "success"
                else:
                    statuses[field[0]] = "failed"
                    fail_count += 1

    dependency_aware = any(
        control_parents.get(field[0]) for field in work_fields
    )
    speculative_requested = dispatch_mode == "speculative" or any(
        field[4] == "speculative" for field in work_fields
    )

    if speculative_requested:
        # The schema's historical wording promised first-winner replicas, but
        # this executor has no replica identity or loser cancellation. Running
        # an arbitrary graph in this mode could report success after every node
        # failed, so reject it until replica semantics are explicit.
        print("  [config] speculative dispatch requires explicit replica semantics", file=sys.stderr)
        fail_count += 1
    elif dependency_aware:
        _dispatch_dependency_graph()
    elif dispatch_mode == "parallel":
        fail_count += _count_failures(_parallel(work_fields))
    elif dispatch_mode == "partitioned":
        for node_type in _NODE_TYPE_ORDER:
            if node_type == "rollback":
                continue
            group = [field for field in work_fields if field[1] == node_type]
            fail_count += _count_failures(_parallel(group))
    else:
        pending = []

        def _flush_pending():
            nonlocal fail_count, pending
            if pending:
                fail_count += _count_failures(_parallel(pending))
                pending = []

        for field in work_fields:
            if field[4] == "parallel" and dispatch_fn is None:
                pending.append(field)
                if len(pending) >= _max_parallel():
                    _flush_pending()
                continue
            _flush_pending()
            rc, _fr = _dispatch_serial(field)
            if rc != 0:
                fail_count += 1
        _flush_pending()

    if rollback_fields and fail_count > 0:
        for field in rollback_fields:
            _dispatch_serial(field)
    elif rollback_fields:
        print("  [skip] rollback — no failures (escalates_to edge not triggered)")
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
    # Close the learning writeback loop after the final reward is known. Both
    # writers are deterministic DB side-channels and remain best-effort.
    if os.environ.get("MO_LEARNING_WRITEBACK", "1") == "1":
        try:
            learning_update_conductor_outcomes(db)
            write_grpo_advantages(db)
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
    try:
        from mini_ork.dispatch import cost_pause
        if cost_pause.check(run_id, float(cost)) != 0:
            apply_env_overrides({"MO_NODE_FINISH_REASON": "paused_for_approval"})
    except Exception:
        pass


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
            except subprocess.CalledProcessError as exc:
                # Partial-apply accounting (roadmap Step 1 / A5): a failed
                # `git apply` used to vanish silently — the run continued
                # believing capture succeeded. Behavior is unchanged (fall
                # through to the fenced-block parser) but the failure is now
                # observable, with the rejected hunks counted.
                rejected = (exc.stderr or "").count("error: patch failed")
                print(f"  [warn] apply-impl-output: git apply failed "
                      f"({rejected} hunk(s) rejected) — trying fenced-block fallback",
                      file=sys.stderr)
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


def _verifier_runs_before_implementer(workflow, node_id) -> bool:
    """Return whether ``node_id`` is a pre-implementation verifier.

    Baseline/parity capture nodes intentionally run before an implementer can
    produce the plan's final artifacts. Applying the hollow-run artifact guard
    to that phase creates a false failed-node count even when the baseline
    verifier itself passes. A workflow with no implementer is entirely
    pre-implementation: its verifier scripts may be deterministic artifact
    producers, so they must run their own contracts. Unknown or malformed
    workflows fail closed by returning ``False``.
    """
    if not workflow or not node_id or not os.path.isfile(workflow):
        return False
    try:
        import yaml  # noqa: PLC0415
        nodes = (yaml.safe_load(open(workflow, encoding="utf-8")) or {}).get("nodes") or []
        current = next(i for i, node in enumerate(nodes) if node.get("name") == node_id)
        first_implementer = next(
            (i for i, node in enumerate(nodes) if node.get("type") == "implementer"),
            None,
        )
    except (OSError, StopIteration, TypeError, AttributeError):
        return False
    if first_implementer is None:
        return True
    return current < first_implementer


def _run_verifier_ref(script, evidence_path, *, plan_path="", artifact_path="", cwd=None):
    """Port of _run_verifier_ref (minus the mo_runtime_exec seam): run the
    verifier script, capture evidence, and treat {"pass": true} as success."""
    cwd = cwd or os.environ.get("MO_TARGET_CWD") or os.getcwd()
    verifier_env = {**os.environ,
                    "MINI_ORK_PLAN_PATH": plan_path,
                    "ARTIFACT_PATH": artifact_path}
    verifier_env = {str(k): str(v) for k, v in verifier_env.items()}
    # A direct ``mini-ork execute`` invocation may know the run directory only
    # through ``evidence_path``/``run_dir`` and not export MINI_ORK_RUN_DIR.
    # Recipe verifiers use that variable as their artifact namespace, so make
    # the executor-to-verifier boundary explicit instead of relying on an
    # outer CLI process to have populated it.
    verifier_env.setdefault("MINI_ORK_RUN_DIR", os.path.dirname(evidence_path))
    with open(evidence_path, "wb") as fh:
        rc = subprocess.run(["bash", script], cwd=cwd, stdout=fh, stderr=subprocess.STDOUT,
                            env=verifier_env).returncode
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
    """The real LLM seam: call the native dispatcher, capturing stdout+stderr as
    the node result — mirrors bash's
    RESULT=$(llm_dispatch --task-class X --node-type Y --prompt-text Z 2>&1)."""
    def d(task_class, node_type, prompt):
        from mini_ork.dispatch import llm_dispatch
        model = os.environ.get("MO_DISPATCH_CHAIN") or node_type
        captured = io.StringIO()
        try:
            with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
                rc = llm_dispatch.llm_dispatch(
                    ["--task-class", task_class, "--node-type", node_type,
                     "--model", model, "--prompt-text", prompt],
                    root=root,
                )
            return rc, captured.getvalue()
        except Exception as exc:
            return 1, captured.getvalue() + str(exc)
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
    from an explicit valid $MO_TARGET_CWD, otherwise the run kickoff's git-toplevel.
    This is
    the CWT-A corruption fix — pins codex to the TARGET repo, not MINI_ORK_ROOT."""
    explicit = os.environ.get("MO_TARGET_CWD") or ""
    if explicit and os.path.isdir(explicit):
        try:
            r = subprocess.run(["git", "-C", explicit, "rev-parse", "--show-toplevel"],
                               capture_output=True, text=True)
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout.strip()
        except Exception:
            pass
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
    return explicit or os.getcwd()


def _assert_lane_capability(root, lane, required):
    """Call the native capability taxonomy (True = satisfiable)."""
    del root
    if not required:
        return True
    try:
        from mini_ork.dispatch import lane_helpers
        lane_helpers.assert_lane_capability(lane, required)
        return True
    except RuntimeError:
        return False
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
    if recipe == "self-migrate":
        self_migrate_outputs = {
            "seam_mapper": "integration-map.json",
            "static_feature_ledger": "static-feature-ledger.json",
            "cost_verifiability_lens": "cost-verifiability-lens.md",
        }
        if node_id in self_migrate_outputs:
            return os.path.join(run_dir, self_migrate_outputs[node_id])
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


def _harvest_self_migrate_artifacts(run_dir, target):
    """Copy self-migrate outputs from an isolated target's run mirror.

    Codex providers are intentionally sandboxed to ``MO_TARGET_CWD``. When the
    engine run directory is outside that target, agents write the requested
    artifacts to ``<target>/.mini-ork/runs/<run-id>`` instead. Harvest those
    files immediately after the implementer returns so verifier and reviewer
    nodes in the same run consume the producer's actual evidence.
    """
    if not run_dir or not target:
        return []
    mirror = os.path.join(target, ".mini-ork", "runs", os.path.basename(run_dir.rstrip(os.sep)))
    if not os.path.isdir(mirror) or os.path.realpath(mirror) == os.path.realpath(run_dir):
        return []
    exact = {
        "self-migrate.diff", "static-feature-ledger.json", "integration-map.json",
        "verdict.json", "reflection.md", "requirements-gap-pass-1.md",
        "requirements-validation-pass-2.md", "pre-retirement-parity.json",
        "pre-retirement-parity-evidence.log",
    }
    prefixes = ("verifier_", "verifier-")
    copied = []
    os.makedirs(run_dir, exist_ok=True)
    for name in sorted(os.listdir(mirror)):
        src = os.path.join(mirror, name)
        if not os.path.isfile(src) or os.path.getsize(src) == 0:
            continue
        if name not in exact and not name.startswith(prefixes):
            continue
        dst = os.path.join(run_dir, name)
        if name == "verdict.json" and os.path.isfile(dst):
            try:
                current = json.load(open(dst, encoding="utf-8"))
            except Exception:
                current = {}
            if isinstance(current, dict) and current.get("source") == "execute@run-level":
                shutil.copy2(dst, os.path.join(run_dir, "run-verdict.json"))
        shutil.copy2(src, dst)
        copied.append(name)
    return copied


def _write_self_migrate_implementer_summary(run_dir, target, impl_log, harvested):
    """Materialize the reviewer/publisher summary for a self-migrate proposal."""
    if not run_dir or not target:
        return
    baseline = ""
    ref_path = os.path.join(run_dir, "pre-implementer-ref")
    if os.path.isfile(ref_path):
        try:
            baseline = open(ref_path, encoding="utf-8").read().strip()
        except OSError:
            baseline = ""
    args = ["git", "-C", target, "diff", "--name-only"]
    if baseline:
        args.append(baseline)
    try:
        changed = subprocess.run(args, capture_output=True, text=True, timeout=15)
        files = [os.path.join(target, line.strip()) for line in changed.stdout.splitlines()
                 if line.strip()] if changed.returncode == 0 else []
    except Exception:
        files = []
    payload = {
        "status": "implemented",
        "worktree_path": target,
        "files_changed": files,
        "implementation_log": impl_log,
        "harvested_artifacts": list(harvested),
    }
    with open(os.path.join(run_dir, "implementer-summary.json"), "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
        handle.write("\n")


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
    fixed_verifiers = {"verifier_typecheck.json", "verifier_test.json"}
    for name in sorted(fixed_verifiers):
        block += _sec(name, os.path.join(run_dir, name))
    # The self-migrate pre-retirement report intentionally has a distinct name:
    # it proves the legacy fork was green before deletion, rather than checking
    # the post-migration implementation. Surface it whenever the recipe emitted
    # it so the reviewer receives the complete retirement evidence set.
    for name in ("pre-retirement-parity.json",):
        path = os.path.join(run_dir, name)
        if os.path.isfile(path) and os.path.getsize(path) > 0:
            block += _sec(name, path)
    try:
        recipe_verifiers = sorted(
            name for name in os.listdir(run_dir)
            if name.startswith("verifier_") and name.endswith(".json")
            and name not in fixed_verifiers
        )
    except OSError:
        recipe_verifiers = []
    for name in recipe_verifiers:
        block += _sec(name, os.path.join(run_dir, name))
    for name in ("integration-map.json", "static-feature-ledger.json", "verdict.json",
                 "self-migrate.diff"):
        path = os.path.join(run_dir, name)
        if os.path.isfile(path) and os.path.getsize(path) > 0:
            block += _sec(name, path)
    if os.path.isfile(diff_path) and os.path.getsize(diff_path) > 0:
        block += f"\n# review-diff.patch\n{open(diff_path).read()}\n"
    else:
        block += "\n# review-diff.patch\n(no diff)\n"
    block += ("\n--- End reviewer inputs ---\n\n"
              "REVIEWER NOTE: The assembled inputs above are required for a real verdict. If any "
              "input is marked '(not available)' or '(no diff)', review what IS present. Only "
              "hard-abstain (verdict=needs_revision with reason 'inputs missing') when BOTH the "
              "diff and the summary are absent — that is the only genuine no-op case. A missing "
              "verifier verdict is a real failure signal, not an abstention excuse.\n")
    return block


def _learned_block(root, task_class, node_type):
    """F5-B (bash _dispatch_node:2357-2382): inject reflect-learned failure modes +
    unconsumed operator-steering messages into LLM node prompts — the READ side of
    the learning loop. Empty when opt-out or for a non-LLM node."""
    if os.environ.get("MO_INJECT_LEARNINGS", "1") != "1":
        return ""
    if node_type not in ("researcher", "implementer", "reviewer"):
        return ""
    del root
    block = ""
    try:
        from mini_ork import context_assembler
        fm = context_assembler.failure_modes_md(
            task_class or "generic", 5, db=os.environ.get("MINI_ORK_DB")
        ).strip()
        if fm:
            block = "\n\n" + fm + "\n"
        from mini_ork.steering import operator_steering
        rows = operator_steering.fetch_for(
            os.environ.get("MINI_ORK_RUN_ID", ""), node_type
        )
        if rows:
            lines = [
                "--- Operator steering (injected supervisor guidance) ---",
                f"{len(rows)} message(s) targeted at this node. Treat as load-bearing:",
            ]
            for row in rows:
                severity = str(row.get("severity", "info")).upper()
                source = row.get("source") or "unknown"
                lines.append(f"- [{severity}] (from {source}) {row.get('message', '')}")
            lines.append("--- /operator steering ---")
            block += "\n" + "\n".join(lines) + "\n"
    except Exception:
        pass
    return block


def _intervention_gate_check(root, node_id, node_type, lane, node_desc):
    """Call the Python-owned optional intervention policy."""
    del root
    try:
        from mini_ork.gates import intervention_gate
        return intervention_gate.intervention_gate_check(
            node_id, node_type, lane, node_desc
        )
    except Exception:
        return True


def _execute_gate_check(plan_path, run_dir, dry_run):
    """Port of bash execute pre-dispatch gate (:1142-1203). A plan with
    plan_status=needs_answers AND real human_questions must NOT dispatch: print the
    refusal, write blocked.json, mark the run failed/ESCALATE, emit an execute_blocked
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
                # verdict='ESCALATE' (not 'BLOCKED'): the task_runs CHECK constraint
                # (0013_task_runs.sql:33) only permits APPROVE/REQUEST_CHANGES/ESCALATE/
                # CRASH or NULL. A needs_answers plan escalates to a human for answers,
                # so ESCALATE is the correct verdict; the "blocked" provenance lives in
                # status='failed' + the execute_blocked run_event + notes + blocked.json.
                con.execute(
                    "UPDATE task_runs SET status='failed', verdict=COALESCE(verdict,'ESCALATE'), "
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
            # Track-B PRM scoring was default-on in the retired executor. Keep
            # that integration native: score the persisted row so its JSON
            # fields and DB defaults exactly match what downstream GRPO reads.
            # Best-effort and opt-out preserve the prior shell contract.
            if (trace_id and db and os.path.isfile(db)
                    and os.environ.get("MO_PRM_SCORE", "1") == "1"):
                try:
                    from mini_ork.learning.process_reward import score_trace
                    score_con = sqlite3.connect(db, timeout=5.0)
                    score_con.execute("PRAGMA busy_timeout=5000")
                    score_con.row_factory = sqlite3.Row
                    try:
                        row = score_con.execute(
                            "SELECT * FROM execution_traces WHERE trace_id=?",
                            (trace_id,),
                        ).fetchone()
                        if row is not None:
                            process_reward = score_trace(dict(row))
                            score_con.execute(
                                "UPDATE execution_traces SET process_reward=? "
                                "WHERE trace_id=?",
                                (process_reward, trace_id),
                            )
                            score_con.commit()
                    finally:
                        score_con.close()
                except Exception:
                    pass
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
    from mini_ork.stores import checkpoints as mc
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
    lane = policy_route_lane(
        node_type,
        workflow_lane,
        dry_run=False,
        root=root,
        task_class=task_class,
    )
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
    # Node prompts and subprocess verifiers refer to MINI_ORK_RUN_DIR as their
    # artifact namespace. ``mini-ork run`` can derive the directory from the
    # plan without exporting it, so publish the resolved value at the node
    # boundary before any provider or verifier subprocess is invoked.
    # Publish the resolved run directory at the node boundary before any
    # provider or verifier subprocess is invoked (canonical contract:
    # mini_ork.context).
    apply_env_overrides({ENV_RUN_DIR: run_dir_eff})

    # The artifact ledger is a semantic boundary, not a replacement for an OS
    # sandbox: it records exactly what the recipe declares, validates integrity
    # on every consumer handoff, and materializes the allowed inputs under the
    # run workspace. Existing recipes with no ports keep their current file
    # conventions and incur only an empty manifest.
    artifact_context = ""
    artifact_ledger = None
    compiled_workflow = None
    if workflow and os.path.isfile(workflow):
        try:
            from mini_ork.workflow import (
                ArtifactContractError,
                ArtifactLedger,
                WorkflowCompileError,
                compile_workflow,
            )

            compiled_workflow = compile_workflow(workflow)
            if node_id in compiled_workflow.nodes:
                artifact_ledger = ArtifactLedger(run_dir_eff, run_id)
                prepared_inputs = artifact_ledger.prepare_inputs(compiled_workflow, node_id)
                artifact_context = artifact_ledger.prompt_context(prepared_inputs)
                apply_env_overrides({
                    "MINI_ORK_NODE_INPUT_MANIFEST": str(prepared_inputs.manifest_path),
                    "MINI_ORK_NODE_INPUT_DIR": str(prepared_inputs.input_root),
                })
        except ArtifactContractError as exc:
            print(f"  [artifact] node_id={node_id}: {exc}", file=sys.stderr)
            return 1, "artifact_contract"
        except WorkflowCompileError as exc:
            print(f"  [artifact] node_id={node_id}: {exc}", file=sys.stderr)
            return 1, "config"
        except Exception as exc:
            print(f"  [artifact] node_id={node_id}: unexpected artifact setup failure: {exc}", file=sys.stderr)
            return 1, "config"
    else:
        apply_env_overrides({
            "MINI_ORK_NODE_INPUT_MANIFEST": None,
            "MINI_ORK_NODE_INPUT_DIR": None,
        })
    # Snapshot the tree BEFORE any implementer node edits it, so the reviewer
    # diff captures only the implementer's delta (not pre-existing dirt from a
    # concurrent session sharing this in-place working tree). Non-destructive.
    _capture_pre_impl_baseline(run_dir_eff)
    cost_sidecar = os.path.join(run_dir_eff, ".last-llm-cost")

    def _charge():
        charge_node_cost(db, run_id, cost_sidecar, root=root)

    # Export the role-aware fallback chain (lead = resolved lane) so a python
    # dispatch backend routes around a hung/flaky lead lane (bash:2224-2225, NEW-5).
    apply_env_overrides({ENV_DISPATCH_CHAIN: dispatch_chain(node_type, lane)})

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
    # (bash routes them through the same gate then falls to their case). Early-phase
    # handlers are registered in EARLY_NODE_HANDLERS (see register_node_handler).
    early_handler = EARLY_NODE_HANDLERS.get(node_type)
    if early_handler is not None:
        return early_handler(root)

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

    plan_content = open(plan_path).read() if plan_path and os.path.isfile(plan_path) else ""
    # F5-B: reflect-learned failure modes + operator steering, injected after node_desc
    # in the LLM prompts (the read side of the learning loop). Empty for non-LLM nodes.
    learned = _learned_block(root, task_class, node_type)
    # Publish the per-node identity + clear any stale resume session in one
    # canonical step (None removes the variable).
    apply_env_overrides(node_env_overrides(
        node_id=node_id, run_dir=run_dir_eff, resume_session_id=None))

    # (E4 turn-resume) During an active recovery, restore this node's persisted
    # transcript and export MO_RESUME_SESSION_ID so a claude lane continues the
    # interrupted conversation (`--resume <id>`, via providers.dispatch_model)
    # instead of starting the node over. Strictly recovery-scoped and fail-soft:
    # off recovery, for codex/gemini, or with no session it is a no-op and the
    # node runs normally.
    if (os.environ.get("MINI_ORK_RECOVERY_CLOSURE", "").strip()
            or os.environ.get("MINI_ORK_RECOVERY_FROM", "").strip()):
        try:
            from mini_ork.recovery.resume_prep import prepare_node_resume  # noqa: PLC0415
            _resume_sid = prepare_node_resume(
                db, run_id, node_id, run_dir=run_dir, model=lane,
                cwd=os.environ.get("MO_TARGET_CWD") or None,
            )
            if _resume_sid:
                apply_env_overrides({ENV_RESUME_SESSION_ID: _resume_sid})
                print(f"  [resume] node_id={node_id} continuing session "
                      f"{_resume_sid[:12]}… via --resume", file=sys.stderr)
        except Exception as e:  # noqa: BLE001 — resume is best-effort
            print(f"  [resume] skipped for node_id={node_id}: {e}", file=sys.stderr)

    ctx = NodeDispatch(
        node_id=node_id, node_type=node_type, node_desc=node_desc,
        prompt_ref=prompt_ref, verifier_ref=verifier_ref, model_lane=model_lane,
        node_requires_capabilities=node_requires_capabilities,
        root=root, run_dir=run_dir, plan_path=plan_path, task_class=task_class,
        db=db, run_id=run_id, recipe=recipe, workflow=workflow,
        lane=lane, run_dir_eff=run_dir_eff, recipe_dir=recipe_dir,
        prompt_file=prompt_file, plan_content=plan_content, learned=learned,
        dispatch_fn=dispatch_fn, trace=trace, charge=_charge,
        artifact_context=artifact_context, artifact_ledger=artifact_ledger,
        compiled_workflow=compiled_workflow,
    )
    # Node-type handler registry (OCP): a new node type is
    # register_node_handler("type", fn) — no edit to this function. Unknown
    # types fall through to (0, "done") exactly as the bash catch-all did.
    handler = NODE_HANDLER_REGISTRY.get(node_type)
    if handler is None:
        return 0, "done"
    return handler(ctx)


# ── Node-type handlers (SOLID M3, OCP) ───────────────────────────────────────
# One function per node type; dispatch_node is preamble + registry lookup.

_IMPLEMENTER_SUBMODES: dict[tuple[str, str], tuple[str, str]] = {
    # (recipe, node_id) -> (results artifact, dispatcher script), both
    # repo-relative. Orchestration recipes replace the single-LLM implementer
    # with a python fan-out dispatcher (bash :2493-2555).
    ("doc-to-features-loop", "per_feature_dispatcher"):
        ("child-runs/_summary.json", "doc-to-features-loop/lib/per_feature_dispatcher.py"),
    ("epic-runner", "epic_dispatcher"):
        ("epic-results.json", "epic-runner/lib/epic_dispatcher.py"),
    ("epic-runner", "wave_aggregator"):
        ("wave-aggregate.json", "epic-runner/lib/wave_aggregator.py"),
}


def register_implementer_submode(recipe: str, node_id: str,
                                 results_artifact: str, script: str) -> None:
    """Register a fan-out dispatcher for (recipe, node_id) — data, not code edits."""
    _IMPLEMENTER_SUBMODES[(recipe, node_id)] = (results_artifact, script)


@dataclass
class NodeDispatch:
    """Everything a node-type handler needs from the dispatch preamble.

    Handlers are (NodeDispatch) -> (rc, finish_reason) callables registered in
    NODE_HANDLER_REGISTRY; the preamble (policy routing, env publish, gates,
    prompt assembly) runs once in dispatch_node before the lookup.
    """

    node_id: str
    node_type: str
    node_desc: str
    prompt_ref: str
    verifier_ref: str
    model_lane: str
    node_requires_capabilities: str
    root: str
    run_dir: str
    plan_path: str
    task_class: str
    db: str
    run_id: str
    recipe: str
    workflow: str
    lane: str
    run_dir_eff: str
    recipe_dir: str
    prompt_file: str
    plan_content: str
    learned: str
    dispatch_fn: Callable
    trace: Callable
    charge: Callable
    artifact_context: str = ""
    artifact_ledger: object | None = None
    compiled_workflow: object | None = None

    @property
    def recipe_eff(self) -> str:
        return self.recipe or os.environ.get("MINI_ORK_RECIPE", "")

    def prepend(self) -> str:
        return (f"\n\n--- Recipe prompt (system context) ---\n{open(self.prompt_file).read()}"
                f"\n--- /recipe prompt ---\n\n") \
            if self.prompt_file and os.path.isfile(self.prompt_file) else ""

    def write_preserving_agent(self, out_file, marker, result):
        # preserve the agent's own tool-call Write when it touched out_file
        if os.path.isfile(out_file) and os.path.getmtime(out_file) > os.path.getmtime(marker):
            open(out_file + ".stdout.md", "w").write(result)
        else:
            open(out_file, "w").write(result)

    def dispatch(self, prompt: str):
        return self.dispatch_fn(self.task_class, self.lane, prompt)

    def declared_output_path(self, fallback: str) -> str:
        """Use a recipe port as the write target when it is unambiguous.

        Legacy handlers retain their file-name conventions. A new recipe with
        one declared output gets a schema-owned target instead of needing a
        node-id-specific branch in the executor.
        """
        if self.artifact_ledger is None or self.compiled_workflow is None:
            return fallback
        try:
            outputs = self.compiled_workflow.nodes[self.node_id].outputs
            if len(outputs) == 1:
                output_name = next(iter(outputs))
                return str(self.artifact_ledger.output_path(
                    self.compiled_workflow, self.node_id, output_name
                ))
        except Exception:
            pass
        return fallback

    def publish_declared_outputs(self) -> bool:
        """Fail the node if its recipe-declared output contract is not met."""
        if self.artifact_ledger is None or self.compiled_workflow is None:
            return True
        try:
            self.artifact_ledger.publish_node_outputs(self.compiled_workflow, self.node_id)
            return True
        except Exception as exc:
            print(f"  [artifact] node_id={self.node_id}: {exc}", file=sys.stderr)
            return False


def _handle_planner_early(root):
    print("  [skip] planner node handled by the Python plan runtime")
    return 0, "done"


def _handle_reflector_early(root):
    # Preserve bash's `… || true`: reflection is a side-channel and must
    # never fail the workflow node. Capturing output also prevents the
    # reflect report from leaking into execute's stdout contract.
    try:
        subprocess.run(
            [sys.executable, "-m", "mini_ork.cli.reflect"],
            capture_output=True,
            env={
                **os.environ,
                "MINI_ORK_ROOT": root,
                "PYTHONPATH": root + os.pathsep + os.environ.get("PYTHONPATH", ""),
            },
        )
    except OSError:
        pass
    return 0, "done"


EARLY_NODE_HANDLERS: dict[str, Callable] = {
    "planner": _handle_planner_early,
    "reflector": _handle_reflector_early,
}


def _handle_researcher(ctx: NodeDispatch):
    out_file = ctx.declared_output_path(
        _researcher_output_file(ctx.run_dir, ctx.recipe_eff, ctx.node_id)
    )
    os.makedirs(os.path.dirname(out_file) or ".", exist_ok=True)
    prompt = (f"{ctx.prepend()}Task: {ctx.node_desc}{ctx.learned}\n\nPlan context:\n"
              f"{ctx.plan_content}{ctx.artifact_context}\n\nWrite your output to: {out_file}")
    marker = os.path.join(ctx.run_dir, f".dispatch-marker-{ctx.node_id}")
    open(marker, "w").write("")
    rc, result = ctx.dispatch(prompt)
    if rc != 0:
        fr = finish_reason_for_failure(rc, result)
        ctx.trace(ctx.node_id, "failure", "researcher", out_file, "", fr)
        return 1, fr
    ctx.write_preserving_agent(out_file, marker, result)
    try:
        os.remove(marker)
    except OSError:
        pass
    if not ctx.publish_declared_outputs():
        ctx.trace(ctx.node_id, "failure", "researcher", out_file, "", "artifact_contract")
        return 1, "artifact_contract"
    ctx.trace(ctx.node_id, "success", "researcher", out_file, "", "done")
    ctx.charge()
    return 0, "done"


def _handle_implementer(ctx: NodeDispatch):
    impl_log = ctx.declared_output_path(
        os.path.join(ctx.run_dir, f"impl-{ctx.node_id}.log")
    )
    # F6-B: implementer sub-mode dispatchers (bash :2493-2555), registry-driven.
    submode = _IMPLEMENTER_SUBMODES.get((ctx.recipe_eff, ctx.node_id))
    if submode:
        impl_rel, script_rel = submode
        fallback_sub_log = os.path.join(ctx.run_dir, impl_rel)
        sub_log = ctx.declared_output_path(fallback_sub_log)
        script = os.path.join(ctx.root, "recipes", script_rel)
        if not os.path.isfile(script):
            print(f"dispatcher script missing: {script}", file=sys.stderr)
            ctx.trace(ctx.node_id, "failure", "implementer", sub_log, "", "error")
            return 1, "error"
        os.makedirs(os.path.dirname(fallback_sub_log) or ".", exist_ok=True)
        os.makedirs(os.path.dirname(sub_log), exist_ok=True)
        rc = subprocess.run(["python3", script]).returncode
        if rc == 0:
            if sub_log != fallback_sub_log and os.path.isfile(fallback_sub_log):
                shutil.copy2(fallback_sub_log, sub_log)
            print(f"  [ok] dispatcher results → {sub_log}")
            if not ctx.publish_declared_outputs():
                ctx.trace(ctx.node_id, "failure", "implementer", sub_log, "", "artifact_contract")
                return 1, "artifact_contract"
            ctx.trace(ctx.node_id, "success", "implementer", sub_log, "", "done")
            return 0, "done"
        print("dispatcher failed", file=sys.stderr)
        ctx.trace(ctx.node_id, "failure", "implementer", sub_log, "", "error")
        return 1, "error"
    prompt = (f"{ctx.prepend()}Implement: {ctx.node_desc}{ctx.learned}\n\nPlan:\n"
              f"{ctx.plan_content}{ctx.artifact_context}\n\n"
              f"Write your execution summary to: {impl_log}")
    os.makedirs(os.path.dirname(impl_log) or ".", exist_ok=True)
    # F4: pin the codex/gemini edit surface to the TARGET repo (kickoff's git
    # toplevel), not os.getcwd(). Without this the implementer diff/writes land
    # in mini-ork's own tree when cwd != target — the CWT-A corruption hazard
    # (bash _dispatch_node:2626-2642). Export so cl_codex.sh reads it.
    target = _resolve_target_cwd(ctx.run_dir_eff)
    apply_env_overrides({ENV_TARGET_CWD: target})
    print(f"  [cwd] codex target: {target}", file=sys.stderr)

    # R5b: the opt-in minimal scaffold is a real executor behavior, not
    # merely a resolver module. Its default remains ``harness``. Capture
    # the resolver's parity stdout so it cannot leak into execute output.
    try:
        from mini_ork.orchestration import scaffold_tier
        with contextlib.redirect_stdout(io.StringIO()):
            tier = scaffold_tier.mo_scaffold_tier(
                ctx.node_type, ctx.task_class
            ).strip()
    except Exception:
        tier = "harness"
    if tier == "minimal":
        try:
            from mini_ork.agent.minimal import run_minimal
            result = run_minimal(prompt, cwd=target)
            output = result.final_output or ""
            with open(impl_log, "w", encoding="utf-8") as handle:
                handle.write(output)
            if output:
                print(f"  [ok] minimal scaffold implementer output → {impl_log}")
                if not ctx.publish_declared_outputs():
                    ctx.trace(ctx.node_id, "failure", "implementer", impl_log, "", "artifact_contract")
                    return 1, "artifact_contract"
                ctx.trace(ctx.node_id, "success", "implementer", impl_log, "", "done")
                return 0, "done"
        except Exception:
            pass
        print("  [err] minimal scaffold implementer failed", file=sys.stderr)
        ctx.trace(ctx.node_id, "failure", "implementer", impl_log, "", "error")
        return 1, "error"

    rc, result = ctx.dispatch(prompt)
    if rc != 0:
        fr = finish_reason_for_failure(rc, result)
        ctx.trace(ctx.node_id, "failure", "implementer", impl_log, "", fr)
        return 1, fr
    open(impl_log, "w").write(result)
    apply_impl_output(impl_log, target)   # ported "capture coin-flip" applier
    if ctx.recipe_eff == "self-migrate":
        harvested = _harvest_self_migrate_artifacts(ctx.run_dir_eff, target)
        _write_self_migrate_implementer_summary(
            ctx.run_dir_eff, target, impl_log, harvested
        )
    if not ctx.publish_declared_outputs():
        ctx.trace(ctx.node_id, "failure", "implementer", impl_log, "", "artifact_contract")
        return 1, "artifact_contract"
    ctx.trace(ctx.node_id, "success", "implementer", impl_log, "", "done")
    ctx.charge()
    return 0, "done"


def _classify_review_node(recipe_eff: str, node_id: str, root: str, run_dir: str):
    """F3/F6 three-way classification matching bash _dispatch_node:2704-2727:
     - recursive-validate-impl/tier4_synth is a PANEL GATE, not a synth: it
       writes panel-verdict.json and MUST run the verdict gate (approval gate).
     - other *synth* nodes are informational: write the artifact_contract
       source_artifact (default synthesis.md) and never gate.
     - everything else is a classic reviewer → review-<id>.json + gate.
    Returns (review_file, is_panel_gate, is_synth)."""
    if recipe_eff == "recursive-validate-impl" and node_id == "tier4_synth":
        return os.path.join(run_dir, "panel-verdict.json"), True, False
    if "synth" in node_id:
        return os.path.join(run_dir, _synth_artifact_name(root, recipe_eff)), False, True
    return os.path.join(run_dir, f"review-{node_id}.json"), False, False


def _handle_reviewer(ctx: NodeDispatch):
    review_file, is_panel_gate, is_synth = _classify_review_node(
        ctx.recipe_eff, ctx.node_id, ctx.root, ctx.run_dir)
    review_file = ctx.declared_output_path(review_file)
    os.makedirs(os.path.dirname(review_file) or ".", exist_ok=True)
    # F2-B: per-case prompt matching bash :2739-2756. The classic reviewer gets the
    # assembled inputs (summary + verifier verdicts + diff) AND the JSON envelope —
    # without the envelope the LLM emits prose → verdict=unknown → false rollback.
    if is_panel_gate:
        prompt = (f"{ctx.prepend()}Synthesize panel verdict for: {ctx.node_desc}{ctx.learned}\n\n"
                  f"Plan:\n{ctx.plan_content}{ctx.artifact_context}\n\nWrite strict JSON to: {review_file}")
    elif is_synth:
        prompt = (f"{ctx.prepend()}Synthesize for: {ctx.node_desc}{ctx.learned}\n\n"
                  f"Plan:\n{ctx.plan_content}{ctx.artifact_context}\n\nWrite your synthesis to: {review_file}")
    else:
        reviewer_inputs = _assemble_reviewer_inputs(ctx.run_dir_eff)
        prompt = (f"{ctx.prepend()}Review the implementation for: {ctx.node_desc}{ctx.learned}\n\n"
                  f"Plan:\n{ctx.plan_content}{ctx.artifact_context}\n\n{reviewer_inputs}\n"
                  'Respond with JSON: {"verdict": "pass|fail|needs_revision", "notes": []}')
    marker = os.path.join(ctx.run_dir, f".dispatch-marker-{ctx.node_id}")
    open(marker, "w").write("")
    rc, result = ctx.dispatch(prompt)
    if rc != 0:
        fr = finish_reason_for_failure(rc, result)
        ctx.trace(ctx.node_id, "failure", "reviewer", review_file, "", fr)
        return 1, fr
    ctx.write_preserving_agent(review_file, marker, result)
    try:
        os.remove(marker)
    except OSError:
        pass
    if not ctx.publish_declared_outputs():
        ctx.trace(ctx.node_id, "failure", "reviewer", review_file, "", "artifact_contract")
        return 1, "artifact_contract"
    verdict = _extract_verdict(ctx.root, review_file)
    print(f"  [info] reviewer verdict={verdict} → {review_file}")
    vn = verdict.lower()
    if is_synth:  # true synth only — panel gate falls through to the verdict gate
        ctx.trace(ctx.node_id, "success", "reviewer", review_file, verdict, "done")
        ctx.charge()
        return 0, "done"
    if vn in _REVIEW_PASS:
        ctx.trace(ctx.node_id, "success", "reviewer", review_file, verdict, "done")
        ctx.charge()
        return 0, "done"
    fr = "verdict_revise" if vn in _REVIEW_REVISE else "verdict_fail"
    ctx.trace(ctx.node_id, "failure", "reviewer", review_file, verdict, fr)
    ctx.charge()
    return 1, fr


def _handle_transform(ctx: NodeDispatch):
    """Run a deterministic data transform between two artifact contracts.

    Transforms run inside MiniOrk, not inside a coding harness. That makes
    behavior such as anonymization reproducible and keeps sensitive routing
    metadata out of the next agent's visible input set.
    """
    if ctx.artifact_ledger is None or ctx.compiled_workflow is None:
        print(f"  [artifact] transform {ctx.node_id} has no compiled workflow", file=sys.stderr)
        return 1, "config"
    try:
        from mini_ork.workflow.transforms import execute_transform

        out_file = execute_transform(ctx.compiled_workflow, ctx.artifact_ledger, ctx.node_id)
    except Exception as exc:
        print(f"  [artifact] transform {ctx.node_id} failed: {exc}", file=sys.stderr)
        ctx.trace(ctx.node_id, "failure", "transform", "", "", "artifact_contract")
        return 1, "artifact_contract"
    if not ctx.publish_declared_outputs():
        ctx.trace(ctx.node_id, "failure", "transform", str(out_file), "", "artifact_contract")
        return 1, "artifact_contract"
    ctx.trace(ctx.node_id, "success", "transform", str(out_file), "", "done")
    return 0, "done"


def _handle_verifier(ctx: NodeDispatch):
    def _publish_success():
        if ctx.publish_declared_outputs():
            return 0, "done"
        return 1, "artifact_contract"

    # Hollow-run guard: fail before any verifier runs if the recipe declares a
    # concrete run-local artifact (absolute contract path) that is missing or
    # zero-byte. Covers the verifier_ref branch (which bypasses the canonical
    # verifier). A verifier ordered before the first implementer is a baseline
    # oracle and cannot require artifacts that do not exist until implementation.
    if (not _verifier_runs_before_implementer(ctx.workflow, ctx.node_id)
            and not _required_artifacts_ok(ctx.plan_path)):
        print("  [fail] verifier node: required artifact(s) missing or empty", file=sys.stderr)
        return 1, "error"
    artifact = ""
    try:
        ac = (json.load(open(ctx.plan_path)).get("artifact_contract") or {}) if ctx.plan_path else {}
        outs = ac.get("outputs") or [] if isinstance(ac, dict) else []
        artifact = outs[0] if outs else ""
    except Exception:
        artifact = ""
    if not artifact:
        # NEW-1: bash (:2899-2902) warns + sets error finish_reason but does NOT
        # return 1 — a verifier node with no artifact_contract outputs does not
        # fail the run. Return rc 0 to match.
        # 2026-07-27: the "informational" finish_reason='error' on a SUCCEEDED
        # node is dropped. Consumers of run_events (the libwit DSP live-feed
        # poller, run-miniork-agent.cjs) map finish_reason='error' to node
        # failure, so every verifier node on a recipe with outputs:[] rendered
        # as "failed / needs another attempt" even though it succeeded and the
        # standalone verify phase passed. Success must report success; the warn
        # line above keeps the diagnostic without poisoning machine consumers.
        print("  [warn] verifier node: no outputs in artifact_contract")
        return _publish_success()
    if ctx.verifier_ref and ctx.recipe_dir:
        script = os.path.join(ctx.recipe_dir, ctx.verifier_ref)
        if not os.path.isfile(script):
            print(f"  [fail] verifier_ref not found: {ctx.verifier_ref}", file=sys.stderr)
            return 1, "error"
        ev_dir = os.path.join(os.environ.get("MINI_ORK_RUN_DIR", ctx.run_dir), "evidence")
        os.makedirs(ev_dir, exist_ok=True)
        ev = os.path.join(ev_dir, os.path.basename(ctx.verifier_ref).replace(".sh", "") + ".log")
        rc = _run_verifier_ref(script, ev, plan_path=ctx.plan_path, artifact_path=artifact)
        # F2-B: persist evidence to verifier_<stem>.json (bash :2886-2888) so the
        # reviewer input assembly can read the typecheck/test verdicts. Before the
        # rc return so failures are visible too (a missing verifier is real signal).
        vstem = ctx.verifier_ref[len("verifiers/"):] if ctx.verifier_ref.startswith("verifiers/") else ctx.verifier_ref
        vstem = vstem[:-3] if vstem.endswith(".sh") else vstem
        persist_dir = os.environ.get("MINI_ORK_RUN_DIR", ctx.run_dir)
        if persist_dir and os.path.isfile(ev) and os.path.getsize(ev) > 0:
            try:
                shutil.copy(ev, os.path.join(persist_dir, f"verifier_{vstem}.json"))
            except OSError:
                pass
        return _publish_success() if rc == 0 else (1, "error")
    module_env = dict(os.environ)
    module_env["PYTHONPATH"] = ctx.root + (
        os.pathsep + module_env["PYTHONPATH"] if module_env.get("PYTHONPATH") else ""
    )
    rc = subprocess.run([
        sys.executable, "-m", "mini_ork.cli.verify", "--plan", ctx.plan_path,
        "--task-class", ctx.task_class, artifact,
    ], env=module_env).returncode
    return _publish_success() if rc == 0 else (1, "error")


def _handle_publisher(ctx: NodeDispatch):
    rc, finish_reason = publisher_node(
        ctx.root, ctx.run_dir_eff, ctx.db, ctx.run_id,
        ctx.recipe_eff, ctx.task_class,
        review_file=os.environ.get("REVIEW_FILE", ""),
        verdict_env=os.environ.get("VERDICT", ""),
    )
    if rc == 0 and not ctx.publish_declared_outputs():
        return 1, "artifact_contract"
    return rc, finish_reason


def _rollback_strategy(workflow_path: str) -> str:
    """The workflow's declared compensation strategy (``rollback_strategy:``
    in workflow.yaml). Empty when undeclared/unreadable — the handler then
    keeps the historical version-registry-only behavior."""
    if not workflow_path or not os.path.isfile(workflow_path):
        return ""
    try:
        import yaml  # noqa: PLC0415
        return str((yaml.safe_load(open(workflow_path)) or {}).get("rollback_strategy") or "")
    except Exception:
        return ""


def _revert_working_tree(root: str, run_dir: str) -> bool:
    """``revert_branch`` compensation (roadmap Step 1 / fix-tracker M3).

    Restore exactly the implementer's ``files_changed`` in the TARGET repo —
    never a blanket ``git checkout .``: each path is strict-child validated
    against the target toplevel (the publisher's OSS-leak guard), tracked
    files are restored via ``git checkout HEAD --``, implementer-created
    untracked files are removed. Leftover changes are reported explicitly
    (M3: "fully restore or clearly report leftover changes").
    Returns True when the tree is clean of the recorded delta afterwards.
    """
    def log(msg):
        print(msg, file=sys.stderr, flush=True)

    summary_path = os.path.join(run_dir, "implementer-summary.json") if run_dir else ""
    files: list[str] = []
    if summary_path and os.path.isfile(summary_path):
        try:
            data = json.load(open(summary_path, encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("files_changed"), list):
                files = [e for e in data["files_changed"] if isinstance(e, str) and e]
        except Exception:
            files = []
    if not files:
        log("  [rollback] revert_branch: no files_changed recorded — working tree untouched")
        return True
    target_repo = os.environ.get("MO_TARGET_CWD", "")
    if not target_repo:
        try:
            target_repo = subprocess.check_output(
                ["git", "-C", root or ".", "rev-parse", "--show-toplevel"],
                stderr=subprocess.DEVNULL).decode().strip()
        except Exception:
            target_repo = root or "."
    real_root = os.path.realpath(target_repo)
    restored, removed, rejected = [], [], []
    for raw in files:
        ap = raw if os.path.isabs(raw) else os.path.join(real_root, raw)
        real = os.path.realpath(ap)
        if real != real_root and not real.startswith(real_root + os.sep):
            rejected.append(raw)
            log(f"  [rollback] reject-revert: path escapes target repo: {raw}")
            continue
        rel = os.path.relpath(real, real_root)
        tracked = subprocess.run(
            ["git", "-C", real_root, "ls-files", "--error-unmatch", "--", rel],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
        if tracked:
            rc = subprocess.run(
                ["git", "-C", real_root, "checkout", "HEAD", "--", rel],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode
            if rc == 0:
                restored.append(rel)
            else:
                rejected.append(raw)
        else:
            # Implementer-created file (absent from HEAD): delete it.
            try:
                if os.path.isfile(real):
                    os.remove(real)
                    removed.append(rel)
            except OSError:
                rejected.append(raw)
    log(f"  [rollback] revert_branch: restored {len(restored)} tracked file(s), "
        f"removed {len(removed)} created file(s), rejected {len(rejected)}")
    # Leftover report: any of the recorded paths still dirty?
    leftover = subprocess.run(
        ["git", "-C", real_root, "status", "--porcelain", "--", *files],
        capture_output=True, text=True).stdout.strip()
    if leftover:
        log(f"  [warn] rollback: leftover changes after revert_branch:\n{leftover}")
        return False
    return True


def _handle_rollback(ctx: NodeDispatch):
    # F4: bash (:3205-3223) does a best-effort version_rollback (workflow then
    # agent), succeeds regardless of whether a prior version exists, sets
    # finish_reason=done and returns 0 — it does NOT set task_runs.status. Was:
    # set_status('rolled_back') + return 1 (a no-op that also mis-set status and
    # double-counted the failure). The upstream failure already failed the run.
    from mini_ork.registries import version_registry as _vr
    reverted = False
    for kind, name in (("workflow", ctx.recipe or "default"), ("agent", "default")):
        try:
            _vr.rollback(kind, name, db=ctx.db)
            reverted = True
            break
        except Exception:
            continue
    if not reverted:
        print("  [ok] rollback: nothing to revert (no prior promoted version)", file=sys.stderr)
    # Working-tree compensation: honor the workflow's declared strategy
    # (declared in recipes/code-fix/workflow.yaml but previously implemented
    # nowhere — fix-tracker M3). Version-registry rollback handles DB state;
    # revert_branch handles FILE state. rc contract unchanged: the rollback
    # node always succeeds and reports, it never re-fails the run.
    if _rollback_strategy(ctx.workflow) == "revert_branch":
        _revert_working_tree(ctx.root, ctx.run_dir_eff or ctx.run_dir)
    print("  [ok] rollback complete")
    # NOTE: bash traces NO rollback node (:3205-3223 has no _trace_write_node_rich).
    # Tracing it with status=success would write a spurious +1-reward execution_traces
    # row — semantically inverted (rollback fires because the run FAILED) — that
    # poisons GRPO/reflect. Deliberately no trace() here to stay faithful.
    return 0, "done"


NODE_HANDLER_REGISTRY: dict[str, Callable[[NodeDispatch], tuple[int, str]]] = {
    "researcher": _handle_researcher,
    "transform": _handle_transform,
    "implementer": _handle_implementer,
    "reviewer": _handle_reviewer,
    "verifier": _handle_verifier,
    "publisher": _handle_publisher,
    "rollback": _handle_rollback,
}


def register_node_handler(node_type: str, handler: Callable, *, phase: str = "main") -> None:
    """Register a node-type handler without editing the executor (OCP).

    phase="early" runs right after the intervention gate (planner/reflector
    semantics — no capability/watchdog gates, no prompt assembly);
    phase="main" runs after the pre-dispatch gates with a full NodeDispatch.
    """
    if phase == "early":
        EARLY_NODE_HANDLERS[node_type] = handler
    else:
        NODE_HANDLER_REGISTRY[node_type] = handler


if __name__ == "__main__":
    raise SystemExit(main())
