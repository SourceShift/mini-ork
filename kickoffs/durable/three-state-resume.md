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

The window that matters most is the one with **no checkpoint row at all**: the
publisher commits, then the process dies before `_cp` records the node. Today
that node reads as `no_row` → rerun → a duplicate commit. A marker written
before the effect and removed only after the success row is durable is what
distinguishes that case from a node that never ran.

Add the third answer. `node_resume_decision` returns `reuse`, `rerun` or
`reconcile`; a `reconcile` decision parks the run for a person instead of
re-executing an irreversible node. `is_node_reusable` keeps its signature and
becomes `decision is reuse`, so no existing caller changes.

## Scope Hint

- `mini_ork/stores/checkpoints.py` — the vocabulary, the decision function, the
  marker helpers.
- `mini_ork/cli/execute_handlers.py` — `_handle_publisher` (line 3010) marks the
  effect pending before it calls `publisher_node`, and aborts if it cannot.
- `mini_ork/cli/execute.py` — `_cp`, the node-end bookkeeping (line 5058),
  clears the marker only once a success checkpoint is durable.
- `mini_ork/recovery/plan.py` — `_reusable_set` (line 252) is the only
  reuse/rerun classifier; it grows a reconcile outcome.
- `tests/test_node_checkpoints.py` — the existing `is_node_reusable` case file
  (cases (g)–(t)); extend it, do not add a new one.

## Expected Edit

Touch five files:

1. **`mini_ork/stores/checkpoints.py`** — the vocabulary and the decision:

   ```python
   #: The node types that can change state outside the run directory. Verified
   #: against the recipe corpus: recipes declare ``type: publisher`` and
   #: ``type: rollback`` and nothing else in this class.
   IRREVERSIBLE_NODE_TYPES = frozenset({"publisher", "rollback"})

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

       Evaluated in this order:
       1. an irreversible node with a pending-effect marker → RECONCILE,
          BEFORE any row is looked up, because the marker exists precisely
          for the case where no row was ever written;
       2. otherwise the existing is_node_reusable rules → REUSE;
       3. otherwise, an irreversible node whose success row exists but whose
          artifact manifest no longer re-verifies → RECONCILE;
       4. otherwise RERUN.

       NEVER raises. Any sqlite/IO/JSON error degrades to RERUN for a
       reversible node and to RECONCILE for an irreversible one: a db it
       cannot read is not evidence that the effect did not land."""
   ```

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

   A missing or unwritable `run_dir` is an error, not a default:
   `mark_effect_pending` returns non-zero and `effect_pending` returns False. The
   caller treats a non-zero rc as a refusal (item 3), so the pair fails closed:
   an effect that cannot be recorded is an effect that is not performed.

3. **`mini_ork/cli/execute_handlers.py`** — `_handle_publisher(ctx)` calls
   `mark_effect_pending(ctx.run_dir_eff, ctx.node_id, "publish")` immediately
   before `publisher_node(...)`, and **returns non-zero with finish_reason
   `"effect_marker_failed"` when that returns non-zero**. `publisher_node` has
   two irreversible write paths — the artifact-contract copy plus commit through
   `_publisher_try_commit_files` (publisher.py:353, called at 770), and the
   outputs[] copy-then-`git commit` loop (publisher.py:889-895). Both are
   downstream of that one call, so one marker before it covers both. Do not mark
   inside `publisher_node`: it has no `node_id` parameter, and the marker must
   outlive its return (item 4).

4. **`mini_ork/cli/execute.py`** — `_cp(node_id, status, node_type, ...)` at
   line 5058 is the single node-end bookkeeping path. Call
   `clear_effect_pending(run_dir, node_id)` **only** in the branch where
   `status == "success"` and `ckpt_rc == 0`, right beside `_node_git_ckpt(...)`.
   On every other outcome — failure, error, skipped, or a success whose
   checkpoint write returned non-zero — leave the marker in place. That ordering
   is the point: the marker dies only once the effect *and* the row recording it
   are both durable. Clearing it earlier reopens the window this change exists to
   close.

5. **`mini_ork/recovery/plan.py`** — `_reusable_set(dag, db_path, run_id,
   run_dir, *, recipe, task_class, node_types)` (line 252) already receives the
   per-node type map. Return a third value, `(reuse, reason, reconcile)`, and
   keep the existing reason strings exactly (`"no_row"`, `"status=<s>"`,
   `"hash_mismatch_or_artifact_corrupt"`, `"plan_json"`) so `--status` output
   does not change for reversible nodes. Add only `"effect_may_have_landed"`.

   For each node, in this order:

   - if `effect_class_for_node_type(types.get(nid, "")) == "irreversible"` and
     `effect_pending(run_dir, nid)` → `reconcile.add(nid)`,
     reason `"effect_may_have_landed"`, `continue`. This branch must come
     **before** the `row_status is None` branch — the missing row is the case
     the marker exists for;
   - then today's `row_status is None` handling (`plan_json` / `no_row`);
   - then today's non-success handling (`status=<s>`);
   - then `node_resume_decision(...)` with
     `node_type=types.get(nid, "")`: `REUSE` → reuse set, `RECONCILE` →
     reconcile set, `RERUN` → today's `hash_mismatch_or_artifact_corrupt`.

   `compute_recovery` (line 576) is the **read-only** classifier behind
   `recover --status`: it reports the reconcile set in the formatted status and
   must write nothing — no inbox row, no sidecar.

   `plan_recovery` (line 751) is the mutating path. When its reconcile set is
   non-empty it must not dispatch, must not add those nodes to the closure, and
   must park the run through the mechanism that already exists for a node review
   — `execute_handlers.py:956-985`:
   - write `<run_dir>/.review-pause` as JSON with
     `{waiting_node: <first reconcile node>, awaiting: [<reconcile nodes>],
     inbox_ids: [...], ts: <epoch>}`;
   - enqueue one row per reconcile node through
     `oversight_inbox.enqueue(gate_id=f"resume-reconcile:{run_id}:{nid}",
     feature="resume-reconcile", phase="recover", context={"run_id": ...,
     "node_id": nid, "reason": "effect_may_have_landed"}, db_path=db_path)`;
   - return the run's existing paused-for-review outcome
     (`node_review.REVIEW_PAUSE_RC`, `"paused_for_review"`), not an error rc.

   `enqueue` is a plain INSERT with no idempotency, so guard each one:
   `SELECT 1 FROM mo_inbox_gates WHERE gate_id=? AND status='pending'` first and
   skip the insert when a row already exists. That guard is what makes a second
   `plan_recovery` call a no-op, and it makes the row resolvable by
   `mini-ork approvals approve <run_id> --node <nid>`, which is the documented
   unpark path.

## Requirements

- One diff, five files. Do not touch `.mini-ork/`, `db/migrations/`,
  `config/risk-tiers.yaml`, or any frozen path.
- No new third-party dependency.
- Do not change `is_node_reusable`'s signature or its fail-closed default.
  Reversible nodes keep today's behaviour exactly.
- Do not rerun an irreversible node on a `RECONCILE` decision and do not report
  the run as failed. A rerun there is a duplicate effect, not a retry.
- `IRREVERSIBLE_NODE_TYPES` is a documented frozenset in the store module, not a
  new registry. A registry is scope creep; the constant is the whole policy.
- `compute_recovery` stays side-effect free. Every write in item 5 belongs to
  `plan_recovery`.
- Every new public function gets a docstring stating its failure behaviour,
  matching the module's existing "never raises / fail closed" style.
- Docstrings and comments in the surrounding style: lowercase, declarative,
  explaining *why* the third state exists rather than restating the code.

## Done When

- **AC1** An irreversible node with a pending-effect marker resolves to
  `RECONCILE` **both when a checkpoint row exists and when none exists**, and
  `is_node_reusable` still returns `False` for it.
- **AC2** On the decision function alone (a synthetic `status='success'` row
  whose artifact manifest no longer re-verifies, `node_type="publisher"`), the
  answer is `RECONCILE`. This is a unit test of `node_resume_decision`; the
  publisher itself does not need to produce such a row for the case to be
  covered.
- **AC3** A reversible node in each of those situations still resolves to
  `RERUN`, exactly as today; a verified reversible success still resolves to
  `REUSE`; `is_node_reusable` returns the same booleans it returns today for
  every existing case (g)–(t).
- **AC4** `node_resume_decision` never raises: a missing db, a malformed
  manifest, an unreadable marker and a sqlite error each degrade to `RERUN` for
  a reversible node and `RECONCILE` for an irreversible one.
- **AC5** `_handle_publisher` marks before `publisher_node` runs and returns
  non-zero without calling it when the mark fails; the marker survives a failing
  or raising publisher; `_cp` clears it only after a success checkpoint commits
  and leaves it in place on every non-success outcome.
- **AC6** `_reusable_set` puts a reconcile node in neither the reuse set nor
  the rerun set, including when that node has no checkpoint row;
  `compute_recovery` writes nothing; `plan_recovery` writes the
  `.review-pause` sidecar, returns `REVIEW_PAUSE_RC`, and writes exactly one
  oversight row — a second call writes none.
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
