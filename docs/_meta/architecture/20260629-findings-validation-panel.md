# Synthesis — strategy-vs-reality-panel (validation consensus)

**Run:** `run-1783095010-6266`
**Quorum:** 4/4 lens reports present + substantive + anchored.
`lens-codex.md` (244 lines, 71 anchors), `lens-kimi.md` (388 lines, 117 anchors),
`lens-opus.md` (148 lines, 36 anchors), `lens-minimax.md` (133 lines, 58 anchors).
All votes cover C1-C8. No ABSENT columns.

**Synthesis role:** cross-family consensus on 8 claims (C1-C8) registered in
the kickoff `kickoffs/strategy-vs-reality-panel.md` (or equivalent — sources
cited by lens evidence).

---

## 1. Consensus table

| # | Finding | Stated verdict | codex | kimi | opus | minimax | **Consensus** | **Confidence** |
|---|---|---|---|---|---|---|---|---|
| **C1** | LangGraph/LangChain structurally lack a closed learning loop | CONFIRMED (gap exists) | PARTIAL | PARTIAL | PARTIAL | AGREE | **PARTIAL** (downgraded) | **CONTESTED** |
| **C2** | Reliability quartet shipped | CONFIRMED | AGREE | AGREE | AGREE | PARTIAL¹ | **AGREE** (qualified) | **HIGH** |
| **C3** | Both gaps real + open: framework-edit verdict.json gap + capture/rollback gap | CONFIRMED | AGREE | AGREE | PARTIAL² | AGREE | **AGREE** (qualified) | **HIGH** |
| **C4** | Trajectory store drafted but NOT built; eval/insight depend on it | CONFIRMED | PARTIAL³ | AGREE⁴ | AGREE | PARTIAL⁵ | **PARTIAL** (qualified) | **CONTESTED** |
| **C5** | Today's learning signal = heuristic process_reward; runs end ungraded at reflect; 0042 reward cols unused | CONFIRMED | AGREE | AGREE⁶ | AGREE | AGREE | **AGREE** | **HIGHEST** |
| **C6** | Routing does NOT yet use log-sigmoid on Smax | CONFIRMED | AGREE | AGREE | AGREE | AGREE | **AGREE** | **HIGHEST** |
| **C7** | ContextNest integration exists (client + assembler + semantic memory) but judge-gated extract→distill→verify pipeline NOT built | CONFIRMED | AGREE | AGREE | AGREE | AGREE | **AGREE** | **HIGHEST** |
| **C8** | Dependency ordering (reliability → trajectory store → eval → learning-on-real-signal → memory brain) is correct | CONFIRMED | PARTIAL⁷ | PARTIAL⁸ | PARTIAL⁹ | AGREE¹⁰ | **PARTIAL** (qualified) | **CONTESTED** |

¹ minimax: quartet present on `feat/run-artifacts-store` + `lib/`, but **not merged to `main`** (main tip `f0a5ac7`, 30+ commits behind branch). See §2.
² opus: verdict.json fix is shipped on the working branch via commit `e185064` + `_verdict_merge.sh` + `tests/unit/test_framework_edit_verdict.sh` — half of "both gaps open" is stale; the capture/rollback half remains open.
³ codex: `lib/trace_store.sh` (main, `trace_write`, `mo_grade_run_reward`) IS a per-node trace writer; what 0047 adds is the **artifact-path/sha registry**, not the trace store itself. Kickoff framing conflated the two.
⁴ kimi: 0047 migration is **uncommitted working-tree**, not on any branch commit; `feat/run-artifacts-store` and `origin/main` point at the same commit (6f4e2d4). Even "drafted" is generous — it's an untracked file.
⁵ minimax: migration + Python writer (`mini_ork/dispatch/telemetry.py:140`) + bash mirror (`lib/llm-dispatch.sh:549-556`) all present on branch; not runtime-active on local state DB (latest applied = 0044). "Underway not active."
⁶ kimi edge case: migration 0042 lines 13-18 + 24 add `validity` defaulting to `'valid'` — heuristic PRM rows read as `valid` even when never judged (silent-poisoning risk for any `WHERE validity='valid'` without source filter).
⁷ codex: Phase-0 scalar eval can ship ahead of 0047; `mo_grade_run_reward` (`lib/trace_store.sh:405`) consumes `execution_traces` directly. Only trajectory-aware eval (Phase 1+) needs the artifact registry.
⁸ kimi: eval doc Phase 0 (`internal-docs/research/2026-07-03-adding-eval-to-miniork-run-flow.md:207-212`) explicitly contemplates shipping **without** trajectory store as prerequisite, contradicting the run-artifacts kickoff's "precondition for evidence bundle, eval, replay, distiller" claim (`kickoffs/eval/run-artifacts-trajectory-store.md:18-20`). The two internal docs disagree.
⁹ opus: ordering is right in principle but **capture/rollback reliability is the hard Phase-0 precondition**, not just trajectory-store completion. Without trustworthy capture, a graded judge encodes noise as signal.
¹⁰ minimax: a judge needs a trace; trajectory store is therefore a hard dep for any in-loop eval node. AGREE on the strict ordering.

---

## 2. Verdict changes (where the panel OVERTURNS or QUALIFIES the stated verdict)

Three findings have stated verdicts that the panel corrects. These are the highest-leverage results — single-verifier register rewrites that need to land in the strategy doc before sign-off.

### C1: stated verdict downgraded CONFIRMED → PARTIAL

**Stated:** "LangGraph/LangChain structurally lack a closed learning loop."
**Panel correction (3 of 4 lenses, with proof):**
- **codex** (`lens-codex.md:7-9`): LangMem SDK (Feb 2025) ships long-term memory + insight extraction; LangSmith has offline + online + multi-turn evals; LangGraph `BaseStore` provides cross-thread persistence; LangChain blog "agent improvement loop starts with a trace" (Mar 2026). Primitives exist.
- **kimi** (`lens-kimi.md:13`): LangSmith + LangMem + LangGraph Store cover most building blocks; the gap is in *autonomous extraction→distillation→promotion*, not in "memory or eval or grading."
- **opus** (`lens-opus.md:11` + `:23`): mini-ork's own eval doc cites LangChain trajectory-eval explicitly (`internal-docs/research/2026-07-03-adding-eval-to-miniork-run-flow.md:96-115`). Strong-form claim is wrong; weak-form ("doesn't autonomously close run→judge→insight→memory→routing without manual wiring") is closer.

**Re-cast (panel consensus):** *LangGraph + LangSmith + LangMem expose the primitives a user could wire; mini-ork closes the loop autonomously. The defensible axis is autonomous-loop closure, not absence-of-primitives.* The minimax vote (AGREE) is a strategic AGREE — the moat is real — but the strategic vote does not require the factual overstatement.

### C4: stated verdict qualified CONFIRMED → PARTIAL (with cross-check)

**Stated:** "Trajectory store drafted but NOT built; eval/insight/replay depend on it."
**Panel qualification (each lens landed on different evidence):**
- **codex** (`lens-codex.md:11`): `lib/trace_store.sh` exists on main and is a per-node trace writer; **0047 (`run_artifacts`) is the missing artifact registry layer, not the missing trajectory store.** Phrasing "no per-turn jsonl trajectory store" conflates them.
- **kimi** (`lens-kimi.md:15` + `:23`): even "drafted" is generous — 0047 is an **uncommitted working-tree file** (`git log --all -- db/migrations/0047_run_artifacts.sql` empty; `git ls-files` empty; branch and main at same commit 6f4e2d4). The Python writer additions (`persist_artifact`, `_validate_rel_path`, `resolve_artifact_abs` in `mini_ork/dispatch/telemetry.py`) are uncommitted working-tree edits on top of `d576e28`.
- **opus** (`lens-opus.md:15`): migration 0047 exists as untracked draft; no per-turn `run_artifacts` table in the live DB schema.
- **minimax** (`lens-minimax.md:12`): migration + Python writer + bash mirror all present on branch, but not runtime-active (state DB latest = 0044).

**Re-cast:** *Trajectory STORE (per-node rows in `execution_traces`) is built and mainline (`lib/trace_store.sh`). Trajectory ARTIFACT INDEX (path/sha keyed by `(run_id, node_id, kind, rel_path)`, migration 0047) exists as code on the feat branch but is undrafted-not-merged — depends which checkout you're auditing. Eval/insight/replay's dependency on the artifact index holds for trajectory-aware eval; Phase-0 scalar eval does NOT need 0047.*

### C8: stated verdict qualified CONFIRMED → PARTIAL (internal-doc contradiction)

**Stated:** "Dependency ordering (reliability → trajectory store → eval → learning-on-real-signal → memory brain) is correct."
**Panel qualification (3 of 4 lenses flag a real internal-doc contradiction):**
- **kimi** (`lens-kimi.md:19-20`): the eval doc's own Phase 0 (`internal-docs/research/2026-07-03-adding-eval-to-miniork-run-flow.md:207-212`) proposes "Add `type: eval` to `bin/mini-ork-execute` + schema; slot it after verify in the run loop (`bin/mini-ork:499-507`)" — **no mention of requiring trajectory store first**. Contradicts `kickoffs/eval/run-artifacts-trajectory-store.md:18-20`.
- **codex** (`lens-codex.md:14`): `mo_grade_run_reward` (`lib/trace_store.sh:405`) consumes `execution_traces` directly with no artifact index needed — Phase-0 scalar eval can ship ahead of 0047.
- **opus** (`lens-opus.md:18` + `:32-33`): ordering is right in spirit, but the **explicit hard precondition for Phase-0 eval should be capture/rollback reliability**, not just trajectory-store completion. A graded judge on top of known-noisy capture locks in theater.
- **minimax** (AGREE): judgment at `lens-minimax.md:16` — a judge requires a trace; for trajectory-aware eval, the dep is hard. But defers to docs that allow a cheaper Phase-0 (process_reward + reviewer PASS/FAIL) first.

**Re-cast (panel consensus):** *The "foundations first" macro-ordering is correct* (a learning loop on unreliable execution learns noise; supporting `internal-docs/roadmap/2026-07-03-exceed-langgraph-langchain.md:74`). However, the **strict ordering between trajectory store and eval is internally contradictory across docs** and needs reconciliation: the eval doc's Phase-0 scalar design could ship before 0047; the artifact registry (0047) is a hard dep only for trajectory-aware eval (Phase 1+) and Agent-as-a-Judge. **Capture/rollback reliability is the unstated Phase-0 hard precondition** for any graded reward to be trustworthy.

---

## 3. Implementation-ready set (HIGHEST confidence — unanimous AGREE)

**Safe to act on now, all 4 lenses agreed with file:line evidence:**

### C5 — heuristic process_reward; runs end ungraded at reflect; 0042 cols unused
- **Evidence (all lenses):** `lib/process_reward.sh:1-43, 99-100, 188-189` (weighted-sum heuristic; v1 covers 80% of clear cases per header); `internal-docs/research/2026-07-03-adding-eval-to-miniork-run-flow.md:45-46` ("execute → deadline → rubric → verify → reflect → exit. No eval, no promote."); `db/migrations/0042_execution_traces_objective_aware_reward.sql:13-18` (reward_value + reward_g + reward_source + validity cols); `grep -rnE "type: *(evaluator|judge|grader|eval)" recipes/` = 0 matches (`lens-minimax.md:13`).
- **Action:** Phase-0 advisory eval node (`type: eval`) wired after verify in `bin/mini-ork-execute` per eval doc Phase 0. Use the existing `mo_grade_run_reward` function (`lib/trace_store.sh:405-438`) — already defined on main, never called from the run loop (see §4 finding M1).
- **Edge case noted by kimi (lens-kimi.md:17, :28):** Migration 0042 line 24 defaults `validity` to `'valid'`. A future query `WHERE validity='valid'` without source filter will silently include heuristic PRM rows as if they were judged. Fix: drop the `'valid'` default (use `'heuristic' | 'judged'` from day one) or add a NOT NULL `reward_source` constraint.

### C6 — routing does NOT use log-sigmoid on Smax
- **Evidence (all lenses):** `grep -rnE "Smax|sigmoid|log.sigmoid" lib/` = 0 matches. Actual routing math at `lib/lane_router.sh:283-301` is GRPO relative-advantage + Bayesian shrinkage + EMA blend + cost tiebreak (`lib/lane_router.sh:87-90, 269-279`). EdgeBench fit is Phase-5 proposal (`internal-docs/research/2026-07-03-adding-eval-to-miniork-run-flow.md:235-237, 288-290`).
- **Action (Phase 1+):** Evaluate whether log-sigmoid on Smax is an *upgrade* on top of GRPO-relative-advantage (kimi + codex note the current algorithm is legitimate and grounded, not wrong — `lens-kimi.md:37`, `lens-codex.md:14`). Do not frame it as replacing; this is additive ceiling prediction, not a swap.

### C7 — ContextNest integration exists, judge-gated extract→distill→verify NOT built
- **Evidence (all lenses):** `lib/cn_client.sh:23-27` — "NEVER write to CN via the tools/store endpoint from mini-ork. Canonical write path is session ingest only (cc_hooks → WAL → consolidation worker). We push events, not memories." `lib/context_assembler.sh:489-616` provides read path. `db/migrations/0046_semantic_memory.sql` on feat branch. `grep -rn "extract|distill|insight"` in `lib/`/`bin/` returns zero matches outside doc references.
- **Action (Phase 5):** Build the EDV pipeline per eval doc §5b. Honor the gate boundary — even a judge-gated distiller inside mini-ork cannot write to CN directly (minimax C7.5 at `lens-minimax.md:25`); promotion goes through `cc_hooks` + consolidation worker via the substrate extractor.

### Implementation-ready: HIGH-confidence but not unanimous AGREE

**C2 — reliability quartet shipped (3 AGREE, 1 PARTIAL caveat)**
- **Evidence:** All 4 PRs merged on `feat/run-artifacts-store` (`5cf433b` PR #65 M1 publisher-commit; `0fe6247`/`3c85083` PR #66 reviewer input assembly; `dd6a7f7`/`3e2b6ac` PR #67 all-lane throttle retry; `753d20e`/`edb2228` PR #68 typecheck-detect). Audit doc `5b7d83b` explicitly marks the quartet fixed.
- **Caveat from minimax (lens-minimax.md:11):** main tip `f0a5ac7` is ~30 commits behind the branch. Code is shipped in the active branch's `lib/`, but `git log main` would not show it. If the strategy doc claims "shipped to main," that text is stale; "shipped on the active branch" is correct.
- **Action:** clarify in any roadmap pitch that "shipped" means "shipped to the active integration branch," not "shipped to the customer's `main`." Add the missing PR → main-merge to the unblock queue.

---

## 4. Missed findings (cross-lens surfacing that the register did not contain)

Eleven items the 4 lenses surfaced that are absent from the kickoff register. Sorted by impact.

### M1 — `mo_grade_run_reward` is defined-but-uncalled dead code (CRITICAL — codex)
**Source:** `lens-codex.md:24` (Finding 1) + `lens-codex.md:13` (C2 supporting evidence).
**Evidence:** `lib/trace_store.sh:405-438` implements rubric 0-8 → `reward_value` + `reward_g` → feed GRPO router. `grep -rn 'mo_grade_run_reward' lib/ bin/` returns only 3 hits — all in `trace_store.sh` itself (definition + comment + usage echo). Function exists on main via squash commit `a473408`.
**Why it matters:** C5 should sharpen from "no eval node" → "eval-loop closure helper is designed + on main, but never wired into the run loop." This is the foundation-already-poured pattern repeated across the whole stack: `trace_store.sh`, `lane_router.sh`, `cn_client.sh` are all the right shapes; none close the loop autonomously. The architect-claim "foundations first" is more accurate as "foundations already poured, automation not built."
**Action:** make wiring `mo_grade_run_reward` into `bin/mini-ork-execute:507` (reflect stage) a Phase-0 implementation-ready task, not a design task.

### M2 — Migration 0042 `validity` default silently promotes heuristic to valid-judged (HIGH — kimi)
**Source:** `lens-kimi.md:17` (edge case bullet under C5) + `lens-kimi.md:28` (Disputes).
**Evidence:** `db/migrations/0042_execution_traces_objective_aware_reward.sql:24` defaults `validity` to `'valid'`. Heuristic PRM rows (today's only `reward_value` writer) read as `valid` even when never judged.
**Silently-broken query shape:** `WHERE validity='valid' AND reward_source IS NOT NULL` — correct filtering requires explicitly checking `reward_source = 'eval@v1'` (or never-`process_reward`); a missed filter silently aggregates heuristic scores as judge scores.
**Action:** migration 0042 is shipped and likely already applied; ship a corrective `0048_validity_no_default.sql` to remove the `'valid'` default (use `'heuristic' | 'judged' | 'pending'` enum). Mention this in the eval node rollout — the silent poisoning is a Goodhart-style hazard for the same reason Goodhart's law applies to PRM-as-PRM (C5).

### M3 — No `reviewer_model` column on `execution_traces` (HIGH — minimax)
**Source:** `lens-minimax.md:26` (C9 missed finding).
**Evidence:** `lib/process_reward.sh:30` comment — "Same-family decontamination removed: see FE note ... Awaiting a real reviewer_model column in execution_traces."
**Why it matters:** Same-family decontamination (a reviewer from the same model family as the implementer cannot grade itself) is the only hygiene barrier preventing a PRM cheating on its own outputs in the eval node. The column is the **schema prerequisite** for any graded judge, not just an algorithm choice.
**Action:** ship `0049_reviewer_model.sql` before wiring the eval node. Backfill is impossible since the data is gone — Phase 0 eval will only have data from the day this column exists.

### M4 — `$50/day` cost circuit-breaker halts the entire queue (HIGH — minimax)
**Source:** `lens-minimax.md:27` (C10 missed finding). Memory note: `framework_edit_verdict_json_gap` corroborates.
**Evidence:** reported in memory note; no per-run circuit-breaker exists, so a single expensive run can stop all subsequent runs that day.
**Why it matters:** a learning loop that halts mid-trajectory won't grow the trajectory store, won't feed the judge, won't accumulate reward signal for router retraining. This is a continuous-loop hazard upstream of every claim in C4-C8.
**Action:** add per-lane + per-objective-domain daily budget caps in `bin/mini-ork-scheduler` so a single expensive run doesn't starve the learning loop entirely.

### M5 — `lib/cleaner.sh:91-107` documents a 2026-06-13 file-reversion race (HIGH — codex, kimi)
**Source:** `lens-codex.md:27` (Finding 4) + `lens-kimi.md:15` (C3 evidence).
**Evidence:** Cleaner stash-pop reverts intervening operator commits in framework-edit / recursive-validate-impl runs. Memory note `framework_edit_capture_unreliable` confirms.
**Why it matters:** capture/rollback reliability is the load-bearing gap; this is the concrete race condition that makes capture a coin flip on recursive validation.
**Action:** treat C3's load-bearing half ("trustworthy capture/rollback") as the Phase-0 hard precondition for any graded eval (sharpens §2 C8 re-cast).

### M6 — Write-side asymmetry: even Phase-5 distiller cannot write to CN directly (MEDIUM — minimax)
**Source:** `lens-minimax.md:25` (C7.5).
**Evidence:** `lib/cn_client.sh:23-27` explicitly forbids CN writes from mini-ork; the canonical pipeline is `cc_hooks → WAL → consolidation worker`. The substrate extractor parses z-dashboard blocks (per CLAUDE.md) but is not gated on mini-ork grading of its own runs.
**Why it matters:** the gate boundary is upstream of the mini-ork run loop. A judge-gated extract→distill→verify pipeline inside mini-ork ultimately has to publish via the substrate hook, not via a direct CN write. Whoever owns the gate (substrate team vs. mini-ork team) needs to be explicit.
**Action:** add a wire-protocol section to the eval node design specifying that the distiller emits structured `z-insight` blocks (and the equivalent non-Claude-agents path) that the substrate consumes; mini-ork does not own the CN write itself.

### M7 — `bin/mini-ork-logs` + per-run `.live.log` sidecar only fixed the outer 2 layers (MEDIUM — minimax)
**Source:** `lens-minimax.md:23` (C3.5). Memory note: `miniork_live_cli_streaming`.
**Evidence:** the live CLI streaming fix (project `live-cli-streaming` shipped) addressed outer-stream buffering for `mo_llm_dispatch`. Middle 2 layers + minimal-agent + SSE UI still buffer.
**Why it matters:** if you can't tail a judge's log live while running, the feedback loop in human-on-the-loop mode is delayed. Not a learning-loop-closure gap, but a learning-loop-observability gap.

### M8 — `lib/trace_store.sh:88-110` documents a verifier_output double-encoding bug (MEDIUM — codex)
**Source:** `lens-codex.md:26` (Finding 3).
**Evidence:** `lib/trace_store.sh:88-110` documents a verifier_output double-encoding bug fixed by FE-1 (2026-06-23) that "silently broke the GRPO reward path" for "10+ DF cycles" before being noticed.
**Why it matters:** reinforces C5/C7 brittleness — silent failure modes in the eval-loop wiring live undetected for weeks. Same family of risk as M5.

### M9 — Audit-doc drift: `5b7d83b` does not verify the fix (MEDIUM — kimi)
**Source:** `lens-kimi.md:35` (Edge case).
**Evidence:** `5b7d83b docs(audit): mark reliability quartet fixed` is a docs-only commit. If PR #67 throttle retry regresses under a workload class, the audit doc still says "fixed" until someone updates it.
**Action:** wire the audit doc to be auto-generated from a liveness check on the four fixes (e.g., a `bin/mini-ork-quartet-check.sh` that runs each fix's reproducer and updates the doc's date stamp).

### M10 — C8 capture/rollback is the unstated Phase-0 hard precondition (re-casts §2 C8 — opus)
**Source:** `lens-opus.md:18` (C8 mixed intent call) + `lens-opus.md:32-33`.
**Evidence:** without trustworthy capture, a graded judge encodes noise as signal (Goodhart pressure). Mini-ork's own roadmap doc states this premise at `internal-docs/roadmap/2026-07-03-exceed-langgraph-langchain.md:74`.
**Why it matters:** §2 C8 re-cast lists this but deserves a separate finding because **no other lens surfaced it as the explicit reorder**. The opus lens is alone on this nuance.

### M11 — Middle-layer-buffering gap (mini-ork-logs) and `bdd-verdict.json` vs `panel-verdict.json` naming split (codex)
**Source:** `lens-codex.md:26` (Finding 2).
**Evidence:** `lib/reflection-refiner.sh:16-27` reads `bdd-verdict.json`; framework-edit path writes `panel-verdict.json` / `review-verdict.json` (per `bin/mini-ork-execute:796-797, 839`). Three different verdict file conventions, no canonical writer.
**Why it matters:** small but a contributor to C3's gate-theater pattern. Pick one canonical name (`verdict.json` is already what `panel-verdict.json` semantically is) and deprecate the others.

---

## 5. Implementation-ready vs. CONTESTED summary

**Implementation-ready (HIGHEST confidence — 4/4 AGREE, safe to act):**
- C5 — add `type: eval` node design (use `mo_grade_run_reward` from main)
- C6 — log-sigmoid on Smax is a Phase 1+ additive design (current GRPO router is legit, not wrong)
- C7 — extract→distill→verify pipeline as Phase-5 work; respect CN write-asymmetry

**HIGH confidence (3/4 AGREE + 1 PARTIAL with caveat, ship with note):**
- C2 — clarify "shipped on active branch" vs "shipped to main"; close the main-merge gap
- C3 — both gaps are real; verdict.json fix is on branch (e185064), capture/rollback still open

**CONTESTED (3/4 PARTIAL or substantive disagreement, needs another look):**
- **C1** — re-cast as "primitives exist, autonomous-loop closure doesn't" before any external pitch
- **C4** — split finding into (a) trajectory STORE is built and mainline (trace_store.sh) and (b) trajectory ARTIFACT INDEX (0047) is drafted-on-feat-not-merged; eval/insight/replay dependence is conditional on which eval (scalar vs trajectory-aware)
- **C8** — reconcile eval doc Phase 0 vs run-artifacts kickoff precondition claim; make capture/rollback reliability the explicit Phase-0 hard precondition

**Will produce 11 follow-on tickets (M1-M11) — not in this run's scope, but each is a discrete work item with file:line evidence; see §4.**

---

## 6. Verifier self-check

- `synthesis.md` exists at `${MINI_ORK_RUN_DIR}/synthesis.md` ✓
- References all 4 lens names (codex, kimi, opus, minimax) ✓
- Per-finding votes cited with `lens-*.md` path:line ✓
- Verdict changes (C1, C4, C8) cite each dissenting lens's proof ✓
- Implementation-ready set + contested set separated ✓
- Missed findings listed with source lens + path:line ✓
- No lens votes fabricated; no `ABSENT` column required (all 4 lens reports substantive)

Panel outcome: **quorum reached, synthesis usable as consensus artifact.**
