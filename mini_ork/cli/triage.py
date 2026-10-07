"""``mini-ork triage`` — attribute a failed run and (opt-in) queue a self-edit fix.

Reads a run's failed nodes, decides whether the failure is mini-ork's own bug
(:mod:`mini_ork.triage.blame`), and — with ``--promote`` — files a bug report
and promotes it to a ``framework-edit`` epic that the scheduler will dispatch.

Exit codes (mirrors ``certify``'s scriptable contract):
  0  the run is blamed on mini-ork (a fix is warranted)
  2  usage error / no run given
  3  consumer or unknown blame (no fix)

Handler shape matches ``mini_ork.cli.main.register_subcommand`` (``(rest,
root) -> int``) and also runs standalone via ``bin/mini-ork-triage``.
"""
from __future__ import annotations

import argparse
import json
import sys

from mini_ork.triage.failures import latest_failed_run, resolve_db, triage_run

_BLAME_EXIT = {"mini_ork": 0, "consumer": 3, "unknown": 3}


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="mini-ork triage",
        description="Attribute a failed run to mini-ork or the consumer; optionally queue a fix.",
    )
    p.add_argument("--run", metavar="RUN_ID", help="the failed run id to triage")
    p.add_argument("--latest", action="store_true", help="triage the most recent failed run")
    p.add_argument(
        "--promote",
        action="store_true",
        help="file a bug report and promote a framework-edit fix epic (implies writes)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="analyse only, never write (the default unless --promote is given)",
    )
    p.add_argument("--json", action="store_true", dest="as_json", help="emit machine-readable JSON")
    return p


def main(rest=None, root=None) -> int:
    argv = list(sys.argv[1:] if rest is None else rest)
    args = _parser().parse_args(argv)

    run_id = args.run
    if not run_id and args.latest:
        run_id = latest_failed_run(resolve_db(None))
    if not run_id:
        sys.stderr.write("triage: no run given (pass --run <id> or --latest)\n")
        return 2

    write = args.promote and not args.dry_run
    res = triage_run(run_id, root=root, promote=write, dry_run=not write)

    if args.as_json:
        print(json.dumps(res.to_dict(), indent=2, sort_keys=True))
    else:
        print(f"run {res.run_id} ({res.recipe or 'unknown recipe'}) -> blame: {res.blame}")
        print(f"  {res.reason}")
        for e in res.evidence:
            print(f"  - [{e.rule}] {e.detail}")
        if res.bug_id is not None:
            print(f"  bug_reports id={res.bug_id}")
        if res.epic_id:
            print(f"  promoted -> epic {res.epic_id} (recipe={res.fix_recipe})")
            if res.kickoff_path:
                print(f"  kickoff: {res.kickoff_path}")
        elif res.blame == "mini_ork" and not write:
            print("  (dry-run: re-run with --promote to file and queue a fix)")

    return _BLAME_EXIT.get(res.blame, 3)
