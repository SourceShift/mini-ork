#!/usr/bin/env python3
# verifiers/test.py — apply framework-edit.diff to a copy and run smoke tests.
#
# Python port of test.sh (bash-removal WS8). Same checks, evidence text, JSON
# schema, and rc semantics.
#
# Inputs (via env):
#   MINI_ORK_RUN_DIR — run directory set by the native execute runtime
#   MINI_ORK_ROOT    — optional repo root
#
# Output: JSON to stdout. Exit code is always 0; caller reads .pass.

import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _verdict_merge import write_verdict  # noqa: E402

RUN_DIR = os.environ["MINI_ORK_RUN_DIR"]
REPO_ROOT = os.environ.get("MINI_ORK_ROOT") or os.getcwd()
NAME = "test"
DIFF = os.path.join(RUN_DIR, "framework-edit.diff")
EVIDENCE = os.path.join(RUN_DIR, f"verifier-{NAME}.log")
CHECKS_TSV = os.path.join(RUN_DIR, f"verifier-{NAME}.checks.tsv")
WORK_PARENT = os.path.join(RUN_DIR, f"verifier-{NAME}-work")
WORKTREE = os.path.join(WORK_PARENT, "repo")

open(CHECKS_TSV, "w").close()
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


def _run(argv, shell=False, cwd=None, env=None):
    if shell:
        return subprocess.run(argv, shell=True, cwd=cwd, stdout=_ev,
                              stderr=subprocess.STDOUT, env=env).returncode == 0
    return subprocess.run(argv, cwd=cwd, stdout=_ev,
                          stderr=subprocess.STDOUT, env=env).returncode == 0


def _make_throwaway_copy():
    shutil.rmtree(WORK_PARENT, ignore_errors=True)
    os.makedirs(WORKTREE)
    archive = subprocess.run(["git", "-C", REPO_ROOT, "archive", "HEAD"],
                             stdout=subprocess.PIPE, check=True).stdout
    tarfile.open(fileobj=io.BytesIO(archive)).extractall(WORKTREE)


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


def _apply_diff_to_copy():
    if not _assert_copy_is_own_git_root():
        _ev.write("copy-not-its-own-git-root\n")
        _ev.flush()
        return False
    sentinel = _diff_sentinel_path()
    if not sentinel:
        _ev.write("sentinel-file-absent-post-apply\n")
        _ev.flush()
        return False
    if subprocess.run(["git", "-C", WORKTREE, "apply", DIFF],
                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode != 0:
        return False
    if not os.path.exists(os.path.join(WORKTREE, sentinel)):
        _ev.write("sentinel-file-absent-post-apply\n")
        _ev.flush()
        return False
    with open(os.path.join(WORK_PARENT, "diff-applied.sha256"), "w") as f:
        f.write(f"{_sha256_file(DIFF)}  {DIFF}\n")
    r = subprocess.run("git diff --no-index /dev/null . || true", shell=True,
                       cwd=WORKTREE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    post = hashlib.sha256(r.stdout).hexdigest()
    with open(os.path.join(WORK_PARENT, "diff-applied.post.sha256"), "w") as f:
        f.write(f"{post}  -\n")
    return (os.path.getsize(os.path.join(WORK_PARENT, "diff-applied.sha256")) > 0
            and os.path.getsize(os.path.join(WORK_PARENT, "diff-applied.post.sha256")) > 0)


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
def _throwaway_copy_created():
    _make_throwaway_copy()
    return os.path.isdir(WORKTREE) and len(os.listdir(WORKTREE)) > 0


_check("throwaway-copy-created", "repo HEAD copied under MINI_ORK_RUN_DIR", _throwaway_copy_created)


def _copy_is_own_git_root():
    if _assert_copy_is_own_git_root():
        return True
    _ev.write("copy-not-its-own-git-root\n")
    _ev.flush()
    return False


_check("copy-is-own-git-root", "throwaway copy is an independent git root", _copy_is_own_git_root)
_check("diff-applies-to-copy", "framework-edit.diff applies to throwaway copy", _apply_diff_to_copy)


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


# Only enforce web-smoke tests when the target repo actually ships them.
# Framework-edit runs against many repos; failing a verifier because a
# repo-specific smoke file is absent is an infrastructure false-negative.
def _web_smoke():
    if not (os.path.getsize(os.path.join(WORK_PARENT, "diff-applied.sha256")) > 0
            and os.path.getsize(os.path.join(WORK_PARENT, "diff-applied.post.sha256")) > 0):
        return False
    env = {k: v for k, v in os.environ.items()
           if k not in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "OPENROUTER_API_KEY", "GEMINI_API_KEY")}
    env["PYTHONPATH"] = "."
    return _run([sys.executable, "-m", "pytest", "tests/test_web_smoke.py", "-q"],
                cwd=WORKTREE, env=env)


if os.path.isfile(os.path.join(WORKTREE, "tests", "test_web_smoke.py")):
    _check("web-smoke-tests-pass", "pytest tests/test_web_smoke.py passes without network keys", _web_smoke)
else:
    _check("web-smoke-tests-skipped", "tests/test_web_smoke.py absent in target repo; skipping web-smoke",
           lambda: True)

checks = []
with open(CHECKS_TSV) as f:
    for line in f:
        cid, desc, passed = line.rstrip("\n").split("\t", 2)
        checks.append({"name": cid, "expected": desc, "actual": "see evidence log",
                       "pass": passed == "true"})
failed = [c["name"] for c in checks if not c["pass"]]
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
    "artifact_ref": "$MINI_ORK_RUN_DIR/framework-edit.diff tests/test_web_smoke.py",
})

tests_pass = "true" if not failed else "false"
# test.py owns tests_pass only; static-check owns files_changed/static_pass.
# Pass empty string for files_changed so the helper does not overwrite the
# value static-check already wrote.
write_verdict("", "", tests_pass)


# Parse-check runs AFTER the write so the file we parse is the file we wrote.
def _verdict_json_parses():
    json.load(open(os.path.join(RUN_DIR, "verdict.json")))
    return True


_check("artifact-verdict-json-parses", "verdict.json parses as JSON", _verdict_json_parses)

_ev.close()
_tsv.close()
sys.stdout.write(VERIFIER_JSON)
sys.exit(0)
