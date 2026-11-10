"""A type-aware lint must not refute code it never examined.

``parserOptions.project`` names a property of the TARGET repo's tsconfig. When
a touched file is outside the project it names — a new ``server/**`` tree the
config never covered — eslint aborts before running a single rule:

    Parsing error: ESLint was configured to run on `…/server/x.ts` using
    `parserOptions.project`: `./tsconfig.json`
    However, that TSConfig does not include this file.

Read as ``lint_rc != 0`` that is a CODE failure, and the implementer buys a
revise round for code no rule looked at (audit 2026-11-04,
``fix-eslint-project-paths``). Two halves here:

  1. the engine retries once over a scoped overlay tsconfig that extends the
     project's and includes the touched files, so the repo's own typed rules
     actually run over the change;
  2. when no overlay is possible (no root tsconfig, a file outside the repo, a
     flat config that has no ``--parser-options``) the verdict ABSTAINS —
     ``failure_kind: toolchain`` — instead of refuting.

The stub eslint is the falsifiable artifact: without ``--parser-options`` it
fails the way production did, with the overlay it passes and records the file
list it was handed.
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
TIER1 = REPO / "recipes" / "recursive-validate-impl" / "verifiers" / "tier1-compile-typecheck.py"

sys.path.insert(0, str(REPO))

from mini_ork.verify import pm  # noqa: E402

# Verbatim from eslint 8/9, so the classifier is tested on the real prose.
PROJECT_ERROR = (
    "Parsing error: ESLint was configured to run on "
    "`<tsconfigRootDir>/server/x.ts` using `parserOptions.project`: "
    "<tsconfigRootDir>/tsconfig.json\n"
    "However, that TSConfig does not include this file. Either:\n"
    "  - Change the file to be included in './tsconfig.json';\n"
    "  - Or, use the overrides section of your .eslintrc.* file;\n"
    "  - Or, disable the rule that requires parserOptions.project.\n"
)

# The stub fails on a bare run (production's behaviour) and passes when handed a
# scoped project that actually lists the file. It copies the overlay it was
# given, so the evidence log and the overlay bytes are checked, not trusted. The
# error prose lives in a file: quoting a message full of backticks into a shell
# script is how a "verbatim" fixture stops being verbatim.
STUB_ESLINT = """#!/bin/sh
project=""
for a in "$@"; do
  case "$a" in
    *project:*) project="${a##*project:}" ;;
  esac
done
if [ -z "$project" ]; then
  cat .eslint-project-error.txt
  exit 2
fi
if ! grep -q 'server/x.ts' "$project"; then
  echo "overlay did not include the touched file" >&2
  exit 2
fi
cp "$project" .eslint-overlay-seen.json
echo "lint clean (scoped project $project)"
exit 0
"""


def _stub(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _build(tmp_path: Path, *, tsconfig: bool = True, flat: bool = False,
           touched: str = "server/x.ts") -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "server").mkdir()
    (repo / "package.json").write_text(json.dumps({"name": "eslint-project-fixture"}),
                                       encoding="utf-8")
    if flat:
        (repo / "eslint.config.js").write_text("export default [];\n", encoding="utf-8")
    else:
        (repo / ".eslintrc.json").write_text(json.dumps({
            "root": True,
            "parserOptions": {"project": "./tsconfig.json"},
        }), encoding="utf-8")
    if tsconfig:
        (repo / "tsconfig.json").write_text(json.dumps({
            "compilerOptions": {"strict": True},
            "include": ["src/**/*"],
        }), encoding="utf-8")
    (repo / "src" / "a.ts").write_text("export const a = 1\n", encoding="utf-8")
    (repo / "server" / "x.ts").write_text("export const x = 1\n", encoding="utf-8")

    bindir = repo / "node_modules" / ".bin"
    bindir.mkdir(parents=True)
    (repo / ".eslint-project-error.txt").write_text(PROJECT_ERROR, encoding="utf-8")
    _stub(bindir / "eslint", STUB_ESLINT)

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "implementer-summary.json").write_text(json.dumps({
        "ready_for_tier1": True,
        "touched_files": [touched],
    }), encoding="utf-8")
    return repo, run_dir


def _run(repo: Path, run_dir: Path) -> tuple[int, dict, str]:
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["MINI_ORK_RUN_DIR"] = str(run_dir)
    proc = subprocess.run([sys.executable, str(TIER1)], cwd=repo, env=env,
                          capture_output=True, text=True, timeout=120)
    assert proc.stderr == "", proc.stderr
    lines = proc.stdout.splitlines()
    assert len(lines) == 1, proc.stdout
    return (proc.returncode, json.loads(lines[0]),
            (run_dir / "tier1-evidence.log").read_text())


# ── the fixture discriminates ───────────────────────────────────────────────


def test_the_bare_eslint_argv_is_red_on_this_fixture(tmp_path):
    """Pre-fix the engine ran exactly this: no project, and eslint aborted."""
    repo, _ = _build(tmp_path)
    proc = subprocess.run([str(repo / "node_modules" / ".bin" / "eslint"),
                           "server/x.ts"], cwd=repo, capture_output=True, text=True)
    assert proc.returncode == 2
    assert "does not include this file" in proc.stdout + proc.stderr


# ── the scoped-project retry ────────────────────────────────────────────────


def test_tier1_retries_over_a_scoped_project_and_passes(tmp_path):
    repo, run_dir = _build(tmp_path)
    rc, payload, evidence = _run(repo, run_dir)

    assert payload["pass"] is True, evidence
    assert payload["lint_rc"] == 0, evidence
    assert rc == 0
    assert "scoped project overlay" in evidence
    assert "lint clean (scoped project" in evidence
    # The overlay is a lint-time artifact, not a deliverable: it must not be
    # left in the target tree for the publisher to trip over.
    assert not list(repo.glob("mo-eslint-tsconfig-*"))


def test_the_overlay_extends_the_project_config_and_lists_the_touched_file(tmp_path):
    """The overlay is what makes the run meaningful: extending keeps aliases and
    strictness; `files` pins exactly what was linted. The stub copies the
    overlay it was handed, so this reads bytes rather than trusting the code."""
    repo, run_dir = _build(tmp_path)
    rc, payload, evidence = _run(repo, run_dir)

    assert payload["pass"] is True, evidence
    overlay = json.loads((repo / ".eslint-overlay-seen.json").read_text())
    assert overlay["extends"] == "./tsconfig.json"
    assert overlay["files"] == ["server/x.ts"]
    assert overlay["include"] == []
    assert rc == 0


def test_no_root_tsconfig_means_the_verdict_abstains(tmp_path):
    """Nothing to extend → no overlay → the error is a toolchain fact."""
    repo, run_dir = _build(tmp_path, tsconfig=False)
    rc, payload, evidence = _run(repo, run_dir)

    assert payload["pass"] is False
    assert payload["failure_kind"] == "toolchain", evidence
    assert "no scoped project overlay possible" in evidence
    assert rc == 1
    assert not list(repo.glob("mo-eslint-tsconfig-*"))


def test_a_flat_config_is_never_handed_parser_options(tmp_path):
    """`--parser-options` is eslintrc-mode only; ESLint 9 would reject the retry
    as an unknown option, so a flat-config repo abstains instead."""
    repo, run_dir = _build(tmp_path, flat=True)
    rc, payload, evidence = _run(repo, run_dir)

    assert payload["pass"] is False
    assert payload["failure_kind"] == "toolchain", evidence
    assert "--parser-options=project:" not in evidence
    assert "no scoped project overlay possible" in evidence
    assert rc == 1


# ── a refutation outranks an abstention ─────────────────────────────────────


def _build_typed_error(tmp_path: Path) -> tuple[Path, Path]:
    """The same repo, but its own typed rule DOES find something.

    The stub exits 1 with a real finding when the overlay is good, and fails the
    project-scope way (exit 2) when it is not — so one run exercises both
    classifiers at once.
    """
    repo, run_dir = _build(tmp_path)
    (repo / "node_modules" / ".bin" / "eslint").write_text(
        """#!/bin/sh
project=""
for a in "$@"; do
  case "$a" in
    *project:*) project="${a##*project:}" ;;
  esac
done
if [ -z "$project" ]; then
  cat .eslint-project-error.txt
  exit 2
fi
echo "server/x.ts"
echo "  3:1  error  Promises must be awaited  @typescript-eslint/no-floating-promises"
exit 1
""", encoding="utf-8")
    (repo / "node_modules" / ".bin" / "eslint").chmod(0o755)
    return repo, run_dir


def test_a_real_lint_finding_after_the_retry_is_a_code_failure(tmp_path):
    repo, run_dir = _build_typed_error(tmp_path)
    rc, payload, evidence = _run(repo, run_dir)

    assert payload["pass"] is False
    assert payload["lint_rc"] == 1, evidence
    assert payload["failure_kind"] == "code", evidence
    assert rc == 1


def test_a_typecheck_refutation_is_not_excused_by_an_eslint_abstention(tmp_path):
    """tsc judged the code and said no; eslint never started. The verdict is a
    refutation — `toolchain` would excuse the tsc error."""
    repo, run_dir = _build(tmp_path, tsconfig=False)
    (repo / "package.json").write_text(json.dumps({
        "name": "eslint-project-fixture",
        "scripts": {"type-check:touched": "tsc --noEmit"},
        # Declared so resolution picks npm — and our stub — over any real
        # manager installed on the host PATH.
        "devEngines": {"packageManager": {"name": "npm", "onFail": "error"}},
    }), encoding="utf-8")
    bindir = repo / "bin"
    bindir.mkdir()
    _stub(bindir / "npm", "#!/bin/sh\necho 'src/a.ts(1,14): error TS1005'\nexit 1\n")
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["MINI_ORK_RUN_DIR"] = str(run_dir)
    env["PATH"] = f"{bindir}{os.pathsep}{env.get('PATH', '')}"
    proc = subprocess.run([sys.executable, str(TIER1)], cwd=repo, env=env,
                          capture_output=True, text=True, timeout=120)
    payload = json.loads(proc.stdout.splitlines()[0])
    evidence = (run_dir / "tier1-evidence.log").read_text()

    assert payload["tsc_rc"] == 1, evidence
    assert payload["lint_rc"] == 2, evidence
    assert payload["failure_kind"] == "code", evidence
    assert proc.returncode == 1


# ── the classifier ──────────────────────────────────────────────────────────


def test_project_scope_error_matches_the_real_prose():
    assert pm.project_scope_error(PROJECT_ERROR)
    assert pm.project_scope_error(
        "error  Parsing error: was not found by the project service. "
        "Consider adding it to the 'files' array")
    assert pm.project_scope_error(
        "You have used a rule which requires parserOptions.project to be specified")


def test_a_typed_finding_is_still_a_code_failure():
    """Fail-closed: the classifier must not blanket-excuse eslint failures."""
    finding = ("server/x.ts\n"
               "  12:5  error  Promises must be awaited  "
               "@typescript-eslint/no-floating-promises\n")
    assert not pm.project_scope_error(finding)
    assert pm.classify_spawn(["eslint", "server/x.ts"], 1, finding) == ""
    assert pm.classify_spawn(["eslint", "server/x.ts"], 2, PROJECT_ERROR) == "toolchain"
