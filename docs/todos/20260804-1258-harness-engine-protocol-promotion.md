# Follow-up — promote the A.3 engine seam to a named `HarnessEngine` Protocol

**Created:** 2026-08-04 12:58
**Status:** RE-TRIGGER MET 2026-08-11 — Phase B assessed **NO-GO** on
openhands-sdk embed, but the named better re-trigger (opencode as a live lane)
**shipped the same day** (`46cd76fc`, opencode-native dispatch engine). Two
executable engines now exist (`EXECUTABLE_MODELS = {"codex","opencode"}`), so the
Protocol can finally be designed against a genuinely heterogeneous adapter pair
instead of claude+codex alone. Protocol promotion is now **actionable** (Task 1).
Items 3 (ratchet) and 4 (isolation selector) are already shipped; the opencode
engine preserved the ratchet by construction (no `ENGINE_COMMAND_BUILDERS` entry).
**Origin:** SE-3 harness-engine standardization, decision record §11
(`internal-docs/research/2026-08-04-harness-engine-standardization.md`)

## Phase B evaluation (2026-08-11)

Grounded in the live tree, not the roadmap:

- **openhands-sdk is absent** — not installed (`import openhands` fails), not in
  `pyproject`, 0 refs in `mini_ork/`. The only trace is the UI fork's JS
  `TestLLM` in `ui/playwright.mock-llm-docker.config.ts` — a frontend test
  harness, not the framework runtime.
- **RQ4 mismatch.** mini-ork's harnesses (claude, codex) are *whole autonomous
  agents* = OpenHands' entire `Agent+LLM+Runtime` stack, not its `LLM` layer.
  Embedding openhands-sdk means hosting its full agent stack (LiteLLM ~100
  providers + agent-server + Docker/Apptainer/Remote workspaces) as one
  heavyweight lane — a large dependency surface for a single engine.
- **No concrete second consumer.** Only two engines exist today
  (`EXECUTABLE_MODELS = frozenset({"codex"})` → `engine_of` ∈ {claude, codex}).
  Promoting the registry to a named 5-verb `spawn/converse/observe/...` Protocol
  now would validate it against claude+codex only — which the current
  `ENGINE_COMMAND_BUILDERS`/`MODEL_DISPATCH_BACKENDS`/`workspace=` registries
  already handle cleanly. That is exactly the premature-abstraction the §11 2/1
  consensus rejected; nothing has changed since 2026-08-04 to add a real second
  consumer.
- **The parts worth having already shipped natively.** The `workspace=`
  isolation axis is wired into dispatch (`_resolve_isolation` providers.py:663,
  `_spawn_in_workspace` core.py:110, local/docker/microvm backends), and the A.2
  boundary shape-check gives a typed-ish result contract. Embedding the sdk to
  get these buys nothing.

**Verdict.** NO-GO on embedding openhands-sdk as the trigger. Per this todo's own
rule, the Protocol promotion stays deferred (the registry is sufficient).

**Better re-trigger than openhands-sdk — NOW SATISFIED (2026-08-11).** opencode
(cross-family panel's worker-runtime pick) is now a live dispatch engine as of
`46cd76fc`: `kind: opencode-native` → a self-contained `opencode_transport.py`
sidecar (mirrors codex), `engine_of("opencode") == "opencode"`, dispatchable as a
lane, smoke-verified end to end (body/usage/cost from real opencode v1.18.4).
This is the concrete, differently-shaped second adapter that makes the Protocol
non-speculative — and it was far lighter than embedding openhands-sdk (one
transport module + registry wiring, no new deps). **Task 1 is therefore
unblocked:** derive the `HarnessEngine` verb set from the two executable adapters
that now exist (codex transport + opencode transport) plus the claude CLI —
three live engines, heterogeneously shaped. NO-GO on openhands-sdk stands; the
Protocol no longer needs it.

## Context

A.3 shipped the *minimum-durable-change* core (merged `64de9f2e`): a per-engine
command-builder registry (`ENGINE_COMMAND_BUILDERS` keyed by `engine_of(model)`)
that removed the inline `if model not in EXECUTABLE_MODELS` claude-arg
special-case from `dispatch_model`. The 2/1 subagent consensus (Builder ·
Reviewer · Future-Maintainer) deferred the full named
`spawn/converse/observe/capabilities/isolation` Protocol because betting a
five-verb interface shape before Phase B decides whether openhands-sdk is even
adopted would be churn if the answer is no-go.

The Reviewer's dissent (a next engine re-adds an inline special-case at a
different call site, and the seam rots) was answered by a **ratchet test**
(`test_engine_command_builder_py`: executable engines have no builder, so
claude-only argv cannot leak by construction) rather than by the Protocol.

## Trigger to unblock

Phase B's openhands-sdk **go** decision. If go: the sdk's `Agent`/`Conversation`
surface becomes the concrete second consumer that validates (or reshapes) the
verb vocabulary — promote then, with a real adapter to check the shape against.
If no-go: this todo likely stays a registry (the seam is sufficient); revisit
only if a third engine lands.

## Task

### 1. Design the `HarnessEngine` Protocol against two real adapters
- **Status:** not started
- **Last worked:** —
- **Remaining:** enumerate the verbs the two live engines (claude CLI, codex
  transport) actually need — argv-build (done, `ENGINE_COMMAND_BUILDERS`),
  spawn (`core.dispatch` today), telemetry-parse (`ProviderSpec.parse_*`),
  session/resume, isolation/workspace. Do NOT invent verbs no engine uses.
  Derive the Protocol from the two adapters, not from the roadmap prose.

### 2. Fold the sibling seams into the engine
- **Status:** not started
- **Last worked:** —
- **Remaining:** `ENGINE_COMMAND_BUILDERS` + `MODEL_DISPATCH_BACKENDS` +
  `ProviderSpec.parse_*` are three parallel per-lane registries. If the Protocol
  earns its keep, they collapse into one typed `HarnessEngine` object per
  engine. Keep the `register_*` OCP hooks working (or provide a shim) so
  external consumers don't break.

### 3. Preserve the ratchet
- **Status:** not started
- **Last worked:** —
- **Remaining:** whatever shape lands, keep an equivalent of the A.3 ratchet —
  a test proving an engine that must NOT receive claude argv cannot, by
  construction (missing capability), not by a maintained negative condition.

### 4. Isolation selector (A.3 sibling, RQ5)
- **Status:** not started (separately tracked in §8 item 5)
- **Last worked:** —
- **Remaining:** re-express local/docker/microVM as a `workspace=` axis on the
  engine. Coordinate with the cloud-exec sandbox roadmap
  (`project_miniork_cloud_exec_sandbox_roadmap`).

## Do NOT

- Do not build the Protocol speculatively before Phase B. The consensus
  explicitly rejected (a)-now; a five-verb interface with one consumer is the
  premature-abstraction failure mode this todo is documenting *away from*.
- Do not derive the verb set from the roadmap; derive it from the adapters.
