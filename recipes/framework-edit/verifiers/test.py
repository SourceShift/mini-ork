#!/usr/bin/env python3
# verifiers/test.py — apply framework-edit.diff to a copy and run smoke tests.
#
# Python port of test.sh (bash-removal WS8). Same checks, evidence text, JSON
# schema, and rc semantics.
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
import shutil
import subprocess
import sys
import tarfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _verdict_merge import write_verdict  # noqa: E402

# Lazy import with local fallback — the verifier may be copied into a
# fixture without the rest of the mini_ork package on PYTHONPATH.
try:                                                    # pragma: no cover — verifier copied without mini_ork
    from mini_ork.verify.test_env import scrubbed_test_env
except Exception:                                       # pragma: no cover — verifier copied without mini_ork
    def scrubbed_test_env(environ=None):
        import os as _os
        env = dict(_os.environ if environ is None else environ)
        for k in list(env):
            if (k in {"MINI_ORK_SECRETS", "MINI_ORK_DB", "MINI_ORK_HOME", "MINI_ORK_PROJECT_HOME",
                      "MINI_ORK_RUN_ID", "MINI_ORK_RUN_DIR", "MINI_ORK_PLAN_PATH", "MINI_ORK_AGENTS",
                      "MO_TARGET_CWD"}
                    or k.endswith(("_API_KEY", "_AUTH_TOKEN", "_ACCESS_TOKEN", "_SECRET", "_SECRET_KEY"))
                    or k.startswith("ANTHROPIC_") or k in {"OPENAI_API_BASE", "OPENAI_BASE_URL"}):
                env.pop(k)
        return env

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


def _materialise_submodules(root, dest):
    """Copy the target's submodule content into the extracted copy.

    ``git archive`` writes a submodule gitlink as an EMPTY directory, so a target
    that vendors code that way hands the copy a hole where the vendored tree
    should be: paths the target really has read as absent, and a smoke test that
    needs them fails against a tree nobody has. Re-archive each submodule at the
    commit the target records and unpack it over the same path.

    Read from the target's own checkout instead of fetching: a verifier that
    reaches the network stops being reproducible, and the remote may no longer
    serve that commit. A submodule the target has not initialised has nothing
    local to read, so it is skipped rather than guessed at."""
    if not os.path.isfile(os.path.join(root, ".gitmodules")):
        return
    listed = subprocess.run(
        ["git", "-C", root, "config", "-f", os.path.join(root, ".gitmodules"),
         "--get-regexp", r"^submodule\..*\.path$"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    for line in listed.stdout.splitlines():
        path = line.partition(" ")[2].strip()
        if not path:
            continue
        source = os.path.join(root, path)
        recorded = subprocess.run(
            ["git", "-C", root, "rev-parse", "--verify", "-q", f"HEAD:{path}"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True).stdout.strip()
        if not recorded or not os.path.exists(os.path.join(source, ".git")):
            continue
        archived = subprocess.run(["git", "-C", source, "archive", recorded],
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        if archived.returncode != 0:
            continue
        target = os.path.join(dest, path)
        os.makedirs(target, exist_ok=True)
        tarfile.open(fileobj=io.BytesIO(archived.stdout)).extractall(target)


def _make_throwaway_copy():
    shutil.rmtree(WORK_PARENT, ignore_errors=True)
    os.makedirs(WORKTREE)
    archive = subprocess.run(["git", "-C", REPO_ROOT, "archive", _base_ref()],
                             stdout=subprocess.PIPE, check=True).stdout
    tarfile.open(fileobj=io.BytesIO(archive)).extractall(WORKTREE)
    _materialise_submodules(REPO_ROOT, WORKTREE)


def _diff_sentinel_path():
    try:
        with open(DIFF, encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.startswith("+++ b/"):
                    return line[len("+++ b/"):].rstrip("\n")
    except OSError:
        pass
    return ""


# The base snapshot is committed so the copy behaves like a real checkout: HEAD
# is the base tree, the applied diff is the working-tree change. With no commit,
# every test that reads the engine sha or bundles the engine (`git rev-parse
# HEAD`, `HEAD^{tree}`) failed in the copy alone — ten false failures on the
# cloud-providers B run (run-1791906478-48004), all green in the real worktree.
_BASE_COMMIT_ENV = {
    "GIT_AUTHOR_NAME": "mini-ork-verifier", "GIT_AUTHOR_EMAIL": "verifier@mini-ork.local",
    "GIT_COMMITTER_NAME": "mini-ork-verifier", "GIT_COMMITTER_EMAIL": "verifier@mini-ork.local",
}


def _commit_base_snapshot():
    """Commit the copy's tree once, before the diff is applied (idempotent)."""
    if subprocess.run(["git", "-C", WORKTREE, "rev-parse", "--verify", "-q", "HEAD"],
                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
        return
    env = {**os.environ, **_BASE_COMMIT_ENV}
    subprocess.run(["git", "-C", WORKTREE, "add", "-A"], stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, env=env)
    subprocess.run(["git", "-C", WORKTREE, "-c", "commit.gpgsign=false", "commit", "-q",
                    "--no-verify", "--allow-empty", "-m", "verifier base snapshot"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)


def _assert_copy_is_own_git_root():
    subprocess.run(["git", "-C", WORKTREE, "init"], stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL)
    _commit_base_snapshot()
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
    env = scrubbed_test_env()
    env["PYTHONPATH"] = "."
    return _run([sys.executable, "-m", "pytest", "tests/test_web_smoke.py", "-q"],
                cwd=WORKTREE, env=env)


if os.path.isfile(os.path.join(WORKTREE, "tests", "test_web_smoke.py")):
    _check("web-smoke-tests-pass", "pytest tests/test_web_smoke.py passes without network keys", _web_smoke)
else:
    _check("web-smoke-tests-skipped", "tests/test_web_smoke.py absent in target repo; skipping web-smoke",
           lambda: True)


# ── epic acceptance: run the kickoff's OWN verification commands ─────────────
# Everything above proves the diff EXISTS and APPLIES. None of it proves the
# epic's feature WORKS: this verifier never ran the tests the epic was written to
# add, so an epic could reach verdict=pass with its package absent and every new
# test failing. That is not hypothetical — a whole delegation's epics were gated
# by a synthesized `py_compile` of the test file itself (2026-10-12).
#
# The kickoff's `## Verification commands` are the authoritative acceptance bar:
# they are the instruction the epic was dispatched with, and they were written to
# be run "from the run's working tree, so mini_ork resolves to the tree under
# test" — which is exactly the patched throwaway copy below.
_VERIFY_CMDS_PROFILE = os.path.join(RUN_DIR, "run_profile.json")
_VERIFY_CMDS_SECTION = re.compile(
    r"^##\s+Verification commands\s*$(.*?)(?=^##\s|\Z)",
    re.MULTILINE | re.DOTALL)
_VERIFY_CMD_TIMEOUT_S = int(os.environ.get("MO_EPIC_VERIFY_TIMEOUT_S", "300"))
# A kickoff that declares more than this many acceptance commands cannot be
# run end-to-end inside the per-command timeout window above the limit; the
# budget guard in _epic_verification_commands_pass rejects it loudly instead
# of silently dropping the tail.
_EPIC_VERIFICATION_COMMAND_LIMIT = 4


def _profile_field(name):
    try:
        with open(_VERIFY_CMDS_PROFILE, encoding="utf-8") as fh:
            return json.load(fh).get(name)
    except (OSError, ValueError):
        return None


def _epic_verification_commands():
    """The kickoff's acceptance commands, in order, deduped.

    ``run_profile.json`` already carries the parsed list (the profile gate reads
    that section), so prefer it; fall back to parsing the kickoff markdown, so a
    run whose profile predates the field is still gated.

    The list is unbounded here: every command the kickoff declared reaches the
    budget guard in ``_epic_verification_commands_pass``, which fails loudly
    on over-limit kickoffs rather than silently dropping the tail.
    """
    raw = _profile_field("verification_command") or []
    if isinstance(raw, str):
        raw = [raw]
    cmds = [str(c) for c in raw if str(c).strip()]
    if not cmds:
        kickoff = str(_profile_field("kickoff_path") or "")
        if kickoff and os.path.isfile(kickoff):
            try:
                with open(kickoff, encoding="utf-8") as fh:
                    section = _VERIFY_CMDS_SECTION.search(fh.read())
            except OSError:
                section = None
            if section:
                cmds = re.findall(r"`([^`\n]+)`", section.group(1))
    out = []
    for cmd in cmds:
        cmd = cmd.strip().strip("`").strip()
        if cmd and cmd not in out:
            out.append(cmd)
    # Every command the kickoff declared reaches the budget guard in
    # _epic_verification_commands_pass, which fails loudly on over-limit
    # kickoffs instead of silently dropping the tail. The old silent [:4] cap
    # was masking the verification bar the kickoff's explicit "## Verification
    # commands" section set (run-1791853722-board-k3 timed out on a bare
    # runner over 8647 tests, never reaching the kickoff's `ruff check`).
    return out


def _sh(cmd):
    """Run one acceptance command the way a human would, with this verifier's
    interpreter standing in for a bare ``pytest``: a throwaway archive has no
    console script on PATH, and the interpreter running this file is the one
    guaranteed to import the tree under test."""
    if cmd.split(" ")[0] == "pytest":
        return f"{sys.executable} -m pytest" + cmd[len("pytest"):]
    return cmd


def _epic_verification_commands_pass():
    if os.environ.get("MO_EPIC_VERIFY_CMDS") == "0":
        return True
    if not os.path.isdir(WORKTREE):
        _ev.write("  patched copy absent — cannot run the kickoff's commands\n")
        return False
    cmds = _epic_verification_commands()
    if len(cmds) > _EPIC_VERIFICATION_COMMAND_LIMIT:
        _ev.write(
            f"  {len(cmds)} commands declared, limit is "
            f"{_EPIC_VERIFICATION_COMMAND_LIMIT} — split the kickoff\n")
        _ev.flush()
        return False
    env = scrubbed_test_env()
    env["PYTHONPATH"] = "."
    env["PATH"] = os.path.dirname(sys.executable) + os.pathsep + env.get("PATH", "")
    for cmd in cmds:
        _ev.write(f"  $ {_sh(cmd)}\n")
        _ev.flush()
        try:
            proc = subprocess.run(_sh(cmd), shell=True, cwd=WORKTREE, env=env,
                                  stdout=_ev, stderr=subprocess.STDOUT,
                                  timeout=_VERIFY_CMD_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            _ev.write(f"  TIMEOUT after {_VERIFY_CMD_TIMEOUT_S}s\n")
            _ev.flush()
            return False
        if proc.returncode != 0:
            _ev.write(f"  rc={proc.returncode}\n")
            _ev.flush()
            return False
    return True


if _epic_verification_commands():
    _check("epic-verification-commands-pass",
           "the kickoff's own ## Verification commands pass in the patched copy",
           _epic_verification_commands_pass)
else:
    _check("epic-verification-commands-none",
           "kickoff declares no ## Verification commands; nothing to execute",
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
