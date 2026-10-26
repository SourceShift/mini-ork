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
