# EPIC — Cloud-exec runtime abstraction + agent sandbox (A1)

> Closes architectural issue **A1** (`docs/audits/20260630-miniork-fix-tracker.md`): agents run
> directly on the host FS → blocks cloud execution + root-causes cross-repo git corruption.
> Grounded in `internal-docs/research/impl-analysis/` (swe-rex, mini-swe-agent, E2B, OpenHands).

**Goal:** introduce a backend-swappable execution seam so every recipe command runs through one
function, then add real filesystem isolation, then remote/cloud execution — without rewriting
recipes. Sequencing mirrors the convergent pattern found in the reference repos.

**Design invariants (from the reference impls):**
- One exec seam the executor/recipes call (`mo_runtime_exec`), never inline `cd`+`bash`.
- Actions are **stateless** (subprocess-per-action) so the backend is a config swap.
- Backends are interchangeable behind the seam, selected by `MO_RUNTIME_BACKEND`.
- Workspace is an explicit per-run dir that becomes the only writable path under isolation.
- Process-group kill on timeout (no orphaned children).

Note: `lib/sandbox/{modal,daytona}.sh` already exist as STUBS that fall back to local — this epic
makes the seam real.

---

## Phase R0a — the exec seam + local backend (FIRST PIECE, additive, no behavior change)
**Deliverables**
- `lib/runtime/contract.sh` — defines + dispatches the runtime interface:
  `mo_runtime_exec "<command>" "<cwd>" [timeout_s]` → prints stdout, returns the command's exit
  code; `mo_runtime_put <local> <dest>`, `mo_runtime_get <src> <local>`; `mo_runtime_start` /
  `mo_runtime_stop` / `mo_runtime_alive` (no-ops for local). A factory sources the backend named
  by `MO_RUNTIME_BACKEND` (default `local`).
- `lib/runtime/local.sh` — local backend implementing the contract, wrapping current behavior:
  `subprocess`-equivalent via `setsid bash -c`, capturing stdout, and **killing the process
  group on timeout** (port mini-swe-agent's `start_new_session`+`killpg`).
- `tests/unit/test_runtime_contract.sh` — asserts: exec returns correct rc + stdout for success
  and failure; cwd is honored; timeout kills the whole process group (spawn a child, assert it's
  gone); backend factory selects `local` by default and errors clearly on unknown backend.

**DoD / smoke**
- `bash -n` clean; `bash tests/unit/test_runtime_contract.sh` green.
- Purely additive — nothing in `bin/` calls it yet, so existing tests still pass (`pytest` green).

**Scope guard:** touch ONLY `lib/runtime/*` + the new test. Do not modify `bin/mini-ork-execute`
in this phase.

## Phase R0b — route the executor through the seam (behavior-preserving refactor)
- Replace the direct `cd`+`bash` / `_run_verifier_ref` subshell call sites in
  `bin/mini-ork-execute` (and the verifier path) with `mo_runtime_exec`.
- Default backend `local` ⇒ byte-for-byte same behavior; the seam is now load-bearing.
- DoD: existing recipe smoke + `pytest` unchanged; a sample `code-fix` run completes as before.

## Phase R2 — bubblewrap backend (cheap FS isolation; stops the clobber)
- `lib/runtime/bubblewrap.sh` implementing the contract via
  `bwrap --unshare-user-try --ro-bind /usr /usr … --tmpfs /tmp --proc /proc --dev /dev
  --new-session --bind "$WORKSPACE" "$WORKSPACE" --chdir "$WORKSPACE" bash -c "$cmd"`.
  Only the run's workspace is writable ⇒ an agent cannot touch a sibling repo's `.git`.
- `MO_RUNTIME_BACKEND=bubblewrap`; auto-skip on non-Linux (macOS dev stays `local`).
- DoD: a test proving a write outside `$WORKSPACE` fails under bubblewrap but succeeds under local.

## Phase R3 — docker, then managed (cloud)
- `lib/runtime/docker.sh` (`docker exec` into a per-run container; workspace mounted) +
  `mo_runtime_put/get` real impls.
- Managed: a mini-ork agent-server inside the sandbox reached over HTTP + workspace archive
  (OpenHands/swe-rex pattern), OR a thin bridge to E2B/Daytona for untrusted/cloud runs.
- DoD: a run completes end-to-end against `MO_RUNTIME_BACKEND=docker`; cloud backend smoke.

---

## Dispatch plan
Each phase is one scoped `code-fix` kickoff (single deliverable, per the 2+-file dispatch rule).
**Start with R0a** — additive, low-risk, validates the now-fixed dispatch vehicle (I-5 verdict.json
+ I-7 test-gate). Verify each phase before the next; keep `local` the default until R2 lands.
