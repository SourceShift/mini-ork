"""Recovery planner — E2 of feat/durable-dag.

The planner turns a run_id + workflow.yaml into the **minimal set of nodes
that must rerun** so a fresh attempt re-uses every valid E1 checkpoint and
re-dispatches exactly the failed branch (and the nodes that depend on it).

Why this is the load-bearing seam for E2:

  * E1 ``is_node_reusable`` already decides per-node whether the previous
    attempt is safe to skip. The planner reads that decision and walks the
    DAG to find the **dependency closure**: every node that transitively
    depends on a non-reusable node must also rerun, because its inputs
    may now differ. A node whose outputs are reused is NOT in the set,
    even if a parallel sibling failed.
  * The planner is **read-only on disk** — it never writes a checkpoint
    row, never moves an artifact, never deletes a run-dir. The execute
    loop is the only writer; the planner just tells it where to start.
  * The planner is **pure-Python** (no LLM dispatch). ``--status`` calls
    this module end-to-end and never invokes an LLM lane — the
    ``status_no_dispatch`` verifier contract enforces that.

Public API:

    load_dag(workflow_yaml_path) -> DAG
        Parses ``workflow.yaml`` (nodes + edges) into adjacency lists.
        Returns a ``DAG`` namedtuple with ``node_ids``, ``parents``
        (id → list of upstream ids), ``children`` (id → list of
        downstream ids), and ``topo`` (a topo-sorted list).

    compute_recovery(workflow_yaml_path, run_id, db_path, run_dir,
                      *, recipe=None, task_class=None,
                      from_node=None) -> RecoveryPlan
        Computes reuse / rerun / closure sets. ``from_node`` overrides
        the auto-detected entry (operator override; e.g. force a wider
        rerun from an earlier known-good point).

    plan_recovery(...) -> RecoveryPlan
        Thin alias with explicit ``strategy`` argument; resolves the
        entry node for ``resume | retry | repair | pause``.

    format_status(plan) -> str
        Pretty-print for ``--status``: reuse / rerun / cost boundary /
        why-not-reused per node. Pure read, no dispatch.

    main(argv=None) -> int
        CLI entrypoint mirroring ``mini_ork_resume.main``.

Design invariants (E2 must not break):

  * Never writes the ``node_checkpoints`` table. Read-only on E1 state.
  * Never edits ``bin/mini-ork resume`` (cost-pause). Recovery MAY CALL
    resume (as a child process) but does not extend its surface.
  * Never introduces leases or turn-resume (E3/E4). The closure is a
    read-time computation; no scheduling primitive is added.

Topology convention:

  Edges in workflow.yaml follow the convention ``from → to`` with
  ``edge_type`` in ``{depends_on, supplies_context_to, verifies,
  escalates_to}``. ALL edges contribute to the dependency relation
  for the closure — ``escalates_to`` and ``verifies`` are still a
  "this node's output flows into the next node's input" relation at
  the level the planner needs. ``rollback`` edges are excluded
  because they are control-flow only (the operator path, not data
  flow) — including them would mark the WHOLE DAG as the closure
  whenever a verifier fires.

Module layout (SOLID SRP split — behavior byte-identical parity port):

  * ``mini_ork.recovery.dag``   — the pure DAG data structure + loader
    (no env, no subprocess, no DB).
  * ``mini_ork.recovery.plan``  — the recovery plan computation
    (``RecoveryPlan`` dataclass + closure/first-node selection +
    ``format_status``).
  * this module                 — CLI concerns: ``main()``, argv parsing,
    path resolution, E3 lease/idempotency wiring, and
    ``_emit_recovery_env``. Everything moved is re-exported here so
    existing importers (``mini_ork.cli.execute``, tests) keep working.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# E1 seam — read by is_node_reusable for the per-node reuse decision.
# Importing at module top means a runtime absence of E1 surfaces
# immediately as ImportError on first call (fail loud).
from mini_ork.context import apply_env_overrides, context_env

# DAG + plan-computation seams (SRP split; re-exported for parity).
from mini_ork.recovery.dag import DAG, load_dag
from mini_ork.recovery.plan import (
    RECOVERY_STRATEGIES,
    RecoveryPlan,
    RecoveryRefused,
    compute_recovery,
    format_status,
    plan_recovery,
)
from mini_ork.recovery.restore import plan_restore, restore_carry_patch

# E3 seam — single-writer lease + idempotent recovery request. Guarded
# (soft) so the planner still imports on a build where E3 is absent; the
# dispatch path checks ``_lease is not None`` before using it.
try:
    from mini_ork.stores import lease as _lease
except Exception:  # noqa: BLE001 — E3 optional at import time
    _lease = None  # type: ignore[assignment]

__all__ = [
    "DAG",
    "RecoveryPlan",
    "RECOVERY_STRATEGIES",
    "load_dag",
    "compute_recovery",
    "plan_recovery",
    "format_status",
    "main",
]


# ─────────────────────────────────────────────────────────────────────────────
# CLI entrypoint — parity with mini_ork_resume.main
# ─────────────────────────────────────────────────────────────────────────────

_USAGE = """\
Usage: mini-ork recover <run_id> [--from-node <id>] [--strategy NAME] [--status]
                                [--carry-patch NAME] [--ack-change] [--force]

Recover a failed run by walking the workflow DAG, marking each node
reusable via E1's `is_node_reusable`, and dispatching ONLY the
earliest non-reusable node + its transitive dependents.

Distinct from `mini-ork resume` (cost-pause): that one clears the
cost sentinel; this one re-enters the execute loop at the closure
root and reuses valid E1 checkpoints without new LLM dispatch.

Arguments:
  run_id                       Run identifier (e.g. run-1781000000-12345)

Options:
  --from-node <id>             Override entry node (operator wants a
                                 wider rerun; closure is recomputed
                                 rooted at this node).
  --strategy NAME              resume | retry | repair | pause | reattach | verify
                                 resume (default): start at closure root
                                 retry:           start at closure root
                                 repair:          + bounded cost ceiling
                                 pause:           compute, do NOT dispatch
                                 reattach:        re-attach to a still-running
                                                   remote proc (remote-nodes-10);
                                                   the planner picks this as
                                                   the default when an
                                                   unconsumed remote_procs row
                                                   exists for the first-incomplete
                                                   node.
                                 verify:          re-run only the verifier
                                                   chain; every upstream node
                                                   must be reusable. Refuses
                                                   with "<node> has no
                                                   reusable checkpoint — use
                                                   --strategy resume" when an
                                                   upstream LLM node cannot be
                                                   reused.
  --status                     Print reuse/rerun split + cost boundary
                                 without dispatching any node.
  --carry-patch NAME           Name of a run-dir-relative patch to apply
                                 to the target tree (default: ``salvage.patch``
                                 or the workflow's
                                 ``recovery.carry_patch``). Applies the
                                 patch BEFORE dispatch when the run was
                                 rolled back and the reuse set contains
                                 an implementer-typed node. ``--status``
                                 prints the resolved target/patch/would-apply
                                 state without touching the tree.
  --ack-change                 Acknowledge a ``needs_change`` retry hint
                                 and proceed past the change-needed gate.
                                 Required when ``<run_dir>/retry-hint.json``
                                 is present with ``needs_change`` set.
  --force                      Override a ``retryable: false`` retry hint
                                 and dispatch anyway.
  --workflow <path>            Override workflow.yaml location.
  --db <path>                  Override state.db location.
  --help, -h                   Show this help
"""


def _resolve_default_paths(
    run_id: str,
) -> tuple[str, str, str, str]:
    """Mirror ``mini_ork_resume._resolve_run_dir`` precedence:
    env > CWD-relative defaults. Returns (run_dir, db_path,
    workflow_yaml_path, recipe).

    Order of precedence for run_dir:
      1. ``MINI_ORK_RUN_DIR`` (authoritative — what the live execute
         loop uses, and what tests can point at an isolated tmp dir).
      2. ``$MINI_ORK_HOME/runs/<run_id>`` (the standard layout).
      3. ``$CWD/.mini-ork/runs/<run_id>`` (the install-default).
    The same precedence mirrors mini_ork_resume (see :44-49) so the
    two subcommands agree on where the artifacts live.

    Recipe resolution (kickoff §1 / lens §1.1) now consults
    ``mini_ork.recipes_catalog.find_recipe`` BEFORE building the
    hard-coded ``<engine>/recipes/<recipe>/workflow.yaml`` path. A
    project-home recipe shadows the engine's; symlinks collapse via
    ``Path.resolve()``; the catalog requires BOTH ``workflow.yaml``
    AND ``task_class.yaml`` to exist (recipes_catalog.py:94-107). When
    the catalog returns ``None`` (a recipe that lives only in the
    engine's bundled set, or a build where the catalog is not wired),
    we fall back to the engine path so today's behavior is preserved
    for legacy consumers. ``MINI_ORK_WORKFLOW`` still wins outright.
    """
    run_dir_env = context_env("MINI_ORK_RUN_DIR", "").strip()
    home_str = context_env("MINI_ORK_HOME") or os.path.join(os.getcwd(), ".mini-ork")
    home = Path(home_str)
    if run_dir_env:
        run_dir = run_dir_env
    else:
        run_dir = os.path.join(home_str, "runs", run_id)
    db = context_env("MINI_ORK_DB") or os.path.join(home_str, "state.db")
    workflow = os.environ.get("MINI_ORK_WORKFLOW") or ""
    recipe = os.environ.get("MINI_ORK_RECIPE") or ""
    if not recipe:
        # A CLI recover has no MINI_ORK_RECIPE; without the run's recipe every
        # checkpoint hashes as recipe="unknown" and reads as hash_mismatch.
        recipe = _recipe_from_run_profile(run_dir)
    if not workflow and recipe:
        # Catalog-first: a project recipe in <home>/recipes/ shadows the
        # engine's bundled recipe of the same name. The catalog walks the
        # same paths the launcher / picker / MCP tool use, so a project
        # author who added ``<home>/recipes/acq-wave5-rsi`` no longer
        # sees ``workflow.yaml not found`` for that recipe.
        from mini_ork.recipes_catalog import find_recipe

        info = find_recipe(recipe, home)
        if info is not None:
            workflow = str(info.path / "workflow.yaml")
        else:
            root = os.environ.get("MINI_ORK_ROOT") or os.path.dirname(
                os.path.dirname(os.path.realpath(__file__))
            )
            workflow = os.path.join(root, "recipes", recipe, "workflow.yaml")
    return run_dir, db, workflow, recipe


def _recipe_from_run_profile(run_dir: str) -> str:
    """The recipe the run was started with, from ``<run_dir>/run_profile.json``."""
    try:
        with open(os.path.join(run_dir, "run_profile.json"), encoding="utf-8") as fh:
            return str(json.load(fh).get("recipe") or "").strip()
    except (OSError, ValueError, AttributeError):
        return ""


def _emit_recovery_env(plan: RecoveryPlan) -> None:
    """Publish the closure set + sku into the env so a follow-up
    ``mini-ork execute`` can pick it up. Keyed by run_id
    so concurrent recoveries in different terminal sessions don't
    cross-contaminate.

    CONTRACT (in-process only): these writes die with this process — the
    hand-off works because the caller (test, API, or the in-process
    execute flow) invokes the executor in the SAME process. A bash caller
    gets the plan via stdout (format_status), not via env. Mutations go
    through the canonical mini_ork.context helper.
    """
    apply_env_overrides({
        "MINI_ORK_RECOVERY_RUN_ID": plan.run_id,
        "MINI_ORK_RECOVERY_SKU": plan.sku,
        "MINI_ORK_RECOVERY_CLOSURE": " ".join(sorted(plan.closure)),
        "MINI_ORK_RECOVERY_STRATEGY": plan.strategy,
    })
    if plan.first_node:
        apply_env_overrides({"MINI_ORK_RECOVERY_FROM": plan.first_node})


def _task_class_for_recipe(recipe: str) -> str:
    """Resolve a recipe's task_class so the recovery config_hash matches the
    one E1 wrote at run time. The original run's config_hash embeds the run's
    task_class; without MINI_ORK_TASK_CLASS set, recover must reproduce it or
    every node reads as a hash-mismatch rerun.

    Lookup order (kickoff §1 / lens §1.1):
      1. ``mini_ork.recipes_catalog.find_recipe`` — already parses the
         catalog entry's ``task_class`` from the project-overlay or
         engine-bundled ``task_class.yaml``; honors the home→engine
         shadowing rule so a project recipe's ``name:`` wins.
      2. Hand-rolled fallback: read ``<root>/recipes/<recipe>/task_class.yaml``
         (``name:`` line). Kept for a recipe that exists in the engine but
         isn't catalog-discoverable for any reason (catalog is the source
         of truth, so this branch should be unreachable on healthy
         installs).
      3. Kebab→snake convention (``framework-edit`` → ``framework_edit``).
         Last-resort fallback so a recipe with no task_class.yaml still
         hashes consistently with the original run.
    """
    if not recipe:
        return ""
    home = Path(context_env("MINI_ORK_HOME") or os.path.join(os.getcwd(), ".mini-ork"))
    from mini_ork.recipes_catalog import find_recipe

    info = find_recipe(recipe, home)
    if info is not None and info.task_class:
        return info.task_class
    root = os.environ.get("MINI_ORK_ROOT") or os.getcwd()
    tc_yaml = os.path.join(root, "recipes", recipe, "task_class.yaml")
    if os.path.isfile(tc_yaml):
        try:
            for line in open(tc_yaml):
                if line.strip().startswith("name:"):
                    return line.split(":", 1)[1].strip().strip('"\'')
        except OSError:
            pass
    return recipe.replace("-", "_")


# ─────────────────────────────────────────────────────────────────────────────
# Hint-gate + restore-plan shims (kickoff §4 / §5)
# ─────────────────────────────────────────────────────────────────────────────


def _load_retry_hint(run_dir: str) -> dict | None:
    """Read ``<run_dir>/retry-hint.json`` if present, else ``None``.

    The retry-hint module is owned by a parallel run; this shim is the
    durable seam that lets a verifying consumer refuse a structurally-
    plausible-but-actually-broken retry. Contract (per the parallel-run
    owner):
        {retryable: bool, strategy: str, from_node: str|null,
         needs_change: null|{kind, summary, detail, evidence},
         command: str}

    Malformed JSON, missing keys, or a missing file all degrade to
    ``None`` ("today's behaviour") so a partial / corrupt hint never
    blocks recovery — the parallel run's contract is verified
    independently.
    """
    if not run_dir:
        return None
    path = os.path.join(run_dir, "retry-hint.json")
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def _format_hint_block(hint: dict) -> str:
    """One-line-per-field operator preview of the retry hint. Used on
    the ``--status`` path so the operator sees the change-needed /
    refusal reason without needing to read the JSON."""
    lines = ["    retry-hint:"]
    lines.append(f"      retryable:    {hint.get('retryable', True)}")
    lines.append(f"      strategy:     {hint.get('strategy')!r}")
    lines.append(f"      from_node:    {hint.get('from_node')!r}")
    nc = hint.get("needs_change")
    if nc:
        lines.append(f"      needs_change: kind={nc.get('kind')!r} summary={nc.get('summary')!r}")
        if nc.get("detail"):
            lines.append(f"                   detail={nc.get('detail')!r}")
        if nc.get("evidence"):
            lines.append(f"                   evidence={nc.get('evidence')!r}")
    cmd = hint.get("command")
    if cmd:
        lines.append(f"      command:      {cmd}")
    return "\n".join(lines)


def _needs_restore(plan: RecoveryPlan, run_dir: str) -> bool:
    """True iff the recovery must apply a carry patch before dispatch.

    Two conditions (kickoff §4):

      1. The reuse set contains an implementer-typed node (or any other
         code-changing type — ``node_types`` carries the type map so
         ``implementer``, ``rollback``, etc. all qualify). Without an
         reused implementer there's nothing for the verifier to verify
         against — the tree is already clean.
      2. The run was rolled back (``rolled-back.json`` OR
         ``salvage.patch`` present). Otherwise nothing to restore.

    Both must hold; the function short-circuits on the cheaper
    ``plan.reuse`` check first.
    """
    has_code_reuse = any(
        plan.node_types.get(nid) in ("implementer",) for nid in plan.reuse
    )
    if not has_code_reuse:
        return False
    if not run_dir:
        return False
    if os.path.isfile(os.path.join(run_dir, "rolled-back.json")):
        return True
    if os.path.isfile(os.path.join(run_dir, "salvage.patch")):
        return True
    return False


def _format_restore_plan(
    run_dir: str,
    cli_carry_patch: str | None,
    workflow_path: str,
) -> str:
    """Pretty-print the read-only restore plan for ``--status``.

    Pulled out of ``plan_restore`` so the operator sees a one-liner
    target/patch/would-apply block at the top of the status output
    (kickoff §4 last bullet). Substring text is free; the CLI never
    shells out to apply on this path.
    """
    plan = plan_restore(
        run_dir,
        cli_carry_patch=cli_carry_patch,
        workflow_path=workflow_path,
    )
    target = plan.get("target") or "<none>"
    patch = plan.get("patch") or "<none>"
    would = plan.get("would_apply") or "<none>"
    lines = ["    restore plan:"]
    lines.append(f"      target:      {target}")
    lines.append(f"      patch:       {patch}")
    lines.append(f"      would_apply: {would}")
    if plan.get("stderr"):
        lines.append(f"      stderr:      {plan['stderr']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None, *, handoff: dict | None = None) -> int:
    """CLI entrypoint mirroring ``mini_ork_resume.main``.

    Args:
      argv — the full argv (positional run_id + flags). None → sys.argv[1:].

      handoff — optional dict; on the dispatch path it is filled with what
        an executor needs (run_id, run_dir, workflow, db_path, recipe,
        request_id, lease_token). ``cli_main`` uses it; in-process callers
        that read the env hand-off can ignore it.

    Returns:
      rc — 0 success, 1 plan/runtime error, 2 usage error, 3 paused.

    The CLI never raises. Errors are coerced to stderr + nonzero rc so
    the bash wrapper sees the same shape as ``mini-ork resume``.
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("--help", "-h"):
        sys.stdout.write(_USAGE)
        return 0 if argv else 2

    # ── minimal hand-rolled flag parse (no argparse dep here so the
    # planner imports cleanly from the bash wrapper which has no
    # third-party deps) ──
    run_id = ""
    from_node: str | None = None
    strategy = "resume"
    status_only = False
    workflow_override = ""
    db_override = ""
    cancel_request_id = ""
    carry_patch = ""
    ack_change = False
    force = False
    positional: list[str] = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--cancel":
            if i + 1 >= len(argv):
                sys.stderr.write("--cancel requires <request_id>\n")
                return 2
            cancel_request_id = argv[i + 1]
            i += 2
        elif a.startswith("--cancel="):
            cancel_request_id = a.split("=", 1)[1].strip()
            i += 1
        elif a == "--from-node":
            if i + 1 >= len(argv):
                sys.stderr.write("--from-node requires <id>\n")
                return 2
            from_node = argv[i + 1]
            i += 2
        elif a.startswith("--from-node="):
            from_node = a.split("=", 1)[1].strip()
            i += 1
        elif a == "--strategy":
            if i + 1 >= len(argv):
                sys.stderr.write("--strategy requires NAME\n")
                return 2
            strategy = argv[i + 1]
            i += 2
        elif a.startswith("--strategy="):
            strategy = a.split("=", 1)[1].strip()
            i += 1
        elif a == "--status":
            status_only = True
            i += 1
        elif a == "--carry-patch":
            if i + 1 >= len(argv):
                sys.stderr.write("--carry-patch requires <name>\n")
                return 2
            carry_patch = argv[i + 1]
            i += 2
        elif a.startswith("--carry-patch="):
            carry_patch = a.split("=", 1)[1].strip()
            i += 1
        elif a == "--ack-change":
            ack_change = True
            i += 1
        elif a == "--force":
            force = True
            i += 1
        elif a == "--workflow":
            if i + 1 >= len(argv):
                sys.stderr.write("--workflow requires <path>\n")
                return 2
            workflow_override = argv[i + 1]
            i += 2
        elif a.startswith("--workflow="):
            workflow_override = a.split("=", 1)[1].strip()
            i += 1
        elif a == "--db":
            if i + 1 >= len(argv):
                sys.stderr.write("--db requires <path>\n")
                return 2
            db_override = argv[i + 1]
            i += 2
        elif a.startswith("--db="):
            db_override = a.split("=", 1)[1].strip()
            i += 1
        elif a.startswith("-"):
            sys.stderr.write(f"recover: unknown flag: {a}\n")
            return 2
        else:
            positional.append(a)
            i += 1

    # ── E5: `recover --cancel <request_id>` — cancel a pending recovery
    # WITHOUT invalidating prior checkpoints. Targets a request_id (not a
    # run_id), so it short-circuits before the positional run_id check. Uses
    # the E5 admin module (no change to E1–E4 logic). ──
    if cancel_request_id:
        home = os.environ.get("MINI_ORK_HOME") or os.path.join(os.getcwd(), ".mini-ork")
        db_path = db_override or os.environ.get("MINI_ORK_DB") or os.path.join(home, "state.db")
        from mini_ork.recovery.admin import cancel_recovery  # noqa: PLC0415
        res = cancel_recovery(db_path, cancel_request_id)
        if not res["ok"]:
            sys.stderr.write(
                f"[mini-ork-recover] could not cancel {cancel_request_id}: "
                f"no such request or DB error\n"
            )
            return 1
        sys.stdout.write(
            f"[mini-ork-recover] cancelled recovery {cancel_request_id} "
            f"(was {res['previous_status']}); lease_released={res['lease_released']}; "
            f"prior checkpoints preserved.\n"
        )
        return 0

    if len(positional) != 1:
        sys.stderr.write(
            f"recover: expected exactly 1 positional arg (run_id), got {len(positional)}\n"
        )
        return 2
    run_id = positional[0]

    if strategy not in RECOVERY_STRATEGIES:
        sys.stderr.write(
            f"recover: --strategy must be one of {RECOVERY_STRATEGIES}, got {strategy!r}\n"
        )
        return 2

    run_dir, db_default, workflow_default, recipe = _resolve_default_paths(run_id)
    workflow = workflow_override or workflow_default
    db_path = db_override or db_default
    task_class = (os.environ.get("MINI_ORK_TASK_CLASS")
                  or _task_class_for_recipe(recipe) or "generic")

    if not os.path.isdir(run_dir):
        sys.stderr.write(
            f"[mini-ork-recover] run dir not found: {run_dir}\n"
        )
        return 1
    if not workflow or not os.path.isfile(workflow):
        sys.stderr.write(
            f"[mini-ork-recover] workflow.yaml not found: {workflow or '<unset>'}\n"
        )
        return 1

    # ── Hint gate (kickoff §5). Reads ``<run_dir>/retry-hint.json`` if
    # present. The retry_hint module is owned by a parallel run; this
    # fallback is the durable seam that lets a verifying consumer refuse
    # a structurally-plausible-but-actually-broken retry. The hint's
    # contract (per the parallel-run owner):
    #   {retryable: bool, strategy, from_node, needs_change: null|
    #    {kind, summary, detail, evidence}, command}
    # No file → today's behaviour. Malformed JSON → today's behaviour.
    hint = _load_retry_hint(run_dir)
    if status_only:
        if hint is not None:
            sys.stdout.write(_format_hint_block(hint) + "\n")
        sys.stdout.write(_format_restore_plan(
            run_dir, carry_patch or None, workflow,
        ) + "\n")
    if hint is not None and not hint.get("retryable", True) and not force:
        sys.stderr.write(
            f"[mini-ork-recover] retry-hint refuses this run "
            f"(strategy={hint.get('strategy')!r});"
            + (" " if not hint.get("needs_change") else "")
            + "\n"
        )
        nc = hint.get("needs_change") or {}
        if nc.get("summary"):
            sys.stderr.write(f"summary: {nc['summary']}\n")
        if nc.get("detail"):
            sys.stderr.write(f"detail:  {nc['detail']}\n")
        sys.stderr.write("Pass --force to override.\n")
        return 1
    if hint is not None and hint.get("needs_change") and not ack_change:
        nc = hint["needs_change"]
        sys.stderr.write(
            f"Needs a change before retrying ({nc.get('kind')!r}): "
            f"{nc.get('summary')}\n{nc.get('detail')}\n"
            f"Fix it, then rerun with --ack-change.\n"
        )
        return 1

    try:
        plan = plan_recovery(
            workflow, run_id, db_path, run_dir,
            recipe=recipe, task_class=task_class,
            from_node=from_node, strategy=strategy,
        )
    except RecoveryRefused as exc:
        # Strategy-specific refusals (verify without reusable upstream
        # nodes, no verifier entry). Operator-facing — rc=1 mirrors the
        # workflow-not-found refusal at lines 386-390.
        sys.stderr.write(f"[mini-ork-recover] {exc}\n")
        return 1

    if status_only:
        sys.stdout.write(format_status(plan))
        return 0

    # ── Carry-patch restore (kickoff §4). For verify / resume / retry,
    # when the reuse set contains an implementer-typed node AND the run
    # was rolled back, restore the salvage (or operator-supplied) patch
    # BEFORE the lease is acquired. The reuse set + node_types come
    # straight from the just-computed plan — no extra reads.
    if strategy in ("verify", "resume", "retry") and _needs_restore(plan, run_dir):
        status, message = restore_carry_patch(
            run_dir,
            cli_carry_patch=carry_patch or None,
            workflow_path=workflow,
        )
        sys.stdout.write(
            f"[mini-ork-recover] restore: {status} — {message}\n"
        )
        if status == "conflict":
            sys.stderr.write(
                "[mini-ork-recover] refusing to dispatch: carry-patch "
                "conflict. Resolve the conflict, then rerun.\n"
            )
            return 1
        if status in ("no_target", "no_patch"):
            sys.stderr.write(
                "[mini-ork-recover] refusing to dispatch: cannot resolve "
                f"restore target/patch ({status}: {message}).\n"
            )
            return 1

    if strategy == "pause":
        # Pure observation — never dispatch. Operator can read the
        # JSON-encoded plan and invoke `recover resume` after review.
        sys.stdout.write(format_status(plan))
        sys.stdout.write(
            "[mini-ork-recover] strategy=pause; not dispatching.\n"
            "  To proceed, run: "
            f"mini-ork recover {run_id} --strategy resume"
            + (f" --from-node {from_node}" if from_node else "")
            + "\n"
        )
        return 0

    if not plan.closure:
        # Nothing to rerun — clean exit so the orchestrator advances
        # to verify without re-entering the loop.
        sys.stdout.write(
            f"[mini-ork-recover] every node is reusable; nothing to recover for {run_id}\n"
        )
        return 0

    # ── E3: single-writer lease + idempotent recovery request ──
    # A recovery must OWN the run before it dispatches. Register the request
    # (idempotent on run_id+from_node+strategy) then acquire the lease. If the
    # lease is already held by another live recovery, return a safe descriptive
    # result and DO NOT dispatch — so two concurrent `recover` calls run the
    # node once (design §5/§7, scenario 6). The acquired token is exported as
    # MINI_ORK_LEASE_TOKEN so execute's checkpoint publish is fenced against a
    # stale worker. Gated on lease_tables_present so a pre-0052 (legacy) DB
    # recovers fence-free exactly as E2 did.
    _token = None
    _req = None
    if _lease is not None and _lease.lease_tables_present(db_path):
        _from = plan.first_node or (from_node or "")
        _req = _lease.request_recovery(db_path, run_id, _from, strategy)
        _token = _lease.acquire_lease(db_path, run_id)
        if _token is None:
            _rid = _req[0] if _req else "<unknown>"
            sys.stdout.write(
                f"[mini-ork-recover] run {run_id} is already being recovered "
                f"(single-writer lease held by another worker; request_id={_rid}); "
                f"not dispatching a second time.\n"
            )
            return 0
        if _req is not None:
            apply_env_overrides({"MINI_ORK_RECOVERY_REQUEST": _req[0]})
            _lease.mark_dispatched(db_path, _req[0], owner_token=_token, cost_usd=0.0)
        apply_env_overrides({"MINI_ORK_LEASE_TOKEN": _token})

    # Active strategies: emit the env, print the plan, hand off to
    # the native executor which honors MINI_ORK_RECOVERY_FROM + CLOSURE.
    _emit_recovery_env(plan)
    sys.stdout.write(format_status(plan))
    sys.stdout.write(
        f"[mini-ork-recover] dispatching closure from node={plan.first_node} "
        f"({len(plan.closure)} node{'s' if len(plan.closure) != 1 else ''} to rerun)\n"
    )
    if handoff is not None:
        handoff.update({
            "run_id": run_id, "run_dir": run_dir, "workflow": workflow,
            "db_path": db_path, "recipe": recipe,
            "request_id": _req[0] if _req else "", "lease_token": _token or "",
        })
    return 0


def cli_main(argv: list[str] | None = None, *, execute_fn=None) -> int:
    """``mini-ork recover`` entry. The subcommand spawns this module in its own
    process, so ``main``'s env hand-off has no in-process executor to read it:
    the CLI used to print "dispatching closure" and exit without running a
    node. Dispatch the closure here, then close the request and release the
    lease so a later recover is not refused for the lease TTL."""
    for key in ("MINI_ORK_RECOVERY_RUN_ID", "MINI_ORK_RECOVERY_SKU", "MINI_ORK_RECOVERY_CLOSURE",
                "MINI_ORK_RECOVERY_STRATEGY", "MINI_ORK_RECOVERY_FROM"):
        os.environ.pop(key, None)  # only this call's plan may drive the executor
    handoff: dict = {}
    rc = main(argv, handoff=handoff)
    if rc != 0 or not handoff:
        return rc
    apply_env_overrides({
        "MINI_ORK_RUN_ID": handoff["run_id"],
        "MINI_ORK_RUN_DIR": handoff["run_dir"],
        "MINI_ORK_WORKFLOW": handoff["workflow"],
        "MINI_ORK_RECIPE": handoff["recipe"] or None,
    })
    exec_argv = ["--recovery"]
    plan_json = os.path.join(handoff["run_dir"], "plan.json")
    if os.path.isfile(plan_json):
        exec_argv.insert(0, plan_json)
    if execute_fn is None:
        from mini_ork.cli import execute as _execute  # noqa: PLC0415
        root = os.environ.get("MINI_ORK_ROOT") or os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

        def _default_executor(a):
            return _execute.main(a, root=root)
        execute_fn = _default_executor
    exec_rc = 1
    try:
        exec_rc = execute_fn(exec_argv)
        return exec_rc
    finally:
        if _lease is not None:
            if handoff["request_id"]:
                _lease.close_recovery(handoff["db_path"], handoff["request_id"],
                                      status="completed" if exec_rc == 0 else "failed")
            if handoff["lease_token"]:
                _lease.release_lease(handoff["db_path"], handoff["run_id"], handoff["lease_token"])


if __name__ == "__main__":
    raise SystemExit(cli_main())
