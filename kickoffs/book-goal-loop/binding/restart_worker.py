#!/usr/bin/env python3
"""worker-restart binding: make the goal-loop fix LIVE by restarting the
researcher book-generation worker so it runs the worktree's FIXED code.

This is the 'deploy' step in LOCAL-WORKER mode (MO_GOAL_DEPLOY_MODE=local-worker):
it lands the fix on the RUNNING worker WITHOUT a push to researcher origin/main.
The prod-push path (MO_GOAL_APPLY_CMD=git push …) stays available for the real
all-books rollout; this path proves the fix first — reversible, dev-scoped, one
book's spend.

Why we stop the SUPERVISOR, not the worker
------------------------------------------
The book-generation worker is NOT a bare `tsx` process — it is supervised by
`scripts/dev-worker-watchdog.sh book-generation` (started by `make worker-book`,
run in the FOREGROUND so the make process blocks on it). The watchdog owns a
`while true` respawn loop: on ANY child death with SHOULD_EXIT=0 it restarts the
worker FROM ITS OWN checkout — and crash restarts are never gated ("recovery is
the point", dev-worker-watchdog.sh:23). So SIGTERMing the leaf worker just feeds
the supervisor: it instantly respawns a PRIMARY-checkout (unfixed) worker that
wins the freed `bull:${QUEUE_PREFIX}worker-singleton:book-generation` SETNX lock,
and our worktree replacement then logs "Singleton lock held … refusing to start"
and exits. That is the exact race that failed the first live run.

The fix is to signal the SUPERVISOR. The watchdog carries
`trap shutdown INT TERM` (dev-worker-watchdog.sh:210): on SIGTERM it gracefully
kills its own worker child (which releases the singleton lock FIRST,
runWorker.ts:357) and `exit 0` WITHOUT respawning (the respawn loop only runs
while SHOULD_EXIT=0). Stopping the make parent follows for free — it is blocked
in the watchdog's foreground, so it returns when the watchdog exits.

Why the worktree replacement is a supervised, drop-in, drain-aware worker
-------------------------------------------------------------------------
We restart by launching the watchdog FROM THE WORKTREE
(`bash <worktree>/scripts/dev-worker-watchdog.sh book-generation`). Because the
watchdog resolves PROJECT_ROOT from its own path, the worktree copy runs the
worktree's `runWorker.ts` — i.e. the W15-fixed source. The sanctioned worktree
symlinks the primary's node_modules and shares its `server/.env`, so QUEUE_PREFIX,
HATCHET_CLIENT_TOKEN/HOST_PORT, REDIS_* and POSTGRES_* are identical; the worker's
registered identity is `(workerName, QUEUE_PREFIX)` where
`workerName = <role arg>` (runWorker.ts:526), NOT the hostname — so the fixed
worker registers under the SAME `(book-generation, dev_)` identity the incumbent
used, which is exactly what forceResumeJob's readiness preflight looks up. And
because it is under a watchdog (not bare), it keeps DRAIN-AWARE restarts through
the 90-minute regen instead of dying on the first source touch or crash.

Readiness is best-effort
-------------------------
GET /api/health/hatchet (worker_ready) is cross-process (API on :PORT is not the
worker process) and can 500 `readiness_unavailable`. So the PRIMARY readiness
signal here is the child's own stdout carrying
'Hatchet book-generation worker registered' (runWorker.ts:812); the HTTP probe
is a secondary confirmation. Even a ready worker may still hit forceResumeJob's
strict `worker_not_ready` preflight — that is the redispatch step's concern (it
carries the audited UnsafeWorkerReadinessBypass), not ours.

Why we pin CHAPTER_PRIMARY_MODEL to the live runner
---------------------------------------------------
Chapter generation runs inside a SEPARATE service — the Chapter microVM runner
(CHAPTER_MICROVM_RUNNER_URL). The worker binds `model: z.literal(
PRIMARY_CHAPTER_MODEL)` into its readyz/result schema at MODULE LOAD, where
`PRIMARY_CHAPTER_MODEL = process.env.CHAPTER_PRIMARY_MODEL ?? 'MiniMax-M3'`
(chapterExecutionRuntime.ts:10). If that literal != the model the runner
actually serves, the very first chapter-run preflight throws
'readiness response violated the exact runtime contract' and the job re-fails
within seconds (observed on book d0df3cdb: worker env said 'glm-5.3-flash', the
live runner served 'glm-5.3'). The model id "travels with the gateway env file,
not a redeploy" (ibid.), so this drift is expected whenever the runner is swapped.
We self-heal it by asking the runner's /readyz what it serves and pinning the
worker's CHAPTER_PRIMARY_MODEL to that at spawn. The pin works because server/.env
is loaded with dotenv override:false (server/config/env.ts:22) — a value already
in the child's process env WINS over the .env line. Best-effort: if the runner is
unreachable we leave the model untouched (an unreachable runner can't generate
anyway — a later honest failure, not one we worsen).

Why we SELF-HEAL a co-located `.mini-ork` (and only then pin MINI_ORK_HOME_DIR)
------------------------------------------------------------------------------
Several chapter-DAG segment nodes (W9_scaffold_sections and its siblings) shell
out to the VENDORED mini-ork binary — verifiedArtifactClient, microvmBurstRunner,
chapterReviewClient, codebaseIngestClient, multiPassWriteAndCritiqueClient and
miniOrkRlmService all resolve the runtime as
`process.env.MINI_ORK_HOME_DIR || <cwd>/.mini-ork` and refuse
(VerifiedArtifactBinaryMissingError) if `<home>/bin/mini-ork` is not executable.
A sanctioned researcher worktree symlinks node_modules and shares server/.env but
NOT the gitignored `.mini-ork`, so the worktree worker's default VENDORED_HOME
(`<worktree>/.mini-ork`) is absent and every one of those nodes dies at the first
scaffold.

Pinning MINI_ORK_HOME_DIR at the PRIMARY home fixes the missing-binary error but
INTRODUCES an overlay-recipe EEXIST: with home != worktree,
`ensureOverlayRecipeSymlink` tries to farm `<primary>/.mini-ork/recipes/<overlay>
-> <worktree>/server/resources/miniork-overlay-recipes/<overlay>` and collides
with the primary's own (different) overlay link. So the loop now HEALS ITSELF: it
builds a co-located `.mini-ork` INSIDE the worktree (`_ensure_colocated_home`) —
top-level symlinks borrow the primary's built runtime + live state, an `engine`
pointer keeps the venv on the primary's real path, and a real `recipes/` dir
re-resolves the overlay links inside the worktree. Home now == worktree, so the
overlay guard early-returns (no EEXIST) AND every shell-out node finds a binary.
Best-effort + non-clobbering: honour an operator-set MINI_ORK_HOME_DIR, else build
(or reuse) the co-located home, else use the worktree's genuine own copy, else pin
the primary, else leave unset (an honest downstream failure beats a fabricated
path).

Env
    MO_GOAL_TARGET_CWD              worktree to run the replacement from (required)
    MO_RESEARCHER_DIR              primary checkout whose vendored `.mini-ork` the
                                    worktree worker borrows when it lacks its own
                                    (default: the standard researcher checkout)
    MO_WORKER_ROLE                  worker role (default: book-generation)
    MO_WORKER_HEALTH_URL            readiness probe
                                    (default http://localhost:7823/api/health/hatchet)
    MO_WORKER_START_TIMEOUT_SECONDS wait for the 'registered' line (default 180)
    MO_WORKER_STOP_TIMEOUT_SECONDS  wait for the supervised unit to exit (default 120)
    MINI_ORK_RUN_DIR               where the replacement's log is written
    MO_GOAL_WORKER_RESTART_DRY     =1 -> resolve + print the plan, touch nothing
    MO_GOAL_SYNC_UPSTREAM          =0 -> skip the pre-spawn upstream merge (default 1)

Why we RESYNC the deploy target with the product branch first
------------------------------------------------------------
The deploy target is a long-lived branch that the loop's children commit their
fixes onto, so it drifts behind the product branch as main advances — and a fix
merged to main is INVISIBLE to the worker here until the branch it actually runs
contains it. Measured 2026-09-20: the jina figure-preservation fix (ba57a71e4)
was merged to main, but the deploy target was 14 commits behind, so every chapter
kept losing its figures while the fix was reported as shipped. A restart is
already the "make the code live" edge, so it is the right place to close that
gap: before spawning, merge the product branch in.

A merge never drops a commit, and a failure here must NEVER block the restart —
a conflict, a dirty tree or an unreachable remote aborts the merge and warns,
leaving the worktree exactly as it was. Resolving a conflict automatically (e.g.
-X ours/theirs) would be a fabricated fix, so we refuse to guess and say so.

Exit 0 == the fixed worker is up (or DRY plan printed); non-zero == restart failed.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

_DEFAULT_ROLE = "book-generation"
_DEFAULT_HEALTH = "http://localhost:7823/api/health/hatchet"
_READY_LINE = "Hatchet book-generation worker registered"
_WATCHDOG_REL = os.path.join("scripts", "dev-worker-watchdog.sh")
# Namespace/lane-probe keys pinned from the worktree's shared server/.env so the
# fixed worker joins the SAME dispatch namespace and the watchdog's lane-lock
# drain probe hits the SAME Redis even if this process's ambient env differs.
_NAMESPACE_KEYS = ("QUEUE_PREFIX", "REDIS_HOST", "REDIS_PORT", "REDIS_PASSWORD")
# Chapter microVM runner probe: read these from the worktree's server/.env to ask
# the runner's /readyz which model it serves, then pin the worker to match.
_RUNNER_KEYS = ("CHAPTER_MICROVM_RUNNER_URL", "CHAPTER_MICROVM_RUNNER_TOKEN")
_RUNNER_PROBE_TIMEOUT = 6
# Vendored mini-ork runtime the chapter DAG shells out to. A sanctioned worktree
# symlinks node_modules but NOT the gitignored `.mini-ork`, so we point the worker
# at the primary checkout's proven vendored copy (see 'Why we pin MINI_ORK_HOME_DIR').
_DEFAULT_RESEARCHER_DIR = "/Volumes/docker-ssd/Migration/Development/researcher"
_MINI_ORK_HOME_REL = ".mini-ork"
_MINI_ORK_BIN_REL = os.path.join("bin", "mini-ork")
# This binding ships at <engine>/kickoffs/book-goal-loop/binding/restart_worker.py.
_ENGINE_ROOT_DEPTH = 3
# Terminal markers in the child log: stop waiting, the start failed.
_FAIL_MARKERS = (
    "Failed to start",
    "refusing to start",
    "giving up",  # watchdog exhausted its restart budget
    "check-dev-toolchain",  # worktree toolchain gate refused
)


# Product branches the deploy target tracks, most-preferred first. The loop's
# branch carries its own fix commits, so it falls behind as main advances — and a
# fix merged to main never reaches the running worker until the branch it runs
# contains it (see 'Why we RESYNC the deploy target').
_SYNC_REFS = ("origin/main", "main")


_FALLBACK_IDENTITY = {
    "GIT_AUTHOR_NAME": "mini-ork",
    "GIT_AUTHOR_EMAIL": "mini-ork@localhost",
    "GIT_COMMITTER_NAME": "mini-ork",
    "GIT_COMMITTER_EMAIL": "mini-ork@localhost",
}


def _identity_env(worktree: str) -> dict:
    """Ambient env plus a fallback git identity for commands that write a commit.

    ``git merge`` creates a commit, so an environment with no identity kills it
    with ``empty ident name ... not allowed`` — the state every CI runner,
    container and fresh sandbox starts in. Because ``_sync_upstream`` reports a
    failed merge as a conflict, that made the deploy target silently stop
    carrying the product branch forward, which is the regression this binding
    exists to prevent. A configured identity is left alone, so a human's own
    settings still win; the fallback is the same one ``vcs/rebase_guard.py`` and
    ``vcs/auto_merge.py`` use.
    """
    env = dict(os.environ)
    configured = subprocess.run(
        ["git", "-C", worktree, "config", "user.email"],
        capture_output=True, text=True,
    ).stdout.strip()
    if configured:
        return env
    for var, value in _FALLBACK_IDENTITY.items():
        if not env.get(var):
            env[var] = value
    return env


def _git(worktree: str, *args: str, timeout: int = 120,
         env: dict | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", worktree, *args],
        capture_output=True, text=True, timeout=timeout, env=env,
    )


def _sync_upstream(worktree: str) -> str:
    """Merge the product branch into the deploy target. Returns a one-line why.

    Best-effort by construction: every failure path aborts the merge and returns
    a description, so the restart that follows is never blocked and the worktree
    is never left mid-merge.
    """
    if os.environ.get("MO_GOAL_SYNC_UPSTREAM", "").strip() == "0":
        return "disabled (MO_GOAL_SYNC_UPSTREAM=0)"

    ref = next(
        (
            candidate
            for candidate in _SYNC_REFS
            if _git(worktree, "rev-parse", "--verify", "--quiet", f"{candidate}^{{commit}}").returncode == 0
        ),
        None,
    )
    if ref is None:
        return "no upstream ref (origin/main, main) — skipped"

    # Best-effort refresh; an unreachable remote must not fail a deploy.
    _git(worktree, "fetch", "--no-tags", "origin", "main", timeout=90)

    if _git(worktree, "merge-base", "--is-ancestor", ref, "HEAD").returncode == 0:
        return f"already contains {ref}"

    merged = _git(worktree, "merge", "--no-edit", ref, env=_identity_env(worktree))
    if merged.returncode == 0:
        head = _git(worktree, "rev-parse", "--short", "HEAD").stdout.strip()
        return f"merged {ref} into the deploy target (HEAD {head})"

    # Never guess: leave the tree byte-identical to how we found it and name the
    # paths so an operator (or the next wave's evidence) can see what to resolve.
    conflicted = _git(worktree, "diff", "--name-only", "--diff-filter=U").stdout.split()
    _git(worktree, "merge", "--abort")
    detail = ", ".join(conflicted[:8]) if conflicted else (
        (merged.stderr.strip().splitlines() or ["(unknown)"])[-1]
    )
    print(
        f"worker-restart WARN: could not merge {ref} into the deploy target — left the tree "
        f"untouched; resolve by hand. Conflicted: {detail}",
        file=sys.stderr,
    )
    return f"merge conflict against {ref} ({detail})"


def _pgrep(pattern: str) -> list[int]:
    """PIDs whose argv matches `pattern`, excluding this process."""
    proc = subprocess.run(
        ["pgrep", "-f", pattern], capture_output=True, text=True
    )
    pids: list[int] = []
    for line in proc.stdout.split():
        try:
            pid = int(line)
        except ValueError:
            continue
        if pid != os.getpid():
            pids.append(pid)
    return pids


def _worker_pids(role: str) -> list[int]:
    """Leaf worker PIDs (pnpm+tsx+node) carrying the runWorker role token."""
    return _pgrep(f"runWorker.ts {role}")


def _watchdog_pids(role: str) -> list[int]:
    """Supervisor PIDs: dev-worker-watchdog.sh for this role. Stopping these
    (not the leaf) is what actually ends the worker — the watchdog respawns a
    killed leaf from its own (primary) checkout."""
    return _pgrep(f"dev-worker-watchdog.sh {role}")


def _term(pids: list[int]) -> tuple[bool, str]:
    """SIGTERM a pid list; report a permission failure, tolerate races."""
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except PermissionError:
            return False, f"cannot SIGTERM pid {pid} (permission denied)"
    return True, ""


def _stop_incumbent(role: str, timeout_s: int) -> tuple[bool, str]:
    """Stop the supervised unit: SIGTERM the watchdog FIRST (its trap kills its
    own worker child + exits without respawning), then fence any leaf worker
    (covers a bare/orphaned process outside a watchdog). Await both gone —
    graceful only, never SIGKILL (a draining worker may hold in-flight lanes)."""
    watchdogs = _watchdog_pids(role)
    workers = _worker_pids(role)
    if not watchdogs and not workers:
        return True, "no supervised worker running (nothing to stop)"

    # Watchdog first so it stops respawning before we touch the leaf.
    ok, why = _term(watchdogs)
    if not ok:
        return False, why
    ok, why = _term(workers)
    if not ok:
        return False, why

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if not _watchdog_pids(role) and not _worker_pids(role):
            return True, f"supervised unit drained (watchdog {watchdogs or '(none)'}, worker {workers or '(none)'})"
        time.sleep(2)
    still_wd = _watchdog_pids(role)
    still_w = _worker_pids(role)
    return (
        False,
        f"supervised unit still alive after {timeout_s}s "
        f"(watchdog {still_wd or '(none)'}, worker {still_w or '(none)'}); refusing SIGKILL",
    )


def _read_dotenv(path: str, keys: tuple[str, ...]) -> dict[str, str]:
    """Minimal dotenv reader for a fixed key set (values may be quoted / have
    inline '='). Empty or absent values are skipped."""
    out: dict[str, str] = {}
    try:
        with open(path, "r", errors="replace") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                if line.startswith("export "):
                    line = line[len("export "):].lstrip()
                key, _, val = line.partition("=")
                key = key.strip()
                if key not in keys:
                    continue
                val = val.strip().strip('"').strip("'").strip()
                if val:
                    out[key] = val
    except FileNotFoundError:
        pass
    return out


def _runner_model(worktree: str) -> tuple[str | None, str]:
    """Best-effort: ask the Chapter microVM runner's /readyz which model it serves,
    so we can pin the worker's CHAPTER_PRIMARY_MODEL to match (see the module
    docstring 'Why we pin CHAPTER_PRIMARY_MODEL'). Returns (model, why) on success,
    (None, why) on any absent-config / unreachable / non-ready case — never raises,
    so a probe miss degrades to leaving the worker's own env model untouched."""
    cfg = _read_dotenv(os.path.join(worktree, "server", ".env"), _RUNNER_KEYS)
    url = cfg.get("CHAPTER_MICROVM_RUNNER_URL", "").rstrip("/")
    token = cfg.get("CHAPTER_MICROVM_RUNNER_TOKEN", "")
    if not url or not token:
        return None, "runner url/token absent from worktree server/.env; left CHAPTER_PRIMARY_MODEL as-is"
    req = urllib.request.Request(f"{url}/readyz", headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=_RUNNER_PROBE_TIMEOUT) as resp:
            body = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception as exc:  # unreachable / timeout / non-JSON — stay best-effort
        return None, f"runner /readyz probe failed ({exc!r}); left CHAPTER_PRIMARY_MODEL as-is"
    model = body.get("model") if isinstance(body, dict) else None
    status = body.get("status") if isinstance(body, dict) else None
    if status != "ready" or not isinstance(model, str) or not model:
        return None, f"runner not ready / no model (status={status!r}); left CHAPTER_PRIMARY_MODEL as-is"
    return model, f"pinned CHAPTER_PRIMARY_MODEL to live runner model {model!r}"


def _has_vendored_binary(home: str) -> bool:
    """True iff `<home>/bin/mini-ork` exists and is executable — the exact check
    the researcher clients make (`fs.accessSync(binPath, X_OK)`) before shelling out."""
    return bool(home) and os.access(os.path.join(home, _MINI_ORK_BIN_REL), os.X_OK)


# Top-level `.mini-ork` entries the co-located home does NOT borrow by symlink:
# recipes (rebuilt as a real dir so overlay links re-resolve inside the worktree),
# engine (rewritten as a pointer file -> the primary home), and .git (the vendored
# runtime's own repo — never shared into a farmed home).
_COLOCATE_EXCLUDE_TOP = frozenset({"recipes", "engine", ".git"})
_ENGINE_REL = "engine"
_RECIPES_REL = "recipes"


def _is_colocated_home(home: str, primary_home: str) -> bool:
    """True iff `home` is a home WE built co-located for this worktree: its `engine`
    pointer file names the primary home. This is the signature that lets us tell our
    own farmed home (idempotent -> reuse) apart from a worktree's genuine vendored
    `.mini-ork` (never clobber)."""
    engine_ptr = os.path.join(home, _ENGINE_REL)
    if not os.path.isfile(engine_ptr):
        return False
    try:
        with open(engine_ptr, "r", errors="replace") as fh:
            return fh.read().strip() == primary_home.rstrip("/")
    except OSError:
        return False


def _ensure_colocated_home(worktree: str, primary_home: str) -> tuple[str | None, str]:
    """Self-heal capability: build a co-located `.mini-ork` INSIDE the worktree so
    the worktree worker resolves its OWN vendored home (home == worktree/.mini-ork)
    rather than borrowing the primary's from a foreign path.

    Why this closes the overlay-EEXIST loop autonomously
    ----------------------------------------------------
    A sanctioned worktree ships node_modules + server/.env but NOT the gitignored
    `.mini-ork`. The old repair pinned MINI_ORK_HOME_DIR at the PRIMARY home — but
    then home != worktree, so verifiedArtifactClient's `ensureOverlayRecipeSymlink`
    tries to create `<primary>/.mini-ork/recipes/verified-artifact ->
    <worktree>/server/resources/miniork-overlay-recipes/verified-artifact` and
    throws EEXIST against the primary's own (different) overlay link. When home ==
    worktree the guard early-returns instead: the link it wants already
    realpath-equals its own target inside the worktree, no symlinkSync, no EEXIST.
    So the fix is to give the worktree a home of its own — this function makes the
    loop do that itself instead of a human hand-farming it.

    Layout (mirrors the proven hand-built home)
    -------------------------------------------
      - top-level: absolute symlink each primary `.mini-ork/<entry>` EXCEPT
        {recipes, engine, .git} — bin, .venv, config, db, state.db … all borrowed,
        so this home shares the primary's built runtime and live state.
      - engine: a pointer FILE whose content is the primary home path, so the
        launcher resolves the engine root (code + .venv) on the primary's REAL path
        — never through a symlink, which would break pyvenv.cfg discovery.
      - recipes/: a REAL dir mirroring primary/.mini-ork/recipes. Base recipes
        (real dirs) become absolute symlinks to the primary; overlay recipes
        (relative links into ../../server/resources/miniork-overlay-recipes) are
        copied VERBATIM so they re-resolve inside THIS worktree's server tree.

    Idempotent + non-clobbering. If our co-located home is already present it is
    reused; a worktree's genuine own vendored home is left untouched (returns None
    so the caller falls back to it). Best-effort: any build error cleans up the
    partial home and returns (None, why); the caller then falls back to the
    primary-home pin (an honest, if EEXIST-prone, path — never a fabricated one)."""
    if not primary_home or not os.path.isdir(primary_home):
        return None, f"primary vendored home missing ({primary_home!r}); cannot build co-located home"
    primary_recipes = os.path.join(primary_home, _RECIPES_REL)
    if not os.path.isdir(primary_recipes):
        return None, f"primary recipes dir missing ({primary_recipes!r}); cannot build co-located home"

    wt_home = os.path.join(worktree, _MINI_ORK_HOME_REL)
    if os.path.lexists(wt_home):
        if _is_colocated_home(wt_home, primary_home):
            return wt_home, f"co-located .mini-ork already present ({wt_home}); reused"
        # A worktree's own genuine vendored home (or an operator's) — never clobber.
        return None, f"worktree already has a non-co-located .mini-ork ({wt_home}); left untouched"

    try:
        os.mkdir(wt_home)
        for entry in sorted(os.listdir(primary_home)):
            if entry in _COLOCATE_EXCLUDE_TOP:
                continue
            os.symlink(os.path.join(primary_home, entry), os.path.join(wt_home, entry))
        with open(os.path.join(wt_home, _ENGINE_REL), "w") as fh:
            fh.write(primary_home.rstrip("/") + "\n")
        wt_recipes = os.path.join(wt_home, _RECIPES_REL)
        os.mkdir(wt_recipes)
        for entry in sorted(os.listdir(primary_recipes)):
            src = os.path.join(primary_recipes, entry)
            dst = os.path.join(wt_recipes, entry)
            if os.path.islink(src):
                # Copy the link string verbatim: a relative overlay link
                # (../../server/…) re-resolves inside THIS worktree; an absolute
                # link points at the same target from anywhere.
                os.symlink(os.readlink(src), dst)
            else:
                # A base recipe real dir: absolute symlink to the primary's copy.
                os.symlink(src, dst)
    except OSError as exc:
        shutil.rmtree(wt_home, ignore_errors=True)
        return None, f"co-located .mini-ork build failed ({exc!r}); cleaned up partial home"
    return wt_home, f"built co-located .mini-ork at {wt_home} (engine->primary, {len(os.listdir(wt_recipes))} recipes co-located)"


def _mini_ork_home(worktree: str) -> tuple[str | None, str]:
    """Best-effort: ensure the worktree worker can resolve the vendored mini-ork
    binary the chapter DAG shells out to (see 'Why we pin MINI_ORK_HOME_DIR').
    Priority: honour an operator-set MINI_ORK_HOME_DIR > SELF-HEAL by building (or
    reusing) a co-located home so home == worktree (overlay guard no-ops) > use the
    worktree's own genuine vendored copy > fall back to the primary checkout's
    proven copy > leave unset. Never raises; a miss degrades to an honest downstream
    failure rather than a fabricated path."""
    if os.environ.get("MINI_ORK_HOME_DIR", "").strip():
        return None, f"MINI_ORK_HOME_DIR already set ({os.environ['MINI_ORK_HOME_DIR']!r}); left as operator override"
    primary = os.environ.get("MO_RESEARCHER_DIR", "").strip() or _DEFAULT_RESEARCHER_DIR
    primary_home = os.path.join(primary, _MINI_ORK_HOME_REL)
    # Self-heal first: a co-located home pins home == worktree, which is what makes
    # the overlay-recipe guard a no-op (no EEXIST) — strictly better than the
    # primary pin. Falls through cleanly when it can't build one.
    colocated, why_colo = _ensure_colocated_home(worktree, primary_home)
    if colocated:
        return colocated, why_colo
    wt_home = os.path.join(worktree, _MINI_ORK_HOME_REL)
    if _has_vendored_binary(wt_home):
        return None, f"worktree carries its own vendored mini-ork; no MINI_ORK_HOME_DIR pin needed ({why_colo})"
    if _has_vendored_binary(primary_home):
        return primary_home, f"pinned MINI_ORK_HOME_DIR to primary vendored runtime {primary_home!r} (worktree lacks .mini-ork; {why_colo})"
    return None, f"no vendored mini-ork found (worktree or {primary_home}); left MINI_ORK_HOME_DIR unset ({why_colo})"


def _is_engine_root(path: str) -> bool:
    return bool(path) and os.path.isdir(os.path.join(path, "mini_ork"))


def _resolve_engine_root() -> str | None:
    """The engine the worker's children must run: normally the loop's own, inherited
    from this process. Pin it explicitly rather than let it be dropped, because the
    vendored launcher's fallbacks are worse than they look — MINI_ORK_HOME/engine in a
    sanctioned worktree points at the PRIMARY researcher home, an older vendored copy
    that predates the reviewer-diff fixes, so an absent pin silently re-arms the
    empty-diff false pass. Falls back to the checkout this binding ships in."""
    for variable in ("MINI_ORK_ENGINE_ROOT", "MINI_ORK_ROOT"):
        candidate = os.environ.get(variable, "").strip()
        if _is_engine_root(candidate):
            return candidate
    root = Path(__file__).resolve().parents[_ENGINE_ROOT_DEPTH]
    return str(root) if _is_engine_root(str(root)) else None


def _env_overlay(worktree: str, log_path: str, extra: dict[str, str] | None = None) -> dict[str, str]:
    """Inherit ambient env, then pin the worktree's log target + namespace keys
    from its shared server/.env so the fixed worker + watchdog probe are
    deterministic regardless of this process's ambient env. `extra` (e.g. the
    runner-aligned CHAPTER_PRIMARY_MODEL) is applied LAST and wins over both
    ambient env and the .env overlay — and, because the worker loads server/.env
    with dotenv override:false, over the .env line the worker reads too."""
    env = dict(os.environ)
    # Scrub run-scoped identity vars before they reach a LONG-LIVED worker. This
    # binding may itself run under a goal-loop parent that pinned MINI_ORK_RUN_DIR/
    # RUN_ID for ITS own run; the worker spawns many `mini-ork run`s, each of which
    # must mint a fresh run id + canonical run dir. Inheriting a stale pin is exactly
    # what split the failing W9_scaffold_sections run — execute wrote to the leaked
    # /tmp dir while the caller read runs/<id>/verified-artifact.json (ENOENT). Core
    # mini-ork now self-defends (main.py pins run_dir), so this is defense-in-depth:
    # stop the leak at the spawn source too.
    for _run_scoped in (
        "MINI_ORK_RUN_DIR", "MINI_ORK_RUN_ID",
        # The rest of THIS run's identity. Inheriting is never right for a
        # long-lived worker: it re-mints a recipe per chapter, and a leaked
        # MINI_ORK_WORKFLOW/MINI_ORK_PLAN_PATH would point its children at the
        # goal-loop's own plan. MINI_ORK_TEST_CMD/MINI_ORK_TYPECHECK_CMD are
        # pinned to `echo` upstream — inheriting those would silently neuter
        # every gate the worker's children run.
        "MINI_ORK_RECIPE", "MINI_ORK_RECIPE_ROOT", "MINI_ORK_WORKFLOW", "MINI_ORK_TASK_CLASS",
        "MINI_ORK_PLAN_PATH", "MINI_ORK_PROFILE_PATH", "MINI_ORK_PROFILE_GATE",
        "MINI_ORK_NODE_INPUT_DIR", "MINI_ORK_NODE_INPUT_MANIFEST",
        "MINI_ORK_TEST_CMD", "MINI_ORK_TYPECHECK_CMD",
        # MO_DISPATCH_CHAIN is this run's per-node lane pin — the dispatcher passes
        # it straight through as `--model` (execute.py _default_llm_dispatch), so an
        # inherited goal-loop chain would mis-bind the lane of every child node that
        # does not set its own. MO_NODE_ID names the node that spawned THIS binding
        # (goal_apply), never anything a fresh child run is doing.
        "MO_DISPATCH_CHAIN", "MO_NODE_ID",
    ):
        env.pop(_run_scoped, None)
    # Deliberately KEPT (not scrubbed): MINI_ORK_DB, so the goal-loop's cost circuit
    # keeps reading the spend it caused. Repointing it would blind the budget rail,
    # not fix a leak. (The engine pair is kept too, but as an explicit re-pin — see
    # _start_replacement, which refuses to rely on inheritance.)
    # The home pair must travel with the replacement pin or not at all. The
    # launcher resolves project_home as PROJECT_HOME || HOME || cwd/.mini-ork
    # (bin/mini-ork), so a goal-loop parent's MINI_ORK_PROJECT_HOME outranks the
    # MINI_ORK_HOME_DIR the caller pinned — the child then writes runs/<id>/
    # verified-artifact.json into the GOAL-LOOP's .mini-ork while the caller reads
    # the worktree's, and reports "verifier did not run" for a run that verified
    # cleanly (observed: W9_scaffold_sections, 7 attempts, mdlen=0). Drop both
    # ambient values; `extra` re-pins them to the one resolved home.
    if (extra or {}).get("MINI_ORK_HOME_DIR"):
        for _home_scoped in ("MINI_ORK_HOME", "MINI_ORK_PROJECT_HOME"):
            env.pop(_home_scoped, None)
    # Same rule for the engine pair: the replacement pin must be the only value
    # standing, or the launcher can resolve an engine other than the one under test.
    if (extra or {}).get("MINI_ORK_ENGINE_ROOT"):
        for _engine_scoped in ("MINI_ORK_ENGINE_ROOT", "MINI_ORK_ROOT"):
            env.pop(_engine_scoped, None)
    env["WORKER_LOG"] = log_path
    env.setdefault("LOKI_ENABLED", "false")  # bounded proof: don't ship to Loki
    env.update(_read_dotenv(os.path.join(worktree, "server", ".env"), _NAMESPACE_KEYS))
    if extra:
        env.update(extra)
    return env


def _start_replacement(worktree: str, role: str, log_path: str) -> tuple[bool, str]:
    """Start the watchdog FROM the worktree (supervises the W15-fixed worker),
    detached, logging to the run dir. Before spawning, align two worktree-parity
    env pins so the worker's first chapter attempt clears its guards: the chapter
    model literal to the live microVM runner (preflight contract), and
    MINI_ORK_HOME_DIR to a vendored mini-ork the DAG's shell-out nodes can find."""
    watchdog = os.path.join(worktree, _WATCHDOG_REL)
    tsx = os.path.join(worktree, "node_modules", ".bin", "tsx")
    if not os.path.isfile(watchdog):
        return False, f"watchdog missing under worktree ({watchdog}) — is it a sanctioned researcher worktree?"
    if not os.path.exists(tsx):
        return False, f"tsx missing under worktree node_modules ({tsx}) — is it a sanctioned worktree?"
    model, why_model = _runner_model(worktree)
    home, why_home = _mini_ork_home(worktree)
    extra: dict[str, str] = {}
    if model:
        extra["CHAPTER_PRIMARY_MODEL"] = model
    if home:
        extra["MINI_ORK_HOME_DIR"] = home
        # PROJECT_HOME outranks HOME_DIR in the launcher, so pin it too — this is
        # the variable that actually decides where runs/<id>/ lands, and it must
        # be the same home the caller reads back. MINI_ORK_HOME is the launcher's
        # second fallback for both project_home and the engine-pointer lookup, so
        # pin it as well rather than leave one foreign home reference in the env.
        extra["MINI_ORK_PROJECT_HOME"] = home
        extra["MINI_ORK_HOME"] = home
    # Pin the engine the children run, independent of the home pin: a worktree's
    # `.mini-ork/engine` pointer names the researcher PRIMARY home, whose vendored
    # copy predates the reviewer-diff fixes. Relying on inheritance here means a
    # hand-run restart silently downgrades every child's engine.
    engine = _resolve_engine_root()
    if engine:
        extra["MINI_ORK_ENGINE_ROOT"] = engine
        extra["MINI_ORK_ROOT"] = engine
    logf = open(log_path, "ab", buffering=0)
    proc = subprocess.Popen(
        ["bash", watchdog, role],
        cwd=worktree,
        stdout=logf,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True,  # detach: survives this binding's exit
        env=_env_overlay(worktree, log_path, extra or None),
    )
    return True, f"spawned worktree watchdog pid {proc.pid} ({why_model}; {why_home}, log {log_path})"


def _await_ready(log_path: str, health_url: str, timeout_s: int) -> tuple[bool, str]:
    """Primary signal: the 'registered' line in the child log. Secondary: health 200."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            with open(log_path, "r", errors="replace") as fh:
                body = fh.read()
        except FileNotFoundError:
            body = ""
        if _READY_LINE in body:
            return True, "worker registered (log line observed)"
        for marker in _FAIL_MARKERS:
            if marker in body:
                return False, f"replacement worker reported a startup failure in its log ({marker!r})"
        try:
            with urllib.request.urlopen(health_url, timeout=3) as resp:
                if resp.status == 200:
                    return True, "worker ready (health 200)"
        except Exception:
            pass  # health probe is best-effort; log line is authoritative
        time.sleep(3)
    return False, f"no readiness signal within {timeout_s}s (log {log_path})"


def main(argv: list[str]) -> int:
    worktree = os.environ.get("MO_GOAL_TARGET_CWD", "").strip()
    role = os.environ.get("MO_WORKER_ROLE", _DEFAULT_ROLE).strip() or _DEFAULT_ROLE
    health = os.environ.get("MO_WORKER_HEALTH_URL", _DEFAULT_HEALTH).strip() or _DEFAULT_HEALTH
    start_to = int(os.environ.get("MO_WORKER_START_TIMEOUT_SECONDS", "180"))
    stop_to = int(os.environ.get("MO_WORKER_STOP_TIMEOUT_SECONDS", "120"))
    run_dir = os.environ.get("MINI_ORK_RUN_DIR", "").strip() or "/tmp"
    log_path = os.path.join(run_dir, f"worker-{role}.log")

    if not worktree or not os.path.isdir(worktree):
        print(f"MO_GOAL_TARGET_CWD unset/not-a-dir: {worktree!r}", file=sys.stderr)
        return 2

    incumbent_wd = _watchdog_pids(role)
    incumbent_w = _worker_pids(role)
    dry = os.environ.get("MO_GOAL_WORKER_RESTART_DRY", "").strip() == "1"
    if dry:
        print(
            f"worker-restart DRY: would SIGTERM watchdog {role} pids={incumbent_wd or '(none)'} "
            f"(its trap kills its worker child {incumbent_w or '(none)'} + exits, no respawn; "
            f"await ≤{stop_to}s for singleton-lock release), then start the watchdog FROM worktree "
            f"{worktree} (log {log_path}), await '{_READY_LINE}' ≤{start_to}s (health {health})"
        )
        return 0

    stopped, why_stop = _stop_incumbent(role, stop_to)
    print(f"worker-restart stop: {why_stop}")
    if not stopped:
        return 1
    # Deploy the fixed code AND the product branch it must keep pace with: a fix
    # merged to main is invisible to this worker until the branch it runs
    # contains it. After the incumbent is down (no live worker racing the file
    # writes) and before the replacement starts (it must load the merged tree).
    print(f"worker-restart sync: {_sync_upstream(worktree)}")
    started, why_start = _start_replacement(worktree, role, log_path)
    print(f"worker-restart start: {why_start}")
    if not started:
        return 1
    ready, why_ready = _await_ready(log_path, health, start_to)
    print(f"worker-restart ready: {why_ready}")
    return 0 if ready else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
