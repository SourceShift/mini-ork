"""``mini-ork harness-card`` — which harness, at which release, served a run.

A report-only CLI over ``mini_ork.observability.harness_card``. It reads the
run's ledger rows, its per-node lane records and the audit chain, and prints
the card. Exit 0 when a card was produced (warnings included — they are
findings, not failures), 2 on a usage error or a missing run dir.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from mini_ork.observability import harness_card


def _usage() -> str:
    return (
        "Usage: mini-ork harness-card <run_id> [--json] [--write] [--db <state.db>]\n"
        "\n"
        "Disclose which harness, at which release, served each lane and node of a run:\n"
        "calls, cost, requested vs served lane, family collapse, and the lane's\n"
        "conformance status for the release that served it.\n"
        "\n"
        "Options:\n"
        "  --json    Emit the card as JSON\n"
        "  --write   Also store harness-card.json and harness-card.md in the run dir\n"
        "  --db      State DB (default: $MINI_ORK_DB, else $MINI_ORK_HOME/state.db)\n"
    )


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in {"-h", "--help"}:
        sys.stdout.write(_usage())
        return 0 if args else 2
    as_json = write = False
    db: str | None = None
    run_id = ""
    while args:
        item = args.pop(0)
        if item == "--json":
            as_json = True
        elif item == "--write":
            write = True
        elif item == "--db":
            if not args:
                sys.stderr.write("harness-card: --db requires a path\n")
                return 2
            db = args.pop(0)
        elif item.startswith("-"):
            sys.stderr.write(f"harness-card: unknown option {item}\n")
            return 2
        else:
            run_id = item
    home = Path(os.environ.get("MINI_ORK_HOME") or ".mini-ork")
    db = db or os.environ.get("MINI_ORK_DB") or str(home / "state.db")
    run_dir = home / "runs" / run_id
    if not run_id or not run_dir.is_dir():
        sys.stderr.write(f"harness-card: no run dir {run_dir}\n")
        return 2
    card = (harness_card.write if write else harness_card.build)(str(run_dir), run_id, db=db)
    sys.stdout.write(json.dumps(card, indent=2, sort_keys=True) + "\n" if as_json
                     else harness_card.render_markdown(card))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
