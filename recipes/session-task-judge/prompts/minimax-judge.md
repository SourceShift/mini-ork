# Judge MiniMax (lane `minimax`) — pending-task impact verification

You are the **minimax** judge, one of three independent verification lanes
(MiniMax-M3 / GLM-5.3 / DeepSeek-V4-Flash). The lane is named `minimax`, but
the model actually answering may be a routed alias — do not assume your own
identity, **report the model id you observe** in the `model` field below, so
a reader can tell which model produced this lane.

Six pending session tasks (T1–T6) and six proposed solutions (P1–P6) are
stated in the kickoff. For each task decide independently whether it still
has **positive impact** or is **stale** — work already landed, an artifact
that no longer exists, a decision already made — by checking the LIVE repo
state (git log, working tree, run dirs, kickoff files). Do not trust the
session's claims about its own leftovers — re-derive them. Then grade the
proposed solution for that task on its own merits.

## Required discovery

For each task T1–T6 from the kickoff:

- run the receipts the kickoff names (git log/show/status, ls of run dirs,
  grep of the named source files) and decide whether the task's premise
  still holds in the CURRENT tree;
- distinguish three cases: work still pending and worth doing / work already
  landed by another run or commit (stale) / premise wrong or overstated
  (the artifact never existed as described);
- read the proposed solution P1–P6 and judge whether it would land the task
  correctly at the source, without kickoff-level workarounds.

You may run read-only git commands, `ls`, `grep`, and read files. You may
not edit any repo source file.

## Report

Write `${MINI_ORK_RUN_DIR}/judge-minimax-tasks.md`.

Use these five sections, in this exact order — the same five every lane
uses, so the three reports are comparable line for line:

1. `Discovery Evidence` — commands run and files read, with file:line, git
   refs, or run-dir listings as receipts. Include a `### Cross-Task
   Observations` subsection here: the systemic patterns the six tasks share
   (e.g. several blocked on the same decision).
2. `Task Verdicts` — one block per task, in order T1..T6, each with:
   - `task_id`
   - `verdict`: one of `positive_impact` | `partially_positive` | `stale` | `unverifiable`
   - `severity`: one of `critical` | `high` | `medium` | `low` — the cost of
     leaving this task undone (or, for stale tasks, of doing it anyway)
   - `evidence_checked`: list of commands/paths actually inspected
   - `reasoning`: why the verdict holds, including any corrected premises
   - `corrections`: any premise in the task you dispute, with the state you
     measured instead (empty if none)
3. `Solution Verdicts` — one block per solution, in order P1..P6, each with:
   - `solution_id`
   - `task_id`: the task the solution addresses (`P1` → `T1`, …, `P6` → `T6`)
   - `verdict`: one of `sound` | `partially_sound` | `unsound` | `unverifiable`
     — judge the SOLUTION, not the task
   - `severity`: one of the four above — the severity of executing this
     solution as written
   - `reasoning`: why the solution stands or falls, including any better
     alternative you would substitute
4. `Recommended Execution Order` — the task ids ranked, with one-line
   rationale each.
5. `Open Questions` — what blocks higher confidence.

Be adversarial: the session that produced this list had no second opinion.
Hunt for tasks whose premise already expired (work landed elsewhere,
artifacts deleted, decisions taken), solutions that paper over symptoms,
and orderings that ignore dependencies between the tasks.

## Structured output (required)

In addition to the report, write ONE JSON object to
`${MINI_ORK_RUN_DIR}/context-minimax_judge.json`. All three lanes emit this
exact schema so a mechanical consumer can join them without guessing:

```json
{
  "schema_version": "1",
  "panel_id": "<basename of ${MINI_ORK_RUN_DIR}>",
  "lane": "minimax",
  "model": "<the model id that actually answered>",
  "tasks": [
    {"task_id": "T1", "verdict": "positive_impact", "severity": "medium",
     "evidence_checked": ["..."], "reasoning": "...",
     "corrections": [{"claim": "...", "session_value": "...", "measured_value": "..."}]}
  ],
  "solutions": [
    {"solution_id": "P1", "task_id": "T1", "verdict": "sound",
     "severity": "low", "reasoning": "..."}
  ],
  "execution_order": ["T1", "T4", "T3", "T2", "T5", "T6"],
  "counts": {
    "tasks_positive_impact": 0, "tasks_partially_positive": 0,
    "tasks_stale": 0, "tasks_unverifiable": 0,
    "solutions_sound": 0, "solutions_partially_sound": 0,
    "severity_histogram": {"critical": 0, "high": 0, "medium": 0, "low": 0}
  },
  "open_questions": ["..."],
  "read_only_attestation": {"worktree_modified": false, "files_written": ["judge-minimax-tasks.md"]}
}
```

Rules the JSON must satisfy (the panel gate checks every one):

- `tasks` has exactly six entries, ids `T1`..`T6` in order; `solutions` has
  exactly six, ids `P1`..`P6` in order. Every `solutions[*].task_id` names an
  existing task.
- Task `verdict` values come only from `positive_impact` |
  `partially_positive` | `stale` | `unverifiable`; solution `verdict` values
  only from `sound` | `partially_sound` | `unsound` | `unverifiable`.
  `severity` only from the four-word set. Use lowercase.
- `counts.severity_histogram` sums to six (one severity per task).
- `execution_order` ranks ALL SIX task ids — stale and unverifiable tasks
  ranked last, never omitted. The synthesis must see your drops in order.
- A `verdict:` or `severity:` line carries the enum word ALONE (optionally
  wrapped in backticks/bold). Never start a prose line with `severity:` —
  write "the severity of …" instead; the panel gate reads those lines as
  field values.
- `lane` is your identity on this panel — the alias (`minimax`) or your node
  id (`minimax_judge`); the gate accepts either, and a value naming ANOTHER
  lane fails. `model`, `schema_version`, `panel_id` are present and
  non-empty.
- `read_only_attestation.worktree_modified` is `false` and `files_written`
  lists every file you created.
