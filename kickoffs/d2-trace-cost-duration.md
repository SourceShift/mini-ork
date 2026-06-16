# Framework Edit: trace_write_node helper + trace_write_or_log wrapper

## Goal

Fix two related defects in `lib/trace_store.sh` so that every `execution_traces`
row carries real `cost_usd` and `duration_ms`, and so that schema drift in any
of the 26 `trace_write … 2>/dev/null || true` call-sites is no longer silent.

Source of truth: `.mini-ork/runs/self-improve-iter-34-20260609115529/synthesis.md`
ranked patches **#2 (`trace_write_node` helper + plan/classify swap)** and
**#3 (`trace_write_or_log` wrapper)**.

Today 87/87 `recursive_self_improve` rows carry `duration_ms=0` and
`cost_usd=0`. Every gradient ranked on cost evidence before this ships
is therefore unreliable.

## Scope Hint

- `lib/trace_store.sh` (both patches land here)
- `bin/mini-ork-plan` (9 inline call-sites swap to `trace_write_node`)
- `bin/mini-ork-classify` (2 inline call-sites swap to `trace_write_node`)
- 26 callers across `bin/mini-ork-{plan,classify,execute,verify,promote}` and
  `lib/circuit_breaker.sh` (Patch #3 sweep)

## Expected Edit

**Patch #2 — `trace_write_node` in `lib/trace_store.sh`:**

1. Hoist `_trace_write_node_rich` from `bin/mini-ork-execute:301-365` into
   `lib/trace_store.sh` as the public function `trace_write_node`.
2. Function reads cost + duration from sidecars at
   `${MINI_ORK_RUN_DIR}/.last-llm-cost` and `.last-llm-duration-ms`.
3. Freshness window guard: ignore sidecar if older than
   `5 * MO_DISPATCH_TIMEOUT` (default ~7500s).
4. Swap 9 inline `trace_write "{...}"` call-sites in `bin/mini-ork-plan`
   (lines 108, 302, 316, 492, 500, 507, 515, 522, 572) to
   `trace_write_node planner ...`.
5. Swap 2 inline call-sites in `bin/mini-ork-classify` (lines 113, 304).

**Patch #3 — `trace_write_or_log` wrapper in `lib/trace_store.sh`:**

1. Add wrapper that routes stderr to
   `${MINI_ORK_RUN_DIR}/trace-write-errors.log` while preserving caller
   exit code 0.
2. Rotation knob: `MO_TRACE_ERR_LOG_MAX_BYTES` (default `1048576`).
3. Mechanically sweep the 26 `trace_write … 2>/dev/null || true` call-sites
   across `bin/mini-ork-{plan,classify,execute,verify,promote}` and
   `lib/circuit_breaker.sh:441,465` to use `trace_write_or_log` instead.

## Requirements

- Both patches MUST be idempotent — re-running the sweep on
  already-converted call-sites must be a no-op.
- The new helpers must source-clean: no `unbound variable` errors under
  `set -u`.
- Add unit test `tests/unit/test_trace_write_node.sh`:
  - Seeds `.last-llm-cost` and `.last-llm-duration-ms` files.
  - Invokes `trace_write_node planner success ...`.
  - Asserts the resulting `execution_traces` row has `cost_usd > 0` AND
    `duration_ms > 1000`.
- Add unit test `tests/unit/test_trace_write_or_log.sh`:
  - Forces an invalid schema column.
  - Asserts trace-write-errors.log gets a line.
  - Asserts the caller's `$?` is 0.
- Do NOT modify `.mini-ork/config/**`.
- Do NOT auto-rotate the error log in this patch — knob only.

## Done When

- `${MINI_ORK_RUN_DIR}/framework-edit.diff` contains the proposed patch
  covering `lib/trace_store.sh`, `bin/mini-ork-plan`, `bin/mini-ork-classify`,
  the 26 swept call-sites, and two new unit tests.
- `${MINI_ORK_RUN_DIR}/verdict.json` contains:
  `{ "tests_pass": true, "static_pass": true, "pass": true }`
- Static (shellcheck) passes.
- Both new tests pass.

## Verification commands

- `shellcheck lib/trace_store.sh bin/mini-ork-plan bin/mini-ork-classify`
- `bash tests/unit/test_trace_write_node.sh`
- `bash tests/unit/test_trace_write_or_log.sh`

## Out of Scope

- Patch #1 (llm_calls producer) — separate concern, defer.
- Patch #4/#5 — covered by D1.
- task_memory / failure_memory writers — covered by D3.
