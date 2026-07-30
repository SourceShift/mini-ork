# Release process

mini-ork uses [Semantic Versioning](https://semver.org/) (SemVer) starting
from v0.1.

## Version policy

**Major (X.0.0)** — backward-incompatible changes. After v1.0:
- Removing or renaming a `bin/mini-ork-*` subcommand
- Changing a documented native Python extension point in a way that breaks
  recipes or integrations
- Removing or renaming a state.db table column without a migration that
  preserves the column under its old name
- Changing the contract of an extension point (`schemas/*.schema.json`)

**Minor (0.X.0)** — additive features that don't break v0.1+ recipes:
- New `bin/` subcommand
- New native Python primitive or extension point
- New migration (always additive — never remove a column without major bump)
- New recipe
- New schema (existing schemas stay compatible)

**Patch (0.0.X)** — bug fixes + doc-only changes:
- Bug fix in any `bin/` or `lib/` file
- New tests
- Doc rewrites
- CI changes

Until v1.0, **minor** versions may include breaking changes if essential to
the redesign trajectory. Each breaking change is called out in CHANGELOG.

## Cutting a release

1. **Pick the bump** based on policy above.
2. **Update CHANGELOG.md** with the new entry. Use the existing format:
   ```
   ## [X.Y.Z] - YYYY-MM-DD
   ### Added / Changed / Fixed / Removed / Deprecated / Security
   - one-line item
   ```
3. **Update version surfaces**: `pyproject.toml`, the regex-readable version
   literal in `bin/mini-ork`, and `mini-ork version`. Keep the CLI contract
   tests aligned with the package metadata.
4. **Verify the release commit**:
   ```bash
   make lint
   make test
   bin/mini-ork validate
   bin/mini-ork garden
   ```
5. **Merge and push the verified release commit** using the worktree gate.
6. **Tag the pushed `main` commit** locally:
   ```bash
   git tag -a vX.Y.Z -m "vX.Y.Z — <one-line title>"
   ```
7. **Push the tag**:
   ```bash
   git push origin vX.Y.Z
   ```
8. **GitHub release**: the tag workflow runs blocking Ruff and unit pytest,
   builds the Python and UI artifacts, then publishes the GitHub release with
   generated notes (see `.github/workflows/release.yml`). Confirm the workflow
   and release assets before announcing it.

## Backward-compatibility commitments

After v1.0:
- A v1.x install can read a state.db created by any earlier v1.y (migrations
  are additive).
- A v1.x recipe directory continues to work with any later v1.y.
- Removed primitives go through a deprecation cycle: minor release marks
  deprecated → next major release removes.

Before v1.0:
- Breaking changes happen, but every breaking change is documented in
  CHANGELOG with a migration note.

## Deprecation policy

A primitive marked `@deprecated since vX.Y` must:

- Still work (with a warning printed to stderr on use)
- Be documented in CHANGELOG as deprecated
- Have a replacement named in the deprecation warning
- Survive ≥1 full minor version before removal in the next major

## Pre-release tags

Use `vX.Y.Z-rc.N` for release candidates:

```bash
git tag -a v0.2.0-rc.1 -m "v0.2.0-rc.1 — memory layer wiring (preview)"
```

RCs are signal to users that the API may shift before final.
