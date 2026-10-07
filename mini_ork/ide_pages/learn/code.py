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
   action is ``prefs set --scope path``: a rule injected into every future run
   whose file scope matches.

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
from mini_ork.learning.code_findings import areas, findings_for
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
FINDINGS_TITLE = "Findings"
FINDING_TITLE = "Finding"

_DAYS_DEFAULT = 30
_DAYS_CHIPS = (7, 30, 90)
_SIM = 0.35           # greedy-leader cosine threshold
_TOP_CLUSTERS = 5     # clusters returned by recurring()
_RECURRING_PER_AREA = 2   # representatives shown in an area row
_REPR_MAX = 70        # area-row "recurring problems" cell cap
_REPR_DETAIL = 160    # recurring-table problem cap
_ISSUE_MAX = 120      # findings-table problem cap
_AREA_FINDINGS = 50   # findings pulled per area

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


def _freshness(home: Path) -> dict[str, Any]:
    n, last = _harvest_summary(home)
    note = "Updated after every reflect pass."
    if not n:
        return S.kv(KV_TITLE, [("Harvested", "nothing yet", "sub", note)])
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

def _areas_table(home: Path, args: dict[str, str]) -> dict[str, Any]:
    db = _db_path(home)
    rows = areas(db=db, since_days=_days(args))
    if not rows:
        return S.lst(AREAS_TITLE,
                     [S.dot("No review findings yet. They appear after your runs are reviewed.")],
                     full=True)
    now = int(time.time())
    selected = str(args.get("area") or "")
    out_rows = []
    for a in rows:
        problems = " · ".join(
            short(c["representative"], _REPR_MAX)
            for c in _recurring_for_area(home, a)[:_RECURRING_PER_AREA]
        ) or "—"
        out_rows.append({
            "cells": [
                S.mono(a["area"]),
                S.cell(a["n_findings"]),
                S.cell(a["n_runs"]),
                S.cell(a["worst_severity"], _sev_colour(a["worst_severity"])),
                S.cell(problems, "sub"),
                S.cell(S.age(a["last_ts"], now), "sub"),
            ],
            "do": S.set_args(area=a["area"]),
            "sel": selected == a["area"],
        })
    return S.table(
        AREAS_TITLE,
        [S.col(fr=2, min=120), S.col(width=70), S.col(width=60), S.col(width=70),
         S.col(fr=3, min=160), S.col(width=60)],
        ["area", "findings", "runs", "worst", "recurring problems", "last"],
        out_rows, full=True,
        note=("From every reviewer and verifier finding in your runs. Click an area to see "
              "each finding and turn a recurring problem into a rule."))


def _findings_for_area(db: str, area: str) -> list[dict[str, Any]]:
    """Findings belonging to *exactly* ``area`` — never a sibling that shares
    its name prefix.

    ``findings_for`` matches a path *prefix* (``file LIKE area || '%'``), which
    is both too wide and ambiguous:

    * a bare directory area ``mini_ork/acp`` would also sweep in the sibling
      directory ``mini_ork/acp_orchestrator/*`` (both exist in this tree; so do
      ``ui/src/context`` vs ``ui/src/contexts`` and ``kickoffs/sdd`` vs
      ``kickoffs/sdd-mechanisms``);
    * whether the area is a file or a directory cannot be read off its suffix —
      ``bin/mini-ork`` and ``Makefile`` are files, ``svc/pay`` is a directory,
      yet none carries an extension. Round 2 regression: ``Path(area).suffix``
      sent every extensionless *file* down the directory branch, so
      ``findings_for("bin/mini-ork/")`` matched nothing and its findings
      vanished.

    So the *data* decides, not the name. Query both shapes and merge:

    * **file** → rows whose ``file`` equals the area exactly;
    * **directory** → rows under ``area + "/"``, the trailing separator forcing
      LIKE to match a real path boundary and therefore only true descendants.

    A given area matches at most one shape, so the merge never double-counts.
    Newest first, capped, to match ``findings_for``'s own ordering.
    """
    base = area.rstrip("/")
    exact = [f for f in findings_for(base, db=db, limit=_AREA_FINDINGS)
             if str(f.get("file") or "") == base]
    under = findings_for(base + "/", db=db, limit=_AREA_FINDINGS)
    merged = exact + under
    merged.sort(key=lambda f: int(f.get("ts") or 0), reverse=True)
    return merged[:_AREA_FINDINGS]


def _recurring_for_area(home: Path, area_row: dict[str, Any]) -> list[dict[str, Any]]:
    """Clusters for one area. ``findings_for`` matches a prefix (see
    ``_findings_for_area``); a bare-filename area (a single file that dominates)
    falls back to its top file's full path."""
    db = _db_path(home)
    fs = _findings_for_area(db, str(area_row["area"]))
    if not fs and area_row.get("files"):
        fs = _findings_for_area(db, str(area_row["files"][0][0]))
    return recurring(fs)


# ── section: area detail (+ one finding) ───────────────────────────────────

def _area_detail(home: Path, args: dict[str, str], area: str) -> list[dict[str, Any]]:
    db = _db_path(home)
    fs = _findings_for_area(db, area)
    clusters = recurring(fs)

    # The summary line must describe the rows listed below, so it is derived
    # from ``fs`` itself — never from an ``areas()`` group row. A group row
    # counts every file in the directory, but the label and this view are the
    # area's own findings, so a directory group labelled with its dominant file
    # would overstate the count and worst severity (reviewer, round 2).
    n_runs = len({f.get("run_id") for f in fs if f.get("run_id")})
    sevs = [str(f.get("severity") or "low").lower() for f in fs]
    worst = min(sevs, key=lambda s: _SEV_ORDER.get(s, 3)) if sevs else "low"
    last = max((int(f.get("ts") or 0) for f in fs), default=0)
    date = time.strftime("%Y-%m-%d", time.localtime(last)) if last else "—"
    # A file area targets its own path; a directory area targets ``area/**``.
    # Decided from the data (see ``_findings_for_area``) — the suffix cannot
    # tell ``bin/mini-ork`` (file) from ``svc/pay`` (directory).
    is_file = any(str(f.get("file") or "") == area.rstrip("/") for f in fs)
    lines = [f"{len(fs)} findings in {n_runs} run{'' if n_runs == 1 else 's'}, "
             f"worst {worst}, last {date}", "", "**Recurring problems**"]
    if clusters:
        for c in clusters:
            files = ", ".join(short(f, 60) for f in c["files"]) or "—"
            lines.append(f"- **{c['n']}×** {short(c['representative'], _REPR_DETAIL)} "
                         f"(files: {files})")
    else:
        lines.append("- none yet")
    # Close clears the area AND any open finding, so the header button does both.
    out: list[dict[str, Any]] = [
        S.markdown(area, "\n".join(lines), full=True,
                   actions=[S.btn("Close area", S.set_args(area="", finding=""), "ghost")])]

    out.append(S.table(
        RECURRING_TITLE,
        [S.col(fr=3, min=180), S.col(width=60), S.col(width=60), S.col(width=70),
         S.col(width=130)],
        ["problem", "times", "runs", "worst", "action"],
        [{"cells": [
            S.cell(short(c["representative"], _REPR_DETAIL)),
            S.cell(f"{c['n']}×"),
            S.cell(c["n_runs"]),
            S.cell(c["worst_severity"], _sev_colour(c["worst_severity"])),
            S.cell("Make it a rule", "blue"),
        ], "do": _rule_action(area, c, is_file=is_file)} for c in clusters],
        full=True,
        note=("Pick a recurring problem and mini-ork will tell every run that touches "
              "this path to avoid it.")))

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
            "sel": selected == finding_key(f)} for f in fs],
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

def _rule_action(area: str, cluster: dict[str, Any], *, is_file: bool) -> dict[str, Any]:
    """The "Make it a rule" CLI action for one recurring problem in ``area``.

    ``is_file`` is decided by the data (see ``_findings_for_area``): a file area
    targets its own path, a directory area targets ``area/**``. It is a required
    keyword so no caller can silently re-introduce the suffix guess that treated
    ``bin/mini-ork`` (a file) as a directory and emitted a glob that never
    matches (reviewer, round 2).
    """
    rep = cluster["representative"]
    slug = hashlib.sha1(rep.encode("utf-8")).hexdigest()[:8]
    base = area.rstrip("/")
    glob = base if is_file else base + "/**"
    text = f"In {area}: avoid this recurring review finding — {rep}"
    return S.cli("prefs", "set", f"review-{slug}", text, "--scope", "path", "--target", glob,
                 confirm=f"Add a rule for {glob}? Every run that touches it will be told this.")


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


def recurring(findings: list[dict[str, Any]], *, sim: float = _SIM,
              top: int = _TOP_CLUSTERS) -> list[dict[str, Any]]:
    """Greedy-leader clustering of the findings' ``issue`` texts (pure).

    TF-IDF (IDF computed over *these* findings) + L2-normalized cosine, so
    ``_dot`` is a true cosine. Each cluster::

        {"representative": <issue nearest the centroid>, "n", "n_runs",
         "files": [top 3], "worst_severity", "finding_ids"}

    Ordered by ``n`` then severity. Findings whose text has no tokens are kept
    as singletons so nothing is silently dropped.
    """
    items = list(findings)
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
        centroid = _l2norm(_sum_vecs(vecs[i] for i in members))
        if centroid:
            rep_i = max(members, key=lambda i: _dot(vecs[i], centroid))
        else:
            rep_i = members[0]
        sevs = [str(items[i].get("severity") or "low").lower() for i in members]
        worst = min(sevs, key=lambda s: _SEV_ORDER.get(s, 3))
        runs = {items[i].get("run_id") for i in members if items[i].get("run_id")}
        out.append({
            "representative": str(items[rep_i].get("issue") or ""),
            "n": len(members),
            "n_runs": len(runs),
            "files": _top_files([items[i].get("file") for i in members]),
            "worst_severity": worst,
            "finding_ids": [finding_key(items[i]) for i in members],
        })
    out.sort(key=lambda c: (-c["n"], _SEV_ORDER.get(c["worst_severity"], _SEV_ORDER["low"])))
    return out[:top]


def _top_files(files) -> list[str]:
    counts: dict[str, int] = {}
    for f in files:
        if f:
            counts[str(f)] = counts.get(str(f), 0) + 1
    return [f for f, _ in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:3]]
