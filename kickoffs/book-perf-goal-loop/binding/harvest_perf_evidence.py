#!/usr/bin/env python3
"""Deep COST+TIME evidence harvester for the PERF goal-loop (MO_GOAL_EVIDENCE_CMD).

Invoked as ``python3 harvest_perf_evidence.py <chapter_number>`` (argv, not
shell) inside ``MO_GOAL_TARGET_CWD`` (the researcher worktree). Where the
correctness harvester explains WHY a chapter fails, this one explains WHERE THE
MONEY AND TIME GO, so the fix child targets the dominant ``verified-artifact``
node instead of guessing from an 80-char predicate slice. The goal-loop threads
this into the child's kickoff as ``{{evidence}}``.

Three best-effort tiers:

  1. DB       — the ``book_chapter_lifecycle`` row (status, quality flags,
                generation_attempts) + the measured wall-clock.
  2. Spend    — per-node ``verified-artifact`` run counts, ``cost_usd`` and
                duration from ``task_runs`` in ``<target>/.mini-ork/state.db``,
                scoped to the current optimization generation by the budget
                file's ``_epoch`` (an unscoped sum is the whole DB history and
                names a node fixed generations ago); the node with the largest
                share, and a re-roll count.
  3. Recovery — failed/rolled-back runs (recovery cycles) and the generated
                run total, as the child's cost-reduction target.

Every tier is defensive: a tier that cannot resolve prints a ``NOTE:`` and the
harvest continues. Evidence is advisory and must never crash the wave. No
secret lives here; the DB tier reads libpq env vars, the spend tiers read the
target's sqlite state.db.
"""
from __future__ import annotations

import os
import re
import sqlite3
import subprocess
import sys

_RECIPE = "verified-artifact"


def _emit(line: str = "") -> None:
    sys.stdout.write(line + "\n")


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


def _state_db_path() -> str:
    return os.path.join(_target_cwd(), ".mini-ork", "state.db")


def _runs_dir() -> str:
    return os.path.join(_target_cwd(), ".mini-ork", "runs")


def _budget_epoch() -> int | None:
    """The generation epoch stamped by baseline.py, or None when unstamped."""
    path = os.environ.get("MO_GOAL_PERF_BUDGET", "").strip() or os.path.join(
        _target_cwd(), ".mini-ork", "perf-budget.json"
    )
    try:
        import json
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh).get("_epoch")
        return int(raw) if raw is not None else None
    except (OSError, ValueError, TypeError, AttributeError):
        return None


# The caller's node identity is not a column on task_runs — kickoff_path is a
# random staging tmpdir whose basename is always ``kickoff.md``, so grouping on
# it collapses every run into one bucket. The identity IS recoverable: the run
# dir's context-pack carries the staged kickoff verbatim, whose first line names
# the node (``# Generate verified artifact <node_key> (<node_type>)``).
_NODE_RE = re.compile(r"^#\s*Generate verified artifact\s+(\S+)\s*\(([^)]+)\)")


def _node_from_kickoff_text(text: str) -> str:
    m = _NODE_RE.match((text or "").strip())
    return f"{m.group(1)} ({m.group(2)})" if m else ""


def _node_for_run(run_id: str, kickoff_path: str) -> str:
    """Best-effort caller-node label for one verified-artifact run. Never raises."""
    try:
        import json
        pack = os.path.join(_runs_dir(), run_id, "context-pack.json")
        if os.path.isfile(pack):
            with open(pack, "r", encoding="utf-8") as fh:
                kickoff = (
                    ((json.load(fh).get("task_brief") or {}).get("content") or {})
                    .get("kickoff", "")
                )
            label = _node_from_kickoff_text(kickoff)
            if label:
                return label
    except Exception:  # noqa: BLE001 — attribution is advisory
        pass
    try:
        if kickoff_path and os.path.isfile(kickoff_path):
            with open(kickoff_path, "r", encoding="utf-8") as fh:
                label = _node_from_kickoff_text(fh.read(4000))
            if label:
                return label
    except Exception:  # noqa: BLE001
        pass
    return f"unattributed ({run_id})"


def _epoch_seconds(text: str) -> float | None:
    t = (text or "").strip()
    if not t:
        return None
    try:
        return float(t)
    except ValueError:
        return None


# ── Tier 1: DB ───────────────────────────────────────────────────────────────

def _tier_db(chapter: str, book: str) -> None:
    _emit("## 1. Live generation-status (book_chapter_lifecycle)")
    _emit()
    sql = (
        "SELECT status, coalesce(rubric_status,''), committed_complete, "
        "permanently_failed, degraded, generation_attempts, "
        "EXTRACT(EPOCH FROM started_at)::text, "
        "EXTRACT(EPOCH FROM finished_at)::text, "
        "EXTRACT(EPOCH FROM committed_at)::text, "
        "EXTRACT(EPOCH FROM updated_at)::text, "
        "left(coalesce(last_error,''), 80) "
        "FROM book_chapter_lifecycle "
        f"WHERE book_uuid='{book}' AND chapter_number={chapter};"
    )
    rc, out, err = _psql(sql)
    if rc != 0:
        _emit(f"NOTE: db query failed rc={rc}: {err.strip()[:200]}")
        _emit()
        return
    rows = out.strip().splitlines()
    if not rows:
        _emit(f"NOTE: no lifecycle row for chapter {chapter}.")
        _emit()
        return
    cols = (rows[0].split("|") + [""] * 11)[:11]
    (status, rubric, committed, permfail, degraded, attempts,
     started_t, finished_t, committed_t, updated_t, lasterr) = cols
    started = _epoch_seconds(started_t)
    finished = _epoch_seconds(finished_t)
    committed_at = _epoch_seconds(committed_t)
    updated_at = _epoch_seconds(updated_t)
    end = finished if finished is not None else (
        committed_at if committed_at is not None else updated_at
    )
    wall = "?" if (started is None or end is None) else f"{max(0.0, end - started):.1f}s"
    _emit("```")
    _emit(f"status               = {status}")
    _emit(f"rubric_status        = {rubric or '(none)'}")
    _emit(f"committed_complete   = {committed}")
    _emit(f"permanently_failed   = {permfail}")
    _emit(f"degraded             = {degraded}")
    _emit(f"generation_attempts  = {attempts}")
    _emit(f"wall-clock           = {wall}")
    _emit("```")
    _emit()
    if lasterr:
        _emit("`last_error` (first 80 chars — the predicate's slice):")
        _emit()
        _emit("```")
        _emit(lasterr)
        _emit("```")
        _emit()


# ── Tier 2: per-node spend ───────────────────────────────────────────────────

def _tier_spend() -> None:
    _emit("## 2. Where the money + time go (verified-artifact task_runs)")
    _emit()
    path = _state_db_path()
    if not os.path.isfile(path):
        _emit(f"NOTE: state.db missing: {path} — cannot measure spend.")
        _emit()
        return
    try:
        con = sqlite3.connect(path)
    except Exception as exc:  # noqa: BLE001
        _emit(f"NOTE: cannot open state.db: {exc}")
        _emit()
        return
    # Window: bounded to the current optimization generation by the same `_epoch`
    # the predicate uses. Without it the total is the WHOLE DB history and the
    # dominant node names a chapter that was fixed generations ago.
    epoch = _budget_epoch()
    try:
        if epoch is None:
            newest = con.execute(
                "SELECT COALESCE(MAX(created_at), 0) FROM task_runs WHERE recipe = ?",
                (_RECIPE,),
            ).fetchone()[0]
            newest = int(newest or 0)
            if newest <= 0:
                con.close()
                _emit("NOTE: no verified-artifact task_runs rows — nothing to attribute.")
                _emit()
                return
            epoch = newest - 86400
            _emit(f"NOTE: budget file has no `_epoch` — falling back to the last "
                  f"24h of verified-artifact runs (created_at > {epoch}).")
            _emit()
        rows = con.execute(
            "SELECT id, COALESCE(cost_usd,0.0), COALESCE(duration_ms,0), "
            "COALESCE(status,''), COALESCE(kickoff_path,'') "
            "FROM task_runs WHERE recipe = ? AND created_at > ?",
            (_RECIPE, int(epoch)),
        ).fetchall()
    except Exception as exc:  # noqa: BLE001 — missing table etc → NOTE
        _emit(f"NOTE: spend query failed: {exc}")
        con.close()
        _emit()
        return
    con.close()

    # Attribute each run to its CALLER node. Grouping in SQL on kickoff_path is a
    # trap: it is a random staging tmpdir whose basename is always ``kickoff.md``,
    # which collapses every run into one bucket. The node identity lives in the
    # run dir's context-pack (see _node_for_run).
    per_node: dict[str, list[float]] = {}
    recovery = 0
    total_cost = 0.0
    total_dur = 0
    for run_id, cost, dur, status, kickoff_path in rows:
        label = _node_for_run(str(run_id), str(kickoff_path))
        bucket = per_node.setdefault(label, [0.0, 0.0, 0.0])
        bucket[0] += 1
        bucket[1] += float(cost)
        bucket[2] += int(dur or 0)
        total_cost += float(cost)
        total_dur += int(dur or 0)
        if status in ("failed", "rolled_back"):
            recovery += 1

    runs = len(rows)
    _emit("```")
    _emit(f"verified-artifact runs     = {runs}")
    _emit(f"total cost_usd            = {total_cost:.4f}")
    _emit(f"total duration_ms         = {total_dur}")
    _emit(f"recovery cycles (fail/roll)= {recovery}")
    _emit("```")
    _emit()

    if not per_node:
        _emit("NOTE: no verified-artifact task_runs rows in window — nothing to attribute.")
        _emit()
        return

    ranked = sorted(per_node.items(), key=lambda kv: kv[1][1], reverse=True)
    _emit("Per-node share (largest cost first):")
    _emit()
    _emit("```")
    for label, (n_runs, n_cost, n_dur) in ranked:
        _emit(f"{label[:60]:60} runs={int(n_runs):3} cost={n_cost:.4f} dur={int(n_dur)}ms")
    _emit("```")
    _emit()

    dom_label, (dom_runs, dom_cost, dom_dur) = ranked[0]
    rerolls = int(sum(max(0, int(v[0]) - 1) for _, v in ranked))
    _emit(f"**Dominant node:** `{dom_label}` — {int(dom_runs)} run(s), "
          f"{dom_cost:.4f} USD, {int(dom_dur)}ms.")
    _emit(f"**Re-roll count:** {rerolls} (runs beyond the first per node — the "
          f"cost lever; a re-rolled node is a full re-dispatch).")
    _emit()


# ── Tier 3: recovery summary ─────────────────────────────────────────────────

def _tier_recovery() -> None:
    _emit("## 3. What to attack")
    _emit()
    _emit(
        "Reduce the DOMINANT node's re-rolls, not the prompt wording. Each re-roll "
        "is a fresh `verified-artifact` dispatch and dominates the cost axis. "
        "Trace why that node re-rolls (drift, caller-schema guard, repair-signal "
        "gap) and eliminate the re-dispatch — then both cost and wall-clock fall "
        "without touching quality."
    )
    _emit()


def main(argv: list[str]) -> int:
    chapter = (argv[0].strip() if argv else "")
    if not re.fullmatch(r"\d+", chapter):
        _emit(f"NOTE: bad/absent chapter id {chapter!r}; emitting spend-only evidence.")
        chapter = ""
    book = (os.environ.get("BOOK_UUID") or "").strip()

    _emit(f"# Performance evidence for chapter {chapter or '(unknown)'} "
          f"of book {book or '(unset)'}")
    _emit()

    try:
        if chapter and re.fullmatch(r"[0-9a-fA-F-]{36}", book):
            _tier_db(chapter, book)
        else:
            _emit("## 1. Live generation-status")
            _emit()
            _emit("NOTE: chapter/BOOK_UUID unresolved — skipping DB tier.")
            _emit()
    except Exception as exc:  # noqa: BLE001 — advisory; never crash the wave
        _emit(f"NOTE: DB tier crashed: {exc}")
        _emit()

    try:
        _tier_spend()
    except Exception as exc:  # noqa: BLE001
        _emit(f"NOTE: spend tier crashed: {exc}")
        _emit()

    try:
        _tier_recovery()
    except Exception as exc:  # noqa: BLE001
        _emit(f"NOTE: recovery tier crashed: {exc}")
        _emit()

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
