# Docs Recipe Example Kickoff

## Goal

Update the project documentation so it matches the current CLI and recipe
surface.

## Scope

- `README.md`
- `docs/ARCHITECTURE.md`
- `docs/CONFIG.md`
- `docs/EXTENSION.md`

Do not change shell, Python, SQL, or provider code.

## Required Changes

- Replace stale command examples with commands that exist today.
- Document any shipped recipe or entrypoint that is currently missing from the
  public docs.
- Keep claims verifiable by local paths, script names, or explicit counts.

## Grep Assertions

- `recursive-self-improve`
- `bin/mini-ork-self-improve`
- `MINI_ORK_DRY_RUN=1 bin/mini-ork run docs`

## Link Expectations

- Relative links inside `docs/` must resolve from the document location.
- Recipe links should point to real files or directories.

## Done When

- The requested docs are updated.
- `recipes/docs/verifiers/grep-assert.sh` passes for the listed assertions.
- `recipes/docs/verifiers/link-verifier.sh` passes for local links in the
  changed files.
