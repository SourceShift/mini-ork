# Three-state resume: an unverifiable irreversible effect reconciles instead of rerunning

## Goal

`mini_ork/stores/checkpoints.py` decides reuse with a boolean: only a
`status='success'` checkpoint row whose artifact manifest re-verifies is
reusable, and **everything else means rerun** (`checkpoints.py:717-719`). For a
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
- `mini_ork/cli/publisher.py` — `publisher_node` at line 561 is the only caller
  that marks a pending effect.
- `mini_ork/recovery/planner.py` — the only consumer of `RECONCILE`.
- `tests/unit/test_node_checkpoint.py` — the three decisions and the
  fail-closed matrix.

## Expected Edit

Touch four files:

1. **`mini_ork/stores/checkpoints.py`** — add the vocabulary and the decision:

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
       RECONCILE when the node is irreversible AND its effect may have landed,
       otherwise REUSE when the existing is_node_reusable rules all hold, else
       RERUN. NEVER raises: any sqlite/IO/JSON error degrades to RERUN for a
       reversible node and RECONCILE for an irreversible one."""
   ```

   `RECONCILE` holds when the node is irreversible and any of:
   a) a pending-effect marker exists for it (below); b) its checkpoint row says
   success but the artifact manifest no longer re-verifies; c) its latest
   `node_attempts` row is a non-success end.

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

   Derive the marker path from `run_dir` only when it is a non-empty existing
   directory; otherwise each helper returns its "cannot bless this" value and
   `node_resume_decision` treats the node as having no marker.

3. **`mini_ork/cli/publisher.py`** — `publisher_node` (line 561) calls
   `mark_effect_pending(run_dir, node_id, "publish")` as late as possible,
   immediately before `_publisher_try_commit_files` (line 353) performs its
   first write, and clears it in a `try`/`finally` so success, refusal and
   exception all clear it. A path that writes nothing (`smoke_shape` publish, a
   refusal raised before any git write) marks nothing at all.

4. **`mini_ork/recovery/planner.py`** — where the planner records a node as
   `rerun`, consult
   `node_resume_decision(..., node_type=<the node's type from the run plan>)`.
   On `RECONCILE`: do not add the node to the rerun set; append a run-level
   reconciliation record naming `node_id`, the node type, the reason, and the
   marker's `started_at` when present; enqueue **one** oversight inbox row per
   run, idempotently keyed so a repeated `recover --status` does not duplicate
   it; keep the existing `reuse`/`rerun` reporting for every other node. A run
   with any reconciliation is reported as parked, not failed.

## Requirements

- One diff, four files. Do not touch `.mini-ork/`, `db/migrations/`,
  `config/risk-tiers.yaml`, or any frozen path.
- No new third-party dependency.
- Do not change `is_node_reusable`'s signature or its fail-closed default.
  Reversible nodes keep today's behaviour exactly.
- Do not rerun an irreversible node on a `RECONCILE` decision and do not report
  the run as failed. A rerun there is a duplicate effect, not a retry.
- Every new public function gets a docstring stating its failure behaviour,
  matching the module's existing "never raises / fail closed" style.
- Docstrings and comments in the surrounding style: lowercase, declarative,
  explaining *why* the third state exists rather than restating the code.

## Done When

- **AC1** A node whose type is in `IRREVERSIBLE_NODE_TYPES` and which has a
  pending-effect marker resolves to `RECONCILE`, not `RERUN`, and
  `is_node_reusable` still returns `False` for it.
- **AC2** An irreversible node whose checkpoint row says `success` but whose
  artifact manifest no longer re-verifies resolves to `RECONCILE`.
- **AC3** A reversible node in each of those two situations still resolves to
  `RERUN`, exactly as today; a verified reversible success still resolves to
  `REUSE`.
- **AC4** `node_resume_decision` never raises: a missing db, a malformed
  manifest, an unreadable marker and a sqlite error each degrade to `RERUN` for
  a reversible node and `RECONCILE` for an irreversible one.
- **AC5** `publisher_node` writes the marker immediately before its first git
  write and clears it on every exit path, including a refusal and an exception;
  a smoke-shape publish writes no marker at all.
- **AC6** The recovery planner parks a run whose plan contains a `RECONCILE`
  node, does not add that node to the rerun set, and writes exactly one
  oversight row when called twice.
- `${MINI_ORK_RUN_DIR}/framework-edit.diff` contains the proposed four-file
  patch.
- `${MINI_ORK_RUN_DIR}/verdict.json` contains
  `{ "files_changed": 4, "tests_pass": true, "static_pass": true, "pass": true }`.

## Files in scope

- `mini_ork/stores/checkpoints.py`
- `mini_ork/cli/publisher.py`
- `mini_ork/recovery/planner.py`
- `tests/unit/test_node_checkpoint.py`

## Out of scope

- Any change to `db/migrations/**`, `is_node_reusable`'s signature, or the
  `node_checkpoints` / `node_attempts` schema.
- Long-lived per-entity workflows, approval expiry, or prompt-injection work.
- Retry policy, backoff, or budget behaviour.
- Rerunning or repairing the reconciled node automatically. A person decides.

## Verification command

```bash
python3 -m pytest tests/unit/test_node_checkpoint.py -q && make lint
```
