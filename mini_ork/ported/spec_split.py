"""Visible/Hidden Spec Split — Python port of lib/spec-split.sh.

Faithful port of the *deterministic* sub-pipelines of `mo_split_visible_hidden`
(the inline Python heredoc that extracts the hidden test subset) and the
verdict-writing half of `mo_run_hidden_suite`. The actual `npx playwright test`
call stays in bash — it requires a live worktree + Playwright runtime and is
out of scope for pytest. The Python port gives callers an in-process surface
and gives parity tests a stable target to byte-diff against the live bash.

Co-existence model (strangler-fig): bash `lib/spec-split.sh` is the
authoritative source. This module mirrors its sub-pipelines exactly. Parity
is enforced by `tests/unit/test_spec_split_py.py` (>=6 cases that invoke
`lib/spec-split.sh` via subprocess — stubbing `mo_run_dir` and `npx` — and
diff against the Python output byte-for-byte).

Pipeline map (bash function → Python):
  mo_split_visible_hidden:
    missing-spec early-bail (rc=1 + error JSON)        → split_visible_hidden raises FileNotFoundError
    Pass 1 collect imports                              → split_visible_hidden pass 1
    Pass 2 locate test.describe wrapper + header        → split_visible_hidden pass 2
    Pass 3 depth-counter walk + 3-line back-lookup      → split_visible_hidden pass 3
    AUTOGEN banner + imports + header + blocks + });    → split_visible_hidden
    spec-split-report.json {visible, hidden, ratio}     → split_visible_hidden
  mo_run_hidden_suite:
    decide_skip (no hidden / no ^test() → skipped=true)  → decide_skip_hidden_suite
    jq -n verdict write (skip shape + ran shape)        → write_verdict
"""
from __future__ import annotations

import json
import pathlib
import re

__all__ = [
    "split_visible_hidden",
    "decide_skip_hidden_suite",
    "write_verdict",
]


_HIDDEN_MARKER_RE = re.compile(r"^\s*//\s*@hidden")
_DESCRIBE_RE = re.compile(r"^\s*test\.describe\(")
_TEST_RE = re.compile(r"^\s*test\(")


def split_visible_hidden(visible_spec_path, iter_dir) -> dict:
    """Mirror lib/spec-split.sh::mo_split_visible_hidden heredoc byte-for-byte.

    Args:
        visible_spec_path: path to the e2e/<EPIC>_*.spec.ts the worker sees.
        iter_dir:          directory to write hidden_spec.ts + spec-split-report.json
                           into (the caller computes this from `mo_run_dir "$epic"/iter-$iter`
                           so the port stays decoupled from the dispatch runtime).

    Returns:
        Dict {visible, hidden, ratio} on success.
        For the no-describe early-bail, returns {visible: 0, hidden: 0, error: "no describe"}.
        Raises FileNotFoundError when visible_spec_path is missing (the bash function
        returns rc=1 in the same case; the port surfaces a Python exception so
        downstream code can `except FileNotFoundError` rather than parse rc codes).

    Writes:
        <iter_dir>/hidden_spec.ts — the hidden subset (or empty marker).
        <iter_dir>/spec-split-report.json — counts + ratio.

    Note: parity with the bash heredoc requires Python `re.match` (anchored at
    start of line, NOT `re.search`) and explicit `\n` joins — both are used here.
    """
    visible_spec_path = pathlib.Path(visible_spec_path)
    iter_dir = pathlib.Path(iter_dir)
    iter_dir.mkdir(parents=True, exist_ok=True)
    hidden_out = iter_dir / "hidden_spec.ts"
    report_out = iter_dir / "spec-split-report.json"

    if not visible_spec_path.is_file():
        # Bash: `jq -n '{visible: 0, hidden: 0, error: "visible spec missing"}' > report; return 1`
        # Hidden file is NOT written in this branch — bash returns before the heredoc.
        report_out.write_text(
            json.dumps({"visible": 0, "hidden": 0, "error": "visible spec missing"})
        )
        raise FileNotFoundError(f"visible spec missing: {visible_spec_path}")

    src = visible_spec_path.read_text()
    lines = src.split("\n")

    # Pass 1: collect imports (everything until first non-import / non-blank / non-//).
    imports: list[str] = []
    i = 0
    while i < len(lines):
        L = lines[i].rstrip()
        if L.startswith("import ") or L.startswith("//") or L == "":
            imports.append(L)
            i += 1
            continue
        break

    # Pass 2: locate the test.describe wrapper + beforeEach.
    header: list[str] = []
    j = i
    while j < len(lines) and not _DESCRIBE_RE.match(lines[j]):
        j += 1
    if j == len(lines):
        # No describe — bail with empty hidden.
        hidden_out.write_text("// no test.describe found — no hidden spec\n")
        report_out.write_text(json.dumps({"visible": 0, "hidden": 0, "error": "no describe"}))
        return {"visible": 0, "hidden": 0, "error": "no describe"}

    # Capture from describe to first test( occurrence — include beforeEach.
    k = j
    while k < len(lines) and not _TEST_RE.match(lines[k]):
        header.append(lines[k])
        k += 1

    # Pass 3: walk tests; pick out @hidden ones.
    hidden_blocks: list[str] = []
    visible_count = 0
    hidden_count = 0
    m = k

    def is_hidden(idx: int) -> bool:
        """Look up to 3 lines BACK from idx for a @hidden marker. Blank
        lines are transparent; the first non-blank line that isn't a
        @hidden marker ends the search."""
        for back in range(1, 4):
            if idx - back < 0:
                return False
            if _HIDDEN_MARKER_RE.match(lines[idx - back]):
                return True
            if lines[idx - back].strip() == "":
                continue
            return False
        return False

    while m < len(lines):
        if _TEST_RE.match(lines[m]):
            start = m
            depth = 0
            end = start
            for n in range(start, len(lines)):
                depth += lines[n].count("{") - lines[n].count("}")
                if depth == 0 and n > start:
                    end = n
                    break
            block = "\n".join(lines[start:end + 1])
            if is_hidden(start):
                hidden_blocks.append(block)
                hidden_count += 1
            else:
                visible_count += 1
            m = end + 1
        else:
            m += 1

    if hidden_count == 0:
        hidden_out.write_text("// no @hidden scenarios in source spec\n")
    else:
        out: list[str] = []
        out.append(
            "// AUTOGEN: hidden spec — DO NOT commit to worktree.\n"
            "// Worker MUST NOT see this file. Runs only at Phase 2 validation gate.\n"
        )
        out.extend(imports)
        out.append("")
        out.extend(header)
        out.extend(hidden_blocks)
        out.append("});\n")
        hidden_out.write_text("\n".join(out))

    total = visible_count + hidden_count
    ratio = (hidden_count / total) if total > 0 else 0
    report = {"visible": visible_count, "hidden": hidden_count, "ratio": ratio}
    report_out.write_text(json.dumps(report))
    return report


def decide_skip_hidden_suite(hidden_path) -> tuple[bool, str]:
    """Mirror lib/spec-split.sh::mo_run_hidden_suite early-bail.

    Returns (True, "no @hidden scenarios") when the hidden spec file is missing
    OR when `^test(` is not present (regex semantics — NOT grep — matching
    bash's `grep -q '^test('`). Otherwise returns (False, "").

    The caller is responsible for skipping the `npx playwright test` call and
    writing the skip-shape verdict via `write_verdict(..., skipped=True, ...)`.
    """
    hidden_path = pathlib.Path(hidden_path)
    if not hidden_path.is_file():
        return True, "no @hidden scenarios"
    try:
        content = hidden_path.read_text()
    except OSError:
        return True, "no @hidden scenarios"
    if not _TEST_RE.search(content):
        return True, "no @hidden scenarios"
    return False, ""


def write_verdict(
    verdict: str,
    rc: int,
    ran_at: str,
    verdict_path,
    skipped: bool = False,
    reason: str = "",
) -> None:
    """Mirror lib/spec-split.sh::mo_run_hidden_suite `jq -n` verdict writes.

    Two shapes, exactly matching the bash invocations:

    Skip path (lib/spec-split.sh line 152):
        jq -n '{verdict: "PASS", scenarios_run: 0, skipped: true,
                reason: "no @hidden scenarios"}' > verdict_path
        → {"verdict": "PASS", "scenarios_run": 0, "skipped": true, "reason": <reason>}

    Run path (lib/spec-split.sh lines 172-177):
        jq -n --arg verdict "$verdict" --argjson rc "$rc"
              --arg ran_at "$(date -u +%FT%TZ)" \
              '{verdict: $verdict, rc: $rc, ran_at: $ran_at, skipped: false}'
        → {"verdict": <verdict>, "rc": <rc>, "ran_at": <ran_at>, "skipped": false}

    `json.dumps` with default settings preserves dict insertion order (Python
    3.7+), so the key order matches jq's emission.
    """
    if skipped:
        payload = {"verdict": "PASS", "scenarios_run": 0, "skipped": True, "reason": reason}
    else:
        payload = {"verdict": verdict, "rc": rc, "ran_at": ran_at, "skipped": False}
    # jq -n default formatting = 2-space indent + trailing newline (the
    # trailing `\n` mirrors jq's stdout behavior under bash's `> file`).
    pathlib.Path(verdict_path).write_text(json.dumps(payload, indent=2) + "\n")