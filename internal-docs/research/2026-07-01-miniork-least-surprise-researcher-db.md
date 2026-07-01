# How mini-ork reduces "surprise" for the researcher app — DB analysis (2026-07-01)

Source: `/Volumes/docker-ssd/Migration/Development/researcher/.mini-ork/state.db`
(264 `task_runs`, 2026-06-10 → 2026-06-30).

## TL;DR
mini-ork's least-surprise value for researcher today is **containment**, not
improvement: ~95% of run churn dies inside its sandbox/worktrees and never touches
the app. But two gaps mean it *contains* surprise without yet *reducing* it — and a
chunk of the "failure" is mini-ork's own instability, i.e. **added** surprise.

## 1. Outcome distribution — the containment story
| status | n | note |
|---|---|---|
| failed | 119 | caught in-run |
| failed / CRASH | 76 | **engine crash (self-inflicted)** |
| classified only | 43 | abandoned before execute |
| executing (stale) | 30 | **zombie: crashed w/o status finalization** |
| published | **14** | reached the app |
| planned / reviewing | 4 | in-flight |

Only **14 / 264 (5.3%)** published. The other 95% — failed, abandoned, or stale —
were absorbed by the orchestrator. Verifier/reviewer/rollback gates mean nothing
merges unverified: **the surprises happen in a blast-shielded runner, not in
production.** That is the core mechanism by which mini-ork lets researcher get built
with the least production surprise: trade lots of cheap, contained, sandboxed
failure for very few app-level surprises.

## 2. The DB *is* updated per run — with two write-back gaps
The 264 rows are real recorded outcomes (the numbers above come straight from them),
so it's NOT that the DB isn't updated. But:
- **30 stale `executing` rows** = runs that crashed/were-killed without the status
  being flipped to `failed`. Abnormal-exit status finalization is missing (same
  crash/corruption class as I-5/I-7/I-16). A dying run orphans its own status row.
- **The learning half of the DB is empty.** `gradient_records` = **8,951** (extraction
  works, growing ~900→3,700/week) but `emergent_patterns` = `pattern_records` =
  `promotion_records` = **0**. mini-ork records *what happened* and even *what could
  improve*, but the reflect→**improve→promote** step never writes back.

## 3. What it's learning about (gradient targets, top)
| target | signals |
|---|---|
| `workflow.recipe.code_fix` | 397 |
| `workflow.node.planner` | 367 |
| `workflow.recipe.framework_edit` | 258 |
| `verifier.*` | ~470 |
| `workflow.node.implementer` | 205 |
| planner/implementer→verifier edges | ~386 |

~9,000 targeted improvement signals, concentrated exactly on the weak spots — a rich
map for GEPA/GRPO to reduce future surprise. It's just not being consumed.

## 4. Surprise is contained, not declining (the honest caveat)
Weekly published/failed: W23 5/31 · W24 0/41 · W25 5/81 · W26 4/42. The failure rate
stays flat (~60–75%) — because the learning loop doesn't close (§2). And 76 CRASHes
(epic-runner 16, code-fix 15, framework-edit 14, refactor-audit 11) are mini-ork's own
bugs, not real catches — **added** surprise, the class the I-5/I-7/I-16 fixes + sandbox
isolation shipped this session directly attack.

## 5. So: how mini-ork helps build with least surprise
- **Primary (working): containment.** Verifier-gated, sandboxed, rollback-on-fail →
  95% of attempts never reach the app. Nothing ships un-gated.
- **Secondary (built, unused): a 9k-signal learning corpus** aimed at its weak spots.
- **Cost: engine instability** injects self-inflicted surprise (crashes / stale status).

## 6. The next levers (to make surprise actually *decline*, not just be contained)
1. **Close the learning loop write-back** — synthesize `gradient_records` into
   `emergent_patterns` → `promotion_records` and feed promoted prompts back (this is
   exactly what the GEPA wiring, R4b, + the reflect→improve→promote path should do;
   verify it runs and populates these tables in researcher). Highest lever.
2. **Finalize status on abnormal exit** — a reaper that flips stale `executing` →
   `failed`/`crashed` so the ledger is truthful (also fixes the 30 zombies).
3. **Kill the self-inflicted CRASH class** — the engine fixes + per-agent sandbox
   from this session; re-measure the CRASH share after they sync into researcher.

**Net:** mini-ork already buys researcher "few production surprises" via containment.
Turning that into "fewer surprises over time" needs the learning loop to actually
close — collecting 9,000 lessons and acting on none is the biggest missed lever.
