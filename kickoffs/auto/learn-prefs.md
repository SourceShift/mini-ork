# Preferences you set reach every LLM node — `mini-ork prefs`

Plan: `docs/plans/2026-10-07-learning-memory-page-refactor.md`, phase P7a (D7b + job J5: "what
does it know about me, and can I fix it?").

## Why

Preferences and constraints reach no model today:

- `context_assembler.context_assemble` reads `$MINI_ORK_HOME/config/user_preferences.json` and
  `constraints.json` (`mini_ork/context_assembler.py:236-250`) only into the planner's
  `context-pack.json`. That pack is written to disk as an audit record and appended to no prompt.
- The DB table built for this, `user_preference_memory` (PK `(user_id, preference_key, scope,
  scope_target)`, `scope ∈ {global, task_class, workflow}`), has 0 rows. Nothing writes it and no
  command exists to set one.
- The node prompt's learned block (`_learned_block`, `mini_ork/cli/execute.py`) carries learned
  failure modes, lessons and operator steering, but nothing the operator set as a standing rule.

## Files in scope (touch ONLY these)

- `mini_ork/memory/preferences.py` (new)
- `mini_ork/cli/prefs_cmd.py` (new)
- `mini_ork/cli/main.py`: ONE line, `"prefs": "mini_ork.cli.prefs_cmd",` placed directly after the
  `"memory-lifecycle"` entry in `_NATIVE_MODULE_SUBS`, plus one line in the `_HELP` text next to
  `memory-lifecycle` if it is listed there. Another worktree edits this file; touch nothing else.
- `tests/unit/test_native_dispatch_py.py`: ONE line, `"prefs",` directly after
  `"memory-lifecycle",` in the expected set (an exact-set guard)
- `mini_ork/cli/execute.py`: ONLY `_learned_block`. Another worktree edits `_run_verifier_ref`;
  touch nothing else.
- `tests/unit/test_prefs.py` (new)

Do NOT modify any other file.

## `mini_ork/memory/preferences.py` (exact API)

DB resolution as in the other learning modules (`db` arg → `MINI_ORK_DB` →
`$MINI_ORK_HOME/state.db`). `user_id` is always `"default"`.

- `set_pref(key, value, *, scope="global", target="", db=None) -> dict`: upsert. `value` is plain
  text, stored as a JSON string. `scope="role"` is NOT allowed by the table's CHECK. Valid scopes
  are `global`, `task_class`, `workflow`; anything else raises `ValueError` with the allowed list.
  For `global`, `target` must be `""`.
- `remove_pref(key, *, scope="global", target="", db=None) -> bool`
- `list_prefs(*, db=None) -> list[dict]`: `{key, value, scope, target, set_at, source: "db"}`, plus
  read-only entries from the legacy JSON files (`source: "file:<path>"`) when present. Legacy
  `constraints.json` `constraints[]` become `{key: "constraint-<i>", value: <text>, scope:
  "global"}`.
- `prefs_for(task_class, workflow="", *, db=None) -> list[dict]`: global + matching `task_class`
  + matching `workflow`, ordered `global` first then `set_at`. Capped at 12 entries, each value
  capped at 600 chars.
- `render_block(prefs) -> str`: `""` when empty, else:
  ```
  --- Operator preferences and constraints (set by the user; follow them) ---
  - <value>   [scope: task_class=<x>]   (scope tag omitted for global)
  --- /operator preferences ---
  ```

## `mini-ork prefs` (`mini_ork/cli/prefs_cmd.py`)

`main(argv) -> int`, the same handler shape as `mini_ork/cli/memory_lifecycle.py`:

- `prefs list [--json]`: a table (key, value[:80], scope, target, source), or JSON.
- `prefs set <key> <value...> [--scope global|task_class|workflow] [--target X]`
- `prefs rm <key> [--scope …] [--target X]`

Exit 0 on success, 2 on a usage error (message on stderr).

## Injection (`_learned_block`)

Build the preferences block via `prefs_for(task_class)` and put it FIRST in the returned block,
before learned failure modes: explicit operator rules outrank learned ones. For each injected
preference, append `{"kind": "preference", "id": f"pref:{scope}:{target}:{key}", "text": value}`
to `sources` when a list is given. It obeys `MO_INJECT_LEARNINGS` like the rest of the block. Any
exception means no preference block; the rest of the learned block is unchanged.

## Tests (`tests/unit/test_prefs.py`, temp DB + temp MINI_ORK_HOME)

- set / list / rm round trip; upsert replaces; a bad scope raises with the allowed list; a global
  pref with a target raises.
- `prefs_for` returns global + the matching task_class only; respects the caps.
- Legacy files are listed with `source: file:…` and included in `prefs_for`.
- `_learned_block(...)` for an implementer with one global pref → the block starts with the
  preferences header; `sources[0]["kind"] == "preference"`; `MO_INJECT_LEARNINGS=0` → empty.
- CLI: `main(["set", "tone", "be terse", "--scope", "global"])` → 0; `main(["list", "--json"])`
  prints it; `main(["set", "x", "y", "--scope", "role"])` → 2.

## Verification command

The command that proves this run succeeded:

```bash
python3.11 -m pytest -q -p no:asyncio tests/unit/test_prefs.py tests/unit/test_native_dispatch_py.py tests/unit/test_learned_record.py   # must exit 0
```

## Done when

- The verification command → 0 failed. Paste the summary line.
- `uvx ruff check mini_ork/memory/preferences.py mini_ork/cli/prefs_cmd.py mini_ork/cli/execute.py tests/unit/test_prefs.py` → clean.
- `git diff --stat` touches only the files in scope: one line in `main.py`, one line in the guard
  test, and only `_learned_block` in `execute.py`.
