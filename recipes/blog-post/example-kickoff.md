# Kickoff — blog post: heterogeneous-family multi-agent is the precondition

## Topic

Why "multi-agent review" is structurally circular when all agents share
the same model family. Drawing from Nasser 2026 (arxiv:2601.05114) and
Rajan 2025 (arxiv:2511.16708): same-family panels have Krippendorff α =
0.042; submodularity gain requires pairwise ρ ≤ 0.25.

## Audience

Senior eng leaders evaluating LLM-agent frameworks for production use.
Already familiar with multi-agent / RAG concepts; not yet familiar with
information-theory framing of agent panels.

## Distribution channel

sourceshift.io

## Length target

1200 words (±20%)

## Key takeaways

1. Same-family multi-agent collapses to coalition (Nasser 2026 α = 0.042).
2. Rajan 2025 submodularity proof requires ρ ≤ 0.25 — heterogeneity is
   the precondition, not an optimization.
3. mini-ork's `config/agents.yaml` lens lanes route to 5 distinct families
   by construction.
4. Detection test: list the model families behind every hunter + every
   validator. If they're all the same vendor, you have a coalition not
   an audit.

## Scope boundaries

- WILL NOT cover: prompt-engineering tricks for individual lenses.
- WILL NOT cover: mini-ork installation / API surface.

## Tone

Sharp, evidence-led, mildly provocative. Second person, contractions OK.
Cite the receipts (Krippendorff α value, the ρ threshold) — don't hand-wave.

## CTA

End with a one-line invitation to inspect mini-ork's lens lanes config
(linked) and run the detection test on the reader's current framework.
