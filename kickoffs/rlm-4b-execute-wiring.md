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
