## Summary

<!-- One or two sentences: what does this PR do and why? -->

## Changes

<!-- Bulleted list of what changed. One line per logical change. -->

- 
- 

## Testing

- [ ] `bash tests/smoke.sh` passes locally
- [ ] `shellcheck lib/*.sh bin/mini-ork` clean (no warnings)
- [ ] Added/updated example in `examples/` if behavior changed
- [ ] Updated `docs/` if config, env vars, or lifecycle changed

## Related Issues

<!-- Closes #NNN -->

## Checklist

- [ ] Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/)
- [ ] No hard-coded paths outside `lib/config.sh`
- [ ] No `eval` for argument construction (use bash arrays)
- [ ] No `grep`/`sed`/`awk` to parse JSON (use `jq`)
- [ ] `set -euo pipefail` present in any new `.sh` file
- [ ] No secrets or internal identifiers in code, comments, or examples
