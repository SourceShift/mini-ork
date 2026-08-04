"""Python port of bin/mini-ork-self-improve — wall-clock-budgeted outer loop
that drives the recursive-self-improve recipe against mini-ork itself.

Strangler-fig parity port. Per iter: git worktree off the base ref → stage lane
override → dispatch `mini-ork run recursive-self-improve` → read the verifier
triple → decide outcome → commit+optional-merge on success → record to
self_improve_runs + learning_record. Time-bounded by soft/hard caps.

The deterministic, bug-prone core is ported as pure functions and parity-gated:
  decide_outcome()      — the iter-33/34 verifier/exec_rc/task_runs cascade
  promote_synthesis_findings / record_success / record_run — the DB blocks (verbatim)
  read_verifier / seconds_to_hms / pre_iter_cost_check / validate_caps

The worktree/LLM dispatch and the bash throttle-guard are seams (dispatch=,
throttle=) — the default shells out exactly as the bash.

    main(argv=None, *, root=None, dispatch=None) -> int
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time

_USAGE = """Usage: mini-ork self-improve [flags]

Flags:
  --soft-cap-hours <H>   finish current iter when reached (default: 3)
  --hard-cap-hours <H>   kill mid-iter and exit (default: 5)
  --max-iters <N>        also bound iteration count (default: unbounded)
  --auto-merge           merge successful iters into parent branch
  --parent-branch <ref>  parent branch to merge into (default: resolved base ref)
  --dry-run              plan + scan + exit before any LLM dispatch
  --resume               continue an interrupted self-improve session
                         (reads MINI_ORK_DB to pick up the last iter)
  --help
"""


def seconds_to_hms(s: int) -> str:
    return f"{s // 3600:d}h{(s // 60) % 60:02d}m{s % 60:02d}s"


def validate_caps(soft: str, hard: str) -> bool:
    try:
        s = float(soft); h = float(hard)
    except (ValueError, TypeError):
        return False
    return 0 < s <= h <= 24


def decide_outcome(converged, exec_rc, pass_bottle, pass_tests, pass_reg,
                   tr_status="", pass_critic=1) -> tuple[str, str]:
    """Verbatim transcription of the outcome-decision cascade (steps 6+backstop).

    pass_critic is the opus post-implementation patch-critic gate (the 4th
    review layer). It defaults to 1 so callers that only supply the three
    deterministic verifiers are unaffected; when the recipe carries the critic
    node, a non-approving verdict blocks promotion exactly like a failed
    deterministic verifier — the point of tightening review for a weak
    implementer is that a diff which reds the critic never reaches success.
    """
    if converged == 1:
        outcome, notes = "converged", "scanner-reported-convergence"
    elif exec_rc == 124:
        outcome, notes = "timed_out", "per-iter-timeout"
    elif exec_rc != 0:
        outcome = "rejected"
        notes = (f"execute-rc={exec_rc};verifier-bottle={pass_bottle};verifier-tests={pass_tests};"
                 f"verifier-reg={pass_reg};critic={pass_critic}")
    elif pass_bottle == 1 and pass_tests == 1 and pass_reg == 1 and pass_critic == 1:
        outcome, notes = "success", "all-verifiers-pass"
    elif pass_bottle == 1 and (pass_tests == 0 or pass_reg == 0 or pass_critic == 0):
        outcome, notes = "rejected", "patch-failed-verifier"
    else:
        outcome, notes = "failed", "planner-or-synth-failed"
    # Backstop: task_runs.status is dispatch-level ground truth.
    if tr_status in ("failed", "rolled_back"):
        if outcome not in ("rejected", "failed", "timed_out", "aborted"):
            notes = f"{outcome}-overridden-by-task_runs.{tr_status};orig-notes={notes}"
            outcome = "rejected"
    return outcome, notes


def read_verifier(run_dir: str, vname: str) -> tuple[int, int]:
    jpath = os.path.join(run_dir, f"verifier-result-{vname}.json")
    if not os.path.isfile(jpath):
        return 0, 0
    try:
        d = json.load(open(jpath))
        return (1 if d.get("pass") else 0), (1 if d.get("converged") else 0)
    except Exception:
        return 0, 0


def read_verifier_triple(run_dir: str) -> tuple[int, int, int, int]:
    """Gated read: bottlenecks-found → (if pass & not converged) tests → reg."""
    pass_bottle, converged = read_verifier(run_dir, "bottlenecks-found")
    pass_tests = pass_reg = 0
    if pass_bottle == 1 and converged != 1:
        pass_tests, _ = read_verifier(run_dir, "self-tests-pass")
        if pass_tests == 1:
            pass_reg, _ = read_verifier(run_dir, "no-regression")
    return pass_bottle, pass_tests, pass_reg, converged


_REVIEW_PASS = {"pass", "approve", "approved"}


def read_review_verdict(run_dir: str, node_id: str) -> int:
    """The opus patch-critic's verdict, read from review-<node_id>.json (the
    classic-reviewer artifact _handle_reviewer writes before it gates). Returns
    1 only on an explicit approving verdict; a missing file, unparseable
    payload, or any non-approving verdict is 0 — fail-closed, so a critic that
    never ran (e.g. the run rolled back upstream) cannot be read as approval.
    Mirrors execute._REVIEW_PASS so the runner and the node agree on the
    vocabulary."""
    jpath = os.path.join(run_dir, f"review-{node_id}.json")
    if not os.path.isfile(jpath):
        return 0
    try:
        verdict = str(json.load(open(jpath)).get("verdict", "")).strip().lower()
    except Exception:
        try:
            m = re.search(r'"verdict"\s*:\s*"([^"]+)"', open(jpath).read())
            verdict = m.group(1).strip().lower() if m else ""
        except OSError:
            verdict = ""
    return 1 if verdict in _REVIEW_PASS else 0


# The improvable surface the kickoff hands the implementer. A rejected iteration
# must leave the main checkout pristine over exactly this surface — the loop
# edits a linked worktree that lives *under* the main tree, and the implementer
# harness has been observed writing back into the parent checkout instead
# (linked-worktree path resolution). This guard is a safety invariant, not a
# root-cause fix: it never touches a path the operator already had dirty before
# the run, so a concurrent hand-edit is safe.
_SELF_IMPROVE_SURFACE = ("mini_ork", "recipes", "schemas", "tests")


def _root_surface_dirty(root: str) -> set[str]:
    """Paths under the improvable surface that are dirty (modified/staged/
    untracked) in ``root`` right now. NUL-delimited so paths with spaces and
    rename records don't corrupt the set."""
    out = subprocess.run(
        ["git", "-C", root, "status", "--porcelain", "-z", "--", *_SELF_IMPROVE_SURFACE],
        capture_output=True, text=True).stdout
    dirty: set[str] = set()
    for rec in out.split("\0"):
        if len(rec) > 3:
            dirty.add(rec[3:])
    return dirty


def _restore_root_surface_leak(root: str, pre_dirty: set[str]) -> list[str]:
    """Revert any surface path that went clean→dirty *during* the run. Paths the
    operator already had dirty (in ``pre_dirty``) are never touched. Returns the
    list of paths restored, for the run log."""
    leaked = sorted(_root_surface_dirty(root) - pre_dirty)
    for path in leaked:
        # Untracked file created by the run → remove; tracked file mutated by the
        # run → restore working tree + index from HEAD.
        subprocess.run(["git", "-C", root, "checkout", "HEAD", "--", path], capture_output=True)
        subprocess.run(["git", "-C", root, "reset", "-q", "--", path], capture_output=True)
        full = os.path.join(root, path)
        if os.path.lexists(full) and subprocess.run(
                ["git", "-C", root, "ls-files", "--error-unmatch", "--", path],
                capture_output=True).returncode != 0:
            try:
                os.remove(full)
            except OSError:
                pass
    return leaked


_VALID_CATEGORY = {"perf", "correctness", "arch", "meta"}
_ARXIV_RE = re.compile(r"\b(\d{4}\.\d{4,6})\b")
_PATH_RE = re.compile(r"\b([a-z_][\w./-]*\.(?:sh|py|sql|md|yaml|yml|json|toml|tsx?|jsx?))\b", re.I)


def promote_synthesis_findings(db, run_id, iter_, synth_path) -> int:
    """Verbatim: parse synthesis.md's ranked patch table → learning_record."""
    try:
        text = open(synth_path).read()
    except OSError:
        return 0
    lines = text.splitlines()
    header_idx = None
    for i, line in enumerate(lines):
        low = line.lower()
        if (line.lstrip().startswith("|") and "rank" in low and "bottleneck" in low
                and "category" in low and "confidence" in low):
            header_idx = i
            break
    if header_idx is None:
        return 0
    table_rows = []
    for line in lines[header_idx + 2:]:
        if not line.strip() or not line.lstrip().startswith("|"):
            break
        table_rows.append(line)
    if not table_rows:
        return 0
    con = sqlite3.connect(db); con.execute("PRAGMA busy_timeout=5000")
    inserted = 0
    for row in table_rows:
        cells = [c.strip() for c in row.strip().strip("|").split("|")]
        if len(cells) < 6:
            continue
        rank_raw, bottleneck, category, patch_summary, evidence, confidence = cells[:6]
        try:
            rank = int(re.sub(r"\D", "", rank_raw) or "0")
        except ValueError:
            rank = 0
        category = (category or "meta").strip().lower()
        if category not in _VALID_CATEGORY:
            category = "meta"
        title = (bottleneck or "").strip()[:200]
        if not title:
            continue
        try:
            conf = float(confidence)
        except (ValueError, TypeError):
            conf = 0.0
        severity = "high" if conf >= 0.85 else "medium" if conf >= 0.7 else "low"
        arxiv = sorted(set(_ARXIV_RE.findall(evidence)))
        paths = sorted(set(_PATH_RE.findall(evidence)))
        ts = int(time.time())
        if con.execute("SELECT 1 FROM learning_record WHERE run_id=? AND iter=? AND rank=? AND title=? LIMIT 1",
                       (run_id, iter_, rank, title)).fetchone():
            continue
        con.execute(
            "INSERT INTO learning_record (run_id, iter, rank, category, title, evidence_paths,"
            " arxiv_refs, patch_summary, outcome, severity, confidence, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,'open',?,?,?,?)",
            (run_id, iter_, rank, category, title, json.dumps(paths), json.dumps(arxiv),
             patch_summary[:1000], severity, conf, ts, ts))
        inserted += 1
    con.commit(); con.close()
    return inserted


def record_success(db, run_id, iter_, branch, sha):
    ts = int(time.time())
    con = sqlite3.connect(db); con.execute("PRAGMA busy_timeout=5000")
    con.execute(
        "INSERT INTO learning_record (run_id, iter, rank, category, title, evidence_paths,"
        " arxiv_refs, patch_summary, outcome, severity, confidence, created_at, updated_at)"
        " VALUES (?,?,0,'meta',?,json_array(?),json_array(),?,'resolved','medium',1.0,?,?)",
        (run_id, int(iter_), f"iter {iter_} success — commit {sha[:8] if sha else 'unknown'}",
         branch, f"All 3 verifiers (bottlenecks-found, self-tests-pass, no-regression) passed; "
                 f"runner committed {sha} to {branch}.", ts, ts))
    con.execute("UPDATE learning_record SET outcome='superseded', updated_at=? WHERE outcome='deferred'", (ts,))
    con.commit(); con.close()


def record_run(db, run_id, iter_, status, notes, wt, branch, soft, hard):
    con = sqlite3.connect(db); con.execute("PRAGMA busy_timeout=5000")
    now = int(time.time())
    con.execute(
        "INSERT INTO self_improve_runs (run_id, started_at, finished_at, iter, worktree_path,"
        " branch_name, soft_deadline_at, hard_deadline_at, outcome, notes)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(run_id) DO UPDATE SET finished_at=excluded.finished_at,"
        " outcome=excluded.outcome, notes=excluded.notes",
        (run_id, now, now, int(iter_), wt or None, branch or None, int(soft), int(hard),
         status, notes or None))
    con.commit(); con.close()


def pre_iter_cost_check(db, budget) -> bool:
    """True → halt (over budget). Mirrors the SQL SUM vs MO_DAILY_BUDGET_USD."""
    if os.environ.get("MINI_ORK_PRE_ITER_COST_CHECK", "1") != "1" or not os.path.isfile(db):
        return False
    con = sqlite3.connect(db)
    try:
        spent = con.execute("SELECT COALESCE(SUM(cost_usd),0) FROM task_runs "
                            "WHERE created_at >= strftime('%s','now','-1 day')").fetchone()[0]
    except Exception:
        spent = 0
    finally:
        con.close()
    return float(spent or 0) >= float(budget)


def _parse_args(argv):
    o = {"soft": "3", "hard": "5", "max_iters": 0, "auto_merge": 0,
         "parent_branch": "", "dry_run": 0, "resume": 0}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--soft-cap-hours":
            o["soft"] = argv[i + 1]; i += 2
        elif a == "--hard-cap-hours":
            o["hard"] = argv[i + 1]; i += 2
        elif a == "--max-iters":
            o["max_iters"] = int(argv[i + 1]); i += 2
        elif a == "--auto-merge":
            o["auto_merge"] = 1; i += 1
        elif a == "--parent-branch":
            o["parent_branch"] = argv[i + 1]; i += 2
        elif a == "--dry-run":
            o["dry_run"] = 1; i += 1
        elif a == "--resume":
            o["resume"] = 1; i += 1
        elif a in ("--help", "-h"):
            return None, 0
        else:
            sys.stderr.write(f"Unknown flag: {a}\n" + _USAGE)
            return None, 2
    return o, None


def main(argv=None, *, root=None, dispatch=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    root = root or os.environ.get("MINI_ORK_ROOT") or os.path.dirname(
        os.path.dirname(os.path.realpath(__file__)))
    os.environ["MINI_ORK_ROOT"] = root

    opts, early = _parse_args(argv)
    if opts is None:
        if early == 0:
            sys.stdout.write(_USAGE)
        return early
    if not validate_caps(opts["soft"], opts["hard"]):
        sys.stderr.write("invalid cap hours\n")
        return 2

    home = os.environ.get("MINI_ORK_HOME") or os.path.join(os.getcwd(), ".mini-ork")
    db = os.environ.get("MINI_ORK_DB") or os.path.join(home, "state.db")
    os.environ["MINI_ORK_HOME"] = home; os.environ["MINI_ORK_DB"] = db
    for d in ("", "worktrees", "runs", "config", "state"):
        os.makedirs(os.path.join(home, d), exist_ok=True)

    # migration 0017 (idempotent)
    mig = os.path.join(root, "db", "migrations", "0017_self_improve_learning.sql")
    if shutil.which("sqlite3") and os.path.isfile(mig):
        if subprocess.run(["sqlite3", db], stdin=open(mig), capture_output=True).returncode != 0:
            sys.stderr.write("[err] migration 0017 failed\n")
            return 3

    ovr = os.path.join(root, "config", "agents.recursive-self-improve.yaml")
    dest = os.path.join(home, "config", "agents.yaml")
    if os.path.isfile(ovr):
        shutil.copyfile(ovr, dest)

    base_ref = os.environ.get("MINI_ORK_SELF_IMPROVE_BASE_REF", "main")
    parent = opts["parent_branch"]
    if not parent:
        if subprocess.run(["git", "-C", root, "rev-parse", "--verify", "--quiet", base_ref],
                          capture_output=True).returncode == 0:
            parent = base_ref
        else:
            parent = subprocess.run(["git", "-C", root, "rev-parse", "--abbrev-ref", "HEAD"],
                                    capture_output=True, text=True).stdout.strip()
    resolved_sha = subprocess.run(["git", "-C", root, "rev-parse", "--verify", parent],
                                  capture_output=True, text=True).stdout.strip() or "unknown"

    start = int(time.time())
    soft_deadline = start + int(float(opts["soft"]) * 3600)
    hard_deadline = start + int(float(opts["hard"]) * 3600)

    it = 0
    if opts["resume"] and os.path.isfile(db):
        try:
            con = sqlite3.connect(db)
            it = con.execute("SELECT COALESCE(MAX(iter),0) FROM self_improve_runs").fetchone()[0]
            con.close()
        except Exception:
            it = 0

    # Outer loop
    kill_flag = os.path.join(home, "state", ".self-improve-kill")
    while True:
        # Fix C (bash bin/mini-ork-self-improve:397-406): poll the UI kill-flag at each
        # iteration boundary — the /kill endpoint touches it; halt the outer loop and
        # CONSUME the flag. The port skipped this, so the runner ran through the kill.
        if os.path.isfile(kill_flag):
            print("==============================================================")
            print(f"[kill-flag] {kill_flag} present — UI requested outer-loop halt")
            print("==============================================================")
            try:
                os.remove(kill_flag)
            except OSError:
                pass
            break
        now = int(time.time())
        if now >= hard_deadline:
            print(f"[hard-cap] reached after {seconds_to_hms(now - start)}")
            break
        if opts["max_iters"] > 0 and it >= opts["max_iters"]:
            print(f"[max-iters] ITER={it} reached --max-iters={opts['max_iters']}")
            break
        rc, it = _iter_run(root, home, db, it, opts, parent, resolved_sha,
                           soft_deadline, hard_deadline, dispatch)
        if rc == 2:
            print("[abort] outer loop break signal received from iter (rc=2)")
            break
        try:
            con = sqlite3.connect(db)
            last = con.execute("SELECT outcome FROM self_improve_runs ORDER BY started_at DESC LIMIT 1").fetchone()
            con.close()
            if last and last[0] == "converged":
                print("[converged] scanner reported convergence; stopping")
                break
        except Exception:
            pass
        if int(time.time()) >= soft_deadline:
            print("[soft-cap] reached; exiting after this iter")
            break

    print(f"\nself-improve session complete\n  iters_run:   {it}\n  db:          {db}")
    return 0


def _iter_run(root, home, db, it, opts, parent, resolved_sha, soft_deadline, hard_deadline, dispatch):
    """One iteration. Returns (rc, new_iter). rc==2 → break outer loop."""
    budget = os.environ.get("MO_DAILY_BUDGET_USD", "50")
    if pre_iter_cost_check(db, budget):
        print(f"[cost-cap-pre-check] HALTING before iter {it + 1}: budget ${budget} reached")
        return 2, it

    it += 1
    ts = time.strftime("%Y%m%d%H%M%S", time.gmtime())
    run_id = f"self-improve-iter-{it}-{ts}"
    wt_path = os.path.join(home, "worktrees", f"iter-{it}-{ts}")
    branch = f"self-improve/iter-{it}-{ts}"
    record_run(db, run_id, it, "pending", "starting", wt_path, branch, soft_deadline, hard_deadline)

    # self-improve/* is a deliberate programmatic worktree; declare it to the
    # reference-transaction worktree-guard exactly as scripts/mini-ork-worktree.sh
    # does, else the hook rejects the non-wt/* branch creation and every iter
    # dies at worktree-add before any dispatch.
    if subprocess.run(["git", "-C", root, "worktree", "add", "-b", branch, wt_path, parent],
                      capture_output=True,
                      env={**os.environ, "ALLOW_WORKTREE_BRANCH_CREATE": "1"}).returncode != 0:
        record_run(db, run_id, it, "failed", "worktree-add-failed", wt_path, branch, soft_deadline, hard_deadline)
        return 1, it

    run_dir = os.path.join(home, "runs", run_id)
    os.makedirs(os.path.join(run_dir, "patches"), exist_ok=True)
    # A bare "Target: <path>" kickoff scores needs_answers at the run_profile
    # gate (no scope / success-criteria / proof command → confidence ~0.55) and
    # the planner refuses to dispatch. Emit the full scope block the classifier
    # needs so the iteration actually plans. Mirrors
    # recipes/recursive-self-improve/example-kickoff.md but with the current
    # Python verification command (bash tests/run-all.sh was retired 2026-07).
    open(os.path.join(run_dir, "kickoff.md"), "w").write(
        f"""# Recursive Self-Improvement — iter {it}

## Goal
Run mini-ork against itself: find and fix the single highest-impact bottleneck
in performance, correctness, or architecture, grounded in cited evidence (run
logs, code paths, benchmark deltas, and arXiv references).

## Scope
- Target repo: this mini-ork checkout at `{wt_path}` — an isolated git worktree
  on branch `{branch}`.
- Improvable surface: the `mini_ork/` Python package, `recipes/`, and
  `schemas/`. Leave `.mini-ork/` (run state) untouched; tests are read-only
  context unless the patch adds a regression test.
- Exactly one patch this iteration; keep the diff minimal and reviewable.

## Success Criteria
- The bottleneck scanner yields a ranked list with at least one
  evidence-grounded candidate.
- The Opus synthesizer emits a patch plan citing both internal evidence (run
  logs / code paths) and external evidence (arXiv refs).
- All three verifiers pass: bottlenecks-found, self-tests-pass, no-regression.
- Existing tests pass with a non-negative quality delta on the patch.

## Provider Policy
- Research lenses: `minimax_lens`, `kimi_lens`, `codex_lens` (no Anthropic).
- Synthesis reviewer: `opus_lens`. Implementer: `codex_lens`.

## Verification Command
- `python3 -m pytest -q` (the Python runtime is the only runtime).
""")

    if opts["dry_run"]:
        print(f"  [dry-run] would dispatch recipe recursive-self-improve inside {wt_path}")
        record_run(db, run_id, it, "aborted", "dry-run", wt_path, branch, soft_deadline, hard_deadline)
        subprocess.run(["git", "-C", root, "worktree", "remove", "--force", wt_path], capture_output=True)
        subprocess.run(["git", "-C", root, "branch", "-D", branch], capture_output=True)
        return 0, it

    # dispatch (seam)
    exec_log = os.path.join(run_dir, "execute.log")
    time_left = hard_deadline - int(time.time())
    per_iter = min(3600, time_left)
    # The iteration worktree lives UNDER the framework tree (home/worktrees/…),
    # so every lane dispatch trips providers.cwd_guard, which refuses a cwd inside
    # mini-ork to stop a target-repo lane from corrupting the framework. Recursive
    # self-improve IS the one sanctioned exception the guard names — a genuine
    # mini-ork self-edit — so opt in explicitly; without it the planner (opus) and
    # every other lane fail preflight with "cwd guard failed" and the iter dies
    # before any research runs.
    env = {**os.environ, "MINI_ORK_RUN_ID": run_id, "MINI_ORK_RUN_DIR": run_dir,
           "MINI_ORK_RECIPE": "recursive-self-improve", "MINI_ORK_SELF_IMPROVE_ITER": str(it),
           "MINI_ORK_SELF_IMPROVE_WORKTREE": wt_path, "MINI_ORK_TIMESTAMP": ts,
           "MO_ALLOW_FRAMEWORK_CWD": "1",
           "MO_RUNTIME_BACKEND": os.environ.get("MO_RUNTIME_BACKEND", "bubblewrap")}
    def _default_dispatch(wt, kickoff, log, timeout_s, e):
        with open(log, "wb") as fh:
            return subprocess.run(
                ["timeout", str(timeout_s), os.path.join(root, "bin", "mini-ork"),
                 "run", "recursive-self-improve", kickoff],
                cwd=wt, stdout=fh, stderr=subprocess.STDOUT, env=e).returncode
    # Snapshot the operator's pre-run dirty surface so a leak the dispatch
    # writes back into the main checkout can be told apart from work the
    # operator already had in flight (see _restore_root_surface_leak).
    pre_dirty = _root_surface_dirty(root)
    exec_rc = (dispatch or _default_dispatch)(
        wt_path, os.path.join(run_dir, "kickoff.md"), exec_log, per_iter, env)

    pass_bottle, pass_tests, pass_reg, converged = read_verifier_triple(run_dir)
    # 4th gate: the opus patch-critic reviews the implementer's actual diff after
    # the deterministic triple passes. Read fail-closed — only an explicit approve
    # counts — so a weak implementer's off-plan / gamed / no-op patch that slips the
    # deterministic gates is still blocked before promotion.
    pass_critic = read_review_verdict(run_dir, "opus_patch_critic")
    print(f"  [iter] verifiers: bottle={pass_bottle} tests={pass_tests} regression={pass_reg} "
          f"critic={pass_critic} converged={converged} exec_rc={exec_rc}")

    tr_status = ""
    if os.path.isfile(db):
        try:
            con = sqlite3.connect(db)
            r = con.execute("SELECT status FROM task_runs WHERE id=?", (run_id,)).fetchone()
            con.close()
            tr_status = r[0] if r else ""
        except Exception:
            tr_status = ""
    outcome, notes = decide_outcome(converged, exec_rc, pass_bottle, pass_tests, pass_reg,
                                    tr_status, pass_critic)

    if outcome == "success":
        dirty = (subprocess.run(["git", "-C", wt_path, "diff", "--quiet"], capture_output=True).returncode != 0
                 or subprocess.run(["git", "-C", wt_path, "diff", "--cached", "--quiet"], capture_output=True).returncode != 0
                 or bool(subprocess.run(["git", "-C", wt_path, "ls-files", "--others", "--exclude-standard"],
                                        capture_output=True, text=True).stdout.strip()))
        if dirty:
            subprocess.run(["git", "-C", wt_path, "add", "-A", "--", ":!.mini-ork/", ":!lens-*.md",
                            ":!synthesis.md*", ":!context-*.json", ":!arxiv-*.md", ":!*-research.json"],
                           capture_output=True)
            subprocess.run(["git", "-C", wt_path, "commit",
                            "-m", f"self-improve: iter {it} — see runs/{run_id}/synthesis.md",
                            "--author=mini-ork-self-improve <self-improve@mini-ork.local>"], capture_output=True)
            sha = subprocess.run(["git", "-C", wt_path, "rev-parse", "HEAD"],
                                 capture_output=True, text=True).stdout.strip()
            record_success(db, run_id, it, branch, sha)
            promote_synthesis_findings(db, run_id, it, os.path.join(run_dir, "synthesis.md"))
        if opts["auto_merge"]:
            subprocess.run(["git", "-C", root, "merge", "--no-ff",
                            "-m", f"merge self-improve iter {it}", branch], capture_output=True)
    else:
        print(f"  [iter] discarding worktree (outcome={outcome})")
        with open(os.path.join(run_dir, "patches", f"iter-{it}.diff"), "wb") as fh:
            subprocess.run(["git", "-C", wt_path, "diff"], stdout=fh, stderr=subprocess.DEVNULL)

    # Invariant: an iteration promotes only through the worktree branch + merge,
    # never through the main checkout's working tree. Revert any surface path the
    # dispatch leaked into root so main stays pristine regardless of outcome.
    leaked = _restore_root_surface_leak(root, pre_dirty)
    if leaked:
        print(f"  [iter] restored {len(leaked)} leaked path(s) in main checkout: {', '.join(leaked)}")
    subprocess.run(["git", "-C", root, "worktree", "remove", "--force", wt_path], capture_output=True)
    record_run(db, run_id, it, outcome, notes, wt_path, branch, soft_deadline, hard_deadline)
    return 0, it


if __name__ == "__main__":
    raise SystemExit(main())
