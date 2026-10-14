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

import fnmatch
import os
import subprocess

# ── frozen kernel ───────────────────────────────────────────────────────────
# The paths an auto-opened (origin=auto-triage) framework-edit epic may never
# change: the escalation policy, the circuit breaker, the budget pause, the
# abstention gate, the sandbox, the integrity guard, the verifier that enforces
# all of this, and the list itself. This built-in list is the FLOOR: it is the
# fail-closed fallback for a missing/unparsable ``config/kernel-frozen.yaml`` and
# the guaranteed minimum every consumer unions with the file, so a diff can never
# unfreeze a path by editing (or deleting) the list.
FROZEN_CONFIG_RELPATH = os.path.join("config", "kernel-frozen.yaml")

FROZEN_PATHS_DEFAULT: list[str] = [
    "config/kernel-frozen.yaml",
    "recipes/framework-edit/verifiers/static-check.py",
    "mini_ork/vcs/repo_integrity_guard.py",
    "mini_ork/runtime/sandbox.py",
    "mini_ork/gates/gate_bootstrap.py",
    "mini_ork/gates/abstain_gate.py",
    "mini_ork/recovery/circuit_breaker.py",
    # The escalation policy. escalation_policy.py is a FORWARD DECLARATION — the
    # operator-owned module that admits auto fix epics lands in a later step;
    # until it does, the decision is made at calibration.should_escalate,
    # auto_repair._escalate_lane, policies.engine.verifier_failure_escalation.
    "mini_ork/recovery/escalation_policy.py",
    "mini_ork/dispatch/calibration.py",
    "mini_ork/recovery/auto_repair.py",
    "mini_ork/policies/engine.py",
    "mini_ork/dispatch/cost_pause.py",
    "mini_ork/stores/safety_events.py",
    "mini_ork/observability/bug_report.py",
]


def _extract_paths(data: object) -> list[str]:
    """Pull the ``paths:`` list out of a parsed kernel-frozen document.

    A document that is not a mapping, has no ``paths`` key, or whose ``paths`` is
    not a list of strings yields ``[]`` — the caller then falls back to the
    built-in default rather than treating a malformed file as "no frozen paths"."""
    if not isinstance(data, dict):
        return []
    paths = data.get("paths")
    if not isinstance(paths, list):
        return []
    out = [p for p in paths if isinstance(p, str) and p.strip()]
    return out


def _minimal_parse(text: str) -> list[str]:
    """Last-resort ``paths:`` extractor for when PyYAML is unavailable.

    Reads the indented ``- <path>`` block under a ``paths:`` key. Deliberately
    narrow: anything it cannot make sense of returns ``[]`` so the caller fails
    closed to the built-in default."""
    out: list[str] = []
    in_paths = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if not line.startswith("- "):
            in_paths = line == "paths:"
            continue
        if in_paths:
            item = line[2:].strip().strip("'\"")
            if item:
                out.append(item)
    return out


def parse_frozen_paths(text: str) -> list[str]:
    """Parse the frozen-path list from a kernel-frozen document string.

    Returns the parsed paths, or ``[]`` when the text is empty/unparsable. The
    pure-parse entry point (no filesystem, no fallback) exists so callers that
    already hold the text — e.g. ``git show HEAD:config/kernel-frozen.yaml`` —
    and callers deciding on a missing file can share one grammar."""
    if not text or not text.strip():
        return []
    try:
        import yaml  # type: ignore
    except Exception:
        return _minimal_parse(text)
    try:
        data = yaml.safe_load(text)
    except Exception:
        return _minimal_parse(text)
    return _extract_paths(data)


def load_frozen_paths(root: str) -> list[str]:
    """Read ``<root>/config/kernel-frozen.yaml``; fail closed to the built-in list.

    A missing, unreadable, empty, or unparsable file returns a copy of
    ``FROZEN_PATHS_DEFAULT`` — never an empty list. Callers that want the file's
    entries *in addition to* the guaranteed floor should union the two."""
    path = os.path.join(str(root), FROZEN_CONFIG_RELPATH)
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return list(FROZEN_PATHS_DEFAULT)
    parsed = parse_frozen_paths(text)
    if not parsed:
        return list(FROZEN_PATHS_DEFAULT)
    return parsed


def frozen_hits(changed: list[str], patterns: list[str]) -> list[str]:
    """Return the ``changed`` paths matching any frozen ``patterns`` (fnmatch).

    Order-preserving and de-duplicated so callers can report the offending paths
    verbatim. An empty ``patterns`` list yields ``[]`` — callers must pass the
    fail-closed list, not an empty one."""
    hits: list[str] = []
    seen: set[str] = set()
    for path in changed:
        if path in seen:
            continue
        for pat in patterns:
            if fnmatch.fnmatch(path, pat):
                hits.append(path)
                seen.add(path)
                break
    return hits


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
    if not baseline and branch == "main":
        # origin/main is a baseline only FOR main. A feature/worktree branch is
        # expected to trail or diverge from it; comparing against it flagged
        # every fresh worktree branch as "clobbered" once another session
        # pushed, and the heal fast-forwarded the ref under a live index.
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

    # ── behind is not clobbered ─────────────────────────────────────────────
    # A tip that is an ancestor of the baseline was overtaken or deliberately
    # moved back along its own line — never a foreign-history clobber. A bare
    # update-ref here would move HEAD without the index and stage a phantom
    # reverse diff of every commit in between (observed 2026-10-02).
    _, behind_rc = _git(repo_root, "merge-base", "--is-ancestor", cur_tip, baseline)
    if behind_rc == 0:
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
