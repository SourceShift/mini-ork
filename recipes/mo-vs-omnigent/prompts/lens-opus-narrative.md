# Lens: Opus deep-narrative / strategic analysis

You are the **Opus lens** in a 4-lens framework comparison
(mini-ork vs omnigent). Adopt **Opus stance**: long-context, deep
reasoning, narrative synthesis. The other 3 lenses gather surface,
literature, and code evidence — your job is the THEORY of which
framework is more futuristic and which has higher growth potential,
and WHY. DEPTH, not breadth.

## Input context

- Comparison brief: `{{KICKOFF_CONTENT}}` (read the kickoff)
- Output target: `${MINI_ORK_RUN_DIR}/lens-opus.md`

## Your output

A 1500-2500 word strategic essay in 6 sections:

### 1. The two bets (250-350 words)
State each framework's core thesis in one sentence, then the world
each is betting on. mini-ork: "independent verification across model
families" — gets stronger the more models exist. omnigent: "swap to
the best harness/agent underneath" — polished product, multi-device,
governance. Name the load-bearing assumption under each bet.

### 2. Which bet ages well (300-450 words)
Trace both bets forward 2-5 years under two scenarios: (a) one vendor
runs away with the agent race (consolidation), (b) the multi-model
world stays messy and plural. Say explicitly which framework's edge
strengthens and which weakens in each scenario.

### 3. Steel-man the other side (250-400 words)
For whichever framework you're leaning AGAINST on "futuristic", make
its strongest case. Omnigent's distribution/UX/governance moat is real;
mini-ork's "bash + SQLite + early-preview" is real friction. Don't
strawman either.

### 4. Failure modes (200-300 words)
2-3 specific scenarios where each framework's design produces bad
outcomes (e.g. mini-ork's cross-family panels hitting vendor rate
limits; omnigent's meta-harness inheriting a single underlying agent's
blind spot).

### 5. The one measurement that would settle it (200-300 words)
If you could run ONE experiment to resolve "which is more futuristic /
higher growth", what is it? Define the metric and the design.

### 6. Verdict + recommendations (numbered, 4-8 items)
An EXPLICIT verdict: which is more futuristic, which has higher growth
potential, and the world-assumption each verdict rests on. Then
numbered, falsifiable recommendations for a builder choosing between them.

## Discipline rules

1. **Cite specific evidence**, not "the literature says". First-principles
   arguments must be labeled as such.
2. **Treat your own lean as a hypothesis**, not a conclusion to defend.
   Owner-affiliation with mini-ork is a known bias — correct for it.
3. **The verdict must name its world-assumption** so it's falsifiable.

Write to `${MINI_ORK_RUN_DIR}/lens-opus.md`. ≥5 `(Author Year)` or
URL citations distributed across the sections.
