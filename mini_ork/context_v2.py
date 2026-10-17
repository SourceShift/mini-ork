"""Context v2 — select prompt context by the files a run touches, not its task class.

The v1 context (``context_assembler``) keys every section on ``task_class`` and
recency, so every framework_edit run gets the same ~1.6k tokens of
mini-ork-internal telemetry critique regardless of what it is building. The
SDD evidence review (docs/plans/2026-10-07-sdd-mechanisms-for-mini-ork.md)
found generic context files to be null-to-negative for agent correctness, and
narrow, specific, negatively framed constraints to help. This module builds
that narrow context:

- the kickoff's own contract (files in scope, out of scope, "Do NOT" lines,
  verification commands), parsed deterministically;
- the problems reviewers and verifiers already found in exactly those files
  (``code_findings``), grouped into recurring problems by TF-IDF similarity
  so the same mistake in different files is one item;
- earlier attempts at the same kickoff and what their reviews found.

Every item carries a stable id (``c:<n>``, ``f:<hash>``, ``p:<run_id>``) so a
planner can acknowledge what it used and a later pass can measure whether an
injected problem recurred.

This module is pure selection, rendering and measurement. It never writes to
the database and never raises on the prompt path: missing tables or files give
empty sections. Wiring lives in ``plan._inject_context`` and
``execute._learned_block``.

Modes (``MO_CONTEXT_V2``): ``off`` | ``shadow`` (default — build and record the
pack, inject nothing new) | ``on``. Under ``on``, a deterministic
``MO_CONTEXT_V2_HOLDOUT`` share of runs (default 0.2) keeps the v1 context so
recurrence can be compared against a held-out arm.

In the ``v2`` arm the pack's task_class-keyed sections replace their v1
counterparts (failure modes, prior runs); the rest of the v1 learned block —
verified emergent patterns, operator preferences, steering — is still rendered
per node, so this is not a wholesale replacement of the v1 blocks.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import tempfile
import time

from mini_ork.context import context_env
from mini_ork.kickoff_scope import do_not_sentences, heading_kind, kickoff_scope
from mini_ork.learning import code_findings, themes

MODES = ("off", "shadow", "on")
DEFAULT_MODE = "shadow"
DEFAULT_HOLDOUT = 0.2
DEFAULT_SIM = 0.35
# Paraphrase tolerance (LF-R13 §3): a reworded restatement often falls below the
# TF-IDF cosine floor while sharing the same token set, or embeds to nearly the
# same point. Both arms are additive — the TF-IDF rule above is unchanged.
CONTAINMENT_MIN = 0.6
EMBED_MIN = 0.6
PACK_FILENAME = "context-pack.v2.json"
# The full success command reaches every node; only an absurd one is cut, and
# the cut is made visible (change 4).
_VERIFY_CMD_MAX = 2000

# ``unknown`` (rank 0) is the label ``code_findings.normalize_severity`` gives a
# finding that carried no severity token. It ranks below every real label, so the
# default ``MO_CONTEXT_V2_MIN_SEVERITY=medium`` floor excludes it instead of
# letting the old "medium" fallback pass 83% of rows (LF-R5 §5).
_SEVERITY_RANK = {"critical": 5, "blocker": 5, "high": 4, "medium": 3, "low": 2,
                  "info": 1, "unknown": 0}
_SEVERITY_WORDS_RE = re.compile(
    r"\b(blocker|critical|major|minor|high|medium|low|nit|info|p[0-3])\b[:\s-]*", re.I)
# A path with a directory (optionally :line[-line]), or a bare file name WITH a
# line number. A bare ``plan.json`` stays: it usually names the problem itself.
_FILE_LINE_RE = re.compile(
    r"(?:\b[\w.-]+/)+[\w.-]+(?::\d+(?:-\d+)?)?|\b[\w.-]+\.\w{1,5}:\d+(?:-\d+)?")
_FILE_EXT_RE = re.compile(
    r"\.(py|pyi|md|ts|tsx|js|jsx|rs|go|sh|sql|yaml|yml|json|toml|txt|css|html|cfg|ini)$", re.I)
_GENERIC_KICKOFF_STEMS = frozenset({
    "kickoff", "wave-kickoff", "child-kickoff", "example-kickoff", "strong", "weak"})
_HEADING_RE = re.compile(r"^\s{0,3}(#{2,6})\s+(.+?)\s*$")
_FENCE_RE = re.compile(r"^\s{0,3}(```|~~~)")
_BACKTICK_CMD_RE = re.compile(r"`([^`\n]{3,300})`")
_DIFF_HEADER_RE = re.compile(r"^diff --git a/(\S+) b/(\S+)\s*$")


# ── mode / holdout ───────────────────────────────────────────────────────────

def mode() -> str:
    """``MO_CONTEXT_V2`` normalized to one of :data:`MODES` (default shadow)."""
    value = context_env("MO_CONTEXT_V2", DEFAULT_MODE).strip().lower()
    return value if value in MODES else DEFAULT_MODE


def holdout_rate() -> float:
    try:
        rate = float(context_env("MO_CONTEXT_V2_HOLDOUT", str(DEFAULT_HOLDOUT)))
    except ValueError:
        return DEFAULT_HOLDOUT
    return min(max(rate, 0.0), 1.0)


def min_severity() -> int:
    """``MO_CONTEXT_V2_MIN_SEVERITY`` as a rank (default medium). Low-severity
    review notes are mostly harness remarks, not lessons about the code."""
    name = context_env("MO_CONTEXT_V2_MIN_SEVERITY", "medium").strip().lower()
    return _SEVERITY_RANK.get(name, _SEVERITY_RANK["medium"])


def in_holdout(run_id: str, rate: float | None = None) -> bool:
    """Deterministic per run: the same run id always lands in the same arm."""
    rate = holdout_rate() if rate is None else rate
    if not run_id or rate <= 0.0:
        return False
    bucket = int(hashlib.sha1(run_id.encode("utf-8")).hexdigest()[:8], 16) / 0xFFFFFFFF
    return bucket < rate


# ── kickoff contract ─────────────────────────────────────────────────────────

def _sections(text: str) -> list[tuple[str, list[str]]]:
    """``(lowercased heading, body lines)`` for every ``##``+ heading. Headings
    inside fenced code are body, not structure."""
    out: list[tuple[str, list[str]]] = []
    title, body, in_fence = "", [], False
    for line in (text or "").splitlines():
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            body.append(line)
            continue
        m = None if in_fence else _HEADING_RE.match(line)
        if m:
            out.append((title, body))
            title, body = m.group(2).strip().lower(), []
        else:
            body.append(line)
    out.append((title, body))
    return [(t, b) for t, b in out if t or any(x.strip() for x in b)]


def _fenced_blocks(lines: list[str]) -> list[str]:
    blocks, buf, in_fence = [], [], False
    for line in lines:
        if _FENCE_RE.match(line):
            if in_fence:
                blocks.append("\n".join(buf).strip())
                buf = []
            in_fence = not in_fence
            continue
        if in_fence:
            buf.append(line)
    return [b for b in blocks if b]


def _bullets(lines: list[str]) -> list[str]:
    out = []
    for line in lines:
        m = re.match(r"^\s*[-*+]\s+(.*\S)", line)
        if m:
            out.append(m.group(1).strip())
    return out


def parse_contract(kickoff_text: str) -> dict:
    """The kickoff's own constraints, parsed without an LLM.

    ``files_in_scope`` — ``kickoff_scope(text)["allow"]``: the paths of every
    allow section, bare bullets included, with a scope entry required to look
    like a file (a directory part, or a known file extension — a dotted symbol
    such as ``code_findings.harvest`` is not one);
    ``out_of_scope`` — the bullets of every deny section;
    ``do_not`` — the sentences that START with "Do not / Don't / Never / Must
    not" (``do_not_sentences``), capped at 10;
    ``verification`` — fenced commands (else backticked commands) in a
    "Verification" section.

    The three scope readers agree by construction: ``files_in_scope``,
    ``gen_profile``'s ``scope_allow`` and ``preferences.scope_paths`` all read
    the same parser (LF-R8). Return keys are unchanged.
    """
    sections = _sections(kickoff_text)
    out_of_scope: list[str] = []
    verification: list[str] = []
    for title, body in sections:
        if heading_kind(title) == "deny":
            out_of_scope.extend(_bullets(body))
        elif "verification" in title or title.startswith("verify"):
            fenced = _fenced_blocks(body)
            if fenced:
                verification.extend(fenced)
            else:
                verification.extend(m.group(1).strip()
                                    for m in _BACKTICK_CMD_RE.finditer("\n".join(body)))
    # ``kickoff_scope`` also accepts dotted symbols (``code_findings.harvest``);
    # a scope entry must look like a path: a directory part or a file extension.
    files = [p for p in kickoff_scope(kickoff_text)["allow"]
             if "/" in p or _FILE_EXT_RE.search(p)]
    return {
        "files_in_scope": files,
        "out_of_scope": out_of_scope[:10],
        "do_not": do_not_sentences(kickoff_text)[:10],
        "verification": verification[:5],
    }


# ── database (read-only) ─────────────────────────────────────────────────────

def _db_path(db: str | None) -> str | None:
    if db:
        return db
    env = context_env("MINI_ORK_DB")
    if env:
        return env
    home = context_env("MINI_ORK_HOME")
    if home:
        candidate = os.path.join(home, "state.db")
        if os.path.isfile(candidate):
            return candidate
    return None


def _connect_ro(db: str | None) -> sqlite3.Connection | None:
    path = _db_path(db)
    if not path or not os.path.isfile(path):
        return None
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0)
        con.row_factory = sqlite3.Row
        return con
    except sqlite3.Error:
        return None


def _like_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _strip_glob(path: str) -> str:
    return re.sub(r"/\*\*?$", "", path).rstrip("/")


def engine_identity() -> str | None:
    """The engine checkout's repo identity — ``repo_identity(MINI_ORK_ROOT)``,
    else the package parent (the mini-ork repository root).

    Reads ``MINI_ORK_ROOT`` through ``context_env`` so a run-scoped binding wins
    over the process env, matching every other reader in this module. Never
    raises (``repo_identity`` is fail-soft).
    """
    return code_findings.engine_repo_identity(context_env("MINI_ORK_ROOT") or None)


def resolve_target_repo(target_repo: str | None = None) -> str | None:
    """The repo identity a selection is scoped to (LF-R12 §4).

    An explicit ``target_repo`` wins; otherwise ``MO_TARGET_CWD`` (the directory
    the dispatched agent ran in) is resolved with ``repo_identity``; otherwise
    the engine checkout's identity. A cwd that no longer resolves (deleted,
    non-git) degrades to the engine rather than ``None``, because ``None`` at
    the query layer means "unscoped" and would turn the filter off.
    """
    if target_repo:
        return target_repo
    cwd = context_env("MO_TARGET_CWD")
    if cwd:
        ident = code_findings.repo_identity(cwd)
        if ident:
            return ident
    return engine_identity()


def _has_target_repo(con: sqlite3.Connection) -> bool:
    """True when this DB carries the LF-R12 column.

    A DB migrated only to 0064 (or a fixture built from that shape) has no
    ``target_repo``; filtering on it would raise and blank every section. The
    filter is skipped there — the same fail-soft rule the rest of the reader
    follows for missing tables/columns.
    """
    try:
        return any(r[1] == "target_repo"
                   for r in con.execute("PRAGMA table_info(code_findings)"))
    except sqlite3.Error:
        return False


def _repo_clause(target_repo: str | None, engine: str | None) -> tuple[str, list]:
    """``(SQL predicate, params)`` for the registered-repo rule.

    A row is kept when ``target_repo`` equals the current identity, or when it
    is NULL AND the current identity IS the engine checkout. NULL is never a
    wildcard (Q8): a finding with no recorded repo belongs to the engine only.
    An unknown current identity yields a predicate that matches nothing rather
    than every row.
    """
    if not target_repo:
        return "0", []
    if engine and target_repo == engine:
        return "(target_repo = ? OR target_repo IS NULL)", [target_repo]
    return "target_repo = ?", [target_repo]


def _query_findings(con: sqlite3.Connection, prefix: str, limit: int,
                    exclude_run: str | None, target_repo: str | None = None,
                    engine: str | None = None,
                    age_cutoff: int | None = None) -> list[dict]:
    if target_repo and _has_target_repo(con):
        clause, clause_params = _repo_clause(target_repo, engine)
    else:
        clause, clause_params = "", []
    where = "file LIKE ? ESCAPE '\\' AND (? = '' OR run_id != ?)"
    params: list = [_like_escape(prefix) + "%", exclude_run or "", exclude_run or ""]
    if clause:
        where += " AND " + clause
        params += clause_params
    if age_cutoff is not None:
        # LF-R11a: the max-age window is a SQL predicate, not a Python pass over
        # an already-limited page — otherwise an old crowd would fill the LIMIT
        # and hide the fresh rows the run should see.
        where += " AND ts >= ?"
        params.append(int(age_cutoff))
    rows = con.execute(
        f"""
        SELECT id, fingerprint, run_id, file, line, severity, category, issue,
               snippet, ts
        FROM code_findings
        WHERE {where}
        ORDER BY ts DESC LIMIT ?
        """,
        params + [int(limit)],
    ).fetchall()
    return [dict(r) for r in rows]


def _sort_newest(rows, limit: int) -> list[dict]:
    return sorted(rows, key=lambda r: str(r.get("ts") or ""), reverse=True)[:limit]


def scope_findings_with_stats(paths: list[str], *, db: str | None = None,
                              limit: int = 300, exclude_run: str | None = None,
                              target_repo: str | None = None) -> tuple[list[dict], dict]:
    """``scope_findings`` plus the decay counts (LF-R11a).

    Returns ``(fresh_rows, stats)`` where ``stats`` carries
    ``n_findings_scanned`` (the pre-decay count), ``n_findings_stale`` (rows the
    age window or the HEAD check dropped) and ``max_age_days``. The scan count
    is taken from the same predicate applied without the age term, so the two
    counts can never contradict each other.
    """
    max_age = code_findings.max_age_days_from_env()
    stats = {"n_findings_scanned": 0, "n_findings_stale": 0,
             "max_age_days": max_age}
    con = _connect_ro(db)
    if con is None:
        return [], stats
    engine = engine_identity()
    # Staleness reads the target repo's HEAD: the explicit identity when given,
    # else MO_TARGET_CWD's identity, else the engine checkout (R12's rule).
    repo_root = resolve_target_repo(target_repo)
    age_cutoff = int(time.time()) - max_age * 86400 if max_age > 0 else None
    try:
        # Phase 1 — the pre-decay scan. It also fixes the prefix/fallback choice
        # for each scope path, so both phases read exactly the same path set.
        scanned: dict[int, dict] = {}
        prefixes: list[str] = []
        for raw in paths:
            prefix = _strip_glob(raw)
            if not prefix:
                continue
            rows = _query_findings(con, prefix, limit, exclude_run, target_repo,
                                   engine)
            if not rows and "." in os.path.basename(prefix):
                parent = os.path.dirname(prefix)
                if parent.count("/") >= 2:
                    prefix = parent + "/"
                    rows = _query_findings(con, prefix, limit, exclude_run,
                                           target_repo, engine)
            prefixes.append(prefix)
            for r in rows:
                scanned.setdefault(r["id"], r)
        stats["n_findings_scanned"] = len(_sort_newest(scanned.values(), limit))

        # Phase 2 — the same prefixes with the age window pushed into SQL.
        survivors: dict[int, dict] = {}
        for prefix in prefixes:
            for r in _query_findings(con, prefix, limit, exclude_run, target_repo,
                                     engine, age_cutoff):
                survivors.setdefault(r["id"], r)

        annotated = code_findings.annotate_staleness(
            _sort_newest(survivors.values(), limit),
            repo_root=repo_root, max_age_days=max_age,
        )
        fresh = [r for r in annotated if not r.get("stale")]
        stats["n_findings_stale"] = max(
            0, stats["n_findings_scanned"] - len(fresh))
        return fresh, stats
    except sqlite3.Error:
        return [], stats
    finally:
        con.close()


def scope_findings(paths: list[str], *, db: str | None = None, limit: int = 300,
                   exclude_run: str | None = None,
                   target_repo: str | None = None) -> list[dict]:
    """Review/verifier findings for the in-scope paths, newest first.

    Each path matches as a prefix. A file with no findings of its own (often a
    new file) falls back to its parent directory, but only when that directory
    is at least three components deep — ``mini_ork/ide_pages/learn`` is a
    neighbourhood, ``mini_ork/cli`` is half the codebase. Both the exact-path
    query and the parent fallback apply the same ``target_repo`` filter
    (LF-R12 §4), so a sibling repo's findings can never leak in through the
    fallback. ``target_repo=None`` reads unscoped, preserving the legacy call
    shape; ``build`` always passes a resolved identity.

    LF-R11a: rows older than ``MO_CONTEXT_V2_MAX_AGE_DAYS`` (default 30) and
    rows whose ``snippet`` no longer occurs at the target repo's HEAD are
    dropped at read time. ``scope_findings_with_stats`` reports the counts;
    this signature and return type are unchanged for every other caller.
    """
    return scope_findings_with_stats(paths, db=db, limit=limit,
                                     exclude_run=exclude_run,
                                     target_repo=target_repo)[0]


def run_findings(run_id: str, *, db: str | None = None) -> list[dict]:
    """Findings harvested from one run (empty until the run is harvested)."""
    con = _connect_ro(db)
    if con is None:
        return []
    try:
        rows = con.execute(
            "SELECT id, fingerprint, run_id, file, line, severity, category, issue, ts "
            "FROM code_findings WHERE run_id = ?", (run_id,)).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.Error:
        return []
    finally:
        con.close()


def kickoff_stem(path: str | None) -> str:
    """Revision-insensitive kickoff identity: ``eng-path-rules-r2.md`` →
    ``eng-path-rules``. Generic names (``kickoff.md``, ``probe-3.md``) have no
    identity and return ``""`` so they never match unrelated runs."""
    stem = os.path.splitext(os.path.basename(path or ""))[0].lower()
    stem = re.sub(r"-r\d+$", "", stem)
    if (not stem or stem in _GENERIC_KICKOFF_STEMS or re.fullmatch(r"probe-\d+", stem)
            or "-" not in stem):
        return ""
    return stem


def prior_attempts(kickoff_path: str, *, db: str | None = None,
                   current_run: str | None = None, limit: int = 3,
                   target_repo: str | None = None) -> list[dict]:
    """Earlier runs of the same kickoff file, newest first, with their findings.

    Each earlier finding is filtered before it is injected (LF-R5 §6): it must
    clear ``min_severity()`` (so an unlabelled ``unknown`` row is dropped) and
    its issue must classify as ``code`` under
    :func:`mini_ork.learning.code_findings.classify_note` (so a receipt, harness
    note or process complaint is never carried into the next run's prompt).

    LF-R12 §4 adds the same repo filter as ``scope_findings``: a finding from an
    earlier attempt at the same kickoff is still irrelevant when that attempt
    ran against a different repository. ``target_repo=None`` leaves the scan
    unscoped for legacy callers; ``build`` passes a resolved identity.
    """
    if not kickoff_path:
        return []
    con = _connect_ro(db)
    if con is None:
        return []
    floor = min_severity()
    engine = engine_identity()
    repo_filter = bool(target_repo) and _has_target_repo(con)
    out = []
    try:
        stem = kickoff_stem(kickoff_path)
        candidates = con.execute(
            """
            SELECT id, status, cost_usd, created_at, kickoff_path FROM task_runs
            WHERE (kickoff_path = ? OR (? != '' AND kickoff_path LIKE ? ESCAPE '\\'))
              AND (? = '' OR id != ?)
            ORDER BY created_at DESC LIMIT 50
            """,
            (kickoff_path, stem, "%/" + _like_escape(stem) + "%", current_run or "",
             current_run or ""),
        ).fetchall()
        runs = [r for r in candidates
                if r["kickoff_path"] == kickoff_path
                or (stem and kickoff_stem(r["kickoff_path"]) == stem)][:int(limit)]
        for r in runs:
            sql = ("SELECT file, line, severity, issue FROM code_findings "
                   "WHERE run_id = ?")
            params: list = [r["id"]]
            if repo_filter:
                clause, clause_params = _repo_clause(target_repo, engine)
                sql += " AND " + clause
                params += clause_params
            sql += " ORDER BY ts DESC LIMIT 20"
            try:
                found = con.execute(sql, params).fetchall()
            except sqlite3.Error:
                found = []
            kept = [
                f for f in found
                if _SEVERITY_RANK.get(str(f["severity"] or "").lower(), 0) >= floor
                and code_findings.classify_note(f["issue"]) == "code"
            ]
            ranked = sorted(kept, key=lambda f: -_SEVERITY_RANK.get(
                str(f["severity"] or "").lower(), 0))[:3]
            out.append({
                "id": f"p:{r['id']}",
                "run_id": r["id"],
                "status": r["status"],
                "findings": [
                    {"file": f["file"], "line": f["line"], "severity": f["severity"],
                     "issue": (f["issue"] or "")[:200]}
                    for f in ranked],
            })
    except sqlite3.Error:
        return []
    finally:
        con.close()
    return out


def verified_themes(task_class: str, *, db: str | None = None,
                    limit: int = 5) -> list[dict]:
    """Authored, verified theme lessons for ``task_class``. The v1 arm has
    carried these since the ``lesson_themes`` table landed, but the v2 pack —
    the LIVE arm — never did, so an approved lesson could reach a holdout run
    and not a normal one.

    Selection is delegated to ``themes.select_verified_themes`` so this reader
    and ``context_assembler._verified_theme_lessons`` can never disagree: own
    task class first, then a class that is one member of a comma-joined set,
    then the legacy ``'*'`` wildcard only for leftover slots. Cold-safe: a
    missing table or column is an empty list, because this runs on the
    injection path where a raise would abort a dispatch whose spend already
    happened.
    """
    con = _connect_ro(db)
    if con is None or limit <= 0:
        return []
    try:
        rows = themes.select_verified_themes(con, task_class, int(limit))
    except sqlite3.Error:
        return []
    finally:
        con.close()
    return [{"id": f"t:{r['theme_id']}", "text": r["lesson_text"],
             "n_runs": int(r["n_runs"] or 0)} for r in rows]


# ── recurring problems (TF-IDF, themes helpers) ──────────────────────────────

def clean_issue(text: str) -> str:
    """Drop severity words and file:line references so the same mistake in
    different files vectorizes the same way."""
    text = _SEVERITY_WORDS_RE.sub(" ", text or "")
    text = _FILE_LINE_RE.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def _idf_over(texts: list[str]) -> tuple[dict[str, float], float]:
    df: dict[str, int] = {}
    for t in texts:
        for tok in set(themes._tokenize(t)):
            df[tok] = df.get(tok, 0) + 1
    n = len(texts)
    return themes._df_to_idf(df, n), math.log(1.0 + n)


def _sev(finding: dict) -> int:
    return _SEVERITY_RANK.get(str(finding.get("severity") or "").lower(), 0)


def cluster(findings: list[dict], *, sim: float = DEFAULT_SIM) -> list[dict]:
    """Greedy-leader clustering of findings' ``issue`` texts.

    IDF is computed over the given findings. Each cluster reports its
    representative (the member closest to the centroid), size, distinct runs,
    top files, worst severity and member ids; ordered by size, then severity,
    then recency.
    """
    texts = [clean_issue(f.get("issue") or "") for f in findings]
    idf, unseen = _idf_over(texts)
    # Leaders are picked most-severe first, so a cluster is named after its worst case.
    order = sorted(range(len(findings)), key=lambda i: -_sev(findings[i]))
    groups: list[dict] = []
    for i in order:
        vec = themes._vector(texts[i], idf, unseen)
        if not vec:
            continue
        best, best_sim = None, sim
        for g in groups:
            s = themes._dot(vec, g["centroid"])
            if s >= best_sim:
                best, best_sim = g, s
        if best is None:
            groups.append({"centroid": vec, "members": [(i, vec)]})
            continue
        best["members"].append((i, vec))
        summed: dict[str, float] = {}
        for _, v in best["members"]:
            for t, w in v.items():
                summed[t] = summed.get(t, 0.0) + w
        best["centroid"] = themes._l2norm(summed)
    clusters = []
    for g in groups:
        members = [findings[i] for i, _ in g["members"]]
        rep_i = max(g["members"], key=lambda m: themes._dot(m[1], g["centroid"]))[0]
        representative = (findings[rep_i].get("issue") or "").strip()
        files: dict[str, int] = {}
        for m in members:
            if m.get("file"):
                files[m["file"]] = files.get(m["file"], 0) + 1
        worst = max(members, key=_sev)
        key = themes.normalize(clean_issue(representative))
        clusters.append({
            "id": "f:" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:10],
            "representative": representative[:240],
            "n": len(members),
            "n_runs": len({m.get("run_id") for m in members}),
            "files": [f for f, _ in sorted(files.items(), key=lambda kv: -kv[1])[:3]],
            "worst_severity": worst.get("severity") or "",
            "finding_ids": [m.get("id") for m in members],
            "last_ts": max(str(m.get("ts") or "") for m in members),
        })
    clusters.sort(key=lambda c: c["last_ts"], reverse=True)  # stable tie-break: newest first
    clusters.sort(key=lambda c: (-c["n"], -_SEVERITY_RANK.get(str(c["worst_severity"]).lower(), 0)))
    return clusters


def _token_set(text: str) -> set[str]:
    """The cleaned issue text's token set, using the same tokenizer the TF-IDF
    arm uses (stopwords and short tokens already dropped)."""
    return set(themes._tokenize(clean_issue(text)))


def _containment(a: set[str], b: set[str]) -> float:
    """``|a ∩ b| / min(|a|, |b|)`` — symmetric token containment; ``0.0`` when
    either side is empty.

    The smaller side is the denominator so a restatement that is either a
    superset or a subset of the cluster's problem scores 1.0: "the verifier
    ignores exit code 124" and "ignores exit code 124" contain each other,
    which is exactly the paraphrase case the cosine floor misses. An
    unrelated short note that happens to share two tokens with a long sentence
    still stays below the floor because the shorter side carries the shared
    tokens only in part.
    """
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def _embedding_cosine(a: str, b: str) -> float | None:
    """Cosine of ``a`` and ``b`` under ``MO_EMBED_PROVIDER``, or ``None``.

    Off unless a provider is named — no embedder lookup happens on the default
    path. Provider vectors are unit-normalized, so the cosine is their dot
    product. Any lookup/embed failure degrades to ``None``: a broken provider
    must never break measurement (``get_embedder`` raises for an unregistered
    name, and a real provider can fail at call time).
    """
    if not os.environ.get("MO_EMBED_PROVIDER", "").strip():
        return None
    try:
        # Lazy import: context_v2 must load on the prompt path even when
        # mini_ork.memory is unavailable (same rule as ``build``).
        from mini_ork.memory.semantic import get_embedder
        va, vb = get_embedder().embed([a, b])
        return float(sum(x * y for x, y in zip(va, vb)))
    except Exception:  # noqa: BLE001 — measurement must never raise
        return None


def recurrence(clusters: list[dict], findings: list[dict], *,
               sim: float = DEFAULT_SIM) -> dict[str, bool]:
    """``{cluster_id: True}`` when any of ``findings`` (one run's own review
    findings) restates the cluster's problem.

    A cluster recurs when any finding restates it by TF-IDF cosine (``>= sim``,
    unchanged), token-set containment (``>= CONTAINMENT_MIN``), or — when
    ``MO_EMBED_PROVIDER`` names a healthy embedder — embedding cosine
    (``>= EMBED_MIN``). IDF is computed over both sides; the extra arms never
    remove a hit the cosine arm found.
    """
    reps = [clean_issue(c.get("representative") or "") for c in clusters]
    texts = [clean_issue(f.get("issue") or "") for f in findings]
    idf, unseen = _idf_over(reps + texts)
    vecs = [v for v in (themes._vector(t, idf, unseen) for t in texts) if v]
    rep_sets = [_token_set(rep) for rep in reps]
    text_sets = [_token_set(text) for text in texts]
    out = {}
    for c, rep, rep_set in zip(clusters, reps, rep_sets):
        cv = themes._vector(rep, idf, unseen)
        hit = bool(cv) and any(themes._dot(cv, v) >= sim for v in vecs)
        if not hit:
            hit = any(_containment(rep_set, ts) >= CONTAINMENT_MIN
                      for ts in text_sets)
        if not hit:
            for text in texts:
                cos = _embedding_cosine(rep, text)
                if cos is not None and cos >= EMBED_MIN:
                    hit = True
                    break
        out[c["id"]] = hit
    return out


# ── the pack ─────────────────────────────────────────────────────────────────

def _constraint_items(contract: dict) -> list[dict]:
    items = []
    for text in contract.get("do_not", []):
        items.append({"kind": "do_not", "text": text})
    for text in contract.get("out_of_scope", []):
        items.append({"kind": "out_of_scope", "text": f"Out of scope: {text}"})
    if contract.get("files_in_scope"):
        listed = ", ".join(f"`{p}`" for p in contract["files_in_scope"][:12])
        items.append({"kind": "scope", "text": f"Touch only these files: {listed}"})
    for cmd in contract.get("verification", [])[:2]:
        # Keep the WHOLE command: a 240-char cut told nodes "success is proven
        # by" a broken half-command (3 of the last 30 kickoffs had longer ones).
        one = " ".join(cmd.split())
        if len(one) > _VERIFY_CMD_MAX:
            one = one[:_VERIFY_CMD_MAX] + " …(truncated)"
        items.append({"kind": "verify", "text": f"Success is proven by: `{one}`"})
    for i, item in enumerate(items):
        item["id"] = f"c:{i}"
    return items


def build(kickoff_path: str, *, task_class: str = "", db: str | None = None,
          run_id: str | None = None, max_clusters: int = 5,
          kickoff_text: str | None = None, run_dir: str | None = None,
          target_repo: str | None = None) -> dict:
    """The v2 pack for one run. Never raises; unreadable inputs give empty sections.

    Selection scope: the kickoff's parsed ``files_in_scope`` when non-empty,
    else (with a ``run_dir``) the run profile's ``scope_allow`` via
    ``preferences.scope_paths``, else empty. The fallback is selection-only —
    ``contract`` is never back-filled, so it grants no edit authority (the
    kickoff guard and the diff-scope checks keep reading the parse).

    Repo scope (LF-R12 §4): ``target_repo`` is the identity findings must belong
    to. When unset it is derived from ``MO_TARGET_CWD``, else the engine
    checkout — so an injection can never quietly become unscoped. A NULL
    ``target_repo`` column means ENGINE-LOCAL and is offered only when that
    derived identity is the engine itself."""
    kickoff_bytes: bytes | None = None
    if kickoff_text is None:
        try:
            with open(kickoff_path, "rb") as fh:
                kickoff_bytes = fh.read()
            kickoff_text = kickoff_bytes.decode("utf-8", errors="replace")
        except OSError:
            kickoff_text = ""
    else:
        kickoff_bytes = kickoff_text.encode("utf-8")
    contract = parse_contract(kickoff_text)
    scope = contract["files_in_scope"]
    if scope:
        scope_source = "kickoff"
    elif run_dir:
        scope_source = "none"
        try:
            # Lazy import: context_v2 must not acquire an import-time dependency
            # on mini_ork.memory (the prompt path also loads with it absent).
            from mini_ork.memory import preferences
            scope = preferences.scope_paths(run_dir) or []
        except Exception:  # noqa: BLE001 — prompt path must fail soft
            scope = []
        if scope:
            scope_source = "profile"
    else:
        scope_source = "none"
    target_repo = resolve_target_repo(target_repo)
    findings, finding_stats = scope_findings_with_stats(
        scope, db=db, exclude_run=run_id, target_repo=target_repo)
    floor = min_severity()
    clusters = [c for c in cluster(findings)
                if _SEVERITY_RANK.get(str(c["worst_severity"]).lower(), 0) >= floor]
    clusters = clusters[:max_clusters]
    prior = prior_attempts(kickoff_path, db=db, current_run=run_id,
                           target_repo=target_repo)
    themes = verified_themes(task_class, db=db)
    constraints = _constraint_items(contract)
    return {
        "version": 2,
        "mode": mode(),
        "run_id": run_id or "",
        "task_class": task_class,
        "kickoff_path": kickoff_path,
        # The contract as the run received it, before any node ran: the kickoff
        # guard (mini_ork.verify.kickoff_guard) compares the file against this at
        # publish time, so a run cannot widen its own scope by editing it.
        "kickoff_sha256": (hashlib.sha256(kickoff_bytes).hexdigest()
                           if kickoff_bytes is not None else ""),
        "kickoff_snapshot": kickoff_text if kickoff_bytes is not None else "",
        "contract": contract,
        "constraints": constraints,
        "file_findings": clusters,
        "prior_attempts": prior,
        "themes": themes,
        # R13 reads this marker to tell a run whose pack was built with the
        # once-only theme wiring from one that predates it (``v2_legacy``).
        "themes_once": True,
        # LF-R11a: the scan count stays the PRE-decay count, beside how many
        # rows the age window and the HEAD check dropped and the window used.
        "n_findings_scanned": finding_stats["n_findings_scanned"],
        "n_findings_stale": finding_stats["n_findings_stale"],
        "max_age_days": finding_stats["max_age_days"],
        # Selection provenance, beside the count it explains: which reader
        # supplied the paths and the resolved list. Never edit authority.
        "scope_source": scope_source,
        "scope_paths": list(scope),
        "item_ids": ([c["id"] for c in constraints] + [c["id"] for c in clusters]
                     + [p["id"] for p in prior] + [t["id"] for t in themes]),
        "built_at": int(time.time()),
    }


def item_ids(pack: dict) -> set[str]:
    return set(pack.get("item_ids") or [])


_ROLE_FOOTER = {
    "planner": ('If an item above shapes your plan, list its id in a top-level '
                '"context_used" array of the plan JSON, e.g. ["c:0", "f:1a2b3c4d5e"]. '
                "Optional. Never invent ids."),
    "implementer": "Before you finish, re-check your change against every [f:…] item above.",
    "reviewer": ("Check specifically whether this change repeats any [f:…] problem above, "
                 "and name the id when it does."),
}


def render(pack: dict, role: str = "planner", *, budget_chars: int = 6000) -> str:
    """Markdown block for one prompt. Constraints are always kept; prior
    attempts are dropped first, then the tail of the findings, to fit the budget."""
    constraints = pack.get("constraints") or []
    clusters = list(pack.get("file_findings") or [])
    prior = list(pack.get("prior_attempts") or [])
    themes = list(pack.get("themes") or [])
    if not (constraints or clusters or prior or themes):
        return ""

    def compose(clusters, prior, themes) -> str:
        lines = ["--- Context for this task (selected by the files in scope and the kickoff) ---"]
        if constraints:
            lines.append("Hard constraints from the kickoff:")
            lines += [f"- [{c['id']}] {c['text']}" for c in constraints]
        if themes:
            lines.append("Lessons induced from earlier runs (guardrail-checked; effect not yet measured):")
            lines += [f"- [{t['id']}] {t['text']}" for t in themes]
        if clusters:
            lines.append("Problems reviewers already found in these files. Do not repeat them:")
            for c in clusters:
                where = ", ".join(c["files"][:3])
                lines.append(f"- [{c['id']}] {c['n']}x in {c['n_runs']} run(s), worst "
                             f"{c['worst_severity'] or 'n/a'} ({where}): {c['representative'][:200]}")
        if prior:
            lines.append("Earlier attempts at this same kickoff:")
            for p in prior:
                lines.append(f"- [{p['id']}] ended {p['status']}")
                lines += [f"  - {f['severity'] or ''} {f['file'] or ''}:{f['line'] or ''} "
                          f"{f['issue'][:160]}".rstrip() for f in p["findings"]]
        footer = _ROLE_FOOTER.get(role)
        if footer:
            lines.append(footer)
        lines.append("--- /context ---")
        return "\n".join(lines) + "\n"

    text = compose(clusters, prior, themes)
    while len(text) > budget_chars and prior:
        prior.pop()
        text = compose(clusters, prior, themes)
    while len(text) > budget_chars and clusters:
        clusters.pop()
        text = compose(clusters, prior, themes)
    # Dropped last: a verified, authored lesson is the highest-signal line we
    # carry, so it outlives the machine-clustered findings under budget pressure.
    while len(text) > budget_chars and themes:
        themes.pop()
        text = compose(clusters, prior, themes)
    return text


# ── diff check (report-only; blocking belongs to the policies epic) ──────────

def _in_scope(path: str, scope: list[str]) -> bool:
    for s in scope:
        base = _strip_glob(s)
        if path == base or path.startswith(base + "/"):
            return True
    return False


def check_diff(diff_text: str, contract: dict) -> dict:
    """Files the diff touched outside the declared scope, and deleted test files."""
    changed, deleted, current = [], [], None
    for line in (diff_text or "").splitlines():
        m = _DIFF_HEADER_RE.match(line)
        if m:
            current = m.group(2)
            if current not in changed:
                changed.append(current)
            continue
        if line.startswith("deleted file mode") and current:
            deleted.append(current)
    scope = contract.get("files_in_scope") or []
    outside = [p for p in changed if scope and not _in_scope(p, scope)]
    deleted_tests = [p for p in deleted
                     if p.startswith("tests/") or os.path.basename(p).startswith("test_")]
    return {"changed": changed, "outside_scope": outside, "deleted_tests": deleted_tests}


# ── persistence ──────────────────────────────────────────────────────────────

def write_json(path: str, obj: dict) -> None:
    """Atomic JSON write (tmp + os.replace); never raises."""
    tmp = None
    try:
        directory = os.path.dirname(path) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".ctxv2.", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, indent=2, ensure_ascii=False, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
    except Exception:  # noqa: BLE001 — prompt path must not fail on an audit write
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def load_pack(run_dir: str) -> dict:
    try:
        with open(os.path.join(run_dir, PACK_FILENAME), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


# ── run-level wiring helpers (used by plan._inject_context / execute._learned_block) ──

def arm(run_id: str) -> str:
    """Which arm this run is in: ``off`` | ``shadow`` | ``v2`` | ``holdout``."""
    m = mode()
    if m != "on":
        return m
    return "holdout" if in_holdout(run_id) else "v2"


def _profile_kickoff(run_dir: str) -> str:
    try:
        with open(os.path.join(run_dir, "run_profile.json"), encoding="utf-8") as fh:
            data = json.load(fh)
        return str(data.get("kickoff_path") or "") if isinstance(data, dict) else ""
    except (OSError, ValueError):
        return ""


def pack_for_run(run_dir: str, *, kickoff_path: str | None = None, task_class: str = "",
                 run_id: str = "", db: str | None = None) -> dict:
    """The run's v2 pack: loaded when the planner already wrote it, else built
    (from ``run_profile.json``'s kickoff when none is given) and written once.
    Never raises; ``{}`` when there is no kickoff to read."""
    try:
        pack = load_pack(run_dir)
        if pack:
            return pack
        kickoff_path = kickoff_path or _profile_kickoff(run_dir)
        if not kickoff_path:
            return {}
        # LF-R12 §4: the run profile names the directory the work targets; its
        # repo identity is what this pack may select findings from. Without a
        # recorded target the derivation inside ``build`` applies (MO_TARGET_CWD
        # → engine), so an unprofiled run is never left unscoped.
        target_cwd = code_findings._run_target_cwd(run_dir)
        target_repo = code_findings.repo_identity(target_cwd) if target_cwd else None
        pack = build(kickoff_path, task_class=task_class, db=db, run_id=run_id,
                     run_dir=run_dir, target_repo=target_repo)
        pack["arm"] = arm(run_id)
        write_json(os.path.join(run_dir, PACK_FILENAME), pack)
        return pack
    except Exception:  # noqa: BLE001 — prompt path must fail soft
        return {}


def rendered_ids(text: str, pack: dict) -> list[str]:
    """Pack item ids that actually appear in ``text`` (after budget trimming)."""
    return [i for i in (pack.get("item_ids") or []) if f"[{i}]" in text]


def node_block(run_dir: str, node_type: str, run_id: str, *, task_class: str = "",
               sources: list[dict] | None = None) -> str:
    """The v2 block for one LLM node, or ``""`` unless this run's arm is ``v2``.
    Appends one ``kind: "context_v2"`` source per injected item id."""
    if not run_dir or arm(run_id) != "v2":
        return ""
    pack = pack_for_run(run_dir, task_class=task_class, run_id=run_id)
    text = render(pack, node_type) if pack else ""
    if text and sources is not None:
        sources.extend({"kind": "context_v2", "id": i} for i in rendered_ids(text, pack))
    return text


def write_injection_record(run_dir: str, node_id: str, *, text: str,
                           sources: list[dict], extra: dict | None = None) -> None:
    """``learned/<node_id>.md`` (the exact injected text) + ``.json`` — the same
    ledger shape the execute handlers write for LLM nodes, here for the planner,
    whose injection was previously unrecorded. Never raises."""
    try:
        learned = os.path.join(run_dir, "learned")
        record = {
            "node_id": node_id,
            "injected": bool(text and text.strip()),
            "reason": "" if text and text.strip() else "nothing matched",
            "sources": list(sources),
            "written_at": int(time.time()),
        }
        record.update(extra or {})
        # LF-R13 §2: arm/held-out provenance for the planner injection. The
        # planner's ledger_extra already carries ``context_v2.arm``; a run with
        # no such extra (or a non-string arm) records None rather than guessing.
        context_extra = (extra or {}).get("context_v2")
        arm = context_extra.get("arm") if isinstance(context_extra, dict) else None
        arm = arm if isinstance(arm, str) else None
        record["arm"] = arm
        record["held_out"] = (arm == "holdout") if arm is not None else None
        write_json(os.path.join(learned, f"{node_id}.json"), record)
        md_path = os.path.join(learned, f"{node_id}.md")
        if record["injected"]:
            with open(md_path, "w", encoding="utf-8") as fh:
                fh.write(text.strip() + "\n")
        elif os.path.exists(md_path):
            os.remove(md_path)
    except Exception:  # noqa: BLE001
        pass


def acknowledgements(plan: dict, pack: dict) -> dict:
    """The planner's optional ``context_used`` list, split into ids that exist
    in the pack and ids it made up."""
    used = plan.get("context_used") if isinstance(plan, dict) else None
    used = [str(u) for u in used] if isinstance(used, list) else []
    known = item_ids(pack)
    return {"used": used, "valid": [u for u in used if u in known],
            "invalid": [u for u in used if u not in known]}
