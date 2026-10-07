"""Code findings — harvest what reviews and verifiers said about each file.

Engineer-first learning surface (plan
``docs/plans/2026-10-07-learning-memory-page-refactor.md``, step E1): the data
behind the "Your code" tab. Reviews and verifiers already say, per file, what
is wrong; this module pulls those findings out of the run directories and
indexes them in two tables:

  code_findings       -- one row per finding, deduped by
                         ``sha256(run_id|source|file|line|issue)``.
  code_findings_runs  -- one row per harvested run dir (the incremental guard).

Same DB resolution and ``ensure_schema`` discipline as
``mini_ork/learning/ledger.py``: cold-safe (a missing DB is a silent no-op,
never a conjured file), ``PRAGMA busy_timeout=5000``, and the write path never
raises — a re-dispatch loop is the most expensive thing a learner can trigger,
and a DB write that interrupts it is worse than no DB write at all.
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import time

_DEFAULT_HOME = ".mini-ork"
_DEFAULT_DEPTH = 3
_DEFAULT_SINCE_DAYS = 30
_DEFAULT_AREAS_LIMIT = 25
_DEFAULT_SHOW_LIMIT = 50
# task_runs statuses after which a run writes no more reviews or verdicts.
_TERMINAL_RUN_STATUSES = frozenset({
    "published", "failed", "completed", "success", "rolled_back", "error",
    "escalated", "cancelled", "killed", "abandoned", "done", "stuck",
})
_MAX_VERIFIER_REASON = 300

# A repo file path with an optional ``:line`` suffix. Extensions are the ones
# the kickoff names; ``[\w./-]`` covers path characters (word, dot, slash,
# dash) so ``tests/unit/test_acp_agent_py.py:6215`` and ``mini_ork/acp/agent.py``
# both match, while prose like "off by one" does not. Longer extensions precede
# their prefixes (``json`` before ``js``, ``tsx`` before ``ts``) so
# ``verdict.json`` never matches as ``verdict.js``.
_PATH_RE = re.compile(
    r"(?P<path>[\w./-]+\.(?:py|json|tsx|ts|js|rs|go|md|yaml|yml|sh|sql|toml))"
    r"(?::(?P<line>\d+))?"
)

# A prose bullet: `- `, `* `, or `1. ` at the start of a line.
_BULLET_RE = re.compile(r"^\s*(?:[-*]|\d+\.)\s+")

_PASS_VERDICTS = {"approve", "approved", "pass", "passed", "ok", "okay"}

# A reviewer note that *reports a check* rather than naming a problem: "PASS: …",
# "ruff clean", "66 passed", "already applied", "verifier artifact", … Reviews
# write these often enough that the file-name-matching rule below harvested them
# as findings (kickoff #5, live 2026-10-07). Module-level because
# ``prune_receipts`` reuses the exact same rule on rows already in the table.
RECEIPT_RE = re.compile(
    r"^\W*(?:pass|ok|verified|checked|fixed|accepted|not a defect|scope ok|resolved|confirmed)\b"
    r"|\b\d+ passed\b"
    r"|ruff (?:clean|check)"
    r"|false (?:negative|positive)"
    r"|verifier artifact"
    r"|reverse[- ]?apply"
    r"|already (?:applied|exists)",
    re.IGNORECASE,
)


def _is_receipt(text) -> bool:
    """True when a note/reason reads as a *report of a check* — "PASS:", "ruff
    clean", "66 passed", "already applied" — not a problem to fix."""
    return bool(RECEIPT_RE.search(str(text or "")))


# ── categorization ────────────────────────────────────────────────────────────
# Deterministic keyword map, first match wins. Labels are the kickoff's
# verbatim; keywords are matched case-insensitively as substrings/regexes.


_CATEGORY_RULES = [
    (
        "test doesn't check the claim",
        r"\b(?:tests?)\b.*\b(?:doesn'?t|does not|never)\b.*\b(?:test|check|assert)"
        r"|passes on base|\bvacuous\b|\btautolog",
    ),
    (
        # Either order: "docstring contradicts ..." and "stale docstring" both
        # name the same defect (the kickoff's own example is "stale docstring").
        "comment or docstring contradicts code",
        r"\b(?:docstring|comment)\b.*\b(?:contradict|false|wrong|stale)"
        r"|\b(?:contradict|false|wrong|stale)\b.*\b(?:docstring|comment)\b",
    ),
    (
        "missing guard or error handling",
        r"\bguard|cold-safe|OperationalError|\brais(?:e|es|ed|ing)\b|\bexception\b|None check|fail-soft",
    ),
    (
        "change outside the agreed scope",
        r"out of scope|outside scope|scope creep|\btouches\b.*\bnot in scope\b",
    ),
    (
        "wrong behaviour",
        r"\bwrong\b|\bincorrect\b|\bdrops\b|\bmisses\b|off by|\bregression\b|\bbroken\b",
    ),
    (
        "performance",
        r"\bslow\b|\bquadratic\b|O\(n|\btimeout\b|re-embed",
    ),
    (
        "security",
        r"\bsecret\b|\btoken\b|\binjection\b|\bunsafe\b|\bpermission\b",
    ),
    (
        "missing artifact or output",
        r"\babsent\b|\bmissing\b\s*(?:file|output|artifact)|\bnot written\b",
    ),
]

_COMPILED_RULES = [
    (label, re.compile(pattern, re.IGNORECASE)) for label, pattern in _CATEGORY_RULES
]


def categorize(issue: str) -> str:
    """Classify an issue string into one of the kickoff's category labels.

    First match wins; no match → ``"other"``. Deterministic and pure.
    """
    text = issue or ""
    for label, rx in _COMPILED_RULES:
        if rx.search(text):
            return label
    return "other"


# ── severity ──────────────────────────────────────────────────────────────────


def _is_pass(verdict) -> bool:
    if verdict is None:
        return False
    return str(verdict).strip().lower() in _PASS_VERDICTS


def normalize_severity(severity, verdict=None) -> str:
    """Normalize a severity token to ``high``/``medium``/``low`` (kickoff §4).

    blocker/blocking/critical/high → high; medium → medium; low/minor/nit →
    low; missing/unknown → medium when the verdict is not a pass, else low.
    """
    if severity is not None:
        s = str(severity).strip().lower()
        if s:
            if "blocker" in s or "blocking" in s or "critical" in s or s in ("high", "major"):
                return "high"
            if s == "medium":
                return "medium"
            if s == "low" or "minor" in s or "nit" in s:
                return "low"
    return "medium" if not _is_pass(verdict) else "low"


# ── parsing (pure) ────────────────────────────────────────────────────────────


def _fenced_bodies(text):
    """Bodies of ``` / ```json fences, in order, from a line-based scan.

    A single regex over fences cannot tell an opener from a closer. Given
    `````python … ``` … ```json … ````` the *closing*
    fence of the python block is the first substring that looks like a bare
    opener, so the regex matches from there and swallows the real ``json``
    fence — the structured payload is lost (reviewer findings[2]). Scanning
    line by line tracks the open/close state, skips any block whose info
    string is not empty/``json`` (``python``, ``diff``, …), and yields only the
    bodies of the candidate fences.
    """
    bodies: list[str] = []
    open_fence = False
    want_body = False
    current: list[str] = []
    for line in text.splitlines():
        if line.strip().startswith("```"):
            if open_fence:
                if want_body:
                    bodies.append("\n".join(current))
                open_fence = False
                want_body = False
                current = []
            else:
                info = line.strip()[3:].strip().lower()
                open_fence = True
                want_body = info in ("", "json")
                current = []
        elif open_fence:
            current.append(line)
    return bodies


def _scan_json_object(text):
    """The first JSON *object* embedded in ``text``, or None.

    framework-edit verifiers emit a Python ``DeprecationWarning`` line to
    stderr before the JSON payload; captured together the file is neither valid
    JSON nor fenced, so a plain parse yields nothing and the verifier's failure
    is dropped silently (reviewer findings[0]). ``raw_decode`` from each ``{``
    recovers the object without assuming the surrounding text is JSON.
    """
    decoder = json.JSONDecoder()
    idx = text.find("{")
    while idx != -1:
        try:
            obj, _ = decoder.raw_decode(text, idx)
        except (json.JSONDecodeError, ValueError):
            obj = None
        if isinstance(obj, dict):
            return obj
        idx = text.find("{", idx + 1)
    return None


def _find_json_object(text):
    """Return the first dict parsed from ``text``, or None.

    Tries the whole text first (plain JSON), then each ```json / ``` fence, then
    an object embedded in surrounding noise. A JSON array is not a dict → None
    (callers want a verdict-bearing object). Prose reviews fall through to None
    and are handled as bullet text.
    """
    if not text:
        return None
    s = text.strip()
    try:
        obj = json.loads(s)
        if isinstance(obj, dict):
            return obj
    except (json.JSONDecodeError, ValueError):
        pass
    for body in _fenced_bodies(s):
        try:
            obj = json.loads(body)
            if isinstance(obj, dict):
                return obj
        except (json.JSONDecodeError, ValueError):
            continue
    return _scan_json_object(s)


def _as_text(value):
    """Coerce any JSON value to a string (or ``None``).

    Reviews and verifiers are machine-written and every field can arrive as the
    wrong type — an ``issue`` that is a list, a ``verdict`` that is a dict. The
    write path must be fail-soft, so nothing downstream may call ``.strip()`` on
    an untrusted value; dicts/lists are rendered as JSON instead of raising.
    """
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (int, float)):
        return str(value)
    try:
        return json.dumps(value, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


def _scalar_text(value):
    """A JSON scalar rendered as text; containers → ``None``.

    File paths, verdicts and severities are scalars by contract — a list or
    dict in one of those fields is malformed data, not a value to store, so it
    degrades to ``None`` instead of a JSON blob in the column.
    """
    if isinstance(value, (list, dict)):
        return None
    return _as_text(value)


def _to_int(value):
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, (list, dict)):
        return None
    m = re.search(r"\d+", str(value))
    return int(m.group()) if m else None


_LEAD_SEVERITY_RE = re.compile(
    r"^\W*(blocker|blocking|critical|major|high|medium|minor|low|nit)\b", re.IGNORECASE)


def _first_path(text):
    m = _PATH_RE.search(text or "")
    return m.group("path") if m else None


def _make_finding(file, line, issue, severity, verdict, snippet):
    issue_text = (_as_text(issue) or "").strip()
    snippet_text = _as_text(snippet)
    if not issue_text:
        issue_text = (snippet_text or "").strip()
    if not issue_text:
        # Nothing to say (no issue text, no snippet): not a finding. These used
        # to be stored as "(no issue)" and were injected into prompts by
        # context_v2 as empty problems (live 2026-10-07: 18 rows, 2 runs).
        return None
    verdict_text = _scalar_text(verdict)
    sev = _scalar_text(severity)
    if not sev:
        # Prose findings carry their severity as a leading word, e.g.
        # "BLOCKER node.py:2440 …" or "nit: …" — 2026-10-07 live data.
        lead = _LEAD_SEVERITY_RE.match(issue_text)
        if lead:
            sev = lead.group(1)
    return {
        "file": _scalar_text(file),
        "line": _to_int(line),
        "severity": normalize_severity(sev, verdict_text),
        "category": categorize(issue_text),
        "issue": issue_text,
        "snippet": (snippet_text or None),
        "verdict": verdict_text,
    }


def _split_bullets(text):
    """Split prose into bullet items (``- ``, ``* ``, ``1. ``). Continuation
    lines join the preceding bullet; front matter before the first bullet is
    dropped. Returns [] when there are no bullet lines — the caller then treats
    the whole text as a single item.
    """
    items = []
    current = None
    for line in (text or "").splitlines():
        if _BULLET_RE.match(line):
            if current is not None:
                items.append(current)
            current = line.strip()
        elif current is not None:
            current = current + "\n" + line.strip()
    if current is not None:
        items.append(current)
    return items


def _prose_to_findings(text, verdict):
    """Turn a prose chunk (a notes/reasons item, or a whole prose review) into
    findings: one per bullet that names a file, or one for the whole text when
    there are no bullets. Items naming no file are kept with ``file=None`` only
    when the verdict is not a pass (they explain a rejection).

    Receipts are not findings (kickoff #5): under a PASS verdict *every* string
    note is a check report — a passing review's prose says what it verified, not
    what is wrong — so nothing is harvested; otherwise a note matching
    ``RECEIPT_RE`` ("PASS: …", "ruff clean", "66 passed", "already applied") is
    skipped before it can become a finding. Structured ``findings[]`` dicts do
    not pass through here and are always kept.
    """
    findings = []
    items = _split_bullets(text)
    if not items:
        items = [text.strip()] if (text or "").strip() else []
    passed = _is_pass(verdict)
    for item in items:
        item = (item or "").strip()
        if not item:
            continue
        if passed or _is_receipt(item):
            continue
        m = _PATH_RE.search(item)
        if m:
            findings.append(
                _make_finding(m.group("path"), m.group("line"), item, None, verdict, None)
            )
        else:
            findings.append(_make_finding(None, None, item, None, verdict, None))
    return findings


# The keys a machine-written finding / dict-note may carry its problem text
# under. ``issue`` is canonical, but a structured ``findings[]`` entry that
# carried only ``problem``/``description``/``message``/``text`` used to parse to
# an empty issue and land as the ``(no issue)`` placeholder (reviewer, round 2);
# the same fallback ``_dict_note_findings`` already used closes that hole.
_ISSUE_KEYS = ("issue", "problem", "description", "message", "text", "note", "detail")


def _issue_of(item):
    """The first non-empty problem text of a dict finding, or ``None``."""
    for key in _ISSUE_KEYS:
        value = item.get(key)
        if _as_text(value):
            return value
    return None


def _dict_note_findings(item, verdict):
    """One ``notes``/``reasons`` *dict* item → findings (kickoff §1).

    Dict items follow the same rule as string items: kept when they name a
    file, or when the verdict is not a pass (they explain a rejection). An item
    carrying neither a file nor any issue text is empty and always dropped, so
    an approved review never mints a ``(no issue)`` row.
    """
    file = _scalar_text(item.get("file"))
    issue = _issue_of(item)
    if file is None and not _as_text(issue) and item.get("snippet") is None:
        return []
    if file is None and _is_pass(verdict):
        return []
    item_verdict = item.get("verdict") if item.get("verdict") is not None else verdict
    return [
        _make_finding(
            file,
            item.get("line"),
            issue,
            item.get("severity"),
            item_verdict,
            item.get("snippet"),
        )
    ]


def _parse_review_raw(text) -> list:
    """Parse one review payload into findings (pure: no DB, no git, no I/O).

    Handles the four live shapes: plain JSON, fenced JSON with prose around it,
    pure prose/bullets, and JSON whose ``notes``/``reasons``/``findings`` carry
    file-bearing strings. Structured ``findings`` dicts are taken as-is; string
    items and bullet lines yield one finding each when they name a file.
    """
    if not text:
        return []
    obj = _find_json_object(text)
    findings: list[dict] = []
    verdict = obj.get("verdict") if isinstance(obj, dict) else None

    if isinstance(obj, dict):
        raw = obj.get("findings")
        if isinstance(raw, list):
            for item in raw:
                if isinstance(item, dict):
                    findings.append(
                        _make_finding(
                            item.get("file"),
                            item.get("line"),
                            _issue_of(item),
                            item.get("severity"),
                            item.get("verdict") if item.get("verdict") is not None else verdict,
                            item.get("snippet"),
                        )
                    )
                elif isinstance(item, str):
                    findings.extend(_prose_to_findings(item, verdict))
        elif isinstance(raw, str):
            findings.extend(_prose_to_findings(raw, verdict))

        for key in ("notes", "reasons"):
            value = obj.get(key)
            if isinstance(value, str):
                findings.extend(_prose_to_findings(value, verdict))
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, str):
                        findings.extend(_prose_to_findings(item, verdict))
                    elif isinstance(item, dict):
                        findings.extend(_dict_note_findings(item, verdict))
    else:
        findings.extend(_prose_to_findings(text, None))
    return findings


def _verifier_failed(obj) -> bool:
    if obj.get("pass") is False:
        return True
    v = obj.get("verdict")
    return isinstance(v, str) and v.strip().lower() in {"fail", "refuted", "error"}


def _check_label(item):
    """Render one ``checks`` entry as a short ``"name: expected"`` label."""
    if isinstance(item, dict):
        name = _as_text(item.get("name") or item.get("id") or item.get("check"))
        detail = _as_text(
            item.get("expected") or item.get("actual") or item.get("description")
        )
        if name and detail:
            return f"{name}: {detail}"
        return name or detail
    return _as_text(item)


def _first_failed_check(obj):
    """The first failed check named by a verifier payload, or None.

    framework-edit verifiers write ``failed_checks`` (list of check names) and
    ``checks`` (list of ``{name, expected, actual, pass}`` dicts — see
    ``recipes/framework-edit/verifiers/static-check.py``); older payloads use
    ``checks`` as a ``{name: bool}`` map. All three are handled so a real failure
    never degrades to a raw JSON dump (kickoff §2).
    """
    checks = obj.get("checks")
    by_name = {}
    if isinstance(checks, list):
        for c in checks:
            if isinstance(c, dict) and c.get("name"):
                by_name[str(c["name"])] = c

    failed = obj.get("failed_checks")
    if isinstance(failed, list):
        for item in failed:
            if item in (None, "", False):
                continue
            if isinstance(item, dict):
                return _check_label(item)
            name = _as_text(item)
            detail = by_name.get(name)
            return _check_label(detail) if detail else name
    elif isinstance(failed, str) and failed.strip():
        return failed.strip()

    if isinstance(checks, list):
        for c in checks:
            if isinstance(c, dict) and c.get("pass") is False:
                return _check_label(c)
    elif isinstance(checks, dict):
        for k, val in checks.items():
            if not val:
                return str(k)
    return None


def _verifier_reason(obj) -> str:
    first_failed = _first_failed_check(obj)
    if first_failed:
        return first_failed
    for key in ("reasons", "notes", "syntax_failures", "missing"):
        value = obj.get(key)
        if isinstance(value, list):
            parts = [p for p in ((_as_text(x) or "").strip() for x in value) if p]
            if parts:
                return "; ".join(parts)
        elif isinstance(value, str) and value.strip():
            return value
    for key in ("error", "message", "reason"):
        value = obj.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return json.dumps(obj, default=str)


def _parse_verifier_raw(name, payload) -> list:
    """Parse one verifier payload. A failed verifier (``pass`` false, or
    ``verdict`` in ``fail|FAIL|REFUTED|error``) yields one high-severity finding
    whose issue is the reason / first failed check (≤ 300 chars) and whose file
    is the first path in that text, if any. Passed verifiers yield [].
    """
    if isinstance(payload, str):
        obj = _find_json_object(payload)
        if obj is None:
            obj = {"raw": payload}
    elif isinstance(payload, dict):
        obj = payload
    else:
        return []
    if not _verifier_failed(obj):
        return []
    reason = _verifier_reason(obj)
    issue = (reason or f"verifier {name} failed")[:_MAX_VERIFIER_REASON]
    verdict = obj.get("verdict")
    return [
        _make_finding(
            _first_path(issue),
            None,
            issue,
            "high",
            _scalar_text(verdict) or "fail",
            None,
        )
    ]


# ── file normalization ────────────────────────────────────────────────────────


def _strip_abs_prefix(path: str, target_cwd=None) -> str:
    """Strip a leading absolute repo/worktree prefix (kickoff §5).

    The run's ``MO_TARGET_CWD`` is tried first (most specific — it is the
    directory the dispatched agent actually ran in), then anything up to
    ``/mini-ork-worktrees/<slug>/`` or ``/mini-ork/``.
    """
    if target_cwd:
        tc = str(target_cwd).rstrip("/")
        if tc:
            if path == tc:
                return path
            if path.startswith(tc + "/"):
                return path[len(tc) + 1:]
    m = re.search(r"/mini-ork-worktrees/[^/]+/(.*)$", path)
    if m:
        return m.group(1)
    m = re.search(r"/mini-ork/(.*)$", path)
    if m:
        return m.group(1)
    return path


def _run_target_cwd(run_dir):
    """The run's ``MO_TARGET_CWD`` read from ``<run_dir>/run_profile.json``
    (kickoff §5). The profile records it under ``roots.exec_cwd`` (fallback
    ``roots.target``); some writers use a top-level ``MO_TARGET_CWD`` or an
    ``env`` map. Returns a stripped string or None — never raises.
    """
    try:
        with open(os.path.join(run_dir, "run_profile.json"), "r",
                  encoding="utf-8", errors="replace") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    for container in (data.get("roots"), data.get("env")):
        if isinstance(container, dict):
            cwd = container.get("exec_cwd") or container.get("target") \
                or container.get("MO_TARGET_CWD")
            if isinstance(cwd, str) and cwd.strip("/"):
                return cwd.strip().rstrip("/")
    cwd = data.get("MO_TARGET_CWD")
    if isinstance(cwd, str) and cwd.strip("/"):
        return cwd.strip().rstrip("/")
    return None


_git_cache: dict[str, set | None] = {}


def _git_ls_files(repo) -> set | None:
    """``git -C <repo> ls-files`` as a set, cached per repo. None on any
    failure (callers keep the bare filename rather than guessing).
    """
    repo = repo or os.environ.get("MINI_ORK_ROOT") or os.getcwd()
    if repo in _git_cache:
        return _git_cache[repo]
    try:
        out = subprocess.run(
            ["git", "-C", repo, "ls-files"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        _git_cache[repo] = None  # cache the failure — do not re-run git per finding
        return None
    if out.returncode != 0:
        _git_cache[repo] = None
        return None
    paths = set(out.stdout.splitlines())
    _git_cache[repo] = paths
    return paths


def normalize_file(file, *, repo_root=None, known_paths=None, target_cwd=None):
    """Normalize a finding's file path (kickoff §5).

    Strips ``./`` and any leading absolute repo/worktree prefix (including the
    run's ``target_cwd``). A bare filename (``node.py``) is resolved to the
    unique matching repo path when exactly one matches; otherwise it stays bare.
    """
    if not file:
        return None
    f = (_scalar_text(file) or "").strip()
    if not f:
        return None
    f = re.sub(r"^\./", "", f)
    f = _strip_abs_prefix(f, target_cwd)
    if "/" not in f:
        paths = known_paths if known_paths is not None else _git_ls_files(repo_root)
        if paths is not None:
            matches = [p for p in paths if p == f or p.endswith("/" + f)]
            if len(matches) == 1:
                return matches[0]
    return f


# ── DB plumbing (mirrors ledger.py) ───────────────────────────────────────────


def _resolve_db(db=None) -> str | None:
    """Explicit arg → ``MINI_ORK_DB`` → ``$MINI_ORK_HOME/state.db`` →
    ``.mini-ork/state.db``. Always returns a path string (None only if db is
    None and no env is set); "skip" is decided by ``_open``.
    """
    if db:
        return db
    env_db = os.environ.get("MINI_ORK_DB")
    if env_db:
        return env_db
    home = os.environ.get("MINI_ORK_HOME") or _DEFAULT_HOME
    return os.path.join(home, "state.db")


def _open(db=None) -> sqlite3.Connection | None:
    """Open with ``PRAGMA busy_timeout=5000``. Returns None when the resolved
    path is missing on disk — never conjure a fresh DB (a corruption-class bug
    for readers that assume the migration ran).
    """
    path = _resolve_db(db)
    if not path or not os.path.exists(path):
        return None
    try:
        con = sqlite3.connect(path, timeout=5.0)
    except (sqlite3.Error, OSError):
        print(f"  [warn] code_findings: cannot open {path}", file=sys.stderr)
        return None
    try:
        con.execute("PRAGMA busy_timeout=5000")
    except sqlite3.Error:
        pass
    return con


# Byte-consistent with db/migrations/0064_code_findings.sql (see prior-art
# lens: ensure_schema DDL must not drift from the migration).
_DDL = """
CREATE TABLE IF NOT EXISTS code_findings (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint  TEXT    NOT NULL UNIQUE,
    run_id       TEXT    NOT NULL,
    source       TEXT    NOT NULL,
    file         TEXT,
    line         INTEGER,
    severity     TEXT    NOT NULL,
    category     TEXT    NOT NULL,
    issue        TEXT    NOT NULL,
    snippet      TEXT,
    verdict      TEXT,
    ts           INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_code_findings_file     ON code_findings(file);
CREATE INDEX IF NOT EXISTS idx_code_findings_run_id   ON code_findings(run_id);
CREATE INDEX IF NOT EXISTS idx_code_findings_category ON code_findings(category);
CREATE TABLE IF NOT EXISTS code_findings_runs (
    run_id       TEXT PRIMARY KEY,
    harvested_at INTEGER NOT NULL,
    n            INTEGER NOT NULL
);
"""


def ensure_schema(db=None) -> None:
    """Idempotent CREATE TABLE + CREATE INDEX. Never raises — a CREATE failure
    is a warning, not a stop-the-line event.
    """
    con = _open(db)
    if con is None:
        return
    try:
        con.executescript(_DDL)
        con.commit()
    except sqlite3.Error as e:
        print(f"  [warn] code_findings: ensure_schema failed: {e}", file=sys.stderr)
    finally:
        try:
            con.close()
        except sqlite3.Error:
            pass


# ── harvest ───────────────────────────────────────────────────────────────────


def _fingerprint(run_id, source, file, line, issue) -> str:
    key = "|".join([run_id or "", source or "", file or "", str(line or ""), issue or ""])
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _read(path) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def _mtime(path) -> int:
    try:
        return int(os.path.getmtime(path))
    except OSError:
        return int(time.time())


def _role_from(path) -> str:
    name = os.path.basename(path)
    if name.startswith("review-"):
        name = name[len("review-"):]
    for suffix in (".json.stdout.md", ".stdout.md", ".json"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            break
    return name


def _verifier_name(path) -> str:
    name = os.path.basename(path)
    for prefix in ("verifier-", "verifier_"):
        if name.startswith(prefix):
            name = name[len(prefix):]
            break
    if name.endswith(".json"):
        name = name[: -len(".json")]
    return name


def _unparseable(text, findings) -> bool:
    """True when a non-empty file yields no findings AND is neither JSON nor
    file-bearing prose — i.e. we looked and could not extract anything.
    """
    if not (text or "").strip():
        return False
    if findings:
        return False
    return _find_json_object(text) is None and _PATH_RE.search(text) is None


def _iter_run_files(run_dir):
    """Yield ``(source, findings, ts, unparseable)`` for every review/verifier
    file in one run dir. ``review-<role>.json.stdout.md`` is read only when the
    ``.json`` is missing or empty (kickoff shape #4).
    """
    review_jsons = sorted(glob.glob(os.path.join(run_dir, "review-*.json")))
    review_mds = sorted(glob.glob(os.path.join(run_dir, "review-*.json.stdout.md")))
    # Both naming conventions are live: the recipe DSL writes
    # ``verifier_<name>.json`` (scheduler.py) while older runs write
    # ``verifier-<name>.json`` (the kickoff's "126 verifier-*.json files").
    verifier_jsons = sorted(
        set(glob.glob(os.path.join(run_dir, "verifier-*.json")))
        | set(glob.glob(os.path.join(run_dir, "verifier_*.json")))
    )
    review_json_set = set(review_jsons)

    for path in review_jsons:
        text = _read(path)
        if not text.strip():
            alt = path + ".stdout.md"
            if os.path.exists(alt):
                text = _read(alt)
        findings = parse_review(text)
        yield "review:" + _role_from(path), findings, _mtime(path), _unparseable(text, findings)

    for path in review_mds:
        base = path[: -len(".stdout.md")]
        if base in review_json_set:
            continue  # already read the .json (empty-json fallback handled above)
        text = _read(path)
        findings = parse_review(text)
        yield "review:" + _role_from(base), findings, _mtime(path), _unparseable(text, findings)

    for path in verifier_jsons:
        text = _read(path)
        payload = _find_json_object(text)
        if payload is None:
            payload = text
        name = _verifier_name(path)
        if isinstance(payload, dict) and isinstance(payload.get("verifier"), str):
            name = payload["verifier"]
        findings = parse_verifier(name, payload)
        yield "verifier:" + name, findings, _mtime(path), _unparseable(text, findings)


def _harvest_run(con, run_id, run_dir, repo_root):
    n = 0
    with_file = 0
    skipped = 0
    paths = _git_ls_files(repo_root)
    target_cwd = _run_target_cwd(run_dir)
    for source, findings, ts, unparseable in _iter_run_files(run_dir):
        if unparseable:
            skipped += 1
        for finding in findings:
            file = normalize_file(finding["file"], repo_root=repo_root,
                                  known_paths=paths, target_cwd=target_cwd)
            fingerprint = _fingerprint(
                run_id, source, file, finding["line"], finding["issue"]
            )
            cur = con.execute(
                """
                INSERT OR IGNORE INTO code_findings
                    (fingerprint, run_id, source, file, line, severity, category,
                     issue, snippet, verdict, ts)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    fingerprint,
                    run_id,
                    source,
                    file,
                    finding["line"],
                    finding["severity"],
                    finding["category"],
                    finding["issue"],
                    finding["snippet"],
                    finding["verdict"],
                    ts,
                ),
            )
            inserted = int(cur.rowcount or 0)
            n += inserted
            if inserted and file:
                with_file += 1
    return n, with_file, skipped


def harvest(home, *, run_ids=None, db=None) -> dict:
    """Harvest review/verifier findings from ``home/runs`` (kickoff §6).

    Skips run dirs already recorded in ``code_findings_runs`` unless they are
    explicitly listed in ``run_ids`` (INSERT OR IGNORE keeps it idempotent per
    fingerprint). Returns ``{"runs", "findings", "with_file",
    "skipped_unparseable"}``.

    ``run_ids``, when given, restricts the harvest to exactly those run dirs
    (already-recorded runs are still re-read, since the caller asked for them).
    """

    stats = {"runs": 0, "findings": 0, "with_file": 0, "skipped_unparseable": 0}
    if not home:
        return stats
    runs_dir = os.path.join(home, "runs")
    if not os.path.isdir(runs_dir):
        return stats
    ensure_schema(db)
    con = _open(db)
    if con is None:
        return stats
    repo_root = os.environ.get("MINI_ORK_ROOT") or os.getcwd()
    wanted = set(run_ids) if run_ids else set()
    try:
        done = {row[0] for row in con.execute("SELECT run_id FROM code_findings_runs")}
        # A run that is still going has not written its reviews yet: harvesting
        # it now would record it as done and its findings would never be read.
        # Runs with no task_runs row (old or foreign run dirs) are harvested.
        # A non-terminal run older than 24 h is stuck, not in flight (the
        # Overview's rule): harvest it, or its reviews would never be read.
        status_of: dict[str, str] = {}
        stale_before = int(time.time()) - 24 * 3600
        try:
            for r in con.execute("SELECT id, status, created_at FROM task_runs"):
                try:
                    started = int(r[2] or 0)
                except (TypeError, ValueError):
                    started = 0
                status_of[str(r[0])] = (str(r[1] or "") if started >= stale_before
                                        else "stuck")
        except sqlite3.Error:
            status_of = {}
        for name in sorted(os.listdir(runs_dir)):
            if name in done and name not in wanted:
                continue
            if wanted and name not in wanted:
                continue
            if name in status_of and status_of[name] not in _TERMINAL_RUN_STATUSES:
                continue
            run_dir = os.path.join(runs_dir, name)
            if not os.path.isdir(run_dir):
                continue
            # Per-run guard: one malformed review file must never raise out of
            # the write path. If it did, this run would stay unmarked and every
            # later harvest would stop at it — permanently (kickoff: cold-safe,
            # never raises on the write path; reviewer findings[0]).
            try:
                n, with_file, skipped = _harvest_run(con, name, run_dir, repo_root)
                con.execute(
                    "INSERT OR REPLACE INTO code_findings_runs (run_id, harvested_at, n) "
                    "VALUES (?, ?, ?)",
                    (name, int(time.time()), n),
                )
                con.commit()
            except Exception as e:  # noqa: BLE001 — fail-soft by contract
                try:
                    con.rollback()
                except sqlite3.Error:
                    pass
                n = with_file = skipped = 0
                print(f"  [warn] code_findings: run {name} failed: {e}", file=sys.stderr)
                if isinstance(e, sqlite3.OperationalError) and (
                        "locked" in str(e).lower() or "busy" in str(e).lower()):
                    # Transient: leave the run unmarked so the next pass reads
                    # it. Marking it here would lose its findings for good.
                    continue
                # Still mark the run harvested (with n=0) so a run that always
                # fails does not block every run after it on the next pass.
                try:
                    con.execute(
                        "INSERT OR REPLACE INTO code_findings_runs (run_id, harvested_at, n) "
                        "VALUES (?, ?, ?)",
                        (name, int(time.time()), 0),
                    )
                    con.commit()
                except sqlite3.Error:
                    pass
            stats["runs"] += 1
            stats["findings"] += n
            stats["with_file"] += with_file
            stats["skipped_unparseable"] += skipped
    except sqlite3.Error as e:
        print(f"  [warn] code_findings: harvest failed: {e}", file=sys.stderr)
    finally:
        try:
            con.close()
        except sqlite3.Error:
            pass
    return stats


# ── prune ─────────────────────────────────────────────────────────────────────


def prune_receipts(db=None) -> int:
    """Delete ``code_findings`` rows whose ``issue`` reads as a receipt (§5).

    The receipt rule stops *new* receipts at the parser, but rows already in the
    table (harvested before the rule, INSERT OR IGNORE by fingerprint since)
    linger — pruning is the only way to drop them, which is why the live proof
    runs prune before rendering. Idempotent: a second call returns 0. Cold-safe
    like the rest of the write path — a missing DB is a silent no-op, no error.
    Returns the number of rows deleted.
    """
    con = _open(db)
    if con is None:
        return 0
    try:
        try:
            rows = con.execute("SELECT id, issue FROM code_findings").fetchall()
        except sqlite3.Error as e:
            print(f"  [warn] code_findings: prune failed: {e}", file=sys.stderr)
            return 0
        ids = [r[0] for r in rows if _is_receipt(r[1])]
        if ids:
            con.executemany("DELETE FROM code_findings WHERE id = ?", [(i,) for i in ids])
            con.commit()
        return len(ids)
    except sqlite3.Error as e:
        print(f"  [warn] code_findings: prune failed: {e}", file=sys.stderr)
        try:
            con.rollback()
        except sqlite3.Error:
            pass
        return 0
    finally:
        try:
            con.close()
        except sqlite3.Error:
            pass


# ── read-only queries ─────────────────────────────────────────────────────────


def _dir_prefix(file, depth) -> str:
    directory = file.rsplit("/", 1)[0] if "/" in file else ""
    if not directory:
        return ""
    return "/".join(directory.split("/")[:depth])


def _aggregate_areas(rows, depth, limit) -> list[dict]:
    groups: dict[str, dict] = {}
    for file, severity, category, run_id, ts in rows:
        prefix = _dir_prefix(file, depth)
        # A bare filename (unresolved, no directory) keys on the file itself so
        # unrelated bare filenames are not merged into one empty-prefix bucket —
        # otherwise `verdict.json` ×3, `pyproject.toml` ×1 and `CLAUDE.md` ×1
        # collapse into a single area labelled `verdict.json` (reviewer
        # findings[1]).
        key = prefix or file
        g = groups.setdefault(
            key,
            {
                "files": {},
                "runs": set(),
                "high": 0,
                "medium": 0,
                "cats": {},
                "last_ts": 0,
            },
        )
        g["files"][file] = g["files"].get(file, 0) + 1
        g["runs"].add(run_id)
        g[severity] = g.get(severity, 0) + 1
        g["cats"][category] = g["cats"].get(category, 0) + 1
        g["last_ts"] = max(g["last_ts"], ts)

    out = []
    for key, g in groups.items():
        total = sum(g["files"].values())
        top_file, top_n = max(g["files"].items(), key=lambda kv: (kv[1], kv[0]))
        # Bare filenames key on themselves (key == file), so a group is a single
        # file and the label is that file — never blank, never another file's
        # name. Directory groups keep the prefix unless one file dominates.
        if not key or key == top_file or (total and (top_n / total) >= 0.60):
            area = top_file
        else:
            area = key
        top_cats = sorted(g["cats"].items(), key=lambda kv: (-kv[1], kv[0]))[:3]
        top_files = sorted(g["files"].items(), key=lambda kv: (-kv[1], kv[0]))[:5]
        worst = "high" if g["high"] else ("medium" if g["medium"] else "low")
        out.append(
            {
                "area": area,
                # The group's identity, before the dominant-file relabel below:
                # a disjoint partition of the findings by ``_dir_prefix`` key.
                # The page carries it so a row and the detail it opens are the
                # same set of findings (reviewer, round 2) instead of re-deriving
                # a recursive file-or-subtree set from the display label.
                "key": key,
                "n_findings": total,
                "n_runs": len(g["runs"]),
                "worst_severity": worst,
                "top_categories": [[c, n] for c, n in top_cats],
                "last_ts": g["last_ts"],
                "files": [[f, n] for f, n in top_files],
                "_high": g["high"],
            }
        )
    out.sort(key=lambda r: (-r["_high"], -r["n_findings"]))
    for r in out:
        r.pop("_high", None)
    return out[:limit]


def _in_repo(files: set, repo_root: str | None) -> set:
    """The subset of ``files`` that are real paths of the code repo.

    Findings also name run artifacts (``verifier_test.json``,
    ``framework-edit.diff``, ``plan.json``) — live 2026-10-07 the top "area" was
    ``verifier_test.json`` with 49 findings. Those are not code an engineer
    owns. A path counts when ``git ls-files`` lists it or it exists under the
    repo root; with no repo to check against, nothing is filtered.
    """
    root = repo_root or os.environ.get("MINI_ORK_ROOT") or os.getcwd()
    listed = _git_ls_files(root)
    if listed is None and not os.path.isdir(root):
        return set(files)
    keep = set()
    for f in files:
        if (listed is not None and f in listed) or os.path.exists(os.path.join(root, f)):
            keep.add(f)
    return keep


def areas(*, db=None, depth=_DEFAULT_DEPTH, since_days=_DEFAULT_SINCE_DAYS,
          limit=_DEFAULT_AREAS_LIMIT, repo_only=True, repo_root=None) -> list[dict]:
    """Group findings by file area (kickoff §7). Read-only.

    Groups by the file's directory prefix up to ``depth`` segments, keeping the
    file itself when a single file dominates (≥ 60% of the area). Ordered by
    high-severity count, then n_findings. With ``repo_only`` (default) only
    findings on files of the code repo count; run artifacts are left out.
    """
    con = _open(db)
    if con is None:
        return []
    cutoff = int(time.time()) - int(since_days) * 86400
    try:
        rows = con.execute(
            "SELECT file, severity, category, run_id, ts FROM code_findings "
            "WHERE file IS NOT NULL AND ts >= ? ORDER BY ts DESC",
            (cutoff,),
        ).fetchall()
    except sqlite3.Error as e:
        print(f"  [warn] code_findings: areas failed: {e}", file=sys.stderr)
        return []
    finally:
        try:
            con.close()
        except sqlite3.Error:
            pass
    if repo_only and rows:
        keep = _in_repo({r[0] for r in rows}, repo_root)
        rows = [r for r in rows if r[0] in keep]
    return _aggregate_areas(rows, depth=depth, limit=limit)


_title_cache: dict[str, str] = {}


def _run_title(kickoff_path, run_id) -> str:
    if not kickoff_path:
        return run_id
    if kickoff_path in _title_cache:
        return _title_cache[kickoff_path]
    title = run_id
    try:
        with open(kickoff_path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if line.startswith("# "):
                    title = line[2:].strip()
                    break
    except OSError:
        pass
    _title_cache[kickoff_path] = title
    return title


def _like_escape(s) -> str:
    return (s or "").replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def findings_for(path_prefix, *, db=None, limit=_DEFAULT_SHOW_LIMIT) -> list[dict]:
    """Findings for a file path prefix, newest first (kickoff §8). Read-only.

    LEFT JOINs ``task_runs`` so findings from run dirs with no ``task_runs`` row
    are kept (their title falls back to the run id) rather than silently dropped.
    """
    con = _open(db)
    if con is None:
        return []
    like = _like_escape(path_prefix) + "%"
    try:
        rows = con.execute(
            """
            SELECT f.run_id, f.source, f.file, f.line, f.severity, f.category,
                   f.issue, f.snippet, f.verdict, f.ts, tr.kickoff_path, tr.status
            FROM code_findings f
            LEFT JOIN task_runs tr ON tr.id = f.run_id
            WHERE f.file LIKE ? ESCAPE '\\'
            ORDER BY f.ts DESC
            LIMIT ?
            """,
            (like, int(limit)),
        ).fetchall()
    except sqlite3.Error as e:
        print(f"  [warn] code_findings: findings_for failed: {e}", file=sys.stderr)
        return []
    finally:
        try:
            con.close()
        except sqlite3.Error:
            pass
    out = []
    for (run_id, source, file, line, severity, category, issue, snippet,
         verdict, ts, kickoff_path, status) in rows:
        out.append(
            {
                "run_id": run_id,
                "run_title": _run_title(kickoff_path, run_id),
                "run_status": status,
                "source": source,
                "file": file,
                "line": line,
                "severity": severity,
                "category": category,
                "issue": issue,
                "snippet": snippet,
                "verdict": verdict,
                "ts": ts,
            }
        )
    return out


# ── CLI ───────────────────────────────────────────────────────────────────────


def _print_human(cmd, result) -> None:
    if cmd == "harvest":
        print(
            f"runs={result['runs']} findings={result['findings']} "
            f"with_file={result['with_file']} "
            f"skipped_unparseable={result['skipped_unparseable']}"
        )
    elif cmd == "areas":
        for r in result:
            cats = ", ".join(f"{c}x{n}" for c, n in r["top_categories"])
            print(f"{r['area']}  n={r['n_findings']} runs={r['n_runs']} "
                  f"worst={r['worst_severity']} [{cats}]")
    elif cmd == "prune":
        print(f"pruned {result} receipt row(s)")
    else:  # show
        for r in result:
            loc = r["file"] or ""
            if r.get("line"):
                loc = f"{loc}:{r['line']}"
            print(f"{r['ts']} [{r['severity']}/{r['category']}] {loc} "
                  f"({r['run_title']}) {r['issue']}")


def main(argv=None) -> int:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", default=None,
                        help="SQLite DB path (default: $MINI_ORK_DB or $MINI_ORK_HOME/state.db)")
    common.add_argument("--home", default=None,
                        help="mini-ork home dir containing runs/ (default: $MINI_ORK_HOME or .mini-ork)")
    common.add_argument("--json", action="store_true", help="emit JSON")

    ap = argparse.ArgumentParser(
        prog="python -m mini_ork.learning.code_findings",
        description="Harvest and query code findings from run directories.",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("harvest", parents=[common],
                   help="harvest review/verifier findings from home/runs")
    p_areas = sub.add_parser("areas", parents=[common],
                             help="group findings by file area")
    p_areas.add_argument("--days", type=int, default=_DEFAULT_SINCE_DAYS,
                         help="only findings newer than N days")
    p_show = sub.add_parser("show", parents=[common],
                            help="list findings for a path prefix")
    p_show.add_argument("path", help="file path prefix")
    sub.add_parser("prune", parents=[common],
                   help="delete harvested receipt notes (check reports, not problems)")

    args = ap.parse_args(argv)
    home = args.home or os.environ.get("MINI_ORK_HOME") or _DEFAULT_HOME

    if args.cmd == "harvest":
        result = harvest(home, db=args.db)
    elif args.cmd == "areas":
        result = areas(db=args.db, since_days=args.days)
    elif args.cmd == "prune":
        result = prune_receipts(db=args.db)
    else:  # show
        result = findings_for(args.path, db=args.db)

    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        _print_human(args.cmd, result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


def parse_review(text) -> list[dict]:
    """Parse one review payload into findings (pure: no DB, no git, no I/O).

    Handles the four live shapes: plain JSON, fenced JSON with prose around it,
    pure prose/bullets, and JSON whose ``notes``/``reasons``/``findings`` carry
    file-bearing strings. Structured ``findings`` dicts are taken as-is; string
    items and bullet lines yield one finding each when they name a file. Items
    with neither issue text nor a snippet are dropped.
    """
    return [f for f in _parse_review_raw(text) if f]


def parse_verifier(name, payload) -> list[dict]:
    """Parse one verifier payload. A failed verifier (``pass`` false, or
    ``verdict`` in ``fail|FAIL|REFUTED|error``) yields one high-severity finding
    whose issue is the reason / first failed check (≤ 300 chars) and whose file
    is the first path in that text, if any. Passed verifiers yield [].
    """
    return [f for f in _parse_verifier_raw(name, payload) if f]
