#!/usr/bin/env python3
"""mini_ork_worktree.py — worktree-first dev for mini-ork (Python port).

Keep `main` clean: never branch/commit implementation work in the main
checkout. Each task gets its own worktree + branch; when green it rebases onto
origin/main and pushes straight to main; then the worktree is torn down.

Port of scripts/mini-ork-worktree.sh (bash-removal Phase 4). Same subcommands,
claim registry location + format ($WORKTREES_DIR/.ownership, TSV slug<TAB>path),
ALLOW_WORKTREE_BRANCH_CREATE=1 for `git worktree add` (the reference-transaction
guard requires it), stderr message shapes, and exit codes (1 = error, 2 = usage).

Usage:
  scripts/mini_ork_worktree.py create <slug> [--owns <path>...] [--branch <name>]
  scripts/mini_ork_worktree.py merge  [<slug>]        # rebase origin/main, test, push HEAD:main
  scripts/mini_ork_worktree.py clean  <slug>          # remove worktree + delete branch + release claims
  scripts/mini_ork_worktree.py owners [--json]        # list active file claims
  scripts/mini_ork_worktree.py release <slug>         # drop a slug's claims
  scripts/mini_ork_worktree.py list                   # git worktree list

Dev loop:
  create → work + commit in the worktree → merge (green-gated push to main) → clean

--owns <path> (repeatable) CLAIMS those paths; creation is refused if a claim
overlaps a live worktree's claim (path-prefix aware). Released on `clean`/`release`
or when the worktree dir disappears.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys

DEFAULT_WORKTREES_DIR = "/Volumes/docker-ssd/ps/mini-ork-worktrees"
DEFAULT_TEST_CMD = "python3 -m pytest -q"


def die(msg: str) -> "SystemExit":
    print(f"[mo-worktree] {msg}", file=sys.stderr)
    raise SystemExit(1)


def git(*args: str, cwd: str | None = None, check: bool = True,
        capture: bool = False, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=check,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        env=env,
    )


def detect_root() -> str:
    """The main checkout: the worktree currently on `main`."""
    try:
        out = git("worktree", "list", "--porcelain", check=True, capture=True).stdout
    except (subprocess.CalledProcessError, OSError):
        return ""
    wt = ""
    for line in out.splitlines():
        if line.startswith("worktree "):
            wt = line.split(" ", 1)[1]
        elif line.startswith("branch ") and line.split(" ", 1)[1] == "refs/heads/main":
            return wt
    return ""


ROOT = os.environ.get("MINI_ORK_ROOT") or detect_root()
WORKTREES_DIR = os.environ.get("MINI_ORK_WORKTREES_DIR", DEFAULT_WORKTREES_DIR)
BRANCH_PREFIX = os.environ.get("MINI_ORK_BRANCH_PREFIX", "wt")
OWNERSHIP_FILE = os.environ.get("MINI_ORK_OWNERSHIP_FILE",
                                os.path.join(WORKTREES_DIR, ".ownership"))


def sanitize_slug(slug: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]", "-", slug)
    # bash ${slug##-} / ${slug%%-}: strip exactly one leading/trailing dash.
    if slug.startswith("-"):
        slug = slug[1:]
    if slug.endswith("-"):
        slug = slug[:-1]
    if not slug:
        die("slug must contain at least one alphanumeric character")
    return slug


def assert_root() -> None:
    if not ROOT:
        die("could not locate the main worktree; set MINI_ORK_ROOT")
    if not os.path.isdir(os.path.join(ROOT, ".git")):
        rc = subprocess.run(["git", "-C", ROOT, "rev-parse", "--git-dir"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            check=False).returncode
        if rc != 0:
            die(f"ROOT is not a git checkout: {ROOT}")


def _supersede_gates(target_dir: str, note: str) -> list[str]:
    """Close pending retry gates bound to a just-merged/removed worktree.

    Stays standalone by intent (no top-level ``mini_ork`` import): bootstrap
    ROOT onto ``sys.path`` at the call site and fail soft — a gate problem
    must never fail an operation that already succeeded. Returns the closed
    run ids (``[]`` when the package is unavailable or nothing matched).
    """
    home = os.environ.get("MINI_ORK_HOME") or os.path.join(ROOT, ".mini-ork")
    try:
        if ROOT and ROOT not in sys.path:
            sys.path.insert(0, ROOT)
        from mini_ork.recovery import retry_notify
        return retry_notify.supersede_gates_for_target(home, target_dir, note)
    except Exception as exc:
        # Fail soft, but visibly: a stale ROOT checkout (local main lags
        # origin after a push) may lack the helper, leaving gates open.
        print(f"[mo-worktree] warn: retry gates for {target_dir} not closed: "
              f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return []


# ── CAID file-ownership registry ───────────────────────────────────────────

def normalize_path(p: str) -> str:
    if p.startswith("./"):
        p = p[2:]
    return p.rstrip("/")


def paths_overlap(a: str, b: str) -> bool:
    a, b = normalize_path(a), normalize_path(b)
    if a == b:
        return True
    return (b + "/").startswith(a + "/") or (a + "/").startswith(b + "/")


def _read_ownership() -> list[tuple[str, str]]:
    if not os.path.isfile(OWNERSHIP_FILE):
        return []
    rows = []
    with open(OWNERSHIP_FILE, encoding="utf-8") as f:
        for line in f:
            parts = line.rstrip("\n").split("\t")
            if len(parts) >= 2 and parts[0]:
                rows.append((parts[0], parts[1]))
    return rows


def _write_ownership(rows: list[tuple[str, str]]) -> None:
    with open(OWNERSHIP_FILE, "w", encoding="utf-8") as f:
        for slug, path in rows:
            f.write(f"{slug}\t{path}\n")


def prune_ownership() -> None:
    if not os.path.isfile(OWNERSHIP_FILE):
        return
    _write_ownership([
        (slug, path) for slug, path in _read_ownership()
        if os.path.isdir(os.path.join(WORKTREES_DIR, slug))
    ])


def assert_no_ownership_conflict(slug: str, claims: list[str]) -> None:
    prune_ownership()
    for rslug, rpath in _read_ownership():
        if rslug == slug:
            continue
        for claim in claims:
            if paths_overlap(claim, rpath):
                die(f"ownership conflict: '{claim}' overlaps '{rpath}' held by live "
                    f"worktree '{rslug}'. Pick a non-overlapping surface, wait for "
                    f"'{rslug}' to merge, or 'release {rslug}' if it's stale.")


def register_ownership(slug: str, claims: list[str]) -> None:
    os.makedirs(WORKTREES_DIR, exist_ok=True)
    with open(OWNERSHIP_FILE, "a", encoding="utf-8") as f:
        for claim in claims:
            f.write(f"{slug}\t{normalize_path(claim)}\n")


def release_ownership(slug: str) -> None:
    if not os.path.isfile(OWNERSHIP_FILE):
        return
    _write_ownership([(rslug, rpath) for rslug, rpath in _read_ownership()
                      if rslug != slug])


def list_owners(json_mode: bool) -> None:
    prune_ownership()
    rows = _read_ownership()
    if json_mode:
        print(json.dumps([{"slug": slug, "path": path} for slug, path in rows],
                         separators=(",", ":")))
    elif rows:
        for slug, path in rows:
            print(f"{slug}\t{path}")
    else:
        print("(no active claims)")


# ── Concord registration (best-effort, fail-open) ─────────────────────────
# A worktree is registered with ContextNest as principal agent:wt-<slug>, with
# its --owns claims as labels, so the per-turn precheck can flag edits inside
# the worktree that fall outside its claimed surface (Concord P2d, audit mode).
# Inline urllib on purpose: this script stays standalone (no mini_ork import,
# runs under the system python3 that `make` resolves).

def _concord(method: str, principal: str, body: dict | None = None) -> None:
    if os.environ.get("MO_CONCORD", "1") == "0":
        return
    import urllib.parse
    import urllib.request
    base = os.environ.get("CN_BASE_URL", "http://127.0.0.1:28080").rstrip("/")
    url = f"{base}/api/v1/coord/principals/{urllib.parse.quote(principal, safe='')}"
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if data is not None else {}
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        timeout = float(os.environ.get("CN_COORD_TIMEOUT_SEC", "2"))
        urllib.request.urlopen(req, timeout=timeout).read()
    except Exception:
        pass  # Concord must never block worktree create/clean


def _worktree_principal(slug: str) -> str:
    return f"agent:wt-{slug}"[:134]


# ── commands ───────────────────────────────────────────────────────────────

def create_worktree(slug: str, opts: list[str]) -> None:
    branch = ""
    owns: list[str] = []
    i = 0
    while i < len(opts):
        if opts[i] == "--owns":
            if i + 1 >= len(opts):
                die("--owns requires a path")
            owns.append(opts[i + 1])
            i += 2
        elif opts[i] == "--branch":
            if i + 1 >= len(opts):
                die("--branch requires a name")
            branch = opts[i + 1]
            i += 2
        else:
            die(f"unknown create option: {opts[i]}")
    assert_root()
    safe_slug = sanitize_slug(slug)
    if not branch:
        branch = f"{BRANCH_PREFIX}/{safe_slug}"
    wt = os.path.join(WORKTREES_DIR, safe_slug)

    if owns:
        assert_no_ownership_conflict(safe_slug, owns)
    if os.path.exists(wt):
        die(f"worktree path already exists: {wt}")
    os.makedirs(WORKTREES_DIR, exist_ok=True)

    # Sync to origin/main so the branch starts from the latest published tip.
    git("-C", ROOT, "fetch", "--quiet", "origin", "main", check=False)
    base = git("-C", ROOT, "rev-parse", "--verify", "--quiet", "origin/main",
               check=False, capture=True).stdout.strip()
    if not base:
        base = git("-C", ROOT, "rev-parse", "HEAD", capture=True).stdout.strip()
    # ALLOW_WORKTREE_BRANCH_CREATE=1 satisfies the reference-transaction guard.
    env = {**os.environ, "ALLOW_WORKTREE_BRANCH_CREATE": "1"}
    git("-C", ROOT, "worktree", "add", "-b", branch, wt, base, env=env)

    if owns:
        register_ownership(safe_slug, owns)
        print(f"[mo-worktree] claimed: {' '.join(owns)}", file=sys.stderr)
    _concord("PUT", _worktree_principal(safe_slug), {
        "harness": "worktree", "cwd": wt, "worktree": wt,
        "labels": {"kind": "worktree", "branch": branch,
                   "owns": [normalize_path(c) for c in owns]},
    })
    print(f"[mo-worktree] ready: {wt}  (branch {branch})")


def merge_worktree(args: list[str]) -> None:
    if args:
        slug = sanitize_slug(args[0])
        wt = os.path.join(WORKTREES_DIR, slug)
        if not os.path.isdir(wt):
            die(f"no worktree for slug '{slug}' at {wt}")
    else:
        wt = git("rev-parse", "--show-toplevel", capture=True).stdout.strip()
        slug = os.path.basename(wt)
    if wt == ROOT:
        die("refusing to merge from the main checkout; run merge inside a task worktree")
    dirty = git("-C", wt, "status", "--porcelain", capture=True).stdout
    if dirty:
        die(f"worktree is dirty: commit or stash before merging: {wt}")
    branch = git("-C", wt, "rev-parse", "--abbrev-ref", "HEAD", capture=True).stdout.strip()

    git("-C", wt, "fetch", "origin", "main")
    pre_rebase = git("-C", wt, "rev-parse", "HEAD", capture=True).stdout.strip()
    base_before = git("-C", wt, "merge-base", "HEAD", "origin/main", capture=True).stdout.strip()
    git("-C", wt, "rebase", "origin/main")
    # Green gate: never push a red branch to main. Override the command per-task
    # with MINI_ORK_TEST_CMD (e.g. a scoped pytest path for a fast, focused gate).
    test_cmd = os.environ.get("MINI_ORK_TEST_CMD", DEFAULT_TEST_CMD)
    rc = subprocess.run(test_cmd, cwd=wt, shell=True, check=False).returncode
    if rc != 0:
        die(f"green gate failed ({test_cmd}) in {wt}; "
            f"{_differential_verdict(wt, test_cmd, pre_rebase, base_before)}")
    git("-C", wt, "push", "origin", "HEAD:main")
    short_sha = git("-C", wt, "rev-parse", "--short", "HEAD",
                    check=False, capture=True).stdout.strip()
    closed = _supersede_gates(
        wt, f"superseded: merged to main as {short_sha}")
    if closed:
        print(f"[mo-worktree] closed retry gate(s): {', '.join(closed)}")
    print(f"[mo-worktree] merged {branch} -> origin/main. "
          f"Tear down with: scripts/mini_ork_worktree.py clean {slug}")


def _differential_verdict(wt: str, test_cmd: str, pre_rebase: str, base_before: str) -> str:
    """Classify a red green gate (Concord P4, "passes alone, fails together").

    The gate ran on the rebased tree, i.e. this branch COMBINED with whatever
    landed on main meanwhile. When main moved, re-run the same command on the
    pre-rebase commit in a throwaway detached worktree: if it passes there, the
    branch is green alone and the failure comes from combining it with the
    named upstream commits — a semantic conflict that git merged cleanly.
    MO_MERGE_DIFFERENTIAL=0 skips the extra run.
    """
    new_base = git("-C", wt, "rev-parse", "origin/main", capture=True,
                   check=False).stdout.strip()
    if os.environ.get("MO_MERGE_DIFFERENTIAL", "1") == "0" or not base_before \
            or new_base == base_before:
        return "fix before merging"
    upstream = git("-C", wt, "log", "--format=%h %s", f"{base_before}..{new_base}",
                   capture=True, check=False).stdout.strip().splitlines()
    tmp = os.path.join(WORKTREES_DIR, f".differential-{os.getpid()}")
    alone_rc = None
    try:
        if git("-C", ROOT, "worktree", "add", "--detach", tmp, pre_rebase,
               check=False, capture=True).returncode == 0:
            alone_rc = subprocess.run(test_cmd, cwd=tmp, shell=True, check=False,
                                      capture_output=True).returncode
    finally:
        git("-C", ROOT, "worktree", "remove", "--force", tmp, check=False, capture=True)
    shown = "; ".join(upstream[:5]) + (f" (+{len(upstream) - 5} more)" if len(upstream) > 5 else "")
    if alone_rc == 0:
        return (f"SEMANTIC CONFLICT: the branch passes alone (pre-rebase {pre_rebase[:8]}) "
                f"but fails combined with {len(upstream)} upstream commit(s): {shown}. "
                "Reconcile with that work before merging.")
    if alone_rc is None:
        return "fix before merging (differential re-run could not create its worktree)"
    return f"the branch also fails alone (pre-rebase {pre_rebase[:8]}); fix before merging"


def clean_worktree(slug_arg: str) -> None:
    slug = sanitize_slug(slug_arg)
    wt = os.path.join(WORKTREES_DIR, slug)
    assert_root()
    if os.path.isdir(wt):
        branch = git("-C", wt, "rev-parse", "--abbrev-ref", "HEAD",
                     check=False, capture=True).stdout.strip()
        closed = _supersede_gates(
            wt, f"superseded: worktree {slug} removed")
        if closed:
            print(f"[mo-worktree] closed retry gate(s): {', '.join(closed)}")
        rc = git("-C", ROOT, "worktree", "remove", wt, check=False).returncode
        if rc != 0:
            git("-C", ROOT, "worktree", "remove", "--force", wt)
        if branch and branch != "main":
            git("-C", ROOT, "branch", "-d", branch, check=False,
                capture=True)
    release_ownership(slug)
    _concord("DELETE", _worktree_principal(slug))
    print(f"[mo-worktree] cleaned {slug}")


def usage() -> None:
    print(_USAGE)


_USAGE = """Usage:
  scripts/mini_ork_worktree.py create <slug> [--owns <path>...] [--branch <name>]
  scripts/mini_ork_worktree.py merge  [<slug>]        # rebase origin/main, test, push HEAD:main
  scripts/mini_ork_worktree.py clean  <slug>          # remove worktree + delete branch + release claims
  scripts/mini_ork_worktree.py owners [--json]        # list active file claims
  scripts/mini_ork_worktree.py release <slug>         # drop a slug's claims
  scripts/mini_ork_worktree.py list                   # git worktree list

Dev loop:
  create → work + commit in the worktree → merge (green-gated push to main) → clean

--owns <path> (repeatable) CLAIMS those paths; creation is refused if a claim
overlaps a live worktree's claim (path-prefix aware). Released on `clean`/`release`
or when the worktree dir disappears."""


def main(argv: list[str]) -> int:
    if not argv:
        usage()
        return 2
    cmd, rest = argv[0], argv[1:]
    if cmd == "create":
        if not rest:
            die("usage: create <slug> [--owns <path>...] [--branch <name>]")
        create_worktree(rest[0], rest[1:])
    elif cmd == "merge":
        merge_worktree(rest)
    elif cmd == "clean":
        if len(rest) != 1:
            die("usage: clean <slug>")
        clean_worktree(rest[0])
    elif cmd == "owners":
        list_owners(bool(rest and rest[0] == "--json"))
    elif cmd == "release":
        if len(rest) != 1:
            die("usage: release <slug>")
        release_ownership(sanitize_slug(rest[0]))
        print(f"[mo-worktree] released claims for {rest[0]}")
    elif cmd == "list":
        git("worktree", "list")
    elif cmd in ("-h", "--help", "help"):
        usage()
    else:
        usage()
        return 2
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except subprocess.CalledProcessError as exc:
        # Mirror `set -e`: a failed child command aborts with its exit code.
        sys.exit(exc.returncode or 1)
    except KeyboardInterrupt:
        sys.exit(130)
