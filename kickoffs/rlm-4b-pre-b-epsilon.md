# rlm-4b-pre-b: epsilon-greedy exploration on the brain side (decide)

## Goal

[framework-edit] ONE concern, ONE file. Add ε-greedy exploration to
`decide()` in `lib/decision_service.sh` so that routing through `decide()`
preserves the explore/exploit behavior the old `bin/mini-ork-execute` inline
argmax had (it is currently pure-exploit — the rlm-4b regression's second half).

After computing the learned/default `route`, with probability `EPSILON` return
an *exploration* lane instead, chosen deterministically under `SEED`. Otherwise
return the exploit `route` unchanged. Read the same `EPSILON`/`SEED` defaults the
prior execute argmax used (grep `bin/mini-ork-execute` git history or
`lib/lane_router.sh` for the existing default; do NOT invent a new default).

Do NOTHING else: no lane_router change (its floor is already fixed), no
`bin/mini-ork-execute` wiring (that is rlm-4b), no other files.

## Scope Hint

- `lib/decision_service.sh` (the `decide` function only)

## Requirements

- ε read from `EPSILON` env with the existing default; exploration selection
  seeded by `SEED` for determinism.
- Cold-start (slice below `${MO_LEARNING_MIN_SAMPLES:-3}`) still returns the
  configured default lane — exploration only applies once there is a learned
  lane to explore away from.
- Strict-mode stays guarded behind the direct-exec check (no source leak).
- `decide`'s JSON output keys unchanged (`route, coalition_ok, reward_estimate,
  recursion_hint`); exploration only changes the `route` value.

## Verification commands

- `shellcheck lib/decision_service.sh`

## Done When

- `bash -n lib/decision_service.sh` passes.
- With `EPSILON=0` `decide` is deterministic (always the exploit lane); with
  `EPSILON=1` it always returns an exploration lane — a smoke proving both ends.
- Sourcing the lib into a lenient shell does not enable `set -u`.
- `bash scripts/learning-loop-closure-gate.sh` still exits 0.
