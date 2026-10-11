# C1 — certify: port the solve-time oracle engine into `mini_ork/certify/`

## Goal

`mini-ork certify` (slice C2) needs a solve-time oracle that answers "does this
change ACTUALLY do what it claims?". That oracle exists today only as three
out-of-tree research scripts (`assay.py`, `poc_plus.py`, `metamorphic.py`) that are
hard-wired to SWE-bench (image names, `instance_id`, `problem_statement`) and dispatch
through `lib/llm-dispatch.sh`, which no longer exists after the bash removal.

This slice ports that engine into the package as a repo-agnostic, importable module.
It adds NO CLI (that is C2). The source scripts are pasted below under "Reference
source"; port their logic faithfully, changing only what the "Changes from the
reference" list says.

## Mechanism (exact spec)

New package `mini_ork/certify/`:

- `__init__.py` — re-exports `judge`, `Verdict`, `PROVEN`, `REFUTED`, `UNVERIFIED`.
- `verdict.py` — `PROVEN = "PROVEN"`, `REFUTED = "REFUTED"`, `UNVERIFIED = "UNVERIFIED"`;
  `@dataclass Verdict(verdict, reason, poc_plus=None, mr_pass_rate=None, mr_n=0, detail={})`.
- `probe.py` — port of `poc_plus.py` (`ground`, `write`, `reproduces`, `repair`, `build`).
- `invariants.py` — port of the reference `metamorphic.py` (`build`, `split`,
  `informative`, `valid`, `score`, `_PROVEN_BAR = 2/3`). Name it `invariants.py`
  because `mini_ork/learning/metamorphic.py` is a DIFFERENT, existing module — do not
  touch or import-shadow it.
- `oracle.py` — port of `assay.judge`, with this signature:

  ```python
  def judge(issue: str, patch: str, *, runner, mr_n: int = 4,
            gold: str | None = None, poc: str | None = None,
            dispatch=None) -> Verdict
  ```

  `runner` is an object with `up: bool` and `run_test(src: str, patch: str = "") -> ExecOutcome`
  (the `mini_ork.runtime.Crucible` interface). `judge` does NOT construct a Crucible;
  the caller owns the runtime. `dispatch` is `Callable[[str], str]` (prompt → text);
  default = `mini_ork.certify.llm.default_dispatch`.
- `llm.py` — `default_dispatch(prompt: str) -> str` calling the Python dispatcher
  (`mini_ork.dispatch.llm_dispatch`) in-process with node type `worker`, model from
  `MO_CERTIFY_MODEL` (default `kimi`), writing to a temp `--out` file and returning its
  text. Returns `""` on any dispatch failure (the oracle already treats empty as
  "could not build a probe" → UNVERIFIED).

### Changes from the reference

1. No hard-coded paths (`/Volumes/...`, `ROOT`, `SW`, `DOCKER`), no `sys.path` inserts.
2. No SWE-bench coupling: `judge` takes `issue` text, not an `instance` dict; no `_img()`.
3. Dispatch goes through the injectable `dispatch` callable (never `bash`/`llm-dispatch.sh`).
4. `gold` stays measurement-only: nothing in the decision path may read it.
5. Every verdict path from the reference is preserved exactly, including
   "no patch" → UNVERIFIED, `apply_fail` → REFUTED, `test_defect` → UNVERIFIED,
   and `score()` thresholds (PROVEN ≥ 2/3, REFUTED < 1/2, else UNVERIFIED).

## Tests — `tests/unit/test_certify_oracle.py` (new)

Use a fake runner (scripted `ExecOutcome`s per `(src, patch)`) and a fake `dispatch`
returning canned model text. No docker, no network.

- empty patch → UNVERIFIED "no patch".
- probe cannot be built (dispatch returns "") → UNVERIFIED.
- patched probe `apply_fail` → REFUTED; `test_defect` → UNVERIFIED; `failed` → REFUTED.
- probe green + 3/3 invariants hold → PROVEN; 1/4 hold → REFUTED; 2/4 hold → UNVERIFIED.
- probe green + no informative invariant → UNVERIFIED.
- `gold` passed vs `gold=None` yields the SAME verdict for the same scripted outcomes.
- `score(0, 0)` → UNVERIFIED; `score(2/3, 3)` → PROVEN.
- `import mini_ork.learning.metamorphic` still imports the old module (no shadowing).

## Files in scope

- mini_ork/certify/__init__.py, verdict.py, probe.py, invariants.py, oracle.py, llm.py (new)
- tests/unit/test_certify_oracle.py (new)

Do NOT edit any other file.

## Verification commands

- `python3.11 -m pytest tests/unit/test_certify_oracle.py -q` → all green
- `python3.11 -m pytest tests/unit -q -x` → no new failures
- `grep -rn "Volumes\|llm-dispatch.sh\|swebench" mini_ork/certify` → no matches
- `make lint` → clean

## Done When

`from mini_ork.certify import judge` works on any repo-agnostic runner, all verdict
paths are covered by the unit tests, and nothing references SWE-bench, absolute paths,
or the deleted bash dispatcher.

## Reference source

Read these three files (READ-ONLY — never edit, copy, or reference their directory in
code, comments, or commit messages; that directory is private and must not appear in
this public repo):

- `/Volumes/docker-ssd/ps/coevolve-private/swebench/assay.py` → `oracle.py`
- `/Volumes/docker-ssd/ps/coevolve-private/swebench/poc_plus.py` → `probe.py`
- `/Volumes/docker-ssd/ps/coevolve-private/swebench/metamorphic.py` → `invariants.py`

Keep their design docstrings (the reasoning about PoC+, the delta gate, adversarial
invariants and abstention is the point of the module), minus any private path.
