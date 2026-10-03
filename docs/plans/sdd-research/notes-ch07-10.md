# SDD book ch07–ch10 — distilled design input for mini-ork spec-driven development mode

Source: /tmp/sdd-book/ch07.md (CodeT), ch08.md (repository-scale SSDE), ch09.md (VibeContract/DbC), ch10.md (SGRM governance). Distilled 2026-10-03 for the mini-ork SDD-mode design (directory of .md specs → autonomous plan→implement→verify delivery with live smoke + UI/UX gates).

---

## 1. Test-first generation mechanics (ch07 — CodeT)

**Core pipeline (3 stages, strictly ordered):**
1. **Generate tests FIRST** from the problem statement — before any implementation exists. The test suite *is* the executable specification (Spec-Anchored tier of the ch03 maturity model).
2. **Generate N independent code candidates** — independent sampling events (different temperatures/contexts), never N copies from one event. Independence is what makes consensus meaningful: shared-origin candidates share hallucination patterns.
3. **Dual-execution agreement** — run every candidate against every test; group candidates into *consensus sets* (equivalence classes passing exactly the same tests); score each set; ship the winner.

**Scoring function:** `score(C) = n × √(p × t)` where n = number of agreeing candidates (linear — every extra independent agreement counts fully), p = pass rate, t = tests passed (square-rooted — diminishing returns stop trivial-test gaming). Worked example: 4 solutions passing 25/30 tests (score ≈ 19.96) beats 1 solution passing 30/30 (score ≈ 5.48). Rationale: convergence of independent attempts is unlikely under independent error — failures are diverse, truths are convergent (RANSAC analogy).

**Test-validity gate (promotes stochastic tests to probabilistic oracles):**
- Run generated tests against *intentionally broken baselines* (always-returns-None, identity function, empty-string return, off-by-one variant of a known-good). A test that never fails any broken baseline is decoration — drop/rewrite/flag it.
- Cheaper complement: *coverage check against the spec text* — every test must reference ≥1 property in the problem statement, every stated property must be exercised by ≥1 test. Catches vacuous tests, not mis-specified ones; use both.

**Prompt design for test generation:**
- Deliberately STRIP worked input/output examples from the test-generation prompt — worked examples cause template-matching (model writes tests mirroring the example and approves whatever it already believed).
- Explicitly ask for adversarial/edge/boundary cases — the goal is tests tailored to break the model's own likely failure modes, not QA-balanced suites.

**Economics & metrics:**
- CodeT ≈ 10× inference cost per accepted solution; evaluating samples is orders of magnitude cheaper than generating them, so filter-heavy beats prompt-heavy.
- Optimize for pass@1 (deployment truth), never pass@k (theoretical ceiling). CodeT lifted HumanEval pass@1 47% → 65.8%.
- Prompt engineering = stochastic (nudges distribution, stylistic); tests = deterministic at runtime, stochastic at authoring (filter). Layer them; tests get the final word.
- Residual risk: consensus around a *shared misconception* no test exposes → addressed by contracts (ch09).
- Composes with role agents: dedicated Tester agent authors the suite, Engineer agent authors candidates — the pipeline is the same.

---

## 2. Repository-scale SDD (ch08 — SSDE, SynFix RelationGraphs, NL2Repo)

**Why structure beats prose at repo scale:** prose loses information faster than the system gains complexity. SSDE's single move: replace ambiguous NL requests with formal artifacts (UML sketches, state machines, AST dependency graphs) that are denser and mechanically checkable. Split them by role: forward-looking specs (UML/state machines) *drive* generation of new modules; ground-truth graphs (AST dependency) *constrain* generation against existing code.

**SynFix RelationGraph (constraint side — "graph in, patch out, propagation check"):**
- Nodes = individual code elements (functions, classes, constants), not files. Typed edges: Defines/References, Reads/Writes (shared mutable state), Inherits/Implements/Overrides (dispatch), Catches/Throws (exception flow — invisible to call graphs).
- Serialize the *relevant subgraph* into the prompt as a structured doc: affected elements, typed edges, short NL summary per cluster, plus an explicit scope constraint ("edit only X unless the fix requires touching the relation"). Don't make the model rediscover dependencies — show them.
- **Propagation check after the patch:** walk the graph treating the patch as a perturbation; list every downstream element with a now-stale assumption (callers missing a new parameter, readers of changed-semantics state). Emit a propagation report gate before merge. This catches "the fix that broke the build".
- Principle: SynFix doesn't make the model smarter, it makes the environment smaller.

**NL2Repo hierarchical sketching (driving side — 3 locked layers):**
1. *Repository sketch* — directory tree + justification per top-level dir. Commits to shape only.
2. *File sketch* — per package: files, imports, signatures of functions/types; bodies deliberately empty (`panic("not implemented")` as contract marker). Pins interfaces without implementations.
3. *Implementation* — bodies only, must respect both upper sketches.
Architectural coherence guarantee: the model cannot move functions across boundaries (layer 1 pinned them), change public signatures (layer 2 pinned them), or introduce circular imports (tree excluded them). Drift is bounded by the sketches. Costs more tokens/latency (intent processed 3×) but failure rate on multi-file tasks drops sharply and debugging localizes to the offending layer. Each layer can be its own agent; the artifact is the handoff contract.

**Cross-cutting guardrails:**
- *Tiered context assembly* with explicit guarantees: skeleton (always present, deterministic) → local slice (touched files + importers, budget-bounded) → RelationGraph summary (exact within perturbed cluster) → retrieval-augmented chunks (best-effort). Mix deliberately; lossy retrieval at the wrong layer makes the model hallucinate type definitions.
- *Style contracts*: analyzer extracts existing conventions (naming, error style, allowed imports per package) into a small declarative doc; fed to the model at generation AND enforced by a post-generation linter. Double-pass enforcement.
- *Verification hierarchy*: unit tests (cheap, first line) → contract tests (catch compile-ok-but-breaks-downstream signature changes) → **regeneration loops** (regenerate impl from spec, run suite, diff against previous impl: empty diff = no drift; small = intentional, review; large = investigate/rollback). Regeneration loops are the operational form of Spec-as-Source.

---

## 3. Contracts as gates (ch09 — VibeContract / Design by Contract)

**Position in the stack:** contracts sit one layer above CodeT. Tests sample the input space (behavioural mistakes on enumerated inputs); contracts state structure over ALL inputs (structural mistakes on inputs no one enumerated). Tests don't compose; contracts can state cross-component obligations ("after this method returns, the cache holds exactly one entry per key"). Both layers, tests never replaced.

**Why tests alone are insufficient for LLM output:** (1) tests sample a vanishing input fraction and no human mental model backs the unsampled rest; (2) vacuous satisfaction — a model told "pass these tests" finds the cheapest pass (return null/constant); (3) no composition.

**Obligation shape (one structured object per obligation):** name, NL gloss (for human review), formal predicate (for the checker), severity level (blocker vs warning), checker reference, inputs the checker must exercise. Four positions: precondition, postcondition, error postcondition (named error variant on failure — not generic exceptions), invariant (state preserved across calls — hardest to translate, what makes regeneration safe).

**Cross-tabulates with a four-component contract taxonomy** (also SGRM's, ch10): functional obligations (→ postconditions), quality constraints (→ postcondition + temporal/resource budget, e.g. "terminates within 100ms on benchmark input"), constitutional constraints (what code must NEVER do → precondition on forbidden inputs / safe-return postcondition), architectural structure (→ module-level invariants).

**Specificity rule:** contracts must be specific enough that the cheapest way to satisfy them is to do the right thing — exact arithmetic, named error variants, explicit return-type discriminators; never loose relational predicates (`Result >= 0` is satisfied by `abs(Result)`).

**Contract-language requirements** (what separates it from a thin assertion wrapper): closed under composition (contract A can reference contract B, checker chains them), open under evolution (add new obligation types without rewriting the checker), supports sound weakening (Liskov).

**Stochastic-generator deltas vs classical DbC:** the generator doesn't understand contracts (statistical association only); no persistent commitment — every regeneration is a fresh agent, so the contract must be re-stated and re-verified every cycle; no incentive gradient — checker verdicts must be precise enough that the next regeneration can actually use them to improve. Contracts are *provisional*, not static: expect some to be wrong, and support rewriting them (immutable contracts rot into encoding model behaviour instead of team intent).

**Three enforcement tiers (enforcement decomposition — same obligation checked at three latencies by three agents):**
1. *Human review* — contracts shown beside the diff; only tier that resolves ambiguity; verdicts are verbs (approve/revise/escalate).
2. *Automated assertion* — per-obligation checker runs during generation (type check, property test, static rule, symbolic execution, SMT); binary verdict with trace; catches routine failures.
3. *CI gating* — full suite on clean checkout, merge blocked on any failure; catches cumulative/compositional failures and contract drift.
Any single tier alone is brittle; interleave all three.

**Workflow = three decompositions:** intent decomposition (high-level intent → atomic task sequences, each contract-sized) → obligation decomposition (each task → structured obligations) → enforcement decomposition (each obligation → three tiers). A task that can't be written as a clean contract signals the task does too much or the intent is ambiguous — split it or escalate to a human; surface ambiguity at the contract layer where it's cheap.

**Separation of authorship as integrity defence:** implementation agent proposes contract draft; a SEPARATE verification agent critiques it against the original intent; team/human arbitrates. Keep the contract checker in a separate codebase with its own review so contracts and satisfying code aren't judged by the same authority.

**Failure modes to engineer against:** wrong contract (well-formed, passes, wrong property — from mis-translation, mis-specification, or spec drift; defend with human review + property-based adversarial tests + cross-check against external specs); over-constrained contract (pins algorithm/data-structure/unobservable state → brittle, team afraid to regenerate; audit quarterly, delete obligations that don't pay); contract theater / illusion of correctness (rituals present, substance absent; contracts mirror the authoring team's blind spots; defend with independent review and expecting production defects — coverage ground truth).

---

## 4. Governance & audit (ch10 — SGRM)

**SGRM = vendor-neutral reference model treating every spec as a machine-readable four-component contract** (functional / quality / constitutional / architectural). Each component gets its own validation strategy: executable tests / benchmark+observability probes / static analysers+policy gates / dependency-graph validators. A spec with all four gives the validator a complete rejection surface; swap generators without re-engineering compliance.

**Six design propositions worth building in:**
- DP1 *Determinism Boundary* — a deterministic validator V(g) → bool bounds LLM acceptance; stochastic generation becomes a controlled workflow.
- DP2 *Composition* — specs compose hierarchically; sub-specs INHERIT constitutional constraints from parents; a child can never relax a safety property.
- DP3 *Verification by Construction* — derive checks directly from the spec (not post-hoc observation); every accepted generation checked against the same source of truth.
- DP4 *Traceability* — every accepted artifact carries a structural link to the spec clause that authorised it, by construction. Audit reduces to static queries ("which clause covers requirement X, does the pipeline enforce it"), no forensic archaeology.
- DP5 *Regeneration* — the spec is the ONLY mutable artifact; code is regenerated, not patched; the codebase is a derived projection of the spec.
- DP6 *Human Override* — reviewers may veto acceptance; the veto is LOGGED against the authorising clause (override trail is itself a governed artifact).

**Maturity ladder:** Spec-First (advisory) → Spec-Anchored (contract tests + CI enforce spec↔code link; humans can veto but not silently edit the spec) → Spec-as-Source (code fully derivative; regeneration cadence a first-class trade-off). Spec-Anchored is the pragmatic enterprise middle.

**Tooling patterns to borrow:** Aider Repository Map (PageRank-weighted call/import-graph compression to fit context; lossy — mitigate with on-demand file fetch + explicit include hints); GitHub Spec Kit / Tessl *probes* (small deterministic read-only functions run BEFORE planning/implementation returning repo-grounded facts — call-site counts, signature existence — to prevent API hallucination); Tessl spec-as-library (one spec clause drives both implementation and its test scaffold); BDD Given/When/Then (Cucumber/Specmatic) as the human↔AI alignment grammar — scenario is a contract checkable against any conforming service via existing CI.

**Open seams (don't over-promise in design):** validator horizon (plans whose state space exceeds V's capacity admit unverified variance — long-horizon repo-scale work is the frontier); temporal stability (pin generator AND validator versions, temperature, seed — a foundation-model upgrade can flip verdicts); sustainability (regeneration cadence has compute cost — consider a budget clause); benchmark validity (validators tuned on contaminated benchmarks inherit bias).

---

## 5. Direct design implications for mini-ork SDD mode

- Parse each .md spec into the SGRM four-component contract (functional / quality / constitutional / architectural) and derive every gate from it, so acceptance is a deterministic validator verdict, not a reviewer-LLM vibe.
- Generate the test suite and smoke specs from the spec BEFORE dispatching the implementer, strip worked examples from the test-generation prompt, and run a test-validity gate (broken-baseline + spec-coverage checks) so smoke specs can't be vacuous or unpassable.
- For high-value steps, sample N independent implementation candidates and pick the winner by CodeT consensus scoring `n × √(p × t)` instead of accepting the first green run.
- Run intent decomposition first: split each spec file into atomic, contract-sized deliverables (one kickoff each), ordered by dependency — never feed an oversized spec to one planner call.
- Lock structure top-down NL2Repo-style: plan emits a repository/file sketch (dirs, files, pinned signatures) as a reviewed artifact, and the implementer may only fill bodies that respect it.
- Feed the implementer a tiered context pack with explicit guarantees — skeleton (always), touched-files slice (budgeted), dependency-graph summary of the perturbed cluster, best-effort retrieval last — plus a style contract extracted from the target repo.
- After every patch, run a propagation check over a typed dependency graph (callers/readers/throwers of touched elements) and fail the cycle with a propagation report if downstream assumptions went stale.
- Enforce three tiers per obligation — in-cycle automated assertions, CI/live smoke gate, and a logged human veto point — and record every accepted artifact with a structural link back to the spec clause that authorised it (spec-clause → commit → test provenance ledger).
- Make the spec the only mutable artifact: failed cycles regenerate from the spec rather than patch the patch, contracts are re-stated and re-verified on every regeneration, and spec edits are the only way to change behaviour.
- Keep authorship separated: the agent that writes contracts/tests must not be the agent that implements, and the verifier payload (not the implementer's self-report or an advisory rubric) is the only authoritative verdict.
