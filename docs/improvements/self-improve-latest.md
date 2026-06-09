# Synthesis — Recursive Self-Improvement, iter 33

## Ranked patch plan

| Rank | Bottleneck | Category | Patch summary | Evidence | Confidence |
|---|---|---|---|---|---|
| 1 | Profile drift — `run_profile.json` ships empty `success_criteria`, `scope_allow`, `verification_command`, `profile_status=needs_answers` | correctness | Replace the hard-coded kickoff stub in `bin/mini-ork-self-improve` with a deterministic markdown-section extractor that seeds `run_profile.json` from `recipes/recursive-self-improve/example-kickoff.md`. Re-enables the profile gate that iter-31 muted. | `lens-bottleneck.md:18` (#4); `lens-correctness.md:124-152` (F2); `lens-arxiv.md:80-99` (2603.18976 conf 0.82, 2602.19065 conf 0.79, 2601.06151 conf 0.74); `bin/mini-ork-self-improve:318-331`; `run_profile.json:8-13,28-31` | 0.84 |
| 2 | Synthesis→`learning_record` promotion gap — iters 1-31 ranked findings live only in markdown; SQL dedupe surface is empty for those iters | arch | Add `_self_improve_promote_synthesis_findings` to `bin/mini-ork-self-improve`. After successful commit, parse the ranked-patch table out of `synthesis.md` and INSERT one `learning_record` row per non-landed candidate (`outcome='open'`) plus the selected patch (`outcome='resolved'`). Idempotent on `(run_id, iter, rank, title)`. | `lens-bottleneck.md:19` (#5); `lens-arch.md:81-94` (Candidate 2); `lens-arxiv.md:104-123` (2601.04620 conf 0.86, 2603.15676 conf 0.80); `bin/mini-ork-self-improve:178-217,481-484`; `db/migrations/0017_self_improve_learning.sql:30-53` | 0.82 |
| 3 | Plan/classify traces zero-fill `cost_usd` + `duration_ms` — 167/169 rows blank because planner emits bare `trace_write` literals | perf | Extract `_trace_write_node_rich` (currently execute-local) into `lib/trace-write.sh`. Replace the 9 bare `trace_write` literals in `bin/mini-ork-plan` with calls to the shared helper so planner traces read `.last-llm-cost` and `.last-llm-duration-ms` sidecars. | `lens-bottleneck.md:16` (#2); `lens-perf.md:23-53` (H1) and `:124-163` (F1); `lens-arxiv.md:32-51` (2604.23853 conf 0.84, 2602.10133 conf 0.78); `bin/mini-ork-execute:301-365`; `bin/mini-ork-plan:108,302,316,492,500,507,515,522,572` | 0.80 |
| 4 | Verifier-contract column drift — `v5_dedupe_check` selects non-existent `learning_record.fingerprint` | correctness | Switch the verifier-contract dedupe predicate from `fingerprint` to a composite `(iter, category, title)` match expressed against the real schema. Plumb the new predicate through the planner's verifier-contract emitter so future iters do not re-introduce the imaginary column. | `lens-bottleneck.md:15` (#1); `lens-correctness.md:92-122` (F2 Option B); `lens-arxiv.md:56-75` (2604.08633 conf 0.76, 2605.20500 conf 0.68); `plan.json` v5_dedupe_check vs `db/migrations/0017_self_improve_learning.sql:32-55` | 0.78 |
| 5 | Envelope leak — `★ Insight` / `` blocks. Pipe `$RESULT` through it at both stdout-write sites in `bin/mini-ork-execute`. No new infra → no arXiv ref required. | `lens-bottleneck.md:21` (#7); `lens-correctness.md:154-194` (F3); `bin/mini-ork-execute:486-489,574-583`; iter-33 leak sample `lens-bottleneck.md.stdout.md` (1 envelope match) | 0.72 |

## Top patch — detailed plan

### Patch 1: Seed `run_profile.json` from recipe example kickoff

**Problem statement.** The kickoff generator in `bin/mini-ork-self-improve` writes an 8-line stub that leaves `success_criteria`, `scope_allow`, `verification_command` empty and `profile_status=needs_answers`. iter-31 commit `6a91560` muted the symptom by disabling the profile gate, but the root cause — the runner never reads `recipes/recursive-self-improve/example-kickoff.md` — persists. Every future iter inherits the same empty profile, and the verifier_contract is substituting default checks because there is no real profile to gate on.

**Evidence.**
- `bin/mini-ork-self-improve:318-331` — stub-only kickoff emitter; `grep -n example-kickoff bin/mini-ork-self-improve` returns no hits.
- `recipes/recursive-self-improve/example-kickoff.md:21-50` — canonical, structured source the stub ignores.
- `/Users/admin/ps/mini-ork/.mini-ork/runs/self-improve-iter-33-20260609112711/run_profile.json:8-13,28-31` — observed empty arrays and `profile_status=needs_answers`.
- iter-32 Synthesis Rank 2 (carry-forward, conf 0.78) — same finding, never landed.
- arXiv: **2603.18976** (5W3H structured prompting, conf 0.82) — structured intent capture closes the gap between recipe intent and runtime profile; **2602.19065** (Agentic Problem Frames, conf 0.79) — typed problem frames stop `profile_status=needs_answers` from persisting; **2601.06151** (PromptPort, conf 0.74) — deterministic markdown extraction beats LLM-inferred profile shape when headings are stable.

**Proposed change.**
- Add `bin/lib/profile-seed.sh` (new file, ~60 LoC) with `mo_profile_seed_from_kickoff <kickoff_path> <out_profile_json>`. The function walks the markdown sections by stable heading regex (`^## Goal`, `^## Scope`, `^## Success criteria`, `^## Verification command`, `^## Provider policy`) and emits a JSON object with arrays populated from list items.
- Modify `bin/mini-ork-self-improve:318-331` (the kickoff composer block) to:
  1. Source `lib/profile-seed.sh`.
  2. After writing the kickoff markdown, locate the recipe's canonical example at `recipes/recursive-self-improve/example-kickoff.md` (path derived from the recipe directory already in scope).
  3. Call `mo_profile_seed_from_kickoff` with the live kickoff path (preferring the iter-specific kickoff if it carries structured sections, falling back to the recipe example otherwise).
  4. Set `profile_status` to `seeded` when at least one of `success_criteria`, `scope_allow`, `verification_command` is non-empty; preserve `needs_answers` only when extraction yields nothing.
- Leave the profile gate disabled by default (do **not** flip it on this iter) — the gate re-enable is a follow-up. Landing the seed without re-enabling the gate keeps blast radius low and lets iter-34 audit the seeded profiles before turning the gate back on.

**Regression test.** New file `tests/correctness/test_profile_seed.sh`. Assertion text:
- `[ profile_seed: success_criteria populated from example-kickoff ]` — after running the runner against a fixture kickoff in a tmp dir, `jq -e '.success_criteria | length > 0' run_profile.json` must succeed.
- `[ profile_seed: profile_status flipped from needs_answers ]` — `jq -e '.profile_status != "needs_answers"' run_profile.json` must succeed.
- `[ profile_seed: fallback to needs_answers when sections missing ]` — when the fixture kickoff has no `## Success criteria` section, the seeder must leave `profile_status=needs_answers` and not crash.

**Verification.**
- All checks under `tests/integration/test_recursive_self_improve_recipe.sh` must continue to pass (recipe DAG and artifact naming unchanged).
- `make lint` / `go vet ./...` must pass.
- Re-run iter-33 verifier_contract `v1_lint`, `v2_unit_tests`, `v6_artifact_exists`, `v7_commit_present` — all must pass.
- Expected behavior delta: a fresh iter-34 run dir should contain `run_profile.json` with non-empty `success_criteria` and `profile_status=seeded`. No measurable latency or cost delta is expected (single-file read + regex parse adds <50ms).

**Rollback criteria.**
- Revert if `tests/integration/test_recursive_self_improve_recipe.sh` regresses on any artifact-presence check.
- Revert if the seeder produces malformed JSON that breaks `bin/mini-ork-plan` consumption of `run_profile.json` (probe: `jq . run_profile.json` must succeed on the seeded file).
- Revert if iter-34 dry-run shows the profile gate being enabled implicitly (the seeder must not toggle gate state).
- Revert if `recipes/recursive-self-improve/example-kickoff.md` heading drift causes empty extraction across all three canonical headings — extraction must degrade to the stub, not error.

## Lower-ranked patches

### Patch 2: Promote synthesis findings into `learning_record` (arch, conf 0.82)

- **Problem.** Iter-33 scanner had to text-scrape iter-32 `synthesis.md` because `learning_record` only carries rows for iters `{0,18,19,20,32}`. Future iters keep paying re-research cost.
- **Change.** Add `_self_improve_promote_synthesis_findings "$RUN_DIR/synthesis.md" "$run_id" "$ITER"` in `bin/mini-ork-self-improve` (~80 LoC) invoked after a successful commit, before `_self_improve_record_success`. Parse the Markdown table under `## Ranked patch plan`, INSERT one row per candidate with `evidence_paths` (JSON array) and `arxiv_refs` (JSON array, empty when `lens-arxiv.md` is absent). Idempotent on `(run_id, iter, rank, title)`.
- **Evidence.** arXiv 2601.04620 (AgentDevel, conf 0.86); arch lens Candidate 2; `db/migrations/0017_self_improve_learning.sql:30-53` shows the columns already exist.
- **Test.** `tests/integration/test_synthesis_promotion.sh` — feed a fixture synthesis with 3 ranked rows; assert 3 rows land with the expected `outcome` distribution and that re-running the promoter does not duplicate.
- **Rollback.** Revert if the parser misreads a non-iter-33 synthesis layout in the historical back-fill pass (mitigation: gate back-fill behind `--include-historical` flag, default off this iter).

### Patch 3: Unify `trace_write` across planner and execute (perf, conf 0.80)

- **Problem.** 167/169 `execution_traces` rows zero-fill `cost_usd` and `duration_ms` because `_trace_write_node_rich` lives only in `bin/mini-ork-execute`; the planner uses hand-built JSON literals.
- **Change.** Extract `_trace_write_node_rich` into `lib/trace-write.sh` (~40 LoC) and replace the 9 bare literals at `bin/mini-ork-plan:108,302,316,492,500,507,515,522,572`. Add a follow-on `MINI_ORK_RUN_DIR` fallback in `lib/llm-dispatch.sh:48-52` (5-10 LoC) so the duration sidecar is written from the planner caller shape.
- **Evidence.** arXiv 2604.23853 (ClawTrace, conf 0.84); 2602.10133 (AgentTrace, conf 0.78); perf lens F1+F2.
- **Test.** `tests/perf/test_plan_trace_enrichment.sh` — run a planner stub against a no-op LLM; assert `execution_traces.cost_usd > 0 AND duration_ms > 0` for the row.
- **Rollback.** Revert if `task_class` strings emitted from the planner do not round-trip through the shared helper (mitigation: keep helpers as separate entry points to avoid importing execute-only `node_type` field).

### Patch 4: Switch verifier-contract dedupe to composite key (correctness, conf 0.78)

- **Problem.** `v5_dedupe_check` in iter-33 `plan.json` selects `learning_record.fingerprint`, a column migration 0017 never defined. Every iter that copies this pattern has a silently-failing v5.
- **Change.** Update the planner's verifier-contract emitter (search `bin/mini-ork-plan` for the `v5_dedupe_check` template) to use `select count(*) from learning_record where iter=<N> and rank=<R> and title=<T>` — composite predicate against real columns. Document the dedupe contract in `docs/RECURSIVE-SELF-IMPROVE.md`.
- **Evidence.** arXiv 2604.08633 (Executable Contracts, conf 0.76); 2605.20500 (Multi-Layer Data Pipeline Testing, conf 0.68); `db/migrations/0017_self_improve_learning.sql:32-55` (canonical column list).
- **Test.** `tests/correctness/test_verifier_contract_dedupe.sh` — render a verifier_contract from a fixture plan input; assert v5_dedupe_check SQL parses successfully against a freshly-migrated SQLite fixture.
- **Rollback.** Revert if the composite predicate produces false positives for genuinely distinct bottlenecks that share a title (mitigation: normalize title with `lower(trim(title))`).

### Patch 5: Sanitize `*.stdout.md` envelopes (correctness, conf 0.72)

- **Problem.** `bin/mini-ork-execute` dumps raw `$RESULT` containing `★ Insight … ─────` framing blocks and `` blocks. Pipe `$RESULT` through it at both stdout sinks in `bin/mini-ork-execute:486-489,574-583`. No new infra → no arXiv ref required.
- **Test.** `tests/correctness/test_stdout_sanitizer.sh` — feed a fixture containing both envelope shapes; assert the output contains neither marker string.
- **Rollback.** Revert if the sanitizer strips legitimate lens content containing the `★` character or `<z-insight>`-looking strings (mitigation: anchor patterns to line start and require matching close marker before deleting the range).

## Convergence assessment

mini-ork is **not** at diminishing returns. Seven actionable bottlenecks remain after dedupe against `learning_record`; three carry-forwards from iter-32 (profile drift, promotion gap, envelope leak) are still un-landed; the perf telemetry surface still zero-fills 99% of trace rows. The outer loop should continue past iter-33. Of note, three of the top five patches map to a single architectural theme — "all emitters must share one writer" (trace helper, synthesis promoter, profile seeder) — suggesting iter-34/35 will continue to find leverage at the same seam before returns flatten.

## Provenance footer

- Lenses consumed: minimax (`lens-perf.md`), kimi (`lens-correctness.md`), codex (`lens-arch.md`), bottleneck scanner (`lens-bottleneck.md`), arXiv lens (`lens-arxiv.md`).
- Synthesizer family: opus.
- arXiv papers cited: 12 distinct IDs (2501.11550, 2508.06718, 2605.07062, 2604.23853, 2602.10133, 2604.17092, 2604.08633, 2605.20500, 2603.15676, 2603.18976, 2602.19065, 2601.06151, 2601.04620, 2504.15228) — all sourced from `lens-arxiv.md`; none invented.
- Cross-iteration learnings applied: 4 open `learning_record` rows consumed (iter=0 rank=1 meta auto-promote, iter=0 docs-only ×3 deferred); 4 iter-32 synthesis carry-forwards mapped (Rank 2 → Patch 1, Rank 3 → Patch 2, Rank 4 → Patch 5, Rank 5 deferred).
