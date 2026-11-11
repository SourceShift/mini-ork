# Three-state resume: the operator can see a parked node, and the docs stop lying

## Goal

Three-state resume shipped in `4464d591`. Three recorded gaps remain, all
operator-facing, none a behaviour change to the decision itself:

1. **`recover --status` cannot show a parked node.** `_reusable_set` puts a
   reconcile node in `RecoveryPlan.parked_nodes`, and `recover` refuses an
   unforced plan that has one (`mini_ork/recovery/planner.py:1430`). But
   `format_status` (`mini_ork/recovery/plan.py:943`) prints `reuse`, `rerun`,
   cost boundary, entry and sku — and never the parked set. So the read-only
   command the operator runs *first* reports a plan that looks dispatchable,
   and the dispatch then refuses with a node the status never mentioned.
   `parked_nodes` is already in `to_dict()`; it is only the human-readable
   print that is missing.

2. **`_fsync_dir`'s docstring claims a tolerance the code does not give.**
   `mini_ork/stores/checkpoints.py:812` says "Best-effort: a platform that
   cannot open a directory for fsync has already given us the file-level
   fsync, so the caller does not fail on it." Only the `os.open` branch
   returns; an `OSError` from `os.fsync` propagates out of the helper and is
   caught by the caller's `except OSError`, which makes `mark_effect_pending`
   return 1 — correctly, because an effect that cannot be durably recorded is
   an effect that must not run. The behaviour is right; the docstring
   describes the opposite for the second branch. State what the code does,
   including why the asymmetry is deliberate.

3. **`recover --help` does not say what `--force` does to a parked node.**
   `mini_ork/recovery/planner.py:203` documents `--force` only as
   "Override a `retryable: false` retry hint and dispatch anyway". Since
   `4464d591` it has a second meaning: re-dispatch an irreversible node that
   a prior run parked for reconciliation. An operator who reads only `--help`
   has no way to learn the transition out of reconcile exists.

Fix all three. Do not change any decision, any reason string, or any exit
code.

## Scope Hint

- `mini_ork/recovery/plan.py` — `format_status` (line 943). Add a `parked`
  section listing `plan.parked_nodes` in sorted order, each with the effect
  record path relative to the run directory, so the operator knows what to
  read before choosing.
- `mini_ork/stores/checkpoints.py` — `_fsync_dir` docstring (line 812) only.
  No code change: the current propagation is the correct fail-closed
  behaviour.
- `mini_ork/recovery/planner.py` — the `--force` entry in the help text
  (line 203) only. Do not touch the refusal block at line 1430; it already
  names the record path and is correct.
- `tests/unit/test_publisher_effect_record.py` — pin the new print and the
  absence of a behaviour change.

## Expected Edit

### 1. `format_status` prints the parked set

Insert a section between the `rerun` block and the `cost boundary` block, in
the same shape as the two blocks above it:

```
    parked (1 node):
      [parked] publish  (.effects/publish.json — irreversible effect, may have landed)
      (none)            # when plan.parked_nodes is empty
```

- Header follows the existing pluralisation idiom
  (`f"    parked ({len(plan.parked_nodes)} node{'s' if len(plan.parked_nodes) != 1 else ''}):"`).
- Node ids sorted, so the output is stable between runs.
- The record path is `os.path.join(".effects", f"{nid}.json")` — relative to
  the run directory, which the first header line already names. Do **not**
  add a `run_dir` field to `RecoveryPlan` for this; the plan does not carry
  one and inventing one changes the serialised shape.
- The empty case prints the section header plus `(none)`, matching the
  `reuse` block, so an operator can tell "no parked nodes" from "this build
  does not know about parking".
- Keep `format_status` a pure read with no dispatcher seam — its
  `status_no_dispatch` verifier contract asserts that.

### 2. `_fsync_dir` docstring

Replace the "best-effort" sentence with one that distinguishes the branches:
a directory that cannot be opened is tolerated (the file-level fsync already
succeeded, and some filesystems refuse `fsync` on a directory handle); an
`OSError` from `os.fsync` itself propagates to the caller, which fails closed
for `mark_effect_pending` (returns non-zero, the publisher refuses to run)
and leaves the record `pending` for `mark_effect_done`. Say why the
asymmetry is deliberate: failing open on a sync error is how a crash loses
exactly the record the sync exists to protect.

### 3. `--force` help text

```
  --force                      Override a ``retryable: false`` retry hint
                                 and dispatch anyway. Also re-dispatches an
                                 irreversible node (the publisher) that a
                                 prior run parked for reconciliation because
                                 its effect may already have landed; the
                                 ``pending`` effect record is left in place.
```

Match the surrounding indentation and the existing ``--flag`` style.

### 4. Tests

Extend `tests/unit/test_publisher_effect_record.py` with:

- a plan built with `parked_nodes=("publish",)` whose `format_status` output
  contains one `[parked] publish` line and the `.effects/publish.json` path;
- a plan with `parked_nodes=()` whose output still shows the `parked` header
  and `(none)`;
- the existing `reuse`/`rerun` lines and the `entry:` line unchanged in both
  (assert on the substring, not a whole-output golden).

Reuse the module's existing fixtures and helpers rather than building a new
`RecoveryPlan` from scratch if one is already there.

## Requirements

- One diff, four files. Do not touch `.mini-ork/`, `db/migrations/`,
  `config/risk-tiers.yaml`, or any frozen path.
- No behaviour change to `node_resume_decision`, `_reusable_set`, the
  `recover` refusal, its exit code, or any reason string.
- No new field on `RecoveryPlan`; `to_dict()` keeps its current shape.
- `format_status` stays side-effect free and dispatcher-free.
- Comments and docstrings in the surrounding style: lowercase, declarative,
  explaining *why*.

## Done When

- AC1 `format_status` prints one `[parked] <node>` line per entry in
  `plan.parked_nodes`, sorted, with the `.effects/<node>.json` path; a plan
  with no parked nodes prints the section header and `(none)`.
- AC2 The `reuse`, `rerun`, `cost boundary`, `entry` and `sku` lines are
  byte-identical to today for every plan, parked or not.
- AC3 `_fsync_dir`'s docstring states that an `os.fsync` error propagates and
  that the caller fails closed; the function body is unchanged.
- AC4 `recover --help` mentions that `--force` re-dispatches a parked
  irreversible node and leaves the `pending` record in place.
- AC5 `python3.11 -m pytest tests/test_node_checkpoints.py
  tests/unit/test_publisher_effect_record.py -q` passes, and `make lint`
  is clean.

## Files in scope

- `mini_ork/recovery/plan.py`
- `mini_ork/recovery/planner.py`
- `mini_ork/stores/checkpoints.py`
- `tests/unit/test_publisher_effect_record.py`

## Out of scope

- Changing when a node is parked, what `--force` does at runtime, or the
  refusal message.
- Any new `recover` subcommand, flag, exit code, or inbox row.
- `format_status` printing effect-record *contents* (the operator reads the
  file; the status names it).
- Fixing the unnumbered Done-When advisory in older kickoffs.

## Verification command

```bash
python3.11 -m pytest tests/test_node_checkpoints.py tests/unit/test_publisher_effect_record.py -q && make lint
```
