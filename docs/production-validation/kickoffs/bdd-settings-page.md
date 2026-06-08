# Production scenario: BDD settings page

## Goal

Build a small settings page in a throwaway React app using the
`bdd-first-delivery` recipe.

## Target repo

A throwaway React/Vite fixture created for the run.

## Product behavior

Add `/settings` with three independent sections:

- Theme: light / dark / system.
- Language: en / de / fr / es.
- Notifications: email and push toggles.

## Backend contract

`GET /api/user/settings` returns:

```json
{
  "theme": "dark",
  "language": "en",
  "notifications": { "email": true, "push": false }
}
```

`PATCH /api/user/settings` accepts partial updates.

## Success criteria

- Decomposer emits at least three sub-epics.
- Spec author writes Playwright specs for the UI surfaces.
- BDD runner verifies route render, state load, PATCH success, and PATCH 500 rollback.
- Reviewer either approves or gives specific REQUEST_CHANGES with failing spec evidence.

## Verification command

```bash
npx playwright test
```

## Risk tolerance

Medium. Do not publish to a real application repo; run in a fixture first.
