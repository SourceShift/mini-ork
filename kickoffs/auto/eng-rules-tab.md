# "Rules" tab — everything agents are told in this repo, and control over it

Plan: `docs/plans/2026-10-07-learning-memory-page-refactor.md`, engineer-first redesign, E4.
User rule in force: only verified learnings reach prompts.

## Why

An engineer needs one place that answers "what will the agents follow when they work on my code,
and how do I change that?". The parts exist, but nothing shows them together and there are no
controls:

- **Your rules:** `mini_ork.memory.preferences.list_prefs()`, scopes `global | task_class |
  workflow | path` (path = glob, injected only when a run's file scope matches). Injected FIRST in
  every researcher/implementer/reviewer prompt. The ledger counts each injection:
  `mini_ork.learning.ledger.injection_counts("preference", ids)`, ids
  `f"pref:{scope}:{target}:{key}"`.
- **Learned rules, verified:** `emergent_patterns` with `status='approved'` and non-blank
  `lesson_text` (38 live), injected under `--- Lessons from recurring patterns (prior runs) ---`;
  ledger kind `pattern`, id = `pattern_id`. 11 more are `proposed` with a lesson, waiting for the
  verification gate.
- **Preview:** `mini_ork/cli/prefs_cmd.py:_preview(kickoff_path, task_class, node, …)` already
  builds the exact learned block a node would get (read-only: it masks `MINI_ORK_RUN_ID`).
- **No way to forget a learned rule.** `emergent_patterns.status` CHECK allows
  `proposed|approved|rejected|superseded`.
- **BUG this phase must fix:** `reflection_pipeline.reflection_persist_suggestions`
  (`mini_ork/learning/reflection_pipeline.py` ~:515) writes
  `INSERT OR REPLACE INTO emergent_patterns (… status …) VALUES (…, 'proposed', …)` on every
  reflect pass. It resets every pattern to `proposed` and wipes `resolved_at`: an operator's
  `rejected` would be undone on the next reflect, and approval history is lost every pass.

## Files in scope (touch ONLY these)

- `mini_ork/ide_pages/learn/rules.py` (new)
- `mini_ork/ide_pages/learn/__init__.py`: add `("rules", "Rules")` right after `("code", "Your code")`
- `mini_ork/cli/prefs_cmd.py`: extract the body of `_preview` into a pure
  `build_preview(kickoff_text, task_class, node) -> (block, sources, task_class, paths)` that
  `_preview` calls (behaviour unchanged)
- `mini_ork/cli/lessons_cmd.py` (new); `mini_ork/cli/main.py`: ONE line `"lessons":
  "mini_ork.cli.lessons_cmd",` right after `"prefs"`; `tests/unit/test_native_dispatch_py.py`:
  ONE line `"lessons",` right after `"prefs",`
- `mini_ork/learning/reflection_pipeline.py`: ONLY the emergent_patterns write in
  `reflection_persist_suggestions`
- `tests/unit/test_ide_pages_learn_rules.py`, `tests/unit/test_lessons_cmd.py` (new); a
  regression test for the persist fix in `tests/unit/test_reflection_pipeline_py.py`

Do NOT modify any other file.

## Changes (exact)

1. **Persist keeps decisions.** Replace `INSERT OR REPLACE` with an upsert that updates the
   evidence columns (`cluster_label, member_item_ids_json, feature_set_json, strength_score,
   suggested_meta_adr, detected_at`) and fills `lesson_text` only when it's empty. It KEEPS
   `status`/`resolved_at` when the existing status is `approved`, `rejected` or `superseded`. New
   rows insert as `proposed`. Test: an approved row stays approved; a rejected row stays rejected
   after a persist pass; a new pattern is proposed.
2. **`mini-ork lessons`** (`lessons_cmd.main(argv)`, same handler shape as `prefs_cmd`):
   - `list [--json]`: approved and proposed patterns with lesson / status / seen-in-runs.
   - `forget <pattern_id>`: status → `rejected`, `resolved_at` = now. It stops being injected.
   - `restore <pattern_id>`: `rejected` → `approved`, only if `lesson_text` is non-blank, else
     exit 2.
   Unknown id → exit 2. DB resolution as in `preferences.py`.
3. **Rules tab** (`rules.py`, `sections(home, args, errors, *, now=None)`). Every section answers
   one question and has an action and an empty state:
   - **"Your rules"** (`S.table`): `rule | applies to | given to agents (7 days) | source`.
     - "applies to" = `everyone` / `task: <x>` / `files: <glob>`; given = ledger uses or
       `not yet`.
     - Row action Remove → `S.cli("prefs", "rm", key, "--scope", scope, "--target", target,
       confirm=…)` (file-sourced rules: Open file).
     - Section action "Add a rule" → `{"thread": "I want to add a rule for mini-ork agents in this
       repo. Ask me what it is and where it applies (everywhere, a task type, or certain files),
       then run mini-ork prefs set."}`.
     - Note: "Given first to every researcher, implementer and reviewer whose work it applies to.
       File rules apply only when the task touches those files."
     - Empty: "No rules yet. Add one, or turn a recurring review problem into a rule from the
       Your code tab."
   - **"Learned rules (verified)"** (`S.table`): `lesson | seen in | given to agents | `.
     - Lesson [:140]; seen in = distinct runs of the member traces; given = ledger uses (+ last
       date).
     - Row `do = S.set_args(rule=<pattern_id>)`.
     - Row actions: "Forget" → `S.cli("lessons", "forget", id, confirm="Stop giving this lesson
       to agents?")`; "Make it mine" → `S.cli("prefs", "set", f"learned-{id[:12]}", lesson,
       "--scope", "global", confirm=…)`.
     - Note: "Only verified lessons reach agents: seen across independent runs, with an
       authored lesson. N more are waiting for verification."
     - Empty state when none.
   - **Rule detail** (when `rule` is set, rendered first): `S.markdown` with the FULL lesson, the
     cluster label, "seen in N runs", the last 5 runs it was given in (ledger run ids → titles,
     each `S.open_run`), and Forget / Make it mine / Close.
   - **"What will an agent be told?"** (preview):
     - chips for the 8 most recent runs with a readable kickoff (`task_runs.kickoff_path` exists;
       chip label = kickoff title[:40]) → `S.set_args(preview=<run_id>)`;
     - node chips `implementer | reviewer | researcher` → `S.set_args(node=…)`;
     - when `preview` is set: `S.markdown` titled `f"{node} for: {title}"` with
       `build_preview(text, task_class_of_run, node)` (the exact block), or "Nothing would be
       given: no rules apply and no verified lessons match this task" when empty.
     - Read-only: no ledger writes.
4. The page never writes. All mutations go through the CLI actions.

## Tests

- `tests/unit/test_lessons_cmd.py`: list / forget / restore / unknown id; restore without a
  lesson → 2.
- `tests/unit/test_ide_pages_learn_rules.py` (temp home + DB):
  - your rules with injection counts, the applies-to labels and the Remove action;
  - learned rules rows, the detail markdown (full lesson), Forget / Make it mine CLI args;
  - preview of a seeded run kickoff, which equals `build_preview` output;
  - empty states; tab order `code, rules, …`.
- `test_reflection_pipeline_py.py`: the persist regression in change 1.

## Verification command

The command that proves this run succeeded (per file; the host kills one CPU-bound process that
runs longer than 30 s):

```bash
for f in tests/unit/test_ide_pages_learn_rules.py tests/unit/test_lessons_cmd.py tests/unit/test_reflection_pipeline_py.py tests/unit/test_native_dispatch_py.py tests/unit/test_prefs_path_scope.py tests/unit/test_ide_pages_learn.py; do env -u MINI_ORK_RUN_ID -u MINI_ORK_DB -u MINI_ORK_HOME python3.11 -m pytest -q -p no:asyncio "$f" || exit 1; sleep 3; done   # must exit 0
```

## Done when

- The verification command → 0 failed. Paste the summary lines.
- `uvx ruff check` on every touched Python file → clean.
- Live proof (read-only): `build_page(Path("/Volumes/docker-ssd/ps/mini-ork/.mini-ork"), "learn", "rules", {})`
  and with `preview=<a recent run id>`. Paste: learned rules (real lessons), and the preview
  block.
- `git diff --stat` touches only the files in scope.
