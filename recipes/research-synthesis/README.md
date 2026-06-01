# Recipe: research-synthesis

**Multi-lens research synthesis using 4 distinct model families.**

A non-audit recipe that demonstrates mini-ork's heterogeneous-family
multi-agent pattern applied to research questions rather than codebases.
Mirrors the shape of `recipes/refactor-audit/` but with lens stances
swapped for research-flavored ones:

| Lens | Stance | Model family | Output |
|---|---|---|---|
| `glm_lens` | Web sweep — BREADTH | Zhipu GLM | 10-25 recent sources, dated, with TL;DR + confidence |
| `kimi_lens` | Academic literature — RIGOR | Moonshot Kimi | 8-15 arxiv/DOI papers, methodology + effect size + replication |
| `codex_lens` | Code-pattern survey — PRACTICE | OpenAI Codex | 8-15 public implementations, file:line evidence |
| `opus_lens` | Deep narrative — THEORY | Anthropic Opus | 1500-2500 word 6-section essay (history → conventional → dissent → edge → open questions → recommendations) |

Then 1 reviewer (synthesizer) composes consensus + dissent into a
ranked synthesis with ★ markers per lens-count and honest reporting
of disputed claims (no vote-rule — per Nasser 2026 same-conviction
voting amplifies bias).

## Quickstart

```bash
mini-ork run research-synthesis path/to/kickoff.md
```

Cycle takes ~10-20 min wall, ~$5-15 real cost (depends on prompt
size). Publisher writes `synthesis.md` to
`docs/research/synthesis-latest.md` and `git commit`s under
`mini-ork@local`.

## Why this recipe matters

It validates the **recipe-as-userland claim**: mini-ork is not an
"audit framework", it's a task operating system. The
`recipes/refactor-audit/` and `recipes/research-synthesis/` recipes
share zero per-recipe runtime — only the framework's primitives
(execute, verify, reflect, publish). New recipes are pure
configuration + prompts + verifier scripts.

## Discipline rules (encoded in prompts)

1. **No fabricated sources.** Every lens prompt explicitly forbids
   inventing URLs/arxiv IDs/repo paths. Missing sources are flagged
   `[lookup: <query>]` instead.
2. **No naked claims.** Every assertion in every lens output gets at
   least one lens-anchored citation.
3. **Surface dissent.** Lenses are explicitly instructed to report
   disagreement, not paper over it.
4. **Synthesizer doesn't vote-rule disputed findings.** Per Nasser
   2026 ([arxiv:2601.05114](https://arxiv.org/abs/2601.05114)),
   same-conviction voting amplifies bias. Disputes are reported as
   disputes; consumer decides.

## Verifier

`verifiers/source-completeness.sh` enforces:
- All 4 `lens-*.md` exist + non-empty (≥20 lines each)
- Per-lens minimum citation count (≥5 for web/lit/code, ≥3 for opus
  narrative)
- `synthesis.md` exists + references all 4 lens names
- Consensus marker count reported (soft signal, no hard requirement)

Outputs JSON with `pass: bool`, `source_count: N`, `missing: [...]`.

## See also

- [`docs/positioning/why-mini-ork.md`](../../docs/positioning/why-mini-ork.md) — the heterogeneous-family literature grounding
- [`recipes/refactor-audit/`](../refactor-audit/) — the audit-shaped sibling recipe
