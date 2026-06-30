# R2: bubblewrap sandbox backend (opt-in, degrade-never-fail)

## Context
Epic `docs/epics/EPIC-cloud-exec-runtime-sandbox.md`, phase R2. The exec seam exists
(`lib/runtime/contract.sh` → `mo_runtime_exec`, factory on `MO_RUNTIME_BACKEND`, default `local`)
and the executor routes through it (R0b). This phase adds the first REAL filesystem-isolation
backend: bubblewrap. Only the run's workspace is writable, so an agent cannot reach or write a
sibling repo's `.git` — this structurally prevents the cross-repo HEAD-clobber corruption.

Reference: `internal-docs/research/impl-analysis/01-runtime-sandbox-swerex-minisweagent.md`
(mini-swe-agent's `BubblewrapEnvironment` is the model).

## Hard delivery-safety constraints (epic "Delivery-safety constraints")
- **Opt-in only.** Default stays `local`. This backend runs ONLY when `MO_RUNTIME_BACKEND=bubblewrap`.
- **Degrade, never fail.** If `bwrap` is not on PATH or the platform is not Linux (e.g. macOS —
  the researcher host), `mo_runtime_exec` must fall back to the `local` backend with a one-line
  WARN and run normally. Never abort a run because bubblewrap is unavailable. (Mirror the
  fall-back-to-local pattern in `lib/sandbox/{modal,daytona}.sh`.)
- No new global state/lock; per-run workspace only; concurrency-safe.

## Deliverables
1. `lib/runtime/bubblewrap.sh` implementing the runtime contract (`mo_runtime_exec` at minimum;
   `mo_runtime_put/get` = plain cp into the workspace; `start/stop/alive` = no-ops). `exec` wraps
   the command like mini-swe-agent:
   ```
   bwrap --unshare-user-try \
     --ro-bind /usr /usr --ro-bind /bin /bin --ro-bind /lib /lib --ro-bind /lib64 /lib64 \
     --ro-bind /etc /etc --tmpfs /tmp --proc /proc --dev /dev --new-session \
     --bind "$WORKSPACE" "$WORKSPACE" --chdir "$WORKSPACE" \
     bash -c "$command"
   ```
   where `$WORKSPACE` is the cwd passed to `mo_runtime_exec`. Preserve exit code + stdout +
   process-group-kill-on-timeout semantics identical to the local backend. Skip `--ro-bind` for
   any of those host paths that don't exist (portability).
2. Availability check: a `bubblewrap_available()` helper (`command -v bwrap` + Linux check). The
   factory/backend must call it and **fall back to local with a WARN** when false — do NOT error.

## Smoke / DoD (must pass)
- `bash -n lib/runtime/bubblewrap.sh` clean.
- `tests/unit/test_runtime_bubblewrap.sh`:
  - If `bwrap` available (Linux CI): a command that writes inside `$WORKSPACE` succeeds, and a
    command that tries to write OUTSIDE `$WORKSPACE` (e.g. to a sibling tempdir) FAILS — proving
    the boundary. Same command under `MO_RUNTIME_BACKEND=local` succeeds (control).
  - If `bwrap` NOT available (e.g. macOS dev): the test asserts `mo_runtime_exec` with
    `MO_RUNTIME_BACKEND=bubblewrap` still runs the command (fell back to local) and emits the WARN.
    Use `_skip` for the isolation assertions on non-Linux, but the fall-back assertion must run.
- Existing `tests/unit/test_runtime_contract.sh` + `pytest` still green. Default behavior unchanged.

## Constraints (scope guard)
- Touch ONLY `lib/runtime/bubblewrap.sh` + the new test (and, if strictly needed, the factory in
  `lib/runtime/contract.sh` to register the `bubblewrap` backend name — minimal).
- Do NOT change the default backend, recipes, or `bin/mini-ork-execute`.
