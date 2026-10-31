# W1-c: code-fix's test and typecheck verifiers must say when their tool never started

## Goal

code-fix is the recipe outside users run most. Its two shell-out verifiers,
`recipes/code-fix/verifiers/test.py` and `recipes/code-fix/verifiers/typecheck.py`,
never emit `failure_kind`. So when the tool could not even start (a missing binary,
`sh: npm: command not found`, an npm `EBADDEVENGINES` refusal on a pnpm repo, or a
suite killed by a signal), the engine reads it as "the code is wrong". It then
refutes the patch and buys implementer revise rounds and rollbacks for code no tool
examined: the 2026-10-11 audit cascade (`mini_ork/verify/pm.py`, spawn-failure
provenance).

The executor already turns an envelope carrying `{"pass": false, "failure_kind": "toolchain"}`
into an abstention (`mini_ork/cli/execute.py`, `_levels.abstain_kind`, `ABSTAIN_RC`).
The verifiers just have to say it.

**Trap: do not reuse `classify_spawn` on a suite log.** Its needle list includes
`"No such file or directory"` and `"ENOENT"`. A test that fails with
`FileNotFoundError: [Errno 2] No such file or directory` prints exactly that while
the tool ran fine, and it would turn a real refutation into an abstention. Suite
logs need a stricter classifier.

Read `mini_ork/verify/pm.py` (`_SPAWN_FAILURES`, `classify_spawn`),
`mini_ork/verify/levels.py` (`FAILURE_KINDS`, `abstain_kind`),
`recipes/code-fix/verifiers/test.py` (`run_suite`, `emit`, the guarded `mini_ork` imports
near the top, `CMD`) and `recipes/code-fix/verifiers/typecheck.py` (`_emit`, `main`, the
guarded `mini_ork.verify.pm` import) first.

## Mechanism (exact spec)

### `mini_ork/verify/pm.py`: add next to `classify_spawn` (leave `classify_spawn` unchanged)

```python
# A test or typecheck SUITE log is not a bare spawn: a test that raises
# FileNotFoundError prints "No such file or directory" while the tool ran fine.
# Only the unambiguous "the tool never started" signals count for a suite, and only
# in the head of its log, before any test output.
_SUITE_SPAWN_FAILURES = (
    "command not found",
    "EBADDEVENGINES",
    "not recognized as an internal or external command",
    "could not determine executable to run",
)
_SUITE_HEAD_LINES = 20


def classify_suite_spawn(argv, rc, output):  # noqa: ARG001 — argv mirrors classify_spawn
    """``"toolchain"`` when a test/typecheck SUITE never started its tool, else ``""``.

    Stricter than :func:`classify_spawn`: the rc rules are the same (127, or killed
    by a signal), but only :data:`_SUITE_SPAWN_FAILURES` count, and only in the first
    :data:`_SUITE_HEAD_LINES` lines. ``""`` keeps today's verdict (fail-closed).
    """
    if rc == 127:
        return "toolchain"
    if isinstance(rc, int) and rc < 0:
        return "toolchain"
    head = "\n".join((output or "").splitlines()[:_SUITE_HEAD_LINES])
    for needle in _SUITE_SPAWN_FAILURES:
        if needle in head:
            return "toolchain"
    return ""
```

### `recipes/code-fix/verifiers/test.py`

1. Add a guarded import next to the existing ones:
   `try: from mini_ork.verify.pm import classify_suite_spawn as _classify_suite_spawn`
   with `except Exception: _classify_suite_spawn = None`. Use the same comment style as
   the replay_check import.
2. Add this helper after `first_fail_line`:

```python
def _suite_failure_kind(post_rc):
    """``"toolchain"`` when the suite run never started its tool, else ``""`` (fail-closed)."""
    if _classify_suite_spawn is None or not isinstance(post_rc, int) or post_rc == 0:
        return ""
    try:
        with open(LOG_PATH, encoding="utf-8", errors="replace") as fh:
            text = fh.read(65536)
    except OSError:
        text = ""
    return _classify_suite_spawn(CMD, post_rc, text)
```

3. In `emit(...)`, when `not passed`: `kind = _suite_failure_kind(post_rc)`. If `kind` is
   non-empty, set `payload["failure_kind"] = kind` before printing. A passing payload never
   carries the key. `emit_unverified` is unchanged.

### `recipes/code-fix/verifiers/typecheck.py`

1. Add `classify_suite_spawn` to the existing guarded `from mini_ork.verify.pm import ...`.
   In that block's `except` fallback, define
   `def classify_suite_spawn(argv, rc, output): return ""`.
2. `_emit` gains a keyword `failure_kind: str = ""`. When it is non-empty and `passed` is
   False, set `payload["failure_kind"] = failure_kind`.
3. In `main`, after the typecheck subprocess: when `exit_code != 0`, read the first
   65536 chars of `LOG_PATH` (`errors="replace"`, and `""` on `OSError`). Pass
   `failure_kind=classify_suite_spawn(cmd, exit_code, text)` to the existing
   `_emit(passed=..., ...)` call. The return value (`exit_code`) is unchanged.

## Files in scope (touch ONLY these)

- `mini_ork/verify/pm.py`
- `recipes/code-fix/verifiers/test.py`
- `recipes/code-fix/verifiers/typecheck.py`
- `tests/unit/test_codefix_failure_kind_py.py` (new)

Do NOT modify any other file.

## Tests (`tests/unit/test_codefix_failure_kind_py.py`, hermetic)

`classify_suite_spawn` unit cases:
- rc 127 gives `"toolchain"`.
- rc -15 gives `"toolchain"`.
- rc 1 with `"npm ERR! code EBADDEVENGINES"` on line 2 gives `"toolchain"`.
- rc 1 with `"sh: 1: pytest: command not found"` on line 1 gives `"toolchain"`.
- **rc 1 with a pytest log whose line 5 is `"E   FileNotFoundError: [Errno 2] No such file or directory: 'x.json'"` gives `""`.**
- rc 1 with `"command not found"` only on line 25 gives `""`.
- rc 0 gives `""`.

Verifier envelopes. Load the two scripts with `importlib.util.spec_from_file_location`,
or run them with `subprocess` the way `tests/unit/test_codefix_tamper_guard.py` /
`tests/unit/test_codefix_red_base.py` drive `recipes/code-fix/verifiers/test.py`; reuse
whichever harness they use.
- test.py `emit(False, "x", 127)` with a LOG_PATH containing `"sh: 1: pytest: command not found"`
  prints a payload with `failure_kind == "toolchain"`, and
  `mini_ork.verify.levels.abstain_kind(payload) == "toolchain"`.
- test.py `emit(False, "x", 1)` with a LOG_PATH containing the FileNotFoundError line has no
  `failure_kind`.
- test.py `emit(True, "ok", 0)` has no `failure_kind`.
- test.py `emit(False, "tests weakened: y", "")` has no `failure_kind` (post_rc is not an int).
- typecheck.py `_emit(passed=False, ..., failure_kind="toolchain")` carries it.
  `_emit(passed=True, ..., failure_kind="toolchain")` does not.

## Verification commands

- `python3 -m pytest -q -p no:asyncio tests/unit/test_codefix_failure_kind_py.py tests/unit/test_codefix_tamper_guard.py tests/unit/test_codefix_red_base.py tests/unit/test_codefix_replay.py tests/unit/test_abstain_gate.py`
- `python3 -m pytest -q -p no:asyncio -k "pm or spawn or typecheck or failure_kind" tests/unit`
- `ruff check --select F,E9 mini_ork/verify/pm.py recipes/code-fix/verifiers/test.py recipes/code-fix/verifiers/typecheck.py tests/unit/test_codefix_failure_kind_py.py`

## Self-application measurement

```measure
python3 -m pytest -q -p no:asyncio tests/unit/test_codefix_failure_kind_py.py --junitxml="${MINI_ORK_RUN_DIR}/w1-codefix-failure-kind-junit.xml"
```

## Required artifacts: declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/w1-codefix-failure-kind-junit.xml`

## Success criteria

- All verification commands pass, and every pre-existing test is unchanged.
- A real test failure, including one whose output says "No such file or directory", is
  still a refutation. Only an unambiguous never-started tool abstains.

## Rules

Edit files directly; do not emit unified diffs. Execute this spec exactly; do not
research and do not add lens or drafter nodes.
