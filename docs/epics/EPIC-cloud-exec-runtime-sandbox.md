# EPIC — Cloud-exec runtime abstraction + agent sandbox (A1)

> Closes architectural issue **A1** (`docs/audits/20260630-miniork-fix-tracker.md`): agents run
> directly on the host FS → blocks cloud execution + root-causes cross-repo git corruption.
> Grounded in `internal-docs/research/impl-analysis/` (swe-rex, mini-swe-agent, E2B, OpenHands).

**Goal:** introduce a backend-swappable execution seam so every recipe command runs through one
function, then add real filesystem isolation, then remote/cloud execution — without rewriting
recipes. Sequencing mirrors the convergent pattern found in the reference repos.

**Design invariants (from the reference impls):**
- One exec seam the executor/recipes call (`mo_runtime_exec`), never inline `cd`+`bash`.
- Actions are **stateless** (subprocess-per-action) so the backend is a config swap.
- Backends are interchangeable behind the seam, selected by `MO_RUNTIME_BACKEND`.
- Workspace is an explicit per-run dir that becomes the only writable path under isolation.
- Process-group kill on timeout (no orphaned children).

Note: `lib/sandbox/{modal,daytona}.sh` already exist as STUBS that fall back to local — this epic
makes the seam real.

---

## Delivery-safety constraints (MUST NOT slow/block researcher or any consumer)

Derived from real usage in the **researcher** consumer (`…/researcher/.mini-ork/state.db`):
264 runs / 20 days, **~29 concurrent**, dominated by code-fix (80), framework-edit (44),
epic-runner (33); runs up to 137 min; host is **macOS**; runs span many git worktrees under
`…/Development/worktrees/dsp-*`. Researcher's delivery velocity depends on these. Therefore:

1. **Default is `local`, zero added overhead.** `MO_RUNTIME_BACKEND` unset ⇒ byte-for-byte
   current behavior. No container/microVM boot, no extra fork, no new latency on the hot path.
   The 264-run cadence must be unaffected until a consumer explicitly opts in.
2. **No new hard dependency; degrade, never fail.** bubblewrap is Linux-only and the researcher
   host is macOS — a missing `bwrap`/`docker` must **auto-fall-back to `local` with a WARN**, never
   abort a run. (Mirror the existing `lib/sandbox/{modal,daytona}.sh` fall-back-to-local pattern.)
3. **Opt-in per run/recipe, not a global flip.** Trusted in-repo delivery (researcher's bread and
   butter) stays `local`; isolation is requested only for untrusted/cloud work. Never change the
   repo-wide default as part of this epic.
4. **Concurrency-safe, no new global lock.** ~29 parallel runs must not serialize. The seam uses
   per-run workspace dirs only (respect v0.6.0 per-run config isolation `lib/config_resolve.sh`);
   no shared mutable state, no global semaphore introduced by the runtime layer.
5. **Behavior-preserving refactor (R0b).** Routing the executor through the seam must not change
   verifier/reviewer/publisher semantics — existing recipe smoke + `pytest` stay green. Land R0b
   only behind the default-`local` path so a sync into researcher is a no-op until opted in.
6. **Safe to sync.** Everything additive + default-off so updating researcher's vendored
   `.mini-ork/` (its `mini-ork update` path) carries the runtime layer with **no behavior change**.
7. **Bonus velocity win, ship first:** the I-5 (verdict.json) + I-7 (test-gate) fixes already on
   source main directly cut researcher's current failure rate (last 24h: framework-edit 10 +
   code-fix 8 + epic-runner 5 failed). Syncing those into researcher's `.mini-ork/` is the
   fastest delivery-speed improvement and is independent of this epic.

**Net:** the runtime/sandbox layer is an *opt-in capability*, not a *mandatory gate*. It can only
make researcher faster (isolation prevents the cross-repo clobber that corrupts runs) and never
slower, because the default path is unchanged.

---

## Phase R0a — the exec seam + local backend (FIRST PIECE, additive, no behavior change)
**Deliverables**
- `lib/runtime/contract.sh` — defines + dispatches the runtime interface:
  `mo_runtime_exec "<command>" "<cwd>" [timeout_s]` → prints stdout, returns the command's exit
  code; `mo_runtime_put <local> <dest>`, `mo_runtime_get <src> <local>`; `mo_runtime_start` /
  `mo_runtime_stop` / `mo_runtime_alive` (no-ops for local). A factory sources the backend named
  by `MO_RUNTIME_BACKEND` (default `local`).
- `lib/runtime/local.sh` — local backend implementing the contract, wrapping current behavior:
  `subprocess`-equivalent via `setsid bash -c`, capturing stdout, and **killing the process
  group on timeout** (port mini-swe-agent's `start_new_session`+`killpg`).
- `tests/unit/test_runtime_contract.sh` — asserts: exec returns correct rc + stdout for success
  and failure; cwd is honored; timeout kills the whole process group (spawn a child, assert it's
  gone); backend factory selects `local` by default and errors clearly on unknown backend.

**DoD / smoke**
- `bash -n` clean; `bash tests/unit/test_runtime_contract.sh` green.
- Purely additive — nothing in `bin/` calls it yet, so existing tests still pass (`pytest` green).

**Scope guard:** touch ONLY `lib/runtime/*` + the new test. Do not modify `bin/mini-ork-execute`
in this phase.

## Phase R0b — route the executor through the seam (behavior-preserving refactor)
- Replace the direct `cd`+`bash` / `_run_verifier_ref` subshell call sites in
  `bin/mini-ork-execute` (and the verifier path) with `mo_runtime_exec`.
- Default backend `local` ⇒ byte-for-byte same behavior; the seam is now load-bearing.
- DoD: existing recipe smoke + `pytest` unchanged; a sample `code-fix` run completes as before.

## Phase R2 — bubblewrap backend (cheap FS isolation; stops the clobber)
- `lib/runtime/bubblewrap.sh` implementing the contract via
  `bwrap --unshare-user-try --ro-bind /usr /usr … --tmpfs /tmp --proc /proc --dev /dev
  --new-session --bind "$WORKSPACE" "$WORKSPACE" --chdir "$WORKSPACE" bash -c "$cmd"`.
  Only the run's workspace is writable ⇒ an agent cannot touch a sibling repo's `.git`.
- `MO_RUNTIME_BACKEND=bubblewrap`; auto-skip on non-Linux (macOS dev stays `local`).
- DoD: a test proving a write outside `$WORKSPACE` fails under bubblewrap but succeeds under local.

## Phase R5 — minimal-agent scaffold tier (native Python; runs AFTER R2)
**Why:** most nodes fire a full Claude/Codex CLI harness (60-turn cap, MCP, big startup) even for
bounded jobs (one-file mechanical fix, doc tweak, "run X and read output"). mini-SWE-agent's
insight: for bounded work a stateless bash-command loop with any model is cheaper, faster, more
deterministic. We **reimplement the pattern natively in Python** (NOT vendoring `mini-swe-agent` /
litellm) so it reuses mini-ork's own `mini_ork.dispatch` provider layer and the runtime seam.
Sequenced after R2 so every command the minimal agent runs goes through `mo_runtime_exec` →
sandboxed for free.

**Deliverables**
- `mini_ork/agent/minimal.py` — a ~linear-history agent: loop {prompt model for ONE next bash
  command → run it via the runtime seam → append output to messages → repeat} with a hard
  turn-cap + a `COMPLETE`/submit sentinel. Stateless actions (subprocess-per-action), no tool-
  calling API (works with any model), messages == trajectory (easy to trace/replay). Calls go
  through `mini_ork.dispatch` (the existing Phase-0 dispatch), and command execution shells out to
  the runtime seam (`mo_runtime_exec`) so it inherits local/bubblewrap/docker isolation.
- **Scaffold-tier router:** extend the classifier / `lib/lane_router.sh` with a `scaffold` axis
  alongside the model lane: `minimal` vs `harness`. Route by task complexity (start CONSERVATIVE —
  only clearly-bounded node types go `minimal`; default `harness`). Expose `MO_SCAFFOLD_TIER`
  override; the GRPO loop can learn the routing over time.
- A node-executor path that, when a node resolves to `scaffold=minimal`, runs `mini_ork.agent.minimal`
  instead of the full CLI harness.

**DoD / smoke**
- `python -m pytest tests/test_minimal_agent_py.py` — the loop solves a trivial bounded task
  (e.g. "create file X with content Y") in ≤N turns, against a stub/dummy model; asserts stateless
  actions + turn-cap + sentinel completion.
- Misroute safety: with `MO_SCAFFOLD_TIER` unset, behavior is unchanged (default `harness`).
- A bounded `code-fix`-style node completes via the minimal tier at materially lower cost/turns
  than the harness (record the delta).

**Constraints:** native Python under `mini_ork/`; no `mini-swe-agent`/litellm dependency; default
tier stays `harness` (opt-in/router-gated) so researcher + all consumers are unaffected until the
router promotes a node type. Minimal-agent command exec MUST go through the runtime seam.

## Phase R3 — docker, then managed (cloud)
- `lib/runtime/docker.sh` (`docker exec` into a per-run container; workspace mounted) +
  `mo_runtime_put/get` real impls.
- Managed: a mini-ork agent-server inside the sandbox reached over HTTP + workspace archive
  (OpenHands/swe-rex pattern), OR a thin bridge to E2B/Daytona for untrusted/cloud runs.
- DoD: a run completes end-to-end against `MO_RUNTIME_BACKEND=docker`; cloud backend smoke.

---

## Dispatch plan
Each phase is one scoped `code-fix` kickoff (single deliverable, per the 2+-file dispatch rule).
**Order: R0a → R0b → R2 → R5 (minimal-agent tier) → R3.** R5 runs after R2 so the minimal agent's
command execution inherits the sandbox. Start with R0a — additive, low-risk, validates the
now-fixed dispatch vehicle (I-5 verdict.json + I-7 test-gate). Verify each phase before the next;
keep `local` the default backend and `harness` the default scaffold tier until explicitly opted in.

**Self-improve flywheel:** as each phase lands it is turned on for the loop's OWN next iterations —
R2 sandbox makes the self-builder run isolated; R5 routes the loop's bounded sub-tasks to the cheap
minimal tier; later GEPA/memory/panel sharpen the loop's optimize/recall/verify. Consumers
(researcher) only get a phase via opt-in / `mini-ork update`, never a forced default flip.
