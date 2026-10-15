# Ticket detail: read live, cache the body back into the mirror

## Goal

`mini_ork/ide_pages/task.py` draws a ticket from the PM mirror only, so a
Linear/Jira ticket's page has no body, no `created_at` and no comment thread —
the mirror never stored them. This epic makes the ticket branch read the issue
live through its adapter and cache what it read *back into the mirror*, so a
later visit with the source unreachable still shows the last-known body.

Read-only. This epic adds **no write path**: no comment, no transition, no
update, no create. `mini_ork/pm/writeback.py` exists and stays unused.

- recipe: framework-edit

## Design decisions already taken (do not re-litigate)

- **Live with a cache column** (not mirror-only, not live-only). The page tries
  the adapter; on success it writes the fetched fields back through
  `mini_ork.pm.store` and renders them; on failure it renders the cached row
  with a visible "source unreachable, showing the last read at <ts>" notice.
  A broken source must never blank the page — that is `kanban.py`'s stated
  rule and this page keeps it.
- The body is the only field that may be large. Cap it the way `_KICKOFF_CAP`
  caps the kickoff: a truncated body renders with a note naming the cap.
- No comment **write**. The thread is displayed; the composer is not built.

## Work

1. **Migration `db/migrations/0072_pm_mirror_body.sql`** (next free number is
   0072). Add to `pm_issues`: `description TEXT`, `created_at TEXT`,
   `body_synced_at TEXT`, `body_error TEXT`. Nullable, no default — an old row
   is "never read live", which is a real state and not an error.
2. **`mini_ork/pm/store.py`** — `IssueRow` gains `description` and `created_at`
   (both `str | None`); its docstring loses the sentence that says a view must
   re-read to get a description. New `cache_body(uid, *, description, created_at,
   body_synced_at, error=None, db=None)` writing only those four columns of one
   row. `upsert_issues` must **not** overwrite a cached body with NULL when a
   later sync row carries none — leave the cached columns alone unless the
   adapter supplied a value.
3. **`mini_ork/ide_pages/task.py`** — the ticket branch calls the adapter:
   - Resolve through `mini_ork.pm.registry.get_provider(provider)`; ask for
     `get(uid)` and `comments(uid)` **only** when capabilities allow, and catch
     `ProviderError` (which `UnknownProvider` is not — handle both) into the
     `body_error` state rather than raising.
   - Render a `markdown` section from `issue.description` when present.
   - Render a comments thread: a `list` of `S.item`, ordered oldest-first,
     each carrying author and timestamp in its `sub`.
   - Render `created_at` beside `updated_at` in the identity facts.
   - On a live failure, render the cached description (if any) plus a
     `callout` naming the source, the age of `body_synced_at`, and the recorded
     `body_error`. Never a traceback, never a URL with a token in it.
4. **`mini_ork/intake/poll.py`** (or wherever the sync lands) — when a sync
   already holds the full `Issue`, hand `description`/`created_at` to
   `cache_body` so the first sync warms the cache for free.

## Constraints

- Offline by default is not an option here — this is the one page allowed to
  touch the network, and it must degrade, not fail. Every test must run with
  the adapter faked; no test may reach a real endpoint.
- `S.guarded` returns a **list** — `extend`, never `append`.
- List items are `S.dot(...)` / `S.item(...)`, never `S.muted(...)` (that
  returns a cell).
- The section discriminator key is `type`, not `kind`.

## Verification commands

- `python3 -m compileall -q mini_ork`
- `python3 -m pytest -q tests/unit/test_ide_pages_task.py tests/unit/test_pm_store.py tests/unit/test_pm_mirror.py`

## Done When

- A seeded ticket with a cached body renders its markdown, its `created_at` and
  its comments when the adapter answers, and renders the cached body with an
  "unreachable" callout when the adapter raises.
- A ticket whose body was never cached and whose source is down renders the
  mirror facts and the callout, and `ok` stays `true`.
- `description` survives a later `upsert_issues` that carries none.
- `${MINI_ORK_RUN_DIR}/panel-verdict.json` contains `"verdict": "pass"` with all verifiers passing.
- All verification commands pass in the isolated worktree.
