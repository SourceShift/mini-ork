"""Your code tab — what reviews found in *your* files, area by area.

Every reviewer and verifier finding mini-ork has harvested lives in the
``code_findings`` tables. This tab turns that pile into an answer to the
engineer's question: "what does mini-ork keep finding in MY code, and how do I
make it stick?"

Two ideas carry it:

1. **Areas.** Findings are grouped by the file/directory they touch
   (``code_findings.areas``), so one glance shows which part of the tree
   attracts the most (and the worst) findings.
2. **Recurring problems.** The keyword ``category`` is weak (113 of node.py's
   146 live findings are ``other``), so ``recurring()`` clusters the findings'
   ``issue`` texts by TF-IDF cosine similarity. The same mistake written in
   five files collapses into ONE cluster the operator can act on — and the
   action drafts a ``prefs set --scope path ... --origin learned --draft`` rule
   for exactly the files the cluster was found in. It is INACTIVE: nothing
   reaches an agent until it is approved on the Rules tab, and it renders under
   the "Learned rules" heading, never "set by the user".

   Recurrence is counted in KICKOFFS, not rows (LF-R10): a cluster is recurring
   only when the problem showed up in at least two distinct kickoffs and in at
   least two ``(kickoff, file)`` pairs. Four restatements of one bug inside one
   run are an emphasis, not a recurrence — they are listed under "Seen in one
   kickoff" by ``seen_once()`` and offer no action. A kickoff is identified by
   ``context_v2.kickoff_stem`` (revision suffixes ``-rN`` collapse, so
   ``ide-node-stream-r3`` and ``-r4`` are ONE kickoff) and falls back to the run
   id for generic kickoff names, which carry no identity of their own.

The page is strictly read-only: it never calls ``harvest`` (reflect is the sole
writer), and every statement is a SELECT. A bare home degrades to an empty
state, never a traceback (each section is wrapped in ``S.guarded``).
"""
from __future__ import annotations

import hashlib
import os
import re
import time
from pathlib import Path
from typing import Any

from mini_ork.ide_pages import spec as S
from mini_ork.ide_pages.learn._common import short

# The TF-IDF primitives live in ``mini_ork.learning.themes``; we import the
# module-private ones directly rather than widening ``themes.__all__`` (themes.py
# is outside this change's file scope). Clustering here must agree with how
# themes group gradients, so the helpers are reused, never re-implemented — the
# blocking lint tier (F + E9) does not flag private-name imports.
from mini_ork.learning.code_findings import (
    _DEFAULT_DEPTH,
    _dir_prefix,
    _in_repo,
    _run_title,
    annotate_landing,
    annotate_staleness,
    areas,
    engine_repo_identity,
    landing_enabled,
)
from mini_ork.learning.themes import (
    _df_to_idf,
    _dot,
    _idf_unseen,
    _l2norm,
    _tokenize,
    _vector,
)

# Section titles — the page contract tests look these up by name.
KV_TITLE = "Harvest"
DAYS_TITLE = "Days"
AREAS_TITLE = "Your code: what reviews found, by area"
RECURRING_TITLE = "Recurring problems"
SEEN_ONCE_TITLE = "Seen in one kickoff"
FINDINGS_TITLE = "Findings"
FINDING_TITLE = "Finding"

_DAYS_DEFAULT = 30
_DAYS_CHIPS = (7, 30, 90)
_SIM = 0.35           # greedy-leader cosine threshold
_TOP_CLUSTERS = 5     # clusters returned by recurring()
_SEEN_ONCE_TOP = 10   # clusters returned by seen_once()
_RECURRING_PER_AREA = 2   # representatives shown in an area row
_REPR_MAX = 70        # area-row "recurring problems" cell cap
_REPR_DETAIL = 160    # recurring-table problem cap
_ISSUE_MAX = 120      # findings-table problem cap
_AREA_FINDINGS = 50   # rows in the area's Findings *table* (the summary and the
                      # recurrence clusters read the full set — kickoff #1)
# The grouping depth ``areas()`` uses. The page re-derives every group key with
# the exact same ``_dir_prefix(file, depth)`` rule, so a table row and the detail
# it opens are ONE set of findings, not two that happen to agree (reviewer,
# round 2).
_DEPTH = _DEFAULT_DEPTH
# ``_make_finding`` stamps this on a finding that carried no issue text at all.
# It is the absence of a problem, not a problem, so ``recurring()`` never
# clusters it (reviewer, round 2: it was the top "recurring problem" live).
_PLACEHOLDER_ISSUE = "(no issue)"

# Severity ranking: lower is worse. Unknown severities sort last.
_SEV_ORDER = {"blocker": 0, "critical": 0, "high": 1, "major": 1,
              "medium": 2, "warn": 2, "warning": 2, "low": 3, "minor": 3}

# Tokens that only restate the severity, never the mistake's identity. Kept to
# unambiguous severity *labels*: "error", "high" and "low" are ordinary content
# words ("error path swallows exceptions"), so stripping them would erase part
# of the finding rather than just its label (reviewer, round 1).
_SEV_WORDS = ("blocker", "critical", "major", "minor", "warning", "medium")
_RE_LINEREF = re.compile(r":\s*\d+(?:\s*[-–]\s*\d+)?")
_RE_FILEISH = re.compile(r"[A-Za-z0-9_./~-]*[A-Za-z0-9_]+\.[A-Za-z]{1,5}\b")
_RE_SEVWORD = re.compile(r"(?i)\b(" + "|".join(_SEV_WORDS) + r")\b")


# ── page entry ─────────────────────────────────────────────────────────────

def sections(home: Path, args: dict[str, str], errors: dict[str, str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    out += S.guarded(errors, KV_TITLE, lambda: _freshness(home))
    area = str(args.get("area") or "").strip()
    if area:
        # The area detail (markdown + recurring table + findings table, plus the
        # one-finding detail) is placed BEFORE the areas table, per kickoff §4.
        out += S.guarded(errors, area, lambda: _area_detail(home, args, area))
    out += S.guarded(errors, DAYS_TITLE, lambda: _days_chips(args))
    out += S.guarded(errors, AREAS_TITLE, lambda: _areas_table(home, args))
    return out


# ── DB resolution ──────────────────────────────────────────────────────────

def _db_path(home: Path) -> str:
    """The single DB path both the code_findings queries and the freshness read use.

    ``MINI_ORK_DB`` wins (the IDE and the live-proof step set it), else the
    home's ``state.db`` — the same order ``reflect.py`` uses. Resolving once is
    what stops the area rows and the freshness count from being read off two
    different DBs (prior-art lens F4).
    """
    return os.environ.get("MINI_ORK_DB") or str(Path(home) / "state.db")


def _days(args: dict[str, str]) -> int:
    try:
        return max(1, int(str(args.get("days") or _DAYS_DEFAULT)))
    except (TypeError, ValueError):
        return _DAYS_DEFAULT


# ── section: freshness ─────────────────────────────────────────────────────

def _harvest_summary(home: Path) -> tuple[int, int]:
    """(runs harvested, newest harvested_at) from ``code_findings_runs``.

    Read through the same read-only ``StateDB`` the other tabs use, but pointed
    at the resolved path so it can never disagree with the area rows.
    """
    from mini_ork.web.db import StateDB

    try:
        conn = StateDB(Path(_db_path(home)))
    except (FileNotFoundError, OSError):
        return 0, 0
    if not conn.has_table("code_findings_runs"):
        return 0, 0
    row = conn.row("SELECT COUNT(*) AS n, MAX(harvested_at) AS last FROM code_findings_runs")
    if not row:
        return 0, 0
    return int(row.get("n") or 0), int(row.get("last") or 0)


def _runs_dir(home: Path) -> Path:
    """Where ``harvest`` reads from — the same ``home/runs`` the writer uses."""
    return Path(home) / "runs"


def _reflect_state(home: Path) -> dict[str, int]:
    """What the reflect lifecycle says about this home: three integers, SELECT-only.

    The page must never write (reflect is the sole writer of these tables), so
    this reads the very ``execution_traces`` table ``reflect.py`` writes and
    nothing else: when the newest ``__reflect__`` pass finished, when the newest
    one started whatever its fate, and how many are still marked 'running'. A
    'running' row is the fingerprint of a reflect killed mid-pipeline (only the
    parent that spawned one finalizes it), so a home with an empty harvest AND
    leaking 'running' rows is saying the harvest never got its turn — a
    different problem from a home with nothing to harvest yet.
    """
    state = {"last_success": 0, "last_attempt": 0, "running": 0}
    from mini_ork.web.db import StateDB

    try:
        conn = StateDB(Path(_db_path(home)))
    except (FileNotFoundError, OSError):
        return state
    if not conn.has_table("execution_traces"):
        return state
    row = conn.row(
        "SELECT "
        "MAX(CASE WHEN status='success' THEN CAST(strftime('%s', created_at) AS INTEGER) END) AS done, "
        "MAX(CAST(strftime('%s', created_at) AS INTEGER)) AS tried, "
        "SUM(CASE WHEN status='running' THEN 1 ELSE 0 END) AS running "
        "FROM execution_traces WHERE task_class='__reflect__'")
    if not row:
        return state
    return {"last_success": int(row.get("done") or 0),
            "last_attempt": int(row.get("tried") or 0),
            "running": int(row.get("running") or 0)}


def _empty_note(home: Path) -> str:
    """ONE sentence naming why there is nothing to show.

    Three different situations used to render the identical "Updated after every
    reflect pass." — no runs at all, runs whose reflect never finished, and a
    harvest that ran and found nothing — and only the first is cured by waiting.
    Every branch here is a read: a directory test and SELECTs.
    """
    runs = _runs_dir(home)
    if not runs.is_dir():
        return f"No runs to harvest yet — {runs} does not exist."
    state = _reflect_state(home)
    if not state["last_success"]:
        if not state["last_attempt"]:
            return "No reflect pass has run in this home yet."
        age = S.age(state["last_attempt"], int(time.time()))
        stuck = (f" {state['running']} pass(es) are still marked running — a reflect "
                 f"that was killed leaves its row that way.") if state["running"] else ""
        return f"Reflect has never finished here (newest attempt {age} ago).{stuck}"
    age = S.age(state["last_success"], int(time.time()))
    return (f"Reflect last finished {age} ago and harvested nothing — open that "
            f"run's reflect.log for what it reached.")


def _freshness(home: Path) -> dict[str, Any]:
    n, last = _harvest_summary(home)
    if not n:
        return S.kv(KV_TITLE, [("Harvested", "nothing yet", "sub", _empty_note(home))])
    note = "Updated after every reflect pass."
    now = int(time.time())
    plural = "" if n == 1 else "s"
    return S.kv(KV_TITLE, [("Harvested", f"{n} run{plural} · last {S.age(last, now)} ago",
                            "text", note)])


# ── section: days chips ────────────────────────────────────────────────────

def _days_chips(args: dict[str, str]) -> dict[str, Any]:
    days = _days(args)
    items = [{"t": f"{d} days", "on": days == d, "do": S.set_args(days=str(d))}
             for d in _DAYS_CHIPS]
    return S.chips(DAYS_TITLE, items)


# ── section: areas table ───────────────────────────────────────────────────

def _area_stats(fs: list[dict[str, Any]]) -> tuple[int, str]:
    """``(distinct run count, worst severity)`` over one finding set.

    The single derivation the areas-table row and the area detail both call, so
    a row and the page it opens are computed from the same set and can never
    disagree (reviewer, round 1). ``_SEV_ORDER`` is the same ranking the rest of
    the page uses; an empty set has no runs and no worst severity above ``low``.
    """
    n_runs = len({f.get("run_id") for f in fs if f.get("run_id")})
    sevs = [str(f.get("severity") or "low").lower() for f in fs]
    worst = min(sevs, key=lambda s: _SEV_ORDER.get(s, 3)) if sevs else "low"
    return n_runs, worst


def _areas_table(home: Path, args: dict[str, str]) -> dict[str, Any]:
    db = _db_path(home)
    days = _days(args)
    rows = areas(db=db, since_days=days)
    if not rows:
        items = [S.dot("No review findings yet. They appear after your runs are reviewed.")]
        # The same three-state note the Harvest line carries: a table that is
        # empty because nothing could be harvested reads exactly like one that
        # is empty because nothing was found, and only this line tells them
        # apart.
        note = _empty_note(home)
        if note:
            items.append(S.dot(note, "sub"))
        return S.lst(AREAS_TITLE, items, full=True)
    now = int(time.time())
    selected = str(args.get("area") or "")
    # ONE membership rule for both views (reviewer, round 2). ``areas()`` groups
    # the window's findings by ``_dir_prefix(file, depth)`` — a *disjoint*
    # partition — and each row now carries that group key. The row's numbers and
    # its click target both come from the key, so a row and the page it opens are
    # the same findings by construction, and no finding is counted twice. r1
    # derived the row from the display label instead, so the ``mini_ork`` row
    # (label, a shallow group) swept in the whole recursive subtree — a superset
    # of ``mini_ork/cli``, ``mini_ork/learning`` … and the sibling findings that
    # *belong* to a dominant-file group (``mini_ork/ide_pages``) fell out of every
    # row. Records carry their own group key now.
    all_fs = _window_findings(db, since_days=days)
    prepared = []
    for a in rows:
        key = str(a.get("key") or a["area"])
        fs = _membership(all_fs, key)
        if not fs:
            # ``areas()`` rolls up every repo in the window; the tab shows one
            # repo's findings. An area with no row in the repo-scoped set (it
            # belongs to another target repo) would otherwise render as a
            # zero-count row, so it is not a row of this tab at all.
            continue
        n_runs, worst = _area_stats(fs)
        prepared.append((key, str(a["area"]), fs, n_runs, worst))
    # Same ordering rule as ``_aggregate_areas`` — worst first, then most findings
    # — now over the derived numbers, so the visible order matches the visible
    # counts rather than a group's hidden totals.
    prepared.sort(key=lambda p: (_SEV_ORDER.get(p[4], 3), -len(p[2])))
    out_rows = []
    for key, label, fs, n_runs, worst in prepared:
        problems = " · ".join(
            short(c["representative"], _REPR_MAX)
            for c in recurring(fs)[:_RECURRING_PER_AREA]
        ) or "—"
        out_rows.append({
            "cells": [
                # ``label`` is ``areas()``'s display name — the dominant file when
                # one owns the group — shown as a CELL; the click target is the
                # group key, so the row always opens its whole group and the
                # siblings a dominant file would otherwise hide (reviewer, r2).
                S.mono(label),
                S.cell(len(fs)),
                S.cell(n_runs),
                S.cell(worst, _sev_colour(worst)),
                S.cell(problems, "sub"),
                S.cell(S.age(max((int(f.get("ts") or 0) for f in fs), default=0), now), "sub"),
            ],
            "do": S.set_args(area=key),
            "sel": selected == key,
        })
    return S.table(
        AREAS_TITLE,
        [S.col(fr=2, min=120), S.col(width=70), S.col(width=60), S.col(width=70),
         S.col(fr=3, min=160), S.col(width=60)],
        ["area", "findings", "runs", "worst", "recurring problems", "last"],
        out_rows, full=True,
        note=("From every reviewer and verifier finding in your runs. Click an area to see "
              "each finding and turn a recurring problem into a rule."))


def _select_findings(conn, where: str, params: list, cutoff: int,
                     join: bool, with_target_repo: bool = False) -> list[dict[str, Any]]:
    """One SELECT over ``code_findings`` with the ``file`` predicate and the
    ``ts`` cutoff pushed into SQL, ahead of any limit.

    Mirrors ``findings_for``'s row shape (LEFT JOIN ``task_runs`` so run
    titles/status resolve, title falling back to the run id) but without that
    helper's ``LIMIT`` — the limit belongs on the Findings *table*, not on the
    query the summary and the clusters read. r1 applied the file filter in
    Python *after* a SQL ``LIMIT 50``, so a crowd of newer prefix siblings
    (``bin/mini-ork-apply``, ``-bugs``, …) evicted ``bin/mini-ork``'s own rows
    before the filter saw them (kickoff #2).

    LF-R10 also exposes each finding's ``kickoff_path`` (already joined for the
    run title) so ``recurring()`` can count recurrence per KICKOFF instead of
    per row. Additive: ``None`` when there is no join or the run has no
    ``task_runs`` row, and every pre-existing key is untouched.
    """
    tr_cols = ", tr.kickoff_path, tr.status" if join else ""
    repo_col = ", f.target_repo" if with_target_repo else ""
    join_sql = " LEFT JOIN task_runs tr ON tr.id = f.run_id" if join else ""
    sql = (
        "SELECT f.run_id, f.source, f.file, f.line, f.severity, f.category, "
        "f.issue, f.snippet, f.verdict, f.ts" + repo_col + tr_cols + " "
        "FROM code_findings f" + join_sql + " "
        f"WHERE {where} AND f.ts >= ? ORDER BY f.ts DESC"
    )
    out: list[dict[str, Any]] = []
    for r in conn.rows(sql, list(params) + [cutoff]):
        kick = r.get("kickoff_path") if join else None
        row = {
            "run_id": r["run_id"],
            "run_title": _run_title(kick, r["run_id"]),
            "run_status": r["status"] if join else None,
            "kickoff_path": kick,
            "source": r["source"],
            "file": r["file"],
            "line": r["line"],
            "severity": r["severity"],
            "category": r["category"],
            "issue": r["issue"],
            "snippet": r["snippet"],
            "verdict": r["verdict"],
            "ts": r["ts"],
        }
        if with_target_repo:
            # LF-R11a: staleness is checked against each row's OWN repo, so the
            # identity travels with the row instead of being assumed.
            row["target_repo"] = r.get("target_repo")
        out.append(row)
    return out


def _group_key(file: Any, depth: int = _DEPTH) -> str:
    """The ``areas()`` group key of a finding's ``file`` — the page re-derives it
    with the very same ``_dir_prefix`` rule ``_aggregate_areas`` uses, so the two
    agree by construction (reviewer, round 2).

    A bare filename (no directory) keys on itself, exactly as ``_aggregate_areas``
    does, so ``Makefile`` is its own group rather than an empty-prefix bucket.
    """
    f = str(file or "")
    if not f:
        return ""
    return _dir_prefix(f, depth) or f


def _has_target_repo(conn) -> bool:
    """True when the DB carries the LF-R12 ``target_repo`` column.

    A DB migrated only to 0064 has no such column; filtering on it would make
    the SQL raise and blank the tab, so the repo predicate is skipped there.
    """
    try:
        return any(r.get("name") == "target_repo"
                   for r in conn.rows("PRAGMA table_info(code_findings)"))
    except Exception:  # noqa: BLE001 — a page section must never raise
        return False


def _window_findings(db: str, *, since_days: int, repo_only: bool = True,
                     repo_root: str | None = None,
                     target_repo: str | None = None) -> list[dict[str, Any]]:
    """Every *live* finding in the ``since_days`` window, newest first — the one
    set the areas table partitions and the detail filters.

    LF-R11a: rows whose ``snippet`` no longer occurs at their own repo's HEAD
    are excluded here (age is already bounded by the ``days`` window). LF-R11b:
    rows whose run's diff landed on origin/main are excluded too (unless
    ``MO_FINDINGS_LANDING=0``). Neither kind is deleted;
    ``_window_findings_with_stats`` exposes both so a section can report how
    many were hidden. The rest of this contract is unchanged: see that function.
    """
    return _window_findings_with_stats(
        db, since_days=since_days, repo_only=repo_only, repo_root=repo_root,
        target_repo=target_repo)[0]


def _window_findings_with_stats(db: str, *, since_days: int,
                                repo_only: bool = True,
                                repo_root: str | None = None,
                                target_repo: str | None = None
                                ) -> tuple[list[dict[str, Any]],
                                           list[dict[str, Any]],
                                           list[dict[str, Any]]]:
    """``(fresh_rows, stale_rows, landed_rows)`` for the window.

    The window (``int(time.time()) - days*86400``) and the ``_in_repo`` filter are
    the exact ones ``areas()`` applies (kickoff #4; reviewer, round 1), so the
    groups that table renders partition this set and their counts sum to it
    (reviewer, round 2).

    LF-R12 §5: ``target_repo`` is the tab's repo identity. Only rows whose
    ``target_repo`` equals it are shown, plus NULL rows when that identity IS the
    engine checkout (NULL is ENGINE-LOCAL, never a wildcard). The predicate is
    pushed into the SQL ``WHERE`` — never a post-hoc Python filter — so it
    composes with the window and cannot re-create the prefix/LIMIT defects this
    file already fixed (f:6c09169399, f:51c3c885a5). When unset it resolves to
    the engine identity; an unresolvable identity keeps only NULL rows, so the
    tab fails closed rather than showing every repo's findings.

    Staleness runs AFTER the full windowed set is materialized (never inside a
    capped slice), grouped by each row's recorded ``target_repo``; NULL rows use
    the engine identity. ``max_age_days=0`` because the days window already
    bounds age — this hides snippet staleness only.

    LF-R11b: after the stale split, rows whose run's diff landed on origin/main
    are peeled off into a third list (``landed``), unless
    ``MO_FINDINGS_LANDING=0``. They are hidden from the listing, never deleted,
    and counted separately so the stale count never absorbs them.

    Read-only throughout: ``StateDB`` opens with ``query_only=ON``, the page's own
    convention (see ``_harvest_summary``).
    """
    from mini_ork.web.db import StateDB

    cutoff = int(time.time()) - int(since_days) * 86400
    try:
        conn = StateDB(Path(db))
    except (FileNotFoundError, OSError):
        return [], [], []
    if not conn.has_table("code_findings"):
        return [], [], []
    join = conn.has_table("task_runs")
    with_repo = _has_target_repo(conn)
    where, params = "f.file IS NOT NULL", []
    if with_repo:
        if target_repo is None:
            target_repo = engine_repo_identity()
        if not target_repo:
            # Fail closed: no resolvable identity means only the legacy
            # engine-local rows are intelligible.
            where += " AND f.target_repo IS NULL"
        elif target_repo == engine_repo_identity():
            where += " AND (f.target_repo = ? OR f.target_repo IS NULL)"
            params.append(target_repo)
        else:
            where += " AND f.target_repo = ?"
            params.append(target_repo)
    fs = _select_findings(conn, where, params, cutoff, join,
                          with_target_repo=with_repo)
    if repo_only and fs:
        keep = _in_repo({str(f["file"]) for f in fs if f.get("file")}, repo_root)
        fs = [f for f in fs if f.get("file") in keep]
    fresh, stale = _split_stale(fs, repo_root=repo_root, target_repo=target_repo)
    landed: list[dict[str, Any]] = []
    if fresh and landing_enabled():
        marked = annotate_landing(fresh, db=db)
        landed = [r for r in marked if r.get("landed")]
        fresh = [r for r in marked if not r.get("landed")]
    return fresh, stale, landed


def _window_stale(db: str, *, since_days: int, repo_only: bool = True,
                  repo_root: str | None = None,
                  target_repo: str | None = None) -> list[dict[str, Any]]:
    """The stale half of ``_window_findings_with_stats`` — hidden, never deleted."""
    return _window_findings_with_stats(
        db, since_days=since_days, repo_only=repo_only, repo_root=repo_root,
        target_repo=target_repo)[1]


def _hidden_counts(db: str, *, since_days: int,
                   area: str) -> tuple[int, int]:
    """``(stale_count, landed_count)`` for ``area`` — the two hidden sets.

    One window computation feeds both counts, so the stale line and the landing
    line are separate numbers over the same set (LF-R11a / LF-R11b) and neither
    can absorb the other.
    """
    _fresh, stale, landed = _window_findings_with_stats(db, since_days=since_days)
    return (len(_membership(stale, area)), len(_membership(landed, area)))


def _split_stale(fs: list[dict[str, Any]], *, repo_root: str | None = None,
                 target_repo: str | None = None
                 ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Partition ``fs`` into ``(fresh, stale)`` via ``annotate_staleness``.

    Rows are grouped by the ``target_repo`` they were harvested under — the R12
    identity of the repo the run actually targeted — so a finding is never
    judged stale against the engine checkout's HEAD. A row with no identity
    (legacy/engine-local) uses the engine's identity, then the caller's
    ``target_repo``/``repo_root`` as last resorts.
    """
    if not fs:
        return [], []
    engine = engine_repo_identity()
    groups: dict[str, list[dict[str, Any]]] = {}
    for f in fs:
        root = (f.get("target_repo") if isinstance(f, dict) else None) \
            or engine or target_repo or repo_root or ""
        groups.setdefault(str(root), []).append(f)
    now = int(time.time())
    fresh: list[dict[str, Any]] = []
    stale: list[dict[str, Any]] = []
    for root, group in groups.items():
        for row in annotate_staleness(group, repo_root=root, now=now,
                                      max_age_days=0):
            (stale if row.get("stale") else fresh).append(row)
    fresh.sort(key=lambda f: int(f.get("ts") or 0), reverse=True)
    stale.sort(key=lambda f: int(f.get("ts") or 0), reverse=True)
    return fresh, stale


def _membership(all_fs: list[dict[str, Any]], area: str) -> list[dict[str, Any]]:
    """The findings of *exactly* ``area`` — the ONE rule both views share.

    ``area`` is a group key from ``areas()`` (``_group_key(file) == area``), or a
    path a user deep-linked. A finding belongs when its group key equals the area
    (the disjoint partition the areas table renders) or when its file *is* the
    area (a file deep-link, ``area=bin/mini-ork``). The two cases never overlap
    for the same finding, so nothing is counted twice and no row over-counts a
    neighbour (reviewer, round 2 — r1's recursive ``area/**`` query made the
    ``mini_ork`` row a superset of ``mini_ork/cli``, ``mini_ork/learning``, …).
    """
    base = area.rstrip("/")
    return [
        f for f in all_fs
        if _group_key(f.get("file")) == base or str(f.get("file") or "") == base
    ]


def _findings_for_area(db: str, area: str, *, since_days: int,
                       repo_root: str | None = None) -> list[dict[str, Any]]:
    """The detail's view of ``area`` — ``_membership`` over the same window the
    areas table partitions, so a row and the page it opens are one set."""
    return _membership(
        _window_findings(db, since_days=since_days, repo_root=repo_root), area)


# ── section: area detail (+ one finding) ───────────────────────────────────

def _area_detail(home: Path, args: dict[str, str], area: str) -> list[dict[str, Any]]:
    db = _db_path(home)
    # The FULL set (not the capped page) drives the summary line and the
    # recurrence clusters; only the Findings *table* below is sliced to
    # ``_AREA_FINDINGS``. r1 fed all three from one 50-capped list, so a
    # 147-finding area claimed "50 findings in 8 runs" and clustered a 50-row
    # sample (kickoff #1). The window is the same ``days`` the areas table used
    # (kickoff #4) and the membership is the same ``_in_repo``-filtered set —
    # the areas-table row now derives its numbers through this very call, so the
    # two views are equal by construction (reviewer, round 1).
    fs = _findings_for_area(db, area, since_days=_days(args))
    clusters = recurring(fs)

    # The summary line describes the rows listed below, and ``_area_stats`` is
    # the exact derivation the areas table uses for its row — so the row count
    # and this count are the same number, not two numbers that happen to agree.
    n_runs, worst = _area_stats(fs)
    last = max((int(f.get("ts") or 0) for f in fs), default=0)
    date = time.strftime("%Y-%m-%d", time.localtime(last)) if last else "—"
    lines = [f"{len(fs)} findings in {n_runs} run{'' if n_runs == 1 else 's'}, "
             f"worst {worst}, last {date}", "", "**Recurring problems**"]
    # LF-R11a / LF-R11b: stale and landed rows are hidden, never deleted — say
    # how many of each, on their own lines, so the operator can tell "quiet
    # because fixed" from "quiet because hidden" and the two counts never merge.
    n_stale, n_landed = _hidden_counts(db, since_days=_days(args), area=area)
    if n_landed:
        lines.insert(1, f"{n_landed} finding(s) hidden as landed on origin/main")
    if n_stale:
        lines.insert(1, f"{n_stale} finding(s) hidden as stale at HEAD")
    if clusters:
        for c in clusters:
            files = ", ".join(short(f, 60) for f in c["files"]) or "—"
            lines.append(f"- **{_occurrence_label(c)}** "
                         f"{short(c['representative'], _REPR_DETAIL)} "
                         f"(kickoffs: {c['kickoffs']}, files: {files})")
    else:
        lines.append("- none yet")
    # Close clears the area AND any open finding, so the header button does both.
    out: list[dict[str, Any]] = [
        S.markdown(area, "\n".join(lines), full=True,
                   actions=[S.btn("Close area", S.set_args(area="", finding=""), "ghost")])]

    # A cluster with no defensible target (or one that is not recurring) carries
    # no action at all, and its action cell says so with an em dash rather than
    # offering a button whose rule could not be scoped to the findings it came
    # from (kickoff §4). Only ``recurring()`` clusters reach this table, so the
    # "Make it a rule" floor and R4's target rules are the ones LF-R4 shipped.
    recur_rows = []
    for c in clusters:
        action = _rule_action(c)
        recur_rows.append({
            "cells": [
                S.cell(short(c["representative"], _REPR_DETAIL)),
                S.cell(_kickoffs_cell(c)),
                S.cell(_files_cell(c)),
                S.cell(c["worst_severity"], _sev_colour(c["worst_severity"])),
                S.cell("Make it a rule", "blue") if action else S.cell("—", "sub"),
            ],
            "do": action,
        })
    out.append(S.table(
        RECURRING_TITLE,
        [S.col(fr=3, min=180), S.col(width=80), S.col(fr=2, min=140), S.col(width=70),
         S.col(width=130)],
        ["problem", "kickoffs", "files", "worst", "action"],
        recur_rows,
        full=True,
        note=("Pick a recurring problem and mini-ork will draft a rule for the files "
              "it was found in — it reaches an agent once you approve it on the "
              "Rules tab.")))

    # Seen in ONE kickoff: shown, never hidden — the operator needs to see the
    # restatements too — but with no rule button, because one kickoff is not yet
    # a recurrence (LF-R10). The kickoff stem is the item's label: it is the
    # thing that would have to repeat for this to become a rule.
    once = seen_once(fs)
    if once:
        out.append(S.lst(
            SEEN_ONCE_TITLE,
            [S.item(short(c["representative"], _REPR_DETAIL),
                    f"{_occurrence_label(c)} in kickoff {_stems_label(c)} · "
                    f"files: {_files_cell(c)}",
                    m="·", tc="sub")
             for c in once],
            full=True,
            note=("These were each seen in a single kickoff, so no rule is drafted. "
                  "A problem becomes recurring — and reachable by the action above — "
                  "when it shows up in a second kickoff.")))

    selected = str(args.get("finding") or "")
    out.append(S.table(
        FINDINGS_TITLE,
        [S.col(fr=2, min=140), S.col(width=70), S.col(fr=3, min=200), S.col(fr=2, min=120)],
        ["file:line", "severity", "problem", "run"],
        [{"cells": [
            S.mono(f"{f.get('file') or '?'}:{f.get('line') or ''}"),
            S.cell(str(f.get("severity") or ""), _sev_colour(str(f.get("severity") or ""))),
            S.cell(short(f.get("issue") or "", _ISSUE_MAX)),
            S.cell(short(f.get("run_title") or f.get("run_id") or "", 60), "sub"),
        ], "do": S.set_args(area=area, finding=finding_key(f)),
            "sel": selected == finding_key(f)} for f in fs[:_AREA_FINDINGS]],
        full=True))

    if selected:
        match = next((f for f in fs if finding_key(f) == selected), None)
        out.append(_finding_detail(match))

    return out


def _finding_detail(f: dict[str, Any] | None) -> dict[str, Any]:
    if not f:
        return S.lst(FINDING_TITLE, [S.bad("That finding is no longer in this area.", "")],
                     actions=[S.btn("Close", S.set_args(finding=""), "ghost")], full=True)
    issue = str(f.get("issue") or "")
    snippet = str(f.get("snippet") or "")
    run_title = str(f.get("run_title") or f.get("run_id") or "")
    run_status = str(f.get("run_status") or "unknown")
    body = f"**{issue}**\n\n"
    if snippet:
        body += "```\n" + snippet.rstrip() + "\n```\n\n"
    body += f"Run: {run_title} · {run_status}"
    acts: list[dict[str, Any]] = []
    if f.get("run_id"):
        acts.append(S.btn("Open run", S.open_run(str(f["run_id"]), run_title), "primary"))
    path = _repo_file(f.get("file"))
    if path is not None:
        acts.append(S.btn("Open file", S.open_path(str(path)), "ghost"))
    acts.append(S.btn("Close", S.set_args(finding=""), "ghost"))
    return S.markdown(FINDING_TITLE, body, full=True, actions=acts)


# ── the "Make it a rule" action ────────────────────────────────────────────

def _rule_target(all_files) -> str | None:
    """The ``--target`` glob for a cluster, derived from its MEMBER files.

    The rule must reach the files the problem was actually found in — no more.
    Round 1 (and the pre-LF-R4 action) targeted the AREA glob instead: a cluster
    whose members were all in ``mini_ork/ide_pages/node.py`` produced
    ``mini_ork/ide_pages/*``, which is injected into runs touching its siblings
    too. The kickoff's four rules, in order:

    * one file → that file;
    * every member a DIRECT child of one directory → ``<dir>/*``;
    * otherwise all under a common directory → ``<dir>/**`` (only when that
      common directory has at least two segments, so a repo-root pairing does
      not degenerate into ``/**``);
    * otherwise ``None`` — no defensible target, so no button.

    Reads ``all_files`` (every member, never the capped top-3 display list).
    """
    files = sorted({str(f) for f in (all_files or []) if f})
    if not files:
        return None
    if len(files) == 1:
        return files[0]
    dirs = {f.rsplit("/", 1)[0] if "/" in f else "" for f in files}
    if len(dirs) == 1:
        only = next(iter(dirs))
        return f"{only}/*" if only else None
    common = _common_dir(dirs)
    if common and len(common.split("/")) >= 2:
        return f"{common}/**"
    return None


def _common_dir(dirs) -> str:
    """The deepest directory shared by every path in ``dirs`` (``''`` if none).

    Segments, not characters: ``a/b`` and ``a/bc`` share nothing, while
    ``a/b`` and ``a/b/c`` share ``a/b``. A member with no directory at all makes
    the answer ``''`` — the common root.
    """
    parts = [d.split("/") for d in dirs]
    if any(not p for p in parts):
        return ""
    common = list(parts[0])
    for p in parts[1:]:
        i = 0
        while i < len(common) and i < len(p) and common[i] == p[i]:
            i += 1
        common = common[:i]
    return "/".join(common)


# A sentence that reports a reproduction command/result, not the problem.
_RE_REPRO = re.compile(r"(?i)^\s*reproduced\b")
# Status tokens an automated reviewer appends to a finding's text.
_RE_STATUS = re.compile(r"(?i)\[(?:true|false)\]|\b(?:passed|failed)\b")
_DRAFT_PREFIX = "Reviewers repeatedly found in "
_DRAFT_MAX = 240          # body cap, excluding the prefix


def _generalise(issue: Any) -> str:
    """A finding's text reduced to the problem itself.

    File:line references and severity words go through
    ``context_v2.clean_issue`` (the same cleaner the clusterer uses, imported
    read-only here — that module is out of scope). On top of it the action drops
    what only made sense in a verifier transcript: any sentence that starts
    ``Reproduced`` and any ``[True]``/``[False]``/``PASSED``/``FAILED`` token.
    A rule that quotes a reproduction result tells a future agent about a run,
    not about the code.
    """
    from mini_ork.context_v2 import clean_issue   # lazy: read-only import

    text = clean_issue(str(issue or ""))
    sentences = re.split(r"(?<=[.!?])\s+", text)
    text = " ".join(s for s in sentences if not _RE_REPRO.match(s))
    text = _RE_STATUS.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def _draft_text(target: str, representative: Any) -> str:
    """The rule text a draft starts from: generalised, prefixed, capped."""
    body = _generalise(representative)
    if len(body) > _DRAFT_MAX:
        body = body[:_DRAFT_MAX].rstrip()
    return f"{_DRAFT_PREFIX}{target}: {body}"


def _rule_action(cluster: dict[str, Any]) -> dict[str, Any] | None:
    """The "Make it a rule" CLI action for one recurring problem, or ``None``.

    The action creates an INACTIVE, learned, file-scoped draft — it does not
    write a live rule. Approving it on the Rules tab is what starts giving it to
    agents, so the confirm string says exactly that. ``None`` means "no button":
    a cluster seen in only one kickoff (``n`` IS ``occurrences`` since LF-R10, so
    the floor reads the recurrence count) is not a recurring problem, and a
    cluster whose member files share no usable directory has no defensible target
    (see ``_rule_target``).
    """
    if int(cluster.get("n") or 0) < 2:
        return None
    target = _rule_target(cluster.get("all_files"))
    if not target:
        return None
    rep = str(cluster.get("representative") or "")
    slug = hashlib.sha1(rep.encode("utf-8")).hexdigest()[:8]
    keys = [str(k) for k in cluster.get("finding_ids") or []]
    return S.cli(
        "prefs", "set", f"review-{slug}", _draft_text(target, rep),
        "--scope", "path", "--target", target,
        "--origin", "learned", "--source", ",".join(keys), "--draft",
        confirm=(f"Create an inactive draft rule for {target}? It is not given to "
                 f"any agent until you approve it on the Rules tab."),
        home=False)


def _repo_file(file: Any) -> Path | None:
    """The on-disk path for a finding's repo-relative ``file``, or None.

    Findings carry paths relative to the run's *target* repo, which is not
    necessarily the engine root, so try the target repo first, then the engine
    root, then cwd — the resolution order ``cli/garden.py`` uses. The first
    candidate that actually exists wins, so a foreign-repo finding opens the
    right file instead of the engine's namesake or nothing.
    """
    if not file:
        return None
    for root in (os.environ.get("MINI_ORK_TARGET_REPO"), os.environ.get("MINI_ORK_ROOT"),
                 os.getcwd()):
        if not root:
            continue
        path = Path(root) / str(file)
        if path.exists():
            return path
    return None


def _sev_colour(severity: Any) -> str:
    s = str(severity or "").lower()
    if s in ("blocker", "critical", "high"):
        return "red"
    if s in ("major", "medium", "warn", "warning"):
        return "yellow"
    return "sub"


# ── clustering (pure) ──────────────────────────────────────────────────────

def finding_key(f: dict[str, Any]) -> str:
    """A stable identity for one finding.

    ``findings_for`` rows carry no DB ``id``, so the key is derived from the
    finding's own fields — enough to round-trip through ``args["finding"]``.
    """
    if f.get("id") is not None:
        return str(f["id"])
    raw = f"{f.get('run_id', '')}|{f.get('file', '')}|{f.get('line', '')}|{f.get('issue', '')}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def _strip_issue(issue: Any) -> str:
    """Drop file paths, ``:line`` refs and severity words before vectorizing, so
    the SAME mistake written in different files shares its discriminating tokens.

    ``themes._tokenize`` (via ``normalize``) already collapses paths and numbers,
    but a bare filename or an inline ``BLOCKER`` would still split otherwise
    identical issues.
    """
    t = str(issue or "")
    t = _RE_LINEREF.sub(" ", t)
    t = _RE_FILEISH.sub(" ", t)
    t = _RE_SEVWORD.sub(" ", t)
    return t


def _sum_vecs(vecs) -> dict[str, float]:
    out: dict[str, float] = {}
    for v in vecs:
        for tok, w in v.items():
            out[tok] = out.get(tok, 0.0) + w
    return out


def _kickoff_stem(path: Any) -> str:
    """``context_v2.kickoff_stem`` — a kickoff's revision-insensitive identity.

    Imported lazily (read-only) exactly as ``_generalise`` imports
    ``clean_issue``: ``context_v2`` is out of this change's file scope and is
    never edited here. ``""`` for a generic name (``kickoff.md``, ``probe-3.md``)
    or a stem with no hyphen — those carry no identity, which is what forces the
    run-id fallback in :func:`_member_stem`.
    """
    from mini_ork.context_v2 import kickoff_stem   # lazy: read-only import

    return kickoff_stem(str(path) if path else None)


def _member_stem(f: dict[str, Any]) -> str:
    """Which KICKOFF a finding belongs to: its kickoff's stem, else its run id.

    LF-R10 counts recurrence in kickoffs, not rows: ``ide-node-stream-r3`` and
    ``-r4`` are one kickoff, so a problem found in both is still a one-kickoff
    problem. A generic kickoff name has no stem — two unrelated ``kickoff.md``
    runs must never merge — so those bucket by ``run_id`` instead, and so do
    findings from runs with no ``task_runs`` row (``kickoff_path`` is ``None``).
    """
    return _kickoff_stem(f.get("kickoff_path")) or str(f.get("run_id") or "")


def _is_recurring(c: dict[str, Any]) -> bool:
    """LF-R10's floor: seen in ≥ 2 kickoffs AND in ≥ 2 ``(kickoff, file)`` pairs.

    One kickoff restating a problem four times is an emphasis, not a recurrence,
    and neither is a pattern spread across two files of a single run.
    """
    return int(c.get("occurrences") or 0) >= 2 and int(c.get("kickoffs") or 0) >= 2


def _mixed_cluster(member_vecs: list[dict[str, float]], stems: list[str],
                   sim: float) -> bool:
    """A cluster spanning ≥ 2 kickoffs whose members joined only via the centroid.

    The greedy-leader loop adds a member when it is close to the cluster's
    running CENTROID; two members from different kickoffs can therefore end up
    together without any cross-kickoff member PAIR reaching ``sim``. The
    "same problem again in another kickoff" story is weaker there, so the table
    says ``2 (mixed)`` instead of overclaiming. Judged on the retained member
    vectors — pure, no DB.
    """
    if len(set(stems)) < 2:
        return False
    for a in range(len(member_vecs)):
        for b in range(a + 1, len(member_vecs)):
            if stems[a] == stems[b]:
                continue
            if _dot(member_vecs[a], member_vecs[b]) >= sim:
                # A cross-kickoff pair stands on its own, so the cluster is not
                # merely a centroid bridge — it is plainly the same problem.
                return False
    return True


def _clusters(findings: list[dict[str, Any]], *, sim: float) -> list[dict[str, Any]]:
    """Every cluster, worst-first (the body of :func:`recurring`).

    TF-IDF (IDF computed over *these* findings) + L2-normalized cosine, so
    ``_dot`` is a true cosine. Each cluster::

        {"representative": <issue nearest the centroid>, "n", "occurrences",
         "n_members", "n_runs", "kickoffs", "stems", "files": [top 3],
         "all_files": [every member file], "mixed", "worst_severity",
         "finding_ids"}

    ``n`` IS ``occurrences`` (LF-R10) — the raw member count moved to
    ``n_members`` — and ``occurrences`` counts distinct ``(kickoff stem, file)``
    pairs, so a restatement inside one kickoff-file adds nothing. ``files`` is a
    DISPLAY list capped at three; ``all_files`` holds every member file,
    uncapped. The cap is fine for a cell and wrong for a decision: the "Make it
    a rule" target is derived from the member files, so building it from the
    top-3 would silently mis-scope a cluster spread over more than three files —
    the cap-then-aggregate defect family already fixed twice in this file
    (f:51c3c885a5, f:6f09169399).

    Ordered by ``kickoffs``, then ``occurrences``, then severity. Findings whose
    text has no tokens are kept as singletons so nothing is silently dropped —
    but a finding whose issue is the ``(no issue)`` placeholder is *not* a
    problem at all, so it is dropped before clustering (reviewer, round 2: it
    topped the live recurring table).
    """
    items = [f for f in findings
             if str(f.get("issue") or "").strip().lower() != _PLACEHOLDER_ISSUE]
    if not items:
        return []

    docs = [_strip_issue(f.get("issue") or "") for f in items]
    df: dict[str, int] = {}
    for d in docs:
        for tok in set(_tokenize(d)):
            df[tok] = df.get(tok, 0) + 1
    n_docs = len(docs)
    idf = _df_to_idf(df, n_docs)
    unseen = _idf_unseen(n_docs)
    vecs = [_vector(d, idf, unseen) for d in docs]

    clusters: list[dict[str, Any]] = []
    for i, v in enumerate(vecs):
        if not v:
            clusters.append({"members": [i], "centroid": {}})
            continue
        best, best_sim = -1, 0.0
        for j, c in enumerate(clusters):
            if not c["centroid"]:
                continue
            score = _dot(v, c["centroid"])
            if score > best_sim:
                best_sim, best = score, j
        if best >= 0 and best_sim >= sim:
            members = clusters[best]["members"]
            members.append(i)
            clusters[best]["centroid"] = _l2norm(_sum_vecs(vecs[m] for m in members))
        else:
            clusters.append({"members": [i], "centroid": v})

    out: list[dict[str, Any]] = []
    for c in clusters:
        members = c["members"]
        member_vecs = [vecs[i] for i in members]
        centroid = _l2norm(_sum_vecs(member_vecs))
        if centroid:
            rep_i = max(members, key=lambda i: _dot(vecs[i], centroid))
        else:
            rep_i = members[0]
        sevs = [str(items[i].get("severity") or "low").lower() for i in members]
        worst = min(sevs, key=lambda s: _SEV_ORDER.get(s, 3))
        runs = {items[i].get("run_id") for i in members if items[i].get("run_id")}
        member_files = [items[i].get("file") for i in members]
        stems = [_member_stem(items[i]) for i in members]
        # One occurrence = one (kickoff, file) pair. Four restatements of one
        # bug in one run and one file are ONE occurrence (LF-R10).
        occurrences = {(stems[k], str(member_files[k] or ""))
                       for k in range(len(members))}
        out.append({
            "representative": str(items[rep_i].get("issue") or ""),
            "n": len(occurrences),        # LF-R10: n IS occurrences
            "occurrences": len(occurrences),
            "n_members": len(members),
            "n_runs": len(runs),
            "kickoffs": len(set(stems)),
            "stems": sorted(set(stems)),
            "files": _top_files(member_files),
            "all_files": _member_files(member_files),
            "mixed": _mixed_cluster(member_vecs, stems, sim),
            "worst_severity": worst,
            "finding_ids": [finding_key(items[i]) for i in members],
        })
    out.sort(key=lambda c: (-c["kickoffs"], -c["occurrences"],
                            _SEV_ORDER.get(c["worst_severity"], _SEV_ORDER["low"])))
    return out


def recurring(findings: list[dict[str, Any]], *, sim: float = _SIM,
              top: int = _TOP_CLUSTERS) -> list[dict[str, Any]]:
    """The clusters that are RECURRING — same problem in ≥ 2 kickoffs (pure).

    LF-R10: recurrence is counted per kickoff, not per row, so ``fire`` in a
    single run repeated four times never reaches this list. See ``_clusters``
    for the shape and the ordering; ``seen_once`` returns the complement.
    """
    return [c for c in _clusters(findings, sim=sim) if _is_recurring(c)][:top]


def seen_once(findings: list[dict[str, Any]], *, sim: float = _SIM,
              top: int = _SEEN_ONCE_TOP) -> list[dict[str, Any]]:
    """The clusters seen in only ONE kickoff (or in one kickoff-file) — pure.

    The complement of :func:`recurring` over the same clustering, in the same
    shape and order, so the two lists partition the clustered findings. These
    are SHOWN (never hidden) but carry no action: one kickoff is not yet a
    recurrence, so there is nothing to draft a rule from.
    """
    return [c for c in _clusters(findings, sim=sim) if not _is_recurring(c)][:top]


def _top_files(files) -> list[str]:
    counts: dict[str, int] = {}
    for f in files:
        if f:
            counts[str(f)] = counts.get(str(f), 0) + 1
    return [f for f, _ in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:3]]


def _member_files(files) -> list[str]:
    """Every distinct member file, in first-seen order — ``_top_files`` uncapped.

    The "Make it a rule" target is a decision about the whole cluster, so it
    reads this, never the three-item display list.
    """
    out: list[str] = []
    seen: set[str] = set()
    for f in files:
        if not f:
            continue
        s = str(f)
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


# ── cluster display helpers (pure) ─────────────────────────────────────────

def _occurrence_label(c: dict[str, Any]) -> str:
    """``4×`` — how many kickoff-file occurrences the cluster was seen in."""
    return f"{int(c.get('occurrences') or 0)}×"


def _kickoffs_cell(c: dict[str, Any]) -> str:
    """The table's kickoffs cell: the stem count, marked ``(mixed)`` when the
    members joined only through the centroid (see :func:`_mixed_cluster`)."""
    n = int(c.get("kickoffs") or 0)
    return f"{n} (mixed)" if c.get("mixed") else str(n)


def _files_cell(c: dict[str, Any]) -> str:
    """The distinct member files, display-capped (``all_files`` drives decisions)."""
    return ", ".join(short(f, 40) for f in c.get("files") or []) or "—"


def _stems_label(c: dict[str, Any]) -> str:
    """A cluster's kickoff stems, named — a stem-less cluster says so."""
    stems = [s for s in c.get("stems") or [] if s]
    return ", ".join(stems) or "an unnamed kickoff"
