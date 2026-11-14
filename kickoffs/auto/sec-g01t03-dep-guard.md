# SEC-G01-T03: dependency hallucination and version-risk guard at code-verification time

## Goal

An implementer that emits `import pkg_x` or adds a `requirements` line has not
verified that `pkg_x` exists. Agents hallucinate dependencies (36.9% of emitted
dependencies in the cited measurements; 90.7% of trials land on squattable
slots), and a hallucinated or unpinned name is exactly the slot an attacker
registers. `code-fix` does not check this at all today.

Add a deterministic, offline dependency gate next to test/typecheck/lint: every
NEW import and every declared requirement in the run's change surface must
resolve against a vendored allowlist, must be version-pinned, and must not come
from a direct URL or VCS source. Grounded in a committed snapshot, never in a
model's opinion — and where the snapshot cannot answer, the gate abstains rather
than passing.

Source: `docs/research/2026-11-13-security-technique-review.md`, G01-T03
(priority 9.0, rank 3). First slice only: existence, pinning, source, and
squat-distance. Live registry truth and a CVE list are later slices — do not add
network calls.

All paths are relative to the repository root.

## Mechanism (exact spec)

### 1. `recipes/code-fix/data/dependency-allowlist.txt` (new, committed)

Generated, not hand-typed. Header exactly:

```
# Vendored dependency allowlist for the code-fix dep gate.
# One normalized name per line: a distribution name or an import name.
# Regenerate: python3 -m mini_ork.verify.dep_guard --write-allowlist \
#   recipes/code-fix/data/dependency-allowlist.txt
# A new dependency is a deliberate, reviewable line in this file — never a
# silent side effect of an implementer's import. Snapshot date: 2026-11-14.
```

then every name from `pyproject.toml` (`[project] dependencies` and every
`[project.optional-dependencies]` list), normalized (`lower()`, every run of
characters outside `[a-z0-9]` → `_`, strip `_`), plus the import aliases in
`IMPORT_ALIASES` below, sorted, one per line. Generate it with the CLI in step 2
and commit the result — do not type it.

### 2. `mini_ork/verify/dep_guard.py` (new)

Standard library only (`ast`, `json`, `os`, `re`, `subprocess`, `tomllib`,
`dataclasses`, `pathlib`). No network. No model access.

**Constants**

- `ALLOWLIST_DEFAULT = "recipes/code-fix/data/dependency-allowlist.txt"`
- `IMPORT_ALIASES = {"pyyaml": ("yaml",), "pymupdf": ("fitz",), "python_dateutil": ("dateutil",), "agent_client_protocol": ("acp",)}`
- `FIRST_PARTY_ROOTS = ("mini_ork", "tests", "recipes", "scripts", "bin", "evals")`
- `REQUIREMENTS_RE = re.compile(r"(^|/)requirements[^/]*\.txt$")`
- `RULES = ("unpinned-dependency", "untrusted-source", "unknown-dependency", "squat-suspect")`

**`@dataclass(frozen=True) class Finding`** — fields in order: `path: str`,
`line: int` (`0` when the finding is file-level), `rule: str` (one of `RULES`),
`name: str`, `why: str`.

**`norm(name: str) -> str`** — `re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")`.

**`one_edit_apart(a: str, b: str) -> bool`** — pure Levenshtein distance exactly
1: equal length with exactly one differing position; or lengths differing by 1
where the shorter is obtained from the longer by deleting exactly one character.
Equal strings and distance ≥ 2 → `False`.

**`load_allowlist(path: Path) -> frozenset[str]`** — read UTF-8, skip blank lines
and `#` comments, `norm()` each remaining line, return the set. Missing or
unreadable file raises `FileNotFoundError` / `OSError` — the caller abstains.

**`check_name(name, *, allowlist, path, line) -> list[Finding]`** — the shared
name rule, in this order:

1. `n = norm(name)`; `n in allowlist` → `[]` (allowed).
2. any `one_edit_apart(n, a)` for `a in allowlist` → one `squat-suspect`
   finding, `why = "one edit from allowlisted '<closest>'"`.
3. otherwise one `unknown-dependency` finding, `why = "not in the vendored allowlist"`.

`closest` is the lexicographically smallest qualifying entry (deterministic).

**`parse_requirements(text: str) -> list[tuple[int, str, str]]`** — `(lineno,
name, spec)` for each declared requirement line, 1-based. Skip blank lines, `#`
comments, `-r`/`--requirement` includes, and `--hash` continuation lines. Strip a
trailing environment marker (`; ...`). Split the name (leading `[A-Za-z0-9._-]+`)
from the spec; a spec containing ` @ ` is a direct reference — put the FULL line
in `spec` for that case so the caller can classify it. Extras (`[...]`) stay on
the spec side and are ignored afterwards.

**`check_requirements(entries, *, allowlist, path) -> list[Finding]`**

- spec contains `git+`, or ` @ http`, or starts with `-e `, or the raw line began
  with `--index-url`/`--extra-index-url` → `untrusted-source`.
- spec has no `==` → `unpinned-dependency` (`why = "no exact == pin"`).
- then `check_name(name, ...)`.
An entry can produce more than one finding (e.g. unpinned AND unknown) — keep both.

**`parse_imports(source: str) -> frozenset[str]`** — `ast.parse(source)`; walk
`ast.Import` and `ast.ImportFrom`; skip `ImportFrom` nodes with `level > 0`
(relative); collect the root segment of every module name. `SyntaxError` →
raise `ValueError` (the caller abstains for that file; it does not guess).

**`is_first_party(root_dir: Path, module: str) -> bool`** — root segment is in
`FIRST_PARTY_ROOTS`, or `<root_dir>/<module>.py` exists, or
`<root_dir>/<module>/__init__.py` exists.

**`check_imports(modules, *, allowlist, root_dir, path, line=0) -> list[Finding]`**
— for each module sorted: skip `sys.stdlib_module_names` membership, skip
`is_first_party`; then `check_name(...)`.

**`new_imports(old: frozenset[str], new: frozenset[str]) -> list[str]`** —
`sorted(new - old)`.

**CLI**
- `python3 -m mini_ork.verify.dep_guard --write-allowlist <path>` — read
  `pyproject.toml` at the repo root with `tomllib`, collect
  `project.dependencies` plus every `project.optional-dependencies` list,
  take each entry's leading name, add the `IMPORT_ALIASES` values for names
  present, write the normalized sorted list under the exact header above,
  print `<n> names written`. Exit 0.
- `python3 -m mini_ork.verify.dep_guard --root <dir> [--path ...]` — scan and
  print `{"findings": [...], "count": n}`; exit 0 clean / 1 findings / 3 when
  the allowlist is missing.

### 3. `recipes/code-fix/verifiers/dep_guard.py` (new)

Model on `recipes/code-fix/verifiers/fallback_guard.py`: single-line JSON on
stdout, `MO_TARGET_CWD` (else cwd) as `root`, change surface from
`MINI_ORK_TOUCHED_FILES` (newline-separated) else
`mini_ork.verify.touched_files.touched_files(root)`, and a `_load_module` helper
that tries the package import after inserting `root` into `sys.path`.

Package import is deferred to `importlib.import_module("mini_ork.verify.dep_guard")`;
**no local module may be named `dep_guard` twice in `sys.modules`** — if the
import fails, emit the abstention envelope and exit 0 (no traceback, no pass).

**What it scans** — whole current content of each touched path (not a diff):

- path matches `REQUIREMENTS_RE` → `parse_requirements` + `check_requirements`;
- `pyproject.toml` → `tomllib.loads`; `project.dependencies` and every
  `optional-dependencies` list; run the same three requirement rules with
  `line = 0`;
- path ends `.py` → `parse_imports(current)`; the base version via
  `git -C <root> show <base>:<path>` (`base = touched_files.scoped_base(root)`);
  when that command fails (new file, no base) treat the old set as empty;
  `new_imports` → `check_imports`. A file that fails to parse is recorded in
  `unparsed` and never silently skipped.

**Envelope** — `verifier: "dep_guard"`, `pass`, `verdict`
(`pass`/`fail`/`abstain`), `status: "unverified"` and
`failure_kind: "toolchain"` only on abstention, `evidence_path: null`,
`checks_run: ["no-unpinned-dependency", "no-untrusted-source", "no-unknown-dependency"]`,
`failed_checks` (distinct rule ids sorted, else `[]`), `checks` (one entry per
check with `{"name", "pass", "gating", "detail"}`), `reasons`, `checked_criteria`,
`error_summary`, `findings` (raw dicts), `unparsed` (paths).

- **Abstain, exit 0**: allowlist file missing (`MO_DEP_ALLOWLIST` overrides the
  default path; the path is resolved relative to `root`); change surface
  unattributable; module unimportable. `pass: false`, `verdict: "abstain"`.
- **Fail, exit 1**: any finding.
- **Pass, exit 0**: `<r> requirements and <i> new imports checked`.

### 4. `recipes/code-fix/workflow.yaml`

Insert this node immediately AFTER the `test` node and change nothing else about
the existing nodes:

```yaml
  # SEC-G01-T03: offline dependency gate — a new import or requirement must
  # exist in the vendored allowlist, carry an exact pin, and not come from a
  # URL/VCS source. Abstains (never passes) without its allowlist snapshot.
  - name: dependencies
    type: verifier
    prompt_ref: null
    verifier_ref: verifiers/dep_guard.py
    dispatch_mode: serial
```

Add exactly these four edges, right after the existing `test` edges:

```yaml
  - { from: implementer, to: dependencies, edge_type: depends_on      }
  - { from: dependencies, to: reviewer,    edge_type: depends_on      }
  - { from: dependencies, to: rollback,    edge_type: escalates_to    }
  - { from: dependencies, to: implementer, edge_type: retries, max_rounds: 2 }
```

Also extend the top-level `description:` to
`"Single-patch code fix with typecheck, fallback-guard, test, dependency, and reviewer gates"`.

### 5. `tests/unit/test_verify_dep_guard_py.py` (new)

Hermetic: `tmp_path`, no network. Import `mini_ork.verify.dep_guard` normally.

- **`norm`**: `"PyYAML" == "pyyaml"`, `"python-dateutil" == "python_dateutil"`,
  `"uvicorn[standard]"` normalizes to `"uvicorn_standard"`.
- **`one_edit_apart`**: `("requets","requests")` True;
  `("requests","requests")` False; `("reqests","requests")` True;
  `("reqesst","requests")` False (distance 2).
- **`load_allowlist`**: comments and blanks skipped; a missing file raises.
- **requirements**: `requests==2.31.0` → no findings against an allowlist of
  `{"requests"}`; `requests` → `unpinned-dependency`;
  `evilpkg` → `unpinned-dependency` + `unknown-dependency`;
  `reqeusts` (one edit from an allowlisted `requests`) → `squat-suspect`;
  `pkg @ https://evil.example/p.whl` → `untrusted-source`;
  `pkg @ git+https://github.com/x/y` → `untrusted-source`; a `# comment` and a
  `-r other.txt` line produce nothing.
- **imports**: `parse_imports("import os, yaml\nfrom x.y import z\n")` →
  `{"os","yaml","x"}`; a relative `from . import a` contributes nothing; a
  `SyntaxError` raises `ValueError`.
- **first party / stdlib**: `check_imports({"os","json"}, ...)` → no findings;
  `check_imports({"mini_ork"}, ...)` → no findings; a `tmp_path` repo with
  `ownmod.py` makes `check_imports({"ownmod"}, root_dir=tmp_path)` clean;
  `check_imports({"requets"}, allowlist={"requests"})` → one `squat-suspect`.
- **`new_imports`**: `({"a","b"}, {"b","c"})` → `["c"]`.
- **CLI `--write-allowlist`**: write a `pyproject.toml` in `tmp_path` with
  `dependencies = ["PyYAML>=6.0", "jsonschema>=4.0"]` and
  `[project.optional-dependencies] web = ["fastapi>=0.110"]`, run
  `--write-allowlist`, assert the file's body lines are exactly
  `["fastapi", "jsonschema", "pyyaml", "yaml"]` and the header starts with `#`.
- **verifier subprocess**: in a `tmp_path` git repo (`git init`, config
  user, one commit) run `sys.executable recipes/code-fix/verifiers/dep_guard.py`
  with `cwd = repo`, `MO_TARGET_CWD = repo`, `MO_DEP_ALLOWLIST` pointing at a
  file containing only `yaml`: (a) touched `needs.py` = `import requets\n` →
  exit 1, `"squat-suspect"` in `failed_checks`; (b) `needs.py` = `import yaml\n`
  → exit 0, `verdict == "pass"`; (c) `MO_DEP_ALLOWLIST` pointing at a missing
  path → exit 0, `verdict == "abstain"`, `failure_kind == "toolchain"`.

## Files in scope (touch ONLY these)

- `mini_ork/verify/dep_guard.py` (new)
- `recipes/code-fix/data/dependency-allowlist.txt` (new, generated by the CLI)
- `recipes/code-fix/verifiers/dep_guard.py` (new)
- `recipes/code-fix/workflow.yaml` (one node, four edges, description line)
- `tests/unit/test_verify_dep_guard_py.py` (new)

Do NOT touch `recipes/code-fix/verifiers/test.py`, `typecheck.py`,
`fallback_guard.py`, `mini_ork/verify/gaming_scan.py`, `touched_files.py`,
`levels.py`, any other recipe, or any doc.

## Verification commands

Run each and require exit 0:

1. `python3 -m pytest -q -p no:asyncio tests/unit/test_verify_dep_guard_py.py`
2. `python3 -m pytest -q -p no:asyncio tests/unit/test_tool_grants_py.py tests/unit/test_eco6_k14_edge_types.py`
3. `python3 -m ruff check --select F,E9 mini_ork/verify/dep_guard.py recipes/code-fix/verifiers/dep_guard.py tests/unit/test_verify_dep_guard_py.py`
4. `python3 -c "import yaml;w=yaml.safe_load(open('recipes/code-fix/workflow.yaml'));n={x['name'] for x in w['nodes']};assert {'dependencies','test'} <= n, n;print(sorted(n))"`
5. `python3 -c "from mini_ork.verify import dep_guard as d; assert d.one_edit_apart('requets','requests')"`

## Self-application measurement

```measure
python3 -m pytest -q -p no:asyncio tests/unit/test_verify_dep_guard_py.py --junitxml="${MINI_ORK_RUN_DIR}/sec-g01t03-junit.xml"
```

## Required artifacts — declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/sec-g01t03-junit.xml`

## Success criteria

- All five verification commands pass, with every pre-existing test unchanged.
- A one-edit name (`requets`) is refuted, an allowlisted import passes, and a
  missing allowlist snapshot abstains instead of passing.

## Rules

Edit files directly; do not emit unified diffs. Execute this spec exactly; do
not research and do not add lens or drafter nodes. When the spec and your
preference disagree, the spec wins.
