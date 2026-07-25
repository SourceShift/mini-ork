"""Recovery plan computation — reuse/rerun/closure sets.

Parity port: moved verbatim from ``mini_ork/recovery/planner.py`` (SOLID
SRP split). This module owns the **plan computation**: given a run_id +
workflow.yaml, it reads the E1 checkpoint decisions and walks the DAG
(``mini_ork.recovery.dag``) to find the **dependency closure** — every
node that transitively depends on a non-reusable node must rerun,
because its inputs may now differ. A node whose outputs are reused is
NOT in the set, even if a parallel sibling failed.

It also owns the ``--status`` pretty-printer (``format_status``) —
pure read, no dispatch.

Lease wiring, env emission, argv parsing, and ``main()`` stay in
``planner.py``; everything here is re-exported from there for parity.

Public API (re-exported from ``mini_ork.recovery.planner``):

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

Design invariants (E2 must not break):

  * Never writes the ``node_checkpoints`` table. Read-only on E1 state.
  * Never edits ``bin/mini-ork resume`` (cost-pause). Recovery MAY CALL
    resume (as a child process) but does not extend its surface.
  * Never introduces leases or turn-resume (E3/E4). The closure is a
    read-time computation; no scheduling primitive is added.
"""
from __future__ import annotations

import dataclasses
import hashlib
import os
import sqlite3

# E1 seam — read by is_node_reusable for the per-node reuse decision.
# Importing at module top means a runtime absence of E1 surfaces
# immediately as ImportError on first call (fail loud).
from mini_ork.recovery.dag import DAG, load_dag
from mini_ork.stores import checkpoints as mc

__all__ = [
    "RecoveryPlan",
    "RECOVERY_STRATEGIES",
    "compute_recovery",
    "plan_recovery",
    "format_status",
]

# Strategy enum — strings, not Enum, so JSON serialization stays trivial.
RECOVERY_STRATEGIES = ("resume", "retry", "repair", "pause")


@dataclasses.dataclass
class RecoveryPlan:
    """The output of compute_recovery / plan_recovery.

    Attributes:
      run_id         — echo of the input
      recipe         — resolved recipe name (used for cost-side display)
      task_class     — resolved task_class (used for routing on retry)
      closure        — set of node_ids to rerun (the minimal set; the
                       earliest non-reusable + its transitive dependents)
      reuse          — set of node_ids whose E1 row is reusable
      failed_node    — earliest non-reusable node in topo order (the root
                       of the closure). None if every node is reusable.
      first_node     — the entry node the execute loop should start at
                       (== failed_node unless from_node overrides)
      from_node      — the operator override (echo)
      strategy       — one of RECOVERY_STRATEGIES
      cost_boundary  — dict with ``paused`` (bool) and ``node`` (str|None)
                       for the ``--status`` print; the execute loop reads
                       ``MINI_ORK_REPAIR_BUDGET`` from env, this is just
                       a display field
      reason         — human-readable explanation of why each failed node
                       failed (keyed by node_id) for the ``--status`` print
      sku            — stable hash of (run_id, recipe, task_class) used
                       to detect "the closure was computed against the
                       same inputs we now want to dispatch". Exec compares
                       to its own sku before honoring the plan; mismatch
                       → recompute."""

    run_id: str
    recipe: str
    task_class: str
    closure: set[str]
    reuse: set[str]
    failed_node: str | None
    first_node: str | None
    from_node: str | None
    strategy: str
    cost_boundary: dict
    reason: dict[str, str]
    sku: str

    def to_dict(self) -> dict:
        out = dataclasses.asdict(self)
        out["closure"] = sorted(self.closure)
        out["reuse"] = sorted(self.reuse)
        return out


# ─────────────────────────────────────────────────────────────────────────────
# E1 lookup helpers
# ─────────────────────────────────────────────────────────────────────────────

def _current_input_hash_for_node(
    run_id: str, node_id: str, recipe_eff: str
) -> str:
    """Compute the SAME per-(run, node) input hash the E1 checkpoint
    writer computed at write-time. Mirrors ``_make_checkpoint_fn`` in
    ``mini_ork_execute.py``: a sha256 of ``{run_id}|{node_id}|{recipe}``.

    Exposed at module-level so tests can stub it (the test seeds a row
    with the matching hash and asserts the planner sees it as reusable).
    """
    return hashlib.sha256(
        f"{run_id}|{node_id}|{recipe_eff}".encode()
    ).hexdigest()


def _current_config_hash(task_class: str, recipe_eff: str, run_id: str) -> str:
    """Mirror of ``_make_checkpoint_fn``'s config_hash:
    sha256 of ``{task_class}|{recipe}|{run_id}``. Stable across the
    planner / execute so a planner decision is honored at execute time.
    """
    return hashlib.sha256(
        f"{task_class}|{recipe_eff}|{run_id}".encode()
    ).hexdigest()


def _reusable_set(
    dag: DAG,
    db_path: str,
    run_id: str,
    run_dir: str,
    *,
    recipe: str,
    task_class: str,
) -> tuple[set[str], dict[str, str]]:
    """For every node in the DAG, ask E1 ``is_node_reusable`` and return
    (reuse_set, reason_map). reason_map[node] = "" if reusable, else a
    short explanation (no row / status=failure / hash mismatch /
    missing artifact / corrupt artifact) for the ``--status`` print.

    A node with NO row is the common case for nodes that never ran
    (e.g. a failed predecessor kept a downstream branch from being
    dispatched). We treat those as "not reusable" so they end up in
    the closure naturally — but we mark the reason as ``"no_row"``
    rather than ``"hash_mismatch"`` because the operator expectation
    is different (a hash mismatch implies a config-change invalidation;
    a no_row means "this node never ran").
    """
    recipe_eff = recipe or "unknown"
    tc_eff = task_class or "generic"
    reuse: set[str] = set()
    reason: dict[str, str] = {}
    for nid in dag.node_ids:
        # Direct DB read first to classify the failure mode. Cheap,
        # and the planner needs the distinction for the --status print
        # (a "no_row" node is NOT a regression — it's a never-dispatched
        # downstream; a "hash_mismatch" node IS a regression signal).
        row_status = _peek_row_status(db_path, run_id, nid)
        if row_status is None:
            reason[nid] = "no_row"
            continue
        if row_status != "success":
            reason[nid] = f"status={row_status}"
            continue
        reusable = mc.is_node_reusable(
            db_path, run_id, nid,
            current_input_hash=_current_input_hash_for_node(
                run_id, nid, recipe_eff),
            current_recipe_version=recipe_eff,
            current_config_hash=_current_config_hash(tc_eff, recipe_eff, run_id),
            run_dir=run_dir,
        )
        if reusable:
            reuse.add(nid)
            reason[nid] = ""
        else:
            reason[nid] = "hash_mismatch_or_artifact_corrupt"
    return reuse, reason


def _peek_row_status(db_path: str, run_id: str, node_id: str) -> str | None:
    """Return the ``status`` column from ``node_checkpoints`` for a node,
    or None if no row exists. Read-only — never writes.

    This is a duplication of part of ``is_node_reusable``'s internal
    SELECT, but the planner needs the classification BEFORE the
    full validity check (which would collapse "no row" and "hash
    mismatch" into the same False). The two helpers stay consistent
    by reading the same columns (input/recipe/config/manifest/status).
    """
    if not db_path or not os.path.isfile(db_path):
        return None
    try:
        con = sqlite3.connect(db_path, timeout=5.0)
        con.execute("PRAGMA busy_timeout=5000")
        try:
            row = con.execute(
                "SELECT status FROM node_checkpoints WHERE run_id=? AND node_id=?",
                (run_id, node_id),
            ).fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return None
    return row[0] if row else None


def _sku(run_id: str, recipe: str, task_class: str) -> str:
    return hashlib.sha256(
        f"{run_id}|{recipe}|{task_class}".encode()
    ).hexdigest()[:12]


# ─────────────────────────────────────────────────────────────────────────────
# Closure computation
# ─────────────────────────────────────────────────────────────────────────────

def compute_recovery(
    workflow_yaml_path: str,
    run_id: str,
    db_path: str,
    run_dir: str,
    *,
    recipe: str = "",
    task_class: str = "generic",
    from_node: str | None = None,
) -> RecoveryPlan:
    """Compute the dependency-closure recovery plan.

    Algorithm (deterministic, no LLM):
      1. Load the DAG from workflow.yaml.
      2. For each node, ask ``is_node_reusable`` → reuse set.
      3. The earliest non-reusable node in topo order is the
         ``failed_node`` (the root of the closure). It is the
         FIRST node whose upstream chain is still intact but whose
         own outputs are not safe to skip.
      4. The closure is ``failed_node + every descendant of failed_node``.
         A node whose only "data flow" path runs through the failed node
         must rerun; a parallel branch's node does NOT.
      5. If ``from_node`` is supplied, the closure is overridden to
         ``from_node + descendants(from_node)`` — operator wants a
         wider rerun. ``failed_node`` is recomputed to ``from_node``
         so ``--status`` prints a coherent picture.
      6. If every node is reusable (reuse == all), closure = empty,
         failed_node = None. ``mini-ork recover`` returns rc=0 with a
         "nothing to do" message — distinct from "no plan", so the
         operator can tell a clean run from a missing one.
    """
    if not run_id:
        raise ValueError("compute_recovery: run_id is required")
    dag = load_dag(workflow_yaml_path)
    recipe_eff = recipe or "unknown"
    tc_eff = task_class or "generic"
    reuse, reason = _reusable_set(
        dag, db_path, run_id, run_dir,
        recipe=recipe_eff, task_class=tc_eff,
    )
    all_set = set(dag.node_ids)

    # Find the earliest non-reusable node in topo order — the closure root.
    failed_node: str | None = None
    for nid in dag.topo:
        if nid not in reuse:
            failed_node = nid
            break

    if from_node:
        if from_node not in all_set:
            raise ValueError(
                f"compute_recovery: --from-node {from_node!r} is not in workflow.yaml"
            )
        closure = dag.descendants(from_node)
        failed_node = from_node
    elif failed_node is None:
        # All nodes reusable → empty closure. Operator gets a clean
        # "nothing to recover" message rather than a vacuous loop.
        closure = set()
    else:
        closure = dag.descendants(failed_node)

    # Re-express the reason map with the operator-friendly labels the
    # status printer expects. ``reason`` already covers every node;
    # closure nodes that ARE reusable (impossible by construction, but
    # defensive) are masked out.
    reason_view: dict[str, str] = {}
    for nid in all_set:
        if nid in reuse:
            reason_view[nid] = "reusable"
        else:
            reason_view[nid] = reason.get(nid, "no_row")

    # first_node is the closure root in topo order. execute.py honors
    # this to skip ancestors entirely (no dispatch, no LLM call, no
    # trace). If closure is empty, first_node is None — execute will
    # print "nothing to do" and exit 0.
    if closure:
        first_node = next(nid for nid in dag.topo if nid in closure)
    else:
        first_node = None

    return RecoveryPlan(
        run_id=run_id,
        recipe=recipe_eff,
        task_class=tc_eff,
        closure=closure,
        reuse=reuse,
        failed_node=failed_node,
        first_node=first_node,
        from_node=from_node,
        strategy="resume",  # default; plan_recovery overrides
        cost_boundary={"paused": False, "node": None},
        reason=reason_view,
        sku=_sku(run_id, recipe_eff, tc_eff),
    )


def plan_recovery(
    workflow_yaml_path: str,
    run_id: str,
    db_path: str,
    run_dir: str,
    *,
    recipe: str = "",
    task_class: str = "generic",
    from_node: str | None = None,
    strategy: str = "resume",
) -> RecoveryPlan:
    """Same as ``compute_recovery`` but pins the strategy into the plan.

    Strategy semantics (E2 scope; leases/turns are E3/E4):
      * ``resume``  — start at the closure root (earliest non-reusable).
                     Default. Mirrors the operator intuition of "where
                     did things stop working".
      * ``retry``   — start at the FIRST non-reusable node in topo order
                     (== the closure root). Same entry as ``resume`` but
                     semantically distinct: the operator is saying "I
                     already know which node failed; just rerun it"
                     rather than "continue from where we left off".
      * ``repair``  — same closure as ``resume``, but sets
                     ``MO_REPAIR_BUDGET`` so the execute loop can refuse
                     further retries if the cost ceiling is hit.
      * ``pause``   — compute the plan, print it, DO NOT dispatch.
                     Return rc=0 with the closure set as JSON on stdout
                     so an external operator (human or script) can
                     invoke ``recover resume`` after reviewing.
    """
    if strategy not in RECOVERY_STRATEGIES:
        raise ValueError(
            f"plan_recovery: strategy must be one of {RECOVERY_STRATEGIES}, got {strategy!r}"
        )
    plan = compute_recovery(
        workflow_yaml_path, run_id, db_path, run_dir,
        recipe=recipe, task_class=task_class, from_node=from_node,
    )
    # Retry semantics: same entry, but the plan carries the operator's
    # explicit "I know what's broken" intent for downstream trace
    # metadata. No behavioral change in this E2 increment; the field
    # is here so future cost-aware routing can use it.
    plan.strategy = strategy
    if strategy == "repair":
        plan.cost_boundary = {"paused": False, "node": None, "budget_usd": _repair_budget_default()}
    return plan


def _repair_budget_default() -> float:
    """Default per-recovery cost ceiling. Reads MO_REPAIR_BUDGET_USD if
    set (operator override); falls back to a conservative 5.00 USD so
    a repair recovery can never silently burn the run's full budget.
    E3/E4 will tighten this against the existing per-run budget_cap_usd.
    """
    raw = os.environ.get("MO_REPAIR_BUDGET_USD", "")
    try:
        v = float(raw)
        if 0 < v < 1000:
            return v
    except (TypeError, ValueError):
        pass
    return 5.00


# ─────────────────────────────────────────────────────────────────────────────
# Status printer (no dispatch)
# ─────────────────────────────────────────────────────────────────────────────

def format_status(plan: RecoveryPlan) -> str:
    """Pretty-print a RecoveryPlan for ``mini-ork recover --status``.

    Pure read; no LLM dispatch; no execute invocation. The
    ``status_no_dispatch`` verifier contract asserts that this function
    never calls into the dispatcher (it has no seam to do so).
    """
    lines: list[str] = []
    lines.append(f"=== mini-ork recover — status (run_id={plan.run_id}) ===")
    lines.append(f"    recipe:     {plan.recipe}")
    lines.append(f"    task_class: {plan.task_class}")
    lines.append(f"    strategy:   {plan.strategy}")
    lines.append("")
    lines.append(f"    reuse ({len(plan.reuse)} node{'s' if len(plan.reuse) != 1 else ''}):")
    if plan.reuse:
        for nid in sorted(plan.reuse):
            lines.append(f"      [reuse]  {nid}")
    else:
        lines.append("      (none)")
    lines.append("")
    lines.append(f"    rerun ({len(plan.closure)} node{'s' if len(plan.closure) != 1 else ''}):")
    if plan.closure:
        # Print in topo-friendly order: closure root first, then BFS by
        # descendant depth so the operator reads top-to-bottom.
        order = sorted(plan.closure, key=lambda n: (
            0 if n == plan.first_node else 1, n))
        for nid in order:
            r = plan.reason.get(nid, "")
            tag = "first" if nid == plan.first_node else "      "
            tail = f"  ({r})" if r else ""
            lines.append(f"      [{tag}] {nid}{tail}")
    else:
        lines.append("      (none — every node is reusable)")
    lines.append("")
    if plan.cost_boundary.get("budget_usd") is not None:
        lines.append(
            f"    cost boundary (repair): ${plan.cost_boundary['budget_usd']:.2f} ceiling"
        )
    elif plan.cost_boundary.get("paused"):
        lines.append(
            f"    cost boundary: paused at node={plan.cost_boundary.get('node')}"
        )
    else:
        lines.append("    cost boundary: (none)")
    lines.append("")
    if plan.first_node:
        lines.append(f"    entry: {plan.first_node}")
    else:
        lines.append("    entry: (none — nothing to recover)")
    lines.append("")
    lines.append(f"    sku: {plan.sku}")
    return "\n".join(lines) + "\n"
