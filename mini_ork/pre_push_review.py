"""Canonical pre-push multi-lens code review runtime.

Faithful port of the six public bash functions that implement the Layer 3
multi-lens code reviewer invoked from ``.githooks/pre-push``. Deterministic
heuristics, native LLM-panel dispatch, persistence, verdicts, forwarding, and
the public CLI all live behind this module's API.

Structure (SRP split — implementation lives in focused submodules, every
public name is re-exported here so existing importers are untouched):
  mini_ork/review/common.py   — env resolution + issue row shape helpers
  mini_ork/review/lenses.py   — check_* heuristic lenses + _default_llm_panel
  mini_ork/review/gitdiff.py  — git merge-base / diff subprocess helpers
  mini_ork/review/verdict.py  — compute_verdict / _apply_verdict
  this module                 — review_run orchestrator, read-only
                                subcommands, forwarding, CLI.

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

Internal helpers:
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

Standalone contracts live in ``tests/unit/test_mini_ork_review_py.py`` and
``tests/unit/test_pre_push_review_py.py``.

Schema citations:
  - ``pre_push_reviews``        — db/migrations/0035_pre_push_reviews.sql
  - ``pre_push_review_issues``  — db/migrations/0035_pre_push_reviews.sql
  - ``bug_reports``             — db/migrations/0029_bug_reports.sql
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Callable, Iterable

# Re-import the peer port so review_forward_to_bug_reports can call into
# it without shelling out to bash (parity tests verify the same DB state
# is reached whether we use bash sourcing or in-process Python helpers).
from mini_ork.observability import bug_report as _bug_report

# ── Re-exports: implementation moved to focused submodules (SRP split).
# Every public name (and the private helpers tests reference) keeps its
# original location here so importers are untouched.
from mini_ork.review.common import (  # noqa: F401
    _DESCRIPTION_MAX,
    _SUGGESTED_FIX_MAX,
    _TITLE_MAX,
    _read_diff,
    _resolve_check_path,
    _resolve_db,
    _resolve_home,
    _resolve_root,
    _truncate_issue,
)
from mini_ork.review.gitdiff import (  # noqa: F401
    _compute_base,
    _count_diff_lines,
    _git_diff,
    _git_shortstat,
)
from mini_ork.review.lenses import (  # noqa: F401
    _HEURISTIC_LENSES,
    _REVIEW_PROMPT,
    _SECRET_PATTERNS,
    _default_llm_panel,
    _run_heuristic_lenses,
    check_added_todos,
    check_bash_syntax,
    check_diff_size,
    check_migration_safety,
    check_secret_patterns,
    check_test_pairing,
)
from mini_ork.review.verdict import _apply_verdict, compute_verdict  # noqa: F401

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
    "_default_llm_panel",
]


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
    llm_panel: Callable[[str], list[dict]] | None = None,
) -> int:
    """Mirror bash ``review_run <source_sha> <target_branch> [--mode ...] [--base ...]``.

    Runs the heuristic checks and, for ``mode='llm_panel'|'hybrid'``, the
    native LLM panel. Persists findings, computes a verdict, and returns the
    review id.

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
    if parsed_mode in ("llm_panel", "hybrid"):
        try:
            issues.extend((llm_panel or _default_llm_panel)(diff_text))
        except Exception:
            pass

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
    :func:`mini_ork.observability.bug_report.bug_report_emit` (in-process — no
    shell-out to ``lib/bug_report.sh``) and then run
    :func:`mini_ork.observability.bug_report.bug_report_sweep` with ``--all``.

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
