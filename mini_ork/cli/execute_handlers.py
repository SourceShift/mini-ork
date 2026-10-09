"""Node dispatch handlers extracted from :mod:`mini_ork.cli.execute`.

The executor remains the compatibility surface. Handler dependencies that
still live there are imported below, while every handler and registry defined
in this module is explicitly re-exported by ``execute``.
"""
from __future__ import annotations

import contextlib
import functools
import hashlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from typing import Callable

from mini_ork.context import (
    ENV_DISPATCH_CHAIN,
    ENV_RESUME_SESSION_ID,
    ENV_RUN_DIR,
    ENV_TARGET_CWD,
    context_env,
    publish_env,
    run_context_scope,
)
from mini_ork.runtime.run_roots import load_run_roots
from mini_ork.cli.main import _module_env, _reflect_timeout_seconds
from mini_ork.observability.node_events import _now_ms, mo_node_emit, mo_node_end, mo_node_start
from mini_ork.execute_compat import (
    ARTIFACT_COMPLETION_LOG,
    declared_artifacts_ok,
    node_strict_handshake,
)
from mini_ork.workflow.store import make_artifact_store


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
_capture_pre_impl_fixture = _execute_delegate("_capture_pre_impl_fixture")
_extract_verdict = _execute_delegate("_extract_verdict")
_harvest_framework_edit_ground_truth = _execute_delegate(
    "_harvest_framework_edit_ground_truth"
)
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
_write_implementer_summary = _execute_delegate("_write_implementer_summary")


# D5: node types that never leave the control plane under --placement remote
# (in-process logic, and the local-tree steps that run after sync-down).
_LOCAL_PLACEMENT_NODE_TYPES = frozenset(
    {"classify", "transform", "eval", "publisher", "rollback", "baseline", "harvest"})


def _node_placement(node_type: str, lane: str, run_dir: str) -> tuple[str, str, str]:
    """``(placement, node_host, session_id)`` for a node's ``node_start`` event.

    remote-nodes-14 §4. ``placement`` is where THIS node's work runs under the
    run's placement (D5), decided by the same rule the dispatch path uses
    (:func:`providers._classify_lane_for_placement`), so the event agrees with
    the spawn. Empty when ``MO_PLACEMENT`` is unset — the default payload stays
    byte-identical. ``node_host``/``session_id`` come from the run's session
    marker (written at run-start provisioning) and are set only for remote.
    """
    from mini_ork.context import context_env
    from mini_ork.dispatch.providers import _classify_lane_for_placement, _lane_kind

    run_placement = context_env("MO_PLACEMENT", "").strip().lower()
    if not run_placement:
        return "", "", ""
    if run_placement != "remote" or node_type in _LOCAL_PLACEMENT_NODE_TYPES:
        return "local", "", ""
    if node_type != "verifier":   # verifier checks always run on the node (epic 11)
        env = {"MO_PLACEMENT": "remote", "MO_NODE_TYPE": node_type,
               "MO_PLACEMENT_LOCAL_ROLES": context_env("MO_PLACEMENT_LOCAL_ROLES", "")}
        from mini_ork.dispatch.llm_dispatch import resolve_lane_family
        model = resolve_lane_family(lane) if lane else ""   # alias -> providers.yaml key
        if _classify_lane_for_placement(_lane_kind(model) if model else "", env) != "remote":
            return "local", "", ""
    node_host = session_id = ""
    try:
        from mini_ork.runtime.workspace_session import session_marker_path
        data = json.loads(session_marker_path(run_dir).read_text(encoding="utf-8") or "{}")
        if isinstance(data, dict):
            session_id = str(data.get("session_id") or "")
            locator = data.get("node") if isinstance(data.get("node"), dict) else {}
            node_host = str(locator.get("name") or locator.get("url") or "")
    except (OSError, ValueError):
        pass
    return "remote", node_host, session_id


_write_self_migrate_implementer_summary = _execute_delegate(
    "_write_self_migrate_implementer_summary"
)
apply_env_overrides = _execute_delegate("apply_env_overrides")
apply_impl_output = _execute_delegate("apply_impl_output")
charge_node_cost = _execute_delegate("charge_node_cost")
dispatch_chain = _execute_delegate("dispatch_chain")
finish_reason_for_failure = _execute_delegate("finish_reason_for_failure")
last_route_provenance = _execute_delegate("last_route_provenance")
node_env_overrides = _execute_delegate("node_env_overrides")
policy_route_lane = _execute_delegate("policy_route_lane")
publisher_node = _execute_delegate("publisher_node")


def _recipe_root(root: str) -> str:
    """Base dir for recipe assets (prompts, verifiers, workflow).

    main.py resolves the recipe against the MINI_ORK_HOME overlay when a
    consumer symlinks a private recipe there (MINI_ORK_ROOT still points at the
    primary checkout) and threads the winning base via MINI_ORK_RECIPE_ROOT.
    Honor it so prompt/verifier resolution matches where the recipe was found;
    absent (dev checkouts), fall back to root unchanged.
    """
    return os.environ.get("MINI_ORK_RECIPE_ROOT") or root


def resolve_prompt_file(root, recipe, prompt_ref, node_type) -> str:
    """Resolve a node prompt, preferring a flat per-run override directory."""
    override_dir = os.environ.get("MINI_ORK_PROMPT_OVERRIDE_DIR", "").strip()
    if override_dir and prompt_ref:
        override_file = os.path.join(override_dir, os.path.basename(prompt_ref))
        if os.path.isfile(override_file):
            return override_file

    recipe_dir = os.path.join(_recipe_root(root), "recipes", recipe) if recipe else ""
    if (
        prompt_ref
        and recipe_dir
        and os.path.isfile(os.path.join(recipe_dir, prompt_ref))
    ):
        return os.path.join(recipe_dir, prompt_ref)
    if recipe_dir and os.path.isfile(
        os.path.join(recipe_dir, "prompts", f"{node_type}.md")
    ):
        return os.path.join(recipe_dir, "prompts", f"{node_type}.md")
    if os.path.isfile(os.path.join(root, "prompts", f"{node_type}.md")):
        return os.path.join(root, "prompts", f"{node_type}.md")
    return ""


def _node_publish_boundary(fn):
    """Own the per-node isolation boundary (bottleneck #1).

    Everything dispatch_node dual-publishes into the run-context layer is wiped
    when the node call returns, so bindings can never leak into the caller's
    context — a bare publish_env outside a boundary would otherwise persist for
    the process lifetime and shadow os.environ for every later reader (tests,
    SDK embedders, sequential runs in one process). Run-level bindings published
    by the enclosing run flow (FAIL_COUNT, recovery markers) remain visible via
    the context-copy semantics of the scope.
    """
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        with run_context_scope({}):
            return fn(*args, **kwargs)
    return wrapper


def _node_attempt_no(db, run_id: str, node_id: str) -> int:
    """1 + the node's recorded attempts. An interrupted remote attempt records
    none, so a recovery re-dispatch gets the SAME number — and therefore the same
    idempotency key, which re-attaches to the still-running remote proc."""
    try:
        if not (isinstance(db, str) and db and os.path.isfile(db)):
            return 1
        con = sqlite3.connect(db, timeout=5.0)
        try:
            row = con.execute("SELECT COUNT(*) FROM node_attempts WHERE run_id=? AND node_id=?",
                              (run_id, node_id)).fetchone()
        finally:
            con.close()
        return int(row[0] or 0) + 1
    except sqlite3.Error:
        return 1


def _write_learned_record(run_dir: str, node_id: str, node_type: str,
                          lane: str, task_class: str, *,
                          attempt: int, block: str,
                          sources: list[dict] | None) -> None:
    """Persist the learned block an LLM node actually received.

    Writes two artifacts under ``<run_dir>/learned/``:

    - ``<node_id>.md`` — ``block.strip()`` followed by a newline, only when
      the block is non-empty. A stale ``.md`` from a prior attempt with an
      empty block is removed.
    - ``<node_id>.json`` — always. ``injected`` is True iff the block was
      non-empty; ``reason`` is "opt-out" when ``MO_INJECT_LEARNINGS != "1"``,
      "nothing matched" when the block was empty, else "". ``sources`` is the
      list collected by ``_learned_block`` (gradient / pattern / steering
      rows, in prompt order).

    The IDE "Learning" tab reads both files; this is the only inspectable
    surface for what the learner actually saw (F5-B, learn-inject kickoff).

    Never raises: an exception here would interrupt the node dispatch and
    waste the LLM spend that already happened. The caller has already
    fall-through semantics (``try/except`` in ``_learned_block``); matching
    that on the write side keeps the read-side cost-of-truth out of the
    dispatch path. Atomic (tmp + ``os.replace``) so an interrupted write
    never leaves a torn file the IDE would happily render as "what the
    learner was told".
    """
    try:
        if not run_dir:
            return
        learned_dir = os.path.join(run_dir, "learned")
        os.makedirs(learned_dir, exist_ok=True)
        # Opt-out / empty-block semantics are reflected in the JSON record;
        # the markdown side mirrors the actual injected text (none).
        opted_out = os.environ.get("MO_INJECT_LEARNINGS", "1") != "1"
        # ``injected`` is True only when something actually reached the prompt.
        # Opt-out short-circuits the upstream gate before the block is built, so
        # any block that does land here (a caller bypassing the gate) is recorded
        # as not-injected — its presence in the record is the audit, not the
        # data.
        injected = bool(block and block.strip()) and not opted_out
        if opted_out:
            reason = "opt-out"
        elif not injected:
            reason = "nothing matched"
        else:
            reason = ""
        record = {
            "node_id": node_id,
            "node_type": node_type,
            "lane": lane,
            "task_class": task_class,
            "attempt": int(attempt),
            "written_at": int(time.time()),
            "injected": injected,
            "reason": reason,
            "sources": list(sources) if sources else [],
        }
        md_path = os.path.join(learned_dir, f"{node_id}.md")
        json_path = os.path.join(learned_dir, f"{node_id}.json")

        # JSON always — atomic via tmp + os.replace so a crash mid-write never
        # leaves the IDE rendering a partial record.
        tmp_json = tempfile.NamedTemporaryFile(
            mode="w", delete=False, prefix=f".{node_id}.", suffix=".json.tmp",
            dir=learned_dir, encoding="utf-8",
        )
        try:
            try:
                json.dump(record, tmp_json, ensure_ascii=False, sort_keys=True)
                tmp_json.flush()
                os.fsync(tmp_json.fileno())
            finally:
                tmp_json.close()
            os.replace(tmp_json.name, json_path)
        except Exception:
            # json.dump / os.replace failed — drop the orphan tmp file
            # (``delete=False`` above means it stays on disk otherwise).
            # Debris in learned/ would surface as "the record is corrupt"
            # in the next read; the outer except still swallows the raise.
            try:
                os.unlink(tmp_json.name)
            except OSError:
                pass
            raise

        # Ledger — only when something was actually injected. Own try/except so
        # a DB blip can never cascade back into the JSON/MD writes above
        # (kickoff learn-ledger §"_write_learned_record change").
        if injected:
            try:
                from mini_ork.learning import ledger as _ledger
                _run_id = os.environ.get("MINI_ORK_RUN_ID") or os.path.basename(
                    run_dir.rstrip(os.sep)
                )
                _ledger.record_injections(
                    run_id=_run_id,
                    node_id=node_id,
                    node_type=node_type,
                    lane=lane,
                    task_class=task_class,
                    attempt=int(attempt),
                    sources=record["sources"],
                )
            except Exception:
                # ledger writers are silent on the write path; the outer swallow
                # already protects the dispatch.
                pass

        # Markdown — only when the block was actually injected. A stale .md
        # from a prior attempt that did inject (and the current attempt did
        # not) is removed so the IDE never shows the previous attempt's text
        # as if it were from the current one.
        if injected:
            tmp_md = tempfile.NamedTemporaryFile(
                mode="w", delete=False, prefix=f".{node_id}.", suffix=".md.tmp",
                dir=learned_dir, encoding="utf-8",
            )
            try:
                try:
                    tmp_md.write(block.strip() + "\n")
                    tmp_md.flush()
                    os.fsync(tmp_md.fileno())
                finally:
                    tmp_md.close()
                os.replace(tmp_md.name, md_path)
            except Exception:
                # Same reasoning as the JSON branch — drop the orphan
                # ``.<node_id>.<...>.md.tmp`` file on any failure.
                try:
                    os.unlink(tmp_md.name)
                except OSError:
                    pass
                raise
        else:
            try:
                os.remove(md_path)
            except FileNotFoundError:
                pass
            except OSError:
                pass
    except Exception:
        # Swallow — node dispatch must continue regardless of record-write
        # failure. The cost-of-truth that already happened in
        # ``failure_modes_md`` / ``operator_steering.fetch_for`` cannot be
        # unmade; surfacing an exception here would discard the LLM work.
        pass


@_node_publish_boundary
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

    # Provenance of the routing decision that produced ``lane`` — read from the
    # context the policy layer just wrote. Persisting it is what lets an outcome
    # be credited to (or debited from) the decision that caused it; without it a
    # learned route, an explore swap, and a recipe pin are indistinguishable
    # after the fact.
    _route_prov = last_route_provenance()

    # Node lifecycle (restored feed): one node_start before dispatch and one
    # node_end at the trace() completion seam. The start map is LOCAL to this
    # dispatch so a pool child that re-enters the path (via
    # _bootstrap_recipe_register) still emits exactly one pair per node, never
    # one per process.
    node_start_ms: dict[str, int] = {}

    # Bind the resolved lane into every trace() call so agent_version_id is stamped
    # (bash passes the shell var dispatch_lane into _trace_write_node_rich's payload).
    def trace(node_id, status, node_type, output_file="", verdict="", finish_reason=""):
        _base_trace(node_id, status, node_type, output_file, verdict, finish_reason,
                    lane=lane,
                    route_source=_route_prov.get("route_source", ""),
                    route_explore=bool(_route_prov.get("route_explore")),
                    route_score=_route_prov.get("route_score"),
                    route_margin=_route_prov.get("route_margin"),
                    predicted_error=_route_prov.get("predicted_error"))
        # F4: publish the durable checkpoint at the SAME single seam as the
        # trace write. The wrapper unifies node-completion side effects so
        # E2's recovery code can rely on every success also having a row.
        _base_checkpoint(node_id, status, node_type, output_file)
        # Node-end at the same seam: duration from the recorded start, else 0
        # when no start was recorded (early-return path) — a node_end without a
        # node_start is the reader's "done/failed" signal and beats silence.
        duration_ms = 0
        if node_id in node_start_ms:
            duration_ms = max(0, _now_ms() - node_start_ms.pop(node_id))
        mo_node_end(run_id, node_id, node_type, duration_ms,
                    verdict=verdict, artifact_path=output_file,
                    finish_reason=finish_reason, db=db)
    # Resolve this run's artifact root from its STABLE identity (run_id) via the
    # artifact store — NOT from an ambient MINI_ORK_RUN_DIR. A long-lived worker
    # can leak that env var and split one run across two directories (producer
    # writes here, verifier reads there); run_id is injected once and never
    # leaks, so keying resolution on it makes that split structurally impossible.
    # base_dir=run_dir keeps the caller's authoritative plan-derived path as the
    # fallback for benchmark/test runs that don't live under <home>/runs.
    _artifact_store = make_artifact_store(run_id, base_dir=run_dir)
    run_dir_eff = str(_artifact_store.run_root)
    # Node prompts and subprocess verifiers refer to MINI_ORK_RUN_DIR as their
    # artifact namespace. ``mini-ork run`` can derive the directory from the
    # plan without exporting it, so publish the resolved value at the node
    # boundary before any provider or verifier subprocess is invoked.
    publish_env({ENV_RUN_DIR: run_dir_eff})

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
                # Reuse the run_id-addressed store built above so producer and
                # consumer nodes resolve to the SAME physical root regardless of
                # any leaked ambient run-dir.
                artifact_ledger = ArtifactLedger(store=_artifact_store)
                prepared_inputs = artifact_ledger.prepare_inputs(compiled_workflow, node_id)
                artifact_context = artifact_ledger.prompt_context(prepared_inputs)
                publish_env({
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
        publish_env({
            "MINI_ORK_NODE_INPUT_MANIFEST": None,
            "MINI_ORK_NODE_INPUT_DIR": None,
        })
    # Capability envelope (SE-3 Phase B2): publish the node's harness-level
    # declarations (workflow.yaml mcp_servers/skills/agent_doc) on the node
    # bus. Masked to None when the node declares nothing so a prior node's
    # envelope can never leak into the next dispatch (MO_RESUME_SESSION_ID
    # discipline). dispatch_model rejects lanes whose engine cannot translate
    # a declared axis instead of silently dropping it.
    _envelope: dict[str, str | None] = {
        "MO_MCP_SERVERS": None,
        "MO_SKILLS": None,
        "MO_AGENT_DOC": None,
    }
    if compiled_workflow is not None and node_id in compiled_workflow.nodes:
        _decl = compiled_workflow.nodes[node_id]
        if _decl.mcp_servers:
            _envelope["MO_MCP_SERVERS"] = ",".join(_decl.mcp_servers)
        if _decl.skills:
            _envelope["MO_SKILLS"] = ",".join(_decl.skills)
        if _decl.agent_doc:
            _envelope["MO_AGENT_DOC"] = _decl.agent_doc
    publish_env(_envelope)
    # Snapshot the tree BEFORE any implementer node edits it, so the reviewer
    # diff captures only the implementer's delta (not pre-existing dirt from a
    # concurrent session sharing this in-place working tree). Non-destructive.
    # Never from a rollback node: by then the tree already holds the run's
    # edit, and the in-place rollback restores TO this snapshot.
    if node_type != "rollback":
        _capture_pre_impl_baseline(run_dir_eff)
    cost_sidecar = os.path.join(run_dir_eff, ".last-llm-cost")

    def _charge():
        charge_node_cost(db, run_id, cost_sidecar, root=root)

    # Export the role-aware fallback chain (lead = resolved lane) so a python
    # dispatch backend routes around a hung/flaky lead lane (bash:2224-2225, NEW-5).
    from mini_ork.dispatch.llm_dispatch import resolve_lane_family
    _chain_lead = resolve_lane_family(lane)
    publish_env({ENV_DISPATCH_CHAIN: dispatch_chain(node_type, _chain_lead)})

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

    recipe_dir = os.path.join(_recipe_root(root), "recipes", recipe) if recipe else ""
    prompt_file = resolve_prompt_file(root, recipe, prompt_ref, node_type)
    override_dir = os.environ.get("MINI_ORK_PROMPT_OVERRIDE_DIR", "").strip()
    override_name = os.path.basename(prompt_ref) if prompt_ref else ""
    if (
        override_dir
        and override_name
        and prompt_file == os.path.join(override_dir, override_name)
    ):
        print(
            f"[prompt-override] node_id={node_id} using {override_name} "
            "from MINI_ORK_PROMPT_OVERRIDE_DIR",
            file=sys.stderr,
        )

    plan_content = open(plan_path).read() if plan_path and os.path.isfile(plan_path) else ""
    # F5-B: reflect-learned failure modes + operator steering, injected after node_desc
    # in the LLM prompts (the read side of the learning loop). Empty for non-LLM nodes.
    # The routed ``lane`` and the ``node_id`` travel with the injection so the
    # retrieval ledger can attribute the memory spend to the decision that
    # caused it (LIMBO); note ``node_id`` is a local here — the env publish that
    # would make it ambient happens on the next line, too late for this call.
    # ``learned_block_sources`` collects exactly what was injected so the IDE
    # "Learning" tab can show the learner what it actually received.
    learned_sources: list[dict] = []
    learned = _learned_block(root, task_class, node_type, lane, node_id,
                             sources=learned_sources)
    if node_type in ("researcher", "implementer", "reviewer"):
        _write_learned_record(
            run_dir_eff, node_id, node_type, lane, task_class,
            attempt=_node_attempt_no(db, run_id, node_id),
            block=learned, sources=learned_sources,
        )
    # Publish the per-node identity + clear any stale resume session in one
    # canonical step (None removes the variable).
    publish_env(node_env_overrides(
        node_id=node_id, run_dir=run_dir_eff, resume_session_id=None,
        attempt=str(_node_attempt_no(db, run_id, node_id)),
        input_hash=hashlib.sha256(f"{run_id}|{node_id}|{recipe}".encode()).hexdigest(),
        node_type=node_type))

    # (E4 turn-resume) During an active recovery, restore this node's persisted
    # transcript and export MO_RESUME_SESSION_ID so a claude lane continues the
    # interrupted conversation (`--resume <id>`, via providers.dispatch_model)
    # instead of starting the node over. Strictly recovery-scoped and fail-soft:
    # off recovery, for codex/gemini, or with no session it is a no-op and the
    # node runs normally.
    if (context_env("MINI_ORK_RECOVERY_CLOSURE").strip()
            or context_env("MINI_ORK_RECOVERY_FROM").strip()):
        try:
            from mini_ork.recovery.resume_prep import prepare_node_resume  # noqa: PLC0415
            _resume_sid = prepare_node_resume(
                db, run_id, node_id, run_dir=run_dir, model=lane,
                cwd=context_env(ENV_TARGET_CWD) or None,
            )
            if _resume_sid:
                publish_env({ENV_RESUME_SESSION_ID: _resume_sid})
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
    # Emit node_start immediately before the handler runs, reusing the already
    # resolved ``lane`` (never re-resolve), and record the start time so the
    # matching node_end can carry a duration. remote-nodes-14 §4: under a
    # placement the payload also says where the node runs.
    placement, node_host, session_id = _node_placement(node_type, lane, run_dir_eff)
    mo_node_start(run_id, node_id, node_type, model_lane=lane, db=db,
                  placement=placement, node_host=node_host, session_id=session_id)
    node_start_ms[node_id] = _now_ms()
    rc, finish_reason = handler(ctx)
    # Handlers that never call trace() (verifier/publisher/rollback/eval) must
    # still close their node — an orphaned node_start renders the node
    # permanently "running" in the DAG. trace() pops the start-map entry, so
    # this fires only for the handlers that bypassed the trace seam.
    if node_id in node_start_ms:
        # The real duration (it was a hard-coded 0, so every Python-era verifier
        # node_end read "0 ms" whether it ran or not — K0.5c AC0).
        started = node_start_ms.pop(node_id, None)
        mo_node_end(run_id, node_id, node_type,
                    max(0, _now_ms() - started) if started is not None else 0,
                    verdict=ctx.node_verdict, finish_reason=finish_reason, db=db)
    return rc, finish_reason


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
    # A handler that never calls trace() can leave a reason here; the fallback
    # node_end carries it as the payload verdict (e.g. verifier_not_executed).
    node_verdict: str = ""

    @property
    def recipe_eff(self) -> str:
        return self.recipe or os.environ.get("MINI_ORK_RECIPE", "")

    def prepend(self) -> str:
        return (f"\n\n--- Recipe prompt (system context) ---\n{open(self.prompt_file).read()}"
                f"\n--- /recipe prompt ---\n\n") \
            if self.prompt_file and os.path.isfile(self.prompt_file) else ""

    def scope_guard(self) -> str:
        return scope_guard_block(self.run_dir_eff)

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


def scope_guard_block(run_dir: str) -> str:
    """F6a: keep agentic lanes (codex / claude CLI) out of OTHER runs'
    artifacts. Live-DB receipt: .mini-ork/runs held 529 past runs / 3.8 GB
    inside MO_TARGET_CWD, and lens agents enumerated them — every tool
    round-trip re-billed that context (worst measured call: 14.77M input
    tokens, $2.89; 27 sibling lens calls returned zero output). No lens,
    researcher, or reviewer legitimately needs another run's directory.

    The same block also carries the repository write guard (B5): a lane that
    commits/merges/pushes corrupts the operator's branch and bypasses review.
    The framework owns all three; the lane only edits the working tree. And the
    resource guard (B7): a suite run must be scoped to a file path, never a bare
    name filter, which collects the whole repo and fans out one worker per file."""
    return (
        "\n--- Scope guard (hard constraint) ---\n"
        f"The ONLY run directory you may read is: {run_dir}\n"
        "Do NOT enumerate, glob, grep, or read files under any other run "
        "directory (sibling runs under .mini-ork/runs/), .git internals, or "
        "node_modules. Past-run artifacts are not evidence for this task and "
        "scanning them wastes the context budget.\n"
        "--- /scope guard ---\n"
        "\n--- Repository write guard (hard constraint) ---\n"
        "Never run `git commit`, `git merge`, `git rebase`, or `git push`. The "
        "framework owns commit, merge and push; a lane that pushes to a shared "
        "branch corrupts the operator's worktree and bypasses review. Leave "
        "your edits in the working tree — the framework will pick them up. "
        "Push is disabled at the process level for this lane, so a forced "
        "attempt can only fail and waste your budget.\n"
        "--- /repository write guard ---\n"
        "\n--- Resource guard (hard constraint) ---\n"
        "If you run tests, pass the changed test FILE path — never a bare name "
        "filter (`-t` / `--testNamePattern` / `-k` alone). A name filter "
        "collects the whole repo and spawns one worker per file, which has "
        "saturated this machine before. Add `--maxWorkers=2` when the runner "
        "supports it.\n"
        "--- /resource guard ---\n")


def _handle_planner_early(root):
    print("  [skip] planner node handled by the Python plan runtime")
    return 0, "done"


def _handle_reflector_early(root):
    # Preserve bash's `… || true`: reflection is a side-channel and must
    # never fail the workflow node. Capturing output also prevents the
    # reflect report from leaking into execute's stdout contract.
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "mini_ork.cli.reflect"],
            capture_output=True,
            timeout=_reflect_timeout_seconds(),
            env={**_module_env(root), "MINI_ORK_ROOT": root},
        )
    except subprocess.TimeoutExpired:
        # Surface it: a silently-skipped reflection is how a gradient outage
        # stayed invisible for hours (the learning loop read as "nothing to
        # learn" while dispatch was actually refused).
        print("  [reflect] timed out — reflection skipped this node "
              "(learning loop lags; run `mini-ork reflect` to catch up)",
              file=sys.stderr)
        return 0, "done"
    except OSError as exc:
        print(f"  [reflect] could not launch reflection ({exc})", file=sys.stderr)
        return 0, "done"
    if proc.returncode != 0:
        tail = (proc.stderr or b"").decode("utf-8", "replace").strip().splitlines()
        note = tail[-1] if tail else f"rc={proc.returncode}"
        print(f"  [reflect] reflection reported a problem: {note[:200]}", file=sys.stderr)
    return 0, "done"


EARLY_NODE_HANDLERS: dict[str, Callable] = {
    "planner": _handle_planner_early,
    "reflector": _handle_reflector_early,
}


def _first_json_object(text: str, required_key: str | None = None) -> dict | None:
    """First brace-balanced ``{...}`` in ``text`` that parses to a dict.

    Mirrors the scanner in ``mini_ork/gates/rubric_scoring.py`` (depth counter
    that respects string literals and backslash escapes), minus the keyed start
    pattern: a lens prints the object itself, so any ``{`` may begin it. When
    ``required_key`` is given, a candidate lacking that key is skipped rather
    than accepted, so recovery stays anchored to the artifact's own shape.
    """
    for start in range(len(text)):
        if text[start] != "{":
            continue
        depth, in_str, esc = 0, False, False
        for i in range(start, len(text)):
            c = text[i]
            if esc:
                esc = False
                continue
            if c == "\\":
                esc = True
                continue
            if c == '"':
                in_str = not in_str
                continue
            if in_str:
                continue
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(text[start:i + 1])
                    except Exception:
                        break
                    if isinstance(obj, dict) and (required_key is None or required_key in obj):
                        return obj
                    break
    return None


def _materialize_lens_json(run_dir: str, node_id: str, text: str) -> None:
    """Honour the lens contract's ``lens-<family>.json`` when the agent never wrote it.

    The lens prompt asks each agent to do two things: emit the object (the
    researcher handler captures stdout to ``lens-<family>.md``) and write
    ``$MINI_ORK_RUN_DIR/lens-<family>.json`` with a tool. Only the first is
    enforced by the harness, so the second is a model-side coin flip, and it
    fails in *both* directions:

      * the agent prints the object and writes nothing — the bytes are in stdout;
      * the agent writes the object to the declared ``lens-<family>.md`` with its
        file tool and prints only prose (``Lens emitted. C3=6 …`` plus a summary)
        — the bytes are in the sibling, and stdout has no ``{`` at all.

    Either way ``recipes/chapter-review/verifiers/panel-completeness.py`` reads a
    ``.json`` that is not there, the synthesizer never emits ``chapter-review.json``,
    and the whole chapter-review is discarded as ``failed_nodes=4`` with the rubric
    verdict never flipping. Recover the file from whichever source holds the object.

    Fail-soft by design: an existing non-empty sibling wins, and any failure
    degrades to today's behaviour (verifier red) rather than crashing the node.
    """
    if not node_id.endswith(("_lens", "-lens")):
        return
    path = os.path.join(run_dir, f"lens-{node_id[:-5]}.json")
    try:
        if os.path.isfile(path) and os.path.getsize(path) > 0:
            return
        obj = _first_json_object(text, required_key="lens")
        if obj is None:
            # The declared output file is the other place the object can land;
            # it is prose when the agent printed the object instead.
            declared = f"{path[:-len('.json')]}.md"
            if os.path.isfile(declared):
                with open(declared, encoding="utf-8") as handle:
                    obj = _first_json_object(handle.read(), required_key="lens")
        if obj is None:
            return
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(obj, handle, indent=2)
        print(f"  [ok] lens artifact recovered from agent output: {os.path.basename(path)}",
              file=sys.stderr)
    except Exception:
        pass


def _declared_node_output_paths(ctx: NodeDispatch) -> list[str]:
    """Absolute paths of the node's per-node workflow ``outputs`` ([] if none)."""
    if ctx.artifact_ledger is None or ctx.compiled_workflow is None:
        return []
    try:
        outputs = ctx.compiled_workflow.nodes[ctx.node_id].outputs
        return [str(ctx.artifact_ledger.output_path(ctx.compiled_workflow, ctx.node_id, name))
                for name in outputs]
    except Exception:
        return []


def _dispatch_started_at(marker: str) -> float | None:
    try:
        return os.path.getmtime(marker)
    except OSError:
        return None


def _stamp_dispatch_marker(marker: str) -> float | None:
    """Touch the dispatch-start marker and return its mtime. Fail-soft: None
    disables artifact completion (fail-closed), never the dispatch itself."""
    try:
        os.makedirs(os.path.dirname(marker) or ".", exist_ok=True)
        open(marker, "w").close()
    except OSError:
        return None
    return _dispatch_started_at(marker)


def _completed_via_artifacts(ctx: NodeDispatch, paths: list[str],
                             since_mtime: float | None, rc: int, finish_reason: str) -> bool:
    """Whether a failed dispatch (rc != 0) still delivered the node.

    The (rc, text) handshake is the agent's self-report; the declared artifacts
    are the deliverable. Completion requires ALL of them to be fresh (written
    after ``since_mtime``, the dispatch-start marker), non-empty, and parseable,
    and the node must not opt out with ``strict_handshake``. Anything else keeps
    the original failure. The distinct ``node.artifact_completion`` run_event
    lets the learning loop tell these apart from a clean handshake (the payload
    carries no ``finish_reason`` key: run_events copies that into a column that
    consumers read as the node's outcome).
    """
    if not paths or since_mtime is None:
        return False
    if node_strict_handshake(ctx.compiled_workflow, ctx.workflow, ctx.node_id):
        return False
    ok, why = declared_artifacts_ok(paths, since_mtime=since_mtime)
    if not ok:
        print(f"  [artifact-check] node_id={ctx.node_id}: not complete — {why}", file=sys.stderr)
        return False
    print(f"  {ARTIFACT_COMPLETION_LOG}: node_id={ctx.node_id} rc={rc} "
          f"suppressed_finish_reason={finish_reason}", file=sys.stderr)
    try:
        mo_node_emit(ctx.run_id, ctx.node_id, ctx.node_type, "node.artifact_completion",
                     json.dumps({"rc": rc, "suppressed_finish_reason": finish_reason,
                                 "artifacts": paths}), db=ctx.db)
    except Exception:
        pass
    return True


def _handle_researcher(ctx: NodeDispatch):
    out_file = ctx.declared_output_path(
        _researcher_output_file(ctx.run_dir, ctx.recipe_eff, ctx.node_id)
    )
    os.makedirs(os.path.dirname(out_file) or ".", exist_ok=True)
    prompt = (f"{ctx.prepend()}Task: {ctx.node_desc}{ctx.learned}\n\nPlan context:\n"
              f"{ctx.plan_content}{ctx.artifact_context}{ctx.scope_guard()}\n\n"
              f"Write your output to: {out_file}")
    marker = os.path.join(ctx.run_dir, f".dispatch-marker-{ctx.node_id}")
    open(marker, "w").write("")
    rc, result = ctx.dispatch(prompt)
    if rc != 0:
        fr = finish_reason_for_failure(rc, result)
        declared = _declared_node_output_paths(ctx) or [out_file]
        if not _completed_via_artifacts(ctx, declared, _dispatch_started_at(marker), rc, fr):
            ctx.trace(ctx.node_id, "failure", "researcher", out_file, "", fr)
            return 1, fr
        # The agent's own files are the deliverable; partial stdout from an
        # aborted dispatch must not overwrite them.
    else:
        ctx.write_preserving_agent(out_file, marker, result)
    _materialize_lens_json(ctx.run_dir, ctx.node_id, result)
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


# Recipes whose implementer exists to edit the target tree. framework-edit
# enforces this in its own ground-truth harvest; recipes whose implementer
# nodes synthesize or research (and legitimately touch no files) stay out.
# `docs` (`doc_editor`) edits documentation in the target tree; its
# grep_assert + link_verifier verifiers pass vacuously on an untouched tree,
# so the same no-change guard that `code-fix` enforces applies (pilot
# mo-9a0cf68ccf).
_RECIPES_REQUIRING_TREE_CHANGES = frozenset({"code-fix", "docs"})

# Run-dir-relative deliverables of a recipe's implementer when its workflow node
# declares no ``outputs``. recursive-validate-impl's tier1-3 verifiers read
# implementer-summary.json, so an implementer that wrote it fresh has delivered
# even when dispatch exited non-zero (watchdog rc=124 after the edits landed).
# Recipes not listed here get no artifact completion: a failed dispatch fails.
_IMPLEMENTER_COMPLETION_ARTIFACTS: dict[str, tuple[str, ...]] = {
    "recursive-validate-impl": ("implementer-summary.json",),
}


def _read_revise_feedback(run_dir: str) -> dict | None:
    """Read the revise-round state the runtime wrote, if any.

    ``<run_dir>/revise/current.json`` is the process-crossing channel: the
    dispatch loop writes it (execute.py's pool children can't see publish_env),
    and the implementer appends the referenced feedback file to its prompt.
    Returns ``None`` when the file is absent — the no-revise fast path.
    """
    if not run_dir:
        return None
    current_path = os.path.join(run_dir, "revise", "current.json")
    if not os.path.isfile(current_path):
        return None
    try:
        with open(current_path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    round_no = data.get("round")
    max_rounds = data.get("max_rounds", 2)
    feedback_path = data.get("feedback") or ""
    feedback_text = ""
    if feedback_path and os.path.isfile(feedback_path):
        try:
            with open(feedback_path, encoding="utf-8", errors="replace") as fh:
                feedback_text = fh.read()
        except OSError:
            feedback_text = ""
    return {"round": round_no, "max_rounds": max_rounds, "feedback_text": feedback_text}


def _review_round_arm(run_id: str) -> str:
    """``MO_REVIEW_ROUND_AWARE``: ``off`` (default) | ``on``. Under ``on``, a
    deterministic ``MO_REVIEW_ROUND_AWARE_HOLDOUT`` share of runs (default 0.2,
    by run id) keeps the plain reviewer, so the effect can be measured."""
    if context_env("MO_REVIEW_ROUND_AWARE", "off").strip().lower() not in ("on", "1", "true"):
        return "off"
    try:
        rate = min(max(float(context_env("MO_REVIEW_ROUND_AWARE_HOLDOUT", "0.2")), 0.0), 1.0)
    except ValueError:
        rate = 0.2
    if run_id and rate > 0.0:
        digest = hashlib.sha1(f"review-rounds:{run_id}".encode("utf-8")).hexdigest()[:8]
        if int(digest, 16) / 0xFFFFFFFF < rate:
            return "holdout"
    return "on"


def _reviewer_round_block(run_dir: str, run_id: str) -> str:
    """Round awareness + a severity bar for the plain reviewer, or ``""``.

    Measured 2026-10-07: framework-edit runs that rolled back after their revise
    rounds mostly hit NEW blockers each round (regressions of the last fix, or
    issues visible since round 1 but reported late), and the reviewer did not
    know that a final-round needs_revision discards the whole delivery. This
    block tells it which attempt it is judging and to block only on high
    severity. It changes what gets approved, so it ships behind a default-off
    flag with a holdout arm; the arm and attempt are recorded in
    ``review-round-aware.json`` for the comparison.
    """
    arm = _review_round_arm(run_id)
    if arm == "off":
        return ""
    revise = _read_revise_feedback(run_dir)
    try:
        cap = max(0, int(context_env("MO_REVISE_ROUNDS", "2")))
    except ValueError:
        cap = 2
    if revise:
        attempt = int(revise.get("round") or 1) + 1
        total = int(revise.get("max_rounds") or cap) + 1
    else:
        attempt, total = 1, cap + 1
    final = attempt >= total
    try:
        record_path = os.path.join(run_dir, "review-round-aware.json")
        records = []
        if os.path.isfile(record_path):
            with open(record_path, encoding="utf-8") as fh:
                loaded = json.load(fh)
            records = loaded if isinstance(loaded, list) else []
        records.append({"arm": arm, "attempt": attempt, "total": total, "final": final,
                        "at": int(time.time())})
        with open(record_path, "w", encoding="utf-8") as fh:
            json.dump(records, fh)
    except (OSError, ValueError):
        pass
    if arm != "on":
        return ""
    consequence = ("on this FINAL attempt it discards the whole delivery and the run rolls back."
                   if final else "the implementer gets one more attempt to fix them.")
    lines = [
        f"## Review attempt {attempt} of {total}",
        f"A fail or needs_revision verdict sends your findings back to the implementer; {consequence}",
        "- Return fail or needs_revision only for HIGH-severity findings: the change is wrong, "
        "unsafe, breaks a test, or misses a requirement of the kickoff. For medium and low "
        "findings, pass and list them in your notes/findings.",
    ]
    if attempt > 1 and revise:
        from mini_ork.context_assembler import cap_block
        lines.append("- First check that every finding from the previous attempt (below) is "
                     "fixed. Raise a NEW blocking finding only if it is high severity.")
        lines.append(cap_block(revise.get("feedback_text") or "",
                               label=f"revise/round-{attempt - 1}.md"))
    if final:
        lines.append("- This is the last attempt: block only if shipping this change would be "
                     "worse than discarding all of it.")
    return "\n".join(lines) + "\n\n"


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
        script = os.path.join(_recipe_root(ctx.root), "recipes", script_rel)
        if not os.path.isfile(script):
            print(f"dispatcher script missing: {script}", file=sys.stderr)
            ctx.trace(ctx.node_id, "failure", "implementer", sub_log, "", "error")
            return 1, "error"
        os.makedirs(os.path.dirname(fallback_sub_log) or ".", exist_ok=True)
        os.makedirs(os.path.dirname(sub_log), exist_ok=True)
        rc = subprocess.run([sys.executable, script]).returncode
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
    # Revise loop: when the runtime wrote <run_dir>/revise/current.json, the
    # failed gates' findings are the ONLY thing the next attempt must act on.
    # Re-sending the whole first-attempt context (recipe prompt + plan + artifact
    # context) to fix one finding is a fresh full session for a delta-sized job —
    # every extra round re-pays for the same background the agent already had.
    # So a revise round keeps the implementer header + the revision-round heading
    # (the markers every harness and reader keys on) but drops the first-attempt
    # background: the agent gets the findings and the working tree it must fix on
    # top of. MO_REVISE_FULL_CONTEXT=1 restores the old full-context prompt.
    from mini_ork.context_assembler import cap_block
    revise = _read_revise_feedback(ctx.run_dir_eff or ctx.run_dir)
    if revise and context_env("MO_REVISE_FULL_CONTEXT", "0") != "1":
        feedback = cap_block(
            revise["feedback_text"],
            label=f"revise/round-{revise['round']}.md",
        )
        prompt = (f"{ctx.scope_guard()}\n"
                  f"Implement: {ctx.node_desc}\n\n"
                  f"## Revision round {revise['round']} of {revise['max_rounds']}\n"
                  "A checker reviewed your previous attempt on this task and found the "
                  "problems below. Fix ONLY these problems, on top of the changes already "
                  "in the working tree. Do not start over, and do not revert your earlier "
                  "work.\n\n"
                  f"{feedback}\n\n"
                  "Your previous changes are already in the working tree — run `git diff` "
                  "to see them, and do not re-explore the repository from scratch.\n\n"
                  f"Write your execution summary to: {impl_log}")
    else:
        prompt = (f"{ctx.prepend()}Implement: {ctx.node_desc}{ctx.learned}\n\nPlan:\n"
                  f"{ctx.plan_content}{ctx.artifact_context}{ctx.scope_guard()}\n\n"
                  f"Write your execution summary to: {impl_log}")
        if revise:
            feedback = cap_block(
                revise["feedback_text"],
                label=f"revise/round-{revise['round']}.md",
            )
            prompt += (f"\n\n## Revision round {revise['round']} of {revise['max_rounds']}\n"
                       f"{feedback}")
    os.makedirs(os.path.dirname(impl_log) or ".", exist_ok=True)
    # F4: pin the codex/gemini edit surface to the TARGET repo (kickoff's git
    # toplevel), not os.getcwd(). Without this the implementer diff/writes land
    # in mini-ork's own tree when cwd != target — the CWT-A corruption hazard
    # (bash _dispatch_node:2626-2642). Export so cl_codex.sh reads it.
    #
    # Prefer the persisted roots record when present (run_profile.json["roots"]);
    # fall back to today's lazy resolution when the record is absent (legacy run
    # dirs created before epic 01 landed). The drive redirect still applies on
    # top of the resolved target — see req #4 of remote-nodes-01.
    roots = load_run_roots(ctx.run_dir_eff or ctx.run_dir)
    target = roots.target if roots else _resolve_target_cwd(ctx.run_dir_eff)
    # P1b: opt-in shared-drive routing. No-op unless MO_SHARED_DRIVE_BACKEND is
    # set, so the default host-tree cwd is unchanged; when set, every node in the
    # run shares one virtual drive (lazy import keeps the seam side-effect-free).
    from mini_ork.runtime.run_drive import resolve_run_drive_cwd
    target = resolve_run_drive_cwd(target)
    publish_env({ENV_TARGET_CWD: target})
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

    run_root = ctx.run_dir_eff or ctx.run_dir
    marker = os.path.join(run_root, f".dispatch-marker-{ctx.node_id}")
    dispatch_started = _stamp_dispatch_marker(marker)
    rc, result = ctx.dispatch(prompt)
    try:
        os.remove(marker)
    except OSError:
        pass
    if rc != 0:
        fr = finish_reason_for_failure(rc, result)
        declared = _declared_node_output_paths(ctx) or [
            os.path.join(run_root, rel)
            for rel in _IMPLEMENTER_COMPLETION_ARTIFACTS.get(ctx.recipe_eff, ())
        ]
        if not _completed_via_artifacts(ctx, declared, dispatch_started, rc, fr):
            ctx.trace(ctx.node_id, "failure", "implementer", impl_log, "", fr)
            return 1, fr
    open(impl_log, "w").write(result)
    if rc == 0:
        # A truncated diff from an aborted dispatch must never be applied; the
        # artifact-completed path keeps only the edits the agent already made.
        apply_impl_output(impl_log, target)   # ported "capture coin-flip" applier
    if ctx.recipe_eff == "framework-edit":
        moved = _implementer_moved_base(ctx.run_dir_eff, target)
        if moved:
            print(f"  [ground-truth] FAIL: {moved}", file=sys.stderr)
            _append_run_note(ctx.db, ctx.run_id, f"impl_moved_base: {moved}")
            ctx.trace(ctx.node_id, "failure", "implementer", impl_log, "", "impl_moved_base")
            return 1, "impl_moved_base"
        ok, fr = _harvest_framework_edit_ground_truth(ctx.run_dir_eff, target)
        if not ok:
            ctx.trace(ctx.node_id, "failure", "implementer", impl_log, "", fr)
            return 1, fr
    if ctx.recipe_eff == "self-migrate":
        harvested = _harvest_self_migrate_artifacts(ctx.run_dir_eff, target)
        _write_self_migrate_implementer_summary(
            ctx.run_dir_eff, target, impl_log, harvested
        )
    else:
        changed = _write_implementer_summary(ctx.run_dir_eff, target, impl_log,
                                             since_mtime=dispatch_started)
        if ctx.recipe_eff in _RECIPES_REQUIRING_TREE_CHANGES and changed == []:
            # The model reported success but git sees no change: pilot task
            # mo-9a0cf68ccf ran every downstream node on an untouched tree and
            # still read as "implemented". Fail here, where the cause is known.
            print("  [ground-truth] FAIL: implementer produced no tree changes",
                  file=sys.stderr)
            ctx.trace(ctx.node_id, "failure", "implementer", impl_log, "", "impl_no_changes")
            return 1, "impl_no_changes"
        _capture_pre_impl_fixture(ctx.run_dir_eff, target)
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


REVIEWER_VERDICT_UNPARSEABLE = "reviewer_verdict_unparseable"


def _implementer_moved_base(run_dir: str, target: str) -> str:
    """Why the implementer's tree delta cannot be trusted, or ``""``.

    The ground-truth harvest diffs the target against ``pre-implementer-ref``,
    the commit HEAD was on before the implementer ran. If the implementer
    commits, rebases, resets or checks out another ref, HEAD moves and the
    delta carries every commit between the two: 2026-10-08,
    sdd-i5-evidence-ledger-20261008122008 rebased onto origin/main mid-run and
    shipped a 54-file diff for a 6-file change, then spent two revise rounds
    chasing it. Fail the node here, with the cause, instead. ``""`` when the
    check cannot run (no baseline, not a git repo).

    A dirty tree at snapshot time makes ``_snapshot_pre_impl_ref`` record a
    ``git stash create`` WIP commit as the baseline (so the harvest diff
    excludes the pre-existing dirt). That stash commit's FIRST parent is the
    HEAD the run started from, so HEAD == that parent also means the base never
    moved — the implementer only left its change uncommitted, as required
    (2026-10-09, run-1791543223-76795 failed ``impl_moved_base`` on exactly
    this shape: HEAD da191c35 vs the "WIP on main: da191c35" stash 92dd9611).
    """
    if not run_dir or not target:
        return ""
    try:
        baseline = open(os.path.join(run_dir, "pre-implementer-ref")).read().strip()
    except OSError:
        return ""
    if not baseline:
        return ""
    head = subprocess.run(["git", "-C", target, "rev-parse", "HEAD"],
                          capture_output=True, text=True)
    if head.returncode != 0:
        return ""
    head_sha = head.stdout.strip()
    if head_sha == baseline:
        return ""
    # Stash-shaped baseline (>= 2 parents) whose first parent is HEAD: the run's
    # own dirt snapshot, not a moved base. A normal commit has exactly 1 parent,
    # and HEAD matching ITS parent would be a backward reset — still a failure.
    parents = subprocess.run(
        ["git", "-C", target, "rev-list", "--parents", "-1", baseline],
        capture_output=True, text=True)
    if parents.returncode == 0:
        parts = parents.stdout.split()
        if len(parts) >= 3 and parts[1] == head_sha:
            return ""
    return (f"HEAD {head_sha[:12]} is not the run's starting commit {baseline[:12]}: the implementer "
            "committed, rebased, reset or checked out in the target, so the tree delta would carry "
            f"unrelated commits. Restore HEAD to {baseline[:12]} and leave the change uncommitted.")


def _append_run_note(db: str, run_id: str, note: str) -> None:
    """Append ``note`` to ``task_runs.notes``. Warns (does not raise): the trace
    already carries the reason, and the node's own failure must not be masked."""
    if not db or not run_id or not os.path.isfile(db):
        return
    try:
        con = sqlite3.connect(db, timeout=15.0)
        try:
            con.execute("PRAGMA busy_timeout = 15000")
            con.execute("UPDATE task_runs SET notes = COALESCE(notes || '; ', '') || ? WHERE id = ?",
                        (note, run_id))
            con.commit()
        finally:
            con.close()
    except sqlite3.Error as exc:
        print(f"  [warn] could not record '{note}' in task_runs.notes: {exc}", file=sys.stderr)


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
                  f"Plan:\n{ctx.plan_content}{ctx.artifact_context}{ctx.scope_guard()}\n\n"
                  f"Write strict JSON to: {review_file}")
    elif is_synth:
        prompt = (f"{ctx.prepend()}Synthesize for: {ctx.node_desc}{ctx.learned}\n\n"
                  f"Plan:\n{ctx.plan_content}{ctx.artifact_context}{ctx.scope_guard()}\n\n"
                  f"Write your synthesis to: {review_file}")
    else:
        reviewer_inputs = _assemble_reviewer_inputs(ctx.run_dir_eff)
        # Deterministic no-op rejection. The implementer declared changes and the
        # tree contains none; dispatching the model here is exactly how a no-op run
        # earned a "pass" (the reviewer read ambient worktree state as if it were
        # this run's output). Block before the spend, not after the verdict.
        if os.path.isfile(os.path.join(ctx.run_dir_eff or "", "review-diff-noop.json")):
            print("  [fail] reviewer: implementer declared changes but produced no diff "
                  "(review-diff-noop.json)", file=sys.stderr)
            ctx.trace(ctx.node_id, "failure", "reviewer", "", "no_op", "no_op")
            return 1, "no_op"
        # The panel/synth branches above hand the reviewer a FILE to write
        # (`Write strict JSON to: {review_file}`); this branch asked only for an
        # inline answer, and that is the branch that loses verdicts. The capture
        # (`claude_result_text`) keeps the lane's FINAL assistant message, and a
        # nested claude CLI appends its own `<z-insight>` block + closing prose —
        # so a judge that emitted the JSON object mid-conversation and then a
        # sign-off is captured with no brace in it at all. Measured 2026-10-06 on a
        # consumer RSI campaign: the 3.2 kB verdict object was assistant message #12, the
        # 271 B prose sign-off was #13, `review-opus_judge.json` held #13, and the
        # runner reported `failed:judge_evidence_missing` on a judge that had run
        # correctly. Naming the file restores the branch to the same contract as
        # panel/synth: the agent writes it, and `write_preserving_agent` keeps an
        # agent-written output (routing the captured text to `.stdout.md`).
        prompt = (f"{ctx.prepend()}Review the implementation for: {ctx.node_desc}{ctx.learned}\n\n"
                  f"Plan:\n{ctx.plan_content}{ctx.artifact_context}{ctx.scope_guard()}\n\n{reviewer_inputs}\n"
                  f"{_reviewer_round_block(ctx.run_dir_eff or ctx.run_dir, ctx.run_id)}"
                  'Respond with JSON: {"verdict": "pass|fail|needs_revision", "notes": []}\n'
                  f'AND write that exact JSON object to {review_file} — a Bash heredoc is fine. '
                  'That file is the artifact the runtime reads; an answer that is prose only is discarded.')
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
    # Hand the verdict to the publisher through run_dir, NOT the environment: nodes
    # execute in a ProcessPoolExecutor (cli/execute.py), so a publish_env write in
    # this worker dies with the worker and the publisher's own process observes
    # nothing — REVIEW_FILE/VERDICT read as "" there, which silently skipped every
    # commit. publisher._publisher_try_commit_files already scans run_dir for
    # panel-verdict.json / review-verdict.json; this is that missing writer, and
    # run_dir is the one surface that actually crosses the process split.
    try:
        with open(os.path.join(ctx.run_dir, "review-verdict.json"), "w",
                  encoding="utf-8") as handle:
            json.dump({"verdict": verdict}, handle)
    except OSError:
        pass
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
    if vn == "unknown":
        # No pass|fail|needs_revision verdict could be parsed. Fail the node
        # explicitly and say why — it used to fall through to an ordinary
        # verdict_fail with no reason. run_events.finish_reason is a CHECK enum,
        # so the trace keeps `verdict_fail` and carries the reason as its
        # verdict; the returned reason tells the revise loop to skip the round.
        print(f"  [fail] reviewer {ctx.node_id}: no parseable verdict in {review_file} "
              f"({REVIEWER_VERDICT_UNPARSEABLE})", file=sys.stderr)
        print(f"  [fail] reviewer {ctx.node_id}: {REVIEWER_VERDICT_UNPARSEABLE}")
        ctx.trace(ctx.node_id, "failure", "reviewer", review_file, REVIEWER_VERDICT_UNPARSEABLE, "verdict_fail")
        _append_run_note(ctx.db, ctx.run_id, f"{REVIEWER_VERDICT_UNPARSEABLE}: node {ctx.node_id}")
        ctx.charge()
        return 1, REVIEWER_VERDICT_UNPARSEABLE
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


VERIFIER_NOT_EXECUTED = "verifier_not_executed"


def _verifier_not_executed(ctx: NodeDispatch, why: str):
    """Fail a verifier node that never ran its script — loudly, with a reason
    (K0.5c AC2). finish_reason stays the CHECK-enum `error`; the reason rides
    in the node_end payload verdict, a [fail] line and task_runs notes. No
    verifier_* evidence is written, so I1 counts it as not proven."""
    line = f"  [fail] verifier node {ctx.node_id}: {VERIFIER_NOT_EXECUTED} ({why})"
    print(line, file=sys.stderr)
    print(line)
    ctx.node_verdict = VERIFIER_NOT_EXECUTED
    _append_run_note(ctx.db, ctx.run_id, f"{VERIFIER_NOT_EXECUTED}: node {ctx.node_id} ({why})")
    return 1, "error"


def _write_evidence_ledger_row(ctx: NodeDispatch, script: str, ev: str, rc: int, vstem: str) -> None:
    """Writer (a): one evidence-ledger row per verifier run (enrichment only).

    Only when ``MO_EVIDENCE_LEDGER`` is not ``0``. ``ac_id`` is the verifier
    JSON's ``ac_id`` when present, else ``verifier:<stem>``; ``verdict`` is
    ``pass`` iff the verifier exited 0. A write failure warns once and never
    fails the node — the ledger is a record, not a gate at write time.
    """
    from mini_ork.verify import evidence_ledger as _el  # noqa: PLC0415

    if _el.mode() == "off":
        return
    try:
        roots = load_run_roots(ctx.run_dir_eff or ctx.run_dir)
        target = (roots.target if roots else "") or context_env("MO_TARGET_CWD", "")
        tree = _el.tree_hash(target) if target else None
        ac_id = _el.verifier_ac_id(ev) or f"verifier:{vstem}"
        _el.append_row(ctx.run_dir_eff or ctx.run_dir, ac_id=ac_id, probe=script,
                       verdict="pass" if rc == 0 else "fail", log=ev, tree=tree)
    except Exception as exc:  # noqa: BLE001 — enrichment must never fail the node
        print(f"  [warn] evidence-ledger write skipped: {exc}", file=sys.stderr)


def _verifier_result_name(ctx: NodeDispatch) -> str:
    """The ``verifier_name`` key the abstain gate groups calibration slices by.

    A declared ``verifier_ref`` (e.g. ``verifiers/typecheck.py``) yields its
    stem (``typecheck``); the canonical verifier yields ``verifier@v1`` — the
    same identity the trace layer stamps as ``reward_source``, so a
    ``verifier_results`` slice lines up with the traces it explains.
    """
    ref = (ctx.verifier_ref or "").strip()
    if ref:
        stem = os.path.basename(ref)
        for suf in (".sh", ".py"):
            if stem.endswith(suf):
                stem = stem[:-3]
        if stem:
            return stem
    return "verifier@v1"


def _rubric_axes_json(run_dir: str) -> str | None:
    """The scored rubric axes for this run (``rubric.json`` items), else None.

    The items carry the per-criterion label/verdict/note the verifier actually
    reasoned over; persisting them is what turns a bare pass/fail into an
    auditable verdict. ``None`` (no rubric, or no items) stores NULL."""
    path = os.path.join(run_dir, "rubric.json") if run_dir else ""
    if not path or not os.path.isfile(path):
        return None
    try:
        doc = json.load(open(path, encoding="utf-8"))
    except (OSError, ValueError):
        return None
    items = doc.get("items") if isinstance(doc, dict) else None
    if not items:
        return None
    try:
        return json.dumps(items)
    except (TypeError, ValueError):
        return None


def _record_verifier_result(ctx: NodeDispatch, verdict: str) -> None:
    """Persist one ``verifier_results`` row for this verifier node (enrichment only).

    ``verifier_results`` (migration 0025) is the labelled history
    ``gates.abstain_gate`` reads to calibrate its LTT confidence threshold and
    the table an operator annotates with ground-truth FP/FN flags — yet before
    this wire it stayed empty while 8k+ traces claimed
    ``reward_source='verifier@v1'``. ``verdict`` is one of the DDL enum
    ``pass|fail|indeterminate|vacuous``; anything else would trip the CHECK and
    be swallowed below.

    ``confidence`` is left NULL by design: the verifier emits a pass/fail
    rubric, not a calibrated posterior, and inventing one would poison the
    conformal calibration the gate computes over this column. The row still
    carries the verdict and the scored axes for audit.

    Best-effort: a recording failure must never fail a verifier node, so every
    exception is swallowed (mirrors ``_write_evidence_ledger_row``).
    """
    if not ctx.db or not ctx.run_id:
        return
    try:
        from mini_ork.gates.verifier_rubric import verifier_result_record  # noqa: PLC0415

        verifier_result_record(
            ctx.db, ctx.run_id, _verifier_result_name(ctx), verdict,
            scored_axes_json=_rubric_axes_json(ctx.run_dir_eff or ctx.run_dir),
        )
    except Exception as exc:  # noqa: BLE001 — enrichment must never fail the node
        print(f"  [warn] verifier_results write skipped: {exc}", file=sys.stderr)


def _handle_verifier(ctx: NodeDispatch):
    post_impl = not _verifier_runs_before_implementer(ctx.workflow, ctx.node_id)

    def _publish_success():
        # Verifier-produced artifacts (verdict.json) were exempt from the
        # pre-run guard; once the scripts have run they must exist.
        if post_impl and not _required_artifacts_ok(ctx.plan_path):
            print("  [fail] verifier node: verifier did not produce its required artifact(s)",
                  file=sys.stderr)
            _record_verifier_result(ctx, "fail")
            return 1, "error"
        if ctx.publish_declared_outputs():
            _record_verifier_result(ctx, "pass")
            return 0, "done"
        _record_verifier_result(ctx, "fail")
        return 1, "artifact_contract"

    def _finish(rc: int):
        # The rc-0 exit is the verifier's own pass; a non-zero rc is a fail.
        # Recording here (not in _publish_success) is what captures the FAIL
        # rows the abstain gate's calibration window also reads.
        if rc == 0:
            return _publish_success()
        _record_verifier_result(ctx, "fail")
        return 1, "error"

    # Hollow-run guard: fail before any verifier runs if the recipe declares a
    # concrete run-local artifact (absolute contract path) that is missing or
    # zero-byte. Covers the verifier_ref branch (which bypasses the canonical
    # verifier). A verifier ordered before the first implementer is a baseline
    # oracle and cannot require artifacts that do not exist until implementation.
    # Artifacts the verifiers themselves write are checked after they run.
    if post_impl and not _required_artifacts_ok(ctx.plan_path, skip_verifier_outputs=True):
        print("  [fail] verifier node: required artifact(s) missing or empty", file=sys.stderr)
        # The verifier never ran — 'vacuous' is the DDL's "produced nothing
        # meaningful" verdict; recording it keeps the audit trail honest.
        _record_verifier_result(ctx, "vacuous")
        return _verifier_not_executed(ctx, "required artifact(s) missing or empty before the verifier ran")
    artifact = ""
    try:
        ac = (json.load(open(ctx.plan_path)).get("artifact_contract") or {}) if ctx.plan_path else {}
        outs = ac.get("outputs") or [] if isinstance(ac, dict) else []
        artifact = outs[0] if outs else ""
    except Exception:
        artifact = ""
    if not artifact and not (ctx.verifier_ref and ctx.recipe_dir):
        # A declared verifier_ref (e.g. verifiers/schema.sh) is a deterministic
        # gate that resolves its own paths from MINI_ORK_RUN_DIR and produces the
        # run-local artifact itself; it MUST run even when artifact_contract
        # .outputs is [] (recipes like verified-artifact leave outputs empty and
        # let the script emit verified-artifact.json). Only short-circuit to
        # vacuous success when there is ALSO no verifier script to run.
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
            return _verifier_not_executed(ctx, f"verifier_ref not found: {ctx.verifier_ref}")
        ev_dir = os.path.join(context_env("MINI_ORK_RUN_DIR", ctx.run_dir), "evidence")
        os.makedirs(ev_dir, exist_ok=True)
        ev = os.path.join(ev_dir, os.path.basename(ctx.verifier_ref).replace(".sh", "").replace(".py", "") + ".log")
        rc = _run_verifier_ref(
            script, ev, plan_path=ctx.plan_path, artifact_path=artifact,
            run_dir=ctx.run_dir_eff or ctx.run_dir,
        )
        vstem = ctx.verifier_ref[len("verifiers/"):] if ctx.verifier_ref.startswith("verifiers/") else ctx.verifier_ref
        vstem = vstem[:-3] if vstem.endswith((".sh", ".py")) else vstem
        _write_evidence_ledger_row(ctx, script, ev, rc, vstem)
        # F2-B: persist evidence to verifier_<stem>.json (bash :2886-2888) so the
        # reviewer input assembly can read the typecheck/test verdicts. Before the
        # rc return so failures are visible too (a missing verifier is real signal).
        persist_dir = context_env("MINI_ORK_RUN_DIR", ctx.run_dir)
        if persist_dir and os.path.isfile(ev) and os.path.getsize(ev) > 0:
            try:
                shutil.copy(ev, os.path.join(persist_dir, f"verifier_{vstem}.json"))
            except OSError:
                pass
        return _finish(rc)
    module_env = _module_env(ctx.root)
    rc = subprocess.run([
        sys.executable, "-m", "mini_ork.cli.verify", "--plan", ctx.plan_path,
        "--task-class", ctx.task_class, artifact,
    ], env=module_env).returncode
    return _finish(rc)


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


def _run_changed_files(run_dir: str) -> list[str]:
    """The implementer's recorded ``files_changed`` for this run ([] if none).

    Shared by the two rollback paths: working-tree compensation reverts these
    paths in git, and the version-registry rollback uses them to find which
    promoted rows this run is responsible for.
    """
    summary_path = os.path.join(run_dir, "implementer-summary.json") if run_dir else ""
    if summary_path and os.path.isfile(summary_path):
        try:
            data = json.load(open(summary_path, encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("files_changed"), list):
                return [e for e in data["files_changed"] if isinstance(e, str) and e]
        except Exception:
            return []
    return []


ROLLED_BACK_FILE = "rolled-back.json"
# Run-created files land here before rollback unlinks them. A harvested diff
# can omit untracked files (code-fix's review diff covers tracked paths only),
# so without this copy a rolled-back run destroys the only record of its new
# work — observed on a ContextNest run whose three new modules had to be
# rebuilt by replaying the implementer transcript.
ROLLED_BACK_CREATED_DIR = "rolled-back-created"


def _preserve_created(run_dir: str, rel: str, real: str, log) -> None:
    """Copy a run-created file under ``<run_dir>/rolled-back-created/<rel>``.

    Non-blocking: a failed copy is logged and rollback proceeds. ``rel`` has
    already been validated as a strict child of the target repo.
    """
    if not run_dir or not os.path.isfile(real):
        return
    base = os.path.realpath(os.path.join(run_dir, ROLLED_BACK_CREATED_DIR))
    dst = os.path.realpath(os.path.join(base, rel))
    if not dst.startswith(base + os.sep):
        log(f"  [rollback] not preserving {rel}: path escapes the preserve dir")
        return
    try:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(real, dst)
    except OSError as exc:
        log(f"  [warn] rollback: could not preserve created file {rel}: {exc}")


def _record_rolled_back(run_dir: str, real_root: str, rels: list[str]) -> None:
    """Persist which paths rollback reverted, as absolute realpaths.

    The post-run ``verify`` runs AFTER rollback and re-checks the plan's
    required artifacts; without this record it reports a file rollback itself
    deleted as "missing or empty" — blaming the implementer for the rollback.
    Non-blocking: a write failure only loses that attribution.
    """
    if not run_dir or not rels:
        return
    paths = sorted(os.path.realpath(os.path.join(real_root, r)) for r in rels)
    try:
        with open(os.path.join(run_dir, ROLLED_BACK_FILE), "w", encoding="utf-8") as fh:
            json.dump({"paths": paths}, fh, indent=2)
    except OSError as exc:
        print(f"  [warn] rollback: could not record reverted paths: {exc}",
              file=sys.stderr, flush=True)


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

    files: list[str] = _run_changed_files(run_dir)
    if not files:
        log("  [rollback] revert_branch: no files_changed recorded — working tree untouched")
        return True
    target_repo = context_env("MO_TARGET_CWD", "")
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
            # Implementer-created file (absent from HEAD): keep a copy in the
            # run dir (the only record of new work a harvested diff may omit),
            # then delete it.
            _preserve_created(run_dir, rel, real, log)
            try:
                if os.path.isfile(real):
                    os.remove(real)
                    removed.append(rel)
            except OSError:
                rejected.append(raw)
    log(f"  [rollback] revert_branch: restored {len(restored)} tracked file(s), "
        f"removed {len(removed)} created file(s), rejected {len(rejected)}")
    _record_rolled_back(run_dir, real_root, restored + removed)
    # Leftover report: any of the recorded paths still dirty?
    leftover = subprocess.run(
        ["git", "-C", real_root, "status", "--porcelain", "--", *files],
        capture_output=True, text=True).stdout.strip()
    if leftover:
        log(f"  [warn] rollback: leftover changes after revert_branch:\n{leftover}")
        return False
    return True


def _revert_inplace_diff(run_dir: str, root: str) -> bool:
    """``keep_run_artifacts_discard_worktree`` compensation for the in-place
    implementer: the agent edits MO_TARGET_CWD directly (and often stages), so a
    failed run leaves the harvested diff sitting in the target tree — the next
    serial epic would start from a dirty base (run-1788363267-21773-se1 left 4
    staged paths behind). Restore each path the run's framework-edit.diff names
    to its PRE-IMPLEMENTER state. Run artifacts (diff, verdicts, logs) are kept
    per the strategy name. Never touches paths outside the diff.

    "Pre-implementer" is the ``pre-implementer-ref`` snapshot, not HEAD: a path
    a concurrent session had already dirtied is restored to that dirty content,
    so the rollback removes only this run's edit. Known limit: an edit another
    session makes to the SAME path WHILE the implementer runs is
    indistinguishable from the implementer's and is discarded with it.
    """
    def log(msg):
        print(msg, file=sys.stderr, flush=True)

    def git_ok(*args):
        return subprocess.run(["git", "-C", real_root, *args],
                              stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL).returncode == 0

    diff = os.path.join(run_dir, "framework-edit.diff") if run_dir else ""
    if not (diff and os.path.isfile(diff) and os.path.getsize(diff) > 0):
        log("  [rollback] discard_worktree: no framework-edit.diff — nothing to revert")
        return True
    target_repo = context_env("MO_TARGET_CWD", "")
    if not target_repo:
        try:
            target_repo = subprocess.check_output(
                ["git", "-C", root or ".", "rev-parse", "--show-toplevel"],
                stderr=subprocess.DEVNULL).decode().strip()
        except Exception:
            target_repo = root or "."
    real_root = os.path.realpath(target_repo)

    baseline = "HEAD"
    ref_path = os.path.join(run_dir, "pre-implementer-ref")
    if os.path.isfile(ref_path):
        with open(ref_path) as fh:
            ref = fh.read().strip()
        if ref and git_ok("cat-file", "-e", f"{ref}^{{commit}}"):
            baseline = ref
        elif ref:
            log(f"  [warn] rollback: pre-implementer-ref {ref[:12]} not found in "
                f"{real_root}; restoring to HEAD")

    numstat = subprocess.run(
        ["git", "-C", real_root, "apply", "--numstat", diff],
        capture_output=True, text=True)
    paths = []
    for line in (numstat.stdout or "").splitlines():
        parts = line.split("\t") if "\t" in line else line.split()
        if len(parts) >= 3 and parts[2]:
            paths.append(parts[2])
    # --numstat lists only the NEW side of a rename; the old side must be
    # restored too, or a reverted move leaves the source file deleted. (The
    # ground-truth harvest diffs with --no-renames; an agent-written diff may not.)
    with open(diff, errors="surrogateescape") as fh:
        for line in fh:
            if line.startswith("rename from "):
                src = line[len("rename from "):].rstrip("\n")
                if src and src not in paths:
                    paths.append(src)
    if not paths:
        log("  [rollback] discard_worktree: diff names no paths — nothing to revert")
        return True
    # Per-path restore is state-agnostic: the agent may have left any mix of
    # staged/unstaged/partial states (git apply -R alone reverts worktree
    # CONTENT but leaves the agent's staged index entries behind — a preflight
    # against run-1788363267-21773-se1 left 3 index corpses).
    pre_dirty = []
    reverted = []
    for rel in paths:
        real = os.path.realpath(os.path.join(real_root, rel))
        if real != real_root and not real.startswith(real_root + os.sep):
            log(f"  [rollback] reject-revert: path escapes target repo: {rel}")
            continue
        reverted.append(rel)
        if baseline != "HEAD" and not git_ok("diff", "--quiet", "HEAD", baseline,
                                             "--", rel):
            # Dirty before the run (another session's work): restore the
            # snapshot content, then drop the agent's staging back to HEAD.
            pre_dirty.append(rel)
            if git_ok("cat-file", "-e", f"{baseline}:{rel}"):
                git_ok("checkout", baseline, "--", rel)
            elif os.path.isfile(real):
                os.remove(real)
            git_ok("reset", "-q", "HEAD", "--", rel)
        elif git_ok("cat-file", "-e", f"HEAD:{rel}"):
            # checkout HEAD resets index AND worktree.
            git_ok("checkout", "HEAD", "--", rel)
        else:
            # Created by the run: keep a copy in the run dir, then unstage
            # and unlink.
            _preserve_created(run_dir, rel, real, log)
            git_ok("rm", "-f", "-q", "--cached", "--", rel)
            try:
                if os.path.isfile(real):
                    os.remove(real)
            except OSError:
                log(f"  [rollback] could not remove created file: {rel}")
    log(f"  [rollback] discard_worktree: per-path restore over {len(paths)} path(s)"
        + (f", {len(pre_dirty)} restored to pre-run dirty state" if pre_dirty else ""))
    # Same attribution record as revert_branch: the post-run verify must report
    # an artifact this rollback removed as rolled back, not as never produced.
    _record_rolled_back(run_dir, real_root, reverted)
    clean = [p for p in paths if p not in pre_dirty]
    leftover = subprocess.run(
        ["git", "-C", real_root, "status", "--porcelain", "--", *clean],
        capture_output=True, text=True).stdout.strip() if clean else ""
    drifted = [p for p in pre_dirty if not git_ok("diff", "--quiet", baseline, "--", p)]
    if leftover or drifted:
        log("  [warn] rollback: leftover changes after discard_worktree revert:\n"
            + "\n".join(filter(None, [leftover, *drifted])))
        return False
    log(f"  [rollback] discard_worktree: target tree back to its pre-run state "
        f"for the run's {len(paths)} path(s)")
    return True


def _run_git(args: list[str], cwd: str, *, env: dict | None = None) -> subprocess.CompletedProcess:
    """Non-raising git runner for the salvage path.

    ``_salvage_before_revert``'s failure policy must catch EVERY git failure and
    log it — nothing there may raise out of ``_handle_rollback``. This seam also
    gives the failure-policy test a single injection point (monkeypatch it to
    make ``commit-tree``/``diff`` fail while the rest of the capture succeeds).
    """
    return subprocess.run(["git", "-C", cwd, *args], capture_output=True,
                          env={**os.environ, **(env or {})})


def _review_diff_paths(diff_path: str) -> list[str]:
    """The paths named by a ``git diff`` patch's ``diff --git a/X b/Y`` headers.

    Best-effort, mirroring ``_revert_inplace_diff``'s numstat parsing: the
    destination always begins with `` b/`` and the source with ``a/``, so the
    split is stable even when a path contains spaces.
    """
    paths: list[str] = []
    try:
        with open(diff_path, errors="surrogateescape") as fh:
            for line in fh:
                if not line.startswith("diff --git "):
                    continue
                rest = line[len("diff --git "):].rstrip("\n")
                if " b/" in rest:
                    lhs, rhs = rest.split(" b/", 1)
                    if lhs.startswith("a/"):
                        paths.append(lhs[2:])
                    if rhs:
                        paths.append(rhs)
                elif rest.startswith("a/"):
                    paths.append(rest[2:])
    except OSError:
        pass
    return paths


def _pre_untracked_set(untracked_path: str) -> set[str]:
    """The run-start untracked inventory (one path per line, ``surrogateescape``)."""
    try:
        with open(untracked_path, errors="surrogateescape") as fh:
            return {ln.rstrip("\n") for ln in fh if ln.strip()}
    except OSError:
        return set()


def _untracked_now(repo: str) -> set[str]:
    """Untracked paths in the target repo right now, ``\\0``-split like the
    preflight inventory (``git ls-files -z --others --exclude-standard``)."""
    r = _run_git(["ls-files", "-z", "--others", "--exclude-standard"], repo)
    if r.returncode != 0:
        return set()
    return {p for p in r.stdout.decode("utf-8", "surrogateescape").split("\0") if p}


def _salvage_before_revert(root: str, run_dir: str, run_id: str) -> dict | None:
    """Snapshot the run's change set into a restorable git object, BEFORE a
    revert destroys it. Operator rule: a failed run must never throw the
    agent's work away.

    Captures exactly the run's paths — ``files_changed``, the ``review-diff.patch``
    headers, and files untracked now that were not untracked at run start —
    into ``refs/mini-ork/salvage/<run_id>`` (a commit parented on HEAD) via a
    TEMP index so the real index, working tree, HEAD and every branch stay
    untouched. Writes ``salvage.patch`` and ``salvage.json`` to the run dir.

    Returns:
        ``None`` when there is nothing to capture (normal revert proceeds).
        A dict when capture was attempted, with ``saved`` True when either the
        ref or a non-empty patch exists — False means the caller must skip the
        revert rather than destroy unsaved work.
    """
    def log(msg):
        print(msg, file=sys.stderr, flush=True)

    target_repo = context_env("MO_TARGET_CWD", "")
    if not target_repo:
        try:
            target_repo = subprocess.check_output(
                ["git", "-C", root or ".", "rev-parse", "--show-toplevel"],
                stderr=subprocess.DEVNULL).decode().strip()
        except Exception:
            target_repo = root or "."
    real_root = os.path.realpath(target_repo)

    # 1. Gather candidate paths from all three sources.
    paths: list[str] = list(_run_changed_files(run_dir))
    review = os.path.join(run_dir, "review-diff.patch") if run_dir else ""
    if review and os.path.isfile(review):
        paths.extend(_review_diff_paths(review))
    pre_unt = os.path.join(run_dir, "pre-implementer-untracked") if run_dir else ""
    if pre_unt and os.path.isfile(pre_unt):
        pre = _pre_untracked_set(pre_unt)
        paths.extend(sorted(_untracked_now(real_root) - pre))

    # 2. Keep only paths strictly inside the target repo; drop run-dir and git
    #    internals so unrelated dirt is never captured. De-dupe on the
    #    normalized rel path (not the raw string) so the same file recorded
    #    once as an absolute path and once repo-relative collapses to one entry.
    rels: list[str] = []
    seen: set[str] = set()
    for raw in paths:
        if not raw:
            continue
        ap = raw if os.path.isabs(raw) else os.path.join(real_root, raw)
        real = os.path.realpath(ap)
        if real == real_root or not real.startswith(real_root + os.sep):
            log(f"  [rollback] salvage: skipping path outside target repo: {raw}")
            continue
        rel = os.path.relpath(real, real_root)
        if rel.startswith(".mini-ork/runs" + os.sep) or rel == ".mini-ork/runs":
            continue
        if rel.startswith(".git" + os.sep) or rel == ".git":
            continue
        if rel in seen:
            continue
        seen.add(rel)
        rels.append(rel)
    rels.sort()
    if not rels:
        return None

    # 3. Base = HEAD (deliberate: the temp index is seeded from HEAD, so a
    #    concurrent session's pre-existing dirt is naturally absent).
    base = _run_git(["rev-parse", "HEAD"], real_root).stdout.decode(errors="replace").strip()
    if not base:
        log("  [warn] rollback: salvage could not resolve HEAD — nothing saved")
        return {"saved": False, "ref": "", "sha": "", "base": "", "files": rels, "restore": []}

    # 4. Temp index → read-tree HEAD → add -A -- <paths> → write-tree →
    #    commit-tree (explicit identity: CI runners have none) → update-ref.
    sha, ref = "", ""
    try:
        with tempfile.TemporaryDirectory(prefix="mo-salvage-idx-") as td:
            env = {"GIT_INDEX_FILE": os.path.join(td, "index")}
            if _run_git(["read-tree", "HEAD"], real_root, env=env).returncode != 0:
                raise RuntimeError("read-tree failed")
            if _run_git(["add", "-A", "--", *rels], real_root, env=env).returncode != 0:
                raise RuntimeError("git add failed")
            tree = _run_git(["write-tree"], real_root, env=env).stdout.decode(errors="replace").strip()
            if not tree:
                raise RuntimeError("write-tree produced no tree")
            msg = f"mini-ork salvage: run {run_id} ({len(rels)} files)"
            commit = _run_git(
                ["-c", "user.name=mini-ork", "-c", "user.email=mini-ork@localhost",
                 "commit-tree", tree, "-p", "HEAD", "-m", msg],
                real_root).stdout.decode(errors="replace").strip()
            if not commit:
                raise RuntimeError("commit-tree produced no commit")
            sha = commit
            ref = f"refs/mini-ork/salvage/{run_id}"
            up = _run_git(["update-ref", ref, sha], real_root)
            if up.returncode != 0:
                ref = ""
                log(f"  [warn] rollback: salvage ref update refused: "
                    f"{up.stderr.decode(errors='replace').strip()}")
    except Exception as exc:
        log(f"  [warn] rollback: salvage ref not created: {exc}")
        sha = ""

    # 5. salvage.patch — from the commit when one exists, else a scoped
    #    ``git diff --binary HEAD -- <paths>`` (created files are already copied
    #    under rolled-back-created/ by _preserve_created during the revert).
    patch_path = os.path.join(run_dir, "salvage.patch") if run_dir else ""
    patch_saved = False
    if patch_path:
        try:
            diff_args = ["diff", "--binary", "HEAD", sha] if sha else \
                ["diff", "--binary", "HEAD", "--", *rels]
            d = _run_git(diff_args, real_root)
            if d.returncode == 0 and d.stdout.strip():
                with open(patch_path, "wb") as fh:
                    fh.write(d.stdout)
                patch_saved = os.path.getsize(patch_path) > 0
            elif d.returncode != 0:
                log(f"  [warn] rollback: salvage.patch diff failed: "
                    f"{d.stderr.decode(errors='replace').strip()}")
        except Exception as exc:
            log(f"  [warn] rollback: salvage.patch not written: {exc}")

    saved = bool(ref) or patch_saved
    restore: list[str] = []
    if sha:
        restore.append(f"git checkout {sha} -- {' '.join(rels)}")
    if patch_saved:
        restore.append(f"git apply {patch_path}")

    # 6. salvage.json — the operator-facing restore instructions.
    if saved:
        doc = {"ref": ref, "sha": sha, "base": base, "files": rels, "restore": restore}
        try:
            with open(os.path.join(run_dir, "salvage.json"), "w", encoding="utf-8") as fh:
                json.dump(doc, fh, indent=2)
        except OSError as exc:
            log(f"  [warn] rollback: salvage.json not written: {exc}")

    if ref:
        short = sha[:12]
        print(f"[salvage] {len(rels)} file(s) from run {run_id} saved before rollback "
              f"→ {ref} ({short}). Restore: git checkout {short} -- <files>  |  "
              f"git apply {patch_path}", file=sys.stderr, flush=True)
    elif saved:
        print(f"[salvage] {len(rels)} file(s) from run {run_id} saved as a patch only "
              f"(no ref) → {patch_path}", file=sys.stderr, flush=True)
    return {"saved": saved, "ref": ref, "sha": sha, "base": base, "files": rels, "restore": restore}


def _handle_rollback(ctx: NodeDispatch):
    # F4: bash (:3205-3223) does a best-effort version_rollback (workflow then
    # agent), succeeds regardless of whether a prior version exists, sets
    # finish_reason=done and returns 0 — it does NOT set task_runs.status. Was:
    # set_status('rolled_back') + return 1 (a no-op that also mis-set status and
    # double-counted the failure). The upstream failure already failed the run.
    from mini_ork.registries import version_registry as _vr
    # Resolve the rows this run actually promoted instead of rolling back a
    # hardcoded name. The old pair here was ("workflow", ctx.recipe) and
    # ("agent", "default"); no live row is named "default" — applied prompt
    # mutations are named by their absolute target path — so the agent rollback
    # was a guaranteed no-op that still set reverted=True and printed success.
    reverted = False
    run_dir = ctx.run_dir_eff or ctx.run_dir
    run_id = ctx.run_id or (os.path.basename(run_dir.rstrip(os.sep)) if run_dir else "")
    changed = _run_changed_files(run_dir)
    try:
        rows = _vr.targets_for_paths(changed, db=ctx.db)
    except Exception as e:
        rows = []
        print(f"  [warn] rollback: registry lookup failed: {e}", file=sys.stderr)
    for row in rows:
        try:
            _vr.rollback("agent", row["name"], db=ctx.db)
            reverted = True
        except Exception as e:
            print(f"  [warn] rollback: {row.get('name')}: {e}", file=sys.stderr)
    if not reverted:
        # Workflow-side fallback, unchanged: an applied workflow version is
        # named by its recipe, and nothing in the run record maps a changed
        # file back to one, so this stays a best-effort by-name attempt.
        try:
            _vr.rollback("workflow", ctx.recipe or "default", db=ctx.db)
            reverted = True
        except Exception:
            pass
    if not reverted:
        print("  [ok] rollback: nothing to revert (no prior promoted version)", file=sys.stderr)
    # Working-tree compensation: honor the workflow's declared strategy
    # (declared in recipes/code-fix/workflow.yaml but previously implemented
    # nowhere — fix-tracker M3). Version-registry rollback handles DB state;
    # revert_branch handles FILE state. rc contract unchanged: the rollback
    # node always succeeds and reports, it never re-fails the run.
    # Outer-loop verification override: when an OUTER driver owns the
    # authoritative gate (goal-loop: deploy -> regen -> DB flip), the
    # in-sandbox reviewer is both redundant and evidence-starved, so a worktree
    # revert here would DESTROY the implementer's verified edit before the real
    # gate ever tests it. The driver sets this flag to keep FILE state (the
    # version-registry/DB rollback above still runs).
    strategy = _rollback_strategy(ctx.workflow)
    keep_worktree = context_env(
        "MINI_ORK_ROLLBACK_KEEP_WORKTREE", "").strip().lower() in ("1", "true", "yes")
    if strategy == "revert_branch":
        if keep_worktree:
            print("  [ok] rollback: MINI_ORK_ROLLBACK_KEEP_WORKTREE set — preserving working-tree "
                  "edit (an outer loop owns verification; an in-sandbox revert would destroy the fix)",
                  file=sys.stderr, flush=True)
        else:
            salvage = _salvage_before_revert(ctx.root, run_dir, run_id)
            if salvage is not None and not salvage.get("saved"):
                print("  [warn] rollback: work could not be saved — leaving the working tree as-is",
                      file=sys.stderr, flush=True)
            else:
                _revert_working_tree(ctx.root, run_dir)
    elif strategy == "keep_run_artifacts_discard_worktree":
        # Declared by recipes/framework-edit/workflow.yaml and
        # recipes/self-migrate/workflow.yaml, implemented nowhere before this:
        # the in-place implementer's diff was left in the target tree on a
        # failed run, so the next serial epic started from a dirty base.
        if keep_worktree:
            print("  [ok] rollback: MINI_ORK_ROLLBACK_KEEP_WORKTREE set — preserving working-tree "
                  "edit (an outer loop owns verification; an in-sandbox revert would destroy the fix)",
                  file=sys.stderr, flush=True)
        else:
            salvage = _salvage_before_revert(ctx.root, run_dir, run_id)
            if salvage is not None and not salvage.get("saved"):
                print("  [warn] rollback: work could not be saved — leaving the working tree as-is",
                      file=sys.stderr, flush=True)
            else:
                _revert_inplace_diff(run_dir, ctx.root)
    print("  [ok] rollback complete")
    # NOTE: bash traces NO rollback node (:3205-3223 has no _trace_write_node_rich).
    # Tracing it with status=success would write a spurious +1-reward execution_traces
    # row — semantically inverted (rollback fires because the run FAILED) — that
    # poisons GRPO/reflect. Deliberately no trace() here to stay faithful.
    return 0, "done"


def _parse_verifier_json(body: str):
    """The LAST top-level JSON object in a ``verifier_*.json`` evidence file, or None.

    The verifier runner merges stdout+stderr into the same file, so a real payload
    is preceded by a ``[x] running: <cmd>`` banner line (see
    ``verify.levels.read_verifier_payload``); a naive ``json.loads(body)`` chokes on
    it — every verifier then abstained and the eval reward was poisoned. Mirrors
    ``scheduler._load_verifier``: try the whole body, then the last top-level object
    via ``raw_decode`` with consumed-span tracking, so a NESTED object (e.g. the
    ``{"pass": true}`` inside a ``checks`` list) is never mistaken for the payload."""
    try:
        obj = json.loads(body)
    except (ValueError, TypeError):
        obj = None
    if isinstance(obj, dict):
        return obj
    decoder = json.JSONDecoder()
    found, pos, consumed = None, 0, 0
    for line in body.splitlines(keepends=True):
        start, pos = pos, pos + len(line)
        stripped = line.lstrip()
        if start < consumed or not stripped.startswith("{"):
            continue
        try:
            obj, end = decoder.raw_decode(body, start + len(line) - len(stripped))
        except ValueError:
            continue
        if isinstance(obj, dict):
            found, consumed = obj, end
    return found


def _banner_command(body: str) -> str:
    """The command named by the verifier's last ``[<name>] running: <cmd>`` line."""
    for line in reversed(body.splitlines()):
        marker = line.find("] running:")
        if marker != -1 and line.lstrip().startswith("["):
            return line[marker + len("] running:"):].strip()
    return ""


def _vacuous_verdict(parsed: dict) -> dict:
    """``parsed`` → the no-signal verdict for a command that ran nothing.

    A literal ``true`` / ``:`` command (``probe_validity._VACUOUS_PROBES``) proves
    nothing, so its ``pass`` must not count in the execution reward and its
    ``suite_green``/``post_rc`` must not claim a green suite. Keep the identity
    fields for the judge's trajectory view; reuse the existing no-signal
    vocabulary (``status: "vacuous"``, read by ``eval_judge._verifier_passed``)
    rather than invent a field."""
    drop = ("pass", "verdict", "status", "suite_green", "post_rc")
    kept = {k: v for k, v in parsed.items() if k not in drop}
    return {**kept, "status": "vacuous"}


def _is_vacuous_command(body: str) -> bool:
    """Whether the banner command the verifier ran is a static no-op (``true``)."""
    cmd = _banner_command(body)
    if not cmd:
        return False
    from mini_ork.verify.probe_validity import is_vacuous_probe  # noqa: PLC0415
    return is_vacuous_probe(cmd)


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
                    parsed = _parse_verifier_json(body)
                    if parsed is None:
                        verifier_verdicts[name] = {"raw": body[:200]}
                    elif _is_vacuous_command(body):
                        # A literal-`true` command is test theater: its pass/suite
                        # must not count as verification (probe_validity's rule).
                        verifier_verdicts[name] = _vacuous_verdict(parsed)
                    else:
                        verifier_verdicts[name] = parsed
        except OSError:
            pass
    return traces, verifier_verdicts


def _stage_checks(plan_present: bool, artifact_ref: str, traces: list,
                  verifier_verdicts: dict, r_exec: float | None) -> dict:
    """Derive VPRM per-stage checks (R1) from the run's structured trace. Each is a
    deterministic signal on one of the six stages, tri-valued (True/False/None so a
    stage with no signal drops out of Σ wₜrₜ rather than scoring 0):

      plan     — the plan stage produced a non-empty contract.
      execute  — the run actually did work: a success trace or a declared artifact.
      verify   — at least one verifier is non-vacuous: a real pass/fail, or a suite
                 that ran green after the patch (suite_green + post_rc==0) even if
                 the delta instrument abstained. A payload whose banner command was
                 a no-op (``true``) is marked ``status: "vacuous"`` upstream, so it
                 can never be the non-vacuous one.
      coverage — the pass FRACTION over the concrete verifiers (== Layer-0 r_exec),
                 crediting *how much* verified, not just that verification ran.

    Kept deliberately rule-based (StructReward): no learned PRM."""
    from mini_ork.learning import eval_judge as ej  # noqa: PLC0415
    checks: dict = {}
    checks["plan"] = True if plan_present else None
    any_success = any((t.get("status") or "") == "success" for t in traces)
    checks["execute"] = True if (any_success or artifact_ref) else (
        False if traces else None)
    concrete = [ej._verifier_passed(v) for v in verifier_verdicts.values()
                if isinstance(v, dict)]
    concrete = [c for c in concrete if c is not None]
    # A suite that ran GREEN after the patch is real, non-vacuous verification even
    # when the delta instrument ABSTAINED (status="unverified") — the exit code is
    # the signal the instrument could not turn into a replay delta (a Rust/jest
    # build with no adapter). Mirrors verify.levels' `preserve` level (post_rc == 0)
    # and ad507150 ("never a false PROVEN", but also never a false refutation).
    green_suite = any(
        isinstance(v, dict) and v.get("suite_green") is True
        and v.get("post_rc") == 0
        for v in verifier_verdicts.values())
    checks["verify"] = True if (concrete or green_suite) else (
        False if verifier_verdicts else None)
    checks["coverage"] = r_exec  # already the [0,1] pass fraction, or None
    return checks


def _subproblem_labels(verifier_verdicts: dict) -> list:
    """Flatten the run's verifiers into verifiable-subproblem labels (R6/SCRL). A
    verifier that declares sub-cases (``subtasks``/``subresults``/``cases`` — a
    list of items each carrying ``pass``/``verdict``) contributes one label per
    case, so a run that fails overall still earns partial progress for the cases it
    passed. A verifier with no sub-cases contributes its own single pass/fail."""
    from mini_ork.learning import eval_judge as ej  # noqa: PLC0415
    labels: list = []
    for v in verifier_verdicts.values():
        if not isinstance(v, dict):
            continue
        cases = None
        for key in ("subtasks", "subresults", "cases"):
            if isinstance(v.get(key), list):
                cases = v[key]
                break
        if cases:
            for c in cases:
                labels.append(ej._verifier_passed(c) if isinstance(c, dict) else bool(c))
        else:
            labels.append(ej._verifier_passed(v))
    return labels


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


def _stamp_run_process_reward(db, run_id, proc_score, exclude_source) -> None:
    """R7 rail (gated by MO_EVAL_STAMP_PROCESS): write the run-level VPRM process
    reward (R1) onto every non-eval delivery trace's ``process_reward`` column, so
    the reflection per-node credit and the SLM distillation learn from PROCESS, not
    just outcome — small models gain more from process than outcome rewards
    (2607.02869). Excludes the dedicated eval row. Best-effort."""
    if not (db and run_id and os.path.isfile(db)):
        return
    from mini_ork.learning import eval_judge as ej  # noqa: PLC0415
    p = ej.clamp01(proc_score)
    con = sqlite3.connect(db, timeout=5.0)
    con.execute("PRAGMA busy_timeout=5000")
    try:
        cols = {r[1] for r in con.execute("PRAGMA table_info(execution_traces)").fetchall()}
        if "process_reward" not in cols:
            return
        con.execute(
            "UPDATE execution_traces SET process_reward=? "
            "WHERE run_id=? AND (reward_source IS NULL OR reward_source != ?)",
            (p, run_id, exclude_source))
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


def _refute_artifacts(run_dir: str) -> tuple[str, str]:
    """Locate this run's refute-or-promote campaign artifacts, if it held one.

    The oracle needs BOTH sides of the experiment: the findings the validator
    produced, and the manifest of plants it was measured against. Env overrides
    win; the run-dir conventions are the fallback. Either side missing returns
    ``("", "")``, which makes the oracle report ``indeterminate`` and the reward
    is left alone — a run that never held a campaign is untouched by this.
    """
    findings = os.environ.get("MO_REFUTE_FINDINGS", "")
    fabrications = os.environ.get("MO_REFUTE_FABRICATIONS", "")
    if not findings:
        for name in ("refute-findings.json", "refute-findings.txt"):
            cand = os.path.join(run_dir, name)
            if os.path.isfile(cand):
                findings = cand
                break
    if not fabrications:
        cand = os.path.join(run_dir, "fabrications.json")
        if os.path.isfile(cand):
            fabrications = cand
    return findings, fabrications


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
        # R4 — a per-run calibrated confidence γ from the judge shrinks the static
        # FP/FN priors: a confident verdict is de-biased less. Default ON, but a
        # no-op until a judge actually emits `confidence`, so zero blast radius on
        # today's runs; set MO_EVAL_CALIBRATED_PRIORS=0 to disable.
        gamma = primary.get("confidence") if primary else None
        if gamma is not None and os.environ.get("MO_EVAL_CALIBRATED_PRIORS", "1") != "0":
            fp_rate, fn_rate = ej.calibrated_priors(gamma, fp_rate, fn_rate)
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

    # ── Layer 2 (R2) + process reward (R1) + partial progress (R6) + decomposition (R3) ──
    # Deterministic, gold-free extensions of the anti-Goodhart rule from the OUTCOME
    # to the PROCESS (2026 verifiable/process-reward cluster). Everything here is
    # RECORDED in the reward vector + eval.json; the score-CHANGING gates default OFF
    # so the execution-backbone reward is unchanged unless a run opts in.
    claimed_verdict = (primary.get("verdict") if primary else "") or verdict
    # One VPRM stage vector feeds both R1 (process_reward over ALL stages) and R2
    # (coherence over the INDEPENDENT execute+verify stages — excluding coverage so
    # it doesn't just re-derive the execution backbone).
    stage_checks = _stage_checks(bool((ctx.plan_content or "").strip()),
                                 artifact_ref, traces, verifier_verdicts, r_exec)
    coh_labels = ej.process_step_labels(stage_checks)                      # R2 basis
    coh = ej.coherence(claimed_verdict, coh_labels)                        # R2
    proc_score, proc_detail = ej.process_reward(stage_checks)             # R1
    sub_score = ej.subproblem_reward(_subproblem_labels(verifier_verdicts))  # R6
    process_meta = {"coherence": coh, "claimed_verdict": claimed_verdict,
                    "coherence_basis": dict(zip(ej.PROCESS_COHERENCE_STAGES, coh_labels)),
                    "process_reward": proc_score, "process_detail": proc_detail,
                    "subproblem_reward": sub_score}

    # R2 gate (default ON since the rework — the basis is now an INDEPENDENT process
    # signal, so it fires only on a real contradiction the backbone can't see:
    # claimed success with no work / no real verification (test theater). ONE-WAY:
    # it only downgrades an OVERCLAIMED SUCCESS (claimed pass, process says fail).
    # A claimed fail that the process would pass is the judge being conservative —
    # the normal veto path — never penalized here. Set MO_EVAL_COHERENCE_GATE=0 off.
    overclaimed_success = ej._verdict_bool(claimed_verdict) is True
    process_meta["overclaimed_success"] = overclaimed_success
    if (overclaimed_success and coh < 1.0
            and os.environ.get("MO_EVAL_COHERENCE_GATE", "1") != "0"):
        try:
            penalty = float(os.environ.get(
                "MO_EVAL_COHERENCE_PENALTY", ej.DEFAULT_COHERENCE_PENALTY))
        except ValueError:
            penalty = ej.DEFAULT_COHERENCE_PENALTY
        gated = ej.coherence_gate(score, coh, penalty)
        process_meta.update(gated_from=score, gated_to=gated)
        print(f"  [eval] INCOHERENT success (coh=0) → gate {score:.2f}→{gated:.2f} "
              f"verdict=needs_revision", file=sys.stderr)
        score, verdict = gated, "needs_revision"

    # R3 decomposition: independent components, each with its own variance, keep the
    # GRPO group's advantage spread alive when the outcome term is near-binary (SEVA
    # Prop 1/2). Recorded always; becomes the PRIMARY score only under the flag.
    components = {
        "execution": exec_meta.get("r_corrected", exec_meta.get("r_exec")),
        "coherence": coh, "process": proc_score, "subproblem": sub_score,
        "correctness": axes.get("correctness"), "groundedness": axes.get("groundedness"),
    }
    decomposed, comp_vec = ej.combine_components(components)
    process_meta["components"] = comp_vec
    process_meta["decomposed_score"] = decomposed
    if os.environ.get("MO_EVAL_DECOMPOSED_REWARD", "0") == "1":
        process_meta["decomposed_from"] = score
        score, verdict = decomposed, ej.verdict_from_score(decomposed)

    # Layer 3b — refutation survival. The oracle plants findings it fabricated and
    # counts how many the validator reported anyway; a validator surviving more of
    # them than the ceiling is not refuting them, so a success built on its
    # findings is not a success. VETO ONLY, and it runs LAST so no later stage can
    # overwrite it: the veto is the outermost layer over the execution backbone.
    # A run that never held a refute campaign has no artifacts → indeterminate →
    # score untouched, which is why this costs nothing for every other recipe.
    refute_meta: dict = {"refute": "absent"}
    try:
        from mini_ork.gates import refute_or_promote_gate as rpg  # noqa: PLC0415
        findings_path, fabrications_path = _refute_artifacts(run_dir)
        survival, _ = rpg.check_fabrication_survival(
            findings_path, fabrications_path, report_dir=run_dir)
        pre_refute = score
        score, refute_meta = ej.refute_veto(score, survival)
        if refute_meta.get("refute") == "REFUTE_FAILED":
            refute_meta.update(gated_from=pre_refute, gated_to=score)
            verdict = "needs_revision"
            try:
                pct = f"{float(refute_meta['fp_rate']):.0%}"
                ceil = f"{float(refute_meta['fp_ceiling']):.0%}"
            except (TypeError, ValueError):
                pct = ceil = "?"
            print(f"  [eval] REFUTE_FAILED — validator survived {pct} of fabricated "
                  f"plants (> {ceil} ceiling) → gate {pre_refute:.2f}→{score:.2f} "
                  f"verdict=needs_revision", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001 — advisory; never sink the run
        refute_meta = {"refute": "unavailable", "error": str(exc)}
    process_meta["refute"] = refute_meta

    # Persist the envelope for offline graders + the data flywheel.
    try:
        with open(os.path.join(run_dir, "eval.json"), "w", encoding="utf-8") as fh:
            json.dump({"score": score, "axes": axes, "verdict": verdict,
                       "rationale": rationale, "trajectory_findings": findings,
                       "reward_source": source, "execution": exec_meta,
                       "process": process_meta}, fh, indent=2)
    except OSError:
        pass

    # Reward vector: numeric axes + execution numbers + process components (R1/R2/R3/R6).
    # DB-safe (numbers only); the full detail lives in eval.json.
    reward_vector = {k: v for k, v in axes.items() if isinstance(v, (int, float))}
    if exec_meta.get("r_exec") is not None:
        reward_vector["r_exec"] = exec_meta["r_exec"]
        reward_vector["r_corrected"] = exec_meta["r_corrected"]
    reward_vector["coherence"] = coh
    if proc_score is not None:
        reward_vector["process_reward"] = proc_score
    if sub_score is not None:
        reward_vector["subproblem_reward"] = sub_score
    reward_vector["decomposed"] = decomposed
    # Measured plant-survival rate, so the GRPO group sees the oracle's signal as
    # a number rather than only as the veto it already applied to `score`.
    if isinstance(refute_meta.get("fp_rate"), (int, float)):
        reward_vector["refute_survival"] = refute_meta["fp_rate"]

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

    # R7 — carry the VPRM process reward into the distillation loop. Default ON;
    # additive (writes the run-level process_reward onto non-eval traces), so the
    # SLM loop sees process signal. Set MO_EVAL_STAMP_PROCESS=0 to disable.
    if proc_score is not None and os.environ.get("MO_EVAL_STAMP_PROCESS", "1") != "0":
        try:
            _stamp_run_process_reward(ctx.db, ctx.run_id, proc_score, source)
        except Exception:
            pass

    print(f"  [eval] {source} score={score:.2f} verdict={verdict} coh={coh} "
          f"proc={proc_score} "
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
