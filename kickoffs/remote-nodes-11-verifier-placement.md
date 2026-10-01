# remote-nodes-11 — Verifier and verify placement

Part of `kickoffs/remote-nodes-roadmap.md`. Design: `docs/architecture/remote-nodes.md`
**D5** (verifiers run remotely; gate *decisions* stay local). Depends on
epic 07 (tree replica) and epic 08 (run-dir mirror, which carries evidence
files back).

## Goal

Under remote placement, every command that **checks** the code runs on the
remote host, against the replica, with the remote toolchain:
- DAG verifier scripts
- post-run `success_verifiers`
- command-fallback verifiers
- step_rules git checks
- the mutation campaign

The control plane still reads the evidence and makes the pass/fail decision
locally. Verifiers never write the truth: no tree sync-down after them, and
the replica is restored afterwards.

## Why (traced 2026-10-01)

- `_run_verifier_ref` (`mini_ork/cli/execute.py:1260-1286`) runs the verifier
  script via `subprocess.run(…, cwd=MO_TARGET_CWD)`. It is explicitly "minus
  the mo_runtime_exec seam". code-fix verifiers then shell out to the
  project's tests and typecheck in that cwd (`recipes/code-fix/verifiers/test.py:218`,
  `typecheck.py:144`) and create `git worktree`s on the target (`test.py:268-317`).
  On the laptop, those need the project's toolchain, which defeats the purpose
  of a cloud node.
- Post-run verify (`mini_ork/cli/verify.py:324-345`) runs `success_verifiers`
  with **no `cwd=`**, so it uses whatever cwd `mini-ork run` was launched
  from. That is a latent local bug, and remotely it is undefined.
- The mutation campaign mutates the target in place (`cli/verify.py:227-244`,
  `gates/mutation_adversary.py:544-577`). `gates/step_rules.py:69-83,306` runs
  git on the workspace.

## Requirements

1. **One routing helper**, `run_check(argv_or_cmd, *, cwd, env, evidence_path,
   timeout)`, in `mini_ork/runtime/contract.py`, extending the existing
   `mo_runtime_exec` module rather than adding a new one.
   - With no run session, or `local` placement, it is a byte-identical
     `subprocess.run`.
   - Under remote placement, it uses `get_run_session(run_id, "remote").exec`
     (epic 02). The cwd, argv and env go through the run's `PathMap` (epic 03),
     so `cwd=/workspace/target`, scripts resolve under `/opt/mini-ork/...`, and
     `MINI_ORK_RUN_DIR=/workspace/run`.
   - The evidence is written remotely into `/workspace/run`. It arrives
     locally through the run-dir pull (epic 08). If the pull is missing it,
     the merged output returned by `exec` is written to `evidence_path` as a
     fallback.
2. Route these call sites through `run_check`:
   - `_run_verifier_ref` (`execute.py:1260`)
   - both branches of post-run verify (`verify.py:324` command fallback,
     `verify.py:336` script)
   - the mutation campaign command execution
   - step_rules git invocations

   The decision code (JSON `pass` parsing, the vacuous-pass rule, gate
   arithmetic) is untouched and stays local.
3. **Post-run verify cwd fix:** pass `cwd=<pinned roots.target>` (epic 01) on
   both branches. A legacy run with no pinned roots keeps today's behaviour.
   Call this out in the PR as an intentional fix.
4. **Replica hygiene after a check.** After every remote `exec` from
   `run_check`, restore the replica's non-ignored paths to the session's
   `last_synced` snapshot: restore from the snapshot tree, then `git clean
   -fd` without `-x`, so ignored caches like `.venv`, `node_modules` and
   `.pytest_cache` survive for speed. Verifier droppings and mutation residue
   can then never be mistaken for agent edits on the next sync-down. Add a
   `restore_replica()` verb to `RemoteWorkspace` that calls a node-agent tree
   endpoint (`POST …/tree/restore`).
5. Events: `remote.check` `{kind: verifier|verify|mutation|step_rule, name,
   rc, ms}`.

## Files in scope (touch ONLY these)

- `mini_ork/runtime/contract.py` (`run_check`)
- `mini_ork/cli/execute.py` (`_run_verifier_ref` only)
- `mini_ork/cli/verify.py` (exec sites + cwd fix)
- `mini_ork/gates/step_rules.py`, `mini_ork/gates/mutation_adversary.py` (exec sites only)
- `mini_ork/runtime/backends/remote.py` + `mini_ork/remote/node_agent/sessions.py` (`restore_replica`)
- `tests/unit/test_run_check_placement.py` (new)

## Out of scope

- Changing verifier semantics, the gate arithmetic or verdict merging.
- Per-project toolchain installation (epic 12 setup scripts).

## Verification command

```bash
python3.11 -m pytest -q tests/unit/test_run_check_placement.py tests/unit -k "verifier or verify or step_rules or mutation" && make lint
```

## Acceptance

All remote cases run against the in-process node-agent (`--runtime host`).
- With placement unset, `_run_verifier_ref` and post-run verify produce
  byte-identical evidence and rc to before (parity tests on a fixture
  verifier).
- Under remote placement:
  - A fixture verifier asserting `os.getcwd() == "/workspace/target"` (or the
    host-runtime equivalent root) passes.
  - Its `{"pass": true}` evidence lands locally at `evidence_path`, and the
    local decision returns 0.
- A verifier that writes `junk.txt` into the target leaves no `junk.txt`
  locally after the next agent spawn's sync-down, because the replica was
  restored. An ignored `.pytest_cache/` survives the restore.
- Post-run verify with roots pinned runs in the target even when launched from
  another cwd.

## Review bar (added after epics 02–04 were reviewed)

Each earlier epic passed its gate and still failed review for the same reason:
the acceptance tests that drive the REAL seam were never written, so unwired or
broken code looked green. This epic is rejected unless:

- every Acceptance bullet above has its own test, and that test exercises the
  production path (the executor / dispatch / CLI entrypoint), not only the new
  module in isolation;
- every new public function or class has at least one production caller
  (`git grep` for it outside its own module and tests);
- nothing is gated only by a skip: a daemon- or network-gated test must run in
  this environment (Docker is available via colima; only `/Volumes/docker-ssd`
  is bind-visible), or the epic says why it cannot;
- the default path (feature env unset) is proven byte-identical by a test.
