# W2-harvest: reject a framework-edit probe whose kickoff points at a run dir holding the answer

## Goal

`mini_ork/learning/probe_harvest.py` turns failed framework-edit runs into frozen probes.
`failed_run_candidates` takes each run's kickoff text verbatim as the probe's
`problem_statement`. Those kickoffs sometimes name another run id, for example
`build_node(Path('…/.mini-ork'), 'lane-repair-hint-r2-20261007160033', …)`.

On 2026-10-30, 2 of 8 harvested framework-edit probes (h01, h06) quoted run ids whose
`<home>/runs/<id>/` still held `framework-edit.diff`, `salvage.patch` and `review-diff.patch`,
i.e. the answer. A probe arm on the host could read it and inflate the measured gain. That
makes the RSI promotion signal (probe scorer, promotion certificate) dishonest. Frozen probes
must not be hand-edited, so the harvester has to refuse such candidates.

Fix: a candidate whose final problem statement names a run whose run dir holds an answer
artifact is rejected with reason `answer-pointer`.

Read `mini_ork/learning/probe_harvest.py` first (`failed_run_candidates`, its `reject()`
helper, the "kickoff that LANDED wins" step, `_strip_frontmatter`), then
`tests/unit/test_probe_harvest_git.py` (`_build_repo`, `_home`, `_row`, `K1_TEXT`,
`test_failed_run_becomes_candidate`, `test_rejection_reasons_in_query_order`).

## Mechanism (exact spec)

### `mini_ork/learning/probe_harvest.py`

1. Module constants and a pure helper:

```python
# Files a run dir keeps that ARE (or contain) the change the run made. A probe
# whose problem statement names such a run hands the arm its answer.
ANSWER_ARTIFACTS = (
    "framework-edit.diff", "framework-edit.diff.agent", "review-diff.patch",
    "review-delta.patch", "salvage.patch",
)
# Run-id-shaped tokens: a slug with a run of at least 6 digits in it
# (`<slug>-YYYYMMDDHHMMSS`, `<slug>-YYYYMMDD-HHMMSS`, `run-<epoch>-<pid>`).
_RUN_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\d{6,}[A-Za-z0-9_.-]*")


def answer_pointers(text: str, home: str) -> list[str]:
    """Run ids named in ``text`` whose ``<home>/runs/<id>/`` holds an answer artifact.

    Sorted and de-duplicated. A token that is not an existing run dir is ignored;
    a run dir with none of :data:`ANSWER_ARTIFACTS` is not a pointer.
    """
```

   It scans `_RUN_TOKEN.findall(text or "")`, strips trailing `.`/`-`/`_` from each token, and
   keeps a token when `os.path.isdir(os.path.join(home, "runs", token))` and any
   `ANSWER_ARTIFACTS` file exists in it. Use `re` (import it if the module does not already).
2. In `failed_run_candidates`, after `problem_statement = _strip_frontmatter(text)` and BEFORE the
   `candidate = {...}` dict is built:

```python
pointers = answer_pointers(problem_statement, home)
if pointers:
    reject("answer-pointer")
    continue
```

   `reject` appends `{"task_id": run_id, "reason": ...}`. The reason string is exactly
   `"answer-pointer"`. Do not change the order of any existing rejection rule.
3. Document `answer-pointer` wherever the module documents its rejection reasons (docstring of
   `failed_run_candidates`).

## Files in scope (touch ONLY these)

- `mini_ork/learning/probe_harvest.py`
- `tests/unit/test_probe_harvest_answer_pointer_py.py` (new)

Do NOT modify any other file.

## Tests (`tests/unit/test_probe_harvest_answer_pointer_py.py`, hermetic)

`answer_pointers` unit cases, using a `tmp_path` home with `runs/`:
- Text naming `lane-x-20261007160033`, whose run dir has `framework-edit.diff`, gives
  `["lane-x-20261007160033"]`.
- The same run dir with no answer artifact gives `[]`.
- A token whose run dir does not exist gives `[]`.
- A run id followed by a period (`…20261007160033.`) is still found.
- Two references to the same run are de-duplicated.
- `salvage.patch` alone counts.
- Text with no digit runs gives `[]`.

Wiring, reusing `test_probe_harvest_git`'s helpers (import them):
- A failed run whose kickoff text names a second run id that has `framework-edit.diff` in
  `<home>/runs/<id>/` is rejected with reason `answer-pointer`, and no candidate is produced.
- The same kickoff without the reference still becomes a candidate (control, mirroring
  `test_failed_run_becomes_candidate`).

## Verification commands

- `python3 -m pytest -q -p no:asyncio tests/unit/test_probe_harvest_answer_pointer_py.py tests/unit/test_probe_harvest.py tests/unit/test_probe_harvest_git.py tests/unit/test_probe_harvest_scrub.py tests/unit/test_probe_harvest_scorer_roundtrip.py`
- `ruff check --select F,E9 mini_ork/learning/probe_harvest.py tests/unit/test_probe_harvest_answer_pointer_py.py`

## Self-application measurement

```measure
python3 -m pytest -q -p no:asyncio tests/unit/test_probe_harvest_answer_pointer_py.py --junitxml="${MINI_ORK_RUN_DIR}/w2-harvest-answer-pointer-junit.xml"
```

## Required artifacts: declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/w2-harvest-answer-pointer-junit.xml`

## Success criteria

- All verification commands pass, and every pre-existing test is unchanged.
- A kickoff with no run reference is harvested exactly as before.

## Rules

Edit files directly; do not emit unified diffs. Execute this spec exactly; do not
research and do not add lens or drafter nodes.
