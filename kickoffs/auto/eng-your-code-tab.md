# "Your code" tab — what mini-ork has learned about each part of your codebase

Plan: `docs/plans/2026-10-07-learning-memory-page-refactor.md`, engineer-first redesign, E3 (+ E6,
the reflect hook).

## Why

The user asked how a software engineer can use the Learning page at all. The answer is this tab:
"what has mini-ork learned about MY code, file by file, and how do I make it stick?"

The data is merged:
- `mini_ork.learning.code_findings`:
  - `harvest(home, db=…)`, incremental;
  - `areas(db=…, depth=3, since_days=30, limit=25)` → `[{area, n_findings, n_runs,
    worst_severity, top_categories, last_ts, files}]`;
  - `findings_for(path_prefix, db=…, limit=50)` → rows with `id, file, line, severity, category,
    issue, snippet, run_id, run_title, run status, ts`.
  - Live (backup DB): 993 runs, 2,146 findings, 1,664 with a file. `mini_ork/ide_pages/node.py`:
    146 findings / 23 runs, e.g. "BLOCKER node.py:2440-2460: the status pill is computed from the
    offset slice".
- **The keyword `category` is weak:** 113 of node.py's 146 findings are `other`. Recurring problems
  must come from grouping findings by similarity. Reuse the TF-IDF pieces of
  `mini_ork.learning.themes` (`normalize`, `_tokenize`, `_vector`, `_dot`, `_l2norm`, `_df_to_idf`).
  Import them; do not copy them.
- Path rules are merged: `mini-ork prefs set <key> <text> --scope path --target '<glob>'` stores a
  rule injected only into runs whose file scope matches. `mini-ork prefs preview <kickoff>` shows
  what a node will be told.
- Nothing harvests automatically yet.

## Files in scope (touch ONLY these)

- `mini_ork/ide_pages/learn/code.py` (new)
- `mini_ork/ide_pages/learn/__init__.py`: add the tab, make it the default
- `mini_ork/cli/reflect.py`: ONLY a new side-channel block calling `code_findings.harvest`
- `tests/unit/test_ide_pages_learn_code.py` (new); `tests/unit/test_ide_pages_learn.py` only for
  the default-tab assertion

Do NOT modify any other file.

## Changes (exact)

1. **Tabs.** `TABS = [("code", "Your code"), ("overview", "Overview"), ("lessons", "Lessons"),
   ("memory", "Memory"), ("improve", "Self-improve")]`, with `DEFAULT_TAB = "code"`.
2. **`code.recurring(findings, *, sim=0.35, top=5) -> list[dict]`** (pure): greedy-leader
   clustering of the findings' `issue` texts using themes' TF-IDF helpers, with IDF computed over
   the given findings. Each cluster: `{"representative": issue closest to the centroid, "n",
   "n_runs", "files": top 3, "worst_severity", "finding_ids"}`, ordered by `n` then severity.
   Strip file paths, line numbers and words like "BLOCKER" before vectorizing, so the same mistake
   in different files groups together.
3. **Section "Your code: what reviews found, by area"** (`S.table`, full width), from
   `areas(since_days=days)`. `days` comes from `args["days"]` (default 30; chips 7 / 30 / 90 via
   `S.set_args`).
   - Columns: `area | findings | runs | worst | recurring problems | last`.
   - "recurring problems" = the top 2 `recurring()` representatives of that area, each ≤ 70 chars,
     joined " · ".
   - Row `do = S.set_args(area=<area>)`, `sel` when selected.
   - Note: "From every reviewer and verifier finding in your runs. Click an area to see each
     finding and turn a recurring problem into a rule."
   - Empty: "No review findings yet. They appear after your runs are reviewed."
4. **Area detail** (when `args["area"]` is set), placed BEFORE the areas table:
   - `S.markdown` titled `f"{area}"`: one line "N findings in M runs, worst <sev>, last <date>",
     then "**Recurring problems**" with each cluster as `- **<n>×** <representative> (files: …)`.
   - `S.table` "Recurring problems": `problem | times | runs | worst | action`. The action is
     `S.btn("Make it a rule", S.cli("prefs", "set", f"review-{slug}", f"In {area}: avoid this
     recurring review finding — {representative}", "--scope", "path", "--target", glob,
     confirm=f"Add a rule for {glob}? Every run that touches it will be told this."))`, where:
     - `glob = area + "/**"` for a directory, else the file path;
     - `slug` = sha1(representative)[:8].
   - `S.table` "Findings": newest first, ≤ 50: `file:line | severity | problem | run`. Problem =
     issue[:120]. Row → `S.set_args(area=area, finding=<id>)`.
   - When `args["finding"]` is set: an `S.markdown` with the FULL issue, the snippet (fenced), and
     the run title + status, plus actions Open run (`S.open_run`), Open file
     (`S.open_path(<repo root>/<file>)` when it exists), Close (`S.set_args(finding="")`).
   - Close area → `S.set_args(area="", finding="")`.
5. **Freshness line.** A small `S.kv` at the top: "Harvested N runs · last <age> ago" from
   `code_findings_runs`, with the note "Updated after every reflect pass."
6. **Reflect hook (E6).** In `mini_ork/cli/reflect.py`, after the existing side-channels:
   `code_findings.harvest(mini_ork_home, db=db_path)`. Print `  [code_findings] harvested N run(s),
   F finding(s)`. Opt-out `MO_CODE_FINDINGS=0`. Fail-soft: catch, print `[code_findings]
   skipped: <exc>`.
7. Read-only page: the page never calls `harvest` (pages must not write).

## Tests (`tests/unit/test_ide_pages_learn_code.py`, temp home + DB seeded via `code_findings` tables)

- `recurring()`: "docstring contradicts the code" in 3 files plus 2 paraphrases → one cluster with
  n=5; an unrelated "query drops pending rows" stays separate; paths and "BLOCKER" don't block
  grouping.
- The default tab is `code`. The areas table rows, the days chips, and the empty state.
- `area=` → the markdown summary, the recurring table with a `prefs set … --scope path --target
  <glob>` CLI action and confirm, the findings table. `finding=` → the full issue + snippet +
  actions.
- The reflect hook is called once and is skipped with `MO_CODE_FINDINGS=0` (monkeypatch
  harvest).

## Verification command

The command that proves this run succeeded:

```bash
env -u MINI_ORK_RUN_ID -u MINI_ORK_DB -u MINI_ORK_HOME python3.11 -m pytest -q -p no:asyncio tests/unit/test_ide_pages_learn_code.py tests/unit/test_ide_pages_learn.py tests/unit/test_code_findings.py   # must exit 0
```

## Done when

- The verification command → 0 failed. Paste the summary line.
- `uvx ruff check mini_ork/ide_pages/learn/code.py mini_ork/ide_pages/learn/__init__.py mini_ork/cli/reflect.py tests/unit/test_ide_pages_learn_code.py` → clean.
- Live proof on a BACKUP of the live DB, harvested first:
  `cp`-style `sqlite3 backup` into a temp file, `code_findings.harvest(home, db=tmp)`, then
  `build_page` with `MINI_ORK_DB=tmp` for `learn/code` and `learn/code area=mini_ork/ide_pages`.
  Paste the areas rows and the recurring problems for ide_pages: real, readable groups, not
  "other". Delete the temp DB.
- `git diff --stat` touches only the files in scope.
