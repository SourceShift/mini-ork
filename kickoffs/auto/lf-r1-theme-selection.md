# LF-R1 verified themes: own task class first, member-class sets, honest header

## Goal

Both theme readers select `task_class = ? OR task_class = '*'` ordered by
`n_runs DESC`, so the 41 wildcard themes (mostly chapter_review "lens fan-out"
lessons) win every slot: for `framework_edit` the first own-class theme is rank
10, and fleet-wide 1622 wildcard injections compare with 20 own-class ones.
Separately, `themes._refresh_theme` writes `task_class = None` for a theme seen
in exactly two classes, so 32 verified themes can never be injected. And the
prompt calls these "verified on earlier runs of this task class", which is false
for `'*'` and overclaims: "verified" means authored + guardrails + support bar,
with no measured effect. Decision record:
`docs/plans/2026-10-15-learning-loop-fixes.md` (R1; decisions Q1, Q2).

All paths are relative to the repository root.

## Mechanism (exact)

1. `mini_ork/learning/themes.py`, `_refresh_theme`: a theme's `task_class` is its
   member-class set — the single class when there is one, otherwise the sorted
   classes joined by `,` (`code_fix,framework_edit`). Never `'*'`, never NULL
   when it has at least one member class (no member class → NULL, as today).
2. `themes.py`: add `reclassify_themes(db=None) -> int` that recomputes
   `task_class` the same way (via `_member_classes_con`) for every `kind='task'`
   theme whose `task_class` IS NULL or `'*'`, and returns how many rows changed.
   Idempotent; cold-safe (missing DB/table → 0, never raises). Call it once at
   the end of `assign_new` (fail-soft) so untouched legacy themes get repaired.
3. `themes.py`: add ONE shared selector
   `select_verified_themes(con, task_class, limit) -> list[dict]` returning
   `{theme_id, lesson_text, n_runs, task_class}` for rows with `status='verified'`,
   `kind='task'`, non-blank `lesson_text`, in three tiers:
   tier 0 `task_class` equals the class exactly; tier 1 the class is one member
   of a comma-joined set; tier 2 legacy `task_class = '*'`, only to fill
   remaining slots. Within a tier: `n_runs DESC, last_seen DESC`. NULL rows are
   never selected. Drop near-duplicates: a candidate whose word-token set
   (`themes._tokenize` on `lesson_text`) overlaps an already-selected one by
   `|A∩B| / min(|A|,|B|) >= 0.8` is skipped. Missing table/column → `[]`.
4. `mini_ork/context_v2.py` `verified_themes` and `mini_ork/context_assembler.py`
   `_verified_theme_lessons` both call `select_verified_themes`; their output
   shapes, ids (`t:<theme_id>`, source `kind: "theme"`) and cold-safety are
   unchanged.
5. Headers. `context_v2.render`: "Lessons verified on earlier runs of this task
   class:" becomes "Lessons induced from earlier runs (guardrail-checked; effect
   not yet measured):". The `context_assembler` block header
   `--- Verified lessons from prior runs (<class>) ---` becomes
   `--- Lessons induced from prior runs (<class>; guardrail-checked, effect not yet measured) ---`
   and its closing line `--- /verified lessons ---` becomes `--- /induced lessons ---`.
   The stored status value `verified` does NOT change. Update the tests that
   assert the old header strings.

## Files in scope

- `mini_ork/learning/themes.py`
- `mini_ork/context_v2.py`
- `mini_ork/context_assembler.py`
- `tests/unit/test_inject_verified_only.py`
- `tests/unit/test_themes.py`
- `tests/unit/test_theme_selection.py` (new)

## Tests (`tests/unit/test_theme_selection.py`, hermetic temp DB)

- nine `'*'` themes (n_runs 10–20) and two `framework_edit` themes (n_runs 3):
  `select_verified_themes(con, 'framework_edit', 5)` returns the two
  `framework_edit` themes first; `context_v2.verified_themes` and
  `_verified_theme_lessons` return the same theme ids in the same order;
- a theme with `task_class='code_fix,framework_edit'` is returned for
  `code_fix` and for `framework_edit` (tier 1) and not for `research_synthesis`;
- a NULL-class verified theme is never returned;
- three near-duplicate lens lessons (token overlap ≥ 0.8) yield at most one;
- `reclassify_themes` on a theme whose members span two classes writes the
  sorted pair, on a `'*'` theme with three member classes writes the triple,
  returns the count, and a second call returns 0;
- `_refresh_theme` on a two-class theme writes the pair, not NULL;
- the rendered v2 block and the v1 block carry the new headers.

## Verification commands

- `/Volumes/docker-ssd/ps/mini-ork/.venv/bin/python -m pytest -q tests/unit/test_theme_selection.py tests/unit/test_themes.py tests/unit/test_inject_verified_only.py tests/unit/test_context_v2.py tests/unit/test_context_v2_wiring.py`

## Self-application measurement

```measure
/Volumes/docker-ssd/ps/mini-ork/.venv/bin/python -m pytest -q tests/unit/test_theme_selection.py tests/unit/test_themes.py tests/unit/test_inject_verified_only.py tests/unit/test_context_v2.py tests/unit/test_context_v2_wiring.py --junitxml="${MINI_ORK_RUN_DIR}/lf-r1-junit.xml"
```

## Required artifacts — declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/lf-r1-junit.xml`

## Rules

Edit files directly; do not emit unified diffs. Never rebase, merge or reset
the worktree during the run. Do not change the stored `status` values, do not
add a migration, and do not touch `mini_ork/learning/code_findings.py`,
`mini_ork/cli/execute.py`, `mini_ork/cli/plan.py` or the live DB under
`/Volumes/docker-ssd/ps/mini-ork/.mini-ork/`.
