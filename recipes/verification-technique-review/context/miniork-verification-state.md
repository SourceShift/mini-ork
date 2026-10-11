# mini-ork verification & anchoring — state snapshot (2026-10-05)

Hand-maintained input for the impact reviewers. Point `MO_VERIFICATION_STATE_FILE`
(or the legacy `MO_RSI_STATE_FILE`) at a newer copy to refresh it without editing
the recipe. Anchors are `module:line` in the mini-ork repo; see
`docs/reference/FEATURE-INVENTORY.md`.

The governing doctrine, stated once and enforced across the stack: **a verdict is
anchored on what the code ACTUALLY did, never on what a model said about it. An LLM
judge may only VETO, never APPROVE.** (`mini_ork/runtime/__init__.py`,
`mini_ork/runtime/engine.py:58`, PR #168.)

## Shipped and acting (the loop uses these today)

- **Execution-anchored runtime (Crucible)** — `mini_ork/runtime/engine.py`. Runs a
  candidate in an isolated runtime (docker / subprocess / prime / modal via the
  pinned `verifiers` runtime; docker-CLI fallback) and returns an `ExecOutcome`
  whose `status` is the primary signal: `passed` / `failed` / `test_defect` /
  `error` / `apply_fail` / `no_run`. A patch is scored ONLY on `passed`/`failed`
  (`.informative`); `test_defect` and `error` are facts about the HARNESS and must
  never be charged to the patch (PR #170). The reward is the execution status; the
  LLM reviewer is demoted to veto-only.
- **Solve-time oracle (Assay)** — `mini_ork/certify/oracle.py`, `certify/probe.py`,
  `certify/invariants.py`. Four-term verdict: (1) PoC+ reproduction test that
  asserts the EXPECTED OUTPUT and fails on base for the RIGHT reason (an
  `AssertionError`, not a setup crash); (2) DELTA gate red→green; (3) METAMORPHIC
  amplification (the same property must hold across perturbed inputs — the only
  term that catches a patch that passes the test and is still wrong; measured
  ~73.6% shortcut rate for single execution tests under compute pressure,
  arXiv 2604.15149); (4) ABSTAIN → UNVERIFIED if any term cannot be established.
  **Every generated test is itself validated against base AND gold before it is
  allowed to judge.** Certificate schema `mini-ork.certificate/v1`
  (`certify/certificate.py`) is a digest-sealed durable artifact.
- **Behavioral verifier (live surfaces)** — `mini_ork/verify/behavioral.py`. Probes
  a live observable (staging API today; UI/journey register via
  `register_surface_handler`) and returns a three-valued, execution-anchored
  verdict: `PROVEN` / `REFUTED` / `UNVERIFIED`. `UNVERIFIED` is explicit abstention
  — never a pass. Metamorphic relations that need no gold (`idempotent_repeat`)
  anchor an oracle from a single request/response (RESTOR 2607.23963). Declaring
  the acceptance `checklist` up front is what makes the check complete —
  an incomplete checklist is the dominant agentic-verification failure mode
  (WebTestBench 2603.25226).
- **Committee aggregation** — `mini_ork/verify/committee.py`. REFUTED outranks;
  confidence-weighted strict-majority PROVEN vs UNVERIFIED; a DECORRELATION guard
  requires PROVEN to come from ≥2 distinct surfaces (correlated same-surface votes
  cannot pass). `pairwise_agreement` scores inter-verdict agreement.
- **Reward write-back** — `mini_ork/verify/reward.py`. `verdict_reward`:
  PROVEN→1.0, REFUTED→0.0, **UNVERIFIED→None (no training target)** — mapping
  abstention to a number would teach the loop that unreachable = refuted.
- **Verifier catalog + ranking** — `mini_ork/verify/catalog.py`. `VerifierCard`
  (name, kind ∈ behavioral|deterministic|reviewer|llm_judge|external,
  surface ∈ api|ui|journey|function, cost, stats) scored by an IRT-GRM-inspired
  heuristic `discrimination × consistency × 1/(1+cost) − fuzz_penalty`
  (`card_score`), ranked for verifier SELECTION. Cards live in `verifiers/*.card.yaml`.
- **Gate registry (8 registered gate types, OCP seam)** —
  `mini_ork/gates/gate_registry.py` + `register_gate_evaluator(type, fn)`. Oracle
  gates seeded by `gate_bootstrap`: coalition, panel-health, synthesis-promote,
  stability, liveness (`gates/native_gates.py`). Verdict contract rc=0 pass /
  1 fail / 2 defer.
- **Promotion gate** — `mini_ork/gates/promotion_gate.py`. No measurement →
  rejected; Δutility ≤ 0 → quarantined; counts DISTINCT `run_id` (memory-poisoning
  defense); verifier/oracle VALIDITY audit sub-family (frozen-control phantom
  gains, pass@k misuse — G03-T08). No human-approval branch by design.
- **Frozen probe scorer** — `mini_ork/learning/probe_scorer.py` (GRASP 2605.29668).
  Scores a directive mutation by re-running a FROZEN balanced probe set
  (`recipes/<recipe>/probes/*.md`) with and without the mutation; accept only when
  fix-rate improves and the per-task no-regression budget holds. Probes with
  `anchor: true` are anchor probes (excluded from the collapse gate).
- **Adversarial test hardening + deterministic rule verifiers** — 358ffd99
  (SWE-ABS 2603.00520; rejects previously-passing patches; VPRMs 2601.17223:
  deterministic rule verifiers beat neural judges).
- **Other acting gates** — `artifact_contract.py` (artifact contract validation),
  `citation_verifier_mechanical.py` (recall-floor + `path:LINE` citation
  resolution), `honest_ci_gate.py` (per-finding confidence intervals from lens
  votes; 2511.12288), `krippendorff_alpha_gate.py` (inter-rater α for panel
  scores), `panel_bias.py`, `coord_gate.py` / `coalition_gate.py` /
  `scope_overlap.py` (coordination overlap), `cw_por.py` (fails CLOSED),
  `refute_or_promote_gate.py` (fabricated-bug survival oracle),
  `mutation_adversary.py` (mutation validator math), `gate_fuzzer.py`,
  `rubric_scoring.py` / `rubric_prescreen.py` / `rubric_cache.py`,
  `adaptive_stability.py`, `step_rules.py`, `profile_gate.py`.
- **Calibrated abstention gate** — `mini_ork/gates/abstain_gate.py` (G04-T05,
  f3d933c4). Reads verifier verdict + confidence, loads labelled history from
  `verifier_results` (migration 0025: verdict + confidence + is_false_positive /
  is_false_negative), escalates to the human-oversight channel on low confidence.
  Dormant until ≥30 labelled rows exist.
- **Human-oversight channel** — `mini_ork/gates/oversight_inbox.py`
  (`mo_inbox_gates`, migration 0002). Write half is wired; **the executor never
  enqueues (0 rows live)**.
- **Eval node + reward contract** — `type: eval` node in the run flow writes
  reward columns (`mini_ork/learning/eval_judge.py`, `EVAL_ANCHOR=0.5`,
  reward_g = (s−anchor)/|anchor|); LLM judge is veto-only.
- **Scrubbed test env** — `mini_ork/verify/test_env.py`. `scrubbed_test_env`
  strips provider credentials (`*_API_KEY`, `*_AUTH_TOKEN`, `ANTHROPIC_*`, …) and
  mini-ork state pointers (`MINI_ORK_SECRETS/DB/HOME/RUN_*`) from the child test
  process so a target-repo test cannot charge the operator or corrupt a real key.
- **Collapse circuit-breaker** — G01-T04 (e8102541 breaker + table 0060;
  e073354e apply loop writes `collapse_history`). HALT path wired for the G7
  score/anchor divergence check.
- **Blind-retry control arm** — G06-T03 (a047f878). Credits an apply/repair
  mechanism only above N blind retries — the matched-attempt control (blind
  resampling beats self-repair, 2607.26117; revision ≈ regeneration, 2607.28908).
- **Base replay in code-fix test verifier** — G03-T11 (20f647a3, BSG-VA 2607.28871):
  replay validation commands on buggy/candidate/gold.

## Shipped as MEASUREMENT only (acting half deliberately not wired)

- G1 `calibration_backtest` cascade blind-spot metric — nothing thresholds on it.
- G3 `mini-ork gate-fuzz` adversarial gate breaker — fuzz failures are not fed
  back as gates.
- G5 `harness-contrast` harness-vs-model attribution — loop does not promote on it.
- G6 oversight inbox — executor never enqueues (0 rows live).
- G7 `collapse-check` score/anchor divergence — breaker built (G01-T04) but the
  check has not fired on live data yet.
- `garden` reports installed-but-inert mechanisms from the trace DB.

## Known weaknesses (MEASURED — these define the gap)

- **Verifier is the bottleneck.** On SWE-bench the cheap lane generated 3/6
  correct fixes but its gate rejected all 3 → shipped 0. The loop certifies
  NON-REGRESSION, not CORRECTNESS.
- **The oracle has a measured hole**: recall ~84%, n=21 (`feedback` note:
  "oracle 100%/FC0/floor 85%/recal…"). Precision needs HARD negatives —
  measure on tests the system actually failed, not easy ones.
- **Execution-anchored single tests are gameable through coverage gaps** — only
  metamorphic / invariance checks close the gap (`certify/invariants.py` is the
  one layer that can).
- **`test_defect` / harness facts are a documented mis-scoring class** — the
  difference between "the bug is still there" and "my test has a typo" is
  load-bearing (a correct patch was rejected because its test was broken).
- **Model-emitted unified diffs**: 77% fail `git apply`; models must edit files
  and let git compute the diff.
- **Live coverage gap**: many shipped verification paths are proven by unit tests
  but have not fired in a live `mini-ork run`.
- **No formal/proof-based verification surface** at all (no SMT/VCC/certificate
  beyond the digest-sealed certificate JSON).
- **No test-quality adequacy metric** (no mutation-score gate on the target's own
  test suite; mutation_adversary validates OUR mutation pipeline, not the repo's
  test suite).

## In flight (kickoffs written, not merged)

E1 code-arm probe harness (measure a code improvement), E2 probe-set growth from
failures, E3 sequential acceptance certificate (e-value style), E4 edit-scope
declaration + rollback targets, E5 harness-effect isolation, E6 point the apply
sweep at the harness, E7 route each failure to its source layer, E8 unfireable
kernel (immutable core the loop cannot edit).
