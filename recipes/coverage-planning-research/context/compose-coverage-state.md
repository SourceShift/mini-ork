# LibWit book composer — coverage machinery, shipped state (2026-10-28)

Hand-written snapshot for the reviewers. Everything below was read out of the code,
with anchors. Treat it as the ground truth to score against: a technique is only
"implement-now" if it attacks one of the named gaps below.

## What the composer must do

An author supplies a requirement set — today 130 named technique entries from a
literature review (`G01-T01` … `G10-T14`), tomorrow a ledger derived from a corpus
that may hold **millions** of arXiv/PubMed documents. The composer must produce one
book in which:

- every requirement item appears **exactly once** (no omission, no duplicate);
- sections are **cohesive and ordered**, not a flat catalogue;
- each item is **grounded** in evidence actually present in the job's evidence bank.

## What exists today

| Mechanism | Where | What it asserts |
|---|---|---|
| `PLAN_CHAPTERS_EVIDENCE_BOUND` | `shared/validation/planEvidenceBinding.ts:57-73` | each chapter has ≥1 source **or** ≥1 aspect node — non-emptiness only |
| AC-3 aspect binding | `server/compose/planningBurstArtifact.ts:800-812` | every **top-level** tree node (`parent_id === null`) bound by ≥1 chapter; throws listing every uncovered id |
| `mustCover` ⇄ `readerSections[].covers` | `shared/validation/bookPlanContract.ts:510-605` | each chapter's own self-declared requirements appear in its own sections |
| Bank digest | `server/compose/bankDigest.ts:48` | 12 000-char rendering of the closed world, ~150 chars/source |
| Min-floor backfill | `server/services/bookGeneration/evidenceBank/chapterSourceFeasibility.ts:69` | raises chapters **below** a source floor; never asks which sources are unassigned |
| Orphan detection | `server/services/bookGeneration/assurance/assurancePipeline.ts:212-232` | "assigned but never cited" only; unassigned sources explicitly out of scope |
| Outline novelty | `server/services/bookGeneration/outlineNoveltyGate.ts:119` | token-Jaccard ≥0.85 on **one chapter's** H2 titles; warn-only in production (`bookOrchestratorContextFirst.ts:3205-3216`) |

## Known-broken (audited 2026-10-28, `docs/book_gen/research/2026-10-28-compose-130-technique-coverage-feasibility.md`)

1. **Must-cover is capped at one item per chapter.** `mustCoverCapForChapters(n) =
   clamp(ceil(n), 12, 200)` (`composeIntentContractService.ts:367-371`) gives 50 for a
   50-chapter book; the extraction prompt then says "if the source names more, keep only
   the ${cap} most load-bearing" (`:437`). 130 items → 50, and nothing records which 80
   were dropped.
2. **The planner never sees the item list.** 12 000-char digest with `(+N omitted)`
   footnotes, while the prompt header still calls the visible subset the closed world;
   every chapter call site sees a 6 000-char distilled topic brief
   (`shared/config/composeLimits.ts:57`).
3. **Entry caps reject the source document.** Review doc is 338 526 chars; topic caps
   at 100 000 chars and `MAX_BOOK_PLAN_BYTES 256KiB − 128KiB = 131 072` **bytes**
   (`shared/config/composeLimits.ts:28`); `customRequirements` 60 000 chars.
4. **Completeness is enforced only at tree roots.** Per-source items land as *leaves*
   (`sourceAspectExtractor.ts`, ≤6 aspects per source), which AC-3 does not require.
5. **No exclusivity anywhere.** AC-3 is a set union (`!boundNodeIds.has`) — its own
   error text says "exactly one primary chapter" but duplicates pass. `mustCover`
   uniqueness is per-chapter only.
6. **Dedupe is wrong-scoped and non-blocking.** Per-chapter title Jaccard, warn-only;
   the book-level duplicate detector is an unused prompt spec behind
   `BOOK_GEN_GLOBAL_REVIEW`.
7. **Constant mismatch.** Derived chapter count clamps to 60
   (`planningSessionService.ts:1144`) while the plan contract caps main chapters at 50
   (`shared/validation/bookPlanContract.ts:20`).

## The scale question this review must answer

If the requirement set and the evidence corpus are both large (10³–10⁶ items), the
composer needs a selection + planning architecture that is *coverage-complete by
construction* and whose cost grows sub-linearly in the corpus. Which published
mechanisms — set-cover/submodular selection, hierarchical tree reduction, agenda
planning, plan verification, coverage metrics — are adoptable inside the current
Compose FSM without exploding plan size or planner context?

## What NOT to propose

- Anything requiring a 200-chapter book (product ceiling is 50 main chapters).
- Anything that silently degrades: the repo's rule is zero-fallback — fail and name
  the missing ids.
- Anything that replaces the aspect tree with a parallel unverified structure; reuse
  the AC-3 gate.
