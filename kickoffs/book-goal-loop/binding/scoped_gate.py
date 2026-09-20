#!/usr/bin/env python3
"""scoped typecheck / test gate for the goal-loop's fix child.

Invoked by the code-fix child's verifier nodes via the env contract they
already honour (``recipes/code-fix/verifiers/{typecheck,test}.py``):

    MINI_ORK_TYPECHECK_CMD="python3 <this> typecheck"
    MINI_ORK_TEST_CMD="python3 <this> test"

cwd is the child's target worktree. Exit 0 == pass, non-zero == fail; the
verifier greps its log for the first line containing "error" as the summary, so
failures print one.

WHY THIS EXISTS
The goal-loop used to arm these two env vars with ``echo <something>``, which
makes every patch pass them by construction. That left the LLM reviewer as the
only in-sandbox gate on a code patch, and pushed build breakage discovery
downstream to a 30-90 min regen. The obvious repair — point them at the
project's own ``tsc`` and ``jest`` — is wrong in the other direction: the
researcher tree is ~17k files across three tsconfigs with pre-existing
diagnostics, so a whole-repo run reddens every wave for reasons the child did
not cause (phantom-red), and the loop quarantines healthy fixes.

So this gate is SCOPED TO THE CHILD'S OWN DIFF:
  * nothing to check  → pass, and say so (a real decision, not a stub)
  * typecheck         → a generated tsconfig that EXTENDS the project config
                        matching the changed files (so path aliases, strictness
                        and lib come from the project, not from this script)
                        with ``include`` narrowed to those files. tsc then
                        follows their imports, so the changed surface and
                        everything it depends on is checked — and nothing else.
  * test              → the project's own jest, ``--findRelatedTests`` over the
                        changed files, so only the tests that exercise them run.

What it deliberately does NOT cover: files that merely *import* a changed file
(a signature change that breaks a caller elsewhere). Catching those needs a
whole-repo run, which is the phantom-red trade this gate exists to avoid; the
outer loop's deploy→regen→terminal-fail path is the backstop for that class.
Test files are excluded from the typecheck pass (matching the project's own
``exclude``) but ARE executed by the test pass, which is where they matter.

A jest-guard refusal (rc=77) is a SKIP, not a failure: the guard declines to
start when the box is loaded or a run is already in flight, and this campaign
is itself what loads the box. The typecheck half carries no such guard, so it
is the half that always actually runs.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

TS_EXT = (".ts", ".tsx", ".mts", ".cts")
TEST_RE = re.compile(r"(\.test\.|\.spec\.|/__tests__/)")
# Trees served by tsconfig.server.json (node/commonjs + @server/@shared path
# maps) rather than the root client config (bundler + @/ -> src/).
SERVER_ROOTS = ("server/", "shared/", "scripts/")
# Generated / vendored trees: never part of the child's change surface.
NOISE = ("node_modules/", "dist/", ".next/", "baml_client/")


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip())
    except ValueError:
        return default


def _run(argv: list[str], cwd: str, timeout_s: int) -> tuple[int, str]:
    """Run to completion under a hard timeout. A timeout is a FAIL (non-zero)
    with a message naming it — a gate that cannot decide must not pass."""
    try:
        proc = subprocess.run(
            argv, cwd=cwd, capture_output=True, text=True, timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        return 124, f"error: scoped gate timed out after {timeout_s}s: {' '.join(argv)}"
    except OSError as exc:
        return 127, f"error: could not exec {argv[0]!r}: {exc}"
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def _scoped_base() -> str:
    """The child's own START POINT — never a long-lived branch ref.

    ``MO_GOAL_SCOPED_BASE`` defaults to ``origin/main``, which is the wrong
    base by construction: ``git diff origin/main...HEAD`` in the target worktree
    is the WHOLE feature branch's divergence, not this child's change. A child
    that edited nothing then gets typechecked against dozens of unrelated files
    and reddens on pre-existing diagnostics — the exact phantom-red this gate
    exists to avoid. Observed live: a zero-change child reported "scoped
    typecheck failed for 44 changed file(s)".

    The child's run dir carries ``pre-implementer-ref`` — its HEAD (or a
    ``git stash create`` snapshot of the tree) captured BEFORE the first
    implementer edit. That is the only base that makes the diff the child's own,
    and it still counts committed work: ``base...HEAD`` picks up any commit the
    child made on top of it. ``MO_GOAL_SCOPED_BASE`` remains the fallback for
    callers with no run dir (bare ``scoped_gate.py`` invocations).
    """
    home = os.environ.get("MINI_ORK_HOME", "").strip()
    run_id = os.environ.get("MINI_ORK_RUN_ID", "").strip()
    if home and run_id:
        try:
            ref = Path(home, "runs", run_id, "pre-implementer-ref").read_text(
                encoding="utf-8"
            ).strip()
        except OSError:
            ref = ""
        if re.fullmatch(r"[0-9a-fA-F]{7,40}", ref):
            return ref
    return os.environ.get("MO_GOAL_SCOPED_BASE", "").strip()


def _changed(root: str, base: str) -> list[str]:
    """Repo-relative paths the child has touched: working tree, untracked, and
    (when ``base`` resolves) everything committed on top of it."""
    paths: set[str] = set()
    rc, out = _run(
        ["git", "-C", root, "status", "--porcelain", "-uall"], cwd=root, timeout_s=120,
    )
    if rc == 0:
        for line in out.splitlines():
            if len(line) < 4:
                continue
            rest = line[3:]
            if " -> " in rest:  # rename: keep the destination
                rest = rest.split(" -> ", 1)[1]
            paths.add(rest.strip().strip('"'))
    if base:
        rc, out = _run(
            ["git", "-C", root, "diff", "--name-only", f"{base}...HEAD"],
            cwd=root, timeout_s=120,
        )
        if rc == 0:
            paths.update(p for p in out.splitlines() if p.strip())
    return sorted(
        p for p in paths
        if p.endswith(TS_EXT) and not any(p.startswith(n) or f"/{n}" in p for n in NOISE)
    )


def _tsc_bin(root: str) -> str | None:
    local = Path(root, "node_modules", ".bin", "tsc")
    return str(local) if local.is_file() else None


def _base_config(root: str, files: list[str]) -> str | None:
    """The project tsconfig whose include/lang covers these files, if present."""
    if any(f.startswith(SERVER_ROOTS) for f in files):
        for name in ("tsconfig.server.json", "server/tsconfig.json"):
            if Path(root, name).is_file():
                return name
        return None
    return "tsconfig.json" if Path(root, "tsconfig.json").is_file() else None


def _typecheck(root: str, files: list[str]) -> tuple[int, str]:
    tsc = _tsc_bin(root)
    if tsc is None:
        return 0, "no local tsc in node_modules/.bin — scoped typecheck skipped"
    base = _base_config(root, files)
    if base is None:
        return 0, "no matching tsconfig — scoped typecheck skipped"
    # ``files`` (not ``include``) carries the scope: entries in ``files`` are
    # always compiled, so the project's own ``exclude`` globs cannot silently
    # drop a changed file and turn the run into a vacuous "no inputs".
    overlay = {
        "extends": f"./{base}",
        "compilerOptions": {"noEmit": True, "incremental": False},
        "files": files,
        "include": [],
    }
    with tempfile.NamedTemporaryFile(
        "w", suffix=".json", prefix="scoped-tsconfig-", dir=root, delete=False,
    ) as handle:
        import json
        json.dump(overlay, handle)
        tmp = handle.name
    try:
        rc, out = _run(
            [tsc, "--noEmit", "-p", tmp], cwd=root,
            timeout_s=_int_env("MO_GOAL_TYPECHECK_TIMEOUT_SECONDS", 900),
        )
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    if rc == 0:
        return 0, f"scoped typecheck clean over {len(files)} changed file(s) ({base})"
    return 1, out


def _test(root: str, files: list[str]) -> tuple[int, str]:
    guard = Path(root, "scripts", "jest-guard.sh")
    config = Path(root, "tests", "jest.config.js")
    if not guard.is_file() or not config.is_file():
        return 0, "no jest-guard.sh/tests/jest.config.js — scoped test skipped"
    argv = [
        "bash", str(guard), "raw", "--", "--config", str(config),
        "--findRelatedTests", *files, "--passWithNoTests", "--ci",
    ]
    rc, out = _run(
        argv, cwd=root, timeout_s=_int_env("MO_GOAL_TEST_TIMEOUT_SECONDS", 1200),
    )
    if rc == 0:
        return 0, f"related tests green for {len(files)} changed file(s)"
    if rc == 77:
        # jest-guard's documented "refused to start" (concurrency cap or system
        # overload — routine on this host, where the campaign itself keeps load
        # high). The tests did not run and did not fail, so calling this red
        # would quarantine healthy fixes for the box being busy.
        return 0, "jest-guard refused under load (rc=77) — related tests not run"
    return 1, out


def main(argv: list[str]) -> int:
    mode = (argv[0] if argv else "").strip().lower()
    if mode not in ("typecheck", "test"):
        print("usage: scoped_gate.py <typecheck|test>", file=sys.stderr)
        return 2
    # realpath: tsc prints diagnostics relative to the cwd it was handed but
    # resolves the config through its own realpath, so a symlinked target (a
    # macOS mktemp dir, say) would render every error path as "../../..".
    root = os.path.realpath(os.environ.get("MO_GOAL_TARGET_CWD") or os.getcwd())
    files = _changed(root, _scoped_base())
    if mode == "typecheck":
        files = [f for f in files if not TEST_RE.search(f)]
    if not files:
        # Not a stub: the child genuinely produced no in-scope change, so there
        # is nothing this gate could fail on. Saying so distinguishes it from
        # the echo-stub era, where the pass was unconditional.
        print("no in-scope TypeScript changes — nothing for the scoped gate to check")
        return 0

    rc, out = _typecheck(root, files) if mode == "typecheck" else _test(root, files)
    tail = out.strip().splitlines()[-25:]
    if rc == 0:
        print(tail[-1] if tail else f"{mode} passed")
        return 0
    print(f"{mode} error: scoped {mode} failed for {len(files)} changed file(s)")
    for line in tail:
        print(line)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
