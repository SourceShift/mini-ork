"""Per-run review findings ledger (kickoff rev6-r5a: review convergence).

The ledger is the durable memory of the review loop. A classic reviewer's
findings are recorded under stable ids (``F1``, ``F2``, ...) so that:

* the next reviewer prompt can say which findings are still open and which
  were fixed (``prior_block``),
* the next implementer round receives structured findings, including ones the
  current review did not repeat (``feedback_lines``),
* a regression — a new finding that matches a fixed one after normalization —
  can be flagged (``reopened``).

It lives at ``<run_dir>/revise/findings.json`` and is stdlib-only: the executor
and the reviewer handler both import it, and it must never need git or a model.
"""
from __future__ import annotations

import json
import os
import re
import time

LEDGER_REL = "revise/findings.json"

_SEVERITIES = ("high", "medium", "low")
_STILL_OPEN = (
    " — STILL OPEN after {n} reviews: the earlier fix did not work; "
    "change the approach for this finding instead of patching the same lines "
    "again."
)


def _ledger_path(run_dir: str) -> str:
    return os.path.join(run_dir, LEDGER_REL)


def _empty() -> dict:
    return {"findings": [], "rounds": []}


def attempt_no(run_dir: str) -> int:
    """The review attempt this run is on: ``revise/current.json`` round + 1.

    ``current.json`` is written by the revise loop *after* a reviewer round, so
    while the next reviewer runs it still holds the previous round. A missing
    or unparseable file means the first review.
    """
    try:
        with open(os.path.join(run_dir, "revise", "current.json"),
                  encoding="utf-8") as handle:
            data = json.load(handle)
        return int(data.get("round")) + 1
    except (OSError, TypeError, ValueError, AttributeError):
        return 1


def load(run_dir: str) -> dict:
    """Read the ledger; a missing, unreadable or corrupt file reads as empty."""
    try:
        with open(_ledger_path(run_dir), encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return _empty()
    if not isinstance(data, dict):
        return _empty()
    findings = data.get("findings")
    rounds = data.get("rounds")
    return {
        "findings": findings if isinstance(findings, list) else [],
        "rounds": rounds if isinstance(rounds, list) else [],
    }


def _normalize_issue(text) -> str:
    """Lowercase and collapse runs of non-alphanumerics to one space."""
    return re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).strip()


def _normalize_severity(value) -> str:
    severity = str(value or "").strip().lower()
    return severity if severity in _SEVERITIES else "medium"


def _normalize_instances(value) -> list[dict]:
    if not isinstance(value, list):
        return []
    instances = []
    for item in value:
        if isinstance(item, dict):
            instances.append({"file": item.get("file", ""),
                              "line": item.get("line", 0)})
    return instances


def _write(run_dir: str, ledger: dict) -> None:
    """Write the ledger atomically (tmp + ``os.replace``)."""
    path = _ledger_path(run_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(ledger, handle, indent=2)
        handle.write("\n")
    os.replace(tmp, path)


def record_review(run_dir: str, attempt: int, review: dict,
                  delta_files: list[str] | None = None,
                  now: int | None = None) -> dict:
    """Fold one review into the ledger and return it.

    Findings re-reported with an existing id keep that id and are refreshed;
    everything else gets the next ``F<k>``. ``resolved`` ids that name an open
    finding not re-reported in this review are marked fixed. New findings that
    match a fixed one are flagged ``reopened``; new findings outside
    ``delta_files`` are flagged ``outside_delta``. The review's own findings are
    rewritten in place so the caller can persist the assigned ids.
    """
    ledger = load(run_dir)
    findings = ledger["findings"]
    rounds = ledger["rounds"]
    by_id = {f.get("id"): f for f in findings
             if isinstance(f, dict) and f.get("id")}

    if isinstance(review, dict):
        raw_findings = review.get("findings")
        raw_resolved = review.get("resolved")
    else:
        raw_findings, raw_resolved = None, None
    if not isinstance(raw_findings, list):
        raw_findings = []
    if not isinstance(raw_resolved, list):
        raw_resolved = []
    resolved_ids = {item for item in raw_resolved if isinstance(item, str)}

    fixed_by_key = {
        (f.get("file"), _normalize_issue(f.get("issue"))): f
        for f in findings
        if isinstance(f, dict) and f.get("status") == "fixed"
    }

    new_count = 0
    reported_again = 0
    outside_delta = 0
    reopened = 0
    reported_ids: set[str] = set()

    for raw in raw_findings:
        if not isinstance(raw, dict):
            continue
        issue = str(raw.get("issue") or "")[:400]
        severity = _normalize_severity(raw.get("severity"))
        line = raw.get("line", 0)
        instances = _normalize_instances(raw.get("instances"))
        raw_id = raw.get("id")
        finding_id = raw_id.strip() if isinstance(raw_id, str) else ""

        if finding_id and finding_id in by_id:
            entry = by_id[finding_id]
            entry["last_seen"] = attempt
            entry["severity"] = severity
            entry["line"] = line
            entry["instances"] = instances
            entry["status"] = "open"
            entry.pop("fixed_in", None)
            reported_ids.add(finding_id)
            reported_again += 1
            continue

        entry = {
            "id": f"F{len(findings) + 1}",
            "status": "open",
            "file": str(raw.get("file") or ""),
            "line": line,
            "severity": severity,
            "issue": issue,
            "instances": instances,
            "first_seen": attempt,
            "last_seen": attempt,
        }
        if (entry["file"], _normalize_issue(issue)) in fixed_by_key:
            entry["reopened"] = True
            reopened += 1
        if delta_files is not None and entry["file"] not in delta_files:
            entry["outside_delta"] = True
            outside_delta += 1
        findings.append(entry)
        by_id[entry["id"]] = entry
        raw["id"] = entry["id"]
        reported_ids.add(entry["id"])
        new_count += 1

    fixed_count = 0
    for finding_id in resolved_ids:
        entry = by_id.get(finding_id)
        if entry is None or entry.get("status") != "open":
            continue
        if finding_id in reported_ids:
            # A finding the reviewer resolved and re-reported is still open.
            continue
        entry["status"] = "fixed"
        entry["fixed_in"] = attempt
        fixed_count += 1

    rounds.append({
        "attempt": attempt,
        "verdict": str(review.get("verdict", "")) if isinstance(review, dict) else "",
        "new": new_count,
        "reported_again": reported_again,
        "fixed": fixed_count,
        "still_open": sum(1 for f in findings
                          if isinstance(f, dict) and f.get("status") == "open"),
        "outside_delta": outside_delta,
        "reopened": reopened,
        "at": int(now if now is not None else time.time()),
    })
    _write(run_dir, ledger)
    return ledger


def _finding_line(finding: dict) -> str:
    return (f"- [{finding.get('id', '?')}] {finding.get('severity', 'medium')} "
            f"{finding.get('file', '')}:{finding.get('line', 0)} — "
            f"{finding.get('issue', '')}")


def prior_block(run_dir: str) -> str:
    """The reviewer's memory of earlier findings, or ``""`` when there are none.

    Open findings come first (the reviewer must judge them by id), then the
    fixed ones the reviewer must not re-report. Capped at 40 lines.
    """
    ledger = load(run_dir)
    findings = [f for f in ledger["findings"] if isinstance(f, dict)]
    if not findings:
        return ""
    open_findings = [f for f in findings if f.get("status") == "open"]
    fixed_findings = [f for f in findings if f.get("status") == "fixed"]
    lines = ["## Your earlier findings (ids are stable across rounds)"]
    lines.extend(_finding_line(f) for f in open_findings)
    if fixed_findings:
        lines.append("Already fixed (do not report again unless the new changes broke it):")
        lines.extend(_finding_line(f) for f in fixed_findings)
    return "\n".join(lines[:40])


def feedback_lines(run_dir: str, attempt: int) -> list[str]:
    """One line per open finding, for the implementer's revise feedback.

    ``attempt`` is the review attempt number; a finding seen in two or more
    reviews carries the STILL OPEN warning so the implementer changes approach
    instead of re-patching the same lines.
    """
    ledger = load(run_dir)
    lines = []
    for finding in ledger["findings"]:
        if not isinstance(finding, dict) or finding.get("status") != "open":
            continue
        line = _finding_line(finding)
        instances = finding.get("instances")
        if isinstance(instances, list) and instances:
            spots = [f"{item.get('file', '')}:{item.get('line', 0)}"
                     for item in instances if isinstance(item, dict)]
            if spots:
                line += f" (also at {', '.join(spots)})"
        try:
            first = int(finding.get("first_seen", attempt))
            last = int(finding.get("last_seen", attempt))
        except (TypeError, ValueError):
            first = last = attempt
        if last - first >= 1:
            line += _STILL_OPEN.format(n=last - first + 1)
        lines.append(line)
    return lines
