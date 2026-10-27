# F1: review findings on S2's check-log parser and R2e's degraded-review fallback (low tier)

## Goal

Pre-push reviews 140 (R2e) and 141 (S2) raised these on main:

1. **`mini_ork/verify/check_log.py` reads `FAIL` anywhere in a section.**
   `_failed_bodies` marks a section failed when ANY body line, stripped, is
   `FAIL`. A passing section whose command output happens to contain a bare
   `FAIL` line (a pytest or tool summary) is reported as failing. The
   static-check verifier ends every section with its verdict line (`ok`,
   `FAIL`, or `skip: …`), so only the section's LAST non-blank line decides.
2. **A long failing section is cut from the top.** `failing_sections` keeps
   the header and the FIRST `max_lines_per_check - 1` body lines. A section
   that starts with a long command echo (`$ ruff check <5 paths>` plus its
   output) loses the finding lines, which the verifier prints just before the
   verdict.
3. **No byte cap.** The `_error_lines` / `_real_error_lines` paths this
   replaces cap their output; a section line can be arbitrarily long.
4. **A name filter that matches nothing drops every failure.** When
   `names` is given but no failed section's id is in it (a verifier names its
   checks differently in JSON and in the log), the result is `[]` and the
   caller falls back to error-shaped noise again.
5. **`mini_ork/review/gate.py`: a degraded fallback is approved silently.**
   When a cached review is degraded, the gate re-runs it; if that re-run
   raises, it falls back to the earlier review (`degraded_stale`), and later
   computes `lens_state = _llm_lenses_all_failed(review_id, db)` with the
   default `fresh=False`. That earlier review was degraded by definition, but
   with zero lens rows `fresh=False` reports `(False, 0)`, so no "no LLM lens
   ran" warning is printed and the secure tier does not block.

All paths are relative to the repository root.

## Mechanism (exact spec)

### 1. `mini_ork/verify/check_log.py`

1. **Verdict.** A section FAILED when its last non-blank line, stripped,
   equals `FAIL`. Body lines that are exactly `FAIL` elsewhere in the section
   do not count.
2. **Keep the tail.** For a failed section, drop the verdict line, then keep
   the header plus the LAST `max_lines_per_check - 1` body lines (the header
   still counts toward the cap). When lines were dropped, insert one line
   `  … (<n> earlier lines omitted)` right after the header, and keep the
   result within `max_lines_per_check` lines by dropping one more body line.
3. **Byte cap.** Truncate each returned line to 400 characters (append `…`
   when truncated). Stop adding lines once the total returned characters
   would exceed 8,000.
4. **Name filter fallback.** When `names` is given and NO failed section's
   id is in `names`, return the unfiltered failed sections instead of `[]`.
   When at least one id matches, keep only the matching ones, as today.
5. `max_lines` (total line cap) is unchanged.

### 2. `mini_ork/review/gate.py`

In the `except Exception:` branch that falls back to `degraded_stale`
(~620-628), set `lens_state = _llm_lenses_all_failed(review_id, db, fresh=True)`
right after `review_id, verdict = degraded_stale`, so the hybrid check below
sees the earlier review's real state (a zero-row review counts as degraded).
Nothing else in the gate changes.

## Files in scope (touch ONLY these)

- `mini_ork/verify/check_log.py`
- `mini_ork/review/gate.py`
- `tests/unit/test_check_log.py`
- `tests/unit/test_review_outside_lock.py`

Do not modify `mini_ork/recovery/auto_repair.py`, `mini_ork/cli/execute.py`, `scripts/mini_ork_worktree.py`, `docs/`, or any path in `config/kernel-frozen.yaml`. Every existing test must pass unchanged.

## Tests (hermetic, no LLM)

`tests/unit/test_check_log.py` (append):

1. A section whose body contains a bare `FAIL` line but whose last line is
   `ok` is not returned.
2. A failed section with 40 body lines where the finding is the last body
   line → the result (cap 20) contains that finding line and the
   `earlier lines omitted` marker, and has at most 20 lines.
3. A body line of 1,000 characters is returned as 401 characters ending in `…`.
4. `names=["not-in-the-log"]` with one failed section → that section is
   returned (fallback); `names=None` → all failed sections.

`tests/unit/test_review_outside_lock.py` (append), following the existing
gate fixtures in that file:

5. A cached review with zero lens rows in an existing `review_lens_runs`
   table, a re-run that raises, hybrid mode, simple tier → stdout contains
   `no LLM lens ran`.

## Verification commands

- `python3.11 -m pytest -q -p no:asyncio tests/unit/test_check_log.py tests/unit/test_review_outside_lock.py tests/unit/test_pre_push_gate.py tests/unit/test_pre_push_gate_degraded.py tests/unit/test_revise_error_lines.py tests/unit/test_auto_repair_verifier_evidence.py`

## Self-application measurement

```measure
python3.11 -m pytest -q -p no:asyncio tests/unit/test_check_log.py --junitxml="${MINI_ORK_RUN_DIR}/bif-f1-junit.xml"
```

## Required artifacts — declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/bif-f1-junit.xml`

## Success criteria

- The verification command passes; every pre-existing test is unchanged.
- `ruff check --select F,E9 mini_ork/verify/check_log.py mini_ork/review/gate.py tests/unit/test_check_log.py tests/unit/test_review_outside_lock.py` is clean.
- A passing section is never reported as failing, and a failing section's finding lines survive the cap.
