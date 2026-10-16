# Session gap audit — `pre-run-lane-health-gate`

- **Session id:** `3e207e26-3029-4418-bf4d-938cbfc7dcff`
- **Session name:** `pre-run-lane-health-gate` (ref `9621ec`)
- **Audited:** 2026-10-16, against engine HEAD `9ee820c1` in `/Volumes/docker-ssd/ps/mini-ork`
- **Method:** every recorded status re-checked against git history, the working
  tree, and a live test run. A plan or a merge message was not accepted as
  evidence; only landed commits, existing files, and passing tests were.

## (a) Task table

| id | description | recorded | VERIFIED | evidence |
|---|---|---|---|---|
| `ci-green-four-jobs` | Get the four failing CI jobs on `main` green | completed | **completed (at the verified head only)** | `gh run view 38565292891` → head `38750445aa9f227500c7d5977b33ed62dd83a0b2`, conclusion `success`, all 9 jobs success (lint, readme claim check, web smoke, `3.11/3.12` × `unit-a/unit-b/rest`) |
| `lane-health-gate` | Pre-run lane-health gate: implement, merge, live-smoke | completed | **completed** | commits `b3c80bb5` + `19e74c72`, both `git merge-base --is-ancestor … HEAD` ✓; files present; wiring at `mini_ork/cli/execute.py:1214-1226`, `mini_ork/cli/main.py:1712-1729`; `python3.11 -m pytest -q -p no:cacheprovider tests/unit/test_lane_preflight_gate_py.py tests/unit/test_lane_native_probe_py.py` → **32 passed in 2.58s** |
| `primary-only-cwd-test` | `test_dispatch_model_fails_fast_on_framework_cwd` fails only in the primary checkout | pending | **still open (confirmed)** | `python3.11 -m pytest -q tests/test_dispatch_cwd_py.py` in the primary → **1 failed, 4 passed**; `assert res.rc == 2` got `127`, `error="spawn failed: [Errno 2] No such file or directory: '/private/var/fold…'"` at `tests/test_dispatch_cwd_py.py:83` |

### Evidence detail — `lane-health-gate`

Commands actually run, and their output:

```
$ git log --oneline -S"lane_preflight" --all
b3c80bb5 gate: refuse dispatch against a dead lane before the run starts
1172a9fd fix(lanes): probe lane liveness at configuration time, …   <- a LATER, other session
$ git merge-base --is-ancestor b3c80bb5 HEAD   # ✓ landed
$ git merge-base --is-ancestor 19e74c72 HEAD   # ✓ landed
$ git show --stat 19e74c72
 mini_ork/dispatch/providers.py          | 10 ++++++-
 tests/unit/test_lane_native_probe_py.py | 50 +++++++++++++++++++++++++++++++++
```

Same `git log -S` probe resolves `resolve_dispatch_lanes`, `_live_probe_native`,
`MO_LANE_HEALTH_GATE` and `lane_preflight_blocked` to `b3c80bb5` — the symbols are
in the landed commit, not only in the worktree.

Artifacts on disk (primary checkout):

| path | size |
|---|---|
| `mini_ork/cli/lane_preflight.py` | 20.2K |
| `tests/unit/test_lane_preflight_gate_py.py` | 18.9K |
| `tests/unit/test_lane_native_probe_py.py` | 9.3K |

Wiring and docs:

- `mini_ork/cli/execute.py:1214-1226` — `lane_preflight.enforce(selected, …)` then
  `print("blocked_reason=…")`, `print("verdict=blocked")`, `return RC_BLOCKED`,
  sitting after the recovery filter and dry-run early return and **before**
  `set_status(db, run_id, "executing")`. `main.py:1992-1993`
  (`mini_ork_execute.main(rest, root=root)`) is the in-process entry the comment
  claims is the single chokepoint for `run`/`resume`/`recover`.
- `docs/operator/env-vars.md:43,71,87`; `docs/operator/feature-flags.md:14` —
  `MO_LANE_HEALTH_GATE` documented with its degradation modes.

Live smoke (this session, on the real registry with real keys and real HTTP):
`MO_LANE_PROBE=1 python3.11 /tmp/lhg-smoke1.py` → `SMOKE1 PASS`,
`failures=1 lanes=['deepseek'] nodes=['deep']` (a junk `DEEPSEEK_API_KEY` flagged,
`sonnet`/`opus`/`glm` clean); and end-to-end `rc=75`,
`lane_preflight` JSON `verdict=blocked`/`blocked_reason=lane_unhealthy`,
`task_runs` `failed`/`ESCALATE`, `run_events=['lane_preflight_blocked']`, 12-node
DAG with **0** nodes dispatched and no `node-*` directories.

## (b) Missing features & gaps

1. **Gate is a no-op for native lanes when the live probe is off.** `MO_LANE_PROBE=0`
   downgrades the gate to key-presence, and native lanes (`opus`, `sonnet`) have no
   `api_key_env` — so for them the delivery gate does nothing. Documented
   (`docs/operator/env-vars.md:87`) but it is the exact case the gate was built for:
   "opus had no credit". A run that sets `MO_LANE_PROBE=0` is unprotected.
   *Not a defect, a documented limitation — flagged because it is the headline case.*
2. **Ambient (credentials-file-only) login is never probed.** By design the native
   probe returns `None` (unknown → non-blocking) when no credential *env var* is
   present, and deliberately does not read `~/.claude/.credentials.json` or the
   Keychain. A subscription-login `opus` running out of credit stays invisible.
3. **Spec-vs-shipped deviation in the wiring.** The kickoff said `if not dry_run:`;
   what shipped is `if not dry_run and dispatch_fn is None:` — test-injected fake
   dispatch paths skip the gate. Justified in-code (`execute.py:1205-1213`: a fake
   lane such as `"producer"` would trip the registry check), and production never
   passes `dispatch_fn`. Recorded here because it is a real behavioural difference
   from the spec, not because it is wrong.
4. **Dynamic submode children are only covered statically.** Fan-out is best-effort
   — child recipes' `model_lane`s are unioned from the `_IMPLEMENTER_SUBMODES`
   registry at gate time; a genuinely dynamic child is guarded only by the
   per-dispatch preflight.
5. **The `CI green` claim is head-scoped, not standing.** Verified at
   `38750445aa9f…` (run 38565292891, 9/9). At the current heads CI is red again —
   `gh run view 38962763409` (head `9ee820c1`, 10-16) fails
   `tests/unit/test_acp_agent_py.py::{test_slash_run_in_orchestrate_mode_launches_directly,
   test_child_run_streams_agent_output_into_the_thread,
   test_thread_usage_is_sent_only_when_the_total_changes}` and
   `tests/test_sdd_e2e_dryrun.py::test_dryrun_spine_end_to_end` on both 3.11 and
   3.12. These belong to other sessions' in-flight work (ACP/orchestrator, SDD
   dry-run), **not** to this session: `gh run view 38962763409 --log-failed | grep -c "lane_preflight\|test_lane_native_probe"` → **0**.
6. **The gate has since been extended by another session without a merge note.**
   `1172a9fd` (`docs/operator/env-vars.md`, `feature-flags.md`, `validate.py`,
   `routing.py`, plus tests) adds config-time lane-liveness probing in `validate`.
   It overlaps the gate in intent (early lane liveness) but neither doc cross-links
   the other, so the two now describe lane liveness in two places.

## (c) Blocked on

- **Nothing blocking this session's shipped work.** All recorded-complete tasks
  verify.
- **Open, awaiting an operator decision:** `primary-only-cwd-test` — the cwd-guard
  test fails in the shared primary and passes at the same commit in clean
  worktrees (A/B this session: `9fb802fa`, `b3c80bb5`, `83cdb577`, `19e74c72`,
  `1155cf8e` → `5 passed` each in `/Users/admin/lhg-ab` and
  `/Volumes/docker-ssd/ps/mini-ork-worktrees/_lhg-ab`). Not caused by this
  session's commits; likely an artifact of the primary's uncommitted multi-session
  state. Should be re-run in a worktree, not fixed live in the primary.
- **Peer-owned, not mine:** the current CI reds (`test_acp_agent_py.py`,
  `test_sdd_e2e_dryrun.py`) sit on other sessions' work.
