# E4 — Turn-level resurrection: `claude --resume` + durable session file + tool receipts

Depends on E1–E3 (the correctness foundation must exist first). See design note §1, §6. This is the operator's priority-A ("resurrect a failed node at turn 9"). It is a **continuation optimization**, not a checkpoint — scope it as such.

## Goal
Resume a failed node's agent at its interrupted turn (e.g. GLM critique that died at turn 9) by continuing the vendor session, surviving sandbox death, without re-firing side-effecting tools.

## Requirements
1. **Capture the provider session id** per node attempt (claude emits `session_id` in its JSON output) → `node_attempts.provider_session_id`.
2. **Persist the CLI session store durably.** The claude session store is the jsonl transcript under `~/.claude/projects/<hash>/*.jsonl`. On checkpoint AND on recoverable failure, copy that jsonl into the run dir / durable store; record it as `node_checkpoints.session_ref`. On a fresh sandbox, restore the jsonl into `~/.claude/projects/…` before resuming — this is what makes turn-resume survive worker/sandbox death (design §6, scenario 2).
3. **Resume via `claude --resume <session_id>`** for a `retry`/`repair` on a node with a session_ref. Only kicks in for lanes routed through the claude binary (opus/sonnet/kimi/minimax/glm). **codex lane: node-level resume only** in v1 (it has its own session model) — document it, don't fake it.
4. **Tool receipts**: persist a receipt (input+output) for every side-effecting tool call BEFORE the node is considered done. On any replay, a completed non-idempotent tool returns its receipt and is NEVER re-invoked. Read-only tools may replay per strategy.

## Files / areas in scope (touch ONLY these)
- `lib/llm-dispatch.sh` + `mini_ork/dispatch/providers.py` (capture session id; add `--resume` on recovery)
- The checkpoint writer (E1) — persist/restore the session jsonl + tool receipts
- A new tool-receipt store (Python)
- `tests/`
Do NOT change node-checkpoint validity (E1) or the recovery planner (E2).

## Verification command
```bash
bash tests/run-all.sh unit && python -m pytest tests/ -q -k "session or receipt or resume_turn"
```
Must exit 0. No test may call a real paid model — stub the claude CLI + session jsonl.

## Acceptance
- A node that stopped at max-turns resumes via `--resume <session_id>` and continues the same conversation (verified with a stub claude that asserts it received `--resume`).
- With the session jsonl persisted, a simulated sandbox death (delete `~/.claude/projects/…`) still resumes after restore.
- Scenario 8: a side-effecting tool that already ran is NOT re-invoked on replay — its receipt is returned.
- codex nodes fall back to node-level resume, documented.
