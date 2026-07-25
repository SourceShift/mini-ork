"""Heuristic check lenses + native LLM review panel for the review runtime.

Extracted from ``mini_ork/pre_push_review.py`` (parity port of
``lib/pre_push_review.sh``). Each check returns a list of dicts with keys
{lens, severity, file, line, title, description, suggested_fix} — exactly
the bash JSONL shape.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Callable

from mini_ork.review.common import _read_diff, _resolve_check_path, _truncate_issue

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


_REVIEW_PROMPT = """You are a code reviewer for the mini-ork project. Review the unified diff
below and identify CONCRETE issues only. Do NOT critique style preferences
or speculate. Focus on runtime bugs, security concerns, test gaps, migration
safety, and architectural problems specific to the change.

Return ONLY a JSON object with this exact shape (no prose, no markdown):
{"issues":[{"severity":"low|medium|high|critical","file":"path/to/file","line":null,"title":"summary","description":"problem","suggested_fix":"fix"}]}
If you find no issues, return {"issues":[]}.

--- DIFF FOLLOWS ---
"""


def _default_llm_panel(
    diff_text: str,
    *,
    dispatch_fn: Callable[..., int] | None = None,
) -> list[dict]:
    """Run the configured review panel through the native dispatcher."""
    from mini_ork.dispatch import llm_dispatch

    dispatch_fn = dispatch_fn or llm_dispatch.mo_llm_dispatch
    panel = os.environ.get("MO_REVIEW_PANEL", "codex kimi glm").split()
    timeout = os.environ.get("MO_REVIEW_LENS_TIMEOUT_S", "180")
    output: list[dict] = []
    for model in panel:
        if model == "gemini" or model.startswith("gemini-") or "-gemini-" in model:
            continue
        with tempfile.NamedTemporaryFile("w", suffix=".out", delete=False) as handle:
            out_file = handle.name
        try:
            try:
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    rc = dispatch_fn(model, _REVIEW_PROMPT + diff_text, out_file, timeout, 4)
            except Exception:
                rc = 1
            if rc != 0:
                continue
            try:
                raw = Path(out_file).read_text()
            except OSError:
                continue
            payload = None
            try:
                payload = json.loads(raw)
            except Exception:
                match = re.search(r"\{[\s\S]*\}", raw)
                if match:
                    try:
                        payload = json.loads(match.group(0))
                    except Exception:
                        payload = None
            if not isinstance(payload, dict) or not isinstance(payload.get("issues", []), list):
                continue
            emitted = 0
            for issue in payload.get("issues", []):
                if not isinstance(issue, dict):
                    continue
                severity = str(issue.get("severity") or "medium").lower()
                if severity not in {"info", "low", "medium", "high", "critical"}:
                    severity = "medium"
                title = str(issue.get("title") or "").strip()[:300]
                if not title:
                    continue
                output.append({
                    "lens": f"llm.{model}",
                    "severity": severity,
                    "file": str(issue.get("file") or "?")[:200],
                    "line": issue.get("line"),
                    "title": title,
                    "description": str(issue.get("description") or "")[:1000],
                    "suggested_fix": str(issue.get("suggested_fix") or "")[:500],
                })
                emitted += 1
                if emitted >= 8:
                    break
        finally:
            for path in (out_file, out_file + ".err.log"):
                try:
                    os.remove(path)
                except OSError:
                    pass
    return output
