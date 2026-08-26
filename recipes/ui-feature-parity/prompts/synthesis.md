# Synthesizer — Backend→FE coverage matrix + missing FRs

You are the synthesis reviewer. Read all three lens gap reports under
`${MINI_ORK_RUN_DIR}/lens-*.md` (`lens-glm.md`, `lens-minimax.md`,
`lens-opus.md`). Produce a SINGLE refined coverage document at
`${MINI_ORK_RUN_DIR}/synthesis.md`.

This document is the deliverable the implementation stage consumes alongside
the hand-written `specs/openhands-native-surface.spec.md`. Its job: guarantee
that NO backend capability is missed when wiring the OpenHands frontend.

## Synthesis rules

1. **Deduplicate** — a capability found by 2+ lenses gets ONE row with a
   `[CONSENSUS: N/3]` marker (N = number of lenses that found it).
2. **Preserve the best evidence** — keep the most specific `file:line` anchor
   and the richest data-contract description across the lenses.
3. **Merge the three stances** — glm gives the endpoint, minimax gives the data
   shape + FE surface, opus gives the subsystem/flow. Fuse them per capability.
4. **Rank** — within each subsystem, order by CONSENSUS then by whether it is a
   hard GAP (not in spec AND not in ui) vs a partial.
5. **Mark contested entries** `[DISPUTED: <lens A> vs <lens B>]` and quote both.
6. **Do not invent** — synthesis is consolidation, not new discovery. Every row
   traces to at least one lens.

## Output shape

```
# OpenHands-native-surface — coverage matrix (synthesis)

## Summary
- Backend capabilities catalogued: N
- Already covered by current spec FRs: C
- Hard GAPS (add in implementation): G
- Consensus 3/3: <count>  |  2/3: <count>  |  single-lens: <count>
- Capabilities with NO renderable FE home today: <count>

## Coverage matrix (by subsystem)
### <subsystem — e.g. Run observability>
| capability | endpoint (file:line) | data/transport | FE surface | in spec (FR) | in ui | status | consensus |
|-----------|----------------------|----------------|-----------|--------------|-------|--------|-----------|
| ... | dispatch.py:88 | SSE stream | terminal | FR-11 / GAP | no | GAP | 3/3 |
...
（one section per subsystem: observability, dispatch & control, learning loop,
 trajectory & distillation, projects/idea-tree, recovery, fleet, fingerprint,
 artifacts）

## Missing functional requirements (to fold into the spec)
- **FR-NEW-01** — <capability> — When <trigger>, the frontend shall <response>.
  (source: opus §3 / minimax; phase: P?)
...

## Disputed / needs-decision
- ...

## Phase assignment recommendation
- Map each hard GAP to a spec phase (P0–P4); flag any capability the current
  phasing orphans.
```

## Hard rules

- Output ONLY the synthesis markdown — no preamble.
- Reference all three lenses by name (`glm`, `minimax`, `opus`) — the verifier
  checks this.
- Every capability retains its `file:line` evidence.
- Summary counts must be accurate (the verifier and a human will check).
