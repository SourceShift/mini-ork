# Memory tab — revision 2 (Opus review of run learn-memory-tab-20261007133743)

WIP commit 04dc1901 holds revision 1 (29 passed, ruff clean; live page renders all 3 sections).
Opus returned `needs_revision`. Fix ONLY these. The edit to `tests/unit/test_ide_pages_learn.py`
was accepted (its old memory test checked removed sections). `--reason` on `memory-lifecycle
--retire` is required and was handled correctly; keep it.

## Files in scope

- `mini_ork/ide_pages/learn/memory.py`
- `tests/unit/test_ide_pages_learn_memory.py`, `tests/unit/test_ide_pages_learn.py`

Do NOT modify any other file.

## Fixes (exact)

1. **BLOCKER, `memory.py:327-328`:** the `HAVING` clause drops every memory with even one pending
   use. On the live DB, 34 (memory, scope) groups qualify but only 9 show. Pending uses must
   simply not count. Filter on resolved uses (`wins + losses >= RETIRE_MIN_USES`), never on
   pending = 0.
2. **BLOCKER, `memory.py:324`:** `n` counts pending uses. `n` = wins + losses (resolved only),
   which is the same denominator as the win rate and the baseline.
3. **`memory.py:373`:** rows from `candidates()` show the smoothed utility, not the resolved win
   rate the baseline uses. Every row shows the resolved win rate, computed the same way as the
   baseline. If a candidate has no resolved uses, show `"—"` and `n=0`.
4. **`memory.py:421`:** the Δ cell is always red, even at +0 pt. Colour it red when Δ ≤ −5 pt,
   green when Δ ≥ +5 pt, else muted.
5. **`memory.py:57`:** `now` is dropped, so the 7-day "given to N nodes" window can't be frozen.
   Thread `now` from `sections(..., now=None)` through to the preferences section (`now or
   int(time.time())`), the way `overview.sections` does.
6. **Tests:**
   - A memory with 10 resolved + 3 pending uses at 60% in a 75% scope is listed with n=10 and
     Δ −15 pt.
   - A 0-pt Δ cell is muted.
   - The 7-day window is tested with a frozen `now` (one injection inside, one outside).
   - A candidate from `candidates()` shows its resolved win rate, not utility.

## Verification command

The command that proves this run succeeded:

```bash
python3.11 -m pytest -q -p no:asyncio tests/unit/test_ide_pages_learn_memory.py tests/unit/test_ide_pages_learn.py   # must exit 0
```

## Done when

- The verification command → 0 failed. Paste the summary line.
- `uvx ruff check mini_ork/ide_pages/learn/memory.py tests/unit/test_ide_pages_learn_memory.py` → clean.
- Live proof: `build_page(Path("/Volumes/docker-ssd/ps/mini-ork/.mini-ork"), "learn", "memory", {})`.
  "Memories to review" draws on all qualifying groups, not 9. Paste the section.
- `git diff 04dc1901 --stat` touches only the files in scope.
