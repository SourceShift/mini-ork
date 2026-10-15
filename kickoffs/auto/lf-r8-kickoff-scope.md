# LF-R8 one kickoff-scope parser, and a Do-NOT parser that reads sentences

## Goal

Three readers parse a kickoff's file scope three different ways, and two are
wrong:

- `gen_profile` (`mini_ork/cli/main.py`) builds `scope_allow` from every heading
  whose title merely CONTAINS "scope", so `## Out of scope` and `## Explicitly
  out of scope — do NOT touch` feed the ALLOW list (they also feed `scope_deny`).
- `paths_in_text` (`mini_ork/memory/preferences.py`), used by `scope_paths` and by
  `context_v2.parse_contract`, accepts only backticked tokens, so a "Files in
  scope" section of bare bullets (`- mini_ork/foo.py`) parses to `[]`: 38 of 257
  headed kickoffs in the repo parse empty.
- `context_v2.parse_contract`'s Do-NOT scan takes any line that starts with
  "never"/"do not", so a wrapped continuation ("never raises into the run.")
  becomes a hard constraint, while a real rule mid-line ("Edit files directly;
  do not emit unified diffs. Do not touch escalation_policy.py, …") is missed.

Decision record: `docs/plans/2026-10-15-learning-loop-fixes.md` (R8).

All paths are relative to the repository root.

## Mechanism (exact)

1. New module `mini_ork/kickoff_scope.py`, pure (no I/O):
   - `heading_kind(title) -> "allow" | "deny" | None`: `deny` when the lowercased
     title contains any of `out of scope`, `out-of-scope`, `not in scope`,
     `outside scope`, `non-goal`, `scope deny`, `forbidden`, `do not touch`;
     otherwise `allow` when it contains `files in scope`, `scope allow`,
     `in scope`, or the word `scope`; otherwise None. Deny wins.
   - `paths_in_line(line) -> list[str]`: backticked path-like tokens (the
     existing `_looks_like_path` rule, glob targets ending `**` excluded) PLUS a
     bare bullet head: when the line is a list item whose first token (up to
     whitespace, ` —`, ` (` or `:`) contains `/` or ends in a `.ext`, that token.
     Prose words are never paths.
   - `kickoff_scope(text) -> {"allow": [...], "deny": [...]}`: paths from allow
     and deny `##`–`######` sections, outside fenced code, normalized (`./`
     stripped), de-duplicated, order kept; a path in both lists stays only in deny.
   - `do_not_sentences(text) -> list[str]`: outside fences, join each list item
     with its continuation lines (indented, or flush-left without a blank line
     between) and each paragraph into one logical line; split into sentences on
     `(?<=[.!?])\s+`; keep sentences that start (after `**`/`__`/backticks) with
     `do not`, `don't`, `never` or `must not` (case-insensitive); strip markup;
     cap 200 chars; de-duplicate; keep order.
2. `gen_profile`: `scope_allow` comes only from sections whose `heading_kind` is
   `allow`; `scope_deny` from `deny` sections. Entry format is unchanged (raw
   bullet lines), so existing profile consumers keep working.
3. `preferences.scope_paths` and `paths_in_text` use `kickoff_scope.paths_in_line`,
   so bare-bullet scope entries count.
4. `context_v2.parse_contract`: `files_in_scope` = `kickoff_scope(text)["allow"]`;
   `out_of_scope` keeps its bullets from deny sections; `do_not` =
   `do_not_sentences(text)` (still capped at 10). Its return keys are unchanged.

## Files in scope

- `mini_ork/kickoff_scope.py` (new)
- `mini_ork/cli/main.py`
- `mini_ork/memory/preferences.py`
- `mini_ork/context_v2.py`
- `tests/unit/test_kickoff_scope.py` (new)
- `tests/unit/test_context_v2.py`
- `tests/unit/test_prefs_path_scope.py`
- `tests/unit/test_run_profile_bullets.py`

## Tests (`tests/unit/test_kickoff_scope.py`, hermetic, inline fixture kickoffs)

- a kickoff with `## Files in scope` of four BARE bullets yields those four
  paths from `kickoff_scope`, `parse_contract` and `scope_paths` (via a temp
  profile written by `gen_profile`) — the three callers agree;
- `## Explicitly out of scope — do NOT touch` with backticked paths: those paths
  are only in `deny`, never in `allow`, never in the profile's `scope_allow`;
- a function-contract bullet wrapped so a line reads `    never raises into the
  run.` yields NO do-not sentence for that fragment, while `Edit files directly;
  do not emit unified diffs. Do not touch escalation_policy.py, execute.py, or
  any recipe.` yields "do not emit unified diffs." and "Do not touch
  escalation_policy.py, execute.py, or any recipe." (two sentences);
- a fenced block containing `## Files in scope` and `- fake.py` contributes nothing;
- prose bullets ("Keep the change small") are not paths.

## Verification commands

- `/Volumes/docker-ssd/ps/mini-ork/.venv/bin/python -m pytest -q tests/unit/test_kickoff_scope.py tests/unit/test_context_v2.py tests/unit/test_prefs_path_scope.py tests/unit/test_run_profile_bullets.py`

## Self-application measurement

```measure
/Volumes/docker-ssd/ps/mini-ork/.venv/bin/python -m pytest -q tests/unit/test_kickoff_scope.py tests/unit/test_context_v2.py tests/unit/test_prefs_path_scope.py --junitxml="${MINI_ORK_RUN_DIR}/lf-r8-junit.xml"
```

## Required artifacts — declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/lf-r8-junit.xml`

## Rules

Edit files directly; do not emit unified diffs. Never rebase, merge or reset
the worktree during the run. Do not change `context_v2.build`, the pack schema,
`prefs_for`'s glob semantics, or anything under `recipes/`, and do not write to
the live DB under `/Volumes/docker-ssd/ps/mini-ork/.mini-ork/`.
