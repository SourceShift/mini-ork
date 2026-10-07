"""Structured test-result adapters for the delta-gate replay.

`replay_check` needs per-test pass/fail outcomes to prove a fail-to-pass
delta. pytest prints those on stdout under `-v`; jest and vitest do not —
the researcher repo runs jest through a bash wrapper that prints only the
`Test Suites: n passed` / `Tests: n passed` summary lines. Both jest and
vitest can write machine-readable results to a file regardless of reporter
configuration, and any gate script can write jest-JSON or JUnit XML to
`MINI_ORK_TEST_RESULTS_DIR`.

These helpers do three things, all as pure functions with no subprocess and
no env mutation:

  - `detect_runners` — which of {pytest, jest, vitest} a command invokes;
  - `augment_for_results` — inject the reporter flags that make jest/vitest
    write a results file (pytest is left byte-for-byte untouched);
  - `parse_results_dir` — turn those files back into (passed, failed) id
    sets, with ids relative to the side's cwd so base and candidate trees
    line up.

The results-dir lifecycle (create / run / parse / clean) lives in
`oracle.py`; this module only consumes the paths it is handed.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import xml.etree.ElementTree as ET


def _shell_words(cmd: str) -> list[str]:
    """Split `cmd` into shell words; fall back to whitespace on bad quoting."""
    try:
        return shlex.split(cmd)
    except ValueError:
        return cmd.split()


def _basename(word: str) -> str:
    return word.rsplit("/", 1)[-1]


# A jest/vitest word: an optional `dir/` prefix followed by the runner's
# basename. The trailing lookahead excludes word-continuation chars (`-`, `.`,
# `_`, `/`, `=`, alnum) so `jest-is-a-word`, `jest.config.js` and `jest=x` are
# not mistaken for the runner, and `node_modules/jest/bin/jest` matches only
# its final basename.
_RUNNER_WORD_RE = re.compile(
    r"(?<![A-Za-z0-9_.\-])"
    r"(?:[A-Za-z0-9_.\-]+/)*"
    r"(?P<runner>jest(?:\.js)?|vitest)"
    r"(?![A-Za-z0-9_.\-/=])"
)


def detect_runners(cmd: str) -> set[str]:
    """Return the subset of ``{pytest, jest, vitest}`` present in ``cmd``.

    A jest/vitest invocation is a shell word whose basename is ``jest``,
    ``jest.js``, or ``vitest`` — so ``npx jest`` and
    ``bash node_modules/.bin/jest`` count, but ``echo jest-is-a-word`` does
    not. pytest keeps its existing substring check.
    """
    runners: set[str] = set()
    if "pytest" in cmd:
        runners.add("pytest")
    for word in _shell_words(cmd):
        base = _basename(word)
        if base in ("jest", "jest.js"):
            runners.add("jest")
        elif base == "vitest":
            runners.add("vitest")
    return runners


def augment_for_results(cmd: str, results_dir: str) -> tuple[str, set[str]]:
    """Insert reporter flags that make jest/vitest write a results file.

    pytest commands are returned unchanged (the `-v` text path stays
    byte-for-byte). For jest, ``--json --outputFile=<results_dir>/jest-<n>.json``
    is inserted after each jest word; for vitest,
    ``--reporter=default --reporter=json --outputFile=<results_dir>/vitest-<n>.json``.
    ``<n>`` numbers each invocation in a ``&&`` chain so the per-invocation
    files stay distinct. Returns ``(cmd, augmented_runners)``.
    """
    if "pytest" in cmd:
        return cmd, set()

    matches = list(_RUNNER_WORD_RE.finditer(cmd))
    if not matches:
        return cmd, set()

    out: list[str] = []
    last = 0
    jest_n = 0
    vitest_n = 0
    augmented: set[str] = set()
    for m in matches:
        out.append(cmd[last:m.end()])
        if m.group("runner").startswith("jest"):
            jest_n += 1
            out.append(f" --json --outputFile={results_dir}/jest-{jest_n}.json")
            augmented.add("jest")
        else:
            vitest_n += 1
            out.append(
                f" --reporter=default --reporter=json "
                f"--outputFile={results_dir}/vitest-{vitest_n}.json"
            )
            augmented.add("vitest")
        last = m.end()
    out.append(cmd[last:])
    return "".join(out), augmented


def _rel_to_cwd(path: str, cwd: str) -> str:
    """Return `path` relative to `cwd` when absolute; unchanged otherwise.

    The same test file lives at a different absolute root on the base and
    candidate trees, so only the cwd-relative suffix is a stable id.
    """
    if os.path.isabs(path):
        try:
            return os.path.relpath(path, cwd)
        except ValueError:
            return path
    return path


def _parse_json_results(path: str, cwd: str) -> tuple[set[str], set[str]] | None:
    """Parse one jest/vitest JSON results file into (passed, failed) id sets."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("testResults"), list):
        return None

    passed: set[str] = set()
    failed: set[str] = set()
    for suite in data["testResults"]:
        if not isinstance(suite, dict):
            continue
        rel = _rel_to_cwd(str(suite.get("name") or ""), cwd)
        assertions = suite.get("assertionResults")
        if not isinstance(assertions, list) or not assertions:
            # A suite that failed to load has no assertion results; count it
            # as one failed id so a collection error still registers.
            if suite.get("status") == "failed":
                failed.add(f"{rel}::<suite load failure>")
            continue
        for a in assertions:
            if not isinstance(a, dict):
                continue
            status = a.get("status")
            full_name = a.get("fullName") or ""
            if status == "passed":
                passed.add(f"{rel}::{full_name}")
            elif status == "failed":
                failed.add(f"{rel}::{full_name}")
            # pending / skipped / todo are ignored
    return passed, failed


def _parse_junit(path: str) -> tuple[set[str], set[str]] | None:
    """Parse one JUnit XML results file into (passed, failed) id sets."""
    try:
        tree = ET.parse(path)
    except (OSError, ET.ParseError):
        return None

    passed: set[str] = set()
    failed: set[str] = set()
    for tc in tree.iter("testcase"):
        classname = tc.get("classname")
        name = tc.get("name")
        if not classname or not name:
            continue
        tid = f"{classname}::{name}"
        if tc.find("failure") is not None or tc.find("error") is not None:
            failed.add(tid)
        elif tc.find("skipped") is not None:
            continue
        else:
            passed.add(tid)
    return passed, failed


def parse_results_dir(results_dir: str, cwd: str) -> tuple[set[str], set[str]] | None:
    """Parse jest/vitest JSON and JUnit XML result files under `results_dir`.

    Returns ``(passed, failed)`` sets of test ids, or ``None`` when the
    directory holds no parsable file. jest/vitest ids are
    ``<file relative to cwd>::<fullName>``; JUnit ids are ``classname::name``.
    Both sides relativize to their own ``cwd`` so the same test yields the
    same id on the base and candidate trees.
    """
    if not results_dir or not os.path.isdir(results_dir):
        return None
    try:
        entries = sorted(os.listdir(results_dir))
    except OSError:
        return None
    json_files = [f for f in entries if f.endswith(".json")]
    xml_files = [f for f in entries if f.endswith(".xml")]
    if not json_files and not xml_files:
        return None

    passed: set[str] = set()
    failed: set[str] = set()
    parsed = False
    for name in json_files:
        res = _parse_json_results(os.path.join(results_dir, name), cwd)
        if res is not None:
            parsed = True
            p, f = res
            passed |= p
            failed |= f
    for name in xml_files:
        res = _parse_junit(os.path.join(results_dir, name))
        if res is not None:
            parsed = True
            p, f = res
            passed |= p
            failed |= f
    if not parsed:
        return None
    return passed, failed


__all__ = ["detect_runners", "augment_for_results", "parse_results_dir"]
