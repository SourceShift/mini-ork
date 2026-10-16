# Session gaps — `f6e1ed90` (orchestrator page PM brief + not_needed state)

Session: `f6e1ed90-7a0a-4081-b46e-e612f0af5fd0` (name `verifier-verdict-failure-kind`)
Domain: backend · Audited: 2026-10-16 · Method: every status verified against git/commits
and file existence, never against the recorded value.

## Task table

| id | description | recorded | VERIFIED | evidence |
|---|---|---|---|---|
| `pm-brief` | PM work breakdown (WS-1..WS-12) from the FR-001..FR-040 audit + ContextNest findings | done | **PARTIAL** — delivered in chat only, never written to disk | no file on disk; this audit dir is the first durable copy |
| `verifier-failure-kind` | A verifier that cannot *run* its tool abstains instead of refuting — the audit-2026-10-11 misclassification (one bad lint call → 10 failed nodes + rollback) | (plan mode) | **DONE** | commit `2f7e4b81` *"Honour verifier failure provenance: toolchain abstains instead of refuting"*; `levels.py:148-205` (`FAILURE_KINDS`/`ABSTAINING_KINDS`/`ABSTAIN_RC=3`/`abstained_kinds`), `execute.py:1291-1380` (abstain counted apart from failures; abstained parent blocks descendants as *abstained*, never failed), `execute.py:589` (`verdict.json` `failure_kinds`), `publisher.py:365` (abstention gate), `pm.py:170` (spawn classifier); tree clean |
| `ws-1-orch-page-tests` | Refresh the 7 stale `test_ide_pages_orch.py` assertions to the shipped shapes | in_progress | **DONE** | commit `ae1307d9` on `origin/main`; `pytest tests/unit/test_ide_pages_orch.py` green |
| `not-needed-state` (WS-12) | `not_needed` terminal run status — migration `0076` + canonical vocabulary + call-site sweep | in_progress | **DONE** | commit `d70f94cf` on `origin/main`; `tests/unit/test_not_needed.py` 17 passed; live run closed |

## Missing features & gaps

**G1 — The GUI half of the AC live replay is owed, and it is the user's.**
`specs/orchestrator-page-redesign.spec.md` marks the replay `[~]`, not `[x]`. The CLI half ran
(all 8 tabs via `board_cmd page orch --tab <t>`, `errors: {}`); the GUI half — launching the IDE
and clicking the tabs — needs the real window to confirm *pixels*, which no tool call can do.
This is a genuine acceptance gap, not a formality: the conformance audit's row-1 fix (a list-act's
`kind`) is only observable drawn.

**G2 — There is no stage-report feed to read, only a place expecting one.**
`<run>/buddy/<epoch>.json` has **no writer**: `grep` for any `buddy/` write returns nothing, and
commit `fae2a7a5` is literally titled *"One orchestrator per run, and the run buddy is gone."*
No `ide_pages/*.py` reads it either. So WS-2 is not "repair a broken feed" — the writer must be
**built**, and anything (page, timeline) that was to consume it has nothing to render today.

**G3 — `MO_BUDDY_*` → `MO_WATCH_*`: the rename is complete but silent.**
Zero `MO_BUDDY_*` references remain in code, docs or shell. Only four `MO_WATCH_*` vars exist:
`MO_WATCH_STALL_SECONDS`, `MO_WATCH_MAX_TURNS`, `MO_WATCH_LONG_NODE_SECONDS`, `MO_WATCH_COST_USD`.
An operator whose shell still exports `MO_BUDDY_*` gets a **silent no-op** — the run watches on
defaults and nothing says why. Needs a one-line deprecation read (warn, then honour) or a documented
break.

**G4 — The `not_needed` bucket is engine-side only.**
The engine now emits a 5th count (`{working, needs_you, done, failed, not_needed}`), but the Rust
client's `Counts` (`mini-ork-ui/src/board.rs`) has 4 tiles. Nothing *breaks* — every field is
`#[serde(default)]`, so the unknown key is dropped — but the UI **cannot show** a not-needed run,
which is half of the "always have a proper final state" ask. Needs the crate change plus the two
submodule pin bumps (`zed-mini-ork/ui/` and `crates/mini_ork_ui`).

**G5 — Linear/Jira from the UI: not started.**
No code, no branch. Blocked on credentials and on the two-tier decision required by the repo's
delivery rule (secure tier needs a real secret store + per-node scoping; simple tier must fail
closed on a missing value). This is the largest un-started item in the original ask.

**G6 — The PM brief itself is not on disk.**
The WS table (the session's primary deliverable) lives only in the chat transcript, so nothing can
track it. This file is the first durable copy; the remaining WS-2..WS-11 should be lifted into it.

## Blocked on

1. **The user, to run the GUI half of the AC-012 replay** — launch the IDE, walk the Orchestrator
   rail entry and the 8 tabs, confirm the `confirm` prompt on a `--force` action and a
   classifications row click re-reading the detail beneath it. No tool call can substitute.
2. **The user, for D1 / D2 / D6** and priority among **WS-2..WS-11**.
3. **The user, for Linear/Jira credentials** and the secure-vs-simple tier choice.
4. **Two fixes, not one, for the IDE to show the fifth bucket.** The running `uvicorn` (PID 81516)
   predates the change and holds the old bucket list in memory — but it *also* runs against the
   wrong home: its parent (`81476`) is `bin/mini-ork serve --port 7090 --home
   /private/tmp/mo-smoke50/.mini-ork`, a temp smoke home with no `epics` table, so page reads that
   omit `X-Mini-Ork-Home` render an empty board. `kill 81516`, then relaunch **without** the
   `--home /private/tmp/...` flag. (Corrected: an earlier draft of this report, and of
   `AGGREGATE.md`, called the restart a single one-action fix.)
