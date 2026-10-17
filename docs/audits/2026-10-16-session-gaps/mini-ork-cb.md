# Session `mini-ork-cb` — self-audit (2026-10-16)

Scope of this session: the **run-page C1–C27 UX programme** (`docs/plans/2026-10-15-run-page-jtbd-ux-review.md`)
across the engine and the IDE crate, plus the **spec-driven-delivery (SDD) ratification** question.

Method: every status below was re-derived from the tree, not from the recorded status.
Commands that were actually run are quoted verbatim.

---

## (a) Task table

| id | description | recorded status | VERIFIED status | evidence |
|---|---|---|---|---|
| `fix-main-buddy` | Fix forward: origin/main red after the run-buddy removal | dispatched, in flight | **SUPERSEDED — not needed** | board: `fix-main-buddy-20261016-111824` `state=not_needed`, `step=rollback`. A peer landed the fix first: `d3b52b38 ide(node): restore the node page after the run-buddy removal`. `python3.11 -c "import mini_ork.ide_pages.node"` → `NODE IMPORT OK`. `_wall` return present at `mini_ork/ide_pages/run.py:834-835`. `grep -rn "_buddy_node_facts" mini_ork/ tests/` → only a prose comment at `mini_ork/acp_orchestrator/watch.py:21`. |
| `rp-e3-header-v2` | Run page: state-aware header (label, actions, menu, updated_at) on every tab | **done** (board: `done`, step `publisher`, +614/−147) | **LANDED 2026-10-17 — `b324fae5`** (surgical re-apply; see §d) | `mini_ork/ide_pages/run_header.py` does not exist in any commit (`git log --oneline -- mini_ork/ide_pages/run_header.py` → empty); the file exists only inside the run dir. Diff is `+614/−147` over 6 files at `.mini-ork/runs/rp-e3-header-v2-20261016-111144/framework-edit.diff`. `git apply --check` → **error: patch failed: mini_ork/ide_pages/node.py:55, patch does not apply** (superseded by `d3b52b38`). Its spec tests pass *inside* the run (`verifier-spec-tests.log` → `pass: True`, 23 selected) but are absent from the tree. |
| `rp-e6-graph-focus` (C13 engine half) | Graph opens on the failing/blocking node; node carries a one-line `reason` | **needs_change** | **LANDED 2026-10-17 — `51c37434`** | `.mini-ork/runs/rp-e6-graph-focus-20261015-222118/verdict.json` → `{"files_changed":2,"tests_pass":true,"static_pass":true,"pass":true}`. `git apply --stat` → `run.py +151`, `tests/unit/test_ide_pages_run_graph_focus.py +170`. `git apply --check` → **clean**. Live `run.py` has no `graph["focus"]` (`grep -n '"focus"' mini_ork/ide_pages/run.py` → no hits). |
| `rp-e10-artifacts-learnings` (C22 + C23) | Artifacts grouped by node/kind with pinned keys; learnings promote/reject + used-by | **needs_change** | **LANDED 2026-10-17 — `c7363e73`** | `verdict.json` → `pass: true`. `git apply --stat` → `run.py +267`, new test `+356`. `git apply --check` → **clean**. |
| `rp-e11b-steer-receipt` (C14 engine half) | `board steer` prints a receipt line; `--json` carries a `receipt` key | in flight | **LANDED 2026-10-17 — `937506bc` + `d71a28d8`** | `verdict.json` → `pass: true`. Worktree `/Volumes/docker-ssd/ps/mini-ork-worktrees/rp-e11b-land` at base `9bd29ede`: `M mini_ork/cli/board_cmd.py` (+75/−6) and untracked `tests/unit/test_board_steer_receipt.py`. No commit on `wt/rp-e11b-land`. |
| **C1–C27 programme** | "implement all" 27 proposed UX changes | in progress | **0 of 27 implemented** | see §b |
| **SDD operator ratification** | Operator reviews a compiled SpecCard, accepts/adjusts, then accepts | design exists | **ABSENT** | `python3 bin/mini-ork specs --help` → only `ingest`, `list`, `lint`. `grep -rn "ratif" mini_ork/cli/ mini_ork/specdir/ --include=*.py` → no matches. `grep -rn "ratification\|SpecCard\|spec-card" mini_ork/ide_pages/` → no matches. |

---

## (b) Missing features & gaps

### The programme's mechanical gap: three green runs never reached `main`

`rp-e3-header-v2`, `rp-e6-graph-focus` and `rp-e10-artifacts-learnings` all end with
`verdict.json` `pass: true`, and their `framework-edit.diff` files still apply (e6, e10) or
nearly apply (e3v2) to today's `main`. None of it is on `main`.
`framework-edit`'s deliverable is the diff in the run dir; the board's `done`/`publisher`
step does **not** imply a commit landed. Treat "relay the diff into a land worktree" as a
separate, always-required step.

### Engine-side changes

| # | change | verified state | evidence |
|---|---|---|---|
| C3 | Triage names the actual failing/blocking node | not landed | `mini_ork/ide_pages/outcome.py` present but no golden-test change; `V2_TABS` unchanged |
| C4 | Every decision callout explains itself | partial | `_gate_needs_you` exists at `outcome.py:610`; no test asserting non-empty text |
| C5 | One attention block | not landed | no `Needs you` lane in `mini_ork/ide_pages/*.py` (`grep` hits only `cn_session.py:98`, a status string) |
| C6 | Sticky run bar + header actions | **engine half landed `b324fae5`** (`run_header.py`); IDE render half not started | `mini_ork/ide_pages/run_header.py` now on main |
| C8 | **Changes** tab | not landed | `mini_ork/ide_pages/run.py:52-54` — `V2_TABS` = story, graph, questions, kickoff, agents, learnings, artifacts. No `changes` |
| C9 | **Checks** tab with explainable badges | not landed | same `V2_TABS`; no `tip` on triage counts |
| C12 | Tab badges (`Questions · 2`) | not landed | `V2_TABS` entries are still `(key, label)` pairs |
| C13 | Failure on the canvas (engine half) | **engine half landed `51c37434`** | e6 diff, §a |
| C14 | Steering receipt (engine half) | **engine half landed `937506bc`+`d71a28d8`** | e11b, §a |
| C18 | Breadcrumbs (`epic › run › retry #2`) | not landed | `grep -rn "crumbs" mini_ork/ide_pages/*.py` → no hits |
| C21 | Copy summary as markdown | not landed | `grep -rn "copy_summary\|Copy summary" mini_ork/ide_pages/*.py` → no hits |
| C22/C23 | Artifacts grouped; learnings actions | **landed `c7363e73`** | e10 diff, §a |
| C25 | Fault attribution in the failure triage | not landed | `outcome.py` present; no fault-class split |

### IDE-side changes (`/Volumes/docker-ssd/ps/mini-ork-ui`, HEAD `9154ef4`)

`9154ef4` is a *peer's* work ("The DAG's empty background is the way back to the run"), not
this programme. Of the IDE halves:

- **C1 — partially implemented, and the gap is exactly the one named.** `src/page.rs:564`
  reads `action.get("confirm")` and prompts for the page's own buttons. `src/store.rs:476`
  `pub fn perform(...)` has **no** `confirm` handling — so the node inspector, flow markers
  and composers, which route through `perform`, act without a prompt. `grep -n "confirm" src/store.rs` → no hits.
- **C2 — not implemented, and the current code hides the bug.** `src/page.rs:432`
  `may_reload()` is `!loading && last_reload >= 2s`. The store observer at
  `src/page.rs:2060-2072` reloads only `if (in_front || graph.is_some()) && may_reload()`,
  having already done `this.seen_board = generation` — so a bump that arrives mid-fetch, or
  while the tab is in the background, is **dropped, not queued**, and the tab will not reload
  on activation either (`seen_board == generation`). No "stale since hh:mm · Retry" banner.
- **Not implemented at all** (no hits): C7 subprocess timeout (`grep -n "timeout" src/board.rs` → none),
  C24 replay ticker throttle (`grep -n "throttle" src/flow.rs` → none),
  C26 OS notification (`grep -rn "notification" src/status.rs src/title_bar.rs` → none),
  C13's renderer half (`grep -n '"reason"' src/graph.rs` → no hits; only `focus_handle`),
  C14's renderer half (`grep -n "delivered\|Sent to" src/sections.rs` → no hits).
- C11 `label()` exists at `src/page.rs:314` but without the state glyph / title-on-every-path work.

### SDD

Deliverable 1 of `docs/plans/2026-10-03-spec-driven-delivery.md` is literally "deterministic
ingestion library + `mini-ork specs` CLI **(no LLM)**" — the quoted claim is the plan's own text,
and the implementation matches it (`mini_ork/specdir/scan.py` = glob + sha256 + schema check).
The **operator accept/adjust step is prompt-specified but unimplemented**: `prompts/contract-compiler.md:109`
reserves `acknowledged: true` to an operator and `spec_card.py:153` starts every card at
`status: "draft"`, yet nothing writes either. `verifiers/ratification-check.py` is deterministic
and simply fails on any unacknowledged entry — so a spec with a real gap parks forever, and a
clean spec writes `ratification: []` and dispatches with no human ever seeing the compiled contract.

---

## (c) Blocked on

1. **User decision on SDD** — option **A** (build the operator ratification gate between
   `contract_compiler` and `ratification_check`) or **B** (A plus LLM spec-triage at ingest).
   Nothing in the SDD operator surface can be built until this is chosen.
2. **e3v2's diff needs a manual re-apply**, not a plain `git apply`: drop its
   `mini_ork/ide_pages/node.py` (−7) and `tests/unit/test_ide_pages_run.py` (−120) hunks
   (superseded by `d3b52b38`), keep `run_header.py` + `spec.py` + `run.py` +
   `tests/unit/test_ide_pages_run_header.py`.
3. **`run.py` serializes the wave.** e3v2, e6 (C13) and e10 (C22/C23) all rewrite
   `mini_ork/ide_pages/run.py`; they must land one at a time, each rebased on the last.
   e11b touches only `mini_ork/cli/board_cmd.py` and is independent — it can go first.
4. **A live `framework-edit` run holds the run dir** for e3v2 (`owner.json`, 22:25) — do not
   reuse that dir; re-apply into a fresh land worktree.

### Verified green at `main` (`beca73ef`)

```
python3.11 -m pytest -q -p no:cacheprovider tests/unit/test_ide_pages_run.py \
  tests/unit/test_ide_pages_node.py tests/unit/test_features_store.py tests/unit/test_board_cmd.py
→ 155 passed in 143.18s (exit 0)
```

This is the set `fix-main-buddy` was dispatched to make pass; it passes because a peer's
`d3b52b38` landed first — not because this session's run did it. `fix-main-buddy`'s own
outcome was `not_needed`.

### Not verified

- The three IDE renderer halts (C13 `reason`, C14 receipt, C12 badges) were checked only by
  `grep` for the expected field names in `crates/mini_ork_ui/src/`; no Rust build or test was
  run in this audit.
- Whether the untracked `docs/specs/` and `docs/ux-briefs/2026-10-1{5,6}-*.md` in the Zed fork
  are this programme's inputs or a peer's; they are uncommitted and were not opened.

### Unrelated observation (not this session's work)

The primary checkout is **not clean** right now — a peer session has
` M mini_ork/cli/specs.py`, ` M mini_ork/ide_pages/spec.py`, ` M mini_ork/web/routes/ide.py`
staged in the working tree. Nothing in this session wrote to those files; the audit's
`git status` shows them only because the audit ran in the same checkout.

---

## (d) Resolution — 2026-10-17

**P0#4a closed.** All three green-but-unlanded runs are now ancestors of `origin/main`:

```
git merge-base --is-ancestor 51c37434 origin/main   # e6  graph focus        → true
git merge-base --is-ancestor c7363e73 origin/main   # e10 artifacts/ditto   → true
git merge-base --is-ancestor b324fae5 origin/main   # e3v2 header           → true
```

Landing order was serialized on `mini_ork/ide_pages/run.py` as predicted:
e11b (`937506bc`, then the regression fix `d71a28d8`) → e6 (`51c37434`) → e10 (`c7363e73`) → e3v2 (`b324fae5`).

**P0#4b closed.** e3v2 was re-applied surgically, not plainly:

- Excluded `mini_ork/ide_pages/node.py` (−7) and `tests/unit/test_ide_pages_run.py` (−120).
  Both are superseded by `d3b52b38`; the six deleted tests are the run-buddy tests that
  `d3b52b38` had already removed, so applying the deletions again would have been a no-op
  conflict, and omitting them leaves no duplicate coverage.
- `spec.py`, `run_header.py` and the new test file applied clean.
- `run.py` went through `git apply -3`, which produced exactly one conflict: whitespace
  after `_wall()` (ours: a blank line, theirs: none). Resolved to main's two-blank-line spacing.
- Green gate on the land worktree before merge: 255 passed, 1:27.

**Engine halves landed after (d) opened.** Each is a `framework-edit` run whose
diff was independently re-verified before merge:

```
git merge-base --is-ancestor 18ffcae7 origin/main   # C3  triage names the real failing node  → true
git merge-base --is-ancestor fdf55574 origin/main   # C4+C5 one Needs-you lane, explained decisions → true
git merge-base --is-ancestor 7d0ac72a origin/main   # C9+C12 Checks tab + tab badges          → true
git merge-base --is-ancestor a70f9271 origin/main   # C8  Changes tab on the main page         → true
git merge-base --is-ancestor 22c36d27 origin/main   # C25 failed run says whose fault it is    → true
git merge-base --is-ancestor 899107f0 origin/main   # C18+C21 crumbs, worktree sub, Copy summary → true
```

- **C3** (`18ffcae7`) — the run rolled back (`impl_moved_base`, reviewer verdict on an
  earlier out-of-scope round); its final diff was scoped to 3 files and landed after
  an independent 267-test run. `run.py` gained `_awaiting_node_ids`.
- **C4+C5** (`fdf55574`) — one `Needs you` lane (`_attention_lane`) at the top of every
  v2 tab, absorbing the parked review / open gate / cost pause / merge approval / open
  asks, each with its own buttons; a healthy run renders no lane. Gate callouts always
  state strategy, from-node, command and unverified levels, falling back to
  `retry-hint.json`. Landing required a rebase over a peer's `_story_tab` →
  `_outcome_tab` rename and a stale assert in `test_ide_pages_run_header.py` that was
  **already red on main** (fixed to assert header-owned controls are not duplicated).
- **C9+C12** (`7d0ac72a`, run `run-1792233993-49139`, 40m13s, $2.24, reviewer pass) —
  a Checks tab with one plain-words row per level (state, meaning, deciding file), the
  level vector still shipped as data, and per-envelope check rows; tab badges
  (`Checks · 1 ✗` red / proven count green, DAG question count). `verification` is now
  an alias for `checks` — the vector was being drawn on two tabs with two badges.
- **C8** (`a70f9271`) — a `Changes` tab between Checks and Kickoff, reusing
  `run_dock._changes_sections` (never a copy of it) with per-file open actions, and
  an `Open as patch` header action only when the diff file exists. A broken or empty
  diff costs the tab's content, never the page.
- **C25** (`22c36d27`, run `run-1792235591-…`, verdict pass) — a failed run's outcome
  now asks `mini_ork.triage.blame.attribute` and says whose fault it is in one
  sentence; `mini_ork` blame adds a confirm-gated `Open triage --promote` action,
  `consumer` and `unknown` add none. Every read failure falls closed to `unknown`.
- **C18+C21** (`899107f0`, run `run-1792239907-45563`, verdict pass, 321-test
  independent re-run) — header `crumbs` (epic › run › retry #N) with a `do` only where
  navigation exists and never a fabricated crumb; the v2 sub regains the worktree
  path; a `Copy summary` menu entry pastes goal, outcome line, verification vector,
  cost and the `mini-ork://run/<id>` link, each line best-effort.
- **C10** (`5379395a`, run `run-1792243065-98237`, verdict pass, 329-test independent
  re-run) — a `Did it meet the criteria` table under the outcome verdict, one row per
  kickoff criterion marked met / unmet / unknown, decided by the verdict's own
  criterion entries first, then a failed check, then a PROVEN level's evidence; an
  undecided row says `No check names this criterion.` and only decided rows carry a
  click-through. The rows are also stamped on `outcome["criteria"]`. Live check on the
  run's own history: `run-1792243065-98237` renders `2 met · 0 unmet · 0 unknown`.
- **C16** (`8e69e70a`, run `run-1792245136-98759`, verdict pass, 342-test independent
  re-run) — a Spend section under the outcome and on Checks: spent vs the per-run cap
  (the watcher's own resolver), today's spend vs the daily cap, remaining, and a
  projection only for a running run past its first minute; the outcome carries a
  `spend` block for the IDE. The run's first cut monkeypatched `json.dumps` globally
  from a page module — removed before landing; the section is a plain dict.
  Renders for `hr-smoke-b-152611`: `$0.84 of a $1.00 per-run cap · $0.16 left`,
  `Daily $33.56 of a $225.00 cap`, `at this rate, ~$1.68 by the end`.
- **Blame taxonomy** (`5c255d39`) — `impl_moved_base` and `dispatcher-dead` now
  attribute to `mini_ork` (new `harness` evidence rule), so C25's callout offers the
  triage action on a harness failure instead of saying it cannot tell. The harness
  reason outranks a provider signature; artifact verdicts stay `consumer`, infra
  reasons stay `unknown`. 41 + 13 tests, CLI proof pasted in the run log.

**Still open:** the IDE renderer halves (C1, C2, C12-render, C13-render, C14-render, C24,
C26) and engine C17 (roster de-dupe + fallback flag, in flight: `wt/rp-c17`) plus the
remaining C-series items. SDD option A-vs-B remains blocked on the user.

**Incident, same day:** the first C9+C12 dispatch was killed after its `code_impact_lens`
found a deterministic scope conflict — the kickoff forbade editing the two tests that pin
`V2_TABS` and the `"Verification · 1"` label. The kickoff was widened to those exact
assertions and re-dispatched; the second run published green. Lesson: a page-shape change
must name the shape pins it invalidates.

---
