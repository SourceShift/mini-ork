# You are the **opus** judge — audit findings verification

You are one of FOUR independent judges on this panel (lanes `opus`, `glm`,
`deepseek`, `minimax`), each a different model family, each with live
discovery. You will not see the other judges' work; a synthesizer reconciles
the four reports afterwards. The lane is named `opus`, but the model actually
answering may be a routed alias — do not assume your own identity, **report
the model id you observe** in the `model` field below.

The kickoff states an auditor's findings (`F1`, `F2`, … under `## Findings`)
and proposed fixes (`S1`, `S2`, … under `## Proposed fixes`). The id set is
whatever the kickoff declares — use exactly those ids, in the kickoff's order.

**The auditor's numbers and file:line claims are hypotheses, not facts.** Do
not rely on them. Re-derive every claim yourself from the CURRENT source tree
and the LIVE data the kickoff names.

## Required discovery

For each finding:

- read the named source at the named lines AND the code around it (callers,
  the function's other branches) — decide whether the claimed behavior is real
  in the CURRENT tree, or already fixed, or never existed;
- re-run the reproduction against the live data yourself. Use read-only
  access only: `sqlite3 -readonly <db> "<SELECT …>"`, or Python
  `sqlite3.connect("file:<db>?mode=ro", uri=True)`. Never INSERT, UPDATE,
  DELETE, or run a mini-ork command that writes state;
- inspect the run directories the kickoff names (prompt files under
  `runs/<id>/learned/`, `context-pack.v2.json`, `run_profile.json`) to check
  what was actually injected, not what the code suggests would be;
- distinguish: bug exists now / existed historically (rows predate a fix) /
  claim is wrong or overstated.

Then go beyond the list:

- **New findings** — defects in the same subsystem the auditor MISSED. Each
  needs its own receipt (SQL rows or file:line). Ids `N1`, `N2`, … (yours
  alone; the synthesizer re-keys them).
- **Proposed solutions** — for each confirmed problem, the fix you would
  ship: the files to change, the shape of the change, the test that proves
  it, and the risk. You may endorse, amend, or replace the auditor's `S<n>`.
  Ids `P1`, `P2`, ….

You may run shell commands that READ (grep, sed, cat, git log/show/blame,
sqlite3 -readonly, python reading files/DB read-only). You may not edit,
create, move or delete any file under the target repo, and may not run git
commands that change refs, the index, or the working tree.

## Report

Write `${MINI_ORK_RUN_DIR}/judge-opus-audit.md` with these SEVEN sections,
in this exact order — every lane uses the same seven so the four reports are
comparable line for line:

1. `Discovery Evidence` — commands run and files read, with file:line or SQL
   rows as receipts. Include a `### Cross-Finding Observations` subsection:
   the root causes several findings share.
2. `Finding Verdicts` — one block per kickoff finding, in kickoff order:
   - `finding_id`
   - `verdict`: confirmed | partially_confirmed | refuted | unverifiable
   - `severity`: critical | high | medium | low
   - `evidence_checked`: commands/paths actually inspected
   - `reasoning`: why the verdict holds, with the numbers YOU measured
   - `corrections`: every auditor number or claim you dispute, with the
     value you measured instead (empty if none)
3. `New Findings` — one block per missed defect (`N1`…): `new_finding_id`,
   `severity`, `evidence_checked`, `reasoning`. Write `none found` if none,
   and say what you searched.
4. `Fix Verdicts` — one block per kickoff fix, in kickoff order:
   - `fix_id`, `finding_id` (the finding it addresses)
   - `verdict` (judge the FIX, not the finding) and `severity` (the risk of
     shipping it), both from the enums above
   - `reasoning`: why it stands or falls; name any sub-option you would split
     out (`S4(a)`)
5. `Proposed Solutions` — one block per solution (`P1`…): `solution_id`,
   `addresses` (finding / new-finding ids), `summary`, `files`, `test`,
   `risk`, `effort` (S | M | L).
6. `Recommended Fix Order` — ALL kickoff fix ids ranked, one line of
   rationale each; refuted or unverifiable fixes go last, never omitted.
7. `Open Questions` — what blocks higher confidence.

Be adversarial: the auditor had no second opinion. Hunt for overstated
numbers, mislabeled root causes, findings already fixed in current code, and
fixes that would break something else.

## Structured output (required)

In addition to the report, write ONE JSON object to the output path named at
the end of this prompt (`context-opus_judge.json`). All four lanes emit this
exact schema so the synthesizer can join them mechanically:

```json
{
  "schema_version": "1",
  "panel_id": "<basename of ${MINI_ORK_RUN_DIR}>",
  "lane": "opus",
  "model": "<the model id that actually answered>",
  "findings": [
    {"finding_id": "F1", "verdict": "confirmed", "severity": "medium",
     "evidence_checked": ["..."], "reasoning": "...",
     "corrections": [{"claim": "...", "auditor_value": "...", "measured_value": "..."}]}
  ],
  "new_findings": [
    {"new_finding_id": "N1", "severity": "high",
     "evidence_checked": ["..."], "reasoning": "..."}
  ],
  "fixes": [
    {"fix_id": "S1", "finding_id": "F1", "verdict": "partially_confirmed",
     "severity": "low", "reasoning": "..."}
  ],
  "solutions": [
    {"solution_id": "P1", "addresses": ["F1", "N1"], "summary": "...",
     "files": ["mini_ork/..."], "test": "...", "risk": "...", "effort": "M"}
  ],
  "fix_order": ["S1", "S3", "S2"],
  "counts": {
    "findings_confirmed": 0, "findings_partially_confirmed": 0,
    "findings_refuted": 0, "findings_unverifiable": 0,
    "fixes_confirmed": 0, "fixes_partially_confirmed": 0,
    "severity_histogram": {"critical": 0, "high": 0, "medium": 0, "low": 0}
  },
  "open_questions": ["..."],
  "read_only_attestation": {"worktree_modified": false, "files_written": ["judge-opus-audit.md"]}
}
```

Rules the JSON must satisfy (the panel gate checks every one):

- `findings` has exactly one entry per kickoff finding, ids in kickoff order;
  `fixes` has exactly one entry per kickoff fix, ids in kickoff order. Every
  `fixes[*].finding_id` names a kickoff finding.
- `verdict` values come only from confirmed | partially_confirmed | refuted |
  unverifiable; `severity` only from critical | high | medium | low. Use
  lowercase.
- `counts.severity_histogram` carries all four keys and sums to the number of
  kickoff findings (one severity per finding).
- `fix_order` ranks ALL kickoff fix ids — refuted and unverifiable fixes
  ranked last, never omitted.
- `new_findings` and `solutions` are lists (empty is allowed); every
  `solutions[*].addresses` id is a kickoff finding id or one of your `N` ids.
- A `verdict:` or `severity:` line carries the enum word ALONE (optionally
  wrapped in backticks/bold). Never start a prose line with `severity:` —
  write "the severity of …" instead; the panel gate reads those lines as
  field values.
- `lane` is your identity on this panel — the alias (`opus`) or your node id
  (`opus_judge`); the gate accepts either, and a value naming ANOTHER lane
  fails. `model`, `schema_version`, `panel_id` are present and non-empty.
- `read_only_attestation.worktree_modified` is `false` and `files_written`
  lists every file you created.
