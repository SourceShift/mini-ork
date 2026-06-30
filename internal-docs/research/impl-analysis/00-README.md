# Implementation analysis — how leaders build what mini-ork lacks (2026-06-30)

Source-level analysis of cloned, real OSS repos, each mapped to a mini-ork gap and a concrete
adoption plan. Companion to `../2026-06-30-similar-orchestrators-and-agent-sandboxing.md` (survey)
and `../../../docs/audits/20260630-miniork-fix-tracker.md` (issue tracker, esp. **A1**).

Repos cloned to `/private/tmp/miniork-ref-analysis/` (shallow): mini-swe-agent, swe-rex,
e2b code-interpreter, OpenHands, gepa, mem0, llm-council.

## The docs

| # | Doc | Repos | mini-ork gap | Recs |
|---|---|---|---|---|
| 01 | `01-runtime-sandbox-swerex-minisweagent.md` | mini-swe-agent + swe-rex | **A1** — host-FS execution, no runtime/sandbox seam | R0/R1/R2 |
| 02 | `02-managed-sandbox-e2b-openhands.md` | E2B + OpenHands | managed cloud sandbox + controller/runtime split | R3 |
| 03 | `03-gepa-reflective-optimization.md` | GEPA | optimization cheaper/better than GRPO | R4 |
| 04 | `04-mem0-semantic-memory.md` | mem0 | semantic/long-term memory (vs SQL run-log) | — |
| 05 | `05-llm-council-panel-bias.md` | llm-council | panel anonymization + arbiter bias controls | I-1/I-6 |

## The one pattern that recurs (docs 01 + 02)
swe-rex, OpenHands, and E2B independently converge on the SAME shape, and it's exactly what
mini-ork lacks:
1. **One exec seam** the agent calls (`execute(action,cwd)`), backend-agnostic.
2. **Stateless actions** (subprocess-per-action) → swapping local→`docker exec`→microVM is a
   backend change, not a rewrite.
3. **Agent/runtime server runs *inside* the sandbox, reached over HTTP** → local==remote to the
   controller.
4. **Workspace moves by archive/file-transfer**, not a shared disk.
5. **Typed sandbox lifecycle** (start/wait-alive/pause/resume/delete) + pooling for cost.

mini-ork's `bin/mini-ork-execute` does the opposite (stateful host `cd`+`bash`, shared disk) —
which is why it can't go cloud and why a stray process clobbered a sibling repo.

## Recommended build sequence (highest leverage first)
1. **R0 — stateless exec seam** (`lib/runtime/contract.sh`: `mo_env_exec/put/get`); refactor
   `bin/mini-ork-execute` to call it; steal mini-swe-agent's process-group-kill-on-timeout.
   *Prerequisite for everything; a refactor, not new infra.*
2. **R2 — bubblewrap backend** (`lib/runtime/bubblewrap.sh`): only the workspace is writable →
   structurally prevents the cross-repo clobber. Cheap, no daemon (Linux/CI/cloud; keep `local`
   on macOS dev).
3. **R3 — docker → managed (E2B/Daytona)**: mini-ork agent-server-in-sandbox over HTTP +
   workspace archive (docs 02). Unlocks true cloud.
4. **R4 — GEPA reflective optimizer** alongside GRPO (`MO_OPTIMIZER=gepa`): minibatch-acceptance
   gate → ~35× fewer rollouts → attacks the cost circuit (I-4). Adapter = 2 methods over the
   existing trace store.
5. **Semantic memory (mem0 pattern)**: `lib/semantic_memory.sh` + sqlite-vec; extract learnings
   at reflect, retrieve at plan/context-assembly. Cross-run learning by meaning.
6. **Panel bias controls (llm-council)**: anonymized cross-review + position randomization +
   rank-aggregation into the existing gates (I-1 quorum dimension, I-6 bias).

R0→R2→R3 is the A1/cloud spine and should lead; R4/memory/panel are independent quality tracks.

## Status
All five analyses are documented. Next step (not yet done): turn R0→R3 into a scoped epic/design
and implement via mini-ork's own pipeline (per the 2+-file dispatch rule), once the dispatch
vehicle is trusted (note: framework-edit verdict.json was fixed — I-5 — and the code-fix
test-gate was fixed — I-7 — so dispatches should now land cleanly).
