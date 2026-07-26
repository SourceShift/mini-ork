# Plan — Bash removal: complete the Python cutover

*2026-07-26 · status: proposed · supersedes the strangler-fig interim state*

## 0. Reality check (why this is a cutover, not a deletion)

`MINI_ORK_RUNTIME=python` is the default and the Python runtime is live, but
bash is still **load-bearing in the default production path**:

1. **Trampoline**: `mini-ork <sub>` → Python `cli/main.py` →
   `_bash_entrypoint_handler` → `bin/mini-ork-*` (bash) → `runtime-select.sh`
   → `exec python3 -m …`. Bash is a live intermediary for 21 subcommands.
2. **4 subcommands have no Python port at all**: `apply`, `garden`,
   `validate`, `recipe-eval` — they execute real bash today.
3. **22 production Python→bash subprocess sites** (the blockers, §3 of the
   inventory): `db/init.sh` migrations, `lib/trace_store.sh` writes,
   5 gate-condition scripts, `lib/cw_por.sh`, `lib/gate_registry.sh` shell
   calls, `finalize.py`/`healer_bridge.py`/`benchmark_suite.py` bash-lib
   calls, 7 provider `cl_*.sh` lane wrappers, `lib/runtime/contract.sh`.
4. **56 `recipes/*/verifiers/*.sh`** — a user-facing recipe contract whose
   runner is `["bash", script]`.
5. **~90 Python parity tests** shell out to their bash twin; **82 `.sh`
   tests** + `tests/run-all.sh`/`smoke.sh` pin the bash side; CI has
   shellcheck + bash-tests jobs; Makefile/.githooks/scripts are bash.

Deleting bash before eliminating 1–4 breaks production. The phases below are
therefore strictly ordered: **replace → cut over → delete → convert tests →
strip tooling**.

Foundations already in place (this year's refactors): every bash lib has a
parity-verified Python port staged (`docs/migration/parity-ports.md`);
subcommand/node/gate/policy/provider **registries** make cutovers
configuration, not surgery; `stores/migrate.py` is a staged Python migration
runner; the web/obs surface is fully Python.

## Phase 0 — Freeze and guardrails (0.5 day)

- Announce: no new `.sh` anywhere; new recipes write Python verifiers.
- Add a CI guard: fail if the count of `lib/*.sh` files *increases*
  (ratchet the inventory: 73 lib + 6 gates + 82 test .sh today).
- Land this plan; tag the repo (`pre-bash-removal`) for rollback reference.

**Exit:** guardrail CI green.

## Phase 1 — Eliminate production bash dependencies (ordered by risk, smallest first)

Each workstream ships independently with its own green gate. Rule: the
Python replacement must be the *staged parity port* where one exists — the
parity tests already prove equivalence.

### WS1 — Subcommand cutover (removes the trampoline)
- Port the 4 bash-only subcommands to `mini_ork/cli/{apply,garden,validate,recipe_eval}.py`
  (their bash bodies: `bin/mini-ork-apply` + `lib/apply.sh`,
  `bin/mini-ork-garden`, `bin/mini-ork-validate`, `bin/mini-ork-recipe-eval`).
- Register all 21 `_EXEC_SUBS` as native module handlers in
  `SUBCOMMAND_REGISTRY`; delete `_bash_entrypoint_handler` and `_EXEC_SUBS`.
- **Exit:** `mini-ork <sub> --help` for all 21 works with `bin/mini-ork-*`
  renamed away (test proves no trampoline).

### WS2 — Python migration runner
- Cut `cli/init.py` + `cli/update.py` from `bash db/init.sh` to
  `mini_ork.stores.migrate` (staged); port the sqlite-grep one-liner in
  `update.py:125`.
- **Exit:** `mini-ork init` on a fresh HOME produces the 111-table db with no
  `db/init.sh` present; `test_migrate_py.py`, `test_mini_ork_update_py.py`
  green after conversion (Phase 3).

### WS3 — trace_store writes
- `cli/invoke_prompt.py` + `cli/reflect.py` write via
  `mini_ork.trace_store` (staged port) instead of `bash -c '… trace_store.sh'`.
- **Exit:** no `trace_store.sh` references in production code.

### WS4 — Gates: native evaluators for the 5 gate scripts
- The M5 `GATE_EVALUATORS` registry makes this registration, not surgery:
  port `gates/{coalition,liveness,panel-health,stability,synthesis-promote}.sh`
  semantics into evaluators over the staged ports (`coalition_gate.py`,
  `circuit_breaker.py`, `cw_por.py`, `adaptive_stability.py`,
  `promotion_gate.py`) and re-register the bootstrap conditions to native
  evaluators instead of script paths.
- `gates/artifact_contract.py:192` and `promotion_gate.py:370` switch from
  `bash -c 'source lib/{gate_registry,cw_por}.sh'` to the Python ports.
- **Exit:** `gate_run_all` passes with `gates/*.sh` absent; oracle-gate
  integration tests green.

### WS5 — Recovery + learning bash-lib calls
- `recovery/finalize.py` (cache/finalize/auto-merge/pr-create),
  `recovery/healer_bridge.py` (cleaner/healer),
  `learning/benchmark_suite.py` (utility_function) — call the staged Python
  ports directly.
- **Exit:** those modules import no `bash`; recovery + learning suites green.

### WS6 — Provider lane wrappers (`cl_*.sh`, 7 lanes) — HARDEST
- The wrappers carry real contracts: argv assembly, credential injection,
  codex's JSONL→sidecar telemetry protocol, glm/minimax gateway quirks.
- Path: for each lane, express the wrapper's contract in
  `config/providers.yaml` (kind registry from M6 supports this) and delete
  the wrapper. `cl_codex.sh` needs a native codex transport (the
  `_dispatch_codex_via_wrapper` backend already exists — extend it to own
  the sidecar protocol).
- **Exit:** live dispatch smoke on all 7 lanes with `lib/providers/` absent
  (run in the mini-ork env, real creds; gate: `test_providers_live.sh`
  replacement).

### WS7 — Sandbox runtime contract
- `agent/minimal.py` shells to `lib/runtime/contract.sh`
  (+bubblewrap/docker/local backends). Either port the contract to
  `mini_ork/runtime/` or retire the minimal scaffold (`MO_SCAFFOLD_TIER`
  defaults to `harness`; the minimal tier is opt-in).
- **Exit:** decision recorded; scaffold tier green either way.

### WS8 — Recipe verifier contract
- Verifiers are user-facing recipe content; the runner executes
  `["bash", script]` (`cli/verify.py:183,194`, `cli/execute.py:1119`).
- Change the runner to dispatch by extension: `.py` →
  `[sys.executable, script]`, `.sh` → bash (legacy, deprecated warning).
- Port the 56 built-in `recipes/*/verifiers/*.sh` to `.py` (mechanical —
  most are small: typecheck/test/lint wrappers).
- **Exit:** all built-in recipes verify with no `.sh` under `recipes/`;
  external `.sh` verifiers still run (compat) but warn.

## Phase 2 — Delete the bash runtime (1 day, after WS1–WS8)

- Delete `bin/mini-ork-*` bash bodies → replace each with a thin
  `exec python3 -m <module>` shim (or fold into `bin/mini-ork <sub>` only
  and drop the suffixed wrappers — decide; keeping thin shims preserves
  muscle memory).
- Delete `lib/runtime-select.sh`, `lib/paths.sh`, all 73 `lib/*.sh`,
  6 `gates/*.sh`, `bin/_worker-launcher.sh`, `bin/mo-check-claude-invocations`
  (Python port exists).
- Remove `MINI_ORK_RUNTIME` handling and the `doctor` lib-presence checks.
- **Exit:** full pytest green with zero `.sh` in `bin/ lib/ gates/`.

## Phase 3 — Test migration (2–3 days)

- Convert ~90 parity `*_py.py` tests to plain unit tests: drop the
  bash-subprocess half, keep the Python assertions (most already have
  python-only cases; the parity layer is partially decoupled — 17 stale
  pins already pass with twins absent).
- Delete the 82 `.sh` tests, `tests/run-all.sh`, `tests/smoke.sh`,
  `tests/lib/setup_state_db.sh` (replace with a Python conftest fixture).
- **Exit:** pytest suite count stabilizes ≈ python-only; no test spawns
  `bash` except recipe-verifier compat tests (WS8).

## Phase 4 — Tooling, CI, docs (1–2 days)

- **CI**: delete `shellcheck` + `bash-tests` jobs; delete
  `mo-check-claude-invocations` advisory step (port is Python — wire it as
  a pytest or a ruff-style check); keep `readme-claim-check` (port to
  Python or keep as the one sanctioned script).
- **Makefile**: retarget `serve/dev/test-obs/worktree*` to Python entry
  points (port `scripts/mini-ork-worktree.sh` — it is the worktree-first
  dev loop, high value).
- **.githooks**: port `pre-push`/`post-commit`/`reference-transaction` to
  Python (they're repo-critical: worktree guard + README drift).
- **scripts/**: port or delete the 16 (most are one-off smokes → delete;
  `mini-ork-worktree.sh` + `readme-*.sh` → port).
- **hooks/*.sh** (Claude Code integration): port to Python entry points or
  document as the one external bash surface (decision; they are
  Claude-side glue, not mini-ork runtime).
- **Docs**: rewrite `AGENTS.md` (drop `MINI_ORK_RUNTIME=bash`,
  `lib/paths.sh`, shellcheck references), `docs/operator/*`, and sweep the
  106 files referencing `.sh` paths; update `.github/CODEOWNERS`.
- **Exit:** `grep -r '\.sh' --include='*.py' mini_ork/ | grep -v test` →
  empty; CI green with 2 jobs removed.

## Phase 5 — Verification & rollout

- Full suite + live smoke (real lanes) in the mini-ork env.
- Bump minor version; changelog entry: "bash runtime removed".
- Keep `pre-bash-removal` tag + one release of the thin-shim bins before
  dropping suffixed wrappers entirely (if that option is chosen).

## Risk register

| Risk | Mitigation |
|---|---|
| Provider wrappers carry undocumented lane quirks (WS6) | Lane-by-lane live smoke before deletion; keep wrappers one release behind a feature flag |
| Parity tests hide behavior only the bash half asserted | Conversion reviews each parity test for bash-only assertions before dropping |
| Recipe verifiers are a public contract (WS8) | Extension-based runner keeps `.sh` working with a deprecation warning for ≥1 release |
| 40+ tests pin `db/init.sh` | WS2 lands the Python runner first; tests migrate in Phase 3 with a conftest fixture |
| Hidden bash callers in consumer repos | `mini-ork doctor` gains a "bash references" check pre-removal; release notes call out |

## Effort estimate

WS1 2d · WS2 0.5d · WS3 0.5d · WS4 1d · WS5 1d · WS6 3d · WS7 0.5d ·
WS8 2d · Phase 2 1d · Phase 3 2–3d · Phase 4 1–2d · Phase 5 1d ≈ **3–4
weeks** of focused work, independently shippable per workstream.

## Inventory reference

Full file:line inventory (bin modes, per-lib P/B/T/X classification, 22
blocker sites, test pins, CI hooks): this plan's source audit,
`docs/audits/2026-07-26-bash-surface-inventory.md`.
