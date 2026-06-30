# Rivet-parity roadmap — best practices to adopt into mini-ork

> Source: comparison of `rivet-dev/rivet` (Apache-2.0 Rust actor runtime, 5.6k★)
> against mini-ork (Bash + SQLite LLM orchestration framework). Written 2026-06-30.

Rivet and mini-ork sit at **different layers** — Rivet is *infrastructure you build
agents on*; mini-ork is an *orchestration harness that drives LLMs to do work*. They
are not competitors. The value is in borrowing Rivet's **programming model** (actors,
durable workflows, co-located state, observability) and its **repo hygiene**
(declarative hooks, schema-first SDKs, dependency/format gates, benchmarks-with-
methodology) — **not** its planetary-scale infra (global edge, infinite horizontal
scale, multi-region placement), which is out of scope for a single-machine dev
orchestrator.

Each item: **what Rivet does → what mini-ork does today → target → acceptance →
effort → autonomous-safe?** "Autonomous-safe" means it can be applied by a mini-ork
dispatch and mechanically verified without risking the review/safety gates.

---

## Phase 0 — Language migration (Bash → Python) ⭐⭐⭐ — gates everything below

> Full rationale + phased plan: **[ADR-001](./adr-001-python-migration.md)**.

The single highest-leverage change, and a prerequisite for the Tier-3 epics.
mini-ork is LLM-agent *orchestration logic*; that ecosystem is Python-first, ~40%
of the value is already Python (FastAPI control plane, client, traceotter), and
nearly every defect this session was a Bash/`exec`/process-fragility bug (E2BIG
`ARG_MAX`, `if`-no-`else` rc masking, hook worktree clobber, sentinel-file
coordination, heredoc capture). **Not** Rust (right for Rivet's runtime layer,
wrong for an I/O-bound orchestrator) — see ADR-001 alternatives.

- **P0.1** Scaffold: `uv` package, `typer` CLI, `pydantic` models, async dispatch
  primitive; Python `bin/mini-ork` shells out to unported subsystems.
- **P0.2** Port `llm-dispatch` + lane wrappers (prompt/stream via files/stdin →
  no E2BIG; real rc/exceptions). **Start here.**
- **P0.3** Port the universal loop as an `asyncio` workflow with a per-node
  journal (also seeds T3.2).
- **P0.4** Port recipes + epics/scheduler; retire Bash + sentinel/hook machinery
  (folds in T1.1 lefthook + T3.1 actor model).
- **Keep:** SQLite schema/migrations, recipes-as-data YAML, GRPO/PRM math, the
  `MO_*` env surface, the FastAPI control plane.
- **Effort:** XL (phased, strangler-fig — ships value each phase). **Autonomous-safe:**
  ❌ human-led; individual phase slices may be dispatched once Python scaffolding exists.

Tiers 1–3 below assume Phase 0 is underway: prefer landing each item in **Python**
where the surface has already been ported, rather than extending Bash.

---

## Tier 1 — Quick wins (autonomous-safe, low architectural risk)

### T1.1 — Declarative git hooks (lefthook) ⭐
- **Rivet:** `lefthook.yml` declaratively wires git hooks.
- **mini-ork today:** hand-written `.githooks/pre-push` + `post-commit`. The
  post-commit watchdog and an external process have clobbered HEAD this session
  (reset to a foreign `refs/codex/curated-sync`). Hand-rolled hooks are the bug
  surface.
- **Target:** adopt `lefthook.yml` as the hook manager; keep the existing checks
  (readme-claim-check, pre_push_review, reversion-guard) as *commands lefthook
  invokes*, not as bespoke scripts that mutate the worktree.
- **Acceptance:** `lefthook install` wires pre-push + post-commit; Layer-1 claim
  check and Layer-3 reviewer still fire on push; no hook performs `git reset`/
  `checkout` of the user's branch; `tests/unit/test_post_commit_head_guard.sh`
  still passes.
- **Effort:** M. **Autonomous-safe:** ⚠️ partial — porting the Layer-3 reviewer
  gate is delicate; do the scaffolding autonomously, gate the reviewer wiring on
  human review.

### T1.2 — Schema-first API + generated client ⭐ (control-plane, PR #34)
- **Rivet:** defines its API once (`rivetkit-json-schema` / `-openapi` /
  `-asyncapi`) and generates TS/Python/Rust/Swift clients.
- **mini-ork today:** `mini_ork/client.py` and the `mini_ork/web/routes/*`
  handlers (launch/steer/stop/kill/profile/answers from PR #34) are hand-written
  and drift independently.
- **Target:** author a single OpenAPI 3.1 spec (`schemas/control-plane.openapi.yaml`)
  describing every PR #34 endpoint + request/response shape; add a CI check that
  the spec matches the live FastAPI routes; (stretch) generate the Python client
  from the spec so server + SDK never drift.
- **Acceptance:** spec validates (`openapi-spec-validator`); a test asserts each
  documented path/method exists in the FastAPI app and vice-versa (no
  undocumented or phantom routes); client smoke test still green.
- **Effort:** M. **Autonomous-safe:** ✅ (spec + drift test). Client codegen:
  stretch.

### T1.3 — `AGENTS.md` + publishable coding-agent skill
- **Rivet:** ships `AGENTS.md` and `npx skills add rivet-dev/skills`.
- **mini-ork today:** has a `CLAUDE.md` + an internal `mini-ork` skill.
- **Target:** add a tool-agnostic `AGENTS.md` (mirrors CLAUDE.md guidance for
  Cursor/Windsurf/etc.); document how to install the `mini-ork` skill.
- **Acceptance:** `AGENTS.md` exists, links the recipe catalogue + lane policy;
  README references it.
- **Effort:** S. **Autonomous-safe:** ✅

### T1.4 — Format + dependency/policy gates
- **Rivet:** `biome.json`, `rustfmt.toml`, `deny.toml` (cargo-deny), `.editorconfig`.
- **mini-ork today:** shellcheck + GitGuardian + readme-claim-check; no formatter,
  no editorconfig, OSS-vet rules enforced by hand.
- **Target:** add `.editorconfig`; add `shfmt` config + a `make fmt`/CI format
  check for bash; add a `deny`-style policy file encoding the OSS-vet rules (no
  absolute `/Users`/`/Volumes` paths, no API keys, no foreign-repo paths) as a
  scriptable gate.
- **Acceptance:** `.editorconfig` present; `shfmt -d` clean on `lib/ bin/ tests/`;
  the OSS-vet policy gate runs in CI and passes on a clean tree, fails on a
  planted violation.
- **Effort:** S–M. **Autonomous-safe:** ✅

### T1.5 — `justfile` + `examples/`
- **Rivet:** `justfile` for canonical dev commands; `examples/` per use case.
- **mini-ork today:** commands scattered across `bin/` + `scripts/` + `make`.
- **Target:** a `justfile` aliasing the canonical flows (init, run, scheduler,
  test layers, fmt, serve); an `examples/` dir with 2–3 runnable kickoff+recipe
  pairs.
- **Acceptance:** `just --list` shows the canonical commands; each example has a
  README and a one-command invocation.
- **Effort:** S. **Autonomous-safe:** ✅

### T1.6 — Benchmarks-with-methodology ⭐
- **Rivet:** README publishes hard numbers *with* a reproducible methodology block.
- **mini-ork today:** "self-improving, no reward model" is asserted, not measured.
- **Target:** a `bench/` script that measures learning-loop value — routing-regret
  reduction over N runs, cost saved vs the static map, lane-win convergence — and
  emits a methodology block for the README/blog.
- **Acceptance:** `bench/learning-loop.sh` runs offline (`MINI_ORK_DRY_RUN=1` with
  synthetic traces) and prints reproducible numbers + methodology; README gains a
  "Benchmarks" section.
- **Effort:** M. **Autonomous-safe:** ✅ (offline/synthetic).

---

## Tier 2 — Moderate (mostly the observability/web surface)

### T2.1 — Built-in inspector ⭐
- **Rivet:** live SQLite viewer, workflow-state stepper (steps + retries), event
  monitor, REPL.
- **mini-ork today:** `mini_ork/web` obs surface + the PR #34 SSE stream (≈60% there).
- **Target:** a live `state.db` viewer; a run-stepper showing each node's
  retries/verdicts; an event tail over `run_events`; a REPL to invoke a
  lane/recipe interactively.
- **Effort:** L. **Autonomous-safe:** ⚠️ (frontend — human-reviewed).

### T2.2 — Three deployment tiers, one engine
- **Rivet:** *just a library* (in-process) → *self-host* (single binary/Docker) →
  *managed cloud*.
- **Target:** make the ladder explicit — local CLI (today) → `docker run mini-ork
  serve` (the control plane, packaged) → multi-tenant. Document the progression.
- **Effort:** M. **Autonomous-safe:** ⚠️ (Dockerfile + docs autonomous; multi-tenant later).

---

## Tier 3 — Architectural epics (human-gated; design-first, NOT one-shot)

### T3.1 — Actor-per-run ⭐⭐
- **Rivet:** one durable, addressable, long-lived process per unit of work; state
  co-located in-memory + auto-persisted.
- **mini-ork today:** a run is a detached bash PID tracked by sentinel files
  (`.pid`, `.stop-requested`, `.cost-pause`) with a `pgrep`-by-run-id fallback in
  `kill_run` — the source of orphan processes + dangling `node_start` events on
  SIGKILL.
- **Target:** model each `task_run` as a supervised actor — one owner process,
  co-located run-dir state, a steering queue, a scheduler, an SSE channel; a
  single addressable handle replaces sentinel-file coordination.
- **Effort:** XL. **Autonomous-safe:** ❌ design + staged delivery.

### T3.2 — Durable execution / replayable loop ⭐⭐
- **Rivet:** workflows checkpoint each step, auto-retry, deterministically replay
  after a crash.
- **mini-ork today:** the universal loop runs in one bash process; a host crash
  loses it. State is in `state.db` but there is no step-replay journal.
- **Target:** a per-node journal so a crashed run resumes from the last completed
  node; makes the framework-edit publisher/rollback chain crash-safe.
- **Effort:** XL. **Autonomous-safe:** ❌

### T3.3 — Hibernation / scale-to-zero
- **Rivet:** runs when active, sleeps when idle, $0 idle.
- **mini-ork today:** a slow lane holds a live process and burns the
  `MO_NODE_TIMEOUT_S` budget while merely *waiting on the provider*.
- **Target:** checkpoint-and-sleep a run blocked on an LLM call; wake on
  completion/next poll, so many runs coexist without N live processes.
- **Effort:** L–XL. **Autonomous-safe:** ❌ (depends on T3.1).

### T3.4 — Durable per-run queue (generalize operator_steering)
- **Rivet:** `for await (const msg of c.queue.iter())`.
- **mini-ork today:** `operator_steering` rows + HTTP `steer_run` are a poor-man's
  per-run queue.
- **Target:** a real durable queue per run carrying steering, HITL approvals, and
  tool requests with iterate-and-ack semantics.
- **Effort:** L. **Autonomous-safe:** ❌ (couples to T3.1).

---

## Dispatch plan

- **Now:** kick off **Phase 0** (ADR-001) — P0.1 scaffold, then P0.2
  `llm-dispatch` port. This is the gating work; everything else lands cleaner once
  it's underway.
- **Cheap parallel wins (language-agnostic, safe to do in Bash today):** T1.3
  (AGENTS.md), T1.4 (.editorconfig + OSS-vet policy gate), T1.5 (justfile) — these
  are repo-hygiene files that don't extend the Bash core, so they don't fight the
  migration.
- **Do in Python as P0 reaches the relevant surface:** T1.2 (OpenAPI schema +
  drift test — the control plane is already Python), T1.6 (benchmarks).
- **Human-gated:** T1.1 (lefthook — folds into P0.4), Tier 2.
- **Design-first epics (require Phase 0):** T3.1 → T3.2 → T3.3/T3.4 (actor model
  is the prerequisite for the durable-execution + hibernation + queue work, and is
  natural in `asyncio`, impractical in Bash).

## What NOT to copy
Global edge network, infinite horizontal scale, multi-region placement, managed-
cloud control plane. mini-ork is a single-machine dev orchestrator; chasing
distributed-infra parity is scope creep. Borrow the model, not the datacenter.
