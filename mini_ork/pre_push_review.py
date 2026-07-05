"""Python port of lib/pre_push_review.sh — multi-lens review of a push diff.

Strangler-fig parity port. Two layers: deterministic heuristic checks (free,
always) and an opt-in LLM panel (MO_REVIEW_LLM_LENSES / mode=llm_panel|hybrid).
The six heuristic checks are transcribed verbatim from the bash's embedded python
(diff text → issue dicts). review_run computes the diff, inserts a
pre_push_reviews row, persists each finding to pre_push_review_issues, and
computes the same severity/consensus verdict via SQL.

The LLM panel (``_run_llm_panel``) is a seam — the default shells out to
mo_llm_dispatch exactly as the bash; overridable for tests.

    review_run(source_sha, target_branch, mode="heuristic", base=None, *, db, root) -> int
    review_verdict_for(rid, *, db) / review_show(rid, *, db) / review_forward_to_bug_reports(rid, *, db, root)
"""
from __future__ import annotations

import os
import re
import sqlite3
import subprocess
import sys
import tempfile


def _db_path() -> str:
    return os.environ.get("MINI_ORK_DB") or os.path.join(
        os.environ.get("MINI_ORK_HOME", ".mini-ork"), "state.db")


def _root() -> str:
    return os.environ.get("MINI_ORK_ROOT") or os.getcwd()


# ── heuristic checks (verbatim transcriptions) ──

def check_bash_syntax(diff: str) -> list[dict]:
    out = []
    files = set()
    for m in re.finditer(r"^\+\+\+ b/(.+)$", diff, re.M):
        f = m.group(1)
        if f.endswith(".sh") or f.startswith("bin/") and not f.endswith(".md"):
            files.add(f)
    for f in sorted(files):
        if not os.path.isfile(f):
            continue
        try:
            with open(f) as fh:
                first = fh.readline()
        except Exception:
            continue
        if "bash" not in first and not first.endswith("sh\n"):
            continue
        r = subprocess.run(["bash", "-n", f], capture_output=True, text=True)
        if r.returncode != 0:
            out.append({"lens": "heuristic.bash_syntax", "severity": "critical",
                        "file": f, "title": f"bash syntax error in {f}",
                        "description": (r.stderr or "")[:500],
                        "suggested_fix": "Run `bash -n " + f + "` locally and fix before push."})
    return out


def check_migration_safety(diff: str) -> list[dict]:
    out = []
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
                    out.append({"lens": "heuristic.migration_safety", "severity": "high", "file": f,
                                "title": "DROP without IF EXISTS in migration",
                                "description": f"Line: {line.strip()[:120]}",
                                "suggested_fix": "Add IF EXISTS so repeated runs are idempotent."})
            if up.startswith("DELETE FROM") and "WHERE" not in up:
                out.append({"lens": "heuristic.migration_safety", "severity": "critical", "file": f,
                            "title": "Unbounded DELETE in migration",
                            "description": f"Line: {line.strip()[:120]}",
                            "suggested_fix": "Add a WHERE clause or scope the delete."})
    return out


def check_added_todos(diff: str) -> list[dict]:
    out = []
    added = 0
    current_file = None
    for line in diff.split("\n"):
        m = re.match(r"^\+\+\+ b/(.+)$", line)
        if m:
            current_file = m.group(1); continue
        if not line.startswith("+") or line.startswith("+++"):
            continue
        if re.search(r"\b(TODO|FIXME|HACK|XXX|KLUDGE)\b", line):
            added += 1
            if added <= 5:
                out.append({"lens": "heuristic.todo_marker", "severity": "low",
                            "file": current_file or "?", "title": "New TODO/FIXME/HACK added",
                            "description": line.strip()[:200],
                            "suggested_fix": "Resolve in this PR or open an explicit issue."})
    return out


def check_diff_size(diff: str) -> list[dict]:
    added = sum(1 for line in diff.split("\n") if line.startswith("+") and not line.startswith("+++"))
    removed = sum(1 for line in diff.split("\n") if line.startswith("-") and not line.startswith("---"))
    if added >= 800:
        return [{"lens": "heuristic.diff_size", "severity": "medium", "file": "_diff",
                 "title": f"Large diff: +{added} / -{removed} lines",
                 "description": "Big diffs are harder to review; consider splitting.",
                 "suggested_fix": "Split into smaller logical commits where possible."}]
    return []


def check_test_pairing(diff: str) -> list[dict]:
    new_lib_bin = set()
    new_tests = set()
    for m in re.finditer(r"^\+\+\+ b/(.+)$", diff, re.M):
        f = m.group(1)
        if (f.startswith("lib/") or f.startswith("bin/")) and f.endswith(".sh"):
            start = m.end()
            if "new file mode" in diff[max(0, start - 500):start]:
                new_lib_bin.add(f)
        if f.startswith("tests/"):
            new_tests.add(f)
    if new_lib_bin and not new_tests:
        return [{"lens": "heuristic.test_pairing", "severity": "low",
                 "file": ",".join(sorted(new_lib_bin))[:200],
                 "title": f"{len(new_lib_bin)} new lib/bin file(s) without paired test changes",
                 "description": "New executable code added but no tests/ files changed.",
                 "suggested_fix": "Add at least one smoke test in tests/integration/ or tests/unit/."}]
    return []


def check_secret_patterns(diff: str) -> list[dict]:
    PATTERNS = [
        (r"AKIA[0-9A-Z]{16}", "AWS access key"),
        (r"-----BEGIN (RSA|EC|OPENSSH) PRIVATE KEY-----", "private key"),
        (r"ghp_[A-Za-z0-9]{30,}", "GitHub PAT"),
        (r"sk-[A-Za-z0-9]{20,}", "OpenAI-style API key"),
        (r"xoxb-\d+-\d+-", "Slack bot token"),
    ]
    current_file = None
    for line in diff.split("\n"):
        m = re.match(r"^\+\+\+ b/(.+)$", line)
        if m:
            current_file = m.group(1); continue
        if not line.startswith("+") or line.startswith("+++"):
            continue
        for pat, name in PATTERNS:
            if re.search(pat, line):
                return [{"lens": "heuristic.secret_leak", "severity": "critical",
                         "file": current_file or "?", "title": f"Possible {name} added to repo",
                         "description": "Matched pattern: " + pat,
                         "suggested_fix": "Remove the secret + rotate the credential immediately."}]
    return []


_HEURISTICS = [check_bash_syntax, check_migration_safety, check_added_todos,
               check_diff_size, check_test_pairing, check_secret_patterns]

_REVIEW_PROMPT = """You are a code reviewer for the mini-ork project. Review the unified diff
below and identify CONCRETE issues only. Do NOT critique style preferences
or speculate. Focus on:
  - bugs that will manifest at runtime
  - security concerns (secret leak, command injection, SQL injection)
  - test gaps for new behavior
  - migration safety (data loss, idempotence)
  - architectural concerns specific to the change

Return ONLY a JSON object with this exact shape (no prose, no markdown):

  {
    "issues": [
      {
        "severity": "low|medium|high|critical",
        "file":     "path/to/file",
        "line":     <integer or null>,
        "title":    "one-line summary <= 120 chars",
        "description": "what is wrong, max 400 chars",
        "suggested_fix": "what to do, max 300 chars"
      }
    ]
  }

If you find no issues, return {"issues": []}.

--- DIFF FOLLOWS ---
"""


def _default_llm_panel(diff_text: str) -> list[dict]:
    """Seam: run the MO_REVIEW_PANEL via mo_llm_dispatch (lib/llm-dispatch.sh)."""
    import json
    dispatch = os.path.join(_root(), "lib", "llm-dispatch.sh")
    if not os.path.isfile(dispatch):
        return []
    panel = os.environ.get("MO_REVIEW_PANEL", "codex kimi glm minimax").split()
    timeout = os.environ.get("MO_REVIEW_LENS_TIMEOUT_S", "180")
    out = []
    for model in panel:
        if model == "gemini" or model.startswith("gemini-") or "-gemini-" in model:
            continue
        with tempfile.NamedTemporaryFile("w", suffix=".out", delete=False) as of:
            out_file = of.name
        try:
            rc = subprocess.run(
                ["bash", "-c",
                 f'source "{dispatch}" 2>/dev/null && mo_llm_dispatch "$1" "$2" "$3" "$4" 4 >/dev/null 2>&1',
                 "_", model, _REVIEW_PROMPT + diff_text, out_file, timeout],
                capture_output=True).returncode
            if rc != 0:
                continue
            try:
                text = open(out_file).read()
            except OSError:
                continue
            d = None
            try:
                d = json.loads(text)
            except Exception:
                mm = re.search(r"\{[\s\S]*\}", text)
                if mm:
                    try:
                        d = json.loads(mm.group(0))
                    except Exception:
                        d = None
            if not isinstance(d, dict):
                continue
            issues = d.get("issues", [])
            if not isinstance(issues, list):
                continue
            emitted = 0
            for it in issues:
                if not isinstance(it, dict):
                    continue
                sev = (it.get("severity") or "medium").lower()
                if sev not in {"low", "medium", "high", "critical", "info"}:
                    sev = "medium"
                title = (it.get("title") or "").strip()[:300]
                if not title:
                    continue
                out.append({"lens": f"llm.{model}", "severity": sev,
                            "file": (it.get("file") or "?")[:200], "line": it.get("line"),
                            "title": title, "description": (it.get("description") or "")[:1000],
                            "suggested_fix": (it.get("suggested_fix") or "")[:500]})
                emitted += 1
                if emitted >= 8:
                    break
        finally:
            for p in (out_file, out_file + ".err.log"):
                try:
                    os.remove(p)
                except OSError:
                    pass
    return out


def _git(root, *args):
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)


def review_run(source_sha, target_branch, mode="heuristic", base=None, *,
               db=None, root=None, llm_panel=None) -> int:
    db = db or _db_path()
    root = root or _root()
    if base is None:
        base = (_git(root, "merge-base", source_sha, f"origin/{target_branch}").stdout.strip()
                or _git(root, "merge-base", source_sha, "main").stdout.strip())
        if not base:
            base = _git(root, "rev-parse", f"{source_sha}^").stdout.strip()
    if base:
        diff_text = _git(root, "diff", f"{base}..{source_sha}").stdout
    else:
        diff_text = _git(root, "show", source_sha).stdout

    shortstat = _git(root, "diff", "--shortstat", f"{base}..{source_sha}").stdout if base else ""
    fm = re.search(r"(\d+) file", shortstat)
    files_changed = int(fm.group(1)) if fm else 0
    lines_added = sum(1 for ln in diff_text.split("\n") if re.match(r"^\+[^+]", ln))
    lines_removed = sum(1 for ln in diff_text.split("\n") if re.match(r"^-[^-]", ln))

    con = sqlite3.connect(db); con.execute("PRAGMA busy_timeout=5000")
    cur = con.execute(
        "INSERT INTO pre_push_reviews (reviewed_at, source_sha, target_branch, reviewer_mode,"
        " files_changed, lines_added, lines_removed, verdict) "
        "VALUES (strftime('%s','now'), ?, ?, ?, ?, ?, ?, 'pending')",
        (source_sha, target_branch, mode, files_changed, lines_added, lines_removed))
    rid = int(cur.lastrowid or 0)
    con.commit()

    issues = []
    for chk in _HEURISTICS:
        try:
            issues.extend(chk(diff_text))
        except Exception:
            pass
    if mode in ("llm_panel", "hybrid"):
        try:
            issues.extend((llm_panel or _default_llm_panel)(diff_text))
        except Exception:
            pass

    for d in issues:
        con.execute(
            "INSERT INTO pre_push_review_issues (review_id, lens, severity, file_path, line_no,"
            " title, description, suggested_fix, status) VALUES (?,?,?,?,?,?,?,?,'open')",
            (rid, d.get("lens", "?"), d.get("severity", "medium"), d.get("file", "?"),
             d.get("line"), (d.get("title") or "")[:300], (d.get("description") or "")[:2000],
             (d.get("suggested_fix") or "")[:1000]))
    con.commit()

    _compute_verdict(con, rid, target_branch)
    con.commit(); con.close()
    return rid


def _count(con, rid, extra="", params=()):
    q = ("SELECT COUNT(*) FROM pre_push_review_issues WHERE review_id=? AND status='open'" + extra)
    return con.execute(q, (rid, *params)).fetchone()[0]


def _compute_verdict(con, rid, target):
    critical = _count(con, rid, " AND severity='critical'")
    high = _count(con, rid, " AND severity='high'")
    total = _count(con, rid)
    heuristic_high = _count(con, rid, " AND severity='high' AND lens LIKE 'heuristic.%'")
    consensus_high = con.execute(
        "SELECT COUNT(*) FROM (SELECT file_path FROM pre_push_review_issues "
        "WHERE review_id=? AND severity='high' AND status='open' AND lens LIKE 'llm.%' "
        "AND file_path IS NOT NULL GROUP BY file_path HAVING COUNT(DISTINCT lens) >= 2)",
        (rid,)).fetchone()[0]
    blocking_high = heuristic_high + consensus_high
    to_main = target in ("main", "master")
    if critical > 0:
        verdict = "block"
    elif blocking_high > 0:
        verdict = "block" if to_main else "warn"
    elif high > 0 or total > 5:
        verdict = "warn"
    else:
        verdict = "approve"
    con.execute(
        "UPDATE pre_push_reviews SET verdict=?, issues_open=?, issues_critical=?, rationale=? WHERE id=?",
        (verdict, total, critical,
         f"target={target} crit={critical} high={high} blocking={blocking_high} "
         f"(heuristic={heuristic_high}, consensus={consensus_high}) total={total}", rid))


def review_verdict_for(rid, *, db=None) -> str:
    con = sqlite3.connect(db or _db_path())
    r = con.execute("SELECT verdict FROM pre_push_reviews WHERE id=?", (rid,)).fetchone()
    con.close()
    return (r[0] if r else "") or ""


def review_show(rid, *, db=None) -> str:
    con = sqlite3.connect(db or _db_path())
    head = con.execute(
        "SELECT verdict, files_changed, lines_added, lines_removed, issues_open, issues_critical,"
        " rationale FROM pre_push_reviews WHERE id=?", (rid,)).fetchone()
    lines = [" | ".join(str(x if x is not None else "") for x in head)] if head else [""]
    lines.append("")
    order = "CASE severity WHEN 'critical' THEN 4 WHEN 'high' THEN 3 WHEN 'medium' THEN 2 WHEN 'low' THEN 1 ELSE 0 END DESC"
    for row in con.execute(
            f"SELECT severity, substr(lens,1,25), substr(COALESCE(file_path,'?'),1,30), substr(title,1,70)"
            f" FROM pre_push_review_issues WHERE review_id=? AND status='open' ORDER BY {order}", (rid,)):
        sev, lens, fp, title = row
        lines.append(f"{sev:<9} | {lens:<25} | {fp:<30} | {title}")
    con.close()
    return "\n".join(lines)


def review_forward_to_bug_reports(rid, *, db=None, root=None) -> int:
    db = db or _db_path()
    root = root or _root()
    from .ported import bug_report  # ported peer
    con = sqlite3.connect(db)
    rows = con.execute(
        "SELECT id, lens, severity, COALESCE(file_path,''), title, COALESCE(description,''),"
        " COALESCE(suggested_fix,'') FROM pre_push_review_issues WHERE review_id=? AND status='open'",
        (rid,)).fetchall()
    home = os.environ.get("MINI_ORK_HOME", ".mini-ork")
    run_dir = os.path.join(home, "runs", f"review-{rid}")
    os.makedirs(run_dir, exist_ok=True)
    n = 0
    for _iid, lens, sev, fp, title, desc, fix in rows:
        os.environ["MINI_ORK_RUN_DIR"] = run_dir
        try:
            bug_report.bug_report_emit(f"review.{lens}", sev, title, desc, fix, fp, 0.85)
        except Exception:
            pass
        n += 1
    con.commit(); con.close()
    try:
        bug_report.bug_report_sweep(all=True)
    except Exception:
        pass
    return n


if __name__ == "__main__":
    sys.stderr.write("pre_push_review.py — import and call review_run / review_verdict_for / review_show\n")
