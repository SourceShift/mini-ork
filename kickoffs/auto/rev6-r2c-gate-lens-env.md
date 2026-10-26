# rev6 · R2c: the pre-push LLM lenses actually run from a worktree, and a review with no working lens says so

## Goal

R2b's lens-outcome lines exposed that the LLM half of the revived pre-push
review has never worked for a worktree merge. The 2026-10-26 merge of R2b
itself printed:

```
pre-push review: lens llm.glm dispatch_failed (0 issues, 0s)
pre-push review: lens llm.deepseek_flash dispatch_failed (0 issues, 0s)
```

**Cause, reproduced:** the merge driver (`_prereview`) and the hook run the
gate with the caller's environment. `run_gate` resolves the primary checkout's
`state.db` and `local.sh`, but not `MINI_ORK_HOME` for the dispatch it
triggers. In a worktree, `python -m mini_ork.dispatch glm` then fails its lane
preflight in 0 ms with `glm: $GLM_API_KEY is not set`. With
`MINI_ORK_HOME=<primary>/.mini-ork` it answers. Every worktree-merge review
since R2a (#52–#58) therefore approved on heuristics alone.

**Two more facts:**
- At `MO_REVIEW_LENS_TIMEOUT_S=180`, glm needed 179 s on a 15 kB diff, and
  timed out under load.
- A review where no LLM lens ran looks identical to a clean review.

All paths are relative to the repository root.

## Mechanism (exact spec)

### 1. `mini_ork/review/gate.py`

1. **`run_gate`, fresh-review path.** The overrides passed to
   `scoped_environ` are the `MO_REVIEW_*` keys of `merged` (unchanged) plus
   `MINI_ORK_HOME = str(resolve_home(cwd, merged))` when `merged` has no
   non-empty `MINI_ORK_HOME`.
2. **New `_llm_lenses_all_failed(review_id, db) -> tuple[bool, int]`.**
   - Read `review_lens_runs` rows for the review (missing table → `(False, 0)`).
   - Return `(True, n)` when there is at least one row and none has
     `status == 'ok'`, where `n` is the number of rows; otherwise `(False, n)`.
3. **After a FRESH review** (not a cache hit), when `mode == "hybrid"` and
   `_llm_lenses_all_failed` is `(True, n)`:
   - print `pre-push review: ⚠ no LLM lens ran (all <n> failed); this verdict rests on heuristics only`;
   - if `load_policy(home=...).tier == "secure"`, return
     `GateResult(1, "unavailable", review_id, False, "secure tier: no LLM lens ran")`
     (with the summary line), whatever the review's verdict;
   - in the simple tier the verdict stands.
   - A `ValueError` from `load_policy` → rc 1, as in `_unavailable`.
4. **On a cache hit**, apply the same check to the cached review: print the
   warning, and fail closed in the secure tier.

### 2. `mini_ork/review/lenses.py`

The `MO_REVIEW_LENS_TIMEOUT_S` default becomes `"600"`. Nothing else changes.

### 3. `docs/operator/feature-flags.md`

- Change the `MO_REVIEW_LENS_TIMEOUT_S` row's default to `"600"`, with the
  reason (glm 179 s on a 15 kB diff).
- Add one sentence after the cache sentence: a hybrid review in which no LLM
  lens ran prints a warning and, in the secure tier, blocks the push.

## Files in scope (touch ONLY these)

- `mini_ork/review/gate.py`
- `mini_ork/review/lenses.py`
- `docs/operator/feature-flags.md`
- `tests/unit/test_pre_push_gate_lens_env.py` (new)

Do NOT modify `mini_ork/pre_push_review.py`, `mini_ork/review/findings.py`,
`mini_ork/review/verdict.py`, `.githooks/pre-push`, `scripts/mini_ork_worktree.py`,
any migration, or any path in `config/kernel-frozen.yaml`. Every existing test
must pass unchanged.

## Tests (`tests/unit/test_pre_push_gate_lens_env.py`, hermetic, no LLM)

Reuse the fixture shape of `tests/unit/test_pre_push_gate.py`:
- a primary repo with a bare origin and a linked worktree;
- a DB from `mini_ork.stores.migrate.init_db(db=..., root=REPO)` (assert rc 0) at `<primary>/.mini-ork/state.db`;
- a fake `review_fn`;
- `MINI_ORK_*` and `MO_REVIEW_*` stripped from the env.

1. **`MINI_ORK_HOME` reaches the review.** Inside the fake `review_fn`,
   `os.environ["MINI_ORK_HOME"]` equals `<primary>/.mini-ork` when the caller's
   env had none. When the caller set one, it is unchanged. After `run_gate`
   returns, the process env is restored.
2. **All lenses failed, simple tier.** `MO_REVIEW_LLM_LENSES=1`, and the fake
   review inserts an approve review plus two `review_lens_runs` rows with
   status `dispatch_failed` → rc 0, verdict `approve`, and the output contains
   `no LLM lens ran (all 2 failed)`.
3. **All lenses failed, secure tier** (`<home>/config/review.yaml`
   `review: {tier: secure}`) → rc 1, verdict `unavailable`.
4. **One lens `ok`, one `dispatch_failed`** → no warning, rc 0.
5. **Heuristic mode** (`MO_REVIEW_LLM_LENSES` unset) with failed rows → no
   warning.
6. **Cache hit** on a review whose lenses all failed, secure tier → rc 1.
7. `lenses._default_llm_panel` with no `MO_REVIEW_LENS_TIMEOUT_S` passes
   `"600"` to the dispatch fn.

## Verification commands

- `python3.11 -m pytest -q -p no:asyncio tests/unit/test_pre_push_gate_lens_env.py tests/unit/test_pre_push_gate.py tests/unit/test_review_findings.py tests/unit/test_pre_push_review_py.py`

## Self-application measurement

```measure
python3.11 -m pytest -q -p no:asyncio tests/unit/test_pre_push_gate_lens_env.py --junitxml="${MINI_ORK_RUN_DIR}/rev6-r2c-junit.xml"
```

## Required artifacts — declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/rev6-r2c-junit.xml`

## Success criteria

- The verification command passes; every pre-existing test is unchanged.
- `ruff check --select F,E9 mini_ork/review/gate.py mini_ork/review/lenses.py` is clean.
- From a linked worktree with no `MINI_ORK_HOME` exported, a hybrid gate run
  dispatches the lenses with the primary home (the `$GLM_API_KEY is not set`
  preflight failure cannot recur).
