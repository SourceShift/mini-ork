# Self-migration: the core-feature manifest + acceptance probes

_Written 2026-07-18. Defines what "the bash→Python migration is done **correctly**" means:
not "unit tests pass" but "**every feature mini-ork promises still works end-to-end**."_

## Why this document exists first

The migration's unit-level oracle is perfect and free: `bash_fn(real.db)` vs `native_fn(real.db)`,
byte-for-byte. But unit-parity is **necessary, not sufficient**. This session's cross_epic rewire
passed unit-parity yet leaked a stray `0` into reflect's stdout — only the *end-to-end* reflect
probe caught it. So the migration's real finish line is a **feature-acceptance suite**: one
replayable probe per promised feature, run as the migration recipe's final gate. This doc is that
suite's specification. Everything else (the recipe, the observe→improve loop) gates on getting
this list right — hence it comes first, for sanity-check.

## The competitive advantage this protects

mini-ork's moat is **verification** — nothing ships unless the verify-gate says so. The migration
is the ideal proving ground because the bash lib is an un-gameable oracle. But the *thing being
protected* is the promise that a user's run is **verified-correct**, and that promise is only kept
if the migrated engine still delivers every feature below. The acceptance suite is the moat applied
to mini-ork itself.

## The 9 core promised features + their acceptance probes

Each probe is **end-to-end** (exercises the feature through its real entrypoint) and **replayable**
(a command or short recipe). A feature is "migrated correctly" only when its probe is green on the
Python runtime **and** byte-matches the bash runtime where a bash reference still exists.

| # | Promised feature | Real entrypoint | Acceptance probe (end-to-end) |
|---|---|---|---|
| 1 | **6-stage loop** classify→plan→execute→verify→reflect→improve | `bin/mini-ork run` | Run `examples/01-hello-world/kickoff.md`; assert every stage emits its artifact (classification.json, plan, execute log, verdict, reflection) and the run reaches a terminal state. |
| 2 | **Recipe execution** (forced recipe → its contract) | `bin/mini-ork run <recipe>` | Run `code-fix` on a seeded red→green fixture; assert the artifact_contract is satisfied. Run `framework-edit` on a 1-line kickoff; assert it produces a **diff, not a commit**. |
| 3 | **Heterogeneous lanes + `learning_governed` routing** | `decision_service.decide()` | `MO_VALIDATE_DO_RUNS=1 MO_LEARNING_MIN_SAMPLES=1 scripts/learning-loop-live-validate.sh 1` — assert the router returns the static default BEFORE and the learned lane AFTER. |
| 4 | **Cost governance** (budget cap + circuit breaker) | `budget_gate`, `cost_pause` | Start a run with a $0.01 per-run cap; assert it halts at the cap and writes the pause sentinel — does not silently overspend. |
| 5 | **Runtime verify-gate** (the moat) | `bin/mini-ork verify` / `reviewer_gate` | Feed a change that is red-on-base + green-on-gold → assert **pass**. Feed a change that is green-by-coverage-gap (a genuine cheat) → assert **reject**. (Precision on hard negatives, not just apply_fail.) |
| 6 | **Learning loop closes** (reward → routing) | `scripts/learning-loop-closure-gate.sh` | Run the closure gate; assert a real trace writes reward, GRPO recomputes advantage, and the next `decide()` reads it. |
| 7 | **`framework-edit` self-modification** (propose-not-commit, blast-radius scope, rollback) | `recipes/framework-edit` | Run a self-edit kickoff; assert scope_gate **blocks** an out-of-scope file, the change lands as a reviewable diff, and rollback restores cleanly. |
| 8 | **Durable / resumable runs** (checkpoints) | `--resume` | Kill a run mid-step; `--resume`; assert it resurrects at the checkpoint (STEP or TURN) without re-executing completed nodes (idempotency + tool receipts). |
| 9 | **Multi-epic delivery** (epics + scheduler) | `bin/mini-ork epics split` / `scheduler` | Split a 3-epic roadmap with a dependency; assert the scheduler dispatches in dependency order and does not drain unrelated epics. |

> Probes 3, 5, 6 are the **moat probes** — they prove the *verified-correctness* promise itself.
> If the migration breaks these, it has broken the product regardless of unit-parity.

## Migration strategy: integration-points first, root → down, one frontier

_Revised 2026-07-18 per the "less deterministic, integration-points first" direction._

The bottom-up leaf approach **splits** integration points: it makes a Python module native but
leaves the paired bash entrypoint (and every lib/script/test/UI ref to it) still live — a scatter
of half-open seams with no way to know which are complete. Replace it with a **single advancing
frontier**:

- **Unit of work = a fork** (an entrypoint's Python↔bash seam), NOT a lib. A fork is "closed"
  only when **every** integration point is resolved:
  - **outbound** — what the Python entrypoint shells to (`lib/X.sh` calls), and
  - **inbound** — every lib, script, test, sandbox, and **web-UI route** that invokes the bash
    entrypoint `bin/mini-ork-<cmd>`.
  Then the bash entrypoint retires and the `runtime-select` fallback for that command drops.
- **Order = root → down.** Dispatcher/`cli` first, then `classify → plan → execute → verify →
  reflect`, closing each fork completely before descending, so a caller is never migrated after
  the thing it calls is already gone.
- **Above the frontier: pure Python. Below: pure bash. Nothing half-open.**

**Why forks, not libs — measured 2026-07-18:** closing the *easiest* fork (`classify`, whose Python
side is already native) still requires resolving **8 integration points**: `lib/llm-dispatch.sh`,
`lib/sandbox/local.sh`, `mini_ork/web/routes/run_detail.py` (the UI), `scripts/runtime-parity-harness.sh`,
`tests/unit/test_mini_ork_classify_py.py`, three `tests/security/*.sh`, and `tests/integration/test_bin_classify.sh`.
Any one of those, if missed, is a dangling reference to a retired entrypoint. Only a **complete
integration-point map, resolved as a set**, prevents that.

### Step 0 — the integration-point map (the enforceable safeguard)

Before any transform: compute every Python↔bash edge and every inbound reference to each
`bin/mini-ork-<cmd>`. This map IS the "don't leave anything incomplete" guarantee — a fork can't be
declared closed while any edge into it survives. Read-only; produced first.

### Lane policy (per 2026-07-18 decision)

| Loop role | Lane | Backoff | Rationale |
|---|---|---|---|
| implementer / worker (the code transform) | **codex** | codex | reliable in this env; implementer must never be GLM (analysis-only) or opus |
| mapper / verifier / reviewer (judgment — the moat) | **opus** | codex | strongest reasoner maps each seam's contract + decides "did parity hold / did a feature break" |
| discovery lens (non-critical) | **GLM** | codex | cheap analysis; GLM's 429 "Fair Usage" silently sinks runs, so codex-backoff is mandatory |

### Per-fork pipeline (less deterministic — the agent reasons per seam)

1. **Map the seam** (opus) — from Step 0's graph: every outbound shell-out + every inbound ref to
   this entrypoint (libs, scripts, tests, sandbox, UI). Reason about each edge's contract (env
   passed, side-effects, stdout capture) — not a fixed template.
2. **Static-feature ledger** (opus — REQUIRED OUTPUT, see below) — before/while porting, catalog
   every function/behavior in this part and deliberately classify it static vs agentic. This is the
   cost/verifiability audit, not bookkeeping.
3. **Make the Python side sole** (codex) — port/rewire every outbound seam to native
   (AST-verify the port is native first; preserve `_bash_lib_call`'s `|| echo 0` error-swallowing
   AND its stdout capture — a printing port leaks into caller stdout, see cross_epic).
4. **Repoint every inbound ref** (codex) — tests → Python-only (convert bash-oracle parity tests to
   standalone), UI/lib/script refs → the Python entrypoint or its module.
5. **Verify** (the moat, made concrete): (a) byte-parity vs the bash oracle on the **live** state.db
   while it still exists · (b) pytest + pyright 0 · (c) a **real run** · (d) the entrypoint's
   **feature-acceptance probe** from the manifest above.
6. **Close the fork** — retire `bin/mini-ork-<cmd>` + drop its `runtime-select` fallback. All gates
   or nothing lands.

## The static-feature ledger — the migration's strategic payload

This is why the migration is worth more than a port. mini-ork's moat is **cost-down at constant
verified correctness**, and those two goals share one root cause:

| Kind | Cost | Verifiability |
|---|---|---|
| **static** (deterministic logic) | ~0 tokens | **un-gameable** — byte-parity vs an oracle, the strongest verification |
| **agentic** (LLM call) | tokens per call | **weak** — LLM-judge is gameable (single-test execution-anchoring gets hacked through coverage gaps) |

So each fork migration MUST emit a ledger classifying every behavior it touches. One row per
function/feature:

| feature | class | verifiability | cost | decision / opportunity |
|---|---|---|---|---|
| `aggregate_win_rates` (rho) | **static** (sqlite) | byte-parity (high) | ~0 | keep static — a unit of the moat |
| `lane_router_*` | **static** (sqlite, 588L) | byte-parity (high) | ~0 | confirm + keep static |
| `gradient_extract` | **agentic** (shells Claude) | LLM-judge (weak) | $$ / call | **cost-down candidate**: make template/deterministic, or gate with a deterministic check |
| `<a runtime-select fork>` | **integration** | — | — | note whether the seam itself is static or agentic |

- **static** confirmed → a unit of the moat proven (cheap + hard-verifiable).
- **agentic** flagged → a cost *and* verifiability liability, and a candidate to push the RIGHT
  direction (agentic→deterministic lowers cost and raises verifiability at once). Pure static→agentic
  is almost never right — it spends tokens and weakens the gate.
- **integration** → a seam; record whether it carries an LLM or is pure plumbing.

**The aggregate ledger = mini-ork's cost/verifiability map**, and the flagged agentic rows = the
cost-down roadmap. The migration produces it as a by-product of touching every part with an oracle
in hand — the one time the whole engine gets read deliberately, function by function.

## The observe → improve loop (why this compounds)

Every gate failure is a **verifier gap**, and fixing it hardens mini-ork for *all* jobs, not just
migration:

| Issue observed this session | Verifier gap | Improvement (general, not migration-specific) |
|---|---|---|
| lib deleted while `bin/` still called it | discover stage missed `bin/` | `bin/` added to the retirement blocker-check |
| unit-parity passed, stdout leaked | unit-parity too shallow | the end-to-end probe is now a **required** gate |
| differential test flaked (shared-db `bug_report_sweep`) | verifier's test isolation weak | per-implementation db copies |
| now/now-epoch boundary race | time-relative tests un-buffered | seed now-offset timestamps |

This is the compounding curve: the migration is the cheap, oracle-backed workload that *pays for*
verifier hardening; the hardened verifier then does the next un-shelling — and the next non-migration
job — more reliably. The migration doesn't just finish; it makes the thing that finishes it better.

## Open question for sanity-check

Are these the right **9 features**, or is a promised capability missing (e.g. trace capture /
observability as a standalone feature, or the HarnessBridge / cross-runtime story)? The recipe and
the acceptance-probe scripts both gate on this list, so it's worth pinning before building them.
