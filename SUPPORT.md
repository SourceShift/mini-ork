# Getting Help

There is no community chat yet. Please use the channels below in order of
preference:

## 1. Read the docs first

- [README](./README.md) — quickstart + concepts
- [docs/ARCHITECTURE.md](./docs/ARCHITECTURE.md) — universal loop + primitives
- [docs/CONFIG.md](./docs/CONFIG.md) — task classes, lanes, env vars
- [docs/EXTENSION.md](./docs/EXTENSION.md) — adding your own recipes/verifiers
- [docs/REDESIGN.md](./docs/REDESIGN.md) — v0.0 → v0.1 migration
- [docs/SAFETY.md](./docs/SAFETY.md) — bounded-autonomy ladder + gates
- [examples/](./examples/) — runnable recipes

## 2. Try the smoke test

```bash
bash tests/smoke.sh
```

If it fails on YOUR machine but passes in CI, the failure pattern is the
fastest way to identify the gap.

## 3. Search existing issues + discussions

Before opening a new issue, search:

- GitHub Issues for known bugs / feature requests
- GitHub Discussions for usage questions (enable Discussions in the repo
  Settings → Features tab if you maintain a fork)

## 4. Open an issue

Use the appropriate template:

- `Bug report` — something doesn't work as documented
- `Feature request` — propose a new framework primitive or recipe
- `Question` — usage help (use Discussions instead if available)

## 5. Security issues

See [SECURITY.md](./SECURITY.md). Do NOT open public issues for security
problems.

## What's NOT in scope for support

- Help debugging your own recipes / prompts that don't touch mini-ork itself
  → use the LLM provider's docs (Anthropic, OpenAI, etc.)
- Help with your project's typecheck/test commands → those are user-supplied
  via `MINI_ORK_TYPECHECK_CMD` / `MINI_ORK_TEST_CMD` env vars
- Help integrating with a specific CI provider (GitHub Actions, GitLab CI,
  CircleCI) — the project ships a GitHub Actions workflow as reference; other
  CIs are user-extensions

## Response-time expectation

mini-ork is currently maintained by a single person in unpaid time.

- Security reports: response within 72 hours
- Critical bugs: response within 1 week
- Feature requests / questions: best-effort

If you need commercial support or guaranteed SLAs, please open a Discussion
and we'll route you to maintainers willing to take on paid work.
