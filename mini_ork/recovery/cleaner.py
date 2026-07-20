"""Python port of lib/cleaner.sh — single-shot cleaner agent that runs ON MAIN.

Strangler-fig parity port. Fixes cross-cutting baseline blockers detected by the
detective: works directly on main via a temp branch, holds an exclusive mkdir
lock, stashes+restores the user's dirty tree (with the HEAD-moved race guard),
tries to reuse a recent unmerged cleaner branch, else spawns a one-shot claude
worker, gauntlet-checks, and squash-merges.

The claude spawn (``_spawn_worker``) and gauntlet run (``_run_gauntlet``) are
seams — overridable for tests; the default implementations shell out exactly as
the bash does. Exit codes match the bash verbatim:
    0 success/reuse/noop-reuse · 2 usage · 3 lock · 4 no-brief · 5 not-main
    6 no-commits · 7 required-checks-fail · 10 main-already-clean

    main(argv=None, *, root=None, home=None, spawn=None, gauntlet=None) -> int
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time


def _git(root, *args):
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)


def _jq_get(path, expr_default):
    """Read one field from a JSON file with a bash `jq -r '.x // "d"'` default."""
    key, _, default = expr_default.partition("//")
    key = key.strip().lstrip(".")
    default = default.strip().strip('"')
    try:
        return json.load(open(path)).get(key) or default
    except Exception:
        return default


def _acquire_lock(lock_dir) -> tuple[bool, int]:
    """mkdir-atomic lock with stale-PID reclaim. Returns (ok, exit_code)."""
    try:
        os.mkdir(lock_dir)
    except FileExistsError:
        pidf = os.path.join(lock_dir, "pid")
        if os.path.isfile(pidf):
            try:
                other = int(open(pidf).read().strip())
            except Exception:
                other = 0
            alive = other > 0
            if alive:
                try:
                    os.kill(other, 0)
                except ProcessLookupError:
                    alive = False
                except PermissionError:
                    alive = True
            if not alive:
                sys.stderr.write(f"[cleaner] stale lock from dead PID {other} — reclaiming\n")
                import shutil
                shutil.rmtree(lock_dir, ignore_errors=True)
                try:
                    os.mkdir(lock_dir)
                except OSError:
                    sys.stderr.write("[cleaner] race lost reclaiming stale lock\n")
                    return False, 3
            else:
                sys.stderr.write(f"[cleaner] lock held by PID {other} — refusing to run\n")
                return False, 3
        else:
            sys.stderr.write("[cleaner] lock held (no PID file) — refusing to run\n")
            return False, 3
    open(os.path.join(lock_dir, "pid"), "w").write(f"{os.getpid()}\n")
    return True, 0


def _default_spawn(root, prompt_file, out_dir, model, budget, scope_card) -> int:
    """Default worker seam — shells out to `claude -p` exactly as the bash."""
    scope = []
    if scope_card and os.path.isfile(scope_card):
        scope = ["--append-system-prompt-file", scope_card]
    disallowed = ("Bash(make deploy*),Bash(npm run migrate:*),Bash(docker*),Bash(kubectl*),"
                  "Bash(gh pr merge*),Bash(git push --force*),Bash(git push -f*),"
                  "Bash(git reset --hard*),Bash(git checkout main*),Bash(rm -rf*),"
                  "Bash(curl* -o*),Bash(wget*),Bash(ssh*),Bash(scp*),Agent,TaskCreate,TaskUpdate")
    prompt = open(prompt_file).read()
    with open(os.path.join(out_dir, "worker.log"), "wb") as lg, \
         open(os.path.join(out_dir, "worker.err"), "wb") as er:
        rc = subprocess.run(
            ["claude", "-p", "--model", model, "--max-budget-usd", str(budget), *scope,
             "--add-dir", root, "--disallowedTools", disallowed,
             "--dangerously-skip-permissions", "--output-format", "stream-json",
             "--include-partial-messages", "--verbose", prompt],
            stdout=lg, stderr=er).returncode
    open(os.path.join(out_dir, "worker.exit"), "w").write(f"{rc}\n")
    return rc


def _default_gauntlet(root, gauntlet_sh, gauntlet_out) -> int:
    return subprocess.run(["bash", gauntlet_sh, root, gauntlet_out],
                          capture_output=True, text=True).returncode


def main(argv=None, *, root=None, home=None, spawn=None, gauntlet=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    mini_root = root or os.environ.get("MINI_ORK_ROOT") or os.getcwd()
    repo_root = os.environ.get("REPO_ROOT") or (
        _git(mini_root, "rev-parse", "--show-toplevel").stdout.strip() or mini_root)
    mo_home = home or os.environ.get("MINI_ORK_HOME") or os.path.join(repo_root, ".mini-ork")
    scope_card = os.path.join(mo_home, "AGENT_SCOPE_CARD.md")
    lock_file = os.path.join(mo_home, "locks", "cleaner.lock")

    detective_json = argv[0] if len(argv) > 0 else ""
    out_dir = argv[1] if len(argv) > 1 else ""
    if not detective_json or not out_dir:
        sys.stderr.write("Usage: cleaner <detective_json> <output_dir>\n")
        return 2

    os.makedirs(out_dir, exist_ok=True)
    os.makedirs(os.path.dirname(lock_file), exist_ok=True)
    lock_dir = lock_file + ".d"
    ok, code = _acquire_lock(lock_dir)
    if not ok:
        return code

    import shutil
    stash_created = [False]
    stash_head = [""]
    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())

    def _restore_and_unlock():
        try:
            if stash_created[0]:
                lst = _git(repo_root, "stash", "list").stdout
                ref = next((ln.split(":")[0] for ln in lst.splitlines()
                            if f"cleaner pre-stash {ts}" in ln), "")
                if ref:
                    _git(repo_root, "checkout", "main")
                    now_head = _git(repo_root, "rev-parse", "HEAD").stdout.strip()
                    if stash_head[0] and now_head and stash_head[0] != now_head:
                        sys.stderr.write("[cleaner] WARNING: HEAD moved during cleaner run\n")
                        sys.stderr.write(f"[cleaner]   stash {ref} preserved; pop manually\n")
                    elif _git(repo_root, "stash", "pop", ref).returncode == 0:
                        sys.stderr.write(f"[cleaner] restored user's pre-stash ({ref})\n")
                    else:
                        sys.stderr.write(f"[cleaner] WARNING: could not auto-pop {ref}\n")
        finally:
            shutil.rmtree(lock_dir, ignore_errors=True)

    try:
        epic_id = _jq_get(detective_json, '.epic_id // "unknown"')
        classification = _jq_get(detective_json, '.classification // "unknown"')
        brief = _jq_get(detective_json, '.cleaner_brief // ""')
        rationale = _jq_get(detective_json, '.rationale // ""')
        if not brief:
            sys.stderr.write("[cleaner] detective gave no cleaner_brief — refusing to run blind\n")
            return 4

        branch = f"chore/cleaner-{classification.replace('_', '-')}-{ts}"
        cur = _git(repo_root, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        if cur != "main":
            sys.stderr.write(f"[cleaner] ERROR: must run on main (currently on {cur})\n")
            return 5

        if (_git(repo_root, "diff", "--quiet").returncode != 0
                or _git(repo_root, "diff", "--staged", "--quiet").returncode != 0):
            sys.stderr.write("[cleaner] working tree dirty — stashing first (will pop on exit)\n")
            if _git(repo_root, "stash", "push", "-u", "-m", f"cleaner pre-stash {ts}").returncode == 0:
                stash_created[0] = True
                stash_head[0] = _git(repo_root, "rev-parse", "HEAD").stdout.strip()

        # reuse fast-path
        lookback = os.environ.get("CLEANER_REUSE_LOOKBACK", "3")
        cands = _git(repo_root, "branch", "--list", "chore/cleaner-*",
                     "--sort=-committerdate", "--format=%(refname:short)").stdout.split()[:int(lookback)]
        for cand in cands:
            if _git(repo_root, "merge-base", "--is-ancestor", cand, "main").returncode == 0:
                continue
            try:
                ahead = int(_git(repo_root, "rev-list", "--count", f"main..{cand}").stdout.strip() or "0")
            except ValueError:
                ahead = 0
            if ahead == 0:
                continue
            sys.stderr.write(f"[cleaner] reuse check: {cand} ({ahead} commits ahead of main)\n")
            if _git(repo_root, "checkout", cand).returncode != 0:
                continue
            reuse_ok = True
            if os.path.isfile(os.path.join(repo_root, "package.json")):
                if subprocess.run(["npm", "run", "-s", "type-check"], cwd=repo_root,
                                  capture_output=True).returncode != 0:
                    reuse_ok = False
                elif subprocess.run(["npm", "run", "-s", "build"], cwd=repo_root,
                                    capture_output=True).returncode != 0:
                    reuse_ok = False
            if not reuse_ok:
                sys.stderr.write(f"[cleaner] reuse: {cand} failed predicate — skipping\n")
                _git(repo_root, "checkout", "main")
                continue
            sys.stderr.write(f"[cleaner] reuse: {cand} PASSES tc+build — fast-path squash-merge\n")
            _git(repo_root, "checkout", "main")
            _git(repo_root, "merge", "--squash", cand)
            if _git(repo_root, "diff", "--cached", "--quiet").returncode == 0:
                _git(repo_root, "branch", "-D", cand)
                new_sha = _git(repo_root, "rev-parse", "HEAD").stdout.strip()
                json.dump({"verdict": "PASS", "new_main_sha": new_sha, "reused_branch": cand,
                           "squashed_commits": 0, "classification": classification,
                           "fast_path": "reuse-noop", "reason": "branch content already in main"},
                          open(os.path.join(out_dir, "verdict.json"), "w"))
                sys.stderr.write(f"[cleaner] === REUSE-NOOP === main already has the fix at sha={new_sha}\n")
                return 0
            _git(repo_root, "commit", "--no-verify", "-m",
                 f"chore(baseline): reuse cleaner branch for {classification} ({epic_id})\n\n"
                 f"Detective rationale: {rationale}\n\nReused unmerged cleaner branch `{cand}`.")
            _git(repo_root, "branch", "-D", cand)
            new_sha = _git(repo_root, "rev-parse", "HEAD").stdout.strip()
            json.dump({"verdict": "PASS", "new_main_sha": new_sha, "reused_branch": cand,
                       "squashed_commits": ahead, "classification": classification,
                       "fast_path": "reuse"}, open(os.path.join(out_dir, "verdict.json"), "w"))
            sys.stderr.write(f"[cleaner] === REUSE SUCCESS === new main sha={new_sha}\n")
            return 0

        # fresh worker
        _git(repo_root, "checkout", "-b", branch)
        prompt_file = os.path.join(out_dir, "prompt.md")
        inbox_dir = os.path.join(mo_home, "INBOX")
        open(prompt_file, "w").write(_build_prompt(epic_id, classification, rationale, brief, branch, inbox_dir, ts))

        spawn = spawn or _default_spawn
        spawn(repo_root, prompt_file, out_dir, os.environ.get("CLEANER_MODEL", "claude-sonnet-4-6"),
              os.environ.get("CLEANER_BUDGET_USD", "5.00"), scope_card)

        ahead = int(_git(repo_root, "rev-list", "--count", "main..HEAD").stdout.strip() or "0")
        if ahead == 0:
            stuck = None
            if os.path.isdir(inbox_dir):
                for f in sorted(os.listdir(inbox_dir)):
                    if f.startswith(f"cleaner-stuck-{ts}") and f.endswith(".md"):
                        stuck = os.path.join(inbox_dir, f); break
            import re
            if stuck and re.search(r"main is clean|main has 0 errors|already (resolved|fixed)",
                                   open(stuck).read(), re.I):
                sys.stderr.write("[cleaner] worker correctly exited stuck — main is clean (rc=10)\n")
                _git(repo_root, "checkout", "main"); _git(repo_root, "branch", "-D", branch)
                json.dump({"verdict": "NO_OP", "reason": "main-already-clean"},
                          open(os.path.join(out_dir, "verdict.json"), "w"))
                return 10
            sys.stderr.write("[cleaner] worker produced no commits — aborting\n")
            _git(repo_root, "checkout", "main"); _git(repo_root, "branch", "-D", branch)
            json.dump({"verdict": "FAIL", "reason": "no commits"},
                      open(os.path.join(out_dir, "verdict.json"), "w"))
            return 6

        open(os.path.join(out_dir, "git.commits"), "w").write(
            _git(repo_root, "log", "--oneline", "main..HEAD").stdout)

        gauntlet_out = os.path.join(out_dir, "gauntlet")
        gauntlet_sh = os.path.join(mini_root, "lib", "gauntlet.sh")
        if not os.path.isfile(gauntlet_sh):
            gauntlet_sh = os.path.join(mo_home, "lib", "gauntlet.sh")
        gauntlet = gauntlet or _default_gauntlet
        grc = gauntlet(repo_root, gauntlet_sh, gauntlet_out)

        if grc != 0:
            required = os.environ.get("CLEANER_REQUIRED_CHECKS", "type-check build").split()
            gjson = os.path.join(gauntlet_out, "gauntlet.json")
            required_ok = True
            advisory = ""
            if os.path.isfile(gjson):
                try:
                    checks = json.load(open(gjson))
                except Exception:
                    checks = []
                by = {c.get("name"): c.get("status") for c in checks}
                for chk in required:
                    if by.get(chk) not in ("pass", "passed", "skipped"):
                        required_ok = False
                        sys.stderr.write(f"[cleaner] required check failed: {chk} ({by.get(chk)})\n")
                advisory = ",".join(c.get("name") for c in checks
                                    if c.get("status") in ("fail", "failed") and c.get("name") not in required)
            else:
                required_ok = False
            if required_ok:
                sys.stderr.write(f"[cleaner] required checks PASS (advisory fails: {advisory or 'none'})\n")
            else:
                sys.stderr.write("[cleaner] required checks FAIL — NOT auto-merging\n")
                json.dump({"verdict": "FAIL", "reason": "required cleaner checks failing",
                           "required_checks": " ".join(required), "advisory_fails": advisory,
                           "commits_made": ahead, "branch": branch},
                          open(os.path.join(out_dir, "verdict.json"), "w"))
                _git(repo_root, "checkout", "main")
                return 7

        sys.stderr.write(f"[cleaner] gauntlet PASS — squash-merging {branch} → main\n")
        _git(repo_root, "checkout", "main")
        _git(repo_root, "merge", "--squash", branch)
        _git(repo_root, "commit", "--no-verify", "-m",
             f"chore(baseline): cleaner fixed {classification} blocker for {epic_id}\n\n"
             f"Detective rationale: {rationale}\n\nCleaner brief: {brief}\n\n"
             f"Auto-spawned by mini-ork detective+cleaner. Commits squashed from "
             f"branch {branch} ({ahead} commits).")
        _git(repo_root, "branch", "-D", branch)
        new_sha = _git(repo_root, "rev-parse", "HEAD").stdout.strip()
        json.dump({"verdict": "PASS", "new_main_sha": new_sha, "squashed_commits": ahead,
                   "classification": classification}, open(os.path.join(out_dir, "verdict.json"), "w"))
        sys.stderr.write(f"[cleaner] === SUCCESS === new main sha={new_sha}\n")
        return 0
    finally:
        _restore_and_unlock()


def _build_prompt(epic_id, classification, rationale, brief, branch, inbox_dir, ts) -> str:
    return f"""You are the **mini-ork cleaner** — a single-shot agent that fixes a cross-cutting
blocker on main so paused epics can resume.

## Why you exist

Epic **{epic_id}** failed gauntlet with classification **{classification}**.
Detective rationale: {rationale}

You are NOT working on {epic_id}'s feature. You are unblocking the BASELINE.

## Your brief (verbatim from detective)

{brief}

## Live branch-state diff (read FIRST)

You are on branch `{branch}` (off main).

**If main has 0 errors AND your branch's failures are pre-existing on disk only, exit immediately**: write `{inbox_dir}/cleaner-stuck-{ts}.md` and exit.

## Hard rules — violations = abort + branch discarded

- NO architectural changes. NO refactoring. Read the type error, change the type/import/argument, run type-check, commit.
- NO touching files outside the brief's exact list.
- NO commit > 50 lines net change without explicit `cleaner_brief` permission.
- You are on branch `{branch}` (off main). Stay on this branch.
- If you can't complete the brief safely: commit nothing, `git stash`, write `{inbox_dir}/cleaner-stuck-{ts}.md`, exit.

## Output

When done, commit and STOP. The orchestrator picks up the rest.
"""


if __name__ == "__main__":
    raise SystemExit(main())
