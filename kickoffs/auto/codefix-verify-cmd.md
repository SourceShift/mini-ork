# Framework Edit: the code-fix test gate must run the run's declared verification command

## Goal

The `code-fix` test verifier ignores the verification command the run was
dispatched with and falls back to auto-detecting a bare runner
(`pytest` / `python3 -m pytest`) in the run's cwd. When a code-fix run targets a
large repo, that collects the whole suite: on `run-1791930074-82118` (the frozen
GRASP apply-gate probe, target = the mini-ork checkout) the verifier collected
**9113 tests**, blew the run's 120 s deadline, and the run died
`verdict: CRASH, interrupted: true` → `reaped: dispatcher pid gone` — while the
implementer's one-line `tally.py` fix was correct and self-verified
(`python3 -m pytest test_tally.py`, 5 passed).

The run's `run_profile.json` already carried the parsed command
(`"verification_command": ["pytest test_tally.py"]`) and
`recipes/framework-edit/verifiers/test.py::_epic_verification_commands()` already
consumes that field for its epic-acceptance tier. Make the code-fix verifier read
the same field, as a source ranked **between** an explicit `MINI_ORK_TEST_CMD`
and today's auto-detect chain.

## Live repro (write it down, then prove it is dead)

1. `mkdir -p <tmp>/run && printf '{"verification_command": ["python3 -m pytest test_tally.py"], "kickoff_path": ""}' > <tmp>/run/run_profile.json`
2. `cd <mini-ork checkout root>` (a tree whose `pytest` collects the whole suite)
3. `MINI_ORK_RUN_DIR=<tmp>/run MINI_ORK_RUN_ID=x MINI_ORK_HOME=<tmp>/home PYTHONPATH=<repo> python3 recipes/code-fix/verifiers/test.py`
   with `MINI_ORK_TEST_CMD` **unset**.

Before: the evidence log's first command line is a bare `python3 -m pytest`
(whole suite). After: it is `test_tally.py`, or the profile's command verbatim.

## Files in scope

- `recipes/code-fix/verifiers/test.py` — the only framework file that changes.
- `tests/unit/test_codefix_verify_cmd_from_profile.py` (new) — the regression test.

Nothing else. In particular do **not** touch
`mini_ork/learning/probe_scorer.py`, `mini_ork/cli/main.py`,
`recipes/code-fix/probes/**` (the frozen probe fixture — it is a measurement
baseline, editing it silently invalidates every prior probe score), or any
`.mini-ork/**`.

## Behaviour spec

`detect_test_cmd()` gains one source, in this precedence:

1. `MINI_ORK_TEST_CMD` — explicit operator override, unchanged, still wins over
   everything (this is the escape hatch; no new opt-out knob).
2. **New:** the run profile's `verification_command`, read from
   `${MINI_ORK_RUN_DIR}/run_profile.json`, falling back to
   `${MINI_ORK_HOME}/runs/${MINI_ORK_RUN_ID}/run_profile.json` when
   `MINI_ORK_RUN_DIR` is unset. Strings are stripped of surrounding backticks and
   whitespace; a list is deduped, order preserved, and joined with ` && `.
3. Today's auto-detect chain (package.json → pytest → cargo → go → ruby → `""`),
   unchanged.

Fail-open, never fail-loud: a missing, unreadable, malformed, or empty profile —
or a profile whose `verification_command` is absent/empty/not a list-or-string —
must fall through to step 3 exactly as today. No new exception may escape
`detect_test_cmd()`.

Copy the *shape* of `_profile_field` / `_epic_verification_commands`
(`recipes/framework-edit/verifiers/test.py:343-378`); do **not** port its
kickoff-markdown fallback — the profile field is the contract, and the extra
parser is out of scope for this run.

The profile-sourced command is a **string source only**. It must flow through the
existing rails unchanged: `_ensure_pytest_verbose` augmentation, the
test-weakening guard (`MO_ALLOW_TEST_CHANGES`), `scrubbed_test_env`, the
baseline/delta replay and the adequacy audit. Do not special-case it.

Observability without breaking consumers: write the resolved command into the
evidence log (`[test] cmd: <cmd>`) near the top. **Do not add, remove or reorder
keys in the `emit()` / `emit_unverified()` JSON payloads** — downstream gates key
on that shape.

## Tests

`tests/unit/test_codefix_verify_cmd_from_profile.py`, new, reusing the
throwaway-repo harness style of `tests/unit/test_codefix_red_base.py`
(`_make_repo` / `_run_verifier`):

- profile command is used when `MINI_ORK_TEST_CMD` is unset — prove it by side
  effect (e.g. the profile command writes a sentinel file the auto-detected
  runner would not), not by reading a log line alone;
- explicit `MINI_ORK_TEST_CMD` still wins over the profile command;
- profile with no `verification_command` key, empty list, malformed JSON, and a
  missing file each fall back to auto-detect without raising;
- multiple commands in the list are joined in order with ` && `;
- the resolved command is echoed into the evidence log.

## Verification commands

```
python3.11 -m pytest -q tests/unit/test_codefix_verify_cmd_from_profile.py
```

```
python3.11 -m pytest -q tests/unit/test_codefix_red_base.py tests/unit/test_verifier_test_env.py tests/unit/test_profile_verification_commands.py
```

## Done when

- `${MINI_ORK_RUN_DIR}/framework-edit.diff` contains the proposed patch and
  touches exactly two files: `recipes/code-fix/verifiers/test.py` and
  `tests/unit/test_codefix_verify_cmd_from_profile.py`.
- `${MINI_ORK_RUN_DIR}/verdict.json` reads
  `{"files_changed": 2, "tests_pass": true, "static_pass": true, "pass": true}`.
- Both verification commands above exit 0, and the live repro in this kickoff now
  shows the profile's command in the evidence log instead of a bare runner.
