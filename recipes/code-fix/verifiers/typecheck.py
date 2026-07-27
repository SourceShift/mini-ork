#!/usr/bin/env python3
# verifiers/typecheck.py — run the project's type-checker and emit structured JSON.
#
# Python port of typecheck.sh (bash-removal WS8). Same rc semantics, env vars,
# and output text.
#
# Exit codes:
#   0  typecheck passed
#   1  typecheck failed
#
# Env vars:
#   MINI_ORK_TYPECHECK_CMD   explicit command to run (skips auto-detect)
#   MINI_ORK_HOME            path to .mini-ork/ dir (default: .mini-ork)
#   MINI_ORK_RUN_ID          current run id (used in log path)

import json
import os
import re
import shutil
import subprocess
import sys

MINI_ORK_HOME = os.environ.get("MINI_ORK_HOME", ".mini-ork")
MINI_ORK_RUN_ID = os.environ.get("MINI_ORK_RUN_ID", "unknown-run")
LOG_DIR = os.path.join(MINI_ORK_HOME, "runs", MINI_ORK_RUN_ID)
os.makedirs(LOG_DIR, exist_ok=True)
LOG_PATH = os.path.join(LOG_DIR, "verifier_typecheck.log")

_SCRIPT_CANDIDATES = ("typecheck", "type-check", "tsc", "check")


def _read_package_json():
    try:
        with open("package.json", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _package_scripts():
    data = _read_package_json()
    if not isinstance(data, dict):
        return {}
    scripts = data.get("scripts")
    return scripts if isinstance(scripts, dict) else {}


# Returns True if the cwd looks like a TypeScript project.
# Marker rules: tsconfig.json present, OR package.json declares typescript
# (dep/devDep), OR package.json has a typecheck-style script.
# A globally-installed tsc is NOT a marker — gate it on real project intent
# (regression: bash/Python repos with tsc on PATH short-circuited on bare tsc).
def _has_ts_marker():
    if os.path.isfile("tsconfig.json"):
        return True
    if os.path.isfile("package.json"):
        data = _read_package_json()
        if isinstance(data, dict):
            for key in ("dependencies", "devDependencies"):
                deps = data.get(key)
                if isinstance(deps, dict) and "typescript" in deps:
                    return True
            scripts = _package_scripts()
            for candidate in _SCRIPT_CANDIDATES:
                if candidate in scripts:
                    return True
    return False


# Returns True if the cwd has a CONFIGURED mypy setup.
# Marker rules: mypy.ini present, OR setup.cfg with a [mypy] section, OR
# pyproject.toml with a [tool.mypy] section. A bare pyproject.toml is NOT a
# marker — nearly every Python repo has one, and `mypy .` on an unconfigured
# tree scans fixtures/vendored code and false-fails.
def _has_mypy_marker():
    if os.path.isfile("mypy.ini"):
        return True
    if os.path.isfile("setup.cfg"):
        try:
            if re.search(r"^\[mypy\]", open("setup.cfg", encoding="utf-8", errors="replace").read(), re.M):
                return True
        except OSError:
            pass
    if os.path.isfile("pyproject.toml"):
        try:
            if re.search(r"^\[tool\.mypy\]", open("pyproject.toml", encoding="utf-8", errors="replace").read(), re.M):
                return True
        except OSError:
            pass
    return False


def detect_typecheck_cmd():
    # Explicit override wins.
    if os.environ.get("MINI_ORK_TYPECHECK_CMD"):
        return os.environ["MINI_ORK_TYPECHECK_CMD"]

    # npm / pnpm / yarn — check package.json scripts first.
    if os.path.isfile("package.json"):
        scripts = _package_scripts()
        for candidate in _SCRIPT_CANDIDATES:
            if candidate in scripts:
                if shutil.which("pnpm"):
                    return f"pnpm run {candidate}"
                if shutil.which("npm"):
                    return f"npm run {candidate}"

    # TypeScript project marker required before we trust a tsc binary.
    if _has_ts_marker():
        if shutil.which("tsc"):
            return "tsc --noEmit"
        if os.path.isfile("./node_modules/.bin/tsc") and os.access("./node_modules/.bin/tsc", os.X_OK):
            return "./node_modules/.bin/tsc --noEmit"

    # Python mypy — require a configured mypy, not just any pyproject.toml.
    if shutil.which("mypy") and _has_mypy_marker():
        return "mypy ."

    # Rust
    if shutil.which("cargo") and os.path.isfile("Cargo.toml"):
        return "cargo check"

    # Go
    if shutil.which("go") and os.path.isfile("go.mod"):
        return "go build ./..."

    # Nothing found — skip and pass
    return ""


def main():
    cmd = detect_typecheck_cmd()

    if not cmd:
        sys.stderr.write("[typecheck] no typecheck command detected — skipping (pass)\n")
        print(json.dumps({
            "verifier": "typecheck", "pass": True, "evidence_path": None,
            "error_summary": "no typecheck tool detected — skipped",
        }, separators=(",", ":"), ensure_ascii=False))
        return 0

    sys.stderr.write(f"[typecheck] running: {cmd}\n")
    with open(LOG_PATH, "wb") as log:
        exit_code = subprocess.run(cmd, shell=True, stdout=log,
                                   stderr=subprocess.STDOUT).returncode

    if exit_code == 0:
        passed = True
        error_summary = ""
    else:
        passed = False
        # Extract first error line for the summary (grep -m1 "error").
        error_summary = "see log"
        try:
            with open(LOG_PATH, encoding="utf-8", errors="replace") as f:
                for line in f:
                    if "error" in line:
                        error_summary = line.rstrip("\n").replace('"', '\\"')[:200]
                        break
        except OSError:
            pass

    print(json.dumps({
        "verifier": "typecheck", "pass": passed, "evidence_path": LOG_PATH,
        "error_summary": error_summary,
    }, separators=(",", ":"), ensure_ascii=False))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
