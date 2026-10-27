# R2e: the pre-push LLM review never runs inside the merge lock; three open gate findings

## Goal

The merge driver (`scripts/mini_ork_worktree.py` `merge_worktree`) reviews the
branch with the pre-push gate BEFORE taking the merge lock (`_prereview`). The
in-lock `git push` then fires `.githooks/pre-push`, which runs the same gate
again. The second run is meant to be a no-op through the patch-id cache.

Since R2d (`mini_ork/review/gate.py`), a degraded hybrid review (no LLM lens
ran) is deliberately NOT cached, so a retry runs the panel again. Side effect,
raised by both LLM lenses in review #69 (finding d6c35ecb4d72f0c8): when the
pre-review is degraded, the in-lock push hook re-runs the LLM panel (up to
`MO_REVIEW_LENS_TIMEOUT_S` = 600 s) while holding the lock. That starves every
other merge on the machine, since peers wait at most 120 s for the lock.

Also fix three open findings on `mini_ork/review/gate.py` from reviews #65/#69:
- **4a27ffaac73e862b:** a panel that raised mid-review leaves an `llm.panel`
  `error` row; when another lens row is `ok`, `_llm_lenses_all_failed` reports
  "not all failed", so the crash goes unreported.
- **30f6b99552ac8995 / 60c59b3e53e06225:** in the secure tier, the "no LLM lens
  ran" early return replaces a `block` verdict with `unavailable` and never
  prints the blocking findings.
- **dfb130ae1a83fb0d:** a CACHED hybrid review with zero lens rows (written
  before lens rows existed) is reused, bypassing the empty-panel rule.

All paths are relative to the repository root.

## Mechanism (exact spec)

### 1. `scripts/mini_ork_worktree.py`

1. **`_patch_id(wt: str) -> str`.** Pipe `git -C <wt> diff --no-color --no-renames origin/main...HEAD`
   into `git -C <wt> patch-id --stable` and return the first token. Return
   `""` for an empty diff or any non-zero rc. No `mini_ork` import.
2. **`_prereview(wt) -> str`.** Returns the reviewed patch-id when the gate
   exited 0, else `""`. Behaviour is otherwise unchanged:
   - `MO_REVIEW_SKIP=1` or no hook → return `""`;
   - rc 1 → `die(...)`, same message;
   - any other rc → print the same message and return `""`.
3. **In `merge_worktree`:**
   - keep `reviewed = _prereview(wt)` (today the return value is ignored);
   - inside the lock, run the push with
     `env={**os.environ, "MO_REVIEW_PRECHECKED_PATCH": reviewed}` when
     `reviewed` is non-empty, else the current env;
   - the `git()` helper already takes `env=`.

### 2. `mini_ork/review/gate.py` — `run_gate`

1. **Skip a patch already reviewed before the lock.** Right after computing
   `pid` (and after the `MO_REVIEW_SKIP` / empty-diff exits), when
   `merged.get("MO_REVIEW_PRECHECKED_PATCH")` is non-empty and equals `pid`,
   return
   `GateResult(0, "skipped", None, False, "reviewed before the merge lock (patch <pid[:12]>)")`
   with the summary line. No review, no cache read, no dispatch.
   - A non-matching value is ignored (the patch changed during the rebase), so
     the gate runs normally.
   - The value only skips when it equals the real patch-id, so it is no weaker
     than `MO_REVIEW_SKIP`.
2. **`_llm_lenses_all_failed(review_id, db, *, fresh=False)`.** Also return
   `(True, n)` when ANY row has `lens == "llm.panel"` and `status == "error"`
   (the panel crashed), whatever the other rows say.
3. **Cache hit in hybrid mode.** Call `_llm_lenses_all_failed(review_id, db, fresh=True)`.
   This treats a cached review with zero lens rows (the table exists) as
   degraded, so it is re-run like the existing `lens_count >= 1` case. Drop
   the `lens_count >= 1` condition there.
4. **Secure-tier degraded path.** Before returning `unavailable`, when the
   review's verdict is `warn` or `block`, print `review_show(...)` and, for
   `block`, `_print_block_findings(...)`. The gate result's `reason` becomes
   `"secure tier: no LLM lens ran"`, plus `" (review verdict: block)"` when the
   verdict was block. rc stays 1.

### 3. `docs/operator/feature-flags.md`

Add a row: `MO_REVIEW_PRECHECKED_PATCH` | unset | set by `make worktree-merge`
for its in-lock push: the patch-id the driver already reviewed before taking
the merge lock; the push hook skips the review when it matches the real
patch-id. Internal; do not set it by hand.

## Files in scope (touch ONLY these)

- `scripts/mini_ork_worktree.py`
- `mini_ork/review/gate.py`
- `docs/operator/feature-flags.md`
- `tests/unit/test_review_outside_lock.py` (new)

Do NOT modify `.githooks/pre-push`, `mini_ork/pre_push_review.py`,
`mini_ork/review/lenses.py`, any migration, or any path in
`config/kernel-frozen.yaml`. Every existing test must pass unchanged.

## Tests (`tests/unit/test_review_outside_lock.py`, hermetic, no LLM)

Gate tests reuse the fixture shape of `tests/unit/test_pre_push_gate_degraded.py`.
Driver tests load the script with `importlib.util.spec_from_file_location`
and use a FakeRunner / monkeypatched `subprocess.run`, as
`tests/unit/test_worktree_script_py.py` does.

1. **Skip on a matching precheck.** `run_gate` with
   `MO_REVIEW_PRECHECKED_PATCH` equal to the branch's patch-id → rc 0,
   verdict `skipped`, `review_fn` never called.
2. **Mismatch runs the review.** A precheck value that differs from the
   patch-id → the review runs (`review_fn` called once).
3. **A crashed panel is degraded.** Lens rows `("llm.glm", "ok")` and
   `("llm.panel", "error")` → `_llm_lenses_all_failed` is `(True, 2)`.
4. **Zero-row cache hit re-runs.** A cached hybrid review with no lens rows
   (the table exists) → `review_fn` called (cache miss).
5. **Secure tier, degraded block.** A degraded review whose verdict is
   `block` → rc 1; output contains the block hint
   (`dismiss one with: mini-ork review resolve`); reason ends with
   `(review verdict: block)`.
6. **Driver `_patch_id`.** Empty for a branch with no commits beyond
   origin/main; non-empty and stable across a clean rebase (real tmp git repo
   with a bare origin).
7. **Driver push env.** `merge_worktree`'s push call receives
   `MO_REVIEW_PRECHECKED_PATCH=<pid>` when `_prereview` returned a pid, and no
   such key when it returned `""` (capture the `env` passed to the push).

## Verification commands

- `python3.11 -m pytest -q -p no:asyncio tests/unit/test_review_outside_lock.py tests/unit/test_pre_push_gate_degraded.py tests/unit/test_pre_push_gate_lens_env.py tests/unit/test_pre_push_gate.py tests/unit/test_worktree_script_py.py tests/unit/test_merge_oss_guard.py tests/unit/test_merge_risk.py`

## Self-application measurement

```measure
python3.11 -m pytest -q -p no:asyncio tests/unit/test_review_outside_lock.py --junitxml="${MINI_ORK_RUN_DIR}/bif-r2e-junit.xml"
```

## Required artifacts — declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/bif-r2e-junit.xml`

## Success criteria

- The verification command passes; every pre-existing test is unchanged.
- `ruff check --select F,E9 scripts/mini_ork_worktree.py mini_ork/review/gate.py tests/unit/test_review_outside_lock.py` is clean.
- The merge driver keeps its "no top-level mini_ork import" property.
- A merge whose pre-review ran never runs the LLM panel inside the merge lock.
