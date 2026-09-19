#!/usr/bin/env python3
"""baseline for the PERF goal-loop: seed and tighten the per-chapter ceilings.

The budget file (``${MO_GOAL_TARGET_CWD}/.mini-ork/perf-budget.json``, or
``MO_GOAL_PERF_BUDGET``) is the ratchet:

    {"<chapter>": {"cost_usd": <float>, "seconds": <float>},
     "_epoch": <int>, "_tighten": <float>}

Ceilings may only ever TIGHTEN. Two verbs:

  --init          Seed each chapter's ceiling from its LIVE measured values:
                  SPEED from the lifecycle wall-clock (``started_at ->
                  finished_at``, fall back to ``committed_at`` then
                  ``updated_at``), COST from the current ``verified-artifact``
                  spend. Stamps ``_epoch`` to now and ``_tighten`` to the
                  (clamped) default.
  --tighten PCT   Multiply every ceiling by ``1 - PCT``, stamp ``_epoch`` to
                  now (a new optimization generation begins), and store
                  ``_tighten``.

The tighten fraction is FLOORED: ``MO_GOAL_PERF_TIGHTEN`` may RAISE it but
never lower it below the shipped default, mirroring ``MO_PROMOTION_MIN_RUNS``
in ``mini_ork/gates/promotion_gate.py``. A floor an environment can lower is
not a floor. The explicit ``--tighten PCT`` is floored too, so ``--tighten 0``
cannot silently stall the ratchet.

Because the book-generation worker is a serial singleton (one chapter at a
time) and ``task_runs`` carries no per-chapter column, the COST measurement is
generation-scoped: the spend since ``_epoch``. At ``--init`` (no prior epoch)
each chapter is therefore seeded from the running total spend, and the ratchet
tightens from there. SPEED is measured per chapter from its own lifecycle row.

No secret lives here; DB reads come from libpq env vars, cost from the target's
sqlite state.db.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import time

_BUDGET_FILENAME = "perf-budget.json"
_RECIPE = "verified-artifact"
# The shipped floor. An env var may raise it; it can never go below this.
_MIN_TIGHTEN = 0.05


def _psql(sql: str) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            [
                "psql",
                "-h", os.environ.get("PGHOST", "REDACTED-INTERNAL-IP"),
                "-p", os.environ.get("PGPORT", "5932"),
                "-U", os.environ.get("PGUSER", "researcher_user"),
                "-d", os.environ.get("PGDATABASE", "researcher_db"),
                "-tA", "-F", "|", "-c", sql,
            ],
            capture_output=True, text=True,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except OSError as exc:
        return 127, "", f"psql exec failed: {exc}"


def _target_cwd() -> str:
    return os.environ.get("MO_GOAL_TARGET_CWD", "").strip() or os.getcwd()


def _budget_path() -> str:
    return os.environ.get("MO_GOAL_PERF_BUDGET", "").strip() or os.path.join(
        _target_cwd(), ".mini-ork", _BUDGET_FILENAME
    )


def _state_db_path() -> str:
    return os.path.join(_target_cwd(), ".mini-ork", "state.db")


def _clamp_tighten(value: str | int | float | None) -> float:
    """Floor a tighten fraction at ``_MIN_TIGHTEN``; a bad value falls back to
    the floor (mirrors ``MO_PROMOTION_MIN_RUNS`` clamping)."""
    if value is None or value == "":
        return _MIN_TIGHTEN
    try:
        return max(_MIN_TIGHTEN, float(value))
    except (TypeError, ValueError):
        return _MIN_TIGHTEN


def _resolve_tighten(explicit: str | int | float | None) -> float:
    """Explicit arg wins, else the env var, else the floor; always floored."""
    if explicit is not None:
        return _clamp_tighten(explicit)
    return _clamp_tighten(os.environ.get("MO_GOAL_PERF_TIGHTEN", _MIN_TIGHTEN))


def _list_chapters(book: str) -> tuple[list[str] | None, str]:
    rc, out, err = _psql(
        "SELECT chapter_number FROM book_chapter_lifecycle "
        f"WHERE book_uuid='{book}' ORDER BY chapter_number;"
    )
    if rc != 0:
        return None, f"db-error: {err.strip()[:80]}"
    chapters = [ln.strip() for ln in out.splitlines() if ln.strip()]
    return chapters, ""


def _measure_cost(epoch: int) -> tuple[float | None, str]:
    path = _state_db_path()
    if not os.path.isfile(path):
        return None, f"state.db missing: {path}"
    try:
        con = sqlite3.connect(path)
        try:
            row = con.execute(
                "SELECT COALESCE(SUM(cost_usd), 0.0) FROM task_runs "
                "WHERE recipe = ? AND created_at > ?",
                (_RECIPE, int(epoch)),
            ).fetchone()
        finally:
            con.close()
    except Exception as exc:  # noqa: BLE001
        return None, f"cost measurement failed: {exc}"
    return float(row[0]), ""


def _measure_speed(chapter: str, book: str) -> tuple[float | None, str]:
    sql = (
        "SELECT EXTRACT(EPOCH FROM started_at)::text, "
        "EXTRACT(EPOCH FROM finished_at)::text, "
        "EXTRACT(EPOCH FROM committed_at)::text, "
        "EXTRACT(EPOCH FROM updated_at)::text "
        "FROM book_chapter_lifecycle "
        f"WHERE book_uuid='{book}' AND chapter_number={chapter};"
    )
    rc, out, err = _psql(sql)
    if rc != 0:
        return None, f"db-error: {err.strip()[:80]}"
    rows = out.strip().splitlines()
    if not rows:
        return None, "no lifecycle row for this chapter"
    cols = (rows[0].split("|") + [""] * 4)[:4]

    def _sec(text: str) -> float | None:
        t = text.strip()
        if not t:
            return None
        try:
            return float(t)
        except ValueError:
            return None

    started = _sec(cols[0])
    finished = _sec(cols[1])
    committed_at = _sec(cols[2])
    updated_at = _sec(cols[3])
    if started is None:
        return None, "started_at NULL where required"
    end = finished if finished is not None else (
        committed_at if committed_at is not None else updated_at
    )
    if end is None:
        return None, "finished_at/committed_at/updated_at all NULL"
    return max(0.0, end - started), ""


def _write_budget(data: dict) -> tuple[bool, str]:
    path = _budget_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.write("\n")
    except OSError as exc:
        return False, f"cannot write budget file {path}: {exc}"
    return True, path


def _cmd_init() -> int:
    book = (os.environ.get("BOOK_UUID") or "").strip()
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", book):
        print(f"BOOK_UUID unset/invalid: {book!r}", file=sys.stderr)
        return 2

    chapters, err = _list_chapters(book)
    if chapters is None:
        print(f"init failed: cannot list chapters: {err}", file=sys.stderr)
        return 2
    if not chapters:
        print("init failed: no chapters found for BOOK_UUID", file=sys.stderr)
        return 2

    # Cost is generation-scoped (serial worker); seed every chapter from the
    # running total once. If it cannot be measured, NO chapter gets a cost
    # ceiling — fail-closed, never fabricate.
    total_cost, err = _measure_cost(0)
    if total_cost is None:
        print(f"init failed: cost unmeasurable: {err}", file=sys.stderr)
        return 1

    data: dict = {"_epoch": int(time.time()), "_tighten": _resolve_tighten(None)}
    for chapter in chapters:
        speed, why = _measure_speed(chapter, book)
        if speed is None:
            print(f"NOTE: ch{chapter} speed unmeasurable: {why} — no ceiling seeded")
            continue
        data[chapter] = {"cost_usd": round(total_cost, 6), "seconds": round(speed, 6)}

    ok, path = _write_budget(data)
    if not ok:
        print(f"init failed: {path}", file=sys.stderr)
        return 1
    print(
        f"init: seeded {len(data) - 2} chapter(s) at epoch {data['_epoch']} "
        f"(tighten floor {data['_tighten']}) -> {path}"
    )
    return 0


def _cmd_tighten(pct_arg: str) -> int:
    path = _budget_path()
    if not os.path.isfile(path):
        print(f"tighten failed: budget file missing: {path}", file=sys.stderr)
        return 2
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        print(f"tighten failed: budget file unreadable: {path} ({exc})", file=sys.stderr)
        return 2
    if not isinstance(data, dict):
        print(f"tighten failed: budget file malformed (not an object): {path}", file=sys.stderr)
        return 2

    pct = _resolve_tighten(pct_arg)
    factor = 1.0 - pct
    tightened = 0
    for key in list(data.keys()):
        if key.startswith("_"):
            continue
        entry = data.get(key)
        if not isinstance(entry, dict):
            continue
        if "cost_usd" in entry and isinstance(entry["cost_usd"], (int, float)):
            entry["cost_usd"] = round(float(entry["cost_usd"]) * factor, 6)
        if "seconds" in entry and isinstance(entry["seconds"], (int, float)):
            entry["seconds"] = round(float(entry["seconds"]) * factor, 6)
        tightened += 1

    data["_epoch"] = int(time.time())
    data["_tighten"] = pct

    ok, path = _write_budget(data)
    if not ok:
        print(f"tighten failed: {path}", file=sys.stderr)
        return 1
    print(
        f"tighten: {tightened} ceiling(s) * {factor:.4f} "
        f"(pct={pct}) at epoch {data['_epoch']} -> {path}"
    )
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="baseline.py",
        description="Seed/tighten the PERF goal-loop per-chapter ceilings.",
    )
    sub = parser.add_subparsers(dest="verb", required=True)
    sub.add_parser("init", help="seed ceilings from live measured values")
    t = sub.add_parser("tighten", help="multiply ceilings by 1-PCT and stamp a new epoch")
    t.add_argument("pct", help="tighten fraction (e.g. 0.10 = tighten 10%%)")
    args = parser.parse_args(argv)

    if args.verb == "init":
        return _cmd_init()
    return _cmd_tighten(args.pct)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
