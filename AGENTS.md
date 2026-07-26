# mini-ork

Task operating system for agents: classify → plan → execute → verify → reflect → improve.
Heterogeneous-model recipe runner with cost governance, runtime verification, and a GRPO learning loop.

This file is the canonical context map. Detail lives in `docs/`; procedural knowledge lives in recipe prompts under `recipes/<name>/prompts/`.

## Map

- **[docs/architecture](docs/architecture)** — system design and component diagrams
- **[Artifact graph contracts](docs/architecture/artifact-graph.md)** — declared ports, run-local manifests, transforms, and visibility limits
- **[docs/operator](docs/operator)** — running mini-ork, env vars, troubleshooting
- **[recipes](recipes)** — available task recipes (`code-fix`, `bug-audit-cmgk`, `framework-edit`, …)
- **[schemas](schemas)** — `task_class.schema.json`, `workflow.schema.json`, `artifact_contract.schema.json`
- **[tests](tests)** — unit and e2e tests

## Working in this repo

- Entrypoint: `bin/mini-ork <subcommand>`
- Python runtime is live; set `MINI_ORK_RUNTIME=bash` to use legacy bash entrypoints.
- Path contract: `lib/paths.sh` resolves `ENGINE_ROOT`, `PROJECT_HOME`, `TARGET_REPO`.
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

## Quality gates (run before committing)

```bash
make test                    # existing test suite
make lint                    # ruff blocking tier (F + E9; advisory: make lint-advisory)
mini-ork validate            # pre-run static checks
mini-ork garden              # drift detection
```

CI mirrors these: `.github/workflows/ci.yml` runs ruff (blocking + advisory
tiers), pytest on 3.11/3.12, shellcheck, and the bash test layers;
`.github/workflows/release.yml` re-runs the blocking lint + a unit-layer
pytest smoke before building release artifacts.

## Extension points

- Recipes: add a directory under `recipes/` with `task_class.yaml`, `workflow.yaml`, `artifact_contract.yaml`.
- Providers: add entries to `config/providers.yaml` and env vars to `config/secrets.local.sh`.
- Gates: add a script under `gates/` and register it in `lib/gate_registry.sh`.

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
