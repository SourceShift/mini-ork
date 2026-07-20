# Remaining Dispatcher Migration — Sub-Handoff

Status: active after `main` commit `be2fa515` (2026-07-20).

This document covers only the dispatcher workstream. The master handoff for the
whole migration program is
`docs/migration/remaining-migration-full-handoff.md`.

## Objective

Retire the remaining Bash dispatcher fixtures and, only after all runtime,
test, integration, benchmark, and source-shape references are closed, retire
`lib/llm-dispatch.sh` itself.

Do not delete the dispatcher merely because a Python port exists. Each unit must
prove that the native implementation owns the behavior and that replacement
coverage exists.

## Current baseline

Remote `origin/main` is `be2fa515`.

Already merged native slices:

- `91861459` — native executable transcript writer and transcript tests.
- `5cd816ae` — native retry contract tests; Bash retry fixture retired.
- `24220991` — native secret-redaction tests; Bash redaction fixture retired.
- `ff74a68f` — native `llm_calls` ledger contract; Bash ledger fixture retired.
- `be2fa515` — native tool-grant behavior coverage.

The following remain intentionally live because their Bash contracts are not
fully retired:

- `lib/llm-dispatch.sh`
- `tests/test_provider_registry.sh`
- `tests/unit/test_providers_live.sh`
- `tests/unit/test_provider_wrappers.sh`
- `tests/perf/test_duration_capture.sh`
- the provider/cost portions of
  `tests/integration/test_dispatch_telemetry_gate.sh`
- execute-gate portions of
  `tests/integration/test_dispatch_telemetry_gate.sh`
- `tests/unit/test_run_artifacts.sh`
- any remaining direct dispatcher references found by the closure scan

## Non-negotiable safety rules

1. Work in a fresh worktree from current `origin/main`.
2. One fixture or one tightly coupled contract group per commit.
3. Preserve the Bash fixture until equivalent native coverage passes.
4. Do not modify user-owned checkout state under `/Volumes/docker-ssd/ps/mini-ork`.
5. Stage explicit migration files only. Never stage secrets, run databases,
   provider configs, generated reports, temporary homes, or unrelated cleanup.
6. Never print or commit credential values.
7. Use only Codex, Kimi, and GLM lanes. MiniMax, DeepSeek, Opus, and implicit
   fallback are forbidden for migration work.
8. Load credentials process-locally from the operator wrappers under
   `~/ps/scripts`; do not copy values into the repository or run artifacts.
9. A failed gate leaves `main` unchanged and preserves evidence in the isolated
   worktree.

## Remaining work queue

### 1. Finish tool-grant fixture retirement — DONE

`tests/unit/test_tool_grants.sh` is retired. Its unique coverage was ported
into native tests in `tests/unit/test_tool_grants_py.py`:

- real `recipes/code-fix/workflow.yaml` producer resolution
  (`test_real_workflow_producer_resolution`);
- undeclared implementer/planner/reviewer defaults
  (`test_undeclared_nodes_fall_through_to_type_defaults`);
- `mcp__<server>` rendering (existing `test_mcp_rendering_and_claude_argv`);
- subprocess argv contract for the canonical Python backend — a real
  `python3 -m mini_ork.dispatch` run against a stub `claude` asserting
  `--allowedTools`, `--strict-mcp-config`, `--mcp-config`, and that
  `--permission-mode bypassPermissions` survives the grant insertion
  (`test_python_dispatch_subprocess_folds_tool_grants`);
- the dead Context7 instruction check on `bin/_worker-launcher.sh`
  (`test_worker_launcher_context7_instruction_resolved`);
- the implementer-has-no-comms/web-MCP structural invariant
  (`test_implementer_profile_has_no_comms_or_web_mcp`);
- the `providers.py` grant-flag source contract
  (`test_providers_source_references_all_grant_flags`).

The only assertions deliberately dropped were the `grep -c ... lib/llm-dispatch.sh`
source-shape checks: those asserted content of the Bash file being retired and
are superseded by the `providers.py` grant-flag contract above. The Bash
argv path is not re-tested because Python is now the canonical dispatch owner.

Regression guard:

```bash
python3 -m pytest tests/unit/test_tool_grants_py.py -q -p no:cacheprovider
python3 -m pyright mini_ork/dispatch/providers.py
```

### 2. Convert provider registry and wrapper fixtures

Native registry resolution lives in `mini_ork.dispatch.providers`.

Remaining Bash dependencies are primarily in:

- `tests/test_provider_registry.sh`;
- `tests/test_provider_wrappers.sh`;
- `tests/test_providers_live.sh`;
- `tests/perf/test_duration_capture.sh`.

Separate deterministic registry behavior from live-provider probes. Convert
the deterministic cases first:

- executable registry entry resolution;
- OpenAI-compatible environment propagation;
- Anthropic-compatible environment/model propagation;
- wrapper precedence;
- missing-key and unknown-lane failures;
- duration sidecar behavior using a stub provider.

Live provider probes must remain opt-in and must use only approved Codex, Kimi,
or GLM credentials. Never use MiniMax or DeepSeek.

### 3. Split and retire the telemetry gate fixture

`tests/integration/test_dispatch_telemetry_gate.sh` currently combines four
contracts:

1. executable transcript sidecar merge and protocol stripping;
2. Codex usage harvesting and estimated cost sidecar;
3. native execute `needs_answers` gate;
4. execute-gate override behavior.

Transcript behavior is natively covered by:

- `mini_ork/dispatch/transcripts.py`;
- `tests/unit/test_dispatch_transcripts_py.py`.

The next agent should split the remaining cost and execute-gate checks into
standalone Python/integration tests before deleting the Bash gate file.

Cost checks must use a stub `codex` executable and injected rates. They must
prove:

- `turn.completed` usage is harvested;
- input/output token sidecars are correct;
- injected rates produce the expected cost;
- no network request is made.

Execute-gate checks must prove:

- `plan_status=needs_answers` exits with code `6`;
- `task_runs` becomes `failed|BLOCKED`;
- one `execute_blocked` event is emitted;
- `blocked.json` is written;
- `MINI_ORK_EXECUTE_GATE=0` bypasses the gate in dry-run mode.

### 4. Convert artifact and duration fixtures

Inspect:

- `tests/unit/test_run_artifacts.sh`;
- `tests/perf/test_duration_capture.sh`.

Use native `mini_ork.dispatch.telemetry.persist_artifact` and the existing
duration sidecar contract. Preserve path validation, hash/size registration,
and zero-duration failure behavior. Benchmark fixtures must remain provider-free
and deterministic.

### 5. Final dispatcher closure

After all fixture groups are migrated, run a repository-wide scan:

```bash
rg -n 'llm-dispatch\.sh|source .*llm-dispatch|\. .*llm-dispatch' \
  bin lib mini_ork recipes scripts tests config docs
```

Classify every surviving reference as one of:

- executable runtime edge — must be removed;
- test/fixture edge — must be migrated or explicitly retained as historical;
- documentation/history — may remain only when clearly historical.

Only then consider deleting `lib/llm-dispatch.sh` and any now-obsolete Bash
tests. Run the full closure and acceptance gates after deletion.

## Required validation stack per unit

Run in this order:

1. Focused native tests for the exact fixture.
2. Affected integration/E2E/security test.
3. Focused Pyright on changed Python modules.
4. `bin/mini-ork validate`.
5. `bin/mini-ork garden` (pre-existing operator-env warning is acceptable;
   new errors are not).
6. `git diff --check`.
7. Explicit diff review for secrets, generated state, and unrelated files.
8. Requirements audit against this handoff and
   `docs/migration/ported-module-ownership-recursive-plan.md`.

For final dispatcher retirement also run:

```bash
python3 -m pytest -q -p no:cacheprovider
python3 -m pyright mini_ork
bin/mini-ork validate
bin/mini-ork garden
git diff --check
```

The broad suite may contain known state-dependent skips and existing Starlette
deprecation warnings. New failures must be isolated and fixed before promotion.

## Merge and push protocol

For every passing unit:

```bash
git status --short
git diff --check
git add <explicit migration files only>
git commit -m '<conventional migration message>'

cd /Volumes/docker-ssd/ps/mini-ork-frc
git fetch origin main
git merge --ff-only <unit-branch>
git push origin main
test "$(git rev-parse main)" = "$(git rev-parse origin/main)"
git merge-base --is-ancestor <unit-commit> origin/main
```

If fast-forward merge is impossible, stop and reconcile from fresh
`origin/main`; do not force-push or reset shared history.

## Completion criteria

Migration is complete only when:

- every runtime caller uses native dispatch;
- all provider, retry, telemetry, artifact, cost, tool-grant, and execute-gate
  contracts have native coverage;
- no executable or test reference to `lib/llm-dispatch.sh` remains;
- the full validation stack passes;
- the final diff is OSS-ready and contains no credentials or local state;
- the retirement commit is merged into `main` and verified on `origin/main`.

If any requirement is unknown, classify it as deferred and document the exact
evidence gap in a new `docs/todos/<timestamp>-*.md` file rather than deleting
the Bash implementation.
