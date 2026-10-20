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
from typing import Any
from pathlib import Path

# E1 seam — read by is_node_reusable for the per-node reuse decision.
# Importing at module top means a runtime absence of E1 surfaces
# immediately as ImportError on first call (fail loud).
from mini_ork.context import (
    apply_env_overrides,
    context_env,
    context_env_snapshot,
    scoped_environ,
)

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
from mini_ork.runtime.placement_env import (
    placement_env_map,
    read_run_placement,
    validate_placement_env,
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
                                [--carry-patch NAME] [--ack-change] [--force]
                                [--lane <alias>=<lane>]… [--allow-local]

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
  --lane <alias>=<lane>        Pin a workflow lane alias to a provider lane
                                 for the resumed run (repeatable). Writes
                                 ``runs/<id>/config/agents.recover.yaml``
                                 and appends the switch to
                                 ``runs/<id>/recover-lanes.log``; the
                                 resumed execute reads the pin via
                                 ``MINI_ORK_AGENTS``.
  --ack-change                 Acknowledge a ``needs_change`` retry hint
                                 and proceed past the change-needed gate.
                                 Required when ``<run_dir>/retry-hint.json``
                                 is present with ``needs_change`` set.
  --force                      Override a ``retryable: false`` retry hint
                                 and dispatch anyway.
  --allow-local                Revive a run recorded as ``--placement remote``
                                 on the host when that placement can no longer
                                 be restored (no node env resolvable). Without
                                 it such a revival is refused.
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
        recipe = _recipe_from_task_runs(db, run_id) or _recipe_from_run_profile(run_dir)
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


def _recipe_from_task_runs(db: str, run_id: str) -> str:
    """The recipe the run actually executed, from its ``task_runs`` row.

    ``run_profile.json``'s ``recipe`` is the profiler's suggestion and can
    differ from what ran (a framework-edit run profiled as ``docs``), so the
    ledger row wins; ``""`` when the row or table is missing.
    """
    import sqlite3

    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            row = con.execute("SELECT recipe FROM task_runs WHERE id = ?", (run_id,)).fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return ""
    return str(row[0] or "").strip() if row else ""


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


def _task_run_status(db: str, run_id: str) -> str:
    """The ``task_runs.status`` for ``run_id`` (``""`` when missing/error).

    Reads through the same module seam the recipe lookup uses (kickoff
    hr-review-resume §2): a row whose status is ``reviewing`` is parked
    waiting on a human review, NOT failed, and the planner must short-circuit
    the hint gate that normally treats such rows as failures. A missing row,
    a missing ``task_runs`` table (pre-0013 DBs), or any sqlite error all
    degrade to ``""`` — the planner's existing "no signal" behaviour. The
    function never raises; callers fall through to today's behaviour when
    the lookup yields nothing.
    """
    import sqlite3

    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    except sqlite3.Error:
        return ""
    try:
        try:
            row = con.execute(
                "SELECT status FROM task_runs WHERE id = ?", (run_id,)
            ).fetchone()
        except sqlite3.OperationalError:
            return ""
    finally:
        con.close()
    return str(row[0] or "").strip() if row else ""


# ─────────────────────────────────────────────────────────────────────────────
# Hint-gate + restore-plan shims (kickoff §4 / §5)
# ─────────────────────────────────────────────────────────────────────────────


def _load_retry_hint(
    run_dir: str,
    *,
    home: str | None = None,
    run_id: str | None = None,
) -> dict | None:
    """Read the retry hint for this run, preferring the module seam.

    Tries ``mini_ork.recovery.retry_hint.load_or_compute(home, run_id)``
    first (kickoff §3 fix 4). Falls back to ``<run_dir>/retry-hint.json``
    when the module is absent (``ImportError``) or returns ``None``. The
    module is the durable seam owned by a parallel run; the file is the
    legacy fallback for runs whose owner hasn't migrated.

    The retry-hint contract (per the parallel-run owner):
        {retryable: bool, strategy: str, from_node: str|null,
         needs_change: null|{kind, summary, detail, evidence},
         command: str}

    Malformed JSON, missing keys, or a missing file all degrade to
    ``None`` ("today's behaviour") so a partial / corrupt hint never
    blocks recovery — the parallel run's contract is verified
    independently. The import is performed lazily so a test can inject a
    stub via ``monkeypatch.setitem(sys.modules, ...)`` BEFORE the planner
    hits this code path (kickoff test 4).
    """
    hint: dict | None = None
    if home and run_id:
        try:
            from mini_ork.recovery.retry_hint import load_or_compute  # pyright: ignore[reportMissingImports] — soft import, may be absent
            hint = load_or_compute(Path(home), run_id)
        except ImportError:
            pass
    if hint is None and run_dir:
        path = os.path.join(run_dir, "retry-hint.json")
        if not os.path.isfile(path):
            return None
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return None
        if isinstance(data, dict):
            hint = data
    return hint


def _hint_entry_node(workflow: str, hint: dict) -> str | None:
    """The node the hint says the run stopped on, when this workflow has it.

    ``--strategy verify`` needs an explicit entry, and its fallback — "the
    first verifier in topo order" — is a guess that can land on a SIBLING of
    the node that actually failed. The closure is then that sibling's
    descendants, so the failed node is never re-dispatched at all: measured
    2026-10-16 on ``run-1792059337-779``, the hint said ``test`` but the entry
    resolved to ``typecheck``, and the revive re-ran typecheck (fresh log)
    while ``retry-exhausted/test.attempts.json`` kept its pre-revive mtime and
    the reviewer escalated on the same stale rc=77. The hint already carries
    the answer, so it is used as the default entry.

    A hint that names a node this workflow does not have — a hint outliving a
    workflow revision — returns ``None`` after a warning, so the caller keeps
    today's fallback instead of refusing the recovery on a stale name.
    """
    candidate = str(hint.get("from_node") or hint.get("failed_node") or "")
    if not candidate:
        return None
    try:
        dag = load_dag(workflow)
    except Exception as exc:  # noqa: BLE001 — an unreadable workflow keeps the fallback
        sys.stderr.write(
            f"[mini-ork-recover] could not read {workflow!r} to check the "
            f"hint's from_node {candidate!r}: {exc}\n"
        )
        return None
    if candidate in dag.node_ids:
        return candidate
    sys.stderr.write(
        f"[mini-ork-recover] retry-hint from_node {candidate!r} is not a node "
        f"of {workflow!r}; ignoring it\n"
    )
    return None


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


def _needs_restore(plan: RecoveryPlan, run_dir: str, carry_patch: str | None = None,
                   workflow: str | None = None) -> bool:
    """True iff the recovery must apply a carry patch before dispatch.

    Three conditions:

      1. The operator passed ``--carry-patch`` explicitly and it resolves to a
         file: the tree must hold it before dispatch, even when the implementer
         re-runs. A resumed implementer continues on top of its own earlier
         work, so the carried change has to be there for it to build on.
      2. The reuse set contains an implementer-typed node. Without a
         reused implementer there's nothing for the verifier to verify
         against — the tree is already clean. (The code only checks
         ``"implementer"``; do not list other types here without also
         extending the membership test below. No recipe declares another
         code-changing node type as of 2026-10-07.)
      3. The run was rolled back: ``rolled-back.json``, ``salvage.patch`` or
         the resolved carry patch (``--carry-patch`` / workflow
         ``recovery.carry_patch``) is present. Otherwise nothing to restore.

    Condition 1 is standalone; conditions 2 and 3 must both hold. The function
    short-circuits on the cheaper ``plan.reuse`` check for the implicit rule.
    """
    if not run_dir:
        return False
    from mini_ork.recovery.restore import _resolve_patch_path

    # An explicit --carry-patch is an operator instruction, not an inference
    # from the reuse set: carry it whether or not the implementer re-runs.
    if carry_patch and _resolve_patch_path(run_dir, carry_patch, None) is not None:
        return True
    has_code_reuse = any(
        plan.node_types.get(nid) in ("implementer",) for nid in plan.reuse
    )
    if not has_code_reuse:
        return False
    if os.path.isfile(os.path.join(run_dir, "rolled-back.json")):
        return True
    if os.path.isfile(os.path.join(run_dir, "salvage.patch")):
        return True
    return _resolve_patch_path(run_dir, carry_patch, workflow) is not None


def _implementer_in_closure(plan: RecoveryPlan) -> bool:
    """True iff the closure that will rerun contains an implementer-typed node.

    Only a rerun implementer edits the target in place, so only it needs the
    stale-baseline refresh. Types come from ``plan.node_types`` (workflow.yaml);
    the same membership rule ``_needs_restore`` uses for the reuse set. A recipe
    that declares no implementer-typed code-changer (no recipe does as of
    2026-10-10) never reaches the refresh.
    """
    return any(plan.node_types.get(nid) == "implementer" for nid in plan.closure)


def _refresh_stale_baseline_for_resume(plan: RecoveryPlan, run_dir: str,
                                       db_path: str) -> bool:
    """Refresh a stale ``pre-implementer-ref`` before a resumed implementer runs.

    Wires ``execute._refresh_stale_pre_impl_baseline`` into the recover dispatch
    path: a resume that re-runs the implementer reuses the run's recorded
    baseline, and a baseline an external actor has since outrun would false-fail
    the implementer with ``impl_moved_base`` even though it never moved HEAD. The
    target is resolved exactly as the original capture resolved it — the
    persisted ``run_profile.json`` roots first, then the live ladder. Fail-soft:
    any error returns False and the run keeps today's behaviour; the helper's own
    laundering refusal and fail-closed ledger read are preserved.
    """
    if not run_dir or not _implementer_in_closure(plan):
        return False
    try:
        from mini_ork.runtime.run_roots import load_run_roots, resolve_run_roots

        roots = load_run_roots(run_dir)
        target = (roots.target if roots and roots.target
                  else resolve_run_roots(run_dir).target)
        if not target:
            return False
        from mini_ork.cli import execute as _execute  # noqa: PLC0415

        return bool(_execute._refresh_stale_pre_impl_baseline(
            run_dir, target, db=db_path, run_id=plan.run_id))
    except Exception as exc:  # noqa: BLE001 — never block a recovery
        sys.stderr.write(f"[mini-ork-recover] baseline refresh skipped: {exc}\n")
        return False


def _clear_stale_stop_request(run_dir: str) -> bool:
    """Drop a ``.stop-requested`` left behind by the attempt this recovery continues.

    The flag is a *soft*-stop signal aimed at a running dispatcher: ``POST /stop``
    writes it, the executor checks it before every node, and only the hard-kill
    path unlinks it — so a run that was stopped rather than killed carries the
    file into the grave. The executor checks it at the very first node, before any
    dispatch, so `recover` used to skip the entire closure, fail the run again and
    still exit 0. Reaching here means every refusal gate has passed and the run is
    owned, so the marker can only be one this command exists to override.

    Returns True when a marker was removed. A marker that cannot be removed is
    reported and left in place: the recovery then behaves as it did before rather
    than pretending the operator's stop was honoured.
    """
    if not run_dir:
        return False
    flag = os.path.join(run_dir, ".stop-requested")
    try:
        os.unlink(flag)
    except FileNotFoundError:
        return False
    except OSError as exc:
        sys.stderr.write(
            f"[mini-ork-recover] could not clear {flag}: {exc} — the run will "
            "stop at its first node instead of continuing\n"
        )
        return False
    sys.stdout.write(
        "[mini-ork-recover] cleared a stop request left by the previous "
        "attempt; continuing\n"
    )
    return True


def _recovery_target_cwd(run_dir: str) -> str:
    """The run's recorded target worktree, or ``""`` when none is recorded.

    Deliberately NOT ``resolve_run_roots``: that ladder bottoms out at
    ``os.getcwd()``, and a value guessed from the operator's shell is the very
    thing this exists to keep out of a recovery (see the ``MO_TARGET_CWD``
    override in ``cli_main``). Only the run's own ``run_profile.json``
    ``roots.target`` is authoritative, and only while that path still exists —
    a target that is gone is left for the run's own ``require_run_target`` to
    name at first use, rather than being papered over here.
    """
    if not run_dir:
        return ""
    from mini_ork.runtime.run_roots import load_run_roots  # noqa: PLC0415

    roots = load_run_roots(run_dir)
    target = (roots.target if roots else "") or ""
    return target if target and os.path.isdir(target) else ""


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


# ─────────────────────────────────────────────────────────────────────────────
# Lane repair (kickoff lane-repair-resume §1) — ``--lane <alias>=<lane>``
# ─────────────────────────────────────────────────────────────────────────────


def _parse_lane_overrides(raw: list[str]) -> dict[str, str]:
    """Turn repeatable ``--lane <alias>=<lane>`` tokens into an ordered
    ``{alias: lane}`` map. Raises ``ValueError`` on a malformed token so the
    caller exits 2 (a bad ``--lane`` must never dispatch)."""
    out: dict[str, str] = {}
    for tok in raw:
        if "=" not in tok:
            raise ValueError(f"expected --lane <alias>=<lane>, got {tok!r}")
        alias, lane = tok.split("=", 1)
        alias = alias.strip()
        lane = lane.strip()
        if not alias or not lane:
            raise ValueError(f"expected --lane <alias>=<lane>, got {tok!r}")
        out[alias] = lane
    return out


def _node_lane_aliases(workflow: str) -> list[str]:
    """The ``model_lane`` values the workflow's nodes declare, in declaration
    order. This is the alias set ``--lane <alias>`` must belong to. A missing
    or malformed workflow yields ``[]`` (the caller reports "no aliases")."""
    import yaml  # noqa: PLC0415 — lazy; the planner must stay dep-light

    try:
        with open(workflow, encoding="utf-8") as fh:
            doc = yaml.safe_load(fh) or {}
    except (OSError, ValueError, yaml.YAMLError):
        return []
    aliases: list[str] = []
    for node in doc.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        ml = node.get("model_lane")
        if isinstance(ml, str) and ml and ml not in aliases:
            aliases.append(ml)
    return aliases


def _registry_lanes() -> list[str]:
    """Lane names the effective providers registry exposes (sorted)."""
    from mini_ork.dispatch import providers  # noqa: PLC0415

    try:
        reg = providers._load_providers_registry()
    except Exception:  # noqa: BLE001 — an unreadable registry is empty, not fatal
        return []
    return sorted(str(k) for k in reg.keys())


def _validate_lane_overrides(overrides: dict[str, str], workflow: str) -> list[str]:
    """Exit-2 error lines for any ``--lane`` alias/lane that is not known.

    The alias must be a ``model_lane`` the workflow's nodes use; the lane must
    be a key of the effective providers registry (an unknown lane must NOT
    silently resolve to ``sonnet`` via ``resolve_lane_model``'s fallback).
    Returns ``[]`` when every override is valid.
    """
    aliases = _node_lane_aliases(workflow)
    lanes = _registry_lanes()
    errs: list[str] = []
    for alias in overrides:
        if alias not in aliases:
            errs.append(
                f"unknown alias {alias!r}; valid aliases: "
                f"{', '.join(aliases) or '<none>'}"
            )
    for alias, lane in overrides.items():
        if lane not in lanes:
            errs.append(
                f"unknown lane {lane!r} (for {alias!r}); valid lanes: "
                f"{', '.join(lanes) or '<none>'}"
            )
    return errs


def _base_overlay_path(home: str | None = None) -> str | None:
    """The overlay the resumed execute will actually merge over the template.

    Mirrors ``agents_config.personal_path`` — ``$MINI_ORK_AGENTS`` when it
    points at an existing file, else ``<home>/config/agents.local.yaml``, else
    ``None`` — so the lane-repair base is the SAME overlay the execute
    resolves. Reading only ``$MINI_ORK_AGENTS`` (the pre-fix behaviour) missed
    ``agents.local.yaml`` whenever the env var was unset: pinning one alias then
    wrote a one-key overlay and silently routed every OTHER alias back to the
    team template's lane. ``personal_path`` raises ``ValueError`` when
    ``$MINI_ORK_AGENTS`` is set but names a missing file; fall back to that raw
    env value there so the (broken) path is still what the preview/log shows.
    """
    from mini_ork.dispatch import agents_config  # noqa: PLC0415

    try:
        return agents_config.personal_path(home=home)
    except ValueError:
        return os.environ.get("MINI_ORK_AGENTS")


def _old_lane(alias: str, run_dir: str, overlay: str | None) -> str:
    """The lane ``alias`` currently resolves to — the base overlay (the
    ``$MINI_ORK_AGENTS`` / ``<home>/config/agents.local.yaml`` file from
    ``_base_overlay_path``, merged on top) wins, then the run snapshot. Falls
    back to the alias itself when neither names it (mirrors
    ``resolve_lane_family``'s fail-open pass-through for the log line only)."""
    import yaml  # noqa: PLC0415

    sources = [overlay] if overlay else []
    sources.append(os.path.join(run_dir, "config", "agents.yaml"))
    for src in sources:
        if not src or not os.path.isfile(src):
            continue
        try:
            with open(src, encoding="utf-8") as fh:
                doc = yaml.safe_load(fh) or {}
            lanes = doc.get("lanes") if isinstance(doc, dict) else None
            if isinstance(lanes, dict) and alias in lanes:
                return str(lanes[alias])
        except (OSError, ValueError, yaml.YAMLError):
            continue
    return alias


def _write_lane_overlay(
    run_dir: str, overrides: dict[str, str], overlay: str | None
) -> Path:
    """Write ``<run_dir>/config/agents.recover.yaml`` = the base overlay deep-
    merged with any prior ``agents.recover.yaml`` and then ``{"lanes":
    overrides}``. Returns the overlay path to hand to the execute via
    ``MINI_ORK_AGENTS``.

    The deep merge (``agents_config.merge``) keeps the operator's other lane
    aliases: only the requested aliases are overwritten, everything else in a
    pre-existing overlay survives. A broken pre-existing overlay is treated as
    empty so a stale ``MINI_ORK_AGENTS`` never crashes ``recover`` — the
    requested ``--lane`` still lands. A prior ``agents.recover.yaml`` is folded
    in as well: a second ``recover --lane`` from a fresh shell does not carry
    the first call's flags, and the env override does not outlive its process,
    so without this the earlier pin would vanish and its node would fall back
    to the dead lane. The new ``--lane`` pins win over both.
    """
    from mini_ork.dispatch import agents_config  # noqa: PLC0415
    import yaml  # noqa: PLC0415

    base: dict = {}
    if overlay:
        try:
            base = agents_config._load_yaml(overlay, "agents overlay")
        except (ValueError, OSError):
            base = {}
    prior = Path(run_dir) / "config" / "agents.recover.yaml"
    if prior.is_file():
        try:
            base = agents_config.merge(
                base,
                agents_config._load_yaml(str(prior), "agents recover overlay"),
            )
        except (ValueError, OSError):
            pass
    merged = agents_config.merge(base, {"lanes": overrides})
    config_dir = Path(run_dir) / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    dest = config_dir / "agents.recover.yaml"
    dest.write_text(yaml.safe_dump(merged, sort_keys=True), encoding="utf-8")
    return dest


def _append_lane_log(run_dir: str, overrides: dict[str, str], overlay: str | None) -> None:
    """Append one ``<iso ts> <alias>: <old lane> -> <new lane>`` line per
    override to ``<run_dir>/recover-lanes.log``."""
    from datetime import datetime, timezone  # noqa: PLC0415

    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    lines = [
        f"{ts} {alias}: {_old_lane(alias, run_dir, overlay)} -> {lane}\n"
        for alias, lane in overrides.items()
    ]
    with open(os.path.join(run_dir, "recover-lanes.log"), "a", encoding="utf-8") as fh:
        fh.writelines(lines)


def _format_lane_block(
    overrides: dict[str, str], run_dir: str, overlay: str | None
) -> str:
    """Operator-facing ``--status`` preview of the requested lane switches
    (read-only — the write happens on the dispatch path only)."""
    lines = ["    lane overrides:"]
    for alias, lane in overrides.items():
        lines.append(f"      {alias}: {_old_lane(alias, run_dir, overlay)} -> {lane}")
    return "\n".join(lines)


def _abandon_lease(db_path: str, run_id: str, req: Any, token: str | None) -> None:
    """A refused restore dispatched nothing: close the request as failed and
    release the lease, so the ledger never records a dispatch that did not run."""
    if _lease is None:
        return
    if req:
        _lease.close_recovery(db_path, req[0], status="failed")
    if token:
        _lease.release_lease(db_path, run_id, token)


def _plan_json_is_valid(run_dir: str) -> bool:
    """True iff ``<run_dir>/plan.json`` exists and parses as a JSON object.

    The literal file is the plan authority for a revival (the same file
    ``cli_main`` hands execute positionally). A recipe-named plan
    (``<recipe>-plan.json``) is not consulted here: recover already resolves
    only ``plan.json`` for the executor, so treating another name as a plan
    would dispatch a closure with nothing to execute.
    """
    if not run_dir:
        return False
    try:
        with open(os.path.join(run_dir, "plan.json"), encoding="utf-8") as fh:
            return isinstance(json.load(fh), dict)
    except (OSError, ValueError):
        return False


def _run_has_ledger_row(db_path: str, run_id: str) -> bool:
    """True iff the run has a ``task_runs`` row (i.e. the lifecycle started it).

    Read-only and fail-soft: a missing DB, a pre-``task_runs`` schema, or any
    sqlite error all read as "no row" — the caller pairs this with the DB's
    existence to decide whether a missing plan is a pre-planning death.
    """
    if not db_path or not run_id or not os.path.isfile(db_path):
        return False
    import sqlite3  # noqa: PLC0415 — scoped to this read

    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            return con.execute(
                "SELECT 1 FROM task_runs WHERE id = ? LIMIT 1", (run_id,)
            ).fetchone() is not None
        finally:
            con.close()
    except sqlite3.Error:
        return False


def _refuse_recovery(handoff: dict, message: str, repair_markers: dict) -> int:
    """Refuse a revival before dispatch, leaving no state that claims it ran.

    ``main`` has already acquired the lease on the dispatch path, so a refusal
    here must abandon it — otherwise the run is lease-blocked for the TTL. A
    recover spawned by auto-repair also hands the run to a human instead of
    stranding ``repair.json`` at ``repairing`` (mirrors ``cli_main``'s
    ``note_stuck`` on the ``main``-level early return).
    """
    sys.stderr.write(message)
    try:
        from mini_ork.recovery import auto_repair  # noqa: PLC0415

        with scoped_environ(repair_markers):
            auto_repair.note_stuck(reason=message.strip())
    except Exception:  # noqa: BLE001 — the hand-off must never fail a recover
        pass
    request_id = handoff.get("request_id") or ""
    _abandon_lease(
        handoff.get("db_path") or "",
        handoff.get("run_id") or "",
        (request_id,) if request_id else None,
        handoff.get("lease_token"),
    )
    return 2


def _reconcile_recovered_cost(handoff: dict) -> None:
    """Raise the run's ``cost_usd`` to the ledger total for this revival.

    ``main._reconcile_run_cost`` is the only caller of
    ``cost_ledger.reconcile_run_cost``, and it lives in ``mini_ork.cli.main``'s
    lifecycle ``finally`` — a tail a ``recover`` never passes through. The
    resumed execute does charge every node it runs (``charge_node_cost`` adds
    to whatever the row already held), but nothing afterwards raises the stored
    total to the ledger, so the revival's spend stayed invisible to every
    reporter that reads ``task_runs.cost_usd``: measured 2026-10-16,
    run-1792059337-779's ``llm_calls`` summed to $7.002127 while the row still
    read $6.725933 after two revives.

    Mirrors ``main``'s fail-soft contract — an unknown run, a missing table and
    any ``sqlite3.Error`` all mean "do nothing", and a stored value is never
    lowered.
    """
    if os.environ.get("MINI_ORK_DRY_RUN", "0") == "1":
        return
    db_path = str(handoff.get("db_path") or "")
    run_id = str(handoff.get("run_id") or "")
    if not db_path or not run_id:
        return
    from mini_ork import cost_ledger  # noqa: PLC0415 — scoped to the recover tail

    cost_ledger.reconcile_run_cost(db_path, run_id)


def _rerun_rubric_prescreen(handoff: dict) -> None:
    """Re-run the advisory rubric prescreen over the recovered run's artifacts.

    A recover re-enters the execute loop directly and never passes through
    ``mini_ork.cli.main``'s lifecycle tail, so ``panel-verdict.json`` kept the
    FAILED attempt's advisory score after a successful revival
    (run-1791619207-22518 showed ``panel_score 37.5 / pass false`` over
    published artifacts). Mirrors ``main._should_run_rubric``'s guards and
    ``main``'s fail-soft call; the prescreen itself rewrites
    ``rubric.json`` + ``panel-verdict.json`` in place.
    """
    import contextlib
    import io
    import sqlite3 as _sq3  # noqa: PLC0415 — scoped, mirrors cli_main's local use

    if os.environ.get("MO_RUBRIC", "1") != "1":
        return
    if os.environ.get("MINI_ORK_DRY_RUN", "0") == "1":
        return
    run_dir = str(handoff.get("run_dir") or "")
    db_path = str(handoff.get("db_path") or "")
    run_id = str(handoff.get("run_id") or "")
    if not run_dir or not os.path.isdir(run_dir) or not db_path or not run_id:
        return
    con = _sq3.connect(db_path, timeout=5.0)
    try:
        row = con.execute(
            "SELECT kickoff_path, task_class FROM task_runs WHERE id = ?",
            (run_id,)).fetchone()
    finally:
        con.close()
    if not row or not str(row[0] or ""):
        return
    from mini_ork.gates import rubric_prescreen  # noqa: PLC0415
    root = os.environ.get("MINI_ORK_ROOT") or os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
    with contextlib.redirect_stdout(io.StringIO()):
        rubric_prescreen.mo_rubric_run_score(
            str(row[0]), run_dir, str(row[1] or "generic"),
            mini_ork_root=root,
            mini_ork_home=os.path.dirname(db_path),
            mini_ork_db=db_path,
        )


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
    allow_local = False
    lane_overrides: list[str] = []
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
        elif a == "--allow-local":
            allow_local = True
            i += 1
        elif a == "--lane":
            if i + 1 >= len(argv):
                sys.stderr.write("--lane requires <alias>=<lane>\n")
                return 2
            lane_overrides.append(argv[i + 1])
            i += 2
        elif a.startswith("--lane="):
            lane_overrides.append(a.split("=", 1)[1].strip())
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
    # The lane-repair base overlay: what the execute would merge over the
    # template (``$MINI_ORK_AGENTS`` or ``<home>/config/agents.local.yaml``).
    # Captured BEFORE any env rewrite so ``--status`` and the dispatch path
    # read the SAME pre-existing overlay (kickoff §1: "other aliases kept").
    lane_base = _base_overlay_path(
        context_env("MINI_ORK_HOME") or os.path.join(os.getcwd(), ".mini-ork")
    )
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

    # ── Parked-run gate (kickoff hr-review-resume §2): a run whose
    # ``task_runs.status='reviewing'`` is parked waiting on a human
    # review, not failed. While the inbox still has a pending node row
    # for it, refuse ``recover`` so the operator doesn't bypass the
    # review by spawning a fresh attempt against a stale tree. The
    # hint that follows in stderr points at ``mini-ork approvals list``
    # — the only path that resolves a parked run today, and the path
    # the executor itself wires (a successful approve on the LAST
    # pending row spawns ``recover --strategy resume`` for the operator
    # automatically). ``--status`` exempts the gate (refusal is a
    # dispatch-time concern, and the operator using ``--status`` is
    # inspecting state, not bypassing review). ``--force`` lets an
    # operator proceed anyway (e.g. the inbox row was orphaned and they
    # want a clean retry); the run is still parked, so this is "I know
    # what I'm doing."
    _task_status = _task_run_status(db_path, run_id)
    is_parked = _task_status == "reviewing"
    if is_parked and not force and not status_only:
        try:
            from mini_ork.gates import node_review as _node_review

            _pending = _node_review.pending_for_run(run_id, db_path=db_path)
        except Exception:  # noqa: BLE001 — never block on a module hiccup
            _pending = []
        if _pending:
            sys.stderr.write(
                f"[mini-ork-recover] run {run_id} is awaiting human review "
                "— mini-ork approvals list\n"
            )
            return 1

    # ── Lane repair (kickoff lane-repair-resume §1): parse + validate
    # ``--lane <alias>=<lane>``. Validation runs BEFORE the hint gate, status
    # print, and lease block, so a bad alias/lane exits 2 with the choices
    # listed and never writes or dispatches. The overlay write happens only on
    # the dispatch path (``--status`` stays read-only on disk).
    lane_pins: dict[str, str] = {}
    if lane_overrides:
        try:
            lane_pins = _parse_lane_overrides(lane_overrides)
        except ValueError as exc:
            sys.stderr.write(f"recover: {exc}\n")
            return 2
        lane_errors = _validate_lane_overrides(lane_pins, workflow)
        if lane_errors:
            sys.stderr.write("recover: invalid --lane value\n")
            for line in lane_errors:
                sys.stderr.write(f"  {line}\n")
            return 2

    # ── Hint gate (kickoff §5). Reads the retry hint for this run,
    # preferring ``mini_ork.recovery.retry_hint.load_or_compute`` (the
    # durable module seam owned by a parallel run) and falling back to
    # ``<run_dir>/retry-hint.json``. The hint's contract (per the
    # parallel-run owner):
    #   {retryable: bool, strategy, from_node, needs_change: null|
    #    {kind, summary, detail, evidence}, command}
    # No hint → today's behaviour. Malformed JSON → today's behaviour.
    hint = _load_retry_hint(
        run_dir,
        home=os.environ.get("MINI_ORK_HOME"),
        run_id=run_id,
    )
    if status_only:
        if lane_pins:
            sys.stdout.write(
                _format_lane_block(lane_pins, run_dir, lane_base) + "\n"
            )
        if hint is not None:
            sys.stdout.write(_format_hint_block(hint) + "\n")
        sys.stdout.write(_format_restore_plan(
            run_dir, carry_patch or None, workflow,
        ) + "\n")
    # ``--status`` must always print the hint + restore + reuse/rerun
    # plan (kickoff §3 fix 3). Refusal is a DISPATCH-time concern; the
    # status printout gives the operator the picture they need to
    # decide whether to use ``--force`` / ``--ack-change``.
    # A parked run (status='reviewing') is NOT a failure, so the
    # ``retryable: false`` / ``needs_change`` refusals do not apply
    # to it — a parked run reaches this branch only when nothing is
    # pending in the inbox, in which case it is exactly the resume
    # the executor meant for it to be.
    if (not is_parked) and hint is not None and not hint.get("retryable", True) and not force and not status_only:
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
    if (not is_parked) and hint is not None and hint.get("needs_change") and not ack_change and not status_only:
        nc = hint["needs_change"]
        # A ``kind == 'lane'`` needs_change is satisfied by the operator's
        # ``--lane <alias>=<lane>`` flag — the flag IS the change (kickoff
        # lane-repair-resume §1: the hint suggests ``recover <run> --lane
        # <alias>=<lane>``, not ``--ack-change``). Let it through when the
        # pin is present and (if the hint names an alias) matches the pin;
        # a lane hint with no ``--lane`` still refuses like any other change.
        lane_ack = (
            nc.get("kind") == "lane"
            and bool(lane_pins)
            and (not nc.get("alias") or nc.get("alias") in lane_pins)
        )
        # A ``needs_change`` kind in ``retry_hint.NO_CHANGE_KINDS`` (an
        # ``interrupted`` run, case 1.6) needs no operator change either:
        # resuming the step IS the fix, so the hint's own command passes
        # without ``--ack-change`` (kickoff interrupted-resume §2). The module
        # is a soft import — ``_load_retry_hint`` may have read a bare
        # ``retry-hint.json`` with the module absent — so read the constant
        # defensively; a missing module degrades to the old refusal.
        try:
            from mini_ork.recovery import retry_hint as _rh
            no_change_kinds = _rh.NO_CHANGE_KINDS
        except Exception:  # noqa: BLE001 — absent recovery module: no exemption
            no_change_kinds = frozenset()
        no_change_ack = nc.get("kind") in no_change_kinds
        if not lane_ack and not no_change_ack:
            sys.stderr.write(
                f"Needs a change before retrying ({nc.get('kind')!r}): "
                f"{nc.get('summary')}\n{nc.get('detail')}\n"
                f"Fix it, then rerun with --ack-change.\n"
            )
            return 1

    # ``verify`` picks its entry itself when the operator names no node, and
    # that pick can land on a sibling of the run's failed node (see
    # ``_hint_entry_node``). The hint knows which node the run stopped on, so
    # it is the default entry there. Restricted to ``verify``: the other
    # strategies start at the earliest stale node and a hint naming a node
    # further downstream would narrow that closure past a genuinely stale
    # upstream node. An explicit ``--from-node`` always wins.
    from_node_eff = from_node
    if not from_node_eff and strategy == "verify" and hint:
        from_node_eff = _hint_entry_node(workflow, hint)

    try:
        plan = plan_recovery(
            workflow, run_id, db_path, run_dir,
            recipe=recipe, task_class=task_class,
            from_node=from_node_eff, strategy=strategy,
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
        # The token fences THIS run only. Nested runs spawned by a node of the
        # closure (SDD/epic children) inherit the env, so name the run the token
        # belongs to; execute presents it only for that run.
        apply_env_overrides({"MINI_ORK_LEASE_TOKEN": _token, "MINI_ORK_LEASE_RUN_ID": run_id})

    # ── Carry-patch restore (kickoff §3 fix 5). Runs AFTER the lease
    # block above so a refused second worker (another recovery is
    # already in flight) returns rc=0 BEFORE we touch the tree. Legacy
    # pre-E3 DBs (lease_tables_present == False) fall through without
    # a lease block — restore runs anyway because no one else can hold
    # the run.
    if strategy in ("verify", "resume", "retry") and _needs_restore(
            plan, run_dir, carry_patch or None, workflow):
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
            _abandon_lease(db_path, run_id, _req, _token)
            return 1
        if status in ("no_target", "no_patch"):
            sys.stderr.write(
                "[mini-ork-recover] refusing to dispatch: cannot resolve "
                f"restore target/patch ({status}: {message}).\n"
            )
            _abandon_lease(db_path, run_id, _req, _token)
            return 1

    # ── Lane repair: write the overlay + log, then hand the overlay path to
    # the execute via MINI_ORK_AGENTS. The "old lane" is captured BEFORE the
    # env is rewritten (both writers read the same pre-existing overlay).
    if lane_pins:
        overlay_path = _write_lane_overlay(run_dir, lane_pins, lane_base)
        _append_lane_log(run_dir, lane_pins, lane_base)
        apply_env_overrides({"MINI_ORK_AGENTS": str(overlay_path)})

    # ── Stale-baseline refresh (resume re-runs the implementer) ──
    # A resumed implementer reuses the run's recorded pre-implementer-ref; if an
    # external actor advanced the target HEAD since that one-time snapshot, the
    # stale baseline false-fails the implementer node with `impl_moved_base`.
    # Drop the stale snapshot here so the pre-dispatch capture re-snapshots the
    # CURRENT tree. Gated on an implementer node being in the closure and
    # internal to the helper: a within-attempt retry keeps its snapshot, and a
    # movement the run's own ledger attributes to the prior implementer is
    # refused (never launder a committed change into an empty harvest).
    _refresh_stale_baseline_for_resume(plan, run_dir, db_path)

    # A stop request is aimed at the dispatcher being replaced, not at this one.
    # The executor honours it before the first node, so leaving it makes the
    # closure a no-op that still reports the run as failed.
    _clear_stale_stop_request(run_dir)

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
            # Recover-only facts cli_main's pre-dispatch guards need; none of
            # them changes what the executor reads.
            "allow_local": allow_local,
            "hint_kind": ((hint or {}).get("needs_change") or {}).get("kind") or "",
            "has_reuse": bool(plan.reuse),
        })
    return 0


def cli_main(argv: list[str] | None = None, *, execute_fn=None) -> int:
    """``mini-ork recover`` entry. The subcommand spawns this module in its own
    process, so ``main``'s env hand-off has no in-process executor to read it:
    the CLI used to print "dispatching closure" and exit without running a
    node. Dispatch the closure here, then close the request and release the
    lease so a later recover is not refused for the lease TTL."""
    # ``MINI_ORK_PLAN_PATH`` is the *caller's* plan (``cli/main.py`` exports it
    # for the run's own execute). The recovery execute below is handed its plan
    # positionally, and ``execute._parse_execute_argv`` seeds ``plan_path`` from
    # this env — so an inherited value makes the positional an "Unexpected
    # argument" and the recover dies rc=2 before dispatching a node (RB-03).
    # Pop it in this same loop: the positional is the only plan authority here.
    for key in ("MINI_ORK_PLAN_PATH",
                "MINI_ORK_RECOVERY_RUN_ID", "MINI_ORK_RECOVERY_SKU", "MINI_ORK_RECOVERY_CLOSURE",
                "MINI_ORK_RECOVERY_STRATEGY", "MINI_ORK_RECOVERY_FROM"):
        os.environ.pop(key, None)  # only this call's plan may drive the executor
    # ── auto-repair hand-off ──: when auto-repair spawned this recover, wait for
    # the spawning lifecycle to exit before dispatching. It still OWNS the run
    # record (``main._close_run_record`` runs in the caller's ``finally``, after
    # reflect); starting now would let that teardown see our ``executing``
    # status and flip the live repair to ``failed``. Fail-soft.
    try:
        from mini_ork.recovery import auto_repair  # noqa: PLC0415
        auto_repair.wait_for_spawner()
    except Exception:  # noqa: BLE001 — the hand-off must never fail a recover
        pass
    # The spawn markers belong to THIS recover only. Drop them from the process
    # env before dispatch, so nothing in the execute subtree (a verifier running
    # the recover tests, a nested recover) can act as this repair attempt; the
    # early-return hand-off below restores them just for its own call.
    _repair_markers = {
        key: os.environ[key]
        for key in ("MO_AUTO_REPAIR_ATTEMPT", "MO_AUTO_REPAIR_RUN_ID",
                    "MO_AUTO_REPAIR_WAIT_PID")
        if key in os.environ
    }
    apply_env_overrides({key: None for key in _repair_markers})
    handoff: dict = {}
    rc = main(argv, handoff=handoff)
    if rc != 0 or not handoff:
        # A recover that never dispatched — the hint gate refused it,
        # ``plan_recovery`` raised ``RecoveryRefused``, another process holds the
        # lease, a lane pin was invalid, or every node is reusable — leaves the
        # auto-repair attempt that spawned it with nothing running. Hand the run
        # to the human instead of stranding ``repair.json`` at
        # ``state='repairing'`` with no owner. No-op for any other caller
        # (``note_stuck`` only acts on a spawned attempt).
        try:
            from mini_ork.recovery import auto_repair  # noqa: PLC0415
            with scoped_environ(_repair_markers):
                auto_repair.note_stuck(
                    reason=f"recover exited rc={rc} without dispatching")
        except Exception:  # noqa: BLE001 — the hand-off must never fail a recover
            pass
        return rc
    # ── Recover-task-class fix (kickoff recover-task-class §1): publish the
    # run's task class so a recovered execute does not silently fall back to
    # "generic" and fail the wrong publish gates (the run's plan.json has no
    # task_class; the normal run flow sets the env, this one did not). Order:
    # the ledger row (what actually ran) → run_profile.json → plan.json →
    # leave unset. An explicitly set non-empty MINI_ORK_TASK_CLASS is never
    # overridden. Read-only and fail-soft; a read error just skips a source.
    _overrides = {
        "MINI_ORK_RUN_ID": handoff["run_id"],
        "MINI_ORK_RUN_DIR": handoff["run_dir"],
        "MINI_ORK_WORKFLOW": handoff["workflow"],
        "MINI_ORK_RECIPE": handoff["recipe"] or None,
    }
    # ── The closure must grade the run's own repo, not the operator's cwd ──
    # ``MO_TARGET_CWD`` is written by the run *lifecycle* (``cli/main.py``,
    # beside the workspace resolution) into the process env the executor and its
    # node subprocesses inherit. ``recover`` never runs that lifecycle — it
    # enters at the closure root and dispatches ``execute`` directly — so no
    # recovered run has it, whatever the closure contains: measured 2026-10-16
    # on the implementer-rooted ``rp-e3-header-v2-20261016-111144`` as well as
    # the verifier-rooted ``rp-e3-header-20261015-222115``, both recording
    # ``node-cmd/verifier_static-check.json`` with ``cwd=<worktree>`` and no
    # ``MO_TARGET_CWD`` (every run the dispatcher launched has it).
    #
    # Each verifier then resolves the repo under test from ``MO_TARGET_CWD``
    # else ``MINI_ORK_TARGET_REPO`` else ``MINI_ORK_ROOT`` else ``os.getcwd()``:
    # the operator's directory. framework-edit's static-check runs
    # ``git -C <operator cwd> apply --check`` and fails
    # ``diff-apply-check-clean`` plus the four checks that depend on it, on a
    # diff that is perfectly good — which is how three recoveries on 2026-10-16
    # were each told "the change was judged wrong — it needs a revision".
    # Recovering from another checkout is worse than failing: the verifiers
    # would grade that checkout and report the run as passing.
    #
    # An operator-set ``MO_TARGET_CWD`` always wins (the documented lever for
    # re-pointing a run whose worktree was cleaned), and an unresolvable target
    # publishes nothing rather than a guess.
    if not context_env("MO_TARGET_CWD").strip():
        _target_cwd = _recovery_target_cwd(handoff["run_dir"])
        if _target_cwd:
            _overrides["MO_TARGET_CWD"] = _target_cwd
    if not context_env("MINI_ORK_TASK_CLASS").strip():
        _task_class = ""
        try:
            import sqlite3 as _sqlite3  # noqa: PLC0415 — scoped to cli_main

            _con = _sqlite3.connect(f"file:{handoff['db_path']}?mode=ro", uri=True)
            try:
                _row = _con.execute(
                    "SELECT task_class FROM task_runs WHERE id = ?",
                    (handoff["run_id"],)).fetchone()
            finally:
                _con.close()
            _task_class = str(_row[0] or "").strip() if _row else ""
        except _sqlite3.Error:
            _task_class = ""
        if not _task_class:
            for _name in ("run_profile.json", "plan.json"):
                try:
                    with open(os.path.join(handoff["run_dir"], _name),
                              encoding="utf-8") as _fh:
                        _task_class = str(
                            (json.load(_fh) or {}).get("task_class") or "").strip()
                except (OSError, ValueError, TypeError):
                    _task_class = ""
                if _task_class:
                    break
        if _task_class:
            _overrides["MINI_ORK_TASK_CLASS"] = _task_class
    # ── RB-08: a run that died before planning cannot be revived by
    # re-entering execute. 92791 ran its implementer with no plan.json and
    # MINI_ORK_PLAN_PATH="" — the closure was dispatched against nothing.
    # Refuse before anything is published or dispatched; the plan-stage
    # message wins over a placement refusal (that run had both problems).
    # Scoped so a genuine mid-run revival is untouched: checkpointed revivals
    # prove planning finished, an `interrupted` hint proves the run was past
    # it, and a DB with no ledger row is a fixture-built mid-run revival (the
    # lane-repair / interrupted-resume suites) rather than a started run.
    _run_id = handoff["run_id"]
    _run_dir = handoff["run_dir"]
    if (not _plan_json_is_valid(_run_dir)
            and not handoff.get("has_reuse")
            and handoff.get("hint_kind") != "interrupted"
            and (_run_has_ledger_row(handoff["db_path"], _run_id)
                 or not os.path.isfile(handoff["db_path"]))):
        return _refuse_recovery(
            handoff,
            f"recover: {_run_id} failed before planning; start a new run instead\n",
            _repair_markers)
    # ── RB-06: restore the run's recorded placement. The lifecycle publishes
    # MO_PLACEMENT/MO_NODE_ENV for a fresh run; a revival re-enters execute
    # directly, so without this every node of a remote run executed on the
    # host. Restore only — never adopt MO_DEFAULT_NODE_ENV, a fresh-run
    # policy that would turn a host revival into a remote one. An operator-set
    # value wins, and a recorded host/absent placement publishes nothing
    # (today's behaviour).
    _recorded_placement, _recorded_env = read_run_placement(_run_dir)
    _placement = context_env("MO_PLACEMENT").strip().lower() or _recorded_placement
    _node_env = context_env("MO_NODE_ENV").strip() or _recorded_env
    if _placement == "remote":
        _problem = validate_placement_env(
            _placement, _node_env, env=context_env_snapshot())
        if _problem and not handoff.get("allow_local"):
            return _refuse_recovery(
                handoff,
                f"recover: {_run_id} ran with placement {_placement}; "
                "restore it or pass --allow-local\n",
                _repair_markers)
        if not _problem:
            _overrides.update(placement_env_map(_placement, _node_env))
    apply_env_overrides(_overrides)
    # ── Recover revival fix 1 (kickoff recover-revival-fixes §1): close the
    # dead attempt before dispatching. The original process died mid-node and
    # left a ``node_start`` with no ``node_end``; the stale-heartbeat watchdog
    # would otherwise read that dead node's heartbeat as a current hang and
    # abort THIS recovery. ``web.control._close_dangling_node_events`` is the
    # same closer ``board kill`` / the run reaper use. Fail-soft: any error
    # (missing table, missing state.db) is printed, never stops the recovery.
    try:
        import sqlite3 as _sqlite3  # noqa: PLC0415 — scoped to cli_main

        from mini_ork.web.control import _close_dangling_node_events  # noqa: PLC0415
        from mini_ork.web.db import StateDB  # noqa: PLC0415

        def _count_node_ends() -> int:
            _con = _sqlite3.connect(handoff["db_path"], timeout=5.0)
            try:
                return _con.execute(
                    "SELECT COUNT(*) FROM run_events WHERE run_id = ? "
                    "AND event_type = 'node_end'",
                    (handoff["run_id"],)).fetchone()[0]
            finally:
                _con.close()

        # Count how many node_end rows the closer *adds* rather than re-deriving
        # the started/ended node-id sets it already computes internally.
        _before = _count_node_ends()
        _close_dangling_node_events(StateDB(Path(handoff["db_path"])), handoff["run_id"])
        print(f"[mini-ork-recover] closed {_count_node_ends() - _before} dangling "
              "node(s) from the previous attempt")
    except Exception as _exc:  # noqa: BLE001 — fail-soft, never stop the recovery
        print(f"[mini-ork-recover] could not close dangling nodes: {_exc}",
              file=sys.stderr)
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
    finally:
        if _lease is not None:
            # Lease bookkeeping must never stop the zombie guard below: a raise
            # here would leave the row ``executing`` with no live process.
            try:
                if handoff["request_id"]:
                    _lease.close_recovery(handoff["db_path"], handoff["request_id"],
                                          status="completed" if exec_rc == 0 else "failed")
                if handoff["lease_token"]:
                    _lease.release_lease(handoff["db_path"], handoff["run_id"], handoff["lease_token"])
            except Exception as exc:  # noqa: BLE001
                sys.stderr.write(f"[mini-ork-recover] lease cleanup failed: {exc}\n")
        # ── Recover revival fix 2 (kickoff recover-revival-fixes §2): never
        # leave a zombie. If execute exited (or raised) with the row still
        # ``executing``, the board would show a dead run as working forever.
        # Reuse the reaper's compare-and-set writer; gated on the status read
        # here, so a row the executor already published is left untouched.
        # Fail-soft; the exception (if any) still propagates.
        try:
            import sqlite3 as _sqlite3  # noqa: PLC0415 — scoped to cli_main
            import time as _time  # noqa: PLC0415

            from mini_ork.orchestration.run_reaper import (  # noqa: PLC0415
                _has_unanswered_ask,
                _mark_failed,
            )

            _run_dir = Path(handoff["run_dir"])
            # A cost-paused run, or one blocked on profile answers, is waiting
            # on ``mini-ork resume`` — not dead. Leave its row alone (mirrors
            # ``run_reaper.close_run_record``). Only a *failing* exit
            # (``exec_rc != 0``, or execute raised so it stayed at its 1
            # default) is a zombie; a clean rc=0 that wrote no terminal status
            # is finished work, never failed here.
            _waiting = (_run_dir / ".cost-pause").exists() or _has_unanswered_ask(_run_dir)
            if exec_rc != 0 and not _waiting:
                _con = _sqlite3.connect(handoff["db_path"], timeout=5.0)
                try:
                    _row = _con.execute(
                        "SELECT status, updated_at FROM task_runs WHERE id = ?",
                        (handoff["run_id"],)).fetchone()
                    if _row and _row[0] in ("executing", "running"):
                        _now = int(_time.time())
                        _mark_failed(
                            _con, handoff["run_id"], _row[0], _row[1], None,
                            f"recover: execute exited rc={exec_rc} without a terminal status",
                            ended_at=_now, now=_now)
                finally:
                    _con.close()
        except Exception as _exc:  # noqa: BLE001 — fail-soft, never mask the exec result
            print(f"[mini-ork-recover] could not reap zombie row: {_exc}", file=sys.stderr)
    # ── rubric pre-screen refresh: recover bypasses ``cli.main``'s lifecycle
    # tail, so re-run the advisory prescreen over the recovered artifacts or
    # ``panel-verdict.json`` keeps the dead attempt's score (2026-10-11,
    # run-1791619207-22518). Fail-soft, before retry-notify/auto-repair.
    if handoff.get("run_dir"):
        try:
            _rerun_rubric_prescreen(handoff)
        except Exception as _exc:  # noqa: BLE001
            print(f"[mini-ork-recover] rubric prescreen failed: {_exc}", file=sys.stderr)
    # ── cost reconcile: the other half of the lifecycle tail ``cli.main``
    # owns. Placed with the rubric refresh above and for the same reason — both
    # are teardown steps a recover would otherwise skip — and after every node
    # of the closure, which is when ``main`` runs it. Fail-soft: an error here
    # must not change the exit code the dispatch returned.
    if handoff.get("db_path") and handoff.get("run_id"):
        try:
            _reconcile_recovered_cost(handoff)
        except Exception as _exc:  # noqa: BLE001
            print(f"[mini-ork-recover] cost reconcile failed: {_exc}", file=sys.stderr)
    # ── remote node release: a recover owns the run's lifecycle for its own
    # span, like ``cli.main``'s ``_run_lifecycle`` (whose ``finally`` releases).
    # Under ``--placement remote`` the resumed execute and the rubric refresh
    # above open a session, and the lifecycle that started the run has already
    # released its node, so a provider creates a FRESH one — which nothing else
    # would release before the TTL (Daytona e2e run-1791912327-41843: auto-repair
    # re-verified after the run and left a sandbox running). Placed after every
    # check of this process and before auto-repair, whose child recover releases
    # at its own end. No-op when the run is not remote.
    try:
        from mini_ork.cli.main import _release_remote_session  # noqa: PLC0415
        _release_remote_session()
    except Exception as _exc:  # noqa: BLE001 — teardown is best-effort; the TTL is the net
        print(f"[mini-ork-recover] remote node release failed: {_exc}", file=sys.stderr)
    # ── retry-notify (kickoff lane-repair-resume §2): mirror ``mini_ork.cli.main``
    # so a re-failed recover reports the owner + fix steps like a fresh ``run``
    # does. Fail-soft — never changes the run's exit code. Only fires when the
    # resumed execute actually ran and failed (a dispatch that never reached the
    # executor leaves ``handoff`` empty and returns before this point).
    if exec_rc != 0 and handoff.get("run_dir") and os.path.isdir(handoff["run_dir"]):
        try:
            from mini_ork.recovery import retry_notify  # noqa: PLC0415

            home = os.environ.get("MINI_ORK_HOME") or os.path.join(os.getcwd(), ".mini-ork")
            retry_notify.notify(Path(home), handoff["run_id"])
        except Exception:  # noqa: BLE001
            pass
    # ── auto-repair: a revived run that fails again re-enters the loop. Mirror
    # ``mini_ork.cli.main``'s hook (kickoff auto-repair-loop). UNCONDITIONAL —
    # a withdrawn publish sets status='failed' while exec_rc == 0 — and quiet:
    # ``maybe_repair`` reads the DB status itself and never raises, so the
    # recover flow's exit code and stdout are unchanged. ``wait_for_exit``: this
    # process released the lease and reaped its row above, so it hands the run
    # to the child and must be gone before the child dispatches.
    try:
        from mini_ork.recovery import auto_repair  # noqa: PLC0415
        _repair_home = os.environ.get("MINI_ORK_HOME") or (
            os.path.dirname(handoff.get("db_path") or "") or None)
        if _repair_home and handoff.get("run_id"):
            auto_repair.maybe_repair(Path(_repair_home), handoff["run_id"],
                                     wait_for_exit=True)
    except Exception:  # noqa: BLE001
        pass
    return exec_rc


if __name__ == "__main__":
    raise SystemExit(cli_main())
