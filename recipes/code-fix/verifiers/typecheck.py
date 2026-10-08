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
#   MINI_ORK_TYPECHECK_CMD   explicit command to run (skips auto-detect AND
#                            scoping — the operator owns that command's scope)
#   MINI_ORK_TYPECHECK_FULL  "1" forces the unscoped whole-project run
#   MINI_ORK_HOME            path to .mini-ork/ dir (default: .mini-ork)
#   MINI_ORK_RUN_ID          current run id (used in log path)
#
# SCOPING (issue #4): an AUTO-DETECTED bare compiler (tsc / mypy) is narrowed
# to the run's touched files, because a whole-project run reddens every lane on
# a repo with pre-existing diagnostics — a phantom red the child did not cause.
# An operator-supplied MINI_ORK_TYPECHECK_CMD is run VERBATIM: only the operator
# knows that command's file-argument syntax. Such a command can scope itself by
# reading $MINI_ORK_TOUCHED_FILES (below). Outside a git work tree, nothing can
# be attributed, so the command runs unscoped — never silently skipped.
#
# Child env:
#   MINI_ORK_TOUCHED_FILES   newline-separated repo-relative paths this run
#                            changed (working tree ∪ untracked ∪ base...HEAD)

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import NamedTuple

try:
    # Late import — the verifier may be copied into a fixture without the rest
    # of mini_ork on PYTHONPATH (same seam as code-fix/verifiers/test.py).
    from mini_ork.verify.test_env import scrubbed_test_env
except Exception:                       # pragma: no cover — defensive only
    def scrubbed_test_env(environ=None):
        import os as _os
        env = dict(_os.environ if environ is None else environ)
        for k in list(env):
            if (k in {"MINI_ORK_SECRETS", "MINI_ORK_DB", "MINI_ORK_HOME",
                      "MINI_ORK_PROJECT_HOME", "MINI_ORK_RUN_ID", "MINI_ORK_RUN_DIR",
                      "MINI_ORK_PLAN_PATH", "MINI_ORK_AGENTS", "MO_TARGET_CWD"}
                    or k.endswith(("_API_KEY", "_AUTH_TOKEN", "_ACCESS_TOKEN",
                                   "_SECRET", "_SECRET_KEY"))
                    or k.startswith("ANTHROPIC_")
                    or k in {"OPENAI_API_BASE", "OPENAI_BASE_URL"}):
                env.pop(k)
        return env


def _child_env(environ=None):
    """Environment for the target repo's type-check command.

    ``scrubbed_test_env()`` strips provider credentials and live mini-ork
    state pointers but preserves ``MO_*`` lane keys by contract; drop those
    too so the child never sees the operator's lane configuration.
    """
    env = scrubbed_test_env(environ)
    for k in list(env):
        if k.startswith("MO_"):
            env.pop(k)
    return env


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


class _Detected(NamedTuple):
    """A detected command plus what the caller may do with its scope.

    ``tool`` is ``"tsc"``/``"mypy"`` only for the bare compilers this module
    discovered itself — the ones it can safely narrow to the touched files.
    Every other command is ``"full"``: its scope belongs to whoever wrote it.
    """
    cmd: str
    tool: str
    bin: str = ""


def detect_typecheck():
    # Explicit override wins — and is never rescoped (see module docstring).
    explicit = os.environ.get("MINI_ORK_TYPECHECK_CMD")
    if explicit:
        return _Detected(explicit, "full")

    # npm / pnpm / yarn — check package.json scripts first.
    if os.path.isfile("package.json"):
        scripts = _package_scripts()
        for candidate in _SCRIPT_CANDIDATES:
            if candidate in scripts:
                if shutil.which("pnpm"):
                    return _Detected(f"pnpm run {candidate}", "full")
                if shutil.which("npm"):
                    return _Detected(f"npm run {candidate}", "full")

    # TypeScript project marker required before we trust a tsc binary.
    if _has_ts_marker():
        if shutil.which("tsc"):
            return _Detected("tsc --noEmit", "tsc", "tsc")
        if os.path.isfile("./node_modules/.bin/tsc") and os.access("./node_modules/.bin/tsc", os.X_OK):
            return _Detected("./node_modules/.bin/tsc --noEmit", "tsc", "./node_modules/.bin/tsc")

    # Python mypy — require a configured mypy, not just any pyproject.toml.
    if shutil.which("mypy") and _has_mypy_marker():
        return _Detected("mypy .", "mypy", "mypy")

    # Rust
    if shutil.which("cargo") and os.path.isfile("Cargo.toml"):
        return _Detected("cargo check", "full")

    # Go
    if shutil.which("go") and os.path.isfile("go.mod"):
        return _Detected("go build ./...", "full")

    # Nothing found — skip and pass
    return _Detected("", "full")


def detect_typecheck_cmd() -> str:
    """The detected command only (compat wrapper around :func:`detect_typecheck`)."""
    return detect_typecheck().cmd


# ── touched-file scoping ─────────────────────────────────────────────────────
#
# A whole-project compiler run on a repo with pre-existing diagnostics reddens
# every lane for reasons the child did not cause, and the loop quarantines
# healthy fixes (observed live: a red main rolled back every lane). The gate is
# therefore narrowed to this run's own change surface.

_HEX_REF = re.compile(r"[0-9a-fA-F]{7,40}")
# Extensions each scoped tool can actually accept on its command line. A file of
# any other type would make the tool error on the *argument* rather than the
# code (``tsc --noEmit README.md`` is TS6054), i.e. a false red.
_SCOPE_EXT = {"tsc": (".ts", ".tsx", ".mts", ".cts"), "mypy": (".py", ".pyi")}
# Never part of a child's change surface.
_NOISE_PREFIX = (".mini-ork/",)


def _git(root: str, *args: str, timeout_s: int = 120) -> tuple[int, str]:
    """Run git without ever raising. Returns ``(rc, stdout)``."""
    try:
        proc = subprocess.run(
            ["git", "-C", root, *args], capture_output=True, text=True, timeout=timeout_s,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 127, ""
    return proc.returncode, proc.stdout or ""


def _is_git_worktree(root: str) -> bool:
    rc, out = _git(root, "rev-parse", "--is-inside-work-tree")
    return rc == 0 and out.strip() == "true"


def _run_dir_ref() -> str:
    """This run's pre-implementer ref, when the run dir carries one."""
    home = os.environ.get("MINI_ORK_HOME", "").strip()
    run_id = os.environ.get("MINI_ORK_RUN_ID", "").strip()
    if not (home and run_id):
        return ""
    try:
        ref = Path(home, "runs", run_id, "pre-implementer-ref").read_text(
            encoding="utf-8"
        ).strip()
    except OSError:
        return ""
    # A garbage ref must never reach git as a revision argument.
    return ref if _HEX_REF.fullmatch(ref) else ""


def _scoped_base(root: str) -> str:
    """The base the diff is taken against — the run's own start point first.

    ``merge-base HEAD origin/main`` is the LAST resort, not the default: once
    origin/main moves, ``origin/main...HEAD`` is the whole branch's divergence
    and drags in other sessions' files. The run dir's pre-implementer ref makes
    the diff this child's own; ``MO_GOAL_SCOPED_BASE`` covers callers with no
    run dir (the goal-loop binding's contract).
    """
    ref = _run_dir_ref() or os.environ.get("MO_GOAL_SCOPED_BASE", "").strip()
    if ref:
        return ref
    for candidate in ("origin/main", "main"):
        rc, out = _git(root, "merge-base", "HEAD", candidate)
        if rc == 0 and out.strip():
            return out.strip()
    return ""


def touched_files(root: str) -> list[str]:
    """Repo-relative paths this run changed: working tree, untracked, and
    (when a base resolves) everything committed on top of it."""
    paths: set[str] = set()
    rc, out = _git(root, "status", "--porcelain", "-uall")
    if rc == 0:
        for line in out.splitlines():
            if len(line) < 4:
                continue
            rest = line[3:]
            if " -> " in rest:  # rename: keep the destination
                rest = rest.split(" -> ", 1)[1]
            path = rest.strip().strip('"')
            if path:
                paths.add(path)
    base = _scoped_base(root)
    if base:
        rc, out = _git(root, "diff", "--name-only", f"{base}...HEAD")
        if rc == 0:
            paths.update(p.strip() for p in out.splitlines() if p.strip())
    return sorted(
        p for p in paths if p and not p.startswith(_NOISE_PREFIX)
    )


def _tsc_overlay(root: str, files: list[str]) -> str | None:
    """A generated tsconfig EXTENDING the project config, narrowed to ``files``.

    Passing files straight to ``tsc`` would bypass ``tsconfig.json`` entirely —
    no path aliases, no ``strict``, no ``lib`` — so every scoped run would
    report bogus errors and the scope would be worse than no scope. ``files``
    (not ``include``) carries the scope so the project's own ``exclude`` globs
    cannot silently drop a changed file into a vacuous "no inputs" run.

    Returns ``None`` when the project has no root ``tsconfig.json`` to extend,
    which makes the caller fall back to the unscoped run.
    """
    if not os.path.isfile(os.path.join(root, "tsconfig.json")):
        return None
    overlay = {
        "extends": "./tsconfig.json",
        "compilerOptions": {"noEmit": True, "incremental": False},
        "files": files,
        "include": [],
    }
    handle = tempfile.NamedTemporaryFile(
        "w", suffix=".json", prefix="mo-scoped-tsconfig-", dir=root, delete=False,
    )
    with handle:
        json.dump(overlay, handle)
    return handle.name


def _emit_pass(reason: str) -> int:
    print(json.dumps({
        "verifier": "typecheck", "pass": True, "evidence_path": None,
        "error_summary": reason,
    }, separators=(",", ":"), ensure_ascii=False))
    return 0


def _scoped_command(
    root: str, det: _Detected, touched: list[str]
) -> tuple[str | None, str, str | None]:
    """``(command, note, overlay_path)`` narrowed to this run's own change surface.

    ``command`` is ``None`` only when the scope is empty *and* git proved the
    run changed nothing this tool can see — a real "nothing to check", which
    the caller reports as a pass. It never returns ``None`` merely because
    scoping failed: an unscopeable tool runs unscoped instead (note ``""``).
    """
    keep = _SCOPE_EXT[det.tool]
    scoped = [f for f in touched if f.endswith(keep)]
    if not scoped:
        return None, f"no {det.tool} files among {len(touched)} touched", None
    if det.tool == "mypy":
        return (
            f"{det.bin} " + " ".join(shlex.quote(f) for f in scoped),
            f"scoped to {len(scoped)} touched file(s)",
            None,
        )
    overlay = _tsc_overlay(root, scoped)
    if overlay:
        return (
            f"{det.bin} --noEmit -p {shlex.quote(overlay)}",
            f"scoped to {len(scoped)} touched file(s)",
            overlay,
        )
    return None, "", None  # no tsconfig.json — unscoped run, never a skip


def main():
    det = detect_typecheck()

    if not det.cmd:
        sys.stderr.write("[typecheck] no typecheck command detected — skipping (pass)\n")
        return _emit_pass("no typecheck tool detected — skipped")

    root = os.path.realpath(os.getcwd())
    full = os.environ.get("MINI_ORK_TYPECHECK_FULL", "") == "1"
    touched = touched_files(root) if _is_git_worktree(root) else []

    cmd = det.cmd
    note = ""
    overlay_path = None
    if not full and det.tool in _SCOPE_EXT:
        scoped_cmd, note, overlay_path = _scoped_command(root, det, touched)
        if scoped_cmd is None:
            if note:  # genuinely nothing this tool can check
                sys.stderr.write(f"[typecheck] {note} — skipping (pass)\n")
                return _emit_pass(f"{note} — skipped")
            note = "no project tsconfig.json — running unscoped"
        else:
            cmd = scoped_cmd

    suffix = f" [{note}]" if note else ""
    sys.stderr.write(f"[typecheck] running: {cmd}{suffix}\n")
    child_env = _child_env()
    if touched:
        child_env["MINI_ORK_TOUCHED_FILES"] = "\n".join(touched)
    try:
        with open(LOG_PATH, "wb") as log:
            exit_code = subprocess.run(cmd, shell=True, stdout=log,
                                       stderr=subprocess.STDOUT,
                                       env=child_env).returncode
    finally:
        if overlay_path:
            try:
                os.unlink(overlay_path)
            except OSError:
                pass

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
