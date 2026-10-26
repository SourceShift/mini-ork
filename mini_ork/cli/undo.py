"""``mini-ork undo <commit|run-id>`` — revert one landed commit, gated.

The repository's policy is fix-forward: ``main`` is never rewritten. An undo
therefore adds a revert commit and lands it through the same
claim → green gate → merge path every other change uses
(:func:`mini_ork.rsi.worktree.revert`). This module adds the refusal surface
around that path:

* the commit must be reachable from ``origin/main``;
* a merge commit needs an explicit ``--mainline N``;
* a commit that was already reverted is refused;
* a publisher run id (``[run <id>]`` in the commit message) or an RSI cycle id
  is mapped to the sha it published.

The module itself performs no destructive git operation: it reads history and
delegates the one write to ``revert()``, so an undo can only ever append.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sqlite3
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Sequence

from mini_ork.sqlite_read import connect_readonly

__all__ = [
    "UndoError",
    "UndoPlan",
    "build_parser",
    "main",
    "plan_undo",
    "record_undo",
    "resolve_ref",
]

#: Every git call this module makes is a read or a local fetch; a hung remote
#: must not hold the command open indefinitely.
_GIT_TIMEOUT_S = 60

_TEST_CMD_PREFIX = "python3.11 -m pytest -q -p no:asyncio "


class UndoError(Exception):
    """A refusal; the message is user-facing."""


@dataclass(frozen=True)
class UndoPlan:
    """Everything the write path needs, resolved before anything is created."""

    ref: str
    sha: str
    subject: str
    mainline: int | None
    files: tuple[str, ...]
    test_cmd: str
    slug: str


# --------------------------------------------------------------------------
# Small git/db helpers
# --------------------------------------------------------------------------


def _git(root: Path, *args: str) -> tuple[int, str, str]:
    """Run ``git -C <root>`` capturing text; never raises on a non-zero rc."""
    proc = subprocess.run(  # noqa: S603 - argv is built here, never interpolated
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        timeout=_GIT_TIMEOUT_S,
        check=False,
    )
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def _tail(text: str, lines: int = 6) -> str:
    rows = [row for row in (text or "").strip().splitlines() if row.strip()]
    return "\n".join(rows[-lines:])


def resolve_root() -> Path:
    """The primary checkout: ``MINI_ORK_ROOT``, else this repository's top."""
    explicit = os.environ.get("MINI_ORK_ROOT")
    if explicit:
        return Path(explicit).resolve()
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise UndoError(f"cannot resolve the primary checkout: {exc}") from exc
    top = proc.stdout.strip()
    if proc.returncode != 0 or not top:
        raise UndoError(
            "cannot resolve the primary checkout: set MINI_ORK_ROOT or run "
            "inside a git repository")
    return Path(top).resolve()


def resolve_db(root: Path) -> Path | None:
    """``MINI_ORK_DB``, else ``<home>/state.db``; ``None`` when it is absent."""
    explicit = os.environ.get("MINI_ORK_DB")
    if explicit:
        return Path(explicit)
    home = os.environ.get("MINI_ORK_HOME")
    base = Path(home) if home else Path(root) / ".mini-ork"
    candidate = base / "state.db"
    return candidate if candidate.is_file() else None


def _rsi_merged_sha(db: Path, ref: str) -> str:
    """The ``merged_sha`` of the RSI cycle ``ref``, or ``""``.

    Read-only on purpose: resolving a ref must never mutate the ledger, and a
    missing table simply means this lookup found nothing.
    """
    try:
        con = connect_readonly(db)
    except (FileNotFoundError, sqlite3.Error):
        return ""
    try:
        row = con.execute(
            "SELECT merged_sha FROM rsi_cycles WHERE cycle_id = ?", (ref,)
        ).fetchone()
    except sqlite3.Error:
        return ""
    finally:
        con.close()
    if not row:
        return ""
    return str(row[0] or "").strip()


# --------------------------------------------------------------------------
# Resolve / plan
# --------------------------------------------------------------------------


def resolve_ref(ref: str, *, root: Path, db: Path | None) -> str:
    """Map a commit-ish, an RSI cycle id, or a publisher run id to one sha."""
    rc, out, _err = _git(root, "rev-parse", "--verify", "--quiet",
                         f"{ref}^{{commit}}")
    sha = out.strip()
    if rc == 0 and sha:
        return sha

    if db is not None:
        merged = _rsi_merged_sha(db, ref)
        if merged:
            return merged

    rc, out, _err = _git(
        root, "log", "origin/main", "--format=%H", "--fixed-strings",
        "--grep", f"[run {ref}]")
    shas = ([row.strip() for row in out.splitlines() if row.strip()]
            if rc == 0 else [])
    if len(shas) == 1:
        return shas[0]
    if len(shas) > 1:
        raise UndoError(
            f"run {ref} published several commits on origin/main: "
            f"{', '.join(shas)}; undo one sha at a time")
    raise UndoError(
        f"no commit found for {ref}: not a commit, an RSI cycle id, or a run "
        f"id with a [run {ref}] commit on origin/main")


def _parents(root: Path, sha: str) -> tuple[str, ...]:
    rc, out, err = _git(root, "rev-list", "--parents", "-n", "1", sha)
    if rc != 0:
        raise UndoError(
            f"cannot read the parents of {sha[:12]}: {_tail(err) or _tail(out)}")
    return tuple(out.split()[1:])


def _is_test_path(path: str) -> bool:
    parts = Path(path).parts
    name = parts[-1] if parts else path
    return bool(parts) and parts[0] == "tests" and name.startswith("test_") \
        and name.endswith(".py")


def plan_undo(ref: str, *, root: Path, db: Path | None,
              mainline: int | None = None,
              test_cmd: str | None = None) -> UndoPlan:
    """Resolve and validate an undo, refusing anything that is not safe to undo."""
    sha = resolve_ref(ref, root=root, db=db)
    sha12 = sha[:12]

    rc, out, err = _git(root, "fetch", "origin", "main")
    if rc != 0:
        raise UndoError(
            "cannot fetch origin/main: "
            + (_tail(err) or _tail(out) or f"git exited {rc}"))

    rc, _out, _err = _git(root, "merge-base", "--is-ancestor", sha, "origin/main")
    if rc != 0:
        raise UndoError(
            f"{sha12} is not on origin/main; undo only reverts landed commits")

    parents = _parents(root, sha)
    if len(parents) > 1:
        if mainline is None:
            raise UndoError(
                f"{sha12} is a merge commit; pass --mainline N "
                f"(1..{len(parents)}) to choose the parent to keep")
        if not 1 <= mainline <= len(parents):
            raise UndoError(
                f"--mainline must be between 1 and {len(parents)} for {sha12}")
    elif mainline is not None:
        raise UndoError("--mainline only applies to merge commits")
    parent_index = mainline or 1
    parent_ref = f"{sha}^{parent_index}"

    rc, out, _err = _git(
        root, "log", "origin/main", "--format=%H", "--fixed-strings",
        "--grep", f"This reverts commit {sha}")
    if rc == 0:
        reverted_by = [row.strip() for row in out.splitlines() if row.strip()]
        if reverted_by:
            raise UndoError(
                f"{sha12} was already reverted by {reverted_by[0][:12]}")

    rc, out, err = _git(root, "diff", "--name-only", "--no-renames",
                        parent_ref, sha)
    if rc != 0:
        raise UndoError(
            f"cannot list the files changed by {sha12}: "
            + (_tail(err) or _tail(out)))
    files = tuple(sorted(row.strip() for row in out.splitlines()
                         if row.strip()))
    if not files:
        raise UndoError(f"{sha12} changes no files")

    if test_cmd is not None:
        gate = test_cmd
    else:
        rc, out, _err = _git(root, "diff", "--diff-filter=A", "--name-only",
                             parent_ref, sha)
        added = ({row.strip() for row in out.splitlines() if row.strip()}
                 if rc == 0 else set())
        candidates = [path for path in files
                      if _is_test_path(path) and path not in added]
        if not candidates:
            raise UndoError(
                f"no existing test file is touched by {sha12}; pass --test-cmd "
                "to gate the revert")
        gate = _TEST_CMD_PREFIX + " ".join(candidates)

    rc, out, _err = _git(root, "log", "-1", "--format=%s", sha)
    subject = out.strip() if rc == 0 else ""
    return UndoPlan(
        ref=ref,
        sha=sha,
        subject=subject,
        mainline=mainline,
        files=files,
        test_cmd=gate,
        slug=f"revert-{sha12}",
    )


# --------------------------------------------------------------------------
# Audit
# --------------------------------------------------------------------------


def record_undo(db: Path | None, plan: UndoPlan, result: Any) -> None:
    """Append one ``audit_log`` row. A broken DB warns once and never raises."""
    if db is None:
        return
    payload = {
        "ref": plan.ref,
        "sha": plan.sha,
        "subject": plan.subject,
        "mainline": plan.mainline,
        "files": list(plan.files),
        "test_cmd": plan.test_cmd,
        "slug": plan.slug,
        "status": result.status,
        "revert_sha": result.sha,
        "detail": result.detail,
    }
    try:
        con = sqlite3.connect(str(db), timeout=5)
        try:
            con.execute(
                "INSERT INTO audit_log (event_type, actor, target, payload) "
                "VALUES (?, ?, ?, ?)",
                ("undo", "human:" + getpass.getuser(),
                 "commit:" + plan.sha, json.dumps(payload, sort_keys=True)))
            con.commit()
        finally:
            con.close()
    except sqlite3.Error as exc:
        sys.stderr.write(f"undo: could not write the audit row: {exc}\n")


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """The ``mini-ork undo`` parser; exposed for the subcommand walkers."""
    parser = argparse.ArgumentParser(
        prog="mini-ork undo",
        description="Revert a landed commit as a new, gated commit. "
                    "Never rewrites main.")
    parser.add_argument(
        "ref", help="a commit sha, an RSI cycle id, or a publisher run id")
    parser.add_argument(
        "--mainline", type=int, default=None, metavar="N",
        help="for a merge commit: the parent to keep (1..parents)")
    parser.add_argument(
        "--test-cmd", default=None, metavar="CMD",
        help="gate command for the revert worktree (defaults to the tests the "
             "commit touched)")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="print the plan; create nothing and record nothing")
    parser.add_argument(
        "--json", action="store_true", help="machine-readable output")
    return parser


def _summary(plan: UndoPlan) -> str:
    lines = [
        f"undo plan for {plan.sha}",
        f"  ref:      {plan.ref}",
        f"  subject:  {plan.subject}",
        f"  mainline: {plan.mainline}",
        f"  slug:     {plan.slug}",
        "  files:",
    ]
    lines.extend(f"    {path}" for path in plan.files)
    lines.append(f"  gate:     {plan.test_cmd}")
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exc:  # ``--help`` (0) and usage errors (2)
        return int(exc.code or 0)

    try:
        root = resolve_root()
        db = resolve_db(root)
        plan = plan_undo(args.ref, root=root, db=db,
                         mainline=args.mainline, test_cmd=args.test_cmd)
    except UndoError as exc:
        sys.stderr.write(f"undo: {exc}\n")
        return 1

    if args.dry_run:
        if args.json:
            sys.stdout.write(json.dumps(asdict(plan), sort_keys=True) + "\n")
        else:
            sys.stdout.write(_summary(plan))
        return 0

    from mini_ork.rsi.cycle import _worktrees_dir
    from mini_ork.rsi.worktree import WorktreeConfig, WorktreeError, revert

    try:
        cfg = WorktreeConfig(
            slug=plan.sha[:12],
            files=plan.files,
            worktrees_dir=_worktrees_dir(root),
            primary=root,
            test_cmd=plan.test_cmd,
        )
        result = revert(cfg, plan.sha, mainline=plan.mainline)
    except (WorktreeError, ValueError, OSError, subprocess.TimeoutExpired) as exc:
        sys.stderr.write(f"undo: {exc}\n")
        return 1

    record_undo(db, plan, result)
    if args.json:
        sys.stdout.write(json.dumps(
            {"plan": asdict(plan), "status": result.status,
             "revert_sha": result.sha, "detail": result.detail},
            sort_keys=True) + "\n")
    else:
        sys.stdout.write(f"undo {plan.sha[:12]}: {result.status}"
                         + (f" {result.sha[:12]}" if result.sha else "") + "\n")
        if result.detail:
            sys.stdout.write(f"  {result.detail}\n")
    return 0 if result.landed else 1


if __name__ == "__main__":
    sys.exit(main())
