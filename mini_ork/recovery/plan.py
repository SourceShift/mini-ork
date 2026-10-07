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
    "RecoveryRefused",
    "compute_recovery",
    "plan_recovery",
    "format_status",
]


class RecoveryRefused(ValueError):
    """A recovery that the planner can compute but refuses to dispatch.

    Distinct from ``ValueError`` (which is reserved for programmer errors
    — bad strategy, unknown ``--from-node``) so the CLI boundary in
    ``mini_ork.recovery.planner.main`` can catch refusal messages
    separately and surface the operator hint without a traceback.

    The kickoff's two refusals live here:

      * ``verify`` with an upstream LLM node lacking a reusable checkpoint.
      * The change-needed gate (``needs_change`` set in the retry hint
        without ``--ack-change``) — though that one is normally raised
        higher in the CLI layer.
    """

    def __init__(self, message: str, *, kind: str = "refused"):
        super().__init__(message)
        self.kind = kind


# Strategy enum — strings, not Enum, so JSON serialization stays trivial.
# ``reattach`` (remote-nodes-10): re-attach to a still-running remote proc
# whose ``remote_procs`` row is unconsumed. The default-selection rule
# lives in ``compute_recovery`` (probes the table for the first-incomplete
# node); the strategy dispatch itself is out of scope for E2 — the
# execute-loop executor branch that honors ``strategy="reattach"`` is a
# separate concern (the dispatch-side execution of reattach), so a
# ``--strategy reattach`` plan currently produces the same closure as
# ``resume`` until the executor learns to short-circuit dispatch when the
# proc is already running on the VM.
#
# ``verify`` (kickoff §3): re-run only the verifier chain while reusing
# every upstream LLM node. Entry = first ``verifier`` in topo order (or
# ``--from-node``); every upstream node must be reusable, with the planner
# treated as implicitly reusable when ``<run_dir>/plan.json`` parses.
RECOVERY_STRATEGIES = ("resume", "retry", "repair", "pause", "reattach", "verify")


# ─────────────────────────────────────────────────────────────────────────────
# workflow.yaml node-type helper
# ─────────────────────────────────────────────────────────────────────────────


def _node_types(workflow_yaml_path: str) -> dict[str, str]:
    """Parse ``workflow.yaml`` a second time to surface each node's ``type``.

    The DAG loader (``mini_ork.recovery.dag.load_dag``) deliberately
    discards ``type`` (see ``dag.py:105-113``) — the closure is
    type-agnostic. ``compute_recovery`` needs node types for two
    decisions: the ``verify`` strategy's entry resolution ("first
    verifier in topo order") and the planner-as-reusable shortcut
    (kickoff §2: ``<run_dir>/plan.json`` makes the skipped-planner
    reusable). The lens recommended the second-yaml-parse option (a) so
    the diff stays inside the declared file surface; this is that parse.

    Soft-imports PyYAML the same way ``dag._yaml_load`` does
    (``dag.py:67-78``); on ImportError or any YAML error, returns ``{}``
    so the planner falls back to a type-blind plan (the closure still
    computes correctly; only the type-keyed shortcuts are skipped).
    """
    if not workflow_yaml_path or not os.path.isfile(workflow_yaml_path):
        return {}
    try:
        import yaml  # type: ignore[import-untyped]

        with open(workflow_yaml_path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except (OSError, ImportError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, str] = {}
    for n in data.get("nodes") or []:
        if not isinstance(n, dict):
            continue
        nid = str(n.get("name") or "").strip()
        typ = str(n.get("type") or "").strip()
        if nid and typ:
            out[nid] = typ
    return out


def _ancestors(dag: DAG, node: str) -> set[str]:
    """Every node transitively upstream of ``node`` (incl. ``node``).

    Mirrors :meth:`DAG.descendants` (``dag.py:52``) but walks the parent
    map. Used by the ``verify`` strategy's upstream-reusability check
    (kickoff §3). ``node`` is included so callers can ask "is ``node``
    itself reusable" without a second membership test.
    """
    if node not in dag.parents:
        return {node}
    seen: set[str] = set()
    stack = [node]
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        stack.extend(dag.parents.get(cur, ()))
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
                       → recompute.
      node_types     — name → type map parsed from workflow.yaml (used by
                       the ``verify`` strategy's entry resolution; carries
                       through ``to_dict`` for downstream consumers)."""

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
    node_types: dict[str, str] = dataclasses.field(default_factory=dict)

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
    node_types: dict[str, str] | None = None,
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

    Planner shortcut (kickoff §2 / lens §1.2): the planner node is special —
    ``mini_ork/cli/execute_handlers.py:453`` short-circuits when
    ``MINI_ORK_RECOVERY_CLOSURE`` is set, so the planner never writes a
    checkpoint row. Today its ``no_row`` makes it the closure root and
    swallows every reusable upstream node. To fix this without scope_cheek,
    a planner-typed node with no checkpoint row is counted as reusable
    (reason ``"plan_json"``) when ``<run_dir>/plan.json`` exists and
    parses as JSON. Any other ``no_row`` node keeps today's behaviour.
    """
    recipe_eff = recipe or "unknown"
    tc_eff = task_class or "generic"
    types = node_types or {}
    reuse: set[str] = set()
    reason: dict[str, str] = {}
    plan_json_present = _plan_json_present_and_parses(run_dir)
    for nid in dag.node_ids:
        # Direct DB read first to classify the failure mode. Cheap,
        # and the planner needs the distinction for the --status print
        # (a "no_row" node is NOT a regression — it's a never-dispatched
        # downstream; a "hash_mismatch" node IS a regression signal).
        row_status = _peek_row_status(db_path, run_id, nid)
        if row_status is None:
            if plan_json_present and types.get(nid) == "planner":
                reuse.add(nid)
                reason[nid] = "plan_json"
                continue
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


def _plan_json_present_and_parses(run_dir: str) -> bool:
    """True iff ``<run_dir>/plan.json`` exists and parses as JSON.

    Cheap, side-effect-free read; used by ``_reusable_set`` to gate the
    planner-as-reusable shortcut. A missing or unparseable plan.json is
    treated as "not present" so a run whose plan was lost falls back to
    the legacy ``no_row`` behaviour rather than claiming the planner
    is reusable on speculation.
    """
    if not run_dir:
        return False
    import json
    path = os.path.join(run_dir, "plan.json")
    if not os.path.isfile(path):
        return False
    try:
        with open(path, encoding="utf-8") as fh:
            json.load(fh)
        return True
    except (OSError, ValueError):
        return False


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


def _probe_reattach_default(
    db_path: str, run_id: str, first_node: str | None
) -> bool:
    """Return True when the first-incomplete node has an unconsumed
    ``remote_procs`` row that the ``reattach`` strategy should pick up.

    epic 10 (kickoff §4): the default-selection rule lives here so the
    CLI never has to guess. Pure read against the table the spawn-side
    journal writes; ``state`` in {``running``, ``detached``, ``starting``}
    means the VM still has the child alive and a re-attach is safe.
    An ``exited`` row is still useful (the proc finished while the
    control plane was away; harvest its output) — counted as reattach
    too because the harvest path is the same code branch.

    Returns False when ``first_node`` is None (the closure is empty —
    nothing to reattach), the DB is missing, or no row exists. False
    never overrides an explicit ``strategy="reattach"`` the caller
    already chose; it's a defaulting helper, not an enforcement point.
    """
    if not first_node:
        return False
    if not db_path or not os.path.isfile(db_path):
        return False
    try:
        con = sqlite3.connect(db_path, timeout=5.0)
        con.execute("PRAGMA busy_timeout=5000")
        try:
            row = con.execute(
                "SELECT 1 FROM remote_procs"
                " WHERE run_id=? AND node_id=?"
                " AND state IN ('starting','running','detached','exited')"
                " LIMIT 1",
                (run_id, first_node),
            ).fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return False
    return row is not None


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
    node_types = _node_types(workflow_yaml_path)
    reuse, reason = _reusable_set(
        dag, db_path, run_id, run_dir,
        recipe=recipe_eff, task_class=tc_eff,
        node_types=node_types,
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

    # No node may appear in BOTH reuse and rerun (kickoff §3 fix 2):
    # the status printer and downstream consumers assume the sets are
    # disjoint. Subtraction runs after closure is final (covers both
    # the from_node override and the auto-detected failure root) and
    # before ``reason_view`` is built so a closure node is never
    # labelled "reusable" on the --status printout.
    reuse -= closure

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
        # remote-nodes-10: default to ``reattach`` when the first-incomplete
        # node has an unconsumed ``remote_procs`` row (epic 10 kickoff §4).
        # The CLI never has to guess — the probe lives in the planner so a
        # bare ``mini-ork recover <run>`` Just Works for laptop-sleep cases.
        strategy=("reattach" if _probe_reattach_default(db_path, run_id, first_node)
                  else "resume"),
        cost_boundary={"paused": False, "node": None},
        reason=reason_view,
        sku=_sku(run_id, recipe_eff, tc_eff),
        node_types=node_types,
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
      * ``reattach`` (remote-nodes-10) — same closure as ``resume``, but
                     the executor should short-circuit dispatch when the
                     first-incomplete node already has an unconsumed
                     ``remote_procs`` row (the VM still has the proc
                     alive or its output). Falls back to ``retry`` when
                     the row exists with ``state`` in {``spawn_failed``,
                     ``orphaned``} (kickoff §4 last bullet).
    """
    if strategy not in RECOVERY_STRATEGIES:
        raise ValueError(
            f"plan_recovery: strategy must be one of {RECOVERY_STRATEGIES}, got {strategy!r}"
        )
    plan = compute_recovery(
        workflow_yaml_path, run_id, db_path, run_dir,
        recipe=recipe, task_class=task_class, from_node=from_node,
    )

    # ``verify`` strategy override (kickoff §3). Entry = first verifier in
    # topo order (or ``--from-node``); closure = entry + descendants;
    # every upstream node must be reusable — otherwise refuse with a
    # clear message rather than letting a non-reusable LLM node silently
    # validate an outdated run. Done AFTER the strategy-agnostic compute
    # so the planner-reuse shortcut (kickoff §2) is already applied.
    if strategy == "verify":
        dag = load_dag(workflow_yaml_path)
        verify_entry = from_node
        if not verify_entry:
            for nid in dag.topo:
                if plan.node_types.get(nid) == "verifier":
                    verify_entry = nid
                    break
        if not verify_entry:
            raise RecoveryRefused(
                "no verifier-typed node found in workflow.yaml; "
                "--strategy verify requires at least one verifier",
                kind="verify_no_entry",
            )
        # ``_ancestors`` is documented to include the node itself
        # (``plan.py:148-166``); exclude the entry here — the entry is
        # precisely the verifier with no success checkpoint, so testing
        # it for reusability would refuse every ``--strategy verify``
        # invocation (kickoff §3 fix 1).
        for nid in _ancestors(dag, verify_entry) - {verify_entry}:
            if nid in plan.reuse:
                continue
            raise RecoveryRefused(
                f"{nid} has no reusable checkpoint — use --strategy resume",
                kind="verify_no_reusable_upstream",
            )
        plan.closure = dag.descendants(verify_entry)
        plan.failed_node = verify_entry
        plan.first_node = next(
            (nid for nid in dag.topo if nid in plan.closure),
            None,
        )
        # Closure subtract from reuse (kickoff §3 fix 2). Done here too
        # because the verify branch overrides ``plan.closure`` AFTER
        # ``compute_recovery`` returned.
        plan.reuse -= plan.closure

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
