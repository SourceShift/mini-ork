# IDE page spec v2: section helpers for the Orca-style run workbench, and reviewer findings that actually show

## Why

The mini-ork IDE (Zed fork) is being redesigned in Orca's style. The plan is
`/Users/admin/.claude/plans/twinkling-churning-feather.md`.

Pages are JSON specs built by `mini_ork/ide_pages/*.py` through the helpers in
`mini_ork/ide_pages/spec.py`, and drawn by the IDE. This run lays the Python foundation:

1. **Version negotiation.** The new IDE will set `MINI_ORK_IDE_SPEC=2` on every `mini-ork board …`
   call. Builders need `ide_level()` to decide whether to emit the new section types. An older IDE
   (env unset) must keep getting today's pages.
2. **Ten new section/helper shapes** used by the coming run workbench. Pages never hand-write
   shapes, so the helpers come first.
3. **The run page hides the reviewer's findings.** `review-reviewer.json` has
   `findings: [{file, line, severity, snippet, issue}]` plus string `notes` / `reasons`.
   - `node_changes._review_items` (~:196) does `review.get("notes") or review.get("findings")`,
     so non-empty string notes hide the structured findings. Example: run
     `fl-k5a-20261007-233207` has both.
   - `_finding_item` (~:620) reads `title`/`text`/`note` but never `issue`, so a findings-only
     review renders blank titles.

## Files in scope (touch ONLY these)

- `mini_ork/ide_pages/spec.py`: add `ide_level()` + the helpers below + module docstring entries.
  Do not change existing helpers.
- `mini_ork/ide_pages/node_changes.py`: ONLY `_review_items` and `_finding_item`
- `tests/unit/test_ide_pages_spec_v2.py` (new)
- `tests/unit/test_ide_pages_node_changes.py`: add tests only

Do NOT modify any other file.

## Changes (exact)

### A. `spec.py`

- **`ide_level() -> int`:** `int(os.environ.get("MINI_ORK_IDE_SPEC") or 1)`. Anything
  non-numeric or < 1 → 1. Read on each call; do not cache.
- **New section helpers.** All go through the existing `_section(kind, title, data, ...)`, so
  `note`/`actions`/`full`/`col`/`row_span` keep working. Colours are the existing names (`COLOURS`);
  state words are `done running failed skipped pending needs_you`.
  - `hero(title, *, goal="", criteria=(), pill=None, meta=(), **opt)` → type `hero`,
    data `{"goal", "criteria": [str], "pill": {"t","c"}|None, "meta": [meta_item]}`;
    `full=True` by default.
  - `meta_item(t, c="sub", *, mono=False)` → `{"t","c","mono"}`.
  - `triage(text, *, tone="muted", icon="", detail="", counts=(), actions=(), menu=(), **opt)` →
    type `triage`, title `""`, data `{"text","tone","icon","detail","counts":[{"t","c"}],"menu":[btn]}`.
    Use the section's existing `actions` field for `actions`. `full=True` by default.
  - `callout(title, text_md="", *, tone="orange", actions=(), **opt)` → type `callout`,
    data `{"text_md","tone"}`; `actions` via the section field.
  - `story_step(step_id, title, *, kind="", state="pending", lane="", model="", headline=None,
    meta=(), dur="", cost="", open=False, do=None, body=())`:
    - returns `{"id","title","kind","state","lane","model","headline":{"t","c"}|None,"meta":[meta_item],"dur","cost","open","do","body":[block]}`;
    - `headline` may be given as a `(t, c)` tuple or a str (c=`sub`).
  - Block helpers (each returns a dict with `"kind"`):
    - `block_md(text)` → `{"kind":"md","text"}`
    - `block_lines(lines)` → `{"kind":"lines","lines":[{"t","c"}]}`, accepting str or `(t, c)`,
      like `code()`
    - `block_files(files, *, diff="", diff_note="", commits=())` → `{"kind":"files", ...}`
    - `block_findings(items, *, verdict=None, reasons=())` → `{"kind":"findings", ...}`
    - `block_checks(rows, *, summary=None)` → `{"kind":"checks", ...}`
  - `story(title, steps, **opt)` → type `story`, data `{"steps": [...]}`.
  - `file_entry(path, *, abs="", status="M", added=0, removed=0)` → `{"path","abs","status","added","removed"}`.
  - `files(title, files, *, diff="", diff_note="", commits=(), **opt)` → type `files`.
  - `finding(issue, *, severity="", file="", line=None, abs="", snippet="", source="")` → dict.
  - `findings(title, items, *, verdict=None, reasons=(), **opt)` → type `findings`,
    data `{"verdict": {"t","c"}|None, "reasons": [str], "items": [...]}`. `verdict` accepts `(t, c)` or a str.
  - `check_row(name, state, *, detail="", log=(), do=None)` → `{"name","state","detail","log":[{"t","c"}],"do"}`.
  - `checks(title, rows, *, summary=None, **opt)` → type `checks`. When `summary` is None,
    compute `{"passing","failing","pending","na"}` from row states:
    - passing: `pass`, `done`, `ok`
    - failing: `fail`, `failed`, `error`
    - pending: `pending`, `running`
    - na: everything else
  - `agent_row(node_id, state, *, lane="", model="", step="", last="", cost="", dur="", do=None)` → dict.
  - `agents(title, rows, **opt)` → type `agents`.
  - `composer(placeholder, cli_args, **opt)` → type `composer`, data `{"placeholder", "cli": [str]}`.
    The IDE appends the typed text as the last argument.
  - `column(title, cards, *, c="sub")` → `{"title","c","count": len(cards),"cards": list(cards)}`.
  - `columns(title, cols, **opt)` → type `columns`.
- **Module docstring:** one line per new section type in the existing "Sections" list, and one
  line for `ide_level()` / `MINI_ORK_IDE_SPEC`.

### B. `node_changes.py`

- **`_review_items`:** from the review JSON, take structured `findings` (list of dicts) FIRST via
  `_finding_item`. Then append string `notes`, then `reasons` (strings → `S.item(text, "")`).
  - If neither is a list, behave as today.
  - The markdown fallback still runs only when no items came from the JSON.
- **`_finding_item`:**
  - The text falls back through `title`, `text`, `note`, `issue`, `summary`.
  - When a `snippet` is present, append it to `sub` as `" · " + snippet` (single line, max 100
    chars, `…` when cut).
  - Severity `blocker` maps like `critical`.
  - Keep path resolution and the Open act unchanged.

## Tests

### `tests/unit/test_ide_pages_spec_v2.py`

- `ide_level()`: unset → 1; `"2"` → 2; `"x"` → 1; `"0"` → 1 (monkeypatch env).
- **Every new helper returns the documented keys.**
  - Section helpers carry `type` and the `_section` layout keys.
  - `hero` and `triage` default to `full`.
  - `checks` summary is computed from row states.
  - `column.count` == number of cards.
  - `story_step` accepts a tuple or str headline.
- **JSON round-trip:** `json.dumps` of a page holding one of each new section succeeds.

### `tests/unit/test_ide_pages_node_changes.py` (add)

- A review JSON with non-empty `notes` (strings) AND `findings` (dicts with `issue`, `file`,
  `line`, `severity`, `snippet`):
  - the items list starts with the findings, with their `issue` as the text, `file:line · snippet`
    as the sub, and high severity → red mark;
  - the notes come after.
- A findings-only review: no blank titles.

## Verification command

The command that proves this run succeeded (per file; the host kills one CPU-bound process
that runs longer than 30 s):

```bash
for f in tests/unit/test_ide_pages_spec_v2.py tests/unit/test_ide_pages_node_changes.py tests/unit/test_ide_pages_run.py; do env -u MINI_ORK_RUN_ID -u MINI_ORK_DB -u MINI_ORK_HOME python3.11 -m pytest -q -p no:asyncio "$f" || exit 1; sleep 3; done   # must exit 0
```

## Done when

- The verification command → 0 failed. Paste the summary lines.
- `uvx ruff check mini_ork/ide_pages/spec.py mini_ork/ide_pages/node_changes.py tests/unit/test_ide_pages_spec_v2.py` → clean.
- Live proof (read-only): `python3.11 -c` calling `node_changes._review_items` on run
  `fl-k5a-20261007-233207`'s reviewer node (live home `/Volumes/docker-ssd/ps/mini-ork/.mini-ork`)
  shows the findings first, each with a non-empty title. Paste the first 3 items.
- `git diff --stat` touches only the files in scope.
