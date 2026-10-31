# W1-b: an unreadable lens record must make the pre-push review degraded, not clean

## Goal

In a hybrid pre-push review, `_llm_lenses_all_failed` (`mini_ork/review/gate.py`)
decides whether the LLM lens panel actually ran. `(True, n)` makes the review
degraded:
- it is not cached, and it is re-run;
- the gate warns in the simple tier;
- the gate refuses the push in the secure tier.

On any exception the function currently returns `(False, 0)`, which reads as
"the lenses ran fine". A locked or corrupt state DB at that moment therefore lets
a review whose whole panel failed be cached and approved on heuristics alone. That
is a fail-open path in a safety gate.

A missing `review_lens_runs` table is NOT this case. `_lens_runs` returns `[]` for
it without raising, and `(False, 0)` stays correct there (pinned by
`test_all_failed_missing_table_is_false_even_fresh`). Only a real read error
reaches the `except`.

Fix: a read error reports `(True, 0)`, i.e. degraded.

Read `mini_ork/review/gate.py` (`_lens_runs`, `_lens_table_exists`,
`_llm_lenses_all_failed` and its callers in the gate) and
`tests/unit/test_pre_push_gate_degraded.py` first.

## Mechanism (exact spec)

### `mini_ork/review/gate.py`, in `_llm_lenses_all_failed`

- The `except Exception:` branch returns `(True, 0)` instead of `(False, 0)`.
- Change its inline comment to `# noqa: BLE001 — unreadable evidence is degraded, not clean`.
- Append one paragraph to the docstring: "A read error (a locked or corrupt DB) reports
  ``(True, 0)``. The lens evidence cannot be read, so the review is treated as degraded:
  it is warned in the simple tier, refused in the secure tier, and never cached. A missing
  table still reports ``(False, 0)``, because ``_lens_runs`` returns ``[]`` for it without raising."

No other line changes.

## Files in scope (touch ONLY these)

- `mini_ork/review/gate.py`
- `tests/unit/test_pre_push_gate_degraded.py` (append tests; change no existing test)

Do NOT modify any other file.

## Tests (append to `tests/unit/test_pre_push_gate_degraded.py`, reusing its fixtures and helpers)

- `test_all_failed_read_error_is_degraded`: monkeypatch `gate_mod._lens_runs` to raise
  `sqlite3.OperationalError("database is locked")`. Assert
  `gate_mod._llm_lenses_all_failed(1, state.db) == (True, 0)`, and the same with `fresh=True`.
- `test_read_error_review_is_not_cached`: mirror `test_degraded_review_is_not_cached_and_reruns`.
  Use a review whose lens rows are all `ok`, with `gate_mod._lens_runs` patched to raise.
  Assert the cache file gets no entry for the patch.
- `test_read_error_secure_tier_blocks`: mirror `test_empty_panel_secure_tier_blocks`, with
  `gate_mod._lens_runs` patched to raise. Assert the gate refuses (rc != 0), exactly as
  that test does.

## Verification commands

- `python3 -m pytest -q -p no:asyncio tests/unit/test_pre_push_gate_degraded.py tests/unit/test_pre_push_gate.py tests/unit/test_pre_push_gate_lens_env.py tests/unit/test_pre_push_review_py.py`
- `ruff check --select F,E9 mini_ork/review/gate.py tests/unit/test_pre_push_gate_degraded.py`

## Self-application measurement

```measure
python3 -m pytest -q -p no:asyncio tests/unit/test_pre_push_gate_degraded.py --junitxml="${MINI_ORK_RUN_DIR}/w1-review-gate-junit.xml"
```

## Required artifacts: declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/w1-review-gate-junit.xml`

## Success criteria

- All verification commands pass, and every pre-existing test is unchanged.
- A healthy review (readable rows, at least one `ok` lens) is cached exactly as today.

## Rules

Edit files directly; do not emit unified diffs. Execute this spec exactly; do not
research and do not add lens or drafter nodes.
