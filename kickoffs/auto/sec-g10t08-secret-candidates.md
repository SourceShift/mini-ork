# SEC-G10-T08: candidate-saturating secret detection beyond regex

## Goal

Regex rule sets find secrets by format, and most secrets have no format: the best
static scanner recovers 36.6% of runtime-exposed credentials, and a scanner that
only knows `AKIA…`, `ghp_…`, `sk-…` is blind to a random 44-character opaque
token by construction. Invert the direction: promote *every* plausible string
literal to a candidate, score it deterministically on intrinsic signal plus code
context, and route the uncertain middle band to review instead of silently
passing it as clean. No model, no network, no entropy-only verdict: the score is
a fixed, inspectable formula, and the band boundaries are the contract.

Source: `docs/research/2026-11-13-security-technique-review.md`, G10-T08
(rank 34). First slice only: no classifier training, no runtime instrumentation,
no gitleaks replacement — a candidate pass that runs beside it.

All paths are relative to the repository root.

## Mechanism (exact spec)

### 1. `mini_ork/verify/secret_candidates.py` (new)

Standard library only (`ast`, `json`, `math`, `os`, `re`, `subprocess`, `sys`,
`pathlib`, `typing`). Start with `from __future__ import annotations` (the
runtime is Python 3.9, where `str | None` at definition time is a `TypeError`).
No network, no model. Module docstring: three sentences — every plausible
literal is a candidate, the score is a fixed formula rather than a learned
classifier, and the middle band abstains to review rather than passing.

**Constants**

```python
LITERAL_MIN = 12
FLAG_BAND = 0.70
REVIEW_BAND = 0.45
FLAGGED = "flagged"
REVIEW = "review"
CLEAN = "clean"
BANDS = (FLAGGED, REVIEW, CLEAN)
MAX_FILE_BYTES = 1 << 20          # 1 MiB
BINARY_EXTENSIONS = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".zip", ".gz",
    ".bz2", ".xz", ".whl", ".so", ".dylib", ".dll", ".class", ".jar", ".pyc",
})
CONTEXT_RE = re.compile(
    r"(?i)(?:secret|token|passw|api[-_]?key|credential|auth|bearer|"
    r"private[-_]?key|access[-_]?key|signing|client[-_]?secret)"
)
_PLACEHOLDER_RE = re.compile(
    r"(?i)^(?:.*(?:example|sample|dummy|placeholder|changeme|redacted|"
    r"your[-_ ]|xxxx|todo|fixme|none|null|true|false).*)$"
)
_FORMAT_RE = re.compile(r"^(?:<[^<>]*>|\$\{[^${}]*\}|\{\w*\}|%[sd]|\.\.\.)$")
```

**`Candidate`** — `NamedTuple(path: str, line: int, literal: str, score: float,
band: str, reasons: tuple[str, ...])`.

**`shannon(text: str) -> float`** — Shannon entropy in bits per character over
the character distribution of `text`; `0.0` for `""` and for a single repeated
character.

**`_classes(text: str) -> int`** — how many of lowercase, uppercase, digit,
other are present (0–4).

**`demote(literal: str) -> str | None`** — the reason this literal cannot be a
secret, or `None` when it survives. In order:

1. `" "`, `"\t"`, or `"\n"` in it → `"contains whitespace"` (a secret is one
   opaque token; a sentence is not);
2. `_PLACEHOLDER_RE` or `_FORMAT_RE` match → `"placeholder or format spec"`;
3. it starts with `"/"`, `"./"`, `"../"`, `"~/"`
   → `"path-like"`;
4. it starts with `"http://"` or `"https://"` and has no `"="` after a `"?"`
   → `"url without a query parameter"`;
5. `len(set(literal)) <= 3` → `"low character variety"`.

**`score_literal(literal: str, context: str) -> tuple[float, tuple[str, ...]]`**
— `context` is the up-to-200 characters preceding the literal on its own line
and the line before it. If `demote` fires, return `(0.0, (reason,))`. Otherwise:

```
entropy_part  = 0.35 * min(shannon(literal) / 4.5, 1.0)
length_part   = 0.25 * (1.0 if 16 <= len(literal) <= 128 else 0.5)
classes_part  = 0.20 * (1.0 if _classes(literal) >= 3 else 0.0)
context_part  = 0.20 * (1.0 if CONTEXT_RE.search(context) else 0.0)
score         = round(entropy_part + length_part + classes_part + context_part, 4)
```

Reasons record which parts fired, as `"entropy=<x.xx>"`, `"length=<n>"`,
`"classes=<n>"`, `"context"` / `"no-context"` — sorted, never free text.

**`band(score: float) -> str`** — `FLAGGED` when `score >= FLAG_BAND`, `REVIEW`
when `score >= REVIEW_BAND`, else `CLEAN`.

**`_context_for(lines: list[str], lineno: int, column: int) -> str`** — the
concatenation of `lines[lineno - 2]` (when it exists) and
`lines[lineno - 1][:column]`, truncated to the last 200 characters.

**`extract_literals(text: str) -> list[tuple[int, int, str]]`** — `(lineno,
column, literal)` triples with `len(literal) >= LITERAL_MIN`:

- try `ast.parse(text)`; on `SyntaxError` fall back to the regex path;
- from the AST take every `ast.Constant` whose value is a `str`, using
  `node.lineno` and `node.col_offset`;
- the regex path is
  `re.finditer(r"""(["'])((?:\\.|(?!\1).)*)\1""", text)` over each line, keeping
  matches whose group length meets the minimum.

**`scan_text(path: str, text: str) -> list[Candidate]`** — one candidate per
extracted literal: score it, attach `band`, drop candidates below
`LITERAL_MIN`, sort by `(line, column)`.

**`scan_file(root: str, rel: str) -> tuple[list[Candidate], str]`** — `(found,
"")` on success; otherwise `([], reason)` where the reason starts `skipped:` for
a path that does not exist, is not a regular file, exceeds `MAX_FILE_BYTES`, has
a `BINARY_EXTENSIONS` suffix, or carries a NUL byte in its first 4 KiB, and
`unreadable:` (or `escapes root`) when the file exists under `root` and could
not be read. Decode as UTF-8 with `errors="replace"`.

**`scan(root: str, paths: list[str]) -> ScanResult`** — candidates for each
path, deduplicated, sorted by `(path, line)`, plus two note lists:
`unreadable` (a file that exists and could not be read, or a path that escapes
`root`) and `skipped` (missing, non-regular, binary, oversized, NUL-bearing).
The split matters: an unreadable surface must never be reported clean.

**`summarize(result: ScanResult, *, show_literal: bool = False) -> dict`** —
`{"counts": {band: n}, "candidates": [...], "unreadable": [...], "skipped":
[...]}` with the candidate dicts `{"path", "line", "literal", "score", "band",
"reasons"}`. The literal is masked by default — ends, length and a digest — so a
report never republishes the string this pass exists to catch; `--show-literal`
opts back in when an operator is triaging one candidate by hand.

**`main(argv=None) -> int`** — argparse `--root` (default `.`), `--paths`
(comma- or newline-separated; when omitted read `MINI_ORK_TOUCHED_FILES`;
empty → usage error 2), `--json`.

Exit codes: `1` when at least one candidate is `FLAGGED`; `3` when none is
flagged but at least one is `REVIEW` (the uncertain band abstains — it is not a
pass) **or** anything on the surface was unreadable; `0` only when every
candidate is `CLEAN` and the surface was fully measured. Text mode prints
`<path>:<line> <band> <score> <masked literal>` per non-clean candidate and a
final `secret-candidates: <n> flagged, <m> review, <k> clean, <u> unreadable`.

### 2. `tests/unit/test_verify_secret_candidates_py.py` (new)

Hermetic: `tmp_path` files, no network, no git required.

- `shannon`: `""` and `"aaaa"` are `0.0`; `"ab"` repeated is `1.0`; a 32-char
  random-looking hex string is between 3.0 and 4.5.
- `demote`: `"correct horse battery staple"` → whitespace; `"YOUR_TOKEN_HERE"`,
  `"changeme"`, `${TOKEN}`, `<your-key>` → placeholder/format; `/etc/passwd` →
  path-like; `https://example.com/` → url; `"aaaaaaaaaaaaaaaaaaaa"` → variety;
  a 40-character mixed token → `None`.
- `score_literal` is deterministic and bounded: the same input scores the same
  twice, and every score is in `[0.0, 1.0]`; the `context` bonus moves the score
  up by exactly `0.20` for an otherwise identical literal.
- `band`: `0.70 → FLAGGED`, `0.6999 → REVIEW`, `0.45 → REVIEW`, `0.4499 →
  CLEAN`.
- **The formatless positive** (the point of the slice): a file containing
  ```python
  _signing_key = "Zk4Qm9vRt2Xb7Lp0Ws3Nh6Yc8Jd1fA5gE2uT"
  ```
  (36 characters, no known vendor prefix) yields a `FLAGGED` candidate — and
  `mini_ork.verify.security_scan.scan_added({path: {lineno: line}})` yields **no**
  finding for that same line, asserting in the test that the regex rules cannot
  see it.
- Negatives stay clean: a sentence, a path, a URL, a placeholder and an
  environment lookup (`os.environ["AWS_SECRET_ACCESS_KEY"]`) all score below
  `FLAG_BAND`, and none of them is `FLAGGED`.
- `extract_literals` finds a Python literal through the AST and still finds one
  in a deliberately malformed `.py` file (the regex fallback), and ignores
  literals shorter than `LITERAL_MIN`.
- `scan` skips a `BINARY_EXTENSIONS` file, a file containing a NUL byte, an
  oversize file and a missing path without raising; a test fixture under
  `tests/` is still reported (path is context, not an excuse — it just does not
  raise the score).
- **CLI**: `main(["--root", str(root), "--paths", "app/x.py", "--json"])`
  returns `1` with one flagged candidate; pointed at a clean file it returns
  `0`; pointed at a file whose only confident hit is a placeholder-free but
  low-entropy literal in the review band it returns `3`. Every `--json` result
  parses and carries `counts` and `candidates`.

## Files in scope (touch ONLY these)

- `mini_ork/verify/secret_candidates.py` (new)
- `tests/unit/test_verify_secret_candidates_py.py` (new)

Do NOT touch `.gitleaks.toml`, `Makefile`, `mini_ork/verify/security_scan.py`,
any `code-fix` verifier, `workflow.yaml`, `artifact_contract.yaml`, or any doc.

## Verification commands

Run each and require exit 0:

1. `python3 -m pytest -q -p no:asyncio tests/unit/test_verify_secret_candidates_py.py`
2. `python3 -m pytest -q -p no:asyncio tests/unit/test_verify_security_scan_py.py`
3. `python3 -m ruff check --select F,E9 mini_ork/verify/secret_candidates.py tests/unit/test_verify_secret_candidates_py.py`
4. `python3 -m mini_ork.verify.secret_candidates --help`

## Self-application measurement

```measure
python3 -m pytest -q -p no:asyncio tests/unit/test_verify_secret_candidates_py.py --junitxml="${MINI_ORK_RUN_DIR}/sec-g10t08-junit.xml"
```

## Required artifacts — declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/sec-g10t08-junit.xml`

## Done when

1. A 36-character formatless token with no vendor prefix is `FLAGGED` — and the
   test proves the shipped regex rules produce no finding for the same line, so
   the uplift is real and not a re-spelling of `security_scan`.
2. The middle band returns exit `3`, never `0`: an uncertain literal abstains to
   review and is not silently reported clean.
3. Placeholders, paths, URLs, sentences and low-variety strings never reach
   `FLAGGED`, and the same input always produces the same score.
4. Binary, NUL-containing, oversize and missing files are skipped without
   raising.
5. All four verification commands pass and every pre-existing test is unchanged.

## Rules

Edit files directly; do not emit unified diffs. Execute this spec exactly; do
not research and do not add lens or drafter nodes. When the spec and your
preference disagree, the spec wins.
