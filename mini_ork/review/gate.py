"""Pre-push review gate — one decision: review, reuse, or skip.

The Layer 3 code review is invoked from two places: ``.githooks/pre-push`` (the
push hook) and ``scripts/mini_ork_worktree.py`` (the merge driver, *before* it
takes the merge lock so a multi-minute review never blocks every other merge).
Both call this module and act on its exit code.

The gate owns four things:

* **Where the state lives.** From a linked worktree, ``git rev-parse
  --git-common-dir`` points at the primary checkout's ``.git``. The review
  database and ``config/local.sh`` live in that primary checkout, not in the
  worktree, so ``primary_root`` resolves it.
* **The review environment.** ``config/local.sh`` is merged into the process
  env with the hook's ``_source_local_env`` rules, so ``MO_REVIEW_*`` knobs
  reach a subprocess that never sourced the file.
* **The patch cache.** Reviews are keyed by ``git patch-id --stable``, so a
  rebase that leaves the patch unchanged reuses the earlier review. The cache
  is a small JSON file under ``$MINI_ORK_HOME/state/``.
* **The tier rule.** If the review cannot run, the secure tier
  (``config/review.yaml`` → ``review.tier: secure``) blocks the push; the
  simple tier warns and proceeds.

Exit codes: ``0`` the push may proceed, ``1`` it is blocked, ``2`` a usage or
unexpected error (the hook maps that to fail-open; see ``.githooks/pre-push``).

The verdict vocabulary at the gate level is ``approve`` / ``warn`` / ``block``
/ ``skipped`` / ``unavailable`` / ``empty``. Only ``approve`` / ``warn`` /
``block`` are ever written to the cache, and never a degraded hybrid review
(one whose LLM lenses all failed, or which recorded no lens run at all) — a
transient lane outage must not pin a patch for the cache TTL. The database
verdicts remain the review runtime's own ``pending`` / ``approve`` / ``warn``
/ ``block`` / ``aborted`` set, untouched by this module.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from mini_ork.context import scoped_environ
from mini_ork.gates.review_policy import load_policy
from mini_ork.pre_push_review import (
    review_forward_to_bug_reports,
    review_run,
    review_show,
    review_verdict_for,
)
from mini_ork.review import verdict as _verdict

__all__ = [
    "CACHE_REL",
    "CACHE_TTL_S",
    "GateResult",
    "primary_root",
    "resolve_home",
    "resolve_db",
    "load_review_env",
    "patch_id",
    "run_gate",
    "main",
]

CACHE_REL = "state/pre-push-review-cache.json"
CACHE_TTL_S = 7 * 86400

_TERMINAL_VERDICTS = ("approve", "warn", "block")
_SECURE_UNAVAILABLE_REASON = (
    "secure tier: the code review could not run, so the push fails closed"
)


@dataclass(frozen=True)
class GateResult:
    """The gate's decision. ``rc`` is what the caller exits with."""

    rc: int            # 0 = push may proceed, 1 = blocked
    verdict: str       # approve|warn|block|skipped|unavailable|empty
    review_id: int | None
    cached: bool
    reason: str


# ── git helpers ────────────────────────────────────────────────────────────


def _git(cwd: str | Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _git_out(cwd: str | Path, *args: str) -> str:
    """stdout of a successful git command, stripped; ``""`` on failure."""
    result = _git(cwd, *args)
    return result.stdout.strip() if result.returncode == 0 else ""


# ── root / home / db resolution ────────────────────────────────────────────


def primary_root(cwd: str | Path) -> Path:
    """The primary checkout, even when ``cwd`` is a linked worktree.

    ``git rev-parse --path-format=absolute --git-common-dir`` is the shared
    ``.git`` directory: ``<primary>/.git`` in a normal checkout and from a
    linked worktree alike. When it is not a ``.git`` directory (a bare repo or
    a submodule layout), fall back to the worktree's own top level.
    """
    common = _git_out(cwd, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if common:
        path = Path(common)
        if path.name == ".git":
            return path.parent
    top = _git_out(cwd, "rev-parse", "--show-toplevel")
    if top:
        return Path(top)
    # Last resort: the caller's directory (a missing repo is handled upstream).
    return Path(cwd)


def resolve_home(cwd: str | Path, env: Mapping[str, str]) -> Path:
    """``$MINI_ORK_HOME`` when set, else ``<primary>/.mini-ork``."""
    explicit = env.get("MINI_ORK_HOME")
    if explicit:
        return Path(explicit)
    return primary_root(cwd) / ".mini-ork"


def resolve_db(cwd: str | Path, env: Mapping[str, str]) -> Path:
    """``$MINI_ORK_DB`` when set, else ``<home>/state.db``."""
    explicit = env.get("MINI_ORK_DB")
    if explicit:
        return Path(explicit)
    return resolve_home(cwd, env) / "state.db"


# ── local.sh env merge ─────────────────────────────────────────────────────

_ASSIGN_RE = re.compile(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$")


def load_review_env(home: Path, env: Mapping[str, str]) -> dict[str, str]:
    """A copy of ``env`` plus the ``MO_REVIEW_*`` keys of ``<home>/config/local.sh``.

    Same rule as the hook's ``_source_local_env``: plain ``KEY=VALUE``,
    optional ``export ``, one layer of matching quotes stripped, blank and
    ``#`` lines ignored. Only ``MO_REVIEW_`` keys are taken, and a key already
    present in ``env`` always wins. No eval, no command substitution.
    """
    merged = dict(env)
    local_env = home / "config" / "local.sh"
    if not local_env.is_file():
        return merged
    try:
        text = local_env.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return merged
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = _ASSIGN_RE.match(line)
        if not match:
            continue
        key, value = match.group(1), match.group(2).strip()
        if not key.startswith("MO_REVIEW_"):
            continue
        if key in env:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        merged[key] = value
    return merged


# ── patch identity ─────────────────────────────────────────────────────────


def patch_id(base: str, head: str, cwd: str | Path) -> str:
    """``git patch-id --stable`` of ``git diff <base> <head>``, or ``""``.

    The patch-id is content-addressed: it is unchanged by a rebase onto a new
    upstream commit, which is what makes the cache useful across the merge
    driver's pre-lock review and the in-lock push hook.
    """
    if not base or not head:
        return ""
    diff = _git(cwd, "diff", "--no-color", "--no-renames", base, head)
    if diff.returncode != 0:
        raise RuntimeError(
            f"git diff {base} {head} failed: {(diff.stderr or '').strip()}"
        )
    if not diff.stdout.strip():
        return ""
    pid = subprocess.run(
        ["git", "-C", str(cwd), "patch-id", "--stable"],
        input=diff.stdout,
        capture_output=True,
        text=True,
        check=False,
    )
    if pid.returncode != 0:
        raise RuntimeError(
            f"git patch-id failed: {(pid.stderr or '').strip()}"
        )
    token = pid.stdout.split()
    return token[0] if token else ""


# ── cache ──────────────────────────────────────────────────────────────────


def _read_cache(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _cache_review_id(entry: object, *, mode: str, target: str, now: int) -> int | None:
    """The cached review id when the entry is reusable, else ``None``."""
    if not isinstance(entry, dict):
        return None
    if entry.get("mode") != mode or entry.get("target") != target:
        return None
    at = entry.get("at")
    review_id = entry.get("review_id")
    if not isinstance(at, int) or not isinstance(review_id, int):
        return None
    age = now - at
    if age < 0 or age > CACHE_TTL_S:
        return None
    return review_id


def _db_verdict(review_id: int, db: Path) -> str:
    return review_verdict_for(review_id, db=str(db)).strip()


def _write_cache(path: Path, cache: dict, pid: str, *, review_id: int,
                 mode: str, target: str, now: int) -> None:
    """Atomically rewrite the cache with this entry and no expired ones."""
    fresh = {
        key: value
        for key, value in cache.items()
        if isinstance(value, dict)
        and isinstance(value.get("at"), int)
        and 0 <= now - value["at"] <= CACHE_TTL_S
    }
    fresh[pid] = {
        "review_id": review_id,
        "mode": mode,
        "target": target,
        "at": now,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir=str(path.parent), prefix=".pre-push-review-cache.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(fresh, handle)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ── db availability ────────────────────────────────────────────────────────


def _db_unavailable_reason(db: Path) -> str:
    """Why ``db`` cannot back a review, or ``""`` when it can."""
    if not db.is_file():
        return f"state.db not found at {db}"
    try:
        con = sqlite3.connect(str(db))
        try:
            con.execute("PRAGMA busy_timeout=5000")
            row = con.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name='pre_push_reviews'"
            ).fetchone()
        finally:
            con.close()
    except sqlite3.Error as exc:
        return f"cannot read state.db at {db}: {exc}"
    if row is None:
        return f"state.db at {db} has no pre_push_reviews table"
    return ""


# ── output ─────────────────────────────────────────────────────────────────


def _emit(result: GateResult, target: str) -> GateResult:
    """Print the mandatory final summary line and return the result."""
    review_id = "-" if result.review_id is None else str(result.review_id)
    cached = "yes" if result.cached else "no"
    print(
        f"pre-push review: verdict={result.verdict} review_id={review_id} "
        f"cached={cached} target={target}"
    )
    return result


def _print_review(review_text: str) -> None:
    """Print a ``review_show`` block with every line gate-prefixed."""
    for line in review_text.splitlines():
        print(f"pre-push review: {line}" if line.strip() else "pre-push review:")


def _lens_runs(review_id: int, db: Path) -> list[tuple]:
    """The recorded lens outcomes for a review, in panel order."""
    con = sqlite3.connect(str(db))
    try:
        con.execute("PRAGMA busy_timeout=5000")
        if con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table'"
            " AND name='review_lens_runs'"
        ).fetchone() is None:
            return []
        return con.execute(
            "SELECT lens, status, issues, elapsed_ms FROM review_lens_runs"
            " WHERE review_id=? ORDER BY id",
            (int(review_id),),
        ).fetchall()
    finally:
        con.close()


def _lens_table_exists(db: Path) -> bool:
    """Whether ``db`` has the ``review_lens_runs`` table.

    ``_lens_runs`` returns ``[]`` both for "no rows" and for "no table"; the
    fresh-review check needs to tell those apart. A read failure is reported as
    "no table", so the caller falls back to the pre-R2d behaviour.
    """
    con = sqlite3.connect(str(db))
    try:
        con.execute("PRAGMA busy_timeout=5000")
        return con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table'"
            " AND name='review_lens_runs'"
        ).fetchone() is not None
    finally:
        con.close()


def _print_lens_runs(review_id: int, db: Path) -> None:
    """One evidence line per lens run; display only, never changes ``rc``."""
    try:
        for lens, status, issues, elapsed_ms in _lens_runs(review_id, db):
            seconds = int(elapsed_ms or 0) // 1000
            print(
                f"pre-push review: lens {lens} {status} "
                f"({int(issues or 0)} issues, {seconds}s)"
            )
    except Exception:  # noqa: BLE001 — evidence must not flip the decision
        return


def _llm_lenses_all_failed(
    review_id: int, db: Path, *, fresh: bool = False
) -> tuple[bool, int]:
    """``(True, n)`` when the review recorded lens runs and none succeeded.

    ``n`` is the number of lens rows. With the default ``fresh=False``, a
    review with no lens rows — or a DB without the ``review_lens_runs``
    table — reports ``(False, 0)``: absence of evidence is not the same as
    evidence that every lens failed.

    ``fresh=True`` means the review *just ran* in hybrid mode. When the table
    exists but holds no row for ``review_id``, a panel that ran recorded
    nothing, so report ``(True, 0)``. A missing table is still ``(False, 0)``.
    """
    try:
        rows = _lens_runs(review_id, db)
        if not rows:
            if fresh and _lens_table_exists(db):
                return True, 0
            return False, 0
    except Exception:  # noqa: BLE001 — evidence must not flip the decision
        return False, 0
    return (not any(status == "ok" for _, status, _, _ in rows)), len(rows)


def _no_llm_lens_warning(n: int) -> None:
    detail = f"all {n} failed" if n >= 1 else "the panel is empty"
    print(
        f"pre-push review: ⚠ no LLM lens ran ({detail}); "
        "this verdict rests on heuristics only"
    )


_BLOCK_HINT = (
    "pre-push review: blocking findings — dismiss one with: "
    "mini-ork review resolve <fingerprint> --as dismissed --reason \"<why>\""
)


def _print_block_findings(review_id: int, db: Path) -> None:
    """The dismiss hint plus each open high/critical linked finding."""
    print(_BLOCK_HINT)
    try:
        con = sqlite3.connect(str(db))
        try:
            con.execute("PRAGMA busy_timeout=5000")
            names = {
                row[0] for row in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                    " AND name IN ('review_findings','review_finding_links')"
                ).fetchall()
            }
            if not {"review_findings", "review_finding_links"} <= names:
                return
            rows = con.execute(
                "SELECT DISTINCT f.fingerprint, f.severity, f.file_path, f.title "
                "  FROM review_finding_links l "
                "  JOIN review_findings f ON f.fingerprint = l.fingerprint "
                " WHERE l.review_id=? AND l.pre_existing=0"
                "   AND f.status='open' AND f.severity IN ('high','critical') "
                " ORDER BY CASE f.severity WHEN 'critical' THEN 4"
                "                          WHEN 'high' THEN 3 ELSE 0 END DESC",
                (int(review_id),),
            ).fetchall()
        finally:
            con.close()
    except Exception:  # noqa: BLE001 — display only, never changes ``rc``
        return
    for fp, severity, file_path, title in rows:
        print(f"  {fp}  {severity}  {file_path or '?'}  {title}")


def _unavailable(reason: str, *, cwd: str | Path, env: Mapping[str, str],
                 target: str) -> GateResult:
    """The tier-aware "the review could not run" path."""
    home = resolve_home(cwd, env)
    try:
        policy = load_policy(home=home, env=env)
        tier = policy.tier
    except Exception as exc:  # noqa: BLE001 — a policy error must fail closed
        text = str(exc) or f"{type(exc).__name__}: {exc}"
        print(f"pre-push review: ✗ review policy error: {text}")
        return _emit(GateResult(1, "unavailable", None, False, text), target)

    if tier == "secure":
        print(f"pre-push review: ✗ {_SECURE_UNAVAILABLE_REASON} ({reason})")
        return _emit(
            GateResult(1, "unavailable", None, False, _SECURE_UNAVAILABLE_REASON),
            target,
        )
    print(
        f"pre-push review: ⚠ the code review did not run ({reason}); "
        "push proceeds (simple tier)"
    )
    return _emit(GateResult(0, "unavailable", None, False, reason), target)


# ── the gate ───────────────────────────────────────────────────────────────


def run_gate(
    *,
    target: str,
    cwd: str | Path,
    env: Mapping[str, str] | None = None,
    head: str = "HEAD",
    base: str = "",
    review_fn=None,
    now: int | None = None,
) -> GateResult:
    """Decide whether the push may proceed; prints its evidence.

    ``review_fn`` lets a test (or another caller) replace
    :func:`mini_ork.pre_push_review.review_run`; it must accept the same
    keyword arguments and return a review id. ``now`` pins the cache clock.
    """
    source_env = dict(os.environ if env is None else env)
    merged = load_review_env(resolve_home(cwd, source_env), source_env)

    if merged.get("MO_REVIEW_SKIP") == "1":
        return _emit(
            GateResult(0, "skipped", None, False, "MO_REVIEW_SKIP=1"), target
        )

    clock = int(time.time()) if now is None else int(now)

    # Resolve the head, then the base. The base the gate resolves is also the
    # base handed to review_run: the patch-id and the reviewed diff must
    # describe the same patch, or a cache hit can reuse the wrong review.
    try:
        head_sha = _git_out(cwd, "rev-parse", head)
        if not head_sha:
            raise RuntimeError(f"cannot resolve {head!r}")
        if base:
            resolved_base = base
        else:
            resolved_base = (
                _git_out(cwd, "merge-base", head_sha, "origin/main")
                or _git_out(cwd, "rev-parse", f"{head_sha}^")
            )
        if not resolved_base:
            raise RuntimeError("cannot determine a review base")
        pid = patch_id(resolved_base, head_sha, cwd)
    except Exception as exc:  # noqa: BLE001 — route failures to the tier rule
        return _unavailable(
            f"{type(exc).__name__}: {exc}", cwd=cwd, env=merged, target=target
        )

    if not pid:
        return _emit(
            GateResult(0, "empty", None, False, "no diff to review"), target
        )

    mode = "hybrid" if merged.get("MO_REVIEW_LLM_LENSES") == "1" else "heuristic"
    db = resolve_db(cwd, merged)
    db_reason = _db_unavailable_reason(db)
    if db_reason:
        return _unavailable(db_reason, cwd=cwd, env=merged, target=target)

    # Cache hit or a fresh review. Any failure here is "unavailable", never a
    # crash: the hook's rc mapping treats a crash as a block, the opposite of
    # the tier intent.
    cache_path = resolve_home(cwd, merged) / CACHE_REL
    lens_state: tuple[bool, int] | None = None
    try:
        cache = _read_cache(cache_path)
        review_id = _cache_review_id(
            cache.get(pid), mode=mode, target=target, now=clock
        )
        cached = False
        # A degraded cache entry — written before this guard existed, or by an
        # older gate — must not pin the patch. Keep it in hand so a re-run that
        # cannot happen leaves the earlier review standing.
        degraded_stale: tuple[int, str] | None = None
        if review_id is not None:
            # Recompute so a dismissal (or a reopen) since the cached review
            # takes effect. A read-only/locked DB falls through to the stored
            # verdict instead of turning a cached approve into "unavailable".
            try:
                _verdict._apply_verdict(review_id, target, str(db))
            except Exception:  # noqa: BLE001
                pass
            verdict = _db_verdict(review_id, db)
            if verdict in _TERMINAL_VERDICTS:
                cached = True
                if mode == "hybrid":
                    all_failed, lens_count = _llm_lenses_all_failed(review_id, db)
                    if all_failed and lens_count >= 1:
                        degraded_stale = (review_id, verdict)
                        review_id = None
                        cached = False
            else:
                review_id = None
        if review_id is None:
            overrides = {
                key: value
                for key, value in merged.items()
                if key.startswith("MO_REVIEW_")
            }
            # The dispatch the review triggers resolves its own home; from a
            # linked worktree that is the primary checkout, not the worktree.
            # Pass it explicitly so a lane preflight (GLM_API_KEY from
            # ``<home>/config/local.sh``) cannot fail in 0 ms.
            if not merged.get("MINI_ORK_HOME"):
                overrides["MINI_ORK_HOME"] = str(resolve_home(cwd, merged))
            try:
                with scoped_environ(overrides):
                    review_id = int(
                        (review_fn or review_run)(
                            head_sha,
                            target,
                            mode=mode,
                            base=resolved_base,
                            cwd=str(cwd),
                            db=str(db),
                        )
                    )
                    verdict = _db_verdict(review_id, db)
            except Exception:  # noqa: BLE001 — see below
                if degraded_stale is None:
                    raise
                # The degraded re-run could not happen: the earlier review is
                # the best evidence there is, and the hybrid check below
                # handles it (warn in the simple tier, block in the secure
                # one). Never surface the exception, and never call this a
                # cache miss in the output.
                review_id, verdict = degraded_stale
                cached = True
            else:
                if mode == "hybrid":
                    # A hybrid review that just ran and recorded no successful
                    # lens is degraded: warn, and do not cache it — a lane
                    # outage must never outlive the retry.
                    lens_state = _llm_lenses_all_failed(
                        review_id, db, fresh=True
                    )
                degraded = bool(lens_state and lens_state[0])
                if verdict in _TERMINAL_VERDICTS and not degraded:
                    _write_cache(
                        cache_path,
                        cache,
                        pid,
                        review_id=review_id,
                        mode=mode,
                        target=target,
                        now=clock,
                    )
                _print_lens_runs(review_id, db)
    except Exception as exc:  # noqa: BLE001
        return _unavailable(
            f"{type(exc).__name__}: {exc}", cwd=cwd, env=merged, target=target
        )

    # A hybrid review whose every LLM lens failed is indistinguishable from a
    # clean one on the verdict alone. Say so, and in the secure tier refuse to
    # let heuristics alone approve the push. Applies to a cache hit too: the
    # cached review's lens rows are what the warning is about.
    if mode == "hybrid":
        if lens_state is None:
            # A cache hit (or a degraded re-run that fell back to its earlier
            # review): the recorded rows are the evidence, so no ``fresh``.
            lens_state = _llm_lenses_all_failed(review_id, db)
        all_failed, lens_count = lens_state
        if all_failed:
            _no_llm_lens_warning(lens_count)
            try:
                tier = load_policy(
                    home=resolve_home(cwd, merged), env=merged
                ).tier
            except Exception as exc:  # noqa: BLE001 — a policy error fails closed
                text = str(exc) or f"{type(exc).__name__}: {exc}"
                print(f"pre-push review: ✗ review policy error: {text}")
                return _emit(
                    GateResult(1, "unavailable", review_id, cached, text), target
                )
            if tier == "secure":
                return _emit(
                    GateResult(
                        1, "unavailable", review_id, cached,
                        "secure tier: no LLM lens ran",
                    ),
                    target,
                )

    if verdict not in _TERMINAL_VERDICTS:
        return _unavailable(
            f"review verdict {verdict!r} is not a terminal verdict",
            cwd=cwd,
            env=merged,
            target=target,
        )

    if verdict == "warn":
        _print_review(review_show(review_id, db=str(db)))
    elif verdict == "block":
        _print_review(review_show(review_id, db=str(db)))
        _print_block_findings(review_id, db)
        if merged.get("MO_REVIEW_AUTO_FIX") == "1":
            forwarded = review_forward_to_bug_reports(review_id, db=str(db))
            print(f"pre-push review: forwarded {forwarded} issue(s) to bug_reports")

    rc = 0 if verdict in ("approve", "warn") else 1
    return _emit(GateResult(rc, verdict, review_id, cached, f"review {verdict}"),
                 target)


# ── CLI ────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    """``python -m mini_ork.pre_push_review gate [--target main|feature]``."""
    parser = argparse.ArgumentParser(
        prog="mini_ork.pre_push_review gate",
        description="Reuse, run, or skip the pre-push code review.",
    )
    parser.add_argument(
        "--target", choices=("main", "feature"), default="main",
        help="the branch the push targets",
    )
    parser.add_argument("--head", default="HEAD", help="commit to review")
    parser.add_argument(
        "--base", default="", help="base commit (default: merge-base with origin/main)"
    )
    parser.add_argument("--cwd", default=".", help="repository working directory")
    args = parser.parse_args(argv)

    try:
        return run_gate(
            target=args.target,
            cwd=args.cwd,
            head=args.head,
            base=args.base,
        ).rc
    except Exception as exc:  # noqa: BLE001 — the hook maps rc 2 to fail-open
        print(
            f"pre-push review: ⚠ reviewer error "
            f"({type(exc).__name__}: {exc}) — fail-open"
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
