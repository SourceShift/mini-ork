# Release process

mini-ork uses [Semantic Versioning](https://semver.org/) (SemVer) starting
from v0.1.

## Version policy

**Major (X.0.0)** — backward-incompatible changes. After v1.0:
- Removing or renaming a `bin/mini-ork-*` subcommand
- Changing the signature of a `lib/*.sh` primitive function in a way that
  breaks recipes
- Removing or renaming a state.db table column without a migration that
  preserves the column under its old name
- Changing the contract of an extension point (`schemas/*.schema.json`)

**Minor (0.X.0)** — additive features that don't break v0.1+ recipes:
- New `bin/` subcommand
- New `lib/` primitive
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
3. **Update `bin/mini-ork version` string** if the version is bumped.
4. **Tag** locally:
   ```bash
   git tag -a vX.Y.Z -m "vX.Y.Z — <one-line title>"
   ```
5. **Verify**:
   ```bash
   bash tests/smoke.sh
   bash -n $(find bin lib hooks tests -type f -name "*.sh")
   ```
6. **Push tag**:
   ```bash
   git push origin main vX.Y.Z
   ```
7. **GitHub release**: CI auto-creates a draft release on tag push (see
   `.github/workflows/release.yml`). Edit the draft to match the CHANGELOG
   entry, then publish.

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
