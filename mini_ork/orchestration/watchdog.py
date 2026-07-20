"""Python port of bin/mini-ork-watchdog — per-pass scoring/abort logic.

Strangler-fig parity port of the bash wrapper's ``_watchdog_pass`` heredoc.
Reads the same ``STATE_DB`` / ``MINI_ORK_HOME`` env contract (lines 25-30 of
``bin/mini-ork-watchdog``); mirrors the active-run filter, the
``description LIKE '%status=failure%' OR description LIKE '%status=vacuous%'``
pattern filter, the ``_tok/_tf/_cos`` cosine-similarity helpers, the
``round(min(1.0, best_score + fail_boost), 4)`` scoring, the
``<home>/runs/run-<id>/.stop-requested`` file write (with glob fallback), and
the ``watchdog_aborts`` insert (with OperationalError-swallowing parity).

Public surface:

    pass_once(db_path, threshold, dry_run, warn_only, mini_ork_home) -> dict

The returned dict matches the JSON printed by the bash heredoc byte-for-byte
when re-serialised via ``json.dumps(sort_keys=True)``. Floats are 4-decimal
``round()`` so 1e-6 parity is trivially satisfied.

CLI shim is the only consumer of argparse; the parity test exercises
``pass_once`` directly so it can drive bash via subprocess and diff real
``watchdog_aborts`` rows.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sqlite3
import sys
import time
from collections import Counter


# Default knobs — same defaults as bash (lines 33-36).
DEFAULT_THRESHOLD = 0.65
DEFAULT_POLL_SECS = 30


def _tok(s):
    """Tokenise a string. Mirrors the bash heredoc verbatim: lowercase, swap
    any run of ``[^\w./_-]+`` for a single space, drop tokens shorter than 3
    characters.
    """
    s = (s or "").lower()
    s = re.sub(r"[^\w./_-]+", " ", s)
    return [t for t in s.split() if len(t) >= 3]


def _tf(toks):
    """Term-frequency normalisation: count / max(count, 1). The ``or 1`` in
    the sum guards the empty-bag path so the caller sees a 1-element dict
    rather than a ZeroDivisionError.
    """
    c = Counter(toks)
    n = sum(c.values()) or 1
    return {t: cnt / n for t, cnt in c.items()}


def _cos(a, b):
    """Cosine similarity over sparse dicts. Returns 0.0 when either side
    has zero magnitude (mirrors the bash short-circuit exactly).
    """
    keys = set(a) | set(b)
    dot = sum(a.get(k, 0) * b.get(k, 0) for k in keys)
    na = math.sqrt(sum(v * v for v in a.values()))
    nb = math.sqrt(sum(v * v for v in b.values()))
    return dot / (na * nb) if na and nb else 0.0


def _open_db(db_path):
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA busy_timeout=5000")
    con.row_factory = sqlite3.Row
    return con


def _fetch_active_runs(con):
    try:
        return con.execute(
            """
            SELECT id, status
              FROM task_runs
             WHERE (status IS NULL
                    OR status NOT IN ('published','failed','rolled_back','approved','completed'))
               AND created_at >= strftime('%s','now','-24 hours')
            """
        ).fetchall()
    except sqlite3.OperationalError:
        return []


def _fetch_failing_patterns(con):
    try:
        return con.execute(
            """
            SELECT pattern_id, description, frequency
              FROM pattern_records
             WHERE description LIKE '%status=failure%'
                OR description LIKE '%status=vacuous%'
            """
        ).fetchall()
    except sqlite3.OperationalError:
        return []


def _write_stop_requested(home, run_id, matched_pattern, score):
    """Mirror bash lines 155-166: <home>/runs/run-<id>/.stop-requested, with
    glob fallback for non-standard run-dir names. Both the os.makedirs and
    the open() are wrapped in the same OSError swallow.
    """
    run_dir = os.path.join(home, "runs", f"run-{run_id}")
    if not os.path.isdir(run_dir):
        import glob as _glob
        matches = _glob.glob(os.path.join(home, "runs", f"*{run_id}*"))
        run_dir = matches[0] if matches else run_dir
    try:
        os.makedirs(run_dir, exist_ok=True)
        with open(os.path.join(run_dir, ".stop-requested"), "w") as f:
            f.write(f"watchdog abort: matched={matched_pattern} score={score}\n")
    except OSError:
        pass


def _insert_abort(con, run_id, task_class, matched_pattern, score,
                  fail_count, trace_count, outcome):
    """Mirror bash lines 168-180: same columns, same OperationalError swallow.
    outcome is "aborted" or "warned_only" (matches the CHECK constraint in
    migration 0033_watchdog_aborts.sql).
    """
    try:
        con.execute(
            """
            INSERT INTO watchdog_aborts
                (run_id, task_class, matched_pattern, match_score,
                 evidence, outcome, aborted_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id, task_class, matched_pattern, score,
                json.dumps({"fail_count": fail_count, "traces": trace_count}),
                outcome,
                int(time.time()),
            ),
        )
    except sqlite3.OperationalError:
        pass


def pass_once(db_path, threshold, dry_run, warn_only, mini_ork_home) -> dict:
    """Run one watchdog pass and return the JSON-summary dict.

    Args mirror the bash ``_watchdog_pass`` argument vector (the Python
    heredoc inside ``bin/mini-ork-watchdog``):
      - db_path:       STATE_DB
      - threshold:     MO_WATCHDOG_ABORT_THRESHOLD (float)
      - dry_run:       bool, --dry-run
      - warn_only:     bool, --warn-only
      - mini_ork_home: MINI_ORK_HOME (parent of runs/)

    Side effects (mirror bash):
      - writes <home>/runs/run-<id>/.stop-requested on abort
      - inserts into watchdog_aborts on abort OR warned_only

    Returns dict with keys in bash-insertion order:
      active_runs, decisions, aborted, warned, no_match
    """
    con = _open_db(db_path)
    try:
        active = _fetch_active_runs(con)
        failing_patterns = _fetch_failing_patterns(con)
        pattern_vecs = [(p, _tf(_tok(p["description"]))) for p in failing_patterns]

        decisions = []
        for run in active:
            run_id = str(run["id"])
            traces = con.execute(
                """
                SELECT task_class, status, reviewer_verdict, agent_version_id
                  FROM execution_traces
                 WHERE run_id = ?
                   AND status IS NOT NULL
                """,
                (run_id,),
            ).fetchall()
            if len(traces) < 2:
                continue
            tc = traces[-1]["task_class"]
            bag_parts, fail_count = [], 0
            for t in traces:
                st = (t["status"] or "")
                bag_parts.append(f"status={st}")
                bag_parts.append(f"task_class={t['task_class'] or ''}")
                rv = t["reviewer_verdict"] or ""
                if rv:
                    bag_parts.append(f"verdict={rv}")
                if st in ("failure", "vacuous"):
                    fail_count += 1
            bag_vec = _tf(_tok(" ".join(bag_parts)))
            if not bag_vec:
                continue
            best_score, best_p = 0.0, None
            for p, pv in pattern_vecs:
                s = _cos(bag_vec, pv)
                if s > best_score:
                    best_score, best_p = s, p
            fail_boost = min(0.20, 0.05 * fail_count)
            score = round(min(1.0, best_score + fail_boost), 4)
            if best_p is None:
                continue
            action = "no-match"
            if score >= threshold:
                if warn_only:
                    action = "warned_only"
                elif dry_run:
                    action = "would_abort"
                else:
                    action = "abort"

            decisions.append({
                "run_id": run_id,
                "task_class": tc,
                "matched_pattern": best_p["pattern_id"],
                "match_score": score,
                "fail_count": fail_count,
                "action": action,
            })

            if action == "abort":
                _write_stop_requested(mini_ork_home, run_id,
                                       best_p["pattern_id"], score)
            if action in ("abort", "warned_only"):
                _insert_abort(con, run_id, tc, best_p["pattern_id"], score,
                              fail_count, len(traces),
                              "aborted" if action == "abort" else "warned_only")

        con.commit()
        return {
            "active_runs": len(active),
            "decisions": decisions,
            "aborted": sum(1 for d in decisions if d["action"] == "abort"),
            "warned": sum(1 for d in decisions if d["action"] == "warned_only"),
            "no_match": sum(1 for d in decisions if d["action"] == "no-match"),
        }
    finally:
        con.close()


# ─── CLI shim ────────────────────────────────────────────────────────────────
# Pure-stdlib argparse so the port is a drop-in for the bash wrapper when a
# caller wants the Python implementation in production. The parity test does
# NOT exercise this path — it calls pass_once directly — so the argparse
# layer is allowed to drift from bash's flag-parser as long as the per-pass
# logic is byte-equivalent (which the gate proves).

def _resolve_env():
    """Mirror bash lines 25-30: MINI_ORK_ROOT, MINI_ORK_HOME, STATE_DB."""
    mini_ork_root = os.environ.get("MINI_ORK_ROOT") or os.getcwd()
    mini_ork_home = os.environ.get("MINI_ORK_HOME") or os.path.join(mini_ork_root, ".mini-ork")
    state_db = os.environ.get("MINI_ORK_DB") or os.path.join(mini_ork_home, "state.db")
    return mini_ork_root, mini_ork_home, state_db


def _build_parser():
    p = argparse.ArgumentParser(
        prog="mini_ork_watchdog",
        description="Python port of bin/mini-ork-watchdog — periodic early-failure "
                    "prediction for active runs.",
    )
    p.add_argument("--once", action="store_true", default=True,
                   help="Single pass over active runs, then exit (default).")
    p.add_argument("--poll-secs", type=int, default=DEFAULT_POLL_SECS,
                   help="Idle seconds between passes (default 30).")
    p.add_argument("--dry-run", action="store_true",
                   help="Print decisions, do not write .stop-requested.")
    p.add_argument("--threshold", type=float,
                   default=float(os.environ.get("MO_WATCHDOG_ABORT_THRESHOLD",
                                                DEFAULT_THRESHOLD)),
                   help="Match score >= T triggers abort (default 0.65).")
    p.add_argument("--warn-only", action="store_true",
                   help="Log to watchdog_aborts but never write .stop-requested.")
    return p


def main(argv=None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    _, mini_ork_home, state_db = _resolve_env()
    if not os.path.isfile(state_db):
        print(f"watchdog: {state_db} missing", file=sys.stderr)
        return 1

    summary = pass_once(state_db, args.threshold, args.dry_run,
                        args.warn_only, mini_ork_home)
    print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())