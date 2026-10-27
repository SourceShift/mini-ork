# RSI Tier-1 #4b: the apply loop refuses to write a frozen path, and stays inside the recipe's prompts

## Goal

The frozen kernel (`config/kernel-frozen.yaml` ∪
`mini_ork/vcs/repo_integrity_guard.FROZEN_PATHS_DEFAULT`) is checked at publish
(`publisher_authored_patch.land_patch`), at the framework-edit verifier (auto-triage
runs only), in the RSI cycle (`mini_ork/rsi/select.py`) and at merge
(`mini_ork/gates/merge_risk.py`). The apply loop checks it **nowhere**:

- `mini_ork/cli/apply.py` `apply_mutation` writes `target_file` directly, with
  no frozen check.
- `mini-ork apply` passes the CLI target through verbatim (`main`:
  `target_file = target`), so any path can be a target.
- `_prompt_file_for` joins a `prompts/<x>` or `agent.<role>.prompt` target onto
  the recipe dir without confining it, so `prompts/../verifiers/static-check.py`
  resolves to the frozen verifier when that file exists.

Make the apply loop refuse a frozen target (and record the refusal), and confine
target resolution to the recipe's own prompt files.

All paths are relative to the repository root.

## Mechanism (exact spec)

### 1. `mini_ork/cli/apply.py`

- **New helper** `_frozen_target(target_file: str) -> list[str]`.
  - Returns `[]` when `target_file` is empty.
  - `root = os.environ.get("MINI_ORK_ROOT") or os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))`.
    Do **not** call `_resolve_root()` here: it writes `MINI_ORK_ROOT` into the
    environment.
  - `real_root = os.path.realpath(root)`; `real = os.path.realpath(target_file)`;
    `rel = os.path.relpath(real, real_root).replace(os.sep, "/")`.
  - Returns `[]` when `rel == ".."` or `rel.startswith("../")` (outside the engine tree).
  - Otherwise returns `frozen_hits([rel], enforced_frozen_paths(real_root))`,
    importing both from `mini_ork.vcs.repo_integrity_guard` locally inside the helper.
  - Because it resolves with `realpath`, a symlink inside the tree that points at
    a frozen file is caught.
- **New helper** `_record_frozen_refusal(paths: list[str], *, candidate_id: str = "", db: str | None = None) -> bool`.
  - Inside one `try`: `from mini_ork.stores import safety_events`;
    `res = safety_events.emit("frozen-kernel-edit", "high", json.dumps({"run_id": "", "paths": list(paths), "boundary": "apply", "candidate_id": candidate_id}), db=_db_path(db))`;
    return `bool(res.get("id"))`.
  - Returns `False` on any exception (including `_db_path` raising when
    `MINI_ORK_DB` is unset). It never raises.
- **`MO_ALLOW_FROZEN_EDIT` is NOT consulted anywhere in `apply.py`.** The apply
  loop is autonomous; the operator token is for a person's deliberate edit through
  a run, not for the loop.
- **`apply_mutation`.** Directly after the existing dry-run / not-enabled early
  return, and before `mode = os.environ.get("MO_APPLY_MODE", "append")`:
  - `hits = _frozen_target(target_file)`;
  - when `hits` is non-empty: write
    `f"apply_apply_mutation: refusing frozen path(s) {', '.join(hits)} (config/kernel-frozen.yaml); the apply loop never edits the frozen kernel\n"`
    to stderr, call `_record_frozen_refusal(hits, candidate_id=candidate_id, db=db)`,
    and `return ""`. The file is not opened for writing, no sidecar and no
    `version_registry` row are written.
- **`apply_run`.** Directly after the line `dry_flag = "1" if dry_run == "1" else "0"`
  and before step 1 (`pick_candidate`):
  - `frozen = _frozen_target(target_file)`;
  - when non-empty:
    - `rationale = f"frozen path: {', '.join(frozen)} is in the frozen kernel (config/kernel-frozen.yaml); the apply loop never edits it"`;
    - `aid = attempt_record(task_class, target_kind, target_name, "none", "", "", "", "", "", "", "", "rejected", rationale, dry_flag, apply_enabled, db=db)`;
    - `_record_frozen_refusal(frozen, db=db)`;
    - write `aid + "\n"` then
      `f'{{"decision":"rejected","task_class":"{task_class}","target":"{target_name}","reason":"frozen_path"}}\n'`
      to stdout;
    - `return 0`.
  - This runs whatever `MO_APPLY_ENABLED` / `MO_APPLY_DRY_RUN` say: a frozen target
    is refused and recorded even in a dry run.
- **`_prompt_file_for` confinement.** Add a helper
  `_inside(path: str, base: str) -> bool` returning
  `os.path.realpath(path).startswith(os.path.realpath(base) + os.sep)`.
  - `agent.<role>.prompt` branch: a candidate path is returned only when it is a
    file AND `_inside(path, os.path.join(recipe_dir, "prompts"))`; otherwise try
    the next name, then `""`.
  - `prompts/` branch: return `path` only when `os.path.isfile(path)` AND
    `_inside(path, os.path.join(recipe_dir, "prompts"))`, else `""`.
  - `harness.` branch (`_harness_target_file_for`): return `cand` only when it is
    a file AND `_inside(cand, recipe_dir)`, else `""`.
  - The returned value stays the joined (non-realpath) path, exactly as today.

## Files in scope (touch ONLY these)

- `mini_ork/cli/apply.py`
- `tests/unit/test_apply_frozen_guard.py` (new)

Do NOT modify any other file.

## Tests (`tests/unit/test_apply_frozen_guard.py`: hermetic temp engine root + temp sqlite, no network)

**Setup.** `import mini_ork.cli.apply as ap`. Scrub with monkeypatch every
variable in `_APPLY_ENV` from `tests/unit/test_cli_apply_py.py` (copy the list)
plus `MO_ALLOW_FROZEN_EDIT`. Build `root = tmp_path / "engine"` with NO
`config/kernel-frozen.yaml` (so `enforced_frozen_paths` falls back to the built-in
floor) and these files:

- `mini_ork/recovery/auto_repair.py` containing `FROZEN = 1\n` (a floor entry);
- `recipes/demo/prompts/ok.md` containing `# ok\n`;
- `recipes/demo/verifiers/x.md` containing `# x\n`.

`monkeypatch.setenv("MINI_ORK_ROOT", str(root))`. DB: `dbp = tmp_path / "state.db"`,
`sqlite3.connect(dbp).executescript((REPO / "db/migrations/0036_safety_events.sql").read_text())`,
`monkeypatch.setenv("MINI_ORK_DB", str(dbp))`; set `ap._SCHEMA_INIT = False` before
and after each test (`ensure_tables` then creates `apply_attempts` itself).
`FROZEN = root / "mini_ork/recovery/auto_repair.py"`.

Tests:

- **`_frozen_target`.** `ap._frozen_target(str(FROZEN)) == ["mini_ork/recovery/auto_repair.py"]`;
  `ap._frozen_target(str(root / "recipes/demo/prompts/ok.md")) == []`;
  `ap._frozen_target(str(tmp_path / "elsewhere.md")) == []`; `ap._frozen_target("") == []`.
- **`apply_run` refuses before picking.** `ap.apply_run("demo", "prompt_file", "x", str(FROZEN), db=str(dbp))`
  returns `0`; `apply_attempts` has exactly one row with `decision='rejected'`,
  `source_kind='none'` and a `rationale` starting with
  `"frozen path: mini_ork/recovery/auto_repair.py"`; `safety_events` has exactly
  one row with `tripwire_id='frozen-kernel-edit'`, `severity='high'`, evidence
  `boundary == "apply"` and `paths == ["mini_ork/recovery/auto_repair.py"]`;
  `FROZEN` still reads `FROZEN = 1\n`.
- **`apply_mutation` refuses.** With `MO_APPLY_ENABLED=1`,
  `ap.apply_mutation("cand-1", str(FROZEN), "NEW-DIRECTIVE", db=str(dbp))` returns
  `""`; the file is unchanged; one `safety_events` row whose evidence has
  `candidate_id == "cand-1"` and `boundary == "apply"`.
- **The token does not unlock it.** The same with `MO_ALLOW_FROZEN_EDIT=1` also
  set: still `""`, file unchanged.
- **A symlink to a frozen file is caught.** Create
  `root / "recipes/demo/prompts/evil.md"` as a symlink to `FROZEN`;
  `apply_mutation` on it (enabled) returns `""` and `FROZEN` is unchanged.
- **An ordinary target still lands.** With `MO_APPLY_ENABLED=1`,
  `apply_mutation("cand-2", str(root / "recipes/demo/prompts/ok.md"), "NEW-DIRECTIVE", db=str(dbp))`
  leaves a file containing `NEW-DIRECTIVE`, and `safety_events` stays empty.
- **No DB table, still refused.** Point `MINI_ORK_DB` at an empty sqlite file:
  `apply_mutation` on `FROZEN` (enabled) returns `""` and the file is unchanged.
- **Target confinement.** With `rd = str(root / "recipes/demo")`:
  `ap._prompt_file_for(rd, "prompts/ok.md").endswith("recipes/demo/prompts/ok.md")`;
  `ap._prompt_file_for(rd, "prompts/../verifiers/x.md") == ""`;
  `ap._prompt_file_for(rd, "agent.ok.prompt").endswith("prompts/ok.md")`;
  `ap._prompt_file_for(rd, "agent.../verifiers/x.prompt") == ""`.

## Verification commands

- `pytest -q tests/unit/test_apply_frozen_guard.py tests/unit/test_cli_apply_py.py tests/unit/test_apply_harness_targets.py`
- `pytest -q tests/unit/test_apply_significance.py tests/unit/test_eco6_k7_apply_hygiene.py tests/unit/test_frozen_kernel.py tests/unit/test_frozen_paths_enforced.py`

## Self-application measurement

```measure
python3 -m pytest -q tests/unit/test_apply_frozen_guard.py --junitxml="${MINI_ORK_RUN_DIR}/apply-frozen-guard-junit.xml"
```

## Required artifacts — declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/apply-frozen-guard-junit.xml`

## Success criteria

- Both verification commands pass with every pre-existing test unchanged.
- `ruff check --select F,E9 mini_ork/cli/apply.py` is clean.

## Rules

Edit files directly; do not emit unified diffs. Execute this spec exactly; do not
research and do not add lens or drafter nodes.
