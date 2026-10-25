# ECOSYSTEM §6 · K4: remove the dead healer/cleaner/finalize chain, cache.py and epic auto-merge

## Goal

These modules have no production caller. They are bash-era ports that only
call each other:

| Module | Lines | Only callers |
|---|---:|---|
| `mini_ork/recovery/healer.py` | 381 | the chain below |
| `mini_ork/recovery/healer_bridge.py` | 469 | the chain |
| `mini_ork/recovery/cleaner.py` | 395 | the chain |
| `mini_ork/recovery/finalize.py` | 751 | nothing (no CLI verb, no `-m` call, no recipe) |
| `mini_ork/cache.py` | 214 | `finalize.py` only; `lookup`/`emit`/`record_hit` never called |
| `mini_ork/vcs/auto_merge.py` | 390 | `auto_merge()` from `finalize.py` only; **`content_ids` is live** (`mini_ork/cli/self_improve.py:624`) |
| `mini_ork/vcs/auto_merge_pr.py` | 130 | no importer |
| `mini_ork/vcs/pr_create.py` | 128 | `finalize.py` only |
| `mini_ork/dispatch/lane_helpers.py::aggregate_cache_stats` | — | `finalize.py` only |

The live replacements already exist: `mini-ork recover` / `repair`,
`mini_ork/recovery/auto_repair.py`, `workspaces.merge` + `board merge` +
`approvals`. Setting `MO_AUTO_MERGE` / `MO_OPEN_PR` today does nothing, yet the
settings UI and docs advertise them.

Delete the chain, keep `content_ids` in a small module of its own, and remove
the misleading claims.

All paths are relative to the repository root.

## Mechanism (exact spec)

1. Create `mini_ork/vcs/content_ids.py` containing `content_ids(repo, branch, target)`
   moved **verbatim** from `mini_ork/vcs/auto_merge.py`, plus the private
   `_git` helper it needs (copied verbatim). Module docstring: one paragraph
   saying it returns the (merge-base, head-tree) content id used by merge
   approvals.
2. `mini_ork/cli/self_improve.py`: change the import to
   `from mini_ork.vcs.content_ids import content_ids`. Nothing else changes.
3. Delete:
   - `mini_ork/recovery/healer.py`, `healer_bridge.py`, `cleaner.py`, `finalize.py`
   - `mini_ork/cache.py`
   - `mini_ork/vcs/auto_merge.py`, `auto_merge_pr.py`, `pr_create.py`
   - `tests/unit/test_healer_py.py`, `test_mo_healer_bridge_py.py`,
     `test_cleaner_py.py`, `test_finalize_py.py`, `test_cache_py.py`,
     `test_auto_merge_py.py`, `test_auto_merge_pr_py.py`, `test_pr_create_py.py`
4. `mini_ork/dispatch/lane_helpers.py`: delete `aggregate_cache_stats`, its
   entry in `__all__`, its docstring lines (`mo_aggregate_cache_stats`) and the
   JSON-shape comment that only serves it. In `tests/unit/test_lane_helpers_py.py`
   delete cases (g) and (h) and their docstring lines.
5. `tests/unit/test_merge_hold_for_review.py`: delete every `test_2_*`,
   `test_3_*`, `test_4_*` and `test_5_*` test and any fixture used only by them.
   Keep the `test_1_content_ids_*` tests, importing from
   `mini_ork.vcs.content_ids`.
6. `tests/unit/test_agent_session_isolation.py`: delete
   `test_cleaner_worker_argv_carries_isolation` only.
7. `mini_ork/settings_catalogue.py`: delete the `MO_AUTO_MERGE` entry (and an
   `MO_OPEN_PR` entry if one exists). If a test pins the catalogue's entry
   count or names, update that test in the same change.
8. Docs — remove or correct the false claims (`docs/FEATURES.md` is handled
   separately; do not touch it):
   - `docs/operator/review-gates.md`: the auto-merge / auto-PR / cleaner hold
     text (around lines 9-10 and 63-65); say merges go through
     `mini-ork board merge` / approvals.
   - `docs/RECURSIVE-MULTI-EPIC-LEARNING.md`: every instruction to set
     `MO_OPEN_PR=1` or `MO_AUTO_MERGE=1` (around lines 34-36, 100, 263,
     280-283). Replace with one sentence: merging an epic's branch is a manual
     `mini-ork board merge` step.
   - `docs/migration/parity-ports.md`: the healer/cleaner rows (around 44, 46):
     mark them "removed 2026-10 (no production caller)".

Keep the `mini_orch_sessions` table and migration 0002 (still used by
`gates/rubric_cache.py` and `ide_pages/context.py`). Do not touch the `healer`
lane role in configs.

## Files in scope (touch ONLY these)

- the files deleted in step 3
- `mini_ork/vcs/content_ids.py` (new)
- `mini_ork/cli/self_improve.py`
- `mini_ork/dispatch/lane_helpers.py`, `tests/unit/test_lane_helpers_py.py`
- `tests/unit/test_merge_hold_for_review.py`, `tests/unit/test_agent_session_isolation.py`
- `mini_ork/settings_catalogue.py` (and the one test that pins it, if any)
- `docs/operator/review-gates.md`,
  `docs/RECURSIVE-MULTI-EPIC-LEARNING.md`, `docs/migration/parity-ports.md`
- `tests/unit/test_eco6_k4_dead_chain_removed.py` (new)

## Tests (`tests/unit/test_eco6_k4_dead_chain_removed.py`, hermetic)

- `importlib.util.find_spec` is `None` for each of: `mini_ork.recovery.healer`,
  `mini_ork.recovery.healer_bridge`, `mini_ork.recovery.cleaner`,
  `mini_ork.recovery.finalize`, `mini_ork.cache`, `mini_ork.vcs.auto_merge`,
  `mini_ork.vcs.auto_merge_pr`, `mini_ork.vcs.pr_create`.
- `mini_ork.vcs.content_ids.content_ids` exists; on a tmp git repo with a `main`
  branch and a `feat` branch it returns `(merge_base_sha, tree_sha_of_feat)`
  that match `git merge-base` / `git rev-parse feat^{tree}`; on a missing
  branch it returns `("", "")`.
- `mini_ork.cli.self_improve` imports cleanly.
- `mini_ork.dispatch.lane_helpers` has no `aggregate_cache_stats` attribute.
- No file under `mini_ork/` contains any of the strings `recovery.healer`,
  `recovery.cleaner`, `recovery.finalize`, `vcs.auto_merge`, `vcs.pr_create`,
  `mini_ork.cache` (walk `mini_ork/**/*.py`).
- `mini_ork.settings_catalogue` has no entry named `MO_AUTO_MERGE`.

## Verification commands

- `python3.11 -m pytest -q -p no:asyncio tests/unit/test_eco6_k4_dead_chain_removed.py tests/unit/test_merge_hold_for_review.py tests/unit/test_agent_session_isolation.py tests/unit/test_lane_helpers_py.py tests/unit/test_self_improve_py.py tests/unit/test_settings_catalogue.py`

## Self-application measurement

```measure
python3.11 -m pytest -q -p no:asyncio tests/unit/test_eco6_k4_dead_chain_removed.py --junitxml="${MINI_ORK_RUN_DIR}/eco6-k4-junit.xml"
```

## Required artifacts — declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/eco6-k4-junit.xml`

## Success criteria

- The verification command passes. Any listed test file that does not exist is
  dropped from the command (do not create it).
- `ruff check --select F,E9 mini_ork/vcs/content_ids.py mini_ork/cli/self_improve.py mini_ork/dispatch/lane_helpers.py mini_ork/settings_catalogue.py` is clean.
- `git grep -n -E "recovery\.(healer|cleaner|finalize)|vcs\.(auto_merge|pr_create)|mini_ork\.cache\b" -- mini_ork tests scripts`
  returns nothing.
