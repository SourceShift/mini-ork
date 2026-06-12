#!/usr/bin/env bash
# Shared helpers for epic-runner verifier scripts.

_evidence_log_init() {
  local name="$1"
  RUN_DIR="${MINI_ORK_RUN_DIR:?MINI_ORK_RUN_DIR required}"
  EVIDENCE="$RUN_DIR/verifier-$name.log"
  CHECKS_TSV="$RUN_DIR/verifier-$name.checks.tsv"
  : >"$CHECKS_TSV"
  exec 3>"$EVIDENCE"
}

_record_check() {
  local id="$1" desc="$2" cond="$3"
  echo "[$id] $desc" >&3
  if eval "$cond" >&3 2>&1; then
    printf '%s\t%s\ttrue\n' "$id" "$desc" >>"$CHECKS_TSV"
    echo "  ok" >&3
  else
    printf '%s\t%s\tfalse\n' "$id" "$desc" >>"$CHECKS_TSV"
    echo "  FAIL" >&3
  fi
}

_check_pnpm_workspace() {
  local repo="$1"
  [ -d "$repo" ] && [ -f "$repo/pnpm-workspace.yaml" ]
}

_check_psql_credentials_set() {
  [ -n "${PGPASSWORD:-}" ] &&
    [ -n "${PGHOST:-}" ] &&
    [ -n "${PGPORT:-}" ] &&
    [ -n "${PGUSER:-}" ] &&
    [ -n "${PGDATABASE:-}" ]
}

_emit_verifier_json() {
  local name="$1" artifact_ref="$2"
  python3 - "$name" "$EVIDENCE" "$CHECKS_TSV" "$artifact_ref" <<'PY'
import json
import sys

name, evidence, checks_tsv, artifact_ref = sys.argv[1:5]
checks = []
with open(checks_tsv, encoding="utf-8") as f:
    for line in f:
        line = line.rstrip("\n")
        if not line:
            continue
        cid, desc, passed = line.split("\t", 2)
        checks.append({
            "name": cid,
            "expected": desc,
            "actual": "see evidence log",
            "pass": passed == "true",
        })

failed = [c["name"] for c in checks if not c["pass"]]
print(json.dumps({
    "verifier": name,
    "pass": not failed,
    "verdict": "pass" if not failed else "fail",
    "evidence_path": evidence,
    "checks_run": [c["name"] for c in checks],
    "failed_checks": failed,
    "checks": checks,
    "reasons": [f"{c['name']} failed; see {evidence}" for c in checks if not c["pass"]],
    "checked_criteria": [c["name"] for c in checks],
    "artifact_ref": artifact_ref,
}, sort_keys=True))
PY
}
