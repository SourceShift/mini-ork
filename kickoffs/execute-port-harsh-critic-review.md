# Harsh-critic review — the ported executor + runtime cutover

Adversarial cross-family panel (kimi + codex + opus) review of the bash→Python
migration's highest-risk surface, BEFORE the live-dispatch default is flipped.
Assume the code is theater until proven otherwise. Your job is to find where the
Python port silently diverges from the bash it claims to replace.

## Scope (read these, in order)

- `mini_ork/ported/mini_ork_execute.py` — the ported executor (helpers + GRPO +
  orchestration backbone + `dispatch_node` live routing + `main`).
- `bin/mini-ork-execute` — the 3449-line bash original it ports.
- `lib/runtime-select.sh` + the `mo_runtime_maybe_delegate` line wired into
  `bin/mini-ork*` — the cutover switch.
- `scripts/runtime-parity-harness.sh` + `scripts/live_dispatch_harness.py` — the gates.
- `tests/unit/test_mini_ork_execute_py.py` — the tests that claim it works.

## The claims to attack

1. **`dispatch_node` faithfully replicates `_dispatch_node`.** It does NOT — by
   the author's own admission it omits recipe-specific file-naming (schema-judge-panel,
   recursive-validate-impl), the recipe dispatchers (per_feature / epic / minimal-scaffold),
   the publisher's oracle-gates + artifact-contract commit, and trace/heartbeat
   side-effects. For EACH omission: does it change a node's pass/fail result, the
   artifact a downstream node reads, or a gate that blocks publish? Name a concrete
   recipe + node where the omission produces a DIFFERENT run outcome than bash.
2. **The live path is verified.** It is only tested with a FAKE dispatch. Find a
   real-provider behavior (agent tool-call Writes vs stdout, gateway capture
   coin-flip, mtime-marker preserve-agent-Write, MO_TARGET_CWD resolution) where
   the fake test passes but a real model would break the wiring.
3. **The reviewer verdict gate matches bash.** Compare the port's pass/revise/fail/
   unknown mapping and the synth-never-gates branch against the bash case statement
   line-by-line. Find a verdict string bash treats differently.
4. **The cutover shim is safe.** Find an entrypoint where `MINI_ORK_RUNTIME=python`
   delegates to a port that is NOT behaviorally equivalent, or where the shim breaks
   the bash default (set -e interaction, arg forwarding, cwd, env inheritance across
   the cascade), or a recipe dispatcher that shells `bin/mini-ork-execute` and now
   gets the Python one mid-run.
5. **The harness proves the cutover is safe.** It skips live dispatch and treats
   `--help` as exit-code-only. What real divergence class does it therefore NOT catch?

## Deliverable

`docs/reviews/execute-port-harsh-critic-<ts>.md` with, per confirmed divergence:
the file:line in the port, the bash line it should match, the concrete
recipe+node+input that triggers a different run outcome, and severity
(blocks-cutover / degrades-quality / cosmetic). End with an explicit verdict:
is the LIVE dispatch default safe to flip, and if not, the exact must-fix list.

Refute-or-promote: do not accept "looks equivalent". Every claim needs a concrete
triggering scenario or it is dropped. A missing recipe special-case is only a
finding if you can name the recipe that breaks.

## Success criteria

- Panel reviewed all scoped files (not skimmed).
- At least the 5 claims above each get an explicit confirmed/refuted with evidence.
- Every confirmed divergence names file:line (port) + file:line (bash) + a
  triggering recipe/node/input.
- Final go/no-go on flipping the live-dispatch default, with a must-fix list.

## Verification commands

- `python -m pytest tests/unit/test_mini_ork_execute_py.py -q`
- `bash scripts/runtime-parity-harness.sh`
- `python3 scripts/live_dispatch_harness.py`
