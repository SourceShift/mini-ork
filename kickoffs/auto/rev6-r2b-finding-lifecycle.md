# rev6 · R2b: pre-push review findings get a lifecycle — fixed, dismissed with a reason, or pre-existing

## Goal

Every one of the 257 pre-push review issues ever recorded is still `open`
(3 critical, 37 high). A finding lives only inside the review that produced it:
- there is no identity across reviews, so a later push cannot say "fixed" or
  "this is the same finding";
- a human cannot dismiss one with a reason;
- a problem in code the push did not touch counts the same as one the push
  introduced.

R2a (merged) made the review run again through `mini_ork/review/gate.py`,
cached by patch-id. This kickoff adds the lifecycle:

- every finding has a stable **fingerprint** and a status: `open`, `fixed`,
  `dismissed` or `pre_existing`;
- the LLM panel sees the earlier findings on the files a push touches. It marks
  fixed ones `resolved`, re-reports still-present ones `same_as` their id, and
  flags problems in unchanged code as `pre_existing`;
- `dismissed` and `pre_existing` findings never count toward a verdict;
- `mini-ork review findings` lists them, and `mini-ork review resolve` lets a
  human record a disposition with a reason;
- a gate cache hit recomputes the verdict, so a dismissal unblocks a re-push
  of the same patch.

The old per-review rows (`pre_push_review_issues`, CHECK
`status IN ('open','dismissed','fixed','wontfix')`) are kept as observations
and are not altered.

All paths are relative to the repository root.

## Mechanism (exact spec)

### 1. `db/migrations/0085_review_findings.sql` (new)

```sql
CREATE TABLE IF NOT EXISTS review_findings (
  fingerprint      TEXT PRIMARY KEY,
  lens_kind        TEXT NOT NULL CHECK (lens_kind IN ('heuristic','llm')),
  file_path        TEXT,
  title            TEXT NOT NULL,
  severity         TEXT NOT NULL CHECK (severity IN ('info','low','medium','high','critical')),
  status           TEXT NOT NULL DEFAULT 'open'
                   CHECK (status IN ('open','fixed','dismissed','pre_existing')),
  first_review_id  INTEGER NOT NULL,
  last_review_id   INTEGER NOT NULL,
  last_issue_id    INTEGER,
  times_seen       INTEGER NOT NULL DEFAULT 1,
  reopened         INTEGER NOT NULL DEFAULT 0,
  reason           TEXT,
  resolved_by      TEXT,
  resolved_at      INTEGER,
  created_at       INTEGER NOT NULL,
  updated_at       INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rf_status_file ON review_findings(status, file_path);
CREATE TABLE IF NOT EXISTS review_finding_links (
  issue_id     INTEGER PRIMARY KEY,
  fingerprint  TEXT NOT NULL,
  review_id    INTEGER NOT NULL,
  pre_existing INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_rfl_review ON review_finding_links(review_id);
INSERT OR IGNORE INTO schema_migrations(filename, applied_at, checksum)
VALUES ('0085_review_findings.sql', strftime('%Y-%m-%dT%H:%M:%fZ','now'), 'review-findings-v1');
```

### 2. `mini_ork/review/findings.py` (new)

```
STATUSES = ("open", "fixed", "dismissed", "pre_existing")
def ensure_schema(con) -> None                     # the same two CREATE TABLE/INDEX IF NOT EXISTS statements
def fingerprint(issue: dict) -> str
def prior_for(con, files: list[str], limit: int = 30) -> list[dict]
def link_issue(con, issue_id: int, review_id: int, issue: dict, now: int) -> str
def settle(con, review_id: int, prior: list[dict], observed: set[str],
           resolved: set[str], now: int) -> list[str]
def resolve(db: str, fp: str, status: str, reason: str, by: str, now: int | None = None) -> None
def list_findings(db: str, status: str = "open", file: str = "", limit: int = 50) -> list[dict]
def main(argv: list[str]) -> int                    # `findings` and `resolve` subcommands
```

1. **`fingerprint(issue)`**: when `issue["same_as"]` is a non-empty string,
   return it unchanged (the caller has checked that it exists). Otherwise, with
   `norm(t)` = lowercase and runs of non-alphanumerics collapsed to one space,
   stripped:
   - heuristic lens (`lens` starts with `heuristic.`):
     `sha1(f"{lens}|{file}|{norm(title)}")[:16]`;
   - LLM lens:
     `sha1(f"llm|{file}|{norm(title)}")[:16]` (the lens name is excluded, so
     two lenses reporting the same title share one finding).
2. **`prior_for(con, files)`**: findings whose `file_path IN files` and whose
   status is not `fixed`, ordered by severity rank (critical first), then
   `updated_at DESC`, at most `limit`. Each is
   `{fingerprint, status, severity, file_path, title, lens_kind}`.
3. **`link_issue`** runs after an issue row is inserted:
   - `fp = fingerprint(issue)`; a `same_as` that names no existing finding
     falls back to the computed fingerprint.
   - **New fingerprint:** insert it with `status = 'pre_existing'` if
     `issue.get("pre_existing") is True`, else `'open'`.
   - **Existing fingerprint:**
     - set `last_review_id`, `last_issue_id`, `severity`, `updated_at`, and
       `times_seen = times_seen + 1`;
     - if its status is `fixed`, set it back to `open` and do
       `reopened = reopened + 1`;
     - leave `dismissed` / `pre_existing` as they are.
   - Insert the link `(issue_id, fp, review_id, 1 if pre_existing else 0)`.
   - Return `fp`.
4. **`settle`**: for each finding in `prior` with status `open` whose
   fingerprint is not in `observed`, set `status = 'fixed'`,
   `resolved_by = 'review:<review_id>'`, `resolved_at = now` when either:
   - its `lens_kind` is `heuristic` (the heuristics are deterministic, so
     silence means gone); or
   - its fingerprint is in `resolved`.
   Return the fingerprints it fixed.
5. **`resolve(db, fp, status, reason, by)`**:
   - `status` must be in `STATUSES` and not `open`;
   - `dismissed` and `pre_existing` need a non-empty `reason`;
   - an unknown `fp` → `ValueError`;
   - sets `status`, `reason`, `resolved_by = by`, `resolved_at` and
     `updated_at`.
6. **`main`** (called as `mini-ork review findings …` / `mini-ork review resolve …`):
   - `findings [--status open|fixed|dismissed|pre_existing|all] [--file PATH] [--limit N] [--json]`
     prints one line per finding:
     `<fp>  <status>  <severity>  <file>  <title>  (seen <n>×)`;
   - `resolve <fp> --as fixed|dismissed|pre_existing [--reason TEXT]` uses
     `by = "human:" + getpass.getuser()`.
   - The DB comes from `mini_ork.review.gate.resolve_db(".", os.environ)`.
   - Exit codes: rc 0 on success, rc 1 on `ValueError` (message on stderr),
     rc 2 on usage errors.

### 3. `mini_ork/review/lenses.py`

1. **`build_review_prompt(diff_text, prior) -> str`**: take `_REVIEW_PROMPT`
   (unchanged) and insert this block immediately before its
   `--- DIFF FOLLOWS ---` line. With a non-empty `prior`, the block starts with
   one line per earlier finding,
   `- [<fp>] <STATUS> <severity> <file> — <title>`, followed by:
   ```
   For each OPEN earlier finding: if this diff fixes it, put its id in "resolved"; if it is still present, report it again with "same_as": "<id>".
   Never report a DISMISSED or PRE_EXISTING finding again.
   ```
   With an empty `prior` it has only the two lines below. Either way the block
   ends with:
   ```
   Set "pre_existing": true on an issue whose problem is in code this diff did not change.
   Output shape: {"issues":[{"severity":"…","file":"…","line":null,"title":"…","description":"…","suggested_fix":"…","same_as":null,"pre_existing":false}],"resolved":[]}
   ```
2. **`_default_llm_panel(diff_text, *, dispatch_fn=None, prior=None, resolved_sink=None)`**:
   - when `prior is None` the prompt stays `_REVIEW_PROMPT + diff_text`
     (unchanged); otherwise it is `build_review_prompt(diff_text, prior) + diff_text`.
   - Each emitted issue keeps the existing keys and adds:
     - `same_as`: the lens's string value, only when it is one of the `prior`
       fingerprints, else `None`;
     - `pre_existing`: `bool(...)`.
   - When `resolved_sink` is a list, extend it with the lens's `resolved`
     strings that are `prior` fingerprints.
   - Everything else is unchanged, including the concurrency from R2a.

### 4. `mini_ork/pre_push_review.py` — `review_run`

After computing `diff_text`:
1. Open the DB, call `findings.ensure_schema(con)`, and compute `files` = the
   paths changed between base and source (reuse the diff's `+++ b/` paths).
   Set `prior = findings.prior_for(con, files)`.
2. LLM modes:
   - with the default panel, call it with `prior=prior, resolved_sink=resolved`;
   - a caller-supplied `llm_panel` keeps today's one-argument call.
3. While persisting each issue, call `findings.link_issue(con, <issue rowid>, rid, d, now)`
   on the same connection and collect the returned fingerprints in `observed`.
4. Before `_apply_verdict`, call
   `findings.settle(con, rid, prior, observed, set(resolved), now)`.

Any exception in steps 1, 3 or 4 is caught and skipped, so the review must
still complete exactly as today. The return value is unchanged.

### 5. `mini_ork/review/verdict.py` — `compute_verdict`

When both `review_finding_links` and `review_findings` exist, every count
query excludes issues linked to a finding whose status is `dismissed` or
`pre_existing`, or whose link has `pre_existing = 1`:
`AND id NOT IN (SELECT l.issue_id FROM review_finding_links l LEFT JOIN review_findings f ON f.fingerprint = l.fingerprint WHERE l.pre_existing = 1 OR f.status IN ('dismissed','pre_existing'))`.
When either table is missing, the queries stay exactly as today. The rationale
string format is unchanged. Apply the same exclusion to `_apply_verdict`'s
`issues_open` / `issues_critical` counts.

### 6. `mini_ork/review/gate.py`

1. On a cache hit, call `verdict._apply_verdict(review_id, target, str(db))`
   before reading the verdict, so a dismissal since the cached review takes
   effect.
2. On `block`, after `review_show`, print
   `pre-push review: blocking findings — dismiss one with: mini-ork review resolve <fingerprint> --as dismissed --reason "<why>"`,
   then one line per open high or critical finding linked to this review:
   `  <fingerprint>  <severity>  <file>  <title>`.

### 7. `mini_ork/pre_push_review.py` — CLI

In `main(argv)`, when the first argument is `findings` or `resolve`, return
`mini_ork.review.findings.main(argv)`. Add both to the usage text.

### 8. Lens outcomes are recorded

The first revived review (#52, 2026-10-26) recorded only a heuristic finding.
Nothing says whether the two LLM lenses ran and found nothing, or failed
(they fail open per lens).

- The migration also creates:
  ```sql
  CREATE TABLE IF NOT EXISTS review_lens_runs (
    id INTEGER PRIMARY KEY, review_id INTEGER NOT NULL, lens TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('ok','dispatch_failed','unparseable','error')),
    issues INTEGER NOT NULL DEFAULT 0, elapsed_ms INTEGER, created_at INTEGER NOT NULL);
  CREATE INDEX IF NOT EXISTS idx_rlr_review ON review_lens_runs(review_id);
  ```
  `findings.ensure_schema` creates it too.
- **`_default_llm_panel(..., lens_sink=None)`**: when `lens_sink` is a list,
  append one dict per lens, in panel order:
  `{"lens": f"llm.{model}", "status": ..., "issues": <emitted count>, "elapsed_ms": <int>}`.
  The status is:
  - `ok` — parsed;
  - `dispatch_failed` — non-zero rc, or the dispatch raised;
  - `unparseable` — out-file missing or no JSON payload;
  - `error` — any other exception.
- **`review_run`** passes `lens_sink` to the default panel and inserts the rows
  for this review id. On any error it skips the rows; the review result is
  unchanged.
- **`gate.run_gate`**, after a fresh (non-cached) review, prints one line per
  row: `pre-push review: lens llm.glm ok (2 issues, 41s)`. A test asserts a
  lens that raised is recorded as `dispatch_failed` and printed.

Add `review_lens_runs` to the files-in-scope DDL; the extra tests go in
`tests/unit/test_review_findings.py`.

## Files in scope (touch ONLY these)

- `db/migrations/0085_review_findings.sql` (new)
- `mini_ork/review/findings.py` (new)
- `mini_ork/review/lenses.py`
- `mini_ork/review/verdict.py`
- `mini_ork/review/gate.py`
- `mini_ork/pre_push_review.py`
- `tests/unit/test_review_findings.py` (new)

Do NOT modify `review_show` / `review_list` output formats,
`db/migrations/0035_pre_push_reviews.sql`, `.githooks/pre-push`,
`scripts/mini_ork_worktree.py`, or any path in `config/kernel-frozen.yaml`.

## Tests (`tests/unit/test_review_findings.py`, hermetic, no LLM)

Build the DB with `mini_ork.stores.migrate.init_db(db=..., root=REPO)` and
assert rc 0. Use a tmp git repo for `review_run`, with a fake `dispatch_fn`
injected by monkeypatching `lenses._default_llm_panel`'s `dispatch_fn`
default via `functools.partial`, or pass `llm_panel` where noted.

1. `fingerprint`:
   - title case and punctuation do not change it;
   - two LLM lenses with the same title share one;
   - a heuristic and an LLM issue with the same title differ;
   - a `same_as` value is returned unchanged.
2. Review 1 on a diff with one LLM high issue → one `open` finding and one link.
   Review 2 on a later diff touching the same file, where the fake lens lists
   that fingerprint in `resolved` and reports nothing → the finding is `fixed`
   with `resolved_by='review:<id>'`.
3. Review 2 re-reporting with `same_as=<fp>` → stays `open`, `times_seen=2`.
4. A finding marked `fixed`, reported again → `open`, `reopened=1`.
5. A heuristic finding (e.g. `heuristic.todo_marker`) absent from a later review of the same file → `fixed` without any `resolved`.
6. An issue with `pre_existing: true` → finding status `pre_existing`, and `compute_verdict` does not count it: a critical pre-existing issue alone gives `approve`.
7. `resolve(fp, "dismissed", reason="")` → `ValueError`. With a reason, the status is `dismissed`, and re-running `_apply_verdict` on the review that blocked turns `block` into `approve`.
8. Gate cache hit after a dismissal: `run_gate` with a fake `review_fn` returning that review id → rc 0 on the second call.
9. With the two tables dropped, `compute_verdict` gives the same result as before this change on a seeded review.
10. `build_review_prompt(diff, [])` contains `pre_existing` and `--- DIFF FOLLOWS ---` exactly once.
    `_default_llm_panel("d", dispatch_fn=f)` with no `prior` still sends exactly `_REVIEW_PROMPT + "d"`.
11. CLI:
    - `python -m mini_ork.pre_push_review findings --status all --json`, with `MINI_ORK_DB` set, lists the seeded findings;
    - `resolve <fp> --as dismissed` without `--reason` → rc 1.

## Verification commands

- `python3.11 -m pytest -q -p no:asyncio tests/unit/test_review_findings.py tests/unit/test_pre_push_gate.py tests/unit/test_pre_push_review_py.py tests/unit/test_mini_ork_review_py.py tests/unit/test_review_extracted.py tests/unit/test_ide_pages_changes.py`

## Self-application measurement

```measure
python3.11 -m pytest -q -p no:asyncio tests/unit/test_review_findings.py --junitxml="${MINI_ORK_RUN_DIR}/rev6-r2b-junit.xml"
```

## Required artifacts — declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/rev6-r2b-junit.xml`

## Success criteria

- The verification command passes; every pre-existing test is unchanged.
- `ruff check --select F,E9 mini_ork/review/findings.py mini_ork/review/lenses.py mini_ork/review/verdict.py mini_ork/review/gate.py mini_ork/pre_push_review.py` is clean.
- The migration applies twice on a copy of a DB without error:
  `mini_ork.stores.migrate.migrate_apply('db/migrations', db=<copy>)` run two
  times, once before and once after `findings.ensure_schema` has run on it.
- A failure anywhere in the lifecycle code leaves the review result exactly
  what it is today.
