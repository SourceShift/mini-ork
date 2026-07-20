"""mo-steer CLI surface — Python port of lib/mo-steer.sh.

Faithful port of the bash CLI in ``lib/mo-steer.sh`` that pushes a
steering message to a live mini-ork worker. The bash script is the
authoritative source — this module gives Python callers an in-process
target and gives ``tests/unit/test_mo_steer_py.py`` a stable surface to
byte-diff against the LIVE bash subprocess (no mocks, no hardcoded
outputs).

Co-existence model (strangler-fig): bash ``lib/mo-steer.sh`` stays
byte-identical. The Python port mirrors its CLI semantics exactly. Parity
is enforced by ``tests/unit/test_mo_steer_py.py`` (eight live-subprocess
cases that drive ``bash lib/mo-steer.sh <args>`` on identical inputs
and diff the resulting JSONL envelope + stderr text + exit codes).

Pipeline map (bash CLI → Python):
  --steer-file= override          → _resolve_steer_paths (override branch)
  epic-id + job + iter derivation → _resolve_steer_paths (derive branch)
  MINI_ORK_HOME/runs/<job>/<epic>/iter-N layout
                                    → _resolve_steer_paths (derive branch)
  JOB inference from epic prefix   → _infer_job_from_epic
  (sed -E 's/-[A-Z0-9]+$//' | tr A-Z a-z)
  Heartbeat mtime liveness         → _check_heartbeat (os.path.getmtime)
  (stat -f %m / stat -c %Y fork)
  Heartbeat state=done|aborting    → _heartbeat_state (last-line JSONL pickup)
  (tail -1 | grep -oE '"state":"[^"]+"')
  Envelope generation              → steer (json.dumps, atomic single write)
  ({id, at, from, body})
  --wait-ack polling               → steer (greps worker.log every 1s, 30s)
  (grep steer_yielded.*steer_id)
  Exit-code → exception mapping    → steer (2 → ValueError, 3-5 → RuntimeError,
  (5 = heartbeat, 7 = no ack)        7 → RuntimeError)

Public surface (mirrors bash CLI argument order — first positional is
epic, second positional is message or stdin-supplied):
    steer(epic, message, *, job='', iter=None, from_=None,
          steer_file='', heartbeat='', worker_log='',
          wait_ack=False, force=False) -> dict
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone

__all__ = [
    "steer",
    "_resolve_steer_paths",
    "_infer_job_from_epic",
    "_check_heartbeat",
    "_heartbeat_state",
    "_new_steer_id",
    "_now_iso",
]

_HEARTBEAT_STALE_SECS = 15
_WAIT_ACK_TIMEOUT_SECS = 30
_WAIT_ACK_TICK_SECS = 1
_HEARTBEAT_DONE_STATES = frozenset({"done", "aborting"})

# Pattern mirrors bash: `sed -E 's/-[A-Z0-9]+$//'`.
# Strip a single trailing `-` followed by uppercase letters / digits, e.g.
# "EXPL-DLG-C" → "EXPL-DLG". Then `.lower()` mirrors the `tr A-Z a-z` step.
_TRAILING_SUFFIX_RE = re.compile(r"-[A-Z0-9]+$")


def _now_iso() -> str:
    """Mirror ``date -u +%FT%TZ`` — UTC ISO-8601 with trailing ``Z``.

    Bash uses ``date -u +%FT%TZ`` (GNU + BSD both honor this format). The
    Python ``datetime.strftime`` with the same format string produces an
    identical representation. We truncate to seconds because bash's
    ``%T`` has no sub-second precision — this guarantees byte-identical
    output between bash and Python (sub-second drift would otherwise
    break the timestamp diff in the parity test).
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _new_steer_id() -> str:
    """Mirror bash ``uuidgen`` (32 hex chars, no dashes).

    bash falls through ``uuidgen`` → ``/proc/sys/kernel/random/uuid`` →
    ``$(date +%s)-$$-$RANDOM``. The Python port collapses to ``uuid4`` —
    the parity test strips ``id`` before diffing envelopes, so the exact
    generator doesn't matter as long as both sides produce a unique id.
    Hex form matches uuidgen on macOS / Linux and is what bash's
    fallback chain emits on hosts without /proc.
    """
    return uuid.uuid4().hex


def _infer_job_from_epic(epic: str) -> str:
    """Mirror bash line 92::

        JOB="$(echo "$EPIC" | sed -E 's/-[A-Z0-9]+$//' | tr '[:upper:]' '[:lower:]')"

    Strips one trailing ``-<UPPERCASE_OR_DIGITS>`` token and lowercases
    the rest. Examples: "EXPL-DLG-C" → "expl-dlg", "FOO-9" → "foo".
    """
    if not epic:
        return ""
    stripped = _TRAILING_SUFFIX_RE.sub("", epic)
    return stripped.lower()


def _repo_root() -> str:
    """Mirror bash line 87::

        REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"

    Tests point CWD at a temp dir without a .git marker so the git
    fallback fires — ``pwd`` then resolves to the temp dir, and bash +
    Python land on the same REPO_ROOT.
    """
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=5,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return os.getcwd()
    if r.returncode == 0 and r.stdout.strip():
        return r.stdout.strip()
    return os.getcwd()


def _latest_iter_dir(epic_dir: str) -> str:
    """Mirror bash lines 98-101::

        ITER_DIR=$(ls -1d "$EPIC_DIR"/iter-* 2>/dev/null | sort -V | tail -1)

    Glob ``iter-*`` directories under ``epic_dir``, sort them with
    natural version ordering, return the highest (latest) path. Returns
    the empty string when no iter dirs exist — bash's ``[ -d "" ]`` then
    fails and exits 4, so callers must treat the empty result as "iter
    dir not found".
    """
    if not os.path.isdir(epic_dir):
        return ""
    matches = [
        os.path.join(epic_dir, name)
        for name in os.listdir(epic_dir)
        if name.startswith("iter-") and os.path.isdir(os.path.join(epic_dir, name))
    ]
    if not matches:
        return ""
    matches.sort(key=lambda p: _natural_iter_key(os.path.basename(p)))
    return matches[-1]


def _natural_iter_key(name: str) -> tuple[object, ...]:
    """Sort key for ``iter-1`` < ``iter-2`` < ``iter-10`` (version sort).

    Mirrors bash ``sort -V``: numeric segments sort numerically, alphabetic
    segments sort lexicographically. Tuple form gives a stable,
    deterministic key for ``sorted(..., key=...)``.
    """
    parts = re.split(r"(\d+)", name)
    return tuple(int(p) if p.isdigit() else p for p in parts)


def _resolve_steer_paths(
    *,
    epic: str,
    steer_file: str,
    job: str,
    iter: int | None,
    heartbeat: str = "",
    log: str = "",
) -> tuple[str, str, str, str]:
    """Return ``(steer_file, heartbeat, worker_log, inferred_job)``.

    Mirrors bash lines 81-106. Two branches:

    override: ``--steer-file=`` was given. Heartbeat + worker_log derive
              from the same directory unless explicit overrides were
              passed (``--heartbeat=``, ``--log=``).
    derive:   REPO_ROOT from git (else pwd) → MINI_ORK_HOME/.mini-ork
              → runs/<job>/<epic>/iter-N. Job is inferred from the epic
              prefix when not given. Missing epic_dir exits 3; missing
              iter_dir exits 4 (callers map to RuntimeError).

    The inferred_job return value is "" when override branch fires, the
    actual job when derive branch fires — tests can assert against it.
    """
    if steer_file:
        hb = heartbeat or os.path.join(os.path.dirname(steer_file), "HEARTBEAT")
        wl = log or os.path.join(os.path.dirname(steer_file), "worker.log")
        return steer_file, hb, wl, ""

    repo_root = _repo_root()
    mini_ork_home = os.environ.get("MINI_ORK_HOME", ".mini-ork")
    runs_dir = os.path.normpath(os.path.join(repo_root, mini_ork_home, "runs"))

    inferred_job = job or _infer_job_from_epic(epic)
    epic_dir = os.path.normpath(os.path.join(runs_dir, inferred_job, epic))
    if not os.path.isdir(epic_dir):
        raise RuntimeError(
            f"ERROR: epic dir not found: {epic_dir}"
        )
    if iter is None or iter == "":
        iter_dir = _latest_iter_dir(epic_dir)
    else:
        iter_dir = os.path.normpath(os.path.join(epic_dir, f"iter-{iter}"))
    if not iter_dir or not os.path.isdir(iter_dir):
        raise RuntimeError(
            f"ERROR: iter dir not found: {iter_dir or os.path.join(epic_dir, 'iter-')}"
        )

    return (
        os.path.join(iter_dir, "STEER.jsonl"),
        os.path.join(iter_dir, "HEARTBEAT"),
        os.path.join(iter_dir, "worker.log"),
        inferred_job,
    )


def _heartbeat_state(path: str) -> str:
    """Mirror bash lines 122::

        HB_STATE=$(cat "$HEARTBEAT" 2>/dev/null | tail -1 | grep -oE '"state":"[^"]+"' | cut -d'"' -f4 || echo unknown)

    Returns the ``state`` field value of the last JSONL line, or
    ``"unknown"`` when the file is missing / unreadable / has no state
    field. The last-line projection matches bash's ``tail -1`` so a
    multi-line HEARTBEAT (older heartbeats appended on each tick) still
    reflects the most recent state.

    NOTE: bash's regex requires compact JSON (``"state":"done"`` with no
    space). We mirror that exactly — heartbeats written by ``jq -nc``
    match, but heartbeats written by ``json.dumps(..., indent=...)`` or
    Python's default ``json.dumps`` (which inserts ``": "`` with a
    space) won't match, and bash falls through to "unknown" too.
    """
    if not path or not os.path.isfile(path):
        return "unknown"
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except OSError:
        return "unknown"
    if not lines:
        return "unknown"
    last = lines[-1]
    m = re.search(r'"state":"([^"]+)"', last)
    if not m:
        return "unknown"
    return m.group(1)


def _check_heartbeat(path: str, *, force: bool = False) -> str:
    """Return one of ``ok`` / ``stale`` / ``done`` / ``aborting`` / ``missing``.

    Mirrors bash lines 111-128. Returns ``missing`` when the heartbeat
    file doesn't exist (allowed — bash treats that as "no heartbeat to
    check, proceed"). Returns ``stale`` when mtime > 15s old. Returns
    ``done`` or ``aborting`` when the last-line state matches; the
    caller maps both to exit 5. ``force=True`` short-circuits to ``ok``
    only for the heartbeat-freshness checks — bash does NOT short-
    circuit on missing heartbeat (the `if [ -f ... ]` guard prevents
    that), so we mirror it: missing + force still returns ``missing``.
    """
    if not path or not os.path.isfile(path):
        return "missing"

    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return "missing"

    age = int(time.time()) - int(mtime)
    if not force and age > _HEARTBEAT_STALE_SECS:
        return "stale"

    state = _heartbeat_state(path)
    if not force and state in _HEARTBEAT_DONE_STATES:
        return state
    return "ok"


def _append_envelope_atomic(steer_file: str, envelope: dict) -> None:
    """Append one JSONL line to ``steer_file`` atomically (single write).

    Mirrors bash lines 134-143::

        MSG_LEN=${#MSG}
        JSON=$(jq -nc ...)
        printf '%s\n' "$JSON" >> "$STEER_FILE"

    bash's ``printf >> file`` is atomic when the resulting write is
    smaller than ``PIPE_BUF`` (~4KB on Linux, ~512B on macOS). Python's
    ``open(..., 'a').write(...)`` produces a single ``write()`` syscall
    on POSIX — the OS then writes the buffer atomically when its size
    is below the pipe-buffering threshold. We explicitly ``flush()`` and
    don't go through any line-buffered text-mode shim that could split
    the write into multiple ``write()`` calls.

    The caller is responsible for ``os.makedirs(parent, exist_ok=True)``
    — the bash script's ``mkdir -p`` fires unconditionally before the
    heartbeat check, so we mirror that here.
    """
    line = json.dumps(envelope) + "\n"
    # Single write + explicit flush mirrors bash's printf >> file
    # atomicity guarantee. Text mode is fine because json.dumps always
    # emits ASCII-safe bytes for the envelope shape {id, at, from, body}.
    with open(steer_file, "a", encoding="utf-8") as fh:
        fh.write(line)
        fh.flush()


def _wait_for_ack(worker_log: str, steer_id: str, *, timeout_secs: int = _WAIT_ACK_TIMEOUT_SECS) -> int:
    """Poll ``worker_log`` for a steer_yielded event matching ``steer_id``.

    Mirrors bash lines 146-157::

        if grep -q '"event":"steer_yielded".*"steer_id":"$ID"' "$WORKER_LOG" 2>/dev/null

    Returns the seconds elapsed on success (mirrors bash's
    ``ack received in ${i}s``). Raises ``RuntimeError`` on timeout,
    mirroring bash's exit 7 + stderr ``WARN: no ack within 30s``.
    """
    pattern = re.compile(
        r'"event"\s*:\s*"steer_yielded".*"steer_id"\s*:\s*"' + re.escape(steer_id) + r'"'
    )
    deadline = time.monotonic() + timeout_secs
    elapsed = 0
    while time.monotonic() < deadline:
        if worker_log and os.path.isfile(worker_log):
            try:
                with open(worker_log, "r", encoding="utf-8", errors="replace") as fh:
                    content = fh.read()
            except OSError:
                content = ""
            if pattern.search(content):
                return elapsed
        time.sleep(_WAIT_ACK_TICK_SECS)
        elapsed += 1
    raise RuntimeError(
        f"[mo-steer] WARN: no ack within {timeout_secs}s — message in queue but unconfirmed"
    )


def steer(
    epic: str,
    message: str,
    *,
    job: str = "",
    iter: int | None = None,
    from_: str | None = None,
    steer_file: str = "",
    heartbeat: str = "",
    worker_log: str = "",
    wait_ack: bool = False,
    force: bool = False,
) -> dict:
    """Mirror ``lib/mo-steer.sh`` CLI semantics — push one steering envelope.

    Required positional arguments:
        epic      — epic id (e.g. "EXPL-DLG-C"); drives path inference
                    unless ``steer_file`` is given.
        message   — body text. Empty / None raises ``ValueError`` with
                    the same stderr phrase bash emits ("empty message").
                    (Bash can read from stdin when ``message`` is omitted
                    on argv; the Python port does not — callers must
                    supply the body explicitly.)

    Keyword arguments:
        job         — explicit job-id (overrides epic-prefix inference)
        iter        — explicit iter-N number (else: latest iter dir)
        from_       — sender tag (defaults to ``$USER`` env, else "user");
                      trailing underscore avoids the Python keyword clash.
        steer_file  — explicit STEER.jsonl path override (skips derivation)
        heartbeat   — explicit HEARTBEAT path override
        worker_log  — explicit worker.log path override
        wait_ack    — poll worker.log for steer_yielded event with matching
                      steer_id for 30s before returning (timeout raises
                      ``RuntimeError``, mirroring bash exit 7)
        force       — bypass heartbeat freshness + state checks (bash's
                      ``--force``)

    Returns ``{"id": str, "steer_file": str, "envelope": dict}`` on
    success and emits the same ``[mo-steer] wrote N-byte steer (id=...)
    -> <path>`` line to stderr that bash emits.

    Exception mapping (mirrors bash exit codes):
        ValueError       — exit 2 (empty message, missing epic)
        RuntimeError     — exit 3 (epic dir), 4 (iter dir), 5 (heartbeat
                           stale / state=done / state=aborting), 7 (no ack)
        FileNotFoundError— I/O failures (parent dir unwritable, etc.) —
                           we let the underlying OSError propagate; no
                           silent fallbacks.
    """
    if not epic:
        raise ValueError(
            "mo-steer: missing epic-id (Usage: steer(<epic>, <message>))"
        )
    if message is None or message == "":
        raise ValueError(
            "ERROR: empty message"
        )

    sender = from_ if from_ is not None else os.environ.get("USER") or "user"

    # Resolve paths + infer job. The inferred_job return is "" in override
    # mode — bash only logs "[mo-steer] inferred job=… from epic prefix"
    # in derive mode without an explicit --job.
    if steer_file:
        sf, hb, wl, _ = _resolve_steer_paths(
            epic=epic, steer_file=steer_file, job=job, iter=iter,
            heartbeat=heartbeat, log=worker_log,
        )
        print(
            f"[mo-steer] using explicit steer file: {sf}",
            file=sys.stderr,
        )
    else:
        sf, hb, wl, inferred_job = _resolve_steer_paths(
            epic=epic, steer_file="", job=job, iter=iter,
            heartbeat=heartbeat, log=worker_log,
        )
        if not job and inferred_job:
            print(
                f"[mo-steer] inferred job={inferred_job} from epic prefix",
                file=sys.stderr,
            )

    # Ensure parent dir exists. bash's `mkdir -p "$(dirname "$STEER_FILE")"`
    # fires unconditionally — we mirror that on both branches.
    parent = os.path.dirname(sf) or "."
    os.makedirs(parent, exist_ok=True)

    # Heartbeat liveness check (skipped when force=True or override path
    # without an explicit heartbeat override). bash's check is gated on
    # `[ -f "$HEARTBEAT" ]` — we honor the same gate via _check_heartbeat
    # returning "missing" when the file isn't there.
    hb_status = _check_heartbeat(hb, force=force)
    if hb_status == "stale":
        try:
            age = int(time.time()) - int(os.path.getmtime(hb))
        except OSError:
            age = -1
        raise RuntimeError(
            f"ERROR: heartbeat stale ({age}s old) — worker likely dead\n"
            f"  {hb}\n"
            f"  Override with --force if you really mean it."
        )
    if hb_status in _HEARTBEAT_DONE_STATES:
        raise RuntimeError(
            f"ERROR: heartbeat says state={hb_status} — worker not accepting steer\n"
            f"  Override with --force."
        )

    # Generate envelope.
    envelope = {
        "id": _new_steer_id(),
        "at": _now_iso(),
        "from": sender,
        "body": message,
    }

    _append_envelope_atomic(sf, envelope)

    # bash line 144: `echo "[mo-steer] wrote $MSG_LEN-byte steer (id=$ID) -> $STEER_FILE" >&2`
    print(
        f"[mo-steer] wrote {len(message)}-byte steer (id={envelope['id']}) -> {sf}",
        file=sys.stderr,
    )

    if wait_ack:
        # Read the module attribute at call time (not via default-arg
        # capture) so monkeypatch.setattr can override _WAIT_ACK_TIMEOUT_SECS
        # for parity tests that want a shorter timeout than the 30s
        # bash hardcodes.
        elapsed = _wait_for_ack(wl, envelope["id"], timeout_secs=_WAIT_ACK_TIMEOUT_SECS)
        print(
            f"[mo-steer] ack received in {elapsed}s — steer delivered + queued",
            file=sys.stderr,
        )

    return {"id": envelope["id"], "steer_file": sf, "envelope": envelope}