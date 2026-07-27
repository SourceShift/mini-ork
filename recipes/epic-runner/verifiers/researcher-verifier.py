#!/usr/bin/env python3
# Project-aware verifier for epic-runner child runs targeting the researcher repo.
#
# Python port of researcher-verifier.sh (bash-removal WS8). Same checks,
# evidence text, JSON schema, and rc semantics.
#
# This script is intended to be passed through MINI_ORK_EPIC_VERIFIER_SCRIPT.
# It runs only when epic-runner invokes it against a researcher epic, not during
# framework-edit's own static verifier dispatch.

import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lib import EvidenceLog, check_pnpm_workspace, check_psql_credentials_set, emit_verifier_json  # noqa: E402

NAME = "researcher"

log = EvidenceLog(NAME)
RUN_DIR = os.environ["MINI_ORK_RUN_DIR"]

REPO = os.environ.get("MINI_ORK_EPIC_TARGET_REPO") or os.path.expanduser("~/ps/researcher")
CHANGED_FILE_LIST = os.path.join(RUN_DIR, f"verifier-{NAME}.changed-files")
TYPECHECK_FILE_LIST = os.path.join(RUN_DIR, f"verifier-{NAME}.typecheck-files")
JEST_INPUT_LIST = os.path.join(RUN_DIR, f"verifier-{NAME}.jest-inputs")


def _normalize_changed_files():
    files = []
    if os.environ.get("EPIC_CHANGED_FILES"):
        for line in os.environ["EPIC_CHANGED_FILES"].splitlines():
            line = line.strip()
            if line.startswith("./"):
                line = line[2:]
            if line:
                files.append(line)
    files = sorted(set(files))
    with open(CHANGED_FILE_LIST, "w") as f:
        for p in files:
            f.write(p + "\n")


def _read_lines(path):
    try:
        return [l.rstrip("\n") for l in open(path, encoding="utf-8") if l.strip()]
    except OSError:
        return []


def _collect_typecheck_files():
    files = sorted({p for p in _read_lines(CHANGED_FILE_LIST)
                    if p.endswith(".ts") or p.endswith(".tsx")})
    with open(TYPECHECK_FILE_LIST, "w") as f:
        for p in files:
            f.write(p + "\n")


def _typecheck_touched():
    files = _read_lines(TYPECHECK_FILE_LIST)
    if not files:
        log.write("skip: no .ts/.tsx files changed\n")
        return True
    if not check_pnpm_workspace(REPO):
        log.write(f"pnpm workspace missing at {REPO}; cannot run type-check:touched\n")
        return False
    return subprocess.run(["pnpm", "--dir", REPO, "type-check:touched"] + files,
                          stdout=log._ev, stderr=subprocess.STDOUT).returncode == 0


def _test_candidates_for_changed_file(path):
    out = []
    dir = os.path.dirname(path)
    base = os.path.basename(path)
    if base.endswith((".test.ts", ".test.tsx", ".spec.ts", ".spec.tsx")):
        if os.path.isfile(os.path.join(REPO, path)):
            out.append(path)
        return out

    stem, dot, ext = base.rpartition(".")
    if not dot:
        stem, ext = base, ""
    if ext in ("ts", "tsx"):
        for candidate in (
            f"{dir}/{stem}.test.{ext}",
            f"{dir}/{stem}.spec.{ext}",
            f"{dir}/__tests__/{stem}.test.{ext}",
            f"{dir}/__tests__/{stem}.spec.{ext}",
        ):
            if os.path.isfile(os.path.join(REPO, candidate)):
                out.append(candidate)
    return out


def _collect_jest_inputs():
    candidates = []
    for path in _read_lines(CHANGED_FILE_LIST):
        candidates += _test_candidates_for_changed_file(path)
    with open(JEST_INPUT_LIST, "w") as f:
        for p in sorted(set(candidates)):
            f.write(p + "\n")


def _jest_related_tests():
    inputs = _read_lines(JEST_INPUT_LIST)
    if not inputs:
        log.write("skip: no changed or adjacent jest test files found\n")
        return True
    if not os.path.isfile(os.path.join(REPO, "server", "jest.config.js")):
        log.write(f"missing jest config at {REPO}/server/jest.config.js\n")
        return False
    return subprocess.run(
        ["npx", "--prefix", REPO, "jest",
         "--config", os.path.join(REPO, "server", "jest.config.js"),
         "--findRelatedTests"] + inputs + ["--runInBand", "--forceExit"],
        stdout=log._ev, stderr=subprocess.STDOUT).returncode == 0


def _sql_probe():
    probe = os.environ.get("EPIC_SQL_PROBE", "")
    if not probe:
        log.write("skip: EPIC_SQL_PROBE not set\n")
        return True
    if not check_psql_credentials_set():
        log.write("missing one or more PostgreSQL env vars: PGPASSWORD, PGHOST, PGPORT, PGUSER, PGDATABASE\n")
        return False
    env = {**os.environ, "PGPASSWORD": os.environ["PGPASSWORD"]}
    return subprocess.run(
        ["psql", "-h", os.environ["PGHOST"], "-p", os.environ["PGPORT"],
         "-U", os.environ["PGUSER"], "-d", os.environ["PGDATABASE"], "-c", probe],
        env=env, stdout=log._ev, stderr=subprocess.STDOUT).returncode == 0


def _no_uncommitted_debt_files():
    r = subprocess.run(
        ["git", "-C", REPO, "status", "--porcelain", "--",
         "*.orig", "*.rej", "*~", ".pytest_cache", "coverage", "server/coverage"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    debt = r.stdout.decode("utf-8", "replace")
    if debt.strip():
        log.write(debt if debt.endswith("\n") else debt + "\n")
        return False
    return True


_normalize_changed_files()
_collect_typecheck_files()
_collect_jest_inputs()

log.write(f"target_repo={REPO}\n")
log.write(f"changed_files={CHANGED_FILE_LIST}\n")
log.write(f"typecheck_files={TYPECHECK_FILE_LIST}\n")
log.write(f"jest_inputs={JEST_INPUT_LIST}\n")

log.record_check("typecheck-passed",
                 "pnpm type-check:touched passes for changed TypeScript files, or is skipped when none changed",
                 _typecheck_touched)
log.record_check("jest-passed",
                 "jest related tests pass when changed or adjacent tests exist, or are skipped when none apply",
                 _jest_related_tests)
log.record_check("sql-probe-passed",
                 "optional EPIC_SQL_PROBE passes with operator-supplied PostgreSQL credentials, or is skipped when unset",
                 _sql_probe)
log.record_check("no-uncommitted-debt-files",
                 "target repo has no leftover debt artifacts such as .orig, .rej, backups, or coverage directories",
                 _no_uncommitted_debt_files)

emit_verifier_json(log, NAME, f"{REPO} {CHANGED_FILE_LIST}")
log.close()

sys.exit(0)
