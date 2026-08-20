"""Node dispatch handlers extracted from :mod:`mini_ork.cli.execute`.

The executor remains the compatibility surface. Handler dependencies that
still live there are imported below, while every handler and registry defined
in this module is explicitly re-exported by ``execute``.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from typing import Callable

from mini_ork.context import (
    ENV_DISPATCH_CHAIN,
    ENV_RESUME_SESSION_ID,
    ENV_RUN_DIR,
    ENV_TARGET_CWD,
)


def _execute_module():
    """Resolve the compatibility module only after both modules initialize."""
    from mini_ork.cli import execute

    return execute


def _execute_delegate(name):
    """Forward helper calls so monkeypatches on ``execute`` remain observable."""
    def delegated(*args, **kwargs):
        return getattr(_execute_module(), name)(*args, **kwargs)

    return delegated


class _ExecuteMembership:
    """Lazy membership view over a container owned by ``execute``."""

    def __init__(self, name):
        self.name = name

    def __contains__(self, item):
        return item in getattr(_execute_module(), self.name)


_REVIEW_PASS = _ExecuteMembership("_REVIEW_PASS")
_REVIEW_REVISE = _ExecuteMembership("_REVIEW_REVISE")
_assemble_reviewer_inputs = _execute_delegate("_assemble_reviewer_inputs")
_assert_lane_capability = _execute_delegate("_assert_lane_capability")
_capture_pre_impl_baseline = _execute_delegate("_capture_pre_impl_baseline")
_extract_verdict = _execute_delegate("_extract_verdict")
_harvest_self_migrate_artifacts = _execute_delegate("_harvest_self_migrate_artifacts")
_intervention_gate_check = _execute_delegate("_intervention_gate_check")
_learned_block = _execute_delegate("_learned_block")
_required_artifacts_ok = _execute_delegate("_required_artifacts_ok")
_researcher_output_file = _execute_delegate("_researcher_output_file")
_resolve_target_cwd = _execute_delegate("_resolve_target_cwd")
_run_verifier_ref = _execute_delegate("_run_verifier_ref")
_synth_artifact_name = _execute_delegate("_synth_artifact_name")
_verifier_runs_before_implementer = _execute_delegate("_verifier_runs_before_implementer")
_watchdog_stale_heartbeat = _execute_delegate("_watchdog_stale_heartbeat")
_write_self_migrate_implementer_summary = _execute_delegate(
    "_write_self_migrate_implementer_summary"
)
apply_env_overrides = _execute_delegate("apply_env_overrides")
apply_impl_output = _execute_delegate("apply_impl_output")
charge_node_cost = _execute_delegate("charge_node_cost")
dispatch_chain = _execute_delegate("dispatch_chain")
finish_reason_for_failure = _execute_delegate("finish_reason_for_failure")
node_env_overrides = _execute_delegate("node_env_overrides")
policy_route_lane = _execute_delegate("policy_route_lane")
publisher_node = _execute_delegate("publisher_node")


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
    from mini_ork.dispatch.llm_dispatch import resolve_lane_family
    _chain_lead = resolve_lane_family(lane)
    apply_env_overrides({ENV_DISPATCH_CHAIN: dispatch_chain(node_type, _chain_lead)})

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
    # P1b: opt-in shared-drive routing. No-op unless MO_SHARED_DRIVE_BACKEND is
    # set, so the default host-tree cwd is unchanged; when set, every node in the
    # run shares one virtual drive (lazy import keeps the seam side-effect-free).
    from mini_ork.runtime.run_drive import resolve_run_drive_cwd
    target = resolve_run_drive_cwd(target)
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
        # BUG6: a synthesizer produces a document, not a pass/fail verdict, so
        # _extract_verdict finds no JSON and returns 'unknown'. Stamping that on
        # the trace misreports the synth as an ambiguous REVIEW — which poisons
        # rho_aggregator (win/loss counting) and the gradient extractor (it read
        # reviewer_verdict='unknown' as a real defect). Trace no verdict instead.
        synth_verdict = "" if vn == "unknown" else verdict
        ctx.trace(ctx.node_id, "success", "reviewer", review_file, synth_verdict, "done")
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
        ev = os.path.join(ev_dir, os.path.basename(ctx.verifier_ref).replace(".sh", "").replace(".py", "") + ".log")
        rc = _run_verifier_ref(script, ev, plan_path=ctx.plan_path, artifact_path=artifact)
        # F2-B: persist evidence to verifier_<stem>.json (bash :2886-2888) so the
        # reviewer input assembly can read the typecheck/test verdicts. Before the
        # rc return so failures are visible too (a missing verifier is real signal).
        vstem = ctx.verifier_ref[len("verifiers/"):] if ctx.verifier_ref.startswith("verifiers/") else ctx.verifier_ref
        vstem = vstem[:-3] if vstem.endswith((".sh", ".py")) else vstem
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


def _read_run_trajectory(db: str, run_id: str, run_dir: str):
    """Best-effort: this run's execution_traces rows + any verifier_*.json
    verdicts in the run dir, for the judge's trajectory view. Fail-open — any
    error yields empties so eval never sinks a run over a missing/locked db."""
    traces: list[dict] = []
    if db and run_id and os.path.isfile(db):
        try:
            con = sqlite3.connect(db, timeout=5.0)
            con.execute("PRAGMA busy_timeout=5000")
            con.row_factory = sqlite3.Row
            try:
                rows = con.execute(
                    "SELECT status, reviewer_verdict, reward_source, reward_value, "
                    "final_artifact_ref FROM execution_traces WHERE run_id=? "
                    "ORDER BY created_at", (run_id,)).fetchall()
                traces = [dict(r) for r in rows]
            finally:
                con.close()
        except Exception:
            traces = []
    verifier_verdicts: dict[str, object] = {}
    if run_dir and os.path.isdir(run_dir):
        try:
            for fn in sorted(os.listdir(run_dir)):
                if fn.startswith("verifier_") and fn.endswith(".json"):
                    name = fn[len("verifier_"):-len(".json")]
                    try:
                        with open(os.path.join(run_dir, fn), encoding="utf-8") as fh:
                            body = fh.read().strip()
                    except OSError:
                        continue
                    try:
                        verifier_verdicts[name] = json.loads(body)  # parsed → pass/verdict
                    except (ValueError, TypeError):
                        verifier_verdicts[name] = {"raw": body[:200]}
        except OSError:
            pass
    return traces, verifier_verdicts


def _verifier_noise_rates(db: str, verifier_names) -> tuple[float, float]:
    """(ρ_FP, ρ_FN) for the run's verifiers — the Layer-1 noise model. Uses
    labeled ``verifier_results`` (migration 0025) when present (FP via the
    shipped verifier_fp_rate primitive, FN computed inline), else conservative
    priors (MO_EVAL_VERIFIER_FP_PRIOR / _FN_PRIOR). Averaged across verifiers.
    Best-effort and fail-open — any error returns the priors."""
    from mini_ork.learning import eval_judge as ej  # noqa: PLC0415
    try:
        fp_prior = float(os.environ.get("MO_EVAL_VERIFIER_FP_PRIOR", ej.DEFAULT_FP_PRIOR))
        fn_prior = float(os.environ.get("MO_EVAL_VERIFIER_FN_PRIOR", ej.DEFAULT_FN_PRIOR))
    except ValueError:
        fp_prior, fn_prior = ej.DEFAULT_FP_PRIOR, ej.DEFAULT_FN_PRIOR
    if not (db and os.path.isfile(db) and verifier_names):
        return fp_prior, fn_prior
    fps: list[float] = []
    fns: list[float] = []
    try:
        from mini_ork.gates.verifier_rubric import verifier_fp_rate  # noqa: PLC0415
        con = sqlite3.connect(db, timeout=5.0)
        con.execute("PRAGMA busy_timeout=5000")
        try:
            for name in verifier_names:
                total = con.execute(
                    "SELECT COUNT(*) FROM verifier_results WHERE verifier_name=?",
                    (name,)).fetchone()[0]
                if not total:
                    continue  # unlabeled → let the prior stand for this verifier
                fn_ct = con.execute(
                    "SELECT COUNT(*) FROM verifier_results "
                    "WHERE verifier_name=? AND is_false_negative=1", (name,)).fetchone()[0]
                fns.append(fn_ct / total)
                try:
                    fps.append(float(verifier_fp_rate(db, name)))
                except (ValueError, TypeError):
                    fps.append(fp_prior)
        finally:
            con.close()
    except Exception:
        return fp_prior, fn_prior
    return (sum(fps) / len(fps) if fps else fp_prior,
            sum(fns) / len(fns) if fns else fn_prior)


def _eval_artifact_text(ctx: NodeDispatch) -> tuple[str, str]:
    """Read the run's first declared final artifact (best-effort). Returns
    (text, repo-relative-or-abs ref)."""
    ref = ""
    try:
        ac = (json.load(open(ctx.plan_path)).get("artifact_contract") or {}) if ctx.plan_path else {}
        outs = ac.get("outputs") or [] if isinstance(ac, dict) else []
        ref = outs[0] if outs else ""
    except Exception:
        ref = ""
    text = ""
    if ref and os.path.isfile(ref):
        try:
            with open(ref, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            text = ""
    return text, ref


def _stamp_run_eval_reward(db, run_id, score, axes, source) -> None:
    """Phase-1 rail (gated by MO_EVAL_STAMP_RUN): stamp the graded eval reward
    across every delivery trace of this run so the GRPO router learns from the
    judge instead of process_reward — mirrors trace_store.grade_run_reward for
    the rubric. Excludes the dedicated eval row (reward_source=source) so it is
    not overwritten. Best-effort."""
    if not (db and run_id and os.path.isfile(db)):
        return
    from mini_ork.learning import eval_judge as ej  # noqa: PLC0415
    s = ej.clamp01(score)
    reward_g = (s - ej.EVAL_ANCHOR) / abs(ej.EVAL_ANCHOR)
    vec = json.dumps({a: ej.clamp01(v) for a, v in axes.items()}) if axes else None
    con = sqlite3.connect(db, timeout=5.0)
    con.execute("PRAGMA busy_timeout=5000")
    try:
        con.execute(
            "UPDATE execution_traces SET reward_value=?, reward_anchor=?, reward_g=?, "
            "reward_direction='higher_is_better', reward_primary_metric=?, "
            "reward_source=?, reward_vector_json=COALESCE(?, reward_vector_json) "
            "WHERE run_id=? AND reward_source != ?",
            (s, ej.EVAL_ANCHOR, reward_g, ej.EVAL_PRIMARY_METRIC, source, vec,
             run_id, source))
        con.commit()
    finally:
        con.close()


def _warn_if_jury_not_decorrelated(jury_lanes) -> None:
    """Advisory (never blocks): a jury drawn from a single model family isn't
    decorrelated, so its consensus is weak — correlated judges make the same
    mistakes, which is exactly what a jury is meant to defeat. Reuses the
    coalition gate's family map. Best-effort."""
    try:
        from mini_ork.gates.coalition_gate import family_of  # noqa: PLC0415
        families = {family_of(lane) for lane in jury_lanes}
        if len(families) < 2:
            print(f"  [eval] jury lanes {jury_lanes} span only family "
                  f"{sorted(families)} — not decorrelated; consensus is weak",
                  file=sys.stderr)
    except Exception:
        pass


def _handle_eval(ctx: NodeDispatch):
    """Advisory per-run graded eval (roadmap Step-3). Dispatches a
    trajectory-aware LLM judge, aggregates its per-axis sub-scores, and persists
    the result to execution_traces under reward_source='eval@v1'. It NEVER gates:
    any dispatch/parse failure falls open to the rubric/PRM heuristic and the
    node still returns success. Logic lives in mini_ork/learning/eval_judge.py."""
    from mini_ork import trace_store  # noqa: PLC0415
    from mini_ork.learning import eval_judge as ej  # noqa: PLC0415

    run_dir = ctx.run_dir_eff or ctx.run_dir
    recipe_prompt = (open(ctx.prompt_file, encoding="utf-8").read()
                     if ctx.prompt_file and os.path.isfile(ctx.prompt_file) else "")
    artifact_text, artifact_ref = _eval_artifact_text(ctx)
    traces, verifier_verdicts = _read_run_trajectory(ctx.db, ctx.run_id, run_dir)
    trajectory_summary = ej.trajectory_digest(traces, verifier_verdicts)

    prompt = ej.build_eval_prompt(
        node_desc=ctx.node_desc,
        plan_content=ej.truncate(ctx.plan_content, 4000),
        artifact_text=ej.truncate(artifact_text),
        trajectory_summary=ej.truncate(trajectory_summary, 4000),
        recipe_prompt=recipe_prompt,
    )

    # Layer 3 — dispatch the judge as a DECORRELATED JURY when MO_EVAL_JURY_LANES
    # (comma-separated lanes from different model families) is set; else a single
    # judge (default). The jury's veto is consensus-based and abstains when the
    # panel can't agree (jury_veto), so no one model owns the veto.
    jury_lanes = [x.strip() for x in
                  os.environ.get("MO_EVAL_JURY_LANES", "").split(",") if x.strip()]
    if len(jury_lanes) >= 2:
        _warn_if_jury_not_decorrelated(jury_lanes)
    envelopes = []
    rc = 1
    if jury_lanes:
        for jlane in jury_lanes:
            jrc, jres = ctx.dispatch_fn(ctx.task_class, jlane, prompt)
            if jrc == 0 and jres:
                env = ej.parse_eval_envelope(jres)
                if env:
                    envelopes.append(env)
        rc = 0 if envelopes else 1
    else:
        rc, result = ctx.dispatch(prompt)
        env = ej.parse_eval_envelope(result) if rc == 0 and result else None
        if env:
            envelopes.append(env)

    primary = envelopes[0] if envelopes else None
    axes = (primary.get("axes") or {}) if primary else {}
    rationale = primary.get("rationale", "") if primary else ""
    findings = primary.get("trajectory_findings", []) if primary else []

    # Layer 0 — execution reward is the backbone (EGCA: execution, not opinion).
    r_exec, exec_detail = ej.execution_reward(verifier_verdicts)
    if r_exec is not None:
        # Layer 1 — de-bias by the verifier's measured/prior FP-FN noise rates.
        fp_rate, fn_rate = _verifier_noise_rates(ctx.db, list(verifier_verdicts.keys()))
        r_corr = ej.noise_correct(r_exec, fp_rate, fn_rate)
        # Layer 3 — the jury (or single judge) may only VETO by consensus, and
        # abstains when the panel disagrees. Empty panel → no veto (fail-open).
        score, jury_meta = ej.jury_veto(r_corr, envelopes)
        # Selective escalation (2510.20369 — ask a strong judge when uncertain):
        # a hung jury dispatches ONE strong tiebreaker lane whose veto decides,
        # rather than silently abstaining. No escalate lane → abstain as before.
        escalate_lane = os.environ.get("MO_EVAL_JURY_ESCALATE_LANE", "").strip()
        if jury_meta.get("jury") == "abstain_low_agreement" and escalate_lane:
            erc, eres = ctx.dispatch_fn(ctx.task_class, escalate_lane, prompt)
            tie = ej.parse_eval_envelope(eres) if erc == 0 and eres else None
            if tie is not None:
                score = ej.judge_veto(r_corr, tie.get("axes") or {})
                jury_meta = {**jury_meta, "jury": "escalated",
                             "tiebreaker_lane": escalate_lane,
                             "tiebreaker_axes": tie.get("axes") or {}}
                print(f"  [eval] hung jury → escalated to {escalate_lane}",
                      file=sys.stderr)
        source, verdict = ej.EXEC_SOURCE, ej.verdict_from_score(score)
        exec_meta = {"r_exec": r_exec, "r_corrected": r_corr,
                     "fp_rate": fp_rate, "fn_rate": fn_rate,
                     "verifiers": exec_detail, "jury": jury_meta}
    elif primary is not None:
        # No execution signal (vacuous / no verifiers) → judge-only, lower trust.
        score = ej.aggregate_axes(axes, primary.get("score"))
        source = ej.JUDGE_SOURCE
        verdict = primary.get("verdict") or ej.verdict_from_score(score)
        exec_meta = {"r_exec": None, "note": "no execution signal — judge-only reward"}
        print("  [eval] no execution signal — judge-only reward (lower trust)",
              file=sys.stderr)
    else:
        # Judge unavailable AND no execution signal → heuristic fallback.
        score, source = ej.fallback_score(run_dir)
        verdict = ej.verdict_from_score(score)
        rationale = "judge unavailable + no execution signal — rubric/PRM heuristic"
        exec_meta = {"r_exec": None, "note": "judge unavailable"}
        print(f"  [eval] judge unavailable (rc={rc}) + no execution signal → "
              f"fallback {source} score={score:.2f}", file=sys.stderr)

    # Persist the envelope for offline graders + the data flywheel.
    try:
        with open(os.path.join(run_dir, "eval.json"), "w", encoding="utf-8") as fh:
            json.dump({"score": score, "axes": axes, "verdict": verdict,
                       "rationale": rationale, "trajectory_findings": findings,
                       "reward_source": source, "execution": exec_meta}, fh, indent=2)
    except OSError:
        pass

    # Reward vector: numeric axes + execution numbers (DB-safe; detail → eval.json).
    reward_vector = {k: v for k, v in axes.items() if isinstance(v, (int, float))}
    if exec_meta.get("r_exec") is not None:
        reward_vector["r_exec"] = exec_meta["r_exec"]
        reward_vector["r_corrected"] = exec_meta["r_corrected"]

    # Write the graded reward onto the wired-but-empty 0042 reward columns.
    try:
        payload = ej.eval_reward_payload(
            ctx.task_class, ctx.run_id, score, reward_vector, verdict,
            source=source, artifact_ref=artifact_ref)
        trace_store.trace_write(payload, db=ctx.db)
    except Exception as exc:  # noqa: BLE001 — advisory; never sink the run
        print(f"  [eval] reward write skipped: {exc}", file=sys.stderr)

    if os.environ.get("MO_EVAL_STAMP_RUN", "0") == "1":
        try:
            _stamp_run_eval_reward(ctx.db, ctx.run_id, score, reward_vector, source)
        except Exception:
            pass

    print(f"  [eval] {source} score={score:.2f} verdict={verdict} "
          f"exec={exec_meta.get('r_exec')} axes={axes}")
    ctx.charge()
    return 0, "done"


NODE_HANDLER_REGISTRY: dict[str, Callable[[NodeDispatch], tuple[int, str]]] = {
    "researcher": _handle_researcher,
    "transform": _handle_transform,
    "implementer": _handle_implementer,
    "reviewer": _handle_reviewer,
    "verifier": _handle_verifier,
    "eval": _handle_eval,
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
