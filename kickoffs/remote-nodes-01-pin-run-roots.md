# remote-nodes-01 — Pin run roots once per run

Part of the remote-node-execution set (`kickoffs/remote-nodes-roadmap.md`). The
design is in `docs/architecture/remote-nodes.md`; this epic implements the
"pinned target" half of decision D4. It runs locally, needs no cloud, and also
fixes a latent local bug.

## Goal

Resolve the run's four roots **once** at run start and persist them, so every
node, verifier and git step reads the same values. The four roots are target
tree, run dir, MINI_ORK_HOME and engine root (`MINI_ORK_ROOT`). Today nothing
pins them.

## Why

- `_resolve_target_cwd` (`mini_ork/cli/execute.py:1393`) is called only by the
  implementer handler (`mini_ork/cli/execute_handlers.py:745`). The result is
  handed to later nodes through `publish_env({ENV_TARGET_CWD: target})`
  (`execute_handlers.py:751`), which mutates the process env.
- `_capture_pre_impl_baseline` (`execute.py:1482`) fires before the implementer
  has published anything. It falls back to `context_env("MO_TARGET_CWD") or
  os.getcwd()` (`execute.py:1505`), so the baseline can snapshot a different
  tree than the one the implementer edits. A guard catches part of this
  (`execute.py:1589-1596`).
- Children of the parallel pool never see the published env.
- The remote design maps fixed host roots to fixed sandbox paths (D4). That is
  impossible while the target root is decided lazily, per node, through env
  mutation.

## Requirements

1. New module `mini_ork/runtime/run_roots.py`:
   - A frozen dataclass `RunRoots(target: str, run_dir: str, home: str,
     engine: str)`.
   - `resolve_run_roots(run_dir, *, env=None) -> RunRoots`. Target resolution
     reuses the exact precedence of `_resolve_target_cwd`: explicit
     `MO_TARGET_CWD` git toplevel, then the kickoff's git toplevel, then the
     kickoff dir, then cwd. Move that logic here and leave
     `_resolve_target_cwd` as a thin wrapper, so behaviour is byte-identical.
   - `load_run_roots(run_dir) -> RunRoots | None` reads the persisted record.
2. Persist the record into `run_profile.json` under a `"roots"` key, at the
   first point in `execute.py` where `run_dir` and the profile exist. That must
   come before `_capture_pre_impl_baseline` can run. The write is idempotent:
   an existing `"roots"` is never overwritten, so a resumed run keeps its
   original roots.
3. Make every consumer read pinned roots first, then fall back to today's
   resolution if no record exists (legacy runs):
   - `_capture_pre_impl_baseline`
   - the implementer handler target
   - DAG verifier cwd (`execute.py:1260` `_run_verifier_ref` callers)
   - the review-diff capture (`execute.py:2012`)
   - publisher target (`mini_ork/cli/publisher.py:204-213`)
4. `resolve_run_drive_cwd` (`mini_ork/runtime/run_drive.py`) keeps its current
   role: it may redirect the *implementer cwd*. The pinned `target` stays the
   resolved git toplevel. Record the drive-redirected cwd separately in the
   profile as `roots.exec_cwd` only when the drive backend is set.
5. Keep `publish_env({ENV_TARGET_CWD: …})` for compatibility, but source it
   from the pinned record. Do not remove the env var: external recipes read it.

## Files in scope (touch ONLY these)

- `mini_ork/runtime/run_roots.py` (new)
- `mini_ork/cli/execute.py` (target resolution, baseline, verifier cwd, review-diff cwd)
- `mini_ork/cli/execute_handlers.py` (implementer target)
- `mini_ork/cli/publisher.py` (target read only)
- `tests/unit/test_run_roots.py` (new)

## Out of scope

- Path translation and any sandbox work (epic 03).
- Changing the default cwd semantics. This is a pin, not a behaviour change.

## Verification command

```bash
python3.11 -m pytest -q tests/unit/test_run_roots.py tests/unit -k "run_roots or baseline or publisher or target_cwd" && make lint
```

## Acceptance

- The baseline and the implementer resolve the **same** target even when the
  executor cwd differs from the target repo. Write a test that sets cwd to an
  unrelated git repo and the kickoff inside the target repo, and assert that
  `pre-implementer-ref` was captured from the target.
- Resuming a run reuses the persisted roots even if `MO_TARGET_CWD` changed in
  between.
- With no `"roots"` key (legacy run dir), behaviour is unchanged. Add a parity
  test against the old `_resolve_target_cwd` outputs for the four precedence
  branches.
- The full unit suite stays green.
