#!/usr/bin/env python3
# verifiers/spec-tests.py — run the blind spec tests authored from the kickoff.
#
# A framework-edit run passes today when the implementer's OWN tests pass, and
# those tests encode the implementer's interpretation of the kickoff rather than
# the kickoff itself (SDE-1 pass 1 invented its own failure taxonomy; SDE-3a
# pass 1 asserted a parked run was stranded). `spec_test_author` closes that gap
# by writing pytest files from the kickoff ALONE, before any implementation
# exists, on a different model lane than the implementer. This verifier is what
# makes those tests count: it runs them against the change and refuses to
# certify a suite the implementer rewrote.
#
# Inputs (via env):
#   MINI_ORK_RUN_DIR  — run directory set by the native execute runtime; the
#                       spec tests live in <run_dir>/spec-tests
#   MO_TARGET_CWD     — the run's TARGET repo/worktree (the repo under test);
#                       the pytest working directory AND the child's PYTHONPATH,
#                       so `import mini_ork...` resolves to the change under test
#                       (in-process and in any subprocess a spec test spawns).
#                       Falls back to MINI_ORK_ROOT, then cwd.
#   MINI_ORK_DB       — state.db (falls back to $MINI_ORK_HOME/state.db);
#   MINI_ORK_RUN_ID   — used to read the spec_test_author node_end from
#                       run_events, the timestamp the tamper check compares to
#   MO_SPEC_TESTS_TIMEOUT_S — pytest timeout (default 900; a present-but-malformed
#                       value fails closed, like kickoff-contract's measure knob)
#
# A spec test runs under a scrubbed env (`mini_ork.verify.test_env.
# scrubbed_test_env`, exactly as kickoff-contract.py does): provider credentials
# and mini-ork state pointers never reach the suite, so a spec test cannot
# charge the operator or corrupt its own result with a network call.
#
# The suite is also isolated from pytest config it did not author: the run dir
# lives under the operator's checkout (`<checkout>/.mini-ork/runs/<id>`), whose
# pyproject `pythonpath = ["."]` would otherwise put THAT checkout's `mini_ork`
# ahead of the target's and test the old code. `-c os.devnull --rootdir
# <spec dir> --confcutdir <spec dir>` drops ancestor ini files and conftests.
#
# Gating (spec §4): the tests are blind ONLY if nobody rewrote them. Any file in
# the spec dir (a `conftest.py` can rewrite reports as surely as a test can be
# edited) whose mtime is later than the author node's node_end `created_at`
# fails the run, naming the files — an implementer "fixing" a failing clause by
# editing the test must not pass. Deletion leaves no mtime, so the author's
# captured reply ends with a `Spec tests written:` manifest (written before its
# node_end, and fenced the same way): a listed file that is gone fails the run
# too, rather than shrinking the suite to "no spec tests authored".
#
# A test function without a `Kickoff:` docstring is deselected and reported,
# NEVER run (hard constraint c:0): the deselect targets come from a `pytest
# --collect-only` preflight (pytest's own nodeids, so an item the AST walk below
# missed is refused too) and the run is checked afterwards against the `-rA`
# summary, so an unmarked test that ran anyway fails the verifier instead of
# passing.
#
# Output: JSON to stdout — and the identical blob in `verifier_spec-tests.json`,
# `verifier-spec-tests.log` and `verifier-spec-tests.checks.tsv` in the run dir.
# Exit 0 on pass, 1 on fail. This verifier never writes `verdict.json`; the run's
# verdict belongs to the verifier nodes that own it.

from __future__ import annotations

import ast
import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime

# mini-ork's verifiers are standalone scripts. The executor exports PYTHONPATH,
# but walk up to the engine root anyway so a hand-run or a caller that forgets it
# still imports `mini_ork.verify.test_env` for the scrubbed child env.
_engine = os.path.dirname(os.path.realpath(__file__))
while not os.path.isdir(os.path.join(_engine, "mini_ork")) and _engine != os.path.dirname(_engine):
    _engine = os.path.dirname(_engine)
if _engine not in sys.path:
    sys.path.insert(0, _engine)

NAME = "spec-tests"
JSON_NAME = f"verifier_{NAME}.json"
EVIDENCE_NAME = f"verifier-{NAME}.log"
CHECKS_TSV_NAME = f"verifier-{NAME}.checks.tsv"

SPEC_DIR_NAME = "spec-tests"
AUTHOR_NODE = "spec_test_author"
DOCSTRING_MARKER = "Kickoff:"
DEFAULT_TIMEOUT_S = 900

# pytest's own default discovery prefix, and the name the kickoff tells the
# author to use (`test_spec_*.py`). A file that is not collected by the plain
# `pytest <dir>` invocation below is not a spec test.
_SPEC_FILE_RE = re.compile(r"^test_.*\.py$")

# The `-rA` short summary prints one line per executed item; the nodeid is the
# second token. Used to prove no deselected item was executed (see
# `unselected_executed`). SKIPPED is absent on purpose: `-rA` folds skips into
# `SKIPPED [n] file.py:line: reason`, which carries no nodeid to check.
_ITEM_LINE_RE = re.compile(r"^(PASSED|FAILED|ERROR|XFAIL|XPASS)\s+(\S+)")
_DESELECTED_RE = re.compile(r"(\d+)\s+deselected")
# pytest's FAILURES section: a `___ test_name ___` header followed by the
# traceback, whose exception line carries the `E` marker at column 0.
_TRACEBACK_HEADER_RE = re.compile(r"^_+\s*(.+?)\s*_+$")
_EXCEPTION_LINE_RE = re.compile(r"^E\s{2,}(\S.*)$")

# Written by pytest itself, not by the author: running the suite (as the
# implementer is told to) creates these, so they are not part of the fence.
_CACHE_DIRS = frozenset({"__pycache__", ".pytest_cache"})

# The author's reply ends with `Spec tests written: <files>` (or `none`). The
# capture stops at a newline, a quote or a backslash, so a manifest embedded in a
# JSON string (`"...\nSpec tests written: a.py\n..."`) does not run on into the
# rest of the document.
_MANIFEST_RE = re.compile(r"Spec tests written:[*_`\s]*([^\n\"\\]*)", re.IGNORECASE)
_MANIFEST_NAME_RE = re.compile(r"test_[A-Za-z0-9_]*\.py")


# ── run-dir layout ──────────────────────────────────────────────────────────

def spec_dir(run_dir: str) -> str:
    return os.path.join(run_dir, SPEC_DIR_NAME)


def spec_files(spec_dir_path: str) -> list[str]:
    """The authored spec-test file names, sorted (empty when none exist)."""
    if not os.path.isdir(spec_dir_path):
        return []
    return sorted(f for f in os.listdir(spec_dir_path)
                  if _SPEC_FILE_RE.match(f) and os.path.isfile(os.path.join(spec_dir_path, f)))


def watched_files(spec_dir_path: str) -> list[str]:
    """Every file under the spec dir, relative and sorted — the tamper fence.

    Not just ``test_*.py``: a ``conftest.py`` hook or a helper module changes the
    suite's verdict as surely as an edited test does. pytest's own caches are the
    only exclusion."""
    out: list[str] = []
    for root, dirs, names in os.walk(spec_dir_path):
        dirs[:] = sorted(d for d in dirs if d not in _CACHE_DIRS)
        for name in names:
            out.append(os.path.relpath(os.path.join(root, name), spec_dir_path))
    return sorted(out)


def author_outputs(run_dir: str) -> list[str]:
    """The author node's captured reply, as the researcher handler wrote it.

    A non-``_lens`` researcher writes to ``context-<node>.json``; when the agent
    wrote that file itself, the reply lands in ``<file>.stdout.md`` instead
    (``write_preserving_agent``). Both are written before the node_end."""
    base = os.path.join(run_dir, f"context-{AUTHOR_NODE}.json")
    return [p for p in (base, base + ".stdout.md") if os.path.isfile(p)]


def author_manifest(outputs: list[str]) -> list[str] | None:
    """The spec-test file names the author reported writing, or None when its
    reply carries no ``Spec tests written:`` line (the last one wins)."""
    text = ""
    for path in outputs:
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                text += fh.read() + "\n"
        except OSError:
            continue
    lines = _MANIFEST_RE.findall(text)
    if not lines:
        return None
    return list(dict.fromkeys(_MANIFEST_NAME_RE.findall(lines[-1])))


# ── when did the author node end? ───────────────────────────────────────────

def _db_path() -> str:
    """The state.db the run's events live in: ``MINI_ORK_DB`` wins, else
    ``$MINI_ORK_HOME/state.db`` (the same resolution as mini_ork/context.py)."""
    db = (os.environ.get("MINI_ORK_DB") or "").strip()
    if db:
        return db
    home = (os.environ.get("MINI_ORK_HOME") or "").strip()
    if home:
        return os.path.join(home, "state.db")
    return ""


def _as_epoch(value) -> float | None:
    """``created_at`` as epoch seconds, or None when it cannot be read as one.

    ``run_events.created_at`` is written as integer epoch seconds
    (``mini_ork/observability/node_events.py``), so that is the expected form; a
    numeric string or an ISO-8601 text timestamp is accepted too, because the
    check must not silently degrade to "no timestamp" when a store writes it
    differently. Anything else returns None and the caller fails closed.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


def author_node_end(run_dir: str) -> tuple[float | None, str]:
    """``(epoch_seconds, error)`` for the ``spec_test_author`` node_end event.

    The LAST node_end of that node wins, so a revise round that re-ran the
    author (and rewrote the tests legitimately) moves the fence forward instead
    of failing every later run. Every failure to establish the timestamp is an
    error — never a silent skip, because a skip would certify an unchecked suite
    as untampered."""
    run_id = (os.environ.get("MINI_ORK_RUN_ID") or "").strip()
    if not run_id:
        return None, "MINI_ORK_RUN_ID is not set"
    db = _db_path()
    if not db:
        return None, "MINI_ORK_DB/MINI_ORK_HOME is not set"
    if not os.path.isfile(db):
        return None, f"state db not found: {db}"
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            rows = con.execute(
                "SELECT created_at, payload_json FROM run_events "
                "WHERE run_id = ? AND event_type = 'node_end'",
                (run_id,)).fetchall()
        finally:
            con.close()
    except sqlite3.Error as exc:
        return None, f"run_events unreadable: {exc}"
    best: float | None = None
    for created_at, payload in rows:
        try:
            parsed = json.loads(payload or "{}")
        except ValueError:
            continue
        if not isinstance(parsed, dict) or parsed.get("node_id") != AUTHOR_NODE:
            continue
        ts = _as_epoch(created_at)
        if ts is not None and (best is None or ts > best):
            best = ts
    if best is None:
        return None, f"no {AUTHOR_NODE} node_end in run_events for run {run_id}"
    return best, ""


def tampered_files(paths: dict[str, str], author_end: float) -> list[str]:
    """The names (``{name: path}`` keys) of files written after the author node
    ended.

    Both sides are compared at the one-second resolution ``created_at`` is
    recorded at (``int``), so a file written in the same second the node ended
    is not read as a rewrite just because its mtime carries a fractional part
    (the executor reads a node_end as covering its whole second, too)."""
    fence = int(author_end)
    late: list[str] = []
    for name, path in paths.items():
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            late.append(name)  # unreadable mtime: refuse to certify it
            continue
        if int(mtime) > fence:
            late.append(name)
    return late


# ── Kickoff: docstring gating ───────────────────────────────────────────────

def has_kickoff_docstring(doc: str | None) -> bool:
    """True iff the docstring starts with ``Kickoff:`` (leading whitespace and a
    blank first line are tolerated; the marker must be the first text)."""
    return bool(doc) and doc.lstrip().startswith(DOCSTRING_MARKER)


def _test_units(path: str) -> list[tuple[str, str | None]]:
    """``[(nodeid_suffix, docstring)]`` for the test items pytest will collect.

    Mirrors pytest's module-level collection: ``test*`` functions at module
    level, and ``test*`` methods of ``Test*`` classes (nodeid
    ``TestFoo::test_bar``). Nested definitions are not collected by pytest, so
    they are not collected here either. A file that does not PARSE raises
    ``SyntaxError`` — the caller reports it rather than treating the file as
    empty (an empty read would silently deselect the whole suite)."""
    with open(path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename=path)
    units: list[tuple[str, str | None]] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
            units.append((node.name, ast.get_docstring(node, clean=False)))
        elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            for child in node.body:
                if (isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                        and child.name.startswith("test")):
                    units.append((f"{node.name}::{child.name}",
                                  ast.get_docstring(child, clean=False)))
    return units


def classify(files: list[str], spec_dir_path: str) -> tuple[list[str], list[str], list[str]]:
    """``(selected_nodeids, deselected_nodeids, files_that_do_not_parse)``.

    A nodeid is ``<filename>::<name>`` — the form pytest reports and the form
    ``--deselect`` accepts — NOT an absolute path, so it matches whether pytest
    is invoked with the absolute spec dir or a relative one."""
    selected: list[str] = []
    deselected: list[str] = []
    unparsable: list[str] = []
    for name in files:
        try:
            units = _test_units(os.path.join(spec_dir_path, name))
        except (OSError, SyntaxError, ValueError):
            unparsable.append(name)
            continue
        for unit, doc in units:
            nodeid = f"{name}::{unit}"
            (selected if has_kickoff_docstring(doc) else deselected).append(nodeid)
    return selected, deselected, unparsable


def kickoff_quote(spec_dir_path: str, nodeid: str) -> str:
    """The ``Kickoff:`` clause the failing test carried (first line, collapsed).

    This is the evidence the implementer's revise round reads: it names the
    kickoff clause that failed, not the implementer's own restatement of it."""
    filename, _, unit = nodeid.partition("::")
    try:
        units = _test_units(os.path.join(spec_dir_path, os.path.basename(filename)))
    except (OSError, SyntaxError, ValueError):
        return ""
    for name, doc in units:
        if name == unit and has_kickoff_docstring(doc):
            for line in (doc or "").splitlines():
                if line.strip():
                    return " ".join(line.split())
    return ""


# ── running the suite ───────────────────────────────────────────────────────

def spec_cwd() -> str:
    """The pytest working directory: the TARGET tree, never the engine.

    ``python -m pytest`` inserts the working directory at ``sys.path[0]``, so
    this is also what makes the kickoff-named modules importable."""
    for key in ("MO_TARGET_CWD", "MINI_ORK_TARGET_REPO"):
        val = os.environ.get(key)
        if val and os.path.isdir(val):
            return val
    root = os.environ.get("MINI_ORK_ROOT")
    if root and os.path.isdir(root):
        return root
    return os.getcwd()


def spec_timeout() -> tuple[int | None, str]:
    """``(timeout_s, error)``. Unset/empty takes the 900 s default; a value that
    is PRESENT but not a positive integer fails closed — a malformed knob is a
    clear error, never a silent fallback (CLAUDE.md's fail-closed rule)."""
    raw = str(os.environ.get("MO_SPEC_TESTS_TIMEOUT_S", "")).strip()
    if not raw:
        return DEFAULT_TIMEOUT_S, ""
    try:
        value = int(raw)
    except ValueError:
        return None, f"MO_SPEC_TESTS_TIMEOUT_S is not an integer: {raw!r}"
    if value <= 0:
        return None, f"MO_SPEC_TESTS_TIMEOUT_S must be a positive integer: {raw!r}"
    return value, ""


# test.py:33-43's inline fallback, for a verifier copied into a fixture without
# the rest of `mini_ork` on PYTHONPATH — kept byte-compatible with the module it
# stands in for so a spec test never sees a different env depending on the import.
_SCRUB_NAMES = frozenset({
    "MINI_ORK_SECRETS", "MINI_ORK_DB", "MINI_ORK_HOME", "MINI_ORK_PROJECT_HOME",
    "MINI_ORK_RUN_ID", "MINI_ORK_RUN_DIR", "MINI_ORK_PLAN_PATH", "MINI_ORK_AGENTS",
    "MO_TARGET_CWD",
})


def _fallback_scrubbed_env() -> dict:
    env = dict(os.environ)
    for key in list(env):
        if (key in _SCRUB_NAMES
                or key.endswith(("_API_KEY", "_AUTH_TOKEN", "_ACCESS_TOKEN", "_SECRET", "_SECRET_KEY"))
                or key.startswith("ANTHROPIC_") or key in {"OPENAI_API_BASE", "OPENAI_BASE_URL"}):
            env.pop(key)
    return env


def spec_env() -> dict:
    """The env the spec suite runs under: provider credentials and mini-ork
    state pointers scrubbed OUT, and ``PYTHONPATH`` set to the target tree alone
    (spec §4, and ``test.py``'s ``PYTHONPATH="."`` beside ``cwd=<target>``) — so
    a subprocess a spec test spawns imports the change under test too, never an
    engine root inherited from the executor."""
    try:
        from mini_ork.verify.test_env import scrubbed_test_env
        env = dict(scrubbed_test_env())
    except Exception:  # noqa: BLE001 — verifier copied without mini_ork
        env = _fallback_scrubbed_env()
    env["PYTHONPATH"] = spec_cwd()
    return env


def _pytest_argv(spec_dir_path: str, *extra: str) -> list[str]:
    """``python -m pytest`` over the spec dir, isolated from config it did not
    author: ``-c os.devnull`` drops every ancestor ini/pyproject (the operator
    checkout above the run dir sets ``pythonpath = ["."]``), and ``--rootdir`` /
    ``--confcutdir`` pin nodeids and conftest discovery to the spec dir."""
    return [sys.executable, "-m", "pytest", "-q", spec_dir_path, "-p", "no:cacheprovider",
            "-c", os.devnull, "--rootdir", spec_dir_path, "--confcutdir", spec_dir_path,
            *extra]


def _kill_group(proc) -> None:
    """SIGKILL pytest's whole process group: a test that forks a child would
    otherwise outlive the timeout and keep the run alive."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except OSError:
        pass
    try:
        proc.wait(timeout=10)
    except Exception:  # noqa: BLE001 — reap best-effort; the group is already killed
        pass


def _run_pytest(argv: list[str], timeout: int) -> tuple[int | None, bool, str]:
    """``(rc, timed_out, output_text)`` for one pytest invocation.

    stdout goes to a FILE, never a PIPE: a grandchild that outlives the timeout
    cannot hold a pipe open and hang the reap the way ``communicate()`` would."""
    with tempfile.TemporaryFile() as out:
        try:
            proc = subprocess.Popen(argv, cwd=spec_cwd(), env=spec_env(),
                                    stdout=out, stderr=subprocess.STDOUT,
                                    start_new_session=True)
        except OSError as exc:
            return None, False, f"could not start pytest: {exc}"
        try:
            rc = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_group(proc)
            out.seek(0)
            return None, True, out.read().decode("utf-8", "replace")
        out.seek(0)
        return rc, False, out.read().decode("utf-8", "replace")


def collect_nodeids(spec_dir_path: str, timeout: int) -> tuple[list[str], bool]:
    """``(nodeids, timed_out)``: the items pytest itself collects from the spec dir.

    Collection imports the spec modules, so it gets the same timeout and
    process-group kill as the run. Deselecting by these ids (not only by the AST
    walk's) refuses an item the walk missed — a test in a subdirectory, a nested
    ``Test`` class — instead of letting it run."""
    rc, timed_out, text = _run_pytest(_pytest_argv(spec_dir_path, "--collect-only"), timeout)
    items: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if "::" in line and not line.startswith(("=", "_", "!", "E ")):
            items.append(line)
    return items, timed_out


def deselect_targets(collected: list[str], selected: list[str],
                     deselected: list[str]) -> tuple[list[str], list[str]]:
    """``(exact_deselect_nodeids, unclassified_nodeids)``.

    Every collected item the gate did not SELECT is deselected by the exact
    nodeid pytest reported. An item whose function the docstring walk could not
    classify is refused too — a test the gate cannot judge must not run — and
    returned separately so the evidence names it."""
    keep = {_base_nodeid(nodeid) for nodeid in selected}
    known_deselected = {_base_nodeid(nodeid) for nodeid in deselected}
    exact: list[str] = []
    unclassified: list[str] = []
    for nodeid in collected:
        base = _base_nodeid(nodeid)
        if base in keep:
            continue
        exact.append(nodeid)
        if base not in known_deselected:
            unclassified.append(nodeid)
    return exact, unclassified


def shadowed_selections(selected: list[str], targets: list[str]) -> list[str]:
    """Selected tests a deselect prefix would ALSO match.

    pytest deselects with ``nodeid.startswith(prefix)``, so deselecting an
    unmarked ``test_x`` also silences a marked ``test_x_extra``. That is a silent
    skip of a test the run claims to have run, so it is named and failed."""
    out: list[str] = []
    for nodeid in selected:
        base = _base_nodeid(nodeid)
        for target in targets:
            target_base = _base_nodeid(target)
            if base != target_base and base.startswith(target_base):
                out.append(f"{base} shadowed by deselect {target_base}")
                break
    return out


def run_spec_tests(spec_dir_path: str, deselected: list[str],
                   timeout: int, log) -> tuple[int | None, bool, str]:
    """Run pytest over the spec dir. ``(rc, timed_out, output_text)``.

    ``--deselect`` keeps a docstring-less test from EVER running (hard
    constraint c:0); ``-rA`` is what lets ``evaluate`` prove it afterwards.
    ``--continue-on-collection-errors``: one spec module that fails to import
    (the kickoff-named module is missing) must not hide every other module's
    verdict from the revise round."""
    extra = ["-rA", "--continue-on-collection-errors"]
    for nodeid in deselected:
        extra += ["--deselect", nodeid]
    rc, timed_out, text = _run_pytest(_pytest_argv(spec_dir_path, *extra), timeout)
    log.write(text if text.endswith("\n") else text + "\n")
    log.flush()
    return rc, timed_out, text


def _base_nodeid(token: str) -> str:
    """``a/b.py::test_x[1-2]`` → ``test_x``-form nodeid ``b.py::test_x``.

    Both parametrization and any rootdir-relative path prefix pytest may add are
    stripped, so a reported item compares equal to the nodeid this verifier
    selected regardless of how pytest rendered it."""
    token = token.split("[", 1)[0]
    file_part, sep, unit = token.partition("::")
    base = os.path.basename(file_part)
    return f"{base}{sep}{unit}" if sep else base


def summary_entries(output: str) -> list[tuple[str, str]]:
    """``[(outcome, base_nodeid)]`` from the ``-rA`` short summary. A module
    that failed to import reports as ``ERROR <file>`` — no ``::``."""
    entries: list[tuple[str, str]] = []
    for line in output.splitlines():
        match = _ITEM_LINE_RE.match(line.strip())
        if match:
            entries.append((match.group(1), _base_nodeid(match.group(2))))
    return entries


def executed_items(output: str) -> list[str]:
    """The nodeids of every item pytest executed (from the ``-rA`` summary).
    A collection error is not an executed item, so it is not listed here."""
    return [nodeid for _, nodeid in summary_entries(output) if "::" in nodeid]


def failed_items(output: str, selected: list[str]) -> list[str]:
    """The selected nodeids that failed. A module-level collection error fails
    every selected test of that module: each is reported with its own clause,
    rather than a bare file name with no ``Kickoff:`` quote attached."""
    out: list[str] = []
    for outcome, nodeid in summary_entries(output):
        if outcome not in ("FAILED", "ERROR"):
            continue
        if "::" in nodeid:
            out.append(nodeid)
            continue
        in_module = [s for s in selected if s.partition("::")[0] == nodeid]
        out.extend(in_module or [nodeid])
    return list(dict.fromkeys(out))


def _unit_key(token: str) -> str:
    """The bare unit name: ``a/b.py::test_x[1-2]`` → ``test_x``."""
    return _base_nodeid(token).rpartition("::")[2].split("[", 1)[0]


def failure_exceptions(output: str) -> dict[str, str]:
    """``{unit name: "ExcType: message"}`` from pytest's FAILURES section.

    The short summary reports ``FAILED <nodeid>`` with no reason, so the only
    place the cause appears is the first ``E`` line under the item's
    ``___ test_name ___`` header. That line is what tells the operator whether
    the clause was refuted or the test died before reaching it."""
    found: dict[str, str] = {}
    current = ""
    for line in output.splitlines():
        header = _TRACEBACK_HEADER_RE.match(line.strip())
        if header:
            name = header.group(1)
            # pytest also separates traceback frames with `_ _ _ _`; that line
            # matches the header shape but names nothing.
            if any(ch.isalnum() for ch in name):
                current = _unit_key(name)
            continue
        exception = _EXCEPTION_LINE_RE.match(line)
        if exception and current:
            found.setdefault(current, " ".join(exception.group(1).split())[:200])
    return found


def unselected_executed(output: str, selected: list[str]) -> list[str]:
    """Executed items that were NOT selected — the c:0 violation, if any.

    The deselect list is derived from an AST walk; if that walk misses how
    pytest really collects a file, a docstring-less test would run anyway. This
    is the check that turns that silent violation into a loud failure."""
    allowed = {_base_nodeid(nodeid) for nodeid in selected}
    return sorted({item for item in executed_items(output) if item not in allowed})


def deselected_count(output: str) -> int:
    for line in output.splitlines():
        match = _DESELECTED_RE.search(line)
        if match:
            return int(match.group(1))
    return 0


# ── evaluation ──────────────────────────────────────────────────────────────

def evaluate(run_dir: str, log) -> dict:
    """Run the spec-tests gate for one run dir. Writes only to ``log``."""
    path = spec_dir(run_dir)
    files = spec_files(path)
    result: dict = {
        "spec_dir": path,
        "files": files,
        "selected": [],
        "deselected": [],
        "tampered": [],
        "failures": [],
        "author_node_end": None,
        "deselected_reported": 0,
        "rc": None,
        "timed_out": False,
        "timeout_s": None,
        "note": None,
        "error": None,
        "checks": [],
        "pass": False,
    }

    # Deletion first: it needs no timestamp, only the author's own record, and a
    # suite emptied by deletion must not reach the "no spec tests" pass below.
    outputs = author_outputs(run_dir)
    manifest = author_manifest(outputs)
    present = {os.path.basename(name) for name in watched_files(path)}
    missing = [name for name in (manifest or []) if name not in present]
    result["manifest"] = manifest
    result["missing"] = missing
    if missing:
        result["error"] = ("spec tests deleted after authoring: " + ", ".join(missing)
                           + f" (listed in the {AUTHOR_NODE} reply's `Spec tests written:`"
                           " manifest, absent from spec-tests/)")
        result["checks"] = [_fail(
            "tamper-detection",
            "every spec test the author reported writing is still present",
            result["error"])]
        return result

    if not files:
        # Nothing to run. The one thing left to rule out is a reply rewritten to
        # drop its manifest — checkable only when the node_end is on record.
        late: list[str] = []
        if outputs:
            author_end, err = author_node_end(run_dir)
            if not err:
                late = tampered_files({os.path.basename(p): p for p in outputs}, author_end)
        if late:
            result["tampered"] = late
            result["error"] = ("spec test author's reply modified after authoring: "
                               + ", ".join(late))
            result["checks"] = [_fail(
                "tamper-detection",
                "the author's reply was not modified after the author node ended",
                result["error"])]
            return result
        result["note"] = "no spec tests authored"
        result["pass"] = True
        result["checks"] = [_note_check(result["note"])]
        return result

    author_end, err = author_node_end(run_dir)
    if err:
        result["error"] = f"cannot establish {AUTHOR_NODE} node_end: {err}"
        result["checks"] = [_fail(
            "tamper-detection",
            f"the {AUTHOR_NODE} node_end timestamp is readable from run_events",
            result["error"])]
        return result
    result["author_node_end"] = author_end
    fenced = {name: os.path.join(path, name) for name in watched_files(path)}
    fenced.update({os.path.basename(p): p for p in outputs})
    late = tampered_files(fenced, author_end)
    result["tampered"] = late
    if late:
        result["error"] = ("spec tests modified after authoring: " + ", ".join(late)
                           + f" (mtime later than {AUTHOR_NODE} node_end {int(author_end)})")
        result["checks"] = [_fail(
            "tamper-detection",
            "no spec test file was modified after the author node ended",
            result["error"])]
        return result

    selected, deselected, unparsable = classify(files, path)
    result["selected"] = selected
    result["deselected"] = deselected
    if unparsable:
        result["error"] = "spec test file(s) do not parse: " + ", ".join(unparsable)
        result["checks"] = [_fail("spec-tests-parse", "every spec test file parses",
                                  result["error"])]
        return result

    if not selected:
        result["error"] = ("no spec test declares a `Kickoff:` clause "
                           f"({len(deselected)} deselected): nothing to verify")
        result["checks"] = [_fail(
            "kickoff-docstrings",
            "at least one spec test function quotes the kickoff clause it checks",
            result["error"])]
        return result

    timeout, terr = spec_timeout()
    if terr:
        result["error"] = terr
        result["checks"] = [_fail("spec-tests-run", "the spec suite runs under a valid timeout",
                                  terr)]
        return result
    result["timeout_s"] = timeout

    # Deselect by the nodeids pytest itself reports, not only the walk's: an item
    # the walk missed is refused instead of run. The walked nodeids ride along as
    # extra prefixes (an unmatched prefix is inert), so a preflight that yields
    # nothing still deselects every unmarked test the walk found.
    collected, collect_timed_out = collect_nodeids(path, timeout)
    if collect_timed_out:
        result["timed_out"] = True
        result["error"] = f"spec suite collection timed out after {timeout}s"
        result["checks"] = [_fail("spec-tests-run", f"the spec suite finishes within {timeout}s",
                                  result["error"])]
        return result
    exact_deselect, unclassified = deselect_targets(collected, selected, deselected)
    result["deselected_unclassified"] = unclassified
    targets = list(dict.fromkeys(exact_deselect + deselected))

    # `--deselect` is a PREFIX match, so a deselected `test_x` also matches a
    # selected `test_x_extra`: that would silently skip a marked test. Refuse
    # instead of certifying a suite that quietly ran less than it reported.
    shadowed = shadowed_selections(selected, targets)
    if shadowed:
        result["error"] = ("a deselected spec test name is a prefix of a selected one: "
                           + "; ".join(shadowed))
        result["checks"] = [_fail(
            "deselect-precision",
            "deselecting an unmarked spec test does not also match a marked one",
            result["error"])]
        return result

    rc, timed_out, output = run_spec_tests(path, targets, timeout, log)
    result["rc"] = rc
    result["timed_out"] = timed_out
    result["deselected_reported"] = deselected_count(output)

    if timed_out:
        result["error"] = f"spec suite timed out after {timeout}s"
        result["checks"] = [_fail("spec-tests-run", f"the spec suite finishes within {timeout}s",
                                  result["error"])]
        return result

    offenders = unselected_executed(output, selected)
    if offenders:
        result["error"] = ("a spec test without a `Kickoff:` docstring was executed: "
                           + ", ".join(offenders))
        result["checks"] = [_fail(
            "kickoff-docstrings",
            "spec tests without a Kickoff: docstring are never executed",
            result["error"])]
        return result

    exceptions = failure_exceptions(output)
    for nodeid in failed_items(output, selected):
        failure = {"test": nodeid, "kickoff": kickoff_quote(path, nodeid)}
        error = exceptions.get(_unit_key(nodeid))
        if error:
            failure["error"] = error
        result["failures"].append(failure)

    checks = [
        {"name": "tamper-detection",
         "expected": "no spec test file was modified after the author node ended",
         "actual": (f"all {len(fenced)} file(s) predate {AUTHOR_NODE} node_end "
                    f"{int(author_end)}; {_manifest_actual(manifest)}"),
         "pass": True, "gating": True},
        {"name": "kickoff-docstrings",
         "expected": "every executed spec test declares a Kickoff: clause",
         "actual": (f"{len(selected)} selected, {len(deselected)} deselected"
                    + (" (" + ", ".join(deselected) + ")" if deselected else "")
                    + (f"; {len(unclassified)} collected item(s) the walk could not "
                       "classify were refused: " + ", ".join(unclassified)
                       if unclassified else "")),
         "pass": True, "gating": True},
        {"name": "spec-tests-pass",
         "expected": "pytest exits 0 over the selected spec tests",
         "actual": _pass_actual(rc, result["failures"]),
         "pass": rc == 0, "gating": True},
    ]
    result["checks"] = checks
    result["pass"] = all(c["pass"] for c in checks if c["gating"])
    return result


def _manifest_actual(manifest: list[str] | None) -> str:
    if manifest is None:
        return ("the author's reply carries no `Spec tests written:` manifest, "
                "so a deleted spec test is not detectable")
    return f"all {len(manifest)} file(s) in the author's manifest are present"


def _failure_clause(failure: dict) -> str:
    """One failing test as the hint's detail and `NEEDS-CHANGE.md` will read it.

    The exception comes before the kickoff quote because the detail is
    truncated downstream, and a test that died before asserting says nothing
    about the change the operator is being asked to revise."""
    parts = [failure["test"]]
    if failure.get("error"):
        parts.append(f"[{failure['error']}]")
    if failure["kickoff"]:
        parts.append(f"← {failure['kickoff']}")
    return " ".join(parts)


def _pass_actual(rc, failures: list[dict]) -> str:
    if rc == 0:
        return "rc=0"
    detail = "; ".join(_failure_clause(f) for f in failures)
    return f"rc={rc}" + (f"; failing: {detail}" if detail else "")


def _fail(name: str, expected: str, actual: str) -> dict:
    return {"name": name, "expected": expected, "actual": actual,
            "pass": False, "gating": True}


def _note_check(note: str) -> dict:
    return {"name": "spec-tests-authored",
            "expected": "the run has spec tests to verify",
            "actual": note, "pass": True, "gating": False}


# ── entry point ─────────────────────────────────────────────────────────────

def _normal(run_dir: str) -> int:
    evidence = os.path.join(run_dir, EVIDENCE_NAME)
    json_path = os.path.join(run_dir, JSON_NAME)
    tsv_path = os.path.join(run_dir, CHECKS_TSV_NAME)
    with open(evidence, "w", encoding="utf-8") as log:
        log.write(f"# {NAME} evidence — run {run_dir}\n")
        result = evaluate(run_dir, log)
        log.write(f"spec dir: {result['spec_dir']}\n")
        log.write(f"files: {', '.join(result['files']) or '(none)'}\n")
        log.write(f"selected: {', '.join(result['selected']) or '(none)'}\n")
        log.write(f"deselected: {', '.join(result['deselected']) or '(none)'}\n")
        manifest = result["manifest"]
        log.write("author manifest: " + ("(absent)" if manifest is None
                                          else ", ".join(manifest) or "none") + "\n")
        if result["note"]:
            log.write(f"note: {result['note']}\n")
        if result["error"]:
            log.write(f"error: {result['error']}\n")
        for failure in result["failures"]:
            log.write(f"failure: {failure['test']}\n")
            if failure["kickoff"]:
                log.write(f"  kickoff: {failure['kickoff']}\n")
        log.write(f"pass: {bool(result['pass'])}\n")

    checks = result["checks"]
    failed = [c["name"] for c in checks if not c["pass"] and c["gating"]]
    reasons = [f"{c['name']} failed: {c['actual']}" for c in checks
               if not c["pass"] and c["gating"]]
    with open(tsv_path, "w", encoding="utf-8") as tsv:
        for check in checks:
            tsv.write(f"{check['name']}\t{check['actual']}\t"
                      f"{'true' if check['pass'] else 'false'}\n")

    payload: dict = {
        "verifier": NAME,
        "pass": bool(result["pass"]),
        "verdict": "pass" if result["pass"] else "fail",
        "evidence_path": evidence,
        "spec_dir": result["spec_dir"],
        "files": result["files"],
        "selected": result["selected"],
        "deselected": result["deselected"],
        "deselected_reported": result["deselected_reported"],
        "tampered": result["tampered"],
        "manifest": result["manifest"],
        "missing": result["missing"],
        "failures": result["failures"],
        "author_node_end": result["author_node_end"],
        "rc": result["rc"],
        "timed_out": result["timed_out"],
        "checks": checks,
        "checks_run": [c["name"] for c in checks],
        "failed_checks": failed,
        "reasons": reasons,
        # `diagnose.py` reads a single `reason`/`summary` string for the failure
        # detail; `reasons` alone would leave a diagnosis without one.
        "reason": "; ".join(reasons) or ("all checks passed" if result["pass"] else ""),
    }
    if result["note"]:
        payload["note"] = result["note"]
    if result["error"]:
        payload["error"] = result["error"]
    # The executor copies its captured stdout over verifier_spec-tests.json
    # (`execute_handlers._handle_verifier`), so stdout — not this file — is what
    # the engine reads back. Serialize ONCE and use that same text for both, so
    # the two copies cannot drift. Writing the file too keeps the verifier
    # self-contained for hand-runs and for the unit tests.
    blob = json.dumps(payload, indent=2)
    with open(json_path, "w", encoding="utf-8") as fh:
        fh.write(blob + "\n")
    sys.stdout.write(blob + "\n")
    return 0 if payload["pass"] else 1


def main(argv: list[str]) -> int:
    run_dir = (os.environ.get("MINI_ORK_RUN_DIR") or "").strip()
    if not run_dir:
        sys.stderr.write(f"{NAME}: MINI_ORK_RUN_DIR is not set\n")
        return 1
    return _normal(run_dir)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
