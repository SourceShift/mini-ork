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

import os
import sys

# E1 seam — read by is_node_reusable for the per-node reuse decision.
# Importing at module top means a runtime absence of E1 surfaces
# immediately as ImportError on first call (fail loud).
from mini_ork.context import apply_env_overrides

# DAG + plan-computation seams (SRP split; re-exported for parity).
from mini_ork.recovery.dag import DAG, load_dag
from mini_ork.recovery.plan import (
    RECOVERY_STRATEGIES,
    RecoveryPlan,
    compute_recovery,
    format_status,
    plan_recovery,
)

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
  --strategy NAME              resume | retry | repair | pause
                                 resume (default): start at closure root
                                 retry:           start at closure root
                                 repair:          + bounded cost ceiling
                                 pause:           compute, do NOT dispatch
  --status                     Print reuse/rerun split + cost boundary
                                 without dispatching any node.
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
    """
    run_dir_env = os.environ.get("MINI_ORK_RUN_DIR", "").strip()
    home = os.environ.get("MINI_ORK_HOME") or os.path.join(os.getcwd(), ".mini-ork")
    if run_dir_env:
        run_dir = run_dir_env
    else:
        run_dir = os.path.join(home, "runs", run_id)
    db = os.environ.get("MINI_ORK_DB") or os.path.join(home, "state.db")
    workflow = os.environ.get("MINI_ORK_WORKFLOW") or ""
    recipe = os.environ.get("MINI_ORK_RECIPE") or ""
    if not workflow and recipe:
        root = os.environ.get("MINI_ORK_ROOT") or os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
        workflow = os.path.join(root, "recipes", recipe, "workflow.yaml")
    return run_dir, db, workflow, recipe


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
    every node reads as a hash-mismatch rerun. Source of truth is
    ``recipes/<recipe>/task_class.yaml`` (`name:`); falls back to the kebab→snake
    recipe-name convention (e.g. ``framework-edit`` → ``framework_edit``)."""
    if not recipe:
        return ""
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


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint mirroring ``mini_ork_resume.main``.

    Args:
      argv — the full argv (positional run_id + flags). None → sys.argv[1:].

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

    plan = plan_recovery(
        workflow, run_id, db_path, run_dir,
        recipe=recipe, task_class=task_class,
        from_node=from_node, strategy=strategy,
    )

    if status_only:
        sys.stdout.write(format_status(plan))
        return 0

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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
