# Secret scanning — `make secrets` and the CI gate

A credential committed to a public repository is compromised the moment it is
pushed: the commit is mirrored, cached and indexed, and deleting it later does
not undo any of that. This repository takes contributions from several agents
working in parallel, each of them wiring API keys into config it is testing, so
"we would notice" is not a control.

There are two `gitleaks` runs over the same `.gitleaks.toml`, and both must be
green before a change is merged:

| | Where | What it scans |
|---|---|---|
| **Simple tier** | `make secrets`, on your laptop | the working tree, including uncommitted and staged changes |
| **Secure tier** | `secret-scan` job in `.github/workflows/ci.yml` | the checkout; a required dependency of `ci result` |

The engine and the config are the same in both, so a green `make secrets` means
the CI job will pass. The difference is the scan root: your working tree has
git-ignored directories that a checkout does not.

## Simple tier — run it before you commit

```bash
brew install gitleaks     # one time; the CI job installs its own copy
make secrets
```

Exit 0 is clean, exit 1 is a finding. The target **fails closed**: if `gitleaks`
is not on `PATH` it prints how to install it and exits 1 rather than reporting a
clean tree it never looked at.

Findings are printed with `--redact`, so the credential itself does not land in
your terminal scrollback or shell history. If you get a finding:

1. **Rotate the key first.** Assume it is compromised from the moment it existed
   in a file on disk, not from the moment CI saw it.
2. Remove it from the working tree and from the commit (`git rebase -i` or
   `git commit --amend`) — rewriting a pushed branch needs a force-push and a
   coordination note, never a silent one.
3. Only then read the next section, if you believe the finding is a fixture.

## Secure tier — the CI job

`secret-scan` installs a **pinned** gitleaks (the version is a `GITLEAKS_VERSION`
env var in the job) and verifies the release's published SHA-256 checksum before
running it. A compromised or truncated release asset fails the install step
instead of scanning nothing.

It is listed in `ci-result`'s `needs:` **and** in that job's `expected` set. Both
are needed: `expected` is what the aggregate actually checks, and it rejects
`skipped` — so a job that never ran cannot read as green.

The CLI is used rather than `gitleaks/gitleaks-action` because the action
requires a paid licence on organisation accounts.

## What the config exempts, and why it is narrow

`.gitleaks.toml` extends the default ruleset (dropping `useDefault` would leave
two allowlists and no rules at all) and adds three exemptions. Each one is
scoped by **path *and* value**, never by path alone:

- **Test fixtures** — `tests/` values that carry an obvious dummy marker
  (`sample`, `dummy`, `1234567890`, …) or are the RFC 6455 handshake nonce. A
  real-looking credential pasted into a test file is still a finding, because a
  test file is exactly where people paste one.
- **PostHog project keys** — `phc_…` is a write-only ingestion token shipped to
  the browser by design; only that value shape, only in the built asset tree.
- **Git-ignored trees** — `.mini-ork/config/`, `config/secrets.local.sh`,
  `docker/openbao/`, `.runtime/`, `node_modules/`, `.venv/`. These are *supposed*
  to hold this install's real keys; scanning them would fail every local run on
  a file that is correct by design. CI scans a checkout, where none exist.
  `__pycache__/` is here for a different reason: a stale `.pyc` reports a
  finding the source no longer contains, and the source is scanned anyway.

`.gitleaks.toml` is a **high-risk path** in `config/risk-tiers.yaml`, so widening
an exemption needs a merge approval. That is the point: the allowlist is the
gate's attack surface, and loosening it should cost more than editing a comment.

## Related

- `docs/operator/review-gates.md` — what else gates a merge, and the risk tiers.
- `docs/architecture/verification-stack.md` — where a secret scan sits relative
  to the run-time verification gates.
