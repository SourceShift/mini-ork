"""suite_strengthen — SWE-ABS-style suite strengthening, in shadow.

The suite-adequacy audit (``mini_ork.gates.suite_adequacy``) can say a green
suite is INADEQUATE because some of its own AST mutants survive. This module
tries to make that suite stronger instead of just downgrading it:

1. it asks a lane to synthesise plausible pytest modules aimed at the surviving
   mutants;
2. it keeps a generated test only when it passes on the candidate, kills at
   least one surviving mutant, and is anchored by the base tree — failing on the
   base is new fail-to-pass evidence (``target``), passing on the base pins
   behaviour the patch preserved (``preserve``).

There is no gold patch, so a test the candidate fails is only recorded
(``candidate_red``) — without gold it cannot be told apart from a wrong test.

Everything is shadow: ``strengthen`` returns a record for the verifier payload
and ``<run_dir>/strengthen/``; it never changes ``pass`` or an exit code, and it
never raises out of ``strengthen``. The module writes nothing inside
``candidate_dir`` or ``base_dir``; mutant runs happen in one throwaway copy.

Knobs (module defaults; see :func:`settings`):

  MO_SUITE_STRENGTHEN                shadow|0|gate, default shadow
  MO_SUITE_STRENGTHEN_MAX_SURVIVORS  cap on targeted survivors (default 5, 1..20)
  MO_SUITE_STRENGTHEN_MAX_TESTS      cap on generated tests (default 3, 1..10)
  MO_SUITE_STRENGTHEN_TIMEOUT_S      per-run pytest timeout (default 60, >=1)
  MO_SUITE_STRENGTHEN_BUDGET_S       wall budget for the whole pass (default 240,
                                     >=1; see :func:`budget_s`)
"""
from __future__ import annotations

import ast
import json
import os
import re
import shutil
import tempfile
import time

from mini_ork.certify import codefix_terms
from mini_ork.certify.context import imports_code_under_test, module_name
from mini_ork.gates import suite_adequacy

MODE_ENV = "MO_SUITE_STRENGTHEN"
BUDGET_ENV = "MO_SUITE_STRENGTHEN_BUDGET_S"

_FENCE_RE = re.compile(r"```(?:python)?\s*\n(.*?)```", re.S)

#: The same copy exclusions suite_adequacy uses: caches and VCS state are never
#: part of a mutant run and must not be copied (or could shadow the mutation).
_COPY_IGNORE = shutil.ignore_patterns(
    ".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    ".tox", ".venv", "venv", "node_modules", ".mini-ork",
)

#: Total candidate-source characters the prompt may carry.
_PROMPT_SOURCE_CHARS = 6000
#: Issue characters the prompt may carry.
_PROMPT_ISSUE_CHARS = 1800


def mode(environ=None) -> str:
    """The strengthening mode: ``"off"``, ``"gate"`` or ``"shadow"`` (default).

    The same parse as :func:`mini_ork.certify.codefix_terms.mode`: a typo never
    gates — only an explicit ``1``/``gate`` enables gate mode, and only
    ``0``/``off``/``false``/``no`` disables the pass entirely. ``gate`` is
    reserved: this kickoff records it but behaves exactly like shadow.
    """
    src = os.environ if environ is None else environ
    raw = str(src.get(MODE_ENV, "") or "").strip().lower()
    if raw in ("0", "off", "false", "no"):
        return "off"
    if raw in ("1", "gate"):
        return "gate"
    return "shadow"


def settings(environ=None) -> dict:
    """Read the three strengthening knobs, clamped to their legal ranges.

    An unparsable value falls back to the module default; an out-of-range value
    clamps to the nearest bound (a typo cannot launch a 1000-mutant sweep).
    """
    src = os.environ if environ is None else environ

    def _int(key, default, lo, hi):
        try:
            v = int(src.get(key, str(default)))
        except (TypeError, ValueError):
            return default
        return min(max(v, lo), hi)

    def _float(key, default, lo):
        try:
            v = float(src.get(key, str(default)))
        except (TypeError, ValueError):
            return default
        return max(v, lo)

    return {
        "max_survivors": _int("MO_SUITE_STRENGTHEN_MAX_SURVIVORS", 5, 1, 20),
        "max_tests": _int("MO_SUITE_STRENGTHEN_MAX_TESTS", 3, 1, 10),
        "timeout_s": _float("MO_SUITE_STRENGTHEN_TIMEOUT_S", 60.0, 1.0),
    }


def budget_s(environ=None) -> float:
    """Wall budget, in seconds, for one whole strengthening pass.

    The per-run caps alone allow one lane call plus up to
    ``max_tests * 2 + max_survivors * max_tests`` pytest runs of ``timeout_s``
    each (about 22 minutes at the defaults) inside the code-fix verifier. Past
    the deadline the lane is not called and no pytest is spawned: those tests
    record ``abstained`` and kill nothing, so an instrument can never push the
    verifier past its own timeout. Default 240, at least 1; unparsable gives
    the default.
    """
    src = os.environ if environ is None else environ
    try:
        return max(float(src.get(BUDGET_ENV, "240")), 1.0)
    except (TypeError, ValueError):
        return 240.0


def descriptor(m) -> str:
    """One survivor as ``<id> <file>:<line> <operator>: <original> -> <mutated>``."""
    return f"{m.id} {m.file}:{m.line} {m.operator}: {m.original} -> {m.mutated}"


def _survivor_ids(report) -> list:
    """Every survivor id in ``report``, defensively (the error path needs it)."""
    try:
        return [s.get("id") for s in report.get("survivors") or []
                if isinstance(s, dict)]
    except Exception:
        return []


def match_survivors(repo_dir: str, report: dict) -> tuple[list, list[str]]:
    """Re-generate the report's mutants and pair them with its survivors.

    Returns ``(matched_mutants, unmatched_survivor_ids)``. A survivor matches
    only when the ``Mutant`` shares its id AND its ``(file, line, operator,
    original, mutated)`` exactly, so a report from a different tree or a
    different mutation seed does not silently target the wrong site. Never
    raises: a generation failure makes every survivor unmatched.
    """
    try:
        survivors = report.get("survivors") or []
        mutants = suite_adequacy.generate_mutants(
            repo_dir, report["files"],
            max_mutants=report["max_mutants"], seed=0,
        )
        by_id = {m.id: m for m in mutants}
        matched: list = []
        unmatched: list[str] = []
        for s in survivors:
            m = by_id.get(s.get("id")) if isinstance(s, dict) else None
            if m is not None and (
                (m.file, m.line, m.operator, m.original, m.mutated)
                == (s.get("file"), s.get("line"), s.get("operator"),
                    s.get("original"), s.get("mutated"))
            ):
                matched.append(m)
            else:
                unmatched.append(s.get("id") if isinstance(s, dict) else None)
        return matched, unmatched
    except Exception:
        return [], _survivor_ids(report)


def admissible(src: str, modules: tuple[str, ...]) -> tuple[bool, str]:
    """Whether a generated block may be offered as suite evidence.

    Checks, in order: it parses; it has at least one top-level ``def test_*``;
    it imports the code under test (a probe that never touches it can say
    nothing about the patch).
    """
    try:
        tree = ast.parse(src)
    except (SyntaxError, ValueError):
        return (False, "does not parse")
    top_level_test = any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test_")
        for node in tree.body
    )
    if not top_level_test:
        return (False, "no top-level test function")
    if not imports_code_under_test(src, modules):
        return (False, "does not import the code under test")
    return (True, "")


def build_prompt(issue: str, survivors: list, candidate_dir: str,
                 max_tests: int) -> str:
    """The lane prompt: the issue, the surviving mutants, and their sources."""
    lines = [
        "STRENGTHEN THE SUITE.",
        "",
        "The candidate fix addresses this issue:",
        issue[:_PROMPT_ISSUE_CHARS],
        "",
        "The candidate test suite leaves these AST mutants alive:",
    ]
    for m in survivors:
        lines.append(descriptor(m))

    budget = _PROMPT_SOURCE_CHARS
    seen = set()
    for m in survivors:
        if m.file in seen or budget <= 0:
            continue
        seen.add(m.file)
        try:
            with open(os.path.join(candidate_dir, m.file), encoding="utf-8",
                      errors="replace") as fh:
                source = fh.read()
        except OSError:
            continue
        chunk = source[:budget]
        budget -= len(chunk)
        lines.append(f"# file: {m.file}")
        lines.append(chunk)

    lines.extend([
        "",
        f"Write at most {max_tests} fenced ```python block(s), each a "
        "self-contained pytest module. Every module must import the code under "
        "test and assert the behaviour the issue intends at the mutated lines.",
    ])
    return "\n".join(lines)


def _record(mode_name: str, status: str, reason: str, *, score_before,
            unmatched=None) -> dict:
    """The 12-key result shape shared by every non-``ran`` outcome."""
    return {
        "mode": mode_name,
        "status": status,
        "reason": reason,
        "targeted": [],
        "unmatched": list(unmatched or []),
        "tests": [],
        "admitted": [],
        "newly_killed": [],
        "score_before": score_before,
        "score_after": None,
        "verdict_after": None,
        "would_rescue": False,
    }


def _write_evidence(out_dir: str, named_blocks: list, result: dict) -> None:
    """Write the generated blocks and the record; swallow ``OSError``.

    The evidence is an instrument, not a gate: a read-only or occupied run dir
    must not turn a completed run into an error.
    """
    try:
        os.makedirs(out_dir, exist_ok=True)
        for name, src in named_blocks:
            with open(os.path.join(out_dir, f"{name}.py"), "w",
                      encoding="utf-8") as fh:
                fh.write(src)
        with open(os.path.join(out_dir, "strengthen.json"), "w",
                  encoding="utf-8") as fh:
            json.dump(result, fh, indent=2)
    except OSError:
        pass


def _run_mutant_copy(candidate_dir: str, targeted: list, pending: list,
                     runner) -> dict:
    """Per-test killed ids, measured against one throwaway mutant copy.

    ``pending`` is ``[(name, src)]`` for the tests that pass on the candidate.
    For each targeted mutant the copy gets that mutant's source, every pending
    test runs against it, and the copy file is restored — the candidate tree
    itself is only ever read. The copy is removed in ``finally``.
    """
    kills = {name: [] for name, _ in pending}
    if not pending or not targeted:
        return kills
    tmp = tempfile.mkdtemp(prefix="mo-strengthen-")
    try:
        copy_dir = os.path.join(tmp, "repo")
        shutil.copytree(candidate_dir, copy_dir, symlinks=True,
                        ignore=_COPY_IGNORE)
        originals = {}
        for m in targeted:
            if m.file not in originals:
                with open(os.path.join(candidate_dir, m.file), "rb") as fh:
                    originals[m.file] = fh.read()
        for m in targeted:
            copy_path = os.path.join(copy_dir, m.file)
            with open(copy_path, "w", encoding="utf-8") as fh:
                fh.write(m.source)
            try:
                for name, src in pending:
                    if runner.run_in(copy_dir, src).status == "failed":
                        kills[name].append(m.id)
            finally:
                with open(copy_path, "wb") as fh:
                    fh.write(originals[m.file])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return kills


def strengthen(report: dict, *, issue: str, candidate_dir: str, base_dir: str,
               mode: str = "shadow", dispatch=None, python: str | None = None,
               timeout_s: float | None = None, max_survivors: int | None = None,
               max_tests: int | None = None, out_dir: str | None = None) -> dict:
    """Generate, admit and score candidate-strengthening tests. Never raises.

    Returns exactly twelve keys: ``mode``, ``status``, ``reason``,
    ``targeted``, ``unmatched``, ``tests``, ``admitted``, ``newly_killed``,
    ``score_before``, ``score_after``, ``verdict_after``, ``would_rescue``.
    ``status`` is ``ran``, ``skipped`` or ``error``; only ``ran`` runs a lane or
    a pytest.
    """
    score_before = report.get("score") if isinstance(report, dict) else None
    unmatched: list = []
    deadline = time.monotonic() + budget_s()
    try:
        cfg = settings()
        timeout_s = cfg["timeout_s"] if timeout_s is None else timeout_s
        max_survivors = cfg["max_survivors"] if max_survivors is None else max_survivors
        max_tests = cfg["max_tests"] if max_tests is None else max_tests

        # ── the ordered skip ladder ────────────────────────────────────────
        if (not isinstance(report, dict)
                or report.get("verdict") != "INADEQUATE"
                or not report.get("survivors")):
            return _record(mode, "skipped", "not inadequate",
                           score_before=score_before)
        if not issue.strip():
            return _record(mode, "skipped", "no kickoff text",
                           score_before=score_before)
        if (not isinstance(report.get("files"), list)
                or not isinstance(report.get("max_mutants"), int)):
            return _record(mode, "skipped", "report lacks files/max_mutants",
                           score_before=score_before)
        matched, unmatched = match_survivors(candidate_dir, report)
        if not matched:
            return _record(mode, "skipped", "no survivor could be regenerated",
                           score_before=score_before, unmatched=unmatched)

        targeted_mutants = matched[:max_survivors]
        targeted = [m.id for m in targeted_mutants]
        modules = tuple(
            name for name in (module_name(m.file) for m in targeted_mutants)
            if name is not None
        )

        prompt = build_prompt(issue, targeted_mutants, candidate_dir, max_tests)
        raw = (dispatch or codefix_terms.bounded_dispatch(deadline))(prompt)
        blocks = _FENCE_RE.findall(raw or "")[:max_tests]
        named_blocks = [(f"t{i}", src) for i, src in enumerate(blocks, 1)]

        runner = codefix_terms.LocalTreeRunner(
            base_dir, candidate_dir, python=python, timeout_s=timeout_s,
            deadline=deadline,
        )

        # ── classify every block; note which tests pass on the candidate ──
        tests: list[dict] = []
        pending: list = []
        for name, src in named_blocks:
            ok, why = admissible(src, modules)
            rec = {
                "name": name, "admissible": ok, "why": why,
                "on_candidate": None, "on_base": None, "kills": [], "kind": None,
            }
            if not ok:
                rec["kind"] = "inadmissible"
                tests.append(rec)
                continue
            on_candidate = runner.run_in(candidate_dir, src).status
            rec["on_candidate"] = on_candidate
            if on_candidate not in ("passed", "failed"):
                rec["kind"] = "abstained"
                tests.append(rec)
                continue
            rec["on_base"] = runner.run_in(base_dir, src).status
            if on_candidate == "failed":
                rec["kind"] = "candidate_red"
                tests.append(rec)
                continue
            pending.append((name, src))
            tests.append(rec)

        # ── one mutant copy: which passing tests kill which survivor ──────
        kills_by_test = _run_mutant_copy(
            candidate_dir, targeted_mutants, pending, runner,
        )
        for rec in tests:
            if rec["name"] not in kills_by_test:
                continue
            rec["kills"] = kills_by_test[rec["name"]]
            if not rec["kills"]:
                rec["kind"] = "no_kill"
            elif rec["on_base"] == "failed":
                rec["kind"] = "target"
            elif rec["on_base"] == "passed":
                rec["kind"] = "preserve"
            else:
                rec["kind"] = "unanchored"

        # ── the result ────────────────────────────────────────────────────
        admitted = [t["name"] for t in tests
                    if t["kind"] in ("target", "preserve")]
        killed_ids = {mid for t in tests if t["kind"] in ("target", "preserve")
                      for mid in t["kills"]}
        newly_killed = [mid for mid in targeted if mid in killed_ids]

        killed = int(report.get("killed", 0))
        survived = int(report.get("survived", 0))
        denominator = killed + survived
        score_after = (round((killed + len(newly_killed)) / denominator, 3)
                       if denominator else None)
        min_score = float(report.get("min_score", 0.6))
        verdict_after = (
            "ADEQUATE"
            if score_after is not None and score_after >= min_score
            else "INADEQUATE"
        )
        would_rescue = verdict_after == "ADEQUATE" and bool(admitted)
        reason = (
            "no test generated" if not named_blocks
            else f"{len(admitted)} of {len(tests)} generated tests admitted; "
                 f"{len(newly_killed)} of {len(targeted)} targeted survivors killed"
        )

        result = {
            "mode": mode,
            "status": "ran",
            "reason": reason,
            "targeted": targeted,
            "unmatched": unmatched,
            "tests": tests,
            "admitted": admitted,
            "newly_killed": newly_killed,
            "score_before": score_before,
            "score_after": score_after,
            "verdict_after": verdict_after,
            "would_rescue": would_rescue,
        }
        if out_dir:
            _write_evidence(out_dir, named_blocks, result)
        return result
    except Exception as exc:  # noqa: BLE001 — the instrument must never raise
        return _record(mode, "error", f"{type(exc).__name__}: {exc}",
                       score_before=score_before, unmatched=unmatched)


__all__ = [
    "MODE_ENV",
    "admissible",
    "build_prompt",
    "budget_s",
    "descriptor",
    "match_survivors",
    "mode",
    "settings",
    "strengthen",
]
