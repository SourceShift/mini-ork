# Fix-Spec — spec-author silent-die failure mode

**Date observed:** 2026-06-02 ~15:53 UTC
**Downstream incident:** libwit `researcher` repo, WAVE 1 `SELF-EVOLVING-P0-P1-F-STATE` sub-epic
**Affected runtime:** `.agentflow/mini-orch/run.sh` orchestrator spec-author phase (Phase 11 BDD spec synth)

## Symptom

Sub-epic iter-1 directory contains ONLY `spec-author-prompt.md`. No subsequent artifacts:

```
$ ls .agentflow/mini-orch/runs/<job>/<EPIC>/iter-1/
spec-author-prompt.md     # 12KB, generated correctly by orch
# (NO spec-author.log)
# (NO spec-author.err)
# (NO spec.md / e2e/_specs/<slug>.spec.ts)
# (NO worker.log / worker.err / worker.pid / commits.log)
```

`state.db` shows `status='escalated'` set at the same timestamp the prompt was written, indicating orch dispatched the spec-author LLM call but never received an output. Orch did NOT invoke the worker phase, did NOT produce `verdict.json`, did NOT log a terminal error.

Net result: sub-epic transitions `not started → escalated` with **zero diagnostic artifacts**. Downstream sessions that wake up to a wave-level completion report cannot diagnose what failed without reading orch-level shell history (which is ephemeral on nohup dispatches).

## Suspected root cause

`spec-author` phase invokes a `claude --print --output-format text "$PROMPT_FILE" > "$ITER_DIR/spec-author.log" 2> "$ITER_DIR/spec-author.err"` (or equivalent). If the LLM call:

1. **Returns empty stdout** → spec-author.log = 0 bytes; orch may interpret as "no spec needed" and skip → but should still write the empty log file.
2. **Returns conversational text not matching parser** → log present but spec extraction fails silently.
3. **Errors out before redirect file creation** → no log/err file at all (matches observed pattern).

The fact that `spec-author.log` and `spec-author.err` are BOTH missing (not zero-byte) suggests the dispatcher invocation itself failed before/at `claude --print` exec, OR the stderr redirect failed, OR the orch wrapper aborted on a non-zero rc without writing the redirected files.

## Companion incident reference

This pattern matches the broader `phase5-prose-claims` silent-exit class:
- TRACK-A codex worker SIGTERM rc=143 with 0-byte log + 0-byte err
- Root cause: `cl_codex.sh:135` exec line ended with `2>/dev/null` swallowing all stderr
- Fixed in libwit commit `e12422e1b` (2026-06-01)

The spec-author dispatcher needs the same audit: scan all spawn paths (`cl_<lane>.sh` invocations from spec-synth steps) for `2>/dev/null` or unredirected error swallowing.

## Proposed fix

### Fix A — emit stub log/err files BEFORE LLM dispatch

In `lib/spec-synth.sh` (or equivalent), pre-create the artifact files so a silent crash still leaves diagnostic breadcrumbs:

```bash
local spec_author_log="$ITER_DIR/spec-author.log"
local spec_author_err="$ITER_DIR/spec-author.err"
: > "$spec_author_log"           # pre-create (zero-byte)
: > "$spec_author_err"           # pre-create (zero-byte)
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] spec-author dispatch starting (epic=$EPIC_ID iter=$ITER lane=$LANE)" >> "$spec_author_err"

# now dispatch — even a hard kill leaves the 'starting' marker
claude --print --output-format text "$PROMPT_FILE" >> "$spec_author_log" 2>> "$spec_author_err"
local rc=$?
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] spec-author exited rc=$rc, stdout_bytes=$(wc -c < "$spec_author_log")" >> "$spec_author_err"
```

This guarantees:
- The presence of `spec-author.err` file (even empty-ish) signals dispatch was attempted
- Final line of `spec-author.err` ALWAYS contains the rc + stdout size, making diagnosis 1-grep

### Fix B — fail-loud on empty spec output

Per Zero-Fallback Rule:

```bash
if [ ! -s "$spec_author_log" ] && [ "$rc" = "0" ]; then
  echo "[spec-synth] FATAL spec-author returned rc=0 but produced 0 bytes stdout — orch refuses to proceed silently" >&2
  echo "fail_reason=spec-author-empty-output" >> "$ITER_DIR/contract-precond.json"
  exit 2
fi
```

### Fix C — distinguish "BE-only kickoff (heuristic skip)" from "spec dispatch FAILED"

The current orch correctly emits this for BE-only kickoffs:
```
[mini-orch] synth-spec: epic=X SKIPPED (BE-only kickoff (no UI files, no routes — heuristic short-circuit))
```

But for F-STATE (which has a `BookJobProgressContent.tsx` FE file in scope) the heuristic should NOT have fired. Need to verify the heuristic gate isn't accidentally classifying mixed FE/BE kickoffs as BE-only. Check the route extractor and `.tsx` glob in the `spec_author_hints` step.

## Acceptance criteria for fix landing upstream

1. Pre-created `spec-author.log` + `spec-author.err` files exist after EVERY spec-author dispatch attempt (even silent failures).
2. Final line of `spec-author.err` ALWAYS encodes `rc + stdout_bytes`.
3. rc=0 with 0-byte stdout triggers explicit FATAL emission (Fix B).
4. Mixed FE/BE kickoff heuristic does NOT misclassify as BE-only when ANY `.tsx` file or route is in scope (Fix C verified via test on F-STATE replay).
5. Test added at `tests/spec-synth/silent-die.bats` (or framework equivalent) that simulates LLM returning empty stdout AND verifies orch FATALs + exits non-zero.

## Downstream patches needed

After upstream lands, libwit `researcher` repo must:
- Pull updated `lib/spec-synth.sh`
- Re-run F-STATE via the dispatch plan at `docs/_meta/todos/20260602-2015-f-state-re-dispatch-plan.md`
- Verify the new error-emission path by deliberately killing the spec-author LLM mid-flight and observing the FATAL log

## Composes with

- `~/ps/mini-ork/docs/fixes/20260602-preflight-gate-hardening.md` (prior upstream fix-spec)
- Downstream: libwit `researcher` Insforge memory `wave_1_self_evolving_5_of_6_shipped_e_assign_manual_rescue_2026_06_02` (id=1250)
- libwit commit `e12422e1b` — analogous fix for codex worker silent-exit class
