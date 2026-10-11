# Per-user LLM lane config: tracked agents.yaml = template, agents.local.yaml = personal overlay

## Goal

`.mini-ork/config/agents.yaml` is TRACKED in git and is the team default. Today a
user who wants different lanes (deepseek vs kimi vs minimax…) must edit that
tracked file, and a commit of it silently changes every other user's lanes.
`MINI_ORK_AGENTS` exists but only two side paths honour it; dispatch ignores it.

Make the tracked file a TEMPLATE and give each user a git-ignored PERSONAL
OVERLAY that affects only them. With no overlay present, behaviour must be
byte-for-byte what it is today.

Work ONLY in the worktree `/Volumes/docker-ssd/ps/mini-ork-worktrees/agents-local-overlay`.

## Files in scope

- `/Volumes/docker-ssd/ps/mini-ork-worktrees/agents-local-overlay/mini_ork/dispatch/agents_config.py` — NEW: the single resolver.
- `/Volumes/docker-ssd/ps/mini-ork-worktrees/agents-local-overlay/mini_ork/dispatch/config_resolve.py` — `resolve_agents_yaml()` and `snapshot_run_config()` use the new resolver.
- `/Volumes/docker-ssd/ps/mini-ork-worktrees/agents-local-overlay/mini_ork/steering/decision_service.py` — `resolve_agents_yaml()` uses it.
- `/Volumes/docker-ssd/ps/mini-ork-worktrees/agents-local-overlay/mini_ork/web/recipes.py` — `load_lanes()` uses it.
- `/Volumes/docker-ssd/ps/mini-ork-worktrees/agents-local-overlay/mini_ork/gates/native_gates.py` — coalition gate's agents path uses it.
- `/Volumes/docker-ssd/ps/mini-ork-worktrees/agents-local-overlay/mini_ork/cli/main.py` — profile step's agents path uses it; update the `MINI_ORK_AGENTS` help line.
- `/Volumes/docker-ssd/ps/mini-ork-worktrees/agents-local-overlay/.mini-ork/config/.gitignore` — NEW: ignore `agents.local.yaml` and `.agents.effective.yaml`.
- `/Volumes/docker-ssd/ps/mini-ork-worktrees/agents-local-overlay/tests/unit/test_agents_local_overlay_py.py` — NEW tests.

Do NOT edit `.mini-ork/config/agents.yaml`, the repo-root `.gitignore`, or any other file.

## Required behaviour (mini_ork/dispatch/agents_config.py)

1. `template_path(home=None) -> str`: `$MINI_ORK_HOME/config/agents.yaml` if it
   exists (home arg overrides the env), else `$MINI_ORK_ROOT/config/agents.yaml`
   (default root "."). This is exactly today's global resolution.
2. `personal_path(home=None) -> str | None`: `$MINI_ORK_AGENTS` if set and the file
   exists; else `<home>/config/agents.local.yaml` if it exists; else None.
3. `merge(base: dict, over: dict) -> dict`: recursive — nested mappings merge key
   by key; any non-mapping value (scalar, list) in `over` replaces the base value.
   A key set to null in `over` removes it. Inputs are never mutated.
4. `effective_path(home=None) -> str`: when `personal_path()` is None return
   `template_path()` UNCHANGED (no file written). Otherwise write the merged YAML
   to `<home>/config/.agents.effective.yaml` — only when its content changed
   (atomic write via temp file + os.replace) — and return that path. The written
   file starts with a comment naming both source files.
5. A malformed/unreadable personal file must NOT silently fall back: raise
   `ValueError` naming the file, so the user sees their own mistake.

## Wiring

- `config_resolve.resolve_agents_yaml()`: keep the run-dir-first branch exactly;
  replace the home/root fallback with `agents_config.effective_path()`.
- `config_resolve.snapshot_run_config()`: snapshot SOURCE becomes
  `agents_config.effective_path()` (so a run freezes the user's merged policy).
  Keep idempotence ("never overwrite an existing snapshot").
- `decision_service.resolve_agents_yaml()`: same as config_resolve.
- `web/recipes.load_lanes(home)`: read `agents_config.effective_path(home)`.
- `native_gates` coalition and `main.py` profile step: replace
  `os.environ.get("MINI_ORK_AGENTS") or <join>` with `agents_config.effective_path()`.
- `main.py` help: `MINI_ORK_AGENTS  personal lane overlay merged over $MINI_ORK_HOME/config/agents.yaml (default: config/agents.local.yaml)`.

## Tests (tests/unit/test_agents_local_overlay_py.py)

Use tmp dirs and monkeypatched env (MINI_ORK_HOME, MINI_ORK_ROOT, MINI_ORK_AGENTS, MINI_ORK_RUN_DIR):
- no overlay → `effective_path()` == template path and no `.agents.effective.yaml` is created;
- overlay `lanes: {implementer: deepseek}` over a template with several lanes → effective
  lanes keep every template lane and change only `implementer`;
- `MINI_ORK_AGENTS` pointing elsewhere wins over `agents.local.yaml`;
- null in overlay removes a key; lists are replaced not concatenated;
- malformed overlay raises ValueError naming the file;
- `config_resolve.snapshot_run_config(run_dir)` freezes the MERGED content;
- `decision_service.default_lane("implementer")` returns the overlay's value;
- the template file's bytes are unchanged after all of the above;
- `git check-ignore` reports `.mini-ork/config/agents.local.yaml` ignored in the worktree
  (skip the assertion if git is unavailable).

## Definition of Done

- `python -m pytest -q tests/unit/test_agents_local_overlay_py.py` passes, and existing tests
  touching config_resolve / decision_service / web recipes still pass.
- `ruff check` clean on every changed file.
- With no agents.local.yaml present, resolution returns the same path as before.
