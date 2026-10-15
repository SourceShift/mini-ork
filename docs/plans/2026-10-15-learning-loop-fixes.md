# Learning-loop fixes — decisions and delivery waves (2026-10-15)

Source: four-family judge panel `learning-judge-4l-20261015125313`
(recipe `audit-judge-panel-4l`; opus-5-5, GLM-5.3, deepseek-v4-flash, MiniMax-M3),
synthesis at `.mini-ork/runs/learning-judge-4l-20261015125313/synthesis.md`.
All 17 audited findings were upheld (several with corrected numbers); the panel
added 16 new findings (X1–X16) and a 13-step solution plan (R1–R13). The
operator asked for all of it, deciding each open question by the panel's
recommendation.

## Decisions (panel §7 questions)

| Q | Question | Decision | Lands in |
|---|---|---|---|
| Q1 | What does `'*'` mean; where do two-class themes go | A theme stores its **member-class set** (`code_fix` or `code_fix,framework_edit`, sorted, comma-joined). Selection: own class exactly, then themes whose set contains the class, then legacy `'*'` rows only to fill. New themes never get `'*'` or NULL. | R1 |
| Q2 | Should "verified" keep its name | The stored status stays `verified` (readers depend on it), but no prompt or page may call it "verified on earlier runs of this task class". The prompt header says what it is: induced from earlier runs and guardrail-checked, effect not yet measured. An `effective` status waits for R13's measurement. | R1 |
| Q3 | Auto-retirement | **Report-only, operator-confirmed.** `memory/retirement.py`'s "never auto-retires" doctrine stands. | R13 |
| Q4 | Landing signal for findings | Max-age window, then quoted-code staleness at HEAD (R11a), then a **patch-id** landing check against `origin/main` (R11b). Never `task_runs.status`. | R11a, R11b |
| Q5 | Arm accounting after R3 | v2 runs before R3 are reported as their own arm `v2_legacy` (they received v1+v2); lift is computed on post-R3 runs only. | R13 |
| Q6 | Lens alias naming and fallbacks | The overlay is operator-owned and `opus_lens: glm` is a deliberate cost choice (2026-10-09 directive), so it stays. The system becomes honest instead: a panel **fails** when two lens aliases resolve to one model family or a fallback served a lens; `agents_config` warns at merge when sibling lens aliases share a family; each run records its own effective lane map. | R7a, R7b |
| Q7 | This panel's gate | Fixed at the source (13af18a4); the run was revived to `published`. | done |
| Q8 | Findings with no repo | A finding with NULL `target_repo` is **engine-local**: it is offered only when the current target is the engine checkout, never as a wildcard. | R12 |
| Q9 | Scope guard vs kickoff for panels | Deferred: future panel launchers copy the named runs' `learned/*` and packs into the panel run dir. Not in this batch. | — |

Operator actions taken now: path rule `review-196c4795` removed (it would have
injected an already-fixed bug under "set by the user").

## Delivery — one framework-edit kickoff per item, in waves

Runs in one wave touch disjoint files and run in parallel, each in its own
worktree off `origin/main`. A wave starts after the previous wave merged.
Migration numbers are assigned at launch (next free on `origin/main`).

| Wave | Kickoff | Item | Files (main) |
|---|---|---|---|
| 1 | `lf-r6-harvest-dedupe` | R6 one row per finding per run | `mini_ork/learning/code_findings.py`, `mini_ork/cli/reflect.py` |
| 1 | `lf-r1-theme-selection` | R1 own-class themes first; class sets; honest header | `mini_ork/learning/themes.py`, `mini_ork/context_v2.py`, `mini_ork/context_assembler.py` |
| 1 | `lf-r4-rule-action` | R4 defuse "Make it a rule" | `mini_ork/ide_pages/learn/code.py`, `mini_ork/memory/preferences.py`, migration |
| 1 | `lf-r7a-panel-family-gate` | R7a family-level panel gate | `mini_ork/gates/panel_heterogeneity.py` (new), panel recipes |
| 2 | `lf-r5-harvest-hygiene` | R5 classifier, verifier reasons, `unknown` severity | `mini_ork/learning/code_findings.py`, `mini_ork/context_v2.py` |
| 2 | `lf-r2-pattern-scope` | R2 patterns scoped by task class | `mini_ork/context_assembler.py`, `mini_ork/learning/pattern_induction.py`, migration |
| 2 | `lf-r8-kickoff-scope` | R8 one scope parser; sentence-level Do-NOT | `mini_ork/kickoff_scope.py` (new), `mini_ork/cli/main.py`, `mini_ork/memory/preferences.py`, `mini_ork/context_v2.py` |
| 2 | `lf-r10-recurring` | R10 recurring = across kickoffs | `mini_ork/ide_pages/learn/code.py` |
| 3 | `lf-r3-inject-once` | R3 render themes once; no phantom planner retrievals | `mini_ork/cli/execute.py`, `mini_ork/cli/plan.py`, `mini_ork/context_assembler.py`, `mini_ork/context_v2.py` |
| 3 | `lf-r9-scope-fallback` | R9 v2 scope falls back to the profile allow list | `mini_ork/context_v2.py` |
| 3 | `lf-r7b-lane-provenance` | R7b merge-time family warning; per-run lane map | `mini_ork/dispatch/agents_config.py`, run-profile writer |
| 3 | `lf-r12-target-repo` | R12 target repo on findings | `mini_ork/learning/code_findings.py`, `mini_ork/context_v2.py`, migration |
| 4 | `lf-r11a-finding-staleness` | R11a age window + staleness at HEAD | `mini_ork/learning/code_findings.py`, `mini_ork/context_v2.py`, `mini_ork/ide_pages/learn/code.py`, migration |
| 4 | `lf-r13-efficacy` | R13 measurable arms; report-only retirement | `mini_ork/context_v2.py`, `mini_ork/cli/execute_handlers.py`, `mini_ork/cli/metrics_context.py`, `mini_ork/memory/retirement.py` |
| 5 | `lf-r11b-landing-signal` | R11b patch-id landing check | `mini_ork/learning/code_findings.py`, migration |

Every kickoff carries a `measure` block (run by the kickoff-contract verifier
from the pre-run snapshot) and a Required-artifacts list. After each run the
diff is reviewed clause-by-clause against its kickoff before merge, because a
green framework-edit verdict proves only that the implementer's own tests pass.
