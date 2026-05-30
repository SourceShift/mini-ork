# Contributing to mini-ork

Thank you for contributing. This document covers the issue workflow, PR process, code style, and how to test changes.

## Reporting Bugs

Use [GitHub Issues](https://github.com/ork-ai/mini-ork/issues/new?template=bug_report.md). Include:

1. **Version** — `mini-ork --version`
2. **Reproduction** — exact command + a minimal `kickoff.md` that triggers the bug
3. **Observed behavior** — paste the relevant lines from `.mini-ork/runs/<run-id>/run.log`
4. **Expected behavior** — what should have happened
5. **Environment** — OS, bash version (`bash --version`), sqlite3 version (`sqlite3 --version`), claude CLI version

## Requesting Features

Use [GitHub Issues](https://github.com/ork-ai/mini-ork/issues/new?template=feature_request.md). Describe the use case first, then the proposed behavior. If you have a draft implementation, link the branch.

## Pull Request Flow

1. Fork the repo and create a branch from `main`: `git checkout -b fix/short-description`
2. Make your changes (see Code Style below).
3. Add or update an example under `examples/` that exercises the change.
4. Run the smoke test: `bash tests/smoke.sh`
5. Run shellcheck on every modified `.sh` file: `shellcheck lib/*.sh bin/mini-ork`
6. Open a PR against `main`. Fill out the PR template.
7. A maintainer will review within a few days. Address feedback with new commits (no force-push to open PRs).

## Code Style

**Shell**

- Target bash 4.0+. Use `#!/usr/bin/env bash` shebangs.
- `shellcheck` clean with no suppressed warnings. Run `shellcheck -x lib/*.sh bin/mini-ork` before pushing.
- Use bash arrays (`arr=()`, `"${arr[@]}"`) — never `eval` to build argument lists.
- Use `jq` for all JSON reads/writes. Never `grep`/`sed`/`awk` JSON fields.
- Quote all variable expansions: `"${var}"`, `"${array[@]}"`.
- `set -euo pipefail` at the top of every script.
- Functions are `snake_case`. Local variables use `local`.
- No hard-coded paths outside `lib/config.sh`. All paths derive from `MINI_ORK_HOME`.

**Naming**

- State.db table names: `snake_case`, plural nouns (`epics`, `epic_reviews`, `runs`).
- Environment variables: `MINI_ORK_<COMPONENT>_<NAME>` (all caps).
- Epic IDs: 8-char hex prefix of SHA-256 of `<run_id>:<epic_index>`.

**YAML / JSON**

- `agents.yaml`: 2-space indent, no tabs.
- No trailing whitespace. Files end with a single newline.

**Markdown**

- Wrap prose at 100 chars.
- Code blocks always specify a language tag.

## Testing via Examples

The primary test harness is `tests/smoke.sh`, which runs `mini-ork deliver` against each file in `examples/`:

```bash
bash tests/smoke.sh                  # all examples
bash tests/smoke.sh examples/01-hello-world.md   # single example
```

Each example `kickoff.md` includes an `<!-- expected-verdict: PASS -->` comment. The smoke test asserts the final run verdict matches.

To add a new test scenario:

1. Create `examples/NN-short-name.md` with a minimal, self-contained kickoff.
2. Include the expected-verdict comment.
3. Keep the kickoff small — the smoke test must complete in under 60 seconds.

Do not add LLM-dependent tests to the smoke suite. Smoke tests must run offline with a mocked `claude` binary (`tests/mocks/claude`).

## Commit Messages

Use [Conventional Commits](https://www.conventionalcommits.org/):

```
feat(bdd-runner): support multi-step Gherkin scenarios
fix(dispatch): retry on SIGPIPE from claude subprocess
docs(config): document MINI_ORK_MAX_ITERS env var
chore(ci): add shellcheck to workflow
```

One subject line ≤ 72 chars. Body optional. No scope for cross-cutting changes.

## License

By contributing, you agree that your contributions will be licensed under the Apache-2.0 License (see [LICENSE](LICENSE)).
