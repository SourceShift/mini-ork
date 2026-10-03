#!/usr/bin/env python3
"""spec-lint — authoring-error lint over the indexed specs.

Design: docs/plans/2026-10-03-spec-driven-delivery.md (spec_lint: 7 authoring
errors, 4-property test). Runs mini_ork.specdir.lint over every spec in
${MINI_ORK_RUN_DIR}/spec-index.json and writes the findings
({spec_id, code, severity, message}) to ${MINI_ORK_RUN_DIR}/spec-lint.json.
Fails on any error-severity finding (NO_ACCEPTANCE, DUP_ID, DEP_CYCLE);
warnings are reported but do not fail.

Verdict: exactly one JSON line on stdout, {"pass": bool, "reason": str, ...};
the executor reads that payload as the node verdict. Exit 0 pass, 1 fail,
2 malformed input. Runs with cwd = target repo and MINI_ORK_RUN_DIR,
MINI_ORK_PLAN_PATH, ARTIFACT_PATH in the environment.

Lints exactly the indexed files (K1 read_entry + lint_text per source_path,
plus cross_spec_findings over them), keyed by the index's spec_id. A missing
or schema-invalid spec-index.json exits 2. An index with zero specs fails,
and an indexed source file that no longer exists is reported as an
error-severity SOURCE_MISSING finding.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _sdd_common import Malformed, atomic_write_json, run_dir, specdir_module  # noqa: E402


def body():
    rd = run_dir()
    index_path = rd / "spec-index.json"
    if not index_path.is_file():
        raise Malformed(f"required input missing: {index_path}")
    index_mod = specdir_module("index")
    try:
        index = index_mod.read_index(index_path)
    except index_mod.SpecIndexError as exc:
        raise Malformed(f"spec-index.json unreadable: {exc}", problems=exc.problems[:20]) from None
    scan = specdir_module("scan")
    lint = specdir_module("lint")

    findings, entries = [], []
    for spec_id, entry in sorted(index["specs"].items()):
        path = Path(entry["source_path"])
        if not path.is_file():
            findings.append(lint.Finding(spec_id=spec_id, code="SOURCE_MISSING", severity=lint.ERROR,
                                         message=f"indexed source file not found: {path}"))
            continue
        scanned, text = scan.read_entry(path)
        entries.append(scanned)
        findings.extend(lint.lint_text(spec_id, text, scanned.size_bytes))
    findings.extend(lint.cross_spec_findings(entries))
    findings.sort(key=lambda f: (f.spec_id, f.code, f.message))

    rows = [f.to_dict() for f in findings]
    atomic_write_json(rd / "spec-lint.json", rows)
    errors = [f for f in findings if f.severity == lint.ERROR]
    detail = {
        "spec_count": len(index["specs"]),
        "error_count": len(errors),
        "warning_count": len(findings) - len(errors),
        "error_codes": sorted({f.code for f in errors}),
        "errors": [f.to_dict() for f in errors][:20],
        "findings_path": str(rd / "spec-lint.json"),
    }
    if not index["specs"]:
        return False, "spec-index.json lists no specs", detail
    if errors:
        return False, f"{len(errors)} error-severity finding(s): {', '.join(detail['error_codes'])}", detail
    return True, f"{len(index['specs'])} spec(s) lint clean ({detail['warning_count']} warning(s))", detail


if __name__ == "__main__":
    from _sdd_common import run_main

    sys.exit(run_main(body))
