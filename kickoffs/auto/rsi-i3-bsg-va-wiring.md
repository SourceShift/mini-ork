# I3 validation-evidence replay on real code-fix runs (G03-T11)

## Goal

`mini_ork/certify/oracle.py:judge` already does buggy/candidate/gold replay:
the reproduction must fail on base, pass on the patch (delta gate), and
metamorphic invariants must fail on base (`PROVEN/REFUTED/UNVERIFIED`). But
nothing outside `mini-ork certify` calls it, so live code-fix runs accept a fix
whose passing tests never touch the bug (arXiv 2607.28871).

## Mechanism (exact spec)

1. In `recipes/code-fix/verifiers/test.py`, after the existing test check
   passes, run a replay check: execute the same test command against the
   pre-change base state (the run's baseline commit, via a temporary
   `git worktree` or `git stash`-free checkout — never mutate the target working
   tree) and require at least one test that passes on the candidate to FAIL on
   the base. Reuse `mini_ork.certify` helpers (probe/oracle delta gate) rather
   than reimplementing where they fit.
2. Outcomes: base-fails/candidate-passes → pass; candidate passes but nothing
   fails on base → fail with reason `tests-do-not-exercise-change`; base state
   cannot be built or the command cannot run → emit an explicit
   `unverified` result (not pass, not fail) in the verifier JSON so the gate can
   treat it as abstention.
3. Opt-out `MO_CODEFIX_REPLAY=0`. Default ON.
4. Keep the verifier's existing JSON envelope keys; add `replay` as a new key.

## Files in scope

- recipes/code-fix/verifiers/test.py
- mini_ork/certify/probe.py or mini_ork/certify/oracle.py (only to expose a reusable helper; no semantic change)
- tests/unit/test_codefix_replay.py (new)

## Tests

New `tests/unit/test_codefix_replay.py` using a tiny throwaway git repo in
tmp_path (a function + a test; base has the bug, candidate fixes it):
- real fix + regression test → pass;
- candidate that adds a test which also passes on base → fail with
  `tests-do-not-exercise-change`;
- `MO_CODEFIX_REPLAY=0` → replay skipped;
- the target working tree is byte-identical before and after the check.

## Success criteria

- `python3.11 -m pytest -q tests/unit/test_codefix_replay.py tests/unit/test_certify_oracle.py` passes.

## Rules

Edit files directly; do not emit unified diffs. Do not touch apply.py or
promotion_gate.py.
