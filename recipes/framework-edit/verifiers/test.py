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

import glob
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
# The base copy lives OUTSIDE WORK_PARENT: _make_throwaway_copy() rmtree's that
# parent, and the base side of a failed command must survive that (RB-26).
BASE_PARENT = os.path.join(RUN_DIR, f"verifier-{NAME}-base")
BASE_WORKTREE = os.path.join(BASE_PARENT, "repo")

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


def _extractall(tar, dest):
    """Extract ``tar`` into ``dest`` with PEP 706's ``data`` filter.

    ``filter=`` exists from 3.12 (backported to 3.11.4); without it 3.13 emits a
    DeprecationWarning into the merged evidence stream. Older interpreters have
    no filter at all, so extract member-by-member there."""
    if hasattr(tarfile, "data_filter"):
        tar.extractall(dest, filter="data")
        return
    for member in tar.getmembers():
        tar.extract(member, dest)


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
        _extractall(tarfile.open(fileobj=io.BytesIO(archived.stdout)), target)


def _make_throwaway_copy():
    shutil.rmtree(WORK_PARENT, ignore_errors=True)
    os.makedirs(WORKTREE)
    archive = subprocess.run(["git", "-C", REPO_ROOT, "archive", _base_ref()],
                             stdout=subprocess.PIPE, check=True).stdout
    _extractall(tarfile.open(fileobj=io.BytesIO(archive)), WORKTREE)
    _materialise_submodules(REPO_ROOT, WORKTREE)


def _recorded_base_ref():
    """The ``pre-implementer-ref`` the diff was cut against, or "" when absent.

    Deliberately does NOT fall back to HEAD: framework-edit may have committed
    the implementer's patch already, so HEAD can contain the diff, and a
    "base" copy built from it would compare the candidate with itself —
    silently turning a real regression into a base-fails abstention."""
    try:
        with open(os.path.join(RUN_DIR, "pre-implementer-ref"), encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def _make_base_copy():
    """Archive ``pre-implementer-ref`` WITHOUT applying the diff — the base tree.

    Built lazily, only after a kickoff command failed on the patched copy, so a
    green run pays nothing. Same construction as ``_make_throwaway_copy``
    (archive, submodules, its own git root) minus the ``git apply``: tests that
    read ``HEAD`` must see the same kind of checkout on both sides, or the
    comparison would report failures the base never had. No recorded base means
    no comparison at all — the caller keeps the failure gating (fail closed)."""
    if not _recorded_base_ref():
        return False
    shutil.rmtree(BASE_PARENT, ignore_errors=True)
    os.makedirs(BASE_WORKTREE)
    archive = subprocess.run(["git", "-C", REPO_ROOT, "archive", _recorded_base_ref()],
                             stdout=subprocess.PIPE, check=True).stdout
    _extractall(tarfile.open(fileobj=io.BytesIO(archive)), BASE_WORKTREE)
    _materialise_submodules(REPO_ROOT, BASE_WORKTREE)
    return _assert_copy_is_own_git_root(BASE_WORKTREE)


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


def _commit_base_snapshot(root=None):
    """Commit the copy's tree once, before the diff is applied (idempotent)."""
    root = WORKTREE if root is None else root
    if subprocess.run(["git", "-C", root, "rev-parse", "--verify", "-q", "HEAD"],
                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
        return
    env = {**os.environ, **_BASE_COMMIT_ENV}
    subprocess.run(["git", "-C", root, "add", "-A"], stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, env=env)
    subprocess.run(["git", "-C", root, "-c", "commit.gpgsign=false", "commit", "-q",
                    "--no-verify", "--allow-empty", "-m", "verifier base snapshot"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)


def _assert_copy_is_own_git_root(root=None):
    root = WORKTREE if root is None else root
    subprocess.run(["git", "-C", root, "init"], stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL)
    _commit_base_snapshot(root)
    top = os.path.realpath(root)
    r = subprocess.run(["git", "-C", root, "rev-parse", "--show-toplevel"],
                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    return r.returncode == 0 and r.stdout.decode().strip() == top


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# Pin the artifact this verifier judged (RB-13/RBF-13): the publisher recomputes
# the sha before publishing and refuses when it no longer matches. Captured
# BEFORE any check reads, applies, or could rewrite the diff, so the recorded
# value is the diff that was actually verified — not whatever is on disk later.
try:
    ARTIFACT_SHA256 = _sha256_file(DIFF)
except OSError:
    ARTIFACT_SHA256 = ""


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


def _command_env(root, *, restore_run_dir=False):
    """The environment a kickoff command runs under, inside ``root``.

    ``PYTHONPATH`` is the ABSOLUTE patched/base copy, prepended to whatever was
    inherited — never ``.``. A relative ``.`` resolved to the verifier's cwd
    while the command ran in the copy, leaking the engine root into the target
    tree and failing ``test_default_root_is_the_engine_root`` on a clean base
    (hr-cli-worktree, RB-26). A ``.`` component in an inherited value is dropped
    for the same reason; everything else inherited is preserved.

    ``PATH`` starts with the verifier's own interpreter directory, so a bare
    ``python3``/``pytest`` in operator prose resolves the interpreter that can
    import the tree under test (sde-3b1: host python3 was 3.9 and raised).
    """
    env = scrubbed_test_env()
    if restore_run_dir:
        # scrub drops it with the credentials; a run-local path, not a secret.
        env["MINI_ORK_RUN_DIR"] = RUN_DIR
    inherited = [
        part for part in env.get("PYTHONPATH", "").split(os.pathsep)
        if part and part != "."
    ]
    env["PYTHONPATH"] = os.pathsep.join([os.path.abspath(root), *inherited])
    env["PATH"] = os.path.dirname(sys.executable) + os.pathsep + env.get("PATH", "")
    return env


# Only enforce web-smoke tests when the target repo actually ships them.
# Framework-edit runs against many repos; failing a verifier because a
# repo-specific smoke file is absent is an infrastructure false-negative.
def _web_smoke():
    if not (os.path.getsize(os.path.join(WORK_PARENT, "diff-applied.sha256")) > 0
            and os.path.getsize(os.path.join(WORK_PARENT, "diff-applied.post.sha256")) > 0):
        return False
    env = _command_env(WORKTREE)
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


def _kickoff_declares_commands():
    """Whether the kickoff states its own verification commands; None when unknown.

    Unknown (no readable kickoff, or a copy of this verifier without the mini_ork
    package) keeps the profile's list, as before.
    """
    kickoff = str(_profile_field("kickoff_path") or "")
    if not (kickoff and os.path.isfile(kickoff)):
        return None
    try:
        with open(kickoff, encoding="utf-8") as fh:
            text = fh.read()
        from mini_ork.gates.profile_gate import declares_verification_command
    except Exception:
        return None
    return declares_verification_command(text)


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
    if cmds and _kickoff_declares_commands() is False:
        # With no verification section the profile falls back to regex-scanning
        # the whole kickoff for runner invocations. Those are hints, not the
        # kickoff's acceptance bar: vg-3-spec-tests (2026-10-15) was gated on the
        # prose fragment "pytest files into" and a `<run_dir>` placeholder.
        cmds = []
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


_GLOB_CHARS = frozenset("*?[")


def _is_test_path_token(token):
    return bool(token) and not token.startswith("-") and (
        "/" in token or token.endswith(".py") or _GLOB_CHARS & set(token))


def _is_glob_path_token(token):
    """A test path token that actually globs a path (not ``-k 'foo*'``)."""
    return (_is_test_path_token(token) and _GLOB_CHARS & set(token)
            and ("/" in token or token.endswith(".py")))


def _dedupe_redundant_test_paths(cmd, root=None):
    """Resolve test paths to concrete files and de-duplicate the union.

    pytest-asyncio (0.23.4) re-wraps ``Module.collect`` on every collection of the
    same path without an idempotence check, so a command naming a test file
    explicitly *and* a glob matching it recurses until the interpreter raises
    ``RecursionError`` during collection — before a single test runs. The change
    under test is then judged wrong (rp-e6-graph-focus, rp-e3-header,
    hr-exec-park, all 2026-10-15).

    Two overlapping GLOBS are the unfixed half of that bug (run-1792253563-27493:
    ``test_ide_pages_run_learnings*.py`` + ``test_ide_pages_run*.py``): the old
    code skipped every glob token, so the duplicate reached pytest. When two or
    more glob tokens expand to an overlapping set, this expands them — relative
    to ``root``, the tree the command will run in — unions them with any explicit
    paths, and hands pytest each concrete file exactly once.

    A lone glob is left verbatim (with an explicit path it covers still dropped),
    and a command this cannot rewrite exactly is left alone to fail loudly
    rather than silently collecting a different set of tests.
    """
    root = WORKTREE if root is None else root
    tokens = cmd.split()
    try:
        parsed = shlex.split(cmd)
    except ValueError:
        return cmd
    if len(parsed) != len(tokens):
        return cmd
    if not any(os.path.basename(token) == "pytest" for token in tokens[:3]):
        return cmd
    if sum(1 for token in tokens if _is_test_path_token(token)) < 2:
        return cmd

    glob_tokens = [token for token in tokens if _is_glob_path_token(token)]
    if len(glob_tokens) >= 2:
        # ``glob.root_dir`` is 3.10+; join against the copy instead.
        expanded = {}
        for token in glob_tokens:
            matches = sorted(
                os.path.relpath(match, root)
                for match in glob.glob(os.path.join(root, token), recursive=True)
                if os.path.isfile(match))
            if not matches:
                # A glob matching nothing is a command we cannot rewrite
                # faithfully; leave it for pytest to report.
                return cmd
            expanded[token] = matches
        hits = {}
        for matches in expanded.values():
            for match in matches:
                hits[match] = hits.get(match, 0) + 1
        if any(count >= 2 for count in hits.values()):
            union = []
            for token in tokens:
                if not _is_test_path_token(token):
                    continue
                for path in expanded.get(token, [token]):
                    if path not in union:
                        union.append(path)
            kept, inserted = [], False
            for token in tokens:
                if _is_test_path_token(token):
                    if not inserted:
                        kept.extend(union)
                        inserted = True
                    continue
                kept.append(token)
            _ev.write(
                f"  [dedupe] expanded {len(glob_tokens)} overlapping globs to "
                f"{len(union)} concrete files (each collected once)\n")
            _ev.flush()
            return " ".join(kept)

    covered = set()
    for token in tokens:
        if _is_test_path_token(token) and _GLOB_CHARS & set(token):
            covered.update(glob.glob(os.path.join(root, token), recursive=True))

    seen, redundant = set(), set()
    for token in tokens:
        if not _is_test_path_token(token) or _GLOB_CHARS & set(token):
            continue
        if token in seen or os.path.join(root, token) in covered:
            redundant.add(token)
        seen.add(token)
    if not redundant:
        return cmd

    # A glob-covered path goes entirely (the glob still collects it); an exact
    # duplicate keeps its first occurrence, so one collection path survives.
    covered_dupes = {token for token in redundant if token in covered}
    pending = set(redundant)
    kept = []
    for token in tokens:
        if token in covered_dupes:
            continue
        if token in pending:
            pending.discard(token)
            continue
        kept.append(token)
    _ev.write(
        f"  [dedupe] dropped {', '.join(sorted(redundant))} — a sibling glob "
        "in the same command already collects it (pytest-asyncio recurses on a "
        "duplicate collection path)\n")
    return " ".join(kept)


def _bind_run_dir(cmd):
    """Bind ``$MINI_ORK_RUN_DIR`` for a kickoff command that names it.

    The replay env is scrubbed of it so the tests under the command never see the
    live run dir, but a kickoff writing ``--junitxml="${MINI_ORK_RUN_DIR}/x.xml"``
    then expanded it empty and crashed on a read-only ``/x.xml`` after every test
    passed (sde-3b2-ledger, 2026-10-15). An unexported shell assignment reaches the
    command line's expansion and not the processes the command starts.
    """
    if "MINI_ORK_RUN_DIR" not in cmd:
        return cmd
    return f"MINI_ORK_RUN_DIR={shlex.quote(RUN_DIR)}; {cmd}"


#: Check rows that carry evidence a command did not fail because of the diff.
#: They are appended after the TSV rows and are non-gating by construction.
_IMPLICIT_CHECKS = []


def _run_acceptance_command(root, cmd):
    """Run one kickoff command in ``root``. Returns ``(rc, timed_out)``.

    ``_command_env`` restores ``MINI_ORK_RUN_DIR`` (run-local, not a secret) so
    `--junitxml="${MINI_ORK_RUN_DIR}/x.xml"` resolves; ``_dedupe`` expands
    globs against THIS tree, so a diff-added test is never collected on the
    base side.
    """
    rewritten = _dedupe_redundant_test_paths(cmd, root)
    _ev.write(f"  $ {_sh(rewritten)}\n")
    _ev.flush()
    try:
        proc = subprocess.run(_bind_run_dir(_sh(rewritten)), shell=True, cwd=root,
                              env=_command_env(root, restore_run_dir=True),
                              stdout=_ev, stderr=subprocess.STDOUT,
                              timeout=_VERIFY_CMD_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        _ev.write(f"  TIMEOUT after {_VERIFY_CMD_TIMEOUT_S}s\n")
        _ev.flush()
        return None, True
    if proc.returncode != 0:
        _ev.write(f"  rc={proc.returncode}\n")
        _ev.flush()
    return proc.returncode, False


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
    base_ready = False
    for cmd in cmds:
        rc, timed_out = _run_acceptance_command(WORKTREE, cmd)
        if timed_out:
            return False
        if rc == 0:
            continue
        # A command the pre-patch tree also fails is an environment/base
        # failure, not a regression introduced by the diff. Replay it ONCE on
        # the base copy (built lazily, so a green run pays nothing) before
        # blaming the change. Base-passes => a real regression; base fails with
        # the SAME exit code => a non-gating abstention in the envelope; base
        # fails differently => the base cannot run the same check, so it stays
        # gating.
        if not base_ready:
            try:
                base_ready = bool(_make_base_copy())
            except Exception as exc:  # noqa: BLE001 — no base tree, no comparison
                _ev.write(f"  base copy could not be built: {exc}\n")
                _ev.flush()
                return False
            if not base_ready:
                _ev.write(
                    "  no usable pre-implementer-ref — cannot compare with the base; "
                    "keeping the failure gating\n")
                _ev.flush()
                return False
        _ev.write("  replaying the failed command on the base copy\n")
        _ev.flush()
        base_rc, base_timed_out = _run_acceptance_command(BASE_WORKTREE, cmd)
        if base_timed_out:
            return False
        if base_rc != 0 and base_rc == rc:
            _IMPLICIT_CHECKS.append({
                "name": "epic-verification-commands-base-fails",
                "expected": "command passes on the patched copy; a failure the base shares is not the diff's",
                "actual": f"patched rc={rc}, base rc={base_rc}",
                "pass": True,
                "gating": False,
                "failure_kind": "base-fails",
                "command": cmd,
                "reason": (f"`{cmd}` failed on the base copy too "
                           f"(patched rc={rc}, base rc={base_rc}); no abstention "
                           "is blamed on the diff"),
            })
            _ev.write(
                f"  [base-fails] `{cmd}` also fails on the base copy "
                f"(rc={base_rc}); recording a non-gating abstention\n")
            _ev.flush()
            continue
        if base_rc != 0:
            _ev.write(
                f"  [base-differs] `{cmd}` fails on the patched copy (rc={rc}) "
                f"and differently on the base copy (rc={base_rc}): the base "
                "cannot run the same check, so the failure stays gating\n")
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
                       "pass": passed == "true", "gating": True})
# A base-fails row is evidence of a shared environment failure, not of a
# regression: it must be visible in `checks` yet never gate the run.
checks.extend(_IMPLICIT_CHECKS)
failed = [c["name"] for c in checks if not c["pass"] and c.get("gating", True)]
VERIFIER_JSON = json.dumps({
    "verifier": NAME,
    "pass": not failed,
    "verdict": "pass" if not failed else "fail",
    "evidence_path": EVIDENCE,
    "checks_run": [c["name"] for c in checks],
    "failed_checks": failed,
    "checks": checks,
    "reasons": [f"{c['name']} failed; see {EVIDENCE}" for c in checks
                if not c["pass"] and c.get("gating", True)],
    "checked_criteria": [c["name"] for c in checks],
    "artifact_ref": "$MINI_ORK_RUN_DIR/framework-edit.diff tests/test_web_smoke.py",
    "artifact_sha256": ARTIFACT_SHA256,
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
