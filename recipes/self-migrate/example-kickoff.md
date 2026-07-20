# Close the `verify` integration fork

## Goal
Close the `verify` fork: make `mini_ork/cli/verify.py` the sole
implementation, repoint every inbound reference to `bin/mini-ork-verify`, and
retire the bash entrypoint — as a reviewable diff, not applied to main.

The `verify` fork was the cleanest proof case (0 outbound seams — the Python
side was already runtime-native). This file remains the root-relative template
for evaluating another fork; all six original top-level forks are now closed.

## Fork
- **fork:** `verify`
- **python entrypoint:** `mini_ork/cli/verify.py`
- **bash entrypoint to retire:** `bin/mini-ork-verify`

## Inbound references to resolve
- `bin/mini-ork` — repoint the top-level run path away from the Bash verifier
- `mini_ork/cli/execute.py` — repoint its `bin/mini-ork-verify` invocation to the Python verify module
- `mini_ork/cli/main.py` — replace dynamic `_bin(root, "verify")` dispatch with the native module
- `mini_ork/cli/verify.py` — self-reference (delegation shim)
- `tests/e2e/test_e2e_recipe_code_fix.sh` — repoint to `python3 -m mini_ork.cli.verify`
- `tests/integration/test_bin_verify.sh` — convert the bash-oracle parity test to standalone Python
- `tests/unit/test_mini_ork_verify_py.py` — drop the live-bash parity dependency once the oracle is gone

## Acceptance criteria
- `verifiers/parity.sh` — cross-runtime byte-parity holds (bash==python) BEFORE
  the bash entrypoint is retired in the diff.
- `verifiers/pre-retirement-parity.sh` — durable parity evidence is captured
  before the migrator can retire the Bash entrypoint.
- `verifiers/feature-acceptance.sh` — `gates/feature_acceptance.sh verify` passes,
  `tests/unit/test_mini_ork_verify_py.py` green, pyright 0 on the port.
- `verifiers/ledger-shape.sh` — `static-feature-ledger.json` classifies every
  behavior in `mini_ork_verify.py` (static vs agentic), agentic rows carry a
  cost-down `opportunity`.
- `verifiers/fork-closure.sh` — the Bash entrypoint is absent and executable/runtime paths contain no surviving `bin/mini-ork-verify` reference.
- No surviving reference to `bin/mini-ork-verify` anywhere in the diff'd tree.

## Verification commands

- `python3 -m pytest tests/unit/test_mini_ork_verify_py.py -q -p no:cacheprovider`
- `bash gates/feature_acceptance.sh verify`
- `python3 -m pyright mini_ork/cli/verify.py`

## Files in scope
- mini_ork/cli/verify.py
- bin/mini-ork-verify
- bin/mini-ork
- mini_ork/cli/execute.py
- mini_ork/cli/main.py
- tests/e2e/test_e2e_recipe_code_fix.sh
- tests/integration/test_bin_verify.sh
- tests/unit/test_mini_ork_verify_py.py
- lib/runtime-select.sh
- scripts/runtime-parity-harness.sh
- gates/feature_acceptance.sh
