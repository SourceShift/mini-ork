# E5 — Trace continuity + UI projection + operator runbook

Depends on E1–E4. See design note §8, §9, §11. Makes recovery legible: one trace, DAG-native UI, and a runbook. Ships the remaining kickoff deliverables (#4 runbook, #5 external-orchestrator handoff).

## Goal
Correlate original + recovered work under one root trace, render recovery inside the existing run DAG, and give operators a runbook to inspect/recover/cancel/diagnose without deleting valid artifacts or repeating spend.

## Requirements
1. **Trace continuity**: all LLM calls, node attempts, and recovery attempts for one logical run correlate under one **root trace**. Use the caller-supplied root trace context when present (e.g. the Researcher compose planner); preserve it across runner/sandbox boundaries. A resumed attempt creates new child/attempt spans, not a disconnected synthetic trace. run/node/checkpoint/attempt ids become queryable trace attributes.
2. **UI projection**: recovery renders in the existing run DAG — completed nodes stay completed, the failed node shows its failed attempt + next action, new attempts nest beneath that node (not a fresh unrelated run). Read from the E1–E3 tables, not log scraping.
3. **Operator runbook** (deliverable #4): how to inspect (`recover --status`), recover, cancel a pending recovery (without invalidating prior checkpoints), and diagnose — including the difference between cost-pause resume, steering resume, and execution recovery.
4. **External-orchestrator handoff** (deliverable #5): a short doc on how a caller supplies a root trace, receives checkpoints, and requests recovery.
5. `recover --cancel <request_id>`: cancels a pending recovery without invalidating previous checkpoints (scenario: cancel a pending recovery).

## Files / areas in scope (touch ONLY these)
- The trace layer (`lib/trace_store.sh` / `mini_ork` trace modules) — root-trace propagation + attempt spans
- The web UI (`mini_ork/web/` + `ui/`) — DAG recovery projection + a by-run recovery view
- `internal-docs/` — the operator runbook + external-orchestrator handoff docs
- `tests/`
Do NOT change checkpoint/recovery/lease logic (E1–E3) or turn-resume (E4).

## Verification command
```bash
bash tests/run-all.sh unit && make web-test && python -m pytest tests/ -q -k "trace or recovery_ui"
```
Must exit 0.

## Acceptance
- Scenario 9: original + recovered LLM calls appear under the same logical trace/run, with distinct node-attempt spans.
- The UI shows a recovered run as one DAG with nested attempts, not two runs.
- `recover --cancel` leaves prior checkpoints valid.
- Runbook + handoff docs exist and cover inspect/recover/cancel/diagnose and the three resume types.
