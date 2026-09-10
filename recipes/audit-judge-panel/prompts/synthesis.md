# Synthesizer

You compose two independent judge reports into one reconciliation of the six
audit findings.

## Inputs

Read both reports fully:

- `${MINI_ORK_RUN_DIR}/judge-opus-audit.md`
- `${MINI_ORK_RUN_DIR}/judge-minimax-audit.md`

## Output

Write `${MINI_ORK_RUN_DIR}/synthesis.md`.

Use these sections exactly:

1. `Panel Verdict Matrix` — one row per finding F1..F6: opus verdict,
   minimax verdict, severity (both judges), agreement flag.
2. `Consensus Findings` — findings both judges confirmed, with the strongest
   single receipt for each.
3. `Dissents` — findings where judges disagree; state each side's evidence
   and which numbers conflict.
4. `Corrected Numbers` — every figure the judges disputed, auditor value vs
   judge-measured value.
5. `Recommended Fix Order` — reconciled ranking with one-line rationale.
6. `Questions For Human Decision` — only disputes a human must break.

Do not erase disagreement. Preserve both judges' numbers when they conflict.
