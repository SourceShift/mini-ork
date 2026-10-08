# IDE pages in Orca's language: Lanes as agent rows, Verify as checks, Automations and Recipes as boards

## Why

The plan is `/Users/admin/.claude/plans/twinkling-churning-feather.md`, P5. The run page, the
Inbox and the Board now speak Orca's visual language: `agents` rows, `checks` lists, `columns`
boards. The other pages are still plain `table` / `kv` / `list` dumps:

| page | today (spec level 2) |
|---|---|
| `lanes` | `table` "Lanes" + `list` Credentials + `list` BYO provider |
| `verify` | `kv` "Certify" + `table` "Recent certificates" |
| `autos` | `table` "Automations" |
| `recipes` | `table` "Catalog" + per-recipe `kv` / `flow` / `list` |

The helpers already exist in `mini_ork/ide_pages/spec.py`:
- `agent_row(...)` + `agents(title, rows)`;
- `checks(title, rows, summary=)` (rows like `run_story`/`node_changes` check rows);
- `columns(title, cols)` (cards: `{"id","title","sub","state","mark","meta","do"}`).

The IDE draws all three.

## Files in scope (touch ONLY these)

- `mini_ork/ide_pages/lanes.py`
- `mini_ork/ide_pages/verify.py`
- `mini_ork/ide_pages/autos.py`
- `mini_ork/ide_pages/recipes.py`
- `tests/unit/test_ide_pages_p5a_restyle.py` (new)

Do NOT touch any other file.

## Rule for every change

**Only at `spec.ide_level() >= 2`.** Level 1 output must stay byte-identical: an older IDE
reads it.
- **Add, don't remove.** Put the new section FIRST and keep the old table below it, now with
  `"title"` prefixed "All · …" where that reads better.
- **Fail-soft:** a broken new section never removes the page (`S.guarded`).

## Changes (exact)

1. **`lanes` (tab `lanes`):** an `agents` section "Lanes · last 24 h", one row per configured
   lane.
   - **Fields:**
     - `id` = the lane alias;
     - `lane` = its provider kind;
     - `model` = the configured model, if known;
     - `step` = `"<calls> calls · <failed> failed"`;
     - `last` = the lane's last error;
     - `cost` = `$usd`.
   - **State:**
     - `failed` when `failed / calls ≥ 0.5` and `calls ≥ 3`;
     - `running` when calls > 0;
     - else `pending`.
   - Read the 24-h numbers from `mini_ork.ide_pages.header._lanes(db)` (on main since
     `ee60c800`); reuse it, do not re-query.
   - `do` = none, or the existing lane detail action if the page has one.
2. **`verify` (tab `certify`):** a `checks` section "Certificates", one row per recent
   certificate.
   - **Fields:**
     - `name` = the run or target;
     - `state` = `pass` / `fail` / `na` from the certificate verdict;
     - `detail` = its one-line summary;
     - `do` = the existing open action.
   - Use the same row keys `spec.checks` expects.
3. **`autos` (tab `automations`):** a `columns` section "Automations" with columns **Enabled**,
   **Paused** and **Disabled** (map from the automation's state field).
   - Card `title` = the name, `sub` = the schedule, `meta` = the last run time/result.
   - Card `do` = the existing per-automation action.
4. **`recipes` (tab `recipes`):** a `columns` section "Recipes", grouped by the recipe's
   `task_class` family:
   - **Code:** code_fix, framework_edit, …;
   - **Research:** research, literature, lens …;
   - **Ops / other**, chosen from task_class keywords. Document the mapping in a dict.

   Card `title` = the recipe name, `sub` = its description (one line), `do` = the existing
   open-recipe action.

## Tests (`tests/unit/test_ide_pages_p5a_restyle.py`)

For each of the 4 pages:
- at level 2 (`monkeypatch.setenv("MINI_ORK_IDE_SPEC", "2")`), the first section has the new
  type (`agents` / `checks` / `columns` / `columns`) and the old table is still present;
- at level 1 (env unset), the section types equal the old list exactly.

Use temp-home fixtures like the existing page tests. When a page needs config (lanes,
recipes), point it at the repo's `config/` and `recipes/` the way those tests do.

For lanes: with a seeded `llm_calls` row for one lane, its row shows the calls/failed counts
and the failed state when ≥ 50% failed.

## Verification command

The command that proves this run succeeded (per file; the host kills one CPU-bound process
that runs longer than 30 s):

```bash
for f in tests/unit/test_ide_pages_p5a_restyle.py tests/unit/test_ide_pages_lanes.py tests/unit/test_ide_pages_verify.py tests/unit/test_ide_pages_recipes.py tests/unit/test_ide_pages_autos.py; do [ -f "$f" ] || continue; env -u MINI_ORK_RUN_ID -u MINI_ORK_DB -u MINI_ORK_HOME python3.11 -m pytest -q -p no:asyncio "$f" || exit 1; sleep 3; done   # must exit 0
```

## Done when

- The verification command → 0 failed. Paste the summary lines.
- `uvx ruff check` on the touched files → clean.
- **Live proof (read-only).** For each page, paste the first section's type and title from:

  ```
  MINI_ORK_IDE_SPEC=2 bin/mini-ork board page <lanes|verify|autos|recipes> --home /Volumes/docker-ssd/ps/mini-ork/.mini-ork
  ```
- `git diff --stat` touches only the files in scope.
