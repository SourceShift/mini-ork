"""Tiered hypothesis-then-confirm discovery over a run's added lines.

Every added dangerous-API line becomes a hypothesis, a deterministic rank
decides which hypotheses reach the expensive oracle tier, and only an executed
probe can confirm one — a pattern match alone never becomes a finding.

The proposer reuses ``mini_ork.verify.security_scan`` for the diff surface and
the ``# sec-allow:`` marker, so this layer excuses exactly the lines the shipped
security gate excuses. The oracle is a probe script under
``$MINI_ORK_RUN_DIR/hypotheses/<probe_name>``: a nonzero exit confirms the
hypothesis, a zero exit clears it, and anything the layer cannot execute leaves
it abstained.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Sequence

from mini_ork.verify import security_scan

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

_DEF_RE = re.compile(r"^\s*(?:async\s+)?def\s+\w+")
_DECORATOR_RE = re.compile(r"^\s*@\w")
_COMPILED_SINKS = tuple(
    (rule, re.compile(pattern), severity)
    for rule, pattern, severity in SINKS
)


@dataclass(frozen=True)
class Hypothesis:
    """One dangerous-API line, pending a deterministic oracle's verdict."""

    id: str  # "<rule>:<path>:<line>" — stable across runs
    rule: str
    path: str  # repo-relative, forward slashes
    line: int
    severity: int
    score: int
    signals: tuple[str, ...]
    outcome: str = ""
    reason: str = ""


def probe_name(hypothesis: Hypothesis) -> str:
    """The probe's filename under ``<run_dir>/hypotheses/``.

    The id cannot be a filename: it carries the ``:`` separator and the path's
    slashes. Nothing parses this name back — the caller already holds the
    hypothesis it was built from.
    """
    return (
        hypothesis.rule
        + "__"
        + hypothesis.path.replace("/", "~")
        + "__%d.py" % hypothesis.line
    )


def _leading_whitespace(line: str) -> int:
    return len(line) - len(line.lstrip())


def function_span(lines: list[str], lineno: int) -> tuple[int, int]:
    """The 1-based inclusive span of the innermost ``def`` enclosing ``lineno``.

    A decorator directly above the definition is counted as part of it: the
    entrypoint signal has to see ``@app.route`` to score the handler it decorates.
    Module-level code (no enclosing definition) is its own span.
    """
    count = len(lines)
    if count == 0:
        return (1, 0)
    index = min(max(int(lineno), 1), count) - 1

    # The indentation of the first non-blank line at or above the sink.
    indent = None
    probe = index
    while probe >= 0:
        if lines[probe].strip():
            indent = _leading_whitespace(lines[probe])
            break
        probe -= 1
    if indent is None:
        return (1, count)

    # The nearest preceding definition indented less than the sink.
    def_index = None
    probe = index
    while probe >= 0:
        line = lines[probe]
        if (
            line.strip()
            and _DEF_RE.match(line)
            and _leading_whitespace(line) < indent
        ):
            def_index = probe
            break
        probe -= 1
    if def_index is None:
        return (1, count)

    def_indent = _leading_whitespace(lines[def_index])
    start = def_index + 1
    probe = def_index - 1
    while (
        probe >= 0
        and lines[probe].strip()
        and _DECORATOR_RE.match(lines[probe])
        and _leading_whitespace(lines[probe]) <= def_indent
    ):
        start = probe + 1
        probe -= 1

    end = count
    for probe in range(def_index + 1, count):
        line = lines[probe]
        if line.strip() and _leading_whitespace(line) <= def_indent:
            end = probe
            break
    return (start, end)


def signals(
    lines: list[str], lineno: int, severity: int, path: str
) -> tuple[int, tuple[str, ...]]:
    """A deterministic integer score plus the sorted names that produced it."""
    score = int(severity)
    names = ["severity=%d" % int(severity)]

    start, end = function_span(lines, lineno)
    span = lines[start - 1:end]
    if any(SOURCES_RE.search(line) for line in span):
        score += 2
        names.append("untrusted-source")
    if any(ENTRYPOINT_RE.search(line) for line in span):
        score += 2
        names.append("entrypoint")

    # Every hypothesis is proposed from an added line, so this always fires and
    # is what lets the rank prefer what the run actually wrote.
    score += 2
    names.append("added")
    if path.endswith(".py"):
        score += 1
        names.append("python")
    return score, tuple(sorted(names))


def _lines_for(
    root: str, path: str, added_lines: dict[int, str]
) -> list[str]:
    """The whole file when it can be read, else the added lines alone.

    ``signals`` needs the enclosing function and its sources, which are not in
    the added lines; a missing file must still score, never raise.
    """
    try:
        text = (Path(root) / path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        highest = max(added_lines) if added_lines else 0
        lines = [""] * max(highest, 0)
        for lineno, line in added_lines.items():
            if 1 <= lineno <= len(lines):
                lines[lineno - 1] = line
        return lines
    return text.splitlines()


def propose(
    root: str, added: dict[str, dict[int, str]]
) -> list[Hypothesis]:
    """One hypothesis per added sink line not excused by ``# sec-allow:``."""
    hypotheses: list[Hypothesis] = []
    for path in sorted(added):
        added_lines = added[path]
        lines = _lines_for(root, path, added_lines)
        for lineno in sorted(added_lines):
            text = added_lines[lineno]
            excused = security_scan.allowance(text)
            match = None
            for rule, pattern, severity in _COMPILED_SINKS:
                if rule in excused:
                    continue
                if not pattern.search(text):
                    continue
                if match is None or severity > match[2]:
                    match = (rule, pattern, severity)
            if match is None:
                continue
            rule, _pattern, severity = match
            score, names = signals(lines, lineno, severity, path)
            hypotheses.append(Hypothesis(
                id="%s:%s:%d" % (rule, path, lineno),
                rule=rule,
                path=path,
                line=lineno,
                severity=severity,
                score=score,
                signals=names,
            ))
    hypotheses.sort(key=lambda hypothesis: (
        -hypothesis.score, hypothesis.path, hypothesis.line))
    return hypotheses


def rank(
    hypotheses: list[Hypothesis],
) -> tuple[list[Hypothesis], list[Hypothesis]]:
    """The oracle tier's selection, plus what it never sees."""
    ordered = sorted(
        hypotheses,
        key=lambda hypothesis: (
            -hypothesis.score,
            -hypothesis.severity,
            hypothesis.path,
            hypothesis.line,
        ),
    )
    return ordered[:TIER3_CAP], ordered[TIER3_CAP:]


def probes(
    run_dir: str, hypotheses: Sequence[Hypothesis]
) -> dict[str, str]:
    """Hypothesis id -> absolute probe path, for probes that exist."""
    if not run_dir:
        return {}
    directory = Path(run_dir) / PROBE_DIR
    mapping: dict[str, str] = {}
    for hypothesis in hypotheses:
        candidate = directory / probe_name(hypothesis)
        try:
            exists = candidate.is_file()
        except OSError:
            exists = False
        if exists:
            mapping[hypothesis.id] = str(candidate.resolve())
    return mapping


def confirm(
    hypothesis: Hypothesis, probe: str, root: str
) -> tuple[str, str]:
    """Run one probe and map its exit status to an outcome."""
    # A missing script is not an interpreter start failure: python would exit 2
    # ("can't open file") and that nonzero code would read as CONFIRMED. Check
    # the file first so an unreadable probe abstains, as the spec requires.
    try:
        present = os.path.isfile(probe)
    except OSError:
        present = False
    if not present:
        return ABSTAINED, "probe could not be executed: no such probe file"

    env = dict(os.environ)
    env["MO_HYPOTHESIS_PATH"] = hypothesis.path
    env["MO_HYPOTHESIS_LINE"] = str(hypothesis.line)
    env["MO_HYPOTHESIS_RULE"] = hypothesis.rule
    try:
        proc = subprocess.run(
            [sys.executable, probe],
            cwd=root,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=PROBE_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return ABSTAINED, "probe timed out"
    except OSError as exc:
        return ABSTAINED, "probe could not be executed: %s" % exc
    if proc.returncode != 0:
        return CONFIRMED, "probe exited %d" % proc.returncode
    return CLEARED, "probe exited 0"


def _entry(hypothesis: Hypothesis) -> dict:
    return {
        "id": hypothesis.id,
        "rule": hypothesis.rule,
        "path": hypothesis.path,
        "line": hypothesis.line,
        "severity": hypothesis.severity,
        "score": hypothesis.score,
        "signals": list(hypothesis.signals),
        "outcome": hypothesis.outcome,
        "reason": hypothesis.reason,
    }


def evaluate(
    root: str, added: dict[str, dict[int, str]], run_dir: str
) -> tuple[str, dict]:
    """The cascade's status (pass | fail | vacuous | abstain) and its report."""
    hypotheses = propose(root, added)
    if not hypotheses:
        return "vacuous", {
            "reason": "no dangerous-API line on the change surface"
        }

    selected, deferred = rank(hypotheses)
    mapping = probes(run_dir, selected)
    resolved: list[Hypothesis] = []
    for hypothesis in selected:
        probe = mapping.get(hypothesis.id)
        if probe is None:
            resolved.append(replace(
                hypothesis,
                outcome=ABSTAINED,
                reason="no probe: nothing confirms this hypothesis",
            ))
            continue
        outcome, reason = confirm(hypothesis, probe, root)
        resolved.append(replace(hypothesis, outcome=outcome, reason=reason))

    counts = {outcome: 0 for outcome in OUTCOMES}
    for hypothesis in resolved:
        counts[hypothesis.outcome] += 1
    status = "fail" if counts[CONFIRMED] else "pass"
    report = {
        "hypotheses": [_entry(hypothesis) for hypothesis in resolved],
        "counts": counts,
        "deferred": [hypothesis.id for hypothesis in deferred],
    }
    return status, report


def _payload(status: str, report: dict) -> dict:
    """Every ``--json`` result carries its counts and hypotheses."""
    payload = {
        "status": status,
        "hypotheses": report.get("hypotheses", []),
        "counts": report.get("counts") or {outcome: 0 for outcome in OUTCOMES},
        "deferred": report.get("deferred", []),
    }
    if report.get("reason"):
        payload["reason"] = report["reason"]
    return payload


def _exit_code(status: str, report: dict) -> int:
    counts = report.get("counts") or {}
    if status == "fail" or counts.get(CONFIRMED, 0):
        return 1
    if counts.get(ABSTAINED, 0):
        return 3
    return 0


def _report(as_json: bool, status: str, report: dict, code: int) -> int:
    payload = _payload(status, report)
    if as_json:
        print(json.dumps(payload, separators=(",", ":"), ensure_ascii=False))
        return code
    for entry in payload["hypotheses"]:
        print("HYPOTHESIS: %s %s:%s:%s" % (
            entry["score"], entry["rule"], entry["path"], entry["line"]))
        print("  severity=%s score=%s outcome=%s" % (
            entry["severity"], entry["score"], entry["outcome"]))
        if entry["reason"]:
            print("  reason: %s" % entry["reason"])
    if payload.get("reason"):
        print("reason: %s" % payload["reason"])
    counts = payload["counts"]
    print(
        "hypothesis-cascade: %d confirmed, %d cleared, %d abstained, %d deferred"
        % (
            counts.get(CONFIRMED, 0),
            counts.get(CLEARED, 0),
            counts.get(ABSTAINED, 0),
            len(payload["deferred"]),
        )
    )
    return code


def main(argv: list[str] | None = None) -> int:
    import argparse  # deferred: module imports stay stdlib-light at import time

    parser = argparse.ArgumentParser(
        prog="hypothesis-cascade",
        description=(
            "Propose hypotheses from added dangerous-API lines and let an "
            "executed probe confirm, clear or abstain on each."
        ),
    )
    parser.add_argument("--root", default=".", metavar="DIR",
                        help="repository root to scan")
    parser.add_argument("--base", default="", metavar="REF",
                        help="base revision for the diff")
    parser.add_argument("--paths", default=None, metavar="PATHS",
                        help="comma- or newline-separated change surface")
    parser.add_argument("--json", action="store_true",
                        help="emit one JSON line instead of text blocks")
    args = parser.parse_args(argv)

    raw_paths = args.paths
    if raw_paths is None:
        raw_paths = os.environ.get("MINI_ORK_TOUCHED_FILES", "")
    only = [
        piece.strip()
        for piece in re.split(r"[,\n]", raw_paths or "")
        if piece.strip()
    ]
    if not only:
        parser.error(
            "a change surface is required: pass --paths or set "
            "MINI_ORK_TOUCHED_FILES"
        )

    added, _unreadable, measured = security_scan.collect_added(
        args.root, args.base, only)
    if not measured:
        # An empty dict here means git failed, NOT that the diff is clean.
        message = (
            "git could not produce a diff — the change surface was not "
            "measured, never an empty clean surface"
        )
        return _report(args.json, "abstain", {"reason": message}, 3)

    surface = set(only)
    added = {
        path: lines for path, lines in added.items() if path in surface
    }
    run_dir = os.environ.get("MINI_ORK_RUN_DIR", "").strip()
    status, report = evaluate(args.root, added, run_dir)
    return _report(args.json, status, report, _exit_code(status, report))


if __name__ == "__main__":
    sys.exit(main())
