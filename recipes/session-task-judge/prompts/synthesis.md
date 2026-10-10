# Synthesizer

You compose three independent judge reports into one reconciliation of the
six pending session tasks.

## Inputs

Read all three reports fully, and all three structured sidecars:

- `${MINI_ORK_RUN_DIR}/judge-minimax-tasks.md`
- `${MINI_ORK_RUN_DIR}/judge-glm-tasks.md`
- `${MINI_ORK_RUN_DIR}/judge-deepseek-tasks.md`
- `${MINI_ORK_RUN_DIR}/context-minimax_judge.json`
- `${MINI_ORK_RUN_DIR}/context-glm_judge.json`
- `${MINI_ORK_RUN_DIR}/context-deepseek_judge.json`

The three sidecars carry the SAME schema (all lanes emit it), so the verdict
matrix is a mechanical join on `task_id` / `solution_id`. Read the markdown
for the reasoning behind each verdict; read the JSON for the verdicts
themselves. If a judge's markdown disagrees with its own JSON, say so —
that mismatch is itself a finding.

## Output

Write `${MINI_ORK_RUN_DIR}/synthesis.md`.

Use these sections exactly:

1. `Panel Verdict Matrix` — one row per task T1..T6: minimax verdict, glm
   verdict, deepseek verdict, severity (three judges), agreement flag. Then
   one row per solution P1..P6 in the same shape.
2. `Consensus Tasks` — tasks where at least two judges agree on the verdict,
   with the strongest single receipt for each. Split into "consensus:
   positive impact — do them" and "consensus: stale — drop them".
3. `Dissents` — every task or solution where the judges disagree; state each
   side's evidence and which premises conflict, and adjudicate against
   primary evidence where you can.
4. `Corrected Premises` — every premise of a task the judges disputed,
   session's claim vs judge-measured repo state.
5. `Recommended Execution Order` — the reconciled ranking, by task id, with
   one-line rationale. Mark dropped (stale) tasks explicitly as dropped
   rather than silently omitting them. Where a judge split a solution into
   sub-options (`P4(1)`, `P6(a)`), carry the sub-option ids through rather
   than collapsing them to the parent.
6. `Questions For Human Decision` — only disputes a human must break.

Do not erase disagreement. Preserve all three judges' verdicts when they
conflict.
