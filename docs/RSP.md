# Responsible Scaling Policy

> Mini-ork's public commitment to capability-thresholded safeguards
> for autonomous and self-improving agentic workflows.
>
> **Version:** 1.0 (initial draft)
> **Status:** Living document — see § 6 Amendment Process
> **Last reviewed:** 2026-06-15
> **Companion implementation spec:** [docs/SAFETY.md](SAFETY.md) — the
> 7-rung autonomy ladder at docs/SAFETY.md:7, PromotionGate contract
> at docs/SAFETY.md:39, append-only audit log at docs/SAFETY.md:97.

---

## What this document is

This is mini-ork's Responsible Scaling Policy (RSP) — a public-facing
commitment defining:

1. The capability axes mini-ork tracks
2. The thresholds along those axes that trigger required safeguards
3. The safeguards required at each threshold
4. The tripwires that mandate a halt
5. The explicit list of things the maintainer commits to do (and not do)
6. The review cadence and amendment process

It complements but does not replace [`docs/SAFETY.md`](SAFETY.md),
which specifies the implementation mechanisms (7-rung ladder at
docs/SAFETY.md:7, promotion gate at docs/SAFETY.md:39, audit log at
docs/SAFETY.md:97). docs/SAFETY.md is "how"; this document is "what"
and "under what conditions."

It is written in the lineage of:

- [Anthropic Responsible Scaling Policy v3](https://www.anthropic.com/news/responsible-scaling-policy-v3) (Feb 2026)
- [OpenAI Preparedness Framework](https://openai.com/index/updating-our-preparedness-framework/) (continuously revised)
- [Google DeepMind Frontier Safety Framework](https://deepmind.google/discover/blog/updating-the-frontier-safety-framework/) (continuously revised)

Mini-ork's RSP is narrower in scope — it covers an agentic *runtime*,
not a frontier *model*.

---

## § 1. Capability axes mini-ork tracks

Mini-ork tracks four orthogonal capability axes. Each has measurable
thresholds. The capability of a deployed instance is the
component-wise maximum across the four axes.

### 1.1 Autonomy hours

How long can a recipe run without human intervention while still
producing verified-passing artifacts?

| Threshold | Definition | Where measured |
|---|---|---|
| **Entry** | ≤ 1 hour autonomous run between human checkpoints | `bin/mini-ork:1` default + circuit breaker at lib/circuit_breaker.sh:105 |
| **Advanced** | 1-8 hours autonomous run, single recipe | recursive-self-improve recipe at recipes/recursive-self-improve/README.md:1 |
| **Frontier** | > 8 hours autonomous run OR multi-recipe orchestration without checkpoint | Reserved for explicitly opted-in advanced operators |

### 1.2 Multi-agent coordination degree

How many agents can a single workflow compose with what dependency
depth?

| Threshold | Definition | Where measured |
|---|---|---|
| **Entry** | ≤ 3 agents per recipe, linear topology | Most starter recipes |
| **Advanced** | 4-10 agents, DAG topology, ≤ 2 coalition-gate layers | Standard recursive-validate-impl, oracle-gated recipes |
| **Frontier** | > 10 agents OR > 2 coalition-gate layers OR cross-recipe trust chains | Cross-epic gradient + scheduler combinations |

### 1.3 Code / system mutation scope

What can the agent change about its environment?

| Threshold | Definition | Where measured |
|---|---|---|
| **Entry** | Read-only on host filesystem, write only to sandbox workspace | Default sandbox adapters at lib/sandbox/local.sh:1 |
| **Advanced** | Write to host filesystem under operator-approved paths, no network egress beyond allowlist | Egress proxy + scope_gate combinations |
| **Frontier** | Self-modification of mini-ork code (rung 6+ per docs/SAFETY.md:7), production code commits, PR creation | Self-improve loop on mini-ork itself |

### 1.4 Dangerous-capability uplift

Does the workflow elicit a model capability that, executed against
the real world, would cause meaningful harm?

| Threshold | Definition | Where measured |
|---|---|---|
| **Entry** | No tasks of the form "execute against external system X" | Most analytical and synthesis recipes |
| **Advanced** | Tasks that *plan* but do not *execute* externally-impactful action | Most refactor / audit / synthesis recipes |
| **Frontier** | Tasks that *execute* against external systems (prod commits, API mutations, network actions) OR tasks deliberately eliciting offensive-cyber / biosecurity / weapons-adjacent capability | `bin/mini-ork-eval` red-team suite (pending), integration tests, deployment recipes |

---

## § 2. Required safeguards per threshold

Each safeguard maps to a specific mini-ork primitive referenced inline.

### 2.1 Entry-threshold safeguards (always required)

1. **Append-only audit log** — every dispatch writes to `audit_log`
   per docs/SAFETY.md:97. SQLite trigger at docs/SAFETY.md:116
   enforces append-only.
2. **Workspace sandbox** — every dispatch runs in a temporary workspace
   (default lib/sandbox/local.sh:1). The workspace is captured as a git
   diff via lib/harness_wrapper.sh:100 (commit a4bf298).
3. **Cost ledger** — every model call writes to `llm_calls` with token
   counts and provider lane (lib/llm-dispatch.sh:1162).
4. **Cost-pause sentinel** — global cost cap enforced by
   lib/cost_pause.sh:47 with `/pause-cost` and `/resume-cost` HTTP
   endpoints (Bearer-token auth, commit 193bd89).
5. **Circuit breaker** — recipes that fail repeated retries within
   window are auto-halted by lib/circuit_breaker.sh:105.

### 2.2 Advanced-threshold safeguards (required when any axis is Advanced)

In addition to entry safeguards:

6. **Verifier-gated promotion** — no candidate workflow version is
   promoted unless `promotion_gate_check()` returns approved=true
   (docs/SAFETY.md:39).
7. **Coalition gate with heterogeneous family panel** — multi-agent
   workflows require ≥ 2 lens families per gate; same-family panels
   reject via lib/coalition_gate.sh:49 + lib/krippendorff_alpha_gate.sh:56.
8. **Egress policy enforcement** — outbound network calls restricted
   to `.mini-ork/config/egress-policies.yaml` allowlist
   (lib/sandbox/omnigent-bridge.sh:68).
9. **Intervention gate** — high-impact operations require explicit
   intervention token (pending — `lib/intervention_gate.sh` to be
   shipped; see § 9 known-gaps).
10. **Identity RBAC** — multi-actor recipes require identity-bound
    dispatch (pending — `lib/identity-rbac.sh` to be shipped; see
    § 9 known-gaps).

### 2.3 Frontier-threshold safeguards (required when any axis is Frontier)

In addition to entry + advanced safeguards:

11. **Fail-closed sandbox enforcement** — `MO_HARNESS_REFUSE_UNSANDBOXED=1`
    must be set; sandbox-absent is a hard refusal not a degraded-mode
    warning. (Pending implementation — see § 9.)
12. **Pre-dispatch capability eval** — `bin/mini-ork-eval` must run a
    capability-evaluation suite against the current
    model+recipe+lane configuration before dispatch. (Pending.)
13. **Per-iteration delta-eval** — for self-improvement loops, after
    every implementer diff, re-run a delta capability-eval. Halt the
    loop if next-iter would gain a dangerous capability. (Pending.)
14. **Heterogeneous-family review for safety-critical diffs** — any
    self-modification of lib/coalition_gate.sh:49,
    lib/sandbox/omnigent-bridge.sh:68, or lib/safety_events.sh:1
    requires panel review with ≥ 3 lens families.
15. **Pre-registered halt criteria** — operator commits to halt
    conditions before the loop starts; system enforces mechanically
    via lib/safety_events.sh:1 (Tier 2 Item 2).
16. **External-system isolation** — frontier-threshold recipes must
    run under lib/sandbox/modal.sh:1 or lib/sandbox/daytona.sh:1
    cloud sandboxes, not lib/sandbox/local.sh:1.

---

## § 3. Tripwires

A tripwire is a condition that mandates immediate halt regardless of
recipe state. When a tripwire fires:

1. The recipe halts; run marked `tripwire_halt` in state.db.
2. A row is written to `safety_events` per
   db/migrations/0036_safety_events.sql:10 with the tripwire identifier,
   evidence, and operator response required.
3. Maintainer commits to publishing post-incident report within 30 days.

### 3.1 Tripwire definitions

| ID | Fires when | Required response |
|---|---|---|
| **TW-1** Cost-cap breach | Per-task cost > 2x configured cap (lib/cost_pause.sh:47) | Halt run; require operator confirm to resume |
| **TW-2** Unsandboxed dispatch at frontier threshold | `MO_HARNESS_REFUSE_UNSANDBOXED=1` + sandbox CLI absent (lib/sandbox/omnigent-bridge.sh:68) | Refuse dispatch; require sandbox or threshold lowering |
| **TW-3** Capability-eval threshold cross | Pre-dispatch capability-eval reports a capability not in the previous baseline | Halt; require operator re-approval with updated baseline |
| **TW-4** Coalition gate same-family approval | Coalition gate would pass with all approvals from one lens family (lib/coalition_gate.sh:49) | Refuse the gate; require operator-supplied additional family |
| **TW-5** Egress policy violation | Outbound network call to non-allowlisted destination | Halt; log destination; require operator policy update |
| **TW-6** Self-modification of safety-critical code without panel | Diff touches lib/coalition_gate.sh:49, lib/sandbox/omnigent-bridge.sh:68, or lib/safety_events.sh:1 with < 3 lens families on review panel | Reject diff; require expanded review |
| **TW-7** Audit-log integrity violation | Attempted UPDATE/DELETE on `audit_log` (blocked by docs/SAFETY.md:116) | Halt all dispatches; require forensic review |
| **TW-8** Verifier deception | Mechanical verifier (lib/citation_verifier_mechanical.sh:61) reports pass while heterogeneous oracle panel reports fail with high Krippendorff-α disagreement (lib/krippendorff_alpha_gate.sh:56) | Halt; flag the verifier for review |

---

## § 4. Explicit commitments

### 4.1 What we commit to do

1. **Publish post-incident reports** within 30 days of any tripwire
   firing, with public summary in `docs/incidents/` directory.
2. **Review this RSP** at every minor-version release (≥ 1×/quarter).
3. **Maintain implementation links** — every safeguard in § 2 points
   to a real implementation in this repo. Deletion or weakening
   requires RSP amendment.
4. **Ship dangerous-capability eval results** for every
   frontier-threshold recipe in CI. Results published to `docs/evals/`.
5. **Honor downstream incident reports** — user-reported incidents get
   public triage within 7 days.
6. **Default safeguards on, opt-out explicit** — every safeguard in
   § 2 defaults to enabled. Disabling requires explicit env-var with
   self-documenting name.
7. **Heterogeneous review of RSP amendments themselves** — proposed
   amendments go through coalition-gate panel review at
   lib/coalition_gate.sh:49.

### 4.2 What we commit *not* to do

1. **No walkback of § 2 safeguards without RSP amendment.** Anthropic
   RSP v3's walkback of the unilateral-pause clause is the negative
   example.
2. **No silent removal of tripwires.** Removing a tripwire requires
   public RSP amendment + 14-day notice.
3. **No safety-test-coverage regressions** without amendment.
4. **No scope expansion without re-evaluation.**
5. **No model dispatch to providers without RSP-equivalent commitments**
   for any frontier-threshold recipe.
6. **No deployment of negative-trajectory training fixtures** to public
   model providers as training data.
7. **No production-system access from auto-mode dispatch.**

### 4.3 Explicit out-of-scope

The maintainer explicitly **does not commit** to:

- Solving the alignment problem.
- Formally verifying any sandbox backend (Tier 3 epic
  T3-A — see kickoffs/roadmap-tier3-research-frontier.md:1).
- Bounding compositional reliability across arbitrary recipe DAGs
  (T3-B in kickoffs/roadmap-tier3-research-frontier.md:1).
- Detecting deceptive alignment in dispatched models.
- Defending against state-actor-level adversaries.
- Operating any safety guarantee under conditions where the model
  significantly exceeds GPT-5 / Claude 4.x / Gemini 2.5 capability
  tier as of 2026-06.

---

## § 5. Capability-threshold determination procedure

1. **Default assumption: highest applicable threshold.** When in doubt,
   apply the highest-threshold safeguards.
2. **`bin/mini-ork-threshold` command** (pending) returns the threshold
   classification for a planned dispatch.
3. **Operator override** logged to `safety_events` per
   db/migrations/0036_safety_events.sql:10.
4. **Frontier dispatches require `MO_RSP_FRONTIER=1`** plus operator-
   token verification.

---

## § 6. Review cadence and amendment process

### 6.1 Review cadence

- **Minor version (≥ 1×/quarter):** RSP reviewed for drift between
  stated safeguards and shipped implementation.
- **Major version (annual):** Full re-derivation of capability axes.
- **Incident-driven:** Tripwire firing triggers review within 30 days.

### 6.2 Amendment process

1. Amendment opened as PR with diff against this document.
2. Heterogeneous-family panel review (3+ lens families) via
   lib/coalition_gate.sh:49.
3. 14-day public notice for non-trivial amendments.
4. Maintainer merges with public rationale.
5. Merge commit referenced in CHANGELOG under "RSP changes" section.

Amendments that *weaken* safeguards face stricter review than
amendments that *strengthen* them. Tightening is mergeable on operator
approval; loosening requires panel approval AND 14-day notice AND
publishable rationale.

---

## § 7. Critique of antecedent RSPs

### 7.1 Anthropic RSP v3 (Feb 2026)

**Lesson taken:** Capability-threshold-to-safeguard mapping is the
useful primitive. Adopted.

**Walkback noted:** RSP v3 walked back the unilateral-pause clause
from v2 and softened several timeline commitments.

**Mini-ork commitment:** Walkbacks require explicit amendment with
14-day notice per § 6.2.

### 7.2 OpenAI Preparedness Framework

**Lesson taken:** Continuously-revised is better than once-and-done.

**Limitation noted:** Relies on internal-only evals. Mini-ork commits
to publishing eval results per § 4.1.4.

### 7.3 Google DeepMind Frontier Safety Framework

**Lesson taken:** Named critical capability levels mapped to deployment
restrictions. Adopted as entry / advanced / frontier model.

**Limitation noted:** Closed-source — external verification of
safeguard implementation impossible. Mini-ork is open-source;
verification of § 2 safeguards is observable in the repository.

---

## § 8. Relationship to docs/SAFETY.md

| This document (`docs/RSP.md:1`) | `docs/SAFETY.md:1` |
|---|---|
| Public commitment | Implementation specification |
| What the maintainer promises | How the code enforces |
| Capability thresholds + tripwires | 7-rung autonomy ladder (docs/SAFETY.md:7) + PromotionGate (docs/SAFETY.md:39) |
| Reviewed quarterly | Updated with the code |
| Amendment process per § 6.2 | PR review per repo convention |

When the two documents disagree, **the more restrictive interpretation
applies** pending reconciliation.

---

## § 9. Known gaps in current implementation

The following safeguards are committed but not yet fully implemented
as of v0.5.0. Tracked for shipping in
kickoffs/roadmap-tier4-ecosystem-launch.md:1 and the Tier 2
implementable list:

| Safeguard | Status |
|---|---|
| § 2.2.9 intervention gate (`lib/intervention_gate.sh`) | Pending |
| § 2.2.10 identity RBAC (`lib/identity-rbac.sh`) | Pending |
| § 2.3.11 fail-closed sandbox flag | Pending — Tier 2 Item 1 |
| § 2.3.12 `bin/mini-ork-eval` suite | Pending — Tier 2 Item 4 |
| § 2.3.13 per-iteration delta-eval | Pending — Tier 2 Item 6 |
| § 2.3.15 `safety_events` table | Shipped — db/migrations/0036_safety_events.sql:10 + lib/safety_events.sh:1 |
| § 5.2 `bin/mini-ork-threshold` command | Pending |
| Negative-trajectory training step | Pending — Tier 2 Item 5 |

This RSP enters force at v0.5.0 with the safeguards that ARE shipped,
and each pending safeguard enters force at the release that implements
it.

---

## § 10. Contact

- **Security / safety reports:** open a private security advisory on
  the mini-ork GitHub repository.
- **RSP amendment proposals:** open a PR against this document with
  the `rsp-amendment` label.
- **Maintainer:** Amir Khakshour (`khakshour.amir@gmail.com`).
- **Public commit log:** every change to this document is in git
  history.

---

## Frequently asked questions

**Q: Why does an open-source agentic runtime need an RSP?**

Because the runtime decides what capabilities ship to deployed users
at scale. Without stated commitments, downstream integrators have no
shared language for compliance reasoning.

**Q: Is this enforceable?**

Public commitment, not legal contract. Enforcement: § 2 safeguards are
observable in the open-source codebase; amendments are visible in git
history; tripwire incidents are published per § 4.1.

**Q: What happens if mini-ork is forked?**

Forks are not bound by this RSP. Each fork's maintainer must publish
their own.

**Q: Mini-ork doesn't have all these safeguards shipped yet. How is
this a commitment?**

§ 9 documents which are shipped and which are pending. The commitment
to ship pending safeguards is itself part of the RSP. Failure to ship
them by the stated release is an RSP violation visible in release notes.
