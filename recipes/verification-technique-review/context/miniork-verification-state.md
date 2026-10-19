# mini-ork verification & anchoring — state snapshot (2026-10-18)

Hand-maintained input for the impact reviewers. Point `MO_VERIFICATION_STATE_FILE`
(or the legacy `MO_RSI_STATE_FILE`) at a newer copy to refresh it without editing
the recipe. Anchors are `module` or `module:line` in the mini-ork repo; see
`docs/reference/FEATURE-INVENTORY.md` and `docs/architecture/verification-stack.md`.

The governing doctrine, stated once and enforced across the stack: **a verdict is
anchored on what the code ACTUALLY did, never on what a model said about it. An LLM
judge may only VETO, never APPROVE.** (`mini_ork/runtime/__init__.py`,
`mini_ork/runtime/engine.py:58`, PR #168.)

## Prior review already absorbed (do not re-recommend as new)

The 2026-10-05/06 review (`run verify-review-20261005-122958`, 2000 papers)
shipped **six techniques**, all ON by default and documented in
`docs/architecture/verification-stack.md`:

| # | Technique | Module | Master knob |
|---|---|---|---|
| ① | Metamorphic relations (veto + rescue) | `mini_ork/certify/relations.py` | `MO_ASSAY_RELATIONS` |
| ② | Differential behavioural-equivalence check | `mini_ork/certify/differential.py` | `MO_ASSAY_DIFFERENTIAL` |
| ③ | Mutation-based suite adequacy (target repo's suite) | `mini_ork/gates/suite_adequacy.py` | `MO_SUITE_ADEQUACY` |
| ④ | Non-nested 5-level correctness vector at publish | `mini_ork/verify/levels.py` | `MO_LEVEL_VECTOR` |
| ⑤ | Gate hackability audit (hollow-input probes) | `mini_ork/gates/hackability.py` | `MO_PROMOTION_GATE_HACKABILITY` |
| ⑥ | Declared equivalence operator (`exact`/`set`/`canonical`/`tolerant`) | `mini_ork/verify/equivalence.py` | `MO_BEHAV_EQUIVALENCE` |

Live A/B proofs (not unit tests): relations REFUTED a special-case cheat that the
old oracle PROVED; differential REFUTED an even/odd-length broken median fix that
was PROVED; suite adequacy downgraded a 2/12-kill suite; the level vector withheld
a commit on `target=UNVERIFIED` where the reviewer had approved; the hackability
audit found three oracle gates at 0.8 and a fix brought them to 0.0; the
equivalence operator made 4/4 correct-but-rejected surfaces PROVEN while 0/5
wrong expectations passed.

The 2026-10-18 review (`run verify-agentic-20261018-215339`, 2000 papers)
shipped **eight** more instruments, documented in sections ⑦–⑭ of
`docs/architecture/verification-stack.md`. Do not re-recommend these as new:

| # | Technique | Module | Knob (default) |
|---|---|---|---|
| ⑦ | Oracle integrity audit (7 injected defects + 3 controls) | `mini_ork/gates/oracle_integrity.py` | `MO_ORACLE_INTEGRITY` (`1`) |
| ⑧ | Judge validity vs execution evidence, per lane | `mini_ork/gates/judge_validity.py` | `MO_JUDGE_VALIDITY` (off), `MO_JUDGE_VALIDITY_MIN` (`0.80`, tighten-only) |
| ⑨ | Reward↔evidence pairing (drop contradicted positive rewards) | `mini_ork/learning/reward_audit.py` + `learning/writeback.py` | `MO_REWARD_EVIDENCE_PAIRING` (`1`; `shadow`; `0`) |
| ⑩ | Verifier mutation discrimination (6 mutations) | `mini_ork/gates/verifier_mutation.py` + `verify/catalog.py` | `MO_VERIFIER_MUTATION` (`1`), `MO_VERIFIER_MUTATION_MIN` (`0.8`) |
| ⑪ | Split-conformal abstention gate | `mini_ork/gates/conformal_gate.py` | `MO_CONFORMAL_ORACLE` (off), `MO_CONFORMAL_ALPHA` (`0.10`) |
| ⑫ | Discrimination-anchored test evidence | `mini_ork/certify/discrimination.py` + `certify/oracle.py` | `MO_ASSAY_DISCRIMINATION` (`1`), `MO_DISCRIMINATION_MIN` (`1.0`) |
| ⑬ | Behavioral recomputation (two independent replays) | `mini_ork/certify/recompute.py` + `verify/behavioral.py` | declared `Observable.recompute` block |
| ⑭ | Hidden regenerable ground truth (PoC variants) | `mini_ork/certify/hidden_checks.py` + `certify/oracle.py` | `MO_ASSAY_HIDDEN` (`1`), `MO_HIDDEN_TRIALS` (`2`), `MO_HIDDEN_CHECK_SECRET` |

All eight have unit coverage; each was merged through a `framework-edit` run
and a scoped green test gate. The review's remaining 51 "next" and 64 "watch"
techniques are NOT shipped — they are still legitimate recommendations.

## Shipped and acting (the loop uses these today)

- **Execution-anchored runtime (Crucible)** — `mini_ork/runtime/engine.py`. Runs a
  candidate in an isolated runtime and returns an `ExecOutcome` whose `status` is
  the primary signal: `passed` / `failed` / `test_defect` / `error` / `apply_fail`
  / `no_run`. A patch is scored ONLY on `passed`/`failed` (`.informative`);
  `test_defect` and `error` are facts about the HARNESS and must never be charged
  to the patch (PR #170). The reward is the execution status; the LLM reviewer is
  veto-only.
- **Solve-time oracle (Assay)** — `mini_ork/certify/oracle.py`, `probe.py`,
  `invariants.py`, `relations.py`, `differential.py`. Four-term verdict: (1) PoC+
  reproduction test that asserts the EXPECTED OUTPUT and fails on base for the
  right reason; (2) DELTA gate red→green; (3) metamorphic amplification plus
  differential collateral-damage veto; (4) ABSTAIN → UNVERIFIED if any term
  cannot be established. **Every generated test is itself validated against base
  AND gold before it may judge.** Certificate schema
  `mini-ork.certificate/v1` (`certify/certificate.py`) is digest-sealed.
- **Behavioral verifier (live surfaces)** — `mini_ork/verify/behavioral.py`. Probes
  a live observable (staging API; UI/journey via `register_surface_handler`) and
  returns `PROVEN` / `REFUTED` / `UNVERIFIED`. `UNVERIFIED` is explicit
  abstention — never a pass. `idempotent_repeat` anchors an oracle from a single
  request/response (RESTOR 2607.23963). The declared `checklist` is what makes the
  check complete; an incomplete checklist is the dominant agentic-verification
  failure mode (WebTestBench 2603.25226).
- **Committee aggregation** — `mini_ork/verify/committee.py`. REFUTED outranks;
  confidence-weighted strict-majority PROVEN vs UNVERIFIED; DECORRELATION guard
  requires PROVEN from ≥2 distinct surfaces.
- **Reward write-back** — `mini_ork/verify/reward.py`. PROVEN→1.0, REFUTED→0.0,
  **UNVERIFIED→None (no training target)**.
- **Verifier catalog + ranking** — `mini_ork/verify/catalog.py`. `VerifierCard`
  (kind, surface, cost, stats) scored by `discrimination × consistency /
  (1+cost) − fuzz_penalty`; cards in `verifiers/*.card.yaml`.
- **Gate registry (8 built-in types + OCP seam)** —
  `mini_ork/gates/gate_registry.py`. Built-ins: deterministic_verifier,
  reviewer_gate, human_gate, budget_gate, scope_gate, deployment_gate,
  liveness_gate, custom; extensions register via
  `register_gate_evaluator(type, fn)` (e.g. `abstain_gate`, `agent_s_gui`).
  Unknown type → `defer`, never `pass`. Oracle gates seeded by `gate_bootstrap`.
- **Promotion gate** — `mini_ork/gates/promotion_gate.py`. No measurement →
  rejected; Δutility ≤ 0 → quarantined; counts DISTINCT `run_id`; verifier/oracle
  VALIDITY audit sub-family (frozen-control phantom gains, pass@k misuse). Now
  also refuses a promote resting on a hackable gate (⑤). No human-approval branch.
- **Frozen probe scorer** — `mini_ork/learning/probe_scorer.py` (GRASP 2605.29668).
  Accepts a directive mutation only when fix-rate improves on a FROZEN balanced
  probe set and the per-task no-regression budget holds.
- **Adversarial test hardening + deterministic rule verifiers** — 358ffd99
  (SWE-ABS 2603.00520; VPRMs 2601.17223).
- **Suite adequacy in the code-fix test verifier** — ③ above; INADEQUATE turns a
  green into UNVERIFIED (`pass:false`, exit 0). Evidence in `suite_adequacy.json`.
- **Level vector at the publisher** — ④ above; `code_fix` requires applies,
  executes, target, preserve. REFUTED → fail+rollback; anything else → withhold
  commit, keep the fix in the tree. `REQUIRED_LEVELS` lives in
  `mini_ork/verify/levels.py:51`.
- **Other acting gates** — `artifact_contract.py`, `citation_verifier_mechanical.py`,
  `honest_ci_gate.py`, `krippendorff_alpha_gate.py`, `panel_bias.py`,
  `coord_gate.py` / `coalition_gate.py` / `scope_overlap.py`, `cw_por.py` (fails
  CLOSED), `refute_or_promote_gate.py`, `mutation_adversary.py`, `gate_fuzzer.py`,
  `rubric_scoring.py` / `rubric_prescreen.py` / `rubric_cache.py`,
  `adaptive_stability.py`, `step_rules.py`, `profile_gate.py`.
- **Calibrated abstention gate** — `mini_ork/gates/abstain_gate.py` (f3d933c4).
  Reads verdict + confidence, loads labelled history from `verifier_results`
  (migration 0025), escalates low-confidence to the oversight channel. Dormant
  until ≥30 labelled rows exist.
- **Human-oversight channel** — `mini_ork/gates/oversight_inbox.py`
  (`mo_inbox_gates`, migration 0002). Write half wired; **the executor never
  enqueues (0 rows live)**.
- **Eval node + reward contract** — `type: eval` node writes reward columns
  (`mini_ork/learning/eval_judge.py`, `EVAL_ANCHOR=0.5`); LLM judge is veto-only.
- **Scrubbed test env** — `mini_ork/verify/test_env.py`. Strips provider
  credentials and mini-ork state pointers from the child test process (also
  scrubs `MO_TARGET_CWD`, 41c20d78).
- **Collapse circuit-breaker** — G01-T04 (e8102541 breaker + table 0060;
  e073354e apply loop writes `collapse_history`).
- **Blind-retry control arm** — G06-T03 (a047f878): credits an apply/repair
  mechanism only above N blind retries (2607.26117, 2607.28908).
- **Base replay in code-fix test verifier** — G03-T11 (20f647a3, BSG-VA
  2607.28871): replay validation commands on buggy/candidate/gold. Replay
  adapters now cover pytest, jest/vitest, JUnit XML, go and cargo test runners.
- **Pre-run lane-health gate** — `mini_ork/cli/lane_preflight.py` (b3c80bb5,
  aaac165d). Probes every lane the compiled DAG will dispatch *before*
  `set_status(executing)`; dead lane → rc 75, `blocked_reason=lane_unhealthy`,
  `lane-preflight.json`, one `run_events` row, zero dispatch. Lanes that are `ok`
  but could not be probed are recorded `unverified`; `MO_LANE_REQUIRE_VERIFICATION=1`
  makes those abort too (default `0` = warn and proceed). Knobs:
  `MO_LANE_HEALTH_GATE` (default `1`), `MO_LANE_PROBE`.
- **Kickoff guard** — `mini_ork/verify/kickoff_guard.py` (385e4f4c). A run must
  not rewrite its own acceptance contract. `MO_KICKOFF_GUARD=off|shadow|on`,
  **default `shadow`** (evaluate + record, never block).
- **Evidence ledger** — `mini_ork/verify/evidence_ledger.py` (e120292a). Binds
  evidence to the exact code state (`tree_hash`). `off` (default) / `1` enforce /
  `shadow`.
- **Probe-validity gate** — `mini_ork/verify/probe_validity.py` (cea7b3b5,
  5c4b3b27). Refuses a run whose verify proved nothing (no verifier node ended
  `done` with `pass:true`), whose probes are aliased across ACs, or whose probes
  already pass on the untouched base tree. `MO_PROBE_VALIDITY` default `0`; a
  `shadow` mode records `would_block` and is counted by `mini-ork metrics sdd`.
- **Human review gates** — `mini_ork/gates/review_policy.py`, `merge_approval.py`,
  `mini-ork approvals` CLI (9c4a48f7, e71555a7). Content-bound merge approvals.
  **Default OFF** — the loop does not stop for a human unless configured.
- **Agent-S GUI gate** — `mini_ork/gates/agent_s_smoke.py`,
  `agent_s_driver.py` (56015c8f, 39a485b4). Advisory; drives gui-agents 0.3.2.

## Shipped as MEASUREMENT only (acting half deliberately not wired)

- `calibration_backtest` cascade blind-spot metric — nothing thresholds on it.
- `mini-ork gate-fuzz` adversarial gate breaker — fuzz failures are not fed back
  as gates (except through the hackability→promotion path).
- `harness-contrast` harness-vs-model attribution — the loop does not promote on it.
- Oversight inbox — executor never enqueues (0 rows live).
- `collapse-check` score/anchor divergence — breaker built, check has not fired on
  live data yet.
- `garden` reports installed-but-inert mechanisms from the trace DB.
- `mini-ork metrics sdd` reads the I1 shadow (`probe_validity`) would-block rate
  without gating on it.

## Known weaknesses (MEASURED — these define the gap for this review)

- **Verifier is the bottleneck.** On SWE-bench the cheap lane generated 3/6
  correct fixes but its gate rejected all 3 → shipped 0.
- **The oracle has a measured hole**: recall ~84%, n=21. Precision needs HARD
  negatives — measure on tests the system actually failed, not easy ones.
- **Execution-anchored single tests are gameable through coverage gaps** — the
  metamorphic and differential terms close part of it, but a cheat can still be
  outside every relation the proposer thought of.
- **`test_defect` / harness facts are still a mis-scoring class** at the edges —
  failure provenance now abstains on toolchain failures (2f7e4b81, d645ba94), but
  a test that fails for the wrong reason can still read as a real refutation.
- **Model-emitted unified diffs**: 77% fail `git apply`; models must edit files.
- **Live coverage gap**: many shipped verification paths are proven by unit tests
  and/or a single real-run smoke, but few have cumulative live firing data.
- **No formal/proof-based verification surface** (no SMT/VCC/certificate beyond
  the digest-sealed certificate JSON and the declared equivalence operator).
- **No soundness measure for the verifier stack as a whole** — each instrument has
  a local smoke, but there is no measured false-accept / false-reject rate on a
  held-out agent task distribution.
- **Long-horizon agent work is under-verified**: the stack verifies a *code
  change* well; it has no first-class notion of an agent *trajectory* (did the
  agent actually do what it claimed across N steps, with tool calls grounded in
  environment state?).
- **No GUI/computer-use verification in the loop** — Agent-S is an advisory gate
  only; nothing execution-anchors a UI outcome.
- **Judge reliability is asserted, not measured continuously** — panel
  heterogeneity and α gates exist, but judge calibration data is not refreshed.

## In flight

E1 code-arm probe harness (measure a code improvement), E2 probe-set growth from
failures, E3 sequential acceptance certificate (e-value style), E4 edit-scope
declaration + rollback targets, E5 harness-effect isolation, E6 point the apply
sweep at the harness, E7 route each failure to its source layer, E8 unfireable
kernel (immutable core the loop cannot edit).
