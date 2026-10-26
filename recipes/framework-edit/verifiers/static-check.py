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

import fnmatch
import hashlib
import importlib.util
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tomllib

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

# The frozen-kernel guard logic + its built-in default list live in the engine
# (mini_ork/vcs/repo_integrity_guard.py) so the verifier and the apply boundary
# share one definition. If the engine predates it, fall back to an inline copy of
# the floor — a stale engine must still refuse, never silently pass.
try:  # noqa: E402
    from mini_ork.vcs.repo_integrity_guard import (  # noqa: E402
        FROZEN_CONFIG_RELPATH,
        FROZEN_PATHS_DEFAULT,
        frozen_hits,
        load_frozen_paths,
        parse_frozen_paths,
    )
except Exception:  # noqa: BLE001
    import fnmatch as _fnmatch  # noqa: E402

    FROZEN_CONFIG_RELPATH = os.path.join("config", "kernel-frozen.yaml")
    FROZEN_PATHS_DEFAULT = [
        "config/kernel-frozen.yaml",
        "recipes/framework-edit/verifiers/static-check.py",
        "mini_ork/vcs/repo_integrity_guard.py",
        "mini_ork/runtime/sandbox.py",
        "mini_ork/gates/gate_bootstrap.py",
        "mini_ork/gates/abstain_gate.py",
        "mini_ork/recovery/circuit_breaker.py",
        "mini_ork/recovery/escalation_policy.py",
        "mini_ork/dispatch/calibration.py",
        "mini_ork/recovery/auto_repair.py",
        "mini_ork/policies/engine.py",
        "mini_ork/dispatch/cost_pause.py",
        "mini_ork/stores/safety_events.py",
        "mini_ork/observability/bug_report.py",
    ]

    def parse_frozen_paths(text):  # type: ignore[misc]
        return []

    def load_frozen_paths(root):  # type: ignore[misc]
        return list(FROZEN_PATHS_DEFAULT)

    def frozen_hits(changed, patterns):  # type: ignore[misc]
        return [c for c in changed if any(_fnmatch.fnmatch(c, p) for p in patterns)]

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


# Pin the artifact this verifier judged (RB-13/RBF-13): the publisher recomputes
# the sha before publishing and refuses when it no longer matches. Captured
# BEFORE any check reads, applies, or could rewrite the diff, so the recorded
# value is the diff that was actually verified — not whatever is on disk later.
try:
    ARTIFACT_SHA256 = _sha256_file(DIFF)
except OSError:
    ARTIFACT_SHA256 = ""


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
    should be: paths the target really has read as absent, and neither the diff
    nor a test that needs them can work. Re-archive each submodule at the commit
    the target records and unpack it over the same path.

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


def _make_patched_copy():
    shutil.rmtree(WORK_PARENT, ignore_errors=True)
    os.makedirs(WORKTREE)
    archive = subprocess.run(["git", "-C", REPO_ROOT, "archive", _base_ref()],
                             stdout=subprocess.PIPE, check=True).stdout
    _extractall(tarfile.open(fileobj=io.BytesIO(archive)), WORKTREE)
    _materialise_submodules(REPO_ROOT, WORKTREE)
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


def _has_ruff_lint_table(pyproject_path):
    """True iff the patched copy's pyproject.toml has a [tool.ruff.lint] table.

    The patched copy's repo decides what 'blocking tier' means; an unrelated
    pyproject (no lint table) has no project-specific blocking tier, so the
    check skips rather than inventing one. Reading pyproject.toml: the file is
    optional and may be unrelated to ruff; tomllib raises on broken TOML, which
    we treat as 'no table' so a malformed file never masquerades as a
    configured project."""
    try:
        with open(pyproject_path, "rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return False
    ruff = data.get("tool", {}).get("ruff")
    if not isinstance(ruff, dict):
        return False
    return isinstance(ruff.get("lint"), dict)


def _resolve_ruff():
    """Return argv to invoke ruff, or None if ruff is not available.

    Resolution order: a ``ruff`` binary on PATH, else ``python -m ruff`` if the
    ``ruff`` module is importable. The importability probe uses ``find_spec`` so
    ruff is never actually imported into the verifier process — an import would
    leak state across invocations and can raise on broken installs."""
    binary = shutil.which("ruff")
    if binary:
        return [binary]
    if importlib.util.find_spec("ruff") is not None:
        return [sys.executable, "-m", "ruff"]
    return None


def _python_lint_blocking_tier():
    py_files = [f for f in _changed_file_lines() if f.endswith(".py")]
    if not py_files:
        _ev.write("skip: no Python files changed\n")
        _ev.flush()
        return True
    if not _has_ruff_lint_table(os.path.join(WORKTREE, "pyproject.toml")):
        _ev.write("skip: pyproject.toml has no [tool.ruff.lint] table\n")
        _ev.flush()
        return True
    argv = _resolve_ruff()
    if argv is None:
        _ev.write(
            'ruff not found — install it the way CI does: pip install "ruff>=0.6"\n')
        _ev.flush()
        return False
    # Only lint files that exist in the patched copy; deleted files are skipped
    # so a deletion can't reintroduce findings the diff doesn't add.
    targets = [os.path.join(WORKTREE, f) for f in py_files
               if os.path.isfile(os.path.join(WORKTREE, f))]
    if not targets:
        return True
    return _run([*argv, "check", *targets], cwd=WORKTREE)


_check("python-lint-blocking-tier",
       "ruff blocking-tier lint passes over changed Python files",
       _python_lint_blocking_tier)


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


ORIGIN_MARKER = "<!-- mini-ork:origin=auto-triage -->"


def _run_kickoff_candidates():
    """The run's kickoff file, most authoritative source first.

    ``run_profile.json`` records ``kickoff_path`` — the file the pipeline itself
    read (``mini_ork/cli/execute.py``; ``context_v2.pack_for_run`` embeds THIS
    file's text into the context pack). The runtime never writes a run-local
    ``kickoff.md``, so that path is a fallback for hand-run fixtures only."""
    out = []
    profile = os.path.join(RUN_DIR, "run_profile.json")
    try:
        with open(profile, encoding="utf-8") as fh:
            data = json.load(fh)
        kp = data.get("kickoff_path") if isinstance(data, dict) else None
        if isinstance(kp, str) and kp.strip():
            out.append(kp)
    except (OSError, ValueError):
        pass
    out.append(os.path.join(RUN_DIR, "kickoff.md"))
    return out


def _first_line_is_marker(text):
    """True iff ``text``'s FIRST line carries the auto-triage origin marker.

    ``startswith`` on the first line, never a substring test: a human-authored
    kickoff that merely QUOTES the marker further down its spec text — this
    repo's own kickoffs do — must still read as human, or the guard would fire
    on legitimate operator runs. A leading BOM is tolerated as in the file read."""
    first = text.split("\n", 1)[0] if text else ""
    return first.lstrip("\ufeff").strip().startswith(ORIGIN_MARKER)


def _read_kickoff():
    """``(first_line, sha256_hex)`` of the first READABLE kickoff candidate.

    The candidates are ``run_profile.json``'s ``kickoff_path`` first, then the
    run-local ``kickoff.md`` (``_run_kickoff_candidates``) — never
    ``context-pack.json``, which re-embeds the kickoff text and holds other
    copies, so sniffing it would let an unrelated string re-classify a run.
    Returns ``("", "")`` when no candidate is readable: a run with no kickoff
    file at all, which neither the marker nor a sha drift can match."""
    for path in _run_kickoff_candidates():
        try:
            with open(path, "rb") as fh:
                data = fh.read()
        except OSError:
            continue
        return (data.decode("utf-8", "replace").split("\n", 1)[0],
                hashlib.sha256(data).hexdigest())
    return "", ""


def _read_kickoff_text():
    """The full text of the first READABLE kickoff candidate (``""`` if none).

    Same candidate order as ``_read_kickoff`` (run_profile.json's
    ``kickoff_path``, then the run-local ``kickoff.md``), so the scope check
    reads the kickoff the pipeline actually received."""
    for path in _run_kickoff_candidates():
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                return fh.read()
        except OSError:
            continue
    return ""


def _load_pack():
    """The run's pre-run context-pack v2 (``{}`` when absent or unreadable).

    The pack is the snapshot taken before any node ran. A missing one — context
    v2 off, or a hand-run fixture — keeps the file-only behaviour: the snapshot
    and sha-drift signals simply never fire. The import is local (the walk-up
    sys.path above already made the engine importable) so a stale engine that
    predates ``context_v2`` still refuses on the file."""
    try:
        from mini_ork import context_v2
    except Exception:  # noqa: BLE001 — no pack: fall back to file-only sniffing
        return {}
    try:
        return context_v2.load_pack(RUN_DIR) or {}
    except Exception:  # noqa: BLE001
        return {}


_KICKOFF_DRIFT_EVIDENCED = [False]


def _evidence_kickoff_drift():
    """Record, once, that the kickoff file changed since the run started.

    The refusal reason is otherwise invisible: an auto-opened run that edits its
    own kickoff to hide the marker produces the same FAIL row as one that never
    carried it. The exact line is contractual."""
    if _KICKOFF_DRIFT_EVIDENCED[0]:
        return
    _KICKOFF_DRIFT_EVIDENCED[0] = True
    _ev.write("frozen-kernel: kickoff modified since run start\n")
    _ev.flush()


def _kickoff_is_auto_triage():
    """True iff the run is auto-opened (origin=auto-triage). Fail closed: ANY of
    three signals marks it.

    1. the PRE-RUN SNAPSHOT — ``context-pack.v2.json``'s ``kickoff_snapshot``,
       the contract the run RECEIVED, as its first line. Rewriting the kickoff
       file afterwards cannot un-mark this one, which is the whole point: the
       auto-promoted kickoffs live under the mini-ork repo's ``kickoffs/auto/``,
       which an auto fix epic's implementer can write.
    2. the kickoff FILE's first line (today's behaviour).
    3. sha DRIFT — the pack recorded a ``kickoff_sha256`` that no longer matches
       the file. A run that rewrote its own contract gets no human privileges,
       and the drift is written to the evidence log.

    A missing pack keeps today's file-only behaviour (1 and 3 stay inactive)."""
    pack = _load_pack()
    snapshot = pack.get("kickoff_snapshot")
    if isinstance(snapshot, str) and _first_line_is_marker(snapshot):
        return True
    first_line, sha = _read_kickoff()
    if _first_line_is_marker(first_line):
        return True
    recorded = pack.get("kickoff_sha256")
    if isinstance(recorded, str) and recorded and sha and sha != recorded:
        _evidence_kickoff_drift()
        return True
    return False


def _target_committed_frozen_paths():
    """The target's committed frozen list: ``git show HEAD:<relpath>``.

    Read from the target's HEAD blob, never the working-tree copy the diff
    modifies, so a diff cannot unfreeze a path by editing the list. Any failure
    (no HEAD, absent file, git error) yields ``[]`` — the built-in default
    still supplies the floor."""
    r = subprocess.run(
        ["git", "-C", REPO_ROOT, "show", f"HEAD:{FROZEN_CONFIG_RELPATH}"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    if r.returncode != 0:
        return []
    return parse_frozen_paths(r.stdout.decode("utf-8", "replace"))


def _frozen_patterns():
    """Engine built-in defaults ∪ engine checkout's file ∪ target HEAD's file.

    Every source only ADDS paths, so deleting entries from ``kernel-frozen.yaml``
    (in the diff or the checkout) cannot shrink the set below the engine's
    built-in floor."""
    pats = set(FROZEN_PATHS_DEFAULT)
    pats.update(load_frozen_paths(_engine))
    pats.update(_target_committed_frozen_paths())
    return sorted(pats)


def _emit_frozen_kernel_event(hits):
    """Best-effort safety-event write for a frozen-kernel refusal.

    A missing DB (the simple tier) or any other error is swallowed: recording
    the refusal is a bonus, and an emit failure must NEVER turn the FAIL into a
    pass. The check's return value is decided by the caller, not here."""
    run_id = os.environ.get("MO_RUN_ID") or os.path.basename(RUN_DIR.rstrip(os.sep))
    try:
        from mini_ork.stores.safety_events import emit
        emit("frozen-kernel-edit", "high",
             json.dumps({"run_id": run_id, "paths": hits}),
             run_id=run_id)
    except Exception:  # noqa: BLE001 — best-effort, never fail the check open
        pass


def _frozen_kernel_guard():
    """Refuse an auto-opened (origin=auto-triage) diff that touches a frozen path.

    A human-authored kickoff (no marker) is unaffected: the legacy
    high-blast-radius guard remains its only boundary. The ALLOW token can
    NEVER unlock this check — it is not consulted at all."""
    if not _kickoff_is_auto_triage():
        return True
    hits = frozen_hits(_changed_file_lines(), _frozen_patterns())
    if not hits:
        return True
    _ev.write("frozen-kernel-refusal: " + ", ".join(hits) + "\n")
    _ev.flush()
    _emit_frozen_kernel_event(hits)
    return False


def _high_blast_radius_guard():
    if any(re.match(r"^(lib/circuit_breaker\.sh|lib/throttle-guard\.sh|\.mini-ork/config/)", f)
           for f in _changed_file_lines()):
        # An auto-opened run never gets the token-unlock path: the marker means
        # the evidence it carries is untrusted, so the legacy paths fail too.
        if _kickoff_is_auto_triage():
            return False
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

_check("frozen-kernel-guard",
       "an auto-opened (origin=auto-triage) run may not change frozen kernel paths",
       _frozen_kernel_guard)


# ── scope-respected (RB-12) ─────────────────────────────────────────────────
# A kickoff's "Files in scope (touch ONLY these)" used to feed prompt
# preferences only; nothing compared it with the diff, so a run could publish
# edits to a file the kickoff ruled out (rp-e3-header-v2-20261016-111144). The
# shared ``mini_ork.kickoff_scope`` reader is the one the profile/context
# builders use, so the check sees the same allow/deny lists the run was scoped
# with. A deny-listed file always gates; a file outside the allow list gates
# only when the allow section's heading or body says ONLY / "do not modify any
# other file"; otherwise it is a recorded, non-gating hint.

_SCOPE_HEADING_RE = re.compile(r"^\s{0,3}(#{2,6})\s+(.+?)\s*$")
_SCOPE_FENCE_RE = re.compile(r"^\s{0,3}(?:```|~~~)")
_SCOPE_ONLY_HEADING_RE = re.compile(r"\bonly\b", re.IGNORECASE)
_SCOPE_ONLY_BODY_RE = re.compile(
    r"do\s+not\s+modify\s+any\s+other\s+file", re.IGNORECASE)


def _normalise_scope_path(path):
    """Repo-relative form: strip ``./`` (the scope parser already does this for
    kickoff entries; diff paths arrive plain)."""
    p = (path or "").strip()
    while p.startswith("./"):
        p = p[2:]
    return p


def _scope_match(path, patterns):
    """True when ``path`` matches any allow/deny entry; globs use ``fnmatch``."""
    return any(fnmatch.fnmatch(path, pattern) for pattern in patterns)


def _only_scope_declared(text):
    """True iff an ALLOW section's heading or body declares an ONLY scope.

    Walk the kickoff exactly as ``kickoff_scope`` does (fences skipped, deny
    sections ignored — an ONLY in "Out of scope" is not an allow-only rule):
    the heading title and every body line under an allow section are tested for
    the word ONLY or the phrase "do not modify any other file"."""
    from mini_ork.kickoff_scope import heading_kind

    current = None
    in_fence = False
    for line in (text or "").splitlines():
        if _SCOPE_FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = _SCOPE_HEADING_RE.match(line)
        if m:
            title = m.group(2)
            current = heading_kind(title)
            if current == "allow" and _SCOPE_ONLY_HEADING_RE.search(title):
                return True
            continue
        if current == "allow" and _SCOPE_ONLY_BODY_RE.search(line):
            return True
    return False


def _scope_respected():
    try:
        from mini_ork.kickoff_scope import kickoff_scope
    except Exception:  # noqa: BLE001 — no parser: fail closed, never pass open
        _ev.write("scope-respected: mini_ork.kickoff_scope unavailable\n")
        _ev.flush()
        return False

    text = _read_kickoff_text()
    scope = kickoff_scope(text)
    allow = [_normalise_scope_path(p) for p in scope.get("allow", [])]
    deny = [_normalise_scope_path(p) for p in scope.get("deny", [])]
    _changed_files()
    changed = [_normalise_scope_path(p) for p in _changed_file_lines()]

    _ev.write(f"scope-respected: allow={allow} deny={deny} changed={changed}\n")
    _ev.flush()

    denied = sorted({p for p in changed if _scope_match(p, deny)})
    if denied:
        _ev.write("scope-respected: gating: deny-listed file(s) in diff: "
                  + ", ".join(denied) + "\n")
        _ev.flush()
        return False

    if not allow:
        _ev.write("scope-respected: no scope declared\n")
        _ev.flush()
        return True

    outside = sorted({p for p in changed if not _scope_match(p, allow)})
    if not outside:
        _ev.write("scope-respected: all changed files inside the declared scope\n")
        _ev.flush()
        return True

    if _only_scope_declared(text):
        _ev.write("scope-respected: gating: ONLY scope violated; file(s) "
                  "outside allow: " + ", ".join(outside) + "\n")
        _ev.flush()
        return False

    # A plain "Files in scope" section is a hint, not a boundary: record the
    # overrun but let the run continue.
    _ev.write("scope-respected: non-gating: file(s) outside allow: "
              + ", ".join(outside) + " (gating: false)\n")
    _ev.flush()
    return True


_check("scope-respected",
       "diff files respect the kickoff's declared file scope",
       _scope_respected)


def _no_test_gaming():
    """Fail a diff whose production code detects or patches the test harness.

    eco6 K10 (spec-suite detection + a pytest MonkeyPatch shim) and K12 (an
    import-time write into scheduler._RETRY_COLUMNS) passed every other check
    here. mini_ork.verify.gaming_scan reads ADDED lines of non-test .py files and
    the patched copy (for the cross-module-write AST rule). A kickoff line
    ``Allow-test-probe: <path>`` exempts one path; the implementer cannot write
    it, because the kickoff is frozen at launch. An engine without the scanner
    fails closed.
    """
    try:
        from mini_ork.verify import gaming_scan  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        _ev.write(f"  gaming_scan unavailable in this engine: {exc}\n")
        return False
    try:
        with open(DIFF, encoding="utf-8", errors="replace") as fh:
            diff_text = fh.read()
    except OSError as exc:
        _ev.write(f"  cannot read {DIFF}: {exc}\n")
        return False

    def _read(rel):
        try:
            with open(os.path.join(WORKTREE, rel), encoding="utf-8") as fh:
                return fh.read()
        except (OSError, UnicodeDecodeError):
            return None

    allowed = gaming_scan.kickoff_allowances(_read_kickoff_text())
    findings = gaming_scan.scan_diff(diff_text, read_file=_read, allowed_paths=allowed)
    for f in findings:
        _ev.write(f"  {f.path}:{f.line}: {f.rule}: {f.text}\n")
    if allowed:
        _ev.write("  kickoff allows: " + ", ".join(sorted(allowed)) + "\n")
    return not findings


_check("no-test-gaming",
       "diff adds no test-harness detection or cross-module patching to production code",
       _no_test_gaming)

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
    "artifact_sha256": ARTIFACT_SHA256,
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
