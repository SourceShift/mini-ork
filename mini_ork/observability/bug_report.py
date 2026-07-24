"""Bug-report channel + orchestrator queue — Python port of ``lib/bug_report.sh``.

Faithful port of the six bash public functions that implement the per-agent
noticed-but-deferred bug reporting channel and the orchestrator queue that
dedupes, ranks, and promotes top-N into new epics. The bash
``lib/bug_report.sh`` stays in place (strangler-fig co-existence); this
module gives Python callers an in-process surface and gives the parity test
a stable target to byte-diff against the live bash subprocess.

Pipeline map (bash function → Python):
  bug_report_emit       → bug_report_emit       (validate → append JSONL row)
  bug_report_sweep      → bug_report_sweep      (walk .mini-ork/runs → upsert bug_reports)
  bug_report_prioritize → bug_report_prioritize (ranked open rows, bash printf format)
  bug_report_promote    → bug_report_promote    (top-N → epic rows + kickoff files)
  bug_report_list       → bug_report_list       (recent rows, bash printf format)
  bug_report_show       → bug_report_show       (sqlite3 -line format)

Internal helpers (mirrors of bash underscore-prefixed functions):
  _bug_report_now         → _now                  (int epoch seconds)
  _bug_report_severity_weight → _severity_weight (critical=8 high=4 medium=2 low=1)
  _bug_report_fingerprint → _fingerprint          (sha256 of normalized title)

Env knobs honored (also accepted as explicit kwargs):
  * ``MINI_ORK_DB``         — state.db path (default ``${MINI_ORK_HOME:-.mini-ork}/state.db``).
  * ``MINI_ORK_HOME``       — runs root (``.mini-ork`` by default) for sweep.
  * ``MINI_ORK_RUN_DIR``    — per-run sink dir (``/tmp`` by default) for emit.
  * ``MINI_ORK_ROOT``       — repo root for promote kickoff files
    (default: parent of ``mini_ork/`` package, mirrors bash's
    ``cd "$(dirname "${BASH_SOURCE[0]}")/.."``).

Output format mirrors:
  * ``bug_report_prioritize`` / ``bug_report_list`` emit rows joined by
    ``" | "`` with a trailing ``"\\n"`` (mirrors ``sqlite3 -separator ' | '``).
  * ``bug_report_show`` emits key/value lines (``key = value\\n``) terminated
    by a blank ``\\n`` (mirrors ``sqlite3 -line``).

Parity is enforced by ``tests/unit/test_bug_report_py.py`` (>=6 cases that
drive the LIVE bash subprocess against a temp DB seeded by ``db/init.sh``
and diff the resulting ``bug_reports`` rows + JSONL sinks + stdout strings
against the Python port byte-for-byte; floats 1e-6 on confidence;
epochs 1-second tolerance).

Schema citations:
  - ``bug_reports``         — db/migrations/0029_bug_reports.sql
  - ``epics``               — db/migrations/0001_core.sql
                              (INSERT target for promote)
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
from pathlib import Path

__all__ = [
    "bug_report_emit",
    "bug_report_sweep",
    "bug_report_prioritize",
    "bug_report_promote",
    "bug_report_list",
    "bug_report_show",
    "_severity_weight",
    "_fingerprint",
    "_now",
]

# Mirrors lib/bug_report.sh:42 (`_bug_report_now`).
def _now() -> int:
    """Mirror bash `date +%s` — integer epoch seconds."""
    return int(time.time())


# Mirrors lib/bug_report.sh:45-53 (`_bug_report_severity_weight`).
_SEVERITY_WEIGHT = {"critical": 8, "high": 4, "medium": 2, "low": 1}
_VALID_SEVERITIES = frozenset(_SEVERITY_WEIGHT.keys())


def _severity_weight(severity: str) -> int:
    """Mirror bash `_bug_report_severity_weight` lines 45-53.

    critical=8, high=4, medium=2, low=1, anything else=1.
    """
    return _SEVERITY_WEIGHT.get(severity, 1)


# Mirrors lib/bug_report.sh:96-103 (`_bug_report_fingerprint`).
_FINGERPRINT_STRIP_RE = re.compile(r"[`\"'()\[\]{},.;:!?]")


def _fingerprint(title: str) -> str:
    """Mirror bash `_bug_report_fingerprint` lines 96-103.

    Normalize (collapse whitespace, lowercase, strip a fixed set of
    punctuation marks), then sha256-hex.
    """
    s = re.sub(r"\s+", " ", (title or "").lower().strip())
    s = _FINGERPRINT_STRIP_RE.sub("", s)
    return hashlib.sha256(s.encode()).hexdigest()


# Mirrors lib/bug_report.sh:40 (`STATE_DB=...`) — env precedence.
def _resolve_db() -> str:
    """Return the state.db path the bash script would pick.

    Resolution order (mirrors bash line 40):
      $MINI_ORK_DB → ${MINI_ORK_HOME:-.mini-ork}/state.db
    """
    env_db = os.environ.get("MINI_ORK_DB")
    if env_db:
        return env_db
    home = os.environ.get("MINI_ORK_HOME") or ".mini-ork"
    return os.path.join(home, "state.db")


def _resolve_run_dir() -> str:
    """Return the per-run sink dir, mirrors bash `MINI_ORK_RUN_DIR:-/tmp`."""
    return os.environ.get("MINI_ORK_RUN_DIR") or "/tmp"


def _resolve_home() -> str:
    """Return the .mini-ork home, mirrors bash `MINI_ORK_HOME:-.mini-ork`."""
    return os.environ.get("MINI_ORK_HOME") or ".mini-ork"


def _resolve_repo_root(repo_root: str | os.PathLike[str] | None = None) -> Path:
    """Return the repo root used by ``bug_report_promote``.

    Resolution order:
      1. Explicit ``repo_root`` kwarg.
      2. ``$MINI_ORK_ROOT`` env var.
      3. Parent of the ``mini_ork`` package (i.e. the repo containing this
         port), which mirrors bash's
         ``cd "$(dirname "${BASH_SOURCE[0]}")/.."`` relative to lib/.
    """
    if repo_root is not None:
        return Path(repo_root)
    env_root = os.environ.get("MINI_ORK_ROOT")
    if env_root:
        return Path(env_root)
    # mini_ork/observability/bug_report.py → mini_ork/observability → mini_ork → REPO
    return Path(__file__).resolve().parent.parent.parent


# Mirrors lib/bug_report.sh:59-93 (`bug_report_emit`).
def bug_report_emit(
    agent_role: str,
    severity: str = "medium",
    title: str = "",
    description: str = "",
    suggested_fix: str = "",
    observed_in: str = "general",
    confidence: float | str = 0.5,
    *,
    run_dir: str | os.PathLike[str] | None = None,
) -> None:
    """Mirror bash `bug_report_emit <role> <sev> <title> <desc> <fix> <obs> <conf>`.

    Appends a single JSONL row to ``${MINI_ORK_RUN_DIR}/noticed_bugs.jsonl``.
    Field truncations (title→300, description→4000, fix→2000, observed_in→200)
    mirror the bash heredoc exactly. Severity is normalized to one of
    ``{low, medium, high, critical}`` — anything else falls back to
    ``"medium"`` (mirrors bash ``case``). Confidence is parsed as float;
    anything that does not parse falls back to ``0.5`` (mirrors bash heredoc).

    Error contract mirrors bash:
      * Missing ``agent_role`` or empty ``title`` → raise ``ValueError``
        (bash raises via ``${1:?agent_role required}`` / ``${3:?title required}``).
      * Any other failure (e.g. JSON serialization, file write) is
        swallowed and returns silently — mirrors bash's ``|| return 0``
        wrapping the heredoc. This is fire-and-forget by design.

    Args:
        run_dir: Override ``MINI_ORK_RUN_DIR``. Defaults to the env var,
                 falling back to ``/tmp`` (mirrors bash).

    Raises:
        ValueError: when ``agent_role`` is empty or ``title`` is empty.
    """
    if not agent_role:
        raise ValueError("agent_role required")
    if not title:
        raise ValueError("title required")

    if severity not in _VALID_SEVERITIES:
        severity = "medium"

    if run_dir is None:
        run_dir = _resolve_run_dir()
    sink = Path(run_dir) / "noticed_bugs.jsonl"

    row: dict[str, object] = {
        "agent_role": agent_role,
        "severity": severity,
        "title": title.strip()[:300],
        "description": description[:4000],
        "suggested_fix": suggested_fix[:2000],
        "observed_in": observed_in[:200],
    }
    try:
        try:
            row["confidence"] = float(confidence)
        except (TypeError, ValueError):
            row["confidence"] = 0.5
        sink.parent.mkdir(parents=True, exist_ok=True)
        with open(sink, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, separators=(",", ":")) + "\n")
    except Exception:
        # Mirrors bash `|| return 0` — emit is fire-and-forget.
        return


# Mirrors lib/bug_report.sh:107-211 (`bug_report_sweep`).
def bug_report_sweep(
    *args: str,
    since: int = 0,
    all: bool = False,  # noqa: A002 — mirrors bash positional flag name
    home: str | os.PathLike[str] | None = None,
) -> int:
    """Mirror bash `bug_report_sweep [--since EPOCH] [--all]`.

    Walks every ``${MINI_ORK_HOME}/runs/<run_id>/noticed_bugs.jsonl``,
    upserting each row into the ``bug_reports`` table by sha256 fingerprint.
    Returns the number of NEW rows inserted (repeat sightings increment
    ``frequency`` and keep max severity/conf instead of inserting).

    Positional ``args`` are parsed for ``--since <epoch>`` and ``--all`` to
    mirror the bash flag-parsing loop; explicit ``since=`` / ``all=`` kwargs
    take precedence. The ``home`` kwarg overrides ``MINI_ORK_HOME``.

    Floats / ints in the upserted ``confidence`` and ``severity`` columns
    match the bash heredoc exactly (severity kept at max rank, confidence
    kept at max). Timestamps are set to ``int(time.time())`` which mirrors
    bash's ``now = int(time.time())`` capture at sweep start.
    """
    parsed_since = since
    parsed_all = all
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--since" and i + 1 < len(args):
            try:
                parsed_since = int(args[i + 1])
            except ValueError:
                parsed_since = 0
            i += 2
            continue
        if a == "--all":
            parsed_all = True
            i += 1
            continue
        i += 1

    if home is None:
        home = _resolve_home()

    runs_dir = Path(home) / "runs"
    if not runs_dir.is_dir():
        return 0

    db = _resolve_db()
    now = _now()

    swept = 0
    con = sqlite3.connect(db)
    try:
        con.execute("PRAGMA busy_timeout=5000")
        sev_rank = {"low": 1, "medium": 2, "high": 3, "critical": 4}

        for run_path in sorted(runs_dir.iterdir()):
            if not run_path.is_dir():
                continue
            sink = run_path / "noticed_bugs.jsonl"
            if not sink.is_file():
                continue
            if not parsed_all and sink.stat().st_mtime < parsed_since:
                continue
            run_id = run_path.name
            try:
                raw_text = sink.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for raw in raw_text.splitlines():
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    row = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                title = (row.get("title") or "").strip()
                if not title:
                    continue
                role = row.get("agent_role") or "unknown"
                sev = row.get("severity") or "medium"
                if sev not in _VALID_SEVERITIES:
                    sev = "medium"
                try:
                    conf = float(row.get("confidence", 0.5))
                except (TypeError, ValueError):
                    conf = 0.5
                fp = _fingerprint(title)
                existing = con.execute(
                    "SELECT id, frequency, severity, confidence FROM bug_reports WHERE fingerprint=?",
                    (fp,),
                ).fetchone()
                if existing:
                    bid, _freq, old_sev, old_conf = existing
                    kept_sev = old_sev if sev_rank[old_sev] >= sev_rank[sev] else sev
                    kept_conf = max(old_conf, conf)
                    con.execute(
                        """UPDATE bug_reports
                              SET frequency=frequency+1,
                                  severity=?, confidence=?,
                                  last_seen_at=?, updated_at=?
                            WHERE id=?""",
                        (kept_sev, kept_conf, now, now, bid),
                    )
                else:
                    con.execute(
                        """INSERT INTO bug_reports
                               (fingerprint, run_id, agent_role, task_class,
                                observed_in, title, description, suggested_fix,
                                severity, confidence, frequency, status,
                                first_seen_at, last_seen_at, updated_at)
                          VALUES (?,?,?,?,?,?,?,?,?,?,1,'open',?,?,?)""",
                        (fp, run_id, role, row.get("task_class"),
                         row.get("observed_in") or "general",
                         title[:300],
                         (row.get("description") or "")[:4000],
                         (row.get("suggested_fix") or "")[:2000],
                         sev, conf,
                         now, now, now),
                    )
                    swept += 1
        con.commit()
    finally:
        con.close()
    return swept


# Mirrors lib/bug_report.sh:213-236 (`bug_report_prioritize`).
_PRIORITIZE_SQL = (
    "SELECT id, severity, frequency, confidence, agent_role, title "
    "FROM bug_reports WHERE status='open' "
    "ORDER BY CASE severity WHEN 'critical' THEN 8 "
    "                      WHEN 'high'     THEN 4 "
    "                      WHEN 'medium'   THEN 2 "
    "                      ELSE 1 END * frequency * confidence DESC"
)


def _format_pipe_row(*cells: object, spec: str = "") -> str:
    """Format one row of a pipe-separated report.

    Mirrors the ``printf`` column shapes used by ``bug_report_prioritize``
    and ``bug_report_list`` exactly. ``spec`` is a string of one char per
    column:
      * ``"d"`` — left-aligned integer, width 4 (bash ``%-4d``).
      * ``"i"`` — right-aligned integer, width 3 (bash ``%3d``).
      * ``"f"`` — float with 2 decimals, default width (bash ``%.2f``).
      * ``"s"`` — left-aligned string, width 15 (bash ``%-15s``).
      * ``"S"`` — left-aligned string, width 8 (bash ``%-8s``).
      * ``"t"`` — passthrough string (no padding) — mirrors bash
        ``substr(title,1,80)`` which is NOT wrapped in ``printf``.
    """
    out: list[str] = []
    for c, kind in zip(cells, spec):
        if kind == "d":
            v: object = int(c)             # type: ignore[arg-type]
            out.append(f"{v:<4d}")
        elif kind == "i":
            v = int(c)                     # type: ignore[arg-type]
            out.append(f"{v:>3d}")
        elif kind == "f":
            v = float(c)                   # type: ignore[arg-type]
            out.append(f"{v:.2f}")
        elif kind == "s":
            out.append(f"{str(c):<15s}")
        elif kind == "S":
            out.append(f"{str(c):<8s}")
        elif kind == "t":
            out.append(str(c))
        else:
            out.append(str(c))
    return " | ".join(out)


# Mirrors lib/bug_report.sh:222-235 (printf column shapes).
def _prioritize_formatter(row: tuple) -> str:
    return _format_pipe_row(
        int(row[0]),        # id %-4d
        row[1],             # severity %-8s
        int(row[2]),        # frequency %3d
        float(row[3]),      # confidence %.2f
        row[4],             # agent_role %-15s
        (row[5] or "")[:80],
        spec="dSifst",
    )


def bug_report_prioritize(top: int = 10) -> str:
    """Mirror bash `bug_report_prioritize [--top N]`.

    Returns ranked open ``bug_reports`` rows joined by ``"\\n"`` with a
    trailing ``"\\n"`` — exactly the form ``sqlite3 -separator ' | '``
    emits to stdout. Default ``top=10``.
    """
    db = _resolve_db()
    con = sqlite3.connect(db)
    try:
        con.execute("PRAGMA busy_timeout=5000")
        rows = con.execute(f"{_PRIORITIZE_SQL} LIMIT ?", (int(top),)).fetchall()
    finally:
        con.close()
    return "".join(_prioritize_formatter(r) + "\n" for r in rows)


# Mirrors lib/bug_report.sh:238-247 (`bug_report_list`).
_LIST_SQL = (
    "SELECT id, status, severity, agent_role, title FROM bug_reports "
    "ORDER BY id DESC LIMIT 50"
)


def _list_formatter(row: tuple) -> str:
    return _format_pipe_row(
        int(row[0]),        # id %-4d
        row[1],             # status %-15s
        row[2],             # severity %-8s
        row[3],             # agent_role %-15s
        (row[4] or "")[:80],
        spec="dsSst",
    )


def bug_report_list() -> str:
    """Mirror bash `bug_report_list`.

    Returns the 50 most recent ``bug_reports`` rows joined by ``"\\n"`` with
    a trailing ``"\\n"`` — exact ``sqlite3 -separator ' | '`` form.
    """
    db = _resolve_db()
    con = sqlite3.connect(db)
    try:
        con.execute("PRAGMA busy_timeout=5000")
        rows = con.execute(_LIST_SQL).fetchall()
    finally:
        con.close()
    return "".join(_list_formatter(r) + "\n" for r in rows)


# Mirrors lib/bug_report.sh:249-252 (`bug_report_show`).
_SHOW_COLUMNS = [
    "id", "fingerprint", "run_id", "agent_role", "task_class", "observed_in",
    "title", "description", "suggested_fix", "severity", "confidence",
    "frequency", "status", "promoted_to_epic_id",
    "first_seen_at", "last_seen_at", "updated_at",
]


def bug_report_show(bid: int) -> str:
    """Mirror bash `bug_report_show <id>` using ``sqlite3 -line`` format.

    Returns the column dump as ``key = value\\n`` lines, terminated by a
    blank ``\\n`` (mirrors ``sqlite3 -line`` output for a one-row query).
    Column names are right-padded to the longest column name so output is
    visually aligned — mirrors ``sqlite3 -line``'s name-column padding
    (``printf '%*s = %s\\n', max_width, col, val``). ``None`` columns
    render as empty after ``=``.

    Raises:
        ValueError: when no row matches the id (mirrors bash behavior of
                    emitting an empty result — the Python port raises to
                    signal callers explicitly; the bash flow is best-effort
                    via subprocess, so callers don't typically observe the
                    empty case).
    """
    db = _resolve_db()
    con = sqlite3.connect(db)
    try:
        con.execute("PRAGMA busy_timeout=5000")
        row = con.execute(
            f"SELECT {_SHOW_COLUMNS[0]}, " + ", ".join(_SHOW_COLUMNS[1:]) +
            " FROM bug_reports WHERE id=?",
            (int(bid),),
        ).fetchone()
    finally:
        con.close()
    if row is None:
        raise ValueError(f"bug_report_show: no row for id={bid}")
    width = max(len(c) for c in _SHOW_COLUMNS)
    lines: list[str] = []
    for col, val in zip(_SHOW_COLUMNS, row):
        if val is None:
            lines.append(f"{col:>{width}} = \n")
        else:
            lines.append(f"{col:>{width}} = {val}\n")
    return "".join(lines)


# Mirrors lib/bug_report.sh:258-369 (`bug_report_promote`).
def bug_report_promote(
    *args: str,
    top: int = 3,
    repo_root: str | os.PathLike[str] | None = None,
) -> int:
    """Mirror bash `bug_report_promote --top N`.

    Selects the top-N ranked open bugs (rank = severity_weight × frequency ×
    confidence, identical SQL to ``bug_report_prioritize``), then for each:

      1. Build ``epic_id = "bug-{id}-{slug}"`` where ``slug`` is the
         title lower-cased + non-`[a-z0-9-]` collapsed to ``-`` and
         trimmed to 60 chars (fallback ``"bug"`` when empty).
      2. Skip if ``epics.id`` already exists (idempotence on re-promote).
      3. Write the kickoff body to
         ``{repo_root}/kickoffs/auto/{epic_id}.md``.
      4. INSERT a ``epics`` row with ``status='not started'``.
      5. UPDATE ``bug_reports`` to ``status='queued_as_epic'``,
         ``promoted_to_epic_id={epic_id}``,
         ``updated_at=strftime('%s','now')``.

    Returns the number of NEW epics promoted (skipped duplicates do not
    count).

    Positional ``args`` are parsed for ``--top N`` to mirror bash; the
    explicit ``top=`` kwarg takes precedence. ``repo_root`` defaults to
    :func:`_resolve_repo_root`.
    """
    parsed_top = top
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--top" and i + 1 < len(args):
            try:
                parsed_top = int(args[i + 1])
            except ValueError:
                parsed_top = 3
            i += 2
            continue
        i += 1

    db = _resolve_db()
    root = _resolve_repo_root(repo_root)
    out_dir = root / "kickoffs" / "auto"
    out_dir.mkdir(parents=True, exist_ok=True)

    con = sqlite3.connect(db)
    try:
        con.execute("PRAGMA busy_timeout=5000")

        rows = con.execute(
            """SELECT id, fingerprint, agent_role, task_class, observed_in,
                      title, description, suggested_fix, severity,
                      confidence, frequency
                 FROM bug_reports WHERE status='open'
                 ORDER BY CASE severity WHEN 'critical' THEN 8
                                       WHEN 'high'     THEN 4
                                       WHEN 'medium'   THEN 2
                                       ELSE 1 END * frequency * confidence DESC
                 LIMIT ?""",
            (int(parsed_top),),
        ).fetchall()

        promoted = 0
        for full in rows:
            bid = int(full[0])
            fp = full[1]
            role = full[2]
            tc = full[3]
            obs_in = full[4]
            title = full[5] or ""
            desc = full[6] or ""
            fix = full[7] or ""
            sev = full[8]
            conf = float(full[9])
            freq = int(full[10])

            slug_seed = re.sub(r"[^a-z0-9-]+", "-", title.lower().strip()).strip("-")[:60]
            if not slug_seed:
                slug_seed = "bug"
            epic_id = f"bug-{bid}-{slug_seed}"

            exists = con.execute(
                "SELECT id FROM epics WHERE id=?", (epic_id,),
            ).fetchone()
            if exists:
                continue

            kickoff_rel = f"kickoffs/auto/{epic_id}.md"
            kickoff_abs = root / kickoff_rel

            body = [
                f"# Bug-promoted epic: {title}",
                "",
                "## Goal",
                "",
                f"Fix the bug noticed by `{role}` agent during a prior run:",
                "",
                f"> {title}",
                "",
                "## Context",
                "",
                f"- Severity: **{sev}** (confidence {conf:.2f}, observed {freq}x)",
                f"- First noticed by: `{role}` agent",
                f"- Observed in: `{obs_in or 'general'}`",
            ]
            if tc:
                body.append(f"- Originating task_class: `{tc}`")
            body.append("")
            body.extend([
                "## Description",
                "",
                (desc or "(none provided)").strip(),
                "",
            ])
            if fix:
                body.extend([
                    "## Suggested fix (from the noticing agent)",
                    "",
                    fix.strip(),
                    "",
                ])
            body.extend([
                "## Verification commands",
                "",
                "- `shellcheck $(git diff --name-only HEAD~1 HEAD | grep '\\.sh$')`",
                "- `bash tests/integration/test_autonomous_epic_pipeline.sh`",
                "",
                "## Done When",
                "",
                "- `${MINI_ORK_RUN_DIR}/verdict.json` contains `{ \"pass\": true }`.",
                "- The originating noticed_bug fingerprint stops recurring in new runs.",
                "",
                f"_Auto-promoted from bug_reports id={bid} (fingerprint={fp[:12]})._",
                "",
            ])
            kickoff_abs.write_text("\n".join(body), encoding="utf-8")

            epic_title = f"BUG-{bid}: {title[:140]}"
            con.execute(
                "INSERT INTO epics(id, title, status, kickoff_path) "
                "VALUES(?,?,'not started',?)",
                (epic_id, epic_title, kickoff_rel),
            )
            con.execute(
                "UPDATE bug_reports SET status='queued_as_epic', "
                "promoted_to_epic_id=?, updated_at=strftime('%s','now') "
                "WHERE id=?",
                (epic_id, bid),
            )
            promoted += 1

        con.commit()
    finally:
        con.close()
    return promoted