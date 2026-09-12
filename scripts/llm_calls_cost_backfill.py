#!/usr/bin/env python3
"""Recompute llm_calls cache-aware cost breakdown for historical rows (F2).

The pre-F2 writers priced EVERY provider's breakdown at Anthropic list rates
and unconditionally subtracted cached+creation from input_tokens. Live-DB
receipts of the damage this repairs: anthropic rows carried $0.0017 total
uncached cost (the subtraction zeroed fresh input the envelope already
excludes cache from), openai/codex rows carried ~$1,160 of phantom breakdown
at Anthropic rates vs $115.73 real, and gateway rows ~$815 fabricated vs
$607.64 real envelope cost.

Recomputes ONLY the three breakdown columns from the stored token columns at
per-provider-family rates/semantics (mini_ork.dispatch.telemetry). Token
columns, cost_usd, and total_tokens are never touched — cost_usd is the
provider-reported billed cost and stays authoritative. Families without a
known rate table (gateway/google) get a zero breakdown: a zero is honest
where a fabricated number is not.

Usage:
    python3 scripts/llm_calls_cost_backfill.py [db] [--apply]
Without --apply the script prints per-family before/after sums and changes
nothing.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from mini_ork.dispatch.models import TokenUsage  # noqa: E402
from mini_ork.dispatch.telemetry import cache_aware_cost, family_of  # noqa: E402


def _family_sums_from_db(con: sqlite3.Connection) -> dict[str, float]:
    sums: dict[str, float] = {}
    q = (
        "SELECT provider, "
        "COALESCE(cost_input_uncached_usd,0)+COALESCE(cost_input_cached_usd,0)"
        "+COALESCE(cost_cache_write_usd,0) "
        "FROM llm_calls"
    )
    for provider, total in con.execute(q):
        sums[family_of(str(provider or ""))] = (
            sums.get(family_of(str(provider or "")), 0.0) + float(total or 0.0)
        )
    return sums


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "db", nargs="?", default=os.environ.get("MINI_ORK_DB")
        or os.path.join(os.getcwd(), ".mini-ork", "state.db")
    )
    ap.add_argument("--apply", action="store_true",
                    help="write the recomputed rows (default: dry-run)")
    args = ap.parse_args()
    if not os.path.isfile(args.db):
        print(f"no db at {args.db}", file=sys.stderr)
        return 1

    con = sqlite3.connect(args.db)
    con.execute("PRAGMA busy_timeout=5000")
    cols = {r[1] for r in con.execute("PRAGMA table_info(llm_calls)").fetchall()}
    if not {"cost_input_uncached_usd", "cost_input_cached_usd",
            "cost_cache_write_usd"} <= cols:
        print("llm_calls lacks the 0024 breakdown columns; nothing to do",
              file=sys.stderr)
        con.close()
        return 1

    before = _family_sums_from_db(con)
    rows = con.execute(
        "SELECT id, provider, input_tokens, output_tokens, cached_input_tokens, "
        "cache_creation_input_tokens FROM llm_calls"
    ).fetchall()
    updates = []
    projected: dict[str, float] = {}
    for rid, provider, in_tok, out_tok, cached, create in rows:
        fam = family_of(str(provider or ""))
        unc, cac, cwr = cache_aware_cost(
            TokenUsage(input_tokens=in_tok or 0, output_tokens=out_tok or 0,
                       cached_input_tokens=cached or 0,
                       cache_creation_tokens=create or 0),
            provider=str(provider or ""),
        )
        updates.append((unc, cac, cwr, rid))
        projected[fam] = projected.get(fam, 0.0) + unc + cac + cwr

    if args.apply:
        con.executemany(
            "UPDATE llm_calls SET cost_input_uncached_usd=?, "
            "cost_input_cached_usd=?, cost_cache_write_usd=? WHERE id=?",
            updates,
        )
        con.commit()

    print(f"{'family':<12} {'before':>14} -> "
          f"{'after' if args.apply else 'projected':>14}")
    for fam in sorted(set(before) | set(projected)):
        print(f"{fam:<12} {before.get(fam, 0.0):>14.2f} -> "
              f"{projected.get(fam, 0.0):>14.2f}")
    print(f"\n{'APPLIED' if args.apply else 'DRY RUN'} — {len(updates)} rows "
          f"recomputed{'' if args.apply else ' (pass --apply to write)'}")
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
