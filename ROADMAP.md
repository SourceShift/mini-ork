# Roadmap

This is a living document. Items move between buckets as priorities shift.
See [GOVERNANCE.md](./GOVERNANCE.md) for how decisions get made.

## Released

### v0.0.0-extract — 2026-05-30
Literal port of an internal multi-agent orchestrator into a standalone repo.
Preserved at git SHA `0ec2bf1` for diff/reference.

### v0.1.0-redesign — 2026-05-30
Architectural inversion: framework ships primitives (universal task loop, 8
node types, 6 edge types, 6 gates, 8 memory namespaces); pipeline shapes
live in `recipes/`. Two reference recipes ship: `code-fix` (minimal) and
`bdd-first-delivery` (multi-stage migration target for the literal port).

## Next (v0.2 — Q3 2026 target)

The memory + reflection layer becomes live, not just stubbed:

- `lib/reflection_pipeline.sh` actually runs background gradient extraction
  on completed runs (currently the primitive exists but isn't wired into the
  bin loop).
- `lib/pattern_store.sh` detects emergent patterns across runs and surfaces
  them as proposed workflow changes.
- `lib/benchmark_suite.sh` gains a built-in seed task set (one per task class)
  so users can `mini-ork eval --candidate <id>` against shipped benchmarks
  immediately after install.
- More starter recipes per the book's task-class table:
  - `recipes/research-synthesis/`
  - `recipes/blog-post/`
  - `recipes/ui-audit/`
  - `recipes/db-migration/`
  - `recipes/ops-runbook/`

## Later (v0.3+ — Q4 2026 / 2027)

The evolution + promotion layer becomes live:

- `lib/group_evolver.sh` proposes workflow candidates based on accumulated
  trace + pattern data; `mini-ork improve` materializes them.
- `lib/promotion_gate.sh` enforces utility-delta + benchmark-pass + safety
  checks before promoting a candidate to the active workflow.
- `lib/version_registry.sh` exposes rollback as a first-class CLI verb:
  `mini-ork rollback <workflow|agent> <name>`.
- A web dashboard (separate repo) reads state.db read-only for visualisation
  of: task_runs by status, agent performance trends, candidate utility deltas,
  pending promotions awaiting human gate.

## Eventually (v1.0)

- Hardened multi-machine state (PostgreSQL backend as an alternative to
  sqlite for teams; same schema)
- A standard plugin protocol so third-party verifier scripts can be installed
  via `mini-ork plugin install <name>`
- Optional remote LLM-call telemetry (opt-in only) for cross-project
  benchmark sharing
- Stability guarantees: SemVer with documented breaking-change policy; every
  v1.x is backward-compatible with all prior v1.y

## Out of scope

These have been considered and intentionally excluded:

- Hosted SaaS version — keep the runtime local-first; users can build their
  own hosted layer on top
- Built-in LLM provider — the framework is provider-neutral; new providers
  plug in via `lib/providers/cl_<name>.sh`
- GUI bundled in the same repo — separate concern; the dashboard repo will
  consume state.db as a read-only contract
- Anything that breaks the bounded-autonomy axioms in [docs/SAFETY.md](./docs/SAFETY.md):
  silent self-mutation, hidden rollback, promotion without measurable utility

## How to influence the roadmap

1. Open a Discussion describing the use case you want to unlock
2. If there's interest, propose it as an Issue with a draft RFC
3. Build a recipe that demonstrates the pattern before asking for framework
   primitives — recipes can graduate into the framework once 2+ use the same
   abstraction

## Last updated

2026-05-30 (initial)
