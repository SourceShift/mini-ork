# W2-harvest-b: framework-edit probes must not carry absolute host paths

## Goal

`failed_run_candidates` (`mini_ork/learning/probe_harvest.py`) uses a failed run's kickoff
verbatim as the frozen probe's `problem_statement`. Framework-edit kickoffs are written for one
host and one worktree. Pre-push review 196 (high) and a scan of the probes committed in 599790b6
show 5 of 6 framework-edit probes carry absolute host paths:
- `Worktree (ALL edits happen here): /Volumes/…/mini-ork-worktrees/<slug>`
- files in scope as `/Volumes/…/mini-ork-worktrees/<slug>/mini_ork/x.py`
- `/Volumes/…/.venv/bin/python3.11` in measure blocks
- another project's home, `/Volumes/…/researcher/.mini-ork`

A probe arm runs in its own copy of the repo (`MO_TARGET_CWD`). These paths send it to edit
**another worktree on the host**, or to a path that does not exist on any other machine. The
probe then measures nothing, or touches a live checkout. The 19 code-fix probes have none of
this.

Fix in the harvester, at harvest time (frozen probes are never hand-edited):
1. Paths that point into **a checkout of this repo** become repo-relative.
2. A virtualenv interpreter path becomes `python3`.
3. Any absolute host path still left means the candidate is rejected with reason `host-path`.

Read `mini_ork/learning/probe_harvest.py` first:
- `failed_run_candidates`, its `reject()` helper and rule order
- `answer_pointers` (the guard added just before this one)
- `_scrub`

Then read `tests/unit/test_probe_harvest_git.py` (`_build_repo`, `_home`, `_row`, `K1_TEXT`) and
`tests/unit/test_probe_harvest_answer_pointer_py.py`.

## Mechanism (exact spec)

### `mini_ork/learning/probe_harvest.py`

1. `repo_checkouts(root: str) -> list[str]`: the absolute paths of every checkout of the repo at
   `root`. That is `root` itself plus every `worktree` line of `git -C root worktree list
   --porcelain`, normalised with `os.path.realpath`, de-duplicated, and sorted **longest first**
   so a nested path wins. On any git error, return `[os.path.realpath(root)]`. No host path may
   be hard-coded.
2. A pure helper:

```python
_VENV_PYTHON = re.compile(r"\S*/\.?venv/bin/(python[0-9.]*)")
# An absolute POSIX path left in a probe after relativisation: something that
# only exists on the harvesting host.
_HOST_PATH = re.compile(r"(?<![\w.:-])/(?:Volumes|Users|home|private|tmp|var|opt|mnt|srv)/[^\s`'\"),;]+")


def relativize_host_paths(text: str, checkouts: list[str]) -> tuple[str, list[str]]:
    """``(text', leftovers)`` — checkout paths made repo-relative, venv pythons -> ``python3``.

    Every occurrence of ``<checkout>/`` is removed (so ``<checkout>/mini_ork/x.py`` ->
    ``mini_ork/x.py``); a bare ``<checkout>`` (no trailing slash) becomes ``.``.
    ``leftovers`` lists the absolute host paths still present afterwards
    (``_HOST_PATH.findall``, sorted, de-duplicated) — another project's home, a /tmp
    helper — which the caller treats as a rejection.
    """
```

   Apply the venv rule first, then strip checkouts, longest first, then collect leftovers.
3. In `failed_run_candidates`, right after the `answer-pointer` check, with `checkouts`
   computed **once** per call (before the loop) as `repo_checkouts(root)`:

```python
problem_statement, leftovers = relativize_host_paths(problem_statement, checkouts)
if leftovers:
    reject("host-path")
    continue
```

   Use the relativised `problem_statement` in the candidate dict. Add `host-path` to the
   docstring's rejection list, after `answer-pointer`.

## Files in scope (touch ONLY these)

- `mini_ork/learning/probe_harvest.py`
- `tests/unit/test_probe_harvest_host_paths_py.py` (new)

Do NOT modify any other file.

## Tests (`tests/unit/test_probe_harvest_host_paths_py.py`, hermetic)

`relativize_host_paths`, with explicit checkout lists (no real git):
- `"Worktree: /x/wt/slug"` with checkouts `["/x/wt/slug"]` gives `"Worktree: ."` and no
  leftovers.
- `"/x/wt/slug/mini_ork/a.py"` gives `"mini_ork/a.py"`.
- A nested checkout wins: checkouts `["/x/wt/slug/inner", "/x/wt/slug"]` (longest first) and
  `/x/wt/slug/inner/f.py` give `f.py`.
- `/Volumes/a/.venv/bin/python3.11 -m pytest` gives `python3 -m pytest`.
- `/Users/me/other-project/.mini-ork` with unrelated checkouts is a leftover.
- `/tmp/helper.sh` is a leftover.
- A URL `https://example.com/Users/x` is NOT a leftover (the negative look-behind).
- Text without paths is unchanged with no leftovers.

`repo_checkouts`:
- In a real `git init` repo in `tmp_path` with one `git worktree add`, it returns both
  realpaths, longest first.
- On a non-git dir it returns `[realpath(dir)]`.

Wiring, reusing `test_probe_harvest_git`'s helpers:
- A failed-run kickoff whose text says `Edit files under <root>/calc.py` (the repo root) becomes
  a candidate whose `problem_statement` says `calc.py`.
- A kickoff naming `/Users/someone/elsewhere/file.txt` is rejected `host-path`.
- A control kickoff without paths is unchanged.

## Verification commands

- `python3 -m pytest -q -p no:asyncio tests/unit/test_probe_harvest_host_paths_py.py tests/unit/test_probe_harvest_answer_pointer_py.py tests/unit/test_probe_harvest.py tests/unit/test_probe_harvest_git.py tests/unit/test_probe_harvest_scrub.py tests/unit/test_probe_harvest_scorer_roundtrip.py`
- `ruff check --select F,E9 mini_ork/learning/probe_harvest.py tests/unit/test_probe_harvest_host_paths_py.py`

## Self-application measurement

```measure
python3 -m pytest -q -p no:asyncio tests/unit/test_probe_harvest_host_paths_py.py --junitxml="${MINI_ORK_RUN_DIR}/w2-harvest-host-paths-junit.xml"
```

## Required artifacts: declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/w2-harvest-host-paths-junit.xml`

## Success criteria

- All verification commands pass, and every pre-existing test is unchanged.
- A kickoff with no absolute paths is harvested exactly as before.

## Rules

Edit files directly; do not emit unified diffs. Execute this spec exactly; do not
research and do not add lens or drafter nodes.
