# Recursive validation example

## Goal

Validate the recent authentication-middleware refactor and continue iterating
until the middleware satisfies the probes below without weakening the existing
security boundary.

## Feature scope

- Preserve current session-token validation behavior.
- Move repeated header parsing into a shared helper.
- Keep public route behavior unchanged for authenticated and anonymous users.

## Definition of Done (probes)

```bash
# P1: touched TypeScript files typecheck
pnpm type-check:touched src/auth/middleware.ts src/auth/session.ts

# P2: middleware unit tests pass
pnpm test -- src/auth/__tests__/middleware.test.ts

# P3: no route skips auth accidentally
rg -n "skipAuth|bypassAuth" src/auth src/routes && exit 1 || exit 0
```

## Hard rules

- Do not store raw session tokens in logs.
- Do not broaden anonymous access to authenticated routes.
- Do not edit unrelated routing, billing, or database files.

## Success command

```bash
pnpm test -- src/auth/__tests__/middleware.test.ts
```

## Expected outputs

- `${MINI_ORK_RUN_DIR}/implementer-summary.json`
- `${MINI_ORK_RUN_DIR}/tier1-evidence.log`
- `${MINI_ORK_RUN_DIR}/tier2-evidence.log`
- `${MINI_ORK_RUN_DIR}/tier3-evidence.log`
- `${MINI_ORK_RUN_DIR}/tier4-{family}.md`
- `${MINI_ORK_RUN_DIR}/reflector.json` when any tier fails
- `${MINI_ORK_RUN_DIR}/replan.json` when reflection mutates the plan
