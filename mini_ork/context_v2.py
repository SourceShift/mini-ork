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
from mini_ork.learning import themes
from mini_ork.memory.preferences import paths_in_text

MODES = ("off", "shadow", "on")
DEFAULT_MODE = "shadow"
DEFAULT_HOLDOUT = 0.2
DEFAULT_SIM = 0.35
PACK_FILENAME = "context-pack.v2.json"

_SEVERITY_RANK = {"critical": 5, "blocker": 5, "high": 4, "medium": 3, "low": 2, "info": 1}
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
_DO_NOT_RE = re.compile(
    r"^\s*(?:[-*+]\s+)?(?:\*\*|__)?(do not|don't|never|must not)\b", re.I)
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


def injects(run_id: str) -> bool:
    """True when v2 context replaces the v1 blocks for this run."""
    return mode() == "on" and not in_holdout(run_id)


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

    ``files_in_scope`` — backticked paths in a "Files in scope" section;
    ``out_of_scope`` — bullets of an "Out of scope" section;
    ``do_not`` — every "Do NOT / Never / Must not" line outside code fences;
    ``verification`` — fenced commands (else backticked commands) in a
    "Verification" section.
    """
    sections = _sections(kickoff_text)
    files: list[str] = []
    out_of_scope: list[str] = []
    verification: list[str] = []
    for title, body in sections:
        if "files in scope" in title:
            for p in paths_in_text("\n".join(body)):
                # ``paths_in_text`` also accepts dotted symbols (``code_findings.harvest``);
                # a scope entry must look like a path: a directory part or a file extension.
                if p not in files and ("/" in p or _FILE_EXT_RE.search(p)):
                    files.append(p)
        elif "out of scope" in title:
            out_of_scope.extend(_bullets(body))
        elif "verification" in title or title.startswith("verify"):
            fenced = _fenced_blocks(body)
            if fenced:
                verification.extend(fenced)
            else:
                verification.extend(m.group(1).strip()
                                    for m in _BACKTICK_CMD_RE.finditer("\n".join(body)))
    do_not: list[str] = []
    in_fence = False
    for line in (kickoff_text or "").splitlines():
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence or not _DO_NOT_RE.match(line):
            continue
        clean = re.sub(r"^\s*[-*+]\s+", "", line).strip().strip("*_").strip()
        clean = clean[:200]
        if clean and clean not in do_not:
            do_not.append(clean)
    return {
        "files_in_scope": files,
        "out_of_scope": out_of_scope[:10],
        "do_not": do_not[:10],
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


def _query_findings(con: sqlite3.Connection, prefix: str, limit: int,
                    exclude_run: str | None) -> list[dict]:
    rows = con.execute(
        """
        SELECT id, fingerprint, run_id, file, line, severity, category, issue, ts
        FROM code_findings
        WHERE file LIKE ? ESCAPE '\\' AND (? = '' OR run_id != ?)
        ORDER BY ts DESC LIMIT ?
        """,
        (_like_escape(prefix) + "%", exclude_run or "", exclude_run or "", int(limit)),
    ).fetchall()
    return [dict(r) for r in rows]


def scope_findings(paths: list[str], *, db: str | None = None, limit: int = 300,
                   exclude_run: str | None = None) -> list[dict]:
    """Review/verifier findings for the in-scope paths, newest first.

    Each path matches as a prefix. A file with no findings of its own (often a
    new file) falls back to its parent directory, but only when that directory
    is at least three components deep — ``mini_ork/ide_pages/learn`` is a
    neighbourhood, ``mini_ork/cli`` is half the codebase.
    """
    con = _connect_ro(db)
    if con is None:
        return []
    out: dict[int, dict] = {}
    try:
        for raw in paths:
            prefix = _strip_glob(raw)
            if not prefix:
                continue
            rows = _query_findings(con, prefix, limit, exclude_run)
            if not rows and "." in os.path.basename(prefix):
                parent = os.path.dirname(prefix)
                if parent.count("/") >= 2:
                    rows = _query_findings(con, parent + "/", limit, exclude_run)
            for r in rows:
                out.setdefault(r["id"], r)
    except sqlite3.Error:
        return []
    finally:
        con.close()
    return sorted(out.values(), key=lambda r: str(r.get("ts") or ""), reverse=True)[:limit]


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
                   current_run: str | None = None, limit: int = 3) -> list[dict]:
    """Earlier runs of the same kickoff file, newest first, with their findings."""
    if not kickoff_path:
        return []
    con = _connect_ro(db)
    if con is None:
        return []
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
            try:
                found = con.execute(
                    "SELECT file, line, severity, issue FROM code_findings "
                    "WHERE run_id = ? ORDER BY ts DESC LIMIT 20", (r["id"],)).fetchall()
            except sqlite3.Error:
                found = []
            ranked = sorted(found, key=lambda f: -_SEVERITY_RANK.get(
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


def recurrence(clusters: list[dict], findings: list[dict], *,
               sim: float = DEFAULT_SIM) -> dict[str, bool]:
    """``{cluster_id: True}`` when any of ``findings`` (one run's own review
    findings) restates the cluster's problem. IDF is computed over both sides."""
    reps = [clean_issue(c.get("representative") or "") for c in clusters]
    texts = [clean_issue(f.get("issue") or "") for f in findings]
    idf, unseen = _idf_over(reps + texts)
    vecs = [v for v in (themes._vector(t, idf, unseen) for t in texts) if v]
    out = {}
    for c, rep in zip(clusters, reps):
        cv = themes._vector(rep, idf, unseen)
        out[c["id"]] = bool(cv) and any(themes._dot(cv, v) >= sim for v in vecs)
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
        one = " ".join(cmd.split())[:240]
        items.append({"kind": "verify", "text": f"Success is proven by: `{one}`"})
    for i, item in enumerate(items):
        item["id"] = f"c:{i}"
    return items


def build(kickoff_path: str, *, task_class: str = "", db: str | None = None,
          run_id: str | None = None, max_clusters: int = 5,
          kickoff_text: str | None = None) -> dict:
    """The v2 pack for one run. Never raises; unreadable inputs give empty sections."""
    if kickoff_text is None:
        try:
            with open(kickoff_path, encoding="utf-8") as fh:
                kickoff_text = fh.read()
        except OSError:
            kickoff_text = ""
    contract = parse_contract(kickoff_text)
    findings = scope_findings(contract["files_in_scope"], db=db, exclude_run=run_id)
    floor = min_severity()
    clusters = [c for c in cluster(findings)
                if _SEVERITY_RANK.get(str(c["worst_severity"]).lower(), 0) >= floor]
    clusters = clusters[:max_clusters]
    prior = prior_attempts(kickoff_path, db=db, current_run=run_id)
    constraints = _constraint_items(contract)
    return {
        "version": 2,
        "mode": mode(),
        "run_id": run_id or "",
        "task_class": task_class,
        "kickoff_path": kickoff_path,
        "contract": contract,
        "constraints": constraints,
        "file_findings": clusters,
        "prior_attempts": prior,
        "n_findings_scanned": len(findings),
        "item_ids": ([c["id"] for c in constraints] + [c["id"] for c in clusters]
                     + [p["id"] for p in prior]),
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
    if not (constraints or clusters or prior):
        return ""

    def compose(clusters, prior) -> str:
        lines = ["--- Context for this task (selected by the files in scope and the kickoff) ---"]
        if constraints:
            lines.append("Hard constraints from the kickoff:")
            lines += [f"- [{c['id']}] {c['text']}" for c in constraints]
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

    text = compose(clusters, prior)
    while len(text) > budget_chars and prior:
        prior.pop()
        text = compose(clusters, prior)
    while len(text) > budget_chars and clusters:
        clusters.pop()
        text = compose(clusters, prior)
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
        pack = build(kickoff_path, task_class=task_class, db=db, run_id=run_id)
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
