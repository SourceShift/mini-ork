# Production scenario: docs real edit

## Goal

Document the production scenario lane in `examples/README.md` so new users know
the difference between the quick dry-run demo and production validation.

## Target repo

This repository: `mini-ork`.

## Scope allow

- `examples/README.md`
- `docs/production-validation/**`

## Scope deny

- `bin/**`
- `lib/**`
- `db/**`

## Success criteria

- `examples/README.md` points to `docs/production-validation/mini-ork-production-scenarios.md`.
- The text says `examples/00-demo.sh` is dry-run topology proof, not production validation.
- Existing relative links still resolve.

## Verification command

```bash
bash scripts/readme-claim-check.sh
```

## Risk tolerance

Low. Documentation-only publish is allowed.
