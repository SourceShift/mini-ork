---
title: "Cross-family panel synthesis: opinions on the mini-ork vs Omnigent improvement plan"
source_run: .mini-ork/runs/comparative-opinions-1781426971
panel: codex × 2 + minimax × 2 + glm × 2 (failed) + kimi × 2 + opus × 2
status: synthesis-note
last_updated: 2026-06-14
audience: agent+human
canonical_path: docs/research/omnigent-vs-mini-ork-panel-synthesis.md
tags: [omnigent, panel-review, heterogeneous-lens, security-substrate, plan-revision]
---

# Cross-family panel synthesis: opinions on the mini-ork vs Omnigent improvement plan

Dispatched the two earlier design docs
(`omnigent-vs-mini-ork-comparison.md`, `omnigent-mini-ork-improvement-plan.md`)
to 10 LLM instances across 5 families × 2 role-hint variants each
(production reliability + competitive strategy). The dispatch driver
is `scripts/comparative-opinions.sh`. 8 lenses returned substantive
opinions; GLM × 2 failed at dispatch (forensics retained at
`/var/folders/.../mo-llm-*.zFFVz5gBWH`).

## Headline finding (Krippendorff α ≈ 1.0 on the C-prompt)

**Every single lens — across 4 distinct model families — independently
ranked the secret-isolation / egress-proxy gap (Ω-E in my plan) as
the wrong phase ordering.** The plan ships it fifth of six phases;
every reviewer says it should be first or second.

Per-lens headlines verbatim:

| Lens | Headline |
|---|---|
| codex-1 | Fix mini-ork's privilege boundary before chasing Omnigent's collaboration surface |
| codex-2 | Mini-ork should borrow Omnigent's trust boundary first, not its collaboration surface |
| kimi-1 | The comparison is mostly fair, but the improvement plan under-weights the security model |
| kimi-2 | Mini-ork's survival depends on hardening its verifier/oracle moat and selectively ingesting Omnigent's **security substrate** |
| minimax-1 | Plan mostly right but **egress proxy (G-4) deserves phase 1, not phase 5** |
| minimax-2 | Phase ordering is wrong — make the stateful policy engine the substrate, ship the egress proxy second |
| opus-1 | The plan defers **the only gap that has unbounded blast radius** and builds three new network surfaces before closing it |
| opus-2 | **G-4 secret-leak is live prod risk today, not Phase 5** |

That is per Rajan 2025 + Nasser 2026 about as strong a heterogeneous-
panel signal as the methodology produces.

## Why every lens converges

Three independent argument chains arrive at the same conclusion:

### Argument A — blast radius (codex-1, opus-1)

Mini-ork's worst-case today is a single misaligned provider response
that prints all env vars; the trace store faithfully records the
secret. Every other gap (UX, collab, cost-pause) has bounded blast
radius; this one is unbounded.

Codex-1 verified the exact code path locally:

```
lib/llm-dispatch.sh:694-696  # resolves .mini-ork/config/secrets.local.sh
lib/llm-dispatch.sh:737-740  # sources it in execution subshell
lib/llm-dispatch.sh:767-772  # second-call sourcing
lib/llm-dispatch.sh:774-781  # invokes claude with --permission-mode bypassPermissions
```

This is not aspirational risk. It's shipped behavior on `main` today.

### Argument B — Omnigent's egress proxy is real, shippable, Apache-2.0 (codex-2, kimi-2, opus-2)

Multiple lenses pulled file evidence from the local `/tmp/omnigent`
clone to prove the practice is engineering, not blog vapor:

```
omnigent/inner/egress/proxy.py:1-12     # MITM HTTP(S) proxy
omnigent/inner/egress/proxy.py:81-131   # private destination blocking
                                        # proxy auth + token-handling
omnigent/sandbox/bwrap.py:15-20         # Linux bubblewrap with default selection
                                        # and graceful fallback
```

Adopting this substrate is a 1-week port; rebuilding it is a 6-month
project we cannot win at the OSS-volunteer scale.

### Argument C — dependency ordering (opus-1, minimax-2)

The plan builds Ω-C (HTTP API: pause/resume/abort endpoints) and Ω-D
(cloud sandboxes) before Ω-E (egress proxy). That is the wrong order.
You cannot safely expose remote control over `bin/mini-ork-execute`
while the executed process still has ambient secret access. Phase
Ω-C *amplifies* Phase Ω-E's risk by extending the attack surface
outward through the HTTP API before the underlying trust boundary
is closed.

Opus-1 worded it sharpest: *"the plan builds three new network
surfaces before closing the one already-broken one."*

## Other consensus signals (3+ lenses)

### Drop or defer live collaboration (Ω-F) entirely

- **minimax-2:** "drop live collaboration from scope entirely"
- **kimi-2:** "not building a collab server it cannot out-engineer"
- **codex-2:** "Omnigent already wins on collaboration; don't compete there"

Mini-ork is not going to out-build Databricks on real-time multi-
user session sharing. The plan's Phase Ω-F should be cut, not
sequenced last.

### Wrap-a-harness (G-7) is higher-leverage than cost-pause (G-6)

- **opus-2:** "G-7 harness-wrapping is competitive must-have, not last phase"
- **codex-1:** "Once secrets are isolated, wrapping Claude Code as a
  node beats writing a bash policy DSL"

The Harvey pattern — "open worker + frontier advisor" — is the
composition story that wins. Cost-pause is operator UX, not moat.

### Policy DSL belongs in Python with TypedDict, not bash

- **kimi-2:** "Designing `policy <name> on <event> when <state-predicate>
  do <action>` in shell is hard to test, type-check, or reason about"
- **minimax-2:** "adopt `omnigent/policies/schema.py`'s TypedDict shape"
- **codex-2:** "The bash-heavy architecture has a ceiling for this plan"

The policy engine (Ω-B) should be implemented in Python with
TypedDict contracts ported from `omnigent/policies/schema.py`, not
in shell. Mini-ork's bash ceiling is fine for orchestration but
wrong for type-safe stateful policy evaluation.

### Per-turn cost advisor is missed entirely

- **kimi-2:** "The plan undervalues Omnigent's per-turn cost advisor
  (`omnigent/runner/cost_advisor.py` + `cost_plan.py`)"
- **minimax-1:** "Mini-ork's cost-pause is reactive; Omnigent's
  advisor is proactive. Both matter; plan only addresses the
  reactive half."

The plan ships cost-pause-and-resume but misses Omnigent's bigger
cost win: an LLM judge that picks the right tier per turn before
spending. This is a substantive omission.

## Where the panel disagrees with the comparison doc

Codex-1 verified `lib/scope_gate.sh` does NOT exist in the current
checkout. The comparison doc says "Mini-ork's `scope_gate.sh` is a
100-line bash script matching glob patterns." That claim was based
on a path the framework references conceptually but does not ship as
a discrete file. Correction needed: scope policy in mini-ork is
distributed across task/profile parsing and gate vocabulary, not a
single 100-line script.

## Revised plan ordering (panel-informed)

The original plan:

```
Ω-A cost-pause           (1st)
Ω-B policy engine        (2nd)
Ω-C HTTP API             (3rd)
Ω-D cloud sandboxes      (4th)
Ω-E egress proxy         (5th)  ← every reviewer flagged this
Ω-F collab + harness     (6th)
```

What 8/10 reviewers across 4 families would do instead:

```
Ω-E' egress proxy + sandbox + secret vault    (1st — close blast radius)
Ω-B' policy engine in Python with TypedDict    (2nd — substrate for the rest)
Ω-G NEW: per-turn cost advisor                 (3rd — proactive cost win)
Ω-A cost-pause (kept)                          (4th — reactive cost UX)
Ω-D cloud sandboxes (sandbox now safe)         (5th)
Ω-C HTTP API + Ω-F harness wrappers            (6th — paired)
COLLABORATION (was Ω-F)                        DROPPED — Omnigent wins, don't compete
```

Three structural changes vs the original:

1. **Ω-E moves from last to first.** Every other phase is gated on
   the trust boundary being closed.
2. **New Ω-G "per-turn cost advisor"** ported from
   `omnigent/runner/cost_advisor.py`. This was a substantive
   omission.
3. **Live collaboration dropped entirely.** Mini-ork's lane is
   artifact-producing, not session-sharing.

## What to keep from the original plan

- The 5-pros-per-side framing in the comparison doc is right.
- The "adopt, don't reimplement" stance for Omnigent's Apache-2.0
  substrate is right.
- The four-explicit-out-of-scopes (hosted SaaS, own provider,
  starfish UI, native YAML) are right.

## Action items for the next iteration

1. **Re-rank phases per the panel** in
   `omnigent-mini-ork-improvement-plan.md`.
2. **Add Phase Ω-G** for the per-turn cost advisor.
3. **Drop Phase Ω-F's live-collab half**; keep the harness-wrapper
   half, pair it with the HTTP API in a renamed Phase Ω-C/F.
4. **Fix the `scope_gate.sh` reference** in the comparison doc
   (codex-1 caught a fabrication).
5. **Diagnose the GLM lens failure** so future panels reach quorum.

## Citations

- **Comparative-opinion run** —
  `.mini-ork/runs/comparative-opinions-1781426971/`
- **Driver script** — `scripts/comparative-opinions.sh`
- **Input docs reviewed** —
  `docs/research/omnigent-vs-mini-ork-comparison.md`,
  `docs/research/omnigent-mini-ork-improvement-plan.md`
- **Cross-family heterogeneity grounding** —
  Rajan 2025 (arxiv:2511.16708) submodularity,
  Nasser 2026 (arxiv:2601.05114) α=0.042 cross-family.
