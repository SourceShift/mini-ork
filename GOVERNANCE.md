# Governance

mini-ork is run as a **lazy-consensus, single-maintainer** project. This document
describes how decisions get made until the project grows large enough to need a
more formal structure.

## Roles

- **Lead maintainer** — the person who started the project (currently:
  see [MAINTAINERS.md](./MAINTAINERS.md)). Has merge rights, sets direction,
  cuts releases, and resolves stalemates.
- **Contributors** — anyone who has had a PR merged. Recognized in the
  [CHANGELOG](./CHANGELOG.md) per release.
- **Reviewers** — contributors invited to review PRs before merge. Inactive
  reviewers (no activity in 6 months) rotate off; this is not a demotion.

## Decision process

mini-ork uses **lazy consensus**:

1. Anyone proposes a change via a Pull Request or GitHub Discussion.
2. The default is **acceptance after 72 hours** if no one objects.
3. If someone objects (with reasoning), the proposer either addresses the
   concern or asks the lead maintainer to break the tie.
4. The lead maintainer's role is to break stalemates and protect the project's
   architectural principles (see [docs/ARCHITECTURE.md](./docs/ARCHITECTURE.md)
   and [docs/SAFETY.md](./docs/SAFETY.md)), not to gatekeep every change.

For **breaking changes** or anything touching the framework's core primitives
(lib/, schemas/, db/migrations/, bin/), require ≥1 reviewer approval in
addition to lazy consensus.

For **recipes/** changes, lazy consensus alone is fine — recipes are user-land
and isolated.

## How to become a reviewer

Three signals, no formal application:

1. You have at least 3 merged PRs in the project.
2. You have reviewed at least 3 other contributors' PRs with substantive,
   constructive feedback.
3. A lead maintainer invites you in a public issue or Discussion.

## How to become a lead maintainer

If the current lead maintainer steps back, they nominate a successor from
active reviewers. If no nomination is made, the contributors vote (1 vote per
person with ≥3 merged PRs in the last 12 months).

## Project scope

mini-ork is intentionally small and composable. The framework ships the
**universal task loop** + **primitives**. Pipeline shapes live in `recipes/`
as user-land examples. Proposals that grow the framework with opinions about
specific pipeline shapes will typically be redirected to a recipe.

What stays in framework scope:

- Task-loop runtime (classify/plan/execute/verify/reflect/improve)
- Node-type interfaces (8 types)
- Edge-type semantics (6 types)
- Gate types (6 built-in)
- Memory namespaces (8 built-in)
- State.db schema (additive only)
- Recipe loader + schema validator

What lives in `recipes/` (user-land):

- Specific pipeline shapes (BDD-first, refactor-stage, bug-hunt patterns)
- Project-specific prompts
- Project-specific verifier scripts
- Domain-specific task classes

When in doubt, propose as a recipe first. Recipes can graduate into framework
primitives later if multiple recipes need the same abstraction.

## Code of conduct

See [CODE_OF_CONDUCT.md](./CODE_OF_CONDUCT.md). The lead maintainer enforces it.

## Security

See [SECURITY.md](./SECURITY.md) for the disclosure process.

## Funding

mini-ork is unfunded. No CLA, no copyright assignment. Contributions are
licensed under [Apache-2.0](./LICENSE) by submission.
