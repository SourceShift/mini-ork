# K0: why most mini-ork spend goes to runs that never publish

*2026-10-07 · read-only analysis for `kickoffs/sdd-mechanisms/k0-unpublished-spend-root-cause.md`.
Method: every unpublished `task_runs` row was classified into exactly one bucket
(first matching rule wins) from the DB (`task_runs`, `llm_calls`, `run_events`,
`execution_traces`) plus its run dir (`verdict.json`, `review-verdict.json`,
`panel-verdict.json`, `verifier_*.json`, `*.checks.tsv`, `execute.log`,
`run_profile.json`, `plan-failure-*`). No code, config or DB row was changed.*

**Data.** Frozen snapshot of `.mini-ork/state.db`, taken 2026-10-07 10:45 with
the SQLite backup API:
`/Volumes/docker-ssd/Migration/Development/backups/k0-baseline-20261007-104547.db`
(1342 runs, 467 published, 388 non-terminal). The snapshot exists because the
run reaper shipped the same morning (`7954fa6f`, `ec54774d`, `bf2805dd`) starts
moving dead non-terminal rows to `failed` on every board read; measuring the
live DB would let H2 shrink under the analysis. The SDD campaign was snapshotted
the same way: `.../backups/k0-sdd10x-20261007-110708.db` (45 runs, 0 published).

## Short answer

- **The 66.9% is real, and worse once the meter is corrected.** At list price
  the whole project cost **$1,169.04**, not $3,151.17. Published runs shrink
  more than unpublished ones, so the unpublished share goes **up from 67.0% to
  76.0%** (H1 rejected as an explanation).
- **Top 3 root causes, ranked by corrected dollars:**
  1. **Silent death: the run never reached a terminal status** — 315 runs,
     $430.76 raw / **$209.94 corrected**. Not SDD; mostly fixed by this
     morning's reaper and lifecycle teardown, two mechanisms still open.
  2. **Post-implementation gate rejection (reviewer / rubric panel)** —
     138 runs, $550.25 raw / **$186.01 corrected**. Mixed: some are correct
     catches that a vacuous verify missed, some are false rejects (an
     *advisory* rubric that still fails runs; verdicts that contradict their
     own checks).
  3. **Verify failure** — 162 runs, $522.99 raw / **$148.60 corrected**.
     Dominated by harness probes, not product tests: two framework-edit probes
     fail in most framework-edit verify failures.
- **Biggest single harness defect outside the top 3:** the reviewer's verdict
  is unparseable (`verdict=unknown`) in 16 runs, $140.21 raw / $89.06 corrected.
- **SDD campaign:** 9 of 45 runs ($167.44, 27%) were **approved by the panel and
  then rolled back** after a publisher error whose reason was never logged (H6).

## AC1 — unpublished spend, mutually exclusive buckets (mini-ork home)

Buckets sum to the total exactly (AC1 tolerance ±0.5%). Raw = `task_runs.cost_usd`;
corrected = the H1 rule below. Labels: (a) no publish expected, (b) never
finalized, (c) false-reject candidate, (d:stage) real failure at that stage,
(e) infra / harness, (f) other.

| Bucket | AC3 label | Runs | Raw $ | Raw % | Corrected $ | Corr. % | Top recipes (raw $) |
|---|---|---:|---:|---:|---:|---:|---|
| B2 never finalized (non-terminal status) | (b) | 388 | 477.34 | 22.6 | 186.80 | 21.0 | verified-artifact 128, chapter-review 103, rsi-technique-review 55 |
| B17 harness: reviewer verdict unparseable ('unknown') | (e) | 16 | 140.21 | 6.6 | 89.06 | 10.0 | research-synthesis 63, doc-to-features-loop 51, recursive-self-improve 17 |
| B12b real failure: reviewer rejected (no verify result) | (d:reviewer) | 39 | 192.62 | 9.1 | 81.76 | 9.2 | framework-edit 90, code-fix 71, book-gen-flow-audit 21 |
| B15 other / unclassified | (f) | 23 | 83.96 | 4.0 | 78.70 | 8.9 | recursive-self-improve 61, framework-edit 21, obs-smoke 1 |
| B9b real failure: a verifier check failed | (d:verify) | 73 | 176.47 | 8.4 | 58.46 | 6.6 | code-fix 90, frontier-llm-research 41, recursive-self-improve 38 |
| B9 real failure: verify tests failed | (d:verify-tests) | 34 | 183.27 | 8.7 | 57.39 | 6.5 | framework-edit 183 |
| B7 verify PASSED, run rejected downstream (candidate false reject) | (c) | 34 | 86.12 | 4.1 | 56.97 | 6.4 | framework-edit 86 |
| B6 dead before work: planner failure | (d:plan) | 6 | 66.81 | 3.2 | 52.17 | 5.9 | epic-runner 42, findings-validation-panel 20, bug-audit-cmgk 3 |
| B1 crash-finalized / killed (reaped CRASH) | (b) | 37 | 64.67 | 3.1 | 49.87 | 5.6 | framework-edit 24, code-fix 11, researcher-qdrant-contract 8 |
| B18 reviewer escalated to a human (no verdict) | (f) | 2 | 49.04 | 2.3 | 39.26 | 4.4 | recursive-validate-impl 49 |
| B13 node error in verifier | (d:verifier) | 25 | 112.49 | 5.3 | 24.78 | 2.8 | code-fix 85, framework-edit 27, audit-findings-validator 1 |
| B7e verify + review passed, run still failed (traces only) | (c) | 21 | 112.47 | 5.3 | 24.69 | 2.8 | chapter-review 112, code-fix 0 |
| B5 dead before work: profile needs_answers | (d:profile) | 40 | 29.60 | 1.4 | 24.52 | 2.8 | recursive-self-improve 14, refactor-audit 10, audit-judge-panel 3 |
| B7b verify passed, reviewer rejected (candidate false reject) | (c) | 32 | 117.03 | 5.5 | 16.17 | 1.8 | code-fix 117 |
| B14 no run dir (unclassifiable) | (f) | 14 | 62.03 | 2.9 | 14.27 | 1.6 | chapter-review 62, frontier-llm-research 0 |
| B10 real failure: verify static/shape check failed (tests ok) | (d:verify-static) | 3 | 22.79 | 1.1 | 5.93 | 0.7 | framework-edit 23 |
| B4 infra: node cost_limit | (e) | 5 | 12.86 | 0.6 | 4.94 | 0.6 | bug-audit-fe-be 11, framework-edit 2, bug-audit-cmgk 0 |
| B8 real failure: implementer produced no diff | (d:implementer) | 15 | 17.40 | 0.8 | 4.36 | 0.5 | framework-edit 17, self-migrate 0 |
| B7c verify passed, panel/rubric rejected (candidate false reject) | (c) | 3 | 31.35 | 1.5 | 3.96 | 0.4 | rsi-technique-review 17, obs-smoke 9, frontier-llm-research 5 |
| B13 node error in implementer | (d:implementer) | 3 | 9.24 | 0.4 | 3.84 | 0.4 | code-fix 6, framework-edit 3, goal-loop 0 |
| B4 infra: node timeout | (e) | 2 | 9.58 | 0.5 | 3.03 | 0.3 | subsystem-integration-discovery 9, research-synthesis-mktcore 1 |
| B7d verify passed, reviewer trace failed (traces only) | (c) | 3 | 8.64 | 0.4 | 2.35 | 0.3 | chapter-review 7, code-fix 1 |
| B16 real failure: researcher/lens node failed | (d:researcher) | 18 | 12.14 | 0.6 | 2.08 | 0.2 | chapter-review 12, frontier-llm-research 0, verified-artifact 0 |
| B11 verify vacuous (nothing checked), run failed | (d:verify-vacuous) | 24 | 27.97 | 1.3 | 2.04 | 0.2 | verified-artifact 28 |
| B3 no publish expected (probe-scorer experiment arms) | (a) | 6 | 2.22 | 0.1 | 0.38 | 0.0 | code-fix__probe_11463_0 1, code-fix__probe_11463_1 1, code-fix__probe_19631_0 0 |
| B12c real failure: reviewer trace failed (traces only) | (d:reviewer) | 6 | 2.02 | 0.1 | 0.11 | 0.0 | framework-edit 2 |
| B9c real failure: verifier trace failed (traces only) | (d:verify) | 3 | 0.00 | 0.0 | 0.00 | 0.0 | verified-artifact 0 |
| **Total unpublished** | | **875** | **2110.33** | 100.0 | **887.89** | 100.0 | |

Rolled up by label:

| Label | Runs | Raw $ | Raw % | Corrected $ | Corr. % |
|---|---:|---:|---:|---:|---:|
| (d) real failure | 289 | 852.82 | 40.4 | 317.44 | 35.8 |
| (b) never finalized (B1 + B2) | 425 | 542.01 | 25.7 | 236.67 | 26.7 |
| (f) other / unclassifiable / escalated | 39 | 195.03 | 9.2 | 132.23 | 14.9 |
| (c) verify passed, rejected later | 93 | 355.61 | 16.9 | 104.15 | 11.7 |
| (e) infra / harness | 23 | 162.64 | 7.7 | 97.02 | 10.9 |
| (a) no publish expected | 6 | 2.22 | 0.1 | 0.38 | 0.0 |
| **Total** | **875** | **2110.33** | 100 | **887.89** | 100 |

### SDD campaign (`/Volumes/docker-ssd/ps/sdd-10x-ork/home`)

All 45 runs are after the meter fix, so raw = corrected.

| Bucket | AC3 label | Runs | Raw $ | Raw % | Corrected $ | Corr. % | Top recipes (raw $) |
|---|---|---:|---:|---:|---:|---:|---|
| B13 node error in publisher | (d:publisher) | 9 | 167.44 | 27.1 | 167.44 | 27.1 | recursive-validate-impl 167 |
| B9b real failure: a verifier check failed | (d:verify) | 10 | 132.27 | 21.4 | 132.27 | 21.4 | recursive-validate-impl 118, spec-driven-delivery 14 |
| B18 reviewer escalated to a human (no verdict) | (f) | 4 | 82.35 | 13.3 | 82.35 | 13.3 | recursive-validate-impl 82 |
| B13 node error in verifier | (d:verifier) | 3 | 80.24 | 13.0 | 80.24 | 13.0 | recursive-validate-impl 62, spec-driven-delivery 18 |
| B5 dead before work: profile needs_answers | (d:profile) | 6 | 74.36 | 12.0 | 74.36 | 12.0 | spec-driven-delivery 74 |
| B2 never finalized (non-terminal status) | (b) | 9 | 37.41 | 6.1 | 37.41 | 6.1 | spec-driven-delivery 22, recursive-validate-impl 16 |
| B7b verify passed, reviewer rejected (candidate false reject) | (c) | 1 | 17.06 | 2.8 | 17.06 | 2.8 | recursive-validate-impl 17 |
| B17 harness: reviewer verdict unparseable ('unknown') | (e) | 1 | 10.73 | 1.7 | 10.73 | 1.7 | recursive-validate-impl 11 |
| B4 infra: node cost_limit | (e) | 1 | 10.69 | 1.7 | 10.69 | 1.7 | recursive-validate-impl 11 |
| B13 node error in implementer | (d:implementer) | 1 | 5.32 | 0.9 | 5.32 | 0.9 | recursive-validate-impl 5 |
| **Total unpublished** | | **45** | **617.87** | 100.0 | **617.87** | 100.0 | |

## AC2 — meter correction rule

- **Overstatement (before 2026-10-01).** Calls with `provider = 'gateway'`
  (non-Anthropic models driven through the claude CLI) and `ts < '2026-10-01'`
  carried the CLI's `total_cost_usd`, which prices unknown models at Anthropic
  rates (fixed in `46f32302` + `13ba1c53`). Each such call is repriced from its
  own tokens at the `pricing.yaml` list rates:
  `cost = (input × in + output × out + cached_input × cache_read) / 1e6`,
  lane → model: minimax → MiniMax-M3 (0.30 / 1.20 / 0.06), glm → GLM-5.3
  (1.40 / 4.40 / 0.26), deepseek → deepseek-v4-pro (1.32 / 3.96 / 0.044),
  deepseek_flash → deepseek-flash (0.30 / 1.20 / 0.006), kimi → kimi-k2.7-code
  (0.95 / 4.00 / 0.30). Calls with no tokens recorded keep their raw cost.
- **Not corrected:** Anthropic calls (the CLI priced them correctly); `openai`
  / codex calls (already recomputed per family by the 2026-09-12 F2 backfill —
  repricing them again double-counts cached input); gateway calls on or after
  2026-10-01 (already list price).
- **Per run:** `corrected_run = raw_run × Σ corrected(call) / Σ raw(call)` over
  the run's `llm_calls`.
- **Undercount (before 2026-09-12).** Already repaired in the DB by the
  2026-09-12 backfill (`task_runs` = `llm_calls` for every ledger-backed run).
  **No correction possible** for the 71 unpublished runs with no `llm_calls`
  rows ($158.87 raw): they keep their raw (placeholder) cost in both columns.

Effect: MiniMax alone was recorded at $1,740.13 for ~1.42 B cached + 153 M input
+ 17.5 M output tokens, about $152 at list price (~11×).

## Hypotheses

| | Verdict | Numbers (snapshot) |
|---|---|---|
| **H1** meter artifact | **Rejected** as an explanation of the share. The meter inflated dollars ~2.7×, but correcting it raises the unpublished share from 67.0% to 76.0%. | total $3,151.17 → $1,169.04; published $1,040.84 → $281.15; unpublished $2,110.33 → $887.89 |
| **H2** never finalized | **Confirmed, and it is the #1 cause.** 425 runs, $542.01 raw / $236.67 corr (B1 + B2). Inside B2: 276 died mid-run ($309.49 / $147.80), 110 **passed** but were never published ($111.25 / $26.73), 2 had a fail verdict but the status write was lost ($56.60 / $12.27). | see RC1 |
| **H3** no publish by design | **Rejected.** Only the probe-scorer arms are no-publish by design: 6 runs, $2.22. Every recipe except `harness-bridge` has an `artifact_contract.yaml`, so the publisher can publish it. framework-edit's 6/173 is gate rejection, not hand shipping: in its 34 "verify passed" failures the reviewer said `needs_revision` (19) or the rubric panel failed it (25 have `panel_pass=false`). | B3 |
| **H4** false rejects | **Partly confirmed.** 93 runs, $355.61 raw / $104.15 corr reached a passing verify and were still failed. Hard false rejects: 12 failed only on the rubric pre-screen ($55.65 / $17.26) and 3 framework-edit runs passed every check in both checks tables yet `verdict.json` says `pass:false`. The rest (51 reviewer rejections after a passing verify, $155.49 / $53.92) cannot be called false without hidden-test truth, and the evidence cuts both ways (RC2). | B7, B7b–B7e |
| **H5** dead before work | **Confirmed but small.** profile `needs_answers` 40 runs $29.60 / $24.52; planner failure 6 runs $66.81 / $52.17; node `cost_limit`/`timeout` 7 runs $22.43 / $7.97. Lane failures are mostly invisible: 738 failed `llm_calls` rows have `error_category` NULL or `unknown`. In the SDD campaign `needs_answers` is larger: 6 runs, $74.36 (12%). | B4–B6 |
| **H6** rollback destroyed good work | **Confirmed in the SDD campaign:** 9 recursive-validate-impl runs, panel `APPROVE`, then publisher error, then rollback — $167.44. Not visible as such in the mini-ork home (goal-loop unpublished total is $0.75). | SDD B13 publisher |

## AC4 — top 3 root causes, with opened run dirs

### RC1 · Silent death (not SDD) — 315 runs, $430.76 raw / $209.94 corrected

The run's owning process ended without writing a terminal status, so the row
stays `executing` / `classified` / `planned` / `reviewing` and every reporter
counted it as in flight. Mechanisms found:

1. **Hard death** — SIGKILL / OOM leaves a `.pid` naming a dead process.
   `run-1781333552-17796-identity-and-rbac` (recursive-validate-impl,
   $41.10 corr): `.pid` written Jun 13 09:10, process gone, `execute.log`
   stops mid-`mini-ork reflect`, status still `executing`.
2. **Lost status write** — `set_status()` (`mini_ork/cli/execute.py`) retries
   three times on a locked DB and then only prints `[warn] … failed after
   retries`. `rsi-review-20260929-084403` (rsi-technique-review): `execute.log`
   ends `rollback complete` / `run-level verdict.json: fail (failed_nodes=20)`,
   status still `executing`.
3. **Execute-only callers** — libwit dispatches verified-artifact through
   `execute` without the publisher, so a passing run is never finalized.
   `run-1789811866-94475`: `oracle-gates: pre-publish pass`, `all nodes
   complete`, run-level verdict `pass`, status `executing`, kickoff in a temp
   `libwit-verified-artifact-*` dir. 110 such runs ($111.25 raw) — this is
   finished work, counted as unpublished only because nothing marks it done.
4. **Python-level abort** (exception, deadline exit): the old lifecycle deleted
   `.pid` and wrote no status. Fixed today in `7954fa6f`.

Status: (1) and (4) are fixed for new runs (`7954fa6f` lifecycle owns `.pid`
and writes `failed` at teardown; the board reaper fails provably dead runs;
`bf2805dd` keeps passing runs out of it). (2) and (3) are open.

### RC2 · Post-implementation gate rejection — 138 runs, $550.25 raw / $186.01 corrected

Labels (c) + (d:reviewer): the implementation existed and a reviewer or the
rubric panel failed it. The evidence is mixed:

- `run-1782939541-98592` (framework-edit) — **false reject.** `verdict.json`
  `{files_changed:4, tests_pass:true, static_pass:true, pass:true}`,
  `reviewer verdict=pass`, and the only negative is `panel-verdict.json`
  `{panel_score:62.5, pass:false, source:"rubric-prescreen"}`. The lifecycle
  labels the rubric "advisory pre-screen", yet it decided the run. 12 runs fail
  this way ($55.65 raw / $17.26 corr).
- `child-1789898497-42951` (code-fix) — **correct reject, wrong reason
  upstream.** The "passing" test verifier ran
  `echo scoped-test-skipped-bounded-goal-loop-demo`; the reviewer found a real
  P0 (a query on a `uuid` column the table does not have). Verify was vacuous;
  the reviewer did verify's job.
- `run-1781706039-50968` (book-gen-flow-audit) — reviewer `fail`, panel 25:
  a real reject.

### RC3 · Verify failure — 162 runs, $522.99 raw / $148.60 corrected

- `run-1781191884-92049` (framework-edit): every check in
  `verifier-test.checks.tsv` is `true` (incl. `web-smoke-tests-pass`), yet
  `verdict.json` says `tests_pass:false`; the real failure is
  `shell-syntax-clean false` in the static checks. The verdict misreports which
  half failed.
- `rsi-scan-2000-20260922-213524` (frontier-llm-research):
  `aggregation completeness failed: 2122 source sections and 2000 prompt
  sections` — an exact-count probe on a corpus that legitimately grew.
- Across the 34 framework-edit "tests failed" runs (B9) the failing check ids
  are mostly harness probes: `apply-sentinel-has-content` (28),
  `web-smoke-tests-pass` (26 — one smoke test failing across unrelated
  changes), `diff-applies-to-copy` (14), `patched-copy-created` (14). 3 of the
  34 passed every check in both tables and still record `pass:false`.

## Other findings worth acting on

- **Reviewer verdict unparseable** (B17): `execute.log` says
  `[ok] reviewer verdict=unknown` and the run fails — 16 runs, $89.06 corrected.
  A harness defect, fixable without SDD (structured verdict, fail the node on a
  parse miss instead of the run).
- **Rejection reasons are not persisted.** `execute.log` keeps stdout only; the
  publisher's `[fail]` / `[warn]` lines go to stderr. The 9 SDD publisher errors
  after `APPROVE` have no recorded cause (`node_end` payload:
  `{"node_type":"publisher","duration_ms":0,"finish_reason":"error"}`).
- **libwit chapter-review runs leave no run dir** (temp kickoff
  `/var/folders/.../libwit-chapter-review…`), so 14 of them ($62.03) are
  unclassifiable (B14) and 21 ($112.47) could only be read from traces (B7e:
  verify and review passed, run still failed).

## AC5 — mapping to I1–I9, and the proposed K-order change

| Root cause | Corrected $ | Maps to |
|---|---:|---|
| RC1 silent death — hard death / abort (B1 + B2 without a verdict) | 197.67 | **not SDD** (shipped today: `7954fa6f`, `ec54774d`, `bf2805dd`) |
| RC1 — lost status write (`set_status` swallows) | 12.27 | **not SDD** — zero-fallback: raise, do not warn |
| RC1 — execute-only callers never finalize passing runs | 26.73 | **not SDD** — caller finalizes, or a terminal "finished, unpublished" state |
| RC2 — advisory rubric acts as a gate | 17.26 | **not SDD** — gate wiring |
| RC2 — reviewer rejects after a passing verify | 53.92 | **I7** (convergence audit replaces reviewer verdict authority), **I2** |
| RC2 — other verify-passed failures (traces-only reads, no reviewer signal) | 32.97 | **I7** |
| RC2 — reviewer rejects with no verify result | 81.87 | **I1** (verify must check what the reviewer is checking), **I7** |
| RC3 — invalid / environment-dependent probes, verdict contradicts checks | 148.60 | **I1** (probe validity), then **I8** (repair instead of terminate) |
| Reviewer verdict unparseable | 89.06 | **not SDD** — structured verdict |
| Dead before work: `needs_answers` (+ SDD $74.36) | 24.52 | **I3** (kickoff contract + answer path) |
| Dead before work: planner failure | 52.17 | **I4** for oversize tasks; parse errors are **not SDD** |
| SDD: approved, then rolled back on a publisher error | 167.44 (SDD) | **I8** (guarded loop: never roll back an approved edit on a publish error) + stderr capture (not SDD) |

**Proposed K-order change** (current: K1 baseline → I1 → I5 → I3 → I2 → I6 → I8 → I7 → I4 → I9):

1. **Insert K0.5 "finalize and record every run" (not SDD) before K1.**
   Raise in `set_status` instead of warning; finalize execute-only runs; capture
   publisher stderr into `execute.log`; make the rubric pre-screen advisory as
   labelled; parse-fail the reviewer node on `verdict=unknown`. *Reason:* RC1 is
   the #1 cause and, with B17 and the stderr gap, keeps ~37% of corrected
   unpublished spend unattributable — K1's before/after metrics would measure
   noise.
2. **Swap I3 ahead of I5 (K3 ↔ K4).** *Reason:* I3 has a measured target in both
   DBs ($24.52 here + $74.36 in SDD); I5 has no bucket in K0, and nothing
   before K5 depends on it.
3. Keep I1 as the first SDD epic. *Reason:* RC3 and the "vacuous verify, reviewer
   catches it" half of RC2 both reduce to probe validity.

## Limits

- (c) is a *candidate* false-reject label: without hidden-test truth a reviewer
  rejection after a passing verify can be right (RC2's second example).
- Precedence decides ties: a run that both errored in a verifier and was
  rejected by the reviewer counts once, as a reviewer rejection (rules below).
- 37 runs ($145.99 raw) stay unclassified: 23 pre-September runs with no events
  or verdict files (B15) and 14 libwit chapter-review runs with no run dir (B14).
- Run dirs are read live while the DB is frozen: one run that was `executing`
  at snapshot time has since written a passing `verdict.json`, so it counts
  under "passed, never published".
- The live DB had moved by one unpublished run ($4.03) when this report was
  written: live `SELECT sum(cost_usd) … status<>'published'` prints **2114.36**;
  the snapshot prints **2110.33**, the figure used throughout.

## AC6 — Reproduce

Snapshot (read-only; `immutable=1` because `sqlite3 -readonly` fails on this
WAL-mode file with "unable to open database file (14)"):

```bash
S=/Volumes/docker-ssd/Migration/Development/backups/k0-baseline-20261007-104547.db
sqlite3 "file:$S?immutable=1" "SELECT round(sum(cost_usd),2) FROM task_runs WHERE status<>'published'"   # 2110.33
sqlite3 "file:$S?immutable=1" "SELECT count(*), sum(status='published'), sum(status NOT IN ('published','failed','rolled_back')), round(sum(cost_usd),2) FROM task_runs"   # 1342|467|388|3151.17
sqlite3 "file:$S?immutable=1" "SELECT status, coalesce(verdict,'-'), count(*), round(sum(cost_usd),2) FROM task_runs WHERE status<>'published' GROUP BY 1,2 ORDER BY 4 DESC"
sqlite3 "file:$S?immutable=1" "SELECT CASE WHEN ts < '2026-09-12' THEN 'A' WHEN ts < '2026-10-01' THEN 'B' ELSE 'C' END era, provider, count(*), round(sum(cost_usd),2) FROM llm_calls GROUP BY 1,2"
sqlite3 "file:$S?immutable=1" "SELECT coalesce(finish_reason,'NULL'), count(*) FROM run_events WHERE event_type='node_end' GROUP BY 1"
sqlite3 "file:$S?immutable=1" "SELECT coalesce(error_category,'NULL'), count(*) FROM llm_calls WHERE status<>'success' GROUP BY 1"
```

Bucket table, label roll-up and per-run detail (writes `per_run.json` next to
the script; run dirs from `K0_RUNS`, default `.mini-ork/runs`):

```bash
python3 k0.py "$S"                                                     # mini-ork home
K0_RUNS=/Volumes/docker-ssd/ps/sdd-10x-ork/home/runs \
  python3 k0.py /Volumes/docker-ssd/Migration/Development/backups/k0-sdd10x-20261007-110708.db
```

Rule order (first match wins): B1 CRASH / killed / crash-finalized notes → B2
non-terminal status → B3 probe-scorer arm → B4 first failing node ended
`timeout`/`cost_limit` → B5 no implementer + `needs_answers` → B6 no implementer
+ `plan-failure-*` or all planner traces failed → B7–B10 verify-schema
`verdict.json` (`pass`, `files_changed`, `tests_pass`) → B17 reviewer
`unknown` → B18 reviewer `ESCALATE` → B9b/B7b/B7c verifier result files +
reviewer / panel → B12b reviewer rejected → B11 all verifier traces vacuous →
B12 first failing node `verdict_revise`/`verdict_fail` → B13 first failing node
`error` → trace fallback (B9c, B7d, B12c, B7e, B16) → B14 no run dir → B15.

The classifier, verbatim as run:

```python
"""K0 — split unpublished mini-ork spend into mutually exclusive causes.

Read-only: the frozen snapshot (immutable URI) + run dirs. Prints the AC1
table (raw + corrected), per-recipe detail, and evidence candidates.
"""
from __future__ import annotations

import collections
import glob
import json
import os
import sqlite3
import sys
from typing import Any

SNAP = sys.argv[1] if len(sys.argv) > 1 else \
    "/Volumes/docker-ssd/Migration/Development/backups/k0-baseline-20261007-104547.db"
RUNS = os.environ.get("K0_RUNS", "/Volumes/docker-ssd/ps/mini-ork/.mini-ork/runs")
TERMINAL = {"published", "failed", "rolled_back"}

# H1 correction: reprice non-Anthropic calls from tokens at pricing.yaml list
# rates (USD / Mtok: input, output, cache_read). Anthropic calls keep their
# recorded cost (the claude CLI priced Anthropic models correctly).
LIST = {
    "minimax": (0.30, 1.20, 0.06),          # MiniMax-M3
    "glm": (1.40, 4.40, 0.26),              # GLM-5.3
    "deepseek": (1.32, 3.96, 0.044),        # deepseek-v4-pro
    "deepseek_flash": (0.30, 1.20, 0.006),  # deepseek-flash
    "kimi": (0.95, 4.00, 0.30),             # kimi-k2.7-code
    "codex": (2.50, 10.00, 0.0),            # gpt-5 (no cache rate in the table)
}
NO_PUBLISH_RECIPES_PREFIX = ("code-fix__probe_",)  # probe-scorer arms: experiments


def con():
    c = sqlite3.connect(f"file:{SNAP}?immutable=1", uri=True)
    c.row_factory = sqlite3.Row
    return c


def corrected_call(row) -> float:
    # H1: only the claude-CLI-priced non-Anthropic era is wrong — provider
    # 'gateway', ts < 2026-10-01 (46f32302). openai/codex were recomputed per
    # family on 2026-09-12 (F2 backfill); post-fix gateway is already list price.
    if row["provider"] != "gateway" or (row["ts"] or "") >= "2026-10-01":
        return float(row["cost_usd"] or 0)
    rates = LIST.get(row["model_id"])
    if rates is None:
        return float(row["cost_usd"] or 0)
    inp = int(row["input_tokens"] or 0)
    out = int(row["output_tokens"] or 0)
    cached = int(row["cached_input_tokens"] or 0)
    if inp == 0 and out == 0 and cached == 0:
        return float(row["cost_usd"] or 0)  # no tokens recorded: uncorrectable, keep raw
    return (inp * rates[0] + out * rates[1] + cached * rates[2]) / 1e6


def node_type(text) -> str:
    try:
        obj = json.loads(text or "{}")
    except (TypeError, ValueError):
        return "?"
    return (obj.get("node_type") if isinstance(obj, dict) else None) or "?"


def verifier_pass(path):
    """pass flag from a verifier result file: JSON (maybe after log lines) or plain text."""
    try:
        text = open(path, encoding="utf-8", errors="replace").read()
    except OSError:
        return None
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if isinstance(obj, dict) and "pass" in obj:
                return bool(obj["pass"])
    try:
        obj = json.loads(text)
        if isinstance(obj, dict) and "pass" in obj:
            return bool(obj["pass"])
    except ValueError:
        pass
    low = text.lower()
    return False if ("fail" in low or "error" in low) else None


def jload(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001
        return None


def features(c, run):
    rid = run["id"]
    d = os.path.join(RUNS, rid)
    f: dict[str, Any] = {"dir": os.path.isdir(d)}
    prof = jload(os.path.join(d, "run_profile.json")) or {}
    f["profile"] = prof.get("profile_status")
    f["plan_failure"] = sorted(os.path.basename(p) for p in glob.glob(os.path.join(d, "plan-failure-*.raw.txt")))
    f["plan"] = os.path.isfile(os.path.join(d, "plan.json"))
    v = jload(os.path.join(d, "verdict.json"))
    f["verdict"] = v if isinstance(v, dict) else None
    f["implementer"] = any(os.path.exists(os.path.join(d, n)) for n in (
        "impl-implementer.log", "implementer-summary.json", "agent-implementer.transcript.json",
        "agent-implementer.stream.jsonl"))
    rv = jload(os.path.join(d, "review-verdict.json"))
    f["review"] = (rv or {}).get("verdict") if isinstance(rv, dict) else None
    pv = jload(os.path.join(d, "panel-verdict.json"))
    f["panel_pass"] = pv.get("pass") if isinstance(pv, dict) else None
    vr = [verifier_pass(p) for p in glob.glob(os.path.join(d, "verifier_*.json"))
          + glob.glob(os.path.join(d, "verifier-result-*.json"))]
    f["vres"] = [x for x in vr if x is not None]
    xl = os.path.join(d, "execute.log")
    f["exec_review"] = None
    if os.path.isfile(xl):
        for line in open(xl, encoding="utf-8", errors="replace"):
            if "reviewer verdict=" in line:
                f["exec_review"] = line.split("reviewer verdict=", 1)[1].split()[0]
    ev = c.execute("SELECT finish_reason, payload_json FROM run_events WHERE run_id=? AND event_type='node_end' "
                   "ORDER BY created_at", (rid,)).fetchall()
    f["finish"] = collections.Counter(e["finish_reason"] for e in ev)
    first_bad = None
    for e in ev:
        if e["finish_reason"] not in (None, "done"):
            first_bad = (e["finish_reason"], node_type(e["payload_json"]))
            break
    f["first_bad"] = first_bad
    tr = c.execute("SELECT verifier_output, status, reviewer_verdict FROM execution_traces WHERE run_id=?",
                   (rid,)).fetchall()
    stages = collections.defaultdict(list)
    for t in tr:
        stages[node_type(t["verifier_output"])].append(t["status"])
    f["stages"] = dict(stages)
    return f


def bucket(run, f):
    notes = run["notes"] or ""
    st = run["status"]
    if run["verdict"] == "CRASH" or notes.startswith(("killed-by-user", "dispatcher exited rc=", "reaped:")):
        return "B1 crash-finalized / killed (reaped CRASH)", "b"
    if st not in TERMINAL:
        return "B2 never finalized (non-terminal status)", "b"
    if (run["recipe"] or "").startswith(NO_PUBLISH_RECIPES_PREFIX):
        return "B3 no publish expected (probe-scorer experiment arms)", "a"
    if f["first_bad"] and f["first_bad"][0] in ("timeout", "cost_limit"):
        return f"B4 infra: node {f['first_bad'][0]}", "e"
    if not f["implementer"] and (f["profile"] == "needs_answers" or "needs_answers" in notes):
        return "B5 dead before work: profile needs_answers", "d:profile"
    # plan.json is NOT a failure signal: chapter-review / verified-artifact never
    # write one, even when they publish. Require evidence the planner failed.
    pl = f["stages"].get("planner") or []
    if not f["implementer"] and (f["plan_failure"] or (pl and all(x == "failure" for x in pl))):
        return "B6 dead before work: planner failure", "d:plan"
    v = f["verdict"]
    if v and v.get("pass") is True:
        return "B7 verify PASSED, run rejected downstream (candidate false reject)", "c"
    if v and v.get("pass") is False:
        if int(v.get("files_changed") or 0) == 0:
            return "B8 real failure: implementer produced no diff", "d:implementer"
        if v.get("tests_pass") is False:
            return "B9 real failure: verify tests failed", "d:verify-tests"
        return "B10 real failure: verify static/shape check failed (tests ok)", "d:verify-static"
    rev = f["review"] or f["exec_review"]
    if rev == "unknown":
        return "B17 harness: reviewer verdict unparseable ('unknown')", "e"
    if rev == "ESCALATE":
        return "B18 reviewer escalated to a human (no verdict)", "f"
    rejected_by_reviewer = rev not in (None, "pass", "approve", "APPROVE", "approved")
    if f["vres"]:
        if any(x is False for x in f["vres"]):
            return "B9b real failure: a verifier check failed", "d:verify"
        if rejected_by_reviewer:
            return "B7b verify passed, reviewer rejected (candidate false reject)", "c"
        if f["panel_pass"] is False:
            return "B7c verify passed, panel/rubric rejected (candidate false reject)", "c"
    if rejected_by_reviewer:
        return "B12b real failure: reviewer rejected (no verify result)", "d:reviewer"
    vs = f["stages"].get("verifier") or []
    if vs and all(s == "vacuous" for s in vs):
        return "B11 verify vacuous (nothing checked), run failed", "d:verify-vacuous"
    if f["first_bad"] and f["first_bad"][0] in ("verdict_revise", "verdict_fail"):
        return f"B12 real failure: {f['first_bad'][1]} {f['first_bad'][0]}", f"d:{f['first_bad'][1]}"
    if f["first_bad"] and f["first_bad"][0] == "error":
        return f"B13 node error in {f['first_bad'][1]}", "e" if f["first_bad"][1] in ("?",) else f"d:{f['first_bad'][1]}"
    # Trace fallback (runs whose dir was a temp home, e.g. libwit chapter-review).
    st = f["stages"]
    ver, rvw, res = st.get("verifier") or [], st.get("reviewer") or [], st.get("researcher") or []
    if "failure" in ver:
        return "B9c real failure: verifier trace failed (traces only)", "d:verify"
    if "failure" in rvw:
        return ("B7d verify passed, reviewer trace failed (traces only)", "c") if "success" in ver \
            else ("B12c real failure: reviewer trace failed (traces only)", "d:reviewer")
    if "success" in ver and "success" in rvw:
        return "B7e verify + review passed, run still failed (traces only)", "c"
    if "failure" in res:
        return "B16 real failure: researcher/lens node failed", "d:researcher"
    if not f["dir"]:
        return "B14 no run dir (unclassifiable)", "f"
    return "B15 other / unclassified", "f"


def main():
    c = con()
    raw_by_run = collections.defaultdict(float)
    cor_by_run = collections.defaultdict(float)
    for row in c.execute("SELECT run_id, provider, model_id, input_tokens, output_tokens, cached_input_tokens, "
                         "cost_usd, ts FROM llm_calls WHERE run_id IS NOT NULL"):
        raw_by_run[row["run_id"]] += float(row["cost_usd"] or 0)
        cor_by_run[row["run_id"]] += corrected_call(row)
    runs = c.execute("SELECT * FROM task_runs WHERE status <> 'published'").fetchall()
    table = collections.OrderedDict()
    per_run = []
    uncorrectable = [0, 0.0]
    for run in runs:
        f = features(c, run)
        b, label = bucket(run, f)
        raw = float(run["cost_usd"] or 0)
        lr = raw_by_run.get(run["id"], 0.0)
        if lr > 0:
            cor = raw * (cor_by_run[run["id"]] / lr)
        else:
            cor = raw
            if raw > 0:
                uncorrectable[0] += 1
                uncorrectable[1] += raw
        t = table.setdefault(b, {"label": label, "n": 0, "raw": 0.0, "cor": 0.0, "recipes": collections.Counter()})
        t["n"] += 1
        t["raw"] += raw
        t["cor"] += cor
        t["recipes"][run["recipe"] or "(null)"] += raw
        per_run.append({"id": run["id"], "recipe": run["recipe"], "status": run["status"], "bucket": b,
                        "label": label, "raw": round(raw, 4), "cor": round(cor, 4),
                        "first_bad": f["first_bad"], "verdict": f["verdict"], "profile": f["profile"],
                        "plan_failure": f["plan_failure"], "implementer": f["implementer"],
                        "review": f["review"] or f["exec_review"], "panel_pass": f["panel_pass"],
                        "vres": f["vres"]})
    tot_raw = sum(t["raw"] for t in table.values())
    tot_cor = sum(t["cor"] for t in table.values())
    print(f"unpublished runs={len(runs)} raw=${tot_raw:.2f} corrected=${tot_cor:.2f}")
    print(f"uncorrectable (no llm_calls rows, raw>0): runs={uncorrectable[0]} ${uncorrectable[1]:.2f}\n")
    print(f"{'bucket':66} {'lbl':16} {'runs':>5} {'raw $':>9} {'raw%':>6} {'corr $':>9} {'corr%':>6}  top recipes (raw $)")
    for b, t in sorted(table.items(), key=lambda kv: -kv[1]["cor"]):
        top = ", ".join(f"{r} {v:.0f}" for r, v in t["recipes"].most_common(3))
        print(f"{b:66} {t['label']:16} {t['n']:5d} {t['raw']:9.2f} {100*t['raw']/tot_raw:5.1f}% "
              f"{t['cor']:9.2f} {100*t['cor']/tot_cor:5.1f}%  {top}")
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "per_run.json")
    with open(out, "w") as fh:
        json.dump(per_run, fh, indent=1, default=str)
    print(f"\nper-run detail: {out}")


if __name__ == "__main__":
    main()
```
