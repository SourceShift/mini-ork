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

### P1 — declared but never exercised, or emitted but unrenderable
| # | gap | where | source |
|---|---|---|---|
| 5 | Deterministic-assignment fix never ran end-to-end; 3 declared cluster artifacts never produced | `0190e322`; `artifact_contract.yaml:27,30,33` | `732bb4cc` G1 |
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
| 18 | Dead scaffolding / stale worktrees: `_ide-engine`, `author-style-review` + `wt/author-style-review` | `d5471234` G3, `732bb4cc` G4 |
| 19 | Untracked deliverables at risk: `git stash -u` strands run deliverables; 244 KB report + 1.2 MB corpus in gitignored `runs/`; tests live in `/tmp` outside CI | `8162bb3f` G4, `732bb4cc` G2, G3 |
| 20 | Doc drift: `docs/todos/2026-10-14-failed-runs-48h-triage.md:375-376` says a run "still reads executing"; the DB says `published` | `83e57620` G4 |
| 21 | Stale plan/record rows: `run-graph-plan` still `pending` while its three children are `completed` and verified | `6881f0ad` G3 — **CLOSED, wrong store** (round 2) |
| 22 | Worktree run resolves a bare home → the level-2 spec path is untested there | `da0b8c69` G3 (mechanism) — **not actionable as written** (round 2) |

### Fully un-started
- ~~**Linear/Jira integration from the UI** — no code, no branch.~~ **RETRACTED — this was already
  shipped; see Corrections.** Ten commits on `origin/main` implement it (`20a62c45`).
- **Run-page IA work** (4 tasks: spec level on the fast path, promote "Why — evidence", collapsed
  Diagnostics, empty-section vanish + tab badges) — verified 0% implemented, plan file only
  (`da0b8c69` G4).
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
- It also established *why* the DAG-click cannot be observed here — a structural reason, not a lack
  of effort: the live engine serves the **v1** tab set at *both* `spec=1` and `spec=2`
  (`key=run` → `[dag, kickoff, overview, agents, learnings, artifacts]`), and v1 leads with `dag`,
  so `goto_main_tab()` finds itself already at `tabs[0]` and early-returns **by design**. The v2 tab
  set exists only as uncommitted working-tree work (`run.py` dirty in the primary) and the running
  process predates it. So the run-page case is **unobservable on this engine** until the server is
  restarted onto the new code — the same `kill 81516` fix, seen from the other side.

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
