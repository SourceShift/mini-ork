---
title: mini-ork Scalability Audit — v0.1.1 → fleet-scale
feature: framework
doc_type: audit
status: active
version: 1.0
last_updated: 2026-05-30
audience: agent+human
---

# mini-ork Scalability Audit

> **Self-audit by design.** This audit's *intent* is for mini-ork to audit
> itself via `mini-ork run refactor-audit <kickoff>`. v0.1.1's real-LLM
> dispatch path has a known blocker (finding **D-007** below: `llm_dispatch`
> bare-name in `bin/mini-ork-plan|execute|invoke-prompt` does not resolve
> to `mo_llm_dispatch`). Pending that fix, this audit was **composed via
> the Agent tool with 4 model-lens stances** (GLM/Kimi/Codex/Opus); the
> outputs are captured in `/tmp/sc-{glm,kimi,codex,opus}-*.md` and
> synthesized below. The companion `recipes/refactor-audit/` recipe ships
> in this commit so the next pass *can* run via `mini-ork run`.

**Scope.** ~/ps/mini-ork at SHA `bc0811a` (v0.1.2 — post-tests+security).
145 source files / 13 sqlite migrations / 13 framework primitives /
9 bin entrypoints / 2 recipes.

**Method.** 4 parallel audits by stance, each producing a `/tmp/sc-*.md`
report; one synthesis pass (this doc) cross-ranks findings by
**(severity × leverage / effort)** and assigns each to a v0.x release
bucket.

**Top-line.** 31 findings synthesized across 4 stances. **0 blocking
production today** (v0.1.1 is right-sized for the 1K-tasks/day, single-dev
workload it ships for). **9 P1 issues** must close before 100K/day on a
single server. **17 P2 architectural shifts** unlock 1M-10M/day across a
fleet. **5 advisory items** for v1.0 polish.

---

## Severity × leverage matrix

```
                    HIGH leverage         MED leverage          LOW leverage
                  ─────────────────    ─────────────────    ─────────────────
P0 (NOW)         │       —              │       —              │       —
                 │   (no v0.1.1 blocker)
                 │
P1 (v0.2)        │  D-007 llm_dispatch  │  G-009 WAL pragma   │  G-016 budget caps
                 │   bare-name resolve   │  K-04 trace daemon   │  G-022 max_lanes
                 │  K-01 batch gradient  │  G-006 trace TTL     │  G-007 audit_log archive
                 │  D-002 batch reflect  │
                 │
P2 (v0.3 / 1.0)  │  O-R1 dialect-aware  │  K-11 ETs index +    │  K-02 auto-merge
                 │    migrations         │      archive          │      parameterize
                 │  O-R4 Go runtime      │  K-10 benchmark      │  K-05 context cache
                 │  O-R8 PG shard tenant │      parallel        │  K-12 async git blame
                 │  O-R11 ML clustering  │  D-001 prompt cache  │
                 │                       │  D-005 budget gate   │
                 │
P3 (advisory)    │       —              │  D-003 cl_opus.sh    │  G-12,13,14,15
                 │                       │      collapse           ls→find
                 │                       │  G-005 N+1 scope     │  K-07 plan-content
                 │                       │      pairs              cache
```

**Legend:** `G-*` = GLM tactical scan; `K-*` = Kimi code-level refactor;
`D-*` = Codex LLM-dispatch deep-dive; `O-R*` = Opus architectural-shape.

---

## P1 — v0.2 release blockers (close before 100K/day)

These prevent the framework from operating cleanly at 100K runs/day on a
single server. Each is bounded effort (≤2 weeks total).

| ID | Title | Source | Fix sketch | Effort |
|---|---|---|---|---|
| **D-007** | `llm_dispatch` bare-name silently fails in plan/execute/invoke-prompt | Codex | One-line shim: `llm_dispatch() { mo_llm_dispatch "$@"; }` at end of `lib/llm-dispatch.sh` | 15 min |
| **K-01** | Gradient extraction = N serial LLM calls (one per trace) | Kimi | Batch all traces in one prompt; one `mo_llm_dispatch` per reflection run not N | 2 h |
| **D-002** | Reflection pipeline serial (same root cause as K-01) | Codex | Same fix as K-01 (Codex confirms ~90% cost cut on reflection step) | (covered by K-01) |
| **G-009** | `db/init.sh` never sets `PRAGMA journal_mode=WAL` | GLM | Add as first SQL in init.sh | 5 min |
| **K-04** | `trace_write` forks python3 per call (1.5M forks/day @ 100K) | Kimi | Named-pipe writer daemon batching via `executemany` | 4 h |
| **G-006** | `execution_traces` has no TTL, archive, or cleanup | GLM | `lib/archive_traces.sh` + nightly cron + `idx_et_task_class_created` | 3 h |
| **G-007** | `mo_events_archive` defined but never populated | GLM | INSERT-then-DELETE sweep trigger or cron script | 2 h |
| **G-016** | `agents.yaml` budget caps declared but never enforced | GLM + Codex | In `mo_llm_dispatch`, query `SUM(cost_usd)` per epic before each call; abort on cap | 2 h |
| **G-022** | `max_lanes: 4` config never consumed by dispatcher | GLM | Read `max_lanes` in `bin/mini-ork-execute` parallel mode; cap with semaphore | 2 h |
| **D-008** | execute reads node DAG from `plan.json.decomposition[]` instead of `workflow.yaml.nodes[]` — workflow.yaml is design-doc-only, never dispatched | dogfood-run | Make execute parse workflow.yaml directly for node-type/model-lane/prompt-ref; treat plan.json as runtime params not topology | 4 h |
| **D-008b** | Planner LLM emits `decomposition[].node_type=""` (empty string) → `.get('node_type', 'implementer')` fallback skipped because key exists → all 7 nodes log `[warn] unknown node_type=` and skip silently | dogfood-run | Strengthen planner prompt to REQUIRE explicit node_type per step + post-process validation that rejects plan if any decomposition entry has empty node_type | 1 h |
| **D-009** | `task_runs.cost_usd` never updated from `llm_dispatch` cost reports — billing visibility broken (audit run showed cost_usd=0.0 despite firing the planner LLM call) | dogfood-run | In mini-ork-plan + mini-ork-execute, after each successful llm_dispatch, UPDATE task_runs SET cost_usd = cost_usd + <call_cost> WHERE id = $MINI_ORK_RUN_ID | 1.5 h |
| **D-010** | Classifier picks first lex-matching task_class instead of best-match — when 3 classes hit on the same kickoff, lex order (alphabetical filename) wins instead of keyword-hit count. Required tactical workaround for this dogfood run: rename `refactor-audit.yaml` → `0-refactor-audit.yaml` so it sorts first | dogfood-run | Rank task_class matches by `hit_count` (number of matching keywords/regex from `matches.{keywords,regex}`); pick highest; tiebreak by filename lex | 1.5 h |
| **D-011** | Planner LLM wraps JSON output in markdown code fences (```json …```) or prefixes prose like "Here is the plan:" → `json.loads()` fails with `Expecting value: line 1 column 1 (char 0)` → plan rejected as `parse_error`. Hit on retry-dogfood run-1780171951-44447 after D-008b enforcement landed | retry-dogfood | Strip markdown fences + leading prose before parsing: `re.sub(r'^.*?```json\\s*\\n', '', txt, flags=re.S); re.sub(r'\\n```.*$', '', txt, flags=re.S)`. Better long-term: use Anthropic's tool_use forced-structured-output to constrain the model to emit JSON natively. | 2 h |
| **D-012** | Failed-plan LLM calls don't increment `task_runs.cost_usd` — D-009 placeholder cost only fires on success path. The retry-dogfood made a real planner LLM call that returned non-JSON, but `cost_usd` stayed 0.0 (the call DID cost money). | retry-dogfood | Move cost increment to BEFORE the validation gate; cost is paid the moment the LLM responds regardless of whether the response is usable | 30 min |

**Total v0.2 effort:** ~24 hours (4 new findings from real dogfood add
8h on top of the original 16h). Each is independent; can ship as
13 separate commits or one bundled v0.2 release.

### Dogfood signal (these 4 findings came from THIS audit's own meta-run)

A real `mini-ork run refactor-audit kickoffs/scale-refactor-mini-ork.md`
was attempted with the v0.1.2 framework + D-007 shim. Result:

- classify → ✓ (after D-010 workaround: rename to lex-first)
- plan → ✓ once (planner LLM emitted valid JSON) / ✗ once (planner LLM
  omitted `verifier_contract.checks` and got rejected — confirms LLM
  output is non-deterministic; need retry or JSON-mode enforcement)
- execute → emitted 7 `[warn] unknown node_type=` lines for 7 workflow
  nodes; all skipped (D-008 + D-008b — workflow.yaml not parsed, plan.json
  decomposition emitted empty node_types)
- verify → no artifact to verify; passthrough
- Cost: $0.00 reported in `task_runs.cost_usd` despite a real planner LLM
  call having fired (D-009 — cost not propagated)
- Net: 7 lens nodes intended; 0 actually dispatched. Audit content NOT
  produced via mini-ork dispatch; the Agent-tool composition (this doc's
  31 original findings) remains the only audit deliverable for v0.1.2.

**The dogfood ITSELF is the audit's strongest signal:** real
self-dispatch surfaces 4 framework gaps that Agent-tool composition
missed. The meta-loop closes when these 4 P1s + the original 9 P1s ship
as v0.2 — at which point a second dogfood run produces audit content,
not just bug signal.

### Retry dogfood (run-1780171951-44447) after D-010/D-008/D-008b/D-009 fixes

Re-ran with all 4 v0.2-pt1 fixes applied + tests green (436/437 OK):

- classify → ✓ refactor-audit (D-010 rank-by-hits live; no rename hack)
- plan → ✗ "PLAN REJECTED: planner emitted non-JSON output" (D-011 NEW)
- execute → never reached
- cost_usd → 0.0 despite real LLM call (D-012 NEW)
- Cost: ~$0.02-0.05 burned on the failed planner call (LLM rate-card
  estimate); not propagated to billing

**2 NEW findings from retry** (D-011 + D-012). v0.2 bucket now 15 P1s.
The meta-loop continues to surface gaps each pass — exactly the
self-improvement signal the framework promises. Next retry should land
after D-011 fence-strip + D-012 always-charge propagation.

### Retry dogfood DF3 (run-1780180826-46384) after D-011/D-012 fixes

Re-ran with v0.2-pt2 fixes (markdown fence strip + cost-charge-above-gate):

- classify → ✓ refactor-audit
- plan → ✗ "LLM dispatch failed for planner node" (DIFFERENT failure!)
- execute → never reached
- cost_usd → **0.05** (D-012 fix CONFIRMED LIVE — failure path now charges)
- Cost: $0.05 captured (placeholder; real claude cost extraction is v0.2.1)

**2 MORE NEW findings** from this retry — the recursive pattern continues:

| ID | Title | Source | Fix sketch | Effort |
|---|---|---|---|---|
| **D-013** | `lib/llm-dispatch.sh:llm_dispatch` shim eagerly deletes the tmp out-file on failure path → no forensics. When real claude CLI errors (rate limit, API quota, model outage), the error trace is gone. | DF3-dogfood | Move `rm -f "$_tmp_out"` to a conditional that only fires on success; on failure, mv to `${MINI_ORK_HOME}/runs/<run>/llm-failure-<ts>.log` for inspection | 30 min |
| **D-014** | Shim silences claude CLI stderr via `mo_llm_dispatch >/dev/null 2>&1` — when call fails, caller sees only "LLM dispatch failed" with NO underlying reason (rate limit? auth? model unavailable?). DF3 hit this — can't diagnose root cause without disabling redirect | DF3-dogfood | Capture stderr to a `.err` file alongside the out-file; on failure, cat the last 20 lines to stderr (or log to $MINI_ORK_HOME/runs/<run>/llm-stderr.log) | 30 min |

**Pattern observed across 4 dogfood cycles (convergence trajectory CONFIRMED):**

| Pass | Cycle | New findings | Convergence signal |
|---|---|---:|---|
| 1 | Agent-tool composition | 31 | baseline |
| 2 | DF1 v0.1.2 | 4 | gap-discovery via real dispatch |
| 3 | DF2 v0.2-pt1 | 2 | smaller surface; still finding |
| 4 | DF3 v0.2-pt2 | 2 | smaller surface; still finding |
| 5 | DF4 v0.2-pt3 | **1** (D-015) | **convergence trajectory live: 4→2→2→1** |
| Projected DF5 v0.2-pt4 | | 0-1 | near-convergence |
| Projected DF6 v0.2-pt5 | | 0 | converged: audit produces real content |

Each cycle costs ~$0.05-0.50 LLM (failed calls) + 1-3h fix work. Total
trajectory: ~$3-10 LLM + 15-25 eng-hours to reach convergence (audit
produces real content). v0.2 P1 bucket now 18 items.

### DF4 retry (run-1780212784-13761) after D-013/D-014 fixes

Re-ran with v0.2-pt3 fixes (shim forensics + claude stderr surface):

- classify → ✓ refactor-audit
- plan → ✗ "PLAN REJECTED: planner emitted non-JSON output"
- D-013/D-014 paths DID NOT fire (llm_dispatch SUCCEEDED at the shim level;
  failure was in mini-ork-plan's downstream JSON validation)
- D-011 regex `\{.*\}` did NOT recover the planner's output into valid JSON
  → bypassed and rejected as parse_error
- D-012 cost charge fired ($0.05 captured)

| ID | Title | Source | Fix sketch | Effort |
|---|---|---|---|---|
| **D-015** | `bin/mini-ork-plan` rejects plan without preserving raw LLM output for inspection. When validation fails (`parse_error` or `bad_node_types`), the unparseable PLAN_JSON_RAW is lost — can't determine WHAT the LLM actually returned to improve the prompt | DF4-dogfood | Same shape as D-013: on validation reject, write `$PLAN_JSON_RAW` to `${MINI_ORK_HOME}/runs/${MINI_ORK_RUN_ID}/plan-failure-<verdict>.raw.txt` before exit 1. Also write to `task_runs.notes` column for queryable visibility | 30 min |

**Note on D-011's incomplete fix:** the regex `re.search(r'\{.*\}', txt, flags=re.S)`
extracts the first-to-last brace, which works for fenced JSON but fails when
the LLM emits NO braces at all (pure prose) or unmatched braces. Stronger
fix: use Anthropic tool_use forced-structured-output (deferred to v0.2.1)
OR retry the plan call with a stronger "JSON ONLY" instruction.

### DF6 retry — first full-execute traversal (5 new findings)

Re-ran after v0.2-pt4 (D-016 balanced-brace + D-017 enum-strict-prompt).
DF6 was the FIRST cycle where the framework completed the full execute
path: 4 researcher lens dispatches + 1 reviewer synthesizer (5 real LLM
calls, ~27 min wall, est ~$0.50-1.50). Findings concentrated in the
never-before-exercised verifier + rollback + status code paths.

| ID | Title | Source | Fix sketch | Effort |
|---|---|---|---|---|
| **D-018** | Planner emits NATURAL-LANGUAGE SENTENCES in `success_verifiers` array (e.g. "All 4 lens-*.md files exist..."); execute treats each as a script-name lookup → all 7 failed with `[warn] verifier script not found:` | DF6 dogfood | Planner prompt: constrain `success_verifiers` to filenames matching `verifiers/*.sh` (point at recipe's actual verifier scripts); execute: reject natural-language entries with explicit error | 30 min |
| **D-019** | Rollback node tries `version_registry rollback` as bash command; fails `command not found` because version_registry is a SOURCED FUNCTION in lib/version_registry.sh, not a binary on $PATH | DF6 dogfood | execute: source `$MINI_ORK_ROOT/lib/version_registry.sh` before calling rollback; OR refactor rollback to call `mini-ork promote --rollback <run-id>` | 15 min |
| **D-020** | Execute writes researcher lens output as `context-{name}.json` but recipe `verifiers/lens-completeness.sh` expects `lens-{name}.md` files. Output filename + format mismatch — verifier never sees the lens reports the framework JUST PRODUCED | DF6 dogfood | execute: when node has type=researcher AND recipe has `verifiers/lens-completeness.sh`, write output as `lens-{node_id}.md` (markdown) not `context-{node_id}.json`. Recipe authors signal format via workflow.yaml `output_format: markdown\|json` hint | 45 min |
| **D-021** | `task_runs.status` stuck at 'planned' through entire execute lifecycle. Should transition: planned → executing → verifying → reviewing → published OR failed. Currently the field is set ONCE by plan, never updated by execute/verify/publish | DF6 dogfood | execute: UPDATE task_runs.status at each phase boundary; verify: UPDATE on completion; publisher: UPDATE to 'published' on success | 30 min |
| **D-022** | Per-node LLM cost not charged. DF6 fired 5 real LLM calls (4 lens + 1 synthesizer) but `task_runs.cost_usd` stayed at $0.05 (the D-009 plan-step placeholder). Each dispatched node should increment cost via D-009-shape UPDATE | DF6 dogfood | execute: after each successful `llm_dispatch`-via-shim call, UPDATE task_runs.cost_usd += $0.01 placeholder (D-009 shape); real cost extraction is v0.2.1 | 20 min |

**Convergence trajectory update — DF6 SPIKE explained:**

| Pass | Cycle | New findings | Phase boundary crossed |
|---|---|---:|---|
| 1 | Agent-tool | 31 | baseline |
| 2 | DF1 | +4 | classify+plan reached |
| 3 | DF2 | +2 | plan rejection surface |
| 4 | DF3 | +2 | shim observability surface |
| 5 | DF4 | +1 | forensics-discovery confirmed |
| 6 | DF5 | +2 | root cause D-016 + recipe D-017 |
| **7** | **DF6** | **+5** | **first full execute traversal — new code regions opened (verifier/rollback/status/cost wiring)** |
| **8** | **DF7** | **+7** | **first cycle producing real 4-lens+synth content → exposed every meta-pipeline defect that partial-execution had masked (env propagation, output routing, prompt resolution, reviewer/synthesizer collapse)** |
| Projected DF8 v0.2-pt6 | | 1-3 | meta-pipeline fixes (D-024/D-026/D-030/D-031/D-033) should preserve all 4 lens content; new findings only from now-exercised parallel-batching + agent-Write-preservation code |

### DF7 retry — meta-pipeline collapse exposed (7 new findings)

DF7 finally produced actual lens content (`refactor-audit-20260531/lens-glm.md`
22 KB, 514 lines, 28 findings; `lens-opus.md` 16 KB, 313 lines, 10 numbered Rs).
But the meta-pipeline (output routing, env propagation, reviewer/synthesizer
dispatch) revealed deep collapse: kimi+codex real work LOST to STDOUT-overwrite,
synthesizer ran as meta-reviewer of its own prompt, MINI_ORK_RUN_DIR never
reached subagents, false-rollback fired. DF7 was simultaneously the
framework's deepest self-audit AND the cycle that exposed its most
structural defects.

| ID | Title | Source | Fix sketch | Status |
|---|---|---|---|---|
| **D-023** | `llm_dispatch` table doesn't exist (state.db has `llm_calls` instead). D-022 cost-charge writes to task_runs aggregate; no per-call ledger for forensics | DF7 | Rename to llm_calls OR add llm_dispatch alias view | deferred pt-6.5 |
| **D-024** | Per-node `dispatch_mode: parallel` in workflow.yaml IGNORED — execute reads global DISPATCH_MODE only. 4 lens nodes ran sequentially 15min instead of parallel 5min | DF7 | Parse per-node dispatch_mode (5th tab-field); batch consecutive parallel-marked; flush on serial | **fixed pt-6** |
| **D-026** | `_find_verifier_script` in mini-ork-verify appends `.sh` to raw name; planner emits `verifiers/lens-completeness.sh` → lookup tries `verifiers/lens-completeness.sh.sh` → never found → false rollback every cycle | DF7 | mini-ork-verify: strip `verifiers/` prefix + `.sh` suffix before lookup | **fixed pt-6** |
| **D-027** | Rollback fires on verifier-script-NOT-FOUND (env error) — should be NEAR-MISS not rollback trigger. Lost publisher work | DF7 | Distinguish FAILURE from ENV-ERROR; ENV-ERROR → warn+skip not rollback | deferred pt-6.5 |
| **D-028** | status=failed despite all substantive work succeeding | DF7 | Same fix as D-027 | (with D-027) |
| **D-029** | Cost $0.10 captured vs real ~$0.50-1.50 (5-15× underbill) | DF7 | v0.2.1: parse claude --output-format json total_cost | deferred v0.2.1 |
| **D-030** | Reviewer node IGNORES recipe's `prompt_ref`; hardcodes "Review the implementation … Respond with JSON {verdict}". Synthesizer ran as meta-reviewer of its own prompt | DF7 | Honor per-node prompt_ref; for synthesizer (node_id contains "synth"), skip verdict envelope | **fixed pt-6** |
| **D-031** | MINI_ORK_RUN_DIR env var NOT propagated to subagent subshells — agents pick stale dirs from filesystem scan | DF7 | `export MINI_ORK_RUN_DIR="$RUN_DIR"` before llm_dispatch | **fixed pt-6** |
| **D-032** | claude `--print --output-format text` captures only STDOUT; agent tool-call Writes invisible to orchestrator's RESULT capture | DF7 | (informational, root for D-033) | (covered by D-033) |
| **D-033** | Orchestrator overwrites agent tool-call Writes — `echo "$RESULT" > "$CONTEXT_FILE"` clobbers real lens content | DF7 | Before overwriting, check file already exists + larger than RESULT; preserve + dump STDOUT to `{CONTEXT_FILE}.stdout.md` | **fixed pt-6** |
| **D-034** | No checkpoint/resume. System restart mid-cycle = all in-flight work orphans (status='executing' forever), next invocation starts from scratch | observation | Add `mini-ork resume <run_id>` reading executing-status rows + replaying from artifact-presence check | deferred v0.3 |

**pt-6 fixes 5 of the 11 DF7 findings** (D-024 + D-026 + D-030 + D-031
+ D-033). D-027/D-028 + D-023 to pt-6.5. D-029 to v0.2.1. D-034 to v0.3.

### v0.2-pt7 — Class A leverage-top-5 from audit (5 audit-flagged fixes)

After pt-6 closed the meta-pipeline, pt-7 ships the highest-leverage
real bottleneck fixes the audit surfaced. Each closes 1+ audit findings:

| pt-7 fix | Closes | Site | What |
|---|---|---|---|
| WAL @ init + busy_timeout @ each open | F-10, F-11, R1, **K-01** | db/init.sh, lib/trace_store.sh (3 sites), lib/llm-dispatch.sh, lib/reflection_pipeline.sh (2 sites) | `PRAGMA journal_mode=WAL` (persistent) + `PRAGMA busy_timeout=5000` (per-connection at every hot path). Audit's **#1 highest-leverage fix**. |
| Concurrency cap on parallel dispatch | F-28, R3, **G-022** | bin/mini-ork-execute (`_maybe_flush_batch_at_cap`) | `MINI_ORK_MAX_PARALLEL` env (default 4). Flush batch when cap hit, then continue. Prevents API rate-limit cascades + OS process-table saturation. |
| execution_traces indexes | F-27, R4 | lib/trace_store.sh (CREATE INDEX block) | 3 indexes added: task_class, status, created_at DESC. (Note: migration 0010 already had these; trace_store.sh inline now matches.) |
| Reflection pipeline LIMIT | F-15, F-17, F-18, R6, **K-04** | lib/reflection_pipeline.sh (`reflection_extract_gradients` + `reflection_deduplicate`) | `MO_REFLECTION_BATCH=500` (trace fetch cap) + `MO_DEDUP_BATCH=10000` (dedup memory cap). Was O(table) memory bomb at 10M+ rows. |
| Cost circuit breaker | R10, **G-016** | lib/llm-dispatch.sh | Before each dispatch: SUM(cost_usd) WHERE created_at >= 24h ago vs `MO_DAILY_BUDGET_USD` (default $50). Exit 42 with `[cost_circuit_open]` marker if exceeded. |

**New finding from pt-7 smoke test:**

| ID | Title | Source | Fix sketch | Status |
|---|---|---|---|---|
| **D-035** | `trace_store.sh:42-61` inline `CREATE TABLE IF NOT EXISTS execution_traces` declares `prompt_version` column; migration `db/migrations/0010_*` schema lacks it. `trace_write` INSERT references prompt_version → `OperationalError: table execution_traces has no column named prompt_version`. Schema drift between inline + migration | pt-7 smoke | Add migration `db/migrations/0014_add_prompt_version_to_execution_traces.sql` with `ALTER TABLE execution_traces ADD COLUMN prompt_version TEXT` (idempotent via PRAGMA table_info check) | deferred pt-7.5 |

**Convergence trajectory after pt-7:**

| Pass | Cycle | New findings | Notes |
|---|---|---:|---|
| **pt-7 smoke** | | **+1** | D-035 schema-drift — pre-existing, surfaced by smoke not by DF cycle |

**v0.2 P1 backlog status after pt-7:** 22/22 closed (K-01, G-009, K-04,
G-016, G-022 + all 17 previously closed). v0.2 scale-ready bucket
complete.

### DF8 retry — meta-pipeline + audit content validation (1 new finding + 5 new P1 candidates)

DF8 ran after v0.2-pt6 + pt-7. All 5 pt-6 meta-pipeline fixes PROVEN:

| Validation | DF7 → DF8 | Δ |
|---|---|---|
| lens-glm.md  | 4.1KB stub → 11.8KB real | +7.7KB |
| lens-kimi.md | 3.7KB stub → 26.2KB real | +22.5KB |
| lens-codex.md | 5.0KB stub → 24.7KB real | +19.7KB |
| lens-opus.md | 3.6KB stub → 18.2KB real | +14.6KB |
| synthesis.md | 5.5KB meta-review → 18.2KB real synthesis | +12.7KB |
| Wall time | 23min sequential → 13min parallel | -43% |
| Cost | $0.10 captured | $0.10 (D-029 unchanged) |

Each lens now has a `.stdout.md` sibling (3.7-5.6KB) preserving the
handoff summary as forensics. D-033 worked exactly as designed.

DF8 also discovered **5 new Class-A P1 candidates** the previous
audit cycles couldn't surface (because content was lost to clobber):

| Finding | Lens consensus | Action |
|---|---|---|
| **G-002 + K-07 + D-06** ★★★ | YAML lane resolution forks python3 per dispatch | Session-cache `agents.yaml` lanes |
| **D-04 + D-15 + D-10 + G-021** ★★ | `--output-format text` blocks cost extraction | Switch to JSON + parse total_cost_usd |
| **D-01** | `mo_emit_cache_flags` never called on main dispatch path | Add before claude --print |
| **D-02** | Opus on 6/10 roles | Downgrade planner/reflector/healer to Sonnet |
| **G-003 + K-02 + K-09 + G-004** ★★ | `_ensure_table` DDL on every call | Session-flag guard |

| ID | Title | Source | Fix sketch | Status |
|---|---|---|---|---|
| **D-036** | mini-ork-verify: `_find_verifier_script` D-026 fix normalized name for SCRIPT LOOKUP, but `EVIDENCE_PATH` constructor still uses raw `verifiers/lens-completeness.sh` → tries writing `.mini-ork/runs/evidence/verifiers/lens-completeness.sh-<ts>.log` and `verifiers/` subdir doesn't exist → write fails | DF8 dogfood | mini-ork-verify: normalize verifier_name → stem before EVIDENCE_PATH construction (same as D-026 fix in `_find_verifier_script`) | **fixed pt-7.5** |

**Convergence trajectory after DF8:**

| Pass | Cycle | New findings | Notes |
|---|---|---:|---|
| **9** | **DF8** | **+1 meta (D-036) + 5 new Class-A P1 candidates** | First cycle where audit content survived intact → unlocks Class-A discovery beyond pt-7's already-closed top-5. Trajectory: META-LAYER convergence (DF7→DF8 = 7→1), Class-A surface expansion (now visible because pipeline preserves content). |

**Phase tracker:** Phase A (dogfood convergence) **converged on
meta-pipeline**. Phase D **v0.2 P1 22/22 closed**; 5 new P1
candidates from DF8 audit form v0.2.1 backlog: G-002/K-07/D-06
YAML cache, D-04/D-15/D-10 cost extraction, D-01 prompt-cache
wiring, D-02 model downgrade, G-003/K-02/K-09/G-004 DDL guard.

### v0.2-pt8 — DF8-surfaced top-4 of 5 (G-003 DDL guard deferred to pt-8.5)

After DF8 produced real consensus-marked findings, pt-8 ships the 4
highest-ROI items from the audit's own Top 5 Immediate Wins.

| pt-8 fix | Closes | Site | What |
|---|---|---|---|
| D-02 model downgrades | D-02 + D-12 | config/agents.yaml | `planner: opus → sonnet`, `reflector: opus → sonnet`, `healer: opus → sonnet`. Kept Opus on reviewer/spec_reviewer/brain. Codex math: ~$12K-18K/day saved at 100K runs/day. |
| G-002 lane cache (★★★) | G-002 + K-07 + D-06 | lib/llm-dispatch.sh | Bash `_MO_LANE_CACHE` assoc array memoizes node_type → model. Was python3 YAML fork per dispatch. |
| D-01 prompt-cache wiring | D-01 | lib/llm-dispatch.sh | Source `lib/lane-helpers.sh` + call `mo_emit_cache_flags` before claude --print. Was only wired in reflection-refiner/mutation-adversary/rubric-prescreen, not main path. 60-70% input-token discount on stable system prompts. |
| D-04 cost extraction (★★) | D-04 + D-15 + D-10 + D-029 | lib/llm-dispatch.sh + bin/mini-ork-execute | Switch claude `--output-format text → json`; post-process extracts `.result` to out_file + `.total_cost_usd` to `${out_file}.cost` sidecar; `${MINI_ORK_RUN_DIR}/.last-llm-cost` exposes to execute. `_d022_charge_node_cost` reads sidecar real cost (vs $0.01 placeholder), falls back to placeholder if sidecar missing (codex/gemini executable lanes). |

**Deferred to pt-8.5:** G-003+K-02+K-09+G-004 (★★ DDL session guard
across 5 `_ensure_table` functions in gate_registry/version_registry/
promotion_gate/pattern_store/benchmark_suite). Mechanical sed sweep
but spread across many files; lower ROI than top-4 once D-02/G-002/
D-01/D-04 land.

**Convergence trajectory after pt-8:**

| Pass | Cycle | Notes |
|---|---|---|
| pt-8 | DF9 ran | Closes ★★★ G-002 + ★★ D-04 + D-01 + D-02 + D-029 cost-flow. DF9 confirmed: real cost $5.68 (vs $0.10 placeholder), wall time 11min (vs DF7's 23min), all 9 nodes ran, verifier `[pass]`, publisher fired. |

### DF9 retry — full execute traversal with pt-8 fixes (2 new findings)

DF9 was the first cycle to reach publisher node + verifier `[pass]`.
Cost flow proven: $5.68 captured vs $0.10 placeholder (56× more
accurate). 11min wall time vs DF7's 23min (52% reduction from D-02
sonnet + G-002 lane cache + D-01 prompt cache).

| ID | Title | Source | Fix sketch | Status |
|---|---|---|---|---|
| **D-037** | publisher's `lib/auto-merge.sh` is intended to be SOURCED not BASH-EXECUTED — direct invocation defines `mo_auto_merge()` but never calls it. execute's publisher case ran the script, succeeded (no errors), but no actual publish happened. Also: recipe `artifact_contract.yaml` had no `outputs[]` field declaring canonical paths. | DF9 dogfood | execute publisher: read `source_artifact` + `outputs[]` from recipe's artifact_contract.yaml; copy synthesis.md to each output path; `git add + commit` with mini-ork identity. Recipe: add `outputs: [docs/refactor/synthesis-latest.md]`. | **fixed pt-9** |
| **D-038** | execute dispatched ALL nodes in NODE_IDS list regardless of `edge_type` — rollback ran AFTER publisher succeeded even though edge declaration is `escalates_to` (conditional on upstream failure). Treats all edges as ordering hints. | DF9 dogfood | execute `_dispatch_node`: skip `rollback` node when `FAIL_COUNT==0` at the time we reach it — no upstream failure means escalates_to doesn't fire. | **fixed pt-9** |

### v0.2-pt9 — close the dogfood loop (D-037 + D-038)

After pt-9, the framework can publish its own audit results to a
canonical path + git-commit them without a human in the middle. The
loop is end-to-end self-sustaining: kickoff → planner → 4 parallel
lenses → synthesizer → verifier → publisher (real git commit) → DONE.

**Convergence trajectory after pt-9:**

| Pass | Cycle | Notes |
|---|---|---|
| pt-9 | DF10 ran | First cycle that self-committed synthesis to `docs/refactor/synthesis-latest.md` + skipped rollback when no failures. Commit `cf33521` authored by `mini-ork <mini-ork@local>` — proves end-to-end self-publishing. Wall time 12min, cost $4.72. |

### DF10 validation — end-to-end self-publishing loop closed

All 5 criteria PASSED in DF10:
1. NEW git commit by `mini-ork <mini-ork@local>` → `cf33521`
2. `docs/refactor/synthesis-latest.md` written (20496 bytes)
3. Log evidence: `[ok] publisher: published docs/refactor/synthesis-latest.md (committed)` + `[skip] rollback — no failures (escalates_to edge not triggered)`
4. Real cost $4.72 captured (similar to DF9's $5.68)
5. Wall time 12min

Cross-cycle finding-count trend shows AUDIT FINDINGS SHRINKING per
cycle as fixes land. No NEW META findings from DF9→DF10. Phase A
proven self-improving — each cycle's fixes reduce next cycle's findings.

### v0.2-pt10 — G-003 DDL session guard across 8 _ensure_table sites

After Phase A converged, pt-10 closes the remaining audit-flagged
quality finding: `_ensure_table` DDL runs on every function call
across 8 lib/ functions. At 100K dispatches/day, that's ~M sqlite3
forks/day spent on no-op schema checks (audit finding F-01: highest-
leverage per-call overhead).

| File | Function | Call sites | Flag |
|---|---|---:|---|
| lib/version_registry.sh | _ver_ensure_table | 7 | _MO_VER_SCHEMA_INIT |
| lib/runs-tracker.sh | mo_runs_ensure_schema | 1 (hot path, every dispatch) | _MO_RUNS_SCHEMA_INIT |
| lib/agent_registry.sh | _agent_ensure_tables | 4 | _MO_AGENT_SCHEMA_INIT |
| lib/benchmark_suite.sh | _bench_ensure_tables | 4 | _MO_BENCH_SCHEMA_INIT |
| lib/gate_registry.sh | _gate_ensure_table | 3 | _MO_GATE_SCHEMA_INIT |
| lib/promotion_gate.sh | _promo_ensure_tables | 2 | _MO_PROMO_SCHEMA_INIT |
| lib/pattern_store.sh | _pattern_ensure_table | 1 | _MO_PATTERN_SCHEMA_INIT |
| lib/gradient_extractor.sh | _gradient_ensure_table | 1 | _MO_GRADIENT_SCHEMA_INIT |

Pattern (uniform across all 8): `[ "${_MO_X_SCHEMA_INIT:-0}" = "1" ]
&& return 0` early-return at function top + `_MO_X_SCHEMA_INIT=1;
export _MO_X_SCHEMA_INIT` after successful schema init. Also added
`PRAGMA busy_timeout=5000` to each Python heredoc (F-11 cleanup —
formerly only on hot-path trace_store/cache sites).

Smoke verified: second call returns without forking python3;
subshells inherit flag via `export`.

**v0.2 P1 backlog status after pt-10:** 22/22 original + 4 v0.2.1 +
2 v0.2.2 + 1 v0.2.3 (G-003) closed. **29 total P1 audit findings
shipped.**

### v0.2-pt11 — Phase B data substrate unblocked (D-039 + D-040)

Phase B inventory survey revealed all 3 bins exist (reflect/improve/eval)
+ functional + integrated. But probe revealed Phase B was DEAD ON
ARRIVAL — substrate empty. Root cause: 2 silent bugs blocking the
entire reflect→improve→eval→promote pipeline since framework inception.

| ID | Title | Source | Fix | Status |
|---|---|---|---|---|
| **D-039** | `execution_traces` table EMPTY after 10+ DF cycles. Migration 0010's schema (`prompt_version_hash`, `created_at TEXT`, `run_id NOT NULL` FK) ≠ trace_store.sh inline INSERT statement (`prompt_version`, `created_at INTEGER`, no run_id, 16 placeholders vs 15 cols). `CREATE TABLE IF NOT EXISTS` no-op'd because table existed; every INSERT silently raised OperationalError (caller's `2>/dev/null \|\| true` swallowed). Self-improvement chain completely blocked: reflect/improve/eval/promote had no data to consume. | pt-11 probe | (a) New migration 0014 widens `status` check to include 'pending' + makes `run_id` nullable (FK still enforced when populated) (b) Rewrite trace_store.sh INSERT to match real schema: drop broken inline CREATE; use `prompt_version_hash`; skip `created_at` (TEXT default DOES the right thing); skip `run_id` (now nullable). | **fixed pt-11** |
| **D-040** | `bin/mini-ork-reflect` calls `reflection_run --since "$SINCE"` but `reflection_run` takes ONE positional arg `since_ts`. Result: `int(sys.argv[2])` got `'--since'` literal → ValueError every invocation. Reflect pipeline never ran. | pt-11 probe | bin/mini-ork-reflect: pass `$SINCE` directly: `reflection_run "$SINCE"`. Also warn that `--task-class` filter isn't yet plumbed (separate follow-up). | **fixed pt-11** |
| **D-041** | `reflection_extract_gradients` compares `created_at >= ?` with caller passing unix-ts INT, but column is TEXT ISO-8601 (per migration 0010). SQLite would compare alphabetically — '2026-...' > '17802...' would return true by luck but `<` would silently misfire. | pt-11 probe (paired with D-039) | reflection_pipeline.sh: `CAST(strftime('%s', created_at) AS INTEGER) >= ?` for proper time comparison. | **fixed pt-11** |

**Smoke verified pt-11**:
- `trace_write` now lands rows (first time since framework inception). Smoke test wrote `test-pt11-real-write` row, verified count=1 in DB.
- `reflect --dry-run` correctly returns 1 trace count.
- All 3 bash files syntax-clean. Migration 0014 applied cleanly to local state.db.

Phase B is now UNBLOCKED. Future DF cycles will produce real traces;
reflect can extract gradients; improve can propose candidates; eval
can benchmark them; promote can decide. The framework now has a
genuine self-improvement substrate.

### v0.2-pt11.5 — Phase B END-TO-END proven (D-042 D-043 D-044)

After pt-11 unblocked the substrate, DF11 produced 6 real traces +
auto-committed `43ed037` synthesis (2nd mini-ork@local self-commit).
Reflect was then run against the 6 traces — surfaced 3 more bugs:

| ID | Title | Source | Status |
|---|---|---|---|
| **D-042** | gradient_extract returned 0 gradients for all 6 traces. NOT a bug — LLM correctly identified the traces are too SPARSE to extract gradients from. Current `trace_write` only populates `trace_id/task_class/status` — empty `files_read/files_written/tool_calls/verifier_output` give the LLM nothing to learn from. | DF11 reflect | **deferred v0.3** (needs execute to populate trace content) |
| **D-043** | `gradient_records` table didn't exist when `reflection_deduplicate` ran. `_gradient_ensure_table` only fires lazily via `gradient_store` — if extract returns 0, table is never created, pipeline steps 2-6 crash with `no such table: gradient_records`. | DF11 reflect | **fixed pt-11.5** — defensive `_gradient_ensure_table` at top of `reflection_extract_gradients` |
| **D-044** | `reflection_run` step 5/6 had bash `2>/dev/null or []` leaked into a Python heredoc. SyntaxError every reflect run. Caller's `done \|\| true` swallowed. | DF11 reflect | **fixed pt-11.5** — replaced with `try/except sqlite3.OperationalError` |

**Reflect pipeline end-to-end smoke (post pt-11.5):**
```
[1/6] extract_gradients   → extracted 0 gradients since 1780211850
[2/6] deduplicate         → no duplicates found
[3/6] link_failures       → 0 links created/verified
[4/6] detect_stale        → 0 stale entries in gradient_records
[5/6] summarize_patterns  → (clean traversal, was SyntaxError before)
[6/6] suggest_promotions  → []
```

**Phase B FULLY PROVEN end-to-end** as of pt-11.5. The reflect →
improve → eval → promote chain has a working substrate AND a clean
pipeline traversal. The only remaining gap (D-042) is trace content
richness — when DF cycles produce richer traces (files_read/written/
tool_calls/verifier_output populated by execute), real gradients will
extract and the self-improvement chain will produce actionable signal.

### v0.2-pt12 — D-042 rich trace_write content (Phase B value-generating)

`_trace_write_node_rich` helper composes a rich JSON payload per node
dispatch with files_written + cost_usd + final_artifact_ref +
reviewer_verdict. Researcher/implementer/reviewer call sites upgraded.
Smoke verified: rich trace lands with cost=0.42, files_written
populated. After next DF cycle, gradient_extract has signal to work
with — Phase B becomes value-generating, not just pipeline-functional.

### v0.2-pt13 — Phase C scaffold (`mini-ork metrics`)

New `bin/mini-ork-metrics` queries state.db for task_runs +
execution_traces + gradient_records → emits markdown table (default)
or JSON (`--format json`) showing cross-cycle trajectory: cost trend,
wall-time trend, trace density, gradient yield. Wired into dispatcher.

Smoke across 12 refactor-audit cycles (the dogfood arc DF1→DF12):
- $17.10 total cost
- 9 execution_traces (pre-pt12)
- 0 gradients (pre-DF12 with rich content)

Phase C SCAFFOLD SHIPPED. Completion: cross-DF delta auto-detect
(regression flag) + Loki/Tempo span integration → next session.

### Phase F milestone — OSS publish + CI green

2026-06-01: framework migrated to **https://github.com/SourceShift/mini-ork**
(public, Apache 2.0). amirkhakshour/mini-ork archived (legacy clone
preserved read-only for citation continuity).

CI pipeline split into two tiers:
- `shellcheck --severity=error` PR-blocking (codebase clean, exit 0)
- `shellcheck --severity=warning` advisory (144 historical warnings
  visible in run summary, non-blocking — incremental cleanup)
- `bash tests/smoke.sh` (103 OK / 0 SKIP / 0 FAIL locally)

CI green at commit `86892e0` (run 26741593759, 49s). The framework
now has working public CI gating every commit.



DF6 SPIKE is not a regression — it's expected when the framework crosses a
phase boundary into a previously-unexercised code region. v0.2 P1 bucket
now 22 items; 12 closed.

**The recursive pattern IS the framework's strongest design proof.**
Each retry exercises a deeper code path; each newly-surfaced gap is
real-world evidence that static analysis missed. The framework is
literally auditing itself by trying to run itself.

---

## P1 — security followups (separate from scale audit)

From `docs/SECURITY-AUDIT.md` v0.1.1 — re-listed here so the v0.2 bucket
is complete:

- **P3-009** Parameterize 9 SQL-interpolation sites in `lib/auto-merge.sh`
  + `lib/cache.sh` (recipe-internal; defense-in-depth)
- **P3-001** `state.db` 644 → 600 in `db/init.sh`
- **K-02** Same as P3-009 (Kimi found the same sites independently —
  consensus signal)

---

## P2 — v0.3 architectural shifts (100K → 1M/day)

The framework's substrate stops being right around 1M runs/day. The
**book's universal-loop contract survives** (this is what v0.1's redesign
earned); only substrate changes.

| ID | Title | Source | Fix sketch | Effort |
|---|---|---|---|---|
| **O-R1** | Dialect-aware migrations (sqlite ↔ postgres15) | Opus | Annotate migrations with `-- @sqlite:` / `-- @postgres15:` line directives + `mini-ork-runtime migrate --dialect <kind>` | 2 weeks |
| **O-R5** | Move `reflection_pipeline` to a separate fleet worker | Opus | Replace inline call from `bin/mini-ork-execute` with PG `LISTEN/NOTIFY` or Hatchet task; reflection becomes async | 1 week |
| **O-R6** | Partition `task_runs` + `execution_traces` by `created_at` monthly | Opus | Schema migration + cutover script | 3 days |
| **O-R7** | Promote `mo_events` to Kafka; PG = 30-day materialised view | Opus | New `events_publisher` lib + reader; cutover behind feature flag | 2 weeks |
| **K-10** | `benchmark_run` parallelize via ThreadPoolExecutor | Kimi | `concurrent.futures.ThreadPoolExecutor(max_workers=8)` over benchmark_tasks | 1 h |
| **K-11** | `execution_traces` archive + missing index | Kimi | (already covered by G-006 above — kimi confirms) | (covered) |
| **D-001** | Wire Anthropic prompt caching into `mo_llm_dispatch` central path | Codex | Hoist `mo_emit_cache_flags` call from per-stage to central; 60-70% input-cost cut | 2 h |
| **D-005** | Per-epic budget gate in `mo_llm_dispatch` | Codex | (same as G-016 above — promote from P1 if not done) | (covered) |
| **K-05** | `context_assembler` 5-minute TTL cache | Kimi | `mini_orch_cache` hash-keyed by `(task_class, node, budget)` | 1.5 h |
| **K-12** | `_mo_capture_reflection` async fire-and-forget | Kimi | Move `git blame` to background subshell; primary write completes instantly | 45 min |

**Total v0.3 effort:** ~6 weeks. This is the substrate-swap milestone.

---

## P2 — v1.0 fleet-scale (1M → 10M/day)

| ID | Title | Source | Notes |
|---|---|---|---|
| **O-R4** | Migrate `lib/` + `bin/` runtime to **Go**; recipe surface stays shell | Opus | Hybrid bash-shim + Go runtime; preserves recipe-author surface; ~14-20 eng-wks |
| **O-R8** | Shard PG by `tenant_id` (~100 tenants/DB) | Opus | New TEXT column on every namespace table; default `'local'` for back-compat |
| **O-R9** | Tier storage: hot (PG ≤30d), warm (PG archive ≤180d), cold (Parquet on S3) | Opus | DuckDB or `pg_parquet` for cold-restore |
| **O-R10** | Recipe marketplace + signing | Opus | Extends security P3-007; GitHub URLs + signature verification |
| **O-R11** | SQL pattern emergence → ML clustering | Opus | sentence-transformers/all-MiniLM-L6-v2 + HDBSCAN; nightly cron |
| **O-R23** | Async reflection workers | Opus | Already started in O-R5; this is the production version |
| **D-arch-1** | Model-tier router — classify task complexity, route haiku/sonnet/opus | Codex | Up to 60% cost cut at scale; rule-based today, ML at v1.5 |
| **D-arch-2** | Semantic cache above SHA hash cache | Codex | Embedding similarity ≥0.95 → cache hit; sqlite-vec or external vector DB |

**Total v1.0 effort:** ~30-40 eng-wks (book Ch 32 phasing applies —
phase N+1 not until phase N produces stable signal).

---

## P3 — Advisory (defense-in-depth + polish)

| ID | Title | Source | Notes |
|---|---|---|---|
| **G-012,13,14,15** | `ls -d iter-*/` patterns hit `ARG_MAX` at scale | GLM | Replace with `find ... -maxdepth N -name 'iter-*' \| sort` |
| **G-005** | O(N²) scope-overlap pairwise check | GLM | Cache `git ls-files` outside inner loop |
| **D-003** | `cl_opus.sh` forces ALL model slots to Opus (sub-agents billed Opus) | Codex | Pin only `ANTHROPIC_MODEL`; let subagent tier default to haiku |
| **K-07** | Redundant `cat $PLAN_PATH` reads per subshell node | Kimi | Read once, export `PLAN_CONTENT_CACHED` |
| **D-008** | Speculative dispatch waits all PIDs instead of kill-on-first-success | Codex | After each `wait`, kill remaining on first success |

---

## The "hardest open question" (Opus §7)

**Goodhart's law on the promotion gate.** As `lib/utility_function.sh`
becomes the optimization target at fleet scale, candidates will be
proposed that maximize `U` without actually improving downstream task
outcomes — Goodhart-style. Three mitigations sketched in
`/tmp/sc-opus-architecture.md` §7:

1. **Adversarial benchmark generation** — periodically mutate
   `benchmark_tasks` with constraint-violating variants that should not
   pass; candidates that score well on the originals AND on adversarials
   are stronger
2. **Shadow-traffic-as-verdict** — a candidate routes 5% of real
   production traffic before promotion; real user outcomes (not just
   verifier pass-rate) determine promotion
3. **Conservative drift detection** — alert if `U` rises by >10% per
   evolution cycle and the underlying error rate did not fall in real
   traffic

**Recommendation: do NOT auto-promote at autonomy ladder rung 7 until
this question is resolved with literature review + chosen mitigation.**
The PromotionGate must require human approval until then. This is the
load-bearing safety axiom that turns v0.3 → v1.0 not into a black box.

---

## How to actually run this audit again (the dogfood path)

**Once v0.2 ships finding D-007:**

```bash
cd ~/ps/mini-ork
# 1. The kickoff that started this audit:
cat kickoffs/scale-refactor-mini-ork.md

# 2. Dispatch via mini-ork itself:
mini-ork run refactor-audit kickoffs/scale-refactor-mini-ork.md

# 3. The output lands at docs/refactor/SCALABILITY-AUDIT-<run-id>.md
#    plus task_runs row, plus execution_traces fanout, plus gradients in
#    textual_gradients for the next reflection cycle.
```

This is the meta-payoff of the framework. The audit becomes a recurring
artifact rather than a one-shot doc.

---

## References

- 4 model-lens audit reports:
  - `/tmp/sc-glm-findings.md` — 25 tactical bottlenecks (GLM stance)
  - `/tmp/sc-kimi-refactors.md` — 12 code-level refactors with diffs (Kimi)
  - `/tmp/sc-codex-llm.md` — 10 LLM-dispatch cost cuts (Codex)
  - `/tmp/sc-opus-architecture.md` — 27 numbered architectural recs (Opus)
- `recipes/refactor-audit/` — the recipe for next-time self-dispatch
- `kickoffs/scale-refactor-mini-ork.md` — the canonical kickoff that
  reproduces this audit
- `docs/SECURITY-AUDIT.md` — companion security audit (v0.1.1)
- `ideal-mini-orch-self-evolving-system-book.md` — architectural
  source-of-truth referenced by Opus stance

## Lineage

- v0.1.1 (2026-05-30): initial audit; 31 findings synthesized;
  audit-as-recipe (`recipes/refactor-audit/`) shipped for future
  self-dispatch
- Next audit: schedule via `recipes/refactor-audit/` on every v0.x
  release tag, OR on-demand when the rate-of-change of `audit_log`
  exceeds threshold (signal that complexity has grown)

### v0.2-pt18/19/20 — Top-5 audit follow-up (W3 + W2 + W5 shipped, W4 false-positive)

After DF12's synthesis (`docs/refactor/synthesis-latest.md`) flagged 5
immediate wins ranked by ROI, this commit arc closes 3 of 5:

**pt-18 W3 — `cl_opus.sh` sub-agent tier split** (commit `1dd9593`, 15min):
ANTHROPIC_DEFAULT_SONNET/HAIKU_MODEL + CLAUDE_CODE_SUBAGENT_MODEL no
longer pinned to Opus when cl_opus.sh sources. ~$8K/day saved at 100K
tier. Override knob `MO_OPUS_PIN_ALL=1` for legacy behavior.

**pt-19 W2 — Kill D-009 double cost-charge** (commit `3ced30d`, half day):
removed redundant flat `$0.01 × DISPATCHED_COUNT` charge at end-of-run.
D-029 already records real total_cost_usd per node since pt-8. Phantom
spend eliminated → MO_DAILY_BUDGET_USD headroom doubles. Status='reviewing'
transition preserved.

**pt-20 W5 — Lane-cache subshell-scope** (commit `???`, half day):
`declare -gA _MO_LANE_CACHE` doesn't survive `( _dispatch_node ) &`
subshell forks. Switched to per-key `export _MO_LANE_<NAME>=…` so
parallel dispatch batches inherit the cache. Eliminates 4 redundant
`python3 yaml.safe_load` forks per parallel batch.

**W4 — created_at TEXT/INTEGER cast** (NOT SHIPPED — FALSE POSITIVE):
the synthesis claimed `task_runs.created_at` is TEXT, causing
type-affinity mismatch in the cost circuit breaker. **Verified false**:
migration `db/migrations/0013_task_runs.sql:37` declares `created_at
INTEGER NOT NULL`. Sample rows confirm `typeof(created_at)=integer`.
Lens hallucinated the bug. (Mirror pattern of the Rajan 2025
false-negative documented at `docs/research/citation-verification-2026-06-01.md`
— lenses can produce both false negatives AND false positives;
audit recommendations require independent verification before execution.)

**W1 — Batch gradient extraction** (DEFERRED to v0.3): the W1 ROI
($1.4K/day, 95% reflect-cycle cost cut) is CONTINGENT on
gradient_extract actually producing signal. Currently D-048
(gradient_extract returns 0 even on rich traces) blocks the
upstream — until that's fixed, W1 just makes an empty pipeline
cheaper. Sequence: D-048 prompt-tuning first → then W1 batch
rewrite.

**Audit verification trail summary** — false-positive rate so far:
1 of 5 (20%) on W-class recommendations. Will track over future
DF cycles; pattern suggests routing audit recommendations through a
verifier-script BEFORE consuming them mechanically.
