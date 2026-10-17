# LF-R11a findings age out and go stale at HEAD

## Goal

`context_v2.scope_findings` offers a run every finding whose path prefix
matches, forever. Of the 1,669 rows in the live table, 604 are older than 30
days and only 387 carry a `snippet`; findings about code that has since been
fixed keep being presented as live problems. Two decays, in this order (panel
R11 items 1–2; Q4 in `docs/plans/2026-10-15-learning-loop-fixes.md`):

1. **Max-age window.** `MO_CONTEXT_V2_MAX_AGE_DAYS` (default 30) drops rows
   whose `ts` is older than the window. Unset → 30. `0` → window disabled
   (the escape hatch for an archaeology query). A malformed value is a
   clear stderr warning and the 30-day default — never a silent 0.
2. **Staleness at HEAD.** A row whose `snippet` no longer occurs in its file
   is stale: the code the reviewer quoted is gone, so the finding is either
   fixed or the file moved on. Rows with no `snippet` fall back to age-only —
   that is 1,282 of 1,669 rows today, and inventing a verdict for them would
   be worse than the decay.

R12 has merged, so findings carry `target_repo` and
`code_findings.repo_identity(path)` resolves a checkout's common git dir.
Staleness is checked against THAT repo's HEAD, never blindly against the
engine checkout. Decision: **no migration.** `snippet` already holds the
quoted line (R5 populates it), so staleness is a read-time computation; a
`blob_sha` column would be a cache, and adding schema to `code_findings.py`
on top of R5/R6/R12 buys speed we have not measured a need for.

All paths are relative to the repository root.

## Mechanism (exact)

1. **`mini_ork/learning/code_findings.py` gains the staleness primitive** —
   pure and never raising:

   - `normalize_for_match(text) -> str` — `" ".join(str(text).split())`, so a
     re-indented line still matches.
   - `file_texts_at_head(repo_root: str, paths: list[str]) -> dict[str, str | None]`
     — one call for all paths. Resolve through git when `repo_root` is a git
     checkout (`git -C repo_root cat-file --batch` fed `HEAD:<path>` for each
     path, parsing the batch stream), and fall back to reading the file under
     `repo_root` when git is unavailable or the path is untracked. A missing
     path maps to `None`. Any git/subprocess error → return `{}` (callers then
     treat every row as fresh; a broken probe must never hide history).
   - `stale_reason(row, *, repo_root, file_texts, now, max_age_days) -> str | None`
     returning `"age"`, `"file-missing"`, `"snippet-absent"`, or `None`.
     Order: age (when `max_age_days > 0` and `now - ts` exceeds it in days) →
     no `file` (None, not stale) → file missing → `snippet` present but not a
     whitespace-normalized substring of the file text → stale. No `snippet` →
     age-only.
   - `annotate_staleness(rows, *, repo_root, now=None, max_age_days=30) -> list[dict]`
     — copies each row and adds `"stale": bool` and `"stale_reason": <reason>`;
     one `file_texts_at_head` call for the rows' distinct `file` values.
   - `max_age_days_from_env(environ=None) -> int` — parse
     `MO_CONTEXT_V2_MAX_AGE_DAYS`; unset/empty → 30; `0` → 0 (disabled);
     negative or non-integer → warn once on stderr (`[code_findings] …`) and
     return 30.
2. **`context_v2.scope_findings`** applies both decays to the rows it returns:
   `max_age = code_findings.max_age_days_from_env()`; when it is > 0, rows
   older than the window are excluded in SQL (`ts >= ?`), not after the fact;
   then the surviving rows are annotated with
   `code_findings.annotate_staleness(rows, repo_root=<resolved repo>)` and
   `stale` rows are dropped. `repo_root` is the current target repo, resolved
   exactly as R12 resolves it for selection (the `target_repo` argument when
   given, else `repo_identity(context_env("MO_TARGET_CWD"))`, else the engine
   root). Keep the existing prefix match, the ≥3-component parent-directory
   fallback and the `exclude_run` filter untouched.
3. **The pack records what it dropped.** `build()`'s pack gains
   `"n_findings_stale"` and `"max_age_days"` beside `"n_findings_scanned"`
   (scan count stays the pre-filter count so a reader can see the drop).
   `scope_findings` returns the fresh rows only; expose the drop count without
   changing its return type by having `build()` compute
   `n_total - len(fresh)` from a `scope_findings(..., with_stats=False)`
   companion — either an out-parameter dict or a second function
   `scope_findings_with_stats()` that returns `(rows, stats)`; keep
   `scope_findings`' existing signature and behaviour for other callers.
4. **The "Your code" tab demotes stale rows.** `mini_ork/ide_pages/learn/code.py`
   `_window_findings` annotates the rows it renders (same helper, respecting
   each row's `target_repo` via `repo_identity`), excludes `stale` rows from
   the "Problems reviewers already found" listing, and prints one line
   `N finding(s) hidden as stale at HEAD` above it when the count is non-zero.
   Stale rows remain visible nowhere else — do not delete them.
5. **A flag for the risky half:** `MO_FINDINGS_STALENESS=0` disables only the
   snippet check (age still applies). Default on.

## Files in scope

- `mini_ork/learning/code_findings.py`
- `mini_ork/context_v2.py`
- `mini_ork/ide_pages/learn/code.py`
- `tests/unit/test_code_findings_staleness.py` (new)
- `tests/unit/test_context_v2.py`
- `tests/unit/test_ide_pages_learn_code.py`

## Tests (`tests/unit/test_code_findings_staleness.py`, hermetic temp git repos + temp DB)

- a finding whose snippet still occurs at HEAD (different indentation) is not
  stale; the same row after an edit that rewrites the line is `snippet-absent`;
- a row whose file was deleted is `file-missing`;
- a row with no snippet and an old `ts` is `age`; with a recent `ts`, not stale;
- `max_age_days=0` keeps the old row; `max_age_days_from_env` maps unset→30,
  `"0"`→0, `"-3"`/`"soon"`→30 with a stderr warning;
- `file_texts_at_head` returns the HEAD text for a tracked file whose working
  tree is dirty (proving the read is HEAD, not the worktree), and `{}` when the
  repo path is not a git checkout;
- `scope_findings` excludes the aged row and the stale row and still returns
  the fresh one; the pack's `n_findings_stale` equals the number dropped;
- with `MO_FINDINGS_STALENESS=0`, the snippet-stale row comes back and the aged
  row still does not;
- the tab hides a stale row and shows the hidden-count line.

## Verification commands

- `/Volumes/docker-ssd/ps/mini-ork/.venv/bin/python -m pytest -q tests/unit/test_code_findings_staleness.py tests/unit/test_context_v2.py tests/unit/test_ide_pages_learn_code.py tests/unit/test_code_findings.py`

## Self-application measurement

```measure
/Volumes/docker-ssd/ps/mini-ork/.venv/bin/python -m pytest -q tests/unit/test_code_findings_staleness.py --junitxml="${MINI_ORK_RUN_DIR}/lf-r11a-junit.xml"
```

## Required artifacts — declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/lf-r11a-junit.xml`

## Rules

Edit files directly; do not emit unified diffs. Never rebase, merge or reset
the worktree during the run. Do not change `_fingerprint`, `classify_note`,
`repo_identity`, the `target_repo` selection rules, `parse_contract`,
`recurring()`, or rule actions; do not add a migration or a column; do not
delete rows (hiding is a read-path decision); and do not write to the live DB
under `/Volumes/docker-ssd/ps/mini-ork/.mini-ork/` (tests use temp DBs).
