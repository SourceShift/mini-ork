"""``mini-ork repair`` — the manual control surface over the auto-repair loop.

    mini-ork repair <run_id> [--dry-run] [--json]
    mini-ork repair --sweep [--since-days N=4] [--dry-run] [--json] [--include-experiments]

The single-run form decides and (unless ``--dry-run``) applies the repair for
one run. ``--sweep`` walks every failed/rolled_back run in the window, printing
one decision per run; with ``--dry-run`` nothing is written and no ``recover`` is
spawned.

Experiments are skipped by default (ids starting ``vt<digit>``, recipes ending
``__probe`` or named ``refactor-audit``), as are runs whose retry gate was
resolved as ``superseded``/``abandoned`` — those are settled, not fixable.

Exit codes: 0 on success, 2 on a usage error. A per-run failure during a sweep
is reported and does not abort the sweep.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from typing import Any

from mini_ork.recovery import auto_repair

_TERMINAL = ("failed", "rolled_back")


def _usage() -> str:
    return (
        "Usage: mini-ork repair <run_id> [--dry-run] [--json]\n"
        "       mini-ork repair --sweep [--since-days N] [--dry-run] [--json]\n"
        "                            [--include-experiments]\n"
        "\n"
        "Decide (and, unless --dry-run, apply) the auto-repair for a failed run.\n"
        "\n"
        "Options:\n"
        "  --sweep                Every failed/rolled_back run in the window\n"
        "  --since-days N         Sweep window in days (default 4)\n"
        "  --dry-run              Decide only; write nothing, spawn nothing\n"
        "  --json                 Emit JSON as the only stdout\n"
        "  --include-experiments  Do not skip vt*/__probe/refactor-audit runs\n"
        "  --home PATH            mini-ork home (default $MINI_ORK_HOME or .mini-ork)\n"
    )


def _resolve_home(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit)
    env_home = os.environ.get("MINI_ORK_HOME")
    if env_home:
        return Path(env_home)
    return Path(os.getcwd()) / ".mini-ork"


def _parse(argv: list[str]) -> tuple[dict[str, Any], str | None]:
    """Parse argv into ``(opts, error)``. ``error`` is a usage message or ``None``."""
    opts: dict[str, Any] = {
        "sweep": False, "dry_run": False, "json": False,
        "since_days": 4, "include_experiments": False,
        "home": None, "run_id": None,
    }
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--sweep":
            opts["sweep"] = True
        elif arg == "--dry-run":
            opts["dry_run"] = True
        elif arg == "--json":
            opts["json"] = True
        elif arg == "--include-experiments":
            opts["include_experiments"] = True
        elif arg in ("--help", "-h"):
            return opts, "__help__"
        elif arg == "--since-days":
            i += 1
            if i >= len(argv):
                return opts, "--since-days requires a value"
            try:
                opts["since_days"] = int(argv[i])
            except ValueError:
                return opts, f"--since-days must be an integer, got {argv[i]!r}"
        elif arg.startswith("--since-days="):
            try:
                opts["since_days"] = int(arg.split("=", 1)[1])
            except ValueError:
                return opts, f"--since-days must be an integer, got {arg!r}"
        elif arg == "--home":
            i += 1
            if i >= len(argv):
                return opts, "--home requires a value"
            opts["home"] = argv[i]
        elif arg.startswith("--home="):
            opts["home"] = arg.split("=", 1)[1]
        elif arg.startswith("-"):
            return opts, f"unknown option: {arg}"
        else:
            opts["run_id"] = arg
        i += 1
    return opts, None


def _is_experiment(run_id: str, recipe: str) -> bool:
    rid = run_id or ""
    if len(rid) > 2 and rid[:2] == "vt" and rid[2].isdigit():
        return True
    rec = recipe or ""
    if rec.endswith("__probe") or rec == "refactor-audit":
        return True
    return False


def _gate_superseded(run_dir: Path) -> bool:
    """True when ``retry-gate.json`` was resolved as superseded/abandoned."""
    path = run_dir / "retry-gate.json"
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return False
    if not isinstance(body, dict):
        return False
    return bool(body.get("superseded") or body.get("abandoned"))


def _created_at_epoch(raw: Any) -> int:
    """Epoch seconds for a ``task_runs.created_at`` stamp (0 when unreadable).

    ``fromisoformat`` keeps an ISO string's own offset, so only a *naive* stamp
    is UTC: forcing ``tzinfo`` would discard a real offset (``+02:00``) and
    shift the string by that offset when the window is applied.
    """
    if isinstance(raw, (int, float)):
        return int(raw)
    if isinstance(raw, str):
        text = raw.strip()
        if text.isdigit():
            return int(text)
        try:
            from datetime import datetime, timezone
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return 0
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp())
    return 0


def _failed_runs(home: Path, since_days: int, *,
                 include_experiments: bool) -> list[dict[str, Any]]:
    """Failed/rolled_back runs inside the window, experiments/gated runs dropped.

    A *withheld* publish is a failed run: ``publisher.py`` forces
    ``status='failed'`` on ``abstain``. A run that a later status rewrite left
    ``published`` while its level report still abstains is that same withheld
    run, so it is swept too (``auto_repair._is_withheld``) — the loop exists to
    revive exactly this class. Window is applied in SQL for published rows (a
    long-lived home has far more published than failed runs).
    """
    from mini_ork.web.db import db_for
    cutoff = int(time.time()) - since_days * 86400
    try:
        db = db_for(home)
        if not db.has_table("task_runs"):
            return []
        rows = db.rows(
            "SELECT id, recipe, status, created_at FROM task_runs "
            "WHERE status IN ('failed','rolled_back') "
            "   OR (status = 'published' AND created_at >= ?) "
            "ORDER BY updated_at DESC",
            (cutoff,),
        )
    except Exception:  # noqa: BLE001 — a missing DB is an empty sweep
        return []
    out: list[dict[str, Any]] = []
    for row in rows:
        run_id = str(row.get("id") or "")
        if not run_id:
            continue
        created = _created_at_epoch(row.get("created_at"))
        if created and created < cutoff:
            continue
        recipe = str(row.get("recipe") or "")
        if not include_experiments and _is_experiment(run_id, recipe):
            continue
        run_dir = home / "runs" / run_id
        if str(row.get("status") or "") == "published" and not auto_repair._is_withheld(run_dir):
            continue
        if _gate_superseded(run_dir):
            continue
        out.append({"id": run_id, "recipe": recipe, "status": str(row.get("status") or "")})
    return out


# Marks the memo wrapper so a nested scope can tell the swap is already in
# effect instead of wrapping the wrapper.
_CATALOG_MEMO_MARK = "_mo_sweep_catalog_memo"
_CATALOG_LOCK = threading.RLock()


@contextmanager
def _cached_catalog():
    """Memoise ``recipes_catalog.list_recipes`` for the sweep's duration.

    A full sweep decides for ~40 runs; ``find_recipe`` re-parses *every*
    recipe's YAML on each lookup, so a sweep re-reads the catalog ~200 times and
    blows the host's 30 s CPU ceiling. The catalog is immutable for the sweep's
    duration, so parse it once (keyed by home) and restore the original after.

    ``find_recipe`` resolves ``list_recipes`` through the module global, so the
    swap is process-wide. It is therefore held under a re-entrant lock and is
    idempotent: a second sweep — or a web server that imported this module while
    a sweep runs — finds the memo already installed, joins it, and never leaves
    a wrapper on top of a wrapper. Only a *naive* read of the global during the
    window can see it at all, and the memo is an ``lru_cache`` around the
    original, so such a reader still gets correct entries.
    """
    from mini_ork import recipes_catalog
    with _CATALOG_LOCK:
        current = recipes_catalog.list_recipes
        if getattr(current, _CATALOG_MEMO_MARK, False):
            yield  # an enclosing scope already memoised the catalog
            return
        memo = lru_cache(maxsize=None)(current)
        setattr(memo, _CATALOG_MEMO_MARK, True)
        recipes_catalog.list_recipes = memo
        try:
            yield
        finally:
            recipes_catalog.list_recipes = current


def _decide_one(home: Path, run_id: str, *, dry_run: bool) -> dict[str, Any]:
    try:
        decision = auto_repair.decide(home, run_id)
    except Exception as exc:  # noqa: BLE001 — one bad run never aborts a sweep
        return {"id": run_id, "action": None, "from_node": None, "lanes": {},
                "reason": "", "signature": "", "error": f"decide failed: {exc}"}
    result = {
        "id": run_id,
        "action": decision.get("action"),
        "from_node": decision.get("from_node"),
        "lanes": decision.get("lanes") or {},
        "reason": decision.get("reason") or "",
        "signature": decision.get("signature") or "",
    }
    if not dry_run and decision.get("action") not in (None, "none"):
        try:
            applied = auto_repair.apply(home, run_id, decision)
            if isinstance(applied, dict):
                result["applied"] = {
                    k: applied.get(k) for k in ("action", "attempt", "pid", "log", "gate")
                    if applied.get(k) is not None
                }
        except Exception as exc:  # noqa: BLE001 — one bad run never aborts a sweep
            result["error"] = str(exc)
    return result


def _print_text(rows: list[dict[str, Any]]) -> None:
    for row in rows:
        node = row.get("from_node") or "-"
        sys.stdout.write(
            f"{row['id']}  {row.get('action')}  {node}  {row.get('reason') or ''}\n"
        )


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    opts, error = _parse(argv)
    if error == "__help__":
        sys.stdout.write(_usage())
        return 0
    if error:
        sys.stderr.write(f"repair: {error}\n")
        sys.stdout.write(_usage())
        return 2

    home = _resolve_home(opts["home"])
    dry_run = bool(opts["dry_run"])

    if opts["sweep"]:
        runs = _failed_runs(home, int(opts["since_days"]),
                            include_experiments=bool(opts["include_experiments"]))
        with _cached_catalog():
            rows = [_decide_one(home, r["id"], dry_run=dry_run) for r in runs]
        if opts["json"]:
            sys.stdout.write(json.dumps(rows, indent=2, sort_keys=True) + "\n")
        else:
            _print_text(rows)
        return 0

    run_id = opts.get("run_id")
    if not run_id:
        sys.stderr.write("repair: a <run_id> or --sweep is required\n")
        sys.stdout.write(_usage())
        return 2

    row = _decide_one(home, run_id, dry_run=dry_run)
    if opts["json"]:
        sys.stdout.write(json.dumps(row, indent=2, sort_keys=True) + "\n")
    else:
        _print_text([row])
    return 0


if __name__ == "__main__":
    sys.exit(main())
