#!/usr/bin/env python3
# Aggregate child recursive-validate-impl verdicts into aggregate-verdict.json.
#
# Python port of per-feature-dispatch-results.sh (bash-removal WS8). Same
# checks, evidence text, JSON schema, and rc semantics.

import glob
import json
import os
import subprocess

RUN_DIR = os.environ.get("MINI_ORK_RUN_DIR") or os.getcwd()
NAME = "per-feature-dispatch-results"
FEATURE_INDEX = os.path.join(RUN_DIR, "feature-index.json")
CHILD_DIR = os.path.join(RUN_DIR, "child-runs")
AGGREGATE = os.path.join(RUN_DIR, "aggregate-verdict.json")
EVIDENCE = os.path.join(RUN_DIR, f"verifier-{NAME}.log")
CHECKS_TSV = os.path.join(RUN_DIR, f"verifier-{NAME}.checks.tsv")

open(CHECKS_TSV, "w").close()
_ev = open(EVIDENCE, "w")
_tsv = open(CHECKS_TSV, "a")


def _aggregate():
    feature_index, child_dir, aggregate_path = FEATURE_INDEX, CHILD_DIR, AGGREGATE

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
    _ev.write(json.dumps(aggregate, sort_keys=True) + "\n")
    _ev.flush()


if os.path.isfile("scripts/miniork/aggregate-child-verdicts.sh") and \
        os.access("scripts/miniork/aggregate-child-verdicts.sh", os.X_OK):
    rc = subprocess.run(["bash", "scripts/miniork/aggregate-child-verdicts.sh", RUN_DIR],
                        stdout=_ev, stderr=subprocess.STDOUT).returncode
    if rc != 0:
        _ev.write("repo aggregate-child-verdicts.sh failed; continuing to shape checks\n")
        _ev.flush()
else:
    try:
        _aggregate()
    except Exception as exc:
        _ev.write(f"{type(exc).__name__}: {exc}\n")
        _ev.flush()


def check(cid, desc, fn):
    _ev.write(f"[{cid}] {desc}\n")
    _ev.flush()
    try:
        ok = bool(fn())
    except Exception as exc:
        _ev.write(f"{type(exc).__name__}: {exc}\n")
        ok = False
    _tsv.write(f"{cid}\t{desc}\t{'true' if ok else 'false'}\n")
    _tsv.flush()


check("aggregate-json-exists", "aggregate-verdict.json exists",
      lambda: os.path.isfile(AGGREGATE) and os.path.getsize(AGGREGATE) > 0)


def _aggregate_shape():
    d = json.load(open(AGGREGATE, encoding="utf-8"))
    for key in ["total", "passed", "failed", "pending", "pass_rate", "features"]:
        assert key in d, f"missing {key}"
    assert isinstance(d["features"], list)
    return True


check("aggregate-json-shape", "aggregate verdict has required shape", _aggregate_shape)


def _all_p0_terminal():
    d = json.load(open(AGGREGATE, encoding="utf-8"))
    pending = [f["id"] for f in d["features"] if f.get("status") == "pending"]
    assert not pending, f"pending features: {pending}"
    return True


check("all-p0-terminal", "all P0 features have terminal child status", _all_p0_terminal)


def _all_p0_passed():
    d = json.load(open(AGGREGATE, encoding="utf-8"))
    assert d["total"] > 0, "no P0 features"
    assert d["failed"] == 0, f"failed={d['failed']}"
    assert d["pending"] == 0, f"pending={d['pending']}"
    assert d["passed"] == d["total"], "not all features passed"
    return True


check("all-p0-passed", "all P0 child verdicts passed", _all_p0_passed)

checks = []
with open(CHECKS_TSV, encoding="utf-8") as f:
    for line in f:
        cid, desc, passed = line.rstrip("\n").split("\t", 2)
        checks.append({"id": cid, "description": desc, "pass": passed == "true"})
failed = [c["id"] for c in checks if not c["pass"]]
print(json.dumps({
    "verifier": NAME,
    "pass": not failed,
    "evidence_path": EVIDENCE,
    "checks_run": [c["id"] for c in checks],
    "failed_checks": failed,
    "checks": checks,
    "reasons": failed,
    "artifact_ref": AGGREGATE,
}))

_ev.close()
_tsv.close()
