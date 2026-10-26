# rev6 · R2d: a degraded review is never cached, and an empty LLM panel counts as "no LLM lens ran"

## Goal

The first working LLM pre-push review (#65, the R2c merge,
2026-10-26) reviewed R2c's own gate change. glm and deepseek_flash
independently reported two real defects in `mini_ork/review/gate.py`
`run_gate`:

1. **A degraded review is cached.** When every LLM lens of a hybrid review
   failed (a transient lane outage), the review still gets a cache entry
   keyed by the patch-id (`_write_cache` runs for any approve/warn/block
   verdict). A retry of the same patch then reuses that review instead of
   running the panel again. In the secure tier, that pins a "no LLM lens ran"
   block for the cache TTL (7 days); in the simple tier, the patch stays
   heuristics-only.
2. **An empty or filtered panel slips past the guard.** `MO_REVIEW_PANEL=""`,
   or a panel whose every lane is filtered out (e.g. only `gemini-*`), runs no
   lens and records no `review_lens_runs` row, so `_llm_lenses_all_failed`
   returns `(False, 0)` and the hybrid review passes silently as
   heuristic-only.

Findings 31baadb5a49ce7aa, fe351bdeffc3f556, f62a869600e74acf and
6d35b966eab76776 in `review_findings`.

All paths are relative to the repository root.

## Mechanism (exact spec)

All changes are in `mini_ork/review/gate.py`.

1. **`_llm_lenses_all_failed(review_id, db, *, fresh: bool = False) -> tuple[bool, int]`.**
   Behaviour is unchanged when `fresh` is false. When `fresh` is true and the
   `review_lens_runs` table EXISTS but holds no row for `review_id`, return
   `(True, 0)`: a review that just ran in hybrid mode and recorded no lens ran
   none. A missing table is still `(False, 0)`.
2. **Fresh review in hybrid mode.** Compute
   `degraded = _llm_lenses_all_failed(review_id, db, fresh=True)[0]` BEFORE
   writing the cache. Write the cache entry only when the verdict is terminal
   AND NOT `degraded`.
3. **Cache hit in hybrid mode.** When the cached review is degraded
   (`_llm_lenses_all_failed(review_id, db)` is `(True, n)` with `n >= 1`),
   treat it as a cache miss: run a fresh review (the existing fresh-review
   path, with `cached = False`).
4. **The warning** reads `no LLM lens ran (all <n> failed)` when `n >= 1`, and
   `no LLM lens ran (the panel is empty)` when `n == 0`. The secure-tier block
   and its reason (`secure tier: no LLM lens ran`) are unchanged.
5. Nothing else changes: heuristic mode never warns, and the summary line
   format is the same.

## Files in scope (touch ONLY these)

- `mini_ork/review/gate.py`
- `tests/unit/test_pre_push_gate_degraded.py` (new)

Do NOT modify `mini_ork/review/lenses.py`, `mini_ork/pre_push_review.py`,
`mini_ork/review/findings.py`, any migration, or any path in
`config/kernel-frozen.yaml`. Every existing test must pass unchanged.

## Tests (`tests/unit/test_pre_push_gate_degraded.py`, hermetic, no LLM)

Reuse the fixture shape of `tests/unit/test_pre_push_gate_lens_env.py`:
- a primary repo with a bare origin and a linked worktree;
- a DB from `mini_ork.stores.migrate.init_db(db=..., root=REPO)` (assert rc 0);
- a fake `review_fn` that inserts a review row with a chosen verdict plus
  chosen `review_lens_runs` rows, and counts its calls;
- `MO_REVIEW_LLM_LENSES=1`.

1. **No cache entry for a degraded review.** A fresh review whose two lens
   rows are `dispatch_failed` → no cache entry is written for the patch-id.
   A second `run_gate` on the same patch calls `review_fn` again (2 calls).
2. **A healthy review is cached as before.** One `ok` row → a cache entry; the
   second call is a cache hit (1 call).
3. **A degraded cached entry re-runs.** Written by hand to the cache and
   pointing at a degraded review → `run_gate` calls `review_fn` (cache miss)
   and the result has `cached=False`.
4. **Empty panel, fresh, simple tier.** A review with no lens rows (the table
   exists) → output contains `no LLM lens ran (the panel is empty)`, rc
   follows the verdict, and there is no cache entry.
5. **Empty panel, secure tier** (`review: {tier: secure}`) → rc 1, verdict
   `unavailable`.
6. **Missing table, fresh review** → no warning; the cache is written as
   today.
7. **Heuristic mode** (`MO_REVIEW_LLM_LENSES` unset) with no rows → no
   warning, and the cache is written.

## Verification commands

- `python3.11 -m pytest -q -p no:asyncio tests/unit/test_pre_push_gate_degraded.py tests/unit/test_pre_push_gate_lens_env.py tests/unit/test_pre_push_gate.py tests/unit/test_review_findings.py`

## Self-application measurement

```measure
python3.11 -m pytest -q -p no:asyncio tests/unit/test_pre_push_gate_degraded.py --junitxml="${MINI_ORK_RUN_DIR}/rev6-r2d-junit.xml"
```

## Required artifacts — declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/rev6-r2d-junit.xml`

## Success criteria

- The verification command passes; every pre-existing test is unchanged.
- `ruff check --select F,E9 mini_ork/review/gate.py tests/unit/test_pre_push_gate_degraded.py` is clean.
- A transient lane outage can never pin a patch's review for the cache TTL.
