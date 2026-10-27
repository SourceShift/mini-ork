# Dispatch resilience (bif), 2026-10-27

Follow-up to the Bifrost study (`maximhq/bifrost`, an LLM gateway), and the
rest of the 2026-10-26 session's open items. The user chose: Bifrost items 1–4
plus the mock provider; build the in-lock review fix for their approval; wire
or remove `MINI_ORK_ON_EVENT`.

## Measured starting point

- **`llm_calls` failures:** 2,675 failed rows, **1,881 `unknown`**. The
  unknowns include 488 timeouts, 128 spawn failures, 93 cwd-confusion
  guards and 64 shape rejects. Unknown means no retry, no fuse, no diagnosis.
- **The fallback walk moves on after any failure;** nothing reads
  `Retry-After` / "try again at".
- **A hung lead lane burns the whole node timeout before fallback:** 3,000 s
  on `sde-3b1`, where two lanes timed out at 1,500 s each.
- **16 of 127 multi-attempt framework-edit runs changed lanes between
  rounds.** Five were fallback-walk effects that lose the provider prompt
  cache (11,109 rows carry cached input).
- **`throttle_guard.py` is unwired by its own docstring.** The lane fuse has
  no per-class cooldown and no probe.
- **`MINI_ORK_ON_EVENT` is documented and shown in the IDE, but nothing calls
  it.** The emitter also blocks up to 5 s per event and can raise.

## Decisions

| # | Change | Kickoff |
|---|---|---|
| R2e | The merge driver tells the push hook which patch it already reviewed, so the in-lock push never runs the LLM panel; also fixes three open gate findings (a crashed panel, secure-tier block findings, zero-row cache hits) | `bif-r2e-review-outside-lock.md` (high tier: the user approves the merge) |
| E1 | `MINI_ORK_ON_EVENT` goes live as the no-server tier of event push (SSE on `serve` is the served tier): non-blocking, a command with arguments (no shell), heartbeats opt-in, plus a `run_finished` event | `bif-e1-event-hook-live.md` |
| B6 | `kind: mock` lanes with rule files (match prompt / lane / attempt → response or preset failure; seeded probability; latency), so dispatch changes are testable | `bif-b6-mock-provider.md` |
| B3 | Lane affinity: a node's later rounds try first the lane that served its first success, while the routing lead is unchanged; an operator remap always wins | `bif-b3-lane-affinity.md` |
| B1 | Typed failure classes and one action table (retry the chain / next lane / stop the walk / wait hint), stored in `metadata_json` (the `error_category` CHECK is unchanged) | `bif-b1-failure-classes.md` (after B6, B3) |
| B2 | A first-output deadline (default 300 s) on every streaming lane but the chain's last | `bif-b2-first-output-deadline.md` (after B1) |
| B4 | Held lanes: per-class cooldowns from B1's classes and wait hints, plus a single probe on re-admission | **Deferred** (measured below) |
| G1 | The gradient extractor's hard-coded 120 s cap becomes `MINI_ORK_GRADIENT_TIMEOUT_S` (default 300) | `bif-g1-gradient-timeout.md` |
| B1b | The pre-push review's findings on B1 and B3: stdout prose may only promote to provider-structured classes (never a fuse-terminal one), the model-access phrase stays on one line, affinity reorders the FINAL chain (it was a no-op for the planner), transient failures carry the wait hint | `bif-b1b-review-findings.md` |
| R5c | Test gaming is HIGH on every review attempt; runtime and eval render the round block from one function, and the eval replays the block a case's run received; an errored eval case scores as an error | `bif-r5c-gaming-is-high.md` |
| S1 | A wrapped "do not modify" sentence keeps its polarity on every line of the scope check (found in R5c's static-check log: every bif kickoff wraps it, so each run was allowed `docs/`, `recipes/` and the kernel-frozen list) | `bif-s1-scope-wrapped-deny.md` |
| S2 | Revise and repair feedback leads with the FAILED checks' own log sections (R5c's three rounds never saw the one-line finding) | `bif-s2-feedback-names-failing-check.md` (high tier: `mini_ork/cli/execute.py`) |

Not adopted (from the study): a semantic response cache, virtual keys and
multi-tenant governance, Starlark code mode. A per-lane `gateway:` setting to
use Bifrost itself as an optional secure-tier transport stays a later option.

## Notes

- The quarantined RSI cycle `rsi-c1792992296-8a0044f3` held stale claims on
  `llm_dispatch.py`, `telemetry.py` and `db/migrations/0085_llm_calls_lane.sql`.
  The claims were released (the branch and worktree are kept). Its migration
  number collides with `0085_review_findings.sql` on main if it is ever
  revived.
- The rev6 runs R2a, R4, R5a and R5b were closed `superseded` with their
  landed commit (f9a73858, 36715db8, 547d0522, 1487a237). The supervisor's
  rule only closes `failed` rows whose diff landed unchanged.
- Anomaly: run `rev6-r2d-gate-cache-degraded-20261026-192210` completed (14
  `llm_calls`, 10 `node_attempts`) but has no `task_runs` row. Nothing in
  `mini_ork/` deletes or replaces `task_runs` rows; cause not found.

## Results

| # | Landed | Notes |
|---|---|---|
| B6, B3, E1, R4b | on main | Operator review fixes before each merge (see each commit) |
| B1 | 437b2fa3 | Operator fix-forward: the kickoff stopped the walk on every `environment` failure, but `[Errno 2]` reads the same for a missing cwd (run-wide) and a missing CLI binary (one lane), and `classify_error`'s `config` (missing key, `$X is empty`, no providers.yaml entry) maps there too. A laptop with one provider key would have stopped at the first keyless lane. Now `spawn_local` names a missing cwd and only run-wide faults stop the walk. Three blind spec tests pinned the kickoff's rule and fail by design |
| G1 | f1dc3604 | Default 300 s; pattern_induction keeps 120 s (it holds a process lock for its whole dispatch) |
| R5c | 1ccebeb7 | Operator salvage: the no-test-gaming scan flagged the new prompt line, which quoted the probe tokens (the kickoff's own exact text); reworded. Round feedback is read from `revise/round-<attempt-1>.md` first |
| B1b | e3457063, e3649b22 | The first merge was blocked by review 123 (two highs): run-wide environment rows now carry the `:refused` actor and never count as fuse strikes; a stdout-promoted class lands in metadata only, never in the fuse-read `error_category` |
| S1 | 7d106336, 8adc3fa0 | Review 130 found a regression (a `Do not` line after an unterminated line); fixed with `break_before_negative` |
| B2 | 7a9affcf | Real lanes: glm, deepseek_flash, opus armed; codex not. Remote/workspace spawns get no deadline (by design; follow-up below) |
| Flag docs | b25c845e, + B2 rows | 10 flags documented |
| R2e | awaiting the user's approval | High tier (`scripts/mini_ork_worktree.py`); approval binds to patch-id 2e5bb817 |
| S2 | awaiting the user's approval | High tier (`mini_ork/cli/execute.py`); approval binds to patch-id 9c00f10b. Replayed on the R5c run dir, the repair section names no-test-gaming and quotes execute_handlers.py:1715 |

### Why the in-run reviewer passed K10, K12 and K13

The reviewer eval (lane `opus_lens` → GLM-5.3) blocks all three, so two
explanations were tested:

1. **Missing verifier context** (R4b): with the reports, summary and verdict
   the in-run reviewer saw, the eval still scores 5/5 incidents, 0/7 false
   blocks (fingerprint 78883e82, $2.10). Refuted.
2. **A repair round the reviewer never re-reviewed**: in all three runs the
   last review ran after the last implementer round, over a diff that
   contained the defect. Refuted.
3. **The round-aware severity bar.** The home sets `MO_REVIEW_ROUND_AWARE=on`
   (since 2026-10-08), so the reviewer is told to block only on HIGH, and on
   the final attempt only if shipping is worse than discarding. The in-run
   K10 reviewer named the pytest shim, rated it medium and passed. Replaying
   K10 with the block its run received (attempt 4 of 4): pass 2/2, defect
   named both times; without it, block 2/2. **Confirmed for K10.** K12 (arm
   on, attempt 1 of 3) and K13 (holdout arm, no block) still block 2/2 with
   their block, so their misses are not explained yet: the eval still omits
   the injected lessons, artifact context and scope guard, or the in-run
   misses were single bad samples. R5c makes test gaming always high and
   lets the eval measure the configuration this home runs.

**After R5c** (eval inputs v3, fingerprint b4d0ee6c, 577eaca6): with the
round block its run received plus the new "test gaming is HIGH" line, K10
blocks 3/3 and names the defect (0/2 with the same block before R5c).
Overall: incidents 5/5, clean 0/7 false blocks, 1 error (clean-k4 timed out,
now scored as an error), $2.47.

### Where failed-call time goes (14 days to 2026-10-27)

48.8 hours over 1,073 failed `llm_calls` rows. Timeouts are 41 h (84%).
`gradient-extract` on glm alone is 21.9 h: 411 calls, each `timeout after
120.0s`, against successes with p90 107 s and p95 113 s, so the hard-coded
cap cuts the latency tail of a call that mostly works (G1). The classes B4
would hold (quota, auth, config, capacity) cost 0.1 h: the existing 1-strike
fuse already fails them fast. B4 is deferred until a measurement shows it
pays; B2 (the first-output deadline) is the targeted fix for hung lanes.

### Open follow-ups

- **Remote first-output deadline.** B2 arms only host spawns; the
  workspace/remote transport does not read stdout line by line.
- **Reviewer-eval fidelity** (review 120, medium): the eval resolves the
  dispatch lane with a different home than `fingerprint()`, caps the diff
  twice (`_cap` then `cap_block`), renders `(not available)` slots for
  commit-sourced cases, and `copy_evidence` on an unreadable run dir deletes a
  case's stored evidence.
- **K12 and K13** in-run misses are still unexplained after the round-block
  replay; the eval still omits the injected lessons, artifact context and
  scope guard.
- **`throttle_retryable`** is dead code since B1, kept because
  `test_dispatch_retry_py.py` pins it.
