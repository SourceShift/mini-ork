# The verification stack

*Added 2026-10-05/06. Six techniques from the 2000-paper verification review
(`recipes/verification-technique-review/`, run `verify-review-20261005-122958`),
each built by a mini-ork `framework-edit` run with RSI on and proven by a real
mini-ork run. Each is on by default **where it runs**, but most do not run
in a plain `mini-ork run`; see "Where each instrument runs today" below.*

*Scope of ① and ② specifically: they are ON in `mini-ork certify`, and in
`mini-ork run code-fix` they run in **shadow** — the verifier executes both
terms and records their verdict, but never lets them change `pass` or the exit
code (section ⑮).*

*A second review on 2026-10-18 (`verify-agentic-20261018-215339`, 2000 arXiv
papers) added eight more instruments — sections ⑦–⑭ — also each built by a
mini-ork `framework-edit` run and merged after a scoped green test gate.*

## The rule everything here obeys

mini-ork's verdicts are **anchored on what the code actually did**, never on what
a model said about it. A model may *propose* a check or *veto* a result. It may
never approve one. Every verdict has three values:

| Verdict | Meaning |
|---|---|
| `PROVEN` | The evidence establishes it. |
| `REFUTED` | The evidence contradicts it, and the evidence is attached. |
| `UNVERIFIED` | The evidence could not be established. This is an **abstention, never a pass**. |

The features below close three holes that were measured in mini-ork itself:

1. **The loop certified non-regression, not correctness.** A live audit found 236 code-fix runs. Of the 61 that were
   published, **none** had a proven red→green, and 39 were published on a red post-patch suite.
2. **The oracle had a recall hole.** It could not judge about 16% of correct fixes (recall 21/25).
3. **Single tests can be gamed through coverage gaps.** A patch that special-cases the tested input passes the test.

## Which "vacuous" guard you mean: three, not one

"There is a vacuous-pass guard, but it is off behind a flag" and "a vacuous pass
cannot be laundered into a success" are both true, and they describe different
code. Naming one path for all three is how the retired `mini_ork_verify.py`
kept being cited for a guard that lives somewhere else.

| Mechanism | Path | What it stops | State today |
|---|---|---|---|
| Verdict | `mini_ork/cli/verify.py` | A run with zero executed verifiers, zero-byte evidence, or a verifier that declares itself vacuous ends `vacuous`/`fail`, never `pass` | **Live, unconditional** |
| Dispatch | `mini_ork/cli/execute_handlers.py` (`_is_vacuous_command`, `_vacuous_verdict`, `_record_verifier_result(…, "vacuous")`) | A literal `true` / `:` / `exit 0` probe — statically no-op — has its `pass`, `suite_green` and `post_rc` stripped, so it earns no execution reward | **Live, unconditional** |
| Pre-publish gate | `mini_ork/verify/probe_validity.py` | A run whose probes are vacuous, aliased across acceptance criteria, or already green on the untouched base tree is not publishable (`verify_vacuous`) | **Default OFF** — `MO_PROBE_VALIDITY` unset/`0`; `1` = enforce, `shadow` = record `would_block` only. Turning it on is a separate change after an n ≥ 30 A/B against the K1 frozen baseline |

The first two are the reason a vacuous run cannot pass today; the third is a
publish-time hold-out whose evidence (`probe-validity.json`) is collected in
`shadow` mode first. `bin/mini-ork-verify` and `mini_ork_verify.py` — the
pre-2026-07 shell oracle and its first Python port — are both retired; any
document still citing them is describing that history, not this code.

## Where each piece lands

```mermaid
flowchart TB
  subgraph certify["mini-ork certify — the Assay oracle (mini_ork/certify/)"]
    P["PoC+ reproduction test"] --> D["delta gate red→green"] --> I["metamorphic invariants"]
    I --> R["① metamorphic RELATIONS<br/>relations.py — veto + rescue"]
    I --> X["② DIFFERENTIAL check<br/>differential.py — collateral-damage veto"]
  end
  subgraph codefix["mini-ork run code-fix — verifier + publish"]
    T["test verifier<br/>recipes/code-fix/verifiers/test.py"] --> A["③ SUITE ADEQUACY<br/>gates/suite_adequacy.py — mutation kill rate"]
    A --> L["④ LEVEL VECTOR<br/>verify/levels.py — 5 independent levels"]
    L --> PUB["publisher: commit only if every required level is PROVEN"]
  end
  subgraph gates["gate registry + promotion"]
    H["⑤ HACKABILITY audit<br/>gates/hackability.py · gate-fuzz --hackability"] --> PG["promotion gate refuses<br/>promotes resting on a hackable gate"]
  end
  subgraph behav["mini-ork verify — behavioral verifier"]
    E["⑥ declared EQUIVALENCE operator<br/>verify/equivalence.py"]
  end
```

## Where each instrument runs today (checked 2026-10-27)

"On by default" means on inside the path that calls the instrument. Only ③,
④ and ⑨ act on ordinary runs. The rest act only under `mini-ork certify`,
at promotion, or when a recipe opts in, or they have no producer yet.

| # | Instrument | Runs in | State |
|---|---|---|---|
| ① | Metamorphic relations | `mini-ork certify`; `mini-ork run code-fix` in shadow (section ⑮) | on inside certify; shadow in code-fix |
| ② | Differential check | `mini-ork certify`; `mini-ork run code-fix` in shadow (section ⑮) | on inside certify; shadow in code-fix |
| ③ | Suite adequacy | every code-fix run (`recipes/code-fix/verifiers/test.py`) | **live** |
| ④ | Level vector | the publisher, for recipes that declare levels (code_fix) | **live** |
| ⑤ | Gate hackability | read by the promotion gate; records come only from a manual `mini-ork gate-fuzz --hackability` | consumer on, producer manual |
| ⑥ | Equivalence operator | the behavioral verifier, for recipes whose contract declares an operator | opt-in per recipe |
| ⑦ | Oracle integrity | read by the promotion gate; nothing writes it | no producer |
| ⑧ | Judge validity | panel gates | off (`MO_JUDGE_VALIDITY`) |
| ⑨ | Reward↔evidence pairing | every GRPO writeback | **live** |
| ⑩ | Verifier mutation | read by the verify catalog; nothing writes scores | no producer |
| ⑪ | Conformal abstention | — | removed (669c181c) |
| ⑫ | Discrimination evidence | `mini-ork certify`, and only when candidates are passed | certify only |
| ⑬ | Behavioral recomputation | the behavioral verifier, declared recipes | opt-in per recipe |
| ⑭ | Hidden regenerable checks | `mini-ork certify` only | certify only |

## ① Metamorphic relations (G10-T01): `mini_ork/certify/relations.py`

**What it checks.** The model proposes up to *k* relations: a SOURCE input, a transform, a FOLLOWUP input
(`T(SOURCE)`), and a RELATION between the two outputs. For example: "reversing the list leaves the median unchanged".
Each relation runs as a real test that calls the code twice. A patch that special-cases the reported input fixes
SOURCE but not FOLLOWUP, so the relation breaks.

**How it decides.** Execution decides; the model only proposes.
- **Veto.** It runs only on a would-be PROVEN. A relation that held on the unpatched code and breaks on the patch
  turns PROVEN into REFUTED. The reason names the relation and the input pair.
- **Rescue.** It runs only where the oracle gave up because it could not build enough invariants. PROVEN ("relative
  to N relations") needs ≥2 held, ≥1 repaired (fails on base, holds on the patch) and zero violations.
- **Abstention.** A relation that cannot execute abstains and never counts as holding. Base runs only *attribute*
  a failure; they never admit a relation.

**Knobs.**
- `MO_ASSAY_RELATIONS` (ON): the master switch.
- `MO_ASSAY_RELATIONS_RESCUE` (ON): the rescue mode.
- `MO_ASSAY_RELATIONS_K` (3, clamped 1–5): relations per judgement.
- `MO_ASSAY_RELATIONS_VETO_MIN` (1): violations needed to veto.

**Evidence.** Each judgement prints one stderr line, `[assay-relations] {json}`, with mode, counts and per-relation
records. The verdict detail key is `relations`.

**Live result.** In rescue mode the cheat `if xs == [1,2,3,4]: return 2.5` was REFUTED by 2 violated relations: the
reversed list `[4,3,2,1]` and the duplicated `SOURCE + SOURCE`. The correct fix was PROVEN relative to 3 relations.
No correct fix was REFUTED in any run.

## ② Differential check (G10-T02): `mini_ork/certify/differential.py`

**What it checks.** The model proposes ONE shared input suite split into two groups:
- `bug_domain`: inputs where the fix *should* change behaviour;
- `preserve`: inputs where it must not.

The same harness runs on base and on the patch, and code compares canonical-JSON observations by sha256. A correct
fix diverges inside the bug's domain (the anchor) and agrees outside it.

**How it decides.**
- **Veto.** It runs only on a would-be PROVEN. ≥ `VETO_MIN` confirmed `preserve` divergences turn PROVEN into
  REFUTED, with the input and both observations attached. A divergence is confirmed by a re-run on each side.
- **Mislabels.** Requiring 2 divergences means one mislabelled `preserve` input can never sink a correct fix.
- **Abstention.** Crashed or unreadable inputs are excluded and never count as agreement. With no anchor, there is
  no veto.

**Knobs.**
- `MO_ASSAY_DIFFERENTIAL` (ON).
- `MO_ASSAY_DIFFERENTIAL_N` (6, clamped 2–8).
- `MO_ASSAY_DIFFERENTIAL_VETO_MIN` (2).

**Evidence.** A stderr line `[assay-differential] {json}`; the detail key is `differential`.

**Live result.** A fix that averages the two middle values *always* fixes `[1,2,3,4]` but breaks every odd-length
list. **Today's oracle PROVED it.** The differential term REFUTED it: `input=[1, 2, 3, 4, 5] base=3 head=2.5` (3 of 3
preserve inputs diverged). The correct fix stayed PROVEN.

## ③ Mutation-based suite adequacy (G03-T04): `mini_ork/gates/suite_adequacy.py`

**What it checks.** Whether the target repo's test suite is strong enough for its green to mean anything. It seeds
deterministic AST mutants into the files the candidate changed:
- comparison flip
- arithmetic swap
- boolean negation
- constant off-by-one
- `return None`
- condition → constant

Each mutant is applied to a temporary **copy** of the repo, and the suite is rerun. The score is
`killed / (killed + survived)`. There is no LLM, and the live tree is never touched.

**How it decides.** It runs inside the code-fix test verifier on every green suite.

| Verdict | When | Effect |
|---|---|---|
| `ADEQUATE` | score ≥ `MIN_SCORE` | The green stands. |
| `INADEQUATE` | score below `MIN_SCORE` | The green becomes UNVERIFIED (`pass:false`, exit 0, `adequacy_unverified`). A green from a suite that can't catch faults is not evidence. |
| `UNVERIFIED` | baseline red or timed out, canary not detected, or too few valid runs | Downgraded as above. The instrument applied but could not measure. |
| `NOT_APPLICABLE` | no Python source changed, or too few mutation sites | The green stands. The instrument does not apply. |

**Knobs.**
- `MO_SUITE_ADEQUACY` (ON).
- `MO_SUITE_ADEQUACY_MAX_MUTANTS` (12, range 1–50).
- `MO_SUITE_ADEQUACY_MIN_SCORE` (0.6).
- `MO_SUITE_ADEQUACY_TIMEOUT_S` (300, per run).

**Evidence.** In the run dir, `suite_adequacy.json` holds the full report, including the survivors as
`file:line operator original -> mutated`. The test verifier's JSON carries a `suite_adequacy` key.

**Live result** (real `mini-ork run code-fix`, same bug, two suites):
- **Strong suite:** 12/12 mutants killed (ADEQUATE), and the fix was published.
- **Weak suite:** 2/12 killed (INADEQUATE, 0.167), so the green was downgraded. The 10 named survivors were exactly
  the untested logic.

**Cost.** At most 2 + N extra suite runs per green verifier. That's cheap for scoped test commands. For a slow full
suite, scope the verification command or lower `MO_SUITE_ADEQUACY_MAX_MUTANTS`.

## ④ Non-nested level vector (G08-T05): `mini_ork/verify/levels.py`

**What it checks.** Correctness levels are **non-nested**: passing a shallow level does not imply a deeper one. Every
run verdict now carries five levels. Each is derived independently, and only from execution artifacts, never from
reviewer or LLM output:

| Level | Evidence | PROVEN when |
|---|---|---|
| `applies` | `implementer-summary.json` | Implemented, with a non-empty file list. |
| `executes` | `verifier_test.json` | The runner reached test outcomes. |
| `target` | `verifier_test.json` | The suite is green **and** the replay proves it exercises the change — a non-empty per-test overlap, or (adapter-less runners) an opaque exit-code delta. |
| `preserve` | `verifier_test.json` | No regression, and the suite is not adequacy-unverified. |
| `contract` | `verifier_behavioral.json` | The behavioral verifier is PROVEN (`n/a` when absent). |

Missing evidence makes a level UNVERIFIED, never PROVEN.

**How it decides.** It acts at the publisher. `code_fix` requires applies, executes, target and preserve.
- **All required PROVEN:** publish.
- **Any REFUTED:** fail the run (rc 1, and rollback is the right compensation).
- **Anything else:** **withhold the commit without rolling back the fix** (rc 0, status `failed`).

Gating at the publisher, not at the verifier exit code, keeps an abstained fix in the tree for inspection.

**Knob.** `MO_LEVEL_VECTOR` (ON).

**Evidence.** `verdict.json` gains `levels`, `levels_reasons`, `levels_required`, `levels_ok` and
`levels_decision`. When the recipe owns `verdict.json`, these go to `run-verdict.json`. The execute log shows
`[ok|ABSTAIN|BLOCK] publisher: level …`.

**Live result** (real `code-fix` A/B on the exact leak path, where the verifier was green with no replay proof and the
reviewer **approved**):
- **Gate on:** `[ABSTAIN] … target=UNVERIFIED — publish withheld`, 0 commits, and the fix kept in the tree.
- **Gate off:** published.

## ⑤ Gate hackability audit (G09-T05): `mini_ork/gates/hackability.py`

**What it checks.** Whether a registered gate can be passed by inputs that carry no evidence. Five fixed "hollow"
operators run against each gate's real evaluator in a scratch DB:
- empty context
- dangling evidence
- empty document
- hollow object
- zero-leaf skeleton

An optional LLM proposer adds more candidates, but a proposal counts only if it is itself hollow, by a mechanical
check. **Hackability** is `passed_bad / valid_trials`. Crashed trials are excluded and counted.

**How it decides.** The record persists at `<db dir>/gate-hackability/<gate_id>.json`. `mini-ork promote` **refuses**
a would-be promote that rests on a gate with hackability above `MO_GATE_HACKABILITY_MAX`. Unmeasured or stale gates
never cause a rejection. (The RSI apply loop gates through its own evaluator and is unaffected.)

**Knobs.**
- `MO_GATE_HACKABILITY_N` (4, range 0–16): proposer documents per gate.
- `MO_GATE_HACKABILITY_BUDGET_USD` (0.50).
- `MO_GATE_HACKABILITY_MAX` (0.25).
- `MO_PROMOTION_GATE_HACKABILITY` (ON).

Run the audit with:

```bash
mini-ork gate-fuzz --hackability --gate <gate_id> [--proposer-lane glm] --json
```

**Live result.** The first audit found `oracle-coalition`, `oracle-liveness` and `oracle-stability` at **0.8**. An
unknown panel or run fell into each backend's fail-open default, and the evaluator mapped that default to "pass". A
fix made those evaluators defer on absent evidence (b53b6c0d), and the re-audit measured **0.0**.
`oracle-panel-health` stays at 0.2, under the threshold.

## ⑥ Declared equivalence operator (G10-T07): `mini_ork/verify/equivalence.py`

**What it checks.** Every comparison is relative to an operator, and a misfit operator rejects correct
implementations. The behavioral verifier's comparison is now an explicit declaration on the observable:

| Operator | Meaning |
|---|---|
| `exact` | The default, byte-identical to before. |
| `set` | Order-insensitive multiset. |
| `canonical` | Declared rules only: `ignore_keys`, `strip_whitespace`, `numeric`. |
| `tolerant` | `abs_tol` / `rel_tol` on numbers that are not bools. |

`register_operator()` is the extension seam. An unknown operator or invalid rules give **UNVERIFIED on every
surface**, never a silent `exact` and never a pass. Every verdict and check carries an `operator` stamp, and every
REFUTED names its operator and the first differing key path (e.g. `$.db_path`).

**Declare it** in the `observable:` block of the `MO_OBSERVABLE_SPEC` descriptor (`equivalence:` and
`expect_body:`), or with `MO_BEHAV_EQUIVALENCE` / `MO_BEHAV_EXPECT_BODY`.

**Live result** (against a live `mini-ork serve`): 4/4 correct-but-rejected surfaces became PROVEN under their fitted
operator, and 0/5 wrong expectations were approved under any operator.

## The 2026-10 review: eight more instruments

```mermaid
flowchart TB
  subgraph promote["promotion gate (mini_ork/gates/promotion_gate.py)"]
    OI["⑦ ORACLE INTEGRITY<br/>gates/oracle_integrity.py"] --> Q["quarantined<br/>oracle_integrity_failed"]
    JV["⑧ JUDGE VALIDITY<br/>gates/judge_validity.py"] --> Q2["quarantined<br/>judge_validity_below_threshold"]
  end
  subgraph grpo["GRPO writeback (mini_ork/learning/writeback.py)"]
    RP["⑨ REWARD↔EVIDENCE PAIRING<br/>learning/reward_audit.py + writeback.py"]
  end
  subgraph catalog["verifier catalog / certificate (verify/catalog.py + certify/certificate.py)"]
    VM["⑩ VERIFIER MUTATION<br/>gates/verifier_mutation.py"]
  end
  subgraph certify2["certify oracle (mini_ork/certify/oracle.py)"]
    DI["⑫ DISCRIMINATION-ANCHORED TEST EVIDENCE<br/>certify/discrimination.py + oracle.py"]
    HC["⑭ HIDDEN REGENERABLE GROUND TRUTH<br/>certify/hidden_checks.py + oracle.py + certificate.py"]
  end
  subgraph behav2["behavioral verifier (mini_ork/verify/behavioral.py)"]
    BR["⑬ BEHAVIORAL RECOMPUTATION<br/>certify/recompute.py + verify/behavioral.py"]
  end
```

## ⑦ Oracle integrity (G03-T02): `mini_ork/gates/oracle_integrity.py`

**What it checks.** The integrity of the certify oracle's `judge` against seven injected defect classes (`silent_payload_non_delivery`, `frozen_result_swap`, `apply_fail_masked_as_pass`, `stale_evidence_replay`, `runner_error_as_pass`, `test_defect_as_failure`, `contract_violating_outcome`) and three controls (`known_good_proven`, `known_bad_refuted`, `known_empty_abstains`). Any injection the judge does not reject, or any control it rejects, fails the audit.

**How it decides.**
- **Pass.** Every defect rejected AND every control accepted.
- **Fail.** Any defect accepted OR any control rejected. A failed audit is the latest record.
- **Abstention.** A missing or unreadable record is *unmeasured*, never a pass; promotion is not blocked on it.

**Knobs.**
- `MO_ORACLE_INTEGRITY` (ON): promotion consumer of the audit.

**Evidence.** Writes `<db dir>/oracle-integrity/latest.json`. When `MO_ORACLE_INTEGRITY` is on and the latest record failed, the promotion decision is `quarantined` with reason `oracle_integrity_failed`.

## ⑧ Judge validity (G10-T03): `mini_ork/gates/judge_validity.py`

**What it checks.** Whether each reviewer-lane family's approval history agrees with independent execution evidence. Per lane, the audit reads `execution_traces.reviewer_verdict` and `verifier_results` for the same run; rows with an unknown verdict or no verifier rows are skipped, never counted as agreement.

**How it decides.**
- **Per-lane accuracy** uses a Wilson lower bound. Below `MO_JUDGE_VALIDITY_MIN` the gate quarantines the promotion with reason `judge_validity_below_threshold`.
- **Abstention.** A lane with fewer than 30 overlapping rows is unmeasured and admits; the gate never converts abstention into a pass.
- **Off by default.** `MO_JUDGE_VALIDITY` unset disables it; `1` enables it.

**Knobs.**
- `MO_JUDGE_VALIDITY` (unset → off): master switch.
- `MO_JUDGE_VALIDITY_MIN` (0.80): agreement floor; tighten-only (lower values clamp up).
- `MO_JUDGE_VALIDITY_ROWS` (200): history rows audited per promotion check.

**Evidence.** Per-lane envelopes written atomically to `<db dir>/judge-validity/<lane>.json`.

## ⑨ Reward↔evidence pairing (G05-T01): `mini_ork/learning/reward_audit.py`, `mini_ork/learning/writeback.py`

**What it checks.** That a run's reward agrees with its verifier results. `classify_run` labels a run `backed`, `contradicted`, or `unmeasured` from verifier results.

**How it decides.** During GRPO advantage computation, a **positive** reward (≥ 0.5) that execution contradicts is dropped before group aggregation; negative rewards and unmeasured runs are untouched. Divergences are enqueued with kind `reward_evidence_divergence`, capped at 50 per call.
- **Abstention.** Unmeasured runs do not drop rewards; mode `shadow` counts and enqueues without dropping rows.
- **Off.** `0` / `off` restores the exact pre-pairing behaviour.

**Knobs.**
- `MO_REWARD_EVIDENCE_PAIRING` (ON): master switch.
- `MO_REWARD_DIVERGENCE_ENQUEUE_LIMIT` (50): max reward-evidence divergences enqueued per writeback call.

**Evidence.** A `reward_evidence_divergence` row per dropped reward (and per counted-but-not-dropped under `shadow`).

## ⑩ Verifier mutation discrimination (G05-T03): `mini_ork/gates/verifier_mutation.py`, `mini_ork/verify/catalog.py`, `mini_ork/certify/certificate.py`

**What it checks.** Whether a verifier's verdict moves when six deterministic mutations are applied to its observed output (`value_flip`, `field_drop`, `type_swap`, `list_reorder`, `truncate_string`, `status_swap`).

**How it decides.** The baseline must be PROVEN. `kill_score = killed / (killed + survived)`. An UNVERIFIED variant counts as abstained, never as killed.
- **`rank_verifiers`** drops only cards measured below the threshold; unmeasured cards are retained.
- **Abstention.** An UNVERIFIED variant abstains; the gate never converts abstention into a kill.

**Knobs.**
- `MO_VERIFIER_MUTATION` (ON): mutation-discrimination scoring.
- `MO_VERIFIER_MUTATION_MIN` (0.8): minimum kill score for a verifier card.

**Evidence.** Writes a `<card>.mutation.json` sidecar; the certificate may carry `mutation_kill_score`.

## ⑪ Conformal abstention — removed (2026-10)

The split-conformal gate (`gates/conformal_gate.py`) and the calibrated `abstain_gate` evaluator were removed: no verifier writes a confidence or a false-positive label, so neither ever had calibration data, and the only switch (a recipe's per-node `gates:` list) is never read by the executor. The Learn-Then-Test bar in `gates/abstain_gate.py` stays; the self-driving escalation policy uses it. See `docs/plans/2026-10-25-ecosystem-section6.md`.

## ⑫ Discrimination-anchored test evidence (G06-T01): `mini_ork/certify/discrimination.py`, `mini_ork/certify/oracle.py`

**What it checks.** Given candidate patches, a generated test must pass the correct candidate and fail the defective ones. The required discrimination rate is `MO_DISCRIMINATION_MIN` (default 1.0).

**How it decides.** Called with `candidates=` on the oracle. A REFUTED discrimination result **downgrades PROVEN to UNVERIFIED**, never to REFUTED, because a non-discriminating test is weak evidence, not counter-evidence.

**Knobs.**
- `MO_ASSAY_DISCRIMINATION` (ON): master switch.
- `MO_DISCRIMINATION_MIN` (1.0): required discrimination rate.

**Evidence.** A `discrimination` block in the oracle's verdict detail.

## ⑬ Behavioral recomputation (G02-T02): `mini_ork/certify/recompute.py`, `mini_ork/verify/behavioral.py`

**What it checks.** A recorded operation sequence replays from its initial state twice, independently; an optional declared expected state or equivalence operator may be supplied. Two identical replays that disagree, or a mismatch with the declared expectation, is REFUTED with a first-divergence witness.

**How it decides.**
- **Abstention.** An apply exception, non-JSON state, empty record set, or invalid equivalence spec is UNVERIFIED.
- **No separate on/off.** Enabled by declaring the `recompute:` block on a behavioral `Observable`; there is no separate knob.

**Evidence.** A `recompute` block in the behavioral verdict detail.

## ⑭ Hidden regenerable ground truth (G02-T08): `mini_ork/certify/hidden_checks.py`, `mini_ork/certify/oracle.py`, `mini_ork/certify/certificate.py`

**What it checks.** From a precommitted parameter set, derives behaviour-neutral variants of the caller-supplied PoC by renaming top-level `def test_*` / `class Test*` identifiers and prepending a trial marker; assertions, imports, literals, order, and environment are unchanged.

**How it decides.** A variant that fails while the visible PoC passes is REFUTED ("the patch depends on the test's identity").
- **Abstention.** A harness status (`error` / `test_defect` / `apply_fail` / `no_run`) is an unmeasured trial, never a refutation.
- **Key material.** Precedence is `MO_HIDDEN_CHECK_SECRET` → `run_id` → patch digest. The chosen source is recorded as `key_source` so a run without a secret never claims secret-backed hiding.

**Knobs.**
- `MO_ASSAY_HIDDEN` (ON): master switch.
- `MO_HIDDEN_TRIALS` (2): hidden trials per judgement; a malformed value raises rather than guessing.
- `MO_HIDDEN_CHECK_SECRET` (unset): key material for hidden-check variants.

**Evidence.** The certificate carries `hidden_check_commitment` and `hidden_trials` when supplied.

## ⑮ Certify terms in code-fix (shadow): `mini_ork/certify/codefix_terms.py`, `recipes/code-fix/verifiers/test.py`

**What it checks.** The code-fix test verifier now calls the two certify veto
terms — ① metamorphic relations and ② differential — directly, using the
replay's proven fail-to-pass test as the PoC. `mini-ork certify` reached them
only through `oracle.judge`, after a PROVEN delta and invariant verdict; the
verifier has the candidate tree, the base worktree, the proven test ids, and the
kickoff all at once, so it can run the same vetoes without the oracle's other
gates. Discrimination and hidden checks are deliberately omitted: code-fix has
no pool of known-bad patches, and the PoC is the implementer's own test.

**Trigger.** A green replay whose overlap contains a Python test file (that file
becomes the PoC), a kickoff in `run_profile.json`, and a non-empty candidate
change set. The change set is `git diff <base>` *plus every untracked file*, so a
fix delivered as new files (the replay's own test overlay supports exactly that
shape) runs the terms rather than abstaining on an empty diff. A run without the
first two is byte-identical to today's payload — no `certify` key, no report
file.

**How it decides.** The terms run in **veto mode** and are recorded; shadow
never obeys them, gate does:
- **Shadow** (default) — `verdict`, `vetoed_by` and `would_block` are recorded;
  `pass` and the exit code are unchanged.
- **Gate** — `MO_CODEFIX_CERTIFY=gate` records the same block with
  `mode: "gate"`, and the publisher's level vector
  (`mini_ork/verify/levels.py:derive_levels`) consumes it. A REFUTED
  `relations` veto maps to `target`; a REFUTED `differential` veto maps to
  `preserve`. Both downgrade a PROVEN level to **UNVERIFIED** — never
  REFUTED — so `publish_decision` abstains: the publish is withheld and the
  fix stays in the tree, with no rollback. The mode recorded in the evidence
  decides; `derive_levels` never reads `MO_CODEFIX_CERTIFY` itself. Flip the
  default only after shadow evidence shows no veto on a run whose fix was
  confirmed correct.
- **Off** — `MO_CODEFIX_CERTIFY=0` (or `off`/`false`/`no`) skips the terms and
  keeps every payload byte-identical to the pre-feature verifier.
- **Abstention.** No kickoff, no Python fail-to-pass test, no base worktree, an
  empty candidate diff, or a runner that is not up → `status: "skipped"` (the
  record goes to `certify_terms.json`, but not to the payload). A term that
  raises gets an `{"error": …}` slot; the verifier still passes.

**Knobs.**
- `MO_CODEFIX_CERTIFY` (`shadow|0|gate`, default shadow): the mode. A typo never
  gates.
- `MO_CODEFIX_CERTIFY_LANE_TIMEOUT_S` (90): per-lane-call timeout; also caps the
  two lane calls to `MO_CERTIFY_LANE_RETRIES=0`.
- `MO_CODEFIX_CERTIFY_PROBE_TIMEOUT_S` (120): per-probe pytest timeout. A
  non-numeric or non-positive value falls back to 120.
- `MO_CODEFIX_CERTIFY_PYTHON` (the verifier's interpreter): interpreter for the
  probe pytest runs.

It respects the terms' own knobs: `MO_ASSAY_RELATIONS`, `MO_ASSAY_RELATIONS_K`,
`MO_ASSAY_RELATIONS_VETO_MIN`, `MO_ASSAY_DIFFERENTIAL`, `MO_ASSAY_DIFFERENTIAL_N`
and `MO_ASSAY_DIFFERENTIAL_VETO_MIN`. With a term's master knob at `0` that slot
is `null` and no call is made.

**Evidence.** `verifier_test.json` key `certify` (directly after
`suite_adequacy`) and `<run dir>/certify_terms.json`. Both carry the nine-key
record: `mode`, `status`, `reason`, `poc_file`, `relations`, `differential`,
`verdict`, `vetoed_by`, `would_block`. Per-record `src`/`detail` and the
top-level `src` are stripped, so the evidence is the counts and the reason, not
the generated probe source.

**Cost.** At most 4 lane calls (2 relations × ≤2 tries, 1 differential suite ×
≤2 tries), each bounded by `MO_CODEFIX_CERTIFY_LANE_TIMEOUT_S` (90 s) with
`MO_CERTIFY_LANE_RETRIES=0`. The probe pytest runs are a separate budget: `2k`
for the relations (patch + base per relation) and `2N + 2p` for the differential
(base + head per selected input, plus a base+head confirm re-run for each of the
`p ≤ N` diverging preserve inputs), each run a single file bounded by
`MO_CODEFIX_CERTIFY_PROBE_TIMEOUT_S` (120 s). With the default `k=3`, `N=6` the
pathological ceiling is 30 single-file runs (if every preserve input diverges)
at ≤120 s each — the lane knobs bound the lane calls, not the probes.

## ⑯ Suite strengthening (SWE-ABS, shadow)

**What it checks.** When the ③ adequacy audit returns `INADEQUATE`, the suite
cannot kill its own mutants. SWE-ABS (arXiv 2603.00520) strengthens a suite by
synthesising plausible-but-incorrect patches and keeping the generated tests
that pass gold and fail those patches. mini-ork has no gold patch, so the
incorrect patches are the audit's own surviving AST mutants (deterministic:
regenerated with `suite_adequacy.generate_mutants(..., seed=0)`), and the anchor
is the base tree.

**Trigger.** An `INADEQUATE` audit with survivors and a kickoff in
`run_profile.json`. It runs inside the code-fix test verifier, not as a
workflow node: a `strengthen` verifier node would become `contract` evidence in
`levels`, would run after the verdict was already written, and a crash in an
optional node would fail the run.

**Admission rule.** A generated pytest module is admitted only when it passes on
the candidate and kills at least one targeted surviving mutant, AND its base
outcome is informative:
- **`target`** — it fails on the base: new fail-to-pass evidence, the same bar
  the replay uses.
- **`preserve`** — it passes on the base: it pins behaviour the patch preserved.
- **`candidate_red`** — the candidate fails the generated test. This is the
  SWE-ABS rejection signal, but it is only *recorded*: without gold, such a test
  cannot be told apart from a wrong test.
- `no_kill`, `unanchored`, `abstained` and `inadmissible` are recorded and
  never admitted.

Everything is **shadow**: `pass`, the exit code and every existing payload key
are unchanged. `gate` is reserved — `MO_SUITE_STRENGTHEN=gate` records the mode
but behaves exactly like shadow in this version.

**Knobs.**
- `MO_SUITE_STRENGTHEN` (`shadow|0|gate`, default shadow). A typo never gates.
- `MO_SUITE_STRENGTHEN_MAX_SURVIVORS` (5, range 1–20): survivors to target.
- `MO_SUITE_STRENGTHEN_MAX_TESTS` (3, range 1–10): generated tests to keep.
- `MO_SUITE_STRENGTHEN_TIMEOUT_S` (60, at least 1): per-run pytest timeout.
- `MO_SUITE_STRENGTHEN_BUDGET_S` (240, at least 1): wall budget for the whole
  pass. Past it the lane is not called and no pytest runs; those tests record
  `abstained`, so the pass can never push the verifier past its own timeout.

**Evidence.** `verifier_test.json` key `suite_strengthening` (directly after
`suite_adequacy`) and `<run_dir>/strengthen/` (`t1.py`… plus
`strengthen.json`). The record is twelve keys: `mode`, `status`, `reason`,
`targeted`, `unmatched`, `tests`, `admitted`, `newly_killed`, `score_before`,
`score_after`, `verdict_after`, `would_rescue`. A run without the trigger is
byte-identical to today's payload.

**Cost.** One lane call, plus at most `max_tests` × (2 + `max_survivors`)
single-file pytest runs: 2 (candidate, base) per generated test and one mutant
copy run per targeted survivor per candidate-passing test. With the defaults
that is at most 21 probes; the mutant runs happen in one throwaway copy, and
neither the candidate nor the base tree is ever written.

## Fixes the live runs found

The real-run smokes caught defects that unit tests had hidden. All are fixed:

| Commit | Defect |
|---|---|
| d69cbafd | The hackability proposer dispatched from the mini-ork checkout, so the cwd guard refused every call. It now uses a scratch `MO_TARGET_CWD`. |
| f5aa21ed | `mini-ork verify` evidence logs were named per second, so two verifications in one second shared a file. Names are now unique. Diff paths now name the exact key. |
| fc9a210a | Recipe verifiers imported whichever `mini_ork` the interpreter found, so new verifier code silently abstained. The executor now puts its own engine first on the verifier's `PYTHONPATH`. |
| ad37d26f | `framework-edit`'s verifier nodes required the `verdict.json` they were about to write, which failed 6/6 builds before review. The pre-run guard now exempts verifier outputs, and the full guard re-runs after the verifier. |
| b53b6c0d | Certifying oracle gates read absent evidence as a pass (hackability 0.8). They now defer. |

## Turning a feature off

Set its master knob to `0`, for one run or in `config/secrets.local.sh`:

```bash
MO_ASSAY_RELATIONS=0 MO_ASSAY_DIFFERENTIAL=0 mini-ork certify ...      # oracle terms
MO_SUITE_ADEQUACY=0 MO_LEVEL_VECTOR=0 mini-ork run code-fix kickoff.md # code-fix gates
MO_PROMOTION_GATE_HACKABILITY=0 mini-ork promote --candidate <id>     # promotion consumer
MO_ORACLE_INTEGRITY=0 MO_JUDGE_VALIDITY=0 MO_REWARD_EVIDENCE_PAIRING=0 MO_VERIFIER_MUTATION=0 MO_ASSAY_DISCRIMINATION=0 MO_ASSAY_HIDDEN=0 mini-ork certify ...  # 2026-10 review opt-outs
```

With a knob at `0`, verdicts are byte-identical to the pre-feature behaviour.

## Operating notes

- **What changes when everything is on.**
  - `code-fix` publishes only when the target is proven: a green replay overlap plus an adequate suite, or one the
    audit doesn't apply to. Runs that used to publish on a green suite alone now end `failed`, with the fix kept in
    the tree.
  - For a repo whose test command has no per-test adapter (not pytest/jest/vitest, no results file), the replay falls
    back to an **opaque exit-code delta**: the command runs unchanged on both sides, and the suite is proven when the
    candidate exits 0, the base exits non-zero, and both logs show tests actually ran. A command that cannot run
    (rc 126/127/77) or prints no test-run marker stays `n/a`/`unverified` — never a false PROVEN.
- **Cost.**
  - Certify makes about 2 extra LLM calls per judgement and up to about 2k + 2N extra sandbox runs.
  - Code-fix adds up to 2 + `MAX_MUTANTS` suite runs per green verifier.
- **Reviewer interaction.** The glm reviewer already rejects runs whose verifier reported `pass:false`. The level gate
  matters most where the verifier is *green but unproven*, which the reviewer approves.
- **Honest limits (2026-10 review).** One instrument is **OFF by default** because it changes promotion outcomes:
  `MO_JUDGE_VALIDITY` (reviewer-vs-execution agreement). It abstains rather than blocks when unmeasured — it never
  converts abstention into a pass — so flipping it to `1` is a deliberate promotion-policy change, not a
  quality-of-life toggle. (The split-conformal abstention gate, `MO_CONFORMAL_ORACLE`, was removed in 2026-10.)
- **Hidden-check hiding is strongest with a secret.** With `MO_HIDDEN_CHECK_SECRET` set, the variant names are
  unpredictable to the implementer. With only `run_id` or the patch digest they are reproducible by anyone who knows
  those inputs — so the certificate records `key_source` rather than implying secret-backed hiding.
- **Honest limits (2026-10 review).** Hidden-check hiding is strongest with `MO_HIDDEN_CHECK_SECRET` set; with only
  `run_id` or a patch digest the variants are reproducible by an implementer who knows those inputs, and the certificate
  records which source was used (`key_source`) so a run never claims secret-backed hiding when no secret was supplied.

## Re-running the live checks

Each feature's real-run smoke recipe is in `kickoffs/auto/vt<N>-*.smoke.md`, next to the kickoff that built it:

| # | Feature | Kickoff |
|---|---|---|
| 1 | relations | `vt1-mr-relations` |
| 2 | hackability | `vt2-hackability-audit` |
| 3 | equivalence | `vt3-equivalence-operator` |
| 4 | differential | `vt4-differential-delta` |
| 5 | adequacy | `vt5-mutation-adequacy` |
| 6 | level vector | `vt6-level-vector` |

Point `MINI_ORK_HOME` at the live home (which holds the secrets), and launch from outside the checkout.
