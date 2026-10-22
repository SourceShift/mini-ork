# RSI gap: SimUtil-UCB ranking for the v2 pack's themes and findings

## Goal

RetroAgent's SimUtil-UCB (arXiv 2603.08561) ranks retrievable memory by
similarity plus measured utility plus an exploration bonus. mini-ork ships it
only for one channel (`mini_ork/memory/semantic.py` `_utility`, `_exploration`,
`rank_with_prior`, used by `context_assembler.semantic_lessons_md`). The live v2
pack (`mini_ork/context_v2.py:build`) orders verified themes by
`n_runs DESC, last_seen DESC` and finding clusters by severity/recency, with no
feedback from whether an injected item helped. The per-run injection ledger
(`runs/<id>/learned/<node>.json`, `sources[].id` naming pack item ids such as
`t:<theme_id>` and `f:<hash>`, plus `arm`/`held_out`) already records which items
each run was given; only the outcome loop is missing.

Close the loop: count, per pack item, how many delivered runs it reached and how
many of those runs succeeded, and order the v2 themes and finding clusters by a
SimUtil-UCB score.

All paths are relative to the repository root.

## Mechanism (exact spec)

1. New migration `db/migrations/0082_context_item_utility.sql` (this exact name
   is claimed for this worktree): table `context_item_utility` —
   `item_id TEXT PRIMARY KEY`, `uses INTEGER NOT NULL DEFAULT 0`,
   `wins INTEGER NOT NULL DEFAULT 0`, `last_used INTEGER`; table
   `context_item_runs` — `run_id TEXT PRIMARY KEY`, `counted_at INTEGER NOT NULL`
   (which runs were already counted). `CREATE TABLE IF NOT EXISTS`, self-record
   in `schema_migrations` like the neighbouring migrations.
2. New `mini_ork/learning/item_utility.py` (creates both tables if absent; every
   function swallows DB/IO errors and returns a neutral value):
   - `resolve_finished(home: str, db: str, *, limit: int = 200) -> int` — for
     `task_runs` rows with a terminal status (`published` counts as a win;
     `failed`, `rolled_back` as a loss; `not_needed` and anything else is
     skipped) that are not in `context_item_runs`: read
     `<home>/runs/<run_id>/learned/*.json`; skip the run when every record has
     `held_out == True`; collect the distinct `sources[].id` values; add 1 use
     (and 1 win when published) per distinct id; insert the run into
     `context_item_runs`. Returns the number of runs counted. A run with no
     `learned/` dir is still marked counted.
   - `score(similarity: float, uses: int, wins: int, n_total: int) -> float` —
     `similarity + W_UTILITY * (_utility(uses, wins) - UTILITY_NEUTRAL)
     + W_EXPLORE * _exploration(uses, n_total)` — the same formula as
     `semantic.rank_with_prior` — importing `W_UTILITY`, `W_EXPLORE`,
     `UTILITY_NEUTRAL`, `_utility` and `_exploration` from `mini_ork/memory/semantic.py`
     (do not copy the constants) so the two channels cannot drift. `n_total` is
     the sum of `uses` over the items being ranked.
   - `rerank(items: list[dict], *, db: str, similarity_key: str | None = None)
     -> list[dict]` — items carry an `"id"`; returns them ordered by `score`,
     using a base similarity of `1.0 - rank / len(items)` from the incoming
     order when `similarity_key` is None (so the existing order is the prior
     and utility/exploration adjust it). Stable for ties.
   - `enabled()`: `MO_ITEM_UCB` unset or not `"0"` → True (opt-out
     `MO_ITEM_UCB=0`).
3. `mini_ork/context_v2.py:build`: when `item_utility.enabled()`, call
   `resolve_finished` once (lazily, at most once per process per 60 s — keep a
   module-level timestamp) and pass `themes` and `clusters` through
   `item_utility.rerank` BEFORE the existing `clusters[:max_clusters]` cut, so
   the cut keeps the best-scored clusters. Severity-floor filtering stays
   before the rerank. The pack gains `"item_ranking": "simutil-ucb"` or
   `"prior-order"`. Any failure → the old order.

## Files in scope (touch ONLY these)

- `db/migrations/0082_context_item_utility.sql` (new)
- `mini_ork/learning/item_utility.py` (new)
- `mini_ork/context_v2.py`
- `tests/unit/test_item_utility.py` (new)

Do NOT modify any other file. Do not touch `mini_ork/memory/semantic.py`,
`mini_ork/learning/themes.py` or `mini_ork/context_assembler.py`.

## Tests (`tests/unit/test_item_utility.py`, hermetic temp home + sqlite)

- `resolve_finished` over three runs (published with `t:a`,`f:x`; failed with
  `t:a`; all-held-out published with `t:b`) → `t:a` uses 2 wins 1, `f:x` uses 1
  wins 1, `t:b` absent; a second call counts 0 runs;
- a run still `executing` is not counted and not marked;
- `rerank` with no stats keeps the incoming order; an item with 10 uses / 10
  wins moves above an item with 10 uses / 0 wins that was ahead of it;
- an item with 0 uses gets a positive exploration term (never ranked below an
  identical-prior item with many losses);
- `build` with `MO_ITEM_UCB=0` → identical pack ordering to the pre-change
  behaviour and `item_ranking == "prior-order"`;
- a corrupt `learned/*.json` → that file is skipped, the rest counted.

## Verification commands

- `pytest -q tests/unit/test_item_utility.py tests/unit/test_context_v2.py tests/unit/test_context_v2_wiring.py tests/test_semantic_memory_py.py`

## Self-application measurement

```measure
python3 -m pytest -q tests/unit/test_item_utility.py tests/unit/test_context_v2.py --junitxml="${MINI_ORK_RUN_DIR}/item-ucb-junit.xml"
```

## Required artifacts — declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/item-ucb-junit.xml`

## Success criteria

- The verification command passes with every pre-existing test unchanged.
- `ruff check --select F,E9 mini_ork/learning/item_utility.py mini_ork/context_v2.py` is clean.

## Rules

Edit files directly; do not emit unified diffs. Execute this spec exactly; do not
research and do not add lens or drafter nodes.
