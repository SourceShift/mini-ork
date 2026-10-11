# Fix: refresh an ORPHANED `pre-implementer-ref` unconditionally on resume

Follow-up to `kickoffs/auto/recover-refresh-pre-implementer-baseline.md`
(landed `7e113de9`). That fix refreshes a stale baseline only when the run's
ledger does **not** record a prior `impl_moved_base` node failure. This misses
the orphaned case.

## Problem

`_refresh_stale_pre_impl_baseline` (`mini_ork/cli/execute.py`, ~line 2011) does:

```
moved = _implementer_moved_base(run_dir, target)
if not moved: return False                      # baseline still valid
if _impl_moved_base_recorded(db, run_id):        # ledger says the run moved it
    <refuse>                                     # keep failing loudly
    return False
<refresh>                                        # drop the snapshot
```

`_impl_moved_base_recorded` is **sticky**: once a run has EVER failed
`impl_moved_base`, the run can never refresh again — even when the failure was
an *orphaned* baseline (the recorded commit is no longer an ancestor of HEAD
because `main` was rewritten by a diverged merge / squash). Observed live
2026-10-10: `rsi-i1-control-arm-…115150` (and the whole `rsi-i*` set) still
prints `[recover] refusing to refresh pre-implementer-ref …` then
`[ground-truth] FAIL`.

**Why the refusal is wrong for an orphaned base:** the harvest diffs the working
tree against the baseline. Laundering — the hazard the refusal exists to stop —
is only possible when the baseline is an **ancestor** of HEAD, because only then
can the implementer's committed change sit in `baseline..HEAD` and be dropped by
a refresh to HEAD. If the baseline is **not** an ancestor of HEAD (histories
diverged), nothing of that baseline can be carried into HEAD, so refreshing to
HEAD cannot hide any run commit. The refusal is a false positive.

## Required behavior

In `_refresh_stale_pre_impl_baseline`, **before** the ledger check, test whether
the baseline is an ancestor of HEAD:

- If the baseline is **NOT** an ancestor of HEAD — `git merge-base --is-ancestor
  <baseline> HEAD` exits non-zero (1 = diverged, 128 = the object is missing
  entirely) — refresh **unconditionally** (skip `_impl_moved_base_recorded`).
- If the baseline **IS** an ancestor of HEAD (exit 0), keep today's behavior:
  the ledger check decides (refuse when it records `impl_moved_base`).

Factor the "remove `pre-implementer-ref` + `pre-implementer-untracked`, print,
return True" block into one helper so both branches call it (no duplicated
removal).

## Invariants this MUST preserve (do not regress)

1. **Within-attempt idempotence** — a plain retry (HEAD == the recorded baseline,
   or the stash-WIP first-parent shape) is still left byte-identical; the guard
   returns `""` so the refresh returns False before any ancestry test.
2. **Laundering stays closed for a forward ancestor** — when the baseline IS an
   ancestor of HEAD and the ledger records `impl_moved_base`, still refuse and
   leave the snapshot in place (the guard must keep failing loudly).
3. **The guard still fires** on a within-attempt move (unchanged code/message).
4. **Missing-object baselines** (`git rev-list` fails, object gc'd) are the same
   class as orphaned → refresh (exit 128 is non-zero → unconditional refresh).

## Where

- `mini_ork/cli/execute.py::_refresh_stale_pre_impl_baseline` (~line 2011) — the
  only functional change.
- Do **not** change `_implementer_moved_base` (`execute_handlers.py`): it stays
  the single definition of "this baseline is a valid pre-tree of HEAD".
- Do **not** change `planner.py`: the "is an implementer in the closure" gate is
  unchanged.

## Acceptance

- New test: an **orphaned** baseline (object missing, or diverged by a reset),
  with the run's ledger **recording** `impl_moved_base` in `notes`, still
  refreshes (`_refresh_stale_pre_impl_baseline` returns True and drops the file).
  Mutation-proof: fails on current code, passes after.
- New test (complement): a **forward-ancestor** baseline (HEAD is a descendant,
  an external commit between) with the ledger recording `impl_moved_base` is
  still refused (returns False; file kept).
- The 8 existing tests in
  `tests/unit/test_resume_refresh_pre_impl_baseline.py` still pass.
- `python3.11 -m pytest -q tests/unit/test_resume_refresh_pre_impl_baseline.py`
  is green.

## Non-goals

- Do NOT broaden the harvest condition in `_implementer_moved_base` — accepting
  any ancestor would sweep external commits into `framework-edit.diff` (the
  sdd-i5 54-file-for-6-file bug the guard exists to stop).
- Do NOT make the refresh unconditional for a forward ancestor.
