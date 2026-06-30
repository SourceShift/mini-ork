# ADR-001 — Migrate mini-ork's core from Bash to Python

- **Status:** Proposed
- **Date:** 2026-06-30
- **Deciders:** maintainers
- **Related:** [Rivet-parity roadmap](./rivet-parity-roadmap.md) (Phase 0)

## Context

mini-ork's core — the universal loop (classify→plan→execute→verify→reflect→
improve→eval→promote), the recipe runner, `lib/llm-dispatch.sh`, the lane
lenses, and the GRPO learning loop — is implemented in Bash with SQLite state
and `jq`/heredoc JSON handling. The newest and hardest subsystems are *already*
Python: the FastAPI control plane (`mini_ork/web`, PR #34), `mini_ork/client.py`,
and traceotter (~888 LOC). The repo is therefore already bilingual, with Python
owning the parts that needed real data structures.

Bash has hit a structural ceiling. Nearly every defect fixed in the 2026-06-30
working session was a Bash/`exec`/process-fragility bug, not a logic bug:

| Defect | Bash root cause |
|---|---|
| codex lane dead fleet-wide (E2BIG) | `RAW_OUT="$big" python3` — `execve()` counts env+argv against `ARG_MAX` |
| shim reported `rc=0` on hard failure | `if cmd; then…; fi` with no `else` returns 0; `$?` after `fi` masks the rc |
| pre-push/post-commit hook clobbered HEAD | hand-rolled hooks `git reset` the worktree and don't restore on failure |
| orphaned runs, dangling `node_start` events | `.pid` / `.stop-requested` / `.cost-pause` sentinel-file coordination |
| framework-edit capture unreliable | diff-vs-implementer-worktree heredoc harvesting |

These are not isolated; they are the cost of orchestrating subprocesses, parsing
JSON, and coordinating concurrency in a language with no types, no exceptions,
no structured concurrency, and a `set -uo pipefail` minefield.

## Decision

**Migrate the mini-ork core to Python, incrementally (strangler-fig), starting
with `llm-dispatch`. Do not rewrite big-bang. Do not migrate to Rust/Go/TS.**

Python is chosen because:

1. **Ecosystem fit.** mini-ork is LLM-agent *orchestration logic*. That world —
   Anthropic/OpenAI SDKs, pydantic, FastAPI, the ML tooling universe — is
   Python-first. (Rivet chose Rust because Rivet is *infrastructure*, where
   cold-start/CPU/memory dominate. mini-ork's bottleneck is LLM **network
   latency**, so a systems language buys nothing.)
2. **Consolidation, not a third language.** ~40% of the value already lives in
   Python; this migrates the rest *onto* it.
3. **It deletes the bug classes above.** No shell `exec` layer (no `ARG_MAX`),
   real exceptions/return values (no `rc=0` masking), structured subprocess +
   DB-transaction coordination (no sentinel files), structured return values
   (no heredoc capture).
4. **Concurrency fits.** Parallel lenses are I/O-bound; `asyncio` is built for
   "fan out N model calls, await all." The GIL is irrelevant for
   network/subprocess-bound work; Bash's `&`/`wait`/FIFO juggling (which the
   `cl_codex.sh` comments admit wedges on macOS) disappears.

## Alternatives considered

| Option | Verdict | Why |
|---|---|---|
| **Python** | **Chosen** | Ecosystem-native, reuses existing code, kills the bug classes, lowest migration cost |
| TypeScript / Node | Rejected | Viable + strong async, but discards the Python investment and is less ML-native |
| Go | Rejected | Great concurrency + single-binary, but discards Python and is weak in the AI ecosystem; only wins if single-binary distribution were goal #1 (it isn't for a dev tool) |
| Rust | Rejected | Right for Rivet's runtime layer, wrong for an I/O-bound orchestrator; ~6-month rewrite to optimize the 2% that isn't network wait; small AI-logic contributor pool |
| Stay on Bash | Rejected | Structural ceiling demonstrated; blocks the actor/durable-execution roadmap |

## Consequences

**Positive**
- Eliminates the `ARG_MAX`, rc-masking, hook-clobber, sentinel-file, and
  capture-unreliability defect classes.
- `pydantic` models replace `jq`/heredoc JSON parsing — the LLM envelopes become
  typed contracts.
- `pytest` replaces fragile Bash test harnesses; CI gets faster and more honest.
- **Unlocks the Rivet roadmap.** Actor-per-run (T3.1) and durable-execution
  (T3.2) are natural in `asyncio` (or via a durable-execution lib like Temporal's
  Python SDK) and effectively impossible to do cleanly in Bash. This migration is
  the **prerequisite** for that work, not a parallel effort.

**Negative / risks**
- Migration effort across a large surface; mitigated by the strangler-fig phasing
  (ship value each phase, never a frozen rewrite).
- Python packaging/deps friction; mitigated by `uv` + a locked environment.
- Two languages coexist during transition; mitigated by a hard rule: new code is
  Python, Bash is only *called*, never *extended*.

## What we keep (do NOT rewrite)
- The SQLite schema + `db/migrations/` (the data model is sound).
- Recipes-as-data (`recipes/**/*.yaml`) — the orchestration *content*.
- The GRPO reward / PRM math and the `MO_*` / `MINI_ORK_*` env-knob surface
  (port the semantics 1:1; validate with the existing learning-loop tests).
- The FastAPI control plane (already Python — it becomes the spine).

## Migration plan (phased, strangler-fig)

**Phase 0 — Scaffold (no behavior change).**
- `uv`-managed package; `typer` CLI skeleton; `pydantic` models for the run,
  node, lane-result, and LLM-envelope shapes; a thin `subprocess`/`asyncio`
  dispatch primitive. New `bin/mini-ork` is Python and *shells out* to every
  unported subsystem. Parity test: Python entrypoint reproduces current CLI
  surface.

**Phase 1 — `llm-dispatch` (the part that keeps breaking).**
- Port `lib/llm-dispatch.sh` + the lane wrappers (`cl_*.sh`) to a Python
  `dispatch` module: typed provider adapters, prompt/stream via files/stdin (no
  env-var payloads → no E2BIG), real rc/exception propagation, token+cost
  sidecars as return values. Retire `test_d013_d014` / `test_cl_codex_e2big` once
  their conditions are structurally impossible.

**Phase 2 — The universal loop.**
- Port classify→plan→execute→verify→reflect as an `asyncio` workflow with a
  per-node journal (this is also roadmap T3.2's seed). Lenses fan out with
  `asyncio.gather`. State writes go through DB transactions, not sentinel files.

**Phase 3 — Recipes + epics/scheduler.**
- Recipe runner reads the existing YAML; the epics/scheduler queue becomes
  async tasks. Auto-merge + review gates wired through the control plane.

**Phase 4 — Retire Bash + the sentinel/hook machinery.**
- Replace `.pid`/`.stop-requested` coordination with the actor model (roadmap
  T3.1); replace hand-rolled git hooks with `lefthook` (roadmap T1.1). Delete the
  Bash core once every phase has a Python equivalent under test.

## Acceptance / done-ness
- Each phase ships behind tests with the Bash path still callable until the
  Python path reaches parity; no phase regresses the learning-loop validation.
- The migration is "done" when `lib/*.sh` core modules are deleted and CI runs
  only `pytest` + the control-plane smoke suite.
