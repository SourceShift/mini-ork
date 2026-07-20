# Close the `cli` integration fork

Status: preflight complete on 2026-07-20; implementation not yet started.

## Goal

Make `mini_ork/ported/mini_ork_cli.py` the sole top-level dispatcher and replace
the Bash implementation at `bin/mini-ork` with a thin Python launcher. Preserve
the installed command path, user-facing CLI contract, run lifecycle, and every
already-closed Python fork. Repoint every runtime caller and retire the Bash
dispatcher implementation as one reviewable migration diff.

## Fork

- **fork:** `cli`
- **isolated target:** `/private/tmp/mini-ork-self-migrate-cli`
- **python implementation:** `/private/tmp/mini-ork-self-migrate-cli/mini_ork/ported/mini_ork_cli.py`
- **Bash implementation to retire:** `/private/tmp/mini-ork-self-migrate-cli/bin/mini-ork`
- **public launcher path to preserve:** `/private/tmp/mini-ork-self-migrate-cli/bin/mini-ork`

Unlike command forks named `bin/mini-ork-<fork>`, CLI closure must not remove
the public `bin/mini-ork` path. It must replace that file with a Python launcher
whose shebang and runtime contain no Bash delegation.

## Pre-retirement evidence

- `tests/unit/test_mini_ork_cli_py.py`: 6 passed against the live Bash oracle.
- `tests/integration/test_bin_dispatcher.sh`: 40 assertions passed.
- `python3 -m pyright mini_ork/ported/mini_ork_cli.py`: 0 errors.
- `pre-retirement-parity.sh` recorded a passing CLI oracle under
  `/private/tmp/mini-ork-cli-preflight`.

## Outbound seams to close or preserve explicitly

- Direct dispatch for already-closed `classify`, `plan`, `verify`, and
  `reflect` forks must invoke their Python modules, never deleted Bash paths.
- The run lifecycle must continue to call the native classify, plan, verify,
  and reflect modules and preserve execute as the one remaining Bash command
  fork until the execute migration closes.
- `_deadline`, repo-integrity, run-config snapshot, and rubric pre-screen shell
  calls are library seams below the CLI frontier. Keep their observable
  contracts explicit; do not silently drop them during dispatcher retirement.
- Generic subcommand dispatch may retain live `bin/mini-ork-<sub>` commands,
  but it must never recreate references to already-retired entrypoints.

## Inbound references to resolve

- `install.sh` and packaging/install tests that install or symlink
  `bin/mini-ork`.
- `.github/workflows/ci.yml` CLI initialization calls.
- `mini_ork/scheduler.py`, `mini_ork/ported/mini_ork_scheduler.py`,
  `mini_ork/web/control.py`, and `mini_ork/web/routes/dispatch.py`.
- `lib/sandbox/local.sh`, `bin/mini-ork-spawn`, `bin/mini-ork-scheduler`,
  `bin/mini-ork-self-improve`, and research/learning scripts that launch a run.
- CLI unit, dispatcher integration, security, E2E, live, and web tests that
  execute or inspect the public launcher.
- `lib/runtime-select.sh`, `scripts/runtime-parity-harness.sh`, and all
  self-migrate verifiers must understand CLI's preserved Python launcher.

Historical documentation may retain literal command examples such as
`bin/mini-ork run`; those are public CLI usage, not references to a Bash
implementation. Runtime closure is defined by interpreter/runtime behavior,
not removal of the public command string.

## Acceptance criteria

- Durable pre-retirement parity remains available after the Bash body is gone.
- `bin/mini-ork` exists, is executable, and launches the Python CLI without
  sourcing Bash or consulting `MINI_ORK_RUNTIME`.
- Direct `classify`, `plan`, `verify`, and `reflect` dispatch reaches the native
  Python modules; `execute` remains a live command fork until its own closure.
- Help, version, doctor, unknown-command, deadline validation, explicit recipe,
  inferred recipe, strict profile, and dry-run lifecycle behavior remain green.
- `tests/unit/test_mini_ork_cli_py.py` becomes a standalone Python contract and
  no longer reads/extracts the retired Bash implementation.
- CLI feature acceptance, focused Pyright, static-feature ledger, and the
  CLI-specialized fork-closure gate all pass.
- No provider credential value, run artifact, temporary adapter, or user-owned
  `.mini-ork/config/agents.yaml` change enters the diff.

## Verification commands

- `python3 -m pytest tests/unit/test_mini_ork_cli_py.py -q -p no:cacheprovider`
- `bash tests/integration/test_bin_dispatcher.sh`
- `python3 -m pyright mini_ork/ported/mini_ork_cli.py`
- `MO_FORK=cli bash recipes/self-migrate/verifiers/parity.sh`
- `MO_FORK=cli bash recipes/self-migrate/verifiers/feature-acceptance.sh`
- `MO_FORK=cli bash recipes/self-migrate/verifiers/ledger-shape.sh`
- `MO_FORK=cli bash recipes/self-migrate/verifiers/fork-closure.sh`

## Provider policy

- Kimi: planner and broad discovery.
- Codex: migrator/implementer.
- GLM 5.2: seam map, static-feature ledger, and reviewer.
- MiniMax and DeepSeek are forbidden for this migration.
- Load Kimi and GLM credentials process-locally from
  `/Users/admin/ps/scripts/cl_kimi.sh` and `cl_glm.sh`; never persist values.
- Never trigger a paid retry or repair loop without explicit user approval.

## Files in scope

- `bin/mini-ork`
- `mini_ork/ported/mini_ork_cli.py`
- `tests/unit/test_mini_ork_cli_py.py`
- `tests/integration/test_bin_dispatcher.sh`
- `install.sh`
- `lib/runtime-select.sh`
- `scripts/runtime-parity-harness.sh`
- `gates/feature_acceptance.sh`
- `recipes/self-migrate/verifiers/pre-retirement-parity.sh`
- `recipes/self-migrate/verifiers/parity.sh`
- `recipes/self-migrate/verifiers/feature-acceptance.sh`
- `recipes/self-migrate/verifiers/fork-closure.sh`
- Runtime callers of `bin/mini-ork` discovered by the seam map.
- Current CLI/operator/migration documentation describing the implementation.

