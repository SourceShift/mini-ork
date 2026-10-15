"""Package-manager resolution (`mini_ork.verify.pm`) and the guard that keeps
verifiers from hardcoding one again.

Background — the defect this file exists to prevent. Verifiers run with
``cwd`` = the TARGET repo, so the correct package manager is a property of
*that* repo. ``tier1-compile-typecheck.py`` shelled out to ``npx eslint``; the
researcher repo declares ``devEngines.packageManager = pnpm`` with
``onFail: "error"``, so **npm refused to start** (``EBADDEVENGINES``) before
eslint ran. That TOOLCHAIN failure was written as ``lint_rc: 1`` next to
``tsc_rc: 0`` and read as a CODE failure — cascading into 10 failed nodes and a
rollback of a correct implementation (run-1791732001-79120).

Three layers here:

1. Resolver unit tests — declaration beats lockfile beats PATH.
2. An **end-to-end regression** that runs the real tier1 verifier against a
   fixture repo declaring pnpm-only ``devEngines``, with stub ``pnpm``/``npx``
   and a stub ``node_modules/.bin/eslint`` that writes a marker. The marker is
   the falsifiable artifact: the fix reaches the repo's own binary, the old
   ``npx`` argv never does. ``test_the_old_npx_argv_is_red_on_this_fixture``
   proves the fixture discriminates rather than asserting it in prose.
3. A structural AST guard: no list literal in any verifier or engine module may
   begin with a package-manager name.
"""
from __future__ import annotations

import ast
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from mini_ork.verify import pm

REPO = Path(__file__).resolve().parents[2]
TIER1 = REPO / "recipes" / "recursive-validate-impl" / "verifiers" / "tier1-compile-typecheck.py"

# The real npm error, verbatim, so the stub npx fails the way production did.
EBADDEVENGINES = (
    'npm error code EBADDEVENGINES\n'
    'npm error Invalid name "pnpm" does not match "npm" for "packageManager"\n'
)


# ── layer 1: resolver unit tests ──────────────────────────────────────────


def _write(root: Path, name: str, content: str) -> None:
    root.joinpath(name).write_text(content, encoding="utf-8")


def test_declaration_devengines_beats_everything(tmp_path):
    """`devEngines.packageManager.name` is the repo's own statement — it wins."""
    _write(tmp_path, "package.json",
           json.dumps({"devEngines": {"packageManager": {"name": "pnpm", "version": "11.0.9"}}}))
    # A package-lock.json is present and would otherwise say npm.
    (tmp_path / "package-lock.json").write_text("{}")
    assert pm.declared_manager(str(tmp_path)) == "pnpm"
    assert pm.package_manager(str(tmp_path)) == "pnpm"


def test_declaration_package_manager_field(tmp_path):
    _write(tmp_path, "package.json", json.dumps({"packageManager": "yarn@4.1.0"}))
    assert pm.declared_manager(str(tmp_path)) == "yarn"


def test_lockfile_used_when_undeclared(tmp_path):
    _write(tmp_path, "package.json", json.dumps({"name": "x"}))
    (tmp_path / "pnpm-lock.yaml").write_text("lockfileVersion: 9\n")
    assert pm.declared_manager(str(tmp_path)) == ""
    assert pm.lockfile_manager(str(tmp_path)) == "pnpm"
    assert pm.package_manager(str(tmp_path)) == "pnpm"


def test_path_fallback_then_npm_last(tmp_path):
    """No declaration, no lockfile → first of pnpm/yarn/bun/npm on PATH."""
    _write(tmp_path, "package.json", json.dumps({"name": "x"}))
    resolved = pm.package_manager(str(tmp_path))
    assert resolved in pm.MANAGERS


def test_absent_package_json_resolves_without_raising(tmp_path):
    """A non-node repo must not explode — the caller decides what to do."""
    assert pm.declared_manager(str(tmp_path)) == ""
    assert pm.lockfile_manager(str(tmp_path)) == ""
    assert pm.package_manager(str(tmp_path)) in pm.MANAGERS


def test_malformed_package_json_is_treated_as_absent(tmp_path):
    _write(tmp_path, "package.json", "{not json")
    assert pm.declared_manager(str(tmp_path)) == ""


def test_unknown_devengines_name_is_not_honoured(tmp_path):
    """A name outside MANAGERS is not a manager we can run."""
    _write(tmp_path, "package.json",
           json.dumps({"devEngines": {"packageManager": {"name": "volta"}}}))
    assert pm.declared_manager(str(tmp_path)) == ""


def test_exec_argv_prefers_the_repo_own_binary(tmp_path):
    """node_modules/.bin/<tool> is the pinned binary — preferred over any manager."""
    bindir = tmp_path / "node_modules" / ".bin"
    bindir.mkdir(parents=True)
    tool = bindir / "eslint"
    tool.write_text("#!/bin/sh\nexit 0\n")
    tool.chmod(tool.stat().st_mode | stat.S_IXUSR)
    _write(tmp_path, "package.json",
           json.dumps({"devEngines": {"packageManager": {"name": "pnpm"}}}))
    argv = pm.exec_argv(str(tmp_path), "eslint", "a.ts")
    assert argv[0] == str(tool)
    assert argv[1:] == ["a.ts"]


def test_exec_argv_falls_back_to_the_declared_manager(tmp_path, monkeypatch):
    """No vendored binary → the DECLARED manager, never a default.

    ``shutil.which`` is pinned so the declared manager reads as *installed*.
    Unpinned, the result depends on the host PATH — a CI box with no pnpm falls
    through to the bare ``[tool]`` argv, which is what made this assertion
    environment-dependent. The pin also lets the assert be exact: the declared
    manager wins, via its ``exec`` subcommand.
    """
    _write(tmp_path, "package.json",
           json.dumps({"devEngines": {"packageManager": {"name": "pnpm"}}}))
    monkeypatch.setattr(
        pm.shutil, "which",
        lambda name: "/usr/bin/pnpm" if name == "pnpm" else None,
    )
    argv = pm.exec_argv(str(tmp_path), "eslint", "a.ts")
    assert argv == ["pnpm", "exec", "eslint", "a.ts"]


def test_run_argv_forwards_npm_args_after_a_separator(tmp_path):
    """npm swallows trailing flags unless they follow `--`; pnpm/yarn/bun don't."""
    _write(tmp_path, "package.json",
           json.dumps({"devEngines": {"packageManager": {"name": "npm"}}}))
    argv = pm.run_argv(str(tmp_path), "test:server", "--runTestsByPath", "a.test.ts")
    assert argv == ["npm", "run", "test:server", "--", "--runTestsByPath", "a.test.ts"]


def test_run_argv_does_not_add_a_separator_for_pnpm(tmp_path):
    _write(tmp_path, "package.json",
           json.dumps({"devEngines": {"packageManager": {"name": "pnpm"}}}))
    argv = pm.run_argv(str(tmp_path), "type-check:touched", "a.ts")
    assert argv == ["pnpm", "run", "type-check:touched", "a.ts"]


def test_runnable_manager_is_empty_for_an_uninstalled_manager(tmp_path):
    """`run_argv` always returns an argv; DETECTION must gate on this instead."""
    _write(tmp_path, "package.json",
           json.dumps({"devEngines": {"packageManager": {"name": "pnpm"}}}))
    assert pm.package_manager(str(tmp_path)) == "pnpm"
    assert pm.runnable_manager(str(tmp_path)) in ("pnpm", "")


def test_describe_names_both_inputs(tmp_path):
    _write(tmp_path, "package.json",
           json.dumps({"devEngines": {"packageManager": {"name": "pnpm"}}}))
    line = pm.describe(str(tmp_path))
    assert "declared=pnpm" in line and "resolved=pnpm" in line


# ── layer 1b: spawn-failure provenance ────────────────────────────────────


def test_classify_spawn_toolchain_on_a_manager_refusal():
    """The production shape: the manager refused to start the tool."""
    assert pm.classify_spawn(["pnpm", "run", "type-check:touched"], 1, EBADDEVENGINES) == "toolchain"


def test_classify_spawn_toolchain_on_missing_binary():
    assert pm.classify_spawn(["eslint"], 127, "") == "toolchain"          # sh's own ee
    assert pm.classify_spawn(["eslint"], 1, "sh: eslint: command not found") == "toolchain"
    assert pm.classify_spawn(["node", "x.js"], 1, "Error: spawn ENOENT") == "toolchain"


def test_classify_spawn_is_fail_closed():
    """The tool ran and rejected the input is NOT our call — "" keeps today's
    behaviour (the caller maps it to `code`). Nothing here abstains."""
    assert pm.classify_spawn(["eslint", "a.ts"], 1, "a.ts:3:1 error Unexpected var") == ""
    assert pm.classify_spawn(["eslint", "a.ts"], 0, "") == ""


# ── layer 2: end-to-end regression on the real verifier ───────────────────


def _build_fixture(tmp_path: Path) -> tuple[Path, Path]:
    """A repo that declares pnpm-only devEngines, plus a run dir.

    Returns ``(repo, run_dir)``. The repo carries:
    * ``bin/pnpm``  — stub, exits 0 (the declared manager, and it works)
    * ``bin/npx``   — stub, prints EBADDEVENGINES and exits 1 (what production did)
    * ``node_modules/.bin/eslint`` — stub that writes ``eslint-was-run`` and exits 0
    """
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "bin").mkdir()

    (repo / "package.json").write_text(json.dumps({
        "name": "pnpm-only-fixture",
        "scripts": {"type-check:touched": "tsc --noEmit"},
        "devEngines": {"packageManager": {"name": "pnpm", "version": "11.0.9",
                                          "onFail": "error"}},
    }), encoding="utf-8")
    (repo / ".eslintrc.json").write_text('{"root": true}\n', encoding="utf-8")
    (repo / "src" / "a.ts").write_text("export const a = 1\n", encoding="utf-8")

    _stub(repo / "bin" / "pnpm", "#!/bin/sh\nexit 0\n")
    _stub(repo / "bin" / "npx", f"#!/bin/sh\nprintf '%s' '{EBADDEVENGINES}' >&2\nexit 1\n")

    bindir = repo / "node_modules" / ".bin"
    bindir.mkdir(parents=True)
    _stub(bindir / "eslint",
          "#!/bin/sh\n: > \"$(dirname \"$0\")/../../eslint-was-run\"\nexit 0\n")

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "implementer-summary.json").write_text(json.dumps({
        "ready_for_tier1": True,
        "touched_files": ["src/a.ts"],
    }), encoding="utf-8")
    return repo, run_dir


def _stub(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _run_verifier(repo: Path, run_dir: Path) -> tuple[int, dict, str]:
    """Run the real tier1 verifier the way `_run_verifier_ref` does: cwd=target
    repo, PATH from the fixture. PYTHONPATH is deliberately NOT set, so the
    verifier's own walk-up bootstrap is what makes `import mini_ork` resolve."""
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["MINI_ORK_RUN_DIR"] = str(run_dir)
    env["PATH"] = f"{repo / 'bin'}{os.pathsep}{env.get('PATH', '')}"
    proc = subprocess.run([sys.executable, str(TIER1)], cwd=repo, env=env,
                          capture_output=True, text=True, timeout=120)
    assert proc.stderr == "", proc.stderr
    lines = proc.stdout.splitlines()
    assert len(lines) == 1, proc.stdout
    return proc.returncode, json.loads(lines[0]), (run_dir / "tier1-evidence.log").read_text()


def test_tier1_reaches_the_repo_own_eslint_on_a_pnpm_only_repo(tmp_path):
    """THE REGRESSION. Pre-fix this verifier ran `npx eslint` → EBADDEVENGINES."""
    repo, run_dir = _build_fixture(tmp_path)
    rc, payload, evidence = _run_verifier(repo, run_dir)

    assert payload["lint_rc"] == 0, evidence
    assert payload["tsc_rc"] == 0, evidence
    assert payload["pass"] is True, evidence
    assert rc == 0

    # The marker proves the REPO'S eslint ran — not a manager shim, not npx.
    assert (repo / "eslint-was-run").is_file(), evidence
    # And the evidence log says which command it was, so this class of failure
    # is a 5-second diagnosis from the log alone.
    assert "node_modules/.bin/eslint" in evidence
    assert "package manager:" in evidence


def test_tier1_emits_toolchain_failure_kind_when_the_manager_refuses(tmp_path):
    """The manager itself refuses (EBADDEVENGINES): the tool never started, so
    the failure carries ``failure_kind: toolchain`` — the provenance the abstain
    edge reads. Pre-fix this was a bare ``tsc_rc: 1`` read as a code failure."""
    repo, run_dir = _build_fixture(tmp_path)
    # The DECLARED manager refuses to run the script, exactly as npm did.
    _stub(repo / "bin" / "pnpm",
          f"#!/bin/sh\nprintf '%s' '{EBADDEVENGINES}' >&2\nexit 1\n")
    rc, payload, evidence = _run_verifier(repo, run_dir)

    assert payload["pass"] is False, evidence
    assert rc == 1
    assert payload["tsc_rc"] == 1, evidence
    assert payload["failure_kind"] == "toolchain", evidence


def test_the_old_npx_argv_is_red_on_this_fixture(tmp_path):
    """Falsification: the same fixture MUST fail under the pre-fix argv.

    If this test ever passes while the one above passes, the fixture has stopped
    discriminating and the regression guard above is worthless.
    """
    repo, _ = _build_fixture(tmp_path)
    proc = subprocess.run(["npx", "eslint", "src/a.ts"], cwd=repo,
                          capture_output=True, text=True,
                          env={**os.environ, "PATH": f"{repo / 'bin'}{os.pathsep}{os.environ['PATH']}"})
    assert proc.returncode != 0
    assert "EBADDEVENGINES" in proc.stderr
    assert not (repo / "eslint-was-run").is_file()


# ── layer 3: structural guard ─────────────────────────────────────────────


def _manager_leading_lists(path: Path) -> list[tuple[int, str]]:
    """Line numbers of list literals whose first element is a disallowed head.

    ``npx`` counts: it is not a package manager but it impersonates the ambient
    npm and trips the same `devEngines` guard — it is the head the production
    defect actually used.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError):  # pragma: no cover — unparseable is not a hit
        return []
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.List) and node.elts:
            head = node.elts[0]
            if (isinstance(head, ast.Constant) and isinstance(head.value, str)
                    and head.value in pm.DISALLOWED_ARGV_HEADS):
                hits.append((node.lineno, head.value))
    return hits


def _guardable_files() -> list[Path]:
    files = sorted((REPO / "mini_ork").rglob("*.py"))
    files += sorted((REPO / "recipes").glob("*/verifiers/*.py"))
    return [f for f in files if "__pycache__" not in f.parts]


def test_no_verifier_hardcodes_a_package_manager():
    """A verifier runs in the TARGET repo — naming a manager is the defect.

    Resolution must go through `mini_ork.verify.pm`; only the defensive
    fallbacks inside `try/except ImportError` may name one, and those build
    their argv from a variable head, so they do not trip this.
    """
    offenders = []
    for path in _guardable_files():
        for lineno, name in _manager_leading_lists(path):
            offenders.append(f"{path.relative_to(REPO)}:{lineno} -> [{name!r}, ...]")
    assert not offenders, (
        "hardcoded package-manager argv found (use mini_ork.verify.pm):\n  "
        + "\n  ".join(offenders)
    )


def test_the_structural_guard_catches_a_real_offender(tmp_path):
    """Falsify the guard itself before trusting it."""
    bad = tmp_path / "bad_verifier.py"
    bad.write_text('import subprocess\nsubprocess.run(["npx", "eslint", "a.ts"])\n',
                   encoding="utf-8")
    assert _manager_leading_lists(bad) == [(2, "npx")]


@pytest.mark.parametrize("manager", pm.MANAGERS)
def test_resolver_recognises_every_manager_it_claims(tmp_path, manager):
    _write(tmp_path, "package.json", json.dumps({"packageManager": f"{manager}@1.0.0"}))
    assert pm.declared_manager(str(tmp_path)) == manager


_ESLINT8 = ("#!/bin/sh\n"
            "for f in \"$@\"; do\n"
            "  [ -e \"$f\" ] || { echo \"No files matching the pattern \\\"$f\\\" were found.\"; exit 2; }\n"
            "done\n"
            ": > \"$(dirname \"$0\")/../../eslint-was-run\"\n"
            "exit 0\n")


def test_tier1_does_not_hand_a_deleted_file_to_eslint(tmp_path):
    """2026-10-15 m3a-run-log-reader-removal: a removal lists the deleted file in
    touched_files; ESLint 8 aborts with exit 2 on a path that does not exist, so
    the deliverable could never pass tier1 and its revise round could not help."""
    repo, run_dir = _build_fixture(tmp_path)
    _stub(repo / "node_modules" / ".bin" / "eslint", _ESLINT8)
    (run_dir / "implementer-summary.json").write_text(json.dumps({
        "ready_for_tier1": True,
        "touched_files": ["src/a.ts", "src/removed.test.ts"],
    }), encoding="utf-8")

    rc, payload, evidence = _run_verifier(repo, run_dir)

    assert payload["lint_rc"] == 0, evidence
    assert payload["pass"] is True, evidence
    assert rc == 0
    assert (repo / "eslint-was-run").is_file(), evidence
    assert "deleted_files=1" in evidence
    assert "deleted: src/removed.test.ts" in evidence
