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

Two facts make the publisher the hard case:

- it has **two** irreversible write paths (the artifact-contract copy plus commit
  through `_publisher_try_commit_files`, and the outputs[] copy-then-`git commit`
  loop), and
- it writes **no `node_checkpoints` row** — `_cp` reaches its checkpoint branch
  only when `output_file` is non-empty, and the publisher reports none, so its
  node end is an `append_node_attempt` row and nothing else. Every publisher
  resume today therefore reads as `no_row`.

So the ordering evidence cannot be a checkpoint row. It is an effect record with
two states, written before the effect and closed when the node end says success.

Add the third answer. `node_resume_decision` returns `reuse`, `rerun` or
`reconcile`; `reconcile` parks the run for a person instead of re-executing an
irreversible node. `is_node_reusable` keeps its signature and becomes
`decision is reuse`, so no existing caller changes.

## Scope Hint

- `mini_ork/stores/checkpoints.py` — the vocabulary, the decision function, the
  effect-record helpers.
- `mini_ork/cli/execute_handlers.py` — `_handle_publisher` (line 3010) records
  the effect as pending before it calls `publisher_node`, and refuses when it
  cannot.
- `mini_ork/cli/execute.py` — `_cp`, the node-end bookkeeping (line 5057),
  closes the record on success in **both** of its branches.
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
       1. an irreversible node whose effect record is ``done`` → REUSE: the
          effect demonstrably completed, so re-running it duplicates it;
       2. an irreversible node whose effect record is ``pending`` → REUSE when
          the operator approved the node's ``node:`` reconciliation row
          (a person confirmed the effect landed), RERUN when they rejected it,
          else RECONCILE;
       3. an irreversible node whose effect record cannot be read → RECONCILE
          (a record it cannot read is not evidence the effect did not land);
       4. otherwise the existing is_node_reusable rules → REUSE;
       5. otherwise, an irreversible node whose success row exists but whose
          artifact manifest no longer re-verifies → RECONCILE;
       6. otherwise RERUN.

       Checked before any row lookup, because the publisher writes no
       checkpoint row at all. NEVER raises: any sqlite/IO/JSON error degrades
       to RERUN for a reversible node and to RECONCILE for an irreversible one.
       """
   ```

   `is_node_reusable` keeps its exact signature and documented fail-closed
   contract; its body becomes
   `return node_resume_decision(...) is ResumeDecision.REUSE` with
   `node_type=""`, so a caller that passes no node type keeps today's behaviour
   byte for byte.

2. **`mini_ork/stores/checkpoints.py`** — the effect record. No new table, no
   new column, no `db/migrations/` change. One JSON file per node under
   `<run_dir>/.effects/<node_id>.json`:
   `{"effect": "publish", "state": "pending"|"done", "started_at": <epoch>,
   "ended_at": <epoch>}`.

   ```python
   def mark_effect_pending(run_dir: str, node_id: str, effect: str) -> int:
       """Record the effect as started, fsync it, and return 0. Non-zero when
       ``run_dir`` is missing/unwritable (never raises)."""

   def mark_effect_done(run_dir: str, node_id: str) -> int:
       """Flip an existing record to ``done`` (adding ``ended_at``) and fsync.
       Returns 0 when the record is already done or absent; non-zero only on a
       write error. Never raises."""

   def clear_effect_record(run_dir: str, node_id: str) -> int:
       """Delete the record; 0 when absent or removed. Never raises."""

   def effect_state(run_dir: str, node_id: str) -> str:
       """'pending', 'done', 'absent' or 'unreadable'. Never raises."""
   ```

   These three answers are what make the record usable: `absent` means nothing
   was ever started, `done` means the effect completed, and `unreadable` is the
   one the decision must not treat as `absent`.

3. **`mini_ork/cli/execute_handlers.py`** — `_handle_publisher(ctx)` calls
   `mark_effect_pending(ctx.run_dir_eff, ctx.node_id, "publish")` immediately
   before `publisher_node(...)`, and **returns non-zero with finish_reason
   `"effect_marker_failed"` without calling it when that returns non-zero**. An
   effect that cannot be recorded is an effect that is not performed. Both
   publisher write paths are downstream of that one call, so one record covers
   both. Do not record inside `publisher_node`: it has no `node_id` parameter.

4. **`mini_ork/cli/execute.py`** — `_cp(node_id, status, node_type, output_file,
   finish_reason)` at line 5057 is the single node-end bookkeeping path. Right
   after its status validation, before the branch that decides whether a
   checkpoint row can be written:

   ```python
   if status == "success":
       mc.mark_effect_done(run_dir, node_id)   # never raises; no-op when absent
   ```

   This must sit **before** the `if status != "success" or not output_file:`
   split, because the publisher takes the `append_node_attempt` branch and never
   reaches the checkpoint branch — that is exactly why the record, not the
   checkpoint row, carries the ordering evidence here. On every other outcome
   (failure, error, skipped) leave the record `pending`. Calling
   `mark_effect_done` unconditionally on a reversible node is a no-op: there is
   no record to flip.

5. **`mini_ork/recovery/plan.py`** — `_reusable_set(dag, db_path, run_id,
   run_dir, *, recipe, task_class, node_types)` (line 252) already receives the
   per-node type map. Return a third value, `(reuse, reason, reconcile)`, and
   keep the existing reason strings exactly (`"no_row"`, `"status=<s>"`,
   `"hash_mismatch_or_artifact_corrupt"`, `"plan_json"`) so `--status` output
   does not change for reversible nodes. Add only `"effect_may_have_landed"`.

   For each node, in this order:

   - if `effect_class_for_node_type(types.get(nid, "")) == "irreversible"` and
     `effect_state(run_dir, nid)` in `("pending", "unreadable")` →
     `node_resume_decision` decides between `RECONCILE` and `RERUN` (the
     approved/rejected reconciliation row is the difference). `RECONCILE` →
     `reconcile.add(nid)`, reason `"effect_may_have_landed"`, `continue`; the
     other → fall through to today's handling. This branch must come **before**
     the `row_status is None` branch — the publisher has no row at all;
   - if the effect record is `done` → `reuse.add(nid)`, reason `""`;
   - then today's `row_status is None` handling (`plan_json` / `no_row`);
   - then today's non-success handling (`status=<s>`);
   - then `node_resume_decision(db_path, run_id, nid,
     current_input_hash=..., current_recipe_version=...,
     current_config_hash=..., run_dir=run_dir, node_type=types.get(nid, ""))`:
     `REUSE` → reuse set, `RECONCILE` → reconcile set, `RERUN` → today's
     `hash_mismatch_or_artifact_corrupt`.

   `compute_recovery` (line 576) is the **read-only** classifier behind
   `recover --status`: it reports the reconcile set in the formatted status and
   writes nothing — no inbox row, no record change.

   `plan_recovery` (line 751) returns a `RecoveryPlan`; keep that contract. Add
   a `parked_nodes: tuple[str, ...]` field to the plan (empty by default) and
   fill it from the reconcile set. On a non-empty park set, `plan_recovery`
   must not add those nodes to the closure and must park the run through the
   mechanism that already exists for a node review (`execute_handlers.py:956-985`):
   - write `<run_dir>/.review-pause` as JSON
     `{waiting_node: <first parked node>, awaiting: [<parked nodes>],
     inbox_ids: [...], ts: <epoch>}`;
   - enqueue one row per parked node with **`gate_id =
     f"node:{run_id}:{nid}:reconcile-{started_at}"`**, `feature="resume-reconcile"`,
     `phase="recover"`, `context={"run_id":..., "node_id": nid,
     "reason": "effect_may_have_landed", "effect": <effect name>}`. The `node:`
     prefix is what makes the row resolvable by
     `mini-ork approvals approve <run_id> --node <nid>`, which is the documented
     unpark path.

   Enqueue atomically, not with a preceding SELECT: one statement of the form
   `INSERT INTO mo_inbox_gates (...) SELECT ?,?,?,?,... WHERE NOT EXISTS
   (SELECT 1 FROM mo_inbox_gates WHERE gate_id=? AND status='pending')`. A
   separate check-then-insert races with another recovery pass and re-enqueues.

   The CLI layer maps a plan whose `parked_nodes` is non-empty to
   `node_review.REVIEW_PAUSE_RC` with finish_reason `"paused_for_review"` — the
   same exit code the executor already uses to stop dispatching and end a run in
   `reviewing`. Do not invent a new status.

   The transition out of reconcile is the existing approval, read on the next
   pass: an **approved** row means the operator confirmed the effect landed →
   the decision returns `REUSE`; a **rejected** row means it did not land →
   `RERUN`, and `plan_recovery` calls `clear_effect_record` before dispatching so
   the same node is not parked again. There is no automatic unpark.

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

- **AC1** An irreversible node whose effect record is `pending` resolves to
  `RECONCILE` both when a checkpoint row exists and when none exists (the
  publisher's real shape); one whose record is `done` resolves to `REUSE`.
- **AC2** On the decision function alone — a synthetic `status='success'` row
  whose artifact manifest no longer re-verifies, `node_type="publisher"` — the
  answer is `RECONCILE`. This is a unit test of `node_resume_decision`; the
  publisher does not need to produce such a row for the case to be covered.
- **AC3** A reversible node in each of those situations still resolves to
  `RERUN`, exactly as today; a verified reversible success still resolves to
  `REUSE`; `is_node_reusable` returns the same booleans it returns today for
  every existing case (g)–(t).
- **AC4** `node_resume_decision` never raises, and the fail-closed matrix holds:
  a missing db, a malformed manifest, an `unreadable` record and a sqlite error
  each degrade to `RERUN` for a reversible node and `RECONCILE` for an
  irreversible one.
- **AC5** `_handle_publisher` records the effect before `publisher_node` runs and
  returns non-zero without calling it when the record cannot be written; a
  failing or raising publisher leaves the record `pending`; `_cp` flips it to
  `done` on success in **both** branches (including the
  `append_node_attempt` branch the publisher actually takes) and leaves it
  `pending` on every non-success outcome.
- **AC6** `_reusable_set` puts a reconcile node in neither the reuse set nor the
  rerun set, including when that node has no checkpoint row; `compute_recovery`
  writes nothing; `plan_recovery` writes the `.review-pause` sidecar, sets
  `parked_nodes`, and enqueues exactly one `node:`-prefixed row per parked node —
  a second call enqueues none. An approved row on the next pass yields `REUSE`;
  a rejected one yields `RERUN` with the record cleared.
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
