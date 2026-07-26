"""mo_healer_bridge.py — Python port of ``lib/mo-healer-bridge.sh``.

Strangler-fig co-existence: the bash source stays in place; this module
gives Python callers an in-process target and gives parity tests a
stable surface to compare against the live bash function via subprocess.

The bash ``mo_run_healer_on_escalate`` function:

  1. Runs ``healer.sh <epic> <run_dir>`` and parses the JSON it prints.
  2. Switches on ``recovery_action``:
       cleaner-on-main / rebase-and-retry / wait-and-retry
         → auto-apply, return 0 on success
       switch-agent / shrink-scope
         → emit a brain hint, return 1
       escalate-human / mark-wontfix / no-op / ""
         → terminal, return 1
       anything else
         → unknown, return 1
  3. Returns 0 if a recovery was applied (caller re-tries the epic),
     1 otherwise.

This port reproduces the decision logic and side-effect dispatch. The
``decide()`` function is pure and exhaustively parity-tested; the
``mo_run_healer_on_escalate()`` entry point mirrors the bash end-to-end
behaviour (env override of ``MINI_ORK_ROOT``, rc 0/1).

WS5 cutover: the ``bash lib/healer.sh`` / ``bash lib/cleaner.sh``
subprocess calls are replaced by the staged native ports
``mini_ork.recovery.healer.decide`` and ``mini_ork.recovery.cleaner.main``
(imported below as module seams so tests can monkeypatch them, exactly
like the bash tests stubbed ``$MINI_ORK_ROOT/lib/*.sh``). The stdout/
stderr/rc forwarding contract is unchanged: healer's stdout is the
parsed JSON line (its stderr is discarded, as before), and the cleaner's
captured stdout/stderr lines are forwarded to this bridge's stderr with
the same six-space indent.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import time

from mini_ork.recovery import cleaner as _cleaner
from mini_ork.recovery import healer as _healer


WAIT_DEFAULT_S = 30
WAIT_MIN_S = 10
WAIT_MAX_S = 300

AUTO_APPLY_ACTIONS = frozenset({"cleaner-on-main", "rebase-and-retry",
                                "wait-and-retry"})
HINT_ACTIONS = frozenset({"switch-agent", "shrink-scope"})
TERMINAL_ACTIONS = frozenset({"escalate-human", "mark-wontfix", "no-op", ""})


def parse_healer_output(raw: str) -> dict:
    """Parse the single JSON line that ``healer.sh`` prints to stdout.

    Mirrors bash's ``jq -r '.field // default'`` semantics: empty or
    invalid JSON yields an empty dict so the caller's defaults kick in.
    The bash source parses three fields (``recovery_action``, ``lesson_id``,
    ``matched``) — Python callers should pull those via the dedicated
    helpers below so the fallback semantics stay identical.
    """
    raw = (raw or "").strip()
    if not raw:
        return {}
    try:
        obj = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return obj if isinstance(obj, dict) else {}


def classify_recovery(healer_output: dict) -> str:
    """Return ``recovery_action`` from parsed healer output (default ``""``).

    Mirrors bash's ``recovery=$(jq -r '.recovery_action // ""')`` — the
    default of ``""`` is what triggers the terminal branch in the case
    statement.
    """
    if not healer_output:
        return ""
    val = healer_output.get("recovery_action")
    return str(val) if val is not None else ""


def extract_lesson_id(healer_output: dict) -> "str | None":
    """Return ``lesson_id`` or ``None`` if missing/null.

    Mirrors bash's ``lesson_id=$(jq -r '.lesson_id // "null"')`` followed
    by the ``[ "$lesson_id" = "null" ]`` guard in ``_mo_bridge_bump_lesson``.
    """
    if not healer_output:
        return None
    val = healer_output.get("lesson_id")
    if val is None:
        return None
    if isinstance(val, str) and val == "null":
        return None
    return str(val)


def extract_matched(healer_output: dict) -> bool:
    """Return the ``matched`` flag (default ``False``)."""
    if not healer_output:
        return False
    val = healer_output.get("matched", False)
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return bool(val)
    return str(val).lower() == "true"


def clamp_wait_s(wait_s) -> int:
    """Clamp ``wait_s`` to ``[WAIT_MIN_S, WAIT_MAX_S]``.

    Mirrors the bash::

        case "$wait_s" in
          ''|*[!0-9]*) wait_s=30 ;;
        esac
        [ "$wait_s" -lt 10 ] && wait_s=10
        [ "$wait_s" -gt 300 ] && wait_s=300

    Non-numeric / empty / ``None`` → ``WAIT_DEFAULT_S`` (30). Fractional
    values are floored via ``int()`` so ``30.9`` becomes ``30``.
    """
    try:
        n = int(wait_s)
    except (TypeError, ValueError):
        n = WAIT_DEFAULT_S
    if n < WAIT_MIN_S:
        n = WAIT_MIN_S
    if n > WAIT_MAX_S:
        n = WAIT_MAX_S
    return n


def extract_wait_s(healer_output: dict) -> int:
    """Read ``recovery_args.wait_s`` from healer output, clamp it.

    Mirrors bash's::

        wait_s=$(echo "$healer_out" | jq -r '.recovery_args.wait_s // 30')
    """
    if not healer_output:
        return WAIT_DEFAULT_S
    args = healer_output.get("recovery_args")
    if not isinstance(args, dict):
        return WAIT_DEFAULT_S
    return clamp_wait_s(args.get("wait_s", WAIT_DEFAULT_S))


def is_bridge_disabled() -> bool:
    """Return True when ``MO_HEALER_BRIDGE_DISABLED=1`` (the escape hatch)."""
    return os.environ.get("MO_HEALER_BRIDGE_DISABLED", "0") == "1"


def action_kind(recovery: str) -> str:
    """Classify a recovery_action string into one of:

    - ``"auto_apply"`` — cleaner-on-main / rebase-and-retry / wait-and-retry
    - ``"hint"`` — switch-agent / shrink-scope
    - ``"terminal"`` — escalate-human / mark-wontfix / no-op / ``""``
    - ``"unknown"`` — anything else (the bash ``*)`` branch)
    """
    if recovery in AUTO_APPLY_ACTIONS:
        return "auto_apply"
    if recovery in HINT_ACTIONS:
        return "hint"
    if recovery in TERMINAL_ACTIONS:
        return "terminal"
    return "unknown"


def decide(healer_output: dict) -> dict:
    """Pure decision function — given parsed healer output, return the bridge
    decision as a dict with keys:

    - ``action``  — the ``recovery_action`` string (``""`` if missing)
    - ``kind``    — one of ``auto_apply`` / ``hint`` / ``terminal`` / ``unknown``
    - ``rc``      — expected return code from ``mo_run_healer_on_escalate``
                    (``0`` iff ``kind == "auto_apply"``, else ``1``)
    - ``wait_s``  — clamped ``recovery_args.wait_s`` (only meaningful for
                    ``wait-and-retry``; ``0`` otherwise)
    - ``lesson_id`` — string or ``None``
    - ``matched`` — bool

    The bash source's case statement collapses all auto-apply branches into
    a single ``rc=0`` outcome when execution succeeds; this helper exposes
    the *intent* without performing the subprocess dispatch.
    """
    action = classify_recovery(healer_output)
    kind = action_kind(action)
    return {
        "action": action,
        "kind": kind,
        "rc": 0 if kind == "auto_apply" else 1,
        "wait_s": (extract_wait_s(healer_output)
                   if action == "wait-and-retry" else 0),
        "lesson_id": extract_lesson_id(healer_output),
        "matched": extract_matched(healer_output),
    }


def write_cleaner_brief(bridge_dir: str, epic: str,
                        lesson_id: "str | None") -> str:
    """Write a ``detective.json`` for ``cleaner.sh``.

    Mirrors the bash ``jq -n --arg ep ...`` invocation in
    ``_mo_bridge_apply_cleaner``. The bash emits ``now|strftime(...)`` for
    ``detected_at``; Python uses ``time.strftime`` in UTC so the value is
    byte-identical when both run in the same minute (parity tests compare
    the *structural* shape, not the literal timestamp).
    Returns the path written.
    """
    os.makedirs(bridge_dir, exist_ok=True)
    payload = {
        "epic_id": epic,
        "classification": "baseline_rot",
        "confidence": 0.9,
        "evidence": [],
        "recommendation": "cleaner-on-main",
        "cleaner_brief": (
            f"mo-healer-bridge recovery (lesson_id={lesson_id}). "
            f"Restore main to a clean baseline so {epic} can retry."
        ),
        "rationale": "mo-healer-bridge auto-recovery",
        "source": "mo-healer-bridge",
        "detected_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    path = os.path.join(bridge_dir, "detective.json")
    with open(path, "w") as fh:
        json.dump(payload, fh, indent=2)
        fh.write("\n")
    return path


def _mini_ork_root() -> str:
    """Resolve ``MINI_ORK_ROOT`` from env (mirrors bash ``${MINI_ORK_ROOT:-...}``)."""
    return os.environ.get("MINI_ORK_ROOT") or os.getcwd()


def _bump_lesson(lesson_id: "str | None", kind: str) -> None:
    """Mirror bash's ``_mo_bridge_bump_lesson`` — skip if lesson_id null/empty.

    In the bash source this dispatches to ``memory-retrieve.sh
    bump-success`` / ``bump-failure``. In this Python port the lesson
    ledger lives outside the bridge's responsibility; the helper is a
    deterministic no-op so parity tests see identical behaviour.
    The ``kind`` argument is retained for parity with the bash signature
    (``success`` / ``failure``) even though no Python side-effect fires.
    """
    del kind  # mirror bash dispatch table; no-op stub.
    if lesson_id is None or lesson_id == "" or lesson_id == "null":
        return
    return


def _apply_cleaner(epic: str, epic_run_dir: str,
                   lesson_id: "str | None") -> int:
    """Apply the ``cleaner-on-main`` recovery (mirrors bash helper).

    WS5 cutover: ``bash $MINI_ORK_ROOT/lib/cleaner.sh <brief> <dir>`` →
    in-process ``mini_ork.recovery.cleaner.main([brief, dir])``. The
    module is always importable, so the bash "cleaner.sh not executable"
    guard is obsolete. stdout/stderr are captured and forwarded with the
    same six-space indent the bash bridge used on the subprocess streams
    (stdout lines first, then stderr lines).
    """
    bridge_dir = os.path.join(epic_run_dir, "healer-cleaner")
    brief_path = write_cleaner_brief(bridge_dir, epic, lesson_id)

    sys.stderr.write(f"[mini-ork] dispatching cleaner-on-main for {epic}...\n")
    rc = 0
    out_buf = io.StringIO()
    err_buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(out_buf), \
                contextlib.redirect_stderr(err_buf):
            rc = int(_cleaner.main([brief_path, bridge_dir]))
    except Exception:
        rc = 1
    for line in out_buf.getvalue().splitlines():
        sys.stderr.write(f"      {line}\n")
    for line in err_buf.getvalue().splitlines():
        sys.stderr.write(f"      {line}\n")

    if rc == 0:
        _bump_lesson(lesson_id, "success")
        sys.stderr.write(
            f"[mini-ork] cleaner-on-main succeeded for {epic} "
            f"— epic will retry\n")
        return 0
    _bump_lesson(lesson_id, "failure")
    sys.stderr.write(
        f"[mini-ork] cleaner-on-main failed (rc={rc}) for {epic}\n")
    return 1


def _apply_rebase(epic: str, worktree: "str | None",
                  lesson_id: "str | None") -> int:
    """Apply the ``rebase-and-retry`` recovery (mirrors bash helper)."""
    if not worktree or not os.path.isdir(worktree):
        sys.stderr.write(
            f"[mini-ork] no worktree path for {epic} — skip rebase\n")
        return 1

    stashed = False
    try:
        diff_proc = subprocess.run(
            ["git", "-C", worktree, "diff", "--quiet", "HEAD"],
            capture_output=True,
        )
        if diff_proc.returncode != 0:
            stash_msg = f"mo-healer-bridge-rebase {epic} {int(time.time())}"
            stash_proc = subprocess.run(
                ["git", "-C", worktree, "stash", "push", "-m", stash_msg],
                capture_output=True,
            )
            stashed = stash_proc.returncode == 0
    except (OSError, subprocess.SubprocessError):
        stashed = False

    sys.stderr.write(f"[mini-ork] git rebase main in {worktree}...\n")
    rc = 0
    try:
        proc = subprocess.run(
            ["git", "-C", worktree, "rebase", "main"],
            capture_output=True, text=True,
        )
        for line in (proc.stdout or "").splitlines():
            sys.stderr.write(f"      {line}\n")
        if proc.returncode != 0:
            rc = proc.returncode
        if proc.stderr:
            for line in proc.stderr.splitlines():
                sys.stderr.write(f"      {line}\n")
    except (OSError, subprocess.SubprocessError):
        rc = 1

    if rc != 0:
        sys.stderr.write(
            "[mini-ork] rebase produced conflicts — aborting + restoring "
            "stash\n")
        subprocess.run(["git", "-C", worktree, "rebase", "--abort"],
                       capture_output=True)
        if stashed:
            subprocess.run(["git", "-C", worktree, "stash", "pop"],
                           capture_output=True)
        _bump_lesson(lesson_id, "failure")
        return 1

    if stashed:
        subprocess.run(["git", "-C", worktree, "stash", "pop"],
                       capture_output=True)
    _bump_lesson(lesson_id, "success")
    sys.stderr.write(
        f"[mini-ork] rebase succeeded for {epic} — epic will retry\n")
    return 0


def _apply_wait(epic: str, lesson_id: "str | None", wait_s: int) -> int:
    """Apply the ``wait-and-retry`` recovery (mirrors bash helper)."""
    sys.stderr.write(
        f"[mini-ork] wait-and-retry for {epic}: sleeping {wait_s}s...\n")
    time.sleep(wait_s)
    _bump_lesson(lesson_id, "success")
    return 0


def mo_run_healer_on_escalate(epic: str, epic_run_dir: str,
                              worktree: "str | None" = None) -> int:
    """Python entry point mirroring bash ``mo_run_healer_on_escalate``.

    Returns ``0`` when a recovery was successfully applied (caller should
    flip the epic back to ``PENDING`` so the outer loop re-dispatches),
    ``1`` for no-recovery / terminal / hint / disabled / failed execution.
    """
    if is_bridge_disabled():
        return 1

    sys.stderr.write(
        f"[mini-ork] mo-healer-bridge: classifying ESCALATE for {epic}\n")

    # WS5 cutover: `bash $MINI_ORK_ROOT/lib/healer.sh <epic> <run_dir>` →
    # in-process mini_ork.recovery.healer.decide. The module is always
    # importable, so the bash "mo-healer not executable" guard is obsolete.
    # As in the bash bridge, the healer's rc and stderr are discarded —
    # only an empty/non-JSON stdout changes the flow.
    healer_out_raw = ""
    try:
        _healer_rc, healer_out_raw, _healer_err = _healer.decide(
            epic, epic_run_dir, mini_ork_root=_mini_ork_root()
        )
    except Exception:
        healer_out_raw = ""

    if not healer_out_raw.strip():
        sys.stderr.write(
            "[mini-ork] mo-healer returned empty — no recovery\n")
        return 1

    parsed = parse_healer_output(healer_out_raw)
    if not parsed:
        sys.stderr.write(
            "[mini-ork] mo-healer returned non-JSON — no recovery\n")
        return 1

    decision = decide(parsed)
    action = decision["action"]
    lesson_id = decision["lesson_id"]
    matched = decision["matched"]

    # bash prints the raw jq values: lesson as the literal string "null"
    # when absent, matched as lowercase true/false.
    lesson_disp = lesson_id if lesson_id is not None else "null"
    matched_disp = "true" if matched else "false"
    sys.stderr.write(
        f"[mini-ork] healer -> recovery={action} lesson={lesson_disp} "
        f"matched={matched_disp}\n")

    if decision["kind"] == "auto_apply":
        if action == "cleaner-on-main":
            return _apply_cleaner(epic, epic_run_dir, lesson_id)
        if action == "rebase-and-retry":
            return _apply_rebase(epic, worktree, lesson_id)
        if action == "wait-and-retry":
            return _apply_wait(epic, lesson_id, decision["wait_s"])
        return 1  # defensive (auto_apply set can only contain the three above)
    if decision["kind"] == "hint":
        sys.stderr.write(
            f"[mini-ork] healer suggests {action} for {epic} — brain hint "
            "emitted, no auto-apply\n")
        return 1
    if decision["kind"] == "terminal":
        return 1

    sys.stderr.write(
        f"[mini-ork] mo-healer unknown recovery: '{action}' — no "
        "auto-apply\n")
    return 1


__all__ = [
    "WAIT_DEFAULT_S",
    "WAIT_MIN_S",
    "WAIT_MAX_S",
    "AUTO_APPLY_ACTIONS",
    "HINT_ACTIONS",
    "TERMINAL_ACTIONS",
    "parse_healer_output",
    "classify_recovery",
    "extract_lesson_id",
    "extract_matched",
    "clamp_wait_s",
    "extract_wait_s",
    "is_bridge_disabled",
    "action_kind",
    "decide",
    "write_cleaner_brief",
    "mo_run_healer_on_escalate",
]