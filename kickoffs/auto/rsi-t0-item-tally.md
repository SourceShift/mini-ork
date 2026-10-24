# RSI Tier-0 #1: item-utility tally reads the newest finished runs first

## Goal

The v2 context pack reranks items with SimUtil-UCB from the
`context_item_utility` tally. In the primary home that table has 0 rows, yet
731 finished runs are untallied. The cause is
`mini_ork/learning/item_utility.py:resolve_finished`:

- It selects un-counted terminal runs `ORDER BY created_at, id LIMIT 200`,
  which is **oldest first**.
- It is called once per pack build (`mini_ork/context_v2.py:775`).
- So each call spends its whole 200-run window on pre-v2 runs that have no
  `learned/` ledger. It marks them counted with no ids, and the v2 runs that
  carry item ids are reached only after the entire history has been walked.

Count the newest runs first, so the evidence that can move the rerank is
counted first.

All paths are relative to the repository root.

## Mechanism (exact spec)

1. `mini_ork/learning/item_utility.py`, `resolve_finished`: change the
   selection order to `ORDER BY created_at DESC, id DESC`. Change nothing else
   in the query:
   - the same terminal statuses;
   - the same `NOT IN (SELECT run_id FROM context_item_runs)` exclusion;
   - the same `LIMIT ?`.
2. Update the docstring of `resolve_finished` to state that runs are counted
   newest first, and why (one sentence: the window must reach runs that carry
   ledgers).
3. Nothing else changes:
   - the marking behaviour (mark first; an all-held-out run is marked and not
     counted);
   - the win rule (`published` is a win);
   - fail-open on errors;
   - the return value (the number of runs counted).

## Files in scope (touch ONLY these)

- `mini_ork/learning/item_utility.py`
- `tests/unit/test_item_utility.py` (only if an existing assertion pins
  oldest-first order; update that assertion and nothing else)
- `tests/unit/test_item_utility_newest_first.py` (new)

Do NOT modify any other file.

## Tests (`tests/unit/test_item_utility_newest_first.py`: hermetic temp home + sqlite, no network)

Build a temp `home` and a temp `db`. In the db, create a minimal
`task_runs(id TEXT PRIMARY KEY, status TEXT, created_at INTEGER)` table and
insert 30 runs:

- **20 old runs** `old-00` … `old-19`: `created_at` 1000 … 1019, status
  `failed`, and no `learned/` directory under `home/runs/<id>`.
- **10 new runs** `new-00` … `new-09`: `created_at` 5000 … 5009, status
  `published`. Each has `home/runs/<id>/learned/planner.json` containing
  `{"held_out": false, "sources": [{"id": "t:<id>"}]}`.

Tests:

- `resolve_finished(home, db, limit=10)` returns 10.
  - `context_item_utility` then holds exactly the 10 ids `t:new-00` …
    `t:new-09`, each with `uses = 1` and `wins = 1`.
  - None of the `old-*` runs is in `context_item_runs`.
- A second call `resolve_finished(home, db, limit=10)` counts the next 10 newest
  runs. Those are old runs with no ledger, so it returns 10 and adds no new
  `context_item_utility` rows.
  - `context_item_runs` then holds 20 run ids.
- A third call with `limit=100` returns 10 (the remaining old runs).
  - A fourth call returns 0.
- Two runs with the same `created_at` are ordered by `id DESC`: the run whose id
  sorts last is counted first when `limit=1`.

## Verification commands

- `pytest -q tests/unit/test_item_utility_newest_first.py tests/unit/test_item_utility.py tests/unit/test_context_v2.py tests/unit/test_context_v2_wiring.py`

## Self-application measurement

```measure
python3 -m pytest -q tests/unit/test_item_utility_newest_first.py --junitxml="${MINI_ORK_RUN_DIR}/item-tally-junit.xml"
```

## Required artifacts — declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/item-tally-junit.xml`

## Success criteria

- The verification command passes, and every pre-existing test is unchanged
  unless it pinned oldest-first order.
- `ruff check --select F,E9 mini_ork/learning/item_utility.py` is clean.

## Rules

Edit files directly; do not emit unified diffs. Execute this spec exactly; do not
research and do not add lens or drafter nodes.
