# Tier 4 — Ecosystem launch roadmap

These epics convert mini-ork from a shipping artifact into a positioned
product. None require new code ground; all leverage shipped work.

Source-of-truth grounding: docs/RSP.md:1 (capability commitments) and
the Tier 2 implementable list referenced inline.

The autonomous scheduler SHOULD NOT dispatch any of these. Each
requires human discretion. Auto-mode work is bounded to T4-D (RSP doc
at docs/RSP.md:1, already shipped) and T4-E (blog post).

## T4-A Funding outreach round 1 (id: t4-a-funding-outreach)

**Goal.** Send funding-email draft (Subject C — "Funding ask:
infrastructure for the next 24 months of agentic AI") to 10 angel /
pre-seed targets. Source draft at
`.claude/handoffs/funding-email-draft.md`.

**Prerequisites.**
- T4-D docs/RSP.md:1 published (gives email a credibility anchor)
- T4-E positioning blog post (gives email a destination beyond repo)

**Out of scope for autonomous mode.** Actually sending the emails.
Auto mode not authorized for personal outreach.

**Done when.** 10 emails sent. 1 response = positioning resonates. 3+
responses = raise viable in next 3 months.

**Effort estimate.** 1 week for research + send.

## T4-B First external design partner (id: t4-b-first-design-partner)

**Goal.** Pick one external team building agentic workflows; give them
white-glove onboarding to mini-ork; get real-world failure mode you can
fix.

**Why now.** Pure dogfooding by maintainer cannot surface failure modes
a real adopter will hit.

**Profile.** Small team (2-8 people), already running multi-agent
workflows with LangGraph/CrewAI/AutoGen, pain point around vendor
lock-in or cost.

**Channels.** LessWrong / Alignment Forum, GitHub Issues on
LangGraph/CrewAI, ChinaTalk Discord, Hacker News.

**Done when.** External team runs 3+ recipes against their workflow,
surfaces ≥ 5 issues, ≥ 3 are merged fixes in mini-ork. Maintainer has
written `docs/design-partner-feedback.md`.

**Effort estimate.** 2-4 weeks elapsed.

## T4-C NeurIPS workshop submission (id: t4-c-neurips-workshop)

**Goal.** Submit paper to NeurIPS 2026 SafeML or ATTRIB workshop.
Angle: "Polyglot RSP-compliant open agentic runtime" grounded in
docs/RSP.md:1.

**Why this venue.** Workshop papers have lower acceptance bar (~30-
40%) than main conference. Faster review cycle. Academic citation that
funding conversations + design-partner conversations can both reference.

**Prerequisites.**
- Tier 2 fully shipped including `bin/mini-ork-eval` (paper's empirical
  spine is eval results)
- ≥ 50 self-improve iterations on record
- docs/RSP.md:1 public so safety story is anchored

**Angle options ranked by viability.**
1. "Empirical scalable oversight via heterogeneous-family panel review
   on open-source agentic runtime" (cites arxiv:2502.04675 directly,
   shows panel-vs-single-reviewer empirical difference using
   lib/coalition_gate.sh:49 + lib/krippendorff_alpha_gate.sh:56)
2. "Cross-family cost-pause as baseline RSP-style tripwire" (cheap to
   demonstrate using lib/cost_pause.sh:47 + lib/safety_events.sh:1)
3. "Recursive-self-improve on open-source agentic infrastructure: 100
   iterations of empirical findings"

**Done when.** Paper submitted to ≥ 1 NeurIPS 2026 workshop with
empirical results. Abstract circulated to T4-A funding recipients.

**Effort estimate.** 4-6 weeks for writing + submission.

## T4-D Public RSP commitment (id: t4-d-public-rsp-commitment)

**Status: SHIPPED at docs/RSP.md:1.**

**Goal achieved.** docs/RSP.md:1 defines mini-ork's capability
thresholds and required safeguards per threshold. References Anthropic
RSP v3, OpenAI Preparedness Framework, DeepMind Frontier Safety
Framework as antecedents.

**Why this was highest-leverage in Tier 4.** Zero code. Single biggest
funding-credibility move available. Differentiates from every other
open-source agentic runtime (LangGraph, CrewAI, AutoGen, Microsoft
Agent Framework, AWS Strands — none has a published RSP equivalent).
Anchors every other Tier 4 item.

**Structure delivered** (per docs/RSP.md:1):
1. Capability axes (docs/RSP.md § 1)
2. Threshold definitions per axis (docs/RSP.md § 1.1-1.4)
3. Required safeguards per threshold (docs/RSP.md § 2)
4. Tripwire definitions TW-1 through TW-8 (docs/RSP.md § 3.1)
5. Commitment list (docs/RSP.md § 4.1-4.3)
6. Review cadence + amendment process (docs/RSP.md § 6)
7. Critique of antecedent RSPs (docs/RSP.md § 7)

## T4-E Positioning launch blog post (id: t4-e-positioning-launch)

**Goal.** One blog post titled approximately "The polyglot
RSP-compliant agentic runtime so frontier labs don't lock the
substrate." Position mini-ork as infrastructure-during-substrate-
formation. Cite docs/RSP.md:1 for the safety commitment.

**Distribution.** Hacker News, LessWrong cross-post, X thread,
Mastodon, Bluesky. Direct-link in T4-A funding emails.

**Prerequisites.**
- T4-D docs/RSP.md:1 published — done.
- Tier 2 Items 1-3 shipped (post claims them as concrete differentiators)

**Tone.** Confident not defensive. Specific. Honest about what is
unsolved per docs/RSP.md:1 § 4.3.

**Done when.** Blog post published, ≥ 3 distribution channels posted
to, included as link in next T4-A funding email batch.

**Effort estimate.** 1 week.

---

## Sequencing within Tier 4

Critical path:

```
T4-D (docs/RSP.md:1)
  ├─→ T4-E (blog post — references RSP)
  │     └─→ T4-A (funding email — links blog + RSP)
  │
  └─→ T4-C (NeurIPS — RSP is empirical spine)
        └─→ T4-A (funding email — cites paper if accepted)

T4-B (design partner) runs in parallel.
```

## Why these stay in Tier 4 not Tier 2

Tier 2 is "engineering work the maintainer must do." Tier 4 is
"leverage moves on top of Tier 2." Doing Tier 4 before Tier 2 items
1-3 produces positioning the codebase cannot back up. Doing Tier 2
without Tier 4 produces credible codebase nobody knows about. Both
required.
