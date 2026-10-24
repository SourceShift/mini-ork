# RSI Tier-0 #5 (loop defect D1): the post-run reflect child gets a durable cwd and the effective lane

## Goal

The recursive-self-improve loop has never produced a learning artifact. In its
home, `gradient_records` and `emergent_patterns` are both 0, and the post-run
reflect pass failed 13 of 13 calls on 2026-10-23. There are two independent
causes, and fixing one alone still yields 0 rows:

1. **The cwd is deleted under the child.**
   - `mini_ork/cli/self_improve.py` runs `mini-ork run` with `cwd=<iteration worktree>`.
   - It removes that worktree with `git worktree remove --force` about 2 s after
     the run returns.
   - The detached reflect child is spawned in `mini_ork/cli/main.py` (the
     post-run reflect block: the inline `subprocess.run(reflect_argv, …)` and the
     detached `subprocess.Popen(reflect_argv, …)`). Neither call passes `cwd=`,
     so the child inherits that worktree.
   - Every later `os.getcwd()` in the child raises
     `[Errno 2] No such file or directory` (`llm_calls` 14–24 of run
     `self-improve-iter-1-20261023054815`).
2. **The lane comes from the raw template.**
   - `mini_ork/cli/reflect.py:_resolve_reflect_model` reads
     `<home>/config/agents.yaml`, then `<root>/config/agents.yaml`, raw.
   - It ignores the per-user overlay (`agents.local.yaml` / `$MINI_ORK_AGENTS`)
     and the run-dir snapshot that dispatch honours.
   - So gradient-extract went to the template's dead `codex` lane (HTTP 400,
     `llm_calls` id 13), while the run's effective policy said `reflector: glm`.

All paths are relative to the repository root.

## Mechanism (exact spec)

### 1. `mini_ork/cli/main.py`, in the post-run reflect block

Right after `has_log_dir` and `reflect_log` are computed, add:

```python
# Never inherit the caller's cwd: the RSI runner launches `mini-ork run` from
# the iteration worktree and removes it ~2 s after the run returns, so every
# later os.getcwd() in the reflect child raised ENOENT. The run dir (else
# home) outlives the run.
reflect_cwd = os.path.abspath(reflect_run_dir if has_log_dir else home)
if not os.path.isdir(reflect_cwd):
    reflect_cwd = None
```

Then pass `cwd=reflect_cwd` to **both** spawns of `reflect_argv`: the inline
`subprocess.run(...)` and the detached `subprocess.Popen(...)`. The `isdir`
guard matters, because the inline path catches only `TimeoutExpired`. Do not
change `reflect_env` or `MO_TARGET_CWD`.

### 2. `mini_ork/cli/reflect.py`, `_resolve_reflect_model(lane, mini_ork_home)`

Replace the raw-YAML candidate loop with the reader dispatch uses:

- Import locally to avoid a cycle:
  `from mini_ork.dispatch.llm_dispatch import _effective_lanes`.
- Compute `lanes = _effective_lanes(str(MINI_ORK_ROOT), mini_ork_home) or {}`.
- When that raises (a broken overlay raises `ValueError`):
  - print `reflect: lane policy unreadable (<exc>); using lane <lane!r>` to
    stderr;
  - return `lane`.
- Return `str(lanes.get(lane) or lanes.get("reflector") or lane)`.
- Keep the name and signature, and keep it in `__all__` if listed.
- Update the docstring: the effective policy, run-dir snapshot > home >
  engine template, with the overlay merged.

An explicit `MINI_ORK_GRADIENT_MODEL` at the caller keeps winning (the caller
is unchanged).

**Out of scope:** the `"codex"` literals in `mini_ork/learning/gradient_extractor.py`
and `mini_ork/learning/pattern_induction.py`, `self_improve.py`, recipes and
schemas.

## Files in scope (touch ONLY these)

- `mini_ork/cli/main.py`
- `mini_ork/cli/reflect.py`
- `tests/unit/test_reflect_lane_overlay.py` (new)
- `tests/unit/test_reflect_durable_cwd.py` (new)

Do NOT modify any other file. Never write under `.mini-ork/`.

## Tests

### `tests/unit/test_reflect_lane_overlay.py`

Hermetic temp homes. An autouse fixture deletes `MINI_ORK_RUN_DIR` and
`MINI_ORK_AGENTS` from the env, the same baseline as
`tests/unit/test_lane_overlay_resolution.py`. Helper `_home(tmp_path, overlay=None)`
writes `config/agents.yaml` with `lanes: {reflector: codex, worker: codex}`
and, optionally, `config/agents.local.yaml`.

- **The overlay wins.** An overlay of `lanes:\n  reflector: glm\n` →
  `_resolve_reflect_model("reflector", home) == "glm"`.
- **`MINI_ORK_AGENTS` wins.** With `MINI_ORK_AGENTS` pointing at a file holding
  `lanes:\n  reflector: minimax\n` → `"minimax"`.
- **The run-dir snapshot wins.** With `MINI_ORK_RUN_DIR=<rd>` and
  `<rd>/config/agents.yaml` holding `lanes:\n  reflector: deepseek_flash\n` →
  `"deepseek_flash"`.
- **No overlay keeps today's answers.** `"codex"` for `"reflector"`, and
  `"codex"` for an unknown lane `"custom_lane"` (it falls back to
  `lanes.reflector`).
- **No policy anywhere.** Monkeypatch `mini_ork.cli.reflect.MINI_ORK_ROOT` to a
  missing dir and use an empty home → `"reflector"` is returned verbatim.
- **A broken overlay.** An overlay file with invalid YAML returns the lane name
  and does not raise.

### `tests/unit/test_reflect_durable_cwd.py`

Reuse `_scrubbed_env`, `_spy_popen`, `_KICKOFF` and the dry-run setup from
`tests/unit/test_reflect_run_scope.py`, imported from that module, and mirror
`test_post_run_reflect_is_detached_and_run_scoped`. Before calling
`mo_main.main([...])`, `monkeypatch.chdir` into a fresh
`tmp_path / "ephemeral"`. Then assert on the spied detached reflect spawn:

- `cwd` is not None and is an existing directory;
- its realpath is under the realpath of `MINI_ORK_HOME`;
- it is not the `ephemeral` directory.

If the dry-run lifecycle cannot start from that cwd, drop the chdir and keep the
other assertions.

## Verification commands

- `pytest -q tests/unit/test_reflect_lane_overlay.py tests/unit/test_reflect_durable_cwd.py tests/unit/test_reflect_run_scope.py tests/unit/test_mini_ork_reflect_py.py`
- `pytest -q tests/unit/test_reflect_deadline.py tests/unit/test_reflect_deadline_cap.py tests/unit/test_reflect_trace_finalize.py tests/unit/test_lane_overlay_resolution.py tests/unit/test_lane_overlay_empty_home.py`

## Self-application measurement

```measure
python3 -m pytest -q tests/unit/test_reflect_lane_overlay.py tests/unit/test_reflect_durable_cwd.py --junitxml="${MINI_ORK_RUN_DIR}/reflect-junit.xml"
```

## Required artifacts — declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/reflect-junit.xml`

## Success criteria

- Both verification commands pass with every pre-existing test unchanged.
- `ruff check --select F,E9 mini_ork/cli/main.py mini_ork/cli/reflect.py` is clean.

## Rules

Edit files directly; do not emit unified diffs. Execute this spec exactly; do not
research and do not add lens or drafter nodes.
