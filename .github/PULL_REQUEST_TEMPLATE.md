## Summary

<!-- One or two sentences: what does this PR do and why? -->

## Changes

<!-- Bulleted list of what changed. One line per logical change. -->

- 
- 

## Testing

- [ ] `make lint` clean (ruff blocking tier: F + E9)
- [ ] Targeted pytest for the changed file passes (`python3 -m pytest tests/unit/test_<area>_py.py -q`)
- [ ] Full suite green before merge (`python3 -m pytest -q`)
- [ ] `mini-ork fallbacks` clean on the changed `.py` files (no silent `except: pass` / `return None`)
- [ ] Updated `docs/` if config, env vars, or lifecycle changed

## Related Issues

<!-- Closes #NNN -->

## Checklist

- [ ] Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/)
- [ ] No hard-coded paths — every path derives from `MINI_ORK_HOME`
- [ ] New behavior registered through a runtime registry, not patched into an executor core
- [ ] No new `.sh` entrypoint (the runtime is Python only)
- [ ] No secrets or internal identifiers in code, comments, or examples
