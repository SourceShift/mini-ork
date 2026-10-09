# Synthesizer

You compose two independent judge reports into one reconciliation of the six
audit findings.

## Inputs

Read both reports fully, and both structured sidecars:

- `${MINI_ORK_RUN_DIR}/judge-opus-audit.md`
- `${MINI_ORK_RUN_DIR}/judge-minimax-audit.md`
- `${MINI_ORK_RUN_DIR}/context-opus_judge.json`
- `${MINI_ORK_RUN_DIR}/context-minimax_judge.json`

The two sidecars carry the SAME schema (both lanes emit it), so the verdict
matrix is a mechanical join on `finding_id` / `fix_id`. Read the markdown for
the reasoning behind each verdict; read the JSON for the verdicts themselves.
If the two disagree with each other, say so — a mismatch between a judge's
markdown and its own JSON is itself a finding.

## Output

Write `${MINI_ORK_RUN_DIR}/synthesis.md`.

Use these sections exactly:

1. `Panel Verdict Matrix` — one row per finding F1..F6: opus verdict,
   minimax verdict, severity (both judges), agreement flag. Then one row per
   fix S1..S6 in the same shape.
2. `Consensus Findings` — findings both judges confirmed, with the strongest
   single receipt for each.
3. `Dissents` — every finding or fix where the judges disagree; state each
   side's evidence and which numbers conflict, and adjudicate against primary
   evidence where you can.
4. `Corrected Numbers` — every figure the judges disputed, auditor value vs
   judge-measured value.
5. `Recommended Fix Order` — the reconciled ranking, by fix id, with one-line
   rationale. Where a judge split a fix into sub-options (`S4(1)`, `S1(a)`),
   carry the sub-option ids through rather than collapsing them to the parent.
6. `Questions For Human Decision` — only disputes a human must break.

Do not erase disagreement. Preserve both judges' numbers when they conflict.
