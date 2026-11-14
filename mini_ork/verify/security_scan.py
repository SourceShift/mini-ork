"""Scan the added lines of a diff for shipped credentials and dangerous API use.

Only lines the diff ADDS are examined, so a pre-existing problem elsewhere in
the tree never reddens a run that did not write it. A finding records the path,
line, rule id and a fixed redacted placeholder — the matched text itself is
never carried into a verdict file.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Mapping

from mini_ork.verify.gaming_scan import added_lines

RULES: tuple[tuple[str, str, str], ...] = (
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

_COMPILED = tuple(
    (rule, re.compile(pattern), why) for rule, pattern, why in RULES
)


@dataclass(frozen=True)
class Finding:
    path: str  # repo-relative, forward slashes
    line: int  # 1-based line number in the new file
    rule: str  # one of RULES
    why: str  # human-readable rationale
    text: str = "<redacted>"  # never the matched text


_ALLOWANCE_RE = re.compile(
    r"#\s*sec-allow:\s*([A-Za-z0-9_-]+(?:\s*,\s*[A-Za-z0-9_-]+)*)"
)


def allowance(line_text: str) -> frozenset[str]:
    """The rule ids excused on this line by a ``# sec-allow:`` marker."""
    match = _ALLOWANCE_RE.search(line_text)
    if match is None:
        return frozenset()
    return frozenset(
        part.strip() for part in match.group(1).split(",") if part.strip()
    )


def scan_added(added: Mapping[str, Mapping[int, str]]) -> list[Finding]:
    """Findings for every added line, sorted by ``(path, line, rule)``."""
    findings: list[Finding] = []
    for path in sorted(added):
        lines = added[path]
        for lineno in sorted(lines):
            text = lines[lineno].strip()
            if not text:
                continue
            excused = allowance(text)
            for rule, pattern, why in _COMPILED:
                if rule in excused:
                    continue
                if pattern.search(text):
                    findings.append(Finding(path, lineno, rule, why))
    findings.sort(key=lambda finding: (finding.path, finding.line, finding.rule))
    return findings


def _git(root: str, *args: str) -> tuple[int, str]:
    """Run git without raising. Returns ``(rc, stdout)``."""
    try:
        proc = subprocess.run(
            ["git", "-C", root, *args],
            capture_output=True, encoding="utf-8", errors="replace",
        )
    except OSError:
        return 127, ""
    return proc.returncode, proc.stdout or ""


def _untracked_paths(status_text: str) -> list[str]:
    """Repo-relative, forward-slashed paths for ``??`` entries in porcelain."""
    paths: list[str] = []
    for line in status_text.splitlines():
        if not line.startswith("?? "):
            continue
        rel = line[3:].strip()
        if len(rel) >= 2 and rel[0] == rel[-1] == '"':
            rel = rel[1:-1]
        if rel:
            paths.append(rel.replace("\\", "/"))
    return paths


def collect_added(
    root: str, base: str
) -> tuple[dict[str, dict[int, str]], list[str], bool]:
    """``build_added`` plus a ``measured`` flag: False when git failed."""
    # ``--no-ext-diff``/``--no-textconv`` are load-bearing: both are driven by
    # repo-controlled config and attributes, so a repository being audited could
    # otherwise make its own diff render empty and pass the gate by hiding the
    # lines it added.
    diff_args = ["diff", "--no-color", "--no-ext-diff", "--no-textconv", "-U0"]
    if base:
        diff_args.append(base)
    rc, diff_text = _git(root, *diff_args)
    if rc != 0:
        return {}, [], False

    added = added_lines(diff_text)

    rc, status_text = _git(root, "status", "--porcelain", "-uall")
    if rc != 0:
        return {}, [], False

    unreadable: list[str] = []
    for rel in _untracked_paths(status_text):
        if rel in added:
            continue
        try:
            text = (Path(root) / rel).read_text(
                encoding="utf-8", errors="replace"
            )
        except OSError:
            # Missing on disk (deleted after staging): report, never drop.
            unreadable.append(rel)
            continue
        added[rel] = {
            i: line for i, line in enumerate(text.splitlines(), start=1)
        }

    return added, unreadable, True


def build_added(root: str, base: str) -> tuple[dict, list[str]]:
    """Added lines for the run's change surface, plus unreadable paths.

    Any git non-zero exit yields ``({}, [])`` — the caller decides that this is
    an abstention, not that the diff is clean.
    """
    added, unreadable, _measured = collect_added(root, base)
    return added, unreadable


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="security-scan",
        description="Report credentials and dangerous API use on added lines.",
    )
    parser.add_argument("--root", default=".", metavar="DIR",
                        help="repository root to scan")
    parser.add_argument("--base", default="", metavar="REF",
                        help="base revision for the diff")
    args = parser.parse_args(argv)

    added, unreadable, measured = collect_added(args.root, args.base)
    if not measured:
        print(json.dumps({"findings": [], "count": 0, "unreadable": []}))
        return 3

    findings = scan_added(added)
    print(json.dumps({
        "findings": [asdict(finding) for finding in findings],
        "count": len(findings),
        "unreadable": unreadable,
    }))
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
