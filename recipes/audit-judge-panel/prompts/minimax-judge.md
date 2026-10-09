# Judge B (lane `minimax`) — audit findings verification

You are judge **B**, one of two independent verification lanes. The lane is
named `minimax`, but the model actually answering may be a routed alias — do not
assume your own identity, **report the model id you observe** in the `model`
field below, so a reader can tell which model produced this lane.

Six audit findings (F1–F6) and their six proposed fixes (S1–S6) are stated in the
kickoff. Independently verify or refute each one against the LIVE state DB and
current source, with emphasis on cost and learning-loop impact. Do not trust the
auditor's numbers — re-derive them.

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

Use these five sections, in this exact order — the same five every lane uses,
so the two reports are comparable line for line:

1. `Discovery Evidence` — commands run and files read, with file:line or SQL
   rows as receipts. Include a `### Cost Impact Recomputation` subsection here:
   dollars and tokens at stake per finding, derived from raw rows.
2. `Finding Verdicts` — one block per finding, in order F1..F6, each with:
   - `finding_id`
   - `verdict`: one of `confirmed` | `partially_confirmed` | `refuted` | `unverifiable`
   - `severity`: one of `critical` | `high` | `medium` | `low`
   - `evidence_checked`: list of commands/paths actually inspected
   - `reasoning`: why the verdict holds, including any corrected numbers
   - `corrections`: any number or claim you dispute, with the value you
     measured instead (empty if none)
3. `Fix Verdicts` — one block per fix, in order S1..S6, each with:
   - `fix_id`
   - `finding_id`: the finding the fix addresses (`S1` → `F1`, …, `S6` → `F6`)
   - `verdict`: one of the four above — judge the FIX, not the finding
   - `severity`: one of the four above — the severity of shipping this fix
   - `reasoning`: why the fix stands or falls, including any sub-option
     (`S4(1)`, `S1(a)`) you would split out and how you verdict it
4. `Recommended Fix Order` — the fix ids ranked by (impact × cheapness), one
   line of rationale each.
5. `Open Questions` — what blocks higher confidence.

Be adversarial about magnitude: confirm or shrink every dollar figure.

## Structured output (required)

In addition to the report, write ONE JSON object to the output path named at the
end of this prompt (`context-minimax_judge.json`). Both lanes emit this exact
schema so a mechanical consumer can join them without guessing:

```json
{
  "schema_version": "1",
  "panel_id": "<basename of ${MINI_ORK_RUN_DIR}>",
  "lane": "minimax",
  "model": "<the model id that actually answered>",
  "findings": [
    {"finding_id": "F1", "verdict": "confirmed", "severity": "medium",
     "evidence_checked": ["..."], "reasoning": "...",
     "corrections": [{"claim": "...", "auditor_value": "...", "measured_value": "..."}]}
  ],
  "fixes": [
    {"fix_id": "S1", "finding_id": "F1", "verdict": "partially_confirmed",
     "severity": "low", "reasoning": "..."}
  ],
  "fix_order": ["S1", "S4", "S3", "S2", "S5", "S6"],
  "counts": {
    "findings_confirmed": 0, "findings_partially_confirmed": 0,
    "findings_refuted": 0, "findings_unverifiable": 0,
    "fixes_confirmed": 0, "fixes_partially_confirmed": 0,
    "severity_histogram": {"critical": 0, "high": 0, "medium": 0, "low": 0}
  },
  "open_questions": ["..."],
  "read_only_attestation": {"worktree_modified": false, "files_written": ["judge-minimax-audit.md"]}
}
```

Rules the JSON must satisfy (the panel gate checks every one):

- `findings` has exactly six entries, ids `F1`..`F6` in order; `fixes` has
  exactly six, ids `S1`..`S6` in order. Every `fixes[*].finding_id` names an
  existing finding.
- `verdict` values come only from the four-word set; `severity` only from the
  four-word set. Use lowercase.
- `counts.severity_histogram` sums to six.
- `lane` is your identity on this panel — the alias (`minimax`) or your node id
  (`minimax_judge`); the gate accepts either, and a value naming the OTHER lane
  fails. `model`, `schema_version`, `panel_id` are present and non-empty.
- `read_only_attestation.worktree_modified` is `false` and `files_written`
  lists every file you created.
