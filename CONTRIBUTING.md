# Contributing to mini-ork

Thank you for contributing. This document covers the issue workflow, PR process, code style, and how to test changes.

## Reporting Bugs

Use [GitHub Issues](https://github.com/SourceShift/mini-ork/issues/new?template=bug_report.md). Include:

1. **Version** — `mini-ork --version`
2. **Reproduction** — exact command + a minimal `kickoff.md` that triggers the bug
3. **Observed behavior** — paste the relevant lines from `.mini-ork/runs/<run-id>/run.log`
4. **Expected behavior** — what should have happened
5. **Environment** — OS, bash version (`bash --version`), sqlite3 version (`sqlite3 --version`), claude CLI version

## Requesting Features

Use [GitHub Issues](https://github.com/SourceShift/mini-ork/issues/new?template=feature_request.md). Describe the use case first, then the proposed behavior. If you have a draft implementation, link the branch.

## Pull Request Flow

`main` is protected, and the repo's own guards assume the worktree flow: a
`reference-transaction` hook refuses a feature branch created directly in the
main checkout. Branch through a worktree rather than `git checkout -b`:

1. `make install-hooks` once per clone — activates the branch guard and the
   pre-push claim check.
2. `make worktree SLUG=short-description [OWNS="path1 path2"]` — creates
   `$MINI_ORK_WORKTREES_DIR/<slug>` on branch `wt/<slug>` off `origin/main`.
   `OWNS` claims the file surface so a concurrent agent cannot edit the same
   files.
3. Make your changes inside the worktree (see Code Style below) and commit
   there. Never commit to `main` in the primary checkout.
4. Run the targeted checks in [Testing](#testing) as you go, and the full set
   before you merge.
5. `make worktree-merge SLUG=short-description` — rebases on `origin/main`,
   runs the green gate (`python3 -m pytest -q`; scope it per task with
   `MINI_ORK_TEST_CMD`), then pushes to `main`.
6. `make worktree-clean SLUG=short-description` — removes the worktree.

Contributors without the hooks installed (a fork, a scratch clone) can open a
pull request against `main` as usual; CI runs the same checks and the
`ci result` job is the one required status.

## Code Style

**Python**

- Python 3.11+. The runtime is Python only — the bash entrypoints were removed
  in the 2026-07 bash-removal
  (`docs/plans/2026-07-26-bash-removal-plan.md`). Do not add a new `.sh`
  entrypoint; port it or use an existing subcommand.
- `ruff` is the linter. `make lint` runs the blocking tier — pyflakes (`F`)
  plus the pycodestyle error class (`E9`) — and must stay at zero.
  `make lint-advisory` runs the style tier (`E,W,I,UP,B`) as a non-blocking
  ratchet, not a gate. Line length is 110.
- No silent fallbacks. `except: pass` and `return None` on error are refused by
  `mini-ork fallbacks` in changed files; a deliberate exception carries
  `# fallback-guard: allow: <why>` on the handler.
- Extend the runtime through its registries (`register_node_handler`,
  `register_gate_evaluator`, `register_provider_kind`, …) instead of editing an
  executor core module — see `docs/architecture`.

**Naming**

- State.db table names: `snake_case`, plural nouns (`epics`, `epic_reviews`, `runs`).
- Environment variables: `MINI_ORK_<COMPONENT>_<NAME>` (all caps).
- Epic IDs: 8-char hex prefix of SHA-256 of `<run_id>:<epic_index>`. `MINI_ORK_HOME`
  is the one root every path derives from.

**YAML / JSON**

- `agents.yaml`: 2-space indent, no tabs.
- No trailing whitespace. Files end with a single newline. Every
  `workflow.yaml` / `task_class.yaml` validates against the schema in
  `schemas/`.

**Markdown**

- Wrap prose at 100 chars.
- Code blocks always specify a language tag.

## Testing

The suite is pytest; there is no shell smoke harness (it went with the bash
runtime). Run the file you changed while you work, and the full suite once,
before you merge:

```bash
python3 -m pytest tests/unit/test_dispatch_retry_py.py -q   # the file you changed
python3 -m pytest -q                                        # full suite (make test)
```

Pass the test **file path**, never a bare `-k` / `-t` name filter: the filter
makes pytest collect the whole tree first, measured at 11 workers x ~900 MB and
8+ minutes of load average 124–138 on an 11-core box. If you cannot name the
file, do not run the suite.

`tests/README.md` documents the layer split and the DB fixtures. CI runs the
same checks locally available — ruff, the sharded pytest matrix on 3.11/3.12,
the README/doc claim check, the verifiers-pin check and the web smoke suite —
plus fast failures CI alone can afford; run the blocking lint before you push
and let CI catch what is uncommon.

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

**mini-ork is Apache-2.0, and stays Apache-2.0.** If you use it — in a product, a
company, a service, closed-source or not — nothing here changes anything for you.
Use it, fork it, sell what you build with it. That is the point of the license, and
we are not walking it back. Every version already published under Apache-2.0 is
irrevocable: it cannot be un-published or retroactively relicensed, by us or anyone.

The rest of this section applies **only if you contribute code.**

### Contributor grant

By submitting a contribution (a pull request, patch, or any work intentionally
sent for inclusion), you certify that:

1. **You wrote it, or you have the right to submit it.** It is your original work,
   or it is covered by a compatible open-source license and you are permitted to
   submit it under Apache-2.0. It is not covered by an employment or client
   agreement that would give someone else ownership without their permission.

2. **You retain copyright to your contribution.** You are not signing it away.

3. **You grant a license to it.** You grant Amir Khakshour and every recipient of
   mini-ork a perpetual, worldwide, non-exclusive, royalty-free, irrevocable
   license to reproduce, modify, publicly display, distribute, **and sublicense**
   your contribution and derivative works of it. You grant the same terms for any
   patent claims you own that your contribution necessarily infringes.

4. **You understand your contribution is public** and distributed under Apache-2.0.

Clause 3 is the one that matters, so here is the plain-English version of why it
exists: the word **"sublicense"** is what lets the project's copyright stay
consolidated — so it can be assigned to a company, dual-licensed for enterprise
customers, or offered under different terms to a specific customer, without
tracking down every past contributor for permission. Projects that skip this end
up unable to make any licensing decision at all once they have contributors.

**What it is not:** it is not a copyright assignment (you keep yours, clause 2),
and it is not permission to take the open-source core away from you (see the
promise above). Contributions are accepted by making them — there is no form to
sign and no document to email.

If you are contributing on behalf of an employer, make sure they are OK with the
above before you open the PR.
