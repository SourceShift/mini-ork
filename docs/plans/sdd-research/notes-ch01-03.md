# SDD Book — Design Notes, Chapters 1–3

Distilled for: mini-ork spec-driven-development mode (dir of .md specs → autonomous plan→implement→verify loops with live smoke + UI/UX gates).

---

## 1. Techniques & named methods

| Technique / method | Chapter | Relevance to SDD mode |
|---|---|---|
| **Capability hallway** (Codex-era → GPT-4-class → agentic era), each generation paired with its eval surface: per-function unit tests → hidden test suites → full CI run + coverage | ch01 §1 | Agentic repo-scale work demands CI-grade verification, not unit snippets |
| **Five-phase SDLC benchmark mapping** (requirements, design, coding, testing, maintenance; 926 studies, 112 tasks) | ch01 §2 | Pipeline stages should be tagged by SDLC phase; coverage is dense at coding/testing, thin at requirements/design — spec parsing fills the thin end |
| **Multi-task SE benchmark** — 11 LLMs × 5 tasks (bug fixing, feature development, refactoring, technical copywriting, research synthesis), automated end-to-end to resist saturation | ch01 §3 | Mirror: a feature queue mixing task types needs per-task-type verdicts, automated end-to-end |
| **Three cracks of multi-task evaluation**: contamination, weak tests, replicability | ch01 §3 | Weak tests = gate tests that pass wrong behavior; audit gate specs before trusting verdicts |
| **Replicability protocol**: document exact model version, temperature, random seed (guidelines paper, 1,200 reproducibility entries) | ch01 §3 | Log lane/model/params per cycle in the run ledger |
| **Seven study types taxonomy** — classify by *what was varied* (model / prompt / context / fine-tune / evaluation / prompt-engineering / human) | ch01 §3 | When a cycle fails, attribute: was it the spec, the context assembly, or the model? Vary one axis at a time |
| **Context engineering as evaluation surface**; retrieve-rank-prompt pipeline is what a score measures, not the model | ch01 §4 | The SDD context assembler (what the implementer sees) must be versioned + logged; same spec + different context = different outcome |
| **CL4SE direct vs indirect context split**: direct (target file, failing test, error trace, API sig) improves accuracy; indirect (README, style guide, CI config) improves coherence but can hurt accuracy | ch01 §4 | Correctness cycles get direct context first; style/UI-craft cycles get indirect context |
| **Spec as context-engineering artifact** — "the spec closes the door": structured, versioned, retrieval-friendly, constrains the generation surface | ch01 §4 | The whole point of the mode: spec = frozen context, making runs evaluable |
| **Vibe coding** (ask→observe→accept loop) vs **intent-specified development** (spec = contract, code = derivative artifact, acceptance = verdict) | ch02 §1 | The mode mechanizes intent-specified development; acceptance must be a verdict from a gate, never an LLM "looks right" |
| **Specification depth dial** — how much behavior is written in machine-verifiable form before generation | ch02 §4 | Spec intake should measure depth and route: too shallow → elicit/flag; adequate → run |
| **SPACE dimensions** (Satisfaction, Performance, Activity, Communication, Efficiency); 85% of studies measure ≤3 lenses → speed wins get published while reliability losses go unmeasured | ch02 §2 | Campaign metrics must pair a speed lens with a reliability lens (cycles-to-green AND post-merge defects/waived gates) |
| **Three-properties rule** for spec depth: #readers, lifetime, failure cost — any one large ⇒ full spec | ch02 §4 | Intake heuristic for how deep a gate suite each spec needs |
| **Formality × verifiability spectrum**; structured intent = markdown with named sections (Inputs/Outputs/Errors/Examples), YAML, JSON Schema + examples | ch03 §1 | Target normal form for ingested specs |
| **SDD maturity ladder**: T1 Spec-First (advisory, drift allowed) → T2 Spec-Anchored (contract tests in CI, spec+code equal partners) → T3 Spec-as-Source (deterministic regeneration) | ch03 §2 | Mode should operate at T2; T3 only where generator is deterministic |
| **Prompt vs specification 4-property test**: stability (versioned/reviewed), verifiability (pass/fail procedure), completeness (inputs/outputs/errors/edges), consumer neutrality (model-independent) | ch03 §3 | Intake classifier: an .md failing the test is a prompt, not a spec |
| **Unrelated-team regeneration test**: could another team rebuild the behavior from the artifact alone? | ch03 §3 | The litmus question the intake validator should encode |
| **Tier-2 tooling triad**: contract tester + CI hook (triggers on spec OR impl paths) + regenerator (spec diff → code diff, reviewed together) | ch03 §4 | The three components the mini-ork mode must provide per feature |
| **Pact-style replayable contract files**; property-based tests (1000 generated cases) | ch03 §2–3 | Gate artifacts should be replayable files, "a number, not an opinion" |

## 2. Spec format requirements

A machine-consumable spec (the normal form the mode should ingest or elicit):

- **Named sections a parser can carve**: Inputs, Outputs, Errors, Edge cases, Examples (ch03 structured intent).
- **Acceptance criteria as executable assertions** — each criterion maps to a procedure returning pass/fail (ch03 verifiability axis; ch02 "gate rejects what does not match").
- **Error conditions enumerated by name** (e.g. EMPTY_CART / INVALID_QTY with trigger conditions) — prompts define the happy path; specs define the edges (ch03 worked contrast).
- **Invariants** stated separately from examples (`total == sum(qty × unit_price)`).
- **Typed inputs/outputs** where possible (JSON Schema-grade; length is irrelevant — a 7-line schema with one invariant is a spec).
- **Consumer neutrality**: no model names, no prompt templates, no "ask the AI to…" — describes *what* must be true, not *how* to ask.
- **Versioned in git with diff history** — the spec is the unit of change; spec diffs reviewed alongside code diffs.
- **Depth matched to risk** (three-properties rule): module-public-contract level (4–5 observable behaviors with status codes) is the production sweet spot; full boundary enumeration (rounding, idempotency, downstream-failure contracts) only when failure cost is high.
- **Maturity declaration**: which tier this spec intends (T1 draft vs T2 anchored) so the pipeline knows whether gates are advisory or blocking.

## 3. Verification / gate design

- **Behavioral correctness, not compilation**: the signature LLM failure is code that compiles yet violates semantics (ch01). Gates must execute the artifact (live smoke, runtime assertions), never settle for build/lint green.
- **Gate quality is a first-class risk ("weak tests" crack)**: a passing suite is meaningful only if it discriminates. Audit gate specs for vacuousness/unpassability *before* the campaign runs (matches researcher memory: 20/32 wave-4 smoke specs were unpassable or vacuous).
- **Replayability**: each gate is a versioned file that re-runs against today's implementation and returns a number, not an opinion (Pact model). Verifier verdict ≠ LLM judgment.
- **CI-hook semantics**: gate triggers on changes to *either* the spec or the implementation — drift in either direction caught on the commit that introduced it.
- **Context must be pinned per evaluation**: a verdict without the context construction recorded is unattributable (ch01 worked example: same model+task, two retrieval pipelines, opposite outcomes).
- **Replicability logging**: model version, temperature, seed, lane per cycle; non-determinism at temp>0 means a single green is one observation, not the answer.
- **Pair speed and reliability lenses** in reporting (SPACE lesson): cycles-to-green alone will reward vibe-grade output.
- **Property-based generation** for data-shaped contracts (1000-case runs) where the spec states invariants.
- **Separate functional gates from craft gates**: function (contract satisfied) and UI/UX craft (design source honored) are different axes; green-on-function does not imply craft (matches memory: RSI verdicts prove function, not craft — unstyled scaffolds ship green).

## 4. Failure modes to design against

**From ch02 (vibe-coding clusters — what the pipeline exists to prevent):**
1. **Silent logic errors** — survive the manual acceptance loop because the loop is the surface they hide under; ad-hoc acceptance rituals catch only familiar errors.
2. **Hidden coupling** — generator optimizes for the context window slice, not the architecture; local optima become global mistakes no later reviewer can recognize.
3. **Drift** — intent lives in 3 places (original prompt, author's memory, latest revision) and diverges with each regeneration; invisible per-commit, unmistakable over months.
4. **Unverifiable behavior** — inputs not enumerated, edges untested; "worked on my laptop" scaled up.
5. The chain **compounds** (silent errors hide in coupling, coupling accelerates drift, drift kills verifiability); a spec cuts it at the first link.
6. **Over-specification** (opposite pole): spec so detailed the human did the work and the model is a typist — intake should flag specs that embed the implementation.
7. **Cognitive offloading / private vibes**: two agents generating against private context diverge; merge becomes translation — shared spec is the coordination artifact.

**From ch01 (benchmark pitfalls → gate pitfalls):**
8. **Contamination** — model has seen the task; for SDD: implementer may have seen the smoke test — keep hidden/holdout assertions where feasible.
9. **Weak tests** — aggregated noisy task-test pairs; a multi-feature campaign inherits the noise of every bad gate.
10. **Replicability loss** — undocumented model/temp/seed makes failures undebuggable.
11. **Benchmark saturation / stale ground truth** — gates reverse-engineered from current code (instead of authored from intent) are flaky and get abandoned (also ch03 §4 unsafe migration).

**From ch03 (adoption/organizational failure modes):**
12. **Org-wide big-bang conversion** — generating a mountain of specs from existing code at once ⇒ flaky reverse-engineered contracts, ignored spec diffs, "SDD doesn't work" in six months. Climb per-feature.
13. **Slide-back** — contract failures routinely waived, or spec-touching PRs outnumber impl PRs ⇒ spec decoupled from code while tooling still looks T2. These two gauges are early-warning metrics.
14. **Theatrical tiers** — formal spec + hand-edited code, or long prompt promoted to "our spec" (fails the 4-property test).

## 5. Direct design implications for mini-ork SDD mode

- Intake must classify every .md with the 4-property test (versioned; inputs/outputs/errors/edges defined; pass/fail procedure derivable; model-neutral) and route failures to an elicitation step instead of running them as prompts.
- Normalize accepted specs into structured-intent form — named Inputs/Outputs/Errors/Edge-cases/Examples sections plus acceptance criteria each mapped to one executable gate assertion.
- Run every feature as a Tier-2 Spec-Anchored loop: the spec file is the sole anchor, each regeneration starts from spec + latest gate results (never chat history), and the gate rejects any artifact that doesn't match.
- Gates must execute behavior (live smoke against a running surface, property-based runs for invariants), never accept compile/lint green as a verdict.
- Audit the generated gate suite for vacuous or unpassable assertions before dispatching the first implementer cycle — weak tests silently convert the whole campaign to vibe coding.
- Record per cycle: lane/model version, temperature/seed where available, and the exact context bundle fed to the implementer, because the context construction — not the model — is what a verdict measures.
- Assemble context deliberately: direct context (spec, failing gate output, target files) for correctness cycles; indirect context (design sources, style guides) only for UI/UX-craft cycles, which get their own separate craft gate.
- One spec = one feature = one kickoff/run (per-feature climb); never batch the spec directory into a single plan, and verify the triad per feature: contract gate + trigger-on-spec-or-impl hook + regenerator.
- Surface slide-back gauges in the campaign ledger: count of waived/red-overridden gates and spec-edit vs code-edit ratio, alerting when gates are being bypassed.
- Report a reliability lens next to the speed lens for every delivered feature: cycles-to-green alongside post-merge gate stability, so fast-but-fragile deliveries are visible.
