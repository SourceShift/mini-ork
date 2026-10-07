"""Explain promotion decisions in plain words, and whether each applied change
is still live in a recipe prompt.

Read-only leaf module — three helpers, no writes and no page imports, so it is
unit-testable without a browser:

* :func:`explain`        — a decision + its rationale → ``{label, reason, colour, test_run}``
* :func:`live_in_prompt` — the prompt file carrying a gradient's applied marker
* :func:`decisions`      — the joined, newest-first history the IDE renders

``promotion_records`` stores only the synthesised candidate id, so ``task_class``
and ``target`` are reachable solely through ``apply_attempts`` and the proposal
text only through ``workflow_candidates`` — every table and column access is
guarded so a home missing one of them still renders.
"""
from __future__ import annotations

import calendar
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

# Rationale substrings, matched in order (first match wins — see ``explain``).
_NO_GAIN = "no strict-superset gain"
_NO_REGRESSION = "per-task no-regression"
_MOCK = "scorer=mock"
_UNVETTED = "UNVETTED promote"
_MEASURED_NOTHING = "measured nothing"

_CONTROL_N_RE = re.compile(r"control_n\s*=\s*(\d+)")
_PROBES_RE = re.compile(r"\[([^\]]+)\]")
_UTILITY_RE = re.compile(r"(-?\d+(?:\.\d+)?)\s*(?:→|->)\s*(-?\d+(?:\.\d+)?)")
_APPLIED_RE = re.compile(r"<!--\s*applied:gradient_records:([^\s>]+)\s*-->")

# Per-process memo of the applied-marker index. The repo has ~200 recipe prompt
# files and ``decisions`` renders up to ``limit`` rows, so re-globbing per row
# would read the tree dozens of times per page render. The index is derived
# purely from files on disk and is safe to share within a process.
_MARKER_INDEX: dict[str, dict[str, str]] = {}


def _verdict(label: str, reason: str, colour: str, test_run: bool) -> dict[str, Any]:
    return {"label": label, "reason": reason, "colour": colour, "test_run": bool(test_run)}


def explain(decision: Any, rationale: Any, *, task_class: Any, source_id: Any,
            repo_root: Path) -> dict[str, Any]:
    """Turn a promotion decision + its rationale into a plain-word verdict.

    Returns ``{"label", "reason", "colour", "test_run"}``. The rationale is
    matched top-to-bottom; the first hit wins, so the ``scorer=mock`` row keeps
    its ``decision == 'quarantined'`` guard (without it the UNVETTED-promote
    rows would be mislabelled).
    """
    dec = str(decision or "").strip().lower()
    text = str(rationale or "")
    test_run = str(source_id or "").startswith("gr-smoke")

    if _NO_GAIN in text:
        m = _CONTROL_N_RE.search(text)
        n = int(m.group(1)) if m else 0
        return _verdict(
            "Not better",
            f"Solved nothing the old prompt didn't also solve when simply retried (control: {n} runs)",
            "yellow", test_run)

    if _NO_REGRESSION in text:
        probes = [p.strip() for p in _PROBES_RE.findall(text) if p.strip()]
        listed = ", ".join(probes) if probes else "unknown probe"
        return _verdict(
            "Broke a task",
            f"A held-out task the old prompt solved now fails ({listed})",
            "red", test_run)

    if _MOCK in text and dec == "quarantined":
        return _verdict(
            "Score was simulated",
            "Refused: the mock scorer makes up numbers; nothing was measured",
            "muted", test_run)

    if _UNVETTED in text:
        return _verdict(
            "Applied without evaluation",
            "Promoted by operator override (MO_APPLY_UNVETTED) on a simulated score",
            "orange", test_run)

    if _MEASURED_NOTHING in text:
        recipe = str(task_class or "").replace("_", "-")
        probes_dir = Path(repo_root) / "recipes" / recipe / "probes"
        reason = (f"No probe set for {task_class} (recipes/{recipe}/probes/ missing)"
                  if not probes_dir.is_dir()
                  else "Probes did not run (launch error or budget)")
        return _verdict("Never evaluated", reason, "muted", test_run)

    if "McNemar" in text or "not significant" in text:
        return _verdict(
            "Not significant",
            "The improvement could be chance (significance test failed)",
            "yellow", test_run)

    if dec == "promoted":
        m = _UTILITY_RE.search(text)
        before, after = (float(m.group(1)), float(m.group(2))) if m else (0.0, 0.0)
        return _verdict(
            "Applied",
            f"Solved more held-out tasks: {before:.2f} → {after:.2f}",
            "green", test_run)

    label = str(decision or "").title() or "Unknown"
    return _verdict(label, text[:160], "sub", test_run)


def _marker_index(repo_root: Path) -> dict[str, str]:
    """Map ``gradient_id → repo-relative prompt path`` for every applied marker."""
    key = str(Path(repo_root))
    idx = _MARKER_INDEX.get(key)
    if idx is None:
        idx = {}
        root = Path(repo_root)
        for p in sorted(root.glob("recipes/*/prompts/*.md")):
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for m in _APPLIED_RE.finditer(text):
                idx.setdefault(m.group(1), p.relative_to(root).as_posix())
        _MARKER_INDEX[key] = idx
    return idx


def live_in_prompt(source_id: Any, repo_root: Path) -> str | None:
    """The repo-relative path of the first ``recipes/*/prompts/*.md`` carrying
    ``<!-- applied:gradient_records:<source_id> -->``, or ``None``."""
    if not source_id:
        return None
    return _marker_index(repo_root).get(str(source_id))


# ── DB access (duck-typed on the StateDB the pages pass: .has_table / .rows) ──

def _has(conn: Any, table: str) -> bool:
    try:
        return bool(conn.has_table(table))
    except Exception:  # noqa: BLE001 — a probe failure must read as "absent"
        return False


def _columns(conn: Any, table: str) -> set[str]:
    try:
        return {r["name"] for r in conn.rows(f"PRAGMA table_info({table})")}  # noqa: S608 — fixed names
    except Exception:  # noqa: BLE001
        return set()


def _iso_epoch(iso_ts: Any) -> int:
    """Parse ``promotion_records.decided_at`` (ISO-8601 text) to UTC epoch seconds.

    Live data carries a malformed tail (``2026-10-05T19:00:fZ`` — the seconds
    field is the literal ``f``), which ``datetime.fromisoformat`` rejects. Fall
    back to the ``YYYY-MM-DDTHH:MM`` prefix so the row still sorts sensibly
    instead of collapsing to 0. Never raises.
    """
    s = str(iso_ts or "").strip()
    if not s:
        return 0
    if s.endswith("Z"):
        s = s[:-1]
    try:
        return calendar.timegm(datetime.fromisoformat(s).timetuple())
    except ValueError:
        pass
    m = re.match(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2})", s)
    if m:
        try:
            return calendar.timegm(datetime.fromisoformat(m.group(1)).timetuple())
        except ValueError:
            return 0
    return 0


def _attempt_index(conn: Any) -> dict[str, dict[str, Any]]:
    """candidate_id → {task_class, target_name, source_id}, first row wins."""
    cols = _columns(conn, "apply_attempts")
    if not {"candidate_id", "task_class", "target_name", "source_id"} <= cols:
        return {}
    idx: dict[str, dict[str, Any]] = {}
    for r in conn.rows("SELECT candidate_id, task_class, target_name, source_id FROM apply_attempts"):
        c = str(r.get("candidate_id") or "")
        if c:
            idx.setdefault(c, dict(r))
    return idx


def _proposal_index(conn: Any) -> dict[str, str]:
    """candidate_id → ``new_val`` of the first mutation."""
    cols = _columns(conn, "workflow_candidates")
    if not {"candidate_id", "mutations"} <= cols:
        return {}
    idx: dict[str, str] = {}
    for r in conn.rows("SELECT candidate_id, mutations FROM workflow_candidates"):
        c = str(r.get("candidate_id") or "")
        if not c or c in idx:
            continue
        try:
            muts = json.loads(r.get("mutations") or "[]")
        except (TypeError, ValueError):
            muts = []
        if isinstance(muts, list) and muts and isinstance(muts[0], dict):
            idx[c] = str(muts[0].get("new_val") or "")
    return idx


def _gradient_index(conn: Any) -> dict[str, dict[str, Any]]:
    """gradient_id → {signal, suggested_change, target}."""
    cols = _columns(conn, "gradient_records")
    if not {"gradient_id", "signal", "suggested_change"} <= cols:
        return {}
    select = ["gradient_id", "signal", "suggested_change"]
    if "target" in cols:
        select.append("target")
    idx: dict[str, dict[str, Any]] = {}
    for r in conn.rows(f"SELECT {', '.join(select)} FROM gradient_records"):  # noqa: S608
        g = str(r.get("gradient_id") or "")
        if g:
            idx.setdefault(g, dict(r))
    return idx


def decisions(db_conn: Any, repo_root: Path, *, limit: int = 50) -> list[dict[str, Any]]:
    """The newest-first promotion history, joined and explained.

    Each row carries the promotion decision + rationale, the joined
    ``task_class`` / ``target`` / proposal text / source gradient, the
    :func:`explain` verdict fields, and ``live_path`` from
    :func:`live_in_prompt`. Tolerates a home missing any of the joined tables
    or columns.
    """
    if not _has(db_conn, "promotion_records"):
        return []
    cols = _columns(db_conn, "promotion_records")
    if not {"candidate_id", "decision", "rationale", "decided_at"} <= cols:
        return []

    select = ["candidate_id", "decision", "rationale", "decided_at"]
    select += [c for c in ("utility_before", "utility_after") if c in cols]
    rows = db_conn.rows(
        f"SELECT {', '.join(select)} FROM promotion_records "  # noqa: S608 — fixed names
        "ORDER BY decided_at DESC LIMIT ?", (int(limit),))

    attempts = _attempt_index(db_conn)
    proposals = _proposal_index(db_conn)
    gradients = _gradient_index(db_conn)

    out: list[dict[str, Any]] = []
    for r in rows:
        cand = str(r.get("candidate_id") or "")
        att = attempts.get(cand, {})
        source_id = str(att.get("source_id") or "")
        grad = gradients.get(source_id, {})
        task_class = str(att.get("task_class") or "")
        target = str(att.get("target_name") or grad.get("target") or cand)
        ex = explain(r.get("decision"), r.get("rationale"),
                     task_class=task_class, source_id=source_id, repo_root=repo_root)
        out.append({
            **ex,
            "candidate": cand,
            "task_class": task_class,
            "target": target,
            "source_id": source_id,
            "decision": str(r.get("decision") or ""),
            "decided_at": _iso_epoch(r.get("decided_at")),
            "decided_at_iso": str(r.get("decided_at") or ""),
            "utility_before": r.get("utility_before"),
            "utility_after": r.get("utility_after"),
            "rationale": str(r.get("rationale") or ""),
            "proposal": str(proposals.get(cand) or ""),
            "signal": str(grad.get("signal") or ""),
            "suggested_change": str(grad.get("suggested_change") or ""),
            "live_path": live_in_prompt(source_id, repo_root),
        })

    out.sort(key=lambda d: d["decided_at"], reverse=True)
    return out
