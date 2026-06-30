# mini-ork Fix Tracker — issues from session-history audit (2026-06-30)

Companion to `20260630-miniork-issues-from-session-history.md` (full evidence).
This file is the **trackable checklist**: status, owner-action, commit, and
what remains for each issue. Branch for landed work: `fix/i16-corruption-hardening`
(off `9a62818` / v0.6.0).

Status key: ✅ landed · 🟡 partial (more to do) · 🔵 open (not started) · ⚪ already-fixed (skip) · 🧪 needs-validation

## Landed this session (branch `fix/i16-corruption-hardening`)

| Issue | Status | Commit | Summary |
|---|---|---|---|
| I-16 HEAD-clobber + tag block | ✅ | `f10fd11` | Standing `lib/repo_integrity_guard.sh` (heals foreign-commit branch clobber outside the 60s post-commit window); pre-push tag-push → README-drift advisory (main still hard-blocks). Wired into `bin/mini-ork run`. 23-assert test. |
| I-16 follow-up (guard bug) | ✅ | `cad6f15` | Guard's LKG filename broke on slash branches (`fix/…`) → guard was inert on every feature branch. Sanitize `/`→`__` + regression test. 26 OK. |
| I-7 test-gate (subset) | ✅ | `47ec266` | `pyproject.toml` pytest `pythonpath=["."]`+`testpaths=["tests"]`. The code-fix `test.sh` was false-failing EVERY dispatch (`mini_ork` unimportable + fixture pollution) → rollback. Now 118 passed; gate emits `pass:true`. |
| I-5 verdict.json | ✅ | `e185064` | framework-edit verifiers now WRITE `verdict.json` (`_verdict_merge.sh` helper) instead of asserting a file nothing produced. Removed self-defeating `artifact-verdict-json-exists` gate. Unblocks framework-edit (was always fail-closed→rollback). 29 OK. |

## Architectural issues (bigger than single fixes)

| # | Issue | Why it matters | Direction |
|---|---|---|---|
| **A1** | **No agent filesystem isolation — agents run directly on the host OS filesystem.** There is no per-agent sandbox/jailed FS; workers `cd` into real repos on the local disk and read/write them in place. | (1) **Blocks cloud execution** — agents can't be shipped to a remote/ephemeral environment because they assume the local FS layout. (2) **Root cause of the corruption (M4)** — a cross-repo codex `exec` with cwd-confusion could reach and clobber THIS repo's `.git` precisely because there's no FS boundary between agents/repos. (3) No reproducibility/parallel-safety isolation beyond git worktrees. | Adopt a real per-agent sandbox: containerized (Docker/Podman), microVM (Firecracker), or a managed sandbox provider (Daytona, E2B, Modal, Fly Machines, gVisor). Give each agent an isolated rootfs + mounted scoped workspace, and a syscall/path boundary so an agent can only touch its own workspace. This also makes cloud/remote execution possible. **The OSS research below targets exactly this.** |

## Open / remaining (priority order)

| Issue | Sev | Status | Remaining work |
|---|---|---|---|
| I-1 lens silent-death | 🔴 | 🔵 | Add pre-synth quorum verifier to `research-synthesis` + other panel recipes (mirror `recipes/recursive-validate-impl/verifiers/tier4-panel-quorum.sh`); make dead-lane dispatch write a visible `*.WARN.md` artifact instead of an empty/absent file (`bin/mini-ork-execute` `_dispatch_node` researcher branch ~1976-1987). |
| I-3 throttle-aware retry | 🔴 | 🔵 | `lib/throttle-guard.sh` exists (correct 429/Fair-Usage classify + backoff ladder) but is only sourced by `mini-ork-self-improve`. Source it in `lib/llm-dispatch.sh` and wrap the single `mo_llm_dispatch` call with classify→backoff→bounded-retry/requeue; surface "paused, retrying at HH:MM". |
| I-2 foreign-home secrets | 🔴 | 🔵 | Add a recipe-start preflight: resolve configured lanes→providers→`api_key_env`, refuse-or-loudly-downgrade when a key/`secrets.local.sh` is missing (currently silent at `lib/llm-dispatch.sh:739/749/768/786`). |
| I-15 scheduler no filter | 🟡 | 🔵 | Add `--epic`/`--roadmap`/`--scope` flag to `bin/mini-ork-scheduler` (bare invocation drains every ready epic — this is what the researcher autopilot does). |
| I-10 verifier wrong-cwd | 🟡 | 🔵 | `_run_verifier_ref` cd's to worktree but falls back to `$PWD` silently when `worktree_path` missing; the no-`verifier_ref` path (`bin/mini-ork-verify`) doesn't cd at all. Add a hard cwd assertion at verify-node entry. |
| I-4 cost circuit global | 🔴 | 🔵 | `lib/llm-dispatch.sh:1186-1198` sums ALL `task_runs` cost vs `MO_DAILY_BUDGET_USD` → one expensive lane halts every dispatch. Make per-epic/per-recipe budget; let cheap lanes continue. |
| I-7 gate theater (rest) | 🔴 | 🟡 | Test-gate false-fail fixed (above). Still missing: an "imports wired / changed symbol is invoked / no dead code" check beyond `bash -n`/`py_compile`; web-smoke is skip-if-absent. |
| I-8 hollow/truncated planner | 🟡 | 🔵 | Recipe runs recover via `_d015_recipe_fallback_plan` (good), but a truncated planner is SWALLOWED into the deterministic fallback rather than surfaced — detect+warn on truncation distinct from invalid-JSON. |
| I-14 observability | 🔴 | 🟡 | v0.6.0 Phase-0 `llm_calls` telemetry exists but not wired live. Remaining: (a) attribute nested/SDK calls (shared-lane → run-wide fallback), (c) provider/cost from actual call ledger not node snapshot + fix hardcoded claude per-turn rates (`llm-dispatch.sh:1348-1350`), (d) single source of truth for status (trajectory shows two). |
| I-17 auto bug-collector | 🟢→🟡 | 🔵 | Pipeline exists but gated off: `MO_BUG_COLLECTOR` (default 0) + `MO_BUG_REPORT_AUTO_PROMOTE` (default 0). Decide defaults / wire an opt-in loop. |
| I-18 stray artifacts in consumer repo | 🔴 | 🔵 | Workers pinned to consumer cwd (`MO_TARGET_CWD`) with no write guard. Add a "write only under run dir" hint + post-run stray-file sweep. |
| I-19 live steering loop | 🟡 | 🔵 | Only DB-queue inject (between-node) works. `lib/mid_node_injector.sh` has no callers; SDK/STEER.jsonl path missing `lib/_worker-sdk-launcher.ts`; mcp-steering not wired into claude/codex invocations. Larger design effort. |

## Already fixed — no action (verified on clean main)

| Issue | Why skip |
|---|---|
| I-6 reviewer diff-direction | Code correct (`lib/pre_push_review.sh:370,383` = `base..HEAD`). Optional: add a regression test (no test covers it). |
| I-9 classifier overrides recipe | Fixed (`bin/mini-ork:142-156`, `mini-ork-classify` honors `--task-class`, skips keyword scan). |
| I-11 framework-edit stash orphan | No whole-tree stash in framework-edit; residual stashes have restore/guard. |
| I-12 coarse scope-revert | `contract.sh` scope-revert gone; replaced by `hooks/scope-enforce.sh` (pre-write deny) + `lib/branch-quarantine.sh` (preserved ref + manifest). Minor: hook is opt-in/fail-open. |
| I-13 rollback leaves changes | Framework-edit doesn't apply-to-tree. BUT see meta-finding M3 — code-fix rollback IS leaving changes; revisit. |

## Meta-findings discovered while fixing (NEW — add to backlog)

| # | Finding | Evidence | Fix direction |
|---|---|---|---|
| M1 | **code-fix publisher never commits** — `[warn] publisher: artifact_contract.yaml has no outputs[] — skipping publish`, so even reviewer-APPROVE work is left uncommitted then rolled back. Had to land I-16/I-5 by hand. | runs `…-10048`, `…-2009` | Give `recipes/code-fix/artifact_contract.yaml` an `outputs[]` (the changed files / a commit), or make the publisher commit the diff on APPROVE. |
| M2 | **code-fix `test.sh` runs the WHOLE repo suite** unscoped, so any unrelated/pre-existing failure rolls back a clean change. | I-16 run rollback | Scope the test gate to the change (changed-file-aware), or honor `MINI_ORK_TEST_CMD` per dispatch. |
| M3 | **rollback is incomplete** — after rollback, working tree still had modified files (I-13 live for code-fix). | runs `…-10048`, `…-2009` | Make `revert_branch`/rollback fully restore or clearly report leftover changes. |
| M4 | **Recurring repo corruption** — a cross-repo codex `exec` (cwd-confusion from the researcher `.mini-ork` autopilot) reset this repo's `main` onto `refs/codex/curated-sync` (3fdeeb4) + flipped `core.bare=true`, TWICE in ~40 min, mid-dispatch. Bleed ref regenerates after deletion. I-16 guard heals on startup but not mid-run. | this session | Stronger guard (watch during runs / file-lock on `.git/config core.bare`), and pin `GIT_DIR`/`-C` for dispatched codex so cwd-confusion can't touch a foreign repo. |
| M5 | **profile gate** asks 3 needs_answers questions even on a fully-specified kickoff (auto-answered, but adds a turn). | every run | Tune `lib/profile_gate.sh` confidence floor for well-specified kickoffs. |

## Recovery runbook (if the repo is corrupted again)
1. `git config core.bare false` (clobber flips it true → "must be run in a work tree").
2. `git update-ref refs/heads/main origin/main` (clobber repoints main to 3fdeeb4).
3. `git update-ref -d refs/codex/curated-sync` (delete bleed ref; it regenerates).
4. Your fix branch is unaffected (clobber targets `main`). Verify: `git log --oneline fix/i16-corruption-hardening`.
5. Root cause = the researcher `.mini-ork` autopilot; pausing its `mini-ork-scheduler` processes stops it. Restart info: `scratchpad/researcher-autopilot-restart.txt`.
