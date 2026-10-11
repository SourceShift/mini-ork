# C4 — certify: reject probes that import the code under test and then redefine it

Depends on C3 (`imports_code_under_test` in `mini_ork/certify/context.py`), merged.

## Goal

`imports_code_under_test(src, modules)` only checks that an import line exists. A probe
that does

```python
from stats import median

def median(xs):            # shadows the real one — the test now exercises a copy
    return sorted(xs)[len(xs) // 2]
```

passes the guard, yet tests its own copy of the code instead of the repository's. Same
hole via `median = lambda xs: ...`, `import stats` + `stats.median = ...`, or
`from stats import median as m` + `def m(...)`. Close it structurally.

## Files in scope

- `mini_ork/certify/context.py` — the guard.
- `tests/unit/test_certify_context.py` — its unit tests.

No other file changes. Do not touch `probe.py` or `invariants.py`: both already call
`imports_code_under_test`, so tightening the function tightens both call sites.

## Mechanism (exact spec)

Rewrite `imports_code_under_test(src, modules)` on top of `ast`:

1. `modules` empty → return `True` (unchanged: SWE-bench-style callers skip the guard).
2. `ast.parse(src)`; on `SyntaxError` fall back to the current regex logic unchanged
   (a probe that does not parse fails later when run; the guard must not crash).
3. Walk the tree. For every `ast.Import` / `ast.ImportFrom` whose module (or an ancestor,
   same `_ancestors` rule as today) is in the target set, record the **bound local names**:
   - `import stats.core` → binds `stats`; `import stats as s` → binds `s`;
   - `from stats import median` → binds `median`; `from stats import median as m` → `m`;
   - `from stats import *` → records nothing but still counts as an import.
   Relative imports (`level > 0`) do not count as importing the code under test.
4. No qualifying import found → `False` (unchanged meaning).
5. Collect **rebindings** anywhere in the module (any depth, including inside test
   functions and classes): names bound by `FunctionDef`, `AsyncFunctionDef`, `ClassDef`,
   `Assign`/`AnnAssign`/`AugAssign` targets (`Name`), `For` targets, `With ... as` targets,
   `NamedExpr`, and `global` re-assignments. Also attribute assignment `X.attr = ...` /
   `setattr(X, ...)` where `X` is a bound module alias from step 3 (monkeypatching the code
   under test).
6. If any bound name from step 3 is rebound per step 5 → `False`. Otherwise `True`.

Keep the public signature and return type identical. Update the module docstring's API
summary line for `imports_code_under_test` to say it also rejects redefinition.

Function parameters and local variables that merely *share* a name inside an unrelated
nested scope still count as a rebinding — be strict; a false reject only costs one probe
retry, a false accept certifies a copy.

## Tests (add to `tests/unit/test_certify_context.py`)

Keep every existing test passing unchanged. Add a parametrized `False` case for each:

- `from stats import median\ndef median(xs): return 0\n`
- `from stats import median\nmedian = lambda xs: 0\n`
- `from stats import median as m\ndef m(xs): return 0\n`
- `import stats\nstats.median = lambda xs: 0\n`
- `import stats as s\nsetattr(s, "median", lambda xs: 0)\n`
- `from stats import median\ndef test_x():\n    def median(xs): return 0\n    assert median([1]) == 0\n`
- `from stats import median\nclass median: pass\n`

And `True` cases that must still pass:

- `from stats import median\ndef test_x():\n    assert median([1, 2, 3]) == 2\n`
- `import stats\nresult = stats.median([1, 2])\n` (assigning a *result* is fine)
- `from stats import median\nimport pytest\n@pytest.mark.parametrize("xs", [[1]])\ndef test_x(xs):\n    assert median(xs) == 1\n`
- a probe with a syntax error that contains `from stats import median` → `True` (regex fallback)

## Success criteria

- `python3.11 -m pytest -q tests/unit/test_certify_context.py tests/unit/test_certify_oracle.py tests/unit/test_certify_cli.py` passes.
- `ruff check mini_ork/certify tests/unit/test_certify_context.py` is clean.
- Diff touches only the two files in scope.
