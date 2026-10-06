# Synthesis — mini-ork IDE pages audit (`board` / `board page` / `mini_ork/ide_pages/`)

Run: `ide-pages-review-20261006200321` · Recipe: `refactor-audit` · Target: worktree `wt/ide-pages` @ `3b291826` · Panel: 5 anonymous responses (A–E), 74 findings total after dedup.
Method: read all five responses in full; cross-referenced by file:line; ranked by ROI = severity × leverage ÷ effort. ★ = finding surfaced independently by 2+ responses (consensus).

Severity key: **P1** = broken or wrong in the happy path today (safety, correctness, measured cost bleed). **P2** = substrate change the P1s patch over. **P3** = tracked, not load-bearing now.
Leverage key: **HIGH** = fixes a *class* of findings or >50% cost/perf. **MED** = one subsystem, material. **LOW** = point fix.

---

## 1 · Severity × leverage matrix

| | HIGH leverage | MED leverage | LOW leverage |
|---|---|---|---|
| **P1** | `A-1…A-7 ★B-13` action verbs reject `--home` (7 sites, verified exit 2) · `A-26` no test executes actions as the IDE does (why A-1…7 shipped green) · `B-1` oversight "Approve" = prose into a thread, gate never resolves · `C-1` no prompt caching, 5-lens panel re-bills shared prefix every call · `C-8` `cap_block` at only 2 sites — the 14.77M-token / $2.89 outlier class | `A-8`+`A-9 ★B-2` destructive one-click verbs without `confirm` (merge→main, automation run) · `A-11 ★E-10 ★B-15` `_live_node` N+1 × unbounded event scan + runs/run state disagreement · `A-15 ★E-2 ★B-8` `_attribute_calls` O(N·M) + silently dropped unattributed cost · `A-12` 3 serial git subprocesses per workspace per 5-s poll · `B-3`+`B-4`+`B-10` kickoff body unread/error-swallowed on the page users actually poll · `C-2` review fallback tail leads with the most expensive model · `C-3` daily budget enforced on one wrapper path only — in-process learning/reflection unbounded | `A-13` header poll can stall 8 s (ambient `CN_TIMEOUT_SEC` unbounded) · `A-20` cold-Docker 0.5 s timeout reports wrong state · `B-18` fresh home renders "$0 spent" as fact · `A-22 ★B-7` "today" is a rolling 24 h · `A-23` `bars` clamps 0→1% — invented sliver · `B-12` "Open" on delivered run opens the live file, not the run's |
| **P2** | `D-R2 ★E-1 ★E-13` PageContext / shared `_io` — 9 of 13 modules touch db/yaml/fs directly, 3 different yaml loaders · `D-R4`+`D-R5` `boardd` daemon + `data_version` invalidation (root fix for A-12/A-14 class) · `D-R7` run-close `run_card` projection table (root fix for A-11/A-14/B-15) · `D-R14 ★B-7 ★B-18` single-metric-single-source (4 cost surfaces today) · `C-arch1`+`C-2` pricing-table-driven router · `C-3`+`C-12` cost gate at the `dispatch_model` chokepoint + retry storm cap | `D-R1` spec→JSON Schema + test-time validation (kills B-17/B-20/B-11 drift class) · `D-R12` action allowlist at the CLI boundary · `C-5` batch reflection (N calls → N/6) · `C-6`+`C-11` wire exact-hash cache + short-TTL prompt dedup into the run path · `C-4` per-lane attempt timeout (today: 4×1500 s serial worst case) · `B-5` `id`/`run_id` canonical accessor · `B-6` pin lane map to run snapshot · `A-16`+`A-18` learn-page fs-probe storm · `A-14 ★D-R7` uncapped `rglob` walk | `B-9` `.dispatch-marker` written, never read — stalled nodes invisible · `B-16` "Open web UI" on 2 of 13 pages · `C-9` 4-chars/token estimate off ~2× · `C-10` stream-json opt-in only · `C-13` per-worker preflight probe · `A-25` credential-name inventory in page JSON · `D-R11` `spec` version field |
| **P3** | — | `D-R10` entry-point page registry · `C-arch2` semantic (similarity) cache | `D-R8` TTL/archive ladder + `mini-ork archive` · `D-R15`+`D-R16` `_meta.sections_ms` + boardd OTel spans · `E-5` tail cache · `E-9` `empty_row` helper · `E-14` `_coerce_row` · `E-15` telemetry index · `D-R9`/`D-R13` explicit "don't": one DB per home forever; no signing yet |

Consensus ledger (★): `--home` action contract A×B×D · `_live_node` A×B×E · `_attribute_calls` A×B×E · cost-surface drift A×B×D · unconfirmed destructive verbs A×B×D · ro-connection trap A-10×B-19 · serve-probe duplication B-14×E-6 · shared-IO dedup B×D×E · verify-page event scans A-19×E-8 · run-dir walk A-14×D-R7 · board poll cost A-12×D-R4.

---

## 2 · Top 5 immediate wins (P1) — 9–12 eng-days total, one engineer, two weeks

| # | ID | Title | Source | One-line fix | Effort |
|---|---|---|---|---|---|
| 1 | `A-1…A-7 ★B-13` + `A-26` | **Action verbs reject `--home` — 7 of 21 action sites broken as invoked** | A, B | Parse-and-strip `--home` in one wrapper before dispatch (or add `--home` SUPPRESS to the `nodes`, `sandbox_gc`, `usage_report`, `bugs`, `traceotter` parsers: `mini_ork/cli/nodes.py:52`, `sandbox_gc.py:38`, `observability/usage_report.py:380`, `cli/bugs.py:166`, `cli/traceotter.py:82`), then land the parametrized test: every `cli(` action in built pages spawned with `--home <tmp>` asserts exit ≤ 2, no usage error | 3 d |
| 2 | `B-1` (+`B-2`, `A-8`, `A-9`) | **Oversight gate "Approve" is a no-op — prose typed into a thread** | B, A | Replace every destructive `S.thread(...)` with `S.cli(...)` + `confirm`: `verify.py:409-414` → `S.cli("oversight","--resolve",id,"--status","approved",confirm=…)`; `orch.py:224,232,297` → `S.cli` or confirmed; add `confirm=` to `run.py:374` merge and `autos.py:103` run-now | 1 d |
| 3 | `C-1` (+`C-8`) | **No prompt caching — the panel re-bills its shared prefix per lens** | C | Emit `cache_control` on the stable prefix in `_claude_spec` (`dispatch/providers.py:617`) / resume via captured `claude_session_id` (`providers.py:345`); apply `cap_block` at the one prompt-assembly chokepoint instead of 2 sites (`context_assembler.py:94` callers) | 3 d |
| 4 | `A-10 ★B-19` (+`B-18`, `A-23`, `B-11`) | **Invented numbers on trusted surfaces** | A, B | Plain rw connection in `setup.py:36` (SELECT never writes; `cost_ledger.py:28` documents the trap) and drop `orch.py:35-47` `mode=ro` for `db_for`; `spent_last_24h(None)` raises → header renders "—"; `spec.py:203` clamp upper bound only, render true 0 as empty | 2 d |
| 5 | `A-11 ★E-10 ★B-15` (+`A-15 ★E-2`, `A-12`) | **Hot-path perf trio vs the 5-s poll (measured 3.5–7.6 s)** | A, E, B | `_live_nodes_batch` — one `IN (…)` query for all active runs (`runs.py:95-124`, E-10's patch); index nodes by `role_lane` in `_attribute_calls` (`run.py:191-214`, E-2's patch); cache `workspaces.status` per branch N s or batch `git for-each-ref` (`board_cmd.py:156`) | 3 d |

Order matters: #1 before #2 — `S.cli` conversions are worthless while the verbs still exit 2 on `--home`. #3 is independent; run it parallel to the IDE-track work.

Honest caveat: E-10 fixes `_live_node`'s *cost*; B-15's *semantic* split (runs page heartbeat vs run page `task_state` stale window, `_STALE_SECONDS = 90`) needs the shared live-state accessor threaded through `ctx` — fold into win #5, +0.5 d if the team wants one state, not just one query.

---

## 3 · v0.x+1 architectural shifts (P2), bundled by theme

### 3a. Data layer — ~3–4 eng-wks
One shared read substrate; one number per metric.
- `D-R2` PageContext: pure page bodies over a home snapshot (`★E-1`, `E-13` `_io.py` is the down payment; 3 yaml loaders → 1, `verify.py:298` CSafeLoader vs `lanes.py:59` safe_load vs `orch.py:241` bare)
- `D-R7` run-close `run_card` projection table — pages stop `rglob`-walking `runs/` (`board_cmd.py:261`); fixes the A-11/A-14/B-15 class at the root
- `D-R14` single-metric-single-source — one query module feeding board JSON, pages, and web (`runs.py:27` raw SELECT vs `board_cmd._runs` fleet_rows today); kills B-7's four-cost-surfaces drift
- `B-5` canonical `run.row(home, id)` accessor; `B-6` pin `_lane_map` to the run snapshot only (`run.py:109` drops home/engine-root fallbacks)
- **Prerequisite P1s:** #4 (honest empty states) and #5 — don't build a cache over numbers that lie.
- **Risk if deferred:** every new page re-decides invalidation and re-derives each metric; drift compounds multiplicatively with page count. This is already the dominant defect factory (B's whole response).

### 3b. Runtime — ~3–4 eng-wks
- `D-R4` `boardd` daemon per home: thin CLI clients, HTTP GET + `ETag`/304 for the IDE poll; absent socket → today's in-process fallback. Surface (commands, JSON) never changes.
- `D-R5` invalidation on `state.db` as hub: `PRAGMA data_version` + mtime sweep of open run dirs — every existing writer already touches state.db.
- `D-R6` actions stay subprocesses forever — the page-bug/orchestrator-bug firewall. Non-negotiable.
- **Prerequisite P1s:** #5 (or the daemon just caches the slow paths' garbage faster).
- **Risk if deferred:** process-per-poll already exceeds the poll period *today* (3.5–7.6 s vs 5 s, 1,300-run home). This is not a 10× problem; 10× makes it absurd (D §2).

### 3c. LLM dispatch — ~3–4 eng-wks
- `C-arch1` router scored by `pricing[provider][model] × measured error-rate per task_class` (pieces exist: `trace_governed`, `learning_governed`, `pricing_strategy.lookup`) — generalizes C-2's hardcoded `"[expensive],[less],sonnet"` tails
- `C-3`+`C-12` budget gate moved into `dispatch_model` (`providers.py:1191`) so learning/reflection call sites (`gradient_extractor.py:496`, `pattern_induction.py:451`, `rubric_prescreen.py:580`) can't bypass; hard ceiling on retry amplification; retries resume the captured session instead of cold re-billing
- `C-5` batch gradient extraction (copy `propose_lessons`' 6-trace batch + `ThreadPoolExecutor`, `pattern_induction.py:422`) — reflection cost flat-per-sweep, not linear-per-trace
- `C-6`+`C-11` wire `cache.lookup` (`cache.py:107`) into lens dispatch keyed `(task_class, lens, prompt-hash)` + short-TTL prompt memo at `core.dispatch` — byte-identical re-runs become free, including the rollback loop
- **Prerequisite P1s:** #3 (cache the prefix before caching anything else; `cache_control` interplay decides the memo's worth).
- **Risk if deferred:** the unbounded case (C-3) is the one that 10× a monthly bill; a single hung-lane day already burns 4×1500 s serial per node (C-4).

### 3d. Contract & observability — ~1.5–2 eng-wks
- `D-R1` generate JSON Schema from `spec.py`, validate in tests + `--strict` dev flag, never hot path — makes B-17 (board vs page shape drift), B-20 (undeclared `label`), B-11 (`ok:True` empties) test failures instead of IDE rendering bugs
- `D-R12` action allowlist at the CLI boundary from the existing subcommand registry; destructive verbs un-allowlistable without `confirm`
- `D-R15` `_meta.sections_ms` on every build (`guarded` already wraps each section) — the 3.5–7.6 s number becomes a permanent per-section regression signal in every real home
- **Prerequisite P1s:** none strict; pairs with #1's test.
- **Risk if deferred:** at 30–50 pages from multiple authors, an unversioned, unvalidated contract turns every renderer change into a breaking change (D §4).

---

## 4 · Long-horizon (P3 + advisory)

- `D-R8` archive ladder (hot <30d → warm zstd → cold manifest-only, `mini-ork archive restore`) — the volume fills before queries slow; ship the tool early, enforce later.
- `D-R10` entry-point page registry (`mini_ork.ide_pages` group, built-in dict as seed) — only when a second page author exists.
- `C-arch2` semantic cache (TF-IDF/embedding similarity gate over `cache.py`) — depends on 3c's exact-hash wiring proving value first.
- `D-R16` boardd as a traced OTel client — after R4 exists.
- `C-9` real tokenizer for budget checks; `C-10` stream-json default for stall detection; `C-13` ship preflight probe results in run env.
- `E-5`/`E-9`/`E-14`/`E-15` micro-refactors — fold opportunistically into 3a; never as standalone PRs.
- `B-16` shared header action bar ("Open web UI" everywhere) — UX polish after the verbs it fires actually work (#1).
- `A-25` shrink credential-name inventory to lane-declared names compared downstream.
- Explicit don'ts, adopted as policy: `D-R9` never consolidate DBs across homes (`--home` = tenancy + blast radius); `D-R13` no signing/sandboxing until a marketplace exists.

---

## 5 · Hardest open question

**D §7: cache invalidation without an owning writer process.** Writers into a home are arbitrary CLI invocations, scheduler ticks, in-flight nodes, the IDE itself, and — at higher autonomy rungs — self-improvement loops applying their own outputs. Options: **(a)** state.db as invalidation hub (write-set contract: every writer touches the DB in one transaction with its side effects), **(b)** filesystem watching per home, **(c)** accepted staleness with "as of" timestamps. D declines to pick, correctly noting (a)'s invariant may be unenforceable for "any process that writes into a home," in which case (a) silently degrades into (c) with extra steps.

**My assessment: the proposed mitigations are sufficient only in composition, and (b) is disqualified for now.** Three reasons:

1. **(b) is dead on this hardware.** The home lives on a network volume; FSEvents/kqueue semantics there are coalescing-lies-at-best. D already half-concedes this. Don't spend a week proving it.
2. **(a) is enforceable exactly where it matters, not everywhere — and that's fine.** The unenforceable writers are user-land recipe artifacts; the writers that drive board freshness (scheduler, node lifecycle, publisher, gates) all pass through framework chokepoints that `D-R12`-style boundary checks can verify. Gate the invariant at the chokepoints, accept that a rogue user-land artifact writer produces staleness, and make that staleness *visible* rather than impossible.
3. **The missing mitigation is the cheapest one: honest staleness.** Ship "as of `<generated_at>`" per page + a visible stale banner when `data_version` hasn't moved but run-dir mtimes have. That converts (a)'s failure mode from *silent wrong data* into *labeled old data* — which the kickoff's rule 1 (no invented numbers) already demands, and which B-11's `ok:True`-on-empty finding shows is currently missing.

**More research needed: yes, one experiment.** Instrument a real home for 7 days: count run-dir mutations that do not coincide with a state.db write. If that population is ~zero, adopt (a) as a framework invariant with the chokepoint checks; if it's material, (a) is theater and the answer is (a)+(c) hybrid with staleness surfacing as the primary control. Until that measurement exists, do not commit R5's write-set contract to docs as an invariant — write it as a strong default.

---

## 6 · Dogfood reflection

**Was this audit reproducible via the framework? Yes — this run is the proof.** The `refactor-audit` recipe executed its full DAG (planner → 5 lenses → anonymize → this synthesizer → verifier → publisher); the panel reports arrived anonymized with sources redacted; the input-manifest scope guard held (this node saw only `panel-responses.md` — the artifact-graph visibility limit working as designed). Anonymization held so well that no response's model family is inferable from content: A reads tactical-grep, B seam-tracing, C economics, D substrate, E patch-shaped — five *methods*, not five model fingerprints. That is the intended diversity signal.

**Did the audit pay the taxes it identified? Yes, three of them, verifiably:**

1. **C-1 applied to this very run.** Planner + 5 lenses + synthesizer each re-billed the shared prefix (framework system prompt + ContextPack) at full input price. The audit's own invoice demonstrates its top dispatch finding.
2. **C-6/C-11 applied.** A byte-identical re-run of this kickoff would re-dispatch every lens from scratch — `cache.lookup` exists but is not consulted by the node executor. Nothing is reusable except what the lenses themselves remembered.
3. **The planner call was wasted.** The plan block states the planner LLM emitted invalid JSON and mini-ork fell back to the deterministic recipe plan. The fallback is the framework working; the wasted planner spend is C-class economics live on the audit's own DAG.

**Was any lens blocked by something the audit identified? No — but one was *shaped* by it.** No lens hit the `--home` exit-2 wall (A-1…7) or a thread-as-verb no-op (B-1) during the run; the anonymize transform and manifest plumbing carried the panel through cleanly. B-9's emit-without-consume (`.dispatch-marker-*` written, never read) was observable in this run's own directory — the panel found the class, and this run instantiated it.

**Meta-loop verdict:** the framework can audit itself, and the audit's findings are priced — literally, in this run's ledger. The next re-run is the regression test: if wins #1/#3 ship first, the same kickoff should cost ~40% of this run and that difference is the verification.

---

## 7 · How to re-run

```bash
cd /Volumes/docker-ssd/ps/mini-ork-worktrees/ide-pages
mini-ork kickoff kickoffs/auto/ide-pages-review.md
```

Recipe `refactor-audit` (resolved from the kickoff's task class; workflow at `recipes/refactor-audit/workflow.yaml`): planner → 5 lenses in parallel → `anonymize_panel` → synthesizer → `lens_completeness` verifier → publisher. Synthesis lands at `<run-dir>/synthesis.md`; publisher copies per the artifact contract to `docs/refactor/synthesis-refactor-audit.md`.

**P1 that blocks self-dispatch: none.** The recipe dispatches fine today. But two named P1s make the re-run strictly worse than it should be, and both are in win #3:

- **`C-6`** — the stage cache is not wired into the lens dispatch path, so a re-run re-pays 100% of the panel even for a byte-identical kickoff. Ship C-6 (or accept the cost knowingly).
- **`C-3`** — the daily budget is enforced only on the `llm_dispatch` wrapper path; any reflection/eval follow-on this kickoff triggers (`gradient_extractor.py:496` et al.) runs uncapped. If the re-run is left unattended overnight, C-3 is the finding that matters.
