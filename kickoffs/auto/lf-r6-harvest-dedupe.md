# LF-R6 one code finding per run, not one per review file

## Goal

Since 2026-10-12 14:39 every run with structured reviewer findings stores each
finding twice in `code_findings`: once as `review:reviewer` and again as
`review:verdict`, because `review-verdict.json` restates the reviewer's
findings and the dedup fingerprint includes `source`
(`mini_ork/learning/code_findings.py` `_fingerprint`, used in `_harvest_run`).
The live DB has 38 such pairs across 14 of 14 recent runs. The "Your code" tab
and context v2 then count each problem twice. Decision record:
`docs/plans/2026-10-15-learning-loop-fixes.md` (item R6).

All paths are relative to the repository root.

## Mechanism (exact)

1. In `_harvest_run`, dedupe WITHIN one run before insert, on the key
   `(normalized file, line, issue.strip())`, across `review:*` sources only.
   Process `review:verdict` findings after every other `review:*` source, so a
   finding the reviewer already reported is kept once as `review:reviewer`
   (or whichever non-verdict review role reported it) and the verdict echo is
   skipped. A finding that appears ONLY in `review-verdict.json` is still
   inserted. `verifier:*` rows are not deduped against each other or against
   reviews (two verifiers failing is two signals).
2. `_fingerprint` does NOT change, so rows already in the table keep their
   identity and an explicit re-harvest (`harvest(home, run_ids=[…])`) of an old
   run stays a no-op.
3. Add `prune_duplicates(db=None) -> int`: delete every `review:verdict` row for
   which another `review:*` row with the same `run_id`, `IFNULL(file,'')`,
   `IFNULL(line,-1)` and `issue` exists. Same cold-safe, never-raises contract as
   `prune_receipts`; idempotent (a second call returns 0); returns the count.
4. Expose it on the module CLI as `prune-duplicates` next to `prune`.
5. In `mini_ork/cli/reflect.py`, inside the existing `MO_CODE_FINDINGS` block and
   right after the `harvest(...)` call succeeds, call
   `code_findings.prune_duplicates(db_path)` in the same fail-soft `try` and print
   `  [code_findings] pruned N duplicate(s)` only when N > 0.

## Files in scope

- `mini_ork/learning/code_findings.py`
- `mini_ork/cli/reflect.py`
- `tests/unit/test_code_findings_dedupe.py` (new)

## Tests (`tests/unit/test_code_findings_dedupe.py`, hermetic, temp DB + temp run dirs)

- a run dir with `review-reviewer.json` and `review-verdict.json` that both
  carry the same structured finding (same file, line, issue) harvests to exactly
  ONE row whose `source` is `review:reviewer`;
- a finding present only in `review-verdict.json` is harvested (one row,
  `review:verdict`);
- harvesting the same run again with `run_ids=[run]` inserts nothing;
- two `verifier:*` files failing with the same reason keep both rows;
- `prune_duplicates` on a seeded table with one reviewer/verdict twin pair and
  one verdict-only row deletes exactly the twin, keeps the verdict-only row, and
  returns 1; a second call returns 0; a missing DB returns 0 without raising.

## Verification commands

- `/Volumes/docker-ssd/ps/mini-ork/.venv/bin/python -m pytest -q tests/unit/test_code_findings_dedupe.py tests/unit/test_code_findings.py`

## Self-application measurement

```measure
/Volumes/docker-ssd/ps/mini-ork/.venv/bin/python -m pytest -q tests/unit/test_code_findings_dedupe.py tests/unit/test_code_findings.py --junitxml="${MINI_ORK_RUN_DIR}/lf-r6-junit.xml"
```

## Required artifacts — declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/lf-r6-junit.xml`

## Rules

Edit files directly; do not emit unified diffs. Never rebase, merge or reset
the worktree during the run. Do not touch `mini_ork/context_v2.py`,
`mini_ork/ide_pages/`, any migration, or the live DB under
`/Volumes/docker-ssd/ps/mini-ork/.mini-ork/`.
