# mini-ork

Task operating system for agents: classify → plan → execute → verify → reflect → improve.
Heterogeneous-model recipe runner with cost governance, runtime verification, and a GRPO learning loop.

This file is the canonical context map. Detail lives in `docs/`; procedural knowledge lives in recipe prompts under `recipes/<name>/prompts/`.

## Map

- **[docs/architecture](docs/architecture)** — system design and component diagrams
- **[Artifact graph contracts](docs/architecture/artifact-graph.md)** — declared ports, run-local manifests, transforms, and visibility limits
- **[Cloud sandbox providers](docs/architecture/cloud-sandbox-providers.md)** — node providers (static VMs, Daytona, …) behind one node-agent protocol; `--placement remote --env <profile>`
- **[Verification stack](docs/architecture/verification-stack.md)** — relations, differential, suite adequacy, level vector, gate hackability, equivalence operator (all ON by default)
- **[docs/operator](docs/operator)** — running mini-ork, env vars, troubleshooting
- **[Python SDK](docs/PYTHON-SDK.md)** — embed mini-ork: importable primitives + the `MiniOrk` orchestrator client
- **[recipes](recipes)** — available task recipes (`code-fix`, `bug-audit-cmgk`, `framework-edit`, …)
- **[schemas](schemas)** — `task_class.schema.json`, `workflow.schema.json`, `artifact_contract.schema.json`
- **[tests](tests)** — unit and e2e tests

## Working in this repo

- Entrypoint: `bin/mini-ork <subcommand>`
- Python runtime is the only runtime (bash entrypoints removed in the 2026-07 bash-removal; see `docs/plans/2026-07-26-bash-removal-plan.md`).
- Path contract: `mini_ork.context.RunContext` resolves `MINI_ORK_ROOT`/`MINI_ORK_HOME`/`MINI_ORK_DB` (formerly `lib/paths.sh`).
- `mini-ork init` scaffolds `.mini-ork/` and writes a committed `.mini-ork/engine` pointer.

## Dev loop — worktree first

`main` stays clean: implementation work never happens in the main checkout. A
`reference-transaction` guard (`.githooks/reference-transaction`) blocks direct
feature-branch creation — branch through a worktree instead.

```bash
make worktree SLUG=<slug>            # new worktree + branch off origin/main
#   … edit + commit inside the worktree …
make worktree-merge SLUG=<slug>      # rebase origin/main → green-gate → push HEAD:main
make worktree-clean SLUG=<slug>      # remove worktree + delete branch
```

- Worktrees live under `/Volumes/docker-ssd/ps/mini-ork-worktrees/<slug>`
  (`MINI_ORK_WORKTREES_DIR` to override); branches are `wt/<slug>`.
- `--owns <path>` claims a file surface (CAID registry); a second worktree whose
  claim overlaps a live one is refused, so concurrent agents can't race a file.
  `make worktree SLUG=x OWNS="mini_ork/foo.py tests/bar"`.
- The green gate runs `python3 -m pytest -q` before pushing; scope it per-task
  with `MINI_ORK_TEST_CMD` (e.g. a single parity gate for a fast merge).
- Merge is `push HEAD:main` (no PR); never `reset --hard` or revert main.
- One-time per clone: `make install-hooks` (activates `.githooks/`).

`scripts/mini-ork-worktree.sh` is the same flow without `make` (and is what the
targets call). It takes an mkdir lock (`$MINI_ORK_WORKTREE_LOCK`, default
`$TMPDIR/mini-ork-worktree.lockdir`, 30 s wait) around the two check-then-act
pairs — the claim-check/register on create, and the re-rebase/push on merge — so
two concurrent agents cannot both pass an ownership check or interleave a stale
push. The green gate runs outside the lock, so a long gate never blocks another
worktree command.

**Merge discipline (the part that goes wrong):**

- **Never `git add -A`.** It sweeps other sessions' work into your commit. Stage
  explicit paths, or `git add <dir>` for a directory you own end to end.
- **Scope assertion.** Read `git diff --stat` before committing: if the diff
  contains files, and especially *deletions* of files you never touched or
  authored, your base is stale — rebase, do not merge.
- **Mandatory post-merge refresh.** After pushing, run
  `git -C <repo> fetch origin main && git -C <repo> merge --ff-only origin/main`.
  Local `main` is stale until it does; a later worktree created from it starts
  behind.
- **If `--ff-only` refuses, STOP.** That means local `main` and `origin/main`
  diverged (someone pushed while you worked). Do **not** `git merge origin/main`
  plain, do **not** `git reset --hard`, do **not** `--no-verify` past it.
  Reconcile so that every commit on both sides survives — a merge commit keeps
  all of them; a rebase rewrites them — and never rewrite a commit that is not
  yours.
- **On smoke failure, fix forward** — from a NEW worktree off latest `main`.
  Never revert `main`, never `git reset --hard`.
- **Commit, merge, push — without being asked.** A finished, verified change is
  shipped, not parked in a worktree.

## Quality gates (run before committing)

```bash
make test                    # existing test suite
make lint                    # ruff blocking tier (F + E9; advisory: make lint-advisory)
mini-ork validate            # pre-run static checks
mini-ork garden              # drift detection
```

CI mirrors these: `.github/workflows/ci.yml` runs ruff (blocking + advisory
tiers), pytest on 3.11/3.12 (sharded), UI typecheck, and web smoke;
`.github/workflows/release.yml` re-runs the blocking lint + a unit-layer
pytest smoke before building release artifacts.

## Feature delivery: two tiers, always

Every new feature ships in two tiers, and both are part of the definition of done:

1. **Secure, cloud-scalable tier** — the recommended one for real and shared use. It must survive a hostile or merely careless deployment: secrets come from a real secret store, access is scoped per node/agent rather than globally, nothing lands in a log or a shell history, and the design still holds when the work moves off one machine onto many.
2. **Simple tier** — for someone trying mini-ork on their own laptop. No server, no daemon, no cloud account, no network round-trip; it must work after at most copying an example config. If the feature only works once infrastructure is stood up, it is not finished.

Do not pick one and call it done. Do not make the simple tier a toy that silently drops the security property the feature exists for: it may fall back to a local file or the environment, but it must fail closed (a missing value is a clear error, never a silent empty string). Document both, and let one config file choose between them, so the user can start simple and switch to the secure tier without rewriting call sites. The vault is the reference implementation of this shape (`config/vault.example.yaml`, `mini-ork vault status`).

## Extension points

- Recipes: add a directory under `recipes/` with `task_class.yaml`, `workflow.yaml`, `artifact_contract.yaml`.
- Providers: add entries to `config/providers.yaml` and env vars to `config/secrets.local.sh`.
- Gates: register an evaluator via `mini_ork.gates.gate_registry.register_gate_evaluator(type, fn)`.

### Python runtime registries (SOLID refactor, 2026-07)

The Python runtime exposes registration APIs so new behavior lands without
editing executor core modules:

- `mini_ork.context` — canonical `RunContext` env contract; mutate process env
  only via `apply_env_overrides` / `scoped_environ`.
- Node types: `mini_ork.cli.execute.register_node_handler(type, fn, phase=)`
  (+ `register_implementer_submode` for fan-out dispatchers).
- Routing policies: `mini_ork.dispatch.routing.register_policy(name, fn)`
  (selected via `MO_ROUTING_POLICY`).
- Gate types: `mini_ork.gates.gate_registry.register_gate_evaluator(type, fn)`.
- Provider kinds / transports: `mini_ork.dispatch.providers.register_provider_kind`
  / `register_dispatch_backend`.
- Subcommands: `mini_ork.cli.main.register_subcommand(name, handler)`.
- Embedders: `mini_ork.memory.semantic.register_embedder_provider(name, factory)`
  (selected via `MO_EMBED_PROVIDER`).

Executor concerns live in dedicated modules: lane routing in
`mini_ork/dispatch/routing.py`, GRPO writeback in `mini_ork/learning/writeback.py`,
publish gates + delivery in `mini_ork/cli/publisher.py`.
