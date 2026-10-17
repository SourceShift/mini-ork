# LF-R9 v2 scope falls back to the profile's allow list

## Goal

`context_v2.build()` derives the file scope it selects findings and prior
attempts with from `parse_contract(kickoff_text)["files_in_scope"]` alone. When
that parse comes back empty — the panel measured 38 of 257 headed kickoffs
parsing to `[]`, because a prose kickoff names files without backticks, or under
a heading the parser does not recognise — the run's v2 pack is built with an
empty scope: no findings, no prior attempts, nothing, even though the run's own
`run_profile.json` carries a `scope_allow` list that the profiler already
extracted from the same kickoff. R8 is fixing the parser so it reads more
kickoffs; R9 is the belt-and-braces layer for the ones it still misses: the run
profile is the fallback source of scope.

Decision record: `docs/plans/2026-10-15-learning-loop-fixes.md` (R9).

All paths are relative to the repository root.

## Mechanism (exact)

1. `build()` gains a keyword-only `run_dir: str | None = None`. Scope selection
   becomes:
   - `files = contract["files_in_scope"]`; if that is truthy, `scope_source =
     "kickoff"`;
   - else if `run_dir` is set and `preferences.scope_paths(run_dir)` is truthy,
     `files = ` that list and `scope_source = "profile"`;
   - else `files = []` and `scope_source = "none"`.
   `files` (whichever source) is passed to `scope_findings(...)` exactly as the
   kickoff list is today — no other change to how findings are matched. Import
   `scope_paths` lazily inside the function (`from mini_ork.memory import
   preferences`) so `context_v2` does not acquire an import-time dependency on
   `memory`, and wrap the call in `try/except Exception` so the prompt path
   still fails soft.
2. The pack gains `"scope_source": <one of "kickoff"|"profile"|"none">` and
   `"scope_paths": files` next to `n_findings_scanned`, so a run's pack says
   which source selected its findings — the same spirit as
   `n_findings_scanned`, and the thing that lets a later reader tell "no
   findings existed" from "we looked in the wrong place".
3. `pack_for_run(run_dir, …)` passes `run_dir=run_dir` to `build()`. This is the
   only call-site change: `pack_for_run` is the run-scoped builder and already
   holds the directory. In the live tree `build()` has no other production
   caller — `cli/plan.py:705` goes through `pack_for_run`, and
   `cli/execute.py:3505` renders the pack `pack_for_run` already wrote — so the
   fallback reaches both the planner pack and the executor's node blocks with no
   further edits. Do NOT edit `cli/plan.py`, `cli/execute.py`, or the pack's
   reader path.
4. **Scope of the fallback is SELECTION ONLY.** It decides which findings,
   clusters and prior attempts are offered to the run. It must NOT touch
   `check_diff`, `_in_scope`, `mini_ork/verify/kickoff_guard.py`, or anything
   else that decides whether an edit is inside the run's declared scope — the
   profile's `scope_allow` is a heuristic extraction of prose bullets and must
   never become edit authority. `scope_paths`' existing guards (stop at the
   `Do NOT …` sentinel, skip glob `**` tokens) already keep the worst prose out;
   do not weaken or duplicate them.

## Files in scope

- `mini_ork/context_v2.py`
- `tests/unit/test_context_v2.py`

## Tests (`tests/unit/test_context_v2.py`, hermetic temp DB + temp run dir)

- a kickoff whose "Files in scope" section parses non-empty keeps
  `scope_source == "kickoff"` and `scope_paths` equal to the parsed list, even
  when a `run_profile.json` sits next to it with a different `scope_allow`;
- a kickoff that parses to `[]` plus a `run_profile.json` whose `scope_allow`
  carries backticked paths gives `scope_source == "profile"`, `scope_paths`
  equal to `preferences.scope_paths(run_dir)`, and a finding on one of those
  paths now appears in `file_findings`;
- with a `run_profile.json` whose `scope_allow` entries are prose only (no
  backticked path-like token), `scope_source == "none"` and the pack is empty of
  findings — no crash, no scope invented;
- `run_dir=None` (or a directory with no `run_profile.json`) reproduces today's
  behaviour exactly: empty parse → `scope_source == "none"`, no findings;
- `pack_for_run` on a run dir with a prose-only kickoff and a real profile
  `scope_allow` writes a pack whose `scope_source == "profile"`.

## Verification commands

- `/Volumes/docker-ssd/ps/mini-ork/.venv/bin/python -m pytest -q tests/unit/test_context_v2.py tests/unit/test_kickoff_guard.py tests/unit/test_prefs_path_scope.py`

## Self-application measurement

```measure
/Volumes/docker-ssd/ps/mini-ork/.venv/bin/python -m pytest -q tests/unit/test_context_v2.py --junitxml="${MINI_ORK_RUN_DIR}/lf-r9-junit.xml"
```

## Required artifacts — declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/lf-r9-junit.xml`

## Rules

Edit files directly; do not emit unified diffs. Never rebase, merge or reset
the worktree during the run. Do not change `parse_contract`, `scope_findings`'
matching rules, `prior_attempts`, `themes`, or any scope-enforcement path
(`check_diff`, `_in_scope`, the kickoff guard), and do not write to the live DB
under `/Volumes/docker-ssd/ps/mini-ork/.mini-ork/` (tests use temp DBs).
