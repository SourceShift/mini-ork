# Dark tables — the loop writes what no route reads

Status: **open**
Opened: 2026-10-25
Found by: `tests/test_learning_routes.py::test_no_populated_table_is_dark`
against this machine's real `./.mini-ork/state.db`.

## Why it matters

The guard exists because "dark data is indistinguishable from absent data". It
skips in a clean checkout, so CI has never seen this; on a host that has run the
loop for weeks, 17 populated tables are served by no route under
`mini_ork/web/`. Each one is a fact the system paid to produce and cannot show.

`lesson_injections` was fixed the same day (`GET /api/v1/learning/injections`).
The rest are listed here so the finding is not lost when the guard is
deselected from a local merge gate.

## Tasks

### 1. Code-findings plane (3 tables)

Status: not started · Last worked: — · Remaining: 3 endpoints or one nested view

- `code_findings`, `code_findings_runs`, `code_findings_landing`
- These are the code-finding analyser's output and where each finding landed.
  Suggested: `GET /api/v1/code-findings` (newest per run, filterable by status)
  plus `…/:finding_id/landing`, has_table-guarded like the learning routes.

### 2. Context items and utility (2 tables)

Status: not started · Last worked: — · Remaining: 1 endpoint + a join

- `context_item_runs`, `context_item_utility`
- Which context item was given to which run, and what it was worth. This is the
  same question as `lesson_injections` for a different asset — one endpoint
  (`GET /api/v1/learning/context-items`) covering both tables.

### 3. Themes and gradients (3 tables)

Status: not started · Last worked: — · Remaining: 1 endpoint

- `gradient_theme`, `theme_idf`, `lesson_themes`
- The induced theme vocabulary. Suggested: fold into `GET /api/v1/learning/patterns`
  as a `themes` key rather than a new top-level route.

### 4. Diagnosis and attempts (2 tables)

Status: not started · Last worked: — · Remaining: 1 endpoint

- `diagnosis_ledger`, `epic_attempts`
- Per-epic failure diagnosis and retry history — belongs under the run/epic
  surface, not learning. `GET /api/v1/runs/:run_id/diagnoses`.

### 5. Run-internals that may deserve EXEMPT-with-reason (7 tables)

Status: not started · Last worked: — · Remaining: a decision per table

- `thread_messages`, `verifier_results`, `revise_rounds`,
  `node_completion_checkpoints`, `remote_procs`, `collapse_history`,
  `mo_inbox_gates`
- Each is read by a *page* or a CLI, not by an HTTP route (`thread_messages` by
  the thread UI, `verifier_results` by the run detail page). The guard scans
  only `mini_ork/web/routes/`; the honest fix is either to expose them there or
  to extend `EXEMPT` with the reason "served by <surface>, not HTTP" — one line
  each, no blanket exemption.

## Definition of done

`python3.11 -m pytest tests/test_learning_routes.py::test_no_populated_table_is_dark`
passes on this machine unchanged (no deselect), and the `EXEMPT` set holds only
tables whose reason names another surface.
