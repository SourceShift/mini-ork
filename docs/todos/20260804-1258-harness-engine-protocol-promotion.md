# Follow-up — promote the A.3 engine seam to a named `HarnessEngine` Protocol

**Created:** 2026-08-04 12:58
**Status:** BLOCKED — gated on Phase B (openhands-sdk go/no-go)
**Origin:** SE-3 harness-engine standardization, decision record §11
(`internal-docs/research/2026-08-04-harness-engine-standardization.md`)

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
