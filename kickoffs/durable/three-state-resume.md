# Three-state resume: an unverifiable irreversible effect reconciles instead of rerunning

## Goal

`mini_ork/stores/checkpoints.py` decides reuse with a boolean: only a
`status='success'` checkpoint row whose artifact manifest re-verifies is
reusable, and **everything else means rerun** (`checkpoints.py:662-720`). For a
reversible node that is correct. For a node that has already touched the
outside world — the publisher — "rerun" is a duplicate effect: a second commit,
a second push.

The gate layer already knows this distinction. `mini_ork/gates/gate_registry.py:425-428`
states it: an unavailable check must not resolve to permission when the decision
is irreversible, which is why gates decompose into `any_fail` / `any_defer` /
`all_pass` rather than one boolean. The resume path has no equivalent third
answer, so the runtime converts "we cannot prove this effect did not land" into
"do it again".

The publisher is the hard case for two concrete reasons:

- it has **two** irreversible write paths — the artifact-contract copy plus
  commit through `_publisher_try_commit_files`, and the outputs[]
  copy-then-`git commit` loop;
- it writes **no `node_checkpoints` row at all**: `_cp` reaches its checkpoint
  branch only when `output_file` is non-empty and the publisher reports none, so
  its node end is an `append_node_attempt` row and nothing else. Every publisher
  resume today therefore reads as `no_row`.

So the ordering evidence cannot be a checkpoint row. It is an effect record with
two states, written before the effect and closed at node end.

Add the third answer. `node_resume_decision` returns `reuse`, `rerun` or
`reconcile`; `reconcile` keeps an irreversible node out of the dispatch closure
and refuses an unforced recovery instead of re-executing it. The operator's
transition out of reconcile is the `--force` override that already exists. The
record's `done` state is what makes a second recovery reuse the node instead of
re-running it.

## Scope Hint

- `mini_ork/stores/checkpoints.py` — the vocabulary, the decision function, the
  effect-record helpers.
- `mini_ork/cli/execute_handlers.py` — `_handle_publisher` (line 3010) records
  the effect as pending before it calls `publisher_node`, and refuses when it
  cannot.
- `mini_ork/cli/execute.py` — `_cp`, the node-end bookkeeping (line 5057),
  closes the record on success in **both** of its branches.
- `mini_ork/recovery/plan.py` — `_reusable_set` (line 252) is the only
  reuse/rerun classifier; `compute_recovery` / `plan_recovery` build the
  closure from it.
- `mini_ork/recovery/planner.py` — the `recover` CLI; its refusal path
  (`--force` override, line 1345) is where a parked plan is reported.
- `tests/test_node_checkpoints.py` — the existing `is_node_reusable` case file
  (cases (g)–(t)); extend it, do not add a new one.

## Expected Edit

Touch seven files:

1. **`mini_ork/stores/checkpoints.py`** — the vocabulary and the decision:

   ```python
   #: The node types that can change state outside the run directory. Verified
   #: against the recipe corpus: recipes declare ``type: publisher`` and nothing
   #: else in this class. ``rollback`` is deliberately absent — it reverts an
   #: effect rather than producing one, and it writes no effect record yet.
   IRREVERSIBLE_NODE_TYPES = frozenset({"publisher"})

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

       Ordered rules, first match wins:
       1. an irreversible node whose effect record is ``done`` → REUSE: the
          effect demonstrably completed, so re-running it duplicates it;
       2. an irreversible node whose effect record is ``pending`` → RECONCILE;
       3. an irreversible node whose effect record is ``unreadable`` →
          RECONCILE (a record it cannot read is not evidence the effect did not
          land);
       4. otherwise the existing is_node_reusable rules → REUSE;
       5. otherwise, an irreversible node whose success row exists but whose
          artifact manifest no longer re-verifies → RECONCILE;
       6. otherwise RERUN.

       Rules 1-3 run before any row lookup, because the publisher writes no
       checkpoint row at all. NEVER raises: an exception raised while
       evaluating rules 4-5 degrades to RERUN for a reversible node and to
       RECONCILE for an irreversible one; it never overrides a ``done`` record
       resolved by rule 1.
       """
   ```

   `is_node_reusable` keeps its exact signature and documented fail-closed
   contract; its body becomes
   `return node_resume_decision(...) is ResumeDecision.REUSE` with
   `node_type=""`, so a caller that passes no node type keeps today's behaviour
   byte for byte.

2. **`mini_ork/stores/checkpoints.py`** — the effect record. No new table, no
   new column, no `db/migrations/` change. One JSON file per node at
   `<run_dir>/.effects/<node_id>.json`:
   `{"effect": "publish", "state": "pending"|"done", "started_at": <epoch>,
   "ended_at": <epoch>}`.

   ```python
   def mark_effect_pending(run_dir: str, node_id: str, effect: str) -> int:
       """Record the effect as started, return 0. Non-zero when ``run_dir`` is
       missing or unwritable (never raises). The write is fsync'd and so is the
       ``.effects`` directory, because a rename/create that is not directory-
       synced can be lost by a crash — which is the case this record exists to
       survive."""

   def mark_effect_done(run_dir: str, node_id: str) -> int:
       """Flip an existing record to ``done`` with ``ended_at`` and fsync.
       Returns 0 when the record is already done or absent (a reversible node
       has nothing to flip); non-zero only on a write error. Never raises."""

   def effect_state(run_dir: str, node_id: str) -> str:
       """'pending', 'done', 'absent' or 'unreadable'. Never raises."""
   ```

   These four answers are what make the record usable: `absent` means nothing
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
   after its status validation and **before** the
   `if status != "success" or not output_file:` split:

   ```python
   if status == "success":
       mc.mark_effect_done(run_dir, node_id)   # never raises; no-op when absent
   ```

   It must sit before the split because the publisher takes the
   `append_node_attempt` branch and never reaches the checkpoint branch — that
   is exactly why the record, not the checkpoint row, carries the ordering
   evidence here. On every other outcome (failure, error, skipped) leave the
   record `pending`.

5. **`mini_ork/recovery/plan.py`** — `_reusable_set(dag, db_path, run_id,
   run_dir, *, recipe, task_class, node_types)` (line 252) already receives the
   per-node type map. Return a third value, `(reuse, reason, reconcile)`, and
   keep the existing reason strings exactly (`"no_row"`, `"status=<s>"`,
   `"hash_mismatch_or_artifact_corrupt"`, `"plan_json"`) so `--status` output
   does not change for reversible nodes.

   For each node, in this order:

   - `node_resume_decision(db_path, run_id, nid, current_input_hash=...,
     current_recipe_version=..., current_config_hash=..., run_dir=run_dir,
     node_type=types.get(nid, ""))` — call it **first**, before the
     `row_status is None` branch, because the publisher has no row;
   - `REUSE` → `reuse.add(nid)`, reason `""` (or `"effect_done"` when the node
     is irreversible; either is fine as long as it is stable);
   - `RECONCILE` → with `force=False`, `reconcile.add(nid)`, reason
     `"effect_may_have_landed"`; with `force=True`, treat it as `RERUN` so the
     node lands in the closure. `--force` has to actually re-dispatch the node:
     a refusal the operator can override but not act on is a dead end;
   - `RERUN` → fall through to today's handling: the `plan_json` / `no_row`
     branch, then the `status=<s>` branch, then
     `hash_mismatch_or_artifact_corrupt`.

   That ordering keeps every existing reason string intact, because a reversible
   node's `RERUN` falls straight into today's code.

   Add `parked_nodes: tuple[str, ...] = ()` to `RecoveryPlan`. `compute_recovery`
   (line 576) is the **read-only** classifier behind `recover --status`: it fills
   `parked_nodes` from the reconcile set, excludes those nodes from the closure,
   and writes nothing — no record change, no sidecar. `plan_recovery` (line 751)
   keeps returning a `RecoveryPlan` and carries `parked_nodes` through
   unchanged; it writes nothing either.

6. **`mini_ork/recovery/planner.py`** — the `recover` CLI. `--force` is already
   parsed at line 1149. Add the parked case in `main` (line 1065) on the path
   that turns a plan into a dispatch — not inside the retry-hint refusal block
   at 1345, which runs only for a `retryable: false` hint. When
   `plan.parked_nodes` is non-empty and `--force` is **not** set, pass
   `force=False` to the planner, print one line per parked node
   (`refusing: <node_id> is an irreversible effect that may already have
   landed; inspect <run_dir>/.effects/<node_id>.json`) and return the existing
   refusal code. With `--force`, dispatch the closure as today — the operator
   has taken responsibility, and the `pending` record stays in place so the run
   can still be re-derived afterwards.

   That is the whole transition out of reconcile: a person reads the record,
   decides, and either forces the retry or leaves the run parked. No new inbox
   row, no new exit code, and no automatic unpark.

## Requirements

- One diff, seven files (six existing, one new test file). Do not touch `.mini-ork/`, `db/migrations/`,
  `config/risk-tiers.yaml`, or any frozen path.
- No new third-party dependency.
- Do not change `is_node_reusable`'s signature or its fail-closed default.
  Reversible nodes keep today's behaviour exactly.
- Do not rerun an irreversible node on a `RECONCILE` decision without `--force`,
  and do not report the run as failed. A rerun there is a duplicate effect, not
  a retry.
- `IRREVERSIBLE_NODE_TYPES` is a documented frozenset in the store module, not a
  new registry. A registry is scope creep; the constant is the whole policy.
- `compute_recovery` and `plan_recovery` stay side-effect free.
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
- **AC4** `node_resume_decision` never raises. An exception raised while
  evaluating a rule degrades to `RERUN` for a reversible node and `RECONCILE` for
  an irreversible one, and never overrides an already-resolved `done` record: a
  missing db, a malformed manifest, an `unreadable` record and a sqlite error are
  each tested for both node types.
- **AC5** `_handle_publisher` records the effect before `publisher_node` runs and
  returns non-zero without calling it when the record cannot be written; a
  failing or raising publisher leaves the record `pending`; `_cp` flips it to
  `done` on success in **both** branches (including the `append_node_attempt`
  branch the publisher actually takes) and leaves it `pending` on every
  non-success outcome.
- **AC6** `_reusable_set` puts a reconcile node in neither the reuse set nor the
  rerun set, including when that node has no checkpoint row;
  `compute_recovery` and `plan_recovery` write nothing;
  `recover` refuses an unforced recovery whose plan has `parked_nodes`, and
  with `--force` builds a plan whose closure **contains** those nodes.
- `${MINI_ORK_RUN_DIR}/framework-edit.diff` contains the proposed seven-file
  patch.
- `${MINI_ORK_RUN_DIR}/verdict.json` contains
  `{ "files_changed": 7, "tests_pass": true, "static_pass": true, "pass": true }`.

## Files in scope

- `mini_ork/stores/checkpoints.py`
- `mini_ork/cli/execute_handlers.py`
- `mini_ork/cli/execute.py`
- `mini_ork/recovery/plan.py`
- `mini_ork/recovery/planner.py`
- `tests/test_node_checkpoints.py`
- `tests/unit/test_publisher_effect_record.py` (new)

## Out of scope

- Any change to `db/migrations/**`, `is_node_reusable`'s signature, or the
  `node_checkpoints` / `node_attempts` schema.
- Long-lived per-entity workflows, approval expiry, or prompt-injection work.
- Retry policy, backoff, budget behaviour, or a new oversight/inbox row.
- Rerunning or repairing the reconciled node automatically. A person decides.

## Verification command

```bash
python3 -m pytest tests/test_node_checkpoints.py tests/unit/test_publisher_effect_record.py -q && make lint
```
