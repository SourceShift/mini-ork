# mini-ork — Issues mined from researcher session history (2026-06-30)

**Source:** 6,218 Claude session transcripts across the `researcher` project
(3,264 jsonl) and the `mini-ork` repo (2,954 jsonl). Extracted 135 unique
human complaint turns mentioning mini-ork + a problem signal, then frequency-ranked
fault signatures across all transcripts.

**Method:** `scratchpad/extract_complaints.py` (user-turn filter, synthetic-prompt
exclusion) + signature grep. Counts below are raw line occurrences (inflated by
long-running monitor loops that repeat the same status text), so treat them as
*relative pain*, not incident counts.

Legend: 🔴 open / recurring · 🟡 partial fix exists · 🟢 documented fix landed (verify it held)

---

## ⚠️ Live incident during this audit (2026-06-30) — I-16 confirmed, with a root cause

While preparing fixes, **this repo got corrupted in real time**: `refs/heads/main`
was reset from clean main to `3fdeeb4` (= `refs/codex/curated-sync`, a *foreign*
repo's "DigitalOcean dark mode logo" commit), the working tree partially bled
toward it (101 files diverged, ~8.4k lines of real source deleted from disk), and
`core.bare` was flipped to `true` (masking everything behind "must be run in a
work tree"). Recovered via `git reset --hard origin/main` (9a62818, v0.6.0) after
verifying no commits were lost (the 4 ahead commits + Python migration are all in
origin/main; the diverged state is preserved in reflog + `keep/curated-sync-3fdeeb4`).

**Root cause (traced):** a codex `exec` session with cwd/`-C` confusion pointing
at a mini-ork worktree while doing other-repo work created
`refs/codex/curated-sync` and reset the tree onto that foreign commit. An active
**researcher**-repo mini-ork scheduler (draining its epic queue — I-15 live) +
~10 concurrent executes were running at the time.

**Status after v0.6.0:** the post-commit watchdog now has a HEAD-clobber guard
(`.githooks/post-commit:46-74`) for exactly this case, but it did **not** prevent
this incident. The bleed ref `refs/codex/curated-sync` has been deleted. This is
direct, current evidence that **I-16 + a cross-repo cwd-confusion guard is the
highest-priority fix** — it actively destroys work.

## v0.6.0 (9a62818) deltas vs this audit (re-verified on clean main)
- I-16 🟡 post-commit HEAD-clobber guard added (insufficient — see incident above)
- I-14 🟡 Phase-0 Python `llm_calls` telemetry exists but **not wired live** (`mini_ork.dispatch`)
- Parallel-run safety: per-run config isolation (`lib/config_resolve.sh`) landed
- codex E2BIG fix landed (`cl_codex.sh` stream-by-file)
- Re-confirmed STILL OPEN on clean main: I-5, I-15, I-3, I-4 (+ I-1, I-2 unchanged)

---

## Tier 1 — Silent failure (agents/lenses die without surfacing)

### I-1 🔴 Lenses silently die and the run continues with a degraded/hollow result
Single biggest theme. A lens (minimax/glm/kimi/opus) dies — 429, missing secret,
gtimeout kill, curl provider never producing output — and the orchestrator does
**not** fail-fast; it synthesizes from whatever survived, so the user gets a
confident-looking but partial answer.

Evidence:
- `1925a9f7…`: "check again ❌ minimax_lens silent-died as it shouldn't get dead"
- `285dba04…`: cost table option "Retry the same dispatch as-is (often resolves **silent-dies** on second run)"
- `a9064ca5…`: ask to "ship a fix to recursive-validate-impl that **fails-fast when ≤2 of 4 lenses report**, then redispatch"
- Memory: `feedback_mini_ork_secrets_for_foreign_home`, `feedback_implementer_lane_policy` (glm 429 "Fair Usage" silently sinks runs)
- Fix docs exist: `docs/fixes/20260602-reviewer-silent-die.md`, `…-spec-author-silent-die.md` → 🟡 partial; lens-level guard still missing.

Fix direction: a lens-quorum gate before synthesis (fail or re-dispatch when
`live_lenses < required`), and make every lane death emit a visible WARN artifact
in the run dir instead of an empty `lens-*.md`.

### I-2 🔴 Foreign-home runs kill curl lanes for lack of secrets
Dispatching against a target repo's `.mini-ork` (no `secrets.local.sh`) silently
disables glm/kimi/minimax → tier-4 quorum fails. Throttle-guard is blind to it.
Memory: `feedback_mini_ork_secrets_for_foreign_home`. Couples with I-1.
Fix direction: preflight secret-presence check per configured lane; refuse to
start (or downgrade explicitly + loudly) rather than start and silently drop lanes.

---

## Tier 2 — Throttle / cost (429 is the #1 raw signal)

### I-3 🔴 No throttle-aware retry/backoff; throttled lanes just die
Frequency: `429|throttle|quota` is the most-cited signal in the corpus.
Evidence:
- `66ab6f61…`: "gpt-5.5 is globally throttled … we need to improve agents to be throttled aware too, maybe orchestrator can identify such issues and **resume them after a specific time**?"
- `a9064ca5…`: "can we make mini ork aware of this and apply the **throttling and retry intelligently**?"
- `53ede002…`: "Re-dispatch the missing glm tier4 lens **after Zhipu daily quota resets**" (manual workaround the human had to do)
Fix direction: detect provider capacity/429 distinctly from real errors; exponential
backoff + requeue with a cooldown; surface "paused, retrying at HH:MM" instead of failing.

### I-4 🟡 Cost circuit halts the whole queue; token spend not steerable
Evidence:
- `bd44720f…`: "why we can't adjust the mini orch setup to **burn tokens wisely**? my aim is high quality, precise, bug-free"
- Memory `feedback_framework_edit_verdict_json_gap`: "$50/day cost circuit halts the whole queue"
Fix direction: per-epic/per-recipe budget instead of one global circuit; let the
queue keep cheap lanes running when one expensive lane trips the cap.

---

## Tier 3 — Verifier / reviewer correctness (gates that lie)

### I-5 🟡 `verdict.json` never emitted → every run shows needs_revision/fail on the artifact
Memory: `feedback_framework_edit_verdict_json_gap`. The verifier doesn't write
`verdict.json`, so the gate defaults to fail even when the reviewer approved and
smoke passed. High raw signal (`verdict.json|needs_revision|verifier never`).
Partial: `docs/fixes/20260627-epic-runner-framework-edit-verifier-failures.md`.
Fix direction: make the verifier always emit a structured verdict; gate on
reviewer-pass + real smoke, not on the missing file.

### I-6 🔴 Reviewer false-positive REQUEST_CHANGES (diff-direction bug)
Evidence:
- `92d76bce…`: "Mini-orch reviewer **false-positive REQUEST_CHANGES** bug … MUST be fixed (CC.4) before Wave 1 fires, otherwise long-running mini-orch burns budget on false negatives"
- Memory `w3_reviewer_diff_direction_p1_2026_06_04` referenced in-session.
Fix direction: reviewer reads diff in the correct direction (base…HEAD); add a
regression test with a known-good diff that must APPROVE.

### I-7 🔴 Gates "theater": passing framework-edit gates while work is dead
Memory: `feedback_harsh_critic_panel_catches_theater` — an autonomous 12-epic
build passed gates but was substantially DEAD (tests were theater).
Fix direction: stronger smoke that exercises imports/wiring, not just file
existence; the README claim-check pattern generalized to "claimed feature actually runs".

---

## Tier 4 — Planner / classifier

### I-8 🔴 Oversized kickoff truncates the planner → silent hollow-plan fallback → vacuous run
Memory: `feedback_large_kickoff_truncates_planner`. High raw signal
(`hollow-plan|truncated planner`). The fallback is silent — looks like a real run.
Evidence also in fault-injection wishlist `a9064ca5…`: "obs-fault-injection recipe
over mini-ork's known faults (lane 429, **truncated planner**, missing secret, **hollow-plan fallback**)".
Fix direction: detect truncated/empty planner JSON and HARD-FAIL with a clear
message; never fall through to a hollow plan silently.

### I-9 🟡 Classifier overrides explicit recipe / misclassifies task_class
Evidence: `2130f986…` D-050: "classifier said task_class=generic instead of
research_synthesis. matches keywords in task_class.yaml may need touch-up."
Partial: `docs/fixes/20260604-dispatch-classifier-overrides-explicit-recipe.md`.
Fix direction: an explicitly-passed recipe must win over the classifier; verify
the 20260604 fix actually short-circuits classification.

---

## Tier 5 — Worktree / checkout / scope hygiene

### I-10 🔴 verifier-wrong-checkout / upstream cwd bug
Evidence:
- `4dc72150…`: "Fix the mini-ork verifier-wrong-checkout bug"
- `9cb0deaa…`: "diagnose+patch mini-ork upstream **cwd bug** first"
- D5/D-015 (`1925a9f7…` table): "planner rejection upstream fix … belongs in ~/ps/mini-ork repo"
Fix direction: verifier must run against the worktree/branch under test, not the
primary checkout; assert cwd at node entry.

### I-11 🟡 framework-edit stashes the whole working tree → orphaned WIP stash
Memory: `feedback_framework_edit_stashes_worktree`. A failed/rolled-back run
orphans uncommitted changes in a `wip-pre-implementer-*` stash.
Fix direction: scope the stash to tracked-by-run files, or restore on failure;
at minimum log the stash ref loudly so it's recoverable.

### I-12 🔴 Scope-revert is coarse (12 out-of-scope files auto-reverted)
Evidence: harness-integ runs (`52872e69…`) repeatedly had "12 out-of-scope files
auto-reverted" and the human had to confirm in-scope edits survived.
Fix direction: scope-gate should warn + quarantine, letting the human/reviewer
confirm before nuking; never silently revert without a manifest.

### I-13 🟡 framework-edit output capture is a coin-flip; runs sometimes leave changes applied
Memory: `feedback_framework_edit_capture_unreliable`, `feedback_framework_edit_verdict_json_gap`
("runs sometimes leave changes applied; git status before apply").
Fix direction: deterministic harvest location; reset residue before apply; prefer
critic-specified direct edits for remediation.

---

## Tier 6 — Observability / UI (transparency gaps)

### I-14 🔴 Agent runs not navigable; LLM calls / cost / transcripts missing in the UI
Strongly repeated, top user ask.
Evidence:
- `84196e34…` & `a9064ca5…`: "full transparency … navigate from each mini ork run to the agents it dispatches, then each agent dispatch which other ones, their full llm call logs, status, if they failed, why failed, exact duration"
- `84196e34…`: "still there's a … bug in the views or ingestion of logs … I can't see any llm calls" (`LLM calls (0)` on tiny_researcher)
- `ef348977…`: "find why planner turns are empty?" (planner: 0 llm calls, $0, no transcript.json)
- `e6830b92…`: agent shown as openai/codex $0 failed while it's actually claude sonnet done — **wrong provider/cost attribution**
- `a9064ca5…`: trajectory page "shows two statuses" (success vs failed) for the same iter
Fix direction: capture stream-json (`MO_TRACE_RICH`) by default for capable
providers; attribute llm_calls to the agent even when dispatched via shim/SDK;
single source of truth for status; correct provider/cost mapping.
Note: Phase-0 telemetry work landed (commit d576e28 persist DispatchResult to
llm_calls) — verify it closes the `LLM calls (0)` and $0-cost cases.

---

## Tier 7 — Scheduler / queue control

### I-15 🟡 Scheduler has no epic filter — drains every ready epic
Memory: `feedback_scheduler_no_epic_filter`. Bare `mini-ork-scheduler` dispatches
EVERY not-started ready epic (dozens of unrelated backlog).
Evidence: `459f3498…`: "list all last 24h mini ork runs still not completed or
not merged" (user fighting an unbounded queue).
Fix direction: `--epic`/`--roadmap` scope flag on the scheduler.

### I-16 🟡 Pre-push hook mutates HEAD / blocks pushes on README drift
Evidence:
- Memory `feedback_prepush_hook_mutates_head`: L2 panel resets branch to a curated-sync ref and doesn't restore on failed push.
- `a9064ca5…`: repeated `readme-claim-check.sh` DRIFT blocks (release pushes blocked by the 4-lens panel).
Fix direction: hook must never mutate HEAD; restore on failure; make the drift
panel advisory-by-default for release tags.

---

## Tier 8 — Feature requests born from pain (not bugs, but the "why")

### I-17 Auto bug-collector: agents find lower-priority bugs that get dropped
Evidence: `196dcd4e…`: "during its work it identifies bugs that get deprioritized
— we need a way in mini ork so such reporting is created automatically … the
orchestrator should receive that bug report, prioritize it and dispatch it."

### I-18 Stray artifacts written to wrong repo
Evidence: `bd44720f…`: "why do we have such files? …bughunt-v2-r1-data.ndjson —
if mini orch we should hint it to **not create them there**." Run artifacts leak
into the consumer repo tree.

### I-19 Steering loop: inject guidance into live agent CLI runs
Evidence: `52872e69…`: "create a loop from you to mini ork … check status, read
agent stream log, find issues … by injecting your messages into the claude and
codex cli runs under the hood of each agent so they get aware of your steering."

---

## Recommended fix order (by pain × leverage)

1. **I-1 + I-3** lens-quorum gate + throttle-aware retry — kills the silent-die /
   429 class that dominates the corpus.
2. **I-8** hard-fail on truncated/hollow plan — stops vacuous runs masquerading as real.
3. **I-5 + I-6** verifier verdict.json + reviewer diff-direction — gates that lie burn budget and trust.
4. **I-10** verifier-wrong-checkout/cwd — correctness of what's actually verified.
5. **I-14** observability — the user's most-repeated explicit ask; also makes 1–4 debuggable.

Per repo rule, fixes touching 2+ files dispatch through `bin/mini-ork run`
(framework-edit / code-fix), one issue per scoped kickoff — do not hand-edit.

> Build the I-8 / I-3 fault-injection guard suggested in `a9064ca5…` (F7): an
> obs-fault-injection recipe over the known faults (lane 429, truncated planner,
> missing secret, hollow-plan fallback). It converts this whole list into an
> automated regression suite.
