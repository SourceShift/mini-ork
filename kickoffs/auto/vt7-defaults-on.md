# VT7 Turn the verification stack ON by default (G10-T01, G10-T02, G03-T04, G08-T05)

## Goal

Five verification features shipped opt-in on 2026-10-05 and each passed a live mini-ork smoke. The operator decision
is to turn ALL of them ON by default: an unset knob now means ON, and `=0` turns one off. At the same time, the
mutation-adequacy audit must stop downgrading changes it cannot apply to. Today any `code-fix` change that touches no
Python source (docs, YAML, a one-line constant) gets `adequacy_unverified`, which the now-default level gate would turn
into a withheld publish. "The instrument does not apply" (NOT_APPLICABLE, keep the green) must be distinguished from
"the instrument applied but could not measure" (UNVERIFIED, downgrade).

Doctrine (unchanged): verdicts are anchored on what code DID; an LLM never approves; UNVERIFIED is abstention, never a pass.

## Mechanism (exact spec)

All paths are under `/Volumes/docker-ssd/ps/mini-ork-worktrees/vt7-defaults-on/`. Every knob stays read at CALL time.
The parse rule is unchanged; only the default value flips.

1. `mini_ork/certify/relations.py`: `enabled()` reads `os.environ.get("MO_ASSAY_RELATIONS", "1")`; `rescue_enabled()`
   reads `os.environ.get("MO_ASSAY_RELATIONS_RESCUE", "1")`. Update both docstrings and the module docstring
   ("DEFAULT ON; `0` disables").
2. `mini_ork/certify/differential.py`: `enabled()` reads `os.environ.get("MO_ASSAY_DIFFERENTIAL", "1")`. Update the
   docstrings the same way.
3. `mini_ork/gates/suite_adequacy.py`:
   - `enabled()` default `"1"`: `src.get("MO_SUITE_ADEQUACY", "1") == "1"`. Update the module docstring's opt-in wording.
   - New verdict value `NOT_APPLICABLE` (export it with the module's other verdict constants, if any).
   - `no-sources` (the `if not files:` branch, ~l.510): return `NOT_APPLICABLE` instead of `UNVERIFIED`. Reason text
     is unchanged.
   - `no-sites` (~l.516), AND the new case where generation yields fewer than `MIN_VALID` mutants because there are
     fewer mutation sites: return `NOT_APPLICABLE`, reason `too-few-sites: <n>`. The changed code is too small to
     mutate meaningfully.
   - Keep `UNVERIFIED` for everything measured-but-unmeasurable: `baseline-timeout`, `baseline-red`, canary not
     detected, and `too-few-valid` (sites existed, runs crashed or timed out).
4. `recipes/code-fix/verifiers/test.py`:
   - `_green_pass` reads `os.environ.get("MO_SUITE_ADEQUACY", "1")`.
   - On `a["verdict"] == "NOT_APPLICABLE"`, call
     `emit(True, f"{reason}; suite adequacy n/a ({a['reason']})", post_rc, replay=replay, adequacy=a)`. The pass is
     kept, and `adequacy_unverified` is NOT set.
   - `ADEQUATE` behaviour is unchanged; `INADEQUATE` / `UNVERIFIED` still downgrade.
   - Update the header comment block (`MO_SUITE_ADEQUACY … default 1`).
5. `mini_ork/verify/levels.py`: both reads in `enabled()` default `"1"`; update the docstring. Nothing else changes.
   NOT_APPLICABLE needs no special case, because levels only reads the `adequacy_unverified` flag.
6. No other behaviour changes. `MO_PROMOTION_GATE_HACKABILITY` is already default ON, and the equivalence operator
   is declaration-driven. Do not touch them.

## Files in scope (touch ONLY these)

- `mini_ork/certify/relations.py`, `mini_ork/certify/differential.py`, `mini_ork/verify/levels.py`: default flips (+docstrings).
- `mini_ork/gates/suite_adequacy.py`: default flip + NOT_APPLICABLE (item 3).
- `recipes/code-fix/verifiers/test.py`: default flip + the NOT_APPLICABLE pass-through (item 4).
- The test files below. All paths are under `/Volumes/docker-ssd/ps/mini-ork-worktrees/vt7-defaults-on/`.

Do NOT modify any other file: no docs, no CHANGELOG, no recipes other than code-fix's verifier.

## Tests

Measured on a probe tree with the five defaults flipped (2026-10-06): exactly these 15 tests fail. Each one pins
"unset = off". Fix each by pinning the knob it tests to `"0"` with `monkeypatch.setenv` (or by asserting the new
default where the test is about the default). Never weaken an assertion about behaviour.

- `tests/unit/test_certify_differential.py::test_knobs_off_byte_identical`: set `MO_ASSAY_DIFFERENTIAL=0`.
- `tests/unit/test_certify_relations.py::test_knobs_off_byte_identical` and
  `::test_knobs_on_no_invariant_rescue_off_unchanged`: set `MO_ASSAY_RELATIONS=0`, and `MO_ASSAY_RELATIONS_RESCUE=0`
  where "rescue off" is meant.
- `tests/unit/test_verify_levels.py`:
  - `::test_emit_run_verdict_bytes`, `::test_real_replay_abstain_knob_off`: set `MO_LEVEL_VECTOR=0`.
  - `::test_enabled_knob`: unset → True, `"0"` → False.
  - `::test_real_strong_knob_on`: set `MO_SUITE_ADEQUACY=0`, because this test is about the level vector.
- `tests/unit/test_suite_adequacy.py`:
  - `::test_enabled_knob`: `enabled({})` is True, `{"MO_SUITE_ADEQUACY": "0"}` is False.
  - `::test_knob_off_byte_identical`: set `MO_SUITE_ADEQUACY=0`.
- `tests/unit/test_codefix_replay.py`: set `MO_SUITE_ADEQUACY=0` in `::test_overlay_carries_new_untracked_test_for_real_fix`,
  `::test_overlay_skips_non_test_source_changes`, `::test_replay_passes_for_real_fix` and `::test_replay_skipped_when_opt_out`.
  They test the replay, not adequacy.
- `tests/unit/test_mini_ork_execute_py.py::test_run_verdict_preserves_recipe_detailed_verdict`: set
  `MO_LEVEL_VECTOR=0`. It pins the legacy 4-key run-verdict.
- `tests/integration/test_remote_nodes_e2e.py`: in `_client`, after the `MO_*` filter, set `env["MO_LEVEL_VECTOR"] = "0"`
  and `env["MO_SUITE_ADEQUACY"] = "0"`. These tests are about remote placement, not verification policy.

New tests, appended to the existing test files (hermetic):

- A. One test per knob: env unset → enabled; `"0"` → disabled. Cover relations, rescue, differential, suite_adequacy
  and levels.
- B. `suite_adequacy`:
  - a repo whose changed files are only `.md` / `.yaml` → `NOT_APPLICABLE` (`no-sources`);
  - a changed `.py` file with fewer than `MIN_VALID` mutation sites (e.g. `X = 1`) → `NOT_APPLICABLE` (`too-few-sites`);
  - a strong / weak suite → `ADEQUATE` / `INADEQUATE`, unchanged.
- C. Drive the REAL `recipes/code-fix/verifiers/test.py` with the knob UNSET in a tmp git repo whose fix changes only a
  `.yaml` file and whose suite is green with replay overlap. The payload has `pass: true` and
  `suite_adequacy.verdict == "NOT_APPLICABLE"`, and `adequacy_unverified` is absent.

## Success criteria

- The verification command passes. All 15 previously failing tests pass, and no other test changes outcome.
- With no `MO_*` knob set, relations, rescue, differential, suite adequacy and the level vector are all active.
- NOT_APPLICABLE never sets `adequacy_unverified`, and never turns a green into a non-pass.
- `ruff check` is clean on every touched file.

## Verification command

cd /Volumes/docker-ssd/ps/mini-ork-worktrees/vt7-defaults-on && env -u MO_ASSAY_RELATIONS -u MO_ASSAY_RELATIONS_RESCUE -u MO_ASSAY_DIFFERENTIAL -u MO_SUITE_ADEQUACY -u MO_LEVEL_VECTOR python3.11 -m pytest -q -p no:cacheprovider -p no:asyncio tests/unit/test_certify_differential.py tests/unit/test_certify_relations.py tests/unit/test_certify_oracle.py tests/unit/test_verify_levels.py tests/unit/test_suite_adequacy.py tests/unit/test_codefix_replay.py tests/unit/test_mini_ork_execute_py.py tests/unit/test_verifier_engine_path.py

## Review bar

Every flipped default must be visible through its real entrypoint: `oracle.judge` (relations, differential), the
code-fix verifier subprocess (adequacy), and `_emit_run_verdict` / `publisher_node` (levels). Test C drives the real
verifier script. Reject:
- any change that makes NOT_APPLICABLE reachable from a measurement failure (timeouts, crashes, red baseline);
- any edit that weakens an assertion instead of pinning a knob.

## Rules

Edit files directly; do not emit unified diffs. Touch only the files listed. Do not add a new `mini-ork` subcommand.
