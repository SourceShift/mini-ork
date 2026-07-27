#!/usr/bin/env python3
"""readme_claim_check.py — Layer 1 (mechanical, sub-second, FREE).

Probes the load-bearing numerical + path claims in README.md against
the live repo state. Catches the 90% class of drift surfaced in the
2026-06-05 audit (docs/audits/20260605-readme-claims-audit.md).

Python port of scripts/readme-claim-check.sh (bash-removal Phase 4) — same
probes, regexes, file paths, counts, messages, and exit codes. Designed to run
from a git hook OR a Make target OR ad-hoc.

Exit codes:
  0  no drift
  1  mechanical drift detected — README is out of sync with the repo
  2  invocation error (missing dep, missing README, etc)

Usage:
  scripts/readme_claim_check.py              # full check
  scripts/readme_claim_check.py --verbose    # print every probe
  scripts/readme_claim_check.py --json       # JSON output (CI-friendly)

Env knobs:
  MO_README                 path to README.md (default: ./README.md)
  MO_README_SKIP_MIGRATIONS 1 to skip migration-count check
  MO_README_DRIFT_TOLERANCE 0|integer — how far off a count can be
                            before flagging drift (default: 0 — strict)
"""

from __future__ import annotations

import glob
import json
import os
import re
import subprocess
import sys

MO_README = os.environ.get("MO_README", "README.md")
TOLERANCE = int(os.environ.get("MO_README_DRIFT_TOLERANCE", "0") or "0")
VERBOSE = False
JSON_MODE = False


def readme_int_after(needle: str) -> str:
    """Pull the integer that appears in README.md before a fixed phrase.

    Mirrors `grep -m1 -oE "[0-9]+ <needle>"`: first matching line wins, and
    the FIRST integer of the first match on that line is returned.
    """
    pattern = re.compile(r"([0-9]+) " + needle)
    try:
        with open(MO_README, encoding="utf-8") as f:
            for line in f:
                m = pattern.search(line)
                if m:
                    return m.group(1)
    except OSError:
        pass
    return ""


def _git_ls_files(pathspec: str) -> list[str]:
    try:
        out = subprocess.run(
            ["git", "ls-files", pathspec],
            capture_output=True, text=True, check=False,
        ).stdout
    except OSError:
        return []
    return [line for line in out.splitlines() if line]


def count_dir(dirpath: str, pat: str) -> int:
    """Count TRACKED direct-child files matching a glob under a dir.

    Uses git ls-files (not find) so untracked working-tree files don't
    inflate the count; a regex post-filter keeps only direct-child paths
    because git pathspec `*` matches across `/` boundaries.
    """
    if not os.path.isdir(dirpath):
        return 0
    # Convert shell glob `cl_*.sh` to regex `cl_[^/]+\.sh`.
    rx = re.escape(pat).replace(r"\*", "[^/]+")
    matcher = re.compile(f"^{re.escape(dirpath)}/{rx}$")
    return sum(1 for p in _git_ls_files(f"{dirpath}/") if matcher.match(p))


def count_subdirs(dirpath: str) -> int:
    """Count distinct second path components among tracked files under a dir."""
    if not os.path.isdir(dirpath):
        return 0
    seconds = set()
    for path in _git_ls_files(f"{dirpath}/"):
        parts = path.split("/")
        if len(parts) > 1 and parts[1]:
            seconds.add(parts[1])
    return len(seconds)


# ── probes ─────────────────────────────────────────────────────────────────
probe_names: list[str] = []
probe_claims: list[str] = []
probe_actuals: list[str] = []
probe_verdicts: list[str] = []
fail_count = 0


def add_probe(name: str, claimed: str, actual: str | int) -> None:
    global fail_count
    claimed_s, actual_s = str(claimed), str(actual)
    probe_names.append(name)
    probe_claims.append(claimed_s)
    probe_actuals.append(actual_s)
    if claimed_s == "":
        diff = TOLERANCE + 1
    else:
        diff = abs(int(actual_s) - int(claimed_s))
    if claimed_s == "" or diff > TOLERANCE:
        probe_verdicts.append("DRIFT")
        fail_count += 1
    else:
        probe_verdicts.append("OK")


def add_optional_numeric_probe(name: str, claimed: str, actual: str | int) -> None:
    """Audit a count when it is claimed; absence of an optional marketing
    claim is not documentation drift."""
    if claimed != "":
        add_probe(name, claimed, actual)
    elif VERBOSE:
        print(f"Skipping optional README inventory claim: {name}")


def run_probes() -> list[str]:
    """Run all probes; returns the list of missing cited paths."""
    # Probe 1 — lib/*.sh count claim
    add_optional_numeric_probe(
        "lib/*.sh count",
        readme_int_after("framework primitives"),
        count_dir("lib", "*.sh"),
    )

    # Probe 2 — bin/mini-ork-* entrypoint count claim
    bin_count_claim = readme_int_after(r"user-facing.*bin/mini-ork.*entrypoints")
    bin_count_actual = len(glob.glob("bin/mini-ork*"))
    add_optional_numeric_probe("bin/mini-ork-* entrypoints",
                               bin_count_claim, bin_count_actual)

    # Probe 3 — migrations count claim
    if os.environ.get("MO_README_SKIP_MIGRATIONS", "0") != "1":
        add_optional_numeric_probe(
            "db/migrations/*.sql count",
            readme_int_after("schema migrations"),
            count_dir("db/migrations", "*.sql"),
        )

    # Probe 4 — recipe inventory: audit a detailed table when present.
    with open(MO_README, encoding="utf-8") as f:
        readme_lines = f.read().splitlines()
    recipes_actual = count_subdirs("recipes")
    if "### RECIPES" in readme_lines:
        in_range = False
        table_rows = 0
        row_re = re.compile(r"^\| `[a-z0-9-]+` \|")
        for line in readme_lines:
            if re.match(r"^### RECIPES", line):
                in_range = True
            if in_range and row_re.match(line):
                table_rows += 1
            if in_range and re.match(r"^Add your own", line):
                break
        add_probe("recipes table rows", str(recipes_actual), str(table_rows))

    # Probe 5 — providers count claim
    add_optional_numeric_probe(
        "lib/providers/cl_*.sh count",
        readme_int_after("model-family wrappers ship"),
        count_dir("lib/providers", "cl_*.sh"),
    )

    # ── regression-guard probes ────────────────────────────────────────────
    # Probe 6 — `install.sh --check` MUST NOT come back (audit closed it)
    regression_install_check = sum(
        1 for line in readme_lines if "install.sh --check" in line
    )
    probe_names.append("regression: install.sh --check banned phrase")
    probe_claims.append("0")
    probe_actuals.append(str(regression_install_check))
    _verdict(regression_install_check > 0)

    # Probe 7 — every cited file/dir path actually exists on disk
    missing_paths: list[str] = []
    cited = set()
    backtick_re = re.compile(r"`([a-zA-Z_./-]+)`")
    prefix_re = re.compile(r"^(recipes|docs|lib|bin|schemas|db|examples|kickoffs)/")
    with open(MO_README, encoding="utf-8") as f:
        for m in backtick_re.finditer(f.read()):
            if prefix_re.match(m.group(1)):
                cited.add(m.group(1))
    for p in sorted(cited):
        if not os.path.exists(p.rstrip("/")):
            missing_paths.append(p)
    probe_names.append("cited paths exist")
    probe_claims.append("0 missing")
    probe_actuals.append(f"{len(missing_paths)} missing")
    _verdict(bool(missing_paths))
    return missing_paths


def _verdict(failed: bool) -> None:
    global fail_count
    if failed:
        probe_verdicts.append("DRIFT")
        fail_count += 1
    else:
        probe_verdicts.append("OK")


def render(missing_paths: list[str]) -> None:
    if JSON_MODE:
        print(json.dumps({
            "verdict": "CLEAN" if fail_count == 0 else "DRIFT",
            "fail_count": fail_count,
            "probes": [
                {"name": n, "claimed": c, "actual": a, "verdict": v}
                for n, c, a, v in zip(probe_names, probe_claims,
                                      probe_actuals, probe_verdicts)
            ],
            "missing_paths": missing_paths,
        }, separators=(",", ":")))
        return
    print("── README claim-check (mechanical, Layer 1) ──")
    print(f"{'PROBE':<45}  {'CLAIMED':<12} {'ACTUAL':<12} VERDICT")
    print(f"{'─────':<45}  {'───────':<12} {'──────':<12} ───────")
    for name, claimed, actual, verdict in zip(
            probe_names, probe_claims, probe_actuals, probe_verdicts):
        print(f"{name:<45}  {claimed:<12} {actual:<12} {verdict}")
    if missing_paths:
        print()
        print("Missing paths cited in README:")
        for p in missing_paths:
            print(f"  - {p}")
    print()
    if fail_count == 0:
        print(f"✓ CLEAN — {len(probe_names)} probes passed")
    else:
        print(f"✗ DRIFT — {fail_count} / {len(probe_names)} probes failed")
        print()
        print("Fix the README to match the repo state, OR fix the repo state to")
        print("match the README (whichever is wrong). Bypass with")
        print("MO_README_DRIFT_SKIP=1 git push  (the pre-push hook honors it)")
        print("or  git push --no-verify  for one-shot.")


def main(argv: list[str]) -> int:
    global VERBOSE, JSON_MODE
    for arg in argv:
        if arg in ("--verbose", "-v"):
            VERBOSE = True
        elif arg == "--json":
            JSON_MODE = True
        elif arg in ("--help", "-h"):
            print(__doc__)
            return 0
    if not os.path.isfile(MO_README):
        print(f"readme-claim-check: {MO_README} not found", file=sys.stderr)
        return 2
    missing = run_probes()
    render(missing)
    return 0 if fail_count == 0 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
