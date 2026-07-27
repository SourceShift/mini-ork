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
    git("-C", wt, "rebase", "origin/main")
    # Green gate: never push a red branch to main. Override the command per-task
    # with MINI_ORK_TEST_CMD (e.g. a scoped pytest path for a fast, focused gate).
    test_cmd = os.environ.get("MINI_ORK_TEST_CMD", DEFAULT_TEST_CMD)
    rc = subprocess.run(test_cmd, cwd=wt, shell=True, check=False).returncode
    if rc != 0:
        die(f"green gate failed ({test_cmd}) in {wt}; fix before merging")
    git("-C", wt, "push", "origin", "HEAD:main")
    print(f"[mo-worktree] merged {branch} -> origin/main. "
          f"Tear down with: scripts/mini_ork_worktree.py clean {slug}")


def clean_worktree(slug_arg: str) -> None:
    slug = sanitize_slug(slug_arg)
    wt = os.path.join(WORKTREES_DIR, slug)
    assert_root()
    if os.path.isdir(wt):
        branch = git("-C", wt, "rev-parse", "--abbrev-ref", "HEAD",
                     check=False, capture=True).stdout.strip()
        rc = git("-C", ROOT, "worktree", "remove", wt, check=False).returncode
        if rc != 0:
            git("-C", ROOT, "worktree", "remove", "--force", wt)
        if branch and branch != "main":
            git("-C", ROOT, "branch", "-d", branch, check=False,
                capture=True)
    release_ownership(slug)
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
