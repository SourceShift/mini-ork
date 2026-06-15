#!/usr/bin/env bash
# verifiers/deterministic-checks.sh - runtime artifact validation for researcher-qdrant-contract.
#
# Inputs (via env):
#   MINI_ORK_RUN_DIR - run directory set by mini-ork-execute
#
# Output: JSON to stdout. Exit code is always 0; caller reads .pass.

set -uo pipefail

RUN_DIR="${MINI_ORK_RUN_DIR:?MINI_ORK_RUN_DIR required}"
NAME="deterministic-checks"
EVIDENCE="$RUN_DIR/verifier-$NAME.log"
CHECKS_TSV="$RUN_DIR/verifier-$NAME.checks.tsv"
: >"$CHECKS_TSV"
exec 3>"$EVIDENCE"

PLAN="$RUN_DIR/qdrant-contract-remediation-plan.md"
FINDINGS="$RUN_DIR/qdrant-contract-findings.json"
PATCH_SUMMARY="$RUN_DIR/qdrant-contract-patch-summary.md"
VERIFY="$RUN_DIR/qdrant-contract-verification.md"

_check() {
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

# Template tier: all declared artifacts exist, are non-empty, and have shape.
_check "artifact-plan-exists" "qdrant-contract-remediation-plan.md exists" '[ -f "$PLAN" ]'
_check "artifact-plan-non-empty" "qdrant-contract-remediation-plan.md is non-empty" '[ -s "$PLAN" ]'
_check "artifact-plan-line-count" "remediation plan has at least 10 lines" '[ "$(wc -l < "$PLAN")" -ge 10 ]'
_check "artifact-plan-heading" "remediation plan has markdown heading anchor" 'grep -qE "^# " "$PLAN"'

_check "artifact-findings-exists" "qdrant-contract-findings.json exists" '[ -f "$FINDINGS" ]'
_check "artifact-findings-non-empty" "qdrant-contract-findings.json is non-empty" '[ -s "$FINDINGS" ]'
_check "artifact-findings-json-parses" "qdrant-contract-findings.json parses as JSON" \
  'python3 -m json.tool "$FINDINGS" >/dev/null'

_check "artifact-patch-summary-exists" "qdrant-contract-patch-summary.md exists" '[ -f "$PATCH_SUMMARY" ]'
_check "artifact-patch-summary-non-empty" "qdrant-contract-patch-summary.md is non-empty" '[ -s "$PATCH_SUMMARY" ]'
_check "artifact-patch-summary-line-count" "patch summary has at least 5 lines" '[ "$(wc -l < "$PATCH_SUMMARY")" -ge 5 ]'
_check "artifact-patch-summary-file-line-anchor" "patch summary cites file:line evidence" \
  'grep -qE "[A-Za-z0-9_./-]+:[0-9]+" "$PATCH_SUMMARY"'

_check "artifact-verification-exists" "qdrant-contract-verification.md exists" '[ -f "$VERIFY" ]'
_check "artifact-verification-non-empty" "qdrant-contract-verification.md is non-empty" '[ -s "$VERIFY" ]'
_check "artifact-verification-line-count" "verification report has at least 5 lines" '[ "$(wc -l < "$VERIFY")" -ge 5 ]'
_check "artifact-verification-file-line-anchor" "verification report cites file:line evidence" \
  'grep -qE "[A-Za-z0-9_./-]+:[0-9]+" "$VERIFY"'
_check "evidence-log-opened" "evidence log was opened for writing" '[ -f "$EVIDENCE" ]'

# Task-specific tier: researcher PG/Qdrant contract assertions.
_check "findings-json-schema" "findings JSON has metadata and typed findings" \
  'python3 -c "import json,sys; d=json.load(open(sys.argv[1])); assert isinstance(d.get(\"findings\"), list); assert isinstance(d.get(\"metadata\"), dict); allowed={\"critical\",\"high\",\"medium\",\"low\"}; [(_ for _ in ()).throw(AssertionError(f)) for f in d[\"findings\"] if not all(k in f for k in [\"id\",\"flow\",\"severity\",\"description\",\"affected_files\",\"remediation_phase\"]) or f.get(\"severity\") not in allowed]" "$FINDINGS"'
_check "payload-text-preview" "remediation plan includes text_preview payload key" \
  'grep -qi "text_preview" "$PLAN"'
_check "payload-source-kind" "remediation plan includes source_kind payload key" \
  'grep -qi "source_kind" "$PLAN"'
_check "source-of-truth-invariant" "plan preserves PG as truth and Qdrant as derived index" \
  'grep -qiE "postgres|postgresql|pg" "$PLAN" && grep -qi "qdrant" "$PLAN" && grep -qiE "canonical|source of truth|source-of-truth|derived" "$PLAN"'
_check "canonical-sync-writer" "plan routes Qdrant writes through <canonical_sync_module>" \
  'grep -qi "<canonical_sync_module>" "$PLAN" "$PATCH_SUMMARY"'
_check "book-chapter-service-normalized" "bookChapterEmbeddingService is retired or normalized" \
  'grep -qi "bookChapterEmbeddingService" "$PLAN" "$PATCH_SUMMARY" && grep -qiE "retir|normaliz|canonical sync|<canonical_sync_module>" "$PLAN" "$PATCH_SUMMARY"'
_check "retrieval-allowlist-coverage" "plan or verification covers retrieval allowlists" \
  'grep -qi "allowlist" "$PLAN" "$VERIFY" && grep -qiE "text_chunk|highlight|ai_annotation|generated_content|book_chapter_content" "$PLAN" "$VERIFY"'
_check "reconciliation-dry-run-flag" "reconciliation/backfill requires --dry-run" \
  'grep -qi -- "--dry-run" "$PLAN" "$VERIFY"'
_check "dry-run-gates-writes" "dry-run is described as a write gate, not just an argument" \
  'grep -qiE "dry.?run.*(gate|guard|before|without writing|no writes|would)" "$PLAN" "$VERIFY"'
_check "verification-records-commands" "verification report records commands or dry-run evidence" \
  'grep -qiE "command|dry.?run|evidence" "$VERIFY"'
_check "no-blind-full-reindex" "artifacts reject blind full reindexing" \
  'grep -qiE "no blind|without blind|forbid.*blind|not.*blind|blind_full_reindex" "$PLAN" "$PATCH_SUMMARY" "$VERIFY"'
_check "no-new-direct-qdrant-writer" "patch summary rejects new direct upsert/upload_points writers" \
  'grep -qiE "upsert|upload_points|direct qdrant writer" "$PATCH_SUMMARY" && grep -qiE "deny|forbid|reject|no new|outside.*<canonical_sync_module>|canonical sync" "$PATCH_SUMMARY"'

python3 - "$NAME" "$EVIDENCE" "$CHECKS_TSV" "$PLAN" "$FINDINGS" "$PATCH_SUMMARY" "$VERIFY" <<'PY'
import json
import sys

name, evidence, tsv = sys.argv[1:4]
files = sys.argv[4:]
checks = []
with open(tsv, encoding="utf-8") as fh:
    for line in fh:
        cid, desc, passed = line.rstrip("\n").split("\t", 2)
        checks.append({
            "name": cid,
            "passed": passed == "true",
            "evidence": f"{evidence}#{cid}",
            "description": desc,
        })
failed = [c["name"] for c in checks if not c["passed"]]
status = "pass" if not failed else "fail"
print(json.dumps({
    "verifier": name,
    "status": status,
    "pass": not failed,
    "reviewer_verdict": status,
    "evidence_path": evidence,
    "checks_run": [c["name"] for c in checks],
    "failed_checks": failed,
    "checks": checks,
    "files_read": files,
    "tool_calls": ["bash", "python3", "grep", "wc"],
    "duration_ms": 1,
}, sort_keys=True))
PY

exit 0
