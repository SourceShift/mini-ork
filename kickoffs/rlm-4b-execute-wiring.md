# rlm-4b: wire bin/mini-ork-execute through the decision service

## Goal

[framework-edit] Make the eng-team consumer adopt the shared decision surface:
`bin/mini-ork-execute` obtains its per-node lane routing by calling
`decide` from `lib/decision_service.sh` (created in rlm-4a) instead of reading
the lane directly. This proves the shared `decide()` surface in the path we
already trust before the book-gen consumer binds it. SCOPE IS ONE FILE.

## Scope Hint

- `bin/mini-ork-execute`

## Requirements

- Source/call `lib/decision_service.sh`'s `decide` for the routing decision at
  the existing lane-selection point; pass the node's `task_class`, `node_type`,
  and `objective_domain` (default `code-delivery` for eng-team runs).
- Behavior-preserving: when `decide` returns the configured default lane (the
  cold-start / `sample_size < 3` case), routing must match today's behavior —
  no regression to existing runs.
- Do not duplicate routing logic; `decide` is the single source of truth.

## Verification commands

- `shellcheck bin/mini-ork-execute`

## Done When

- `bash -n bin/mini-ork-execute` passes.
- `bash scripts/learning-loop-closure-gate.sh` still exits 0 (14/14) — routing
  through `decide` does not regress the learning loop.
- `bash scripts/smoke-learning-loops.sh` still passes 10/0 (no regression).

## REQUIRED before wiring (regression found in run-1782404269 — DEFERRED)

The naive wire regressed the learning knobs. Before execute adopts `decide()`,
these must land FIRST (this is now a 2-3 file change, not a 1-file wire):
1. `lib/lane_router.sh` `lane_router_preferred_lane` hardcodes `runs_count >= 3`.
   Make the floor read `MO_LEARNING_MIN_SAMPLES` (default 3) so the knob is live.
2. ε-exploration: the old inline argmax honored `EPSILON`/`SEED` (ε-greedy
   explore/exploit). `decide()`/`lane_router` implement no ε. Either relocate
   ε-greedy into `lib/decision_service.sh` (the "brain side") so execute→decide
   preserves explore/exploit, OR explicitly drop it and update
   `scripts/smoke-learning-loops.sh` expectations — decide deliberately.
3. Validate with BOTH `scripts/smoke-learning-loops.sh` (set MIN_SAMPLES=2, seed
   2 traces, expect learned lane) AND `learning-loop-closure-gate.sh` as
   verifier evidence before approval.

Until then the eng-team path keeps its working inline argmax (honors the knobs);
`decide()` remains available for the book-gen consumer.
