# Judge: MiniMax (MiniMax-M3) — audit findings verification

You are the MiniMax judge. Six audit findings about this repo's run history
are stated in the kickoff. Your job: independently verify each one against
the LIVE state DB and current source, with emphasis on cost and learning-loop
impact. Do not trust the auditor's numbers — re-derive them.

## Required discovery

For each finding F1–F6 from the kickoff:

- re-run the reproduction SQL against the live DB the kickoff names;
- read the named source files and confirm or refute the claimed code path;
- for cost claims, recompute totals from the raw `llm_calls` rows rather
  than trusting the auditor's aggregation;
- check whether each claimed bug still fires in the CURRENT tree or only
  explains historical rows.

You may run `sqlite3` read-only queries and read files. You may not edit any
repo source file.

## Report

Write `${MINI_ORK_RUN_DIR}/judge-minimax-audit.md`.

Use these sections exactly:

1. `Discovery Evidence` — commands run and files read, with file:line or SQL
   rows as receipts.
2. `Finding Verdicts` — one block per finding, in order F1..F6, each with:
   - `finding_id`
   - `verdict`: one of `confirmed` | `partially_confirmed` | `refuted` | `unverifiable`
   - `severity`: one of `critical` | `high` | `medium` | `low`
   - `evidence_checked`: list of commands/paths actually inspected
   - `reasoning`: why the verdict holds, including any corrected numbers
   - `corrections`: any number or claim you dispute, with the value you
     measured instead (empty if none)
3. `Cost Impact Recomputation` — your own table: dollars and tokens at stake
   per finding, derived from raw rows.
4. `Recommended Fix Order` — ranked by (impact × cheapness), one-line rationale.
5. `Open Questions` — what blocks higher confidence.

Be adversarial about magnitude: confirm or shrink every dollar figure.
