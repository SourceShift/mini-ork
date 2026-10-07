# I3 (revision 2, fresh run on the current engine) — kickoff contract lint + a blocked run asks, waits, and resumes instead of failing

Epic `sdd-i3-kickoff-contract` (kickoffs/sdd-mechanisms/roadmap.md). Today, when the run
profile still `needs_answers` after the auto-answerer (`MO_AUTO_ANSWER_PROFILE`, default on)
and there is no TTY, `mini_ork/cli/plan.py:696-713` writes a `plan_status=needs_answers` plan.
Then `mini_ork/cli/execute.py` `_execute_gate_check` (~:2620) writes `blocked.json`, marks the
run **failed/ESCALATE**, and exits 6. At teardown, `run_reaper.close_run_record` turns any
non-zero exit into `failed` as well. The questions are lost, and the only way forward is a
brand-new run.

## Goal

1. `kickoff_lint.lint` names every missing contract section.
2. A headless run that is blocked on questions writes one structured ASK file per question.
   It exits **resumable**: not `failed`, not reaped.
3. `mini-ork resume <run_id> --answer <ask_id>=<text>` records answers. Once every ASK is
   answered, it applies them to the run profile and continues the SAME run, which then passes
   the profile gate.

## Acceptance

- **AC1** — `lint()` adds one `warn` finding per missing contract section. Each message names
  the section, e.g. `"The kickoff has no '## Out of scope' section."`. The five sections are
  Goal, Acceptance, Files in scope, Out of scope, Verification command. Match headings
  case-insensitively at any `#` level. Accept these synonyms: `Acceptance criteria`, `Success
  criteria` and `Done when` for Acceptance; `Scope` for Files in scope; `Non-goals` for Out of
  scope. Acceptance also needs at least one AC id (`AC1`, `AC-1`, `AC 1`); without one, add a
  warn: `"The Acceptance section has no AC ids (AC1, AC2, …)."`. Keep findings `warn`, never
  `error`, so no existing caller starts refusing kickoffs (`mcp_context/server.py:1247` renders
  them). The existing success-section warn stays.
  The contract applies only when `recipe` is given AND the recipe has an implementer node.
  That is the same condition as the existing Files-in-scope warn (`kickoff_lint.py:227`,
  `_recipe_has_implementer`). Researcher/review-only recipes are exempt (decided; do not
  reopen).
- **AC2** — a headless blocked run:
  - Writes `<run_dir>/asks/<ask_id>.json` per human question (`ask_id` = `ask-<n>`, 1-based in
    question order). Each file validates against the new `schemas/ask.schema.json`:
    `{schema_version:"ask@1", ask_id, run_id, question, blocking_refs:[<paths or ids it
    blocks, e.g. "run_profile">], options:[…] (may be empty), default:<string|null>,
    created_at:<epoch s>, answer:null, answered_at:null}`.
  - Leaves `task_runs.status` NON-terminal: keep `planned`. The `task_runs` CHECK enum has no
    paused/blocked value, so add none. `verdict` stays NULL.
  - Emits one `run_events` row for the block. Reuse an existing `event_type` / `finish_reason`
    allowed by the CHECK; read `db/` for the enum, do not widen it.
  - Prints `[asks] <n> question(s) written to <run_dir>/asks — answer with: mini-ork resume
    <run_id> --answer ask-1=<text>` and exits **6** (same code as today, so callers keep their
    mapping).
  - `run_reaper.probe()` returns `paused` for a run dir with an unanswered ASK. Mirror the
    existing `.cost-pause` branch at `run_reaper.py:200`.
  - `run_reaper.close_run_record()` leaves such a run's status unchanged. Mirror the
    `.cost-pause` early return at `run_reaper.py:130`.
  - `reap()` never fails it.
- **AC3** — `mini-ork resume <run_id> --answer ask-1=<text> [--answer ask-2=<text> …]`:
  - Writes `answer` and `answered_at` into each ASK file.
  - While any ASK is still unanswered, prints which ones remain and exits 0 without
    continuing.
  - When all are answered, applies them through `plan.py` `_apply_profile_answers` with the
    same payload shape `_prompt_profile_questions` builds (`{"answers": {question: answer},
    "auto_answered": False}`). Then it continues the same run (same `run_id`, same run dir,
    same recipe + kickoff), and the profile gate passes.
  - Without `--answer`, `resume` keeps today's cost-pause behaviour byte-for-byte
    (`tests/unit/test_mini_ork_resume_py.py` must stay green).
  - Re-entering the lifecycle for an existing `run_id` must not create a second `task_runs`
    row or crash on the existing run dir. Read how `_run_lifecycle_impl` (`main.py:648+`) and
    `MINI_ORK_RUN_ID` (`main.py:764`) handle that, and make it idempotent.

## Files in scope

- `mini_ork/kickoff_lint.py`: AC1
- `mini_ork/cli/plan.py`: write the ASK files at the gate-block (~:696)
- `mini_ork/cli/execute.py`: `_execute_gate_check` (~:2620) stops marking the run failed when
  ASK files exist
- `mini_ork/orchestration/run_reaper.py`: `probe` + `close_run_record` paused branch for open
  ASKs
- `mini_ork/cli/resume.py`: `--answer` path
- `mini_ork/cli/main.py`: only if resume's re-entry needs a hook in `_run_lifecycle_impl`
- `schemas/ask.schema.json` (new)
- `tests/unit/test_kickoff_contract.py` (new)
- `tests/unit/test_kickoff_lint.py`: fixtures only. The complete-kickoff cases gain the five
  contract sections so `lint` stays silent on a complete kickoff. Change expectations only
  where AC1 changes the output.

Do NOT modify any other file. Do not touch `mini_ork/recovery/retry_notify.py` or the start
guard in `mini_ork/cli/main.py` (~:770); another session owns it.

## Out of scope

- Splitting kickoffs (epic I4).
- Answering from the board, the web UI or ACP. The ASK file format is the contract those
  surfaces will read later.
- Changing the auto-answerer or `MO_AUTO_ANSWER_PROFILE`'s default.
- Any new `task_runs.status` value or CHECK change.
- `run_events.event_id` is UNIQUE. Include the run id (`evt-asks_blocked-<run_id>-<ts>`) so
  two runs blocking in the same second cannot collide.

## Tests (`tests/unit/test_kickoff_contract.py`)

- AC1: a kickoff with all five sections + `AC1` → no contract warns. Drop each section in turn
  → exactly that section is named. Synonyms are accepted. An Acceptance section without AC ids
  → the AC-id warn.
- AC2: drive the gate-block with a temp home + seeded `task_runs` row and a profile that stays
  `needs_answers` (`MO_AUTO_ANSWER_PROFILE=0`, no TTY). Then assert:
  - the `asks/ask-*.json` files validate against the schema;
  - exit 6;
  - status is still `planned`;
  - `probe()` returns `paused`;
  - `close_run_record(..., rc=6)` returns None and changes nothing;
  - `reap()` leaves it alone.
- AC3:
  - a partial answer → exit 0, still paused, ASK file updated;
  - answering all → `profile-answers.json` written, the profile `ready`, and the continuation
    invoked for the same `run_id`, with a monkeypatched lifecycle entry and no real LLM call;
  - a second `task_runs` row is never inserted;
  - `resume <run_id>` without `--answer` behaves exactly as before.
- Falsify each gate once: with the paused branch removed, the reaper/close assertions must
  fail. Say in the summary that you did this.

## Verification command

```bash
env -u MINI_ORK_RUN_ID -u MINI_ORK_DB -u MINI_ORK_HOME python3.11 -m pytest -q -p no:asyncio tests/unit/test_kickoff_contract.py tests/unit/test_kickoff_lint.py tests/unit/test_run_reaper.py tests/unit/test_run_finalization.py tests/unit/test_mini_ork_resume_py.py   # must exit 0
```

## Done when

- The verification command → 0 failed. Paste the summary line.
- `uvx ruff check mini_ork/kickoff_lint.py mini_ork/cli/plan.py mini_ork/cli/execute.py mini_ork/cli/resume.py mini_ork/orchestration/run_reaper.py tests/unit/test_kickoff_contract.py tests/unit/test_kickoff_lint.py` → clean.
- `git diff --stat` touches only the files in scope.
