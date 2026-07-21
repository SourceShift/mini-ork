# mini-ork

Task operating system for agents: classify → plan → execute → verify → reflect → improve.
Heterogeneous-model recipe runner with cost governance, runtime verification, and a GRPO learning loop.

This file is the canonical context map. Detail lives in `docs/`; procedural knowledge lives in recipe prompts under `recipes/<name>/prompts/`.

## Map

- **[docs/architecture](docs/architecture)** — system design and component diagrams
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
mini-ork validate            # pre-run static checks
mini-ork garden              # drift detection
```

## Extension points

- Recipes: add a directory under `recipes/` with `task_class.yaml`, `workflow.yaml`, `artifact_contract.yaml`.
- Providers: add entries to `config/providers.yaml` and env vars to `config/secrets.local.sh`.
- Gates: add a script under `gates/` and register it in `lib/gate_registry.sh`.
