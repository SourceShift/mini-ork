# Session gaps — `20a62c45` (work-intake delivery, primary checkout)

**Session:** `20a62c45-9604-44b0-808d-4a66dd73ae2d` — the mini-ork **primary checkout** session,
owner of the Linear/Jira/GitHub intake, the consolidated ticket board, attachments, gated
write-back, Slack/Discord, and the worktree/child-target fixes.

**Audit window:** 2026-10-16, late evening. **Method:** every claimed status re-checked against
git, the engine source, the crate source, and the live state DB. Nothing below is inherited: every
line marked *receipt* is a command this audit ran, with its output.

**This report corrects `AGGREGATE.md`.** That document lists *Linear/Jira integration from the UI*
as "no code, no branch" (quoting `f6e1ed90` G5). It is shipped. See §2 G1.

---

## 1. Task table

Live state at audit time: `git rev-parse --short HEAD` → `0abc587d`; `git rev-list --count
HEAD..origin/main` → **0**; `origin/main..HEAD` → **0**.

| id | description | recorded | VERIFIED | evidence |
|---|---|---|---|---|
| W1 | Linear adapter (read-only source) | done | **LANDED** | `4379ff03 pm: add the Linear adapter as a read-only source`; `mini_ork/pm/providers/linear.py` (28.5 KB) |
| W2 | Jira adapter (read-only source) | done | **LANDED** | `8448f6bc pm: add the Jira adapter as a read-only source`; `git log -S_ISSUE_KEY -- mini_ork/pm/providers/jira.py` → `8448f6bc`; `jira.py:373,598,734 using quote(key, safe='')` |
| W3 | Board draws imported tickets beside epics | done | **LANDED** | `d28c2b0f board: draw imported tickets beside epics on the Tasks tab` |
| W4 | **Consolidated board** — one page, PM mirror (local epics + Linear/GitHub/Jira), columns by status | done | **LANDED as code — but the epic the user asked for was never written** | `kanban.py:5-6` (module docstring: "one `columns` section of epic cards *and* the tickets imported from the PM mirror (Linear, Jira, GitHub)"); `_epic_buckets:523` and `_tickets_into:582` both fill the **same** `buckets` map keyed by column title; `_fair_share:333`; `_source_actions:499` (All / Epics / per-source). See §2 G2 for the missing epic file |
| W5 | Per-source fair share in a capped column | done | **LANDED** | `da2d3834 board: every source gets a share of a capped column; one adapter rule` |
| W6 | Attachments: store, preview, reach the run's kickoff | done | **LANDED, both tiers** | engine `f4d5bb75` + `012d3fe9`; `mini_ork/threads/attachments.py`; crate `ac63ea4` (document renderer) + `46dda68` (`MINI_ORK_IDE_OPEN` page args); `store.rs:554-608` (`pick_files`) |
| W7 | Gated write-back (comment / transition) | done | **LANDED** | `1be093e8 pm: gated write-back — comment and transition on Linear and Jira`; `mini_ork/pm/writeback.py` |
| W8 | Report a finished run back to its ticket | done | **LANDED + tested** | `d29b632b`; `mini_ork/intake/report.py`; `tests/unit/test_intake_report.py` (in the 59-passed run below) |
| W9 | Slack transport | done | **LANDED** | `991a8f59 integrations: Slack transport over the thread seam`; `mini_ork/integrations/slack.py` |
| W10 | Discord transport | done | **LANDED** | `7d09f02c integrations: Discord transport over the thread seam`; `mini_ork/integrations/discord.py` |
| W11 | **Child run targeted the primary checkout, not its worktree** | done | **LANDED + tested** | `668b1198 epic-runner: pin each child to the epic's declared target repo`; `recipes/epic-runner/lib/epic_dispatcher.py:303 _child_target`, `:292 env["MO_TARGET_CWD"] = target`; `tests/unit/test_epic_dispatcher_child_env_py.py` |
| W12 | Scheduler never reclaims a live epic | done | **LANDED** | `52bd2e6f` (in-flight attempt is not stale) + `109a927c` (claiming scheduler alive via `claimed_by`); `tests/unit/test_scheduler_py.py` |
| W13 | Test DB isolation guard | done | **LANDED** | `f4394bbd test(isolation): never let a test resolve the live state.db` |
| W14 | Fix the planner lane chain, then re-dispatch Jira + blocked epics | in progress | **SUPERSEDED — not this session's any more** | My repoint of `.mini-ork/config/agents.local.yaml` was deliberately reverted when minimax recovered. The real fix is the peer commit `1172a9fd fix(lanes): probe lane liveness at configuration time, and drop the dead kimi default` (2026-10-16 16:58) + `7f9ebb47` (assert the default tail). `wit-4-jira` is now `done`, so the "re-run Jira" half is moot. See §2 G5 |
| W15 | Sync local and main | done | **DONE, then advanced by peers** | Sync landed at `f9e0c455`; the primary now reads `0abc587d` with 0 behind / 0 ahead (peers pushed the audit commits and the primary fast-forwarded) |

**Receipt — test run (this audit):**

```
.venv/bin/python -m pytest -q tests/unit/test_epic_dispatcher_child_env_py.py \
    tests/unit/test_threads_attachments.py tests/unit/test_intake_report.py \
    tests/unit/test_agent_server_attachments.py
→ 59 passed in 16.60s

.venv/bin/python -m pytest -q tests/unit/test_ide_pages_thread.py -k "attachment or document or pick"
→ 5 passed, 16 deselected in 14.32s
```

---

## 2. Missing features & gaps

### G1 — `AGGREGATE.md` is wrong about Linear/Jira in the UI (highest value)
The aggregate's "Fully un-started" section says: *"Linear/Jira integration from the UI — no code,
no branch."* There are **ten commits on `origin/main`** implementing exactly that: `4379ff03`,
`8448f6bc`, `d28c2b0f`, `da2d3834`, `f4d5bb75`, `012d3fe9`, `1be093e8`, `d29b632b`, `991a8f59`,
`7d09f02c`, plus the crate half `ac63ea4` / `46dda68`. All verified present by
`git merge-base --is-ancestor <sha> origin/main` and by reading the symbols named in the table
above. **Replace that entry.**

### G2 — The consolidated-board epic the user asked for was never written
The user's instruction was: *"Write a new epic for the consolidated board: one Board page that
reads the PM mirror (local epics plus Linear, GitHub and Jira tickets) as columns by status,
alongside or replacing the runs-only view."* The **behaviour shipped** (W4). The **epic artefact
did not**:

```
grep -ril "consolidated board|one board page|reads the pm mirror" docs/ recipes/ .mini-ork/
→ (no matches, exit 0)
```
The `epics` table carries `board-shows-imported-tickets` (`done`) — the narrower Oct-13 epic — but
no consolidated-board epic. This is the same class `8162bb3f` calls "declared but never
exercised", inverted: *exercised but never declared*, so nothing tracks the requirement and no
verifier ran against it. It is a documentation artefact owed to the user, and the one item from
the four-point order that is still open.

### G3 — The mirror is empty, so the board has nothing to draw
```
/Volumes/docker-ssd/ps/mini-ork/.mini-ork/state.db  →  pm_issues: 0   threads: 3
```
Zero mirrored tickets. Every external source in `config/pm.yaml` ships `enabled: false` and no
source credential exists in the vault. The Board renders correctly and truthfully says so
(`kanban.py:466-480` — "No PM source is enabled — add a source to config/pm.yaml"). **Not a code
gap; blocked on the operator.**

### G4 — The `:7090` server runs pre-sync code **and defaults to a `/tmp` smoke home**
```
lsof -nP -iTCP:7090 -sTCP:LISTEN   → Python 81516, 127.0.0.1:7090 (LISTEN)
ps -o pid,lstart -p 81516          → started Thu Oct 15 14:17:53 2026
lsof -a -p 81516 -d cwd -Fn        → /Volumes/docker-ssd/ps/mini-ork
ps -ww -o command= -p 81476        → bin/mini-ork serve --port 7090 --home /private/tmp/mo-smoke50/.mini-ork
```
81516 is the re-exec'd child of 81476 (identical start second; `mini-ork serve` re-execs uvicorn —
see the launcher's venv re-exec). Two independent defects, and **the restart fixes only the first**:

1. **Pre-sync code.** Started Oct 15 14:17:53, before every commit in §1 — the engine half of the
   delivery is invisible in the IDE. (The `EPIC-work-intake-thread-communication.md` §4 "reboot
   trap" still cites PID 55861 / Oct 10; stale, current PID is 81516.)
2. **A smoke home as the server default.** It was launched `--home /private/tmp/mo-smoke50/.mini-ork`
   — a temp home whose `state.db` has **no `epics` table** (`sqlite3 … "SELECT 1 FROM epics"` →
   `no such table: epics`). Probed live:

```
curl -s ":7090/ide/page?key=kanban&tab=tasks"                       → "No epics table — initialise
                                                                       the project (`mini-ork init`)"
curl -s -H 'X-Mini-Ork-Home: <repo>/.mini-ork' ":7090/ide/page?…"  → the real home's Freshness line
                                                                       ("No PM source is enabled …")
```

   So the `X-Mini-Ork-Home` header **is** honoured and selects the right home — but any page read
   that omits it, or any IDE path that does not send it, is answered out of `/tmp` and shows an
   empty project. A restart that repeats the same `--home` reproduces this exactly. **Drop it.**

**The correct restart is two changes, not one:** a fresh process (`kill 81516` first, or the port
stays held by the old listener) launched **without** `--home /private/tmp/mo-smoke50/.mini-ork`.
**Operator action.**

### G5 — The "re-dispatch the blocked epics" half of task 3 is unowned
I repointed the home lane overlay off dead minimax, then restored it when minimax recovered. The
durable fix landed from a peer (`1172a9fd`, `7f9ebb47`). Nothing in *this* session's queue is
gated on the old chain, and `wit-4-jira` / `wit-4-linear` / `wit-3-integrations` / `wit-2-*` /
`wit-5-writeback` / `board-shows-imported-tickets` / `intake-report-back` are all `done`. The
epics still open in the queue (`rsi-e1..e8`, `libwit-se-1..6`, `cn-ui-field-map`, `frc-b*`) were
never in this session's assignment and are blocked on their own causes (e.g. `cn-ui-field-map`
needs ContextNest `/field/basins`, which does not exist).

### G6 — A false commit attribution in this session's own memory
`project_work_intake_delivered_20261013.md` records the Jira adapter as `976c9e41`. That sha is
`test(intake): pin the pm config in the status JSON test` — a test fix from the same day. The
adapter's real commit is `8448f6bc`. Corrected in the memory file as part of this audit. The
general hazard: a session logged a *related* commit it made in the same hour as the *feature's*
commit, and the two were never separated.

### G7 — Another session's dirty file in the primary
`git status --porcelain` → `M mini_ork/cli/specs.py` (not this session's). Harmless now, but it is
the exact shape that blocks a later `--ff-only` when the owning session's work diverges from
`main`.

### G8 — Crate ancestry check (closure, not a gap)
The `ui` submodule now pins `9154ef4` (a peer's bump). My Rust half shipped at `785cc110`:
`git -C mini-ork-ui merge-base --is-ancestor 785cc110 9154ef4` → **YES**. So the attachment
previews and `pick_files` are inside the pin; no orphaned crate commit.

---

## 3. Blocked on

**The user (operator actions):**
1. **Restart the `:7090` server** (PID 81516, up since Oct 15 14:17) so the IDE serves the synced
   engine — **and drop the `--home /private/tmp/mo-smoke50/.mini-ork` its parent (81476) was
   started with.** Restarting with the same flag reproduces G4's second defect: the server would
   still answer header-less page reads out of `/tmp`. Nothing else makes the delivered work
   visible.
2. **A ticket credential + `enabled: true`** for one source — `LINEAR_API_KEY`, or
   `JIRA_API_TOKEN` + `JIRA_EMAIL`, or `GITHUB_TOKEN`, in
   `.mini-ork/config/vault.yaml` / `secrets.local.sh` (key **names** only), then flip that source
   in `config/pm.yaml`. Until then `pm_issues` stays 0 by design.
3. **The tier choice the aggregate also asks for — recommendation:** no new decision is needed.
   Both tiers already exist and are the reference shape (`CLAUDE.md`): the **simple tier** is
   `secrets.local.sh` / an env var (no server, fails closed on a missing key — `pm/config.py`
   already errors naming the key), and the **secure tier** is the vault
   (`config/vault.example.yaml`, `mini-ork vault status`). Pick per deployment; the call sites do
   not change.
4. **Decision on G2:** whether to write the consolidated-board epic now (I can, ~30 min) or fold
   the requirement into the work-intake epic's §3 table as shipped.

**Not blocked on anyone:** G2 is a document I can write; G6 is already fixed.
