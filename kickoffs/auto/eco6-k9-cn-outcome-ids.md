# ECOSYSTEM §6 · K9: persist the ContextNest context the engine injects, so outcomes can be credited

## Goal

ContextNest's outcome loop credits the atoms a run was shown with the run's
verdict. Commit 1aeeec09 made `cn_client.render_atoms_md` print each hit's
`id=cn-<32 hex>`, which matches the settle regex
(`_CN_ATOM_ID_RE` in `mini_ork/cli/execute.py`). It still credits nothing:

- `_collect_cn_atom_ids(run_dir)` only reads `<run_dir>/cn_prefetch/*.md`
  (written solely by the Claude Code hook `hooks/subagent-prefetch.sh`) and
  `id`/`atom_id` fields in `<run_dir>/learned/*.json` (which hold `th-`/`pat-`
  ids).
- The engine's own CN injection never writes to the run dir:
  `mini_ork/context_assembler.py::contextnest_atoms_md` and
  `mini_ork/cli/plan.py::_contextnest_atoms_md` return a capsule (no atom ids)
  or, as a fallback, `render_atoms_md(retrieve(...))` (with ids), and the text
  goes straight into the prompt.
- Evidence: the newest `cn_prefetch` file is from 2026-08-23; none of the runs
  after 1aeeec09 has a `cn_prefetch` dir; no run dir has a `cn-outcomes.json`.

Persist exactly the block the engine injected, so the existing settle finds the
ids that block carries. Credit only what the run was actually shown: do not
retrieve extra atoms just to have ids to credit.

All paths are relative to the repository root.

## Mechanism (exact spec)

1. `mini_ork/cn_client.py`: add
   ```python
   def persist_prefetch(block: str, label: str) -> str | None:
   ```
   - If `block` is empty/whitespace, or `MINI_ORK_RUN_DIR` is unset/empty, or
     the dir does not exist, return `None` and write nothing.
   - Otherwise append `block` (followed by a newline) to
     `$MINI_ORK_RUN_DIR/cn_prefetch/engine-<label>.md`, creating the
     `cn_prefetch` dir if needed. `label` is sanitized to `[A-Za-z0-9_-]`
     (other characters become `_`). Return the file path.
   - Never raise: any `OSError` returns `None`.
2. `mini_ork/context_assembler.py::contextnest_atoms_md`: compute the return
   value as today, then call `persist_prefetch(<returned text>, "context")`
   (through the same `client` object when it has the attribute, else
   `mini_ork.cn_client.persist_prefetch`), and return the same text unchanged.
3. `mini_ork/cli/plan.py::_contextnest_atoms_md`: same, with label `"planner"`,
   inside the existing `try` (the function must still never raise).
4. Nothing in `mini_ork/cli/execute.py` changes: `_collect_cn_atom_ids` already
   reads every `*.md` in `cn_prefetch/`.

## Files in scope (touch ONLY these)

- `mini_ork/cn_client.py`
- `mini_ork/context_assembler.py`
- `mini_ork/cli/plan.py`
- `tests/unit/test_eco6_k9_cn_prefetch.py` (new)

Do NOT modify `mini_ork/cli/execute.py` or the hooks.

## Tests (`tests/unit/test_eco6_k9_cn_prefetch.py`, hermetic, no network)

Use a stub client object with `available() -> True`, `capsule(...) -> ""`
(forces the atoms path), `retrieve(q, n)` returning one hit
`{"similarity": 0.8, "metadata": {"kind": "decision", "ts": "2026-10-25T00:00:00"}, "session_id": "898364fe-a053", "id": "cn-275a1cef9eb1ba9c328a5fee8230defb", "content": "c"}`,
and the real `render_atoms_md` / `persist_prefetch`.

- **The block lands in the run dir.** With `MINI_ORK_RUN_DIR=<tmp>` and a brief
  file, `contextnest_atoms_md(brief, client=stub)` returns text containing
  `id=cn-275a1cef9eb1ba9c328a5fee8230defb`, and
  `<tmp>/cn_prefetch/engine-context.md` contains the same text.
- **The settle finds it.** `mini_ork.cli.execute._collect_cn_atom_ids(<tmp>)`
  returns `["cn-275a1cef9eb1ba9c328a5fee8230defb"]`.
- **Capsule path persists the capsule.** A stub whose `capsule` returns a
  200-character text with a `## ` heading: the capsule block is returned and
  written to `engine-context.md`; `_collect_cn_atom_ids` returns `[]` (a capsule
  has no ids, and nothing extra is retrieved — assert `retrieve` was not
  called).
- **No run dir, no write.** With `MINI_ORK_RUN_DIR` unset, nothing is written
  and the return value is unchanged.
- **Appends, not overwrites.** Two calls append both blocks to the same file.
- **Never raises.** `persist_prefetch("x", "a/b")` with `MINI_ORK_RUN_DIR`
  pointing at a file (not a dir) returns `None`; the label `a/b` becomes
  `engine-a_b.md` in the normal case.
- **Planner path.** `mini_ork.cli.plan._contextnest_atoms_md(brief)` with
  `mini_ork.cn_client` monkeypatched to the stub functions writes
  `engine-planner.md`.

## Verification commands

- `python3.11 -m pytest -q -p no:asyncio tests/unit/test_eco6_k9_cn_prefetch.py tests/unit/test_cn_atom_id_settle.py tests/unit/test_cn_client_py.py tests/unit/test_context_assembler_py.py tests/unit/test_cn_ui_verdict_outcome.py`

## Self-application measurement

```measure
python3.11 -m pytest -q -p no:asyncio tests/unit/test_eco6_k9_cn_prefetch.py --junitxml="${MINI_ORK_RUN_DIR}/eco6-k9-junit.xml"
```

## Required artifacts — declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/eco6-k9-junit.xml`

## Success criteria

- The verification command passes with every pre-existing test unchanged.
- `ruff check --select F,E9 mini_ork/cn_client.py mini_ork/context_assembler.py mini_ork/cli/plan.py` is clean.
