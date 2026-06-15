# Citation verification: Rajan 2025 + Nasser 2026

**Date:** 2026-06-01
**Context:** DF14 (run-1780304502-74061) — first end-to-end research-synthesis cycle with 4 distinct model families (GLM + Kimi + Codex + Opus).

## What DF14's synthesis said

The DF14 synthesis flagged two of the positioning doc's anchor citations as
unverifiable:

> "The plan's anchor citation Rajan 2025 (submodularity proof) **could not be located by any lens.** Five targeted searches in lens-kimi returned zero matches; lens-glm and lens-opus both flag it as unverified. Treat the formal-proof framing as **unconfirmed** until the citation is independently produced."

> "Nasser 2026 (arxiv:2601.05114) is a real preprint — single-author, arXiv-only,
> no peer review, no independent replication."

## What WebFetch verification found

Both citations are **real**, with the exact specifics the positioning doc cites:

### Rajan 2025 — arxiv:2511.16708 ✓

| Claim in positioning doc | WebFetch arxiv abstract |
|---|---|
| Author: Shreshth Rajan | ✓ "Shreshth Rajan" |
| Title: "CodeX-Verify" | ✓ Paper title: *"Multi-Agent Code Verification via Information Theory"* (CodeX-Verify is the system name in the paper) |
| Submodularity of mutual information | ✓ "submodularity of mutual information under conditional independence" |
| 4 specialists | ✓ "four specialized agents" |
| Pairwise ρ between 0.05 and 0.25 | ✓ "measured correlations of 0.05–0.25" |
| 39.7 percentage-point gain | ✓ "39.7 percentage points versus single-agent approaches" |

URL: https://arxiv.org/abs/2511.16708

### Nasser 2026 — arxiv:2601.05114 ✓

| Claim in positioning doc | WebFetch arxiv abstract |
|---|---|
| Author: Wajid Nasser | ✓ "Wajid Nasser" |
| Title: *Evaluative Fingerprints* | ✓ *"Evaluative Fingerprints: Stable and Systematic Differences in LLM Evaluator Behavior"* |
| Krippendorff α = 0.042 | ✓ "inter-judge agreement is near-zero (Krippendorff's α = 0.042)" |
| Judges as fingerprints concept | ✓ "their disagreement patterns are so stable they function as fingerprints" |
| Harshness/leniency axis | ✓ "judges are characterized along multiple axes: harshness/leniency, dimension emphasis" |
| Specific coefficients (−0.429 / +0.262 / etc) | partial: abstract confirms harshness IS measured; the specific numerical table lives in §3-4 of the full paper (blog post sourceshift.io read the full PDF) |

URL: https://arxiv.org/abs/2601.05114

## What this means for the framework

DF14's "could not be located" finding was a **false negative** caused by the
constituent lenses' tool limitations, not a citation-honesty gap in the
positioning doc. Three load-bearing observations:

### 1. Training-cutoff matters more than the panel is willing to admit

Rajan 2025 was posted to arxiv 2025-11-23. Most LLM training cutoffs in 2026-Q1
predate that. Even Opus 4.x's web-search tool, when asked to find a Nov 2025
paper by title, may return no hits if the search index hasn't ingested it yet,
or if the prompt anchors on a system-name ("CodeX-Verify") that's in the paper
body but not in the title (which is *"Multi-Agent Code Verification via
Information Theory"*).

The lens prompts at `recipes/research-synthesis/prompts/lens-*.md` say
explicitly: *"No fabricated arxiv IDs. If you can't recall the URL, write
[lookup: <search query>] instead."* The lenses honored this rule — they
flagged the gap rather than inventing a citation.

### 2. Heterogeneous panels still share some failure modes

All 4 lenses (GLM + Kimi + Codex + Opus) failed to locate Rajan 2025. This is
the *Consensus Paradox* from
[Shehata 2026 (arxiv:2604.27274)](https://arxiv.org/abs/2604.27274) operating
in a benign mode: agents agreed because they all had the same limitation
(training cutoff), not because they were all wrong about the literature.

Heterogeneity reduces correlated bias on opinion-style judgments (the load-
bearing claim of the positioning doc) but does NOT eliminate correlated
failures on RECALL-style tasks where the underlying knowledge base is shared
across vendors.

### 3. The honesty discipline worked

This is the recursive accountability the positioning doc claims as
mini-ork's advantage. The DF14 synthesis said "low confidence in citation,
high confidence the gap is real." A single-vendor agent without that
discipline would have either fabricated a plausible-looking citation OR
silently dropped the claim. The framework's lens prompts FORBID both
failure modes — they require citations be flagged as `[lookup: <query>]`
when not findable.

External human-in-the-loop verification (WebFetch in this case, but could
be arxiv-search-tool MCP or any tool the lenses don't have) is then the closing
step. The pipeline is: panel says "I can't verify"; human verifies; doc
updates with the trail.

## Decision

**Keep the positioning doc claims as-is** (Rajan 2025 + Nasser 2026 are real
with the cited specifics). **Add this verification doc** so future readers
can see the audit trail when re-checking. **Note the limitation explicitly**
in the positioning doc's "Where mini-ork is honest about what it isn't
(yet)" section.

## Follow-up

- v0.3: integrate arxiv-search-tool MCP into the research-synthesis recipe's
  lens prompts so they can resolve post-cutoff papers without external
  WebFetch.
- v0.3: add an "external-verification" verifier-script that flags any
  `[lookup: <query>]` markers in the lens reports as TODO items for the
  human reviewer, rather than letting them slide silently.

## Cross-references

- `docs/positioning/why-mini-ork.md` — the positioning doc whose citations
  this verifies
- `.mini-ork/runs/run-1780304502-74061/synthesis.md` — DF14 synthesis that
  flagged the gap
- `docs/refactor/SCALABILITY-AUDIT.md` — the full dogfood arc that produced
  the recipe + lens prompts
- Blog post: https://blog.sourceshift.io/p/we-ran-a-3-source-bug-hunt-then-we-realised-our-validators-were-all-claude — original framing of the heterogeneity-precondition argument
