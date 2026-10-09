"""Structured test-result adapters for the delta-gate replay.

`replay_check` needs per-test pass/fail outcomes to prove a fail-to-pass
delta. pytest prints those on stdout under `-v`; jest and vitest do not —
the researcher repo runs jest through a bash wrapper that prints only the
`Test Suites: n passed` / `Tests: n passed` summary lines. Both jest and
vitest can write machine-readable results to a file regardless of reporter
configuration, and any gate script can write jest-JSON or JUnit XML to
`MINI_ORK_TEST_RESULTS_DIR`.

A repo with no pytest entrypoint still needs a verifiable fail-to-pass base.
The adapter therefore recognises the other structured runners too:

  - `go test -json` emits a newline-delimited JSON event stream on stdout
    (there is no output-file flag), and
  - `cargo test` can emit libtest's JSON event stream the same way
    (``--format=json``, nightly; the command must already request it).

Both are parsed back from the run log rather than from ``results_dir``.

These helpers do four things, all as pure functions with no subprocess and
no env mutation:

  - `detect_runners` — which of {pytest, jest, vitest, go, cargo} a command
    invokes (``go``/``cargo`` require the ``test`` subcommand, since both are
    build tools first);
  - `augment_for_results` — inject the flags that make a runner emit
    machine-readable output (pytest is left byte-for-byte untouched; jest/
    vitest write a results file, ``go test`` gets ``-json`` on stdout);
  - `parse_results_dir` — turn the results *files* back into (passed, failed)
    id sets, with ids relative to the side's cwd so base and candidate trees
    line up;
  - `parse_log_results` — the same for runners whose structured output is the
    run log itself (``go test -json``, cargo's JSON stream).

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

#: A suite/package that could not be loaded (collection or build error) is
#: reported with this id suffix. It is WEAK fail-to-pass evidence — a real
#: failing assertion is the strong evidence — and the oracle keys the
#: weak/strong split on this exact suffix, so every adapter must use it.
_SUITE_LOAD_FAILURE_SUFFIX = "::<suite load failure>"

#: A `go test` invocation: the `go` word followed by the `test` subcommand.
#: `go` is a build tool too, so a bare `go` word is NOT enough — `go build`
#: and `go vet` must not widen replay applicability into a cold base build.
_GO_TEST_RE = re.compile(
    r"(?<![A-Za-z0-9_.\-])"
    r"(?:[A-Za-z0-9_.\-]+/)*"
    r"go[ \t]+test(?![A-Za-z0-9_.\-/=])"
)

#: A command that already asks the runner for JSON output; the go augmentation
#: is then a no-op (re-adding `-json` would be harmless but the tests pin the
#: unchanged form).
_JSON_FLAG_RE = re.compile(r"(?:^|\s)-json(?:\s|$)")


def _toolchain_words(words: list[str]) -> set[str]:
    """Return the ``{go, cargo}`` **test** invocations among shell `words`.

    jest/vitest are test runners by name, but `go` and `cargo` are build
    tools first: ``go build`` / ``cargo build`` are NOT test runs and must not
    be mistaken for one (that would make an adapter-less build command
    "applicable" and trigger a cold base build). Require the ``test``
    subcommand word after the tool word, tolerating intervening flag words
    (``cargo +nightly test`` — the documented way to reach cargo's JSON).
    """
    out: set[str] = set()
    bases = [_basename(w) for w in words]
    for i, base in enumerate(bases):
        if base not in ("go", "cargo"):
            continue
        j = i + 1
        while j < len(bases) and bases[j].startswith(("-", "+")):
            j += 1
        if j < len(bases) and bases[j] == "test":
            out.add(base)
    return out


def detect_runners(cmd: str) -> set[str]:
    """Return the subset of ``{pytest, jest, vitest, go, cargo}`` in ``cmd``.

    A jest/vitest invocation is a shell word whose basename is ``jest``,
    ``jest.js``, or ``vitest`` — so ``npx jest`` and
    ``bash node_modules/.bin/jest`` count, but ``echo jest-is-a-word`` does
    not. pytest keeps its existing substring check. ``go``/``cargo`` count
    only as a ``go test`` / ``cargo test`` pair (see :func:`_toolchain_words`).
    """
    runners: set[str] = set()
    if "pytest" in cmd:
        runners.add("pytest")
    words = _shell_words(cmd)
    for word in words:
        base = _basename(word)
        if base in ("jest", "jest.js"):
            runners.add("jest")
        elif base == "vitest":
            runners.add("vitest")
    runners |= _toolchain_words(words)
    return runners


def _augment_go_test_json(cmd: str) -> tuple[str, bool]:
    """Append ``-json`` to a ``go test`` invocation so it prints its per-test
    event stream to stdout (``go test`` has no output-file flag, so the oracle
    parses the run log). A command that already carries ``-json`` is returned
    unchanged. Returns ``(cmd, has_go_test)``.
    """
    if not _GO_TEST_RE.search(cmd):
        return cmd, False
    if _JSON_FLAG_RE.search(cmd):
        return cmd, True
    return _GO_TEST_RE.sub(lambda m: m.group(0) + " -json", cmd), True


def augment_for_results(cmd: str, results_dir: str) -> tuple[str, set[str]]:
    """Insert the flags that make a runner emit machine-readable output.

    pytest commands are returned unchanged (the `-v` text path stays
    byte-for-byte). For jest, ``--json --outputFile=<results_dir>/jest-<n>.json``
    is inserted after each jest word; for vitest,
    ``--reporter=default --reporter=json --outputFile=<results_dir>/vitest-<n>.json``.
    ``<n>`` numbers each invocation in a ``&&`` chain so the per-invocation
    files stay distinct. For ``go test``, ``-json`` is appended (its stream
    goes to stdout, parsed from the log). cargo is never augmented — its
    ``--format=json`` is nightly-only, so the command must already ask for it.
    Returns ``(cmd, augmented_runners)``.
    """
    if "pytest" in cmd:
        return cmd, set()

    matches = list(_RUNNER_WORD_RE.finditer(cmd))
    if not matches:
        go_cmd, has_go = _augment_go_test_json(cmd)
        return go_cmd, ({"go"} if has_go else set())

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


def _escapes_cwd(rel: str) -> bool:
    return rel == ".." or rel.startswith(".." + os.sep)


def _rel_to_cwd(path: str, cwd: str) -> str:
    """Return `path` relative to `cwd` when absolute; unchanged otherwise.

    The same test file lives at a different absolute root on the base and
    candidate trees, so only the cwd-relative suffix is a stable id.

    The two roots are not always spelled the same way even when they are the
    same directory: ``jest``/``vitest`` report the *resolved* path of a suite
    (node realpaths modules as it loads them), while the caller passes the
    path it created. On macOS every ``mkdtemp`` under ``/var`` has a
    ``/private/var`` twin, so relativizing the two spellings as-written yields
    a junk ``../../../../..`` chain that can never match the candidate's id.
    When the literal relativization escapes the cwd, retry on the real paths;
    if that also escapes (a suite genuinely outside the tree), keep the
    literal answer so an outside-cwd suite is still reported as before.
    """
    if not os.path.isabs(path):
        return path
    try:
        rel = os.path.relpath(path, cwd)
    except ValueError:
        return path
    if not _escapes_cwd(rel):
        return rel
    try:
        rel_resolved = os.path.relpath(os.path.realpath(path), os.path.realpath(cwd))
    except ValueError:
        return rel
    return rel if _escapes_cwd(rel_resolved) else rel_resolved


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


def _json_lines(text: str):
    """Yield the parsed JSON objects on the ``{``-prefixed lines of `text`.

    Both ``go test -json`` and cargo's JSON reporter emit one JSON object per
    line (NDJSON) interleaved with plain build/test chatter; non-JSON and
    unparsable lines are skipped.
    """
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            yield obj


def _parse_go_json_log(text: str) -> tuple[set[str], set[str]] | None:
    """Parse a ``go test -json`` event stream into (passed, failed) id sets.

    Each ``{"Action": "pass"|"fail", "Package": p, "Test": t}`` event with a
    ``Test`` is one test outcome; the id is ``<pkg>::<Test>``. A package whose
    test binary could not build has NO per-test outcome: newer go emits an
    explicit ``{"Action": "build-fail", "ImportPath": p}`` event, older go
    only a package-level ``{"Action": "fail", "Package": p}``. Both map onto
    ``<pkg>::<suite load failure>`` — a load failure, i.e. WEAK evidence, not
    a real failing assertion. A package-level ``pass``/``fail`` for a package
    that DID produce per-test outcomes is just the summary and is ignored.
    Returns ``None`` when the log carries no outcome at all.
    """
    passed: set[str] = set()
    failed: set[str] = set()
    pkg_failed: set[str] = set()
    pkg_with_tests: set[str] = set()
    seen = False
    for ev in _json_lines(text):
        action = ev.get("Action")
        pkg = str(ev.get("Package") or "")
        if action == "build-fail":
            seen = True
            imp = str(ev.get("ImportPath") or pkg)
            failed.add(f"{imp}{_SUITE_LOAD_FAILURE_SUFFIX}" if imp
                       else _SUITE_LOAD_FAILURE_SUFFIX)
            continue
        if action not in ("pass", "fail"):
            continue
        test = str(ev.get("Test") or "")
        if not test:
            # Package-level summary; a fail here is only a load failure when
            # the package produced no per-test outcome (resolved below).
            if action == "fail" and pkg:
                pkg_failed.add(pkg)
            continue
        seen = True
        pkg_with_tests.add(pkg)
        tid = f"{pkg}::{test}" if pkg else test
        (passed if action == "pass" else failed).add(tid)
    for pkg in pkg_failed - pkg_with_tests:
        seen = True
        failed.add(f"{pkg}{_SUITE_LOAD_FAILURE_SUFFIX}")
    if not seen:
        return None
    return passed, failed


def _parse_cargo_json_log(text: str) -> tuple[set[str], set[str]] | None:
    """Parse cargo's libtest JSON stream into (passed, failed) id sets.

    libtest emits ``{"type": "test", "event": "ok"|"failed"|"started"|...,
    "name": <test path>}`` per test; ``ok``/``failed`` are the outcomes and
    the id is the test's full name. ``started``/``ignored`` events and the
    ``{"type": "suite", ...}`` records are ignored. Returns ``None`` when no
    ok/failed event appears (e.g. a build failure, which carries no test
    events and must fall through to the exit-code instrument).
    """
    passed: set[str] = set()
    failed: set[str] = set()
    seen = False
    for ev in _json_lines(text):
        if ev.get("type") != "test":
            continue
        event = ev.get("event")
        if event not in ("ok", "failed"):
            continue
        seen = True
        name = str(ev.get("name") or "")
        if not name:
            continue
        (passed if event == "ok" else failed).add(name)
    if not seen:
        return None
    return passed, failed


def parse_log_results(
    text: str, cwd: str, runner: str
) -> tuple[set[str], set[str]] | None:
    """Parse a runner's run log into (passed, failed) ids, or ``None``.

    The log-based twin of :func:`parse_results_dir` for the runners whose
    structured output IS stdout (``go test -json``; cargo's JSON stream): the
    oracle captures stdout to the run log, so these are parsed from it. Only
    ``go`` and ``cargo`` have a log adapter; any other runner (or a log with
    no recognisable events) yields ``None`` so the caller falls through to its
    exit-code instrument. `cwd` is accepted for signature symmetry with
    :func:`parse_results_dir` — these ids are package/suite names, already
    root-independent.
    """
    if not isinstance(text, str):
        return None
    if runner == "go":
        return _parse_go_json_log(text)
    if runner == "cargo":
        return _parse_cargo_json_log(text)
    return None


__all__ = [
    "detect_runners",
    "augment_for_results",
    "parse_results_dir",
    "parse_log_results",
]
