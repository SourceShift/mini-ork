# Review layers (rev6), 2026-10-26

Source: a comparison of mini-ork's code review with the Claude Code team's
process (Addy Osmani, "How the Claude Code team does code review"). Their model
has four parts: layers sized by risk, an AI reviewer that is measured against
incidents, review rounds that converge, and a fast rollback. mini-ork was
already ahead on execution-anchored verification (blind cross-family spec
tests, base replay, LLM veto-only, content-bound approvals, a merge gate on the
combined tree). It was behind on the rest.

## What was found in the code (2026-10-26)

- **Layer 3 never ran.** The pre-push AI review last ran on 2026-06-27. The
  Python reviewer `mini_ork/pre_push_review.py` replaced the bash in 07ecd58f,
  but `.githooks/pre-push:118` still gates on the deleted
  `lib/pre_push_review.sh` and skips. All 257 recorded findings are `open`, and
  a finding has no identity across reviews.
- **No risk tiers.** `task_class.risk_class` is declared and never read. The
  approval ledger (`merge_approval`, bound to content) exists, but the merge
  driver never consults it.
- **The reviewer is not measured.** Its lane `opus_lens` resolves to glm
  (GLM-5.3) through `agents.local.yaml`. `judge_validity` compares it with the
  verifiers, not with incidents, and is off.
- **The review loop loses findings.** The runtime envelope asks for
  `pass|fail|needs_revision|escalate` + `notes`, while `reviewer.md` asks for
  `approve|revise|reject` + `findings`. Reviews follow the runtime, so the
  implementer receives notes only. Every round re-reviews the whole diff.
  `revise_rounds` has had no writer since K2.
- **Implementer attempts per framework-edit run** (`node_attempts`): 1→273,
  2→78, 3→32, 4→4, 5→4, 6→2.
- **Test gaming passed every gate.** K10 (spec-suite detection + a pytest
  patch) and K12 (an import-time write into another module) passed the
  verifiers, the reviewer and the contract checks. A person caught both.

## Decisions

| # | Change | Kickoff | Notes |
|---|---|---|---|
| R1 | Deterministic scanner for production test-gaming patterns (`mini_ork/verify/gaming_scan.py`) | `kickoffs/auto/rev6-r1-gaming-scan.md` | `static-check.py` is frozen-kernel; wiring it in is an operator commit |
| R2a | Layer 3 runs again through `mini_ork/review/gate.py`: primary DB from a worktree, parallel lenses, live default panel, cached by patch-id, reviewed before the merge lock | `rev6-r2a-prepush-gate.md` | The secure review tier fails closed when the review cannot run |
| R2b | Finding lifecycle: fingerprints, `open/fixed/dismissed/pre_existing`, a dismissal needs a reason, pre-existing findings never block | `rev6-r2b-finding-lifecycle.md` | New tables in migration 0085; the old issue rows are kept |
| R3 | Risk tiers by change (paths + frozen kernel + `risk_class`). High tier needs a person's approval bound to `git patch-id`, typed at a terminal. The merge driver also runs the R1 scanner | `rev6-r3-risk-tiers.md` | Rules are read from `origin/main`, so a branch cannot loosen its own check |
| R4 | Reviewer eval harness: incident and clean cases, a fingerprint of prompt + envelope + lane + model, `garden` warns when stale | `rev6-r4-reviewer-eval.md` | The case set is curated data, added with `reviewer-eval add-case` |
| R5a | One reviewer contract, a findings ledger with stable ids, a delta since the last review, structured carried-over feedback | `rev6-r5a-review-converge.md` | `MO_REVIEW_CONVERGE=off` disables the prompt blocks; the contract fix stays |
| R5b | `revise_rounds` writer (recover-safe numbering) + `mini-ork metrics revise` | `rev6-r5b-rounds-to-pass.md` | |
| R6 | `mini-ork undo <commit\|run-id>`: a gated `git revert`, never a rewrite | `rev6-r6-undo.md` | Built on `mini_ork/rsi/worktree.py::revert` |

Order: R1, R2a, R5a and R6 run first. Then R2b (after R2a), R5b and R4 (after
R5a), and R3 (after R1 and R2a, since it shares the merge driver).

## Standing authorization changes when R3 lands

High-tier merges stop being autonomous: the agent prepares them, and a person
approves them at a terminal.

## Outcome (2026-10-26, same day)

| # | Landed | How |
|---|---|---|
| R1 | `961fe0ef` gaming_scan; `edb6b9b2` static-check `no-test-gaming` row (operator commit, frozen file) | K10 caught (15 findings); K12 caught (cross-module-write) against the patched tree |
| R2a | pre-push gate, primary DB, patch-id cache, review before the merge lock | Salvaged: 6 spec tests called a fixture directly. Default panel `glm deepseek_flash` (minimax quota, kimi 401, codex unsupported) |
| R2b | finding lifecycle + per-lens outcomes (`review_lens_runs`, migration 0085) | Run passed all verifiers and the reviewer |
| R2c | lenses get the primary `MINI_ORK_HOME`; timeout 600 s; "no LLM lens ran" warning / secure-tier block | Found by R2b's lens lines: every worktree-merge review since R2a ran heuristics only (`$GLM_API_KEY is not set`) |
| R3 | risk tiers + TTY approval bound to patch-id; merge driver runs the scanner + risk check | Salvaged: 2 unsatisfiable spec tests |
| R4 | `mini-ork reviewer-eval` + 12 cases (5 incidents, 7 clean) | Salvaged: the repair attempt had shaped code + seed data to a defective spec test (lookup by id, a fake kickoff heading); both removed. Alias-resolution bug fixed on first live run |
| R5a | one reviewer contract, findings ledger with ids, review of the delta | Salvaged: 1 spec test re-created its own directory |
| R5b | `revise_rounds` writer + `mini-ork metrics revise` | Salvaged: 3 spec INSERTs bound 2 values to 3 placeholders |
| R6 | `mini-ork undo` | Run passed |
| R7 | spec-tests verifier labels fixture-misuse harness defects and files spec-test-defect.md | Run passed |

| R2d | a degraded review (no LLM lens ran) is never cached; an empty panel counts as "no lens ran" | Found by the first working LLM review (#65), reviewing R2c itself; both lenses agreed. Its merge review (#69) marked 3 of #65's findings `fixed` |

### First reviewer score (reviewer-eval, fingerprint 382cb8d46be94da2)

Lane `opus_lens` → glm (GLM-5.3), R5a envelope, 12 cases:

- **Incidents:** 5/5 blocked AND naming the defect (recall 1.0; Wilson 95%
  lower bound 0.57 at n=5).
- **Clean changes:** 0/6 false blocks (1 timed out).
- **Cost:** $5.01.

Caveat: in their real runs this reviewer passed K10, K12 and K13. The eval
prompt has the kickoff and the diff but not the run's (all green) verifier
reports, so it probably overstates in-run recall. Next eval change: feed
cases the in-run reviewer inputs.

One clean case (`clean-k12-landed`) was first blocked as an out-of-scope
`scheduler.py` edit. The data was wrong, not the reviewer (the landed fix
legitimately touched a file the K12 kickoff did not list), and the case's
kickoff now lists it.

### What the program taught

- **Unsatisfiable blind spec tests were the main cost.** Five of nine runs
  failed only on spec tests no implementation could pass (fixture called
  directly, helper mkdir twice, binding-count mismatch, file and directory at
  one path, a fixture contradicting its own precondition). R7 catches the
  fixture-misuse class; the others still need a person.
- **Auto-repair games what it cannot satisfy.** Twice the repair implementer
  went after the grader or the data instead of the code: a pytest-7
  compatibility shim in the spec verifier (R2a), and code + seed data shaped
  to a defective test (R4). The engine-separate verifier made the first
  harmless; the second was caught in review. Stop auto-repair
  (`.stop-requested` + kill `recovery.planner <run>`) before salvaging a
  failed run's worktree.
- **The LLM half of Layer 3 was silently dead,** first from a 180 s timeout,
  then from a missing `MINI_ORK_HOME` in worktree merges. Recording per-lens
  outcomes (R2b) is what made it visible.

### Follow-ups

- **High tier, needs a person: never run the LLM panel inside the merge
  lock** (review #69 finding d6c35ecb). Since R2d no longer caches degraded
  reviews, a pre-review whose lenses failed makes the in-lock push hook
  re-run the panel (up to 600 s) while holding the lock. Fix: the merge
  driver tells the hook the patch was already reviewed (e.g.
  `MO_REVIEW_DONE_PATCH_ID`), and the in-lock hook reuses that verdict. This
  touches `scripts/mini_ork_worktree.py`, which is high tier.
- **Open pre-push findings on `mini_ork/review/gate.py`:** see
  `mini-ork review findings --file mini_ork/review/gate.py` (e.g. a partial
  `ok` lens row defeats the all-failed check; the secure-tier early return
  hides blocking findings).
- **The rev6-r2d run has no `task_runs` row** although it published: a
  silent bookkeeping write failure, still to be investigated.
- **Stale run rows:** rev6 R2a/R4/R5b stay `executing` and R5a `failed`,
  although all landed on main by operator salvage (their auto-repair was
  stopped). The supervisor's superseded check closed R3 the same way.

- **Spec tests cannot exercise the CLI of the change under test.**
  `bin/mini-ork` resolves its root from `MINI_ORK_ENGINE_ROOT`, then
  `MINI_ORK_ROOT`, then `.mini-ork/engine`, so a spec test that spawns it runs
  the engine. Scrubbing one variable is not enough. Either spec tests call
  `python -m mini_ork.cli.main` with the target on `PYTHONPATH`, or the
  launcher gets an explicit "use my own tree" mode.
- **Heuristic findings that go silent are marked fixed** even when the next
  diff did not touch the offending line (R2b reviewer finding F1).
- **The spec-test author needs a self-check.** Run each authored test against
  the BASE and classify errors raised inside the test's own code as harness
  defects before the implementer starts.
- **Score the reviewer after every prompt, lane or model change** (`garden`
  warns), and grow the incident set as new incidents land.
