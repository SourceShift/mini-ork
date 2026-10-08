# `mini_ork_worktree.py merge` refuses to push unclaimed paths, confidential material or secrets to the public main

## Why (2026-10-08)

`SourceShift/mini-ork` is public (OSS). The user: "make sure no non-OSS file is committed and is
in main remote".

Today the main checkout's 144 untracked files moved into a task worktree (other sessions'
kickoffs, `n8n-reply.txt`, `evals/heldout/results/*`, a draft recipe). An in-place publisher
also committed whole files, sweeping another session's edits into a run's commit. An audit
found nothing non-OSS on `origin/main`. But nothing PREVENTS it: `merge_worktree`
(`scripts/mini_ork_worktree.py` ~:287) rebases, runs the green gate and does
`git push origin HEAD:main`. Whatever the branch carries goes public.

Two cheap, deterministic checks belong at that single choke point:

1. **Claims.** A worktree created with `--owns <path>` declares its file surface (the CAID
   registry, `OWNERSHIP_FILE`). A branch whose diff against `origin/main` touches paths outside
   its claims is carrying something it did not mean to.
2. **Confidentiality and secrets.** Added lines must not contain:
   - generic confidential-business patterns (fundraising, investor material, valuation,
     personal email), plus private terms from an untracked file;
   - credential shapes.

## Files in scope (touch ONLY these)

- `scripts/mini_ork_worktree.py`: ONLY `merge_worktree` + new helpers
- `tests/unit/test_merge_oss_guard.py` (new)

## Changes (exact)

1. **`_branch_paths(wt) -> list[str]`:** `git -C wt diff --name-only origin/main...HEAD`,
   after the rebase.
2. **Claims check.** When the slug has claims in `OWNERSHIP_FILE`:
   - every branch path must be covered by a claim (exact path or path-prefix, the same matching
     `create` uses for overlap);
   - a `kickoffs/auto/<slug>.md` path is always allowed;
   - otherwise refuse:
     `die("merge refused: <n> path(s) outside this worktree's claims: <paths…> — add --owns or drop them")`.
   - A slug with NO claims skips this check (old worktrees).
   - `MO_MERGE_ALLOW_UNCLAIMED=1` overrides, with a printed warning.
3. **Content check:** `git -C wt diff origin/main...HEAD` added lines (`+` lines, not `+++`).
   - Refuse on a case-insensitive match of the confidential regex:

     ```
     fundrais|investor|pitch[ -]?deck|venture capital|\bvaluation\b|seed round|term sheet|@gmail\.com
     ```

     plus extra terms read from an optional untracked file `.mini-ork/oss-guard-terms.txt`, one
     regex per line. That file is ignored if absent and is never committed: the names it holds
     are themselves sensitive.
   - The COMMITTED regex must stay generic. Specific product, company or person names belong
     ONLY in the private terms file, never in code, tests, docs or this kickoff.
   - Refuse on any credential shape:

     ```
     sk-[A-Za-z0-9_-]{24,}|ghp_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|AKIA[0-9A-Z]{16}|xox[bpa]-[A-Za-z0-9-]{10,}|-----BEGIN [A-Z ]*PRIVATE KEY
     ```

     except lines whose value contains `test`, `fake`, `dummy` or `example` (test fixtures such
     as `sk-test-value-never-shown-…`).
   - **The refusal** prints file:line and the matched term, NOT the full line (no echoing of a
     secret).
   - `MO_MERGE_ALLOW_OSS=1` overrides, with a warning. It is for a deliberate, reviewed public
     mention.
4. **Both checks run BEFORE the green gate**, so they are cheap and fail fast, and before
   `git push`. Nothing is pushed on refusal; the worktree stays as is.

## Tests (`tests/unit/test_merge_oss_guard.py`; temp origin + clone + worktree; stub the green gate with `MINI_ORK_TEST_CMD=true`)

- A branch touching only claimed paths → merge proceeds: the push reaches the temp origin.
- A branch also adding `n8n-reply.txt` (unclaimed) → refused, nothing pushed, and the message
  names the path.
- A branch adding a line `+ we are fundraising …` → refused, and the message shows the file:line
  and the term only.
- A branch adding `KEY = "sk-live-…"` (a real-shaped key) → refused. `sk-test-value-…` → allowed.
- `.mini-ork/oss-guard-terms.txt` containing `acmecorp` plus a line mentioning AcmeCorp →
  refused.
- `MO_MERGE_ALLOW_UNCLAIMED=1` / `MO_MERGE_ALLOW_OSS=1` → proceed, with a warning.

## Verification command

The command that proves this run succeeded (per file; the host kills one CPU-bound process
that runs longer than 30 s):

```bash
for f in tests/unit/test_merge_oss_guard.py tests/unit/test_worktree_script_py.py tests/unit/test_needs_you_truth.py; do env -u MINI_ORK_RUN_ID -u MINI_ORK_DB -u MINI_ORK_HOME python3.11 -m pytest -q -p no:asyncio "$f" || exit 1; sleep 3; done   # must exit 0
```

## Done when

- The verification command → 0 failed. Paste the summary lines.
- `uvx ruff check scripts/mini_ork_worktree.py tests/unit/test_merge_oss_guard.py` → clean.
- `git diff --stat` touches only the files in scope.
