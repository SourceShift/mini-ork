# Close the `verify` integration fork

## Goal
Close the `verify` fork: make `mini_ork/ported/mini_ork_verify.py` the sole
implementation, repoint every inbound reference to `bin/mini-ork-verify`, and
retire the bash entrypoint — as a reviewable diff, not applied to main.

The `verify` fork is the cleanest complete fork (0 outbound seams — the Python
side is already runtime-native), so it is the proof
case for this recipe before the harder forks (`reflect`, `cli`, `execute`).

## Fork
- **fork:** `verify`
- **python entrypoint:** `/Volumes/docker-ssd/ps/mini-ork/mini_ork/ported/mini_ork_verify.py`
- **bash entrypoint to retire:** `/Volumes/docker-ssd/ps/mini-ork/bin/mini-ork-verify`

## Inbound references to resolve
- `/Volumes/docker-ssd/ps/mini-ork/bin/mini-ork` — repoint the top-level run path away from the Bash verifier
- `/Volumes/docker-ssd/ps/mini-ork/bin/mini-ork-execute` — repoint the legacy executor path away from the Bash verifier
- `/Volumes/docker-ssd/ps/mini-ork/mini_ork/ported/mini_ork_execute.py` — repoint its `bin/mini-ork-verify` invocation to the Python verify module
- `/Volumes/docker-ssd/ps/mini-ork/mini_ork/ported/mini_ork_cli.py` — replace dynamic `_bin(root, "verify")` dispatch with the native module
- `/Volumes/docker-ssd/ps/mini-ork/mini_ork/ported/mini_ork_verify.py` — self-reference (delegation shim)
- `/Volumes/docker-ssd/ps/mini-ork/tests/e2e/test_e2e_recipe_code_fix.sh` — repoint to `python3 -m mini_ork.ported.mini_ork_verify`
- `/Volumes/docker-ssd/ps/mini-ork/tests/integration/test_bin_verify.sh` — convert the bash-oracle parity test to standalone Python
- `/Volumes/docker-ssd/ps/mini-ork/tests/unit/test_mini_ork_verify_py.py` — drop the live-bash parity dependency once the oracle is gone

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
- `python3 -m pyright mini_ork/ported/mini_ork_verify.py`

## Files in scope
- /Volumes/docker-ssd/ps/mini-ork/mini_ork/ported/mini_ork_verify.py
- /Volumes/docker-ssd/ps/mini-ork/bin/mini-ork-verify
- /Volumes/docker-ssd/ps/mini-ork/bin/mini-ork
- /Volumes/docker-ssd/ps/mini-ork/bin/mini-ork-execute
- /Volumes/docker-ssd/ps/mini-ork/mini_ork/ported/mini_ork_execute.py
- /Volumes/docker-ssd/ps/mini-ork/mini_ork/ported/mini_ork_cli.py
- /Volumes/docker-ssd/ps/mini-ork/tests/e2e/test_e2e_recipe_code_fix.sh
- /Volumes/docker-ssd/ps/mini-ork/tests/integration/test_bin_verify.sh
- /Volumes/docker-ssd/ps/mini-ork/tests/unit/test_mini_ork_verify_py.py
- /Volumes/docker-ssd/ps/mini-ork/lib/runtime-select.sh
- /Volumes/docker-ssd/ps/mini-ork/scripts/runtime-parity-harness.sh
- /Volumes/docker-ssd/ps/mini-ork/gates/feature_acceptance.sh
