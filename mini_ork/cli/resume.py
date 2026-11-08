"""Python port of ``bin/mini-ork-resume``.

Companion to ``mini_ork.dispatch.cost_pause`` (Epic E4). The dispatcher writes
``.cost-pause`` sentinel files when cumulative run cost crosses
``MO_PAUSE_EVERY_USD``. This entrypoint removes the sentinel and records
the approval to ``<run_dir>/.cost-pause-approvals.jsonl`` so the resume
action is auditable.

The bash script at ``bin/mini-ork-resume`` is the live reference; this
module is a pure-Python peer under ``mini_ork/cli/`` so it can be
called in-process by tests and other modules without forking a shell.
Parity is enforced by ``tests/unit/test_mini_ork_resume_py.py`` which
runs both implementations through ``subprocess`` and asserts byte-equal
output (rc, stdout, stderr, jsonl row shape).
"""
from __future__ import annotations

import datetime
import json
import os
import sys
import time
from pathlib import Path

_USAGE = """\
Usage: mini-ork resume <run_id> [--no-restart] [--answer ask-N=<text>]...

Clear the cost-pause sentinel for <run_id> + record an audit row, resume a
run parked by its per-run budget (.budget-exhausted) once it fits again, or
record answers to the run's open profile questions and continue the run.

Arguments:
  run_id           Run identifier (e.g. run-1781000000-12345)

Options:
  --help, -h       Show this help
  --no-restart     Clear the sentinel + audit only; do not restart the run
  --answer ask-N=text   Answer the run's open question ask-N (repeatable)

After resume, the run is restarted with
``mini-ork recover <run_id> --strategy resume`` (the way approvals
resume a review-parked run); pass --no-restart to only clear the
sentinel and audit the approval. The cumulative spend counter is
preserved - the NEXT pause fires when cost crosses the NEXT multiple of
$MO_PAUSE_EVERY_USD (default $25), not immediately.

When --answer is given, the run's asks/ask-N.json files are updated; once
every question is answered the answers are applied to the run profile and
the same run continues (same run_id, run dir, recipe and kickoff).
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


def _restart_run(run_id: str, run_dir: str) -> str:
    """Spawn ``recover --strategy resume`` detached; return the message tail.

    The run never stopped (a sentinel or a marker was written but the dispatch
    path is what refuses), so clearing the file alone would leave it dead.
    ``_spawn_recover`` raises only when the launcher is unusable; the file is
    already gone, so degrade to the manual command instead of tracing back.
    Imported here, not at module scope: the clear-and-audit path (and the
    parity tests that drive it) must not pay for the approvals/ledger import.
    """
    from mini_ork.cli import approvals_cmd  # noqa: PLC0415

    log_path = Path(run_dir) / "cost-resume.log"
    try:
        _pid, spawned_log = approvals_cmd._spawn_recover(
            Path(run_dir).parent.parent,
            run_id,
            ["--strategy", "resume"],
            log_path=log_path,
        )
    except (approvals_cmd._LauncherMissingError, OSError):
        return f"\nresume with: mini-ork recover {run_id} --strategy resume"
    return f"; restarting (log: {spawned_log})"


def _budget_db(run_dir: str) -> str:
    """The ledger the dispatch gate reads: ``MINI_ORK_DB``, else ``<home>/state.db``."""
    return (os.environ.get("MINI_ORK_DB")
            or os.path.join(os.path.dirname(os.path.dirname(run_dir)), "state.db"))


def _resume_budget_park(
    run_id: str,
    run_dir: str,
    *,
    approver: str | None,
    now: datetime.datetime | None,
    restart: bool,
) -> tuple[int, str, str | None, bool] | None:
    """Resume a run parked by its per-run ceiling; ``None`` when no such park.

    The dispatch gate refuses the next node and prints ``mini-ork resume
    <run>`` as the unblock, so this is where that promise is kept: re-ask the
    budget policy, and only clear ``.budget-exhausted`` when the run now fits.

    Fail closed, in two directions:

    * the run is still over its ceiling → refuse (rc 1) and name the number the
      ceiling has to exceed;
    * the operator removed the declared ceiling instead of raising it → refuse
      too. A parked run must not become unlimited because a variable was
      unset; the message says so and names the spend it has to cover.
    """
    from mini_ork.cost import budget  # noqa: PLC0415

    marker = budget.read_marker(run_dir)
    if marker is None:
        return None
    db = _budget_db(run_dir)
    verdict = budget.verdict(db, run_id, run_dir)
    if verdict.exhausted or verdict.budget is None:
        ceiling = ("no per-run budget is declared for this run"
                   if verdict.budget is None else verdict.detail)
        sys.stderr.write(
            f"[mini-ork-resume] {run_id} is still parked: {ceiling}. "
            f"Set {budget.BUDGET_ENV} (or run_profile.json budget_usd) above "
            f"${verdict.spent:.2f}, then resume again — removing the ceiling "
            f"does not make a parked run unlimited.\n"
        )
        return 1, "", None, False

    approvals = os.path.join(run_dir, ".cost-pause-approvals.jsonl")
    if approver is None:
        approver = os.environ.get("USER") or "unknown"
    ts = _now_iso(now)
    row = _format_audit_row(run_id, approver, ts,
                            json.dumps({"marker": budget.MARKER_NAME,
                                        "payload": marker}))
    with open(approvals, "a") as fh:
        fh.write(row)
    budget.record(run_dir, verdict)  # not exhausted → removes the marker

    stdout_msg = (f"[mini-ork-resume] resumed {run_id} (approver={approver}, "
                  f"{verdict.detail}, audit={approvals})")
    if restart:
        stdout_msg += _restart_run(run_id, run_dir)
    stdout_msg += "\n"
    return 0, stdout_msg, approvals, True


def resume(
    run_id: str,
    *,
    home: str | None = None,
    approver: str | None = None,
    now: datetime.datetime | None = None,
    restart: bool = True,
) -> tuple[int, str, str | None, bool]:
    """Mirror ``bin/mini-ork-resume`` for a single run_id.

    Returns ``(rc, stdout_msg, audit_path, sentinel_removed)``.

    rc semantics match bash exactly:
      * 1 — ``run_dir`` does not exist (stderr is written by this fn).
      * 1 — the run is parked by its per-run budget and still does not fit
             (see :func:`_resume_budget_park`); nothing is cleared.
      * 0 — no sentinel and no budget marker present (stderr warning is
             written by this fn, ``audit_path`` is ``None``,
             ``sentinel_removed`` is ``False``).
      * 0 — success (no stderr; ``audit_path`` is the jsonl path;
             ``sentinel_removed`` is ``True``; ``stdout_msg`` is the
             "[mini-ork-resume] resumed ..." line).

    After the sentinel is cleared and the audit row written, the run itself
    is restarted with ``mini-ork recover <run_id> --strategy resume`` via
    ``approvals_cmd._spawn_recover`` — the same hand-off ``mini-ork approvals``
    uses for a review-parked run. ``restart=False`` (CLI ``--no-restart``)
    keeps the old clear-and-audit-only behaviour. A launcher that cannot be
    spawned degrades to a manual-resume hint in ``stdout_msg`` rather than
    raising: the sentinel is already gone, so the operator must be told how
    to drive the run forward.

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
        # A run parked by its per-run ceiling has no cost-pause sentinel — the
        # gate refuses the *next* node and leaves ``.budget-exhausted``. The
        # dispatch gate prints ``mini-ork resume <run>`` as the unblock, so this
        # entrypoint must be the one that honours it; falling through to the
        # cost-pause warning would tell the operator "already running?" and
        # leave the run dead.
        parked = _resume_budget_park(run_id, run_dir, approver=approver,
                                     now=now, restart=restart)
        if parked is not None:
            return parked
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

    stdout_msg = (
        f"[mini-ork-resume] resumed {run_id} (approver={approver}, "
        f"audit={approvals})"
    )
    if restart:
        stdout_msg += _restart_run(run_id, run_dir)
    stdout_msg += "\n"

    return 0, stdout_msg, approvals, True


def _list_ask_files(run_dir: str) -> list[str]:
    """ASK file paths in 1-based ask-N order."""
    asks_dir = os.path.join(run_dir, "asks")
    if not os.path.isdir(asks_dir):
        return []
    names = sorted(
        n for n in os.listdir(asks_dir)
        if n.startswith("ask-") and n.endswith(".json")
    )
    return [os.path.join(asks_dir, n) for n in names]


def _parse_answer(arg: str) -> tuple[str, str] | None:
    """``ask-1=text`` → ``(ask_id, text)``; ``None`` when malformed."""
    ask_id, sep, text = arg.partition("=")
    if not sep or not ask_id.strip():
        return None
    return ask_id.strip(), text


def _lifecycle_entry(argv: list[str], root: str) -> int:
    """Re-enter the lifecycle in-process (monkeypatch seam for tests)."""
    from mini_ork.cli import main as cli_main

    return cli_main._run_lifecycle(argv, root)


def _reenter_lifecycle(run_id: str, *, home: str | None = None,
                       root: str | None = None) -> int:
    """Continue the same run after its profile questions were answered.

    Reads the recipe + kickoff back out of ``run_profile.json`` (written at
    classify time), pins ``MINI_ORK_RUN_ID`` so the lifecycle reuses the existing
    ``task_runs`` row and run dir (classify upserts; ``os.makedirs`` is
    idempotent), and re-enters the lifecycle in-process.
    """
    run_dir = _resolve_run_dir(run_id, home=home)
    profile_path = os.path.join(run_dir, "run_profile.json")
    try:
        profile = json.load(open(profile_path, encoding="utf-8"))
    except Exception:
        sys.stderr.write(
            f"[mini-ork-resume] cannot continue {run_id}: no run_profile.json\n"
        )
        return 1
    recipe = str(profile.get("recipe") or "")
    kickoff = str(profile.get("kickoff_path") or "")
    if not recipe or not kickoff or not os.path.isfile(kickoff):
        sys.stderr.write(
            f"[mini-ork-resume] cannot continue {run_id}: recipe/kickoff unknown\n"
        )
        return 1
    os.environ["MINI_ORK_RUN_ID"] = run_id
    if root is None:
        root = os.environ.get("MINI_ORK_ROOT") or os.path.dirname(
            os.path.dirname(os.path.realpath(__file__)))
    return _lifecycle_entry([recipe, kickoff], root)


def resume_answers(
    run_id: str,
    answers: dict[str, str],
    *,
    home: str | None = None,
) -> tuple[int, str]:
    """Record ``--answer`` values into the run's ASK files and, once every
    question is answered, apply them to the run profile and continue the run.

    Returns ``(rc, stdout_msg)``. ``rc`` is 0 on success (including "still
    unanswered"), 1 when the run dir or continuation prerequisites are missing.
    """
    run_dir = _resolve_run_dir(run_id, home=home)
    if not os.path.isdir(run_dir):
        sys.stderr.write(
            f"[mini-ork-resume] run dir not found: {run_dir}\n"
        )
        return 1, ""

    ask_files = _list_ask_files(run_dir)
    if not ask_files:
        sys.stderr.write(
            f"[mini-ork-resume] no open questions for {run_id} (nothing to answer)\n"
        )
        return 0, ""

    answered_at = int(time.time())
    questions: dict[str, str] = {}
    remaining: list[str] = []
    for path in ask_files:
        try:
            data = json.load(open(path, encoding="utf-8"))
        except (OSError, ValueError):
            remaining.append(os.path.basename(path))
            continue
        ask_id = str(data.get("ask_id") or "")
        if ask_id in answers and str(answers[ask_id]).strip():
            data["answer"] = str(answers[ask_id])
            data["answered_at"] = answered_at
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2)
                fh.write("\n")
        if not data.get("answer"):
            remaining.append(ask_id or os.path.basename(path))
        else:
            questions[str(data.get("question") or "")] = str(data.get("answer"))

    if remaining:
        ids = ", ".join(remaining)
        return 0, f"[mini-ork-resume] still unanswered: {ids}\n"

    # All answered: apply through plan._apply_profile_answers with the same
    # payload shape _prompt_profile_questions builds, then continue the run.
    from mini_ork.cli.plan import _apply_profile_answers

    profile_path = os.path.join(run_dir, "run_profile.json")
    payload = {"answers": questions, "auto_answered": False}
    if not _apply_profile_answers(profile_path, payload):
        sys.stderr.write(
            f"[mini-ork-resume] could not apply answers for {run_id}\n"
        )
        return 1, ""
    with open(os.path.join(run_dir, "profile-answers.json"), "w",
              encoding="utf-8") as fh:
        json.dump(questions, fh, indent=2)
        fh.write("\n")
    rc = _reenter_lifecycle(run_id, home=home)
    return rc, ""


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]

    run_id = ""
    answers: dict[str, str] = {}
    restart = True
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("--help", "-h"):
            sys.stdout.write(_usage())
            return 0
        if a == "--no-restart":
            restart = False
            i += 1
        elif a == "--answer":
            if i + 1 >= len(argv):
                sys.stderr.write("--answer requires ask-N=text\n")
                return 2
            parsed = _parse_answer(argv[i + 1])
            i += 2
            if parsed is None:
                sys.stderr.write(f"malformed --answer: {argv[i - 1]!r} (expected ask-N=text)\n")
                return 2
            answers[parsed[0]] = parsed[1]
        elif a.startswith("--answer="):
            parsed = _parse_answer(a[len("--answer="):])
            if parsed is None:
                sys.stderr.write(f"malformed --answer: {a!r} (expected ask-N=text)\n")
                return 2
            answers[parsed[0]] = parsed[1]
            i += 1
        elif a.startswith("-"):
            sys.stderr.write(f"Unknown flag: {a}. Try --help\n")
            return 2
        else:
            if run_id:
                sys.stderr.write(f"Unexpected argument: {a}\n")
                return 2
            run_id = a
            i += 1

    if not run_id:
        sys.stdout.write(_usage())
        return 2

    if answers:
        rc, stdout_msg = resume_answers(run_id, answers)
        if stdout_msg:
            sys.stdout.write(stdout_msg)
        return rc

    rc, stdout_msg, _audit, _removed = resume(run_id, restart=restart)
    if stdout_msg:
        sys.stdout.write(stdout_msg)
    return rc


if __name__ == "__main__":
    sys.exit(main())