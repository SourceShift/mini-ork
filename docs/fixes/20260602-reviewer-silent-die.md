# Fix-Spec — reviewer silent-die failure mode

> ## ⚠️ CORRECTION (2026-06-02 22:05 UTC)
>
> **Root cause re-attributed.** Forensic inspection of libwit's
> `.agentflow/mini-orch/runs/.../W3B-C/iter-1/` showed `review.log` and
> `verdict.json` were ABSENT — but `rubric.json` was present with
> `{"pass": false, "score": -1, "parse_error": true, "items": []}`.
> The iter never reached the reviewer phase. **The real failure is in
> `lib/rubric-prescreen.sh:144-205` parser**, not `lib/review.sh`.
>
> Rubric pre-screen dispatches with `--output-format stream-json` and tries
> 4 fallback strategies to extract `{pass:..,score:..}` from the
> stream-json log. When the LLM emits long thinking_delta sequences with
> no clean structured-output JSON in the final result, all 4 fall through
> and the parser writes the `parse_error:true` sentinel. The orch then
> aborts the iter at the rubric gate, BEFORE reviewer dispatch.
>
> Fixes A-E below are still valid hygiene for `lib/review.sh` (since the
> reviewer dispatch is also stream-json-fragile), but the load-bearing
> recurring blocker is one layer earlier. Recommended cascade:
>
> 1. **Switch rubric to `--output-format json`** (not stream-json) — rubric
>    doesn't need partial streaming; single result wrapper has 1 known
>    extraction path
> 2. **Add `--json-schema` constraint** with `{pass:bool, score:int, items:[]}`
>    so the model can't drift into prose
> 3. **Preserve LLM output snippet in parse_error case** — currently invisible
>    when parser fails
> 4. **Soft-fail rubric parse errors** — heuristic gate, not load-bearing;
>    parse_error=true should fall through to reviewer, not abort iter
>
> Downstream insforge memory `id=1253` documents the corrected diagnosis
> with file:line evidence. Fix is approximately 30-60min surgical edit to
> `lib/rubric-prescreen.sh`.

---

## Original fix-spec (still relevant for review.sh hygiene)


**Date observed:** 2026-06-02 across ~3 hours of dispatch (WAVE 3a 5/5 + WAVE 3b 3/3 sub-epics, every iteration)
**Downstream incident:** libwit `researcher` repo — SELF-EVOLVING-WAVE-3A-MEMORY-CLUSTER + SELF-EVOLVING-WAVE-3B-CAPSULE-EVIDENCE
**Affected runtime:** `.agentflow/lib/review.sh` reviewer phase (after worker emits commits, before auto-merge)

## Symptom

Reviewer iter directory contains a 0-byte `review.log` AND a 0-byte `review.err`. Orch loop emits:

```
[mini-orch] review lane resolved: agents.yaml reviewer=opus → using lane=opus (env override = no)
[mini-orch] review epic=<EPIC> iter=<N> WARN review failed
[mini-orch] auto-merge skip: no APPROVE verdict for <EPIC>
```

`state.db` row stays `status='in progress'`. No `verdict.json` is written. Worker's actual commits land on the feature branch (visible via `git log feat/<branch>`) but never get auto-merged because the verdict file is absent.

## Reproduction frequency

8/8 reviewer dispatches during the 2026-06-02 self-evolving wave (WAVE 1 F-STATE, WAVE 3a all 5, WAVE 3b W3B-A + W3B-C). Sample epic IDs:

- `W3A-A` memory-health migration
- `W3A-B` memory-health-probe service
- `W3A-C` skill-distiller
- `W3A-D` book-prompt-registry
- `W3A-E` vertical-classifier-LLM
- `W3B-A` capsule writer retirement
- `W3B-C` evidence cascade acquisition
- `F-STATE` per-chapter state envelope

## Key observation: `MO_REVIEWER_LANE=opus` does NOT bypass

The downstream session set `MO_REVIEWER_LANE=opus` to dodge the suspected glm-specific failure path. orchestrator.log confirmed `using lane=opus`. **The silent-die still occurred at the same rate.** This rules out the lane being the cause; the bug lives in `lib/review.sh`'s wrapper around the LLM call, not in any specific lane script.

## Companion incident reference

This is the same shape as the `spec-author silent-die` failure (`./20260602-spec-author-silent-die.md`):
- Pre-LLM-call artifact files NOT pre-created
- Stderr redirect path swallowing errors before they hit the err file
- Empty stdout interpreted as "review skipped" instead of "review FAILED"

And matches the broader `phase5-prose-claims` codex silent-exit class — libwit commit `e12422e1b` (2026-06-01) fixed `cl_codex.sh:135`'s `2>/dev/null` trailing redirect.

## Suspected root cause

`lib/review.sh` (or whatever wrapper sits between orch and the chosen lane's `cl_<lane>.sh`) likely:

1. **Invokes the lane wrapper** with a prompt file → output redirect to `$ITER_DIR/review.log`
2. **No pre-create of review.log/review.err** — if the dispatcher errors before reaching the redirect, no files exist
3. **No rc check after the call** — empty stdout + rc=0 is treated as "reviewer said nothing actionable, default to skip"
4. **No `set -e` propagation** — a failure deep in the lane script doesn't bubble up

## Proposed fix

### Fix A — pre-create review.log + review.err BEFORE LLM dispatch

In `lib/review.sh` (or the canonical reviewer wrapper):

```bash
local review_log="$ITER_DIR/review.log"
local review_err="$ITER_DIR/review.err"
: > "$review_log"           # pre-create (zero-byte)
: > "$review_err"           # pre-create (zero-byte)
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] reviewer dispatch starting (epic=$EPIC iter=$ITER lane=$LANE)" >> "$review_err"

# Dispatch
"$LANE_SCRIPT" --print --output-format text "$REVIEW_PROMPT_FILE" >> "$review_log" 2>> "$review_err"
local rc=$?
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] reviewer exited rc=$rc, stdout_bytes=$(wc -c < "$review_log")" >> "$review_err"
```

This guarantees:
- Presence of `review.err` (even empty-ish) signals dispatch was attempted
- Final line of `review.err` ALWAYS encodes rc + stdout size — 1-grep diagnosis
- If a hard kill (SIGKILL, SIGTERM) lands between the markers, the "starting" line survives, telling us WHEN dispatch began

### Fix B — fail-loud on empty review output (per Zero-Fallback Rule)

```bash
if [ ! -s "$review_log" ] && [ "$rc" = "0" ]; then
  echo "[review] FATAL reviewer returned rc=0 but produced 0 bytes stdout — refusing to skip silently" >&2
  echo "fail_reason=reviewer-empty-output" > "$ITER_DIR/verdict.json"  # so auto-merge gets explicit ESCALATE not absence
  echo "verdict=ESCALATE" >> "$ITER_DIR/verdict.json"
  exit 2
fi
```

This converts "silent skip" → "explicit ESCALATE" so the orch loop's status-machine knows the iter is bad and the operator (downstream session) gets a clear signal.

### Fix C — verdict.json is mandatory; absence is a FATAL

After the LLM call returns, BEFORE the auto-merge phase reads verdict.json:

```bash
if [ ! -f "$ITER_DIR/verdict.json" ]; then
  echo "[review] FATAL no verdict.json after reviewer exit (rc=$rc) — review pipeline is broken" >&2
  echo "{\"verdict\":\"ESCALATE\",\"reason\":\"missing-verdict-file\"}" > "$ITER_DIR/verdict.json"
  exit 3
fi
```

### Fix D — propagate `set -uo pipefail` through every nesting layer

If `lib/review.sh` is missing `set -uo pipefail`, a failure in a piped subshell (`claude --print ... | jq ...`) gets masked. Audit every layer:

```bash
# At the top of lib/review.sh
set -uo pipefail
```

Same audit for any helper sourced into review.sh.

### Fix E — add 1-token health-check probe BEFORE the real review call

If the lane's `cl_<lane>.sh` is broken (bad API key, model rotation, expired token), the silent-die manifests as 0-byte stdout. Add a fast pre-check:

```bash
local probe_rc
echo "ok" | timeout 10 "$LANE_SCRIPT" --print --output-format text "say OK" > /dev/null 2>&1
probe_rc=$?
if [ "$probe_rc" != "0" ]; then
  echo "[review] FATAL lane=$LANE failed 1-token health probe rc=$probe_rc — abort review before main dispatch" >&2
  echo "fail_reason=lane-unhealthy" > "$ITER_DIR/verdict.json"
  exit 4
fi
```

Cost: ~$0.001 per dispatch. Saves ~$0.30-1.00 of wasted main-dispatch + 5-15 min of orch wall time when the lane is genuinely broken.

## Acceptance criteria for fix landing upstream

1. Pre-created `review.log` + `review.err` files exist after EVERY review dispatch attempt (even hard kills).
2. Final line of `review.err` ALWAYS encodes `rc + stdout_bytes`.
3. rc=0 with 0-byte stdout triggers explicit FATAL emission (Fix B) + writes `verdict.json` with `verdict=ESCALATE`.
4. Absence of `verdict.json` post-review triggers FATAL (Fix C).
5. `lib/review.sh` + all sourced helpers have `set -uo pipefail` at top.
6. 1-token health probe (Fix E) gates the main review dispatch.
7. Test added at `tests/review/silent-die.bats` that simulates LLM returning empty stdout AND verifies orch writes ESCALATE verdict instead of silent skip.

## Downstream patches needed

After upstream lands, libwit `researcher` repo must:
- Pull updated `lib/review.sh`
- Re-run any in-flight wave that hit silent-die — the new behavior will mark them ESCALATE explicitly so the resumer knows where to look
- Confirm the health-probe (Fix E) catches a deliberately-broken lane

## Composes with

- `./20260602-spec-author-silent-die.md` — sister failure mode (same shape, different LLM call site)
- `./20260602-preflight-gate-hardening.md` — preflight gate catches many upfront failures but cannot catch mid-dispatch silent-die
- libwit commit `e12422e1b` — analogous fix for codex worker silent-exit class
- libwit Insforge memory `wave_1_6of6_and_wave_3b_3of3_shipped_self_evolving_2026_06_02` (id=1252) — downstream session log documenting the 8-time occurrence pattern

## Cost of NOT fixing

Per 8-dispatch sample size:
- Manual squash-merge rescue: ~5-10 min per epic × 8 = ~60-80 min wasted operator time
- Wasted reviewer LLM dispatches (rc=0 / 0 bytes still bills the prompt): ~$0.30-0.80 × 8 = ~$3-6
- Concurrent session interference window (between worker commit and manual rescue): increases lint-staged --no-stash sweep risk

Net: ~1-1.5 hours operator time + ~$5-10 LLM cost per 8 dispatches that should've auto-merged.
