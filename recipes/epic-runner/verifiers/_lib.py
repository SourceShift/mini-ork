#!/usr/bin/env python3
# Shared helpers for epic-runner verifier scripts.
# Python port of _lib.sh (bash-removal WS8).

import json
import os


class EvidenceLog:
    """Port of _evidence_log_init + _record_check + _emit_verifier_json."""

    def __init__(self, name):
        run_dir = os.environ["MINI_ORK_RUN_DIR"]
        self.evidence = os.path.join(run_dir, f"verifier-{name}.log")
        self.checks_tsv = os.path.join(run_dir, f"verifier-{name}.checks.tsv")
        open(self.checks_tsv, "w").close()
        self._ev = open(self.evidence, "w")
        self._tsv = open(self.checks_tsv, "a")

    def write(self, text):
        """Write a raw line to the evidence log (bash ``>&3``)."""
        self._ev.write(text)
        self._ev.flush()

    def record_check(self, cid, desc, fn):
        """Port of _record_check: fn returns truthy, or runs a command whose
        output goes to the evidence log and whose rc is the verdict."""
        self._ev.write(f"[{cid}] {desc}\n")
        self._ev.flush()
        try:
            ok = bool(fn())
        except Exception as exc:
            self._ev.write(f"{type(exc).__name__}: {exc}\n")
            ok = False
        self._tsv.write(f"{cid}\t{desc}\t{'true' if ok else 'false'}\n")
        self._tsv.flush()
        self._ev.write("  ok\n" if ok else "  FAIL\n")
        self._ev.flush()

    def close(self):
        self._ev.close()
        self._tsv.close()


def check_pnpm_workspace(repo):
    return os.path.isdir(repo) and os.path.isfile(os.path.join(repo, "pnpm-workspace.yaml"))


def check_psql_credentials_set():
    return all(os.environ.get(k) for k in ("PGPASSWORD", "PGHOST", "PGPORT", "PGUSER", "PGDATABASE"))


def emit_verifier_json(log, name, artifact_ref):
    """Port of _emit_verifier_json (reads the checks TSV, prints the verdict)."""
    checks = []
    with open(log.checks_tsv, encoding="utf-8") as f:
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
        "evidence_path": log.evidence,
        "checks_run": [c["name"] for c in checks],
        "failed_checks": failed,
        "checks": checks,
        "reasons": [f"{c['name']} failed; see {log.evidence}" for c in checks if not c["pass"]],
        "checked_criteria": [c["name"] for c in checks],
        "artifact_ref": artifact_ref,
    }, sort_keys=True))
