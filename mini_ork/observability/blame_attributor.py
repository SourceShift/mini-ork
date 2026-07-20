"""Python port of lib/blame_attributor.sh — git-blame-based penalty attribution.

When a later run finds a defect, blame the offending lines back to the run+lane
that authored them and apportion a signed penalty across the contributing groups
by line-count share. Strangler-fig parity port: git shells out; the awk/inline-
python logic (porcelain parse, trailer grouping, judge, apportionment) is ported
verbatim in behaviour.

    attribute(defect_json_path, *, db, judge_cmd="", found_run_id, repo_root,
              dry_run=False, trailer_run=..., trailer_lane=...) -> list[row]

Returns the attribution rows (also inserted into defect_attributions unless
dry_run). The default judge is deterministic (severity → fixed penalty + a small
length perturbation) so attribution is testable offline.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess


def normalize_severity(s: str | None) -> str:
    # Mirror the bash quirk: `case "${1:-medium}"` matches on the default-
    # substituted value, but `printf '%s' "$1"` echoes the ORIGINAL arg — so an
    # empty/unset severity matches the medium branch yet prints "" (not "medium").
    s = "" if s is None else s
    key = s or "medium"
    return s if key in ("low", "medium", "high", "critical") else "medium"


def validate_penalty(raw: str) -> str:
    """Return canonical repr of a penalty in [-1, 0]; raise ValueError otherwise."""
    if raw == "" or any(c not in "0123456789.+-" for c in raw):
        raise ValueError(f"penalty not numeric: {raw}")
    if len(raw) >= 2 and raw[0] in "+-" and raw[1] in "+-":
        raise ValueError(f"malformed penalty: {raw}")
    try:
        v = float(raw)
    except (TypeError, ValueError):
        raise ValueError(f"penalty not numeric: {raw}")
    if v < -1.0 or v > 0.0:
        raise ValueError(f"penalty out of [-1, 0]: {raw}")
    return repr(v)


def blame_lines(repo_root: str, file: str, start, end) -> list[tuple[str, str]]:
    if not os.path.isdir(repo_root):
        raise FileNotFoundError(f"repo not found: {repo_root}")
    if not os.path.isfile(os.path.join(repo_root, file)):
        raise FileNotFoundError(f"file not found: {repo_root}/{file}")
    out = subprocess.run(
        ["git", "-C", repo_root, "blame", "--line-porcelain", "-L", f"{start},{end}", "--", file],
        capture_output=True, text=True).stdout
    records: list[tuple[str, str]] = []
    sha = ""
    email = ""
    for line in out.splitlines():
        parts = line.split()
        if (parts and len(parts[0]) >= 7 and all(c in "0123456789abcdef" for c in parts[0])
                and len(parts) >= 2 and parts[1].isdigit()):
            sha = parts[0]
            email = ""
        elif line.startswith("author-mail "):
            email = line.split(None, 1)[1].lstrip("<").rstrip(">")
        elif line.startswith("\t"):
            if sha:
                records.append((sha, email))
    return records


def _trailer(repo_root, key, sha) -> str:
    out = subprocess.run(
        ["git", "-C", repo_root, "log", "-1", f"--format=%(trailers:key={key},valueonly)", sha],
        capture_output=True, text=True).stdout
    return out.strip()


def group_by_run(repo_root, records, trailer_run, trailer_lane) -> list[dict]:
    """Group blame records by (run_id, lane) in the bash sort order."""
    resolved = {}
    for sha in sorted({r[0] for r in records}):
        rid = _trailer(repo_root, trailer_run, sha) or f"sha:{sha}"
        lane = _trailer(repo_root, trailer_lane, sha)
        if not lane:
            lane = next((email for s, email in records if s == sha), "")
        resolved[sha] = (rid, lane)
    pairs = []
    for sha, _email in records:
        rid, lane = resolved.get(sha, (f"sha:{sha}", ""))
        if not lane:
            lane = "unknown"
        pairs.append(f"{rid}\t{lane}")
    pairs.sort()
    groups = []
    prev = None
    count = 0
    for p in pairs:
        if p == prev:
            count += 1
        else:
            if prev is not None:
                rid, lane = prev.split("\t", 1)
                groups.append({"run_id": rid, "lane": lane, "line_count": count})
            prev = p
            count = 1
    if prev is not None:
        rid, lane = prev.split("\t", 1)
        groups.append({"run_id": rid, "lane": lane, "line_count": count})
    return groups


def default_judge(defect: dict) -> str:
    sev = (defect.get("severity") or "medium").lower()
    base = {"low": -0.1, "medium": -0.4, "high": -0.7, "critical": -0.95}.get(sev, -0.4)
    rep = defect.get("defect_report") or ""
    adj = min(0.05, max(-0.05, (len(rep) % 17) / 200.0 - 0.04))
    p = max(-1.0, min(0.0, base + adj))
    return f"{p:.4f}"


def call_judge(defect_json_path, diff_path, judge_cmd="") -> str:
    cmd = judge_cmd or os.environ.get("MO_BLAME_JUDGE_CMD", "")
    if cmd:
        out = subprocess.run([cmd, defect_json_path, diff_path], capture_output=True, text=True).stdout
        raw = out.strip().splitlines()[-1] if out.strip() else ""
    else:
        with open(defect_json_path, encoding="utf-8") as f:
            raw = default_judge(json.load(f))
    return validate_penalty(raw)


def apportion(total_penalty, groups) -> list[dict]:
    total = float(total_penalty)
    total_lines = sum(g["line_count"] for g in groups) or 1
    running = 0.0
    n = len(groups)
    out = []
    for i, g in enumerate(groups):
        share = g["line_count"] / total_lines
        if i == n - 1:
            p = total - running
        else:
            p = round(total * share, 6)
            running += p
        row = dict(g)
        row["penalty"] = round(p, 6)
        out.append(row)
    return out


_REQUIRED = ("found_run_id", "blamed_run_id", "lane", "code_region",
             "task_class", "severity", "penalty", "decay_halflife_days")


def insert_attribution(db, row) -> None:
    missing = [k for k in _REQUIRED if k not in row]
    if missing:
        raise ValueError(f"missing keys: {missing}")
    con = sqlite3.connect(db)
    con.execute("PRAGMA busy_timeout=5000")
    con.execute(
        """INSERT INTO defect_attributions
             (found_run_id, blamed_run_id, lane, code_region, task_class,
              severity, penalty, decay_halflife_days)
           VALUES (?,?,?,?,?,?,?,?)""",
        (row["found_run_id"], row["blamed_run_id"], row["lane"], row["code_region"],
         row["task_class"], row["severity"], float(row["penalty"]),
         float(row.get("decay_halflife_days", 30.0))))
    con.commit()
    con.close()


def attribute(defect_json_path: str, *, db: str, judge_cmd: str = "", found_run_id: str,
              repo_root: str, dry_run: bool = False,
              trailer_run: str | None = None, trailer_lane: str | None = None) -> list[dict]:
    if not found_run_id:
        raise ValueError("--found-run-id or MINI_ORK_RUN_ID required")
    if not os.path.isfile(defect_json_path):
        raise FileNotFoundError(f"defect json not found: {defect_json_path}")
    trailer_run = trailer_run or os.environ.get("MO_BLAME_TRAILER_RUN", "Mini-ork-run-id")
    trailer_lane = trailer_lane or os.environ.get("MO_BLAME_TRAILER_LANE", "Mini-ork-lane")

    with open(defect_json_path, encoding="utf-8") as f:
        d = json.load(f)
    file = d.get("file", "")
    ranges = d.get("ranges") or []
    severity = normalize_severity(d.get("severity") or "medium")
    task_class = d.get("task_class") or "unknown"
    code_region = d.get("code_region") or "unknown"
    if not file:
        raise ValueError("defect.file required")

    records: list[tuple[str, str]] = []
    range_count = 0
    for r in ranges:
        if not (isinstance(r, (list, tuple)) and len(r) == 2):
            continue
        records.extend(blame_lines(repo_root, file, r[0], r[1]))
        range_count += 1
    if range_count == 0 or not records:
        raise ValueError("no blameable lines (empty ranges or file gone)")

    groups = group_by_run(repo_root, records, trailer_run, trailer_lane)
    if not groups:
        raise ValueError("grouping produced no rows")

    # judge needs a diff file path (default judge ignores it)
    penalty = call_judge(defect_json_path, os.devnull, judge_cmd)
    apportioned = apportion(penalty, groups)

    rows = []
    for g in apportioned:
        row = {
            "found_run_id": found_run_id,
            "blamed_run_id": g["run_id"],
            "lane": g["lane"],
            "code_region": code_region,
            "task_class": task_class,
            "severity": severity,
            "penalty": g["penalty"],
            "decay_halflife_days": float(os.environ.get("DECAY", "30.0")),
            "_line_count": g["line_count"],
        }
        if not dry_run:
            insert_attribution(db, row)
        rows.append(row)
    return rows
