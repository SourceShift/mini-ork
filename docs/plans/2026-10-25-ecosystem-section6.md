# ECOSYSTEM §6 — remove it or make it live (2026-10-25)

`docs/ECOSYSTEM.md` §6 lists what is "wired but not live": code that exists but
does not change a run. The rule for each item: **remove** it, or **make it
live** only if live would improve outcomes. "Keep" is allowed only for a guard
that is correctly refusing, or a live feature that is waiting for traffic.

Evidence comes from six read-only audits against main `c25ffec4` and the live
`state.db` (2026-10-25). Each change ships as one framework-edit run with blind
spec tests (`kickoffs/auto/eco6-k*.md`), except frozen-kernel edits, which are
operator commits.

## Decisions

| §6 item | Decision | Why (evidence) | Ships as |
|---|---|---|---|
| `improve` → `eval` → `promote` | **Remove** the three subcommands, `bin/` shims and `group_evolver` | `eval` hard-codes `utility_delta = 0` and runs every task with `runner_fn=None`; `promote` refuses every `candidate`; `run` never calls them. Every live promotion goes through `apply`. `benchmark_suite.py` and `promotion_gate.py` stay (epics rsi-e2/e5 and six importers use them). | K1 |
| Apply loop | **Keep opt-in**, fix hygiene now | Probe sets hold 2–3 probes and the control solves 100% of them, so a live sweep can only quarantine. It goes live after the probe harvest (rsi-e2). Fixed now: it re-picked one dead source 16 times; candidate rows never left `candidate`. | K7 |
| Learned routing (97 of ~11k routes) | **Fix the screen**; starvation is operational | The lane keys were fine: the served lane is recorded at trace time since 09-12. Learned routes dropped to 0 after 10-19 because recent runs were launched with `MO_ROUTING_POLICY=workflow_default` and framework-edit pins every node. The real defect: the RouteMoA screen read groups-won/rows from `agent_performance_memory` as a success rate and screened out busy, good lanes (3 of 6 live picks flipped). Where learned routing ran (code_fix reviewer) it did better: 0.81 vs 0.45 success, $0.29 vs $0.77 per route. | K8 |
| Collapse history (2 rows) | **Keep** the halt | It is fed per promotion iteration, so it fills when apply runs. It is the only guard against self-training collapse. | — |
| Metric-anchor arm of the verifier audit | **Remove** | It reads `scored`/`held_out`/`agreements` columns `collapse_history` does not have, so it is always `intact=None`. `metric_anchor.py` stays for `mini-ork metric-anchor`. | K7 |
| Item-utility ranking | **Already live** | Fixed by a8ea463b: 325 rows, consumed by the context pack (`item_ranking=simutil-ucb`). | — |
| CoBa revise-value stop | **Remove** | It stops a round only below a 5% pass rate; round 2 of framework_edit passes 46% (19/41), so it can never fire. | K2 |
| Lesson conflicts (0 rows) | **Keep** | A guard on the lesson write; no new lesson has been authored since it merged. | — |
| Abstain / conformal gates | **Remove** the evaluators, keep the LTT bar | The only switch was a recipe's per-node `gates:` list, which nothing reads; 0 of 1,164 verifier results carry a confidence or label. The LTT bar in `abstain_gate.py` feeds the escalation policy. | operator commit `669c181c` |
| Diagnosis ledger | **Already live** (writer) | Fixed by ff28c65d; waiting for its first recordable repair (~2.6/day in October). | — |
| Self-driving escalation | **Keep** (refusing correctly) | 0 rule-detected engine faults in 36 diagnoses; the 2 model engine diagnoses chose `owner` at 0.72 confidence, below the 0.85 bar. Lowering the bar would start paid engine edits on guesses. Prerequisite before it may act alone: freeze `escalation.py`, `diagnose.py`, `diagnoser_llm.py`, `supervisor.py`, `graph_patch.py` (epic rsi-e8). | — |
| 24 fix epics waiting | **Prune**: archive 18, keep 5 | None is a fix epic. 3 are delivered, 4 delivered elsewhere in Python, 11 obsolete or kickoff-less; rsi-e2/e4/e5/e7/e8 are real. R8 (opt-in supervisor dispatch) stays off until rsi-e8 is scoped. | K12 + ops |
| Graph patches | **Already live** | R6 runs every sweep; the one patch since SDE-5a belonged to a run R4 correctly superseded. | — |
| Per-node `gates:` (347 nodes) | **Remove** | The compiler never parses them; wiring them by id would fail all 347 nodes, by type would be a no-op. Real gates run per task class. | K13 |
| `recursion:` keys | `max_iterations`, `budget_cap_total_usd`: **make live**; three others: **remove** | Declared caps (5–8) never applied: `MO_REVISE_ROUNDS=2` always won. `budget_cap_per_iter_usd`, `convergence_check`, `divergence_kill` have no reader. | K2 (live) + K16 (remove) |
| Edge types collapsing to `depends_on` | **Remove** 5 of 8 (aliases for one release) | `supplies_context_to`, `verifies`, `blocks`, `human_decision_gate`, `verifies_user_choice` behave exactly like `depends_on`. | K14 |
| `validate` never checks the schema | **Fix** | 5 recipes fail `workflow.schema.json` today; nothing would enforce the removals above. | K3 |
| OTel / Langfuse export | **Make live** the run export; **remove** the dead in-process buffer and score mapper | `otel_export.export()` already builds the span tree from `state.db`; the setup page shows "exporting" while nothing exports. | K10 |
| healer / cleaner / finalize, `cache.py`, epic auto-merge / PR | **Remove** (keep `content_ids`) | Bash-era ports with no production caller; `MO_AUTO_MERGE` / `MO_OPEN_PR` do nothing but are advertised. | K4 |
| Chat integrations | **Make live**, one-way for now | Both tiers were built and tested; only the entry points were missing. Added with a mandatory channel allowlist (anyone who can message a bot could otherwise start runs); the Discord Gateway is `--once` only (no RESUME); results are not yet posted back. | K15a + K15b |
| GEPA | **Keep opt-in**, fix docs | Installed by `[optimize]`, documented in `PYTHON-SDK.md`; §6's "not installed" was wrong for the repo venv. | docs |
| Cost pause records but does not halt | **Fix** | No dispatch path reads `.cost-pause`; resume clears it but never restarts. | K5 |
| ContextNest outcome atom ids | **Fix** (1aeeec09 was incomplete) | The engine's own CN injection never reaches the run dir the settle reads; 0 `cn-outcomes.json` in 1,422 runs. | K9 |
| `refactor_audit` declared by 3 recipes | **Fix** (rename + uniqueness check) | Classification can never reach the two cmgk recipes. | K3 |
| Web stop / kill / answers without a check | **Fix** | A foreign web page can fire them (no-cors POST, DNS rebinding); guarded with `require_local_caller`. | K6 |
| Publish before the post-run verify | **Fix by marking**, no hard gate | Of the 30 most recent fail/partial post-run verifies on published runs, 1 was a real defect (now refused before publish), 15 were environment noise and 14 were the verify misreading paths or reading stale traces; a hard gate would have blocked 8 good publishes. Step-rules path parsing fixed; a fail after publish now sets `verdict=ESCALATE` plus an inbox item. | K11 |

## Progress (complete 2026-10-25)

| Kickoff | What landed | Commit |
|---|---|---|
| operator | abstain/conformal gate evaluators removed; LTT bar kept | `669c181c` |
| K6 | web stop / kill / answers refuse a foreign Origin | `c0f1b1e6` |
| K4 | healer/cleaner/finalize, `cache.py`, epic auto-merge/PR removed; `content_ids` kept | `e404bd14` |
| K1 | `improve` / `eval` / `promote` removed | `222219e0` |
| K9 | engine-injected ContextNest context persisted for outcome credit | `7950699e` |
| K2 | declared recursion caps + budget honored; CoBa removed | `3de6813e` |
| K7 | apply skips dead sources, records candidate status; metric-anchor arm removed | `79e3c5f3` |
| K8 | RouteMoA screen reads the slice's own outcomes | `13fe4e81` |
| K11 | step-rules path misreads fixed; fail-after-publish marked ESCALATE + inbox | `ec4a5998` |
| K15a | chat simple tier: config, allowlist, `threads serve` | `47de862d` |
| K12 | `epics archive`; scheduler bootstraps `priority` (empty ready-set bug) | `69c1122d` |
| K3 | validate checks the workflow schema + task-class uniqueness; cmgk classes renamed | `a7e1c07f` |
| K15b | chat secure tier: `POST /api/v1/chat/<transport>` webhook routes | `2d1852b2` |
| K10 | OTel/Langfuse export after every run (vault, two tiers); dead buffer removed | `d10a34a2` |
| K13 | per-node `gates:` and node `max_cost_usd` removed | `04b67bd5` |
| K5 | cost pause stops new dispatches; `resume` restarts the run | `ac76a64a` |
| K14 | five decorative edge types → deprecated aliases of `depends_on` | `b10821ea` |
| K16 | three unread `recursion:` keys removed | `e9682838` |

### Review notes

Every framework-edit diff was read before merge; five needed hand fixes:

- **K10** — the repair round added a shim to production `otel_export.py` that
  detected the blind spec suite and patched pytest's `MonkeyPatch.setattr` so
  three defective spec cases would pass. Removed; no other merge contains
  test-detection code (grepped all 18).
- **K12** — an import-time patch of `scheduler._RETRY_COLUMNS` from `epics.py`
  replaced by a one-line fix in `scheduler.py` (a real empty-ready-set bug).
- **K3** — one ambiguous blind spec case ("when present" read as file
  presence); an out-of-scope `MINI_ORK_HOME` pin reverted.
- **K13** — the IDE node/run pages still rendered or asserted the removed
  gates; a missing key silently dropped the whole node section.
- **K5, K14, K16** — correct diffs failed on kickoff problems (a test command
  that always timed out; a needed test file outside scope) and were merged by
  hand after a bounded gate.

## Follow-ups that are not §6

- An auditor's `grep` printed the API keys in `config/secrets.local.sh` into a
  subagent transcript on 2026-10-25. Rotate them.
- `MINI_ORK_ON_EVENT` (`observability/emit_hook.py`) is documented but nothing
  calls `mo_emit_hook`.
- The supervisor wrapper's `git fetch` fails intermittently (permission errors,
  timeouts), so "has the fix landed" checks sometimes read a stale `origin/main`.
