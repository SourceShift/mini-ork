# LF-R4 "Make it a rule" creates a scoped, generalised DRAFT, not an operator order

## Goal

The "Your code" tab's "Make it a rule" button (`mini_ork/ide_pages/learn/code.py`
`_rule_action`) writes a path rule that is (a) scoped to the whole AREA glob
rather than the files the problem was found in, (b) the raw finding text,
repro strings included ("Reproduced: [False] PASSED, [True] FAILED."), and
(c) rendered by `mini_ork/memory/preferences.py` `render_block` under
"Operator preferences and constraints (set by the user; follow them)" — the
highest-authority block in a prompt. The one live example described a bug that
was already fixed on main. Decision record:
`docs/plans/2026-10-15-learning-loop-fixes.md` (R4).

All paths are relative to the repository root.

## Mechanism (exact)

1. Migration `db/migrations/0074_preference_origin.sql` (use the next free
   number on `origin/main` if 0074 is taken when you start) adds to
   `user_preference_memory`: `origin TEXT NOT NULL DEFAULT 'operator'`
   (`'operator'` | `'learned'`), `source_ids TEXT` (JSON array of the finding
   keys the rule came from, NULL for operator rules), and
   `active INTEGER NOT NULL DEFAULT 1`. `preferences.ensure_schema` /
   `_ensure_schema_con` add the same columns when missing (cold-safe, mirrors
   the migration exactly).
2. `preferences.py`: `set_pref(..., origin="operator", source_ids=None, active=True)`;
   `list_prefs` returns `origin`, `source_ids`, `active`; `prefs_for` returns only
   `active` rows; `render_block` renders two blocks: operator rules under the
   existing heading, and learned rules under
   `--- Learned rules (from review findings — check they still apply before following) ---`
   … `--- /learned rules ---`. A prompt with only one kind shows only that block.
   Add `approve_pref(key, *, scope, target, text=None, db=None) -> bool` that sets
   `active=1` and, when `text` is given, replaces the value.
3. `mini_ork/cli/prefs_cmd.py`: `prefs set` accepts `--origin learned`,
   `--source <id,id,…>` and `--draft` (stores `active=0`); new action
   `prefs approve <key> --scope … --target … [--text "…"]`; `prefs list` shows an
   origin column and marks drafts.
4. `code.py`, the button:
   - the target is computed from the cluster's MEMBER files (add an `all_files`
     list to each `recurring()` cluster): one file → that file; all members
     direct children of one directory → `<dir>/*`; otherwise all under a common
     directory of at least two segments → `<dir>/**`; otherwise no button (the
     action cell shows "—");
   - no button for a cluster with fewer than 2 members;
   - the text is a generalised draft: the representative with file:line
     references and severity words removed (`context_v2.clean_issue`), any
     sentence starting `Reproduced` and any `[True]`/`[False]`/`PASSED`/`FAILED`
     token removed, capped at 240 characters, prefixed
     `Reviewers repeatedly found in <target>: `;
   - the command is `prefs set review-<slug> <text> --scope path --target <glob>
     --origin learned --source <finding keys> --draft`, with a confirm string that
     says it creates an inactive draft to approve on the Rules tab.
5. `mini_ork/ide_pages/learn/rules.py`: drafts are listed with an "Approve"
   button (`prefs approve …`), and learned rules are labelled as learned.

## Files in scope

- `db/migrations/0074_preference_origin.sql` (new; renumber if taken)
- `mini_ork/memory/preferences.py`
- `mini_ork/cli/prefs_cmd.py`
- `mini_ork/ide_pages/learn/code.py`
- `mini_ork/ide_pages/learn/rules.py`
- `tests/unit/test_prefs.py`
- `tests/unit/test_prefs_path_scope.py`
- `tests/unit/test_ide_pages_learn_code.py`
- `tests/unit/test_prefs_learned_rules.py` (new)

## Tests

- `tests/unit/test_prefs_learned_rules.py`: a `--draft --origin learned` rule is
  listed but NOT returned by `prefs_for` and not rendered; after
  `approve_pref` it renders under the "Learned rules" heading, not under
  "set by the user"; an operator rule and a learned rule together render two
  blocks; `approve_pref(text=…)` replaces the value; `ensure_schema` on a DB
  created before the migration adds the three columns and keeps existing rows
  as `origin='operator'`, `active=1`.
- `tests/unit/test_ide_pages_learn_code.py`: a cluster whose members are all in
  `mini_ork/ide_pages/node.py` targets `mini_ork/ide_pages/node.py` (not
  `mini_ork/ide_pages/*`); `prefs_for(paths=['mini_ork/ide_pages/_cn.py'])` does
  not return that rule after approval; a one-member cluster has no button; the
  draft text for "The log proxy leaks … Reproduced: [False] PASSED, [True]
  FAILED." contains neither `Reproduced` nor `PASSED`.
- Search `tests/` for assertions on the "set by the user" heading and update
  only those that the two-block render changes.

## Verification commands

- `/Volumes/docker-ssd/ps/mini-ork/.venv/bin/python -m pytest -q tests/unit/test_prefs_learned_rules.py tests/unit/test_prefs.py tests/unit/test_prefs_path_scope.py tests/unit/test_ide_pages_learn_code.py`

## Self-application measurement

```measure
/Volumes/docker-ssd/ps/mini-ork/.venv/bin/python -m pytest -q tests/unit/test_prefs_learned_rules.py tests/unit/test_prefs.py tests/unit/test_prefs_path_scope.py tests/unit/test_ide_pages_learn_code.py --junitxml="${MINI_ORK_RUN_DIR}/lf-r4-junit.xml"
```

## Required artifacts — declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/lf-r4-junit.xml`

## Rules

Edit files directly; do not emit unified diffs. Never rebase, merge or reset
the worktree during the run. Do not change `prefs_for`'s path-glob semantics,
do not touch `mini_ork/context_v2.py`, `mini_ork/learning/`, `mini_ork/cli/execute.py`,
and do not write to the live DB under `/Volumes/docker-ssd/ps/mini-ork/.mini-ork/`.
