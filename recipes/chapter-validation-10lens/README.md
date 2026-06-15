# chapter-validation-10lens — parallel multi-lens chapter validation

10 small focused reviewer agents each judge ONE slice of chapter
validation; a synthesizer rolls the 10 verdicts into one
pass/revise/block call; a publisher emits a human-readable report.

## The 10 lenses

| ID | Lens | What it judges |
|---|---|---|
| 01 | structure | H1/H2/H3 hierarchy, section ordering, required sections present |
| 02 | factuality | spot-check 3-5 load-bearing claims against assigned sources |
| 03 | voice_tone | register match to genre + publisher_style; no shifts |
| 04 | length_density | word count vs target; paragraph length distribution; filler |
| 05 | forbidden_constructs | AI-tells, padding phrases, meta-references, emoji policy |
| 06 | markdown_format | math wrapping, code blocks, list parallelism, image refs |
| 07 | coverage | every assigned_source_id cited; no off-topic digressions |
| 08 | coherence | section transitions, no redundant paragraphs, pronoun clarity |
| 09 | reader_contract | genre-appropriate pedagogical or narrative scaffolding |
| 10 | synthesis_originality | through-line / thesis present; sources integrated not summarized |

Each lens emits `${MINI_ORK_RUN_DIR}/lens-NN-verdict.json` with verdict,
score 0-10, issues, and evidence refs.

## Why 10 small lenses instead of 1 big reviewer?

- **Single-point-of-failure elimination**: a one-shot reviewer either
  catches all 10 surfaces or misses some. 10 specialized prompts mean
  each surface gets full attention.
- **Parallel dispatch cuts wall-clock**: with `dispatch_mode:
  partitioned`, all 10 lenses run concurrently. End-to-end time is
  bounded by the slowest lens, not the sum.
- **Lens-level retry**: when lens 04 (length_density) reports `block`
  but the chapter actually meets the target, you re-fire ONLY lens
  04, not the full reviewer.
- **Cross-lane diversity**: each lens can sit on a different
  `model_lane` (lens_01..lens_10 in agents.yaml). A 10-lane panel of
  distinct model families avoids the same-vendor blind-spot trap
  documented in the README's "Why heterogeneous-family multi-agent"
  section.
- **Per-lens cost cap**: cheap lenses (structure, forbidden_constructs,
  markdown_format) can sit on budget models; expensive lenses
  (factuality, synthesis_originality) get the smart lane.

## Lane assignment (suggested)

`config/agents.yaml`:

```yaml
lanes:
  lens_01: sonnet      # structure — cheap pattern match
  lens_02: opus        # factuality — needs deep reasoning
  lens_03: sonnet      # voice/tone — register sniffing
  lens_04: sonnet      # length — mostly mechanical
  lens_05: sonnet      # forbidden — pattern match
  lens_06: sonnet      # markdown — mechanical
  lens_07: opus        # coverage — semantic match
  lens_08: opus        # coherence — semantic reasoning
  lens_09: sonnet      # reader contract — heuristic
  lens_10: opus        # synthesis — judgment call
```

7 lenses on sonnet + 3 on opus = ~70% cost reduction vs all-opus.

## Synthesizer rollup rules

Deterministic, no improvisation:

- `pass` — every lens passes AND weighted_score >= 75
- `revise` — ≥1 lens revise AND no lens block AND weighted_score >= 50
- `block` — any lens block OR ≥2 critical issues OR weighted_score < 50

## Run it

```bash
mini-ork run chapter-validation-10lens kickoffs/validate-chapter-4.md
```

The kickoff describes the chapter under review + chapter_context (see
`example-kickoff.md`). The planner emits `plan.json`, the 10 lenses
fire in parallel, the synthesizer rolls up, the publisher emits a
markdown report, the verifier confirms artifact completeness.

## Output artifacts

| File | Producer | Purpose |
|---|---|---|
| `${RUN_DIR}/plan.json` | planner | chapter_context + lens_partitions + verifier_contract |
| `${RUN_DIR}/lens-NN-verdict.json` × 10 | each lens | per-lens verdict + score + issues |
| `${RUN_DIR}/panel-verdict.json` | synthesizer | rolled-up pass/revise/block + weighted_score + critical_issues |
| `${RUN_DIR}/chapter-validation-report.md` | publisher | human-readable scoreboard + issue table + next-action |
| `${RUN_DIR}/verifier-lens-outputs-complete.log` | verifier | per-artifact ok/fail evidence |

## Composes with

- **MINI_ORK_ON_EVENT hook** (PR #12) — push each lens completion
  event out to a dashboard / chat / supervisor in real-time.
- **operator_steering** (PR #13) — supervisor can inject mid-flight
  steering at any lens role between dispatches.
- **mini-ork-mcp-steering** (PR #13 Phase 3) — each lens agent can
  call `get_operator_steering(run_id, role="lens_NN")` to consult
  the supervisor before scoring.
