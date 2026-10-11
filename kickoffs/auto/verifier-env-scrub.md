# Run target-repo test suites without provider keys or mini-ork state in their environment

## Goal

Test verifiers run the TARGET repository's test suite with the full inherited
environment: every provider API key (`MINIMAX_API_KEY`, `GLM_API_KEY`, …), the path to
the credential file (`MINI_ORK_SECRETS`), and mini-ork's own state pointers
(`MINI_ORK_DB`, `MINI_ORK_HOME`, `MINI_ORK_RUN_ID`). Two measured consequences:

1. **Data loss.** On held-out tasks (the target repo is mini-ork itself) the target's
   unit tests resolved the credential store through `MINI_ORK_SECRETS` and overwrote a
   real MiniMax key with a test fixture value; every minimax dispatch then failed (401).
   The same runs wrote fixture rows (`kimi`, `glm`, `not-a-lane`) into the caller's
   `llm_calls` table through `MINI_ORK_DB` + `MINI_ORK_RUN_ID`.
2. **Exposure.** Any repository certify / code-fix is pointed at can read the operator's
   provider keys from its own test code.

A test suite never needs either. Scrub them.

## Files in scope

(The code-fix verifiers are claimed by another live worktree; they get the same
one-line change in a follow-up. Do NOT touch `recipes/code-fix/`.)


- `mini_ork/verify/test_env.py` — NEW: the one scrub function.
- `recipes/framework-edit/verifiers/test.py` — the `subprocess.run(argv, …)` calls that
  run the test command (lines ~55–58), not the git plumbing calls.
- `recipes/recursive-self-improve/verifiers/self-tests-pass.py` — the test run (~line 251).
- `mini_ork/certify/oracle.py` — `replay_check._run`.
- `tests/unit/test_verifier_test_env.py` — NEW.

No other file changes.

## Mechanism (exact spec)

### `mini_ork/verify/test_env.py`

```python
def scrubbed_test_env(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """A copy of `environ` (default os.environ) safe to hand a target repo's test suite."""
```

Drop a variable when ANY of these holds (case-sensitive names):

- it is one of `MINI_ORK_SECRETS`, `MINI_ORK_DB`, `MINI_ORK_HOME`, `MINI_ORK_PROJECT_HOME`,
  `MINI_ORK_RUN_ID`, `MINI_ORK_RUN_DIR`, `MINI_ORK_PLAN_PATH`, `MINI_ORK_AGENTS`;
- its name ends with `_API_KEY`, `_AUTH_TOKEN`, `_ACCESS_TOKEN`, `_SECRET`, `_SECRET_KEY`;
- its name starts with `ANTHROPIC_` or is `OPENAI_API_BASE` / `OPENAI_BASE_URL`.

Keep everything else unchanged (PATH, HOME, VIRTUAL_ENV, PYTHONPATH, MINI_ORK_ROOT,
MINI_ORK_TEST_CMD, MO_* knobs, …). Export it from the module's `__all__`.

### Call sites

Each verifier imports it lazily with a local fallback, so a verifier copied alone into a
fixture keeps working:

```python
try:
    from mini_ork.verify.test_env import scrubbed_test_env
except Exception:                      # pragma: no cover — verifier copied without mini_ork
    def scrubbed_test_env(environ=None):
        import os
        env = dict(os.environ if environ is None else environ)
        for k in list(env):
            if (k in {"MINI_ORK_SECRETS", "MINI_ORK_DB", "MINI_ORK_HOME", "MINI_ORK_PROJECT_HOME",
                      "MINI_ORK_RUN_ID", "MINI_ORK_RUN_DIR", "MINI_ORK_PLAN_PATH", "MINI_ORK_AGENTS"}
                    or k.endswith(("_API_KEY", "_AUTH_TOKEN", "_ACCESS_TOKEN", "_SECRET", "_SECRET_KEY"))
                    or k.startswith("ANTHROPIC_") or k in {"OPENAI_API_BASE", "OPENAI_BASE_URL"}):
                env.pop(k)
        return env
```

and pass `env=scrubbed_test_env()` to the `subprocess.run` that executes the TEST or
TYPE-CHECK command. Git plumbing calls (`git status`, `git archive`, `git apply`, …) are
not changed. In `replay_check._run`, pass `env=scrubbed_test_env()` too (import at module
top is fine there — it is inside the `mini_ork` package).

Do not change what command is run, its cwd, shell flag, output handling or exit-code logic.

## Tests (`tests/unit/test_verifier_test_env.py`)

- `scrubbed_test_env({...})` drops every listed name and suffix/prefix match, keeps PATH,
  HOME, MINI_ORK_ROOT, MINI_ORK_TEST_CMD, MO_DAILY_BUDGET_USD, PYTHONPATH; does not mutate
  its input.
- the same for framework-edit's `test.py` if an existing test already drives it (reuse
  its harness); otherwise a focused test that monkeypatches `subprocess.run` in the
  imported module and asserts `env` was passed without the secret names.
- `replay_check`: monkeypatch `subprocess.run` in `mini_ork.certify.oracle`, call
  `replay_check` with a trivial command, assert the `env` kwarg excludes `MINI_ORK_SECRETS`
  and `*_API_KEY`.
- every existing test of these verifiers still passes.

## Success criteria

- `python3.11 -m pytest -q tests/unit/test_verifier_test_env.py` passes, plus every existing
  test that references any of the touched verifier files or `replay_check`
  (`grep -rl "verifiers/test.py\|verifiers/typecheck.py\|self-tests-pass\|replay_check" tests/`).
- `ruff check` clean on touched Python files.
- Diff touches only the files in scope.
