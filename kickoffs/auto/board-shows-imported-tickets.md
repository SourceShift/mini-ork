# Board shows imported tickets beside epics

## Goal

The Board's Tasks tab draws mini-ork's own epics only. Put the tickets imported from Linear, Jira
and GitHub — the PM mirror — on the same four columns, so one board answers "what work exists,
and where is it" without switching tools.

**Deliverable.** The Tasks tab in `mini_ork/ide_pages/kanban.py` draws mirror issues next to the
epic cards, plus `tests/unit/test_ide_pages_kanban_tickets.py`.

**Scope:**
- Read only the mirror: `mini_ork.pm.store.query_rows()` for rows, `mirror_state()` for
  per-source freshness, `links_for()` for run edges. Building the page makes **no network call**
  and imports no adapter — a slow or broken source must not slow or blank the Board.
- Skip mirror rows whose `provider` is `local`: those are the epics the tab already draws from the
  `epics` table, so drawing both would duplicate every card.
- Columns from the closed `IssueState`: `backlog`, `todo` → Backlog; `in_progress` → Working;
  `blocked`, `in_review` → Needs you; `done` → Done. `canceled` is not drawn; count it in the
  section note instead.
- A ticket card: title `"<native_key> · <title>"`; sub `"<provider> · <native_state>"` — the
  source's own word, never only the normalized one; meta items for assignee and priority; a chip
  naming the source. `do` opens the linked run when `links_for(uid)` has a `run` link, else
  `S.url(row.url)` when the row has a url, else no `do` key. A ticket with a run link also gets an
  `agent` chip, so a person can see mini-ork is working it.
- One per-column cap and one newest-first order shared by epic and ticket cards, so a large Jira
  project cannot push every epic off the board.
- A source filter in `args["source"]`: `all` (default), `local` (epics only), or a provider id
  (that source's tickets only). Render it as header buttons using `S.set_args(source=…)`.
- Freshness and empty states as list/warn lines above the columns — never a blank board:
  - no external source registered → one line naming `config/pm.yaml`, saying a source needs
    `enabled: true` and its `token_env` key in the vault;
  - a source whose last sync recorded an `error` → a warn line with the source id and that error
    text (already credential-scrubbed by `mini_ork/pm/http.py`);
  - otherwise a muted "synced <age> ago" per source.
- A header button "Sync tickets" → `S.cli("intake", "once", "--dry-run")`: it refreshes the mirror
  and starts nothing. `tests/unit/test_ide_pages_actions.py` must still resolve every `cli`
  action the page emits.
- Two tiers (CLAUDE.md) already exist upstream of this page: simple = `mini-ork intake poll` over
  stdlib HTTP with the key in `secrets.local.sh`; secure = the signed intake webhook keeps the
  mirror fresh and keys live in sops/OpenBao. The board only reads the mirror, so it behaves the
  same under either — state that in the module docstring.

**Out of scope.** Writing to a ticket (wit-5-writeback), the Runs tab, and any Rust:
`ui/src/store.rs` already opens `{"url": …}` actions.

**Acceptance.** Offline tests on a temp home seeded through `mini_ork.pm.store.upsert_issues`:
a Linear `in_progress` ticket lands in Working and a Jira `in_review` one in Needs you; an
unmapped state shows the native word in the sub line; a `local` mirror row does not duplicate its
epic card; a `canceled` ticket is not drawn; `source=linear` shows only Linear cards; a recorded
sync error renders a warn line that does not contain the token; with no external source the tab
still builds and names `config/pm.yaml`; the existing epic-card tests stay green unchanged.

- max attempts: 2

---

## Scope Hint

- `mini_ork/ide_pages/kanban.py`
- `tests/unit/test_ide_pages_kanban_tickets.py`
- `mini_ork/pm/store.py` — read-only reference: `query_rows`, `mirror_state`, `links_for`, `upsert_issues`
- `tests/unit/test_ide_pages_kanban_epics.py` — read-only: must stay green unchanged

## Verification commands
- `python3.11 -c "import mini_ork.ide_pages.kanban"`
- `python3.11 -m pytest -q tests/unit/test_ide_pages_kanban_tickets.py tests/unit/test_ide_pages_kanban_epics.py tests/unit/test_ide_pages_actions.py`

Run these from the run's working tree (the isolated worktree), so `mini_ork` resolves to the tree under test. A syntax check is not acceptance.

## Done When

- `${MINI_ORK_RUN_DIR}/panel-verdict.json` contains `"verdict": "pass"` with all verifiers passing.
- All verification commands pass in the isolated worktree.
