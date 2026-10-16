# mini-ork + mini-ork-ui — aggregated session gaps

**Collected:** 2026-10-16 · **Sources:** 13 sessions filed reports, plus relayed
correspondence from ≈6 more working on the mini-ork engine, the `mini-ork-ui` crate, and the
`zed-mini-ork` fork · **Scope:** mini-ork and its UI only (other workstreams were explicitly
excluded).

Each session self-audited its own assignments — pulling its task list, then re-checking every
status against git, the engine source, and the state DB, **never against the recorded value**.
The per-session files sit beside this one; this document is the synthesis.

> **Reading rule — attested vs receipted.** Sessions were asked to separate what they
> *witnessed this turn* (a command they ran, a ref they resolved) from what they *inherited*
> from an earlier turn. Where a session flagged a claim as inherited, this aggregate keeps that
> flag rather than promoting it to fact. Three sessions explicitly say their "tests green" line
> was not re-run in the audit window (`d5471234` G5, `6881f0ad` G4, `fix-node-status-mismatch`).
> Treat those as attested.

## Coverage

| session | report | what it owns | committed? |
|---|---|---|---|
| `f6e1ed90` (this session) | `f6e1ed90-orchestrator-page-pm.md` | orchestrator rail page PM brief; `not_needed` terminal state; verifier `failure_kind` | untracked |
| `6881f0ad` | `6881f0ad-zed-ui-dag-click.md` | UI DAG zoom + background-click; crate pins; cn-ui epics | untracked |
| `8162bb3f` | `8162bb3f-sdd-p2-recovery.md` | SDD P2 recovery / spec-driven delivery completeness | ✅ `beca73ef` |
| `732bb4cc` | `732bb4cc-author-style-review.md` | author-style-review recipe; deterministic assignment | untracked |
| `83e57620` | `83e57620-failed-run-triage.md` | 48h failed-run triage; regression fix | untracked |
| `86880f3c` | `86880f3c-revive-monitor.md` | revive/monitor path; rubric scoring; recovery | ✅ `f3b52c8d` |
| `d5471234` | `d5471234-orchestrator-always-visible.md` | per-run orchestrator section on the run Overview | ✅ `4c3836c4` (local `main`) |
| `3e207e26` | `pre-run-lane-health-gate.md` | pre-run lane-health gate | untracked |
| `f5671eaa` | `f5671eaa-per-node-sandboxes.md` | per-node sandbox isolation + artifact transport | untracked |
| `da0b8c69` | *(none — relayed inline)* | run-page IA + DAG zoom; crate pin | **plan mode, blocked** |
| `20a62c45` | `20a62c45-work-intake-delivery.md` | work intake / delivery: Linear, Jira, board, threads, attachments | ✅ `60c00154` |
| `mini-ork-cb` | `mini-ork-cb.md` | run delivery: green runs that never landed; C1–C27; SDD ratification | untracked |
| `cb737d8d` | `cb737d8d-sdd-run-revive.md` | SDD run-initiation failure root-cause; revive monitor | ✅ `e13b67f2`, `a401f596`, `76d91e55`, `e0171807` |

**Not reported** (messaged, no reply at collection time): the remaining sessions from the sweep.
The request is idempotent — a late report dropped in this directory is folded in by re-running
the aggregation.

**Excluded as not unambiguously mini-ork/UI** — reported for transparency, not judged:
`setup-health-config-hub` (`7464af`), `do` (Branch) (`82a523`).

---

## The cross-cutting findings — read these first

**1. "Declared but never exercised" is the dominant failure shape.**
Three independent sessions found the same pathology: a mechanism is declared in a contract, a
design doc, or a spec, and **no run has ever produced it**.
- `artifact_contract.yaml:27/30/33` declares three cluster artifacts; the deterministic-assignment
  fix (`0190e322`, on `main`) has **never run end-to-end** — the one successful run's
  `families.json` and `cluster-assignment.json` are absent and its `clusters.json` is the old
  model-authored shape (`732bb4cc` G1).
- `docs/architecture/artifact-transport.md` (`0792dc43`) is a **design + roadmap epic only** —
  `git log -S artifact_transport -- mini_ork/` has no implementing commit, and the only engine
  string match is a pointer comment at `remote/providers/run_nodes.py:20`. Artifacts still move by
  local-worktree relay, so a node has **no home** for a recipe-declared `path` (`f5671eaa` G1).
- 6 of 15 SDD epics have no implementation at their specified paths (`8162bb3f` G1).

This is the highest-value class to close: the contract says the feature exists, so nothing else
in the pipeline flags its absence.

**2. Two different objects are both called "the orchestrator."**
`d5471234` ships the **per-run decision core** (`orchestration/runner_orchestrator.py`) as a row
on the run's Overview tab. `f6e1ed90` audits the **orchestrator rail page** with 8 tabs
(`board page orch --tab <t>`). Different objects, one word — the user's "where is the
orchestrator?" is ambiguous by construction, and the two surfaces will keep being confused until
they are named apart in the UI.

**3. The gates have blind spots that let real defects through.**
Each of these was found *after* the defect shipped:
- **Scoped merge gates cannot catch cross-file test pollution** — the polluter's file passes in
  isolation; only a broad/combined run exposes it. That is exactly how `83e57620`'s regression
  G1 shipped.
- **CI green ticks are head-scoped.** `pre-run-lane-health-gate` verified "CI green" for run
  `38565292891` at commit `38750445` (9/9 success) — but the *current* heads are red again in
  `test_acp_agent_py.py` (3 tests) and `test_sdd_e2e_dryrun.py`. Superseded heads read
  "cancelled," making CI a weak signal on a moving branch.
- **Lane-health degrades to a no-op for native lanes** (opus/sonnet) under
  `MO_LANE_PROBE=0` — the exact "opus had no credit" case — and ambient credential-file login is
  never probed at all, by design (`pre-run-lane-health-gate` G3).
- **Deleted targets are found at verifier time, not preflight.** Six engine runs
  ($8.22 / $4.19 / $3.36 / $1.21 / $1.15 / $1.14) each failed one check because `roots.target` was
  gone (`86880f3c` G5). **Dead lanes fail late too** — kimi 401 and a `deepseek_flash` fallback
  tail dropped from every chain with no `providers.yaml` entry (`86880f3c` G4).

**4. A revived run carries the previous attempt's residue — in the score and in the record.**
- **Rubric contamination (a concrete, ready fix):** `artifact_summary`
  (`gates/rubric_scoring.py:141`, listing at `:166`) hands the grader *every* top-level run-dir
  file, so a revived run's `NEEDS-CHANGE.md` / `rolled-back.json` from the superseded attempt
  score it `pass=false` **after a green publish**. Call site `gates/rubric_prescreen.py:570`.
  `86880f3c` calls this the one concrete engine fix available.
- **A `recover` re-runs the closure root, not the hint's node.** On `run-1792059337-779`
  (hint `from_node=test`) the retry re-ran *typecheck* (fresh `verifier_typecheck.log` 23:06:06,
  rc=0) while `retry-exhausted/test.attempts.json` kept its 13:00:09 mtime — and the reviewer
  escalated on the **stale rc=77**. Exit 0, a real LLM verdict, the wrong node (`86880f3c` G2,
  candidate defect, not proven).
- **Observability:** a revived row keeps the *first* attempt's `ended_at`/`cost_usd`
  (`779`: `ended_at` 2026-10-15 13:04:07, cost 6.73 after two revives) — `86880f3c` G3.

**5. The UI cannot render what the engine now emits.**
- The `not_needed` terminal state is **engine-side only** — the Rust client's `Counts`
  (`mini-ork-ui/src/board.rs`) still has 4 tiles, so the UI cannot *show* a not-needed run
  (`f6e1ed90` G4).
- The **serve fast path lacks `MINI_ORK_IDE_SPEC`** (`grep -c` → 0 in `ide_http.rs`, 2 in
  `board.rs`), so the run page is served at **level 1** (retired v1 tabs DAG/Kickoff/Overview)
  while the subprocess path serves level 2. Verified live: `curl :7090` returned s1 code
  `'Detail'` (`da0b8c69` G3). This is the highest-value UI gap.
- **No IDE page renders any SDD mechanism** (`8162bb3f` G2).
- The orchestrator's **"Armed" predicate is `enabled()` alone**, but the wiring exists only on
  the dependency-graph `_parallel` path (`execute.py:1163`). On unwired `_parallel` call sites the
  layer is *enabled but structurally cannot act*, yet the UI prints "Armed — no interventions this
  run," implying a choice (`d5471234` G2).

**6. Things land shadow / default-OFF.**
Nine landed epics ship with their probe disabled (`MO_PROBE_VALIDITY=shadow`, etc.) — `8162bb3f`
G3. Combined with finding 1, a shipped-but-shadowed feature is indistinguishable from an
unimplemented one from the outside.

**7. "Verified on a scoped gate" ≠ verified.**
Named repeatedly: the full `tests/unit` timed out at 48:50 with no result (`6881f0ad` G6);
`dd15418f` merged on a scoped 328-passed/1-skipped gate, not the suite (`f5671eaa` G3);
audit turns citing inherited green claims (`d5471234` G5, `6881f0ad` G4). The scoped-gate class
in finding 3 is the same failure seen from the other side.

---

## Ranked gap list

### P0 — correctness or cost, fix available now
| # | gap | where | source |
|---|---|---|---|
| 1 | Rubric grader ingests superseded attempts' files → green publish scores `pass=false` | `gates/rubric_scoring.py:141,166`; `rubric_prescreen.py:570` | `86880f3c` G1 |
| 2 | Run page served at level 1 on the serve fast path (retired tabs) while subprocess serves level 2 | `crates/mini_ork_ui/src/ide_http.rs` (no `MINI_ORK_IDE_SPEC`) | `da0b8c69` G3 — **fixed in the working tree, uncommitted** (round 2) |
| 3 | `recover` re-runs the closure root, not the hint's node → escalation on a stale rc | recovery path; evidence in `run-1792059337-779` | `86880f3c` G2 |
| 4 | Deleted targets + dead lanes discovered at verifier time, not preflight (≈$17 and a blocked `deepseek_flash` tail) | preflight / `providers.yaml` | `86880f3c` G5, G4 |
| 4a | **Three green runs never landed** — `rp-e3-header-v2` / `rp-e6-graph-focus` / `rp-e10-artifacts-learnings` carry `verdict.json` `pass:true` and diffs that still apply, yet none is on `main`. A board `done / step=publisher` does **not** imply a commit | `framework-edit`'s deliverable *is* the diff in the run dir | `mini-ork-cb` G1 |
| 4b | `e3v2` reads `done` on the board but `ide_pages/run_header.py` exists in no commit; `git apply --check` fails at `node.py:55` (superseded by `d3b52b38`) — needs a surgical re-apply, not a plain apply | run dir; `node.py:55` | `mini-ork-cb` G2 |
| 4c | **A verifier whose sidecar omits `verifier` is unreadable**, so the page reports "the cause was not classified" over a verdict sitting on disk | `mini_ork/verify/levels.py` `read_verifier_payload` | `run-page-ux-clarity` G-new (round 3) |
| 4d | **The abstain mechanism can almost never fire: 123 of 124 verifier scripts never emit `failure_kind`** — the runner is fixed and tested, but nothing in production hands it the input it keys on | `mini_ork/verify/pm.py:182` (`classify_spawn`); `recipes/recursive-validate-impl/verifiers/tier1-compile-typecheck.py:172` | new — round 6; fix is **one shared writer, not 123 patches** (addendum below) |

### P1 — declared but never exercised, or emitted but unrenderable
| # | gap | where | source |
|---|---|---|---|
| 5 | Deterministic-assignment fix never ran end-to-end; 3 declared cluster artifacts never produced | `0190e322`; `artifact_contract.yaml:27,30,33` | `732bb4cc` G1 — **blocked on the user**, not on work (round 5) |
| 6 | Artifact transport is a design doc only; node has no home for a recipe-declared `path` | `docs/architecture/artifact-transport.md`; `run_nodes.py:20` | `f5671eaa` G1 |
| 7 | 6 of 15 SDD epics unimplemented; no IDE page renders any SDD mechanism | SDD paths I2/I4/I6/I7/I8/I9 | `8162bb3f` G1, G2 |
| 8 | `not_needed` 5th bucket missing from the Rust board `Counts` (+ pin bump) | `mini-ork-ui/src/board.rs` | `f6e1ed90` G4 |
| 9 | Orchestrator "Armed" over-promises on unwired `_parallel` paths | `execute.py:1163`; run Overview section | `d5471234` G2 |
| 10 | Nine epics ship shadow / default-OFF | `MO_PROBE_VALIDITY=shadow`, etc. | `8162bb3f` G3 |
| 11 | Lane-health no-ops for native lanes under `MO_LANE_PROBE=0` | pre-run gate | `3e207e26` G3 |

### P2 — hygiene, evidence, and drift
| # | gap | source |
|---|---|---|
| 12 | Revived rows keep the first attempt's `ended_at`/`cost_usd` | `86880f3c` G3 |
| 13 | Isolation boots N+1 sandboxes (6 for 5 nodes) — disclosed, not a bug, but contradicts "one node per sandbox" | `f5671eaa` G2 — **DELIVERED** `d0fd901f` (round 2) |
| 14 | Epic-runner reflector wired `depends_on` where the plan specified `escalates_to` → wrong runtime semantics | `6881f0ad` G2 — **NOT A GAP, closed** (round 2) |
| 15 | No stage-report writer: the `<run>/buddy/` feed's writer was removed (`fae2a7a5`), leaving nothing to render | `f6e1ed90` G2 |
| 16 | Orchestrator section is per-run and `LIMIT 6`; no cross-run view | `d5471234` G4 |
| 17 | `MO_BUDDY_*` silently ignored after the rename to `MO_WATCH_*` | `f6e1ed90` G3 |
| 18 | Dead scaffolding / stale worktrees: `_ide-engine`, `author-style-review` + `wt/author-style-review` | `d5471234` G3, `732bb4cc` G4 — **`author-style-review` half DONE**; `_ide-engine` open (round 5) |
| 19 | Untracked deliverables at risk: `git stash -u` strands run deliverables; 244 KB report + 1.2 MB corpus in gitignored `runs/`; tests live in `/tmp` outside CI | `8162bb3f` G4, `732bb4cc` G2, G3 — **`/tmp`-tests half DELIVERED `effc3881`** (round 5) |
| 20 | Doc drift: `docs/todos/2026-10-14-failed-runs-48h-triage.md:375-376` says a run "still reads executing"; the DB says `published` | `83e57620` G4 |
| 21 | Stale plan/record rows: `run-graph-plan` still `pending` while its three children are `completed` and verified | `6881f0ad` G3 — **CLOSED, wrong store** (round 2) |
| 22 | Worktree run resolves a bare home → the level-2 spec path is untested there | `da0b8c69` G3 (mechanism) — **not actionable as written** (round 2) |

### Fully un-started
- ~~**Linear/Jira integration from the UI** — no code, no branch.~~ **RETRACTED — this was already
  shipped; see Corrections.** Ten commits on `origin/main` implement it (`20a62c45`).
- **Run-page IA work** (4 tasks: spec level on the fast path, promote "Why — evidence", collapsed
  Diagnostics, empty-section vanish + tab badges) — was "verified 0%, plan file only"; **now
  implemented, uncommitted** (`run-page-ux-clarity`, round 3): `run.py`/`outcome.py` promote
  `Why — evidence` to second on the story page and build the collapsed `Diagnostics` section from
  `story_step(open=False)` + `block_lines` so **no renderer change is needed** (`render_story`
  already keys expansion off `self.expanded`); empties return `None`/`[]` instead of "No … recorded"
  cards; counts move onto the tab labels. Plan mode lifted; the audit file is being written.
- **`cn-ui-field-map`** — blocked on ContextNest `/field/basins`, which does not exist
  (`6881f0ad`).

---

## Contradictions found — and how they resolved

**Crate tip `9154ef4` "unpushed" vs "pushed" — RESOLVED: pushed.**
`da0b8c69` reported (severity: highest) that the superproject `e88610c48e` pins submodule
`9154ef4` while `git log origin/main..main` in the crate listed it as unpushed, so a fresh clone
could not check out the pinned sha. Two other reports (`6881f0ad`, `d5471234`) and a **direct
check in this aggregation** disagree:
```
crate origin/main                  → 9154ef4
crate git log origin/main..main    → (empty)
submodule checkout log             → 9154ef4, clean
superproject ls-tree HEAD          → 160000 commit 9154ef4
```
The claim was **true when observed and closed** by `6881f0ad`'s push (`0aefe69..9154ef4`) later
the same day. Not outstanding. The general hazard it names — a peer committing in one crate
checkout while another commits elsewhere — remains real; `da0b8c69` noted three UI checkouts in
play.

**`push-ui-919178b7` "stranded" — NON-ISSUE.** `919178b7` is an ancestor of crate HEAD `9154ef4`,
which is on the crate's `origin/main` and pinned by mini-ork `63fc80a0` (`d5471234`).

**`ff-local-main` "diverged, FF refused" — STALE.** Remote advanced to `63fc80a0`; local `main` is
0 behind, ≥1 ahead (`d5471234`). The pre-compaction record was superseded.

**`828252928`-run status "still executing" — STALE.** DB returns `published` (`83e57620` G4).

---

## Blocked on the user

**Pixel confirmation (4 sessions, independently, for 4 different surfaces).** No tool call can
assert a rendered GPUI row; each needs the IDE launched:
1. Run → Overview: the `Orchestrator — armed` section should sit under "Why? — evidence"
   (`d5471234`).
2. The Orchestrator rail entry and its 8 tabs — the CLI half already ran `errors: {}`; the GUI
   half is the spec's one open `[~]` item (`f6e1ed90` G1).
3. Click the empty DAG background → returns to the run main page (`6881f0ad`).
4. Run-page IA + zoom behaviour (`da0b8c69`).

**Push local `main`.** It now carries audit commits from several sessions (`beca73ef`, `f3b52c8d`,
`4c3836c4`) — not any one session's to push (`d5471234`, `mini-ork-df`).

**Credentials / config.**
- kimi lane returns 401, making bare `mini-ork validate` exit non-zero (`732bb4cc`, `86880f3c`).
- `deepseek_flash` fallback tail has no `providers.yaml` entry (`86880f3c` G4).

**Decisions.**
- Priority among WS-2..WS-11 of the orchestrator-page PM brief (`f6e1ed90`).
- Linear/Jira credentials **and** the secure-vs-simple tier choice (`f6e1ed90` G5).
- Remove the leftover `_ide-engine` scaffolding or keep it deliberately (`d5471234` G3).
- The still-open `ram-sentinel` question (`86880f3c`).
- Whether the one-line doc fix (P2 #20) is worth a merge cycle (`83e57620`).

## Verified-done (so they are *not* re-opened)

Engine: `2f7e4b81` verifier `failure_kind` abstain (`f6e1ed90`); `d70f94cf` `run_status` /
`not_needed` (`f6e1ed90`, `86880f3c`); `ae1307d9` orchestrator-page test refresh (`f6e1ed90`);
`b3c80bb5` + `19e74c72` lane-health gate (`3e207e26`); `8f00468b`, `9ee820c1`, `16f11413`
revive-path commits (`86880f3c`); `e0e99542`, `59f14447`, `778a0686`, `2d786fec`, `24893d48`,
`6801221c` failed-run triage (`83e57620`); `dd15418f` per-node isolation (`f5671eaa`);
`9ff1fea8` + `64251073` orchestrator section (`d5471234`); `0918cf4f` + `f950a3b3`
author-style-review (`732bb4cc`); `6f60a89c`, `e76c13ab` SDE fixes (`f5671eaa`); `9154ef4` +
`d00229f` crate DAG work (`6881f0ad`, `da0b8c69`).
UI: `9154ef4` DAG zoom + background click, `cargo test -p mini_ork_ui --lib` → 46 passed
(`6881f0ad`); crate pins `63fc80a0` (mini-ork) and `e88610c48e` (zed fork, pushed to `fork`).
Intake / integrations: `4379ff03` (Linear), `8448f6bc` (Jira), `d28c2b0f` + `da2d3834` (board),
`f4d5bb75` + `012d3fe9` (attachments), `1be093e8` (write-back), `d29b632b` (report-back),
`991a8f59` (Slack), `7d09f02c` (Discord) — every one an ancestor of `origin/main` (`20a62c45`).

**Corrected by its own author, do not list:** `83e57620` retracts its prior F841 / `_wall()`
gap — `ruff check --select F,E9` is clean and `run.py:831-835` is complete on `origin/main`.

---

## Corrections to this document

**C-1 — The "Linear/Jira un-started" claim was FALSE. Retracted.**
I wrote it from an earlier grep that returned nothing and stated it as verified. The code had
already shipped. Re-verified by `git merge-base --is-ancestor` on all ten commits (now listed under
Verified-done); `20a62c45` caught it. This is precisely the failure class the aggregate's own §7
warns about — I asserted a *negative* from one search instead of a *positive* check. The corrected
"Fully un-started" list is now just: run-page IA (`da0b8c69` G4) and `cn-ui-field-map`.

**C-2 — "local `main` is N ahead" lines are time-stamped, not live.**
Local `main` moved three times during the sweep (`0abc587d` → `95a4af77` → `60c00154`). Re-measure
at publication time; do not quote any divergence figure from this document.

**C-3 — My "second defect in `read_verifier_payload`" was FALSE. Retracted.**
I read a docstring against a loop and asserted a code bug from the mismatch, without reading the
iterator the loop runs over. `_iter_json_objects` walks **bottom-up**, so first-match and
last-object are the same thing and nothing was broken. I published this to `origin/main` in round 3
before checking it, which is the same error class as C-1: a *negative* about code asserted from one
reading instead of a positive check. Caught by `run-page-ux-clarity`, who owns the function and
checked. The retraction is in the body, not only here.

**C-4 — the reported "57 of 117" was an estimate, not a count.** The same session measured it after
the fact: **62 of 123**, matching this document's independent count. It corrected its own figure and
flagged it as a self-report rather than fixing it silently — worth recording because the failure
mode is the one the session was *reporting*: a plausible number, asserted, never measured.

## Late additions (reports that arrived after the first pass)

**Delivery integrity — a whole class** (`mini-ork-cb` G1, now P0 #4a): three runs with
`verdict.json` `pass:true` and diffs that still apply, none on `main`. Root cause: for
`framework-edit` the deliverable *is* the diff in the run dir, so a green board row never implies a
commit. Strongest argument for extending the two-tier rule to runs — "done" must mean **landed**,
not merely **judged**.

**`C1` half-done, `C2`'s bug is silent** (`mini-ork-cb` G4): 0 of 27 implemented. `C1` is *half* —
`page.rs:564` honours `confirm` but `src/store.rs:476` `perform` does not, so the node inspector
and flow markers still act unconfirmed. `C2` is hidden: `page.rs:2060-2072` advances `seen_board`
then reloads only if `may_reload()`, so background/mid-fetch bumps are **dropped, not queued**, and
activation triggers no reload either.

**SDD operator ratification is prompt-specified and unimplemented** (`mini-ork-cb` G5):
`prompts/contract-compiler.md:109` reserves `acknowledged:true` to an operator, `spec_card.py:153`
starts every card `draft`, and nothing writes either. `mini-ork specs` has `ingest|list|lint` only —
a spec with a real gap parks at `ratification_check` forever; a clean one dispatches with **no human
ever seeing the compiled contract**. Needs a user decision: option A (operator ratification gate)
vs B (A + LLM spec-triage at ingest).

**The consolidated board shipped as code but its epic was never written** (`20a62c45` G2): one
`buckets` map filled by both `_epic_buckets` and `_tickets_into` (`kanban.py:5-6,523,582`, with
`_fair_share:333`, `_source_actions:499`) — the behaviour is real, the requested artefact is
missing.

**The pm mirror is empty by design** (`20a62c45` G3): live `.mini-ork/state.db` → `pm_issues: 0`,
`threads: 3`, every external source `enabled: false`. The board correctly renders "add a source to
config/pm.yaml". Blocked on the operator, not on code.

**`:7090` runs the wrong engine AND the wrong home — TWO fixes, not one**
(`20a62c45` G4, corrected by that session after investigation; corroborates `f6e1ed90` G4 and
`da0b8c69` G3). This document first called it "the single highest-leverage one-action fix". That
was wrong, and a restart taken from it would appear to fail. The two defects:

1. Listener PID **81516**, started **Oct 15 14:17:53**, predates every shipped commit — the
   delivered work is invisible.
2. **Its default home is `/tmp`.** The parent (`81476`) was launched as
   `bin/mini-ork serve --port 7090 --home /private/tmp/mo-smoke50/.mini-ork` — a temp smoke home
   whose `state.db` has no `epics` table. Confirmed here from the process argv. Any page read that
   omits `X-Mini-Ork-Home` is answered out of that scratch project and renders an **empty board**;
   the header *is* honoured, the **default** is what's broken.

**Correct action: `kill 81516`, then relaunch WITHOUT `--home /private/tmp/...`.** Repeating the
flag reproduces the empty board exactly. Note also that the restart is the user's server and their
call — `mini-ork-81` correctly declined to take it on a peer's say-so, and so should any session
that reads this.

---

## Delivery — round 1 (delegation)

Delegated by fanning each gap back to the session that audited it. Status at close:

**Delivered**
- **P2#20** doc drift → `a6ef9ff2` on `origin/main` (`deliver-mini-ork-tasks`). Gated on a real
  check (every changed path `.md`), not a trivially-true one.
- **P2#17** stale `MO_BUDDY_*` → committed **locally** as `5e668ebb` (not pushed — see below).
  Warn-once per variable, through the single funnel both knob readers already share. Verified:
  `MO_BUDDY_STALL_SECONDS=42` warns once *and* still yields the default 300; second call silent;
  40 watch tests pass; ruff clean.
- **P1#7 + P1#10 (partial)** → `kickoffs/sdd-mechanisms/DECISIONS.md` @ `fc2c1d91`
  (`mini-ork-39`): all 15 epics listed with declared path + verified status (9 landed, 6 missing),
  and every landed epic now carries a recorded keep-shadow / shadow / unconditional decision
  rather than a default.
- **Consolidated-board epic** → `docs/epics/EPIC-consolidated-ticket-board.md` @ `16642bc3`
  (`mini-ork-81`): documents shipped behaviour only — the single `buckets` map (`kanban.py:648`)
  both `_epic_buckets:523` and `_tickets_into:582` fill, `_fair_share:333`, the no-network reader
  rule, the Sources pane — with 8 acceptance criteria each naming the file it is checkable
  against, and the user's open "alongside, not replacing" decision recorded. Gate: 50 passed.

**Blocked on the user — routed, not fired**
- **P1#7 epic implementation** (I2/I4/I6/I8 dependency-ready; I7 waits on I8, I9 on I4).
  `mini-ork-39` declined to launch and gave three reasons, all correct: (1) the mini-ork contract
  forbids launching the scheduler from an agent turn — it is reaped at the turn boundary, so it
  must run under tmux/launchd/CI; (2) the SDD epics are **not ingested** (`mini-ork epics list` →
  96 epics, zero `sdd-*`); (3) a bare `scheduler` drains **every** ready epic — 19 ready across
  unrelated programmes (`libwit-se-*`, `rlm-*`, `ai-skill-*`, `frc-*`), so an unscoped run would
  spend on the wrong work. Correct path: `epics ingest kickoffs/sdd-mechanisms/roadmap.md`, then
  dispatch only `sdd` ids in a scoped loop under tmux. **A supervised, budget-spending run —
  needs the user's go/no-go.**
- **P1#8** (5th board bucket in the Rust `Counts`) — **held, not skipped**: `board.rs` is dirty in a
  peer's crate checkout, so adding the field concurrently would collide with their uncommitted work.
  Release once their edit lands.
- **Run-page IA** (`da0b8c69`) — delegated to `run-page-ux-clarity` **conditionally**: that session
  is in plan mode and cannot edit until its user approves.
- **P0#2** (`ide_http.rs` spec level) — in flight by a peer; `MINI_ORK_IDE_SPEC` is now present and
  the file is dirty. Deliberately not duplicated.

**Reclassified — not a gap**
- **P2#15** (no stage-report writer) — the writer was removed **deliberately** (`fae2a7a5`, "the run
  buddy is gone"; `watch.py:21` records the move out of the deleted `run_buddy.py`). Building one
  would resurrect a deliberately-removed mechanism. It is a **decision for the user**, not a
  deliverable — delegating it as written would have been a regression.

**New process finding — how changes land on `origin/main`**
`deliver-mini-ork-tasks` reported local `main` diverged, an `--ff-only` refresh refusing, and
concluded "origin/main is a curated mirror, so local main must never be pushed." Re-checked at
close: divergence is `0/0` and `fc2c1d91` **is** on origin — a transient state, not a policy;
`origin/main` does receive session pushes (that same session pushed `a6ef9ff2`). The durable rule
underneath it is the one that matters: **push a commit built on `origin/main`, never the primary's
local `main` verbatim**, because the primary can carry local-only commits. This aggregation pushed
twice from the primary (`f8b0f3e0`); both times it published only docs/spec commits and the
affected peer consented after the fact, but the method was the risky one. Land from a worktree of
`origin/main`.

---

## Delivery — round 2 (delegation, continued)

**Closed without code**
- **P2#14** (reflector wired `depends_on`, not the plan's `escalates_to`) — **not a gap.**
  `6881f0ad` reported the deviation; `fix-run-graph-edges` checked the in-repo rationale
  (`recipes/epic-runner/workflow.yaml:72-76`, committed `1ab7ad14`) *against the compiler* rather
  than accepting it: `workflow/compiler.py:405` adds a source to `parents[target]` only when
  `not recursive and edge_type not in {"escalates_to", "retries"}`, so an `escalates_to`-only node
  lands with **no control parents** and is schedulable before its predecessor ran; and `:498-501`
  makes `escalates_to` a live loop only with a `recursion:` block, which epic-runner lacks
  (`max_self_correction_iterations: 0`). The plan's edge would have been **inert and parentless**.
  `depends_on` is a correction to the plan, not a regression.
- **P2#21** (`run-graph-plan` stale `pending`) — **wrong store.** It is a ContextNest fragment
  (`cn-fed07f7369f13ad3ad4bb3d9ac049260`, `task_id=run-graph-plan`, `kind=todo`,
  `session_id=6881f0ad`, emitted `2026-10-16T10:44:07Z`), which is why `sqlite3 .mini-ork/state.db`
  found nothing. ContextNest dedupes `tasks[]` by `task_id` keeping the last status, so the stale
  row closes by emitting a later `completed` from the owning session — no HTTP write, no DB
  mutation. **The lesson generalises: two task stores were being read as one.**

**Delivered**
- **P2#13** → `d0fd901f` on `origin/main` (`mini-ork-c4`): `docs/architecture/per-node-sandboxes.md`
  now opens with an explicit "N, or N+1?" paragraph (N node sandboxes + one run-level; the extra is
  the process with no node to name, which returns `""` from `run_nodes.isolated_node`), so the top
  framing matches the Cost/Verified sections. The same commit fixed a dead anchor
  (`artifact-transport.md` cited `remote.py:1758`; the real sites are `:1959`/`:2022`) and added a
  **"Status: what ships today (verified 2026-10-16)"** section to that doc — it is a design, not
  shipped behaviour, with what *is* enforced tabulated (absolute outputs yes; relative outputs
  skipped at `execute.py:2153`; `_required_artifacts_ok` at `:2126`) and the two live recipes that
  declare relative outputs named (`bug-audit-fe-be`, `blog-cohesion`).

**Half-delivered, half the user's — and correctly not decided by an agent**
- **P1#6** (a recipe-declared `path` has no home) → the *document* is now honest about the state
  (`d0fd901f`), which needed no decision. The node-side home was **declined**: it resolves a fork
  the user has an open, unanswered question on — cross-node artifacts through a cloud shared
  filesystem (their stated preference) versus the local relay with a surviving `path` contract.
  `mini-ork-c4` refused to pick a branch *and* refused to delete the promise from the contract,
  because removing it forecloses the branch the user may choose. That is the right call: an agent
  guessing here would silently answer the user's question with trivia.

**Not actionable as written**
- **P2#22** — `mini-ork-c4` cannot turn "a worktree run resolves a bare home, so the level-2 spec
  path is untested there" into an artifact or a test. It needs the specific spec path and the
  specific run.

**Status changes in the tree, uncommitted**
- **P0#2** (`ide_http.rs` missing `MINI_ORK_IDE_SPEC`) is **fixed in the working tree** but not
  committed: `src/board.rs` gains a single `pub const IDE_SPEC: &str = "2"` and `src/ide_http.rs`
  both sets it on a server it starts and sends it **per request** (`spec=`), with the reason in the
  comment — a server already listening was started by someone else and defaults to level 1. One
  constant, two read paths, because two literals is what produced the divergence.
  **This is in-flight peer work, not this session's** — `fix-run-graph-edges` attributed the two
  dirty files to me; the diff says P0#2. It also means **P1#8** (the 5th `Counts` bucket in
  `board.rs`) is still colliding with an uncommitted file, so it stays held.

**A peer refusing the right thing, again**
- `fix-run-graph-edges` **stopped coordinate-clicking** after its synthetic clicks kept landing in
  the user's live Alacritty terminal ("window Alacritty of application process alacritty"), where a
  second agent session runs. Guessing screen coordinates over someone's live shell is not a check
  worth running, and it handed the verification back rather than continue. Recorded because it is
  the same judgement as the `:7090` restart refusal and the P1#6 refusal: the tool *can* do a
  thing, and the right answer is still no.
- It also established *why* the DAG-click cannot be observed here — and, after a correction from
  that session, the reason is **dated to the running process, not asserted of the code**: on pid
  **81516** (started **Oct 15 14:17:53**) both `spec=1` and `spec=2` answer the **v1** tab set
  (`key=run` → `[dag, kickoff, overview, agents, learnings, artifacts]`), and v1 leads with `dag`,
  so `goto_main_tab()` finds itself already at `tabs[0]` and early-returns. That is *explained by*
  the process predating the spec plumbing — whose last commit is `6f15537d` at 2026-10-16 14:54,
  about **24h after** the process started — rather than being a property of the engine. The
  distinction matters: a future reader told "the engine serves v1" would hunt a handler bug the
  restart will very likely vaporise. So: **unobservable on that process**, re-check after the
  restart. Same caveat applies to **P0#2** — its `IDE_SPEC` + per-request `spec=` is a *client* half;
  until the server side is restarted, no `spec=2` request exercises the new path end to end, so any
  "P0#2 verified live" claim needs the restart in the receipt.

**Second witness for the `:7090` home defect (falsifiable in one command)**
- `mini-ork-81` offered an independent, replayable receipt: the same page at the same route differs
  **18× in bytes** by header alone —
  `curl -s ':7090/ide/page?key=kanban&tab=tasks'` → **931 bytes**, one item: *"No epics table —
  initialise the project (`mini-ork init`) to see the Tasks column here."*; with
  `-H 'X-Mini-Ork-Home: /Volumes/docker-ssd/ps/mini-ork/.mini-ork'` → **17343 bytes**, the four
  Tasks columns. Both HTTP 200, both valid JSON. That is the empty board the argv predicts.
  It also fixes this document's route: the page is `/ide/page?key=kanban&tab=tasks` — the key is
  **`kanban`**, not `board` (`ide_pages/__init__.py:23`).

**New reports folded in (arrived after round 1)**
- **`cb737d8d`** — SDD run-initiation root-cause + revive monitor. Five specs / 36 ACs carry
  **zero** executable fences → `gates-materialize` emits one `NO_EXECUTABLE_FENCE` per AC →
  `test-validity` fails → 8 failed nodes; the recovery loop is non-convergent by construction (the
  replanner writes SpecCards mutating `acceptance[ACn].gate.probe`, which `gates-materialize` never
  reads — it rebuilds probes from the spec's own bash fences). Landed and on `origin/main`:
  `e13b67f2` (`NO_EXECUTABLE_FENCE` WARNING), `a401f596` (the `# ACn precondition` marker could
  never fire — an injected `set -euo pipefail` header meant the author's marker was never on line
  0), `e0171807` (`specs --help` codes line now derived from `SEVERITY`, the literal having gone
  stale the moment `e13b67f2` added a code), `76d91e55` (decision-record addendum recording that
  lint did *not* close the guarantee the record claimed). **Open:** all five specs still have 0
  fence headers — that authoring *is* the unblock; two ACs (`codefix-classifier-coverage.md` AC8,
  `specdir-kickoff-precedence.md` AC7) cite a `tests/unit/test_classify.py` that does not exist,
  and AC1 duplicates AC8 verbatim. **Blocked on the user for one thing:** which existing test
  proves which AC — guessing writes `precondition` tags that fail the gate by construction.
- **`mini-ork-c4`** — P2#13/P1#6 above, plus a merge-discipline flag: the primary checkout is
  **diverged, not stale** (1 ahead / 1 behind `origin/main`, HEAD moving under it while sessions
  work, siblings' live edits in the tree — `rubric_scoring.py`, `ide_pages/*`, `verify/levels.py`).
  `--ff-only` correctly refuses. Per merge discipline that means *stop*, not reconcile — and any
  session told to "just FF the primary" will fail until whoever owns those commits lands them.
  None of its work is stranded: it is all on `origin/main`.

---

## Delivery — round 3

**P0 4c — a verifier whose sidecar the reader cannot name-match (`run-page-ux-clarity`).**
`mini_ork/verify/levels.py::read_verifier_payload` returns *"the LAST JSON object in `path` whose
`verifier` field equals `verifier`"* — so a sidecar that never writes that field can never be
matched. **Verified here, independently of the reporting session's numbers:** in
`recipes/*/verifiers/` there are **123** scripts and only **62** contain a `verifier` key at all, so
for the other 61 the payload is `None`, `_first_failed_node` comes back empty, `_case_unknown`
fires, and the page tells the user *"Failed at ?; the cause was not classified"* for a run whose
verdict JSON is on disk. On `run-1792151796-92942` the cause was recoverable and specific:
`gates_materialize`, kind `code`.

**A second claim of mine about this function was REFUTED by its author — see C-3.** I read the
docstring ("the LAST JSON object …") against a loop that `return`s on the first match and called it
a bug. It is not one: `_iter_json_objects` (`levels.py:81`) walks the file **bottom-up**
(`for i in range(len(lines) - 1, -1, -1)`), so the first object it yields *is* the last blob in the
file, and returning on that match is exactly the docstring's promise — a retry appends its verdict
last. `levels.py:206` corroborates independently: *"Scan bottom-up like
:func:`read_verifier_payload`"*. The docstring still read as a bug to anyone who cannot tell the
iterator is reversed, so the prose was worth fixing; no behaviour was wrong.

The sharper criticism is `run-page-ux-clarity`'s, and it is right: name-matching a field that two
thirds of writers omit is **the wrong lookup in the first place** — the file is named
`verifier_<stem>.json`, which already says who wrote it. The landed fallback covers the 62; the
primary reader still cannot distinguish a *missing* verdict from a *malformed* one.

Fix in the tree (uncommitted): `read_single_verifier_payload` as a fallback — it returns the file's
one JSON object, or `None` when it holds none or several, because `_iter_json_objects` yields a
single-line file's object twice, so counting occurrences is wrong and comparing by value is what
works. Plus `HINT_VERSION = 4 → 5`, since `load_or_compute` serves `retry-hint.json` only on a
version match and **mtime cannot date a stale hint** — a version bump is the only way to retire one.
Gate: `test_retry_hint.py` + `test_verify_levels.py` → 66 passed.

**New cross-cutting class — a withheld publish record is indistinguishable from a real failure.**
`remove-run-buddy-orchestrator` swept both homes: 28 revive candidates in 48h, **0 revivable**. Three
of them (`run-1792157583-93406`, `run-1792059337-779`, `run-1792045839-99014`) read `failed` with a
run-level `verdict: pass`, their deliverables **committed on their target branches** (`b100b691a`,
`ac8122d05`, `fc228bca7`), and only the publish record withheld by the `publish-unattributable`
gate. The gate cannot, by itself, distinguish an already-applied patch from a foreign edit.

> **Discriminator before calling it a code defect.** Read each target's **reflog** for the three
> SHAs. If a reflog shows the commit landing and then silence, the gate genuinely cannot tell the
> two apart and that *is* the defect. If instead the run's side of the reflog is quiet, the known
> cause is different — a sidecar that was rotated or moved — which the same gate reports the same
> way. Same symptom, different fix, and the reflog is what separates them. Recorded as a candidate
> with that check attached, not as a verdict.

**Finding — the `2026-10-16-run-initiation` specs do not share a target repo.**
`remove-run-buddy-orchestrator` asked which `main`/remote the five untracked specs land on, and
would not guess. The answer is not one answer. Read from each spec's own cited paths:

| spec | cites | belongs to |
|---|---|---|
| `crate/pick-files-directories.md` | `crates/mini_ork_ui/`, `script/clippy` | **zed-mini-ork fork** |
| `engine/codefix-classifier-coverage.md`, `engine/specdir-kickoff-precedence.md` | `mini_ork/cli/classify.py`, `tests/unit/test_classify*.py` | **engine** |
| `engine/board-preflight.md` | `tests/unit/test_board_cmd.py`, `test_board_preflight.py` | **engine** |
| `engine-phase2/dispatch-preflight-sheet.md` | `mini_ork/ide_pages/orch.py`, `tests/fixtures/sdd_e2e/specs` | **engine** |

So four of the five are engine specs sitting in the fork's tree. Remotes, verified in the checkout:
zed-mini-ork's HEAD is `mini-ork`, upstream `fork/mini-ork`, and `origin` is
`git@github.com:zed-industries/zed` — **upstream Zed, never pushed**; fork work goes to `fork` on
`mini-ork`. The engine repo lands on `origin/main` as `git push origin HEAD:main` **from a worktree
of `origin/main`**, never the primary's local `main` verbatim. Placement flag: the engine repo has
`specs/` at its root and **no** `docs/specs/` — settle the layout before creating the tree.

**The precondition question is mechanical, the consequence is the user's.** For
`crate/pick-files-directories.md`, AC1/AC4/AC5 already pass on the untouched tree, so the gate's own
rule (*"a probe tagged `precondition` must pass now; any other probe that passes now is vacuous and
fails the gate"*) makes tagging them `precondition` the sanctioned mechanism rather than a judgement
call. What is *not* mechanical is what it means: that spec's gate would then prove exactly one
property. Recommendation given — tag them, and write the consequence **into the spec**, so a later
reader cannot mistake a green gate for five proven ACs. AC3 (a before/after clippy comparison that
is not expressible as a single command) should be left explicitly unresolved rather than forced.

**Attribution correction.** `fix-run-graph-edges` reported the two dirty crate files as mine;
`run-page-ux-clarity` reports them as its own — and they are (`IDE_SPEC` + the per-request `spec=`
above). Three sessions, two wrong guesses, neither about the right party. The diff never lied.

**Round 2 was pushed by a peer, not by this session.** `mini-ork-81` pushed `93ec6448` to
`origin/main` and gave its reason: the commit sat in the **primary checkout**, leaving it 1 ahead and
blocking the next fast-forward for everyone. The commit is docs-only and additive (three files under
this directory; `git show --stat` confirms no engine paths), so the blast radius was nil. It also
cited a rule — *"Commit, merge, push — without being asked"* — as living in the user's `CLAUDE.md`.
**I said that line was in no file I could read. That was WRONG, and the way it was wrong is the
useful part. The rule is real: engine `CLAUDE.md` line 77, section "Dev loop — worktree first"**
(`git ls-files --error-unmatch CLAUDE.md` → tracked; it is on `origin/main` too). My `grep -rn`
across three `CLAUDE.md` paths returned no match because **engine `CLAUDE.md` is a symlink to
`AGENTS.md`** and BSD `grep -r` does not follow a symlink passed as an argument — and my
`2>/dev/null` **discarded the diagnostic that would have said so**. Two failures stacked: a search
that silently skipped the file it was asked about, and an error deliberately suppressed so the
skipping was invisible. The engine's own `CLAUDE.md` forbids exactly that second one
(*"Never silently discard errors"*), which is a fitting way to have earned it.

**Consequence, and it settles the question: pushing a finished, verified change is the house default
in this repo.** The push was right for the reason given *and* for the rule; the retraction here is
only about how I checked, not about the act.

**Not a defect of any session — a hazard of the layout.** A second crate checkout exists at
`/Volumes/docker-ssd/ps/mini-ork-ui` (clean, same `origin`, same HEAD `9154ef4`). Landing `board.rs`
or `ide_http.rs` from two checkouts can conflict on push, which is the standing reason **P1#8** (the
5th `Counts` bucket, which also edits `board.rs`) is held rather than rushed.

**Do not prune this shared checkout while sessions are live.** A fetch now prints *"There are too
many unreachable loose objects; run 'git prune' to remove them"* and points at `.git/gc.log`. That
warning is the repository asking for a prune — and with several live sessions working in one
checkout, unreachable loose objects are exactly what a peer's reflog holds for unpushed or
pre-rebase work. Pruning now deletes recovery points that are not any single agent's to judge as
garbage. Deleting `.git/gc.log` is not the safe alternative either: it **re-arms** automatic cleanup
rather than postponing it. Both are the user's call, taken when sessions are quiescent. Raised by
`fix-run-graph-edges`, who surfaced it rather than acting — the same judgement as the `:7090`
restart refusal, applied to an irreversible operation on shared state.

**C-5 — "the push rule is in no `CLAUDE.md` I can read" was FALSE. Retracted.** The rule is
`CLAUDE.md:77` in the engine repo. My search failed on a **symlink**: `CLAUDE.md → AGENTS.md`, and
BSD `grep -r` will not follow a symlink named as an argument; `2>/dev/null` hid the warning that
would have revealed it. Third instance this sweep of the same underlying error — **a negative about
code, asserted from one search instead of a positive check** (C-1 one grep, C-3 one reading, C-5 one
grep with a suppressed error). The pattern is worth naming: every one of my wrong claims was a
*negative* ("there is no code", "the loop is broken", "the rule does not exist"), and every one
survived because the failing check was quiet rather than loud.

**C-5, addendum — the obvious remedy is also wrong, measured.** `mini-ork-81` reproduced the
symlink cause independently and then tested the fix I would have reached for: **`-R` fails exactly
like `-r`.** BSD grep will not follow a symlink passed as a *file argument*, and "follow all
symlinks" does not change the file-argument case — `grep -Rn <pat> CLAUDE.md` → rc=1, same as
`-rn`. The two forms that do work are to **drop `-r` entirely** (`grep -n <pat> CLAUDE.md` succeeds,
because non-recursive grep just opens the path) or to **name a directory**
(`grep -rn <pat> .` finds it, reported as `AGENTS.md:77`). This is worth keeping precisely because
it is the fourth instance of the same shape rather than a fifth unrelated error: a *plausible*
remedy asserted from how the flag reads, not from running it. `mini-ork-81` also deleted its
earlier em-dash theory of the cause for the same reason — a wrong-cause memory is worse than no
memory — and its framing of the generalisation is sharper than mine: **a check that cannot fail
loudly is not a check.** Both halves of line 77 are that discipline — the retraction is a check that
was too quiet to fail, and "shipped, not parked" is the same rule applied to delivery.

---

## Delivery — round 4

**`1165b056` (learning-loop fix program)** — report filed as `1165b056-learning-loop-fixes.md`,
verified against `origin/main` `5b8d4a98` rather than against recorded status.

- **Shipped, all ancestors of `origin/main`:** R1 `d405ef48`, R2 `5b8d4a98`, R4 `aaa87512`,
  R5 `95a4af77`, R6 `b2f5b224`, R7a `dc2d8793`, R10 `fda51845`, plus
  `recipes/audit-judge-panel-4l` and `mini_ork/verify/panel_heterogeneity.py`.
- **In the plan of record but NOT shipped** (`docs/plans/2026-10-15-learning-loop-fixes.md`,
  `9fb802fa`): R3 — the double injection is still live, `mini_ork/cli/execute.py:3567` returns
  `pref_block + v2_block + block` — plus R7b, R9, R11a, R11b, R12 (no `target_repo` symbol
  anywhere) and R13 (no `v2_legacy`/`effective` symbol). R8 in progress. **This is cross-cutting
  finding 1 again:** a plan of record that reads like a shipped contract.

**New defect — the reversion watchdog undoes a legitimate rebase (P1).**
`.githooks/post-commit:132-152` restores a file whenever its content equals a **parent** commit's
blob — which is precisely the working-tree state a rebase checkout produces. So every
`make worktree-merge` within ~60 s of a commit wedges with *"local changes would be overwritten"*.
Witnessed on `lf-r5` and `lf-r2`; `.mini-ork/file-reversion-guard.log` records
`restored-reverted-to-2e8367f7…`. Worked around with `MO_REVERSION_GUARD_DISABLED=1`; the source
still needs fixing. **Not one session's problem:** worktree-merge is how changes land in this repo,
so a guard that makes it wedge after every commit sits on the critical path for every session.
It caused the two wedged rebases and the retry loops that followed them — but **not** the "push
race" friction reported elsewhere here, which is a ref-update race on `origin`; the guard only ever
touches the local working tree. (I inferred the causal link; the session that owns the defect
corrected it, and the distinction matters because the two have different fixes.)

**Proven but deliberately not applied** — the live-DB cleanup of 166 non-code rows / 280 artifact
file paths is demonstrated on a **copy** and held back from `$MINI_ORK_HOME/state.db` while R8 runs.
Correct call; recorded as pending, not done.

**The primary's divergence closes as this session's, not a peer's.** `1165b056` found local `main`
diverged and inferred "another session's unpushed docs(audit) commits" — correct, and it is this
aggregation's: local `351481b1` is **patch-id-identical** to `8d8be650` (`git patch-id --stable` on
both → `e1307ee4e056ad09a8e5e3a275a8351990444b1c`), so it is a true duplicate of what is already on
`origin/main` and dropping it loses nothing. It clears with `git pull --rebase` once the ~11 dirty
files from other sessions quiet; `--ff-only` will keep refusing in the meantime, and an `--ff-only`
refusal there is **not** evidence of new drift. Not cleared by this session for that reason.

---

## Delivery — round 5

**P2#19 — the `/tmp` tests are now in CI, DELIVERED `effc3881`.** `tests/unit/test_author_style_imitation_review.py`,
12 tests, merged via `make worktree-merge SLUG=style-assign-tests` — and it ports more than the
scripts: in-process `importlib` loading for logic (matching `test_frontier_llm_research_recipe.py`),
subprocess for the CLI exit-code contract, and pins the design itself (the clusterer publishes
`families.json`, the contract declares all three cluster artifacts, `split.py` runs assign before
split, and the clusterer prompt contains **no `technique_keys`** — the exact artefact the model used
to hand-write, so its absence is now a test rather than a hope). Gate: `python3.11 -m pytest
tests/unit -q -k "recipe or author_style"` → 450 passed; the file alone runs in 4.9 s.
**Gate note worth keeping: bare `python3` does not work here (3.9, no fastapi) — it must be
`python3.11`.** A gate that silently degrades to the wrong interpreter is the "verifier could not
run" class this sweep has already seen once.

**P2#18 — half of it was already done, by a different session, and was verified rather than
re-done.** The `author-style-review` worktree directory *and* its `wt/author-style-review` ref were
both already gone; the session checked the recipe content is still on `main` instead of repeating
the cleanup. No session's claim needs correcting here, but the item is now honestly split:
`author-style-review` closed, `_ide-engine` still open.

**P1#5 — blocked on the user, and a peer refused to run it, correctly.** Running the
deterministic-assignment recipe end-to-end is a **real-cost LLM run (~2 h, real money)**, so the
session declined to launch it on a peer's instruction and raised it to its own user. That is the
permission-laundering refusal applied where it actually costs something, and the aggregate now
carries P1#5 as **blocked-on-user, not blocked-on-work** — it needs an authorisation, not an
assignee.

**The primary reconciled, and the retractions survived it.** Local `main` went through
`57a589ae` ("reconcile a superseded duplicate") and `2eefcbc1` (merge of `origin/main`), and it is
now **3 ahead / 1 behind** — no longer carrying the duplicate as a blocker. Verified directly rather
than assumed, because a merge is exactly where a retraction can be silently reverted: the primary's
`AGGREGATE.md` still contains C-3 (×3), C-5 (×2), the round-4 reversion-guard text, and P0 4c. It
matches `origin/main` on every one of those. A wrong-side resolution here would have restored the
false claim on the next push from the primary; it did not.

---

## Delivery — round 6

**The verifier-provenance plan is fully implemented, and its two declared follow-ons are both open.**
The plan of record at `/Users/admin/.claude/plans/peaceful-brewing-alpaca.md` is **done, not pending** —
`2f7e4b81` carries all eleven files it named (`verify/levels.py`, `cli/execute.py`,
`cli/execute_handlers.py`, `cli/publisher.py`, `recovery/auto_repair.py`, `recovery/retry_hint.py`,
four test modules), and every symbol it specified is on `origin/main`: `FAILURE_KINDS` (`levels.py:148`),
`ABSTAINING_KINDS` (`:149`), `ABSTAIN_RC = 3` (`:153`), `abstain_count` (`execute.py:1138`), the
`rc == _levels.ABSTAIN_RC` branches (`:1291`, `:1295`, `:1460`, `:1509`). Verified by ancestry and by
reading the symbols, not from the plan's own status. **No session needs to start this work.**

**P0 4d (new) — the mechanism is wired and cannot fire: 1 of 124 verifiers emits `failure_kind`.**
The plan's own follow-on section says so — *"Emitter sweep — follow-on commits, after the core is
green … each of the 31 shell-out verifiers gains `failure_kind`"* — and the sweep has not happened.
Checked positively rather than by one grep, because "nothing emits it" is exactly the negative I have
been wrong about three times this sweep:
`git grep -l failure_kind origin/main -- 'recipes/*/verifiers/*.py'` → **1**
(`tier1-compile-typecheck.py:172`, the reference); `git ls-tree -r --name-only origin/main recipes/ |
grep '/verifiers/.*\.py$' | wc -l` → **124**. There is no shared envelope writer to fix once:
`verify/pm.py` exposes `classify_spawn` and the manager resolvers, and **each verifier builds its own
`envelope` dict and must set the key itself** (`tier1:165-180` is the whole pattern — `classify_spawn(...)
or classify_spawn(...) or "code"`). So the sweep is genuinely per-file work, ~123 files, and its stated
scope of 31 undercounts even the shell-out subset.

**Why this is P0 and not a nit.** `classify_spawn` is correct, tested, and reachable — and in
123 of 124 recipes nothing calls it, so a runner that never started still arrives at the engine as
`pass: false` with no provenance, which is *precisely the 2026-10-11 incident the whole mechanism was
built to stop* (one bad lint call → 10 failed nodes → rollback on an implementation never checked).
The fix landed at the reading end; the writing end is still absent. **This is the fourth instance this
sweep of one shape — a control that exists but has no input in practice:** `NO_EXECUTABLE_FENCE`
shipped as a warning so the corpus still lints clean, the reversion guard fires on a state a rebase
produces, and now the abstain vocabulary that no verifier speaks. Worth naming as a class, not four
separate findings: **each was verified at the mechanism and never at the input**, and each passes its
own tests.

**The plan's learning hand-off is real and is the user's.** `classify_spawn` (`pm.py:182`) is *not* an
empty stub — I expected one and checked, and it has a working body: `rc == 127` → `toolchain`, a negative
`rc` (a tool killed by a signal before it reached a verdict — the `m3b-activity-feed-backend-removal`
`lint_rc -15` case) → `toolchain`, then a conservative `_SPAWN_FAILURES` pattern list. The `# noqa:
ARG001` is only because `argv` is contractual-and-unused. What is left for the user is the docstring's
own ask: **which pattern or rc separates "the tool never started" from "the tool ran and rejected the
input"** — domain knowledge about the runners, ≈8 lines, and *every* shell-out verifier calls this one
function, so refining it here is refining it everywhere. Nothing downstream is blocked on it, and it is
still the highest-leverage unfilled slot in the codebase this sweep found.

**Concurrent-edit note, for whoever lands the sweep.** `mini_ork/cli/execute.py` is dirty at this audit
(held by a peer), but the mechanism it holds is already landed — so the sweep does not collide with
that edit, and the sweep touches only `recipes/*/verifiers/*.py` and `verify/pm.py`, neither of which
that peer is in.

### Round 6, addendum — what P0 4d actually costs, and why it is unowned

**The cause is structural, not 123 oversights.** Each verifier prints its own envelope inline —
`git grep -l 'print(json.dumps' origin/main -- 'recipes/*/verifiers/*.py' | wc -l` → **52**, so at
least half write the JSON themselves by hand — and `verify/pm.py` deliberately has no envelope
writer. `git grep -l '_lib' origin/main -- 'recipes/*/verifiers/*.py' | wc -l` → **2**, against a
single `_lib` module in the whole tree. So **at most 2 of 124 verifiers route through any shared
library at all.** The consequence is the part that makes this P0 rather than a chore: patched by
hand, it is 123 independent edits with 123 chances to drift, *and the next verifier anyone writes
silently rejoins the 123* — which is how the gap reopened after `2f7e4b81` closed its own half.

**The leverage is a shared envelope writer, and a precedent for the shape already exists.**
`recipes/epic-runner/verifiers/_lib.py` prints one envelope from a predicate (`"pass": not failed`,
`:86`) rather than each verifier assembling its own. It does **not** yet carry `failure_kind` — the
precedent is the writer *shape*, not the abstain key — so closing 4d is one helper gaining
`failure_kind = classify_spawn(...)` at its shell-out site, plus call-site swaps for the 52 (and, for
the other 72, whatever they use instead), and every verifier written afterwards inherits it. **The
sweep's stated scope of "31 shell-out verifiers" undercounts the call sites and misreads the fix as
per-file.**

**Unowned, and it needs the user's go-ahead — a peer correctly declined to authorise it.**
`mini-ork-84`, which re-derived every number above independently and supplied the shared-writer
framing, declined to start it: a multi-file change across 123 files is routed through mini-ork by its
user's standing instruction, and a peer request cannot authorise it. That is the third refusal this
sweep and the right one — the same discipline as P1#5's ~2h run and the `:7090` restart. **P0 4d is
therefore open-and-unowned, not blocked:** it needs a decision to assign it, not a technical
unblocking. Recorded here so it is not mistaken for "someone has it".

**One correction to round 6's own scope line.** Round 6 said the sweep is "~123 files"; that is the
count of *verifiers*, not of *edits*. The measured edit set is the shared writer (1) plus the
call sites that bypass it — 52 printing inline, 122 with no `_lib` reference. Directionally the same
finding, but the file count was mine and it was not measured.

---

## Method note — the one error this sweep produced five times

Every wrong claim below was **a check that passed while answering a narrower question than the one
asked**. None was carelessness and none was a check that failed; each was a check that *succeeded*
at the wrong scope, which is why each survived review.

| the claim | the check that passed | the question actually being asked |
|---|---|---|
| C-1 — "there is no code that does this" | one `grep` for the obvious name | is there code anywhere under any other name |
| C-3 — "the loop returns the wrong match" | one reading of the docstring's word "LAST" | does the loop's first match *equal* the last writer |
| C-5 — "the push rule is in no `CLAUDE.md`" | one `grep -r` with `2>/dev/null` | does the rule exist in a *symlinked* file |
| `-R` — "this is the fix for C-5" | the flag's documentation ("follow all symlinks") | does BSD grep follow a symlink passed as a *file argument* |
| the stale re-read — "the harness invented a version" | two markers matched a plausible producer | did that version ever exist *on disk* |

Two of the five were mine (C-1, C-3), one is shared (C-5 and its remedy, mine then corrected by
`mini-ork-81`), and two are `mini-ork-81`'s own, retracted by it unprompted — the last one downgraded
from "a version that never existed" to "a stale-but-real generation", which is the ordinary cache
behaviour it looks like. **The class is not "unverified claim". It is "verified at the wrong scope",
and it is invisible from inside the check.**

**This applies to this document.** Every number above was produced by the same method that produced
those five errors, so a bare count here is a hypothesis until its measurement is named — which is why
each P0 entry carries its command rather than its conclusion, and why P0 4d is filed as a decision to
assign rather than a technical unblock. The useful discipline is the one `mini-ork-81` named and then
applied to its own memory: **ask what question the check answers, not whether it passed.**

