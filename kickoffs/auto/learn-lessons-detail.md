# Lessons tab — open any learning and read it in full

## Why

The user asked: in Learning & memory → Lessons, it must be possible to inspect each learning in
full. Today (`mini_ork/ide_pages/learn/lessons.py`) every item is cut short and nothing opens:

- **Gradients:** a table of the newest 15 rows, each one cell `short(f"{target} · {signal}")`
  (120 chars). `suggested_change` is never shown, the "injected" column is always `—`, and there
  are no row actions.
- **Patterns:** `short(cluster_label, 80)`. The authored `lesson_text` (52 of 57 patterns have
  one since today's backfill) is never shown.
- **Failure modes:** category · stage plus counts. The error messages are never shown.

Everything needed for a full view exists:
- `gradient_records`: `gradient_id, target, signal, suggested_change, evidence` (a trace id),
  `confidence, task_class, created_at` (epoch s).
- `execution_traces.trace_id` → `run_id`; run title = the first `# ` heading of
  `task_runs.kickoff_path`.
- Themes: `gradient_theme(gradient_id, theme_id, similarity)` and `lesson_themes(theme_id, kind,
  role, task_class, representative, n_gradients, n_runs, first_seen, last_seen, lesson_text,
  status)` (live since today).
- `emergent_patterns` (`pattern_id, cluster_label, lesson_text, strength_score, status,
  member_item_ids_json, detected_at, resolved_at`) and `pattern_records(pattern_id, description,
  lesson_text, evidence_trace_ids, frequency)`.
- `failure_memory(failure_id, run_id, workflow_stage, failure_category, error_message,
  occurred_at)`.
- Injection counts: `mini_ork.learning.ledger.injection_counts(source_kind, ids)`.

The page renderer supports `S.markdown(title, text, path=None)` (shown whole and scrollable),
and table rows `{"cells", "do", "sel"}` with `S.set_args(...)`.

## Files in scope (touch ONLY these)

- `mini_ork/ide_pages/learn/lessons.py`
- `tests/unit/test_ide_pages_learn_lessons.py` (new)

Do NOT modify any other file. Use `mini_ork.ide_pages.spec`, `mini_ork.ide_pages.learn._common`
and `mini_ork.learning.ledger` (read-only functions).

## Changes (exact)

1. **Every row opens.** The `item` page arg selects what's shown in full:
   - gradient rows: `do = S.set_args(item=f"g:{gradient_id}")`
   - pattern rows: `do = S.set_args(item=f"p:{pattern_id}")`
   - failure-mode rows: `do = S.set_args(item=f"f:{category}|{stage}")`

   The selected row has `sel: True`. Convert Patterns and Failure modes to `S.table` so rows can
   carry `do`/`sel`.
2. **Detail panel**, rendered FIRST when `args["item"]` is set. It's a full-width `S.markdown`
   section with the complete text (never truncated), followed by a `S.kv` of facts with actions:
   - **Gradient** (title: `f"Learning · {target}"`). Markdown:
     ```
     **What was observed**
     <signal, full>

     **What to change**
     <suggested_change, full>
     ```
     kv:
     - confidence, task class, recorded (`YYYY-MM-DD HH:MM`)
     - evidence = run title + run id (via the trace)
     - theme = representative[:160] + `f"{n_gradients} similar notes across {n_runs} runs"` +
       kind (`about mini-ork itself` / `about the task`)
     - given to agents = `injection_counts("gradient", [id])` uses + last date, or `"not given to
       any agent yet"`

     Actions: `S.btn("Open run", S.open_run(run_id, title))`, `S.btn("Close", S.set_args(item=""), "ghost")`.
     Below it, an `S.table` "Similar notes in this theme": up to 8 other members (newest first,
     signal[:140], each row opens that gradient).
   - **Pattern** (title: `f"Pattern · {pattern_id}"`). Markdown: `**Lesson**` + full `lesson_text`
     (or "No lesson authored yet — this pattern is only a frequency count." + the cluster label),
     then `**Cluster**` + the full cluster label. kv: status, strength, evidence runs (count of
     distinct runs over its member traces), given to agents (`injection_counts("pattern", …)`).
     Close action.
   - **Failure mode** (title: `f"Failure mode · {category} · {stage}"`). Markdown: the 5 most
     recent full `error_message`s, each under a `**<run title> · <date>**` heading (in a fenced
     code block if multi-line). kv: count, runs, last seen, recovery that worked (existing logic).
     Each of those runs gets an `Open run` action (≤ 5). Close action.
   - Unknown or stale `item` → a small `S.lst` "That learning no longer exists" with Close.
3. **More rows.**
   - Gradients: 25 per page with `offset` paging. Section actions "Newer" / "Older" →
     `S.set_args(goff=…)`, shown only when applicable. The note says "showing a–b of N".
   - Gradient cell text = signal only (target moves to its own 140 px column), up to 160 chars.
   - The "injected" column shows real counts from the ledger (`—` when 0).
   - Patterns: 20 rows, title = `lesson_text` when present, else the cluster label marked
     `(no lesson)`.
4. Every query tolerates a missing table or column (`has_table` / try), as the module does today.

## Tests (`tests/unit/test_ide_pages_learn_lessons.py`, temp home + DB; seed the tables above)

- A gradient row's `do` is `{"set": {"item": "g:<id>"}}`. With `item=g:<id>` the first section is
  markdown containing the FULL signal and suggested_change (seed strings longer than 500 chars;
  assert they appear unabridged), the kv shows the run title from the kickoff heading, the theme
  line and the injection count. Open run / Close actions are present. Similar notes lists the
  other theme members.
- Pattern detail shows the full `lesson_text`; one without a lesson shows the "only a frequency
  count" sentence.
- Failure-mode detail shows the full multi-line error messages and Open run actions.
- Paging: 30 seeded gradients → first page 25 with an "Older" action; `goff=25` → 5 rows with a
  "Newer" action.
- Unknown item → "That learning no longer exists".
- Selected row has `sel: True`.

## Verification command

The command that proves this run succeeded:

```bash
python3.11 -m pytest -q -p no:asyncio tests/unit/test_ide_pages_learn_lessons.py tests/unit/test_ide_pages_learn.py   # must exit 0
```

## Done when

- The verification command → 0 failed. Paste the summary line.
- `uvx ruff check mini_ork/ide_pages/learn/lessons.py tests/unit/test_ide_pages_learn_lessons.py` → clean.
- Live proof (read-only): `build_page(Path("/Volumes/docker-ssd/ps/mini-ork/.mini-ork"), "learn", "lessons", {})`,
  then pick the first gradient row's `do.set.item` and render again with it. Paste the detail
  markdown text and kv. It must show the complete signal and suggested change of a real gradient.
- `git diff --stat` touches only the files in scope.
