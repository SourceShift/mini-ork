#!/usr/bin/env bash
# bug_report.sh — per-agent bug reporting channel and orchestrator queue.
#
# Each agent CLI dispatch can append a JSONL row to
# ${MINI_ORK_RUN_DIR}/noticed_bugs.jsonl describing an observed-but-deferred
# issue. The reflect stage sweeps every run dir, dedupes, ranks, and (when
# the threshold is crossed) promotes top-N into new epics via the existing
# epics ingest path.
#
# JSONL row shape (one per line):
#   {"agent_role": "reviewer",
#    "title": "Rubric pass=true with score=6 despite two FAIL items",
#    "description": "longer prose ...",
#    "suggested_fix": "Short-circuit pass=false on any critical FAIL.",
#    "severity": "high",
#    "confidence": 0.88,
#    "observed_in": "lib/verifier-rubric.sh"}
#
# Public API:
#   bug_report_emit       <agent_role> <severity> <title> <description>
#                         <suggested_fix> [observed_in] [confidence]
#       Append a row to the current run dir's noticed_bugs.jsonl.
#
#   bug_report_sweep      [--since EPOCH] [--all]
#       Walk .mini-ork/runs/*/noticed_bugs.jsonl, upsert into bug_reports
#       table (dedupe by sha256(normalized title)).
#
#   bug_report_prioritize [--top N]
#       Print ranked open bugs (severity_weight * frequency * confidence).
#
#   bug_report_promote    --top N
#       Take top-N ranked open bugs, create epic rows + kickoff files,
#       and flip bug_reports.status to 'queued_as_epic'.
#
#   bug_report_list / bug_report_show <id>

[ "${0:-}" = "${BASH_SOURCE[0]:-}" ] && set -Eeuo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
STATE_DB="${MINI_ORK_DB:-${MINI_ORK_HOME:-.mini-ork}/state.db}"

_bug_report_now() { date +%s; }

# Severity → numeric weight for ranking. Higher weight = more urgent.
_bug_report_severity_weight() {
  case "$1" in
    critical) echo 8 ;;
    high)     echo 4 ;;
    medium)   echo 2 ;;
    low)      echo 1 ;;
    *)        echo 1 ;;
  esac
}

# bug_report_emit <agent_role> <severity> <title> <description>
#                 [suggested_fix] [observed_in] [confidence]
# Writes a JSONL row to ${MINI_ORK_RUN_DIR}/noticed_bugs.jsonl.
# Best-effort: if MINI_ORK_RUN_DIR is unset, falls back to /tmp.
bug_report_emit() {
  local agent_role="${1:?agent_role required}"
  local severity="${2:-medium}"
  local title="${3:?title required}"
  local description="${4:-}"
  local suggested_fix="${5:-}"
  local observed_in="${6:-general}"
  local confidence="${7:-0.5}"
  case "$severity" in
    low|medium|high|critical) : ;;
    *) severity="medium" ;;
  esac
  local run_dir="${MINI_ORK_RUN_DIR:-/tmp}"
  local sink="${run_dir}/noticed_bugs.jsonl"
  mkdir -p "$run_dir" 2>/dev/null || true
  python3 - "$sink" "$agent_role" "$severity" "$title" "$description" \
                   "$suggested_fix" "$observed_in" "$confidence" <<'PY' || return 0
import json, sys
sink, role, sev, title, desc, fix, obs_in, conf = sys.argv[1:9]
row = {
    "agent_role": role,
    "severity": sev,
    "title": title.strip()[:300],
    "description": desc[:4000],
    "suggested_fix": fix[:2000],
    "observed_in": obs_in[:200],
}
try:
    row["confidence"] = float(conf)
except ValueError:
    row["confidence"] = 0.5
with open(sink, "a", encoding="utf-8") as f:
    f.write(json.dumps(row, separators=(",", ":")) + "\n")
PY
}

# Normalize a title for dedupe fingerprint.
_bug_report_fingerprint() {
  python3 - "$1" <<'PY'
import hashlib, re, sys
s = re.sub(r"\s+", " ", sys.argv[1].lower().strip())
s = re.sub(r"[`\"'\(\)\[\]\{\},.;:!?]", "", s)
print(hashlib.sha256(s.encode()).hexdigest())
PY
}

# bug_report_sweep [--since EPOCH] [--all]
# Walks all run dirs, finds noticed_bugs.jsonl, upserts into the table.
bug_report_sweep() {
  local since=0 all=0
  while [ $# -gt 0 ]; do
    case "$1" in
      --since) since="$2"; shift 2 ;;
      --all)   all=1; shift ;;
      *) shift ;;
    esac
  done
  local home="${MINI_ORK_HOME:-.mini-ork}"
  python3 - "$STATE_DB" "$home" "$since" "$all" <<'PY'
import json, os, sqlite3, sys, time, hashlib, re
from pathlib import Path

db, home, since_str, all_str = sys.argv[1:5]
since = int(since_str)
sweep_all = all_str == "1"
runs_dir = Path(home) / "runs"
if not runs_dir.is_dir():
    print(0)
    sys.exit(0)

con = sqlite3.connect(db)
con.execute("PRAGMA busy_timeout=5000")
now = int(time.time())

def fingerprint(title: str) -> str:
    s = re.sub(r"\s+", " ", title.lower().strip())
    s = re.sub(r"[`\"'\(\)\[\]\{\},.;:!?]", "", s)
    return hashlib.sha256(s.encode()).hexdigest()

swept = 0
for run_path in runs_dir.iterdir():
    if not run_path.is_dir():
        continue
    sink = run_path / "noticed_bugs.jsonl"
    if not sink.is_file():
        continue
    if not sweep_all and sink.stat().st_mtime < since:
        continue
    run_id = run_path.name
    try:
        lines = sink.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        continue
    for raw in lines:
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
        if sev not in {"low", "medium", "high", "critical"}:
            sev = "medium"
        try:
            conf = float(row.get("confidence", 0.5))
        except (TypeError, ValueError):
            conf = 0.5
        fp = fingerprint(title)
        existing = con.execute(
            "SELECT id, frequency, severity, confidence FROM bug_reports WHERE fingerprint=?",
            (fp,),
        ).fetchone()
        if existing:
            bid, freq, old_sev, old_conf = existing
            # Keep max severity + max confidence on repeat sightings.
            sev_rank = {"low": 1, "medium": 2, "high": 3, "critical": 4}
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
con.close()
print(swept)
PY
}

bug_report_prioritize() {
  local top=10
  while [ $# -gt 0 ]; do
    case "$1" in
      --top) top="$2"; shift 2 ;;
      *) shift ;;
    esac
  done
  sqlite3 -separator ' | ' "$STATE_DB" \
    "SELECT printf('%-4d', id),
            printf('%-8s', severity),
            printf('%3d', frequency),
            printf('%.2f', confidence),
            printf('%-15s', agent_role),
            substr(title,1,80)
       FROM bug_reports
      WHERE status='open'
      ORDER BY
        CASE severity WHEN 'critical' THEN 8
                      WHEN 'high'     THEN 4
                      WHEN 'medium'   THEN 2
                      ELSE 1 END * frequency * confidence DESC
      LIMIT $top;"
}

bug_report_list() {
  sqlite3 -separator ' | ' "$STATE_DB" \
    "SELECT printf('%-4d', id),
            printf('%-15s', status),
            printf('%-8s', severity),
            printf('%-15s', agent_role),
            substr(title,1,80)
       FROM bug_reports
      ORDER BY id DESC LIMIT 50;"
}

bug_report_show() {
  local bid="${1:?id required}"
  sqlite3 -line "$STATE_DB" "SELECT * FROM bug_reports WHERE id=$bid;"
}

# bug_report_promote --top N
# Takes the top-N ranked open bugs, creates a new epic row per bug, writes
# a per-epic kickoff under kickoffs/auto/bug-<id>.md, and flips bug_reports
# status to 'queued_as_epic'.
bug_report_promote() {
  local top=3
  while [ $# -gt 0 ]; do
    case "$1" in
      --top) top="$2"; shift 2 ;;
      *) shift ;;
    esac
  done
  python3 - "$STATE_DB" "$top" "$MINI_ORK_ROOT" <<'PY'
import sqlite3, sys, re
from pathlib import Path

db, top_str, repo_root = sys.argv[1:4]
top = int(top_str)
repo_root = Path(repo_root)
out_dir = repo_root / "kickoffs" / "auto"
out_dir.mkdir(parents=True, exist_ok=True)

con = sqlite3.connect(db)
con.execute("PRAGMA busy_timeout=5000")

rows = con.execute("""
    SELECT id, fingerprint, agent_role, task_class, observed_in,
           title, description, suggested_fix, severity, confidence, frequency
      FROM bug_reports
     WHERE status='open'
     ORDER BY
       CASE severity WHEN 'critical' THEN 8
                     WHEN 'high'     THEN 4
                     WHEN 'medium'   THEN 2
                     ELSE 1 END * frequency * confidence DESC
     LIMIT ?
""", (top,)).fetchall()

promoted = 0
for (bid, fp, role, tc, obs_in, title, desc, fix, sev, conf, freq) in rows:
    # Build a stable epic id.
    slug_seed = re.sub(r"[^a-z0-9-]+", "-", title.lower().strip()).strip("-")[:60]
    if not slug_seed:
        slug_seed = "bug"
    epic_id = f"bug-{bid}-{slug_seed}"
    # Skip if epic already exists (idempotence on re-promote).
    exists = con.execute("SELECT id FROM epics WHERE id=?", (epic_id,)).fetchone()
    if exists:
        continue
    kickoff_rel = f"kickoffs/auto/{epic_id}.md"
    kickoff_abs = repo_root / kickoff_rel

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

    # Insert epic with status='not started' (no deps yet — operator can add).
    epic_title = f"BUG-{bid}: {title[:140]}"
    con.execute(
        "INSERT INTO epics(id, title, status, kickoff_path) VALUES(?,?,'not started',?)",
        (epic_id, epic_title, kickoff_rel),
    )
    con.execute(
        "UPDATE bug_reports SET status='queued_as_epic', promoted_to_epic_id=?, updated_at=strftime('%s','now') WHERE id=?",
        (epic_id, bid),
    )
    promoted += 1

con.commit()
con.close()
print(promoted)
PY
}

if [[ "${BASH_SOURCE[0]:-}" == "${0:-}" ]]; then
  echo "bug_report.sh — source me and call bug_report_emit / bug_report_sweep / bug_report_prioritize / bug_report_promote / bug_report_list / bug_report_show"
fi
