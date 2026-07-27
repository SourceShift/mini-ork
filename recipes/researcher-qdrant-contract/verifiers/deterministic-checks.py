#!/usr/bin/env python3
# verifiers/deterministic-checks.py - runtime artifact validation for researcher-qdrant-contract.
#
# Python port of deterministic-checks.sh (bash-removal WS8). Same checks,
# evidence text, JSON schema, and rc semantics.
#
# Inputs (via env):
#   MINI_ORK_RUN_DIR - run directory set by the native execute runtime
#
# Output: JSON to stdout. Exit code is always 0; caller reads .pass.

import json
import os
import re
import sys

RUN_DIR = os.environ["MINI_ORK_RUN_DIR"]
NAME = "deterministic-checks"
EVIDENCE = os.path.join(RUN_DIR, f"verifier-{NAME}.log")
CHECKS_TSV = os.path.join(RUN_DIR, f"verifier-{NAME}.checks.tsv")

open(CHECKS_TSV, "w").close()
_ev = open(EVIDENCE, "w")
_tsv = open(CHECKS_TSV, "a")

PLAN = os.path.join(RUN_DIR, "qdrant-contract-remediation-plan.md")
FINDINGS = os.path.join(RUN_DIR, "qdrant-contract-findings.json")
PATCH_SUMMARY = os.path.join(RUN_DIR, "qdrant-contract-patch-summary.md")
VERIFY = os.path.join(RUN_DIR, "qdrant-contract-verification.md")


def _check(cid, desc, fn):
    _ev.write(f"[{cid}] {desc}\n")
    _ev.flush()
    try:
        ok = bool(fn())
    except Exception as exc:
        _ev.write(f"{type(exc).__name__}: {exc}\n")
        ok = False
    _tsv.write(f"{cid}\t{desc}\t{'true' if ok else 'false'}\n")
    _tsv.flush()
    _ev.write("  ok\n" if ok else "  FAIL\n")
    _ev.flush()


def _read(path):
    try:
        return open(path, encoding="utf-8", errors="replace").read()
    except OSError:
        return ""


def _grep(pattern, path, flags=0):
    return re.search(pattern, _read(path), flags) is not None


def _grep_any(pattern, *paths, flags=0):
    """grep -q pattern file1 file2 … — true when ANY file matches."""
    return any(_grep(pattern, p, flags) for p in paths)


def _grep_all(pattern, *paths, flags=0):
    """grep -q pattern file1 file2 — true when EVERY file matches."""
    return all(_grep(pattern, p, flags) for p in paths)


def _nonempty(path):
    return os.path.isfile(path) and os.path.getsize(path) > 0


def _line_count_at_least(path, n):
    return os.path.isfile(path) and _read(path).count("\n") >= n


# Template tier: all declared artifacts exist, are non-empty, and have shape.
_check("artifact-plan-exists", "qdrant-contract-remediation-plan.md exists", lambda: os.path.isfile(PLAN))
_check("artifact-plan-non-empty", "qdrant-contract-remediation-plan.md is non-empty", lambda: _nonempty(PLAN))
_check("artifact-plan-line-count", "remediation plan has at least 10 lines",
       lambda: _line_count_at_least(PLAN, 10))
_check("artifact-plan-heading", "remediation plan has markdown heading anchor",
       lambda: _grep(r"^# ", PLAN, re.M))

_check("artifact-findings-exists", "qdrant-contract-findings.json exists", lambda: os.path.isfile(FINDINGS))
_check("artifact-findings-non-empty", "qdrant-contract-findings.json is non-empty", lambda: _nonempty(FINDINGS))


def _json_parses(path):
    json.load(open(path, encoding="utf-8"))
    return True


_check("artifact-findings-json-parses", "qdrant-contract-findings.json parses as JSON",
       lambda: _json_parses(FINDINGS))

_check("artifact-patch-summary-exists", "qdrant-contract-patch-summary.md exists",
       lambda: os.path.isfile(PATCH_SUMMARY))
_check("artifact-patch-summary-non-empty", "qdrant-contract-patch-summary.md is non-empty",
       lambda: _nonempty(PATCH_SUMMARY))
_check("artifact-patch-summary-line-count", "patch summary has at least 5 lines",
       lambda: _line_count_at_least(PATCH_SUMMARY, 5))
_check("artifact-patch-summary-file-line-anchor", "patch summary cites file:line evidence",
       lambda: _grep(r"[A-Za-z0-9_./-]+:[0-9]+", PATCH_SUMMARY))

_check("artifact-verification-exists", "qdrant-contract-verification.md exists", lambda: os.path.isfile(VERIFY))
_check("artifact-verification-non-empty", "qdrant-contract-verification.md is non-empty", lambda: _nonempty(VERIFY))
_check("artifact-verification-line-count", "verification report has at least 5 lines",
       lambda: _line_count_at_least(VERIFY, 5))
_check("artifact-verification-file-line-anchor", "verification report cites file:line evidence",
       lambda: _grep(r"[A-Za-z0-9_./-]+:[0-9]+", VERIFY))
_check("evidence-log-opened", "evidence log was opened for writing", lambda: os.path.isfile(EVIDENCE))


# Task-specific tier: researcher PG/Qdrant contract assertions.
def _findings_json_schema():
    d = json.load(open(FINDINGS, encoding="utf-8"))
    assert isinstance(d.get("findings"), list)
    assert isinstance(d.get("metadata"), dict)
    allowed = {"critical", "high", "medium", "low"}
    for f in d["findings"]:
        assert all(k in f for k in ("id", "flow", "severity", "description",
                                    "affected_files", "remediation_phase")), f
        assert f.get("severity") in allowed, f
    return True


_check("findings-json-schema", "findings JSON has metadata and typed findings", _findings_json_schema)
_check("payload-text-preview", "remediation plan includes text_preview payload key",
       lambda: _grep(r"text_preview", PLAN, re.I))
_check("payload-source-kind", "remediation plan includes source_kind payload key",
       lambda: _grep(r"source_kind", PLAN, re.I))
_check("source-of-truth-invariant", "plan preserves PG as truth and Qdrant as derived index",
       lambda: _grep(r"postgres|postgresql|pg", PLAN, re.I)
       and _grep(r"qdrant", PLAN, re.I)
       and _grep(r"canonical|source of truth|source-of-truth|derived", PLAN, re.I))
_check("canonical-sync-writer", "plan routes Qdrant writes through <canonical_sync_module>",
       lambda: _grep_any(r"<canonical_sync_module>", PLAN, PATCH_SUMMARY, flags=re.I))
_check("book-chapter-service-normalized", "bookChapterEmbeddingService is retired or normalized",
       lambda: _grep_any(r"bookChapterEmbeddingService", PLAN, PATCH_SUMMARY, flags=re.I)
       and _grep_any(r"retir|normaliz|canonical sync|<canonical_sync_module>", PLAN, PATCH_SUMMARY, flags=re.I))
_check("retrieval-allowlist-coverage", "plan or verification covers retrieval allowlists",
       lambda: _grep_any(r"allowlist", PLAN, VERIFY, flags=re.I)
       and _grep_any(r"text_chunk|highlight|ai_annotation|generated_content|book_chapter_content",
                     PLAN, VERIFY, flags=re.I))
_check("reconciliation-dry-run-flag", "reconciliation/backfill requires --dry-run",
       lambda: _grep_any(r"--dry-run", PLAN, VERIFY, flags=re.I))
_check("dry-run-gates-writes", "dry-run is described as a write gate, not just an argument",
       lambda: _grep_any(r"dry.?run.*(gate|guard|before|without writing|no writes|would)",
                         PLAN, VERIFY, flags=re.I))
_check("verification-records-commands", "verification report records commands or dry-run evidence",
       lambda: _grep(r"command|dry.?run|evidence", VERIFY, re.I))
_check("no-blind-full-reindex", "artifacts reject blind full reindexing",
       lambda: _grep_any(r"no blind|without blind|forbid.*blind|not.*blind|blind_full_reindex",
                         PLAN, PATCH_SUMMARY, VERIFY, flags=re.I))
_check("no-new-direct-qdrant-writer", "patch summary rejects new direct upsert/upload_points writers",
       lambda: _grep(r"upsert|upload_points|direct qdrant writer", PATCH_SUMMARY, re.I)
       and _grep(r"deny|forbid|reject|no new|outside.*<canonical_sync_module>|canonical sync",
                 PATCH_SUMMARY, re.I))

checks = []
with open(CHECKS_TSV, encoding="utf-8") as fh:
    for line in fh:
        cid, desc, passed = line.rstrip("\n").split("\t", 2)
        checks.append({
            "name": cid,
            "passed": passed == "true",
            "evidence": f"{EVIDENCE}#{cid}",
            "description": desc,
        })
failed = [c["name"] for c in checks if not c["passed"]]
status = "pass" if not failed else "fail"
print(json.dumps({
    "verifier": NAME,
    "status": status,
    "pass": not failed,
    "reviewer_verdict": status,
    "evidence_path": EVIDENCE,
    "checks_run": [c["name"] for c in checks],
    "failed_checks": failed,
    "checks": checks,
    "files_read": [PLAN, FINDINGS, PATCH_SUMMARY, VERIFY],
    "tool_calls": ["bash", "python3", "grep", "wc"],
    "duration_ms": 1,
}, sort_keys=True))

_ev.close()
_tsv.close()
sys.exit(0)
