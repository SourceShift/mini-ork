# Three-state resume: an unverifiable irreversible effect reconciles instead of rerunning

## Goal

`mini_ork/stores/checkpoints.py` decides reuse with a boolean: only a
`status='success'` checkpoint row whose artifact manifest re-verifies is
reusable, and **everything else means rerun** (`checkpoints.py:662-720`). For a
reversible node that is correct. For a node that has already touched the
outside world — the publisher — "rerun" is a duplicate effect: a second commit,
a second push, a second `migrate_apply`.

The gate layer already knows this distinction. `mini_ork/gates/gate_registry.py:425-428`
states it: an unavailable check must not resolve to permission when the decision
is irreversible, which is why gates decompose into `any_fail` / `any_defer` /
`all_pass` rather than one boolean. The resume path has no equivalent third
answer, so the runtime converts "we cannot prove this effect did not land" into
"do it again".

Add the third answer. `node_resume_decision` returns `reuse`, `rerun` or
`reconcile`; a `reconcile` decision parks the run for a person instead of
re-executing an irreversible node. `is_node_reusable` keeps its signature and
becomes `decision is reuse`, so no existing caller changes.

This is the same defect the durable-execution literature names for replay: a
step whose acknowledgement was lost is not a failed step, and collapsing the two
produces the duplicate write the retry was supposed to make safe.

## Scope Hint

- `mini_ork/stores/checkpoints.py` — the vocabulary, the decision function, the
  marker helpers.
- `mini_ork/cli/execute_handlers.py` — `_handle_publisher` (line 3010) marks the
  effect pending before it calls `publisher_node`.
- `mini_ork/cli/execute.py` — `_cp`, the node-end bookkeeping (line 5058),
  clears the marker only once a success checkpoint is durable.
- `mini_ork/recovery/plan.py` — `_reusable_set` (line 252) is the only
  reuse/rerun classifier; it grows a reconcile outcome.
- `tests/test_node_checkpoints.py` — the existing `is_node_reusable` case
  file (cases (g)–(t)); extend it, do not add a new one.

## Expected Edit

Touch five files:

1. **`mini_ork/stores/checkpoints.py`** — the vocabulary and the decision:

   ```python
   IRREVERSIBLE_NODE_TYPES = frozenset({
       "publisher", "publish", "merge", "apply", "migrate", "migrate_apply",
   })

   def effect_class_for_node_type(node_type: str) -> str:
       """'irreversible' for a node type that can change state outside the run
       directory, else 'reversible'. Unknown and empty types are reversible."""

   class ResumeDecision(str, Enum):
       REUSE = "reuse"
       RERUN = "rerun"
       RECONCILE = "reconcile"

   def node_resume_decision(db, run_id, node_id, *, current_input_hash,
                            current_recipe_version, current_config_hash,
                            run_dir="", node_type="") -> ResumeDecision:
       """Why a prior attempt at this node can or cannot be reused.
       RECONCILE when the node is irreversible AND its effect may have landed;
       otherwise REUSE when the existing is_node_reusable rules all hold, else
       RERUN. NEVER raises: any sqlite/IO/JSON error degrades to RERUN for a
       reversible node and RECONCILE for an irreversible one."""
   ```

   `RECONCILE` holds exactly when `effect_class_for_node_type(node_type) ==
   "irreversible"` **and** one of:
   a) `effect_pending(run_dir, node_id)` is true;
   b) a `status='success'` row exists but the artifact manifest no longer
      re-verifies (the existing artifact check fails).

   Do **not** add "any non-success attempt end" as a third condition: a
   publisher that failed before writing anything has no marker, and rerunning
   it is safe and desirable. The marker, not the attempt status, is the signal.

   `is_node_reusable` keeps its exact signature and documented fail-closed
   contract; its body becomes
   `return node_resume_decision(...) is ResumeDecision.REUSE` with
   `node_type=""`, so a caller that passes no node type keeps today's behaviour
   byte for byte.

2. **`mini_ork/stores/checkpoints.py`** — the pending-effect marker. No new
   table, no new column, no `db/migrations/` change:

   ```python
   def mark_effect_pending(run_dir: str, node_id: str, effect: str) -> int:
       """Write <run_dir>/.effects/<node_id>.json {effect, started_at} and fsync
       it. rc=0 written, non-zero otherwise (never raises)."""

   def clear_effect_pending(run_dir: str, node_id: str) -> int:
       """Remove the marker; rc=0 when absent or removed. Never raises."""

   def effect_pending(run_dir: str, node_id: str) -> bool:
       """True when a marker file exists for this node."""
   ```

   When `run_dir` is empty or not an existing directory, every helper returns
   its conservative value: `mark_effect_pending` returns non-zero and
   `effect_pending` returns False, so a run without a run directory behaves
   exactly as today rather than pretending an effect is pending.

3. **`mini_ork/cli/execute_handlers.py`** — `_handle_publisher(ctx)` calls
   `mark_effect_pending(ctx.run_dir_eff, ctx.node_id, "publish")` immediately
   before `publisher_node(...)`. `publisher_node` has **two** irreversible write
   paths — the artifact-contract copy plus commit through
   `_publisher_try_commit_files` (publisher.py:353, called at 770), and the
   outputs[] copy-then-`git commit` loop (publisher.py:889-895). Both are
   downstream of that one call, so one marker before it covers both. Do not
   mark inside `publisher_node`: it has no `node_id` parameter, and the marker
   must outlive its return (see item 4).

4. **`mini_ork/cli/execute.py`** — `_cp(node_id, status, node_type, ...)` at
   line 5058 is the single node-end bookkeeping path. Call
   `clear_effect_pending(run_dir, node_id)` **only** in the branch where
   `status == "success"` and `ckpt_rc == 0`, right beside `_node_git_ckpt(...)`.
   On every other outcome — failure, error, skipped, or a success whose
   checkpoint write returned non-zero — leave the marker in place. That ordering
   is the whole point: the marker must die only once the effect *and* the row
   that records it are both durable. Clearing it earlier reopens the window the
   change exists to close (commit landed, process died before the checkpoint,
   next resume sees no marker and duplicates the commit).

5. **`mini_ork/recovery/plan.py`** — `_reusable_set(dag, db_path, run_id,
   run_dir, *, recipe, task_class, node_types)` (line 252) already receives the
   per-node type map. Change it to return a third value, `(reuse, reason,
   reconcile)`:

   - for each node with a row, compute
     `dec = node_resume_decision(db_path, run_id, nid, current_input_hash=...,
     current_recipe_version=..., current_config_hash=..., run_dir=run_dir,
     node_type=types.get(nid, ""))`;
   - `REUSE` → `reuse.add(nid)`, reason `""` (today's behaviour);
   - `RECONCILE` → `reconcile.add(nid)`, reason `"effect_may_have_landed"`;
   - `RERUN` → today's `hash_mismatch_or_artifact_corrupt` reason.

   Update `compute_recovery` (line 576) and `plan_recovery` (line 751): a
   reconcile node is **excluded from the dispatch closure and from the rerun
   set**, recorded in the plan, and reported as parked rather than failed. Then
   enqueue exactly one oversight row:
   `oversight_inbox.enqueue(gate_id=f"resume-reconcile:{run_id}",
   feature="resume-reconcile", phase="recover", context={...}, db_path=db_path)`.
   `enqueue` is a plain INSERT with no idempotency, so guard it: `SELECT 1 FROM
   mo_inbox_gates WHERE gate_id=? AND status='pending'` first and skip the
   insert when a row already exists. That guard is what makes a second
   `recover --status` a no-op.

## Requirements

- One diff, five files. Do not touch `.mini-ork/`, `db/migrations/`,
  `config/risk-tiers.yaml`, or any frozen path.
- No new third-party dependency.
- Do not change `is_node_reusable`'s signature or its fail-closed default.
  Reversible nodes keep today's behaviour exactly.
- Do not rerun an irreversible node on a `RECONCILE` decision and do not report
  the run as failed. A rerun there is a duplicate effect, not a retry.
- `IRREVERSIBLE_NODE_TYPES` is a documented frozenset in the store module, not
  a new registry. A registry is scope creep; the constant is the whole policy.
- Every new public function gets a docstring stating its failure behaviour,
  matching the module's existing "never raises / fail closed" style.
- Docstrings and comments in the surrounding style: lowercase, declarative,
  explaining *why* the third state exists rather than restating the code.

## Done When

- **AC1** A node whose type is in `IRREVERSIBLE_NODE_TYPES` with a
  pending-effect marker present resolves to `RECONCILE`, not `RERUN`, and
  `is_node_reusable` still returns `False` for it.
- **AC2** An irreversible node with a `status='success'` row whose artifact
  manifest no longer re-verifies resolves to `RECONCILE`.
- **AC3** A reversible node in each of those two situations still resolves to
  `RERUN`, exactly as today; a verified reversible success still resolves to
  `REUSE`.
- **AC4** `node_resume_decision` never raises: a missing db, a malformed
  manifest, an unreadable marker and a sqlite error each degrade to `RERUN` for
  a reversible node and `RECONCILE` for an irreversible one.
- **AC5** `_handle_publisher` marks before `publisher_node` runs; the marker
  survives a failing or raising publisher; and `_cp` clears it only after a
  success checkpoint commits, leaving it in place on every non-success outcome.
- **AC6** `_reusable_set` returns a reconcile node in neither the reuse set nor
  the rerun set, `compute_recovery` parks the run and writes exactly one
  oversight row, and a second call writes none.
- `${MINI_ORK_RUN_DIR}/framework-edit.diff` contains the proposed five-file
  patch.
- `${MINI_ORK_RUN_DIR}/verdict.json` contains
  `{ "files_changed": 5, "tests_pass": true, "static_pass": true, "pass": true }`.

## Files in scope

- `mini_ork/stores/checkpoints.py`
- `mini_ork/cli/execute_handlers.py`
- `mini_ork/cli/execute.py`
- `mini_ork/recovery/plan.py`
- `tests/test_node_checkpoints.py`

## Out of scope

- Any change to `db/migrations/**`, `is_node_reusable`'s signature, or the
  `node_checkpoints` / `node_attempts` schema.
- Long-lived per-entity workflows, approval expiry, or prompt-injection work.
- Retry policy, backoff, or budget behaviour.
- Rerunning or repairing the reconciled node automatically. A person decides.

## Verification command

```bash
python3 -m pytest tests/test_node_checkpoints.py -q && make lint
```
