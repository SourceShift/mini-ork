# C3 — certify: show the oracle the code under test, and refuse probes that don't import it

Depends on C1 (`mini_ork/certify/` engine) and C2 (`mini-ork certify` CLI), both merged.

## Goal

On a repo the model has never seen, the oracle's reproduction probe does not import the
code under test — it RE-IMPLEMENTS the buggy function inline and tests its own copy.
Measured on a 1-file demo repo (`stats.median` returns the upper middle for even-length
input): the generated probe defined its own `def median(...)` with the bug baked in, so

- the CORRECT fix was judged **REFUTED** ("the reported bug still reproduces") — the
  patch cannot change a copy that lives in the test file;
- a special-cased CHEAT fix was **UNVERIFIED** instead of REFUTED.

The oracle works on SWE-bench only because the model already knows those libraries by
name. The prompts give it the issue text and nothing about the repo. Fix both halves:
give the model the base source of the files the change touches, and structurally reject
any probe or invariant that does not import the repo's own module.

## Mechanism (exact spec)

### New `mini_ork/certify/context.py`

```python
@dataclass(frozen=True)
class CodeContext:
    text: str            # prompt-ready block; "" when nothing to show
    modules: tuple[str, ...]   # importable dotted module names of the changed .py files
    files: tuple[str, ...]     # repo-relative paths included

def changed_py_files(patch: str) -> list[str]
def module_name(path: str) -> str | None
def code_context(repo: Path, base_sha: str, patch: str, max_chars: int = 8000) -> CodeContext
```

- `changed_py_files`: repo-relative paths from `--- a/<p>` / `+++ b/<p>` headers, `.py` only,
  de-duplicated in first-seen order, excluding `/dev/null` and paths under `test/`,
  `tests/`, or named `test_*.py` / `*_test.py`.
- `module_name`: strip `.py`; drop a leading `src/`; `pkg/__init__.py` → `pkg`;
  `pkg/sub/mod.py` → `pkg.sub.mod`; return `None` if any part is not a valid identifier.
- `code_context`: for each changed file that exists at `base_sha`
  (`git -C repo show <base_sha>:<path>`), append
  ``"# file: <path>  (import as: <module>)\n```python\n<source>\n```\n"``, stopping once
  `max_chars` is reached (truncate the last file with a `# … truncated` line). The source
  shown is the BASE (buggy) version only — never the patch or the head version.

### Engine changes

- `oracle.judge(..., context: CodeContext | None = None)`: pass it to
  `probe.build(...)` and `invariants.build(...)`. `None` keeps today's behaviour exactly
  (SWE-bench-style callers are unaffected).
- `probe.py`: when `context` has text, append to the PoC prompt:

  ```
  THE CODE UNDER TEST (the buggy version, before any fix):
  <context.text>

  Your test MUST import the code under test from the repository (e.g. `from <module> import <name>`)
  and call it. NEVER copy, re-implement, or redefine the code under test in the test file —
  a test of a copy proves nothing about the repository.
  ```

- `invariants.py`: the same block appended to the invariant prompt.
- Structural guard (the load-bearing part — prompts alone are not enough):
  `imports_code_under_test(src: str, modules: tuple[str, ...]) -> bool` in `context.py`,
  true iff `src` has an `import <m>` / `import <m>.…` / `from <m> import …` /
  `from <m>.… import …` line for some `m` in `modules` (a parent package of `m` also counts:
  `from stats import median` satisfies module `stats`).
  - `probe.build`: when `context.modules` is non-empty, a candidate probe that fails the
    guard is rejected BEFORE it is run (reason
    `"probe does not import the code under test (<modules>)"`) and counts as a failed try.
    If every try is rejected, `judge` returns UNVERIFIED with that reason.
  - `invariants.build`: candidates failing the guard are dropped before base validation.
  - With `context=None` or empty `modules`, the guard is skipped (no behaviour change).

### CLI (`mini_ork/cli/certify.py`)

- After reading the patch: `ctx = code_context(repo, base_sha, patch_text)`; pass
  `context=ctx` to `judge`.
- Certificate: add `"context_files": list(ctx.files)` inside `method` (schema stays
  `mini-ork.certificate/v1`; update the key-set assertion in the tests accordingly).

## Tests

`tests/unit/test_certify_context.py` (new):
- `changed_py_files` on a patch touching `stats/__init__.py`, `README.md`,
  `tests/test_x.py`, and a new file (`--- /dev/null`) → only the non-test `.py` paths.
- `module_name`: `stats/__init__.py`→`stats`, `src/pkg/a.py`→`pkg.a`, `a-b/c.py`→`None`.
- `code_context` on a tmp two-commit git repo: text contains the BASE source and not the
  head source; `modules == ("stats",)`; truncation at a small `max_chars`.
- `imports_code_under_test`: true for `from stats import median`, `import stats`,
  `from stats.core import x` (module `stats`); false for a file that only defines
  `def median`.

`tests/unit/test_certify_oracle.py` (extend):
- with a `CodeContext(modules=("stats",))` and a fake dispatch whose probe defines
  `def median` and never imports `stats` → UNVERIFIED, reason contains
  "does not import the code under test", and the runner was never called.
- same setup but the probe does `from stats import median` → proceeds to the delta gate.
- the PoC prompt the fake dispatch receives contains the context text when given, and
  does not when `context=None`.

`tests/unit/test_certify_cli.py` (extend): `judge` receives a `context` whose `modules`
come from the two-commit repo's changed file; certificate `method.context_files` lists it.

## Files in scope

- mini_ork/certify/context.py (new)
- mini_ork/certify/oracle.py, mini_ork/certify/probe.py, mini_ork/certify/invariants.py
- mini_ork/cli/certify.py
- tests/unit/test_certify_context.py (new), tests/unit/test_certify_oracle.py,
  tests/unit/test_certify_cli.py

## Verification commands

- `python3.11 -m pytest tests/unit/test_certify_context.py tests/unit/test_certify_oracle.py tests/unit/test_certify_cli.py -q` → all green
- `python3.11 -m pytest tests/unit -q` → no new failures
- `ruff check mini_ork/ tests/` → clean

## Done When

Unit tests green AND (operator-run, live) on the demo repo: the correct fix certifies
**PROVEN** (exit 0) and the special-cased cheat certifies **REFUTED** (exit 1).
