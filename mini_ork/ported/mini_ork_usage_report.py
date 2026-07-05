"""Python port of bin/mini-ork-usage-report — per-(code_region, lane) report.

Strangler-fig co-tenant: ``bin/mini-ork-usage-report`` stays untouched; this
port gives Python callers an in-process target and the parity test a stable
surface for byte-for-byte comparison against the live bash. The bash script
composes two embedded Python heredocs (collect + render); this port splits
those into pure functions so tests can pin inputs/outputs and the smoke
self-test can run in-process without a nested subprocess round-trip.

Public surface (mirrors bash sub-commands + smoke self-test):

    collect_region_expertise(db_path, since=0) -> dict
    render_json(report) -> str                   # indent=2 + sort_keys=True + '\\n'
    build_smoke_db(db_path) -> None              # CREATE TABLE + seed rows
    run_smoke() -> int                           # build tmp dir, run + assert
    parse_argv(argv) -> tuple                    # mirrors bash case block
    help_text() -> str                           # bash heredoc verbatim
    main(argv=None, *, stdout=None, stderr=None) -> int

Output parity:

* The bash script emits JSON via ``json.dump(report, f, indent=2,
  sort_keys=True)`` + a single trailing ``\\n``. Python must mirror
  exactly. ``sort_keys=True`` is load-bearing: the multi-region/lane
  case depends on lexicographic entry ordering for byte-equality.
* Datetime format string is ``%Y-%m-%dT%H:%M:%fZ`` (Python 6-digit
  microseconds). The defect_attributions timestamp parser accepts
  both ``%Y-%m-%dT%H:%M:%fZ`` (Python-style) and ``%Y-%m-%dT%H:%M:%SZ``
  (SQLite-style, 3-digit milliseconds or no-fraction) — both forms
  appear in legacy migration seeds.
* Missing-DB branch emits ``{"generated_at", "source_db", "since",
  "entry_count": 0, "entries": [], "note": "state.db not found; no
  region evidence to summarize"}``. The ``note`` field is ONLY present
  in this branch — production + smoke paths omit it.

Env knobs honored (also accepted as explicit kwargs to ``main()``):

* ``MINI_ORK_DB``   — state.db path (default
  ``${MINI_ORK_DB:-${MINI_ORK_HOME:-$(pwd)/.mini-ork}/state.db}``).
* ``MINI_ORK_HOME`` — base dir for the default DB path.

Parity is enforced by ``tests/unit/test_mini_ork_usage_report_py.py``
(8 cases driving the LIVE bash subprocess against a temp DB seeded by
``db/init.sh`` and comparing parsed region_expertise.json dicts with
floats within 1e-6, ``generated_at`` ignored).
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import warnings
from collections import defaultdict

# The bash script uses datetime.utcnow() / utcfromtimestamp() (pre-3.12
# API) verbatim. To preserve byte-equal parity with bash's strftime
# output, the port mirrors those exact calls — silencing the
# deprecation chatter keeps the parity surface noise-free.
warnings.filterwarnings("ignore", message=".*utcnow.*", category=DeprecationWarning)
warnings.filterwarnings("ignore", message=".*utcfromtimestamp.*",
                        category=DeprecationWarning)


__all__ = [
    "collect_region_expertise",
    "render_json",
    "build_smoke_db",
    "run_smoke",
    "parse_argv",
    "help_text",
    "main",
]


def _db_path() -> str:
    """Default DB path: ${MINI_ORK_DB:-${MINI_ORK_HOME:-$(pwd)/.mini-ork}/state.db}."""
    db = os.environ.get("MINI_ORK_DB")
    if db:
        return db
    home = os.environ.get("MINI_ORK_HOME")
    if home:
        return os.path.join(home, "state.db")
    return os.path.join(os.getcwd(), ".mini-ork", "state.db")


def _now_iso() -> str:
    """Mirror bash ``datetime.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%fZ')``."""
    return _dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%fZ")


def _parse_ts(ts_raw):
    """Two-pass strptime mirroring bash heredoc lines 332-342.

    Accepts both Python's 6-digit-microsecond form
    (``%Y-%m-%dT%H:%M:%fZ``) and SQLite's 3-digit-millisecond /
    no-fraction form (``%Y-%m-%dT%H:%M:%SZ``). Returns None on
    parse failure (bash skips the row silently).
    """
    try:
        return _dt.datetime.strptime(ts_raw, "%Y-%m-%dT%H:%M:%fZ")
    except ValueError:
        stripped = ts_raw
        if "." in ts_raw:
            stripped = ts_raw[: ts_raw.index(".")] + "Z"
        try:
            return _dt.datetime.strptime(stripped, "%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            return None


def collect_region_expertise(db_path: str, since: int = 0) -> dict:
    """Mirror bash heredoc (production path): query lane_region_advantage +
    defect_attributions, apply the frc-a5 decay formula, aggregate per
    (code_region, agent_version_id).

    Returns the dict the bash script writes as JSON:

        {
          "generated_at": "<ISO-8601 UTC, %Y-%m-%dT%H:%M:%fZ>",
          "source_db":    "<path>",
          "since":        <epoch int>,
          "entry_count":  <int>,
          "entries": [
            {
              "code_region":               <str>,
              "lane":                      <str, agent_version_id>,
              "advantage":                 <float, weighted mean × runs_count>,
              "sample_size":               <int, sum of runs_count>,
              "outstanding_blame_penalty": <float, sum of decayed penalties>,
            }, ...
          ],
        }

    Empty/OperationalError tables → empty entries list. Mirrors bash's
    try/except sqlite3.OperationalError fallbacks exactly.
    """
    con = sqlite3.connect(db_path)
    try:
        con.execute("PRAGMA busy_timeout=5000")
        con.row_factory = sqlite3.Row

        now = _dt.datetime.utcnow()

        # ── Slice A: per-(region, lane) advantage + sample size ─────
        try:
            region_rows = con.execute(
                """
                SELECT agent_version_id, task_class, node_type,
                       objective_domain, code_region, relative_advantage,
                       runs_count
                  FROM lane_region_advantage
                 WHERE code_region IS NOT NULL AND code_region <> ''
                   AND last_updated >= ?
                """,
                (_dt.datetime.utcfromtimestamp(int(since)).strftime(
                    "%Y-%m-%dT%H:%M:%S.000Z"),),
            ).fetchall()
        except sqlite3.OperationalError:
            region_rows = []

        agg = defaultdict(lambda: {"adv_sum": 0.0, "n": 0, "runs": 0})
        for r in region_rows:
            key = (r["code_region"], r["agent_version_id"])
            agg[key]["adv_sum"] += float(r["relative_advantage"]) * int(
                r["runs_count"])
            agg[key]["n"] += int(r["runs_count"])
            agg[key]["runs"] += int(r["runs_count"])

        # ── Slice B: outstanding_blame_penalty per (region, lane) ──
        penalty_by_region_lane = defaultdict(float)
        try:
            pen_rows = con.execute(
                """
                SELECT lane, code_region, task_class,
                       penalty, decay_halflife_days, ts
                  FROM defect_attributions
                 WHERE penalty IS NOT NULL AND penalty <> 0
                   AND code_region IS NOT NULL AND code_region <> ''
                """
            ).fetchall()
            for pr in pen_rows:
                try:
                    pen = float(pr["penalty"])
                    hlf_raw = pr["decay_halflife_days"]
                    hlf = float(hlf_raw) if hlf_raw is not None else 30.0
                except (TypeError, ValueError):
                    continue
                if hlf <= 0:
                    continue
                ts = _parse_ts(pr["ts"])
                if ts is None:
                    continue
                age_days = max((now - ts).total_seconds() / 86400.0, 0.0)
                decay = 0.5 ** (age_days / hlf)
                effective = pen * decay
                key = (pr["code_region"], pr["lane"])
                penalty_by_region_lane[key] += effective
        except sqlite3.OperationalError:
            pass

        # ── Merge: one entry per (region, lane) pair, sorted ────────
        entries = []
        all_keys = set(agg.keys()) | set(penalty_by_region_lane.keys())
        for (region, lane) in sorted(all_keys):
            stats = agg.get((region, lane), {"adv_sum": 0.0, "n": 0, "runs": 0})
            advantage = stats["adv_sum"] / stats["n"] if stats["n"] > 0 else 0.0
            entries.append({
                "code_region": region,
                "lane": lane,
                "advantage": round(advantage, 6),
                "sample_size": int(stats["runs"]),
                "outstanding_blame_penalty": round(
                    penalty_by_region_lane.get((region, lane), 0.0), 6),
            })

        return {
            "generated_at": now.strftime("%Y-%m-%dT%H:%M:%fZ"),
            "source_db": db_path,
            "since": int(since),
            "entry_count": len(entries),
            "entries": entries,
        }
    finally:
        con.close()


def render_json(report: dict) -> str:
    """Mirror bash ``json.dump(report, f, indent=2, sort_keys=True)`` + '\\n'.

    ``sort_keys=True`` is load-bearing for the multi-region/lane case:
    the bash JSON emitter sorts keys lexicographically, and the smoke
    parity test compares the parsed dicts (which preserves order via
    Python 3.7+ dict semantics). Without ``sort_keys=True``,
    intermediate key reordering would break byte-equality on
    unordered input (e.g. random-hash dict iteration).
    """
    return json.dumps(report, indent=2, sort_keys=True) + "\n"


def build_smoke_db(db_path: str) -> None:
    """Mirror bash --smoke heredoc (lines 117-175): CREATE TABLE IF NOT
    EXISTS lane_region_advantage + defect_attributions, then seed 3
    region rows (codex_lens/kimi_lens in lib + codex_lens in bin) and
    1 fresh defect_attribution for codex_lens/lib at age=0.

    Used by ``run_smoke()`` to build a synthetic DB without depending
    on db/init.sh (the smoke must work in isolation, e.g. from a CI
    checkout where mini-ork init hasn't run).
    """
    con = sqlite3.connect(db_path)
    try:
        con.executescript("""
CREATE TABLE IF NOT EXISTS lane_region_advantage (
  agent_version_id   TEXT    NOT NULL,
  task_class         TEXT    NOT NULL,
  node_type          TEXT    NOT NULL DEFAULT '',
  objective_domain   TEXT    NOT NULL DEFAULT '',
  code_region        TEXT    NOT NULL DEFAULT '',
  relative_advantage REAL    NOT NULL DEFAULT 0.0,
  runs_count         INTEGER NOT NULL DEFAULT 0,
  success_count      INTEGER NOT NULL DEFAULT 0,
  last_updated       TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  PRIMARY KEY (agent_version_id, task_class, node_type, objective_domain, code_region)
);
CREATE TABLE IF NOT EXISTS defect_attributions (
  id                   INTEGER PRIMARY KEY AUTOINCREMENT,
  found_run_id         TEXT    NOT NULL,
  blamed_run_id        TEXT    NOT NULL,
  lane                 TEXT    NOT NULL,
  code_region          TEXT    NOT NULL,
  task_class           TEXT    NOT NULL,
  severity             TEXT    NOT NULL DEFAULT 'medium',
  penalty              REAL    NOT NULL,
  decay_halflife_days  REAL    NOT NULL DEFAULT 30.0,
  ts                   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
""")
        con.execute("""
          INSERT INTO lane_region_advantage
            (agent_version_id, task_class, node_type, objective_domain,
             code_region, relative_advantage, runs_count, success_count)
          VALUES
            ('codex_lens', 'code-fix', 'implementer', 'code-delivery',
             'lib', 0.45, 2, 2),
            ('kimi_lens',  'code-fix', 'implementer', 'code-delivery',
             'lib', -0.45, 2, 0),
            ('codex_lens', 'code-fix', 'implementer', 'code-delivery',
             'bin', 0.20, 1, 1)
        """)
        con.execute("""
          INSERT INTO defect_attributions
            (found_run_id, blamed_run_id, lane, code_region, task_class,
             severity, penalty, decay_halflife_days, ts)
          VALUES
            ('run-found-smoke', 'run-blamed-smoke', 'codex_lens', 'lib',
             'code-fix', 'high', -0.6, 30.0,
             strftime('%Y-%m-%dT%H:%M:%fZ','now'))
        """)
        con.commit()
    finally:
        con.close()


def run_smoke() -> int:
    """Mirror bash --smoke: build tmp dir, create synthetic DB, invoke
    collect_region_expertise + render_json, assert top-level + 5
    required entry fields, assert codex_lens/lib has advantage≈0.45,
    sample_size==2, outstanding_blame_penalty in [-1, 0].

    Returns 0 on pass, non-zero on any assertion failure. Writes a
    short PASS/FAIL line to stdout (mirrors bash printf) and FAIL
    diagnostics to stderr.
    """
    tmp = tempfile.mkdtemp(prefix="mini-ork-usage-report-smoke.")
    smoke_db = os.path.join(tmp, "state.db")
    smoke_out = os.path.join(tmp, "region_expertise.json")
    try:
        build_smoke_db(smoke_db)
        # Run production path against synthetic DB (mirrors bash
        # `bash $0 --db "$DB" --out "$OUT" --since "$SINCE" --format "$FORMAT"`).
        report = collect_region_expertise(smoke_db, since=0)
        with open(smoke_out, "w", encoding="utf-8") as f:
            f.write(render_json(report))

        with open(smoke_out, encoding="utf-8") as f:
            d = json.load(f)
        required_top = ("generated_at", "entries")
        for k in required_top:
            assert k in d, f"missing top-level field: {k}"
        entries = d["entries"]
        assert isinstance(entries, list), "entries must be a list"
        assert len(entries) >= 1, f"entries must be ≥1, got {len(entries)}"
        required_entry = (
            "code_region", "lane", "advantage",
            "sample_size", "outstanding_blame_penalty",
        )
        for i, e in enumerate(entries):
            missing = [k for k in required_entry if k not in e]
            assert not missing, f"entry {i} missing fields: {missing}"
        target = next(
            (e for e in entries
             if e["code_region"] == "lib" and e["lane"] == "codex_lens"),
            None,
        )
        assert target is not None, (
            "expected codex_lens/lib entry in smoke fixture"
        )
        assert abs(target["advantage"] - 0.45) < 1e-6, (
            f"smoke advantage drifted: {target['advantage']}"
        )
        assert target["sample_size"] == 2, (
            f"smoke sample_size drifted: {target['sample_size']}"
        )
        assert target["outstanding_blame_penalty"] < 0.0, (
            f"smoke outstanding_blame_penalty should be negative, got "
            f"{target['outstanding_blame_penalty']}"
        )
        assert target["outstanding_blame_penalty"] >= -1.0, (
            f"smoke outstanding_blame_penalty out of [-1, 0]: "
            f"{target['outstanding_blame_penalty']}"
        )
        sys.stdout.write(
            f"mini-ork-usage-report --smoke: PASS ({len(entries)} entries, "
            f"codex_lens/lib advantage={target['advantage']}, "
            f"outstanding_blame_penalty="
            f"{target['outstanding_blame_penalty']:.4f})\n"
        )
        return 0
    except AssertionError as exc:
        sys.stderr.write(f"mini-ork-usage-report --smoke: FAIL\n{exc}\n")
        return 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def parse_argv(argv):
    """Mirror bash case block (lines 89-100).

    Returns ``(db, out, since, fmt, smoke, help_flag, unknown, parse_err)``.
    Defaults: db=_db_path(), out="./region_expertise.json", since=0,
    fmt="json", smoke=False.
    """
    if argv is None:
        argv = sys.argv[1:]
    db = _db_path()
    out = "region_expertise.json"
    since = 0
    fmt = "json"
    smoke = False
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("--help", "-h"):
            return (db, out, since, fmt, smoke, True, None, None)
        if a == "--db":
            if i + 1 >= len(argv):
                return (db, out, since, fmt, smoke, False, None,
                        "missing value for --db")
            db = argv[i + 1]
            i += 2
        elif a == "--out":
            if i + 1 >= len(argv):
                return (db, out, since, fmt, smoke, False, None,
                        "missing value for --out")
            out = argv[i + 1]
            i += 2
        elif a == "--since":
            if i + 1 >= len(argv):
                return (db, out, since, fmt, smoke, False, None,
                        "missing value for --since")
            try:
                since = int(argv[i + 1])
            except ValueError:
                return (db, out, since, fmt, smoke, False, None,
                        f"invalid epoch for --since: {argv[i+1]}")
            i += 2
        elif a == "--format":
            if i + 1 >= len(argv):
                return (db, out, since, fmt, smoke, False, None,
                        "missing value for --format")
            fmt = argv[i + 1]
            i += 2
        elif a == "--smoke":
            smoke = True
            i += 1
        else:
            return (db, out, since, fmt, smoke, False, a, None)
    return (db, out, since, fmt, smoke, False, None, None)


def help_text() -> str:
    """Mirror bash ``_usage`` heredoc verbatim (lines 50-87)."""
    return (
        "Usage: mini-ork-usage-report [--db PATH] [--out PATH] [--since EPOCH]\n"
        "                             [--format json] [--smoke] [--help]\n"
        "\n"
        "Emit region_expertise.json: per-(code_region, lane) advantage, sample\n"
        "size, and outstanding blame penalty.\n"
        "\n"
        "Options:\n"
        "  --db PATH         SQLite state DB (default: $MINI_ORK_DB or\n"
        "                    .mini-ork/state.db).\n"
        "  --out PATH        Output file (default: ./region_expertise.json).\n"
        "  --since EPOCH     Unix timestamp lower bound (default: 0, i.e. all time).\n"
        "  --format json     Output format (default; reserved for future markdown).\n"
        "  --smoke           Build a synthetic DB, emit a fixture region_expertise.json,\n"
        "                    assert it parses + contains ≥1 entry with the 5 required\n"
        "                    fields. Exits non-zero on any assertion failure.\n"
        "  --help            This message.\n"
        "\n"
        "Schema (region_expertise.json):\n"
        "  {\n"
        "    \"generated_at\": \"<ISO-8601 UTC>\",\n"
        "    \"source_db\":    \"<absolute path>\",\n"
        "    \"since\":        <epoch int>,\n"
        "    \"entry_count\":  <int>,\n"
        "    \"entries\": [\n"
        "      {\n"
        "        \"code_region\":                \"<top-level dir>\",\n"
        "        \"lane\":                       \"<agent_version_id / model lane>\",\n"
        "        \"advantage\":                  <float, post-decay mean>,\n"
        "        \"sample_size\":                <int, sum of runs_count>,\n"
        "        \"outstanding_blame_penalty\":  <float, sum of decayed penalties>\n"
        "      },\n"
        "      ...\n"
        "    ]\n"
        "  }\n"
    )


def main(argv=None, *, stdout=None, stderr=None) -> int:
    """Mirror bash dispatch (lines 89-257).

    * ``--help`` / ``-h`` → print help, exit 0.
    * ``--smoke`` → run synthetic self-test, exit 0 on pass.
    * Missing DB → emit empty-but-valid report with ``note`` field,
      exit 0 (mirrors bash lines 240-256).
    * Production → collect + render + write JSON to OUT, exit 0.
    * Unknown flag / parse error → print error + help to stderr/stdout,
      exit 2.
    """
    out = stdout if stdout is not None else sys.stdout
    err = stderr if stderr is not None else sys.stderr

    db, out_path, since, fmt, smoke, help_flag, unknown, parse_err = (
        parse_argv(argv)
    )
    # `fmt` accepted for bash parity (--format is reserved for future
    # markdown). Suppress unused-var diagnostics without dropping the
    # value from the return tuple (tests assert on the shape).
    del fmt

    if help_flag:
        out.write(help_text())
        return 0

    if parse_err:
        err.write(f"mini-ork-usage-report: {parse_err}\n")
        out.write(help_text())
        return 2

    if unknown is not None:
        err.write(f"mini-ork-usage-report: unknown flag: {unknown}\n")
        out.write(help_text())
        return 2

    if smoke:
        # Delegate to run_smoke; it writes its own PASS/FAIL line to
        # stdout/stderr (mirrors bash printf behaviour).
        return run_smoke()

    # ── Missing-DB branch: emit empty-but-valid report with note ──
    if not os.path.isfile(db):
        report = {
            "generated_at": _now_iso(),
            "source_db": db,
            "since": int(since),
            "entry_count": 0,
            "entries": [],
            "note": "state.db not found; no region evidence to summarize",
        }
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(render_json(report))
        return 0

    # ── Production path ────────────────────────────────────────────
    report = collect_region_expertise(db, since=since)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(render_json(report))

    # bash echo: "mini-ork-usage-report: wrote $OUT (... bytes, $SINCE since)"
    try:
        n_bytes = os.path.getsize(out_path)
    except OSError:
        n_bytes = 0
    out.write(
        f"mini-ork-usage-report: wrote {out_path} ({n_bytes} bytes, "
        f"{since} since)\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
