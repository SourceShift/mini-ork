"""Pre-push code review — Python port of ``lib/pre_push_review.sh``.

Faithful port of the six public bash functions that implement the Layer 3
multi-lens code reviewer invoked from ``.githooks/pre-push``. The bash
``lib/pre_push_review.sh`` stays in place (strangler-fig co-existence);
this module gives Python callers an in-process surface and gives the
parity test a stable target to byte-diff against the live bash subprocess.

Pipeline map (bash function → Python):
  review_run                       → review_run
                                     Creates pre_push_reviews row, runs
                                     the heuristic check_* lenses in fixed
                                     order, persists each issue to
                                     pre_push_review_issues, computes
                                     verdict via compute_verdict, updates
                                     verdict+rationale.
  review_verdict_for <rid>         → review_verdict_for
                                     Prints just the verdict word.
  review_show <rid>                → review_show
                                     Header row + open issues, sorted by
                                     severity DESC, with printf column
                                     widths exactly matching bash.
  review_list [N]                  → review_list
                                     Last N reviews with printf widths.
  review_forward_to_bug_reports    → review_forward_to_bug_reports
                                     Emits one bug_report_emit per open
                                     issue, then bug_report_sweep --all.
                                     Returns the forwarded count.

Internal helpers (mirrors of bash underscore-prefixed functions):
  _check_bash_syntax       → check_bash_syntax
  _check_migration_safety  → check_migration_safety
  _check_added_todos       → check_added_todos
  _check_diff_size         → check_diff_size
  _check_test_pairing      → check_test_pairing
  _check_secret_patterns   → check_secret_patterns

Env knobs honored (also accepted as explicit kwargs):
  * ``MINI_ORK_DB``         — state.db path (default
                                ``${MINI_ORK_HOME:-.mini-ork}/state.db``).
  * ``MINI_ORK_HOME``       — runs root (default ``.mini-ork``).
  * ``MINI_ORK_ROOT``       — repo root (default parent of ``mini_ork/``).

Output format mirrors:
  * ``review_verdict_for`` echoes just the verdict word with a trailing
    newline (matches ``sqlite3 ... ;`` output which always appends ``\\n``).
  * ``review_show`` first emits the header row joined by ``" | "`` with
    a trailing newline, then a blank line, then the per-issue rows joined
    by ``" | "`` with a trailing newline (mirrors ``sqlite3 -separator ' | '``
    plus the bash ``echo`` between the two sections).
  * ``review_list`` emits one row per review joined by ``" | "`` with a
    trailing newline (mirrors ``sqlite3 -separator ' | '``).

Parity is enforced by ``tests/unit/test_mini_ork_review_py.py`` (>=6
cases that drive the LIVE bash subprocess against a temp DB seeded by
``db/init.sh`` and a throwaway git repo, and diff the resulting
``pre_push_reviews`` + ``pre_push_review_issues`` rows + stdout strings
against the Python port byte-for-byte; floats 1e-6; epochs 1-second
window).

Schema citations:
  - ``pre_push_reviews``        — db/migrations/0035_pre_push_reviews.sql
  - ``pre_push_review_issues``  — db/migrations/0035_pre_push_reviews.sql
  - ``bug_reports``             — db/migrations/0029_bug_reports.sql
"""
from __future__ import annotations

import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Iterable

# Re-import the peer port so review_forward_to_bug_reports can call into
# it without shelling out to bash (parity tests verify the same DB state
# is reached whether we use bash sourcing or in-process Python helpers).
from mini_ork.ported import bug_report as _bug_report

__all__ = [
    "review_run",
    "review_verdict_for",
    "review_show",
    "review_list",
    "review_forward_to_bug_reports",
    "compute_verdict",
    "check_bash_syntax",
    "check_migration_safety",
    "check_added_todos",
    "check_diff_size",
    "check_test_pairing",
    "check_secret_patterns",
]


# ─────────────────────────────────────────────────────────────────────────
# Env resolution (mirrors bash lines 26-27)
# ─────────────────────────────────────────────────────────────────────────

def _resolve_db() -> str:
    """Return the state.db path the bash script would pick.

    Resolution order (mirrors ``lib/pre_push_review.sh:27``):
      $MINI_ORK_DB → ${MINI_ORK_HOME:-.mini-ork}/state.db
    """
    env_db = os.environ.get("MINI_ORK_DB")
    if env_db:
        return env_db
    home = os.environ.get("MINI_ORK_HOME") or ".mini-ork"
    return os.path.join(home, "state.db")


def _resolve_home() -> str:
    """Return the .mini-ork home, mirrors ``MINI_ORK_HOME:-.mini-ork``."""
    return os.environ.get("MINI_ORK_HOME") or ".mini-ork"


def _resolve_root() -> str:
    """Return MINI_ORK_ROOT: env var or parent of mini_ork/ package.

    Mirrors bash ``cd "$(dirname "${BASH_SOURCE[0]}")/.."`` semantics
    relative to lib/. The Python package parent (``mini_ork/``) maps to
    the repo root.
    """
    env_root = os.environ.get("MINI_ORK_ROOT")
    if env_root:
        return env_root
    # mini_ork/ported/<this>.py → mini_ork/ported → mini_ork → REPO
    return str(Path(__file__).resolve().parent.parent.parent)


# ─────────────────────────────────────────────────────────────────────────
# Issue row shape helpers (mirrors bash JSONL contract)
# ─────────────────────────────────────────────────────────────────────────

# Field truncations that bash applies at INSERT time (lines 451-454).
_TITLE_MAX = 300
_DESCRIPTION_MAX = 2000
_SUGGESTED_FIX_MAX = 1000


def _truncate_issue(issue: dict) -> dict:
    """Truncate title/description/suggested_fix at the bash contract lengths.

    Mirrors the bash heredoc at ``pre_push_review.sh:451-454``.
    """
    return {
        "lens": issue.get("lens", "?"),
        "severity": issue.get("severity", "medium"),
        "file": issue.get("file") if issue.get("file") is not None else "?",
        "line": issue.get("line"),
        "title": (issue.get("title") or "")[:_TITLE_MAX],
        "description": (issue.get("description") or "")[:_DESCRIPTION_MAX],
        "suggested_fix": (issue.get("suggested_fix") or "")[:_SUGGESTED_FIX_MAX],
    }


def _read_diff(diff_or_path: str | os.PathLike[str]) -> str:
    """Accept either a diff string or a path to a diff file.

    Mirrors the bash convention where each ``_check_*`` reads the diff
    path. Python accepts both so callers (tests, callers) can pass either.
    """
    p = Path(diff_or_path)
    if p.is_file():
        return p.read_text(encoding="utf-8", errors="replace")
    if isinstance(diff_or_path, (str, bytes, bytearray)):
        return diff_or_path if isinstance(diff_or_path, str) else diff_or_path.decode("utf-8", "replace")
    return str(diff_or_path)


def _resolve_check_path(path: str, cwd: str | os.PathLike[str] | None) -> str:
    """Resolve a file path from the diff against the review cwd.

    The bash subprocess reads ``bash -n <file>`` from its cwd; the Python
    port must do the same so file paths in the diff resolve to the same
    on-disk files in both ports.
    """
    if cwd is None or os.path.isabs(path):
        return path
    return os.path.join(str(cwd), path)


# ─────────────────────────────────────────────────────────────────────────
# Heuristic check lenses
# Each check returns a list of dicts with keys {lens, severity, file, line,
# title, description, suggested_fix} — exactly the bash JSONL shape.
# ─────────────────────────────────────────────────────────────────────────

# Mirrors lib/pre_push_review.sh:34-66 (_check_bash_syntax).
def check_bash_syntax(diff_or_path: str | os.PathLike[str],
                      *, cwd: str | os.PathLike[str] | None = None) -> list[dict]:
    """Mirror bash ``_check_bash_syntax``.

    Walks every ``+++ b/<file>`` header in the diff; for each ``.sh`` or
    ``bin/<polyglot>`` file that exists on disk, reads the first line to
    confirm a bash shebang (skip non-bash polyglot scripts so Python CLIs
    in ``bin/`` aren't flagged), then runs ``bash -n`` on the file. A
    non-zero exit emits a critical JSONL row.

    Args:
        diff_or_path: the diff string OR a path to a diff file.
        cwd: working directory used to resolve file paths from the diff
             (mirrors the bash subprocess's cwd). Defaults to the
             current process cwd.
    """
    diff = _read_diff(diff_or_path)
    files: set[str] = set()
    for m in re.finditer(r"^\+\+\+ b/(.+)$", diff, re.M):
        f = m.group(1)
        if f.endswith(".sh") or f.startswith("bin/"):
            files.add(f)
    out: list[dict] = []
    for f in sorted(files):
        # Use the cwd-resolved path for existence + shebang checks so the
        # diff paths resolve to the same on-disk files the bash subprocess
        # sees, but pass the relative path to ``bash -n`` so its error
        # output matches bash byte-for-byte (bash emits the path it was
        # given; absolute paths diverge).
        path = _resolve_check_path(f, cwd)
        if not os.path.isfile(path):
            continue
        try:
            with open(path) as fh:
                first = fh.readline()
        except OSError:
            continue
        if "bash" not in first and not first.endswith("sh\n"):
            continue
        try:
            r = subprocess.run(["bash", "-n", f], cwd=str(cwd) if cwd is not None else None,
                               capture_output=True, text=True)
        except OSError:
            continue
        if r.returncode != 0:
            out.append(_truncate_issue({
                "lens": "heuristic.bash_syntax",
                "severity": "critical",
                "file": f,
                "line": None,
                "title": f"bash syntax error in {f}",
                "description": (r.stderr or "")[:500],
                "suggested_fix": f"Run `bash -n {f}` locally and fix before push.",
            }))
    return out


# Mirrors lib/pre_push_review.sh:68-103 (_check_migration_safety).
def check_migration_safety(diff_or_path: str | os.PathLike[str]) -> list[dict]:
    """Mirror bash ``_check_migration_safety``.

    Walks every ``+++ b/db/migrations/*.sql`` hunk; for each, scans added
    lines. ``DROP TABLE/INDEX/COLUMN/VIEW`` without ``IF EXISTS`` is a
    HIGH finding; ``DELETE FROM`` without ``WHERE`` is a CRITICAL finding.
    """
    diff = _read_diff(diff_or_path)
    out: list[dict] = []
    for m in re.finditer(r"^\+\+\+ b/(db/migrations/[^\n]+\.sql)$", diff, re.M):
        f = m.group(1)
        start = m.end()
        end = diff.find("\ndiff --git ", start)
        if end < 0:
            end = len(diff)
        hunk = diff[start:end]
        for added in re.finditer(r"^\+(.*)$", hunk, re.M):
            line = added.group(1)
            up = line.upper().strip()
            if up.startswith(("DROP TABLE", "DROP INDEX", "DROP COLUMN", "DROP VIEW")):
                if "IF EXISTS" not in up:
                    out.append(_truncate_issue({
                        "lens": "heuristic.migration_safety",
                        "severity": "high",
                        "file": f,
                        "line": None,
                        "title": "DROP without IF EXISTS in migration",
                        "description": f"Line: {line.strip()[:120]}",
                        "suggested_fix": "Add IF EXISTS so repeated runs are idempotent.",
                    }))
            if up.startswith("DELETE FROM") and "WHERE" not in up:
                out.append(_truncate_issue({
                    "lens": "heuristic.migration_safety",
                    "severity": "critical",
                    "file": f,
                    "line": None,
                    "title": "Unbounded DELETE in migration",
                    "description": f"Line: {line.strip()[:120]}",
                    "suggested_fix": "Add a WHERE clause or scope the delete.",
                }))
    return out


# Mirrors lib/pre_push_review.sh:105-129 (_check_added_todos).
def check_added_todos(diff_or_path: str | os.PathLike[str]) -> list[dict]:
    """Mirror bash ``_check_added_todos``.

    Counts added lines containing TODO/FIXME/HACK/XXX/KLUDGE; emits at
    most 5 low-severity rows.
    """
    diff = _read_diff(diff_or_path)
    added = 0
    current_file: str | None = None
    out: list[dict] = []
    for line in diff.split("\n"):
        m = re.match(r"^\+\+\+ b/(.+)$", line)
        if m:
            current_file = m.group(1)
            continue
        if not line.startswith("+") or line.startswith("+++"):
            continue
        if re.search(r"\b(TODO|FIXME|HACK|XXX|KLUDGE)\b", line):
            added += 1
            if added <= 5:
                out.append(_truncate_issue({
                    "lens": "heuristic.todo_marker",
                    "severity": "low",
                    "file": current_file or "?",
                    "line": None,
                    "title": "New TODO/FIXME/HACK added",
                    "description": line.strip()[:200],
                    "suggested_fix": "Resolve in this PR or open an explicit issue.",
                }))
    return out


# Mirrors lib/pre_push_review.sh:131-147 (_check_diff_size).
def check_diff_size(diff_or_path: str | os.PathLike[str]) -> list[dict]:
    """Mirror bash ``_check_diff_size``.

    Counts ``+`` (excluding ``+++``) and ``-`` (excluding ``---``)
    lines. When added >= 800, emits a medium-severity row.
    """
    diff = _read_diff(diff_or_path)
    # Mirror bash ``grep -cE "^\+[^+]"`` / ``"^-[^-]"`` exactly.
    added = sum(1 for line in diff.split("\n") if re.match(r"^\+[^+]", line))
    removed = sum(1 for line in diff.split("\n") if re.match(r"^-[^-]", line))
    if added >= 800:
        return [_truncate_issue({
            "lens": "heuristic.diff_size",
            "severity": "medium",
            "file": "_diff",
            "line": None,
            "title": f"Large diff: +{added} / -{removed} lines",
            "description": "Big diffs are harder to review; consider splitting.",
            "suggested_fix": "Split into smaller logical commits where possible.",
        })]
    return []


# Mirrors lib/pre_push_review.sh:149-177 (_check_test_pairing).
def check_test_pairing(diff_or_path: str | os.PathLike[str]) -> list[dict]:
    """Mirror bash ``_check_test_pairing``.

    Detects NEW ``lib/`` or ``bin/`` ``.sh`` files that arrive without a
    paired ``tests/`` change. The "new file" heuristic matches bash:
    scans 500 chars before the ``+++ b/<file>`` header for the literal
    ``new file mode`` marker emitted by git diff for newly added files.
    """
    diff = _read_diff(diff_or_path)
    new_lib_bin: set[str] = set()
    new_tests: set[str] = set()
    for m in re.finditer(r"^\+\+\+ b/(.+)$", diff, re.M):
        f = m.group(1)
        if (f.startswith("lib/") or f.startswith("bin/")) and f.endswith(".sh"):
            start = m.end()
            end = diff.find("\ndiff --git ", start)
            if end < 0:
                end = len(diff)
            # Look at the 500 chars before the +++ b/ header for the
            # `new file mode` marker (this is the diff metadata block).
            if "new file mode" in diff[max(0, start - 500):start]:
                new_lib_bin.add(f)
        if f.startswith("tests/"):
            new_tests.add(f)
    if new_lib_bin and not new_tests:
        return [_truncate_issue({
            "lens": "heuristic.test_pairing",
            "severity": "low",
            "file": ",".join(sorted(new_lib_bin))[:200],
            "line": None,
            "title": f"{len(new_lib_bin)} new lib/bin file(s) without paired test changes",
            "description": "New executable code added but no tests/ files changed.",
            "suggested_fix": "Add at least one smoke test in tests/integration/ or tests/unit/.",
        })]
    return []


# Mirrors lib/pre_push_review.sh:179-209 (_check_secret_patterns).
_SECRET_PATTERNS: list[tuple[str, str]] = [
    (r"AKIA[0-9A-Z]{16}", "AWS access key"),
    (r"-----BEGIN (RSA|EC|OPENSSH) PRIVATE KEY-----", "private key"),
    (r"ghp_[A-Za-z0-9]{30,}", "GitHub PAT"),
    (r"sk-[A-Za-z0-9]{20,}", "OpenAI-style API key"),
    (r"xoxb-\d+-\d+-", "Slack bot token"),
]


def check_secret_patterns(diff_or_path: str | os.PathLike[str]) -> list[dict]:
    """Mirror bash ``_check_secret_patterns``.

    Walks every added line; matches each regex in turn. Returns on the
    FIRST match (mirrors the bash ``return`` after emitting one issue).
    """
    diff = _read_diff(diff_or_path)
    current_file: str | None = None
    for line in diff.split("\n"):
        m = re.match(r"^\+\+\+ b/(.+)$", line)
        if m:
            current_file = m.group(1)
            continue
        if not line.startswith("+") or line.startswith("+++"):
            continue
        for pat, name in _SECRET_PATTERNS:
            if re.search(pat, line):
                return [_truncate_issue({
                    "lens": "heuristic.secret_leak",
                    "severity": "critical",
                    "file": current_file or "?",
                    "line": None,
                    "title": f"Possible {name} added to repo",
                    "description": "Matched pattern: " + pat,
                    "suggested_fix": "Remove the secret + rotate the credential immediately.",
                })]
    return []


# Order in which review_run iterates the heuristic lenses. Mirrors the
# bash heredoc at pre_push_review.sh:415-421 (the order of calls inside
# the { } block).
_HEURISTIC_LENSES: list[tuple[str, Callable[..., list[dict]]]] = [
    ("check_bash_syntax", check_bash_syntax),
    ("check_migration_safety", check_migration_safety),
    ("check_added_todos", check_added_todos),
    ("check_diff_size", check_diff_size),
    ("check_test_pairing", check_test_pairing),
    ("check_secret_patterns", check_secret_patterns),
]


def _run_heuristic_lenses(diff_text: str, cwd: str | os.PathLike[str] | None) -> list[dict]:
    """Run every heuristic lens in the canonical order, threading ``cwd``.

    Mirrors the bash block at pre_push_review.sh:415-421.
    """
    issues: list[dict] = []
    for _, fn in _HEURISTIC_LENSES:
        try:
            issues.extend(fn(diff_text, cwd=cwd))
        except TypeError:
            # Lens does not accept cwd (e.g. test stub); fall back.
            issues.extend(fn(diff_text))
        except Exception:
            # Mirrors bash `|| true` on the heuristic block — never let
            # a single check crash the whole review.
            continue
    return issues


# ─────────────────────────────────────────────────────────────────────────
# Git diff helpers
# ─────────────────────────────────────────────────────────────────────────

def _compute_base(source_sha: str, target_branch: str, cwd: str | os.PathLike[str]) -> str:
    """Mirror the bash merge-base fallback chain at pre_push_review.sh:370-378.

    Order:
      1. ``origin/<target_branch>``
      2. ``main``
      3. ``<source_sha>^`` (parent) — only if the prior attempts returned
         empty (no common ancestor at all).
    """
    try:
        r = subprocess.run(
            ["git", "merge-base", source_sha, f"origin/{target_branch}"],
            cwd=str(cwd), capture_output=True, text=True,
        )
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
        r = subprocess.run(
            ["git", "merge-base", source_sha, "main"],
            cwd=str(cwd), capture_output=True, text=True,
        )
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except OSError:
        pass
    try:
        r = subprocess.run(
            ["git", "rev-parse", f"{source_sha}^"],
            cwd=str(cwd), capture_output=True, text=True,
        )
        if r.returncode == 0:
            return r.stdout.strip()
    except OSError:
        pass
    return ""


def _git_diff(base: str, source_sha: str, cwd: str | os.PathLike[str]) -> str:
    """Mirror ``git diff $base..$source_sha``. Falls back to ``git show``."""
    try:
        if base:
            r = subprocess.run(
                ["git", "diff", f"{base}..{source_sha}"],
                cwd=str(cwd), capture_output=True, text=True,
            )
        else:
            r = subprocess.run(
                ["git", "show", source_sha],
                cwd=str(cwd), capture_output=True, text=True,
            )
        return r.stdout if r.returncode == 0 else ""
    except OSError:
        return ""


def _git_shortstat(base: str, source_sha: str, cwd: str | os.PathLike[str]) -> str:
    """Mirror ``git diff --shortstat``."""
    try:
        r = subprocess.run(
            ["git", "diff", "--shortstat", f"{base}..{source_sha}"],
            cwd=str(cwd), capture_output=True, text=True,
        )
        return r.stdout if r.returncode == 0 else ""
    except OSError:
        return ""


def _count_diff_lines(diff_text: str, *, base: str, source_sha: str, cwd: str | os.PathLike[str]) -> tuple[int, int, int]:
    """Return (files_changed, lines_added, lines_removed).

    Mirrors the bash sequence at pre_push_review.sh:389-397:
      * files_changed from ``grep -oE '[0-9]+ file'`` on ``--shortstat``
        (first match; falls back to 0 if absent).
      * lines_added from ``grep -cE "^\\+[^+]"`` (a plain integer, no
        ``|| echo 0`` chaining).
      * lines_removed from ``grep -cE "^-[^-]"``.
    """
    shortstat = _git_shortstat(base, source_sha, cwd)
    m = re.search(r"(\d+) file", shortstat or "")
    files_changed = int(m.group(1)) if m else 0
    # Count directly from the diff text to mirror the grep -cE semantics.
    # bash uses ``grep -cE "^\+[^+]"`` (and the ``-`` analog): the regex
    # requires a non-``+`` character immediately after the leading ``+``,
    # so a bare ``+`` line (often a blank-context marker) is NOT counted.
    # Mirror that exactly with ``re.match`` instead of ``startswith``.
    lines_added = sum(
        1 for line in diff_text.split("\n")
        if re.match(r"^\+[^+]", line)
    )
    lines_removed = sum(
        1 for line in diff_text.split("\n")
        if re.match(r"^-[^-]", line)
    )
    return files_changed, lines_added, lines_removed


# ─────────────────────────────────────────────────────────────────────────
# Verdict policy
# ─────────────────────────────────────────────────────────────────────────

# Mirrors lib/pre_push_review.sh:459-522 (compute_verdict / verdict UPDATE).
def compute_verdict(rid: int, target_branch: str, *, db: str | None = None) -> tuple[str, str]:
    """Mirror the bash verdict-policy heredoc at lines 459-522.

    Returns ``(verdict, rationale)`` where verdict ∈
    ``{approve, warn, block}``. The rationale format is
    ``"target=<t> crit=<c> high=<h> blocking=<b> (heuristic=<hh>, consensus=<ch>) total=<n>"``
    — identical to the bash UPDATE.

    Args:
        rid: review id to compute verdict for.
        target_branch: target branch name (``main`` / ``master`` / feature).
        db: explicit state.db path; defaults to :func:`_resolve_db`.
    """
    if db is None:
        db = _resolve_db()
    con = sqlite3.connect(db)
    try:
        con.execute("PRAGMA busy_timeout=5000")
        cur = con.execute(
            "SELECT COUNT(*) FROM pre_push_review_issues "
            "WHERE review_id=? AND severity='critical' AND status='open'",
            (rid,),
        ).fetchone()[0]
        critical = int(cur)
        cur = con.execute(
            "SELECT COUNT(*) FROM pre_push_review_issues "
            "WHERE review_id=? AND severity='high' AND status='open'",
            (rid,),
        ).fetchone()[0]
        high = int(cur)
        cur = con.execute(
            "SELECT COUNT(*) FROM pre_push_review_issues "
            "WHERE review_id=? AND status='open'",
            (rid,),
        ).fetchone()[0]
        total = int(cur)
        cur = con.execute(
            "SELECT COUNT(*) FROM pre_push_review_issues "
            "WHERE review_id=? AND severity='high' AND status='open' "
            "AND lens LIKE 'heuristic.%'",
            (rid,),
        ).fetchone()[0]
        heuristic_high = int(cur)
        cur = con.execute(
            "SELECT COUNT(*) FROM ("
            "  SELECT file_path FROM pre_push_review_issues "
            "  WHERE review_id=? AND severity='high' AND status='open' "
            "    AND lens LIKE 'llm.%' AND file_path IS NOT NULL "
            "  GROUP BY file_path HAVING COUNT(DISTINCT lens) >= 2"
            ")",
            (rid,),
        ).fetchone()[0]
        consensus_high = int(cur)
    finally:
        con.close()

    blocking_high = heuristic_high + consensus_high
    to_main = target_branch in ("main", "master")
    if critical > 0:
        verdict = "block"
    elif blocking_high > 0:
        verdict = "block" if to_main else "warn"
    elif high > 0 or total > 5:
        verdict = "warn"
    else:
        verdict = "approve"

    rationale = (
        f"target={target_branch} crit={critical} high={high} "
        f"blocking={blocking_high} (heuristic={heuristic_high}, "
        f"consensus={consensus_high}) total={total}"
    )
    return verdict, rationale


def _apply_verdict(rid: int, target_branch: str, db: str) -> None:
    """Compute + persist verdict for ``rid``. Mirrors the bash UPDATE."""
    verdict, rationale = compute_verdict(rid, target_branch, db=db)
    con = sqlite3.connect(db)
    try:
        con.execute("PRAGMA busy_timeout=5000")
        cur = con.execute(
            "SELECT COUNT(*) FROM pre_push_review_issues "
            "WHERE review_id=? AND status='open'",
            (rid,),
        ).fetchone()[0]
        issues_open = int(cur)
        cur = con.execute(
            "SELECT COUNT(*) FROM pre_push_review_issues "
            "WHERE review_id=? AND severity='critical' AND status='open'",
            (rid,),
        ).fetchone()[0]
        issues_critical = int(cur)
        con.execute(
            "UPDATE pre_push_reviews "
            "   SET verdict=?, issues_open=?, issues_critical=?, rationale=? "
            " WHERE id=?",
            (verdict, issues_open, issues_critical, rationale, rid),
        )
        con.commit()
    finally:
        con.close()


# ─────────────────────────────────────────────────────────────────────────
# review_run orchestrator
# ─────────────────────────────────────────────────────────────────────────

# Mirrors lib/pre_push_review.sh:350-526 (review_run).
def review_run(
    source_sha: str,
    target_branch: str,
    *args: str,
    mode: str = "heuristic",
    base: str = "",
    cwd: str | os.PathLike[str] | None = None,
    db: str | None = None,
) -> int:
    """Mirror bash ``review_run <source_sha> <target_branch> [--mode ...] [--base ...]``.

    Runs the heuristic checks (and, for ``mode='llm_panel'|'hybrid'``, the
    LLM panel — INTENTIONALLY NOT ported at parity depth; callers must
    invoke bash for that branch). Persists findings, computes verdict,
    returns the review_id.

    Args:
        source_sha: local commit SHA being pushed.
        target_branch: target branch (e.g. ``main``, ``master``, ``feature/foo``).
        *args: positional flags mirroring bash (``--mode X``, ``--base X``);
               the explicit kwargs take precedence.
        mode: reviewer mode (``heuristic`` / ``llm_panel`` / ``hybrid``).
        base: override the merge-base fallback chain with a specific base SHA.
        cwd: working directory for git commands (defaults to ``MINI_ORK_ROOT``
             or the parent of the package).
        db: explicit state.db path; defaults to :func:`_resolve_db`.

    Returns:
        The integer review_id of the newly inserted row.

    Raises:
        ValueError: when ``source_sha`` or ``target_branch`` is missing.
    """
    if not source_sha:
        raise ValueError("source_sha required")
    if not target_branch:
        raise ValueError("target_branch required")

    # Mirror bash arg parsing for positional flags.
    parsed_mode = mode
    parsed_base = base
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--mode" and i + 1 < len(args):
            parsed_mode = args[i + 1]
            i += 2
            continue
        if a == "--base" and i + 1 < len(args):
            parsed_base = args[i + 1]
            i += 2
            continue
        i += 1

    if db is None:
        db = _resolve_db()
    if cwd is None:
        cwd = _resolve_root()

    # Compute the diff (mirrors bash lines 366-386).
    if parsed_base:
        base_sha = parsed_base
    else:
        base_sha = _compute_base(source_sha, target_branch, cwd)
    diff_text = _git_diff(base_sha, source_sha, cwd)

    files_changed, lines_added, lines_removed = _count_diff_lines(
        diff_text, base=base_sha, source_sha=source_sha, cwd=cwd,
    )

    # Insert the review row (mirrors bash INSERT at lines 401-407).
    now = int(time.time())
    con = sqlite3.connect(db)
    try:
        con.execute("PRAGMA busy_timeout=5000")
        cur = con.execute(
            """INSERT INTO pre_push_reviews
               (reviewed_at, source_sha, target_branch, reviewer_mode,
                files_changed, lines_added, lines_removed, verdict)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')""",
            (now, source_sha, target_branch, parsed_mode,
             files_changed, lines_added, lines_removed),
        )
        con.commit()
        # sqlite3 returns Optional[int] for lastrowid but the value is
        # always set after a successful INSERT — narrow explicitly.
        assert cur.lastrowid is not None
        rid = int(cur.lastrowid)
    finally:
        con.close()

    # Run heuristic lenses (mirrors bash heredoc at lines 414-430).
    issues = _run_heuristic_lenses(diff_text, cwd)

    # Persist issues (mirrors bash INSERT at lines 445-454).
    con = sqlite3.connect(db)
    try:
        con.execute("PRAGMA busy_timeout=5000")
        for d in issues:
            con.execute(
                """INSERT INTO pre_push_review_issues
                   (review_id, lens, severity, file_path, line_no,
                    title, description, suggested_fix, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open')""",
                (rid,
                 d.get("lens", "?"),
                 d.get("severity", "medium"),
                 d.get("file", "?"),
                 d.get("line"),
                 (d.get("title") or "")[:_TITLE_MAX],
                 (d.get("description") or "")[:_DESCRIPTION_MAX],
                 (d.get("suggested_fix") or "")[:_SUGGESTED_FIX_MAX]),
            )
        con.commit()
    finally:
        con.close()

    # Compute + persist verdict.
    _apply_verdict(rid, target_branch, db)

    return rid


# ─────────────────────────────────────────────────────────────────────────
# Read-only subcommands
# ─────────────────────────────────────────────────────────────────────────

# Mirrors lib/pre_push_review.sh:528-531 (review_verdict_for).
def review_verdict_for(rid: int | None, *, db: str | None = None) -> str:
    """Mirror bash ``review_verdict_for <rid>``.

    Returns just the verdict word with a trailing newline (matches
    ``sqlite3 ... ;`` which always appends ``\\n``).

    Args:
        rid: review id to look up.
        db: explicit state.db path; defaults to :func:`_resolve_db`.

    Raises:
        ValueError: when ``rid`` is not provided or no row exists.
    """
    if rid is None:
        raise ValueError("review_id required")
    if db is None:
        db = _resolve_db()
    con = sqlite3.connect(db)
    try:
        con.execute("PRAGMA busy_timeout=5000")
        row = con.execute(
            "SELECT verdict FROM pre_push_reviews WHERE id=?",
            (int(rid),),
        ).fetchone()
    finally:
        con.close()
    if row is None:
        raise ValueError(f"review_verdict_for: no row for id={rid}")
    return f"{row[0]}\n"


# Mirrors lib/pre_push_review.sh:533-550 (review_show).
def review_show(rid: int | None, *, db: str | None = None) -> str:
    """Mirror bash ``review_show <rid>``.

    Returns:
        ``header\\n\\nissues\\n`` where ``header`` is the ``verdict |
        files_changed | lines_added | lines_removed | issues_open |
        issues_critical | rationale`` row, and ``issues`` is one row per
        open issue formatted with the bash printf widths
        (``%-9s`` ``%-25s`` ``%-30s`` then ``substr(title,1,70)``),
        sorted by severity DESC. Rows are joined by ``" | "``.

    Args:
        rid: review id.
        db: explicit state.db path; defaults to :func:`_resolve_db`.

    Raises:
        ValueError: when ``rid`` is not provided or no row exists.
    """
    if rid is None:
        raise ValueError("review_id required")
    if db is None:
        db = _resolve_db()
    con = sqlite3.connect(db)
    try:
        con.execute("PRAGMA busy_timeout=5000")
        row = con.execute(
            "SELECT verdict, files_changed, lines_added, lines_removed, "
            "       issues_open, issues_critical, rationale "
            "  FROM pre_push_reviews WHERE id=?",
            (int(rid),),
        ).fetchone()
        if row is None:
            raise ValueError(f"review_show: no row for id={rid}")
        header = " | ".join("" if v is None else str(v) for v in row)

        issue_rows = con.execute(
            "SELECT printf('%-9s', severity), "
            "       printf('%-25s', substr(lens,1,25)), "
            "       printf('%-30s', substr(COALESCE(file_path,'?'),1,30)), "
            "       substr(title,1,70) "
            "  FROM pre_push_review_issues "
            " WHERE review_id=? AND status='open' "
            " ORDER BY CASE severity WHEN 'critical' THEN 4 "
            "                       WHEN 'high'     THEN 3 "
            "                       WHEN 'medium'   THEN 2 "
            "                       WHEN 'low'      THEN 1 ELSE 0 END DESC",
            (int(rid),),
        ).fetchall()
    finally:
        con.close()

    out_lines = [header + "\n", "\n"]
    for ir in issue_rows:
        out_lines.append(" | ".join(str(v) for v in ir) + "\n")
    return "".join(out_lines)


# Mirrors bin/mini-ork-review:46-53 (`list` subcommand).
def review_list(n: int = 10, *, db: str | None = None) -> str:
    """Mirror bash ``review list [N]``.

    Returns the last N reviews formatted with the bash printf widths
    (``%-4d`` ``<localtime>`` ``%-8s`` ``%-25s`` then
    ``"%4d issues / %d critical"``), joined by ``" | "``.

    Args:
        n: limit (default 10; matches bash default ``${1:-10}``).
        db: explicit state.db path; defaults to :func:`_resolve_db`.
    """
    if db is None:
        db = _resolve_db()
    con = sqlite3.connect(db)
    try:
        con.execute("PRAGMA busy_timeout=5000")
        rows = con.execute(
            "SELECT printf('%-4d', id), "
            "       datetime(reviewed_at,'unixepoch','localtime'), "
            "       printf('%-8s', verdict), "
            "       printf('%-25s', substr(target_branch,1,25)), "
            "       printf('%4d issues / %d critical', issues_open, issues_critical) "
            "  FROM pre_push_reviews "
            " ORDER BY reviewed_at DESC LIMIT ?",
            (int(n),),
        ).fetchall()
    finally:
        con.close()
    return "".join(" | ".join(str(v) for v in r) + "\n" for r in rows)


# Mirrors lib/pre_push_review.sh:554-581 (review_forward_to_bug_reports).
def review_forward_to_bug_reports(
    rid: int | None,
    *,
    db: str | None = None,
    home: str | None = None,
) -> int:
    """Mirror bash ``review_forward_to_bug_reports <rid>``.

    For each open issue on the review, emit a ``bug_report`` via
    :func:`mini_ork.ported.bug_report.bug_report_emit` (in-process — no
    shell-out to ``lib/bug_report.sh``) and then run
    :func:`mini_ork.ported.bug_report.bug_report_sweep` with ``--all``.

    Returns:
        Number of issues forwarded.

    Args:
        rid: review id whose open issues should be forwarded.
        db: explicit state.db path; defaults to :func:`_resolve_db`.
        home: explicit ``MINI_ORK_HOME`` override; defaults to
              :func:`_resolve_home`.
    """
    if rid is None:
        raise ValueError("review_id required")
    if db is None:
        db = _resolve_db()
    if home is None:
        home = _resolve_home()

    con = sqlite3.connect(db)
    try:
        con.execute("PRAGMA busy_timeout=5000")
        rows = con.execute(
            "SELECT id, lens, severity, COALESCE(file_path,''), title, "
            "       COALESCE(description,''), COALESCE(suggested_fix,'') "
            "  FROM pre_push_review_issues "
            " WHERE review_id=? AND status='open'",
            (int(rid),),
        ).fetchall()
    finally:
        con.close()

    if not rows:
        return 0

    # Mirror bash: write to ${MINI_ORK_HOME}/runs/review-$rid/ so the
    # sweep picks up the sink. Use the env override below so the in-process
    # bug_report_emit lands in the right dir.
    run_dir = Path(home) / "runs" / f"review-{rid}"
    run_dir.mkdir(parents=True, exist_ok=True)
    prev_run_dir = os.environ.get("MINI_ORK_RUN_DIR")
    os.environ["MINI_ORK_RUN_DIR"] = str(run_dir)
    try:
        for _, lens, sev, file_path, title, desc, fix in rows:
            _bug_report.bug_report_emit(
                f"review.{lens}",
                sev,
                title,
                desc,
                fix,
                file_path or "general",
                0.85,
                run_dir=str(run_dir),
            )
        _bug_report.bug_report_sweep("--all", home=home)
    finally:
        # Restore env so we don't leak into other tests.
        if prev_run_dir is None:
            os.environ.pop("MINI_ORK_RUN_DIR", None)
        else:
            os.environ["MINI_ORK_RUN_DIR"] = prev_run_dir
    return len(rows)


# ─────────────────────────────────────────────────────────────────────────
# Module CLI dispatcher (mirrors bin/mini-ork-review lines 38-57 for Python callers)
# ─────────────────────────────────────────────────────────────────────────

def run_cli(argv: Iterable[str] | None = None) -> str:
    """Argparse-free dispatcher mirroring ``bin/mini-ork-review``.

    Subcommands:
      * ``run <sha> <branch> [--mode ...] [--base ...]`` → review_run, prints id.
      * ``show <rid>`` → review_show, prints text.
      * ``verdict <rid>`` → review_verdict_for, prints verdict.
      * ``forward <rid>`` → review_forward_to_bug_reports, prints count.
      * ``list [N]`` → review_list, prints text.

    Returns the stdout string the bash CLI would have produced.
    """
    if argv is None:
        argv = ()
    tokens = list(argv)
    if not tokens:
        return ""
    sub, rest = tokens[0], tokens[1:]
    if sub == "run":
        if len(rest) < 2:
            raise ValueError("run <source_sha> <target_branch> [--mode X] [--base X]")
        sha, branch = rest[0], rest[1]
        flags = list(rest[2:])
        kw: dict[str, Any] = {}
        i = 0
        while i < len(flags):
            if flags[i] == "--mode" and i + 1 < len(flags):
                kw["mode"] = flags[i + 1]
                i += 2
                continue
            if flags[i] == "--base" and i + 1 < len(flags):
                kw["base"] = flags[i + 1]
                i += 2
                continue
            i += 1
        return f"{review_run(sha, branch, *flags, **kw)}\n"
    if sub == "show":
        return review_show(int(rest[0]))
    if sub == "verdict":
        return review_verdict_for(int(rest[0]))
    if sub == "forward":
        return f"{review_forward_to_bug_reports(int(rest[0]))}\n"
    if sub == "list":
        n = int(rest[0]) if rest else 10
        return review_list(n)
    raise ValueError(f"review: unknown subcommand {sub}")


_USAGE = """Usage: mini-ork review <subcommand> [args]

  run <source_sha> <target_branch> [--mode heuristic|llm_panel]
  show <review_id>
  verdict <review_id>
  forward <review_id>
  list [N]
"""


def main(argv: list[str] | None = None) -> int:
    """CLI entry mirroring bin/mini-ork-review's case dispatch.

    empty / help / --help / -h → usage on stdout, rc 0.
    unknown subcommand         → error on stderr + usage on stdout, rc 2.
    known subcommand           → run_cli output on stdout, rc 0.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("help", "--help", "-h"):
        sys.stdout.write(_USAGE)
        return 0
    try:
        sys.stdout.write(run_cli(args))
    except ValueError as e:
        sys.stderr.write(f"{e}\n")
        sys.stdout.write(_USAGE)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())