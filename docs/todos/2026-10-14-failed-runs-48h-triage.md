# Failed mini-ork runs, last 48 h — triage, REPLAY receipt & revive plan

**Scope:** every `task_runs` row with `status=failed` in the last 48 h
(2026-10-12 20:00 → 2026-10-14 12:00). **10 runs found**, all under
`.mini-ork/runs/`. Nine `code-fix`, one `framework-edit`.

> **UPDATED 2026-10-15 — action A (REPLAY) DONE. See §0.**
> Two corrections to the first edition of this doc are in §5.

---

## 0. REPLAY receipt — the cluster is CLOSED ✅

A fresh remote `code-fix` run on current `HEAD` against the same Daytona target
**published**:

| | |
|---|---|
| run | `run-1792047347-71903` |
| result | `task_runs.status = **published**` |
| cost / wall | **$0.209** / 522 s |
| target commit | `cac3ddb mini-ork(code-fix): implementer [run run-1792047347-71903]` |
| nodes | implementer, typecheck, test, reviewer, eval, **publisher** — all `rc=0` |
| reviewer | `verdict: pass` |
| test verifier | `pass`, `post_rc=0`, `strong_overlap` on `test_add`, suite adequacy **1.000** (4/4 mutants killed) |
| diff | exactly `calc.py:5`: `return a - b` → `return a + b` |

**Conclusion:** the publish path that abstained `publish-no-authored-patch` in
run-48337 works on `HEAD`. Runs 1–3 and 5–8 are closed as *historical repros of
a now-fixed defect*, not open bugs. No further fix is warranted for them.

**Two launch preconditions surfaced during the replay** (both are gates, §6):

```
MO_IGNORE_PENDING_FIX=1        # else RC_BLOCKED=75: a failed run is left "pending change"
MO_REMOTE_ALLOW_DIRTY_ENGINE=1 # else the remote run dies at the planner
```

Target repo left at `cac3ddb` (working, published). The prior publish commits
dropped by the reseed are in the fixture's reflog.

---

## 1. The ten runs

| # | run id | class | cost | when | status today |
|---|---|---|---|---|---|
| 1 | `run-1791914481-48337` | publish abstain: no authored patch | $0.47 | 10-13 20:01 | **REPLAYED → publishes** |
| 2 | `run-1791913440-97590` | publish withheld: unverified levels | $0.54 | 10-13 19:59 | **FIXED** (same storm) |
| 3 | `run-1791912327-41843` | publish withheld: unverified levels | $0.50 | 10-13 19:38 | **FIXED** (same storm) |
| 4 | `run-1791911811-5826` | lane credentials (+ translation crash) | $0.13 | 10-13 19:16 | **mixed** (§5) |
| 5 | `run-1791911602-90288` | early death, pre-plan | $0.05 | 10-13 19:13 | **root cause found** (§3) |
| 6 | `run-1791911354-79155` | early death, pre-plan | $0.05 | 10-13 19:09 | **root cause found** (§3) |
| 7 | `run-1791910825-57872` | credential + rollback | $0.09 | 10-13 19:04 | largely operator |
| 8 | `run-1791910667-50438` | early death, pre-plan | $0.05 | 10-13 18:57 | **root cause found** (§3) |
| 9 | `run-1791930074-82118` | implementer edited frozen probe fixture | $0.01 | 10-14 00:21 | **LIVE — guard gap** |
| 10 | `run-1791788217-61320` | framework-edit epic child interrupted | $2.04 | 10-12 20:52 | retryable `resume` |

All nine `code-fix` runs share one kickoff
(`/Volumes/docker-ssd/ps/mo-daytona-e2e-kickoff.md`) and target
`/Volumes/docker-ssd/ps/mo-daytona-e2e-target`, with `placement: remote`,
`environment: daytona`.

---

## 2. Runs 1–3 — already fixed by `88a3f99e`

The remote path was under active repair the same evening:

```
18:50  b485f1d0  feat(remote): node-provider seam — remote placement obtains its node from a provider
19:01  faf4e232  fix(framework-edit): commit the verifier copy's base tree before the diff
19:24  f6d8f42f  fix(node-agent): host runtime maps its real dirs back to the sandbox view in output
19:50  a602d58e  fix(daytona): bootstrap node-agent deps into a venv, never the user site
20:21  88a3f99e  fix(remote): a verified remote run publishes its authored patch   ← names run-48337
20:35  b3bc8627  docs(cloud-providers): Daytona verified end to end
```

`88a3f99e` names **run-1791914481-48337** as its repro and fixes both causes:
the remote sidecar teed stream chunks as records cut at arbitrary bytes
(**169 of 205 were fragments**) so tool calls were unparsable; and the
implementer's sandbox paths (`/workspace/target/calc.py`) never matched the
local repo — `PathMap.to_host` inverts the run's own map. It carries tests and a
live verification note. The commit is on `origin/main`, and §0 re-proves it.

**Runs 2–3** withheld on *levels* rather than the patch (`executes` UNVERIFIED
with `post_rc=1` / `no post_rc`; `target`/`preserve` not proven) — the same
remote stream/sync defects, upstream of the fix.

---

## 3. Runs 5–8 — early death root cause: the dirty-engine guard

Run dirs for `50438` / `79155` / `90288` (the **same task retried three times**)
contain only `config/`, `context-pack.json`, `context-pack.v2.json`, `learned/`,
`run_profile.json` — no plan, no verdict — plus (on `90288`) a zero-byte
`.node-provider.lock`.

The replay **reproduced this exact signature on demand**:

```
LLM dispatch failed for planner node (rc=1): llm_dispatch: engine tree at
'/Volumes/docker-ssd/ps/mini-ork' is dirty; a sha must name an exact tree.
Set MO_REMOTE_ALLOW_DIRTY_ENGINE=1 to override (testing only).
```

→ `returncode 1`, a run dir with only `context-pack.json` + `run_profile.json`,
and a **`.node-provider.lock` left behind**. That is runs 5–8.

**Root cause:** a `--placement remote` run ships the engine tree to a sandbox and
must name an exact sha, so it refuses a dirty primary checkout. The primary was
dirty that evening (another session was writing). **The guard is correct and
fail-closed** — the defects are (a) the failure leaves a stale
`.node-provider.lock`/lease behind, and (b) the error is only visible in the run
log, not in `task_runs.notes`.

---

## 4. Run 10 — `framework-edit` epic child interrupted

`run-1791788217-61320`, $2.04, verdict NEEDS-CHANGE: *"Interrupted during
implementer: the run stopped before the step finished."* Carries a retryable
`resume`. Not a defect — revive by resuming.

---

## 5. Corrections to the first edition

### §5-corrected — the typecheck envelope is NOT a publish blocker

The first edition claimed a target with no typecheck tool "can never publish".
**That is false.** `execute.log:6` of the successful run `996` reads:

```
[ok] publisher: level gate pass (required=['applies', 'executes', 'target', 'preserve'])
```

`contract` is **not** in the required set, and the real abstain trigger is
`_levels.abstained_kinds(run_dir)` (`mini_ork/cli/publisher.py:371`), which keys
on a `failure_kind` the typecheck envelope does not set. Run `996` published with
`contract: UNVERIFIED`; so did §0's replay.

**What remains true:** `recipes/code-fix/verifiers/typecheck.py:337` `_emit_pass()`
writes `{"pass": true, "error_summary": "…skipped"}` with no `checks` list, and
`mini_ork/verify/levels.py:177` `_read_verifier_evidence()` therefore classifies
it **`"unparsable"`** instead of `"abstained"`. So `levels_reasons.contract`
carries a **misleading string** in every code-fix run. Severity: **cosmetic /
diagnostic**, not a blocker. `abstained_kinds()` (levels.py:194) already
documents this shape; the contract reader just doesn't share the tolerance.

### New — run 9 is a real guard gap

`run-1791930074-82118`'s implementer targeted **the primary checkout** and edited
the frozen probe fixture `recipes/code-fix/probes/fixtures/probe-1/tally.py`
(the code-arm's seeded-bug fixture). The fixture is currently intact (still
carries the intentional `range(1, len(values) - 1)` off-by-one), but a run that
can write the probe's own fixture can contaminate a later code-arm calibration.
The "frozen" declaration is prose, not enforced at write time. **This is the one
genuinely live defect in the set.**

---

## 6. Findings surfaced *by doing the replay*

1. **A failed run blocks all new runs** until acknowledged. Launching anything
   returned `RC_BLOCKED=75`:
   > *"needs a change before this run can continue: pending run
   > run-1791910825-57872 (glm rejected the credentials). Confirm with:
   > `mini-ork board retry run-1791910825-57872 --ack-change` (or set
   > `MO_IGNORE_PENDING_FIX=1`)."*

   A single credential failure parks the queue. Worth a clearer operator signal.

2. **A published remote run left its Daytona sandbox `STARTED`.** Teardown is
   best-effort (`mini_ork/cli/main.py:913` `_release_remote_session()`, "the node
   TTLs it"), billed until TTL (default 360 min) or `mini-ork sandbox-gc`. The
   replay's sandbox `de83a1ee-…` was reaped by hand. Low severity, real cost.

3. **Runs 4 / 7 are lane credentials, not code.** `deepseek_flash,minimax,kimi
   rejected the credentials`; `glm … Invalid API key`. Operator action. Run 5826
   additionally hit `host path in 'text' survives translation`
   (`mini_ork/runtime/path_map.py:55`) during a *repair* retry — no commit after
   10-01 addresses the `text` channel, so it is **suspected live but unverified**
   (its dispatch was already failing on credentials).

---

## 7. What's left to do (the user's call)

- **B** — Correct `_emit_pass` (`typecheck.py:337`) so the reason string reads
  *abstained*, not *unparsable*. Cosmetic; NOT a publish blocker. *(Downgraded
  from the first edition.)*
- **C** — Enforce the frozen probe fixture at write time (§5). **The one live
  defect.** Small.
- **D** — Repro the `survives translation` crash on `HEAD` (diagnostic).
- **E (new)** — Clear the stale `.node-provider.lock`/lease left by a
  dirty-engine death, and surface that failure in `task_runs.notes` (§3).
- **F (new)** — Decide whether a failed run should hard-block the queue (§6.1).

**Explicitly NOT recommended:** re-fixing runs 1–3/5–8 (replayed → publishes),
or treating runs 4/7 as code bugs.

---

## Appendix — evidence

- Replay run dir: `.mini-ork/runs/run-1792047347-71903`
  (`retry-exhausted/{implementer,typecheck,test,reviewer,eval,publisher}.attempts.json`
  all `rc:0`; `review-verdict.json` `pass`; `verifier_test.json` `post_rc:0`;
  `.node-provider.json` handle `de83a1ee-…`).
- `task_runs` (`mini-ork/.mini-ork/state.db`): `run-1792047347-71903 → published`.
- Target repo: `cac3ddb` on `/Volumes/docker-ssd/ps/mo-daytona-e2e-target`.
- Key source anchors: `recipes/code-fix/verifiers/typecheck.py:337,380`;
  `mini_ork/verify/levels.py:132,171,177,194,235`;
  `mini_ork/cli/publisher.py:371`; `mini_ork/cli/publisher_authored_patch.py:607`;
  `mini_ork/runtime/path_map.py:55,67`; `mini_ork/cli/main.py:913,940`;
  `mini_ork/cli/sandbox_gc.py`.
