"""Every plausible string literal is promoted to a candidate secret.

The score is a fixed, inspectable formula rather than a learned classifier.
The uncertain middle band abstains to review rather than passing as clean.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import math
import os
import re
from typing import NamedTuple

LITERAL_MIN = 12
FLAG_BAND = 0.70
REVIEW_BAND = 0.45
FLAGGED = "flagged"
REVIEW = "review"
CLEAN = "clean"
BANDS = (FLAGGED, REVIEW, CLEAN)
MAX_FILE_BYTES = 1 << 20  # 1 MiB
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

_PATH_PREFIXES = ("/", "./", "../", "~/")
_STRING_LITERAL_RE = re.compile(r"""(["'])((?:\\.|(?!\1).)*)\1""")
_NUL_SCAN_BYTES = 4096
_CONTEXT_LIMIT = 200
_WINDOWS_ABSOLUTE_RE = re.compile(r"^[A-Za-z]:")


class Candidate(NamedTuple):
    path: str
    line: int
    literal: str
    score: float
    band: str
    reasons: tuple[str, ...]


class ScanResult(NamedTuple):
    """Candidates, plus what was not measured and what was deliberately skipped.

    The two note lists are not the same thing. ``unreadable`` is a measurement
    failure — a file we were asked about and could not read, or a path that
    escaped the root — so the caller must not report that surface clean.
    ``skipped`` is a deliberate non-carrier (binary, oversized, NUL-bearing) and
    is reported for the operator without changing the verdict.
    """

    candidates: list
    unreadable: list
    skipped: list


def shannon(text: str) -> float:
    """Shannon entropy in bits per character over ``text``'s distribution."""
    if not text:
        return 0.0
    total = len(text)
    counts: dict[str, int] = {}
    for char in text:
        counts[char] = counts.get(char, 0) + 1
    return -sum(
        (count / total) * math.log2(count / total) for count in counts.values()
    )


def _classes(text: str) -> int:
    """How many of lowercase, uppercase, digit and other are present (0-4)."""
    present = 0
    if any(char.islower() for char in text):
        present += 1
    if any(char.isupper() for char in text):
        present += 1
    if any(char.isdigit() for char in text):
        present += 1
    if any(not char.isalnum() for char in text):
        present += 1
    return present


def demote(literal: str) -> str | None:
    """The reason this literal cannot be a secret, or ``None`` when it survives."""
    if " " in literal or "\t" in literal or "\n" in literal:
        return "contains whitespace"
    if _PLACEHOLDER_RE.search(literal) or _FORMAT_RE.match(literal):
        return "placeholder or format spec"
    if literal.startswith(_PATH_PREFIXES):
        return "path-like"
    if literal.startswith("http://") or literal.startswith("https://"):
        query = literal.find("?")
        has_query_parameter = query != -1 and "=" in literal[query + 1:]
        if not has_query_parameter:
            return "url without a query parameter"
    if len(set(literal)) <= 3:
        return "low character variety"
    return None


def score_literal(literal: str, context: str) -> tuple[float, tuple[str, ...]]:
    """Score ``literal`` in ``context`` on a fixed formula in ``[0.0, 1.0]``."""
    reason = demote(literal)
    if reason is not None:
        return 0.0, (reason,)

    entropy = shannon(literal)
    entropy_part = 0.35 * min(entropy / 4.5, 1.0)
    length_part = 0.25 * (1.0 if 16 <= len(literal) <= 128 else 0.5)
    class_count = _classes(literal)
    classes_part = 0.20 * (1.0 if class_count >= 3 else 0.0)
    has_context = bool(CONTEXT_RE.search(context))
    context_part = 0.20 * (1.0 if has_context else 0.0)

    score = round(entropy_part + length_part + classes_part + context_part, 4)
    reasons = (
        "entropy=%.2f" % entropy,
        "length=%d" % len(literal),
        "classes=%d" % class_count,
        "context" if has_context else "no-context",
    )
    return score, tuple(sorted(reasons))


def band(score: float) -> str:
    """``FLAGGED`` at or above ``FLAG_BAND``, ``REVIEW`` at ``REVIEW_BAND``."""
    if score >= FLAG_BAND:
        return FLAGGED
    if score >= REVIEW_BAND:
        return REVIEW
    return CLEAN


def mask(literal: str) -> str:
    """A triage-safe rendering: the ends, the length and a digest, not the secret.

    A candidate is not a verdict, but a candidate IS a copy of the literal, so
    printing it in full puts the very string this pass exists to catch into a
    terminal, a log or a run record. The digest is what lets two reports agree
    that they saw the same string.
    """
    digest = hashlib.sha256(literal.encode("utf-8", "replace")).hexdigest()[:12]
    if len(literal) <= 16:
        # A short literal has no middle to hide: three leading and two trailing
        # characters would be a third of it, so show none of it.
        return "len=%d sha256=%s" % (len(literal), digest)
    return "%s..%s len=%d sha256=%s" % (literal[:3], literal[-2:], len(literal), digest)


def _context_for(lines: list[str], lineno: int, column: int) -> str:
    """The line before plus the line prefix, truncated to the last 200 chars."""
    pieces: list[str] = []
    previous = lineno - 2
    if 0 <= previous < len(lines):
        pieces.append(lines[previous])
    current = lineno - 1
    if 0 <= current < len(lines):
        pieces.append(lines[current][:column])
    return "".join(pieces)[-_CONTEXT_LIMIT:]


def _extract_literals_regex(text: str) -> list[tuple[int, int, str]]:
    triples: list[tuple[int, int, str]] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        for match in _STRING_LITERAL_RE.finditer(line):
            literal = match.group(2)
            if len(literal) >= LITERAL_MIN:
                triples.append((lineno, match.start(), literal))
    return triples


def extract_literals(text: str) -> list[tuple[int, int, str]]:
    """``(lineno, column, literal)`` triples at or above ``LITERAL_MIN``.

    Bytes literals count: ``b"..."`` is how a credential reaches a request body
    or an HMAC call as often as a ``str`` does.
    """
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return _extract_literals_regex(text)

    triples: list[tuple[int, int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, (str, bytes)):
            if isinstance(node.value, bytes):
                literal = node.value.decode("utf-8", "replace")
            else:
                literal = node.value
            if len(literal) >= LITERAL_MIN:
                triples.append((node.lineno, node.col_offset, literal))
    triples.sort(key=lambda triple: (triple[0], triple[1]))
    return triples


def scan_text(path: str, text: str) -> list[Candidate]:
    """One scored candidate per extracted literal, ordered by position."""
    lines = text.splitlines()
    candidates: list[Candidate] = []
    for lineno, column, literal in extract_literals(text):
        context = _context_for(lines, lineno, column)
        score, reasons = score_literal(literal, context)
        candidates.append(
            Candidate(path, lineno, literal, score, band(score), reasons)
        )
    return candidates


def on_disk(root: str, rel: str) -> "str | None":
    """The path for ``rel`` under ``root``, or ``None`` when it escapes the root.

    ``rel`` comes from a diff or from ``MINI_ORK_TOUCHED_FILES``, and both are
    strings this module did not author. An absolute path, a ``..`` segment or a
    symlink pointing outside the root would otherwise make the report describe a
    file the caller never asked about — and, worse, publish its contents as
    "clean".
    """
    if not rel or rel.startswith(("/", "\\")) or _WINDOWS_ABSOLUTE_RE.match(rel):
        return None
    base = os.path.realpath(root)
    candidate = os.path.realpath(os.path.join(base, rel))
    # ``"/"`` is already a separator, so appending another one would make the
    # prefix ``"//"`` and reject every path under a filesystem-root scan.
    prefix = base if base.endswith(os.sep) else base + os.sep
    if candidate == base or not candidate.startswith(prefix):
        return None
    return candidate


def _read(root: str, rel: str) -> tuple[bytes | None, str]:
    """``(data, "")`` when the file is scannable, else ``(None, reason)``."""
    path = on_disk(root, rel)
    if path is None:
        return None, "escapes root"
    if not os.path.exists(path):
        # A change surface names deleted files too, so absence is ordinary. It
        # is still reported: nobody should read "0 candidates" as "this path was
        # scanned and found clean" when the file is not there.
        return None, "skipped: missing"
    if not os.path.isfile(path):
        return None, "skipped: not a regular file"
    try:
        if os.path.getsize(path) > MAX_FILE_BYTES:
            return None, "skipped: larger than %d bytes" % MAX_FILE_BYTES
        if os.path.splitext(rel)[1].lower() in BINARY_EXTENSIONS:
            return None, "skipped: binary extension"
        with open(path, "rb") as handle:
            data = handle.read()
    except OSError as exc:
        return None, "unreadable: %s" % exc.strerror
    if b"\x00" in data[:_NUL_SCAN_BYTES]:
        return None, "skipped: contains NUL bytes"
    return data, ""


def scan_file(root: str, rel: str) -> tuple[list[Candidate], str]:
    """Candidates for one file plus the reason it was not measured (``""``=ok)."""
    data, reason = _read(root, rel)
    if data is None:
        return [], reason
    return scan_text(rel, data.decode("utf-8", errors="replace")), ""


def scan(root: str, paths: list[str]) -> ScanResult:
    """Every candidate across ``paths``, ordered, with what was not measured.

    Duplicates are collapsed first: the same path listed twice would otherwise
    double-count its candidates and make the summary lie about how many
    distinct literals were seen.
    """
    candidates: list[Candidate] = []
    unreadable: list[str] = []
    skipped: list[str] = []
    for rel in dict.fromkeys(paths):
        found, reason = scan_file(root, rel)
        candidates.extend(found)
        if reason.startswith("unreadable") or reason == "escapes root":
            unreadable.append("%s: %s" % (rel, reason))
        elif reason:
            skipped.append("%s: %s" % (rel, reason))
    candidates.sort(key=lambda candidate: (candidate.path, candidate.line))
    return ScanResult(candidates, unreadable, skipped)


def summarize(
    result: ScanResult,
    *,
    show_literal: bool = False,
) -> dict:
    """Band counts plus each candidate, masked unless the caller opted in."""
    counts: dict[str, int] = {name: 0 for name in BANDS}
    entries: list[dict] = []
    for candidate in result.candidates:
        counts[candidate.band] = counts.get(candidate.band, 0) + 1
        entries.append({
            "path": candidate.path,
            "line": candidate.line,
            "literal": (
                candidate.literal if show_literal else mask(candidate.literal)
            ),
            "score": candidate.score,
            "band": candidate.band,
            "reasons": list(candidate.reasons),
        })
    return {
        "counts": counts,
        "candidates": entries,
        "unreadable": list(result.unreadable),
        "skipped": list(result.skipped),
    }


def _split_paths(raw: str) -> list[str]:
    return [part.strip() for part in re.split(r"[,\n]", raw or "") if part.strip()]


def main(argv: list[str] | None = None) -> int:
    """Exit ``1`` when flagged, ``3`` when nothing is proven clean, else 0.

    The review band and an unreadable surface both exit ``3``: an uncertain
    literal and a file we could not open are both "not measured", and a scanner
    that calls them clean is the failure mode this pass exists to remove.
    """
    parser = argparse.ArgumentParser(
        description="Promote string literals to secret candidates and score them."
    )
    parser.add_argument("--root", default=".", help="repository root to join paths onto")
    parser.add_argument(
        "--paths",
        default=None,
        help="comma- or newline-separated paths; defaults to MINI_ORK_TOUCHED_FILES",
    )
    parser.add_argument("--json", action="store_true", help="emit a JSON summary")
    parser.add_argument(
        "--show-literal",
        action="store_true",
        help="print candidate literals in full instead of the masked form",
    )
    args = parser.parse_args(argv)

    raw = args.paths
    if raw is None:
        raw = os.environ.get("MINI_ORK_TOUCHED_FILES", "")
    paths = _split_paths(raw)
    if not paths:
        parser.error("no paths to scan: pass --paths or set MINI_ORK_TOUCHED_FILES")

    result = scan(args.root, paths)
    counts: dict[str, int] = {name: 0 for name in BANDS}
    for candidate in result.candidates:
        counts[candidate.band] = counts.get(candidate.band, 0) + 1

    if args.json:
        print(json.dumps(summarize(result, show_literal=args.show_literal), indent=2))
    else:
        for candidate in result.candidates:
            if candidate.band == CLEAN:
                continue
            literal = (
                candidate.literal if args.show_literal else mask(candidate.literal)
            )
            print(
                "%s:%d %s %s %s"
                % (candidate.path, candidate.line, candidate.band,
                   candidate.score, literal)
            )
        for note in result.unreadable:
            print("unreadable %s" % note)
        print(
            "secret-candidates: %d flagged, %d review, %d clean, %d unreadable"
            % (counts[FLAGGED], counts[REVIEW], counts[CLEAN], len(result.unreadable))
        )

    if counts[FLAGGED]:
        return 1
    if counts[REVIEW] or result.unreadable:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
