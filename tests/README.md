# mini-ork Tests

## Quick Start

```bash
# From the repo root:
python3 -m pytest -q
```

The suite is pure Python (the bash test layers were removed in the
2026-07 bash-removal — see `docs/plans/2026-07-26-bash-removal-plan.md`).
`pyproject.toml` sets `pythonpath=["."]` and `testpaths=["tests"]`.

## Layout

| Path | Contents |
|---|---|
| `tests/unit/` | Unit tests for `mini_ork/*` modules (`*_py.py`) |
| `tests/integration/` | Cross-component integration tests |
| `tests/langgraph_parity/` | Parity tests against the LangGraph reference |
| `tests/live/` | Live-provider tests (skipped without credentials) |
| `tests/fixtures/` | Shared fixtures |
| `tests/_apply_loop_fixtures/` | Fixtures for the apply-loop tests |

The table lists what exists; the CI matrix shards `tests/unit` into
`unit-a`/`unit-b` and runs everything else as `rest`.

## Conventions

- DB-backed tests bootstrap the schema with
  `mini_ork.stores.migrate.init_db(db_path, root=repo)` — no subprocess,
  ~1s. Use a `tmp_path` db per test.
- Tests that drive CLIs prefer `python -m <module>` subprocesses or
  in-process `main(argv)` calls.
- The `conftest.py` autouse fixture snapshots/restores `os.environ` and
  cwd — never leak either (a past leak of `.mini-ork/state.db` into the
  suite cwd poisoned later tests).
- Long-history: parity tests that compared the Python port against the
  retired bash twins were converted to plain unit tests in Phase 3
  (2026-07-27); expectations are semantic, not recorded goldens.

## CI

`.github/workflows/ci.yml` runs ruff (blocking + advisory), pytest on
3.11/3.12 sharded `unit-a`/`unit-b`/`rest`, the README/doc claim check, the
verifiers-pin check, and the web smoke suite; the `ci result` job aggregates
them so one required status covers all of it. The merge green-gate is
`python3 -m pytest -q` (scope per task with `MINI_ORK_TEST_CMD`).
