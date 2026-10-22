# RSI gap: Trace2Skill conflict check before a lesson becomes live

## Goal

Trace2Skill consolidates lessons distilled from many traces without letting a new
lesson silently duplicate or contradict an existing one. mini-ork writes lessons
in two places, both through the same grounded author
(`mini_ork/learning/pattern_induction.py:induce_cluster`):
`pattern_induction.induce_pending` (fills `pattern_records.lesson_text` and
`emergent_patterns.lesson_text`) and `mini_ork/learning/themes.py:promote` (sets
`lesson_themes.lesson_text` and `status='verified'`). Neither checks the new text
against the lessons already live for the same task class, so two contradictory
lessons can both reach a prompt, and near-duplicates pile up.

Add one conflict check between authoring and the write.

All paths are relative to the repository root.

## Mechanism (exact spec)

1. New migration `db/migrations/0083_lesson_conflicts.sql` (this exact name is
   claimed for this worktree): table `lesson_conflicts` — `id INTEGER PRIMARY KEY`,
   `source_kind TEXT NOT NULL` (`theme` | `pattern`), `source_id TEXT NOT NULL`,
   `against_kind TEXT NOT NULL`, `against_id TEXT NOT NULL`,
   `relation TEXT NOT NULL` (`duplicate` | `contradicts` | `refines`),
   `why TEXT`, `candidate_text TEXT`, `created_at INTEGER NOT NULL`; unique
   `(source_kind, source_id, against_kind, against_id)`. `CREATE TABLE IF NOT
   EXISTS`, self-record in `schema_migrations` like the neighbouring migrations.
2. New `mini_ork/learning/lesson_conflict.py` (creates the table if absent):
   - `live_lessons(con, task_class) -> list[dict]` — `{"kind", "id", "text"}` for
     `lesson_themes` rows with `status='verified'` and non-blank `lesson_text`,
     and `emergent_patterns` rows with non-blank `lesson_text`, whose task class
     equals `task_class` or is `'*'` / NULL / empty. Missing tables or columns →
     skip that source.
   - `neighbours(text, lessons, *, k=5, min_overlap=0.3)` — rank by token
     containment `|A∩B| / min(|A|,|B|)` over lowercase word tokens (implement it
     here; do not import private helpers from other modules); keep those
     `>= min_overlap`, top `k`.
   - `check(con, text, *, task_class, source_kind, source_id, dispatch_fn=None,
     model=None) -> dict` returning `{"verdict": "ok"|"duplicate"|"conflict"|"unchecked", "relations": [...]}`:
     - no neighbours → `ok`, no LLM call;
     - otherwise ONE call through the same dispatch path `induce_cluster` uses
       (pass `dispatch_fn`/`model` through; default to pattern_induction's
       default dispatch), asking for strict JSON
       `[{"id": ..., "relation": "duplicate|contradicts|refines|independent", "why": ...}]`
       for each numbered neighbour;
     - any `contradicts` → `conflict`; else any `duplicate` → `duplicate`; else
       `ok`. `refines` and `independent` never block;
     - an LLM failure or unparseable reply → `unchecked`;
     - every non-`independent` relation is written to `lesson_conflicts` (with
       the candidate text), idempotently on the unique key;
     - on `conflict`, enqueue one oversight item through
       `mini_ork.gates.oversight_inbox.enqueue` (`gate_id="lesson-conflict"`,
       `feature=f"lesson conflict: {source_kind} {source_id}"`), skipping it when
       a row with that gate_id and feature already exists in any status.
   - `enabled()`: `MO_LESSON_CONFLICT_CHECK` unset or not `"0"` → True.
3. `mini_ork/learning/pattern_induction.py:induce_pending` and
   `mini_ork/learning/themes.py:promote`: right after `induce_cluster` returns a
   non-empty text and before the UPDATE, when `lesson_conflict.enabled()`, call
   `check` (task class from the row; `source_kind` `pattern` / `theme`).
   - `ok` → write exactly as today.
   - `duplicate` or `conflict` → do not write; append a skip entry with reason
     `lesson duplicate` / `lesson conflict` to the report.
   - `unchecked` → do not write, reason `conflict check unavailable` (fail
     closed: the row stays unauthored and is retried on the next pass).
   - A raise inside `check` is treated as `unchecked`.

## Files in scope (touch ONLY these)

- `db/migrations/0083_lesson_conflicts.sql` (new)
- `mini_ork/learning/lesson_conflict.py` (new)
- `mini_ork/learning/pattern_induction.py`
- `mini_ork/learning/themes.py`
- `tests/unit/test_lesson_conflict.py` (new)

Do NOT modify any other file.

## Tests (`tests/unit/test_lesson_conflict.py`, hermetic temp sqlite; inject `dispatch_fn`, never call a real lane)

- `neighbours`: a near-restatement scores ≥ 0.3, an unrelated lesson is dropped;
- `check` with no live lessons → `ok` and `dispatch_fn` not called;
- a fake dispatch returning `contradicts` → `conflict`, one `lesson_conflicts`
  row, one oversight row; a second identical call adds neither;
- `duplicate` → `duplicate`; `refines` → `ok` with a `refines` row recorded;
- a dispatch that raises, and one that returns non-JSON → `unchecked`;
- `themes.promote` with a fake author + a fake conflict dispatch → the theme
  stays `candidate` with blank `lesson_text` and the report names
  `lesson conflict`; with an `ok` verdict → `verified` as before;
- `induce_pending` with a `duplicate` verdict → `pattern_records.lesson_text`
  stays blank;
- `MO_LESSON_CONFLICT_CHECK=0` → both writers behave exactly as before.

## Verification commands

- `pytest -q tests/unit/test_lesson_conflict.py tests/unit/test_pattern_induction_py.py tests/unit/test_themes.py tests/unit/test_themes_promote.py`

## Self-application measurement

```measure
python3 -m pytest -q tests/unit/test_lesson_conflict.py --junitxml="${MINI_ORK_RUN_DIR}/lesson-conflict-junit.xml"
```

## Required artifacts — declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/lesson-conflict-junit.xml`

## Success criteria

- The verification command passes with every pre-existing test unchanged.
- `ruff check --select F,E9 mini_ork/learning/lesson_conflict.py mini_ork/learning/pattern_induction.py mini_ork/learning/themes.py` is clean.

## Rules

Edit files directly; do not emit unified diffs. Execute this spec exactly; do not
research and do not add lens or drafter nodes.
