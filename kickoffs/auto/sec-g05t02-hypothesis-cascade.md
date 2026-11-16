# SEC-G05-T02: tiered hypothesis-then-confirm discovery with deterministic oracles

## Goal

Detection as a three-tier cascade over the run's change surface: a cheap
proposer turns each added dangerous-API line into a vulnerability *hypothesis*,
a deterministic static-signal rank decides which hypotheses are worth an
expensive check, and only a hypothesis a deterministic oracle actually confirms
is reported as a finding. The oracle — an executed probe, never a model and
never a bare pattern match — is the accept rule; an unconfirmed hypothesis is
recorded as an abstention, not a finding and not a cleared line.

Source: `docs/research/2026-11-13-security-technique-review.md`, G05-T02
(rank 16). First slice only: no model proposer, no PoC synthesis, no fuzzing —
a deterministic cascade that runs beside the shipped `security` gate.

All paths are relative to the repository root.

## Mechanism (exact spec)

### 1. `mini_ork/verify/hypothesis_cascade.py` (new)

Standard library only (`ast`, `json`, `os`, `re`, `subprocess`, `sys`, `pathlib`,
`typing`, `dataclasses`). Start with `from __future__ import annotations` (the
runtime is Python 3.9, where `str | None` at definition time is a `TypeError`).
No network, no model, no new dependency. Module docstring: three sentences —
every added dangerous-API line becomes a hypothesis, a deterministic rank decides
which ones reach the oracle tier, and only an executed probe can confirm one.

Reuse, do not re-implement: `mini_ork.verify.security_scan.Finding`,
`security_scan.scan_added`, and `security_scan.allowance` (so a `# sec-allow:`
marker excuses a line here too).

**Constants**

```python
CONFIRMED = "confirmed"
CLEARED = "cleared"
ABSTAINED = "abstained"
OUTCOMES = (CONFIRMED, CLEARED, ABSTAINED)
TIER3_CAP = 20                    # hypotheses allowed to reach the oracle tier
PROBE_TIMEOUT_S = 60
PROBE_DIR = "hypotheses"          # under $MINI_ORK_RUN_DIR
SINKS: tuple[tuple[str, str, int], ...] = (
    # (rule, regex, severity 1..3)
    ("sql-fstring", r"(?i)\b(?:SELECT|INSERT|UPDATE|DELETE)\b[^\"']*[\"']\s*\+|f[\"'][^\"']*(?:SELECT|INSERT|UPDATE|DELETE)\b[^\"']*\{", 3),
    ("eval-exec", r"\b(?:eval|exec)\s*\(", 3),
    ("subprocess-shell", r"\b(?:subprocess\.(?:run|call|Popen|check_output)|os\.system|os\.popen)\s*\(|shell\s*=\s*True\b", 3),
    ("unsafe-deserialization", r"\b(?:pickle|marshal)\.loads\(|\byaml\.load\(", 2),
    ("tls-verify-off", r"\bverify\s*=\s*False\b", 2),
    ("untrusted-path-join", r"\bos\.path\.join\([^)]*\b(?:request|input|argv|environ)\b", 2),
)
SOURCES_RE = re.compile(
    r"\b(?:request\.(?:args|form|json|data|values|files)|input\s*\(|sys\.argv|"
    r"os\.environ|environ\.get)\b"
)
ENTRYPOINT_RE = re.compile(
    r"^\s*(?:@\w+\.(?:route|get|post|put|delete)|async\s+def\s+main\b|"
    r"def\s+main\b|if\s+__name__\s*==)"
)
```

**`Hypothesis`** — a frozen dataclass
`(id: str, rule: str, path: str, line: int, severity: int, score: int,
signals: tuple[str, ...], outcome: str = "", reason: str = "")`.
`id` is `"<rule>:<path>:<line>"`; it is stable across runs.

**`probe_name(hypothesis: Hypothesis) -> str`** — the probe's filename:
`"<rule>__" + path.replace("/", "~") + "__<line>.py"`. The id itself cannot be a
filename — it carries the `:` separator and the path's slashes. Nothing parses
the name back; the caller already holds the hypothesis.

**`function_span(lines: list[str], lineno: int) -> tuple[int, int]`** — the
1-based inclusive line range of the innermost `def`/`async def` enclosing
`lineno`. Take `indent` = the leading whitespace of the first non-blank line at
or above `lineno`. The definition is the nearest preceding line matching
`^\s*(?:async\s+)?def\s+\w+` whose indentation is shorter than `indent`; its span
ends immediately before the next non-blank line whose indentation is at or below
the definition's. When no such definition exists, return `(1, len(lines))` —
module-level code is its own span.

**`signals(lines: list[str], lineno: int, severity: int, path: str) -> tuple[int, tuple[str, ...]]`**
— `(score, signal names)`; deterministic, no I/O:

- `+severity` for the sink's severity, always `"severity=<n>"`;
- `+2 "untrusted-source"` when `SOURCES_RE` matches any line inside
  `function_span`;
- `+2 "entrypoint"` when `ENTRYPOINT_RE` matches any line inside the span;
- `+2 "added"` — every hypothesis is proposed from an added line, so this
  always fires and is what lets the rank prefer what the run actually wrote;
- `+1 "python"` when the path ends in `.py`.

Signals are returned sorted; the score is the plain sum.

**`propose(root: str, added: dict[str, dict[int, str]]) -> list[Hypothesis]`** —
one hypothesis per added line that matches a `SINKS` entry and is **not**
excused by `security_scan.allowance(line)`. Severity is the highest matching
sink's. Every hypothesis starts with `outcome=""`. `root` is needed because
`signals` reads the whole file: the function a line sits in, and the untrusted
source or route decorator in that function, are not in the added lines. Read it
with `encoding="utf-8", errors="replace"`; a missing or unreadable file scores
against the added lines alone (nothing raises). Return sorted by
`(-score, path, line)`.

**`rank(hypotheses: list[Hypothesis]) -> tuple[list[Hypothesis], list[Hypothesis]]`**
— sort by `(-score, -severity, path, line)` and truncate to `TIER3_CAP`; return
`(selected, deferred)`, the second unchanged so the report can name what was
never shown to the oracle tier.

**`probes(run_dir: str, hypotheses: Sequence[Hypothesis]) -> dict[str, str]`** —
for each hypothesis whose `<run_dir>/hypotheses/<probe_name>` exists, map its
`id` to that absolute path. A missing directory is an empty mapping, not an
error.

**`confirm(hypothesis: Hypothesis, probe: str, root: str) -> tuple[str, str]`** —
run the probe with `sys.executable`, cwd `root`, env carrying
`MO_HYPOTHESIS_PATH`, `MO_HYPOTHESIS_LINE` and `MO_HYPOTHESIS_RULE`, captured,
timeout `PROBE_TIMEOUT_S`:

- nonzero exit → `(CONFIRMED, "probe exited <n>")`;
- zero exit → `(CLEARED, "probe exited 0")`;
- `TimeoutExpired` → `(ABSTAINED, "probe timed out")`;
- `OSError` → `(ABSTAINED, "probe could not be executed: <err>")`.

**`evaluate(root: str, added: dict[str, dict[int, str]], run_dir: str) -> tuple[str, dict]`**
— return `(status, report)` where status is one of
`pass | fail | vacuous | abstain`:

1. no added lines, or no hypothesis proposed → `("vacuous", {"reason": "no dangerous-API line on the change surface"})`;
2. `rank` the hypotheses; for each selected hypothesis look up its probe:
   absent → `outcome=ABSTAINED, reason="no probe: nothing confirms this hypothesis"`;
   present → `confirm(...)`;
3. status `fail` when any hypothesis is `CONFIRMED`; otherwise `pass`;
4. report keys: `hypotheses` (list of dicts with `id, rule, path, line,
   severity, score, signals, outcome, reason`), `counts` (`{outcome: n}` for all
   three outcomes, zeros included), and `deferred` (ids past `TIER3_CAP`).

The layer never decides a hypothesis is a vulnerability on the strength of its
pattern: `security_scan` already reports the pattern, and this tier exists only
to hand a hypothesis to something that can execute it.

**`main(argv=None) -> int`** — argparse `--root` (default `.`), `--base`
(optional), `--paths` (comma- or newline-separated; when omitted read
`MINI_ORK_TOUCHED_FILES`; empty → usage error 2), `--json`. Added-line
collection is `security_scan.collect_added(root, base, only)` — it returns
`(added, unreadable, measured)`, so a third element of `False` means git failed:
report that as an abstention (`status = "abstain"`, reason naming git), never as
an empty clean surface. Without `--base`, pass `base=""` (the diff is then the
working-tree delta) and still pass `only` = the `--paths` list so the untracked
harvest stays bounded to the change surface.

Exit codes: `1` when any hypothesis is `CONFIRMED`; `3` when none is confirmed
but at least one is `ABSTAINED` (the uncertain band is a review item, never a
silent pass); `0` only when every hypothesis is `CLEARED` or there is nothing
to measure. Text mode prints one `HYPOTHESIS: <score>` block per hypothesis
(literal rules and paths, never matched source text) plus a final
`hypothesis-cascade: <n> confirmed, <m> cleared, <k> abstained, <d> deferred`.

### 2. `recipes/code-fix/verifiers/hypothesis.py` (new)

Follows `verifiers/security.py` exactly in shape: scope from
`MINI_ORK_TOUCHED_FILES` else the run's dirty set, module loaded from the ENGINE
(`_HERE.parents[2]`, then plain import), one single-line JSON envelope on
stdout, `MO_TARGET_CWD` as root, `MINI_ORK_RUN_DIR` for probes.

Mapping to the node contract:

- `status == "fail"` → `pass: false, verdict: "fail"`, exit **1**, and every
  confirmed hypothesis appears in `findings` (`rule`, `path`, `line`,
  `detail` = the probe reason).
- `status == "pass"` → `pass: true, verdict: "pass"`, exit 0, abstained
  hypotheses listed in `findings` with `gating: false`.
- `status == "vacuous"` → `pass: true, verdict: "vacuous"`, **no `status`
  key**, non-empty `error_summary`, one non-gating check, exit 0.
- `status == "abstain"` (module unloadable, change surface unattributable, git
  failure) → `status: "unverified"`, `failure_kind: "toolchain"`, exit 0.

A node abstention fails the whole run and withholds the publish, so only a
genuine inability to measure may carry it; "no hypothesis to check" is vacuous.
Correspondingly, `collect_added`'s `measured == False` (git failed) is the one
abstention this node must emit on an ordinary repository.

### 3. `recipes/code-fix/workflow.yaml` (edit)

Add a node `hypotheses` (type `verifier`, `verifier_ref: verifiers/hypothesis.py`,
`dispatch_mode: serial`) directly after `security`, and the edges
`security → hypotheses depends_on`, `hypotheses → reviewer depends_on`,
`hypotheses → rollback escalates_to`, `hypotheses → implementer retries
max_rounds: 2`. Extend the top-level `description` with `hypothesis`. Comment the
node with what produces probes (`${MINI_ORK_RUN_DIR}/hypotheses/<probe_name>`)
and that it is vacuous — never a false refusal — until something does.

### 4. `recipes/code-fix/artifact_contract.yaml` (edit)

Declare `- verifiers/hypothesis.py` with a one-line comment. Change nothing else.

### 5. `tests/unit/test_verify_hypothesis_cascade_py.py` (new)

Hermetic: `tmp_path`, no network, no git required except where a test opts in.

- `propose` returns a hypothesis for `q = "SELECT * FROM u WHERE id = " + user`
  and for `eval(payload)`, with severity 3, and `id == "<rule>:<path>:<line>"`.
- A `# sec-allow: eval-exec` comment on the line suppresses the hypothesis.
- A `verify = False` line yields severity 2; the highest severity wins when two
  sinks match one line.
- `signals`: a sink inside a function that mentions `request.args` and is
  reached from an `@app.route` scores the sink severity + 2 (source) + 2
  (entrypoint) + 2 (added) + 1 (python); the same sink at module level with no
  source scores severity + 2 + 1. Assert the exact integer for both.
- `rank` is deterministic (same input twice → same order) and truncates at
  `TIER3_CAP`, returning the overflow in `deferred`.
- `confirm`: a probe script that exits 1 → `CONFIRMED`; `exit 0` → `CLEARED`;
  a probe that sleeps past the timeout (monkeypatch `PROBE_TIMEOUT_S` to 1) →
  `ABSTAINED`; a missing probe → `ABSTAINED` with a reason naming it.
- `evaluate`: no added lines → `vacuous`; a hypothesis with no probe → `pass`
  with an `ABSTAINED` count and **no** finding; the same hypothesis with a
  failing probe → `fail`; a probe that passes → `pass` with `CLEARED`.
- **The anti-regex point**: a line matching a sink but with a passing probe is
  `CLEARED`, not reported — asserting that the pattern alone never confirms.
- CLI: `main(["--root", root, "--paths", "app/x.py", "--json"])` returns `1`
  with a confirmed hypothesis, `3` with only abstained ones, `0` on a clean
  file; every `--json` result parses and carries `counts` and `hypotheses`.
- Node: `vacuous` (no dangerous line) → `pass: true`, `verdict: "vacuous"`, no
  `status` key; `fail` → exit 1 and the finding names the probe reason;
  a confirmed hypothesis makes the node fail even though the pattern-matched
  `security` gate would have passed it on its own.

## Files in scope (touch ONLY these)

- `mini_ork/verify/hypothesis_cascade.py` (new)
- `recipes/code-fix/verifiers/hypothesis.py` (new)
- `recipes/code-fix/workflow.yaml`
- `recipes/code-fix/artifact_contract.yaml`
- `tests/unit/test_verify_hypothesis_cascade_py.py` (new)

Do NOT touch `mini_ork/verify/security_scan.py`, `gaming_scan.py`,
`deception_check.py`, `mutant_audit.py`, any other verifier, any doc, or the
Makefile.

## Verification commands

Run each and require exit 0:

1. `python3 -m pytest -q -p no:asyncio tests/unit/test_verify_hypothesis_cascade_py.py`
2. `python3 -m pytest -q -p no:asyncio tests/unit/test_verify_deception_py.py tests/unit/test_verify_security_oracle_py.py tests/unit/test_eco6_k14_edge_types.py tests/unit/test_verify_vacuous_verifier.py tests/unit/test_tool_grants_py.py`
3. `python3 -m ruff check --select F,E9 mini_ork/verify/hypothesis_cascade.py recipes/code-fix/verifiers/hypothesis.py tests/unit/test_verify_hypothesis_cascade_py.py`
4. `python3 -m mini_ork.verify.hypothesis_cascade --help`

## Self-application measurement

```measure
python3 -m pytest -q -p no:asyncio tests/unit/test_verify_hypothesis_cascade_py.py --junitxml="${MINI_ORK_RUN_DIR}/sec-g05t02-junit.xml"
```

## Required artifacts — declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/sec-g05t02-junit.xml`

## Done when

1. A hypothesis becomes a finding only when an executed probe confirms it; the
   same line with a passing probe is `cleared`, and with no probe is
   `abstained` — the pattern match alone never decides.
2. The uncertain band returns exit `3`, never `0`: an unconfirmed hypothesis is
   a review item, not a silent pass.
3. Ranking is deterministic and inspectable: the same input twice produces the
   same order and the same integer scores, and the oracle tier is capped.
4. A line excused by `# sec-allow:` proposes nothing, and a pre-existing sink
   outside the added lines is never proposed.
5. The node is vacuous — never a false refusal — when the change surface has no
   dangerous-API line, and abstains only when it cannot measure at all.
6. All four verification commands pass and every pre-existing test is unchanged.

## Rules

Edit files directly; do not emit unified diffs. Execute this spec exactly; do
not research and do not add lens or drafter nodes. When the spec and your
preference disagree, the spec wins.
