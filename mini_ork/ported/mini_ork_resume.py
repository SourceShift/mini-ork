"""Python port of ``bin/mini-ork-resume``.

Companion to ``mini_ork.ported.cost_pause`` (Epic E4). The dispatcher writes
``.cost-pause`` sentinel files when cumulative run cost crosses
``MO_PAUSE_EVERY_USD``. This entrypoint removes the sentinel and records
the approval to ``<run_dir>/.cost-pause-approvals.jsonl`` so the resume
action is auditable.

The bash script at ``bin/mini-ork-resume`` is the live reference; this
module is a pure-Python peer under ``mini_ork/ported/`` so it can be
called in-process by tests and other modules without forking a shell.
Parity is enforced by ``tests/unit/test_mini_ork_resume_py.py`` which
runs both implementations through ``subprocess`` and asserts byte-equal
output (rc, stdout, stderr, jsonl row shape).
"""
from __future__ import annotations

import datetime
import os
import sys

_USAGE = """\
Usage: mini-ork resume <run_id>

Clear the cost-pause sentinel for <run_id> + record an audit row.

Arguments:
  run_id           Run identifier (e.g. run-1781000000-12345)

Options:
  --help, -h       Show this help

After resume, the next dispatch step against this run will be
allowed to proceed. The cumulative spend counter is preserved -
the NEXT pause fires when cost crosses the NEXT multiple of
$MO_PAUSE_EVERY_USD (default $25), not immediately.
"""


def _usage() -> str:
    return _USAGE


def _resolve_run_dir(run_id: str, home: str | None = None) -> str:
    if home is None:
        home = os.environ.get("MINI_ORK_HOME")
    if not home:
        home = os.path.join(os.getcwd(), ".mini-ork")
    return os.path.join(home, "runs", run_id)


def _now_iso(now: datetime.datetime | None = None) -> str:
    if now is None:
        now = datetime.datetime.now(datetime.timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%SZ")


def _format_audit_row(
    run_id: str, approver: str, now_iso: str, sentinel_payload: str
) -> str:
    payload = sentinel_payload
    if payload.endswith("\n"):
        payload = payload[:-1]
    return (
        '{"resumed_at":"%s","approver":"%s","run_id":"%s","sentinel_payload":%s}\n'
        % (now_iso, approver, run_id, payload)
    )


def resume(
    run_id: str,
    *,
    home: str | None = None,
    approver: str | None = None,
    now: datetime.datetime | None = None,
) -> tuple[int, str, str | None, bool]:
    """Mirror ``bin/mini-ork-resume`` for a single run_id.

    Returns ``(rc, stdout_msg, audit_path, sentinel_removed)``.

    rc semantics match bash exactly:
      * 1 — ``run_dir`` does not exist (stderr is written by this fn).
      * 0 — no sentinel present (stderr warning is written by this fn,
             ``audit_path`` is ``None``, ``sentinel_removed`` is ``False``).
      * 0 — success (no stderr; ``audit_path`` is the jsonl path;
             ``sentinel_removed`` is ``True``; ``stdout_msg`` is the
             "[mini-ork-resume] resumed ..." line).

    Stderr is written in-process to mirror bash's behavior so the parity
    test can compare it against bash's captured stderr.
    """
    run_dir = _resolve_run_dir(run_id, home=home)
    if not os.path.isdir(run_dir):
        sys.stderr.write(
            f"[mini-ork-resume] run dir not found: {run_dir}\n"
        )
        return 1, "", None, False

    sentinel = os.path.join(run_dir, ".cost-pause")
    if not os.path.isfile(sentinel):
        sys.stderr.write(
            f"[mini-ork-resume] no cost-pause sentinel for {run_id} "
            "(already running?)\n"
        )
        return 0, "", None, False

    approvals = os.path.join(run_dir, ".cost-pause-approvals.jsonl")
    if approver is None:
        approver = os.environ.get("USER") or "unknown"
    ts = _now_iso(now)
    with open(sentinel, "r") as fh:
        sentinel_body = fh.read()
    row = _format_audit_row(run_id, approver, ts, sentinel_body)
    with open(approvals, "a") as fh:
        fh.write(row)
    os.remove(sentinel)

    return (
        0,
        f"[mini-ork-resume] resumed {run_id} (approver={approver}, "
        f"audit={approvals})\n",
        approvals,
        True,
    )


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    first = argv[0] if argv else ""
    if first == "" or first == "--help" or first == "-h":
        sys.stdout.write(_usage())
        return 2 if first == "" else 0
    rc, stdout_msg, _audit, _removed = resume(first)
    if stdout_msg:
        sys.stdout.write(stdout_msg)
    return rc


if __name__ == "__main__":
    sys.exit(main())