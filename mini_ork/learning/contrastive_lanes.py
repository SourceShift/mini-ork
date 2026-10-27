"""Contrastive lane mining (MGM) into the existing pattern → lesson path.

A *contrastive pair* is one run and one node where lane W has a ``success``
trace and a different lane L has a ``failure`` trace at the same node. Reviewer
verdict failures and moved-base failures are excluded: they describe the
artifact or the base, not the lane. Infra failures (``error``, ``timeout``,
``cost_limit``) are reported as routing evidence but never written — the lane
router already penalises them.

Quality keys that clear the independence floor are upserted into
``pattern_records`` with ``output_type='best_practice_rule'``. From there the
*existing* pipeline takes over: ``pattern_induction.induce_pending`` authors
``lesson_text``, ``reflection_suggest_promotions``/
``reflection_persist_suggestions`` propose an ``emergent_patterns`` row, and
``reflection_verify_patterns`` approves only with a lesson and independent
evidence. This miner writes neither prompt files nor ``emergent_patterns``.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
from typing import Any

from mini_ork.context import context_env

# Failures that describe the artifact or the base, not the lane. A pair built
# from one of these says nothing about which lane is better.
EXCLUDED_REASONS = frozenset({"verdict_revise", "verdict_fail", "impl_moved_base"})

# Failures that describe the plumbing, not the lane. Report-only evidence.
INFRA_REASONS = frozenset({"error", "timeout", "cost_limit"})

# A key must recur across at least this many independent runs before it can be
# written. The floor may be raised by configuration, never lowered.
MIN_RUNS_FLOOR = 3

_TRACE_RE = re.compile(r"^tr-(?P<node_type>[a-z_]+)-(?P<node_id>.+)-[0-9a-f]{8}$")
_WINDOW_RE = re.compile(r"^(\d+)([dh])$")


def enabled() -> bool:
    """Whether the MGM miner is enabled (default ON, ``MO_MGM_MINER=0`` opts out)."""
    return os.environ.get("MO_MGM_MINER", "1") != "0"


def find_pairs(
    con: sqlite3.Connection, *, since_iso: str | None = None
) -> list[dict]:
    """Return contrastive lane pairs from ``execution_traces``.

    One pair per ``(run, task_class, node_type, node_id, winner, loser)``: lane
    ``winner`` has at least one success and lane ``loser`` has at least one
    failure at that node in that run. A lane is never paired with itself.
    Malformed rows (bad JSON, non-dict verifier output, trace-id/node-type
    mismatch, empty lane, empty run) are skipped. A missing table returns ``[]``.
    """
    sql = (
        "SELECT trace_id, run_id, task_class, agent_version_id, status, "
        "verifier_output FROM execution_traces "
        "WHERE status IN ('success','failure')"
    )
    params: list[Any] = []
    if since_iso:
        sql += " AND created_at >= ?"
        params.append(since_iso)
    sql += " ORDER BY created_at, trace_id"
    try:
        rows = con.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        return []

    # group key -> {"success": {lane: [(trace_id, reason)]},
    #               "failure": {lane: [(trace_id, reason)]}}
    groups: dict[tuple, dict[str, dict[str, list[tuple[str, str]]]]] = {}
    for row in rows:
        trace_id, run_id, task_class, agent_version_id, status, vo_raw = row
        try:
            vo = json.loads(vo_raw)
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
        if not isinstance(vo, dict):
            continue
        node_type = vo.get("node_type")
        if not isinstance(node_type, str):
            continue
        match = _TRACE_RE.match(trace_id or "")
        if match is None or match.group("node_type") != node_type:
            continue
        lane = (agent_version_id or "").strip()
        if not lane:
            continue
        run = str(run_id or "").strip()
        if not run:
            continue
        finish_reason = str(vo.get("finish_reason") or "")
        if status == "failure" and finish_reason in EXCLUDED_REASONS:
            continue
        if status not in ("success", "failure"):
            continue

        node_id = match.group("node_id")
        group = groups.setdefault(
            (run, task_class, node_type, node_id),
            {"success": {}, "failure": {}},
        )
        side = "success" if status == "success" else "failure"
        group[side].setdefault(lane, []).append((trace_id, finish_reason))

    pairs: list[dict] = []
    for (run, task_class, node_type, node_id), group in groups.items():
        for winner, success_recs in group["success"].items():
            for loser, failure_recs in group["failure"].items():
                if loser == winner:
                    continue
                loser_reasons = sorted({reason for _, reason in failure_recs})
                kind = (
                    "quality"
                    if any(r and r not in INFRA_REASONS for r in loser_reasons)
                    else "infra"
                )
                pairs.append(
                    {
                        "run_id": run,
                        "task_class": task_class,
                        "node_type": node_type,
                        "node_id": node_id,
                        "winner": winner,
                        "loser": loser,
                        "winner_trace_ids": sorted(t for t, _ in success_recs),
                        "loser_trace_ids": sorted(t for t, _ in failure_recs),
                        "loser_reasons": loser_reasons,
                        "kind": kind,
                    }
                )

    pairs.sort(
        key=lambda p: (
            p["task_class"],
            p["node_type"],
            p["node_id"],
            p["winner"],
            p["loser"],
            p["run_id"],
        )
    )
    return pairs


def summarize(pairs: list[dict]) -> list[dict]:
    """Collapse pairs into one key per ``(task_class, node_type, node_id, winner, loser)``."""
    grouped: dict[tuple, list[dict]] = {}
    for pair in pairs:
        key = (
            pair["task_class"],
            pair["node_type"],
            pair["node_id"],
            pair["winner"],
            pair["loser"],
        )
        grouped.setdefault(key, []).append(pair)

    keys: list[dict] = []
    for key, key_pairs in grouped.items():
        task_class, node_type, node_id, winner, loser = key
        run_ids = sorted({p["run_id"] for p in key_pairs})
        kind = (
            "quality"
            if any(p["kind"] == "quality" for p in key_pairs)
            else "infra"
        )
        reasons = sorted({r for p in key_pairs for r in p["loser_reasons"]})
        by_run = {p["run_id"]: p for p in key_pairs}
        trace_ids: list[str] = []
        for run in run_ids:
            pair = by_run[run]
            trace_ids.extend(pair["winner_trace_ids"])
            trace_ids.extend(pair["loser_trace_ids"])
        trace_ids = list(dict.fromkeys(trace_ids))
        keys.append(
            {
                "task_class": task_class,
                "node_type": node_type,
                "node_id": node_id,
                "winner": winner,
                "loser": loser,
                "run_ids": run_ids,
                "n_runs": len(run_ids),
                "kind": kind,
                "reasons": reasons,
                "trace_ids": trace_ids,
            }
        )

    keys.sort(
        key=lambda k: (
            k["task_class"],
            k["node_type"],
            k["node_id"],
            k["winner"],
            k["loser"],
        )
    )
    return keys


def pattern_id_for(
    task_class: str, node_type: str, node_id: str, winner: str, loser: str
) -> str:
    """Stable ``pattern_records.pattern_id`` for a contrastive key."""
    digest = hashlib.sha256(
        f"{task_class}|{node_type}|{node_id}|{winner}|{loser}".encode()
    ).hexdigest()[:12]
    return "pat-mgm-" + digest


def describe(key: dict) -> str:
    """Human-readable cluster label for a contrastive key (truncated to 480).

    Starts with ``cluster: task_class=<tc> `` so
    ``context_assembler.pattern_task_class`` scopes on it, and never contains
    ``status=failure``/``status=vacuous`` so the watchdog's failing-pattern
    matcher cannot trigger on it.
    """
    task_class = key["task_class"]
    node_type = key["node_type"]
    node_id = key["node_id"]
    winner = key["winner"]
    loser = key["loser"]
    n_runs = key["n_runs"]
    reasons = key["reasons"]
    return (
        f"cluster: task_class={task_class} contrastive node={node_type}/{node_id} "
        f"winner={winner} loser={loser} (runs={n_runs}): in each run the "
        f"{winner} attempt succeeded where the {loser} attempt at the same node "
        f"failed ({', '.join(r or 'unknown' for r in reasons)}); the lesson is "
        f"what the succeeding attempt did that the failing one did not"
    )[:480]


def _window_to_since(window: str | None) -> str | None:
    """Convert a ``Nd``/``Nh`` window into an ISO cutoff; anything else = all history."""
    if not window:
        return None
    match = _WINDOW_RE.match(str(window))
    if match is None:
        return None
    n = int(match.group(1))
    seconds = n * (86400 if match.group(2) == "d" else 3600)
    return time.strftime(
        "%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(int(time.time()) - seconds)
    )


def _upsert(con: sqlite3.Connection, key: dict) -> None:
    """Write the quality key. Never touches lesson_text/status/output_type/promoted_to."""
    pid = pattern_id_for(
        key["task_class"], key["node_type"], key["node_id"],
        key["winner"], key["loser"],
    )
    description = describe(key)
    trace_ids = key["trace_ids"]
    row = con.execute(
        "SELECT evidence_trace_ids FROM pattern_records WHERE pattern_id=?",
        (pid,),
    ).fetchone()
    if row is not None:
        try:
            old = json.loads(row[0]) if row[0] else []
        except (json.JSONDecodeError, TypeError, ValueError):
            old = []
        if not isinstance(old, list):
            old = []
        evidence = list(dict.fromkeys(old + trace_ids))[:200]
        con.execute(
            "UPDATE pattern_records SET frequency=?, evidence_trace_ids=?, "
            "description=?, "
            "last_seen=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE pattern_id=?",
            (key["n_runs"], json.dumps(evidence), description, pid),
        )
    else:
        con.execute(
            "INSERT INTO pattern_records (pattern_id, description, "
            "evidence_trace_ids, frequency, first_seen, last_seen, output_type) "
            "VALUES (?,?,?,?,strftime('%Y-%m-%dT%H:%M:%fZ','now'),"
            "strftime('%Y-%m-%dT%H:%M:%fZ','now'),'best_practice_rule')",
            (pid, description, json.dumps(trace_ids[:200]), key["n_runs"]),
        )


def mine_into_patterns(
    *,
    db_path: str | None = None,
    window: str | None = None,
    min_runs: int | str = 3,
) -> dict:
    """Mine contrastive pairs into ``pattern_records``. Never raises.

    Infra keys go to ``report["routing"]`` only. Quality keys below the
    independence floor are held. Quality keys at or above it are upserted.
    """
    report: dict[str, Any] = {
        "enabled": enabled(),
        "pairs": 0,
        "keys": 0,
        "quality_keys": 0,
        "written": 0,
        "held": 0,
        "routing": [],
    }
    if not report["enabled"]:
        return report

    db = db_path or context_env("MINI_ORK_DB", "") or os.path.join(
        context_env("MINI_ORK_HOME", "")
        or os.path.join(os.getcwd(), ".mini-ork"),
        "state.db",
    )
    if not os.path.isfile(db):
        return report

    try:
        floor = max(MIN_RUNS_FLOOR, int(min_runs))
    except (TypeError, ValueError):
        floor = MIN_RUNS_FLOOR

    since_iso = _window_to_since(window)

    con: sqlite3.Connection | None = None
    try:
        con = sqlite3.connect(db)
        con.execute("PRAGMA busy_timeout=5000")
        pairs = find_pairs(con, since_iso=since_iso)
        keys = summarize(pairs)
        report["pairs"] = len(pairs)
        report["keys"] = len(keys)
        report["quality_keys"] = sum(
            1 for key in keys if key["kind"] == "quality"
        )
        for key in keys:
            if key["kind"] == "infra":
                report["routing"].append(
                    {
                        "task_class": key["task_class"],
                        "node_type": key["node_type"],
                        "node_id": key["node_id"],
                        "winner": key["winner"],
                        "loser": key["loser"],
                        "n_runs": key["n_runs"],
                        "reasons": key["reasons"],
                    }
                )
                continue
            if key["n_runs"] < floor:
                report["held"] += 1
                continue
            _upsert(con, key)
            report["written"] += 1
        con.commit()
    except sqlite3.Error as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
        return report
    finally:
        if con is not None:
            con.close()
    return report
