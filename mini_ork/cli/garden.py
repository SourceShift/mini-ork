"""Native Python port of ``bin/mini-ork-garden`` — drift detection with
concrete ``Fix:`` hints.

Mirrors wshobson/agents' ``make garden`` discipline: scans for stale runs,
orphaned worktrees, output-path collisions, missing env-var docs, and
oversize recipe prompts. Every finding ships a remediation command.

This is a parity port: stdout/stderr text, finding order, exit codes, and
even one bash quirk (see ``_orphan_stashes``) match the bash source. One
check has no bash counterpart — ``_check_inert_mechanisms`` reads
``state.db`` rather than the filesystem (see its comment) and runs after the
ported five, so their output order and text are untouched.

    main(argv=None) -> int

Path resolution replaces ``lib/paths.sh`` sourcing with the conventions used
by the other native CLI modules:

    root        = $MINI_ORK_ROOT        or <package>/../.. (engine root)
    home        = $MINI_ORK_HOME        or ./.mini-ork
    target_repo = $MINI_ORK_TARGET_REPO or $PWD

Exit codes (mirror bash exactly):

    0  clean, or only warnings/infos in non-strict mode
    1  errors found, or warnings found with --strict
    2  usage error (unknown flag / unexpected positional)
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

# ─────────────────────────────────────────────────────────────────────────────
# Help text — verbatim copy of bash's `cat <<'EOF' … EOF` block in _usage().
# ─────────────────────────────────────────────────────────────────────────────
HELP_TEXT = (
    "Usage: mini-ork garden [--strict]\n"
    "\n"
    "Drift detection. Every finding includes a concrete Fix: hint.\n"
    "\n"
    "Options:\n"
    "  --strict    Exit nonzero on warnings too.\n"
    "  --help      Show this help.\n"
)

MAX_PROMPT_KB = 32
MAX_WORKFLOW_KB = 16

# Env vars exempt from the docs-drift check (bash's grep -vE exclusion list).
_ENV_DOC_EXCLUDE = frozenset({
    "MINI_ORK_ROOT",
    "MINI_ORK_HOME",
    "MINI_ORK_DB",
    "MINI_ORK_RUN_ID",
    "MINI_ORK_RECIPE",
    "MINI_ORK_WORKFLOW",
    "MINI_ORK_TASK_CLASS",
    "MINI_ORK_PROFILE_PATH",
    "MINI_ORK_TARGET_REPO",
    "MINI_ORK_ENGINE_ROOT",
    "MINI_ORK_PROJECT_HOME",
})

# bash: grep -RoE '\bMO_[A-Z_]+\b|\bMINI_ORK_[A-Z_]+\b' lib bin
_ENV_VAR_RE = re.compile(r"\bMO_[A-Z_]+\b|\bMINI_ORK_[A-Z_]+\b")

# bash: grep -E '^stash@\{[0-9]+\}: wip-pre-implementer'
_STASH_RE = re.compile(r"^stash@\{\d+\}: wip-pre-implementer")


# ─────────────────────────────────────────────────────────────────────────────
# Findings collector — mirrors bash's _error/_warn/_info + ERRORS/WARNINGS/
# INFOS counters. All findings go to stderr with the same tag padding.
# ─────────────────────────────────────────────────────────────────────────────
class Findings:
    def __init__(self) -> None:
        self.errors = 0
        self.warnings = 0
        self.infos = 0

    def _emit(self, tag: str, msg: str, fix: str) -> None:
        sys.stderr.write(f"{tag} {msg}\n")
        sys.stderr.write(f"          Fix: {fix}\n")

    def error(self, msg: str, fix: str) -> None:
        self._emit("[error]  ", msg, fix)
        self.errors += 1

    def warn(self, msg: str, fix: str, *, counted: bool = True) -> None:
        self._emit("[warning]", msg, fix)
        if counted:
            self.warnings += 1

    def info(self, msg: str, fix: str) -> None:
        self._emit("[info]   ", msg, fix)
        self.infos += 1


# ─────────────────────────────────────────────────────────────────────────────
# Check 1 — output-path collisions across recipes (verbatim port of the bash
# heredoc: only target-repo paths are flagged; ${MINI_ORK_RUN_DIR} paths are
# intentionally shared).
# ─────────────────────────────────────────────────────────────────────────────
def _collision_map(root: str) -> list[tuple[str, list[str]]]:
    import yaml
    from collections import defaultdict

    collisions: dict[str, list[str]] = defaultdict(list)
    recipes_dir = os.path.join(root, "recipes")
    if not os.path.isdir(recipes_dir):
        return []
    for recipe in sorted(os.listdir(recipes_dir)):
        ac = os.path.join(recipes_dir, recipe, "artifact_contract.yaml")
        if not os.path.isfile(ac):
            continue
        try:
            with open(ac) as f:
                data = yaml.safe_load(f) or {}
        except Exception:
            continue
        for out in data.get("outputs") or []:
            path = out.get("path") if isinstance(out, dict) else out
            # Run-dir internal paths are intentionally shared; only flag
            # target-repo paths.
            if isinstance(path, str) and not path.startswith("${MINI_ORK_RUN_DIR}"):
                collisions[path].append(recipe)
    return [(out, collisions[out]) for out in sorted(collisions)
            if len(collisions[out]) > 1]


def _check_collisions(root: str, findings: Findings) -> None:
    for path, recipes in _collision_map(root):
        findings.error(
            f"output collision: '{path}' used by {','.join(recipes)}",
            "use recipe-specific output paths in artifact_contract.yaml",
        )


# ─────────────────────────────────────────────────────────────────────────────
# Check 2 — oversize recipe prompts / workflows.
# bash: find "$MINI_ORK_ROOT/recipes" -type f -name '*.md' / 'workflow.yaml',
# size_kb = st_size // 1024 (bash integer division).
# ─────────────────────────────────────────────────────────────────────────────
def _walk_files(base: str, name_pred) -> list[str]:
    """``find <base> -type f`` — depth-first, directory order, no symlinks."""
    out = []
    if not os.path.isdir(base):
        return out
    for dirpath, _dirs, files in os.walk(base):
        for fn in files:
            path = os.path.join(dirpath, fn)
            if os.path.islink(path) or not os.path.isfile(path):
                continue
            if name_pred(fn):
                out.append(path)
    return out


def _check_sizes(root: str, findings: Findings) -> None:
    recipes_dir = os.path.join(root, "recipes")
    for f in _walk_files(recipes_dir, lambda n: n.endswith(".md")):
        size_kb = os.path.getsize(f) // 1024
        if size_kb > MAX_PROMPT_KB:
            findings.warn(
                f"recipe prompt exceeds {MAX_PROMPT_KB} KiB: {f} ({size_kb} KiB)",
                "split detail into recipes/<name>/prompts/references/",
            )
    for f in _walk_files(recipes_dir, lambda n: n == "workflow.yaml"):
        size_kb = os.path.getsize(f) // 1024
        if size_kb > MAX_WORKFLOW_KB:
            findings.warn(
                f"recipe workflow exceeds {MAX_WORKFLOW_KB} KiB: {f} ({size_kb} KiB)",
                "decompose into smaller nodes or move detail to references/",
            )


# ─────────────────────────────────────────────────────────────────────────────
# Check 3 — stale runs directories.
# bash: find "$MINI_ORK_HOME/runs" -maxdepth 1 -type d -mtime +30
# (the runs dir itself is included in find's output when it is old enough).
# ─────────────────────────────────────────────────────────────────────────────
def _check_stale_runs(home: str, findings: Findings, *, now: float | None = None) -> None:
    runs_dir = os.path.join(home, "runs")
    if not os.path.isdir(runs_dir):
        return
    now = time.time() if now is None else now
    # find lists the start dir first, then its direct children.
    candidates = [runs_dir]
    for entry in os.listdir(runs_dir):
        path = os.path.join(runs_dir, entry)
        if os.path.isdir(path) and not os.path.islink(path):
            candidates.append(path)
    for run_dir in candidates:
        # find -mtime +30: age in 24h units, rounded down, strictly > 30.
        age_days = int((now - os.path.getmtime(run_dir)) / 86400)
        if age_days > 30:
            findings.info(
                f"run directory older than 30 days: {run_dir}",
                f"archive or remove with 'rm -rf {run_dir}'",
            )


# ─────────────────────────────────────────────────────────────────────────────
# Check 4 — orphaned wip-pre-implementer stashes in the target repo.
#
# BASH QUIRK (preserved for parity): the bash loop runs inside a pipeline
# (`git stash list | grep ... | while read`), i.e. in a subshell, so the
# WARNINGS increments performed by _warn are LOST when the subshell exits.
# The warning lines are printed, but the warnings counter does not move —
# meaning a garden run whose only findings are orphan stashes still prints
# "garden: clean" and never trips --strict. counted=False mirrors that.
# ─────────────────────────────────────────────────────────────────────────────
def _check_orphan_stashes(target_repo: str, findings: Findings) -> None:
    if not shutil.which("git"):
        return
    result = subprocess.run(
        ["git", "-C", target_repo, "stash", "list"],
        capture_output=True,
        text=True,
        check=False,  # bash: 2>/dev/null ... || true — failures tolerated
    )
    for line in result.stdout.splitlines():
        if _STASH_RE.search(line):
            findings.warn(
                f"orphaned implementer stash in target repo: {line}",
                "review and drop with 'git stash drop <stash>'",
                counted=False,  # see docstring: bash pipeline-subshell quirk
            )


# ─────────────────────────────────────────────────────────────────────────────
# Check 5 — env-var docs drift. Collect MO_*/MINI_ORK_* referenced in lib/
# and bin/ and require docs/operator/env-vars.md to exist when any
# non-exempt vars are referenced.
# ─────────────────────────────────────────────────────────────────────────────
def _referenced_env_vars(root: str) -> set[str]:
    found: set[str] = set()
    for sub in ("lib", "bin"):
        base = os.path.join(root, sub)
        if not os.path.isdir(base):
            continue
        for dirpath, _dirs, files in os.walk(base):
            for fn in files:
                try:
                    with open(os.path.join(dirpath, fn), errors="replace") as f:
                        text = f.read()
                except OSError:
                    continue
                found.update(_ENV_VAR_RE.findall(text))
    return found


def _check_env_docs(root: str, findings: Findings) -> None:
    remaining = _referenced_env_vars(root) - _ENV_DOC_EXCLUDE
    if remaining:
        env_doc = os.path.join(root, "docs", "operator", "env-vars.md")
        if not os.path.isfile(env_doc):
            findings.warn(
                f"env-var documentation missing: {env_doc}",
                f"create {env_doc} documenting env vars",
            )


# ─────────────────────────────────────────────────────────────────────────────
# Check 6 — installed-but-inert mechanisms. Extension beyond the bash source:
# the only check here that reads ``state.db`` rather than the filesystem.
#
# A mechanism can be wired in, switched on, and still never fire. UCCI
# escalation abstains until its calibration slice clears a sample floor, so a
# tree with a live router and five margin rows looks healthy while the
# escalation path is structurally unreachable — and no filesystem check can
# tell that from a mechanism that is working.
#
# Three trace columns are the chain, each written by a different migration:
#
#   route_margin IS NOT NULL              the calibration input exists (0057)
#   predicted_error IS NOT NULL           the map produced a prediction (0059)
#   route_source = 'calibrated_escalation'  the pick was overridden (0054)
#
# Severity is deliberately ``info``, never ``warning``: a fresh database has no
# rows and a mechanism switched off has no firings, and neither is drift. A
# correctly dormant mechanism must never turn --strict red.
# ─────────────────────────────────────────────────────────────────────────────
def _check_inert_mechanisms(home: str, findings: Findings) -> None:
    import sqlite3

    db = os.environ.get("MINI_ORK_DB") or os.path.join(home, "state.db")
    if not os.path.isfile(db):
        return
    from mini_ork.dispatch import calibration

    if not calibration.enabled():
        findings.info(
            "UCCI escalation disabled (MO_UCCI=0)",
            "unset MO_UCCI to restore calibrated escalation",
        )
        return

    floor = calibration.min_samples()
    try:
        con = sqlite3.connect(db)
        con.execute("PRAGMA busy_timeout=5000")
        try:
            # Grouped, so the result set is bounded by #task_class x #lane
            # rather than by the trace count.
            groups = con.execute(
                "SELECT task_class, agent_version_id, COUNT(*), "
                "       SUM(CASE WHEN predicted_error IS NOT NULL THEN 1 ELSE 0 END) "
                "FROM execution_traces "
                "WHERE route_margin IS NOT NULL AND created_at >= ? "
                "GROUP BY task_class, agent_version_id",
                (calibration.recent_cutoff(),)).fetchall()
            fired, last = con.execute(
                "SELECT COUNT(*), MAX(created_at) FROM execution_traces "
                "WHERE route_source = 'calibrated_escalation'").fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        # Locked, foreign, or pre-migration database — fail open. Reporting
        # "0 firings" from a DB that cannot answer would be a false alarm, and
        # the columns read above only exist from migration 0057 onward.
        return

    # ``_fit`` tries the lane's own slice first, then the task_class pooled
    # across lanes, so a slice counts as fitted if either grouping clears the
    # floor. Anything less means the map abstains — for every lane in it.
    pooled: dict[str, int] = {}
    for task_class, _lane, n, _pred in groups:
        pooled[task_class] = pooled.get(task_class, 0) + n
    lane_sizes = [n for _tc, _lane, n, _pred in groups]
    best = max(lane_sizes + list(pooled.values()), default=0)
    rows = sum(lane_sizes)
    predictions = sum(p for _tc, _lane, _n, p in groups)

    if fired:
        # All-time on purpose, and without the windowed counts: a mechanism that
        # fired before the recency window is not dead, and mixing the two clocks
        # in one line would read as "0 predictions, 3 escalations".
        findings.info(
            f"UCCI escalation fired: {fired} escalation(s), last {last}",
            "no action; the map is consulted and overriding picks",
        )
    elif best < floor:
        findings.info(
            f"UCCI escalation installed but inert: {rows} margin row(s), largest slice "
            f"{best} < floor {floor}; the map abstains on every call",
            f"feed one slice {floor - best} more margin row(s), or lower "
            "MO_UCCI_MIN_SAMPLES to smoke the path",
        )
    else:
        findings.info(
            f"UCCI escalation fitted but never fired: largest slice {best} >= floor "
            f"{floor}, {predictions} prediction(s), 0 escalation(s)",
            "inspect the margin distribution against MO_UCCI_TARGET_ERROR",
        )


# ─────────────────────────────────────────────────────────────────────────────
# Path resolution — the Python-side replacement for sourcing lib/paths.sh.
# ─────────────────────────────────────────────────────────────────────────────
def _resolve_paths() -> tuple[str, str, str]:
    root = (os.environ.get("MINI_ORK_ROOT")
            or str(Path(__file__).resolve().parents[2]))
    home = (os.environ.get("MINI_ORK_HOME")
            or os.path.join(os.getcwd(), ".mini-ork"))
    target_repo = os.environ.get("MINI_ORK_TARGET_REPO") or os.getcwd()
    return root, home, target_repo


# ─────────────────────────────────────────────────────────────────────────────
# CLI dispatcher — mirrors bash's arg-parsing and exit-code flow exactly.
# ─────────────────────────────────────────────────────────────────────────────
def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    strict = False
    for arg in argv:
        if arg in ("--help", "-h"):
            sys.stdout.write(HELP_TEXT)
            return 0
        if arg == "--strict":
            strict = True
        elif arg.startswith("-"):
            sys.stderr.write(f"Unknown flag: {arg}\n")
            sys.stderr.write(HELP_TEXT)
            return 2
        else:
            sys.stderr.write(f"Unexpected argument: {arg}\n")
            sys.stderr.write(HELP_TEXT)
            return 2

    root, home, target_repo = _resolve_paths()

    findings = Findings()
    _check_collisions(root, findings)
    _check_sizes(root, findings)
    _check_stale_runs(home, findings)
    _check_orphan_stashes(target_repo, findings)
    _check_env_docs(root, findings)
    _check_inert_mechanisms(home, findings)  # no bash counterpart; DB-backed

    # ── summary (order mirrors bash) ──
    e, w, i = findings.errors, findings.warnings, findings.infos
    if e > 0:
        sys.stderr.write(f"garden: {e} error(s), {w} warning(s), {i} info\n")
        return 1
    if strict and w > 0:
        sys.stderr.write(f"garden: {e} error(s), {w} warning(s), {i} info (strict mode)\n")
        return 1
    if w > 0 or i > 0:
        sys.stderr.write(f"garden: {e} error(s), {w} warning(s), {i} info\n")
        return 0
    sys.stdout.write("garden: clean\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
