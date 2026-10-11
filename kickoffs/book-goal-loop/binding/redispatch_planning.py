#!/usr/bin/env python3
"""Re-dispatch a wedged PLANNING burst — the planning twin of redispatch_chapter.py.

The chapter loop re-dispatches the WRITE stage through researcher's sanctioned
``forceResumeJob``. Planning needs its own path, because the two stages resume
by completely different mechanisms, and using the chapter one here would be a
silent no-op:

  * A chapter job resumes from ``book_chapter_lifecycle`` rows.
  * A DSP planning burst has NO lifecycle rows yet — it resumes from a durable
    checkpoint, which is a different service call entirely. The product's own
    route handler spells out why: a DSP planning job "resumes from a durable
    checkpoint, NOT a Claude Agent SDK sandbox session ... the legacy
    resumePlanFromSession gate ... can NEVER succeed for them, which is exactly
    the dead Resume button the user hit."

So this calls ``server/scripts/resumeFailedPlanningJobs.ts`` — the general,
sanctioned, ops-facing driver that branches on the failure class and, for a
PLANNING-STALLED job, calls ``planningSessionService.resumeFailedPlanningJob``
(abandoned bursts resume from their persisted checkpoint). It is dry-run by
default upstream, so ``--apply`` is required for it to act.

Contract (recipes/goal-loop/lib/transforms.py::_run_apply): invoked as
``python3 redispatch_planning.py <run_uuid>`` (argv, not shell) inside
MO_GOAL_TARGET_CWD, once per unit, AFTER the deploy has made the new code live.
Ordering is load-bearing: re-dispatching before the worker restarts would let
the stale worker pick the unit up and re-fail with the same bug.

Unit identity is the run UUID; the text ``job_id`` the planning tables key on is
resolved by join. Both are passed where each is needed.

Set MO_GOAL_REDISPATCH_DRY=1 to print the plan without acting.
Connection comes from libpq env vars; no secret lives here.

THREE TRAPS THIS SCRIPT EXISTS TO AVOID — each one measured, none guessable
from the driver's own docs. All three make the naive "shell out to the driver
and trust its exit code" version fail *silently*, which is worse than useless
inside an autonomous loop: the loop would bank a re-dispatch that never
happened and then re-observe the identical wedge next wave.

  1. THE DRIVER REFUSES TO ACT WHILE THE BURST READS AS RUNNING.
     ``resumeFailedPlanningJobsPolicy.ts::decideRecoveryAction`` returns
     ``{action: 'wait', reason: 'canonical worker execution is already in
     progress'}`` for ``fsm=plan_sketching`` with session ``active`` or
     ``burst_running``. A WEDGED burst is ``burst_running`` — that is the whole
     reason the product's detectors are blind to it (see job_thrash.py). So the
     sanctioned resume path is blind to the same wedge, for the same cause, and
     calling it from that phase is a no-op that still exits 0. It only acts for
     ``fsm`` in {plan, failed} or ``plan_sketching`` + session ``abandoned``
     (``isResumablePlanningShape``). Hence ``_await_resumable_window``: poll the
     FSM cheaply until the job is in a phase the driver will actually drive,
     rather than burning a whole goal-loop wave on a refused call.
     This is derived from the policy source, not measured against a live wedge:
     the wedge we had was already repaired by the human fix before this landed,
     so there was no live ``failed``-phase call to observe. The no-op phase, by
     contrast, WAS observed — that is the dry-run reading quoted above.

  2. THE REPO-ROOT .env POISONS THE DRIVER'S DB TARGET.
     The driver builds its pool from ``POSTGRES_HOST/PORT/USER/DB/PASSWORD``
     (``server/config/database.ts``), and dotenvx loads the CWD's ``.env``
     BEFORE ``server/.env`` and never overrides an already-set key. The
     researcher checkout's root ``.env`` sets ``POSTGRES_HOST=localhost
     POSTGRES_PORT=5933`` — a dead local endpoint. Measured 2026-09-21: the
     driver initialised ``host=localhost, port=5933``, failed pool acquisition
     with an EMPTY error string, and reported ``resumeFailedPlanningJobs:
     fatal``. Nothing in the output names the host, so this reads as a
     mysterious DB fault rather than a wrong endpoint. The fix is to put the
     correct ``POSTGRES_*`` into the child's ENVIRONMENT, because dotenvx
     honours a pre-set process env and that outranks the stray file.

  3. THE EXIT CODE IS NOT THE VERDICT.
     Under trap 2 the driver finished its work, closed its pool, and then hung
     on an open handle instead of exiting — so a run that DID the re-dispatch
     would be scored as a failure by exit code. The authority is the driver's
     own ``decisions[]`` record, which names the action it took per job. We
     parse that. An unreadable/absent decision is reported as failure, never as
     success: staying conservative is what keeps the loop from banking a
     re-dispatch that did not happen.
"""
from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import threading
import time

_DEFAULT_SCRIPT = os.path.join("server", "scripts", "resumeFailedPlanningJobs.ts")
# Keys whose .env value must never override an explicitly exported one: the
# launcher exports these deliberately, and the operator may point them at a
# different instance than server/.env names.
_ENV_LINE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
_ANSI = re.compile(r"\x1b\[[0-9;]*m")

# The states a wedged planning job cycles through. Leaving them in any direction
# is progress, and makes a re-dispatch churn rather than a repair.
_PLANNING_REGION = {"plan", "plan_sketching", "failed"}

# Sessions in the planning region that make the driver's decision a no-op
# (`wait`). Mirrors the branch in decideRecoveryAction, and the reason this
# script polls instead of calling: these are the phases where "the burst is
# running" is indistinguishable from "the burst is wedged".
_NOOP_SESSIONS = {"active", "burst_running", "awaiting_steer"}

# Decision actions that mean the driver refused to move the job. `unsupported`
# is included because it is a refusal with a reason, not a success.
_REFUSALS = {"wait", "unsupported"}

# The driver's terminal self-report, which is what we actually key on. The two
# modes end on DIFFERENT lines, so both are matched:
#   * dry  -> "resumeFailedPlanningJobs: dry-run only; no state was changed"
#             whose body carries decisions[] = {jobId, action, reason}. It
#             RETURNS right after, with no action taken.
#   * apply-> "resumeFailedPlanningJobs: summary" whose body carries
#             outcomes[] = {job_id, ok, detail}. Logged as the last statement of
#             main(), AFTER every driveJob() has run.
# pino-pretty prints the message line first and the object after it, closing at
# column 0 — so "terminal" is the marker line followed by an unindented `}`.
_TERMINAL_MSG = re.compile(r"resumeFailedPlanningJobs: (dry-run only|summary)")
# decisions[] entry (dry mode): {"jobId": ..., "action": ..., "reason": ...}
_DECISION = re.compile(r'"jobId":\s*"([^"]+)"[^}]*?"action":\s*"([^"]+)"', re.S)
# outcomes[] entry (apply mode): {"job_id": ..., "ok": true|false, "detail": ...}
_OUTCOME = re.compile(r'"job_id":\s*"([^"]+)"[^}]*?"ok":\s*(true|false)', re.S)


def _q(sql: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "psql",
            "-h", os.environ.get("PGHOST", "100.74.239.22"),
            "-p", os.environ.get("PGPORT", "5932"),
            "-U", os.environ.get("PGUSER", "researcher_user"),
            "-d", os.environ.get("PGDATABASE", "researcher_db"),
            "-tA", "-F", "|", "-c", sql,
        ],
        capture_output=True,
        text=True,
    )


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def _load_dotenv(path: str) -> dict[str, str]:
    """Parse KEY=VALUE lines. Existing os.environ wins (operator override)."""
    out: dict[str, str] = {}
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                m = _ENV_LINE.match(line)
                if not m:
                    continue
                key, val = m.group(1), m.group(2).strip()
                if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
                    val = val[1:-1]
                out.setdefault(key, val)
    except OSError:
        return {}
    return out


def _resolve_job(run_uuid: str) -> tuple[str | None, str, str]:
    """run uuid -> (text job_id, fsm current_state, newest session status)."""
    proc = _q(
        "SELECT r.job_id, coalesce(f.current_state,''), "
        "coalesce((SELECT s.status FROM compose_planning_sessions s "
        "          WHERE s.job_id = r.job_id "
        "          ORDER BY s.updated_at DESC LIMIT 1), '') "
        "FROM book_generation_runs r "
        "LEFT JOIN compose_job_fsm_state f ON f.job_id = r.id "
        f"WHERE r.id='{run_uuid}';"
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        return None, "", ""
    cols = (proc.stdout.strip().splitlines()[0].split("|") + [""] * 3)[:3]
    return cols[0] or None, cols[1].strip().lower(), cols[2].strip().lower()


def _await_resumable_window(uuid: str) -> tuple[str, str, str | None]:
    """Poll until the driver would actually act, or the budget runs out.

    Returns (verdict, message, job_str) where verdict is one of ``advanced``
    (the job already left the planning region — nothing to do), ``ready`` (the
    job is in a phase the driver drives), or ``timeout``.

    Polling the FSM is a sub-second psql call; calling the driver is a ~20s tsx
    cold start. So we spend the cheap probe to avoid spending the expensive one
    on a phase where the answer is known in advance to be `wait`.
    """
    budget = _int_env("MO_GOAL_REDISPATCH_WINDOW_SECONDS", 900)
    poll = _int_env("MO_GOAL_REDISPATCH_POLL_SECONDS", 30)
    deadline = time.monotonic() + budget
    last = ""

    while True:
        job_str, fsm, session = _resolve_job(uuid)
        if not job_str:
            # Nothing to re-dispatch. Not an error: the loop may be racing a
            # resume the product did itself, and a missing row is not our failure.
            return "advanced", f"{uuid[:8]} no job row — nothing to re-dispatch", None

        if fsm and fsm not in _PLANNING_REGION:
            return "advanced", (
                f"{job_str} already left planning (state={fsm}) — re-dispatch is a no-op"
            ), job_str

        if fsm == "plan_sketching" and session in _NOOP_SESSIONS:
            # The phase the driver refuses from — and the phase a wedge spends
            # most of its cycle in. Wait for the cycle to turn.
            last = f"{fsm}/{session}"
            if time.monotonic() >= deadline:
                return "timeout", (
                    f"{job_str} stayed in {last} for {budget}s — the driver would "
                    "only `wait`; no re-dispatch attempted"
                ), job_str
            time.sleep(min(poll, max(deadline - time.monotonic(), 1)))
            continue

        # fsm in {plan, failed} or plan_sketching/abandoned: the driver acts.
        return "ready", f"{job_str} (fsm={fsm or '?'} session={session or '?'})", job_str


def _child_env(researcher: str) -> dict[str, str]:
    """server/.env as the base, ambient env on top — plus the DB target, pinned.

    Pinning POSTGRES_* is trap 2: dotenvx reads the CWD's root ``.env`` before
    ``server/.env`` and never overrides a key already present, so unless these
    are in the child's process env the stray localhost endpoint wins and the
    driver dies without naming the host. Ambient env still outranks the file,
    so an operator pointing at a different instance keeps control.
    """
    dotenv = _load_dotenv(os.path.join(researcher, "server", ".env"))
    env = dict(dotenv)
    env.update(os.environ)
    for key in ("POSTGRES_HOST", "POSTGRES_PORT", "POSTGRES_USER",
                "POSTGRES_DB", "POSTGRES_PASSWORD"):
        if key not in env and key in dotenv:
            env[key] = dotenv[key]
    return env


def _kill_group(proc: subprocess.Popen[str], grace: int = 10) -> None:
    """SIGTERM then SIGKILL the child's whole process GROUP, not just the child.

    The direct child is tsx's ``.bin`` shim; the node process that holds the DB
    pool is its GRANDCHILD. Killing only the shim orphans a live pool client and
    leaves it reconnecting — this codebase has already measured that failure
    (``server/config/database.ts`` documents 62 orphaned clients, 33 of which
    stormed reconnects during one outage). ``start_new_session=True`` puts the
    shim in its own group so one ``killpg`` reaches the node process too.
    """
    try:
        pgid = os.getpgid(proc.pid)
    except OSError:
        return
    try:
        os.killpg(pgid, signal.SIGTERM)
    except OSError:
        return
    try:
        proc.wait(timeout=grace)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(pgid, signal.SIGKILL)
    except OSError:
        pass


def _read_until_terminal(proc: subprocess.Popen[str], timeout: int) -> tuple[str, bool]:
    """Stream stdout until the driver's terminal self-report is complete.

    Returns (text, complete). ``complete`` is True once the terminal marker AND
    the unindented ``}`` that closes its object have both been seen — i.e. the
    driver has told us everything it is going to.

    This exists because the driver RELIABLY fails to exit: importing
    ``planningSessionService`` pulls in the unified worker's
    ``llm-provider-circuit-subscriber``, which opens a Redis client that is never
    quit, so the event loop stays alive forever after ``main()`` returns. The
    work is over in seconds; the process is not. Reading for the terminal report
    and then reaping is what keeps a wave from blocking on a dead handle.

    In apply mode ``summary`` is the last statement of ``main()`` and every
    ``driveJob`` has already run, so stopping there is safe. A short grace then
    lets ``.finally(databaseConfig.close())`` finish before the group is killed.
    """
    lines: list[str] = []
    seen_marker = False

    def _reader() -> None:
        nonlocal seen_marker
        assert proc.stdout is not None
        for line in proc.stdout:
            lines.append(line)
            if _TERMINAL_MSG.search(line):
                seen_marker = True
            elif seen_marker and line.rstrip("\n") == "}":
                # Object closed; nothing further is promised.
                return

    reader = threading.Thread(target=_reader, daemon=True)
    reader.start()
    deadline = time.monotonic() + timeout
    while reader.is_alive() and time.monotonic() < deadline:
        reader.join(timeout=0.5)

    complete = seen_marker or not reader.is_alive()
    if proc.poll() is None:
        # The reader returned on the closing brace — the normal path — but the
        # process is still up, hanging on the Redis handle. Give it a moment to
        # close its pool on its own, then reap the whole group so no orphaned
        # client survives the wave.
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            _kill_group(proc)
    reader.join(timeout=5)
    return "".join(lines), complete


def _run_driver(researcher: str, job_str: str, dry: bool, env: dict[str, str]) -> int:
    tsx = os.path.join(researcher, "node_modules", ".bin", "tsx")
    if not os.path.isfile(tsx):
        print(f"tsx not found at {tsx} (set MO_RESEARCHER_DIR to a built checkout)",
              file=sys.stderr)
        return 2

    args = [tsx, _DEFAULT_SCRIPT, job_str] + ([] if dry else ["--apply"])
    # Ceiling on the driver's WORK, not its exit — it does not exit (see
    # _read_until_terminal). Generous because a cold tsx start on this ~17k-file
    # checkout plus the pool handshake over the tailnet is tens of seconds.
    timeout = _int_env("MO_GOAL_REDISPATCH_TIMEOUT_SECONDS", 300)

    proc = subprocess.Popen(
        args, cwd=researcher, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        start_new_session=True,
    )
    out, complete = _read_until_terminal(proc, timeout)
    err = ""
    if proc.stderr is not None:
        try:
            err = proc.stderr.read() or ""
        except (OSError, ValueError):
            err = ""

    clean = _ANSI.sub("", out)
    for line in (clean.strip().splitlines()[-6:] if clean.strip() else []):
        print(f"    {line.strip()}")

    if not complete:
        print(f"    no terminal report from the driver within {timeout}s "
              f"(pid {proc.pid}) — cannot confirm a re-dispatch", file=sys.stderr)
        if err.strip():
            print(f"    stderr: {err.strip()[:300]}", file=sys.stderr)
        return 1

    if dry:
        decision = None
        for jid, action in _DECISION.findall(clean):
            if jid == job_str:
                decision = action
        if decision is None:
            print(f"    no decision record for {job_str} — cannot confirm a "
                  "re-dispatch", file=sys.stderr)
            return 1
        if decision in _REFUSALS:
            print(f"    driver declined: action={decision} — the re-dispatch did "
                  "NOT happen", file=sys.stderr)
            return 1
        return 0

    # Apply mode: the summary's outcome for OUR job is the verdict. Its absence
    # is a failure — the driver may have driven a different job or none.
    ok = None
    for jid, flag in _OUTCOME.findall(clean):
        if jid == job_str:
            ok = flag == "true"
    if ok is None:
        print(f"    no outcome recorded for {job_str} — the driver finished but "
              "did not report this job", file=sys.stderr)
        return 1
    if not ok:
        print(f"    driver reported the re-dispatch FAILED for {job_str}",
              file=sys.stderr)
        return 1
    return 0


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: redispatch_planning.py <run_uuid>", file=sys.stderr)
        return 2
    run_uuid = argv[0].strip()
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", run_uuid):
        print(f"bad run uuid: {run_uuid!r}", file=sys.stderr)
        return 2

    researcher = os.environ.get("MO_RESEARCHER_DIR", "").strip()
    if not researcher or not os.path.isdir(researcher):
        print(f"MO_RESEARCHER_DIR unset/not a dir: {researcher!r}", file=sys.stderr)
        return 2
    if not os.path.isfile(os.path.join(researcher, _DEFAULT_SCRIPT)):
        print(f"{_DEFAULT_SCRIPT} not found under {researcher}", file=sys.stderr)
        return 2

    dry = os.environ.get("MO_GOAL_REDISPATCH_DRY", "").strip() == "1"

    verdict, message, job_str = _await_resumable_window(run_uuid)
    if verdict == "advanced":
        print(message)
        return 0
    if verdict == "timeout":
        print(message, file=sys.stderr)
        return 1

    print(f"{run_uuid[:8]} {'WOULD re-dispatch' if dry else 're-dispatching'} "
          f"planning for {message}")
    rc = _run_driver(researcher, job_str or "", dry, _child_env(researcher))
    if rc == 0 and dry:
        print("    (dry run — nothing was re-dispatched)")
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
