# Synthesis — UI audit findings

You are the SYNTHESIZER. The 5 lens contributions are at
`${MINI_ORK_RUN_DIR}/lens-{a11y,perf,visual,interaction,edge}.md`. Your
job: produce a UNIFIED, PRIORITIZED findings document.

## Input

1. `${KICKOFF_PATH}` — audit scope
2. `${MINI_ORK_RUN_DIR}/plan.json` — surfaces + severity rubric +
   verifier_contract
3. Five lens reports

## Output

Write `${MINI_ORK_RUN_DIR}/findings.md`:

```markdown
# UI audit findings — <date> — <surface or surfaces>

## Executive summary
- P0: <N> findings — <one-line theme>
- P1: <N> findings — <theme>
- P2: <N>
- P3: <N>
- Top three fixes (sequenced by user impact ÷ effort): <list>

## P0 (must fix before next release)

### 1. <Title — keep from originating lens; cross-reference lens>
- Lens: a11y | perf | visual | interaction | edge
- Surface / anchor: <surface name + file:line OR URL+selector>
- Observed: <merged statement of the problem>
- Why P0: <criterion — WCAG fail / regression / brand violation / etc>
- Fix sketch: <1-2 sentences>
- Verify: <reproduction recipe>

### 2. …

## P1 (within next sprint)
…

## P2 (polish backlog)
…

## P3 (nice-to-have)
…

## Cross-lens patterns
- Pattern A — <e.g. "focus-ring missing on most interactive elements" —
  appears in a11y_lens AND interaction_lens AND visual_lens — root
  cause in shared <Button> primitive, single fix closes 7 findings>
- Pattern B — …

## Lens contributions summary
| Lens | Findings emitted | Findings kept | Notes |
|---|---:|---:|---|
| a11y_lens (GLM) | 12 | 11 | dropped 1 false positive (button is decorative-only) |
| perf_lens (Kimi) | 7 | 7 | — |
| visual_lens (Codex) | 14 | 13 | merged 2 into one parent finding |
| interaction_lens (Opus) | 9 | 9 | — |
| edge_lens (MiniMax) | 11 | 10 | dropped 1 — paste-bomb already mitigated server-side |

## Process notes (audit-trail; not user-facing)
- Synthesizer self-check:
  - [ ] every finding has severity in {P0,P1,P2,P3}
  - [ ] every finding has file:line OR URL+selector anchor
  - [ ] every finding has a fix sketch ≥ 1 sentence
  - [ ] each lens has ≥ 1 finding kept OR explicit N/A rationale
```

## Rules

- DO NOT downgrade severity to make the report friendlier. WCAG AA fail
  stays P0.
- Cross-lens patterns are HIGH-VALUE — if 3 lenses independently surface
  the same root cause, that's the highest-leverage fix.
- File:line anchors must be EXACT — if a lens supplied a stale anchor,
  flag it for re-check rather than invent.

## What you do NOT do

- Don't add new findings the lenses didn't surface.
- Don't drop a lens's findings silently — every drop needs a note in
  the Lens-contributions summary table.
