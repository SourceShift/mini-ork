"""Merge-time risk tiers: a change is gated by WHAT it touches, not who wrote it.

Every change reaches ``main`` through ``scripts/mini_ork_worktree.py merge``.
This module decides whether that branch is *high tier* — it changes a
high-tier path, or it carries a ``[run <id>]`` commit whose recipe declares
``risk_class: high``/``critical`` — and, if so, requires an approval bound to
the branch's patch (``git patch-id --stable`` of ``origin/main...HEAD``, which
survives a clean rebase).

The rules are read from ``origin/main`` when it has them, so a branch cannot
weaken the check it is being checked by. ``MO_RISK_TIERS_FILE`` is the
tests/CI escape hatch. Anything unexpected in this module fails closed: a
broken rules file or a failing approval lookup refuses the merge.

The module is also a CLI so the merge driver can run it as a subprocess::

    python -m mini_ork.gates.merge_risk check --worktree DIR --slug SLUG

Exit codes: 0 = may merge, 1 = refused (message on stdout), 2 = usage.
"""
from __future__ import annotations

import argparse
import copy
import fnmatch
import os
import re
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

__all__ = [
    "DEFAULT_RULES",
    "RiskVerdict",
    "branch_patch_id",
    "changed_files",
    "check_merge",
    "classify",
    "load_rules",
    "main",
    "run_ids_in_range",
]


# The in-code fallback. ``config/risk-tiers.yaml`` must mirror it — the
# kickoff's "self-application" property depends on this list naming the
# module, the driver, and the rules file itself.
DEFAULT_RULES: dict = {
    "enabled": True,
    "include_frozen_kernel": True,
    "high_risk_classes": ["high", "critical"],
    "high": [
        "config/risk-tiers.yaml",
        "config/kernel-frozen.yaml",
        ".github/workflows/**",
        ".githooks/**",
        "db/migrations/**",
        "install.sh",
        "scripts/install-system-deps.sh",
        "scripts/full_install.py",
        "scripts/mini_ork_worktree.py",
        "mini_ork/cli/install_command.py",
        "mini_ork/dispatch/vault.py",
        "mini_ork/dispatch/vault_write.py",
        "mini_ork/dispatch/secrets.py",
        "mini_ork/cli/vault_cmd.py",
        "mini_ork/remote/secrets_scope.py",
        "config/vault.example.yaml",
        "config/secrets.example.sh",
        ".mini-ork/config/**",
        "mini_ork/gates/merge_risk.py",
        "mini_ork/gates/merge_approval.py",
        "mini_ork/cli/approvals_cmd.py",
        "mini_ork/verify/gaming_scan.py",
    ],
}

_RUN_ID_RE = re.compile(r"\[run ([A-Za-z0-9._:-]+)\]")
_RECIPE_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_RULES_RELPATH = os.path.join("config", "risk-tiers.yaml")


@dataclass(frozen=True)
class RiskVerdict:
    """The tier a branch falls into and why."""

    tier: str                    # "low" | "high"
    reasons: tuple[str, ...]     # e.g. "db/migrations/0085_x.sql matches db/migrations/**"
    patch_id: str
    files: tuple[str, ...]


# ── rules ──────────────────────────────────────────────────────────────────


def _read_text(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _rules_from_text(text: str, source: str) -> dict:
    """Parse one rules document; raise ``ValueError`` naming ``source``."""
    import yaml

    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ValueError(f"risk tiers at {source} is not valid YAML: {exc}") from exc

    if document is None:
        overrides: dict = {}
    elif not isinstance(document, Mapping):
        raise ValueError(
            f"risk tiers at {source} must be a mapping, "
            f"got {type(document).__name__}"
        )
    else:
        raw = document.get("risk_tiers")
        if raw is None:
            overrides = {}
        elif not isinstance(raw, Mapping):
            raise ValueError(
                f"'risk_tiers:' in {source} must be a mapping, "
                f"got {type(raw).__name__}"
            )
        else:
            overrides = dict(raw)

    high = overrides.get("high", DEFAULT_RULES["high"])
    if not isinstance(high, list) or not all(isinstance(p, str) for p in high):
        raise ValueError(f"'high' in {source} must be a list of strings")

    high_classes = overrides.get("high_risk_classes", DEFAULT_RULES["high_risk_classes"])
    if not isinstance(high_classes, list) or not all(
        isinstance(c, str) for c in high_classes
    ):
        raise ValueError(f"'high_risk_classes' in {source} must be a list of strings")

    rules = copy.deepcopy(DEFAULT_RULES)
    rules.update(overrides)
    return rules


def load_rules(wt: str, env: Mapping[str, str]) -> dict:
    """Resolve the rules, highest precedence first.

    1. ``MO_RISK_TIERS_FILE`` (tests/CI) — a path to a YAML file;
    2. ``git show origin/main:config/risk-tiers.yaml`` (the published rules);
    3. ``<wt>/config/risk-tiers.yaml`` (bootstrap, before the file lands);
    4. ``DEFAULT_RULES``.

    A source that exists but does not parse raises — it never silently falls
    through to a weaker one.
    """
    explicit = (env.get("MO_RISK_TIERS_FILE") or "").strip()
    if explicit and os.path.isfile(explicit):
        return _rules_from_text(_read_text(explicit), explicit)

    proc = subprocess.run(
        ["git", "-C", wt, "show", "origin/main:config/risk-tiers.yaml"],
        capture_output=True,
        text=True,
    )
    if proc.returncode == 0:
        return _rules_from_text(proc.stdout, "origin/main:config/risk-tiers.yaml")

    path = os.path.join(wt, _RULES_RELPATH)
    if os.path.isfile(path):
        return _rules_from_text(_read_text(path), path)

    return copy.deepcopy(DEFAULT_RULES)


# ── git primitives ─────────────────────────────────────────────────────────


def changed_files(wt: str) -> list[str]:
    """Paths this branch changes vs ``origin/main`` (its own commits)."""
    out = subprocess.run(
        ["git", "-C", wt, "diff", "--name-only", "--no-renames", "origin/main...HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    if out.returncode != 0:
        raise RuntimeError(f"git diff failed in {wt}: {(out.stderr or '').strip()}")
    return [p for p in out.stdout.splitlines() if p.strip()]


def branch_patch_id(wt: str) -> str:
    """``git patch-id --stable`` of ``origin/main...HEAD``'s diff, or ``""``.

    The id is content-addressed, so it is unchanged by a clean rebase onto a
    newer ``origin/main``. An approval bound to it stays valid across an
    upstream move and is invalidated the moment the branch's own patch changes.
    """
    diff = subprocess.run(
        ["git", "-C", wt, "diff", "--no-color", "--no-renames", "origin/main...HEAD"],
        capture_output=True,
        text=True,
    )
    if diff.returncode != 0:
        raise RuntimeError(
            f"git diff origin/main...HEAD failed in {wt}: "
            f"{(diff.stderr or '').strip()}"
        )
    if not diff.stdout.strip():
        return ""
    pid = subprocess.run(
        ["git", "-C", wt, "patch-id", "--stable"],
        input=diff.stdout,
        capture_output=True,
        text=True,
        check=False,
    )
    if pid.returncode != 0:
        raise RuntimeError(f"git patch-id failed: {(pid.stderr or '').strip()}")
    token = pid.stdout.split()
    return token[0] if token else ""


def run_ids_in_range(wt: str) -> list[str]:
    """``[run <id>]`` ids in this branch's commit messages, de-duplicated.

    Oldest commit first (``--reverse``): the reason list then reads in the
    order the runs happened.
    """
    proc = subprocess.run(
        ["git", "-C", wt, "log", "--reverse", "--format=%B", "origin/main..HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git log failed in {wt}: {(proc.stderr or '').strip()}")
    seen: list[str] = []
    for match in _RUN_ID_RE.finditer(proc.stdout):
        run_id = match.group(1)
        if run_id not in seen:
            seen.append(run_id)
    return seen


# ── risk-class lookup ──────────────────────────────────────────────────────


def _recipe_for_run(run_id: str, db: str) -> str:
    """``task_runs.recipe`` for ``run_id``; a missing DB/table/row is ``""``."""
    if not db:
        return ""
    try:
        uri = Path(db).resolve().as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=2.0)
    except (sqlite3.Error, OSError, ValueError):
        return ""
    try:
        row = conn.execute(
            "SELECT recipe FROM task_runs WHERE id = ?", (run_id,)
        ).fetchone()
    except sqlite3.Error:
        return ""
    finally:
        conn.close()
    if row is None:
        return ""
    recipe = row[0]
    return recipe if isinstance(recipe, str) else ""


def _recipe_risk_class(wt: str, recipe: str) -> str:
    """``risk_class`` from the recipe's task class, or ``""`` when unreadable."""
    if not recipe or not _RECIPE_RE.match(recipe):
        return ""
    rel = f"recipes/{recipe}/task_class.yaml"
    text = ""
    proc = subprocess.run(
        ["git", "-C", wt, "show", f"origin/main:{rel}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode == 0:
        text = proc.stdout
    else:
        try:
            text = _read_text(os.path.join(wt, rel))
        except OSError:
            return ""
    import yaml

    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError:
        return ""
    if not isinstance(document, Mapping):
        return ""
    risk_class = document.get("risk_class")
    return risk_class.strip() if isinstance(risk_class, str) else ""


# ── classification ─────────────────────────────────────────────────────────


def _matches(path: str, pattern: str) -> bool:
    if fnmatch.fnmatch(path, pattern):
        return True
    # ``dir/**`` also covers anything under ``dir/`` even where a platform's
    # fnmatch does not let ``*`` cross the separator.
    return pattern.endswith("/**") and path.startswith(pattern[:-3] + "/")


def classify(
    wt: str,
    *,
    engine_root: str,
    db: str,
    env: Mapping[str, str],
) -> RiskVerdict:
    """Decide a branch's tier from its changed paths and the runs it carries."""
    rules = load_rules(wt, env)
    files = changed_files(wt)
    patch_id = branch_patch_id(wt)

    if not rules.get("enabled", True):
        return RiskVerdict(
            tier="low", reasons=("risk tiers disabled",),
            patch_id=patch_id, files=tuple(files),
        )

    patterns = list(rules.get("high") or [])
    if rules.get("include_frozen_kernel"):
        from mini_ork.vcs.repo_integrity_guard import enforced_frozen_paths

        patterns.extend(enforced_frozen_paths(engine_root))

    reasons: list[str] = []
    for path in files:
        for pattern in patterns:
            if _matches(path, pattern):
                reasons.append(f"{path} matches {pattern}")
                break

    high_classes = rules.get("high_risk_classes") or []
    for run_id in run_ids_in_range(wt):
        recipe = _recipe_for_run(run_id, db)
        if not recipe:
            continue
        risk_class = _recipe_risk_class(wt, recipe)
        if risk_class and risk_class in high_classes:
            reasons.append(
                f"run {run_id} is recipe {recipe} (risk_class {risk_class})"
            )

    tier = "high" if reasons else "low"
    return RiskVerdict(
        tier=tier, reasons=tuple(reasons), patch_id=patch_id, files=tuple(files),
    )


# ── the merge decision ─────────────────────────────────────────────────────


def check_merge(
    wt: str,
    slug: str,
    *,
    engine_root: str,
    db: str,
    env: Mapping[str, str],
) -> tuple[bool, str]:
    """``(may_merge, message)``.

    Low tier merges exactly as before. High tier needs an approval bound to
    the branch's patch id; otherwise a request is enqueued and the refusal
    spells out the command a person must run at a terminal. Any exception is a
    refusal — this check fails closed.
    """
    try:
        verdict = classify(wt, engine_root=engine_root, db=db, env=env)
        if verdict.tier == "low":
            return True, "risk tier: low"

        from mini_ork.gates import merge_approval, review_policy

        run_id = f"wt:{slug}"
        policy = review_policy.load_policy(home=os.path.dirname(os.path.abspath(db)))
        if merge_approval.is_approved(
            run_id, verdict.patch_id, db_path=db, env=env, policy=policy
        ):
            return (
                True,
                f"risk tier: high — approved for patch {verdict.patch_id[:12]}",
            )

        lines = ["risk tier: high"]
        lines.extend(verdict.reasons)
        lines.append("this change needs a person's approval. At a terminal, run:")
        lines.append(f"  mini-ork approvals approve wt:{slug} --head {verdict.patch_id}")
        merge_approval.request(
            run_id,
            "",
            verdict.patch_id,
            branch=f"wt/{slug}",
            base_branch="main",
            db_path=db,
        )
        return False, "\n".join(lines)
    except Exception as exc:  # noqa: BLE001 — fail closed on any error
        return False, f"risk check could not run: {exc}"


# ── CLI ────────────────────────────────────────────────────────────────────


def _engine_root() -> str:
    """The repo root that holds this module: ``<root>/mini_ork/gates/...``."""
    return str(Path(__file__).resolve().parents[2])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="merge-risk",
        description="Decide whether a worktree branch needs merge approval.",
    )
    subs = parser.add_subparsers(dest="command", required=True)
    check = subs.add_parser("check", help="classify a worktree branch")
    check.add_argument("--worktree", required=True, metavar="DIR")
    check.add_argument("--slug", required=True)

    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return 0 if exc.code == 0 else 2

    from mini_ork.review.gate import resolve_db

    db = os.fspath(resolve_db(args.worktree, os.environ))
    ok, message = check_merge(
        args.worktree,
        args.slug,
        engine_root=_engine_root(),
        db=db,
        env=os.environ,
    )
    print(message)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
