# SEC-G06-T05: a deterministic security gate in the `code-fix` verifier set

## Goal

`code-fix` gates tests, types and silent fallbacks. It has no security check at
all: nothing looks at the diff an agent just wrote for a committed credential or
a dangerous-API addition. Add that gate — deterministic, scoped to the same
change surface the other verifiers use (`MINI_ORK_TOUCHED_FILES`, else the run's
dirty set), failing closed on a hit and holding (never passing) when it cannot
measure.

Source: `docs/research/2026-11-13-security-technique-review.md`, G06-T05
(priority 10.0, rank 2). This is the first slice only: pattern rules over added
lines. The candidate-saturating entropy classifier is G10-T08, a separate item —
do not build it here.

All paths are relative to the repository root.

## Mechanism (exact spec)

### 1. `mini_ork/verify/security_scan.py` (new)

Standard library plus one import: `from mini_ork.verify.gaming_scan import added_lines`.
No network, no subprocess except the CLI's own `git diff`, no model access.
Module docstring: three sentences saying what it scans, that it only reads ADDED
lines, and that findings never carry the matched text.

**Rule table** — exactly these ten entries, in this order, as a module constant
`RULES: tuple[tuple[str, str, str], ...]`:

```python
RULES = (
    ("private-key", r"-----BEGIN [A-Z ]*PRIVATE KEY-----", "private key material"),
    ("aws-access-key", r"\bAKIA[0-9A-Z]{16}\b", "AWS access key id"),
    ("github-token", r"\bgh[pousr]_[A-Za-z0-9]{20,}\b", "GitHub token"),
    ("provider-api-key", r"\bsk-[A-Za-z0-9_-]{20,}\b", "provider API key"),
    ("slack-token", r"\bxox[abposr]-[A-Za-z0-9-]{10,}\b", "Slack token"),
    ("credential-literal", r"(?i)\b(?:password|passwd|secret|api_key|apikey|access_token)\b\s*[:=]\s*[\"'][^\"']{8,}[\"']", "credential literal"),
    ("world-writable", r"\bchmod\s+(?:-R\s+)?(?:0?777|a\+rwx)\b", "world-writable permission"),
    ("tls-verify-off", r"\bverify\s*=\s*False\b", "TLS verification disabled"),
    ("shell-exec", r"\bos\.system\(|\bshell\s*=\s*True\b", "shell execution"),
    ("unsafe-deserialization", r"\b(?:pickle|marshal)\.loads\(|\byaml\.load\(", "unsafe deserialization"),
)
```

**`@dataclass(frozen=True) class Finding`** — fields in order: `path: str`
(repo-relative, forward slashes), `line: int` (1-based in the new file),
`rule: str`, `why: str`, `text: str = "<redacted>"`. `text` exists so the
envelope shape matches the other verifiers; it is ALWAYS the literal
`"<redacted>"` — a credential must never travel into a verdict file.

**`allowance(line_text: str) -> frozenset[str]`** — the rule ids excused on this
line. A line may carry `# sec-allow: <rule>[,<rule>...]` (case-sensitive marker,
one or more comma-separated ids); return those ids. No marker → empty set. This
is how a fixture or an intentionally dangerous test string is excused, per line
and per rule — a whole-file opt-out does not exist.

**`scan_added(added: Mapping[str, Mapping[int, str]]) -> list[Finding]`**

- For each path in `sorted(added)`, for each `lineno` in sorted line order:
  - `text = raw.strip()`; skip it when blank;
  - `excused = allowance(text)`;
  - for each `(rule, pattern, why)` in `RULES`, skip the rule when its id is in
    `excused`; otherwise `re.search(pattern, text)` — a match appends
    `Finding(path, lineno, rule, why)`.
- Duplicates are impossible (one finding per rule per line), so no set is needed.
- Return sorted by `(path, line, rule)`.
- Do not skip comments, test paths or `# noqa` lines: a credential in a comment
  still ships. Only the `sec-allow` marker excuses a line.

**`build_added(root: str, base: str) -> tuple[dict, list[str]]`**

- `diff = git -C root diff --no-color -U0 <base>` when `base` is non-empty, else
  `git -C root diff --no-color -U0`; parse the stdout with `added_lines`.
- Then for every path in `git -C root status --porcelain -uall` that is
  *untracked* (`??` prefix) and not already in the parsed dict: read the file as
  UTF-8 (`errors="replace"`), and record every line as added —
  `{path: {i: line}}`. Store paths forward-slashed.
- Second return value: the list of paths that could not be read (missing on
  disk — deleted after staging); those are reported, never silently dropped.
- Any `git` non-zero exit returns `({}, [])` — the caller decides that this is
  an abstention, not that the diff is clean.

**CLI** — `python3 -m mini_ork.verify.security_scan --root <dir> [--base <ref>]`
prints `{"findings": [...], "count": <n>, "unreadable": [...]}` (findings as
`{"path", "line", "rule", "why", "text"}`) and exits 0 when `count == 0`, 1 when
there are findings, 3 when `build_added` could not produce a diff at all.

### 2. `recipes/code-fix/verifiers/security.py` (new)

Model the file on `recipes/code-fix/verifiers/fallback_guard.py`: same
`_load_module` idea (try `import mini_ork.verify.security_scan` after inserting
`root` into `sys.path`; on failure emit the abstention envelope and exit 0 — no
silent pass, no traceback), same `_touched`/`is_git_worktree` scope resolution,
same single-line JSON on stdout.

- `root = os.environ.get("MO_TARGET_CWD", "").strip() or os.getcwd()`
- Change surface: `MINI_ORK_TOUCHED_FILES` (newline-separated) when set, else
  `mini_ork.verify.touched_files.touched_files(root)`.
- Base ref: `mini_ork.verify.touched_files.scoped_base(root)`.
- Run the scan over `build_added(root, base)`.
- Envelope keys: `verifier: "security"`, `pass`, `verdict`
  (`"pass"`/`"fail"`/`"abstain"`), `status: "unverified"` only on abstention,
  `failure_kind: "toolchain"` only on abstention, `evidence_path: null`,
  `checks_run: ["no-credential-in-diff", "no-dangerous-api-in-diff"]`,
  `failed_checks` (the distinct rule ids, sorted, when failing, else `[]`),
  `checks` (one entry per check: `{"name", "pass", "gating", "detail"}`),
  `reasons` (`[]` when passing, else one line naming the count and the first
  three `path:line rule` hits), `checked_criteria` (the two check names),
  `error_summary`, `findings` (the raw finding dicts, `text` always
  `"<redacted>"`).
- **Abstentions** — each emits `pass: false`, `verdict: "abstain"`,
  `status: "unverified"`, `failure_kind: "toolchain"`, a reason naming the
  cause, and **exit 0** so the executor withholds publish instead of rolling
  back: (a) the scanner module cannot be imported; (b) the change surface
  cannot be attributed (`_touched` returns `None`); (c) `build_added` produced
  nothing because `git` failed. An empty diff that `git` DID produce is not an
  abstention — it is a pass with detail `"no added lines"`.
- **Fail** — any finding: exit 1, `verdict: "fail"`.
- **Pass** — no findings: exit 0, `verdict: "pass"`, detail
  `"<n> added lines across <m> files"`.

### 3. `recipes/code-fix/workflow.yaml`

Insert this node immediately after the `fallbacks` node (same indentation, same
key order) and change nothing else about the existing nodes:

```yaml
  # SEC-G06-T05: deterministic security gate over the same change surface the
  # test/typecheck/fallback gates use — committed credentials and dangerous-API
  # additions. Fails closed on a hit; abstains (never passes) when the scanner
  # or the change surface cannot be resolved.
  - name: security
    type: verifier
    prompt_ref: null
    verifier_ref: verifiers/security.py
    dispatch_mode: serial
```

Add exactly these four edges, in this order, placed right after the existing
`fallbacks` edges:

```yaml
  - { from: implementer, to: security,   edge_type: depends_on      }
  - { from: security,    to: reviewer,   edge_type: depends_on      }
  - { from: security,    to: rollback,   edge_type: escalates_to    }
  - { from: security,    to: implementer, edge_type: retries, max_rounds: 2 }
```

Also extend the top-level `description:` to
`"Single-patch code fix with typecheck, fallback-guard, security, test, and reviewer gates"`.

### 4. `tests/unit/test_verify_security_scan_py.py` (new)

Hermetic (`tmp_path`, no network, no `git` required for the `scan_added` cases).
The module under test is `mini_ork.verify.security_scan`; import it normally.

Helper `_added(**paths)` builds `{path: {n: text}}` from `{"a.py": "x\ny"}` style
strings (1-based line numbers).

- **`credential-literal`** fires on `password = "hunter2hunter2"` and on
  `api_key: 'abcdef123456'`; does not fire on `password = os.environ["PW"]` or
  `password = ""`.
- **`aws-access-key`** fires on `key = "AKIAIOSFODNN7EXAMPLE"`; does not fire on
  `AKIA1234` (too short).
- **private-key / github-token / slack-token / provider-api-key** each fire on
  their canonical fixture line and not on an obviously benign line.
- **dangerous rules**: `world-writable` on `chmod 777 /tmp/x`, `tls-verify-off`
  on `requests.get(url, verify=False)`, `shell-exec` on `os.system(cmd)`,
  `unsafe-deserialization` on `pickle.loads(blob)`.
- **no text leak**: every finding's `text == "<redacted>"` and the matched
  secret never appears in `repr(findings)`.
- **allowance**: `x = "AKIAIOSFODNN7EXAMPLE"  # sec-allow: aws-access-key`
  produces no finding; the same line with `# sec-allow: github-token` still
  produces the `aws-access-key` finding.
- **ordering**: a two-path, two-rule diff returns findings sorted by
  `(path, line, rule)`.
- **`build_added`** in a `tmp_path` git repo (`git init`, `git config
  user.email/user.name`, commit an empty file, then write a new untracked file
  containing a secret): the returned dict contains the untracked path with its
  lines numbered from 1, and the scan over it finds the credential. Skip this
  test with `pytest.mark.skipif(shutil.which("git") is None, ...)`.
- **`build_added` with a bogus base** (`"deadbeef"`) returns `({}, [])`.

### 5. `recipes/code-fix/verifiers/security.py` end-to-end check

Add to the SAME test file a subprocess test that runs the verifier script
(`sys.executable verifiers/security.py`) twice in a `tmp_path` git repo, with
cwd = repo root and `MO_TARGET_CWD=<repo>`, `MINI_ORK_TOUCHED_FILES=leaky.py`:

- a `leaky.py` holding an AWS key → exit 1, JSON has
  `verifier == "security"`, `verdict == "fail"`, `"aws-access-key"` in
  `failed_checks`, and `"<redacted>"` as every finding's `text`;
- a clean `leaky.py` → exit 0, `verdict == "pass"`;
- with `MINI_ORK_TOUCHED_FILES=""` and no touched files → either a pass (empty
  diff) or `verdict == "abstain"`; assert only that it never exits 1 with an
  empty `findings` list (an abstention must not look like a refutation).

## Files in scope (touch ONLY these)

- `mini_ork/verify/security_scan.py` (new)
- `recipes/code-fix/verifiers/security.py` (new)
- `recipes/code-fix/workflow.yaml` (the one node, four edges, description line)
- `tests/unit/test_verify_security_scan_py.py` (new)

Do NOT touch `recipes/code-fix/verifiers/test.py`, `typecheck.py`,
`fallback_guard.py`, `mini_ork/verify/gaming_scan.py`, `levels.py`,
`touched_files.py`, any other recipe, or any doc.

## Verification commands

Run each and require exit 0:

1. `python3 -m pytest -q -p no:asyncio tests/unit/test_verify_security_scan_py.py`
2. `python3 -m pytest -q -p no:asyncio tests/unit/test_tool_grants_py.py tests/unit/test_eco6_k14_edge_types.py`
3. `python3 -m ruff check --select F,E9 mini_ork/verify/security_scan.py recipes/code-fix/verifiers/security.py tests/unit/test_verify_security_scan_py.py`
4. `python3 -c "import yaml;w=yaml.safe_load(open('recipes/code-fix/workflow.yaml'));n={x['name'] for x in w['nodes']};assert {'security','test','typecheck','fallbacks'} <= n, n;print(sorted(n))"`

## Self-application measurement

```measure
python3 -m pytest -q -p no:asyncio tests/unit/test_verify_security_scan_py.py --junitxml="${MINI_ORK_RUN_DIR}/sec-g06t05-junit.xml"
```

## Required artifacts — declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/sec-g06t05-junit.xml`

## Success criteria

- All four verification commands pass, with every pre-existing test unchanged.
- The seeded-credential test shows the gate refuting a diff the current verifier
  set would have passed, and the abstention paths never report `pass: true`.

## Rules

Edit files directly; do not emit unified diffs. Execute this spec exactly; do
not research and do not add lens or drafter nodes. When the spec and your
preference disagree, the spec wins.
