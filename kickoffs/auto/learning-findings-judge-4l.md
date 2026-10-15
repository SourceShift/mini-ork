# Four-lane judge panel — learning-loop and "Your code" tab findings (2026-10-15)

Execute the audit-judge-panel-4l workflow exactly. Do not research, do not add
lens, drafter or arbiter nodes.

An auditor (Claude, one model, no second opinion) inspected mini-ork's
learning features and the IDE "Learning & memory → Your code" tab, and claims
the injection plumbing works but most of what it injects is wrong. Every
number and file:line below is the auditor's HYPOTHESIS. Re-derive each one
yourself from the code and the live data; do not rely on the auditor.

## Where to look (absolute paths, read-only)

- Code (current origin/main, 738a938b): `/Volumes/docker-ssd/ps/mini-ork-worktrees/judge-panel-4l`
  — call it `W`. Key files: `W/mini_ork/learning/code_findings.py`,
  `W/mini_ork/ide_pages/learn/code.py`, `W/mini_ork/context_v2.py`,
  `W/mini_ork/context_assembler.py`, `W/mini_ork/cli/execute.py`
  (`_learned_block`), `W/mini_ork/memory/preferences.py`,
  `W/mini_ork/learning/themes.py`, `W/mini_ork/cli/metrics_context.py`.
- Live DB: `/Volumes/docker-ssd/ps/mini-ork/.mini-ork/state.db` — query ONLY
  with `sqlite3 -readonly` or `file:…?mode=ro`. Tables: `code_findings`,
  `code_findings_runs`, `task_runs`, `lesson_themes`, `emergent_patterns`,
  `user_preference_memory`, `llm_calls`.
- Live runs: `/Volumes/docker-ssd/ps/mini-ork/.mini-ork/runs/<run_id>/` —
  `learned/<node>.md|.json` is the exact text injected into a node prompt and
  its sources; `context-pack.v2.json` is the v2 pack; `run_profile.json`.
- Overlay in force for normal runs: `/Volumes/docker-ssd/ps/mini-ork/.mini-ork/config/agents.local.yaml`.

Do NOT edit, create or delete any file under `W` or the primary checkout, do
not write to the DB, and do not run mini-ork subcommands that write state.
`bin/mini-ork metrics context` and `python -c` calls that only read are fine.

## Findings

### F1 — Every code finding is stored twice (claimed severity: medium)
`review-verdict.json` re-states the reviewer's findings, and the dedup
fingerprint includes `source` (`code_findings.py:787-789`, used at `:897`), so
one finding lands as `review:reviewer` AND `review:verdict`. Claimed: 1683 rows
vs 1648 distinct `(run_id,file,line,issue)`; `mini_ork/ide_pages/kanban.py`
lines 621/244/371 each appear twice in the tab.

### F2 — "Recurring problems" are not recurring (claimed severity: high)
`recurring()` (`ide_pages/learn/code.py:643-712`) counts cluster members and
distinct `run_id`s. All 5 clusters shown for area `mini_ork/ide_pages` come
from ONE kickoff each: "4× in 1 run" is one review restating one bug four
ways; "2 runs" are review-loop rounds of the same kickoff
(`ide-node-stream-r3/-r4/-r5…`, `ide-kickoff-search-r4/-r5…`).
`context_v2.kickoff_stem()` (`context_v2.py:304`) already collapses rounds and
is unused here.

### F3 — Findings about rejected drafts are treated as facts about the file (claimed severity: high)
Every source run of those 5 clusters has `task_runs.status='failed'`; the
line numbers (`search.py:446`, `node.py:580`) refer to candidate code that
never merged. Nothing tracks open/fixed. The "log proxy leaks into the
transcript lower bound" bug is already fixed on main (`ide_pages/node.py:767-775`,
"r6 fix #1").

### F4 — TF-IDF clustering merges different bugs (claimed severity: medium)
The `search.py` cluster labelled "5×" holds five distinct findings (lines
446, 801, 446, 529, 541); the "times" column reads as one bug five times.

### F5 — Prose findings are attached to the wrong file (claimed severity: low)
`_prose_to_findings` takes the first path in the text (`code_findings.py:384-387`).
A process complaint ("This delivery was pushed to origin/main as commit
43740937 … before the review") is filed under `mini_ork/ide_pages/__init__.py:32`.

### F6 — "Make it a rule" mints stale, raw, over-broad rules with operator authority (claimed severity: high)
`_rule_action` (`ide_pages/learn/code.py:558-573`) targets the AREA glob, not
the cluster's own files, and copies the raw representative text. Live row in
`user_preference_memory`: key `review-196c4795`, scope `path`, target
`mini_ork/ide_pages/*`, text ends "Reproduced: [False] PASSED, [True]
FAILED." — describing the already-fixed F3 bug. `render_block`
(`memory/preferences.py:540-565`) heads it "set by the user; follow them".
Claimed: `prefs_for(...)` with `scope_paths('…/runs/run-1792058012-31521')`
(a run that touched only `ide_pages/_cn.py`) returns this rule.

### F7 — Wildcard themes crowd out task-class themes (claimed severity: high)
Both theme readers select `task_class = ? OR task_class = '*'` ORDER BY
`n_runs DESC` (`context_v2.py:384-391`, `context_assembler.py:566-570`).
Claimed: 41 verified `'*'` themes; the framework_edit run
`sde-3a-escalate-20261015-122857` planner (`learned/planner.md`) received 5
themes, all `'*'`, mostly about chapter_review lens fan-outs, and none of the
12 verified framework_edit themes. Also judge: the 2026-10-11 panel found
"1 of 5306 themes verified"; did the promotion change since then over-correct
(`themes.py` around `:935-1012`: is "verified" a guardrail pass rather than
measured efficacy)?

### F8 — 32 verified themes can never be injected (claimed severity: medium)
They have `task_class = ''`, which matches neither a real class nor `'*'`.

### F9 — The same themes are injected twice into one prompt (claimed severity: medium)
`_learned_block` (`cli/execute.py:3109`) concatenates the v2 block AND the v1
`failure_modes_md` block; both render verified themes. `context_v2` claims v2
"replaces the v1 blocks" (`context_v2.py:107-109`). Claimed receipt:
`runs/run-1791930074-82118/learned/reviewer.json` lists the same 5 theme ids
as `context_v2` `t:` items and again as `kind: theme`. This also means the
v2-vs-holdout comparison is "v1+v2 vs v1".

### F10 — Emergent patterns ignore the task (claimed severity: high)
`_approved_emergent_rows` (`context_assembler.py:~590-620`) returns every
approved pattern regardless of task class; `task_class` only scopes the UCB
utility bookkeeping. Claimed: the code_fix reviewer in
`run-1791930074-82118` (a toy `tally.py` fix) got patterns about
`__classify__`, `__reflect__`, goal_loop and self_migrate.

### F11 — v2 findings almost never reach a run (claimed severity: high)
v2 reads file scope only from a "Files in scope" kickoff heading
(`context_v2.py:171`). Claimed: 53 of the last 60 `context-pack.v2.json` have
empty `contract.files_in_scope`, while 64 of the last 80 `run_profile.json`
carry `scope_allow` (which `preferences.scope_paths` already parses).

### F12 — Harness and "check passed" notes are injected as code problems (claimed severity: medium)
`RECEIPT_RE` (`code_findings.py:65-74`) misses them. Claimed: the pack of
`run-1791930074-82118` injected "Diff inspected and in scope: …", "Root cause
of the false is environmental, not the implementation …" and "To clear the
gate: resolve the suite-adequacy module availability …" under "Problems
reviewers already found in these files. Do not repeat them".

### F13 — The kickoff "Do NOT" parser grabs wrapped continuation lines (claimed severity: medium)
`_DO_NOT_RE` (`context_v2.py:69`, used `:186-197`) matches any line starting
with "never"/"do not". Claimed: the only hard constraint the
`sde-3a-escalate-20261015-122857` planner received was "[c:0] never raises
into the run." — the tail of a wrapped sentence in its kickoff.

### F14 — The v2 severity floor filters almost nothing (claimed severity: low)
`normalize_severity` (`code_findings.py:153-168`) makes an unlabelled prose
finding `medium` whenever the verdict is not a pass, so
`MO_CONTEXT_V2_MIN_SEVERITY=medium` passes nearly all prose.

### F15 — The recurrence metric cannot show lift (claimed severity: high)
Claimed: `bin/mini-ork metrics context` reports `recurrence_rate=0.0` for
v2 (96 groups) and holdout (44 groups). The detector does fire on a review-
loop pair (3 of 8 r4 clusters recur in r5) but misses paraphrases (a known
r4→r5 repeat scores cosine 0.283 < 0.35). Since injected items are one-off
bugs from other tasks, they cannot recur, so lift can never be positive. No
mechanism retires an injected lesson or rule that shows no effect.

### F16 — Multi-family judge panels are silently single-family (claimed severity: high)
The overlay maps `opus_lens: glm` and `minimax_lens: opus`. Claimed:
`llm_calls` for panel run `run-1791725633-31802` shows `opus_lens` served by
`glm` and `laguna_lens` falling back to `minimax`, yet its gate passed.
Learned lessons and verdicts that rely on such panels are less independent
than their labels say.

### F17 — Findings may leak across target repos (claimed severity: medium, UNVERIFIED by the auditor)
`code_findings` has no repo column and `scope_findings` matches on relative
path prefix only (`context_v2.py:242-285`), so a finding on `tally.py` or
`src/main.rs` in one target repo could be offered to a different repo.
Verify or refute with data.

## Proposed fixes

- **S1** (F1) — Drop `source` from the fingerprint (or skip a `review-verdict.json` finding already present from a reviewer), and prune existing duplicates.
- **S2** (F2, F4) — A problem is "recurring" only when it appears in ≥ 2 distinct kickoff stems within the window; collapse members by (kickoff stem, file, nearby line); show a "kickoffs" column instead of "runs" and "times".
- **S3** (F3) — Give findings a lifecycle: harvest only from runs whose diff landed, or mark each finding open/fixed by checking its snippet/line at HEAD; expire it when the file changes.
- **S4** (F5, F12) — Tighten attribution and the receipt filter: classify notes as code / process / harness, and only keep code-problem notes whose subject is the path.
- **S5** (F6) — "Make it a rule" targets the cluster's own files, opens an editable generalised text, records the source finding ids, and renders learned rules under their own heading, not "set by the user".
- **S6** (F7, F8) — Select task-class themes first and use `'*'` only to fill remaining slots; repair or reclassify `task_class=''` themes.
- **S7** (F9) — Render verified themes once per prompt (skip the v1 verified-lessons block when the v2 block carries them); fix the docstring.
- **S8** (F10) — Filter patterns by task class or by similarity to the kickoff before UCB ranking.
- **S9** (F11) — When a kickoff has no "Files in scope" heading, fall back to `preferences.scope_paths(run_dir)`.
- **S10** (F13) — Accept a "Do NOT / Never" line only when it starts a bullet or paragraph, not a wrapped continuation.
- **S11** (F14) — Default an unlabelled severity to `low` (or `unknown`) so the floor works.
- **S12** (F15) — Measure recurrence across kickoff stems with a paraphrase-tolerant matcher, and auto-retire injected items that show no effect.
- **S13** (F16) — Gate panel heterogeneity on `llm_calls` (served model per lens alias must be distinct) and make lane names honest in the overlay.
- **S14** (F17) — Record the target repo on every finding and filter by it at selection time.

## Required artifacts — declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/judge-opus-audit.md`
- `${MINI_ORK_RUN_DIR}/judge-glm-audit.md`
- `${MINI_ORK_RUN_DIR}/judge-deepseek-audit.md`
- `${MINI_ORK_RUN_DIR}/judge-minimax-audit.md`
- `${MINI_ORK_RUN_DIR}/context-opus_judge.json`
- `${MINI_ORK_RUN_DIR}/context-glm_judge.json`
- `${MINI_ORK_RUN_DIR}/context-deepseek_judge.json`
- `${MINI_ORK_RUN_DIR}/context-minimax_judge.json`
- `${MINI_ORK_RUN_DIR}/synthesis.md`

## Success

All four judge reports, four sidecars and the synthesis exist, and
`verifiers/panel-completeness.py` passes, including the served-model check.
