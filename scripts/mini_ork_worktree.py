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
from collections.abc import Iterable, Iterator

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


# ── OSS guard: refuse to publish unclaimed paths or confidential material ───
# `main` is public. Before `merge` rebases this branch's own commits onto
# origin/main and pushes them, two cheap, deterministic checks run at that one
# choke point: the branch must stay inside its --owns claims, and its added
# lines must carry no confidential terms or credential shapes. Both run after
# the rebase (so `origin/main..HEAD` is exactly the branch's own commits) and
# before the green gate and `git push` — a refusal pushes nothing.
#
# The content check scans EVERY commit's patch plus every commit message, not
# just the net diff: a secret added in one commit and removed in the next still
# lands in public history once pushed.

# One private-terms file (one regex per line), untracked and never committed:
# the names it holds are themselves sensitive, so a refusal names a private
# match only by its line number in the file. Absent file ⇒ ignored.
_TERMS_FILE = "oss-guard-terms.txt"

# Generic, OSS-safe terms only. Product/company/person names belong in the
# private terms file — never in this code, the tests, the docs or the kickoff.
_CONFIDENTIAL_RE = (
    r"fundrais|investor|pitch[ -]?deck|venture capital|\bvaluation\b"
    r"|seed round|term sheet|@gmail\.com"
)

# Credential shapes. Each alternative is a named group so a refusal can name
# WHICH shape matched without ever echoing the secret itself.
_CREDENTIAL_RE = re.compile(
    r"(?P<openai_key>sk-[A-Za-z0-9_-]{24,})"
    r"|(?P<github_pat>ghp_[A-Za-z0-9]{30,})"
    r"|(?P<github_fine_grained_pat>github_pat_[A-Za-z0-9_]{30,})"
    r"|(?P<aws_access_key>AKIA[0-9A-Z]{16})"
    r"|(?P<slack_token>xox[bpa]-[A-Za-z0-9-]{10,})"
    r"|(?P<private_key>-----BEGIN [A-Z ]*PRIVATE KEY)"
)

# A credential-shaped value that names itself a fixture is not a leak
# (`sk-test-value-never-shown-…`). Scoped to the matched value, not the line,
# so `key = "sk-live-…"  # not a test` is still refused.
_FIXTURE_WORDS = ("test", "fake", "dummy", "example")

# The guard's own definition and tests necessarily spell out the terms and
# key shapes they refuse; their added lines are not scanned.
_OSS_SELF = ("scripts/mini_ork_worktree.py", "tests/unit/test_merge_oss_guard.py")

_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
_FILE_HEADER_RE = re.compile(r"^\+\+\+ b/(.+)$")


# Pin the diff format against user/repo config: colour (color.ui=always would
# prefix every line with an ANSI escape and make the scan fail open), external
# diff drivers, textconv, noprefix/mnemonic prefixes and rename detection.
_PLAIN_DIFF = ("--no-color", "--no-ext-diff", "--no-textconv", "--no-renames",
               "--src-prefix=a/", "--dst-prefix=b/")


def _branch_paths(wt: str) -> list[str]:
    """Paths this branch changes vs origin/main, i.e. its own commits.

    ``--no-renames``: a rename out of an unclaimed path lists that path too.
    """
    out = git("-C", wt, "-c", "core.quotepath=off", "diff", "--name-only",
              "--no-renames", "origin/main...HEAD", capture=True).stdout
    return [p for p in out.splitlines() if p.strip()]


def _added_lines(diff_text: str) -> Iterator[tuple[str, int, str]]:
    """Yield ``(path, lineno, text)`` for every added line of a patch.

    Same walk as ``mini_ork/review/lenses.py::check_secret_patterns`` (path from
    the ``+++ b/`` header, new-file line numbers from each ``@@`` hunk), but
    hunk-aware: inside a hunk every ``+`` line is content, so an added line
    that itself starts with ``++`` is scanned rather than mistaken for a file
    header. Anything that is not ``+``/``-``/`` ``/``\\`` ends the hunk. Kept
    local on purpose — this script is standalone and does not import
    ``mini_ork``.
    """
    path = "?"
    lineno = 0
    in_hunk = False
    for raw in diff_text.split("\n"):
        if in_hunk:
            if raw.startswith("+"):
                yield path, lineno, raw[1:]
                lineno += 1
                continue
            if raw.startswith(" "):
                lineno += 1
                continue
            if raw.startswith(("-", "\\")):
                continue  # removed lines and "\ No newline" keep the counter
            in_hunk = False  # fall through: a header, a new hunk, a commit line
        hunk = _HUNK_RE.match(raw)
        if hunk:
            lineno = int(hunk.group(1))
            in_hunk = True
            continue
        header = _FILE_HEADER_RE.match(raw)
        if header:
            path = header.group(1)


def _message_lines(wt: str) -> Iterator[tuple[str, int, str]]:
    """Yield ``("commit <sha> message", lineno, text)`` for each branch commit."""
    out = git("-C", wt, "log", "--format=%h%x01%B%x00", "origin/main..HEAD",
              capture=True).stdout
    for record in out.split("\x00"):
        sha, sep, body = record.strip("\n").partition("\x01")
        if not sep:
            continue
        for i, line in enumerate(body.splitlines(), 1):
            yield f"commit {sha} message", i, line


def _oss_terms_path() -> str:
    home = os.environ.get("MINI_ORK_HOME") or os.path.join(ROOT, ".mini-ork")
    return os.path.join(home, _TERMS_FILE)


def _confidential_patterns() -> list[tuple[str, str]]:
    """``(regex, label)`` pairs: the committed generic regex plus private terms.

    A generic match is labelled by its matched text; a private term only as
    ``private term #<line>``, so the sensitive name never reaches a log.
    """
    patterns = [(_CONFIDENTIAL_RE, "")]
    terms = _oss_terms_path()
    if os.path.isfile(terms):
        with open(terms, encoding="utf-8") as f:
            for n, line in enumerate(f, 1):
                if line.strip():
                    patterns.append((line.strip(), f"private term #{n}"))
    return patterns


def _is_fixture_value(value: str) -> bool:
    low = value.lower()
    return any(word in low for word in _FIXTURE_WORDS)


def _scan(lines: Iterable[tuple[str, int, str]]) -> list[str]:
    """``path:line matches <term>`` notes for lines that must not ship.

    The offending line is never included — only the location and the matched
    generic term, a private term's number, or the credential shape's name — so
    neither a secret nor a private name is echoed.
    """
    patterns = _confidential_patterns()
    found: list[str] = []
    for path, lineno, line in lines:
        if path in _OSS_SELF:
            continue
        for pat, label in patterns:
            m = re.search(pat, line, re.IGNORECASE)
            if m:
                found.append(f"{path}:{lineno} matches {label or m.group(0)}")
        for m in _CREDENTIAL_RE.finditer(line):
            if not _is_fixture_value(m.group(0)):
                found.append(f"{path}:{lineno} matches {m.lastgroup}")
    return list(dict.fromkeys(found))


def _oss_findings(diff_text: str) -> list[str]:
    """Findings for the added lines of one patch text."""
    return _scan(_added_lines(diff_text))


def _summarize(findings: list[str], limit: int = 10) -> str:
    shown = "; ".join(findings[:limit])
    if len(findings) > limit:
        shown += f" (+{len(findings) - limit} more)"
    return shown


def _check_claims(slug: str, wt: str) -> None:
    """A claimed worktree may only merge paths it claimed (or its kickoff)."""
    claims = [path for rslug, path in _read_ownership() if rslug == slug]
    if not claims:
        return  # old worktree, created without --owns: nothing to enforce
    allowed_kickoff = f"kickoffs/auto/{slug}.md"
    outside = [
        p for p in _branch_paths(wt)
        if normalize_path(p) != allowed_kickoff
        and not any(paths_overlap(p, claim) for claim in claims)
    ]
    if not outside:
        return
    if os.environ.get("MO_MERGE_ALLOW_UNCLAIMED") == "1":
        print(f"[mo-worktree] warn: MO_MERGE_ALLOW_UNCLAIMED=1 — merging "
              f"{len(outside)} unclaimed path(s): {', '.join(outside)}",
              file=sys.stderr)
        return
    die(f"merge refused: {len(outside)} path(s) outside this worktree's "
        f"claims: {', '.join(outside)} — add --owns or drop them")


def _check_oss_content(wt: str) -> None:
    """No confidential term or credential shape may reach the public main.

    Every branch commit's own patch is scanned (history, not the net diff),
    then every commit message.
    """
    history = git("-C", wt, "-c", "core.quotepath=off", "log", "-p",
                  *_PLAIN_DIFF, "--format=commit %H", "origin/main..HEAD",
                  capture=True).stdout
    found = _oss_findings(history) + _scan(_message_lines(wt))
    if not found:
        return
    if os.environ.get("MO_MERGE_ALLOW_OSS") == "1":
        print(f"[mo-worktree] warn: MO_MERGE_ALLOW_OSS=1 — allowing "
              f"{len(found)} confidential/secret finding(s) into the public "
              f"main: {_summarize(found)}", file=sys.stderr)
        return
    die(f"merge refused: {len(found)} confidential/secret finding(s): "
        f"{_summarize(found)} — remove it, or set MO_MERGE_ALLOW_OSS=1 only "
        f"for a reviewed public mention")


def _oss_guard(wt: str, slug: str) -> None:
    """The single choke point: claims first, then content. Fails closed."""
    _check_claims(slug, wt)
    _check_oss_content(wt)


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
    # Fail fast before the (slower) green gate and before anything is pushed:
    # the branch must stay inside its claims and carry nothing confidential.
    _oss_guard(wt, slug)
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
            rc = git("-C", ROOT, "worktree", "remove", "--force", wt,
                     check=False).returncode
        if rc != 0:
            # Not a registered worktree any more (its admin dir under
            # .git/worktrees was pruned by another process). Still release the
            # claims and the branch below — a stale claim would refuse every
            # later worktree on the same files — and leave the directory for a
            # human to inspect rather than deleting unknown content.
            print(f"[mo-worktree] warn: {wt} is not a registered worktree; "
                  "releasing its claims and leaving the directory in place",
                  file=sys.stderr)
            if not branch or branch == "HEAD":
                branch = f"wt/{slug}"
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
