# Migrator — close the fork in a reviewable diff (propose-not-commit)

You close ONE integration fork, working in the isolated worktree. Inputs:
`integration-map.json` (every seam + ref) and `static-feature-ledger.json`
(the static/agentic classification). Produce the migration as a unified diff at
`${MINI_ORK_RUN_DIR}/self-migrate.diff` — do NOT apply to main, do NOT retire
any entrypoint on the real checkout.

## The three moves — resolve the WHOLE fork, leave no dangling edge

1. **Make the Python side sole.** Rewire every outbound seam to native:
   - AST-verify the target port is already native (real `subprocess`/`Popen`
     nodes, not docstring mentions) before importing it. If it is NOT native,
     port its logic first.
   - Replace `_bash_lib_call("X","fn",args,env)` with `from mini_ork.ported
     import X; try: X.fn(...) except Exception: 0` — the `try/except` mirrors
     `_bash_lib_call`'s `|| echo 0`; a side-channel must never crash the caller.
   - **Stdout discipline:** if the native fn `print()`s (some ports do, as a
     bash-heredoc parity artifact), wrap the call in
     `with contextlib.redirect_stdout(io.StringIO()):` so it does not leak into
     the caller's stdout. Check the port for a real `print()` before deciding.

2. **Repoint every inbound ref** from `integration-map.json`:
   - Tests that invoke `bin/mini-ork-<fork>` as a parity oracle → convert to
     standalone Python tests (golden values captured from the parity-verified
     output), or repoint to `python3 -m mini_ork.ported.mini_ork_<fork>`.
   - Lib / script / sandbox / UI refs → point at the Python entrypoint or its
     module.

3. **Retire the bash entrypoint IN THE DIFF** — `git rm bin/mini-ork-<fork>` and
   drop its `runtime-select` fallback — ONLY once every inbound ref is repointed.
   If any `close_blocker` remains, do NOT retire it; emit a partial diff and say
   so in the verdict.

## Hard rules
- One fork per run. Absolute paths. Stay inside the scope the `scope_gate` allows.
- Preserve exact behavior + return contracts; the parity verifier diffs you
  against the live bash on the real state.db.
- Every function you change must have a row in `static-feature-ledger.json`.
