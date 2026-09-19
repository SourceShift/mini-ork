# Kickoff: Fix the researcher defect blocking book chapter {{unit_id}}

## Goal

Book `d0df3cdb-8164-450e-b841-2c9354ea0423` chapter **{{unit_id}}** cannot reach
"committed + quality" in researcher's compose book pipeline. Find the real root
cause in the researcher `server/` code and land a minimal, correct fix so the
chapter regenerates and commits. You are fixing the CODE, not regenerating the
chapter.

## Evidence (read this first — it is the signal, not a one-line summary)

A status string is not enough to diagnose this. The goal-loop harvested the deep
failure picture for THIS chapter — the live DB row + the UNTRUNCATED
`last_error`, the artifact the lane actually produced (its `##`/`###` headings +
title), whether mini-ork's in-sandbox verify passed while the host guard rejected
post-hoc, and the produced-vs-required heading delta read from researcher source:

{{evidence}}

(One-line predicate reason, for reference: `{{reason}}`. The full evidence is
also written to `{{evidence_path}}`.)

## How to think about this

Read the evidence and form your OWN root-cause hypothesis. The evidence may
include a "why this likely re-rolls forever" section — treat that as a LEAD to
confirm in source, not as gospel.

Two failure shapes are common here; the evidence tells you which one you have:

1. **The lane drifts and is never corrected.** The node's prompt already asks
   for the required headings, but the lane emits different ones, and the
   in-sandbox repair turn is handed no structural finding — so it re-rolls the
   whole chapter forever. If the evidence shows the required headings ARE already
   in the prompt and the produced headings still differ, **do not just re-word
   the prompt.** That has been tried; the lane ignores it. Trace WHY the repair
   turn receives no corrective signal for this node and fix THAT, so a drifting
   node is told exactly which `## <heading>` it is missing on its next turn.

2. **The contract and the prompt genuinely disagree.** The prompt asks for
   headings the caller-contract does not require (or vice-versa). Reconcile them
   at the source of truth rather than papering over one side.

Prefer a **class-level** fix — one that repairs every node sharing the failure
mode — over a single-node patch, when the evidence shows the same mechanism can
hit sibling nodes.

## Where to trace (leads to confirm, not a checklist to follow blindly)

- The lens spec named in the evidence: the produced `node_type`'s
  `requiredSections` + `sectionPolicy`. Note whether the policy is presence
  (unset) or `exact_h2`.
- `server/compose/verifiedArtifact/verifiedArtifactProduction.ts` — how repair
  findings are built and what they gate on; specifically whether a
  presence-policy node's `requiredSections` ever reach the repair turn, or
  whether the repair signal is gated behind `sectionPolicy === 'exact_h2'` /
  `requiredH2Headings` and therefore silent for presence-policy nodes.
- The in-sandbox artifact contract vs. the host `composeArtifactGuardFor` guard.
  If the in-sandbox verify passes vacuously (no declared outputs) while the host
  rejects, the lane is never told what it got wrong — the two contracts disagree.

## Scope

Edit only files under `server/`. Do NOT touch tests, infra, migrations, or
anything outside `server/`. Keep the diff minimal and targeted at the root cause.

## Success criteria

- The fix addresses the mechanism the evidence points to — not a cosmetic prompt
  tweak the lane will ignore, and not a one-off special-case for this single
  chapter.
- Minimal, reviewable diff — no drive-by refactors.
- `tsc` stays green for the files you touched.

## Model preference

`minimax` / `codex` for the implementer (code lane — never glm).

## Verification (the outer loop runs it, not you)

After your patch lands on researcher `main` and the book-generation worker
restarts, chapter {{unit_id}} regenerates and its `book_chapter_lifecycle` row
flips to `committed_complete=true rubric_status=pass`. The goal-loop drives that
redispatch and re-checks every chapter. Your job is the correct, minimal
`server/` patch — nothing else.
