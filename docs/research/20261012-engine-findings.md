# Engine findings — 2026-10-12 LangGraph-parity campaign

Five defect classes observed while an operator session drove the P1 parity
epic and the three-lens LangGraph survey (runs cited inline; all evidence
paths are relative to the run directory under `$MINI_ORK_HOME/runs/`).
Engine at `496a57c5`..`a8f22462`.

## F1. `resume` without `--answer` is a silent no-op

`mini_ork/cli/resume.py` splits into `resume()` (clears the `.cost-pause`
sentinel only) and `resume_answers()` (applies answers, then re-enters the
lifecycle in-process via `_reenter_lifecycle`, pinning `MINI_ORK_RUN_ID`).
A resume invoked without `--answer` on a run parked at
`plan_status=needs_answers` prints nothing of consequence and exits 0 —
the run stays parked. Every fresh dispatch asks two `run_profile` questions
(scope + success command) before any node executes, so this is the FIRST
interaction with almost every new run.

**Suggested fix:** `resume()` should refuse (exit non-zero with a message)
when the run is at `needs_answers` and no `--answer` was passed, instead of
silently clearing a sentinel that is not set.

## F2. A resume without `MO_TARGET_CWD` corrupts `roots.target` and wedges every later dispatch — silently

`resolve_target_cwd` (`mini_ork/dispatch/providers.py:1120-1128`) resolves
`request.cwd → $MO_TARGET_CWD → os.getcwd()`. A resume launched from inside
the framework tree without `MO_TARGET_CWD` stamps `roots.target` /
`roots.exec_cwd` in `run_profile.json` with the framework home. Every
subsequent worker-lane dispatch then fails `cwd_guard`
(`providers.py:1131-1157`, rc 2) and the CLI exits **rc 241 with no stdout**
— the error is visible ONLY in the `llm_calls.error_message` column of
`state.db`. Later resumes preserve the corrupted roots (upserts don't
re-derive them), so setting the env var after the fact does not recover the
run. Evidence: `run-1791788924-58661--survey-lens-space--D1/run_profile.json`
(had to be hand-edited back to the target worktree before resume-5 worked);
`llm_calls` cwd-guard rows on runs `run-1791750091-88029`,
`run-1791751285-97779`, `run-1791753977-79305`, `run-1791755690-30881`.

**Suggested fixes:** (a) `resume_answers()` should re-stamp
`roots.target` from `$MO_TARGET_CWD` when set, never from `os.getcwd()`;
(b) the cwd-guard refusal should surface on stdout/stderr with its rc
instead of dying at rc 241; (c) a run whose `roots.target` points into a
framework tree should refuse at resume time with the exact edit needed.

## F3. SDK-lane timeouts carry a single flat 120s wall, no per-attempt deadline

`llm_calls.error_message` rows `timeout after 120.0s` on
`run-1791750091-88029` and `run-1791755690-30881` — a per-run lane with no
operator-visible per-attempt deadline knob; one slow attempt burns the run's
retry budget at an opaque wall.

**Suggested fix:** expose a per-lane (or per-provider) request deadline in
`config/providers.yaml` and echo the effective value in the dispatch log
line, so a timeout names its ceiling.

## F4. Engine hangs after terminal `retry-exhausted` (multiprocessing leak)

`run-1791798883-27913` wrote its terminal state (`retry-exhausted/`,
`review-verdict.json`, 12:21) but the engine process tree — parent
`python3 -m mini_ork.cli.resume` plus a `multiprocessing.resource_tracker`
and one spawn worker — stayed alive, 0% CPU, holding no open handles into
the run dir, for 7+ minutes until killed. On kill, the resource tracker
reported **5 leaked semaphore objects**. Net effect: a run that LOOKS live
(a process exists) is dead on disk; supervision keyed on process liveness
never fires.

**Suggested fix:** after writing a terminal state, join/teardown the
process pool with a deadline and exit non-zero if teardown times out;
investigate the semaphore leak in the tier-verifier fan-out path.

## F5. Verifier blind spot: negative-prose claims are never checked

The machine checks verify POSITIVE references (`mini_ork/*.py` paths, env
vars, symbols — AC4-style `test -f` loops), but not NEGATIVE assertions
("no X exists", "`ls deploy/` returns nothing") nor non-`mini_ork` path
literals (`<run_dir>/…`). Three fabrications of exactly this class survived
a full revision iteration of the survey report (fabricated-internals count
9 → 3; the residual 3 were all negative-prose or foreign-literal claims) —
see `run-1791798883-27913/panel-verdict.json` → `modern_technique_gaps`.
Operator repair note: `run-1791798883-27913/operator-repair.md`.

**Suggested fix (recipe-level):** extend `reviewer_hard_rules` so every
negative assertion ("no X", "zero grep hits for X") must be re-grepped by
the verifier, and every path literal (any `<dir>/<file>` mention) must
`test -e` against the cited tree — not only `mini_ork/`-prefixed paths.
