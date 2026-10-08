# A green-but-unverified test suite is an abstention, not a failure, in the eval reward

## Why (live, 2026-10-08)

Run `ide-orca-f2a-flow-20261008165006` (code-fix on a Rust repo): the reviewer passed it and
the build was green. The eval node still wrote `score: 0.0`, `verdict: needs_revision`, while
its own axes were high (correctness 0.9, completeness 0.85, groundedness 0.95, safety 1.0):

```
"process": {"coherence": 0.0, "claimed_verdict": "pass",
            "coherence_basis": {"execute": true, "verify": false},
            "process_detail": {"plan": 1.0, "execute": 1.0, "verify": 0.0, "coverage": null},
            "overclaimed_success": true, "gated_from": 0.9, "gated_to": 0.0}
"execution": {"r_exec": null, "note": "no execution signal — judge-only reward"}
```

The run's two verifiers (each JSON follows a one-line `[x] running: …` banner;
`verify.levels.read_verifier_payload` handles that):

- **`verifier_test.json`:** `{"pass": false, "status": "unverified", "suite_green": true,
  "post_rc": 0, "replay_unverified": true, "error_summary": "unverified: replay supports pytest,
  jest, vitest, or a results file; none produced for this command"}`. The suite (`script/mini-ork-build`)
  ran green after the patch. The test-delta replay only abstained because the instrument cannot
  read a Rust build.
- **`verifier_typecheck.json`:** `{"pass": true}`, but the command was the literal `true`.

So the R2 coherence gate (`mini_ork/cli/execute_handlers.py` ~:2600-2635, `_stage_checks`
~:2287) read "claimed success with no real verification" and zeroed the score. Every
green Rust/IDE run gets reward 0 and `needs_revision`. That poisons the GRPO signal for these
runs, and the IDE shows a needs_revision eval on a delivered change.

## Files in scope (touch ONLY these)

- `mini_ork/cli/execute_handlers.py`: ONLY `_stage_checks` (and, if the root cause is there,
  the lines in the eval handler that build `verifier_verdicts`)
- `mini_ork/learning/eval_judge.py`: ONLY `_verifier_passed`
- `tests/unit/test_eval_green_abstention.py` (new)

Do NOT touch any other file.

## Changes (exact)

1. **Root cause first.** Find why `coherence_basis.verify` came out `false` for this run, and
   say which it was:
   - `verifier_verdicts` was empty or unparsed (the banner line);
   - `_verifier_passed` mapped `pass: false` + `status: unverified` to False;
   - the label mapping.
2. **`eval_judge._verifier_passed`.** A payload with `status == "unverified"` is an
   ABSTENTION. Return `None` (no execution signal), whatever its `pass` field says. A REFUTED or
   failed payload stays False, and a real pass stays True.
3. **`_stage_checks` verify stage.** `True` when any verifier payload ran a suite green after the
   patch: `suite_green is True` and `post_rc == 0`, even if it abstained on the delta. That is
   real, non-vacuous verification. Otherwise keep the current rules.
   - A typecheck/test command that is the literal `true` is vacuous and must not count. Use
     the payload, not the command, if the command is not available; do not invent a new field.
4. **Unchanged:**
   - `r_exec` / coverage: an abstention is still not a pass for the execution reward.
   - The gate still fires when no verifier ran, or every verifier was vacuous.

## Tests (`tests/unit/test_eval_green_abstention.py`)

- `_verifier_passed({"pass": False, "status": "unverified", "suite_green": True})` is `None`.
  `{"pass": False}` is still False; `{"pass": True}` is True.
- `_stage_checks` with the F2a-shaped verifiers (above) → `verify is True`. With only a vacuous
  verifier (`{}` or `status: "vacuous"`) → not True.
- **End to end through the coherence gate** (call the same helpers the eval handler uses):
  - a claimed pass with the F2a-shaped verifiers is NOT gated to 0;
  - a claimed pass with NO verifiers is still gated.

## Verification command

The command that proves this run succeeded (per file; the host kills one CPU-bound process
that runs longer than 30 s):

```bash
for f in tests/unit/test_eval_green_abstention.py tests/unit/test_eval_judge.py; do env -u MINI_ORK_RUN_ID -u MINI_ORK_DB -u MINI_ORK_HOME python3.11 -m pytest -q -p no:asyncio "$f" || exit 1; sleep 3; done   # must exit 0
```

## Done when

- The verification command → 0 failed. Paste the summary lines.
- One line naming the root cause from step 1.
- `uvx ruff check` on the touched files → clean.
- `git diff --stat` touches only the files in scope.
