# Fix: refresh `pre-implementer-ref` on a resume that re-runs the implementer

## Problem

`recover --strategy resume` re-runs the `implementer` node when its artifact hash
mismatches (`hash_mismatch_or_artifact_corrupt`). The implementer edits
`MO_TARGET_CWD` **in place, starting from the CURRENT tree HEAD**. But the run's
`<run_dir>/pre-implementer-ref` still holds the base captured when the run FIRST
started — possibly days earlier.

The post-implementer ground-truth guard
`mini_ork/cli/execute_handlers.py::_implementer_moved_base` (lines ~1422-1469)
compares HEAD to that stale baseline and fails:

```
[ground-truth] FAIL: HEAD <now> is not the run's starting commit <old>:
  the tree delta would carry unrelated commits.
```

This is a **false positive** whenever `main` advanced between the run's first
start and the resume. The guard's own docstring attributes ALL HEAD movement to
"the implementer committed, rebased, reset or checked out" — it has no allowance
for an *external* base advance across a resume. Observed live 2026-10-10:
7 `rsi-i*` runs (base orphaned by a main rewrite) and any resumed run on a moved
`main` fail this guard deterministically; the only current workaround is a manual
`rm <run_dir>/pre-implementer-ref` before recover (a kickoff/env workaround —
NOT acceptable).

## Required behavior

When a resume/recover will (re-)run the `implementer`, the baseline must reflect
the tree the implementer is about to start from. Concretely:
`<run_dir>/pre-implementer-ref` must be re-captured to the CURRENT target
HEAD (capturing current tracked dirt as the `git stash create` WIP commit, or
HEAD when clean) at the point the implementer node is about to run on the
resumed attempt — replacing a baseline that is **not** a valid pre-tree for the
current HEAD.

## Invariants this MUST preserve (do not regress)

1. **Within a single run/attempt**, the first capture wins: if the implementer
   ran and left edits UNCOMMITTED (HEAD unchanged), a retry must NOT re-snapshot
   — the baseline still equals HEAD, so the implementer's own uncommitted delta
   stays in scope. The refresh triggers only when the baseline is no longer the
   current pre-implementer tree (HEAD has moved off it).
2. **The laundering hazard must stay closed.** If a PRIOR implementer attempt
   COMMITTED its change (HEAD advanced by the implementer itself), refreshing the
   baseline to that moved HEAD would exclude the already-committed change from
   the harvested delta. This case must NOT silently produce an empty diff. Either
   keep failing loudly for that shape, or otherwise prove the committed change is
   still carried (e.g. detect that HEAD is a descendant of the baseline via the
   run's own commits and refuse if so). Choose the safest correct behavior and
   document the reasoning.
3. **The guard must still fire** when the implementer genuinely rebases / resets /
   checks out another ref during the resumed attempt (HEAD moves AFTER the fresh
   snapshot).
4. A run's `pre-implementer-ref` that is **orphaned** (exists as an object but is
   NOT an ancestor of HEAD, i.e. `main` was rewritten) is the same class: it is
   not a valid pre-tree for the current HEAD and must be refreshed, not honored.

## Where

- `mini_ork/cli/execute.py::_capture_pre_impl_baseline` (~line 1908) — idempotent
  `if os.path.isfile(ref_path): return`. The idempotence is correct WITHIN an
  attempt and wrong ACROSS a resume onto a moved tree.
- Call site `mini_ork/cli/execute_handlers.py:524` (before every non-rollback
  node).
- Guard `mini_ork/cli/execute_handlers.py::_implementer_moved_base` (~1422).
- Recovery entry: how `recover --strategy resume` decides the closure / entry
  (`entry: implementer` in the recover status output).

Pick the minimal, safe placement (refresh in the resume path when the implementer
is in the closure is likely cleaner than changing the always-on snapshot fn).

## Acceptance

- New test: a resumed run whose recorded baseline is (a) an ancestor-but-not-HEAD
  and (b) orphaned, with the implementer in the closure, proceeds past the guard
  and harvests a delta from CURRENT HEAD — mutation-proof (fails on the current
  code, passes after the fix).
- Existing guard test (the stash-shape / `run-1791543223-76795` case in the
  docstring) still passes.
- `_implementer_moved_base` still fails when HEAD moves during the attempt.
- Gate: `python3.11 -m pytest -q` over the execute/recover unit set is green.

## Non-goals

- Do NOT change the guard's semantics for a within-attempt move.
- Do NOT make the refresh unconditional (it must not break invariant 1).
