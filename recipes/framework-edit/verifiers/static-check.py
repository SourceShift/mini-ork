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
        tarfile.open(fileobj=io.BytesIO(archived.stdout)).extractall(target)


def _make_patched_copy():
    shutil.rmtree(WORK_PARENT, ignore_errors=True)
    os.makedirs(WORKTREE)
    archive = subprocess.run(["git", "-C", REPO_ROOT, "archive", _base_ref()],
                             stdout=subprocess.PIPE, check=True).stdout
    tarfile.open(fileobj=io.BytesIO(archive)).extractall(WORKTREE)
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


def _kickoff_is_auto_triage():
    """True iff the run's OWN kickoff carries the auto-triage origin marker as
    its FIRST line.

    The run's kickoff is ``run_profile.json``'s ``kickoff_path`` (falling back to
    ``RUN_DIR/kickoff.md``), never ``context-pack.json`` — which re-embeds the
    kickoff text and holds other copies, so sniffing it would let an unrelated
    string re-classify a run. The marker must be the FIRST line (``startswith``,
    never a substring test): a human-authored kickoff that merely QUOTES the
    marker in its spec text — this repo's own kickoffs do — must still read as
    human, or the guard would fire on legitimate operator runs."""
    for path in _run_kickoff_candidates():
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                first_line = fh.readline()
        except OSError:
            continue
        return first_line.lstrip("\ufeff").strip().startswith(ORIGIN_MARKER)
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
