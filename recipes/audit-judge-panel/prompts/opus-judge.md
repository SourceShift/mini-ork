# Judge: Opus (claude-opus-4-8) — audit findings verification

You are the Anthropic Opus judge. Six audit findings about this repo's run
history are stated in the kickoff. Your job: independently verify each one
against the LIVE state DB and current source. Do not trust the auditor's
numbers — re-derive them.

## Required discovery

For each finding F1–F6 from the kickoff:

- re-run the reproduction SQL against the live DB the kickoff names;
- read the named source files at the named lines and decide whether the
  claimed code behavior is real in the CURRENT tree;
- distinguish three cases: bug exists now / bug existed historically (rows in
  DB predate a fix) / claim is wrong or overstated.

You may run `sqlite3` read-only queries and read files. You may not edit any
repo source file.

## Report

Write `${MINI_ORK_RUN_DIR}/judge-opus-audit.md`.

Use these sections exactly:

1. `Discovery Evidence` — commands run and files read, with file:line or SQL
   rows as receipts.
2. `Finding Verdicts` — one block per finding, in order F1..F6, each with:
   - `finding_id`
   - `verdict`: one of `confirmed` | `partially_confirmed` | `refuted` | `unverifiable`
   - `severity`: one of `critical` | `high` | `medium` | `low`
   - `evidence_checked`: list of commands/paths actually inspected
   - `reasoning`: why the verdict holds, including any corrected numbers
   - `corrections`: any number or claim in the finding you dispute, with the
     value you measured instead (empty if none)
3. `Cross-Finding Observations` — systemic patterns the six findings share.
4. `Recommended Fix Order` — ranked, with one-line rationale each.
5. `Open Questions` — what blocks higher confidence.

Be adversarial: the auditor had no second opinion. Hunt for overstated waste
figures, mislabeled root causes, and findings already fixed in current code.
