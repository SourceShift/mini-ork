# mini-ork

Task operating system for agents: classify → plan → execute → verify → reflect → improve.
Heterogeneous-model recipe runner with cost governance, runtime verification, and a GRPO learning loop.

This file is the canonical context map. Detail lives in `docs/`; procedural knowledge lives in recipe prompts under `recipes/<name>/prompts/`.

## Map

- **[docs/architecture](docs/architecture)** — system design and component diagrams
- **[Artifact graph contracts](docs/architecture/artifact-graph.md)** — declared ports, run-local manifests, transforms, and visibility limits
- **[Cloud sandbox providers](docs/architecture/cloud-sandbox-providers.md)** — node providers (static VMs, Daytona, …) behind one node-agent protocol; `--placement remote --env <profile>`
- **[Verification stack](docs/architecture/verification-stack.md)** — relations, differential, suite adequacy, level vector, gate hackability, equivalence operator (suite adequacy + level vector gate every code-fix run; the rest run under `mini-ork certify`, at promotion, or opt-in — see the doc's "where each runs" table)
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

`scripts/mini_ork_worktree.py` is the driver (`make worktree*` invokes it). It
takes an mkdir lock (`$MINI_ORK_WORKTREE_LOCK`, default
`$TMPDIR/mini-ork-worktree.lockdir`, 30 s wait) around the two check-then-act
pairs — the claim-check/register on create, and the fetch/rebase/push on merge
— so two concurrent agents cannot both pass an ownership check or interleave a
stale push. The fetch happens *inside* the critical section, so the rebase can
never target a ref cached before a minutes-long green gate. The green gate runs
outside the lock, so a long gate never blocks another worktree command, and the
push itself runs in a bounded retry (`MO_MERGE_PUSH_ATTEMPTS`, default 5).

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
- **Self-approve high-risk merges (operator rule, 2026-11-06).** A path under
  `config/risk-tiers.yaml` (`.github/workflows/**`, `.githooks/**`,
  `db/migrations/**`, secrets and vault modules, …) is refused at merge time
  with a patch id. Do not stop and ask the human: run
  `mini-ork approvals approve wt:<slug> --head <patch-id> --note "<why this
  change is safe and what it does>" --no-resume` yourself, then re-run
  `make worktree-merge`. The refusal prints the exact command; take the id from
  it, never guess. The review is still real — read `git diff`, name the change
  in the note — but the approval is yours to give. The same applies to node
  reviews the runtime parks: an in-run agent may never approve its own node, but
  the driving agent may approve a *completed* node review.

## Validation policy (ordered — do not start at the bottom)

Objective: fast delivery, with CI catching what is uncommon. Run the smallest
check that can catch the mistake you just made, and widen only as the change
gets closer to `main`.

**While editing** — the changed test file, by path, no name filter:

```bash
python3 -m pytest tests/unit/test_<area>_py.py -q
```

A bare `-k` / `-t` filter makes pytest collect the whole tree first (measured:
11 workers x ~900 MB, load average 124–138 for 8+ minutes on an 11-core box).

**When the change is done** — in this order, stopping at the first failure:

1. `make lint` — ruff blocking tier (F + E9). Seconds, and it catches broken
   code before anything expensive starts.
2. `mini-ork validate` — pre-run static checks (a kickoff needs numbered
   Done-When criteria).
3. `mini-ork fallbacks` — no silent `except: pass` / `return None` on error in
   the changed `.py` files.
4. The targeted pytest files for the area you touched.
5. `make test` — the full suite. Only before you merge; CI runs the same suite
   on 3.11 and 3.12, sharded, at no cost to you.

**While the PR is being reviewed** — do not re-run the full suite after a
formatting fix, and do not add a full local run "to be safe": the required
check is CI's `ci result`. Re-run locally only for a CI failure you cannot read
from the log.

**In-run agents** — a `code-fix` run's verifier nodes run the gate once, scoped
to the change surface (`MINI_ORK_TOUCHED_FILES`, else the run's dirty set since
its base ref). An implementer inside the run must not spend its budget
re-running the suite; it runs the changed test file, if anything.

What CI runs: `.github/workflows/ci.yml` — ruff (blocking + advisory), pytest on
3.11/3.12 sharded `unit-a`/`unit-b`/`rest`, the feature-registry ↔ skill check
(`check-skill`), the README/doc claim check, the verifiers-pin check, and the
web smoke suite; the `ci result` job is the single required status.
`.github/workflows/release.yml` re-runs the blocking lint plus a unit-layer
pytest smoke before building artifacts. `mini-ork garden` (drift detection) is
**not** in CI yet, so it is not a step here — run it deliberately when you
touch docs, recipes or config.

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
