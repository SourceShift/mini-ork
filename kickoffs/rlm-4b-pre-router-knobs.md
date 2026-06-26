# rlm-4b-pre: make the learning router knob-honoring + ε-aware (unblocks 4b)

## Goal

[framework-edit] Before `bin/mini-ork-execute` can route through `decide()`
without regressing the learning loop, the brain-side router must honor the knobs
the old inline argmax honored. Two focused changes, two files:

1. `lib/lane_router.sh`: `lane_router_preferred_lane` currently hardcodes its
   `runs_count >= 3` sample floor. Make it read `MO_LEARNING_MIN_SAMPLES`
   (default 3) so the knob is live again.
2. `lib/decision_service.sh`: add ε-greedy exploration on the brain side, using
   `EPSILON` (default current value) and `SEED`, so `decide()` returns an
   exploration lane with probability ε and the learned lane otherwise. This is
   the behavior the rlm-4b diff comment claimed ("exploration belongs on the
   brain side") but never implemented.

This does NOT wire `bin/mini-ork-execute` (that stays rlm-4b, dispatched after
this lands).

## Scope Hint

- `lib/lane_router.sh`
- `lib/decision_service.sh`

## Requirements

- `lane_router_preferred_lane` floor = `${MO_LEARNING_MIN_SAMPLES:-3}`, not a
  literal 3.
- `decide()` ε-greedy: with probability `EPSILON` (read the existing default),
  pick an exploration lane deterministically under `SEED`; else the learned
  lane. Cold-start (slice below the floor) still returns the configured default.
- Strict mode stays guarded behind the direct-exec check (no source leak).
- No change to `bin/mini-ork-execute` in this kickoff.

## Verification commands

- `shellcheck lib/lane_router.sh lib/decision_service.sh`

## Done When

- `bash -n lib/lane_router.sh lib/decision_service.sh` pass.
- `MO_LEARNING_MIN_SAMPLES=2` path: `scripts/smoke-learning-loops.sh` passes
  10/0 (a 2-trace seeded slice now routes to the learned lane, not the default).
- `bash scripts/learning-loop-closure-gate.sh` still exits 0 (14/14).
- Sourcing either lib into a lenient shell does not enable `set -u`.
