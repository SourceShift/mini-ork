# Agents get their learned lessons in every run — default MINI_ORK_DB to $MINI_ORK_HOME/state.db

## Why (live evidence, 2026-10-07)

The learned block every researcher / implementer / reviewer gets at dispatch (`_learned_block`,
`mini_ork/cli/execute.py` ~:2520-2610) is EMPTY in any run whose environment lacks
`MINI_ORK_DB`:

- `_learned_block` calls `context_assembler.failure_modes_md(..., db=os.environ.get("MINI_ORK_DB"))`;
- `context_assembler._db_path(None)` (`mini_ork/context_assembler.py:37-43`) raises
  `RuntimeError("MINI_ORK_DB unset")`;
- `_learned_block` swallows it in `try/except`, so the lessons silently vanish.

Reproduced in a node-like env on a copy of the live home: MINI_ORK_DB **unset → 0 chars, no
sources**; set → 894 chars with the verified pattern lessons. In the live ledger
(`lesson_injections`), `pattern` sources have NEVER appeared, and every learned record since
17:50 shows only `context_v2` items. The documented default is
`MINI_ORK_DB` → `$MINI_ORK_HOME/state.db` (`mini_ork/cli/main.py:151`,
`mini_ork/context.py:93` `RunContext`). `execute.py:2651` even comments "callers set
MINI_ORK_HOME but not always MINI_ORK_DB". The same raise exists in other modules
(`trace_store.py:26`, `registries/agent_registry.py:51`, `recovery/circuit_breaker.py:91`,
`learning/benchmark_suite.py:74`, …).

## Files in scope (touch ONLY these)

- `mini_ork/cli/main.py`: ONLY `main()` (publish the default at startup)
- `mini_ork/cli/execute.py`: ONLY `main()` (same default when execute is invoked directly)
- `mini_ork/context_assembler.py`: ONLY `_db_path`
- `tests/unit/test_learned_db_default.py` (new)

Do NOT modify any other file.

## Fix (exact)

1. **`main()`** (`main.py`, right after `MINI_ORK_ROOT` is set, ~:1089): when `MINI_ORK_DB` is
   unset (`context_env("MINI_ORK_DB")` empty), publish
   `os.path.join(<MINI_ORK_HOME or ./.mini-ork>, "state.db")` via
   `mini_ork.context.apply_env_overrides({"MINI_ORK_DB": …})`, the sanctioned env mutator. Never
   override an explicitly set value.
2. **`execute.main()`** (~:730, where `home` is resolved): the same default, same helper, same
   rule.
3. **`context_assembler._db_path(db)`:** explicit arg → `MINI_ORK_DB` →
   `$MINI_ORK_HOME/state.db` (home default `.mini-ork`). No raise. Callers already handle a
   missing file (`if not os.path.isfile(dbp): return ""`).
4. Leave the other modules' raises alone (out of scope). Fixes 1–2 cover every module during a
   run.

## Tests (`tests/unit/test_learned_db_default.py`, temp home with a seeded state.db holding one approved emergent pattern with a lesson)

- `_db_path(None)` with `MINI_ORK_DB` unset and `MINI_ORK_HOME=<tmp>` → `<tmp>/state.db`;
  explicit arg wins; env wins over home.
- With `MINI_ORK_DB` unset, `execute._learned_block(None, "framework_edit", "implementer", …,
  sources=s)` contains the lesson, and `s` has a `pattern` source (use `MO_SEMANTIC_INJECT=0` to
  keep it on the static path, and `MO_CONTEXT_V2=off`).
- `main()` startup publishes the default when unset and leaves an explicit value untouched (call
  the helper you extract, or `main(["--help"])` if it reaches the publish point first; keep it
  hermetic).
- `execute.main` publishes it too: test the extracted helper.

## Verification command

The command that proves this run succeeded (per file; the host kills one CPU-bound process
that runs longer than 30 s):

```bash
for f in tests/unit/test_learned_db_default.py tests/unit/test_learned_record.py tests/unit/test_context_assembler_py.py tests/unit/test_inject_verified_only.py; do env -u MINI_ORK_RUN_ID -u MINI_ORK_DB -u MINI_ORK_HOME python3.11 -m pytest -q -p no:asyncio "$f" || exit 1; sleep 3; done   # must exit 0
```

## Done when

- The verification command → 0 failed. Paste the summary lines.
- `uvx ruff check mini_ork/cli/main.py mini_ork/cli/execute.py mini_ork/context_assembler.py tests/unit/test_learned_db_default.py` → clean.
- Proof: in a throwaway home (a `sqlite3` backup copy of the live state.db), with MINI_ORK_DB
  unset, `_learned_block(...)` for a framework_edit implementer returns the lessons block with
  `pattern` sources. Paste it.
- `git diff --stat` touches only the files in scope.
