# Stage 2 — attachments on a thread, and the `document` section payload

## Goal

Carry files on a thread and describe a previewable document to the IDE. This is the engine
(Python) half of Stage 2; the Rust renderer lives in the separate `mini-ork-ui` repository and is
a follow-up (see "Out of scope").

**Deliverable.** File parts survive into `thread_messages`, and the thread page emits a `document`
section for each attachment `mini_ork.documents.extract()` can preview.

**Scope:**
- **Attachments in.** `_flatten_message` in `mini_ork/web/routes/agent_server.py` keeps only
  `type == "text"` blocks, so every file part is dropped today. Store each file part under the
  thread's own directory (`$MINI_ORK_HOME/attachments/<thread_id>/`, created on demand, the name
  sanitized so it cannot escape that directory — the same layout `mini_ork/acp/agent.py` already
  writes for ACP sessions; reuse its pattern rather than inventing a second one) and append one
  `thread_messages` row with
  `role=MessageRole.FILE`, empty text and the stored absolute path in `attachments`. The store
  already supports this (`mini_ork/threads/store.py` — `MessageRole.FILE`, the `attachments`
  column); do not add a migration.
- **Section payload.** A new `S.document(title, preview)` builder in `mini_ork/ide_pages/spec.py`
  that turns a preview model into a JSON-safe section: `{"type": "document", "title": …,
  "kind": "grid" | "text" | "pages", …}` — grid → `columns` + `rows` (already capped by the
  extractor), text → its blocks, pages → absolute PNG paths written next to the attachment (never
  raw bytes in the page JSON). Document every key in the builder's docstring: it is the contract
  the Rust renderer will read.
- **Thread page.** `mini_ork/ide_pages/thread.py` renders a FILE-role message as a `document`
  section built from `extract(path)`. An extraction error (`DocumentExtractionError` and its
  subclasses) renders as a `warn` line naming the file and the error text — fail visible, never a
  blank pane. Unsupported formats get a single muted line with the path.
- **Two tiers** (CLAUDE.md): attachments are local files under the home on both tiers; nothing here
  uploads anywhere. Say so in the builder docstring.

**Out of scope.** The Rust side — a `"document"` arm in `render_section` plus `documents.rs`, and a
file picker copied from `projects.rs` with `files: true`. That crate's source of truth is the
`mini-ork-ui` repository (commit there, push its main, then bump the `ui` pin here and in
zed-mini-ork); it compiles only inside the zed-mini-ork workspace, so it cannot be delivered or
verified by a run against this repo. Until it lands, the IDE shows its generic placeholder for a
`document` section — expected.

**Acceptance.** Offline tests: a message with one text and one file part yields the text turn
plus one FILE-role row whose attachment exists under the thread's directory; a hostile filename
(`../../x`) stays inside it; a FILE-role row round-trips through `mini_ork/threads/store.py`; the
thread page emits a `document` section of kind `grid` for a CSV attachment and of kind `text` for
a DOCX; an unreadable attachment yields a `warn` line, not an empty section; the existing thread
page tests stay green.

- max attempts: 2

---

## Scope Hint

- `mini_ork/web/routes/agent_server.py`
- `mini_ork/ide_pages/spec.py`
- `mini_ork/ide_pages/thread.py`
- `tests/unit/test_agent_server_attachments.py`
- `tests/unit/test_ide_pages_thread.py`
- `mini_ork/threads/store.py` — read-only reference: `MessageRole.FILE`, `attachments`
- `mini_ork/documents/__init__.py` — read-only reference: `extract()` and the preview models
- `mini_ork/acp/agent.py` — read-only reference: the existing `attachments/<id>/` writer

## Verification commands
- `python3.11 -c "import mini_ork.ide_pages.thread, mini_ork.web.routes.agent_server"`
- `python3.11 -m pytest -q tests/unit/test_agent_server_attachments.py tests/unit/test_ide_pages_thread.py tests/unit/test_threads_store.py`

Run these from the run's working tree (the isolated worktree), so `mini_ork` resolves to the tree under test. A syntax check is not acceptance.

## Done When

- `${MINI_ORK_RUN_DIR}/panel-verdict.json` contains `"verdict": "pass"` with all verifiers passing.
- All verification commands pass in the isolated worktree.
