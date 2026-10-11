# Orphaned-base failed runs — census (2026-10-11)

Classifies the 807 failed runs after the base-drift fix (`bf5a02e9`).
`orphaned` = the run's `pre-implementer-ref` is a real commit in the mini-ork
repo but is NOT an ancestor of HEAD (main was rewritten).

## Whole-corpus split

| class | count | meaning |
|---|---:|---|
| no-base | 580 | no pre-implementer-ref file — recoverable (baseline written fresh) |
| ancestor | 59 | base is an ancestor of HEAD — reuse-only |
| orphaned | 91 | base diverged from HEAD — was un-revivable; fix `bf5a02e9` now refreshes |
| missing | 1 | base object gc'd |
| other-repo | 76 | base not in the mini-ork repo (another target / gc'd) |
| **total failed** | **807** | |

## The 91 orphaned runs, by landed-ness on `origin/main`

| bucket | count | action |
|---|---:|---|
| landed >=0.6 | 43 | superseded — the change is on main |
| not-landed | 20 | mostly first rounds of a series whose later round landed |
| no-diff artifact | 28 | reviews / migrations / child runs (different class) |

### landed (43)

| run | landed | kickoff |
|---|---|---|
| `bgl-u2-202609180512` | 12/12 | u2-10lens-parallel-edges.md |
| `bgl-u3-202609180512` | 12/12 | u3-recursion-schema-shape.md |
| `bgl-u4a-202609180525` | 12/12 | u4a-goal-loop-recipe-skeleton.md |
| `bgl-u4b-202609180542` | 12/12 | u4b-goal-loop-driver-rsi.md |
| `eng-your-code-tab-20261007163940` | 12/12 | eng-your-code-tab.md |
| `ide-board-perf-20261006205933` | 12/12 | ide-board-perf.md |
| `ide-board-perf-r5-20261007003516` | 12/12 | ide-board-perf-r5.md |
| `ide-board-perf-r6-20261007005110` | 12/12 | ide-board-perf-r6.md |
| `ide-kickoff-search-r2-20261007095002` | 12/12 | ide-kickoff-search-r2.md |
| `ide-kickoff-search-r3-20261007103833` | 12/12 | ide-kickoff-search-r3.md |
| `ide-kickoff-search-r4-20261007111217` | 12/12 | ide-kickoff-search-r4.md |
| `ide-kickoff-search-r5-20261007114140` | 12/12 | ide-kickoff-search-r5.md |
| `ide-node-changes-r2-20261007100450` | 12/12 | ide-node-changes-r2.md |
| `ide-node-changes-r3-20261007104328` | 11/12 | ide-node-changes-r3.md |
| `ide-node-stream-20261006224824` | 12/12 | ide-node-stream.md |
| `ide-node-stream-r2-20261006231312` | 11/12 | ide-node-stream-r2.md |
| `ide-node-stream-r3-20261007005337` | 11/12 | ide-node-stream-r3.md |
| `ide-node-stream-r5-20261007042918` | 9/12 | ide-node-stream-r5.md |
| `ide-node-stream-r6-20261007083102` | 10/12 | ide-node-stream-r6.md |
| `ide-pages-p1-20261006202520` | 12/12 | ide-pages-p1-fixes.md |
| `lane-repair-channel-20261007175319` | 11/12 | lane-repair-channel.md |
| `lane-repair-hint-r2-20261007160033` | 12/12 | lane-repair-hint-r2.md |
| `learn-gate-20261007121336` | 9/12 | learn-gate.md |
| `learn-memory-tab-20261007133743` | 12/12 | learn-memory-tab.md |
| `learn-themes-20261007124729` | 12/12 | learn-themes.md |
| `node-commands-20261007114301` | 12/12 | node-commands.md |
| `node-commands-r2-20261007132049` | 12/12 | node-commands-r2.md |
| `node-overview-20261007165632` | 11/12 | node-overview.md |
| `publish-carry-plus-edits-20261008193351` | 12/12 | publish-carry-plus-edits.md |
| `refactor-exec-handlers-1787257914` | 12/12 | refactor-execute-extract-node-handlers.md |
| `retry-hint-20261007103745` | 11/12 | kickoff.md |
| `retry-hint-r2-20261007114437` | 12/12 | retry-hint-r2.md |
| `rsi-build-rsi-i2-verifier-audit-20260929-131940` | 3/5 | rsi-i2-verifier-audit.md |
| `rsi-build-rsi-i2-verifier-audit-20260929-135337` | 12/12 | rsi-i2-verifier-audit-repair.md |
| `rsi-build-rsi-i5-harness-sweep-20260929-142756` | 12/12 | rsi-i5-harness-sweep-repair.md |
| `rsi-build-rsi-i6-abstain-gate-20260929-131930` | 12/12 | rsi-i6-abstain-gate-repair.md |
| `run-1789844133-3878` | 12/12 | goal-diagnosis.md |
| `run-orchestrator-scaffold-20261008124718` | 12/12 | run-orchestrator.md |
| `vt3-equivalence-operator-20261005-193921` | 12/12 | vt3-equivalence-operator.md |
| `vt4-differential-delta-20261005-201153` | 12/12 | vt4-differential-delta.md |
| `vt5-mutation-adequacy-20261005-194054` | 12/12 | vt5-mutation-adequacy.md |
| `vt6-level-vector-20261005-203840` | 12/12 | vt6-level-vector.md |
| `wire-mm-fn-1787234938` | 12/12 | wire-metamorphic-function-surface.md |

### not-landed (20)

| run | landed | kickoff |
|---|---|---|
| `bgl-u1-202609180511` | 6/12 | u1-recipe-register-loader.md |
| `fe-parity-p0-keystone-1787739803` | 0/12 | stage2-p0-keystone.kickoff.md |
| `ide-board-perf-r2-20261006212540` | 0/12 | ide-board-perf-r2.md |
| `ide-board-perf-r3-20261006223927` | 0/12 | ide-board-perf-r3.md |
| `ide-board-perf-r4-20261006230254` | 0/12 | ide-board-perf-r4.md |
| `ide-kickoff-search-20261007091357` | 7/12 | ide-kickoff-search.md |
| `ide-node-changes-20261007091357` | 3/12 | ide-node-changes.md |
| `ide-node-stream-r4-20261007020955` | 6/12 | ide-node-stream-r4.md |
| `learn-overview2-20261007133743` | 7/12 | learn-overview2.md |
| `node-artifacts-20261007165902` | 6/12 | node-artifacts.md |
| `rsi-build-rsi-i1-control-arm-20260929-115150` | 2/12 | rsi-i1-control-arm.md |
| `rsi-build-rsi-i2b-audit-sensitivity-20260929-193126` | 0/12 | rsi-i2b-audit-sensitivity.md |
| `rsi-build-rsi-i3-bsg-va-wiring-20260929-122109` | 0/12 | rsi-i3-bsg-va-wiring.md |
| `rsi-build-rsi-i3-bsg-va-wiring-20260929-131925` | 0/12 | rsi-i3-bsg-va-wiring-repair.md |
| `rsi-build-rsi-i4-collapse-halt-20260929-115155` | 0/12 | rsi-i4-collapse-halt.md |
| `rsi-build-rsi-i4-collapse-halt-20260929-122018` | 3/12 | rsi-i4-collapse-halt-repair.md |
| `run-1791541963-98230` | 0/9 | rsi-i2-verifier-audit-repair.md |
| `run-1791542947-20010` | 0/12 | rsi-i2-verifier-audit-repair.md |
| `run-1791567564-35413` | 0/12 | needs-change-reasons-subullets.md |
| `vt2-hackability-audit-20261005-193952` | 6/12 | vt2-hackability-audit.md |

### no-diff (28)

| run | landed | kickoff |
|---|---|---|
| `child-1790348515-96925` | — | kickoff.md |
| `child-1790350588-96925` | — | kickoff.md |
| `child-1790353035-77381` | — | kickoff.md |
| `child-1790373062-20785` | — | kickoff.md |
| `goalloop-self-20260925-170038` | — | wave-kickoff.md |
| `goalloop-self-20260925-181638` | — | wave-kickoff.md |
| `goalloop-self-20260925-184740` | — | wave-kickoff.md |
| `goalloop-self-20260925-222456` | — | wave-kickoff.md |
| `goalloop-self-20260926-061458` | — | wave-kickoff.md |
| `ide-pages-review-20261006200321` | — | ide-pages-review.md |
| `learn-memory-tab-r2-20261007142114` | — | learn-memory-tab-r2.md |
| `learn-overview2-r2-20261007142114` | — | learn-overview2-r2.md |
| `pr210-review-20260930-101508` | — | review-pr210-scheduler-retry.md |
| `rsi-review-20260929-075339` | — | kickoff.md |
| `rsi-review-20260929-080220` | — | kickoff.md |
| `rsi-review-20260929-084403` | — | kickoff.md |
| `run-1784474339-7704` | — | mini-ork-self-migrate-verify-kickoff.md |
| `run-1784478877-84933` | — | mini-ork-self-migrate-verify-v2-kickoff.md |
| `run-1784482847-61479` | — | mini-ork-self-migrate-verify-v3-kickoff.md |
| `run-1789844094-1929` | — | goal-diagnosis.md |
| `run-1791569054-502` | — | distill-g05-attempt-2.md |
| `run-1791569210-4101` | — | zinsight-transcript-strip-attempt-3.md |
| `run-1791617654-41370` | — | session-task-judgment-2026-10-10.md |
| `run-mig-lane-helpers-1784298506` | — | port-lane-helpers.md |
| `self-improve-iter-1-20260804155023` | — | kickoff.md |
| `self-improve-iter-1-20260804165336` | — | kickoff.md |
| `vt6-leak-k0-20261005220411` | — | strong.md |
| `vt6-leak-k1-20261005215711` | — | strong.md |

