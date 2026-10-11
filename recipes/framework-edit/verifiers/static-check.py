#!/usr/bin/env python3
# verifiers/static-check.py — run static checks over framework-edit.diff.
#
# Python port of static-check.sh (bash-removal WS8). Same checks, evidence
# text, JSON schema, and rc semantics.
#
# Inputs (via env):
#   MINI_ORK_RUN_DIR — run directory set by the native execute runtime
#   MO_TARGET_CWD    — the run's TARGET repo/worktree (the repo under test);
#                      preferred over MINI_ORK_ROOT, which names the ENGINE
#   MINI_ORK_ROOT    — engine repo root (fallback when MO_TARGET_CWD is unset)
#
# Output: JSON to stdout. Exit code is always 0; caller reads .pass.

import hashlib
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# mini-ork's verifiers are standalone scripts. The executor exports PYTHONPATH
# (`mini_ork/cli/execute.py`, _run_verifier_ref), but walk up to the engine root
# anyway so a hand-run or a caller that forgets it still imports.
_engine = os.path.dirname(os.path.realpath(__file__))
while not os.path.isdir(os.path.join(_engine, "mini_ork")) and _engine != os.path.dirname(_engine):
    _engine = os.path.dirname(_engine)
if _engine not in sys.path:
    sys.path.insert(0, _engine)
from _verdict_merge import write_verdict  # noqa: E402
from mini_ork.verify.pm import run_argv  # noqa: E402

RUN_DIR = os.environ["MINI_ORK_RUN_DIR"]


def _target_repo_root():
    """The repo UNDER TEST — the run's target worktree, not the engine.

    ``MINI_ORK_ROOT`` names the engine the run executes from. On a worktree-first
    run that is a DIFFERENT repo from the target whose tree framework-edit.diff
    was cut against (``MO_TARGET_CWD``). Archiving the engine silently omits
    paths the target still has — notably a path the engine has since converted to
    a git submodule, which ``git archive`` renders as an empty directory — so a
    diff valid against the target cannot apply to the copy. Prefer the target."""
    for key in ("MO_TARGET_CWD", "MINI_ORK_TARGET_REPO"):
        val = os.environ.get(key)
        if val and os.path.isdir(val):
            return val
    return os.environ.get("MINI_ORK_ROOT") or os.getcwd()


def _base_ref():
    """The commit the diff was cut against (``pre-implementer-ref``).

    framework-edit commits the implementer's patch, so a re-verify after that
    commit sees a HEAD that ALREADY contains the diff; archiving HEAD then makes
    the diff read as 'already applied' and fail. Archive the recorded base so the
    throwaway copy is always the tree the diff applies onto. Falls back to HEAD
    when no recorded base resolves."""
    try:
        with open(os.path.join(RUN_DIR, "pre-implementer-ref"), encoding="utf-8") as fh:
            ref = fh.read().strip()
    except OSError:
        ref = ""
    if ref and subprocess.run(["git", "-C", REPO_ROOT, "cat-file", "-e", f"{ref}^{{commit}}"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
        return ref
    return "HEAD"


REPO_ROOT = _target_repo_root()
NAME = "static-check"
DIFF = os.path.join(RUN_DIR, "framework-edit.diff")
EVIDENCE = os.path.join(RUN_DIR, f"verifier-{NAME}.log")
CHECKS_TSV = os.path.join(RUN_DIR, f"verifier-{NAME}.checks.tsv")
CHANGED = os.path.join(RUN_DIR, f"verifier-{NAME}.changed-files")
WORK_PARENT = os.path.join(RUN_DIR, f"verifier-{NAME}-work")
WORKTREE = os.path.join(WORK_PARENT, "repo")

open(CHECKS_TSV, "w").close()
open(CHANGED, "w").close()
_ev = open(EVIDENCE, "w")
_tsv = open(CHECKS_TSV, "a")


def _check(cid, desc, fn):
    _ev.write(f"[{cid}] {desc}\n")
    _ev.flush()
    try:
        ok = bool(fn())
    except Exception:
        ok = False
    _tsv.write(f"{cid}\t{desc}\t{'true' if ok else 'false'}\n")
    _tsv.flush()
    _ev.write("  ok\n" if ok else "  FAIL\n")
    _ev.flush()


def _run(argv, cwd=None, shell=False):
    """Run a command with output to the evidence log; return rc == 0."""
    if shell:
        return subprocess.run(argv, shell=True, cwd=cwd, stdout=_ev,
                              stderr=subprocess.STDOUT).returncode == 0
    _ev.write(f"$ {shlex.join(argv)}\n")
    _ev.flush()
    return subprocess.run(argv, cwd=cwd, stdout=_ev,
                          stderr=subprocess.STDOUT).returncode == 0


def _changed_files():
    """git apply --numstat | awk '{print $3}' | sed '/^$/d' | sort -u > CHANGED"""
    r = subprocess.run(["git", "-C", REPO_ROOT, "apply", "--numstat", DIFF],
                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    names = set()
    for line in r.stdout.decode("utf-8", "replace").splitlines():
        parts = line.split("\t") if "\t" in line else line.split()
        if len(parts) >= 3 and parts[2]:
            names.add(parts[2])
    with open(CHANGED, "w") as f:
        for n in sorted(names):
            f.write(n + "\n")


def _diff_sentinel_path():
    try:
        with open(DIFF, encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.startswith("+++ b/"):
                    return line[len("+++ b/"):].rstrip("\n")
    except OSError:
        pass
    return ""


def _assert_copy_is_own_git_root():
    subprocess.run(["git", "-C", WORKTREE, "init"], stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL)
    top = os.path.realpath(WORKTREE)
    r = subprocess.run(["git", "-C", WORKTREE, "rev-parse", "--show-toplevel"],
                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    return r.returncode == 0 and r.stdout.decode().strip() == top


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _write_apply_sentinels():
    with open(os.path.join(WORK_PARENT, "diff-applied.sha256"), "w") as f:
        f.write(f"{_sha256_file(DIFF)}  {DIFF}\n")
    r = subprocess.run("git diff --no-index /dev/null . || true", shell=True,
                       cwd=WORKTREE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    post = hashlib.sha256(r.stdout).hexdigest()
    with open(os.path.join(WORK_PARENT, "diff-applied.post.sha256"), "w") as f:
        f.write(f"{post}  -\n")
    return (os.path.getsize(os.path.join(WORK_PARENT, "diff-applied.sha256")) > 0
            and os.path.getsize(os.path.join(WORK_PARENT, "diff-applied.post.sha256")) > 0)


def _make_patched_copy():
    shutil.rmtree(WORK_PARENT, ignore_errors=True)
    os.makedirs(WORKTREE)
    archive = subprocess.run(["git", "-C", REPO_ROOT, "archive", _base_ref()],
                             stdout=subprocess.PIPE, check=True).stdout
    tarfile.open(fileobj=io.BytesIO(archive)).extractall(WORKTREE)
    if not _assert_copy_is_own_git_root():
        return False
    sentinel = _diff_sentinel_path()
    if not sentinel:
        return False
    if subprocess.run(["git", "-C", WORKTREE, "apply", DIFF],
                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode != 0:
        return False
    if not os.path.exists(os.path.join(WORKTREE, sentinel)):
        return False
    return _write_apply_sentinels()


def _grep_file(pattern, path, flags=0):
    try:
        return re.search(pattern, open(path, encoding="utf-8", errors="replace").read(), flags) is not None
    except OSError:
        return False


# Template tier: declared artifacts exist and have basic shape.
_check("artifact-diff-exists", "framework-edit.diff exists", lambda: os.path.isfile(DIFF))
_check("artifact-diff-non-empty", "framework-edit.diff is non-empty",
       lambda: os.path.isfile(DIFF) and os.path.getsize(DIFF) > 0)
_check("artifact-diff-shape", "framework-edit.diff has unified-diff anchors",
       lambda: _grep_file(r"^(diff --git|--- |\+\+\+ |@@ )", DIFF, re.M))
_check("evidence-log-written", "evidence log is writable", lambda: os.access(EVIDENCE, os.W_OK))

# Task-specific tier.
def _diff_consistent_with_repo_root():
    """The diff matches the repo root: it applies forward (not yet applied), or it
    applies in REVERSE (the implementer already edited the tree in place, the
    framework-edit default, so a forward --check always fails with 'already
    exists'). Fails only when neither holds: a stale or corrupt diff."""
    if _run(["git", "-C", REPO_ROOT, "apply", "--check", DIFF]):
        return True
    _ev.write("  forward --check failed; trying --reverse (edit applied in place)\n")
    _ev.flush()
    return _run(["git", "-C", REPO_ROOT, "apply", "--check", "--reverse", DIFF])


_check("diff-apply-check-clean", "diff applies cleanly to repo root, or is already applied there",
       _diff_consistent_with_repo_root)


def _changed_files_check():
    _changed_files()
    return os.path.getsize(CHANGED) > 0


_check("changed-files-extracted", "changed file list can be extracted", _changed_files_check)
_check("patched-copy-created", "diff applies to throwaway copy for static checks", _make_patched_copy)


def _copy_is_own_git_root():
    top = os.path.realpath(WORKTREE)
    r = subprocess.run(["git", "-C", WORKTREE, "rev-parse", "--show-toplevel"],
                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    if r.returncode == 0 and r.stdout.decode().strip() == top:
        return True
    _ev.write("copy-not-its-own-git-root\n")
    _ev.flush()
    return False


_check("copy-is-own-git-root", "throwaway copy is an independent git root", _copy_is_own_git_root)


def _diff_introduces_sentinel():
    sentinel = _diff_sentinel_path()
    if sentinel and os.path.exists(os.path.join(WORKTREE, sentinel)):
        return True
    _ev.write("sentinel-file-absent-post-apply\n")
    _ev.flush()
    return False


_check("diff-introduces-sentinel", "first diff-introduced path exists after apply", _diff_introduces_sentinel)
_check("apply-sentinel-has-content", "diff and patched-tree sentinels are non-empty",
       lambda: (os.path.isfile(os.path.join(WORK_PARENT, "diff-applied.sha256"))
                and os.path.getsize(os.path.join(WORK_PARENT, "diff-applied.sha256")) > 0
                and os.path.isfile(os.path.join(WORK_PARENT, "diff-applied.post.sha256"))
                and os.path.getsize(os.path.join(WORK_PARENT, "diff-applied.post.sha256")) > 0))


def _changed_file_lines():
    try:
        return [l.strip() for l in open(CHANGED, encoding="utf-8", errors="replace") if l.strip()]
    except OSError:
        return []


def _shell_syntax_clean():
    ok = True
    for f in _changed_file_lines():
        if f.endswith(".sh"):
            p = os.path.join(WORKTREE, f)
            if os.path.isfile(p):
                if not _run(["bash", "-n", p]):
                    ok = False
    return ok


_check("shell-syntax-clean", "changed shell files pass bash -n", _shell_syntax_clean)


def _python_compile_clean():
    ok = True
    for f in _changed_file_lines():
        if f.endswith(".py"):
            p = os.path.join(WORKTREE, f)
            if os.path.isfile(p):
                if not _run([sys.executable, "-m", "py_compile", p]):
                    ok = False
    return ok


_check("python-compile-clean", "changed Python files compile", _python_compile_clean)


def _typescript_typecheck():
    if any(re.search(r"\.(ts|tsx)$", f) for f in _changed_file_lines()):
        # The manager is a property of the repo being checked (WORKTREE/ui), not
        # of the host: `pnpm --dir` assumed pnpm and `npm --prefix` assumed npm,
        # and each fails against the other's repo. Resolve per root, run there.
        ui = os.path.join(WORKTREE, "ui")
        if os.path.isfile(os.path.join(ui, "package.json")):
            return _run(run_argv(ui, "typecheck"), cwd=ui)
        return _run(run_argv(WORKTREE, "typecheck"), cwd=WORKTREE)
    _ev.write("skip: no ts/tsx files changed\n")
    _ev.flush()
    return True


_check("typescript-typecheck-or-explicit-skip", "run typecheck when TS/TSX files changed", _typescript_typecheck)


def _high_blast_radius_guard():
    if any(re.match(r"^(lib/circuit_breaker\.sh|lib/throttle-guard\.sh|\.mini-ork/config/)", f)
           for f in _changed_file_lines()):
        # grep -R -q -E token RUN_DIR/context-pack.json RUN_DIR/kickoff.md
        pat = re.compile(r"(^|[^A-Z0-9_])ALLOW_HIGH_BLAST_RADIUS([^A-Z0-9_]|$)")
        for name in ("context-pack.json", "kickoff.md"):
            p = os.path.join(RUN_DIR, name)
            if not os.path.isfile(p):
                return False
            if pat.search(open(p, encoding="utf-8", errors="replace").read()):
                return True
        return False
    return True


_check("high-blast-radius-guard", "high-blast-radius paths require explicit ALLOW_HIGH_BLAST_RADIUS token",
       _high_blast_radius_guard)

# Build the changed-files list (static-check owns files_changed) and capture count.
_changed_files()
files_changed_count = len(_changed_file_lines())

checks = []
with open(CHECKS_TSV) as f:
    for line in f:
        cid, desc, passed = line.rstrip("\n").split("\t", 2)
        checks.append({"name": cid, "expected": desc, "actual": "see evidence log",
                       "pass": passed == "true"})
failed = [c["name"] for c in checks if not c["pass"]]
changed = _changed_file_lines()
VERIFIER_JSON = json.dumps({
    "verifier": NAME,
    "pass": not failed,
    "verdict": "pass" if not failed else "fail",
    "evidence_path": EVIDENCE,
    "checks_run": [c["name"] for c in checks],
    "failed_checks": failed,
    "checks": checks,
    "reasons": [f"{c['name']} failed; see {EVIDENCE}" for c in checks if not c["pass"]],
    "checked_criteria": [c["name"] for c in checks],
    "artifact_ref": "$MINI_ORK_RUN_DIR/framework-edit.diff",
    "changed_files": changed,
})

static_pass = "true" if not failed else "false"
write_verdict(static_pass, str(files_changed_count))

# Parse-check runs AFTER the write so the file we parse is the file we wrote.
def _verdict_json_parses():
    json.load(open(os.path.join(RUN_DIR, "verdict.json")))
    return True


_check("artifact-verdict-json-parses", "verdict.json parses as JSON", _verdict_json_parses)

_ev.close()
_tsv.close()
sys.stdout.write(VERIFIER_JSON)
sys.exit(0)
