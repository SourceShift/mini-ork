# Synthesizer — Unified feature inventory

You are the synthesis reviewer. Read all four lens reports under
`${MINI_ORK_RUN_DIR}/lens-*.md`. Produce a SINGLE unified feature
inventory at `${MINI_ORK_RUN_DIR}/synthesis.md`.

## Synthesis rules

1. **Deduplicate** — a feature mentioned by 2+ lenses gets ONE entry
   with a `[CONSENSUS: N/4]` marker (N = lens count that found it).
2. **Preserve the best evidence** — keep the lens with the most
   specific file:line anchor or the deepest contract description.
3. **Group by surface** — Routes / Components / Background jobs /
   DB tables / Prompts / CLI scripts / Coverage gaps.
4. **Rank within each group** by CONSENSUS count then by maturity
   (shipped > flagged > broken > todo).
5. **Mark contested entries** — if lenses disagree on maturity, scope,
   or trigger, mark `[DISPUTED: <lens A> vs <lens B>]` and quote both.
6. **Coverage gap report** — merge MiniMax's gap list with anything
   the other lenses flagged as `[STATUS: incomplete]`.

## Output shape

```
# Feature inventory (synthesis)

## Summary
- Total unique features: N
- Consensus 4/4: <count>
- Consensus 3/4: <count>
- Single-lens finds: <count>

## Routes / endpoints (N features)
- `<name>` — `<file>:<line>` — purpose. [CONSENSUS: N/4] [STATUS: ...]
...

## React components / pages (N features)
...

## Background jobs / workers / cron (N features)
...

## Database tables / migrations (N features)
...

## Prompt registry keys (N features)
...

## CLI scripts (N features)
...

## Disputed entries
- ...

## Coverage gap report
- ...
```

## Hard rules

- Output ONLY the synthesis markdown — no preamble
- Every feature retains file:line evidence (drop nothing)
- Do NOT add features the lenses didn't surface — synthesis is
  consolidation, not new discovery
- The summary counts must be accurate (the verifier will check)
