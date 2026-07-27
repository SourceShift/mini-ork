#!/usr/bin/env python3
# verifiers/fork-closure.py — deterministic no-dangling-runtime-edge gate.
#
# Python port of fork-closure.sh (bash-removal WS8). Same checks, rc semantics,
# and JSON output.
#
# The LLM integration map is useful analysis, but fork retirement must not rely
# on an LLM noticing every caller. This gate inspects the migrated worktree and
# requires both the retired entrypoint and all executable/runtime references to
# be absent. Historical documentation is intentionally outside this runtime
# gate and is updated during the completion audit.

import ast
import json
import os
import subprocess
import sys

RUN_DIR = os.environ["MINI_ORK_RUN_DIR"]
REPO_ROOT = os.environ.get("MO_TARGET_CWD") or os.environ.get("MINI_ORK_ROOT") or os.getcwd()
# bash: FORK="${MO_FORK:?MO_FORK required}" — unset OR empty is a hard error.
FORK = os.environ.get("MO_FORK", "")
if not FORK:
    sys.stderr.write("MO_FORK: MO_FORK required\n")
    sys.exit(1)
EVIDENCE = os.path.join(RUN_DIR, "verifier-fork-closure.log")

passed = True
reasons = []
os.makedirs(RUN_DIR, exist_ok=True)

search_roots = [rel for rel in ("bin", "lib", "mini_ork", "scripts", "tests", "gates", "web", "ui")
                if os.path.exists(os.path.join(REPO_ROOT, rel))]

open(EVIDENCE, "w").close()


def _rg(args, append=True):
    mode = "ab" if append else "wb"
    with open(EVIDENCE, mode) as f:
        # rg exits 0 when matches found, 1 when none — a match means a dangling ref.
        return subprocess.run(["rg"] + args, cwd=REPO_ROOT, stdout=f,
                              stderr=subprocess.STDOUT).returncode == 0


if FORK == "cli":
    ENTRYPOINT = os.path.join(REPO_ROOT, "bin", "mini-ork")
    if not (os.path.isfile(ENTRYPOINT) and os.access(ENTRYPOINT, os.X_OK)):
        passed = False
        reasons.append(f"public CLI launcher is missing or not executable: {ENTRYPOINT}")
    else:
        launcher_err = None
        with open(EVIDENCE, "a") as ev:
            try:
                source = open(ENTRYPOINT, encoding="utf-8").read()
                if not source.startswith("#!/usr/bin/env python3\n"):
                    raise SystemExit("launcher does not have the Python shebang")
                for forbidden in ("MINI_ORK_RUNTIME", "runtime-select", "BASH_SOURCE", "source "):
                    if forbidden in source:
                        raise SystemExit(f"launcher retains Bash delegation token: {forbidden}")
                tree = ast.parse(source, filename=ENTRYPOINT)
                imports_cli = any(
                    isinstance(node, ast.ImportFrom)
                    and node.module == "mini_ork.cli.main"
                    and any(alias.name == "main" for alias in node.names)
                    for node in ast.walk(tree)
                )
                if not imports_cli:
                    raise SystemExit("launcher does not import mini_ork.cli.main.main")
                ev.write("CLI launcher is executable, Python-only, and delegates to the native dispatcher\n")
            except SystemExit as exc:
                launcher_err = str(exc)
                ev.write(f"{exc}\n")
            except Exception as exc:
                launcher_err = str(exc)
                ev.write(f"{type(exc).__name__}: {exc}\n")
        if launcher_err is not None:
            passed = False
            reasons.append("public CLI launcher is not Python-only — see verifier-fork-closure.log")

    if _rg(["-n", "--regexp", "_bin\\([^)]*['\"]cli['\"]|mini-ork-cli",
            "mini_ork", "tests", "scripts", "bin", "lib", "gates"]):
        passed = False
        reasons.append("suffixed or dynamic CLI runtime references remain — see verifier-fork-closure.log")
else:
    ENTRYPOINT = os.path.join(REPO_ROOT, "bin", f"mini-ork-{FORK}")
    NEEDLE = f"bin/mini-ork-{FORK}"
    DYNAMIC_PATTERN = ("_bin\\([^)]*['\"]" + FORK + "['\"]|['\"]bin['\"][[:space:]]*/[[:space:]]*['\"]mini-ork-"
                       + FORK + "['\"]")
    if os.path.exists(ENTRYPOINT):
        passed = False
        reasons.append(f"legacy entrypoint still exists: {ENTRYPOINT}")
    if search_roots and _rg(["-n", "--fixed-strings", "--hidden", "--glob", "!.git/**",
                             "--", NEEDLE] + search_roots, append=False):
        passed = False
        reasons.append(f"runtime references to {NEEDLE} remain — see verifier-fork-closure.log")
    if os.path.isdir(os.path.join(REPO_ROOT, "mini_ork")) and \
            _rg(["-n", "--regexp", DYNAMIC_PATTERN, "mini_ork", "tests", "scripts", "bin", "lib"]):
        passed = False
        reasons.append(f"dynamic runtime references for '{FORK}' remain — see verifier-fork-closure.log")

print(json.dumps({
    "name": "fork-closure",
    "fork": FORK,
    "pass": passed,
    "evidence": EVIDENCE,
    "reasons": reasons,
}))

sys.exit(0 if passed else 1)
