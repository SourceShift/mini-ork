"""Python port of lib/repo_integrity_guard.sh — standing guard against
cross-repo clobbers of the current branch ref.

Strangler-fig parity port of ``repo_integrity_check_and_heal``. Best-effort: any
git error returns 0 (never aborts the caller). Git operations shell out; the
ported logic is the baseline resolution (LKG file → origin/main → reflog), the
two-condition clobber test (baseline-not-ancestor AND tip-older), and the
compare-and-swap heal via ``git update-ref``.

    check_and_heal(cwd=None, now_iso=None) -> 0 always

``now_iso`` (bash's ``date -u +%Y-%m-%dT%H:%M:%SZ``) is injectable so the parity
test can pin the TSV recovery-log timestamp.
"""
from __future__ import annotations

import os
import subprocess


def _git(cwd, *args) -> tuple[str, int]:
    r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)
    return r.stdout.strip(), r.returncode


def check_and_heal(cwd: str | None = None, now_iso: str | None = None) -> int:
    if os.environ.get("MO_REPO_INTEGRITY_GUARD_DISABLED", "0") == "1":
        return 0
    cwd = cwd or os.getcwd()
    repo_root, rc = _git(cwd, "rev-parse", "--show-toplevel")
    if rc != 0 or not repo_root:
        return 0

    branch, rc = _git(repo_root, "symbolic-ref", "--quiet", "--short", "HEAD")
    if rc != 0 or not branch:
        return 0  # detached HEAD
    cur_tip, rc = _git(repo_root, "rev-parse", "--verify", "-q", "HEAD")
    if rc != 0 or not cur_tip:
        return 0

    mo_dir = os.path.join(repo_root, ".mini-ork")
    os.makedirs(mo_dir, exist_ok=True)
    branch_safe = branch.replace("/", "__")
    lkg_file = os.path.join(mo_dir, f"last-known-good-ref.{branch_safe}")
    log_file = os.path.join(mo_dir, "repo-integrity-guard.log")

    # ── baseline resolution: LKG file → origin/main → reflog ────────────────
    baseline = ""
    if os.path.exists(lkg_file) and os.path.getsize(lkg_file) > 0:
        with open(lkg_file) as f:
            baseline = f.read().strip()
    if not baseline:
        om, rc = _git(repo_root, "rev-parse", "--verify", "-q", "origin/main")
        if rc == 0:
            baseline = om
    if not baseline:
        rl, rc = _git(repo_root, "reflog", "show", f"refs/heads/{branch}", "--format=%H", "-n", "1")
        if rc == 0 and rl:
            baseline = rl.splitlines()[0]

    def _record(tip):
        try:
            with open(lkg_file, "w") as f:
                f.write(tip + "\n")
        except OSError:
            pass

    # cold start / identity → record tip and exit
    if not baseline:
        _record(cur_tip)
        return 0
    if cur_tip == baseline:
        _record(cur_tip)
        return 0

    # ── two-condition clobber test ──────────────────────────────────────────
    _, anc_rc = _git(repo_root, "merge-base", "--is-ancestor", baseline, cur_tip)
    orphan = anc_rc != 0   # baseline NOT an ancestor of tip → orphaned

    tip_ct_s, _ = _git(repo_root, "show", "-s", "--format=%ct", cur_tip)
    base_ct_s, _ = _git(repo_root, "show", "-s", "--format=%ct", baseline)
    tip_ct = int(tip_ct_s) if tip_ct_s.isdigit() else 0
    base_ct = int(base_ct_s) if base_ct_s.isdigit() else 0
    older = tip_ct < base_ct

    if orphan and older:
        # CLOBBER — heal via compare-and-swap update-ref only
        _, rc = _git(repo_root, "update-ref", f"refs/heads/{branch}", baseline, cur_tip)
        if rc == 0:
            ts = now_iso or subprocess.run(
                ["date", "-u", "+%Y-%m-%dT%H:%M:%SZ"], capture_output=True, text=True
            ).stdout.strip()
            try:
                with open(log_file, "a") as f:
                    f.write(f"{ts}\t{baseline}\t{cur_tip}\trestored-branch-clobbered-from-{cur_tip}\n")
            except OSError:
                pass
        # do NOT rewrite LKG on heal — recorded good SHA is still the baseline
        return 0

    # legitimate advance → re-record
    _record(cur_tip)
    return 0
