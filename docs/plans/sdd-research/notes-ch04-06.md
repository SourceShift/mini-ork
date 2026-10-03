# SDD Book — Design Notes from Chapters 4–6

Distilled for: mini-ork "spec-driven development" mode — a flow that ingests a directory of .md
spec files and autonomously delivers each feature through plan→implement→verify loops with live
smoke tests and UI/UX gates.

---

## 1. Role/agent topology (ch04 — The Virtual Software Company and Role-Based Agents)

### Canonical role set (the "assembly line")
Four fixed workstations, each a role-separated agent with a written job description (SOP):

| Role | Reads | Emits (typed artifact) |
|---|---|---|
| PM agent | user intent / source spec | PRD: user stories, acceptance criteria, non-functional reqs, out-of-scope |
| Architect agent | PRD | Design doc: modules, interfaces, data model, sequence of interactions |
| Developer agent | PRD + Design | Source code (directory of files) |
| QA agent | Code + PRD (+ Design) | Test plan / test report; executes tests, feeds failures back |

The only backward arrow in the healthy pipeline: **Verification → Implementation** (executable
feedback loop). Failure reports are typed artifacts too.

### Handoff artifacts & protocol rules
- **Every hand-off is a typed document** with a fixed shape (known required sections). PRD ≠ design
  ≠ code ≠ test plan. Each artifact is a file: storable, diffable, human-reviewable before the next
  phase starts.
- **Schema-validated gates between agents**: every artifact carries a JSON Schema (book shows
  draft/2020-12 examples, e.g. PRD requires `prd_id` matching `^PRD-[0-9]{4}$`, `user_stories`
  minItems 1, `acceptance_criteria` minItems 1, `additionalProperties: false`). Malformed artifact →
  rejected at the gate, looped back to the emitting agent — never cascades downstream.
- **Constitutional constraints per role**: machine-checkable rule "this role emits artifacts of type
  X only" (allowed_artifact_types enum). Rejects role drift (PM writing code, dev rewriting
  requirements) at the schema gate.
- **Communicative dehallucination (ChatDev)**: an agent missing information MUST ask, never guess.
  Clarifying question goes upstream (dev → PM → human if PRD is silent); pipeline does not advance
  until resolved. Cost asymmetry: one chat turn vs a whole wasted code/test/revise iteration.
  Empirical: ChatDev composite quality metric 0.1523 → 0.3953 with dehallucination + structured
  hand-offs enabled together.
- **Spec ratification diff**: PM's PRD must be ratified against the original requirements list
  before the architect reads it; any requirement in the source list absent from the PRD is a flagged
  diff item requiring explicit acknowledgement (prevents "lost context" silent requirement drops).
  Audit trail preserved as a versioned spec diff.

### Framework variants
- **MetaGPT**: fixed roles, long phases, SOPs baked verbatim into prompts (exact section lists,
  format rules, "do not write code, stop after section 5"). Executable feedback: engineer's code is
  run, crash trace fed back into next iteration.
- **ChatDev**: lifecycle sliced into atomic subtasks; each subtask = one short two-agent chat ending
  in one small typed artifact ("relay race"). Insight: many short chats stay coherent where one long
  chat forgets/contradicts itself.
- **AgileCoder**: dynamic role assembly per task class (refactor → navigator + parallel developers;
  bug fix → reproducer + debugger + fixer). Handles long-context repository work. New failure
  surface: role-selection itself can be wrong.

### When NOT to orchestrate (cost/latency cracks)
- Role pipeline ≈ 5× token cost and serial latency; worth it only when hallucination cost is high
  (long-lived, security-sensitive, human-maintained code).
- Single model wins for: tight self-contained generation (regex, small function), latency-sensitive
  interactive Q&A, low-stakes throwaway prototyping.
- Context-window pressure: chain of agents each reading prior outputs blows the window; SDD
  mitigation = each agent reads the **tight ratified spec**, not the cumulative prior outputs.
- Once a spec is ratified, regeneration of the artifact is amortised — spec is source of truth.

---

## 2. Scaffolding requirements (ch05 — Architectural Scaffolding for Autonomous Coding Agents)

### Four-layer agent runtime anatomy
1. **Tool abstraction layer** — primitive ops: read/write file, run shell, search, invoke LLM.
   Ambiguous tool descriptions are a scaffolding failure that cascades (wrong tool/wrong args).
2. **State management layer** — conversational context, tool-call history, evolving workspace state;
   the agent's short-term memory across multi-step tasks.
3. **Context retrieval layer** — fetch only relevant snippets; Aider's PageRank-derived repo map
   compresses the codebase into a navigable "table of contents" (call graph + import relations →
   smallest file set with max navigational context).
4. **Execution & feedback layer** — propose → run → report result back. Real execution (tests,
   compile, lint) is what enables self-correction; without it the agent is flying blind.

Book claims ≥12 architectural dimensions determine agent reliability (tool design quality, state
representation, context selection, execution isolation, error recovery, cost control...). Agents
often fail on SWE-bench because of scaffolding, not model weakness.

### File-system guardrails (three)
- **Scope restriction** — path resolution confined to project root (deny anything escaping it).
- **Atomic operations** — write to temp file then rename; no partial-write corruption.
- **Backup snapshots** — preserve pre-change state for rollback.

### Version control integration (three capabilities)
- **diff reading** — understand semantic impact vs last known-good state before committing.
- **branch management** — isolated feature branch, never disrupt main.
- **log inspection** — commit history as context pure file-reading can't provide.
Poor VCS integration → silent regressions.

### Build/test harness exposure
- Run test suite, capture pass/fail + stack traces; lint + static analysis (ruff/mypy/eslint);
  incremental rebuilds.
- **Sandboxed subprocess with timeout + resource limits** — runaway test or infinite loop must not
  freeze the agent system.

### Verification stack (defense in depth; no single method suffices)
| Depth | Checks | When | Latency |
|---|---|---|---|
| Fast/shallow | lint, format | every change | seconds |
| Medium | type check, contract verification | every PR | minutes |
| Deep | property-based tests, integration tests | on merge | tens of minutes |

- **Static analysis**: deterministic, runs on any snippet anytime; catches untestable classes
  (null derefs, injection, sensitive-API misuse).
- **Property-based testing**: invariants over thousands of random inputs; exposes edge cases the
  agent didn't think to test.
- **Contract checking (DbC)**: pre/postconditions derived from the spec, checked around each call,
  catches semantic violations regardless of "looks right" or passing unit tests.
- SDD framing: scaffolding must expose enough capability for the agent to **prove** spec compliance
  (run tests, static analysis, property checks, report to the verification layer).

### AR vs bidirectional architectures (practical note)
AR models fit generation; comprehension of large codebases degrades. Compensate with retrieval-
augmented context or hierarchical summarization (file-level summaries → import graph → function
detail). Always route generated output through an independent verification layer.

---

## 3. Executable spec formulation (ch06 — Formulating Executable Specifications)

### Definition: a spec is executable when a machine can read it, run it, and confirm/refute a
candidate implementation. Three simultaneous properties:
1. **Parseable** — formal grammar/schema/DSL; failure to parse = rejected at the door.
2. **Carries a total evaluation procedure** — an oracle returns a verdict for *every* input in the
   domain, not just imagined cases.
3. **Deterministic semantics** — two evaluators must agree; stochastic verdicts are not executable.

### Loop invariants (verifier discipline)
- **Spec is read-only/stable during a generation attempt** — agent cannot edit the contract it's
  trying to satisfy ("a mutable contract invites the agent to negotiate itself into a passing
  grade").
- **Verifier is independent of the generator** — a test suite the LLM wrote and the LLM grades is
  theatre. Verifier runs sandboxed, no FS/network access to the generator.
- **Atomic verdict** — exactly `pass` or `fail` + traceable reason; partial credit forbidden.

### Four-layer spec structure (verifier walks top-down, fails fastest on most consequential)
1. **Constitutional constraints** — what it may NEVER do (forbidden behaviours, security
   invariants, regulatory caps). Outranks everything.
2. **Quality constraints** — how well (latency bounds, error budgets, thresholds — every one a
   number with a unit, probability with sample size, or count with bound).
3. **Functional obligations** — what it must do (pre/postconditions, type signatures, I/O tables).
4. **Architectural structure** — where each obligation lives (module boundaries, dependency
   direction, ownership).

### Spec language selection (decision tree)
- Catastrophic failure + concurrent/distributed → TLA+; structural invariants → Alloy.
- Data shape / API contract → OpenAPI + JSON Schema; business workflow → Gherkin; policy → Rego;
  service interaction → Pact; data validation → CUE; DB shape → SQL DDL + constraints.
- Mixed logic → one rigorous language per layer; mixing DSLs is normal.
- One-off script → skip executable contract (Spec-First tier).
- Sanity check: list obligations the chosen language cannot express; non-empty list ⇒ those go
  unverified by automation — broaden toolset or accept review-only enforcement.

### CURRANTE-style three-phase, three-gate workflow (Markdown + structured frontmatter contracts)
- **Phase 1 Narrative intent** (prose, then FROZEN/immutable for the run) → **Narrative gate**
  catches *missing intent*.
- **Phase 2 Contract** (YAML clauses: `given/when/then/except`, example I/O) — validator enforces
  bidirectional traceability: every clause has a narrative antecedent AND every narrative claim has
  a clause; phase ends when the validation report is empty → **Contract gate** catches
  *unverifiable intent*. LLM may draft clauses; human verifies (role shifts from authoring to
  verification; gate stays).
- **Phase 3 Generation + verification** — agent generates; sandboxed verifier (test runner +
  property-based checks) returns pass / fail-with-reason / contract-unsatisfiable. Unsatisfiable
  loops back to Phase 2, **never** Phase 1. Human checkpoint at every phase transition — an
  autonomous pipeline could rewrite contracts to make them pass.
- Contract example shape:
```yaml
- id: refund_on_cancel_within_cooling_off
  given: [order.status == 'cancelled', cancellation.timestamp within order.cooling_off_window]
  when:  [refund.requested == true]
  then:  [refund.amount == order.total_amount, refund.method == order.payment_method,
          refund.settled within 5 business days of refund.requested]
  except: [order.payment_method == 'gift_card' (store credit instead)]
```
- Contract specifies WHAT, never HOW (no API call / DB / queue named).

### Seven authoring errors (lint rules for a spec-intake validator)
1. **Vague acceptance criteria** — "responds quickly"; diagnostic: clause passes all random inputs ⇒
   exercises nothing. Fix: number+unit / probability+sample size / count+bound.
2. **Implementation leakage** — spec names mechanism ("uses Redis", "POST to gateway"); diagnostic:
   would the contract change if reimplemented in another framework? Fix: inputs/outputs only.
3. **Missing negative cases** — every clause ends in success; fix: ≥1 negative clause per positive;
   PBT (Hypothesis/QuickCheck/fast-check) enumerates violations.
4. **Over-specification** — contract longer than the implementation it would produce; forecloses
   correct implementations the author didn't imagine. Keep to invariants + observable behaviour.
5. **Tests masquerading as spec** — clauses bound to internal symbols; diagnostic: rename an
   internal function, count broken clauses. Contract = public behaviour; tests enforce it.
6. **Spec/code drift** — co-version both; spec change is a CI gate: spec PR merges before impl PR.
7. **Hand-edited generated code** — diagnostic: git log of generated code dominated by
   post-generation edits ⇒ the contract is wrong. Never edit output; fix contract, regenerate.

Root cause of all seven: treating the spec as a *document* instead of a *contract*. Test: "would an
independent verifier, given only this contract, produce the same verdict as my team?"

---

## 4. Named tools/frameworks cited

- **MetaGPT** — fixed-role virtual company; SOPs written into role prompts; executable feedback
  (crash traces re-enter engineer context).
- **ChatDev** — chat-chain of atomic two-agent subtasks; communicative dehallucination rule
  (ask, never guess); composite quality 0.1523→0.3953.
- **AgileCoder** — dynamic role assembly per task class; built for long-context repository work;
  outperforms on full-program executability benchmarks.
- **SWE-agent / OpenHands / Aider** — reference autonomous-agent runtimes (four-layer anatomy);
  Aider contributes the PageRank repo map.
- **SWE-bench** — benchmark showing scaffolding (not model) is often the limiting factor.
- **CURRANTE** — VS Code extension enforcing 3-phase human-gated SDD (narrative → contract →
  generation/verification) over Markdown + YAML contracts.
- **Spec languages**: TLA+ (temporal, TLC model checker), Alloy (relational, SAT counterexamples),
  B-Method/Event-B (refinement calculus), Z notation (stagnant tooling), OpenAPI/JSON Schema,
  CUE, Gherkin/Cucumber, Rego/OPA, Pact, SQL DDL constraints.
- **PBT tools**: Hypothesis (Py), QuickCheck (Haskell), fast-check (TS).
- **Spec Kit** (ch12 pointer) — enforces spec-PR-before-impl-PR with read-only repository probes.
- **SGRM / DP1** (ch10 pointer) — determinism boundary: bound LLM acceptance by a deterministic
  validator to control output variance.

---

## 5. Direct design implications for mini-ork SDD mode

1. Model each .md spec file as a frozen "narrative intent" and compile it into a schema-validated YAML contract (given/when/then/except clauses + acceptance criteria) in a separate intake step whose output is gated before any implementer runs.
2. Enforce typed, JSON-Schema-validated hand-offs between every lane (PRD → design → code → test report) and reject malformed artifacts back to the emitting lane instead of passing them downstream.
3. Run a spec-ratification diff at intake: every requirement present in the source .md but absent from the compiled contract is a blocking flag requiring explicit acknowledgement in the run ledger.
4. Make the compiled contract read-only for the whole generation attempt and route all "spec is wrong/unsatisfiable" outcomes to a contract-revision step, never to silent spec edits by the implementer lane.
5. Keep the verifier lane fully independent of the implementer lane — separate sandbox, no shared writable state, and never let the implementer author the smoke spec it will be graded against.
6. Verdicts must be atomic pass/fail with a traceable reason string; forbid partial credit in gate_cmd results so "mostly working" never merges.
7. Add a spec-lint pass implementing the seven authoring errors (vague criteria, implementation leakage, missing negatives, over-spec, test-bound clauses, drift, hand-edit detection) that runs before any budget is spent on generation.
8. Adopt a communicative-dehallucination primitive: an implementer missing information emits a structured ASK artifact that pauses the step and escalates to the spec author instead of guessing.
9. Layer verification as fast lint (every cycle) → type/contract check (every step) → property-based + live smoke + UI/UX judge (before merge), walking constitutional → quality → functional layers top-down so the most consequential failures fail first.
10. Since specs are the source of truth, treat generated code as derivative: a smoke failure first asks "is the contract wrong?" (loop to contract revision) and hand-edits to generated output are a lint violation detected via git log.
