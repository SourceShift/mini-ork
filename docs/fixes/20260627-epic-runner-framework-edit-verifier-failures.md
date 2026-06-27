# Epic-Runner + framework-edit verifier failures in multi-epic AI skill delivery

**Date:** 2026-06-27  
**Origin:** `SourceShift/libwit` researcher repo, AI-SKILL-ORCH epic-runner dispatch (phases B–H)  
**Author:** kimi-autopilot  
**Status:** Fix-spec — most fixes already landed in `main`; the dispatcher spawn-override footgun still needs a guard.

## Summary

While delivering the AI-skill orchestration consolidation (`AI-SKILL-ORCH`) through the epic-runner recipe, the Wave 0 child framework-edit runs repeatedly failed verification for infrastructure reasons that had nothing to do with code quality. The child diffs were valid and the child rubric verifiers passed, but the workflow-level verifier nodes aborted the run.

This doc records the three failure modes, the fixes, and the one remaining footgun.

## Failure modes observed

| # | Failure | Symptom | Fix status |
|---|---|---|---|
| 1 | Verifier cwd is the untracked child workspace created by `mini-ork-spawn` | `fatal: current working directory is untracked` from `git archive HEAD` in `static-check.sh` / `test.sh` | **Fixed in main** — `_run_verifier_ref` honors `MO_TARGET_CWD` first |
| 2 | `framework-edit/verifiers/test.sh` unconditionally requires `tests/test_web_smoke.py` | `web-smoke-test-exists` fails on repos that never shipped a web smoke test | **Fixed in main** — skip web-smoke checks when the file is absent |
| 3 | `MINI_ORK_EPIC_SPAWN_BIN=/dev/null` is accepted but then executed | `PermissionError: [Errno 13] Permission denied: '/dev/null'` in `_spawn_command` | **Open** — dispatcher only checks `Path.exists()`; needs regular-file / executable guard |

## Fix details

### 1. Verifier cwd trapped in untracked child workspace

**Where:** `bin/mini-ork-execute`, function `_run_verifier_ref`  
**Root cause:** `_run_verifier_ref` reads `implementer-summary.json:worktree_path` and `cd`s there before running the verifier script. For epic-runner child runs, that path is the directory created by `mini-ork-spawn` (e.g. `.mini-ork/runs/<parent>/children/<child>/worktree`). That directory is inside the target git repo but is itself untracked, so `git -C <cwd> archive HEAD` fails.

**Fix in main (lines 89–109 of `bin/mini-ork-execute`):**

```bash
local _verify_cwd="${MO_TARGET_CWD:-}"
if [ -z "$_verify_cwd" ] || [ ! -d "$_verify_cwd" ]; then
  # ... read implementer-summary.json worktree_path ...
fi
if [ -z "$_verify_cwd" ] || [ ! -d "$_verify_cwd" ]; then
  _verify_cwd="$PWD"
fi
```

Operator can now export `MO_TARGET_CWD=/path/to/target/repo` when invoking epic-runner. The verifier runs in a real git root, `git archive HEAD` succeeds, and the diff is applied to a throwaway copy of the intended base.

**Usage for epic-runner:**

```bash
MO_TARGET_CWD=/Volumes/docker-ssd/Migration/Development/researcher \
  ./.mini-ork/bin/mini-ork run epic-runner kickoff.md
```

### 2. Missing `tests/test_web_smoke.py` aborts every framework-edit run

**Where:** `recipes/framework-edit/verifiers/test.sh`  
**Root cause:** The verifier had a hard check that `tests/test_web_smoke.py` must exist after applying the diff. Many repos (including the researcher repo at the time) do not ship this file, so every framework-edit run failed the test verifier even when no web changes were made.

**Fix in main (around lines 95–102):**

```bash
if [ -f "$WORKTREE/tests/test_web_smoke.py" ]; then
  _check "web-smoke-test-exists" "tests/test_web_smoke.py exists after patch" \
    '[ -f "$WORKTREE/tests/test_web_smoke.py" ]'
  _check "web-smoke-tests-pass" "pytest tests/test_web_smoke.py passes without network keys" \
    '...'
else
  _check "web-smoke-test-exists" "tests/test_web_smoke.py not present; skipping web smoke tests" 'true'
  _check "web-smoke-tests-pass" "no web smoke tests to run" 'true'
fi
```

This preserves the safety gate for repos that *do* ship web smoke tests while not fail-closing repos that don't.

### 3. Spawn-override footgun: any existing path is accepted as the spawn binary

**Where:** `recipes/epic-runner/lib/epic_dispatcher.py`, `_spawn_command`  
**Root cause:** The dispatcher checks `if spawn_bin.exists():` and then executes `spawn_bin`. Setting `MINI_ORK_EPIC_SPAWN_BIN=/dev/null` satisfies `exists()` but is not a real executable, so `subprocess.Popen` raises `PermissionError` and the whole dispatcher aborts before dispatching any child epic.

**Recommended fix:**

```python
import os
import stat

def _is_usable_spawn_bin(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        mode = path.stat().st_mode
        return stat.S_ISREG(mode) and os.access(path, os.X_OK)
    except OSError:
        return False
```

Then replace `if spawn_bin.exists():` with `if _is_usable_spawn_bin(spawn_bin):`. This rejects `/dev/null`, directories, and non-executable files.

**Workaround until the guard lands:** do not set `MINI_ORK_EPIC_SPAWN_BIN=/dev/null`. If you need to disable the spawn path, use a path that does not exist at all; the dispatcher will then fall back to the `mini-ork run framework-edit --smoke-shape` path.

## Additional project-local hardening (researcher repo)

Because the researcher checkout was running an older `.mini-ork` snapshot, a few extra local patches were applied during the rescue. These are worth upstreaming or converging with main:

- `recipes/framework-edit/verifiers/test.sh` and `static-check.sh` compute `REPO_ROOT` via `MINI_ORK_TARGET_REPO` or `git rev-parse --show-toplevel` so they tolerate being invoked from an untracked run directory.
- The same scripts symlink `node_modules`, `server/node_modules`, and `.pnpm-store` into the throwaway copy so `pnpm type-check` does not try to reinstall.
- `test.sh` writes a default `verdict.json` stub if the implementer/publisher did not create one, preventing a chicken-and-egg verifier failure.

## Recommendations

1. Keep the `MO_TARGET_CWD` change in main; document it in `docs/OPERATOR-STEERING.md` or the epic-runner README.
2. Apply the `_is_usable_spawn_bin` guard to close the `/dev/null` footgun.
3. Consider back-porting the `MINI_ORK_TARGET_REPO` / node_modules-symlink / verdict-stub helpers from the researcher rescue into the upstream framework-edit verifiers.
4. Ensure downstream repos (starting with `SourceShift/libwit`) update their bundled `.mini-ork` to a main cut that includes these fixes, so operators do not need to patch locally.

## Traceability

- Downstream run that exposed the issues: `run-1782569239-95532` (B–H, failed Wave 0 verifiers)
- Downstream rescue run with `MO_TARGET_CWD`: `run-1782574866-85840` (D–H)
- Recovery worktree: `/Volumes/docker-ssd/Migration/Development/worktrees/ai-skill-orch-recovery`
