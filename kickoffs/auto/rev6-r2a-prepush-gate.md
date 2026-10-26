# rev6 · R2a: the pre-push code review runs again — Python gate, primary DB, parallel lenses, cached by patch-id

## Goal

The pre-push "Layer 3" code review has not run since 2026-06-27 (51 reviews,
then none). Its Python implementation exists: `mini_ork/pre_push_review.py`
(`review_run`, `review_verdict_for`, `review_show`,
`review_forward_to_bug_reports`) replaced the bash in 07ecd58f. But
`.githooks/pre-push` `_layer3_run` (lines 114-247) still gates on the deleted
`lib/pre_push_review.sh`, prints "Layer 3 not installed", and returns 0.

Three more defects would bite as soon as it runs again:

1. **The review writes to the wrong DB from a worktree.** `REPO_ROOT` is the
   hook file's parent, so in a worktree it is the worktree. The hook's DB
   default `<repo_root>/.mini-ork/state.db` and `local.sh`
   (`<repo_root>/.mini-ork/config/local.sh`, where `MO_REVIEW_LLM_LENSES=1`
   lives) do not exist there. Every `make worktree-merge` push would review in
   heuristic mode against an empty DB.
2. **The lenses run one after another** (`_default_llm_panel`,
   `mini_ork/review/lenses.py:309`), each with up to `MO_REVIEW_LENS_TIMEOUT_S`
   = 180 s. The default panel `"codex kimi glm"` includes kimi, whose key
   returns 401.
3. **The review would hold the merge lock.** `scripts/mini_ork_worktree.py`
   `merge_worktree` pushes inside `_merge_lock` (line 603), and the push fires
   the hook. A multi-minute review there blocks every other merge.

This kickoff adds `mini_ork/review/gate.py`, which owns one decision:
review this branch, reuse an earlier review of the same patch, or skip, and
return an exit code. The hook and the merge driver both call it. The driver
calls it before taking the lock, so the in-lock push hits the cache. The
finding lifecycle (fixed / dismissed / pre-existing) is a separate kickoff
(R2b) and is out of scope here.

All paths are relative to the repository root.

## Mechanism (exact spec)

### 1. `mini_ork/review/gate.py` (new)

```
CACHE_REL = "state/pre-push-review-cache.json"
CACHE_TTL_S = 7 * 86400

@dataclass(frozen=True)
class GateResult:
    rc: int            # 0 = push may proceed, 1 = blocked
    verdict: str       # approve|warn|block|skipped|unavailable|empty
    review_id: int | None
    cached: bool
    reason: str

def primary_root(cwd: str | Path) -> Path
def resolve_home(cwd, env: Mapping[str, str]) -> Path
def resolve_db(cwd, env: Mapping[str, str]) -> Path
def load_review_env(home: Path, env: Mapping[str, str]) -> dict[str, str]
def patch_id(base: str, head: str, cwd) -> str
def run_gate(*, target: str, cwd, env: Mapping[str, str] | None = None,
             head: str = "HEAD", base: str = "",
             review_fn=None, now: int | None = None) -> GateResult
def main(argv: list[str] | None = None) -> int
```

1. **`primary_root(cwd)`** runs
   `git -C <cwd> rev-parse --path-format=absolute --git-common-dir`. If the
   result's basename is `.git`, return its parent; otherwise return
   `git -C <cwd> rev-parse --show-toplevel`. This is the primary checkout even
   when `cwd` is a linked worktree.
2. **`resolve_home`**: `env["MINI_ORK_HOME"]` if non-empty, else
   `primary_root(cwd) / ".mini-ork"`. **`resolve_db`**: `env["MINI_ORK_DB"]` if
   non-empty, else `resolve_home(...) / "state.db"`.
3. **`load_review_env(home, env)`** returns a copy of `env`. For each line of
   `home/config/local.sh` (if present), apply the hook's `_source_local_env`
   rule: plain `KEY=VALUE`, optional `export `, one layer of matching
   quotes stripped, blank and `#` lines ignored. A key is set only when it
   starts with `MO_REVIEW_` and is not already in `env`.
4. **`patch_id(base, head, cwd)`** pipes
   `git -C <cwd> diff --no-color --no-renames <base> <head>` into
   `git patch-id --stable` and returns the first token of the output, or `""`
   when the diff is empty.
5. **`run_gate`**:
   1. `env = load_review_env(resolve_home(cwd, env or os.environ), env or os.environ)`.
   2. `MO_REVIEW_SKIP == "1"` → `GateResult(0, "skipped", None, False, "MO_REVIEW_SKIP=1")`.
   3. `head_sha = git rev-parse <head>`. If `base` is empty, use
      `git merge-base <head_sha> origin/main`; if that fails, use
      `git rev-parse <head_sha>^`.
   4. `pid = patch_id(base, head_sha, cwd)`. Empty → `GateResult(0, "empty", None, False, "no diff to review")`.
   5. `mode = "hybrid" if env.get("MO_REVIEW_LLM_LENSES") == "1" else "heuristic"`.
   6. **DB check.** If `db = resolve_db(cwd, env)` is not a file, or has no
      `pre_push_reviews` table, the result is **unavailable** (step 9).
   7. **Cache.** Read `resolve_home(...) / CACHE_REL` (a JSON object;
      unreadable → `{}`).
      - An entry `cache[pid]` with equal `mode` and `target`, `at` within
        `CACHE_TTL_S`, and a `pre_push_reviews` row `id = review_id` whose
        `verdict` is approve/warn/block → reuse it (`cached=True`), no new review.
      - Otherwise, inside `mini_ork.context.scoped_environ` with the
        `MO_REVIEW_*` keys of `env`, call
        `review_id = (review_fn or pre_push_review.review_run)(head_sha, target, mode=mode, base=base, cwd=str(cwd), db=str(db))`.
        Then read the verdict with
        `pre_push_review.review_verdict_for(review_id, db=str(db)).strip()`.
      - Write `cache[pid] = {"review_id", "mode", "target", "at": now}` after
        dropping entries older than `CACHE_TTL_S`, atomically (tmp file in the
        same dir + `os.replace`; create the dir). Never write a cache entry for
        a verdict outside approve/warn/block.
      - Any exception in this step → unavailable.
   8. **Verdict → result:**
      - `approve` → rc 0;
      - `warn` → rc 0, printing `review_show(review_id, db=...)`;
      - `block` → rc 1, printing `review_show(...)`. If
        `MO_REVIEW_AUTO_FIX == "1"`, first call
        `review_forward_to_bug_reports(review_id, db=...)` and print the count.
      - Any other verdict → unavailable.
   9. **Unavailable:**
      - `mini_ork.gates.review_policy.load_policy(home=resolve_home(...))` with
        `tier == "secure"` → `GateResult(1, "unavailable", ...)` with the reason
        "secure tier: the code review could not run, so the push fails closed";
      - a `ValueError` from `load_policy` → rc 1 with the error text;
      - otherwise (simple tier) → rc 0, printing a `⚠` warning that the review
        did not run and why.
   - Every printed line starts with `pre-push review:`.
   - The final summary line is
     `pre-push review: verdict=<v> review_id=<id or -> cached=<yes|no> target=<t>`.
6. **`main(argv)`**: argparse with `--target` (choices `main`, `feature`,
   default `main`), `--head` (default `HEAD`), `--base` (default `""`), and
   `--cwd` (default `.`). It runs `run_gate` and returns `result.rc`. Usage
   errors → rc 2.

### 2. `mini_ork/pre_push_review.py`

In `main(argv)`: when the first argument is `gate`, return
`mini_ork.review.gate.main(rest)` (lazy import). Add `gate` to the usage
text. Nothing else changes.

### 3. `.githooks/pre-push`

Replace the body of `_layer3_run(repo_root, target_main)` with a call to the
gate:
- `target = "main" if target_main == 1 else "feature"`.
- **Interpreter:** the first executable of
  `<primary>/.venv/bin/python` and `<repo_root>/.venv/bin/python`, else
  `sys.executable`. `<primary>` is computed from
  `git rev-parse --path-format=absolute --git-common-dir` exactly as in
  `gate.primary_root`; inline the few lines, because the hook must not import
  `mini_ork`.
- **Command:** `[python, "-m", "mini_ork.pre_push_review", "gate", "--target", target, "--cwd", str(repo_root)]`,
  with `cwd=str(repo_root)` and `PYTHONPATH=str(repo_root)` prepended to the
  existing value. stdout/stderr are inherited.
- **Exit codes:**
  - rc 0 → return 0;
  - rc 1 → print `✗ pre-push: Layer 3 blocked the push.`, then the bypass
    lines `MO_REVIEW_SKIP=1 git push` and `git push --no-verify` with the
    existing wording, and return 1;
  - any other rc → print `⚠ pre-push: Layer 3 reviewer error (rc=<rc>) — fail-open.`
    and return 0.
- Delete the now-unused `review_lib`, bash and `bin/mini-ork-review`
  references inside `_layer3_run`. Do not touch Layers 1, 2a, 2a.5 or 2b, or
  `main()`.

### 4. `mini_ork/review/lenses.py` — `_default_llm_panel`

- The default panel becomes `"glm minimax deepseek_flash"` (still overridable
  with `MO_REVIEW_PANEL`).
- **Run the lenses concurrently:** one `concurrent.futures.ThreadPoolExecutor`
  with `max_workers=len(panel)`, and one temp out-file per lens.
  `contextlib.redirect_stdout` / `redirect_stderr` are process-global, so wrap
  the WHOLE pool (enter both in the calling thread before submitting, exit
  after all futures finish), never per lens.
- Collect results in panel order, so the output order equals today's
  sequential order.
- Per-lens parsing, truncation, the 8-issue cap and temp-file cleanup are
  unchanged.

### 5. `scripts/mini_ork_worktree.py` — review before the lock

Add `_prereview(wt: str) -> None` and call it in `merge_worktree` after the
green gate passes and before `with _merge_lock(...)`:
- `MO_REVIEW_SKIP == "1"`, or no `<wt>/.githooks/pre-push` → return.
- **Interpreter:** `<ROOT>/.venv/bin/python` if executable, else
  `sys.executable`.
- **Command:** `[python, "-m", "mini_ork.pre_push_review", "gate", "--target", "main", "--cwd", wt]`,
  with `cwd=wt`, env `PYTHONPATH=wt` (prepended) and `check=False`.
- **Exit codes:**
  - rc 1 → `die("pre-push code review blocked the merge (findings above); fix them, or re-run with MO_REVIEW_SKIP=1 if a human accepts them")`;
  - any rc other than 0 and 1 → print `[mo-worktree] pre-push review errored (rc=<rc>); the push hook will retry it` and continue.
- Keep the script free of a top-level `mini_ork` import.

### 6. `docs/operator/feature-flags.md`

- Change the `MO_REVIEW_PANEL` row's default to `"glm minimax deepseek_flash"`.
- Add rows for `MO_REVIEW_LLM_LENSES` (`"0"`; `1` runs the LLM panel with the
  heuristics) and `MO_REVIEW_SKIP` (`"0"`; `1` skips the review; a human
  decision).
- Add one sentence under the table: the review is cached by `git patch-id` in
  `$MINI_ORK_HOME/state/pre-push-review-cache.json`, and in the secure review
  tier (`config/review.yaml` `tier: secure`) a review that cannot run blocks
  the push.

## Files in scope (touch ONLY these)

- `mini_ork/review/gate.py` (new)
- `mini_ork/pre_push_review.py`
- `mini_ork/review/lenses.py`
- `.githooks/pre-push`
- `scripts/mini_ork_worktree.py`
- `docs/operator/feature-flags.md`
- `tests/unit/test_pre_push_gate.py` (new)

Do NOT modify `mini_ork/review/verdict.py`, any migration or schema, the
`pre_push_reviews` / `pre_push_review_issues` tables, or any path in
`config/kernel-frozen.yaml`. Every existing test must pass unchanged.

## Tests (`tests/unit/test_pre_push_gate.py`, hermetic, no LLM)

**Fixture:**
- a tmp git repo with a bare `origin` (main pushed), plus a linked worktree created with `git worktree add`;
- a DB from `mini_ork.stores.migrate.init_db(db=..., root=REPO)` (assert rc 0) placed at `<primary>/.mini-ork/state.db`;
- a fake `review_fn` that inserts a `pre_push_reviews` row with a chosen verdict and counts its calls.

Strip `MINI_ORK_*` and `MO_REVIEW_*` from the env, then:

1. `primary_root(<linked worktree>)` == the primary checkout; `resolve_db` from
   the worktree == `<primary>/.mini-ork/state.db`.
2. `load_review_env` reads `MO_REVIEW_LLM_LENSES=1` from
   `<home>/config/local.sh` (with `export ` and quotes), ignores non-`MO_REVIEW_`
   keys, and never overrides a key already in `env`.
3. approve → rc 0; block → rc 1; warn → rc 0.
4. A second `run_gate` on the same patch after a rebase onto a new upstream
   commit (patch-id unchanged) → `cached=True` and `review_fn` is called once
   in total.
5. A changed patch → `review_fn` is called again.
6. `MO_REVIEW_SKIP=1` → rc 0, verdict `skipped`, no `review_fn` call.
7. No diff → verdict `empty`, rc 0.
8. Missing DB:
   - simple tier → rc 0, verdict `unavailable`;
   - with `<home>/config/review.yaml` `review: {tier: secure}` → rc 1.
9. `review_fn` raising → `unavailable`, with the same tier rule.
10. `_default_llm_panel` with a fake `dispatch_fn` that sleeps 1 s per lens
    over a 3-lens panel finishes in under 2.5 s and returns issues in panel order.
11. `python -m mini_ork.pre_push_review gate --help` exits 0.
12. The hook text no longer contains `pre_push_review.sh` and calls
    `mini_ork.pre_push_review` with the argument `gate`.

## Verification commands

- `python3.11 -m pytest -q -p no:asyncio tests/unit/test_pre_push_gate.py tests/unit/test_pre_push_review_py.py tests/unit/test_mini_ork_review_py.py tests/unit/test_review_extracted.py tests/unit/test_worktree_script_py.py tests/unit/test_merge_oss_guard.py`

## Self-application measurement

```measure
python3.11 -m pytest -q -p no:asyncio tests/unit/test_pre_push_gate.py --junitxml="${MINI_ORK_RUN_DIR}/rev6-r2a-junit.xml"
```

## Required artifacts — declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/rev6-r2a-junit.xml`

## Success criteria

- The verification command passes; every pre-existing test is unchanged.
- `ruff check --select F,E9 mini_ork/review/gate.py mini_ork/review/lenses.py mini_ork/pre_push_review.py scripts/mini_ork_worktree.py` is clean.
- `.githooks/pre-push` no longer references `lib/pre_push_review.sh` or
  `bin/mini-ork-review` (comments in other files that cite the old bash line
  numbers stay).
- The merge driver never runs an LLM review while holding the merge lock.
