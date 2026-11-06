"""``mini-ork fallbacks`` — run the silent-fallback check from a shell or CI.

    mini-ork fallbacks                 # changed .py files in the working tree
    mini-ork fallbacks --all           # every .py under the repo root
    mini-ork fallbacks path/to/a.py    # exactly these files
    mini-ork fallbacks --json          # machine-readable findings

Exit codes:

    0  no findings (or nothing to check — stated, never implied)
    1  at least one swallowing handler, or an unparsable/unreadable file
    2  usage error

The rule itself lives in ``mini_ork.verify.fallback_guard``; this module owns
argument handling and reporting only, so the ``code-fix`` verifier and this
command cannot drift apart.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from mini_ork.verify import fallback_guard

HELP_TEXT = (
    "Usage: mini-ork fallbacks [paths...] [--all] [--json]\n"
    "\n"
    "Detect silent exception swallowing (`except: pass`, `except Exception:\n"
    "return None`). An intentional silence is annotated with\n"
    "`# fallback-guard: allow: <why>` on the `except` line.\n"
    "\n"
    "Options:\n"
    "  --all    scan every .py file under the current directory\n"
    "  --json   print findings as JSON\n"
    "  --help   show this help\n"
)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    want_all = want_json = False
    paths: list[str] = []
    for arg in argv:
        if arg in ("--help", "-h"):
            sys.stdout.write(HELP_TEXT)
            return 0
        if arg == "--all":
            want_all = True
        elif arg == "--json":
            want_json = True
        elif arg.startswith("-"):
            sys.stderr.write(f"Unknown flag: {arg}\n")
            sys.stderr.write(HELP_TEXT)
            return 2
        else:
            paths.append(arg)

    cwd = Path.cwd()
    if paths:
        targets = [Path(p) for p in paths]
        scope = "the given paths"
    elif want_all:
        targets = fallback_guard.find_python_files(cwd)
        scope = f"every .py file under {cwd}"
    else:
        targets = fallback_guard.changed_python_files(cwd)
        scope = "changed .py files"

    findings = fallback_guard.findings_for_paths(targets)
    if want_json:
        print(json.dumps({
            "scope": scope, "files_checked": len(targets),
            "pass": not findings,
            "findings": [f.as_dict() for f in findings],
        }, indent=2))
    else:
        for finding in findings:
            sys.stderr.write(finding.render() + "\n")
        if findings:
            sys.stderr.write(
                f"fallbacks: {len(findings)} finding(s) in {len(targets)} "
                f"file(s) ({scope})\n")
        elif not targets:
            sys.stdout.write(
                f"fallbacks: nothing to check ({scope}) — use --all to scan "
                "the tree\n")
        else:
            sys.stdout.write(
                f"fallbacks: OK ({len(targets)} file(s), {scope})\n")
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
