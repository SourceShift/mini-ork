#!/usr/bin/env bash
# Aggregate child recursive-validate-impl verdicts into aggregate-verdict.json.

set -uo pipefail

RUN_DIR="${MINI_ORK_RUN_DIR:-$PWD}"
NAME="per-feature-dispatch-results"
FEATURE_INDEX="$RUN_DIR/feature-index.json"
CHILD_DIR="$RUN_DIR/child-runs"
AGGREGATE="$RUN_DIR/aggregate-verdict.json"
EVIDENCE="$RUN_DIR/verifier-$NAME.log"
CHECKS_TSV="$RUN_DIR/verifier-$NAME.checks.tsv"

: >"$CHECKS_TSV"
exec 3>"$EVIDENCE"

python3 - "$FEATURE_INDEX" "$CHILD_DIR" "$AGGREGATE" >&3 <<'PY'
import glob, json, os, sys
feature_index, child_dir, aggregate_path = sys.argv[1:4]

features = []
if os.path.exists(feature_index):
    data = json.load(open(feature_index, encoding="utf-8"))
    raw = data.get("features", data if isinstance(data, list) else [])
    features = [f for f in raw if f.get("priority") == "P0"]

records = {}
for path in sorted(glob.glob(os.path.join(child_dir, "*.json"))):
    try:
        rec = json.load(open(path, encoding="utf-8"))
    except Exception as exc:
        rec = {"feature_id": os.path.basename(path), "status": "failed", "error": str(exc)}
    fid = rec.get("feature_id") or rec.get("id") or os.path.basename(path)
    records[fid] = rec

rows = []
for feature in features:
    fid = feature.get("id")
    rec = records.get(fid, {})
    status = rec.get("status") or "pending"
    verdict_path = rec.get("verdict_path")
    if verdict_path and os.path.exists(verdict_path):
        try:
            verdict = json.load(open(verdict_path, encoding="utf-8"))
            status = "passed" if verdict.get("pass") is True else "failed"
        except Exception:
            status = "failed"
    if status not in {"passed", "failed", "pending"}:
        status = "pending"
    rows.append({
        "id": fid,
        "title": feature.get("title"),
        "status": status,
        "child_run_id": rec.get("child_run_id"),
        "child_run_dir": rec.get("child_run_dir"),
        "verdict_path": verdict_path,
        "final_artifact_ref": rec.get("final_artifact_ref"),
        "files_written": rec.get("files_written", []),
    })

total = len(rows)
passed = sum(1 for row in rows if row["status"] == "passed")
failed = sum(1 for row in rows if row["status"] == "failed")
pending = sum(1 for row in rows if row["status"] == "pending")
aggregate = {
    "total": total,
    "passed": passed,
    "failed": failed,
    "pending": pending,
    "pass_rate": (passed / total) if total else 0,
    "features": rows,
}
with open(aggregate_path, "w", encoding="utf-8") as f:
    json.dump(aggregate, f, indent=2, sort_keys=True)
print(json.dumps(aggregate, sort_keys=True))
PY

check() {
  local id="$1" desc="$2" cond="$3"
  echo "[$id] $desc" >&3
  if eval "$cond" >&3 2>&1; then
    printf '%s\t%s\ttrue\n' "$id" "$desc" >>"$CHECKS_TSV"
  else
    printf '%s\t%s\tfalse\n' "$id" "$desc" >>"$CHECKS_TSV"
  fi
}

check "aggregate-json-exists" "aggregate-verdict.json exists" '[ -s "$AGGREGATE" ]'
check "aggregate-json-shape" "aggregate verdict has required shape" \
  'python3 - "$AGGREGATE" <<'"'"'PY'"'"'
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
for key in ["total", "passed", "failed", "pending", "pass_rate", "features"]:
    assert key in d, f"missing {key}"
assert isinstance(d["features"], list)
PY'
check "all-p0-terminal" "all P0 features have terminal child status" \
  'python3 - "$AGGREGATE" <<'"'"'PY'"'"'
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
pending = [f["id"] for f in d["features"] if f.get("status") == "pending"]
assert not pending, f"pending features: {pending}"
PY'
check "all-p0-passed" "all P0 child verdicts passed" \
  'python3 - "$AGGREGATE" <<'"'"'PY'"'"'
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
assert d["total"] > 0, "no P0 features"
assert d["failed"] == 0, f"failed={d['failed']}"
assert d["pending"] == 0, f"pending={d['pending']}"
assert d["passed"] == d["total"], "not all features passed"
PY'

python3 - "$NAME" "$EVIDENCE" "$CHECKS_TSV" "$AGGREGATE" <<'PY'
import json, sys
name, evidence, checks_tsv, aggregate = sys.argv[1:5]
checks = []
with open(checks_tsv, encoding="utf-8") as f:
    for line in f:
        cid, desc, passed = line.rstrip("\n").split("\t", 2)
        checks.append({"id": cid, "description": desc, "pass": passed == "true"})
failed = [c["id"] for c in checks if not c["pass"]]
print(json.dumps({
    "verifier": name,
    "pass": not failed,
    "evidence_path": evidence,
    "checks_run": [c["id"] for c in checks],
    "failed_checks": failed,
    "checks": checks,
    "reasons": failed,
    "artifact_ref": aggregate
}))
PY
