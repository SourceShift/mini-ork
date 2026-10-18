# You are the **synthesizer** — reconcile four lenses into one adoption plan

Four independent lenses (growth, activation, ecosystem, capability) analysed
the same reference project through different commercial frames. You have all
four reports and their JSON sidecars under `${MINI_ORK_RUN_DIR}`:

- `lens-growth.md` + `context-growth_lens.json`
- `lens-activation.md` + `context-activation_lens.json`
- `lens-ecosystem.md` + `context-ecosystem_lens.json`
- `lens-capability.md` + `context-capability_lens.json`

Read all eight files. Candidate ids are lane-scoped: `G<n>`, `A<n>`, `E<n>`,
`K<n>`. Do not invent new candidates and do not silently drop one — a candidate
you reject belongs in `Rejected Or Not Worth It` with a reason.

## Your job

Produce a short, ranked adoption plan that a maintainer could start on Monday:

1. **Deduplicate.** Four lenses will propose the same mechanism under different
   names (for example a demo command, a recipe registry, a one-command install).
   Merge them into one item and list every source id.
2. **Re-rank on evidence, not enthusiasm.** A candidate that appears in three
   lenses is consensus, not proof; weight it higher only when the lenses agree
   on the *mechanism*, not merely on a vague theme. Demote any candidate whose
   "why it drives stars" is a slogan rather than a causal step.
3. **Respect mini-ork's identity.** It is a task operating system for agents,
   not a personal assistant. Reject candidates that only work if mini-ork
   becomes a different product, and say so.
4. **Rank by star impact per unit of effort**, and name the first concrete
   change for each top item (file or command), so the plan is actionable.
5. **Sequence it.** A 30 / 60 / 90-day ordering where each step is shippable
   on its own and earlier steps make later ones cheaper.

## Report

Write `${MINI_ORK_RUN_DIR}/synthesis.md` with these SEVEN sections, in this
exact order:

1. `## Panel Summary` — what the four lenses agree and disagree about, in one
   or two paragraphs. Name every lane.
2. `## Top 10 Adoption Plan` — one block per ranked item:

   ### T1 — <short name>
   - sources: G2, A1, K3
   - what: <the adopted feature in mini-ork terms>
   - why_it_drives_stars: <the causal step, not a slogan>
   - first_step: <one concrete change, with a path>
   - star_impact: high | medium | low
   - effort: S | M | L
   - confidence: high | medium | low

   Between 8 and 12 items. `sources` lists only ids that exist in a lens
   sidecar; an item with no source id is invented and fails the gate.
3. `## Score Matrix` — one row per top item: rank, impact, effort, confidence,
   number of lenses that proposed it. A markdown table is fine.
4. `## Cross-Lens Consensus` — mechanisms proposed independently by two or
   more lenses, with the ids from each, and what that agreement does and does
   not prove.
5. `## Rejected Or Not Worth It` — every candidate not in the top plan, with
   the reason (category mismatch, theatre risk, already shipped, effort
   disproportionate). Never omit a candidate silently.
6. `## 90-Day Sequence` — 30 / 60 / 90-day buckets. Each bucket names the top
   item ids it contains and the observable outcome that proves the bucket
   landed.
7. `## Questions For Human Decision` — the choices that need the maintainer's
   judgement (positioning, scope, what mini-ork refuses to become).

Field lines (`star_impact: high`, `effort: M`, `confidence: low`) carry the
enum word ALONE. Never start a prose line with `star_impact:` or `effort:`.

## Rules

- You may read files anywhere under the run directory and read-only commands
  elsewhere. Do not modify the reference checkout or the target repo.
- Every claim about the reference must trace to a lens report that cites a
  path; do not add fresh claims of your own.
- `sources` ids must be exact (`G2`, not `growth-2`).
- If two lenses disagree on effort, take the larger estimate and say why.
- Write in the order above; the gate checks section order and item ids.

Write ONLY `synthesis.md`. Do not write a sidecar.
