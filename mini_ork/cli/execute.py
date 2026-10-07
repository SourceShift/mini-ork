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
import traceback
import uuid
from dataclasses import dataclass
from typing import Callable  # noqa: F401 -- compatibility re-export for handlers

from mini_ork.context import (  # noqa: F401 -- compatibility re-exports
    ENV_DISPATCH_CHAIN,
    ENV_RESUME_SESSION_ID,
    ENV_RUN_DIR,
    ENV_TARGET_CWD,
    RunContext,
    apply_env_overrides,
    context_env,
    context_env_snapshot,
    node_env_overrides,
    publish_env,
    run_context_scope,
)


# ── extracted modules (re-exported contracts; definitions live in the modules) ──
from mini_ork.learning.writeback import (  # noqa: F401
    learning_update_conductor_outcomes,
    reward_from_status,
    write_grpo_advantages,
)
from mini_ork.dispatch.routing import (  # noqa: F401
    dispatch_chain,
    last_route_provenance,
    learning_governed_lane,
    learning_static_lane,
    policy_route_lane,
)
from mini_ork.cli.publisher import (  # noqa: F401
    _envsubst,
    _publisher_try_commit_files,
    is_rubric_prescreen,
    publisher_node,
)
from mini_ork.runtime.run_roots import (  # noqa: F401
    RunRoots,
    load_run_roots,
    persist_run_roots,
    resolve_run_roots,
)
from mini_ork.runtime.contract import run_check  # noqa: F401 -- routing helper
from mini_ork.execute_compat import normalize_implementer_summary, write_json_atomic
from mini_ork.verify import levels as _levels

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


# Reward hygiene: finish_reasons that mark an INFRA exit the agent never
# controlled — a watchdog timeout or a cost-circuit trip. A node that aborts
# for one of these never got a fair chance to produce a fix, so its low reward
# must not enter the group-relative advantage the learning loop trains on
# (see mini_ork/learning/writeback.py). These two reasons only ever accompany a
# forced abort, never a capability outcome, so keying on them alone is safe.
# Genuine capability failures (test-red / apply-fail / reviewer reject) carry
# finish_reason 'error' and stay learnable. lane-fuse (rc43) currently collapses
# into 'error' upstream and empty-text rc=0 lanes trace as success — both are a
# finer detection problem deferred to the layered-reward slice.
_NON_LEARNABLE_FINISH_REASONS = frozenset({"timeout", "cost_limit"})


def is_non_learnable_exit(finish_reason: str) -> bool:
    return (str(finish_reason) or "").lower() in _NON_LEARNABLE_FINISH_REASONS


def infer_trace_code_region(payload: str) -> str:
    """files_written → the top-level dir of the first in-repo relative file
    ('(root)' for root-level files). Verbatim transcription of the bash's
    embedded python; returns '' when nothing maps (bash prints nothing)."""
    try:
        data = json.loads(payload or "{}")
    except json.JSONDecodeError:
        return ""
    run_dir = context_env("MINI_ORK_RUN_DIR") or os.environ.get("RUN_DIR") or ""
    roots = [context_env("MO_TARGET_CWD") or "", context_env("MINI_ORK_ROOT") or "", os.getcwd()]
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
    target = context_env("MO_TARGET_CWD") or ""
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


def _emit_run_verdict(run_dir, fail_count, dispatched, *, dry_run=False, task_class=""):
    # A rehearsal must not occupy the run's own record. A nested lifecycle that
    # inherits MINI_ORK_RUN_ID shares the run dir; when the rehearsal wrote
    # verdict.json first, this guard skipped the live run and the run dir kept a
    # rehearsal of a different workflow as its outcome.
    if not (run_dir and os.path.isdir(run_dir)):
        return
    panel = os.path.join(run_dir, "panel-verdict.json")
    # A real panel gate owns the run verdict; the advisory rubric's score file
    # (same name) does not.
    if not dry_run and os.path.isfile(panel) and not is_rubric_prescreen(panel):
        return
    verdict = "fail" if fail_count > 0 else "pass"
    verdict_path = os.path.join(
        run_dir, "verdict.dryrun.json" if dry_run else "verdict.json")
    if os.path.isfile(verdict_path):
        try:
            existing = json.load(open(verdict_path, encoding="utf-8"))
        except Exception:
            existing = {}
        if isinstance(existing, dict) and existing.get("source") == "execute@run-level":
            return
        if not dry_run:
            # A recipe may own verdict.json as a detailed deliverable. Keep that
            # evidence intact and put executor bookkeeping beside it.
            verdict_path = os.path.join(run_dir, "run-verdict.json")
    # Level-vector stamp (MO_LEVEL_VECTOR=1, live only): append the five report
    # keys after the four byte-stable pairs. Knob off or dry_run keep today's
    # bytes (the byte-stability invariant tests 8 / 11 enforce).
    if _levels.enabled() and not dry_run:
        report = _levels.level_report(run_dir, task_class)
        payload = {
            "verdict": verdict,
            "failed_nodes": fail_count,
            "dispatched": dispatched,
            "source": "execute@run-level",
        }
        payload.update(report)
        try:
            open(verdict_path, "w").write(
                json.dumps(payload, separators=(",", ":"), ensure_ascii=False) + "\n")
        except OSError:
            return
        print(f"  [verdict] run-level {os.path.basename(verdict_path)}: {verdict} "
              f"(failed_nodes={fail_count}) levels_ok={str(report['levels_ok']).lower()}")
        return
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


def _recipe_root(root: str) -> str:
    """Base dir for recipe assets (register.py, workflow, artifact_contract).

    main.py threads MINI_ORK_RECIPE_ROOT when it resolves a recipe against the
    MINI_ORK_HOME overlay (a consumer symlinks a private recipe there while
    MINI_ORK_ROOT points at the primary checkout). Honor it so every recipe-
    asset read in this module matches where the recipe was found; absent (dev
    checkouts), fall back to root unchanged.
    """
    return os.environ.get("MINI_ORK_RECIPE_ROOT") or root


def _bootstrap_recipe_register(root, recipe: str) -> None:
    """Fire a recipe's optional ``register.py`` side effects in THIS process.

    A recipe may ship ``recipes/<recipe>/register.py`` to self-register artifact
    transforms and implementer submodes (e.g. goal-loop's ``goal_state_eval`` /
    ``goal_sweep_plan``). Those registrations live in per-process module globals
    (``mini_ork.workflow.transforms._TRANSFORMS``), so EVERY process that
    compiles or executes the workflow must load them — not just the parent.
    macOS ``ProcessPoolExecutor`` children are ``spawn``-ed with a fresh import
    state, so a register loaded only in ``execute.main`` is invisible to the
    child running a ``type:transform`` node, which then dies with "unknown
    artifact transform". Hence this is called BOTH in ``main`` and in
    ``_isolated_dispatch_worker``.

    Guarded on a real recipe directory so pure custom-workflow runs stay a
    no-op; ``load_recipe_register`` itself is a no-op when ``register.py`` is
    absent and is idempotent per process. A load error propagates as
    ``RecipeRegisterError``.
    """
    rbase = _recipe_root(root)
    if recipe and os.path.isdir(os.path.join(rbase, "recipes", recipe)):
        from pathlib import Path as _Path

        from mini_ork.cli.recipe_register import load_recipe_register

        load_recipe_register(_Path(os.path.join(rbase, "recipes", recipe)))


def _isolated_dispatch_worker(payload):
    """Run one native node in a process-isolated environment.

    Provider routing mutates process environment variables, so live concurrent
    nodes cannot safely share threads. The worker recreates best-effort trace
    and checkpoint writers locally and returns captured output to the parent.
    """
    (field, root, run_dir, plan_path, task_class, db, run_id,
     recipe, workflow) = payload
    # Spawned pool child: re-run the recipe register bootstrap here or a
    # type:transform node cannot resolve its recipe-local transform.
    _bootstrap_recipe_register(root, recipe)
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
        # The worker runs in a child process, so the traceback cannot cross the
        # pool boundary as a live object — capture it as a string HERE, where the
        # frames still exist, or the failing frame is lost and the crash is
        # undiagnosable from the parent's stderr.
        stderr.write(f"native parallel worker failed: {exc}\n")
        stderr.write(traceback.format_exc())
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
                publish_env({"MO_REPAIR_BUDGET_USD": f"{v:.2f}"})
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
    publish_env({"MINI_ORK_RECOVERY_ACTIVE": "1"})
    if effective_from:
        publish_env({"MINI_ORK_RECOVERY_FROM": effective_from})
    print(
        f"    recovery: from_node={effective_from or '<unset>'} "
        f"closure={len(node_ids)}/{before_count} nodes"
    )
    return node_ids, 0


def _maybe_triage_failed_run(db, run_id, home, root) -> None:
    """Attribute a failed run and queue a fix — off unless ``MO_FAILURE_TRIAGE=1``.

    Best-effort by contract: triage reads ``run_events`` and may write a bug row,
    but it must never raise into the run that just failed, so every failure here
    is logged and swallowed. Promotion of the bug to a schedulable
    ``framework-edit`` epic is a second, independent opt-in
    (``MO_FAILURE_TRIAGE_PROMOTE=1``): emitting is safe, spending budget is not.
    """
    if not run_id or context_env("MO_FAILURE_TRIAGE", "") != "1":
        return
    try:
        from mini_ork.triage.failures import triage_run

        triage_run(
            run_id,
            home=home,
            db=db,
            root=root,
            promote=context_env("MO_FAILURE_TRIAGE_PROMOTE", "") == "1",
        )
    except Exception:  # noqa: BLE001 — triage must never fail a run
        sys.stderr.write(
            f"execute: failure triage raised for {run_id}:\n{traceback.format_exc()}"
        )


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
        workflow = os.path.join(_recipe_root(root), "recipes",
                                os.environ["MINI_ORK_RECIPE"], "workflow.yaml")
    run_dir = (os.path.dirname(plan_path) if plan_path
               else (context_env("MINI_ORK_RUN_DIR") or "."))

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
    retry_edges: dict[str, tuple[str, int]] = {}
    if workflow and os.path.isfile(workflow):
        from mini_ork.workflow import compile_workflow

        compiled = compile_workflow(workflow)
        control_parents = compiled.control_parents
        retry_edges = compiled.retry_edges

        # A recipe that declares a `recursion:` block owns its own loop caps.
        # Publish them so the driver honors the declaration instead of its own
        # hand-copied default. Recipes without a block publish nothing, so they
        # stay byte-identical to before this existed.
        from mini_ork.workflow.recursion import load_recursion_config

        recursion = load_recursion_config(workflow)
        if recursion is not None:
            publish_env({
                "MO_RECURSION_MAX_ITERATIONS": str(recursion.max_iterations),
                "MO_RECURSION_CONVERGENCE_CHECK": recursion.convergence_check,
                "MO_RECURSION_BUDGET_CAP_PER_ITER_USD": f"{recursion.budget_cap_per_iter_usd:.2f}",
                "MO_RECURSION_BUDGET_CAP_TOTAL_USD": f"{recursion.budget_cap_total_usd:.2f}",
                "MO_RECURSION_DIVERGENCE_KILL": recursion.divergence_kill,
            })

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
        _emit_run_verdict(run_dir, fail_count, dispatched, dry_run=True)
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
    # Recipe-local register.py bootstrap in the PARENT (serial/in-process
    # dispatch + compile_workflow above). The process-isolated path also
    # bootstraps inside each pool child (_isolated_dispatch_worker) — a spawned
    # child does not inherit this. See _bootstrap_recipe_register.
    _bootstrap_recipe_register(root, recipe)
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

    # Remote-nodes-02 (D2): tear the run-scoped workspace session down on
    # SIGTERM / Ctrl-C too, not only via the run-level ``finally`` below. The
    # handlers chain to whatever was installed before, so a single SIGTERM still
    # terminates and Ctrl-C still raises KeyboardInterrupt — exactly as without
    # a session.
    if not dry_run and run_id:
        from mini_ork.runtime.workspace_session import install_teardown_signal_handlers

        install_teardown_signal_handlers(run_id)
        # The dispatcher pid kill_run signals (web/control.py reads it; nothing
        # had written it since the bash runtime was removed).
        try:
            with open(os.path.join(live_run_dir, ".pid"), "w", encoding="utf-8") as fh:
                fh.write(f"{os.getpid()}\n")
        except OSError:
            pass

    def _dispatch_serial(field):
        # D1: bash keeps FAIL_COUNT as a shell var visible to _mo_policy_route_lane's
        # trace_governed branch (:2014). Publish it so the port's policy_route_lane sees
        # the live prefix-failure count (else trace_governed never escalates).
        publish_env({"FAIL_COUNT": str(fail_count)})
        # The per-node isolation boundary lives ON dispatch_node
        # (_node_publish_boundary) so no caller can publish without it.
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
        publish_env({"FAIL_COUNT": str(fail_count)})
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

    # Revise-loop cap read once per run: 0 (or negative) restores the legacy
    # block-descendants-and-rollback behaviour exactly.
    revise_rounds_cap = _revise_rounds_limit()

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
        fields_by_id = {field[0]: field for field in work_fields}
        # revise round bookkeeping: target node_id -> rounds already consumed.
        round_used: dict[str, int] = {}

        def _control_descendants(target: str) -> set[str]:
            """``target`` plus every node reachable from it through control parents."""
            descendants = {target}
            frontier = [target]
            while frontier:
                current = frontier.pop()
                for node_id, field in fields_by_id.items():
                    if node_id in descendants:
                        continue
                    if current in control_parents.get(node_id, ()):
                        descendants.add(node_id)
                        frontier.append(node_id)
            return descendants

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
            outcomes = _parallel(batch)
            for field, rc, _finish_reason in outcomes:
                if rc == 0:
                    statuses[field[0]] = "success"
                else:
                    statuses[field[0]] = "failed"
                    fail_count += 1

            # ── revise loop (retries edges) ────────────────────────────────
            # A failed node that carries a `retries` edge sends its findings
            # back to the edge's target (normally the implementer) instead of
            # blocking its descendants. Group the failures by target so several
            # gates failing in one wave produce ONE combined revise round.
            revise_groups: dict[str, list[tuple[tuple, int, str]]] = {}
            for field, rc, finish_reason in outcomes:
                if rc == 0 or field[0] not in retry_edges:
                    continue
                if finish_reason == REVIEWER_VERDICT_UNPARSEABLE:
                    # Nothing to send back: the reviewer judged nothing. Re-running
                    # the implementer would spend a full round on a format failure.
                    print(f"[revise] skipped for {field[0]}: reviewer verdict unparseable "
                          "— no findings to send back", file=sys.stderr)
                    continue
                target, max_rounds = retry_edges[field[0]]
                cap = min(max_rounds, revise_rounds_cap)
                if round_used.get(target, 0) >= cap:
                    continue
                revise_groups.setdefault(target, []).append((field, rc, finish_reason))
            if revise_groups:
                for target, failed_sources in revise_groups.items():
                    _target_max = retry_edges[failed_sources[0][0][0]][1]
                    round_no = round_used.get(target, 0) + 1
                    round_used[target] = round_no
                    reset_ids = _control_descendants(target)
                    feedback_path = _write_revise_feedback(
                        live_run_dir, round_no, _target_max, failed_sources)
                    _archive_revise_round(
                        live_run_dir, round_no,
                        [fields_by_id[node_id] for node_id in reset_ids])
                    _write_revise_current(
                        live_run_dir, round_no, _target_max, feedback_path)
                    for node_id in reset_ids:
                        prior = statuses.pop(node_id, None)
                        if prior in {"failed", "blocked"}:
                            fail_count -= 1
                        pending[node_id] = fields_by_id[node_id]
                    source_names = ", ".join(field[0] for field, _rc, _fr in failed_sources)
                    print(
                        f"[revise] round {round_no}/{_target_max}: {source_names} found "
                        f"problems → re-running {target} with feedback ({feedback_path})",
                        file=sys.stderr,
                    )

    dependency_aware = any(
        control_parents.get(field[0]) for field in work_fields
    )
    speculative_requested = dispatch_mode == "speculative" or any(
        field[4] == "speculative" for field in work_fields
    )

    # Remote-nodes-02 (D2): wrap the per-node dispatch in a finally that closes
    # the run-scoped workspace session exactly once. SIGTERM triggers the
    # handler above (which closes too) AND a default second-SIGTERM exit; a
    # KeyboardInterrupt from a worker thread falls through here.
    try:
        if not dry_run and run_id and context_env("MO_PLACEMENT", "").strip().lower() == "remote" \
                and not _provision_remote_session(run_id, live_run_dir, db):
            set_status(db, run_id, "failed")
            _maybe_triage_failed_run(db, run_id, home, root)
            return 1
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
        _emit_run_verdict(live_run_dir, fail_count, len(fields_list), task_class=task_class)
        _post_run_learning(db, live_run_dir, run_id, task_class, fail_count=fail_count)
        if fail_count > 0:
            set_status(db, run_id, "failed")
            _maybe_triage_failed_run(db, run_id, home, root)
            sys.stderr.write(f"execute: {fail_count} node(s) failed\n")
            return 1
        print("\nexecute: all nodes complete")
        return 0
    finally:
        # Remote-nodes-02: tear down the run-scoped workspace session here so
        # a SIGTERM, a worker-thread KeyboardInterrupt, or a normal exit all
        # converge on the same single ``down()`` per run. ``close_run_session``
        # is idempotent and never raises — see workspace_session.py.
        if not dry_run and run_id:
            try:
                os.unlink(os.path.join(live_run_dir, ".pid"))
            except OSError:
                pass
            try:
                from mini_ork.runtime.workspace_session import close_run_session

                # Under `mini-ork run` the lifecycle owns a remote session (its
                # later steps check on the node) and releases it at the end.
                close_run_session(run_id, keep_remote=context_env("MO_REMOTE_SESSION_SCOPE") == "lifecycle")
            except Exception as exc:  # noqa: BLE001
                print(
                    f"[warn] run-level close_run_session failed: {exc}",
                    file=sys.stderr,
                )


def _provision_remote_session(run_id: str, run_dir: str, db) -> bool:
    """remote-nodes-14 §3: bring a ``--placement remote`` run's session up
    BEFORE the first node, so a full node, a failed setup or a failed sync
    surfaces before any LLM spend and the ``remote.setup.step`` checklist
    precedes every ``node_start``. The dispatch path later reuses the same
    registered session. False (after reporting why) fails the run
    ``remote_unavailable``."""
    from mini_ork.runtime.workspace_session import get_run_session

    env = {**context_env_snapshot(), "MINI_ORK_RUN_ID": run_id, "MINI_ORK_RUN_DIR": run_dir}
    try:
        get_run_session(run_id, "remote", env=env)
    except Exception as exc:  # noqa: BLE001 — any provisioning failure ends the run here
        print(f"execute: remote_unavailable: {exc}", file=sys.stderr)
        try:
            from mini_ork.observability.node_events import mo_node_emit
            mo_node_emit(run_id, "remote-workspace", "workspace", "remote.run.failed",
                         json.dumps({"failure_class": "remote_unavailable",
                                     "error": str(exc)[:500]}), db=db)
        except Exception:  # noqa: BLE001 — the event is advisory
            pass
        return False
    return True


# ── post-run learning side-channels ──


def _post_run_learning(db, run_dir, run_id, task_class="", fail_count=None):
    """Post-run learning side-channels (each best-effort; never fail the run):

    0. implementer run-verified stamp (opt-in via MO_IMPL_REWARD=run_verified).
       Overwrites the per-row reward on the run's implementer rows with
       ``run_verified@v1`` = (1.0 if fail_count==0 else 0.0) - lam*cost/ref.
       Runs BEFORE step 1 so the rubric fill-ONLY gate doesn't clobber it
       and step 3's advantage recompute sees the stamped values in the same
       run. If ``fail_count`` is None the stamp is skipped (the call site
       was made before the dispatch loop finished counting).
    1. rubric grading — fill-ONLY: a per-node reward already on the row
       (status-anchored stamp or eval@v1) encodes within-run lane
       differentiation a uniform rubric value would erase.
    2. conductor outcome reconciliation + GRPO writeback (the global APM face).
    3. region/domain advantage recompute. preferred_lane() reads
       region → domain → global, but the region/domain tables were refreshed
       exclusively inside `mini-ork reflect`, so between reflects the router
       rode stale slice advantages (live DB: region frozen 2026-07-19 while
       APM updated per-run). Full-history window is one cheap EMA step over
       the reward-bearing rows — the same shape reflect runs at since=0.
    4. auto-apply sweep (task #19, AutoSaddler 2608.23041) — bounded top-1
       gradient per agent-prompt target, gated through the same apply_run.
       Opt-in twice (MO_AUTO_APPLY=1 AND MO_APPLY_ENABLED=1): a sweep without
       the master gate would audit but never write, which is pure noise.
    """
    if (os.environ.get("MO_IMPL_REWARD", "") == "run_verified"
            and fail_count is not None):
        try:
            from mini_ork.learning import writeback as _wb_impl_reward  # noqa: PLC0415
            _wb_impl_reward.stamp_impl_run_verified(
                db, run_id, verified=(fail_count == 0))
        except Exception:
            pass
    if os.environ.get("MO_GRADE_RUN_REWARD", "1") == "1":
        try:
            from mini_ork import trace_store  # noqa: PLC0415
            trace_store.grade_run_reward(run_dir, run_id, db=db)
        except Exception:
            pass
    if os.environ.get("MO_LEARNING_WRITEBACK", "1") == "1":
        try:
            learning_update_conductor_outcomes(db)
            write_grpo_advantages(db)
        except Exception:
            pass
    if os.environ.get("MO_LANE_ROUTER", "1") != "0":
        try:
            from mini_ork import lane_router  # noqa: PLC0415
            lane_router.recompute_advantages(since=0, db=db)
        except Exception:
            pass
    if (os.environ.get("MO_AUTO_APPLY", "0") == "1"
            and os.environ.get("MO_APPLY_ENABLED", "0") == "1"
            and task_class):
        try:
            from mini_ork.cli import apply as _apply  # noqa: PLC0415
            _apply.auto_sweep(task_class, db=db)
        except Exception:
            pass


# These are the deterministic operations _dispatch_node's live (non-dry-run)
# branches wire around the LLM call: DB status/cost writes and the "capture
# coin-flip" output applier. Ported + parity-gated ahead of the live routing
# (whose LLM dispatch is integration territory).
#
# ── per-node live-path support helpers (deterministic; increment 4) ──

def set_status(db, run_id, new_status, *, dry_run=False):
    """Retrying task_runs status write; terminal states stamp ended_at + duration_ms.

    Raises ``RuntimeError`` when the write cannot be made: after the retries for
    a locked/busy DB, at once for anything else (a missing table does not heal
    by waiting). It used to print ``[warn]`` and return, and a terminal status
    that never landed left the run reading as in flight forever (K0 root cause
    #1, zero-fallback)."""
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
            msg = str(e).lower()
            if "locked" not in msg and "busy" not in msg:
                break  # not transient — retrying cannot help
            time.sleep(0.5 * (attempt + 1))
    if last_err is not None:
        raise RuntimeError(f"set_status({new_status!r}) for {run_id} could not be written: {last_err}") from last_err


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
            # Upper bound is a garbage-value guard, not a plausibility clamp:
            # the old `v < 10` silently re-billed every node whose real bill
            # hit $10+ at $0.01 — on the live DB that zeroed ~$425 of June
            # 2026 giant-context runs from task_runs (run-1781280905: $40.79
            # real, $0.62 recorded). $1000 keeps the guard against corrupt
            # sidecars while admitting any legitimate per-node bill.
            if 0 < v < 1000:
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
            publish_env({"MO_NODE_FINISH_REASON": "paused_for_approval"})
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


VERIFIER_PRODUCED_ARTIFACTS = frozenset({"verdict.json"})


def _required_artifacts_ok(plan_path, *, skip_verifier_outputs=False) -> bool:
    """Hollow-run guard for the verifier node (parity: bash _mo_required_artifacts_ok).
    A recipe that declares a concrete, run-local artifact (an ABSOLUTE, env-expanded
    artifact_contract path such as ``${MINI_ORK_RUN_DIR}/framework-edit.diff``) but
    produces nothing — missing OR zero-byte — fails. Relative canonical outputs are
    publish-targets (exempt), so a genuine artifact is never false-failed. Returns
    True when all required artifacts exist + are non-empty (or none apply).

    ``skip_verifier_outputs`` exempts artifacts the verifiers THEMSELVES write
    (``verdict.json``). Checked before the verifier runs, requiring them is
    circular: every framework-edit build failed both verifier nodes on its own
    not-yet-written verdict, skipped the reviewer and rolled back (6/6 builds,
    2026-10-05). The verifier handler re-runs the full check after the scripts,
    so a verifier that never writes its verdict still fails."""
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
            if skip_verifier_outputs and os.path.basename(p) in VERIFIER_PRODUCED_ARTIFACTS:
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


def _verifier_argv(script):
    """Extension-native verifier dispatch: ``.py`` runs under the current
    interpreter; ``.sh`` keeps working via bash (user-facing contract) with a
    one-line deprecation warning; anything else keeps legacy bash behavior."""
    if script.endswith(".py"):
        return [sys.executable, script]
    if script.endswith(".sh"):
        print(f"warning: verifier '{script}' is a bash script — .sh verifiers are deprecated, port to .py",
              file=sys.stderr)
    return ["bash", script]


def _run_verifier_ref(script, evidence_path, *, plan_path="", artifact_path="", cwd=None, run_dir=""):
    """Run the verifier script, capture it, and treat {"pass": true} as success.

    Routes the subprocess through ``mini_ork.runtime.contract.run_check``
    so the same call site serves both the local placement (legacy
    ``subprocess.run`` byte-identical) and remote placement (epic
    remote-nodes-11): under remote placement the verifier runs against
    the replica, the cwd + env + argv reach the run's PathMap, the
    evidence lands in the run-dir and arrives through the pull (the
    fallback path writes it directly when the pull missed it).
    """
    if not cwd:
        # Prefer the pinned target from run_profile.json["roots"]; fall back
        # to MO_TARGET_CWD for legacy run dirs created without the record.
        roots = load_run_roots(run_dir) if run_dir else None
        cwd = (roots.target if roots else (context_env("MO_TARGET_CWD") or os.getcwd()))
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
    # A ``.py`` verifier runs in the TARGET repo, where ``mini_ork`` resolves to
    # whatever the interpreter finds (a venv editable install, or nothing in a
    # vendored install) — not necessarily the engine running this executor. Live
    # smoke (2026-10-05): the code-fix verifier imported a stale checkout, so the
    # new suite-adequacy audit abstained as `module-unavailable`. Put the
    # executing engine first so verifiers judge with the code that dispatched them.
    engine_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
    inherited = verifier_env.get("PYTHONPATH", "")
    verifier_env["PYTHONPATH"] = (engine_root + os.pathsep + inherited) if inherited else engine_root
    # ``run_check`` handles the local-vs-remote routing; the legacy
    # ``subprocess.run(..., stdout=fh, stderr=STDOUT)`` shape is preserved
    # on the local branch. Under remote placement, the helper writes the
    # merged output to ``evidence_path`` from the run-dir pull's content
    # (falling back to the exec'd ``output`` string when the pull missed).
    argv = _verifier_argv(script)
    started_at = time.time()
    rc, _ = run_check(argv, cwd=cwd, env=verifier_env,
                      evidence_path=evidence_path)
    ended_at = time.time()
    # Best-effort command recording (kickoff ide-node-commands §1). Inlined
    # to keep the module surface flat — no top-level name beyond
    # ``_run_verifier_ref`` itself. Writes <run_dir>/node-cmd/<stem>.json
    # with argv/cmd/cwd/env/rc/timing so the IDE's stream view can render
    # the full command and its output for non-agent nodes. Must never
    # raise (caller's rc and evidence bytes are byte-identical to the
    # legacy path); only the whitelisted env keys are projected.
    try:
        import shlex as _shlex  # noqa: WPS433 — local to keep shlex off the module surface
        _record_env_whitelist = (
            "MINI_ORK_PLAN_PATH",
            "ARTIFACT_PATH",
            "MINI_ORK_RUN_DIR",
            "MINI_ORK_RUN_ID",
            "MO_TARGET_CWD",
            "PYTHONPATH",
        )
        target_run_dir = run_dir or os.path.dirname(evidence_path) or ""
        if target_run_dir:
            ev_stem = os.path.basename(evidence_path or "")
            for sfx in (".log", ".json"):
                if ev_stem.endswith(sfx):
                    ev_stem = ev_stem[: -len(sfx)]
                    break
            if ev_stem.startswith("verifier_"):
                ev_stem = ev_stem[len("verifier_") :]
            if ev_stem:
                record_dir = os.path.join(target_run_dir, "node-cmd")
                record_path = os.path.join(record_dir, f"verifier_{ev_stem}.json")
                safe_env = {k: verifier_env[k] for k in _record_env_whitelist if k in verifier_env}
                _record_payload = {
                    "script": script,
                    "argv": [str(a) for a in argv],
                    "cmd": _shlex.join([str(a) for a in argv]),
                    "cwd": cwd or "",
                    "env": safe_env,
                    "started_at": float(started_at),
                    "ended_at": float(ended_at),
                    "rc": int(rc) if rc is not None else None,
                    "output_path": evidence_path or "",
                }
                try:
                    os.makedirs(record_dir, exist_ok=True)
                except OSError:
                    pass
                else:
                    try:
                        write_json_atomic(record_path, _record_payload)
                    except OSError:
                        # Read-only filesystem, permission denied, etc. —
                        # silently no-op, never raise: this is a sidecar,
                        # not a control flow input.
                        pass
    except Exception:
        pass
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
        model = context_env("MO_DISPATCH_CHAIN") or node_type
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
    artifact_contract.yaml `source_artifact` (default synthesis.md).

    For a reviewer/synth node this MUST resolve to a single output filename.
    A list-valued `source_artifact` (the stale D-037 "input staging" form) has
    no single-file meaning here; joining it onto a path yields an opaque
    `TypeError` deep in posixpath — and since the reviewer node runs in a
    ProcessPool child, that traceback never reaches the parent. Reject a
    non-string value explicitly, naming the recipe + key, so the misconfig is
    diagnosable at its source instead of surfacing as "N node(s) failed".
    """
    default = "synthesis.md"
    contract = (os.path.join(_recipe_root(root), "recipes", recipe, "artifact_contract.yaml")
                if recipe else "")
    if not contract or not os.path.isfile(contract):
        return default
    try:
        import yaml  # noqa: PLC0415 — lazy, matches bash's inline python
        d = yaml.safe_load(open(contract, encoding="utf-8")) or {}
        value = d.get("source_artifact") if isinstance(d, dict) else None
    except Exception:
        return default
    if not value:
        return default
    if not isinstance(value, str):
        raise ValueError(
            f"source_artifact in recipes/{recipe}/artifact_contract.yaml must be a "
            f"single filename string for a reviewer/synth recipe, got "
            f"{type(value).__name__}: {value!r}. The synthesizer writes ONE file "
            f"into $MINI_ORK_RUN_DIR; name that file here (e.g. chapter-review.json)."
        )
    return value


def _resolve_target_cwd(run_dir_eff):
    """Port of bash _dispatch_node:2633-2641. Derive the implementer edit-surface cwd
    from an explicit valid $MO_TARGET_CWD, otherwise the run kickoff's git-toplevel.
    This is the CWT-A corruption fix — pins codex to the TARGET repo, not MINI_ORK_ROOT.

    Thin wrapper over :func:`mini_ork.runtime.run_roots.resolve_run_roots`. The
    precedence ladder lives there so it can be reused, persisted, and parity-tested.
    """
    return resolve_run_roots(run_dir_eff).target


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
_PRE_IMPL_FIXTURE_DIR = "pre-impl-fixture"
_PRE_IMPL_MANIFEST = "MANIFEST.json"


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
    # Pin the run's four roots (target, run_dir, home, engine) into
    # run_profile.json["roots"] at the earliest run_dir-using boundary, BEFORE
    # the cwd snapshot below. Idempotent: a resumed run keeps its original
    # roots even when MO_TARGET_CWD has since changed.
    persist_run_roots(run_dir)
    roots = load_run_roots(run_dir)
    cwd = (roots.target if roots else (context_env("MO_TARGET_CWD") or os.getcwd()))
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
            run_id = os.path.basename(run_dir.rstrip(os.sep))
            subprocess.run(
                ["git", "-C", cwd, "update-ref", f"refs/mo/pre-impl/{run_id}", ref],
                capture_output=True,
            )
        # `git stash create` snapshots TRACKED state only. Record the untracked
        # inventory too, so the ground-truth harvest can tell implementer-created
        # files apart from pre-existing untracked dirt (a node_modules symlink,
        # another session's scratch files).
        unt = subprocess.run(["git", "-C", cwd, "ls-files", "-z", "--others",
                              "--exclude-standard"],
                             capture_output=True, text=True,
                             errors="surrogateescape")
        if unt.returncode == 0:
            # -z: unquoted names, so non-ASCII paths match the harvest's list.
            with open(os.path.join(run_dir, "pre-implementer-untracked"), "w",
                      errors="surrogateescape") as fh:
                fh.write("".join(p + "\n" for p in unt.stdout.split("\0") if p))
    except Exception:
        pass


def _harvest_framework_edit_ground_truth(run_dir, target):
    """framework-edit's implementer self-writes framework-edit.diff, and nothing
    validated that artifact against reality. A hallucinating agent
    (run-1788369968-8009-se2) emitted a corrupt diff — garbage hunk context,
    already-landed files re-declared as new creations — AND fabricated its
    verification claims ("git apply --check → EXIT=0"), burning the whole
    verifier+reviewer wave before the lie surfaced.

    The working tree is the only witness that cannot lie. After
    apply_impl_output, re-harvest the REAL delta (tracked diff vs the
    pre-implementer baseline + implementer-created untracked files) and REWRITE
    framework-edit.diff from it: the artifact then always applies, the static
    gate's reverse-check is trivially true, and dispatcher landing is exact.
    If the tree is untouched, a diff-only agent is still legitimate — try
    applying its artifact ONCE; if that also fails, fail the implementer node
    immediately instead of five nodes later.

    Returns (ok, finish_reason). ok=True with "" when the harvest succeeded or
    the guard is inapplicable (no run_dir / not a git repo / infra error —
    verifiers remain the downstream net for the legacy artifact in that case).
    """
    if not run_dir or not target:
        return True, ""
    try:
        if subprocess.run(["git", "-C", target, "rev-parse", "--git-dir"],
                          capture_output=True).returncode != 0:
            return True, ""
    except Exception:
        return True, ""

    baseline = ""
    ref_path = os.path.join(run_dir, "pre-implementer-ref")
    if os.path.isfile(ref_path):
        try:
            baseline = open(ref_path).read().strip()
        except OSError:
            baseline = ""
    baseline_untracked = set()
    unt_path = os.path.join(run_dir, "pre-implementer-untracked")
    if os.path.isfile(unt_path):
        try:
            with open(unt_path, errors="surrogateescape") as fh:
                baseline_untracked = {line for line in fh.read().splitlines() if line}
        except OSError:
            baseline_untracked = set()

    if baseline and subprocess.run(
            ["git", "-C", target, "cat-file", "-e", f"{baseline}^{{commit}}"],
            capture_output=True).returncode != 0:
        # The baseline was captured in a different tree than the one being
        # harvested, so diffing against it is meaningless. Say so loudly.
        print(f"  [ground-truth] baseline {baseline[:12]} is not a commit in "
              f"{target}; keeping agent artifact", file=sys.stderr)
        return True, ""

    # surrogateescape: a non-UTF-8 byte in the tree must round-trip into the
    # diff file unchanged, not raise and silently fall back to the agent's diff.
    _txt = {"capture_output": True, "text": True, "errors": "surrogateescape"}

    def _delta():
        # Diff against a COMMIT-ish, never the index: agents sometimes
        # `git add` inside the target, which would blank a plain `git diff`.
        # --no-renames: a move appears as its own delete + create, so the
        # in-place rollback can name (and restore) both sides of it.
        tracked = subprocess.run(
            ["git", "-C", target, "diff", "--no-color", "--no-renames",
             "--full-index", "--binary", baseline or "HEAD"],
            timeout=300, **_txt)
        if tracked.returncode != 0:
            return None
        parts = [tracked.stdout] if tracked.stdout.strip() else []
        now = subprocess.run(
            ["git", "-C", target, "ls-files", "-z", "--others", "--exclude-standard"],
            timeout=60, **_txt)
        for rel in (now.stdout or "").split("\0"):
            if not rel or rel in baseline_untracked:
                continue
            if rel.startswith(".mini-ork/"):
                continue  # run-mirror evidence, never product changes
            if not os.path.isfile(os.path.join(target, rel)):
                continue
            new = subprocess.run(
                ["git", "-C", target, "diff", "--no-color", "--binary",
                 "--no-index", "--", "/dev/null", rel],
                timeout=60, **_txt)
            # --no-index exits 1 when files differ — that IS the new-file diff.
            if new.returncode in (0, 1) and new.stdout.strip():
                parts.append(new.stdout)
        return "".join(parts)

    diff_path = os.path.join(run_dir, "framework-edit.diff")
    try:
        delta = _delta()
    except Exception as exc:
        print(f"  [ground-truth] harvest error ({exc}); keeping agent artifact",
              file=sys.stderr)
        return True, ""
    if delta is None:
        print("  [ground-truth] git diff failed; keeping agent artifact",
              file=sys.stderr)
        return True, ""

    if not delta.strip():
        if os.path.isfile(diff_path) and os.path.getsize(diff_path) > 0:
            applied = subprocess.run(
                ["git", "-C", target, "apply", "--whitespace=nowarn", diff_path],
                capture_output=True, text=True, timeout=120)
            if applied.returncode != 0:
                err = "; ".join((applied.stderr or "").strip().splitlines()[:4])
                print("  [ground-truth] FAIL: tree untouched and the agent's "
                      f"framework-edit.diff does not apply: {err}",
                      file=sys.stderr)
                return False, "impl_diff_unusable"
            print("  [ground-truth] tree untouched; agent diff applied cleanly")
            try:
                delta = _delta() or ""
            except Exception:
                delta = ""
        if not delta or not delta.strip():
            print("  [ground-truth] FAIL: implementer produced no tree changes",
                  file=sys.stderr)
            return False, "impl_no_changes"

    agent_diff = ""
    if os.path.isfile(diff_path):
        try:
            with open(diff_path, errors="surrogateescape") as fh:
                agent_diff = fh.read()
        except OSError:
            agent_diff = ""
    if agent_diff and agent_diff != delta:
        try:
            with open(diff_path + ".agent", "w", errors="surrogateescape") as fh:
                fh.write(agent_diff)
        except OSError:
            pass
    with open(diff_path, "w", errors="surrogateescape") as fh:
        fh.write(delta)
    n_files = len(re.findall(r"^diff --git ", delta, flags=re.M))
    print(f"  [ground-truth] framework-edit.diff rewritten from tree delta "
          f"({n_files} files)")
    return True, ""


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


def _write_implementer_summary(run_dir, target, impl_log, *, since_mtime=None):
    """Materialize the implementer-summary.json the publisher's commit gate reads.

    Only self-migrate had a writer, so every other recipe (code-fix included) left
    the file absent — publisher._publisher_try_commit_files then found no
    files_changed and skipped the commit, meaning a passing run could never
    publish. Derive the list from the working tree instead of trusting the model's
    self-report, scoped by <run_dir>/pre-implementer-ref so a concurrent session's
    pre-existing dirt is never swept into the commit (the same baseline
    _assemble_reviewer_inputs uses for the reviewer diff). Untracked files are
    included because a newly created file is exactly the kind of change the commit
    must carry; anything under run_dir is excluded so run sidecars (which may live
    inside an in-place target tree) can never be committed.

    An agent-written summary already at that path is merged, not overwritten
    (see the merge below); ``since_mtime`` is the dispatch-start time, and an
    older file is treated as absent.

    Returns the derived file list, or None when it could not be derived (no
    run_dir/target, or a git command failed) — callers must not read None as
    "the implementer changed nothing".
    """
    if not run_dir or not target:
        return None
    baseline = ""
    ref_path = os.path.join(run_dir, "pre-implementer-ref")
    if os.path.isfile(ref_path):
        try:
            baseline = open(ref_path, encoding="utf-8").read().strip()
        except OSError:
            baseline = ""
    under_run_dir = os.path.realpath(run_dir)
    # Untracked files that already existed before the implementer ran are not its
    # changes unless it touched them after the snapshot: a docs run whose prompt
    # was "hi" otherwise reported the project's pre-existing .gitignore as changed.
    pre_untracked: set[str] = set()
    snap_mtime = None
    snap_path = os.path.join(run_dir, "pre-implementer-untracked")
    if os.path.isfile(snap_path):
        try:
            with open(snap_path, encoding="utf-8") as fh:
                pre_untracked = {line.strip() for line in fh if line.strip()}
            snap_mtime = os.path.getmtime(snap_path)
        except OSError:
            pre_untracked, snap_mtime = set(), None
    files: list[str] = []
    derived = True
    try:
        args = ["git", "-C", target, "diff", "--name-only"]
        if baseline:
            args.append(baseline)
        rels: list[str] = []
        untracked_argv = ["git", "-C", target, "ls-files", "--others", "--exclude-standard"]
        for argv in (args, untracked_argv):
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=15)
            if proc.returncode == 0:
                for line in proc.stdout.splitlines():
                    rel = line.strip()
                    if not rel:
                        continue
                    if argv is untracked_argv and rel in pre_untracked and snap_mtime is not None:
                        try:
                            if os.path.getmtime(os.path.join(target, rel)) <= snap_mtime:
                                continue
                        except OSError:
                            continue
                    rels.append(rel)
            else:
                derived = False
        for rel in rels:
            full = os.path.join(target, rel)
            real = os.path.realpath(full)
            if real == under_run_dir or real.startswith(under_run_dir + os.sep):
                continue
            if real not in files:
                files.append(real)
    except Exception:
        files = []
        derived = False
    payload = {
        "status": "implemented" if (files or not derived) else "no_changes",
        "worktree_path": target,
        "files_changed": files,
        "implementation_log": impl_log,
    }
    summary_path = os.path.join(run_dir, "implementer-summary.json")
    # Merge into the summary the agent wrote instead of clobbering it: the
    # recursive-validate-impl prompt asks for {ready_for_tier1, touched_files},
    # and overwriting that with this payload is what left tier1 reading
    # ready_for_tier1=False / touched_files=[] (SDD K2-K4). Engine keys stay
    # git-derived; the agent's other keys survive; normalization fills the
    # canonical tier keys. since_mtime drops a summary left by an earlier
    # recursion iteration so its touched_files cannot outlive the change.
    merged = _fresh_agent_summary(summary_path, since_mtime)
    merged.update(payload)
    write_json_atomic(summary_path, normalize_implementer_summary(merged))
    return files if derived else None


def _fresh_agent_summary(path, since_mtime):
    """The JSON object at ``path`` when it is valid and (if ``since_mtime`` is
    given) written at or after it; otherwise an empty dict."""
    try:
        if since_mtime is not None and os.path.getmtime(path) < since_mtime:
            return {}
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _capture_pre_impl_fixture(run_dir, target):
    """Snapshot a minimal, git-sourced fixture of the implementer's pre-edit
    state so a later probe harvest can vendor a small, self-contained
    reproduction (the shape ``recipes/code-fix/probes/fixtures/<stem>/``
    expects). Nothing produces that shape today.

    Every byte in the fixture comes from git (``git show <baseline>:<relpath>``)
    or from an existing file under run_dir — never model output. A
    model-authored fixture is a model-authored test, the exact failure this
    capture exists to prevent. Idempotent: the first capture wins (a revision
    loop can call this more than once per run).
    """
    if not run_dir or not target:
        return
    manifest_path = os.path.join(run_dir, _PRE_IMPL_FIXTURE_DIR, _PRE_IMPL_MANIFEST)
    if os.path.isfile(manifest_path):
        return
    ref_path = os.path.join(run_dir, "pre-implementer-ref")
    if not os.path.isfile(ref_path):
        return
    try:
        baseline = open(ref_path, encoding="utf-8").read().strip()
    except OSError:
        baseline = ""
    if not baseline:
        return

    args = ["git", "-C", target, "diff", "--name-only"]
    if baseline:
        args.append(baseline)
    rels: list[str] = []
    try:
        for argv in (args, ["git", "-C", target, "ls-files", "--others", "--exclude-standard"]):
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=15)
            if proc.returncode == 0:
                rels.extend(line.strip() for line in proc.stdout.splitlines() if line.strip())
    except Exception:
        rels = []
    changed: list[str] = []
    seen: set[str] = set()
    for rel in rels:
        if rel not in seen:
            seen.add(rel)
            changed.append(rel)

    profile_path = os.path.join(run_dir, "run_profile.json")
    task_class = ""
    verification_command: list = []
    kickoff_path = ""
    if os.path.isfile(profile_path):
        try:
            with open(profile_path, encoding="utf-8") as handle:
                profile = json.load(handle)
            if isinstance(profile, dict):
                task_class = profile.get("task_class") or ""
                vc = profile.get("verification_command")
                if isinstance(vc, list):
                    verification_command = vc
                kickoff_path = profile.get("kickoff_path") or ""
        except Exception:
            pass

    files_dir = os.path.join(run_dir, _PRE_IMPL_FIXTURE_DIR, "files")
    created_by_run: list[str] = []
    for rel in changed:
        proc = subprocess.run(
            ["git", "-C", target, "show", f"{baseline}:{rel}"],
            capture_output=True,
        )
        if proc.returncode == 0:
            dst = os.path.join(files_dir, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with open(dst, "wb") as handle:
                handle.write(proc.stdout)
        else:
            created_by_run.append(rel)

    manifest = {
        "run_id": os.path.basename(run_dir.rstrip(os.sep)),
        "task_class": task_class,
        "target_repo": target,
        "baseline_ref": baseline,
        "changed_files": changed,
        "verification_command": verification_command,
        "kickoff_path": kickoff_path,
        "created_by_run": created_by_run,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    os.makedirs(os.path.dirname(manifest_path), exist_ok=True)
    with open(manifest_path, "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2)
        handle.write("\n")


def _review_pathspecs(worktree, files):
    """Turn declared ``files_changed`` into git pathspecs relative to the repo root.

    ``files_changed`` is recorded ABSOLUTE (the publisher's commit gate and
    ``_revert_branch`` both consume that shape), but git accepts a pathspec only
    relative to the repo root — a bare absolute path matches NOTHING and still
    exits 0. That silent miss produced a 0-byte ``review-diff.patch``, so the
    reviewer was handed "(no diff)" and passed a run whose edit it never saw.
    Entries resolving outside the repo (a generated or symlinked directory such as
    ``baml_client`` pointing at a neighbouring checkout) are dropped: they cannot
    name a path inside this tree.
    """
    if not worktree or not files:
        return []
    try:
        top = subprocess.run(
            ["git", "-C", worktree, "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=15)
    except Exception:
        return []
    if top.returncode != 0 or not top.stdout.strip():
        return []
    root = os.path.realpath(top.stdout.strip())
    specs: list[str] = []
    for entry in files:
        raw = str(entry).strip()
        if not raw or raw.startswith(":"):  # ':' is pathspec magic, not a path
            continue
        try:
            absolute = raw if os.path.isabs(raw) else os.path.join(worktree, raw)
            # Resolve the PARENT only, never the leaf: a symlinked entry whose own
            # path is inside the tree stays in the pathspec even though its target
            # is elsewhere, while an entry that already points outside is dropped.
            parent = os.path.realpath(os.path.dirname(absolute))
            candidate = os.path.join(parent, os.path.basename(absolute))
        except Exception:
            continue
        if candidate != root and not candidate.startswith(root + os.sep):
            continue
        rel = os.path.relpath(candidate, root)
        if rel and rel != "." and not rel.startswith(os.pardir) and rel not in specs:
            specs.append(rel)
    return specs


def _tree_has_no_change(worktree, baseline, specs):
    """True when git sees neither a tracked delta nor an untracked file.

    Mirrors how ``_write_implementer_summary`` builds ``files_changed`` (tracked
    diff + untracked list), so a declared change that neither surface can see is a
    genuine no-op rather than a capture failure. An unreadable git state returns
    False — never assert a no-op that cannot be proven.
    """
    selector = ["--", *specs] if specs else []
    for argv in (["diff", "--name-only"], ["ls-files", "--others", "--exclude-standard"]):
        args = ["git", "-C", worktree, *argv]
        if argv[0] == "diff" and baseline:
            args.append(baseline)
        args += selector
        try:
            proc = subprocess.run(args, capture_output=True, text=True, timeout=15)
        except Exception:
            return False
        if proc.returncode != 0:
            return False
        if any(line.strip() for line in proc.stdout.splitlines()):
            return False
    return True


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
        # Prefer the pinned target from run_profile.json["roots"]; fall back
        # to MO_TARGET_CWD for legacy run dirs without the record.
        roots = load_run_roots(run_dir) if run_dir else None
        worktree = (roots.target if roots else (context_env("MO_TARGET_CWD") or os.getcwd()))
    specs = _review_pathspecs(worktree, files)
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
            # Never narrow the delta to nothing: when every declared path resolved
            # outside the tree there is no pathspec to scope by, and capturing the
            # whole delta is strictly wider than reporting a silent 0 bytes.
            if specs:
                args += ["--", *specs]
            with open(diff_path, "w") as fh:
                subprocess.run(args, stdout=fh, stderr=subprocess.DEVNULL)
        if not (os.path.isfile(diff_path) and os.path.getsize(diff_path) > 0):
            open(diff_path, "w").close()
    except Exception:
        open(diff_path, "w").close()

    # A declared change the reviewer cannot see is a no-op, not an abstention.
    # Left alone the reviewer falls through to the ambient worktree state and
    # passes on work this run never did — observed live: a code-fix child reported
    # pass while the tree stayed at its pre-run commit. Recorded as a run-local
    # marker so the reviewer node can block deterministically, before any spend,
    # instead of asking the model to notice an absence.
    no_op = False
    if files and not (os.path.isfile(diff_path) and os.path.getsize(diff_path) > 0):
        no_op = _tree_has_no_change(worktree, baseline, specs)
    noop_marker = os.path.join(run_dir, "review-diff-noop.json")
    try:
        if not no_op and os.path.isfile(noop_marker):
            # A marker left by an earlier attempt of this run (e.g. a recover
            # that ran before the work was restored) must not block this one.
            os.unlink(noop_marker)
        if no_op:
            with open(noop_marker, "w",
                      encoding="utf-8") as handle:
                json.dump({
                    "status": "no_op",
                    "worktree": worktree,
                    "declared_files": files,
                    "pathspecs": specs,
                    "reason": ("implementer declared files_changed but the tree "
                               "differs from the pre-implementer baseline in none of them"),
                }, handle, indent=2)
                handle.write("\n")
    except OSError:
        pass

    def _sec(title, path):
        if os.path.isfile(path) and os.path.getsize(path) > 0:
            from mini_ork.context_assembler import cap_block
            body = cap_block(open(path, encoding="utf-8", errors="replace").read(),
                             label=title)
            return f"\n# {title}\n{body}\n"
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
        from mini_ork.context_assembler import cap_block
        block += (f"\n# review-diff.patch\n"
                  f"{cap_block(open(diff_path, encoding='utf-8', errors='replace').read(), label='review-diff.patch')}\n")
    elif no_op:
        block += ("\n# review-diff.patch\n(no diff — the implementer declared changes but the "
                  "tree differs from the pre-implementer baseline in none of them; see "
                  "review-diff-noop.json)\n")
    else:
        block += "\n# review-diff.patch\n(no diff)\n"
    block += ("\n--- End reviewer inputs ---\n\n"
              "REVIEWER NOTE: The assembled inputs above are required for a real verdict. If any "
              "input is marked '(not available)' or '(no diff)', review what IS present. Only "
              "hard-abstain (verdict=needs_revision with reason 'inputs missing') when BOTH the "
              "diff and the summary are absent — that is the only genuine no-op case. An empty "
              "diff against declared files_changed is NOT that case: the implementer claimed work "
              "the tree does not contain, which is a real failure, never a pass. A missing "
              "verifier verdict is a real failure signal, not an abstention excuse.\n")
    return block


def _learned_block(root, task_class, node_type, lane="", node_id="",
                    *, sources: list[dict] | None = None):
    """F5-B (bash _dispatch_node:2357-2382): inject reflect-learned failure modes +
    unconsumed operator-steering messages into LLM node prompts — the READ side of
    the learning loop. Empty when opt-out or for a non-LLM node.

    ``lane`` and ``node_id`` are the identity of the dispatch that is asking.
    They are recorded on the retrieval ledger alongside the injected memories so
    the memory spend this block costs is attributable to the routed lane and the
    node that chose it (LIMBO, arXiv 2609.14138) instead of being invisible;
    they do not change which memories come back. ``node_type`` also sets the
    retrieval count — a judgment node sees more of the loop's record than a
    mechanical one (see context_assembler._limbo_limit).

    ``sources`` is an out-param: when supplied, one dict is appended per row
    actually injected — ``kind: "gradient"`` / ``kind: "pattern"`` for the
    ``failure_modes_md`` rows (passed through) and ``kind: "steering"`` for
    each operator-steering message appended below. With ``sources=None`` the
    returned markdown is byte-identical to the same call with a list.
    """
    if os.environ.get("MO_INJECT_LEARNINGS", "1") != "1":
        return ""
    if node_type not in ("researcher", "implementer", "reviewer"):
        return ""
    del root
    # Operator-set preferences (kickoff §"Injection (`_learned_block`)"). The
    # prefs block is built first so it sits ahead of failure_modes + steering
    # in the rendered prompt; explicit operator rules outrank learned ones.
    # Any exception means no preference block; the rest of the learned block
    # is unchanged — mirrors the existing fail-soft contract below.
    pref_block = ""
    pref_sources: list[dict] = []
    try:
        from mini_ork.memory import preferences
        # The run's declared file scope (run_profile.json → scope_allow), so a
        # path-scoped rule reaches only the runs that touch a matching file.
        # ``MINI_ORK_RUN_DIR`` is published before this call (dispatch_node,
        # publish_env({ENV_RUN_DIR: run_dir_eff})); missing/empty → no path rules.
        _run_dir = context_env("MINI_ORK_RUN_DIR", "")
        _paths = preferences.scope_paths(_run_dir) if _run_dir else []
        _prefs = preferences.prefs_for(task_class or "generic", paths=_paths)
        pref_block = preferences.render_block(_prefs)
        if pref_block and sources is not None:
            for _p in _prefs:
                pref_sources.append({
                    "kind": "preference",
                    "id": f"pref:{_p['scope']}:{_p['target']}:{_p['key']}",
                    "text": _p["value"],
                })
    except Exception:
        pref_block = ""
        pref_sources = []
    # Context v2 (MO_CONTEXT_V2=on, not held out): the kickoff's constraints and
    # the problems reviewers already found in this run's files, from the pack the
    # planner wrote. It sits after the operator's preferences and before the
    # learned failure modes. Any other arm, or any failure, adds nothing.
    v2_block = ""
    v2_sources: list[dict] = []
    try:
        from mini_ork import context_v2
        v2_block = context_v2.node_block(
            context_env("MINI_ORK_RUN_DIR", ""), node_type,
            context_env("MINI_ORK_RUN_ID", ""), task_class=task_class or "",
            sources=v2_sources)
        if v2_block:
            v2_block = "\n\n" + v2_block
    except Exception:
        v2_block, v2_sources = "", []
    block = ""
    try:
        from mini_ork import context_assembler
        fm = context_assembler.failure_modes_md(
            task_class or "generic", 5, db=os.environ.get("MINI_ORK_DB"),
            node_type=node_type, lane=lane, node_id=node_id,
            sources=sources,
        ).strip()
        if fm:
            block = "\n\n" + fm + "\n"
        from mini_ork.steering import operator_steering
        rows = operator_steering.fetch_for(
            context_env("MINI_ORK_RUN_ID", ""), node_type
        )
        if rows:
            lines = [
                "--- Operator steering (injected supervisor guidance) ---",
                f"{len(rows)} message(s) targeted at this node. Treat as load-bearing:",
            ]
            # Collect steering sources into a local list and only commit
            # them to ``sources`` after the prompt block has been built —
            # a mid-loop exception would otherwise leak entries that never
            # reached the prompt, and ``sources`` would claim more was
            # injected than actually was.
            steering_sources: list[dict] = []
            for row in rows:
                severity = str(row.get("severity", "info")).upper()
                source = row.get("source") or "unknown"
                message = row.get("message", "")
                lines.append(f"- [{severity}] (from {source}) {message}")
                if sources is not None:
                    steering_sources.append({
                        "kind": "steering",
                        "id": row.get("id"),
                        "severity": severity,
                        "source": source,
                        "message": message,
                    })
            lines.append("--- /operator steering ---")
            block += "\n" + "\n".join(lines) + "\n"
            if sources is not None:
                # Only commit sources after the prompt block has been built —
                # a mid-loop exception would otherwise leak entries that never
                # reached the block, and ``sources`` would claim more was
                # injected than actually was.
                sources.extend(steering_sources)
    except Exception:
        pass
    if (pref_sources or v2_sources) and sources is not None:
        # Pref sources are prepended so they appear in prompt order (prefs
        # first, then context v2), matching the IDE "Learning" tab contract
        # that the user's operator rules show up ahead of learned failure modes.
        sources[:0] = pref_sources + v2_sources
    return pref_block + v2_block + block


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
    # The planner already persisted one ASK file per question (asks/ask-N.json)
    # and exited 6. When this gate is reached anyway (a standalone execute
    # against a needs_answers plan), the run is resumable — refuse to dispatch
    # and signal exit 6 WITHOUT writing blocked.json or marking the run failed.
    asks_dir = os.path.join(run_dir, "asks")
    if os.path.isdir(asks_dir) and any(
            n.startswith("ask-") and n.endswith(".json") for n in os.listdir(asks_dir)):
        print("[blocked] plan_status=needs_answers — answer the ASK files, then "
              "resume (mini-ork resume <run_id> --answer ask-1=<text>)")
        return True
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
    home = context_env("MINI_ORK_HOME") or os.path.join(os.getcwd(), ".mini-ork")
    db = context_env("MINI_ORK_DB") or os.path.join(home, "state.db")
    run_id = (context_env("MINI_ORK_RUN_ID") or os.environ.get("MINI_ORK_TASK_RUN_ID")
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
    verdict, finish_reason, lane, route_source, route_explore, route_score,
    route_margin, predicted_error)`."""
    from mini_ork import trace_store  # noqa: PLC0415

    def _tf(node_id, status, node_type, output_file="", verdict="", finish_reason="",
            lane="", route_source="", route_explore=False, route_score=None,
            route_margin=None, predicted_error=None):
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
        # Route provenance (migration 0054). Persisting WHY this lane was chosen is
        # what makes a lane's win attributable: without it a learned route, an
        # epsilon-greedy explore swap, and a recipe pin collapse into one
        # indistinguishable "agent_version_id", and no outcome can be credited to
        # the decision that produced it.
        if route_source:
            extra["route_source"] = route_source
            extra["route_explore"] = bool(route_explore)
            if route_score is not None:
                extra["route_score"] = float(route_score)
            # The margin over the runner-up, signed by the router. UCCI fits a
            # monotone map from this margin to the observed error rate, so a
            # missing margin just means "uncalibratable row", not a broken write.
            if route_margin is not None:
                extra["route_margin"] = float(route_margin)
            # The calibrated error probability behind an escalation decision.
            # Persisted so the backtest can check the map against the outcome;
            # a missing prediction means "uncalibratable row", not a broken write.
            if predicted_error is not None:
                extra["predicted_error"] = float(predicted_error)
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
        # Reward hygiene: mark infra exits (timeout / cost-circuit) non-'valid'
        # so writeback drops them from the group-relative advantage instead of
        # filing a low reward that drags the group mean down and hands undeserved
        # advantage to whichever lane had healthy groupmates. Default stays
        # 'valid' (trace_store) for every genuine capability outcome.
        if is_non_learnable_exit(finish_reason):
            extra["validity"] = "infra_failed"
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


# ── revise loop (retries edges) ──────────────────────────────────────────────
# A failed node with a `retries` edge sends its findings back to the edge's
# target for a bounded number of rounds, instead of blocking descendants. All
# revise state crosses the process split through the run dir (``revise/``):
# publish_env writes die in pool workers, so the feedback file + current.json
# are the only reliable channel back into the implementer's next dispatch.

def _revise_rounds_limit() -> int:
    """``MO_REVISE_ROUNDS``, default 2. ``<=0`` disables the revise loop."""
    try:
        return int(context_env("MO_REVISE_ROUNDS", "2"))
    except ValueError:
        return 2


def _verifier_stem(field) -> str:
    """The verifier evidence stem for a node field (mirrors ``_handle_verifier``).

    ``verifier_<stem>.json`` is the run-local evidence the reviewer input
    assembly reads; the stem derives from the node's verifier_ref (field[5]),
    falling back to the node id when no verifier script is declared.
    """
    verifier_ref = (field[5] if len(field) > 5 else "") or ""
    if verifier_ref.startswith("verifiers/"):
        stem = verifier_ref[len("verifiers/"):]
    else:
        stem = verifier_ref or field[0]
    if stem.endswith((".sh", ".py")):
        stem = stem[:-3]
    return stem


def _tail_lines(path: str, count: int) -> list[str]:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.readlines()[-count:]
    except OSError:
        return []


def _revise_failure_section(run_dir: str, field, finish_reason: str) -> str:
    """One per-source section of a revise feedback file."""
    node_id, node_type = field[0], field[1]
    lines = [f"## {node_id} ({node_type})"]
    if node_type == "verifier":
        stem = _verifier_stem(field)
        evidence = os.path.join(run_dir, f"verifier_{stem}.json")
        lines.append(f"verifier evidence: verifier_{stem}.json")
        body = ""
        if os.path.isfile(evidence):
            try:
                with open(evidence, encoding="utf-8", errors="replace") as fh:
                    body = fh.read()
            except OSError:
                body = ""
        summary = ""
        if body:
            try:
                data = json.loads(body)
            except Exception:
                data = None
            if isinstance(data, dict):
                for key in ("error_summary", "reasons", "failed_checks", "verdict"):
                    value = data.get(key)
                    if value:
                        summary = json.dumps(value)[:4000]
                        break
                evidence_path = data.get("evidence_path")
                if evidence_path:
                    lines.append(f"evidence_path: {evidence_path}")
            else:
                summary = body[:4000]
        if summary:
            lines.append(f"error summary: {summary}")
        else:
            lines.append("(no verifier evidence produced)")
        # Best-effort tail of the human-readable log, if one exists.
        for candidate in (f"verifier-{stem}.log", f"evidence/{stem}.log"):
            tail = _tail_lines(os.path.join(run_dir, candidate), 80)
            if tail:
                lines.append(f"evidence tail ({candidate}):")
                lines.extend(line.rstrip("\n") for line in tail)
                break
    elif node_type == "reviewer":
        review_file = os.path.join(run_dir, f"review-{node_id}.json")
        body = ""
        if os.path.isfile(review_file):
            try:
                with open(review_file, encoding="utf-8", errors="replace") as fh:
                    body = fh.read()
            except OSError:
                body = ""
        data = None
        if body:
            try:
                data = json.loads(body)
            except Exception:
                data = None
        if isinstance(data, dict):
            lines.append(f"verdict: {data.get('verdict', 'unknown')}")
            notes = data.get("notes")
            if notes:
                if isinstance(notes, list):
                    lines.append("notes:")
                    lines.extend(f"- {note}" for note in notes)
                else:
                    lines.append(f"notes: {notes}")
            elif data.get("reasons"):
                lines.append(f"reasons: {json.dumps(data['reasons'])[:4000]}")
        else:
            lines.append(f"(review file unparseable): {body[:4000]}")
    else:
        lines.append(f"finish reason: {finish_reason or 'failed'}")
    return "\n".join(lines)


def _write_revise_feedback(run_dir: str, round_no: int, max_rounds: int,
                           sources) -> str:
    """Write ``<run_dir>/revise/round-<n>.md`` and return its path."""
    revise_dir = os.path.join(run_dir, "revise")
    os.makedirs(revise_dir, exist_ok=True)
    header = (
        f"Round {round_no} of {max_rounds}: the previous attempt was checked and these "
        "problems were found. Fix ONLY these problems, on top of the changes already "
        "in the working tree. Do not start over, and do not revert your earlier work.\n"
    )
    sections = [_revise_failure_section(run_dir, field, finish_reason)
                for field, _rc, finish_reason in sources]
    path = os.path.join(revise_dir, f"round-{round_no}.md")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(header + "\n\n".join(sections) + "\n")
    return path


def _write_revise_current(run_dir: str, round_no: int, max_rounds: int,
                          feedback_path: str) -> None:
    revise_dir = os.path.join(run_dir, "revise")
    os.makedirs(revise_dir, exist_ok=True)
    with open(os.path.join(revise_dir, "current.json"), "w", encoding="utf-8") as fh:
        json.dump({"round": round_no, "max_rounds": max_rounds,
                   "feedback": feedback_path}, fh)


def _node_revise_artifacts(run_dir: str, field) -> list[str]:
    """Per-node output files that must leave the run root on a revise round."""
    node_id, node_type = field[0], field[1]
    if node_type == "verifier":
        return [os.path.join(run_dir, f"verifier_{_verifier_stem(field)}.json")]
    if node_type == "reviewer":
        return [os.path.join(run_dir, f"review-{node_id}.json")]
    if node_type == "implementer":
        return [os.path.join(run_dir, f"impl-{node_id}.log")]
    return []


def _archive_revise_round(run_dir: str, round_no: int, reset_fields) -> None:
    """Move the prior round's outputs out of the run root into ``revise/round-<n>/``.

    History is kept so the next round's reviewer never reads stale files; the
    reset nodes re-run and write fresh evidence.
    """
    dest = os.path.join(run_dir, "revise", f"round-{round_no}")
    os.makedirs(dest, exist_ok=True)
    for field in reset_fields:
        for source in _node_revise_artifacts(run_dir, field):
            if os.path.isfile(source):
                try:
                    shutil.move(source, os.path.join(dest, os.path.basename(source)))
                except OSError:
                    pass
    review_diff = os.path.join(run_dir, "review-diff.patch")
    if os.path.isfile(review_diff):
        try:
            shutil.move(review_diff, os.path.join(dest, "review-diff.patch"))
        except OSError:
            pass




from mini_ork.cli.execute_handlers import (  # noqa: E402,F401
    EARLY_NODE_HANDLERS,
    NODE_HANDLER_REGISTRY,
    NodeDispatch,
    _IMPLEMENTER_SUBMODES,
    _classify_review_node,
    _eval_artifact_text,
    _handle_eval,
    _handle_implementer,
    _handle_planner_early,
    _handle_publisher,
    _handle_reflector_early,
    _handle_researcher,
    _handle_reviewer,
    _handle_rollback,
    _handle_transform,
    _handle_verifier,
    _read_run_trajectory,
    _revert_working_tree,
    _rollback_strategy,
    _stamp_run_eval_reward,
    _verifier_noise_rates,
    _warn_if_jury_not_decorrelated,
    dispatch_node,
    register_implementer_submode,
    register_node_handler,
    REVIEWER_VERDICT_UNPARSEABLE,
)


if __name__ == "__main__":
    raise SystemExit(main())
