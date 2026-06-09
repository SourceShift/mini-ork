# Synthesis — Recursive Self-Improvement, iter 19

## Ranked patch plan

| Rank | Bottleneck | Category | Patch summary | Evidence | Confidence |
|---|---|---|---|---|---|
| 1 | Planner LLM dispatches unconditionally when `run_profile.profile_status=needs_answers` | correctness | Add pre-dispatch profile gate in `bin/mini-ork-plan` that emits a deterministic `plan_status=needs_answers` artifact and skips `llm_dispatch` when profile is unready | lens-bottleneck.md #3; lens-correctness.md Bug 3; lens-arch.md candidate 2; arXiv 2605.07062 (control-plane authority boundaries) | 0.89 |
| 2 | Worktree base-branch drift: iter-N forks from prior-iter audit tip, not `main` HEAD; landed fixes invisible to next iter | arch | In `bin/mini-ork-self-improve`, resolve base to `main` (override via `MINI_ORK_SELF_IMPROVE_BASE_REF`) before `git worktree add`; preflight `git fetch --quiet` when remote exists | lens-bottleneck.md #1; lens-correctness.md Bug N2; lens-arch.md candidate 1; arXiv 2605.07062, 2601.11647 | 0.86 |
| 3 | `_self_improve_record_success` indiscriminately flips every `deferred` row to `superseded` on any successful commit | correctness | Filter the UPDATE by `category` match OR `evidence_paths` overlap with `git diff --name-only`; add `updated_at < ts - 7*86400` decay fallback | lens-bottleneck.md #2; lens-correctness.md Bug 2; lens-arch.md candidate 4; arXiv 2605.07242 (barrier-first cascade repair) | 0.82 |
| 4 | Synthesis → `learning_record` promotion gap: iter-18 produced 5 ranked patches; only 1 (success-meta) reached the table | arch | Add a synthesis-patch parser to `bin/mini-ork-self-improve` `_self_improve_record_success` that inserts `outcome='open'` rows for lower-ranked patches keyed by stable title hash | lens-bottleneck.md #6; lens-arch.md candidate 4; arXiv 2511.06179 (MemoriesDB structured promotion), 2605.15815 (BootstrapAgent verifiable knowledge contracts) | 0.78 |
| 5 | `llm_dispatch` call sites do not capture `duration_ms`; no per-node latency signal exists in trace path | perf | In `lib/llm-dispatch.sh`, emit a `.last-llm-duration` sidecar on the success path; thread it into `_trace_write_node_rich` and `node_runs.duration_ms` column | lens-perf.md C2; learning_record.id=3 (open, 18 iters stale); arXiv 2605.08563 (CCRM context-contamination retries) | 0.72 |

## Top patch — detailed plan

### Patch 1: Pre-dispatch profile gate in `mini-ork-plan`

**Problem statement.** `bin/mini-ork-plan` reads `profile_status` and `confidence` into plan metadata but dispatches the planner LLM unconditionally. When the profile is `needs_answers` (as in this very iteration's run_profile), the planner burns ~$0.05/call producing a plan against missing success criteria. iter-15/16/17 execute.logs show 3 identical cascade failures in 14 seconds (timestamps 081624 / 081629 / 081633) — each a separate $0.05 charge against an under-specified profile.

**Evidence.**
- `bin/mini-ork-plan:200-211` — `profile_status` and `confidence` read into plan metadata only.
- `bin/mini-ork-plan:218` — single, unconditional `llm_dispatch` call. `grep -n llm_dispatch bin/mini-ork-plan` returns exactly one hit.
- This run's `plan.json:risk_notes[5]` explicitly notes `success_criteria empty in profile (profile_status=needs_answers)`.
- iter-15/16/17 cascade evidence: `runs/self-improve-iter-1{5,6,7}-*/execute.log` timestamps 081624 / 081629 / 081633.
- learning_record state: not yet promoted (carry-forward of iter-18 Patch 3, rank #3, conf 0.85, NOT landed on `main` — see lens-bottleneck.md table rows 107-112).
- arXiv 2605.07062 (Barnes, 2026) — control-plane authority boundaries: profile completeness must sit before provider dispatch, not after.

**Proposed change.** In `bin/mini-ork-plan`, immediately before line 218 (`PLAN_JSON_RAW=$(llm_dispatch ...)`), insert a gate block:

```bash
# Profile gate (Patch 1, iter 19): block planner dispatch when run_profile is under-specified.
# Override with MINI_ORK_PROFILE_GATE=0 for back-compat / exploratory use.
PROFILE_GATE="${MINI_ORK_PROFILE_GATE:-1}"
CONFIDENCE_FLOOR="${MINI_ORK_PLAN_CONFIDENCE_FLOOR:-0.7}"
if [ "$PROFILE_GATE" = "1" ]; then
  _gate_block=0
  if [ "$profile_status" = "needs_answers" ]; then _gate_block=1; fi
  if awk "BEGIN{exit !($confidence < $CONFIDENCE_FLOOR)}"; then _gate_block=1; fi
  if [ "$_gate_block" = "1" ]; then
    python3 - "$PLAN_OUT_PATH" "$profile_status" "$confidence" "$human_questions_json" <<'PY'
import json, sys
out, status, conf, hq = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
plan = {
  "plan_status": "needs_answers",
  "blocked_by": "run_profile",
  "profile_status": status,
  "confidence": float(conf),
  "human_questions": json.loads(hq) if hq else [],
  "decomposition": [],
  "dependencies": [],
  "objective": "blocked: profile incomplete",
}
with open(out, "w") as f: json.dump(plan, f, indent=2)
PY
    echo "{\"plan_status\":\"needs_answers\",\"blocked_by\":\"run_profile\"}"
    exit 0
  fi
fi
```

Files touched: `bin/mini-ork-plan` only (~30 LOC inserted before `:218`). No schema change. No new tables. Reuses the existing `plan.json` shape with one optional `plan_status` key.

**Regression test.** Add `recipes/recursive-self-improve/verifiers/profile-gate.sh` plus a fixture run_profile at `tests/fixtures/run_profile-needs-answers.json`. The verifier asserts:

1. `MINI_ORK_PROFILE_GATE=1 MINI_ORK_PROFILE_PATH=<fixture> bin/mini-ork-plan` exits 0.
2. The resulting `plan.json` contains `"plan_status": "needs_answers"` AND `"blocked_by": "run_profile"`.
3. `node_runs` table contains NO row for the planner node with `lane LIKE '%opus%' OR '%codex%'` for this run_id (no LLM dispatch occurred).
4. Inverse fixture (`profile_status=ready`, `confidence=0.9`) DOES produce an `llm_dispatch` row in `node_runs` — guarding against the gate over-firing.

Assertion text for primary check: `assert plan["plan_status"] == "needs_answers" and plan["blocked_by"] == "run_profile" and not any(r["lane"] in ("opus","sonnet","codex") for r in node_runs)`.

**Verification.** Pre-existing tests that must keep passing: `recipes/recursive-self-improve/verifiers/no-regression.sh` (utility-delta gate from `f8967b1`), `recipes/recursive-self-improve/verifiers/bottlenecks-found.sh`, the full `tests/test_mini_ork_plan_*.py` suite if present (`grep -rn test_mini_ork_plan tests/ || true`). Expected benchmark delta: per-spiral planner cost drops from ≈$0.25 (5 × $0.05) to ≈$0.00 on the under-specified path; expected magnitude ≈$0.20 saved per spiral. Wall-clock saving: ~14s per cascade (the iter-15/16/17 burn time).

**Rollback criteria.** Discard this patch if:

- Any pre-existing recipe verifier flips from `pass` to `fail` after the gate lands.
- `MINI_ORK_PROFILE_GATE=1` causes ≥1 legitimate (`profile_status=ready` AND `confidence >= 0.7`) plan to be blocked across a 5-iter shakeout.
- The gate produces a `plan.json` shape that breaks `bin/mini-ork-execute` parsing (manifested as `node_runs` rows missing `plan_id` or as decomposition deserialization errors).
- A planner-confidence floor of 0.7 is shown to gate >10% of historical valid plans in `node_runs` replay.

## Lower-ranked patches

### Patch 2: Resolve worktree base to `main` (with explicit override)

**Problem.** `bin/mini-ork-self-improve:111` reads `PARENT_BRANCH=$(git rev-parse --abbrev-ref HEAD)`; `:251` calls `git worktree add -b "$branch" "$wt_path" "$PARENT_BRANCH"`. Result: iter-19 worktree HEAD = `a5b29b4` (iter-18 audit tip), but `main` HEAD = `f8967b1` (utility-delta gate landed AFTER iter-18 published). Iter-19 sees the dead `bench_delta_ok=1` line and re-discovers a closed issue. Manifests as lens-correctness.md Bug N2 (worktree stale verifier).

**Evidence.** lens-bottleneck.md #1; lens-arch.md candidate 1; reproduction: `diff <(git show main:recipes/recursive-self-improve/verifiers/no-regression.sh) recipes/.../no-regression.sh` — non-empty in current worktree. arXiv 2605.07062 (control-plane authority for base selection), 2601.11647 (workflow-base scoring).

**Change.** Replace `PARENT_BRANCH` resolution at `:111` with:

```bash
MINI_ORK_BASE_REF="${MINI_ORK_SELF_IMPROVE_BASE_REF:-main}"
if git rev-parse --verify --quiet "refs/remotes/origin/$MINI_ORK_BASE_REF" >/dev/null 2>&1; then
  git fetch --quiet origin "$MINI_ORK_BASE_REF" || true
fi
PARENT_BRANCH="$MINI_ORK_BASE_REF"
if ! git rev-parse --verify --quiet "$PARENT_BRANCH" >/dev/null; then
  PARENT_BRANCH=$(git rev-parse --abbrev-ref HEAD)
  echo "warn: base ref '$MINI_ORK_BASE_REF' not found; falling back to current branch '$PARENT_BRANCH'" >&2
fi
```

**Test.** Verifier creates an empty branch ahead of `main`, calls `bin/mini-ork-self-improve --dry-run`, and asserts the resulting worktree's `git rev-parse HEAD` equals `git rev-parse main`. Inverse: with `MINI_ORK_SELF_IMPROVE_BASE_REF=feature/x`, verify the override wins.

**Rollback.** Discard if any quarantined experimental branch needs the old current-branch-tip behavior and the env override is insufficient.

### Patch 3: Filter `deferred → superseded` UPDATE by category + evidence-path overlap

**Problem.** `bin/mini-ork-self-improve:175-179` unconditionally flips every `outcome='deferred'` row to `superseded` on any successful commit. Today the bug is dormant (0 deferred rows), but the next loop-produced deferral will be silently superseded by any unrelated success. lens-bottleneck.md #2; lens-correctness.md Bug 2; arXiv 2605.07242 (barrier-first cascade repair).

**Change.** Replace the unfiltered SQL with a Python helper that:

1. Reads `git diff --name-only HEAD~1 HEAD` for the success commit.
2. For each `outcome='deferred'` row, computes intersection of `evidence_paths` (JSON array) with the diff set.
3. Marks `superseded` only when intersection is non-empty OR the row's `category` matches the new commit's category (passed in by `_self_improve_record_success`).
4. Adds a 7-day decay fallback: rows with `updated_at < ts - 7*86400` AND zero overlap age to `superseded` with reason `decay`.

**Test.** Synthetic reproduction from lens-correctness.md (lines 88-121): seed 2 deferred rows, commit touching only `a.sh`, assert row pointing to `b.sh` stays `deferred`.

**Rollback.** Discard if backlog of deferred rows grows unbounded (>50 rows) after 5 iterations.

### Patch 4: Synthesis → `learning_record` promotion hook

**Problem.** lens-bottleneck.md #6: iter-18 published 5 ranked patches; only the success-meta row reached `learning_record`. Patches 2-5 exist only in `synthesis.md` text. Every future SQL-based dedupe scan silently re-emits them as novel. lens-arch.md candidate 4; arXiv 2511.06179 (MemoriesDB), 2605.15815 (BootstrapAgent contracts).

**Change.** Add `_self_improve_promote_synthesis_patches` to `bin/mini-ork-self-improve`. Called from `_self_improve_record_success` after the success-meta row insert. Logic:

1. Parse `${RUN_DIR}/synthesis.md` for the `## Ranked patch plan` table (regex on `| <rank> | <bottleneck> | <category> | <summary> | <evidence> | <conf> |`).
2. For each row ranked ≥ 2, compute `title_hash = sha1(category + bottleneck_title)`.
3. `INSERT OR IGNORE INTO learning_record(run_id, iter, rank, category, title, evidence_paths, arxiv_refs, patch_summary, outcome, severity, confidence, ..., title_hash)` with `outcome='open'`. Title_hash is a uniqueness key — preserve as an indexed column.
4. On parse failure, log to `${RUN_DIR}/promotion.err` and fall through to current success-meta-only behavior.

**Schema.** Add `title_hash TEXT` column to `learning_record` via a new migration `db/migrations/0019_learning_record_title_hash.sql`. New infra is justified by arXiv 2511.06179 (structured promotion) — paper present in `lens-arxiv.md`.

**Test.** Run `bin/mini-ork-self-improve` against a fixture run dir whose `synthesis.md` has 3 ranked patches. Assert `SELECT COUNT(*) FROM learning_record WHERE run_id = <run> AND outcome = 'open'` returns 2 (ranks 2 and 3, not the top patch which is being landed). Re-run; assert count stays 2 (idempotence via `title_hash`).

**Rollback.** Discard if the parser produces false-positive promotions (rows that don't match a real ranked patch) that pollute the dedupe table.

### Patch 5: `duration_ms` capture for `llm_dispatch`

**Problem.** `lib/llm-dispatch.sh` has zero success-path latency capture. learning_record.id=3 has tracked this 18 iterations open. lens-perf.md C2; arXiv 2605.08563 (CCRM, cited in iter-18 synthesis).

**Change.** First, probe: `sqlite3 state.db ".schema node_runs"` — if `duration_ms` column already exists, scope collapses to ~15 LOC (just wire it). If not, add via migration plus emit in `lib/llm-dispatch.sh` success branch:

```bash
_t0_ms=$(python3 -c 'import time; print(int(time.time()*1000))')
# ... existing dispatch ...
_t1_ms=$(python3 -c 'import time; print(int(time.time()*1000))')
echo "$((_t1_ms - _t0_ms))" > "$RUN_DIR/.last-llm-duration"
```

Then in `bin/mini-ork-execute:_trace_write_node_rich` (`:300-340`), read `.last-llm-duration` next to `.last-llm-cost`. Wrap 3 call sites at `:473`, `:510`, `:566`.

**Test.** `recipes/recursive-self-improve/benchmark_tasks/latency-trace-completeness.json` — run a 3-node research → implement → review flow; assert every `node_runs` row has `duration_ms IS NOT NULL`, `>= 100`, `<= 300000`.

**Rollback.** Discard if measurement overhead exceeds 2% of dispatch time (unlikely; bash `python3 -c` is ~30-40ms).

## Convergence assessment

**Not converging.** Two structural defects compound across every iteration until closed:

1. **Worktree base-branch drift (Patch 2)** — landed patches are invisible to the next iter, so the loop re-discovers closed issues. iter-19 demonstrates this concretely: the worktree's stale `no-regression.sh` carries the dead `bench_delta_ok=1` while `main` already has the fix from `f8967b1`.
2. **Synthesis → `learning_record` promotion gap (Patch 4)** — every future bottleneck-scan that dedupes via SQL silently re-emits all non-top iter-N patches as novel. iter-18 produced 5 ranked patches, 1 reached the table; the other 4 reappeared in this iter's scan.

Until both close, every loop iteration partially re-discovers prior work AND has an incomplete dedupe surface. The outer loop should NOT terminate. Recommend prioritizing Patch 1 (active cost burn, simplest fix, highest correctness leverage) for this iteration, then Patch 2 (drift) for iter-20, then Patch 4 (promotion) for iter-21. After all three land, re-assess convergence — at that point the loop's memory will actually drive its behavior.

## Provenance footer

- Lenses consumed: minimax (perf), kimi (correctness), codex (arch + arxiv + bottleneck).
- Synthesizer family: opus.
- arXiv papers cited: 6 directly in patch evidence (2605.07062, 2601.11647, 2605.07242, 2511.06179, 2605.15815, 2605.08563). 6 additional papers available in `lens-arxiv.md` for lower-ranked / future work (2605.03675, 2605.06527, 2604.15877, 2604.13102, 2604.00917, 2605.08017).
- Cross-iteration learnings applied: 5 rows from `learning_record` (3 resolved excluded: id=1, id=4 via `f8967b1`, id=5; 2 open consulted: id=2 surfaced via Patch 4 framing, id=3 surfaced as Patch 5).
- Source bottleneck-scan: `lens-bottleneck.md` (8 ranked rows, 5 novel + 4 carry-forward of un-landed iter-18 patches).
- Arch + arXiv lens artifacts were written to the iter-19 worktree at `/Volumes/docker-ssd/ps/mini-ork/.mini-ork/worktrees/iter-19-20260609095403/lens-{arch,arxiv}.md` due to a run-dir write sandbox denial; synthesis consumed them at those paths.
