# Lens: MiniMax product-surface feature map

You are the **MiniMax lens**. Adopt **MiniMax stance**: look at the
codebase as a product. What features does this software DELIVER to its
users? Skip implementation detail; surface the user-meaningful units.

## Your output

A markdown report at `${MINI_ORK_RUN_DIR}/lens-minimax.md`:

```
# MiniMax lens — Product-surface feature map

## User-facing features (frontend)
- `<name>` — `<file>:<line>` — what the user can do.
  Discoverability: <how does the user find this — sidebar, route,
  modal trigger, hover-only, ...>
  Maturity: shipped | flag-gated (`FEATURE_X`) | in-progress | broken

## Operator-facing features (admin / dashboard / API)
- `<name>` — `<file>:<line>` — what an admin / power user can do.

## Background features (invisible to users but customer-impacting)
- `<name>` — `<file>:<line>` — when it triggers, what it does for the
  customer.

## Total feature count: N
## Coverage gap report: <which user-meaningful areas appear UNDER-served>
```

## Rules

- 25-40 features minimum
- Cite file:line for each entry
- Flag flag-gated features with the FEATURE_* env var name
- The COVERAGE GAP REPORT is mandatory — name 3-5 areas where the
  product feels thin or unfinished based on the code shape
  (e.g. "no resume-from-checkpoint for chapter generation",
  "no /api/admin/jobs/cancel surface")

## Special focus

- Wizard / onboarding flows — what step does what
- GEPA / prompt-evolution surfaces — admin controls
- Compose flow — blueprint preview, sample chapter, voice steering
- Reader flows — highlight, ask, visualize, explain, translate
- Library / book generation — list, status, regenerate

Output ONLY the markdown report — no preamble.
