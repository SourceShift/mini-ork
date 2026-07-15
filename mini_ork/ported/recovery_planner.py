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
"""
from __future__ import annotations

import dataclasses
import hashlib
import os
import sqlite3
import sys

# E1 seam — read by is_node_reusable for the per-node reuse decision.
# Importing at module top means a runtime absence of E1 surfaces
# immediately as ImportError on first call (fail loud).
from mini_ork.ported import mini_ork_checkpoints as mc  # noqa: E402

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

# Strategy enum — strings, not Enum, so JSON serialization stays trivial.
RECOVERY_STRATEGIES = ("resume", "retry", "repair", "pause")


@dataclasses.dataclass(frozen=True)
class DAG:
    """Parsed workflow.yaml. All adjacency lists are dicts keyed by node id.

    ``parents[node]`` lists nodes that produce data flowing into ``node``.
    ``children[node]`` lists nodes that consume data produced by ``node``.
    ``topo`` is a stable topological sort (Kahn's algorithm; ties broken
    by workflow.yaml declaration order)."""

    node_ids: tuple[str, ...]
    parents: dict[str, tuple[str, ...]]
    children: dict[str, tuple[str, ...]]
    topo: tuple[str, ...]

    def descendants(self, root: str) -> set[str]:
        """All nodes transitively downstream of ``root`` (incl. ``root``)."""
        if root not in self.children:
            return {root}
        seen: set[str] = set()
        stack = [root]
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            stack.extend(self.children.get(cur, ()))
        return seen


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
# DAG loader
# ─────────────────────────────────────────────────────────────────────────────

def _yaml_load(path: str) -> dict:
    try:
        import yaml  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "recovery_planner: PyYAML is required to parse workflow.yaml"
        ) from e
    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        return {}
    return data


def load_dag(workflow_yaml_path: str) -> DAG:
    """Parse ``workflow.yaml`` into a ``DAG``.

    Edges with ``edge_type == "escalates_to"`` are EXCLUDED from the
    dependency relation: they are operator-path edges, not data flow
    edges. A failed verifier escalating to rollback must not pull the
    whole DAG into the closure (that would defeat the point of E2).
    All other edge_types (``depends_on``, ``supplies_context_to``,
    ``verifies``) are treated as data-flow deps — see module docstring
    for the topology convention.
    """
    if not workflow_yaml_path or not os.path.isfile(workflow_yaml_path):
        raise FileNotFoundError(
            f"recovery_planner: workflow.yaml not found: {workflow_yaml_path!r}"
        )
    wf = _yaml_load(workflow_yaml_path)
    nodes = wf.get("nodes") or []
    if not isinstance(nodes, list):
        raise ValueError(
            f"recovery_planner: workflow.yaml nodes must be a list, got {type(nodes).__name__}"
        )
    declared_order: list[str] = []
    parents: dict[str, list[str]] = {}
    children: dict[str, list[str]] = {}
    for n in nodes:
        if not isinstance(n, dict):
            continue
        nid = str(n.get("name") or "").strip()
        if not nid:
            continue
        declared_order.append(nid)
        parents.setdefault(nid, [])
        children.setdefault(nid, [])

    for e in (wf.get("edges") or []):
        if not isinstance(e, dict):
            continue
        src = str(e.get("from") or "").strip()
        dst = str(e.get("to") or "").strip()
        if not src or not dst:
            continue
        if src not in children or dst not in parents:
            # Edge references an unknown node — ignore (workflow.yaml
            # validation belongs to plan, not the recovery planner).
            continue
        if str(e.get("edge_type") or "").strip() == "escalates_to":
            continue
        # Dedup per-node adjacency.
        if dst not in children[src]:
            children[src].append(dst)
        if src not in parents[dst]:
            parents[dst].append(src)

    # Stable topo sort: Kahn's algorithm with declared-order tiebreak.
    topo: list[str] = []
    indeg: dict[str, int] = {nid: len(parents.get(nid, ())) for nid in declared_order}
    # Use a sorted "ready" queue so ties break by declaration order.
    ready: list[str] = [nid for nid in declared_order if indeg[nid] == 0]
    ready.sort(key=declared_order.index)
    while ready:
        ready.sort(key=declared_order.index)
        cur = ready.pop(0)
        topo.append(cur)
        for child in children.get(cur, ()):
            indeg[child] -= 1
            if indeg[child] == 0 and child not in topo:
                ready.append(child)
    # Cycle guard: anything left in `indeg > 0` after Kahn's is a cycle
    # (workflow.yaml is a DAG per E1 contract; surface the error rather
    # than silently mis-computing the closure).
    if len(topo) != len(declared_order):
        leftover = [nid for nid in declared_order if nid not in topo]
        raise ValueError(
            "recovery_planner: workflow.yaml has a cycle through "
            f"{leftover}; cannot compute a dependency closure"
        )
    return DAG(
        node_ids=tuple(declared_order),
        parents={k: tuple(v) for k, v in parents.items()},
        children={k: tuple(v) for k, v in children.items()},
        topo=tuple(topo),
    )


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
    """Export the closure set + sku into the env so a follow-up
    ``mini-ork execute`` invocation can pick it up. Keyed by run_id
    so concurrent recoveries in different terminal sessions don't
    cross-contaminate.
    """
    os.environ["MINI_ORK_RECOVERY_RUN_ID"] = plan.run_id
    os.environ["MINI_ORK_RECOVERY_SKU"] = plan.sku
    os.environ["MINI_ORK_RECOVERY_CLOSURE"] = " ".join(sorted(plan.closure))
    if plan.first_node:
        os.environ["MINI_ORK_RECOVERY_FROM"] = plan.first_node
    os.environ["MINI_ORK_RECOVERY_STRATEGY"] = plan.strategy


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
    positional: list[str] = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--from-node":
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

    # Active strategies: emit the env, print the plan, hand off to
    # mini-ork-execute which honors MINI_ORK_RECOVERY_FROM + CLOSURE.
    _emit_recovery_env(plan)
    sys.stdout.write(format_status(plan))
    sys.stdout.write(
        f"[mini-ork-recover] dispatching closure from node={plan.first_node} "
        f"({len(plan.closure)} node{'s' if len(plan.closure) != 1 else ''} to rerun)\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
