# Surface the controllable-feature catalogue as an IDE page

## Goal

The controllable-feature registry (`mini_ork/features/registry.py`) is the single
source of truth for the run knobs a user may control and what each costs. It is
now reachable three ways — the `mini-ork features` CLI, the `/wizard` ACP
command, and `GET /api/v1/features` (just merged) — but **no IDE page shows it**.
Add a `features` page so the mini-ork IDE activity bar lists the catalogue.

## Acceptance

- AC1 — new module `mini_ork/ide_pages/features.py` exposing
  `build(home: Path, tab: str | None, args: dict[str, str]) -> dict`, structured
  like the sibling `mini_ork/ide_pages/setup.py` (module docstring, `from
  mini_ork.ide_pages import spec as S`, `TABS`, a `build`). It reads
  `mini_ork.features.catalogue_json()` — the registry SSOT. Do NOT hand-maintain
  a feature list.
- AC2 — register `"features": "Features"` in the `PAGES` dict in
  `mini_ork/ide_pages/__init__.py` (beside the existing 15 keys).
- AC3 — add a `"features"` arm to BOTH `glyph()` and `default_label()` in
  `ui/src/page.rs` (the match blocks near lines 41 and 57), in the style of the
  existing arms. NOTE: the old mirror path
  `zed-mini-ork/crates/mini_ork_ui/src/page.rs` NO LONGER EXISTS — `ui/src/page.rs`
  is the live Rust file (the crate moved to `ui/`).
- AC4 — the page shows the catalogue: a header with `premium_threshold`, then one
  row per feature giving its id, label, tier (premium when its own multiplier
  exceeds the threshold, else baseline) and cost multiplier. Render with the
  EXISTING shapes from `mini_ork/ide_pages/spec.py` (e.g. `S.table`, `S.kv`,
  `S.chips`, `S.callout`, `S.hero`) — do NOT add a new shape and do NOT edit
  `spec.py`. Follow `setup.py` for how shapes are assembled into the returned dict.
- AC5 — a new unit test asserts `build_page(home, "features")` returns
  `ok=True` with at least one feature row, and that `"features"` is in `PAGES`.
  Mirror what `tests/unit/` already does for another ide page.

## Files in scope

- `mini_ork/ide_pages/features.py` (new)
- `mini_ork/ide_pages/__init__.py`
- `ui/src/page.rs`
- `tests/unit/test_ide_pages_features.py` (new)

## Out of scope

- Do NOT change `mini_ork/features/registry.py`, `mini_ork/web/routes/features.py`,
  or `mini_ork/ide_pages/spec.py`.
- Do NOT add any interactive control (checkbox/toggle/form) or a write path —
  this is a READ-ONLY listing page. The wizard's selection UI is a separate task.
- Do NOT touch `mini_ork/cli/main.py` or any other `ide_pages/*` module.
- Do NOT add a new Rust widget or component — the glyph/label arms are the only
  Rust change.

## Verification command

`python3 -m pytest tests/unit/test_ide_pages_features.py -q`
