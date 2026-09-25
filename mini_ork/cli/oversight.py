"""``mini-ork oversight`` — enqueue/resolve and rule-by-fatigue report over mo_inbox_gates.

The operator surface for the oversight channel: the write half
(``mini_ork.gates.oversight_inbox``) and the measurement half
(``mini_ork.learning.oversight_calibration``).

It **always exits 0**. It reports state and records resolutions; it never gates
a run, never resolves anything on its own, and never wires the executor to the
inbox. Bad input is written to stderr and the usage to stdout — still exit 0 —
so a caller can invoke it from a dashboard or a shell prompt without a nonzero
status poisoning the pipeline.

Actions (the first one seen wins):

  ``--calibrate [--since DAYS] [--json]``   the default action
  ``--pending [--json]``                    the pending list
  ``--enqueue GATE_ID --feature NAME [--phase P] [--context JSON]``
  ``--resolve ID --status approved|rejected [--note TEXT]``
"""
from __future__ import annotations

import json
import sys
import time

from mini_ork.gates import oversight_inbox
from mini_ork.learning.oversight_calibration import calibrate, summarize

__all__ = ["main"]


def _usage() -> str:
    return (
        "Usage: mini-ork oversight [--calibrate [--since DAYS] [--json]]\n"
        "                          [--pending [--json]]\n"
        "                          [--enqueue GATE_ID --feature NAME "
        "[--phase P] [--context JSON]]\n"
        "                          [--resolve ID --status approved|rejected "
        "[--note TEXT]]\n"
        "\n"
        "Report rule-by-fatigue, abandonment, and stale backlog over the\n"
        "mo_inbox_gates oversight channel, and record resolutions into it.\n"
        "This command always exits 0; it never gates a run.\n"
        "\n"
        "Options:\n"
        "  --calibrate     Per-gate calibration report (the default action)\n"
        "  --since DAYS    Restrict --calibrate to items enqueued within DAYS\n"
        "  --pending       List the still-pending items\n"
        "  --enqueue       Insert a pending item (prints the new inbox_id)\n"
        "  --feature NAME  The feature an --enqueue item belongs to\n"
        "  --phase P       The phase an --enqueue item sits at\n"
        "  --context JSON  A JSON object carried with an --enqueue item\n"
        "  --resolve ID    Close item ID (prints true/false)\n"
        "  --status S      approved or rejected, for --resolve\n"
        "  --note TEXT     The review note written by --resolve\n"
        "  --json          Emit JSON instead of the human rendering\n"
        "  --help          This message\n"
    )


def _parse(argv: list[str]) -> dict:
    """Tolerant flag parser. Raises ``ValueError`` on an unknown flag or a
    value-taking flag with no value."""
    opts: dict = {"action": None, "json": False, "help": False}
    i = 0
    while i < len(argv):
        a = argv[i]

        def value(i: int = i) -> str:
            if i + 1 >= len(argv):
                raise ValueError(f"flag {argv[i]!r} requires a value")
            return argv[i + 1]

        if a in ("--help", "-h"):
            opts["help"] = True
            i += 1
        elif a == "--json":
            opts["json"] = True
            i += 1
        elif a == "--calibrate":
            opts["action"] = "calibrate"
            i += 1
        elif a == "--pending":
            opts["action"] = "pending"
            i += 1
        elif a == "--enqueue":
            opts["action"] = "enqueue"
            opts["gate_id"] = value()
            i += 2
        elif a == "--feature":
            opts["feature"] = value()
            i += 2
        elif a == "--phase":
            opts["phase"] = value()
            i += 2
        elif a == "--context":
            opts["context"] = value()
            i += 2
        elif a == "--resolve":
            opts["action"] = "resolve"
            opts["inbox_id"] = value()
            i += 2
        elif a == "--status":
            opts["status"] = value()
            i += 2
        elif a == "--note":
            opts["note"] = value()
            i += 2
        elif a == "--since":
            opts["since"] = value()
            i += 2
        else:
            raise ValueError(f"unknown flag: {a}")
    return opts


def main(argv=None, *, stdout=None, stderr=None) -> int:
    out = stdout if stdout is not None else sys.stdout
    err = stderr if stderr is not None else sys.stderr
    if argv is None:
        argv = sys.argv[1:]

    try:
        opts = _parse(list(argv))
    except ValueError as exc:
        err.write(f"{exc}\n")
        out.write(_usage())
        return 0

    if opts["help"]:
        out.write(_usage())
        return 0

    action = opts["action"] or "calibrate"

    try:
        if action == "calibrate":
            since = None
            if "since" in opts:
                since = time.time() - float(opts["since"]) * 86400.0
            report = calibrate(since=since)
            if opts["json"]:
                out.write(json.dumps(report, sort_keys=True) + "\n")
            else:
                out.write(summarize(report) + "\n")
            return 0

        if action == "pending":
            rows = oversight_inbox.pending()
            if opts["json"]:
                out.write(json.dumps(rows, sort_keys=True) + "\n")
            elif not rows:
                out.write("no pending oversight items\n")
            else:
                for r in rows:
                    out.write(
                        f"{r['inbox_id']}  {r['gate_id']}  {r['feature']}  "
                        f"{r['phase'] or '-'}\n"
                    )
            return 0

        if action == "enqueue":
            context = json.loads(opts["context"]) if "context" in opts else {}
            inbox_id = oversight_inbox.enqueue(
                opts.get("gate_id", ""),
                opts.get("feature", ""),
                phase=opts.get("phase", ""),
                context=context,
            )
            out.write(f"{inbox_id}\n")
            return 0

        if action == "resolve":
            ok = oversight_inbox.resolve(
                int(opts["inbox_id"]),
                opts.get("status", ""),
                review_note=opts.get("note", ""),
            )
            out.write(f"{ok}\n")
            return 0
    except Exception as exc:  # noqa: BLE001 — a report must never poison a pipeline
        err.write(f"{type(exc).__name__}: {exc}\n")
        return 0

    err.write(f"unknown action: {action}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
