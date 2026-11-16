"""Every plausible string literal is promoted to a candidate secret.

The score is a fixed, inspectable formula rather than a learned classifier.
The uncertain middle band abstains to review rather than passing as clean.
"""

from __future__ import annotations

import argparse
import ast
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


class Candidate(NamedTuple):
    path: str
    line: int
    literal: str
    score: float
    band: str
    reasons: tuple[str, ...]


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
    """``(lineno, column, literal)`` triples at or above ``LITERAL_MIN``."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return _extract_literals_regex(text)

    triples: list[tuple[int, int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
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


def scan_file(root: str, rel: str) -> list[Candidate]:
    """Candidates for one file, skipping missing, binary and oversized paths."""
    path = os.path.join(root, rel)
    if not os.path.exists(path) or not os.path.isfile(path):
        return []
    try:
        if os.path.getsize(path) > MAX_FILE_BYTES:
            return []
        if os.path.splitext(rel)[1].lower() in BINARY_EXTENSIONS:
            return []
        with open(path, "rb") as handle:
            data = handle.read()
    except OSError:
        return []
    if b"\x00" in data[:_NUL_SCAN_BYTES]:
        return []
    return scan_text(rel, data.decode("utf-8", errors="replace"))


def scan(root: str, paths: list[str]) -> list[Candidate]:
    """Every candidate across ``paths``, sorted by ``(path, line)``."""
    candidates: list[Candidate] = []
    for rel in paths:
        candidates.extend(scan_file(root, rel))
    candidates.sort(key=lambda candidate: (candidate.path, candidate.line))
    return candidates


def summarize(candidates: list[Candidate]) -> dict:
    """Band counts plus each candidate, with the literal reported in full."""
    counts: dict[str, int] = {name: 0 for name in BANDS}
    entries: list[dict] = []
    for candidate in candidates:
        counts[candidate.band] = counts.get(candidate.band, 0) + 1
        entries.append({
            "path": candidate.path,
            "line": candidate.line,
            "literal": candidate.literal,
            "score": candidate.score,
            "band": candidate.band,
            "reasons": list(candidate.reasons),
        })
    return {"counts": counts, "candidates": entries}


def _split_paths(raw: str) -> list[str]:
    return [part.strip() for part in re.split(r"[,\n]", raw or "") if part.strip()]


def main(argv: list[str] | None = None) -> int:
    """Exit ``1`` when flagged, ``3`` when only the review band abstains, else 0."""
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
    args = parser.parse_args(argv)

    raw = args.paths
    if raw is None:
        raw = os.environ.get("MINI_ORK_TOUCHED_FILES", "")
    paths = _split_paths(raw)
    if not paths:
        parser.error("no paths to scan: pass --paths or set MINI_ORK_TOUCHED_FILES")

    candidates = scan(args.root, paths)
    counts: dict[str, int] = {name: 0 for name in BANDS}
    for candidate in candidates:
        counts[candidate.band] = counts.get(candidate.band, 0) + 1

    if args.json:
        print(json.dumps(summarize(candidates), indent=2))
    else:
        for candidate in candidates:
            if candidate.band == CLEAN:
                continue
            print(
                "%s:%d %s %s %s"
                % (
                    candidate.path,
                    candidate.line,
                    candidate.band,
                    candidate.score,
                    candidate.literal,
                )
            )
        print(
            "secret-candidates: %d flagged, %d review, %d clean"
            % (counts[FLAGGED], counts[REVIEW], counts[CLEAN])
        )

    if counts[FLAGGED]:
        return 1
    if counts[REVIEW]:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
