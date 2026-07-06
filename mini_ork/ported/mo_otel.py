"""Span-buffer for live OTel — Python port of lib/mo_otel.sh.

Faithful port of the JSONL span emitter used by mini-ork to flush a
run's lifecycle events to Langfuse via the OTLP/JSON exporter. The Python
port gives callers an in-process surface (no `bash`/subprocess per call)
and gives the parity test a stable target to byte-diff against the live
bash.

Co-existence model (strangler-fig): bash `lib/mo_otel.sh` is the
authoritative source. This module mirrors its surfaces exactly. Parity is
enforced by `tests/unit/test_mo_otel_py.py` (>=6 cases that drive the
LIVE bash subprocess against a per-case MINI_ORK_RUN_DIR temp dir and
diff the resulting `.otel-spans.jsonl` buffer line-by-line against the
Python port byte-for-byte; the `flush` case uses MO_OTEL_DRY_RUN=1
against a `db/init.sh`-seeded state.db and diffs the printed OTLP
payload). Internally-generated timestamps (`root_begin.start_ms`,
`root_end.end_ms`) are compared within a 1500ms tolerance window
(subprocess-drift between the bash and Python invocations is expected);
explicit-arg timestamps (`mo_otel_agent`'s `start_ms`/`end_ms`) are
compared exactly.

Gating mirrors lib/mo_otel.sh:11-19:
  MO_OTEL=1                  master switch — without it every function
                              no-ops and returns 0
  MINI_ORK_RUN_DIR           buffer location (.otel-spans.jsonl lives here);
                              mo_otel_enabled() requires it set
  MO_OTEL_DRY_RUN=1          flush prints the OTLP payload instead of POSTing
  LANGFUSE_PUBLIC_KEY/SECRET flush leaves the host; without creds the JSONL
                              buffer survives on disk for a later resync

Failure mode: best-effort everywhere (mirrors bash). Observability must
never break execution — every entry point returns 0.

Pipeline map (bash function → Python):
  mo_otel_enabled     → mo_otel_enabled     (MO_OTEL=='1' AND MINI_ORK_RUN_DIR set)
  mo_otel_buf         → mo_otel_buf         (run_dir/.otel-spans.jsonl, '/.' fallback)
  _mo_otel_now_ms     → _now_ms             (gdate → time.time_ns → seconds*1000)
  mo_otel_emit        → mo_otel_emit        (append + newline; swallow OSError)
  mo_otel_root_begin  → mo_otel_root_begin  (delegates to mo_otel_emit)
  mo_otel_root_end    → mo_otel_root_end    (rc=='0' → 'success', else 'failure')
  mo_otel_agent       → mo_otel_agent       (delegates to mo_otel_emit)
  mo_otel_flush       → mo_otel_flush       (subprocess python3 -m mini_ork.otel_export)

JSON key/whitespace parity: the bash printf strings at lib/mo_otel.sh:54,
63, 71 emit no-space JSON (`{"type":"agent","node_id":"...",...}`). The
port reproduces that shape via `json.dumps(payload, separators=(",", ":"))`
with dict keys inserted in the same order as the bash printf — both the
buffer-diff and the flush-payload diff rely on this for byte-equality
(structural compare tolerates drift, but we don't want to depend on it).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

__all__ = [
    "mo_otel_enabled",
    "mo_otel_buf",
    "mo_otel_emit",
    "mo_otel_root_begin",
    "mo_otel_root_end",
    "mo_otel_agent",
    "mo_otel_flush",
    "_now_ms",
]


# Mirrors lib/mo_otel.sh:23-25
def mo_otel_enabled() -> bool:
    """Return True iff `MO_OTEL=1` and `MINI_ORK_RUN_DIR` is set.

    Bash semantics:
        [ "${MO_OTEL:-0}" = "1" ] && [ -n "${MINI_ORK_RUN_DIR:-}" ]
    Both conditions must hold. When False, every other entry point must
    be a silent no-op returning 0.
    """
    return (
        os.environ.get("MO_OTEL", "0") == "1"
        and bool(os.environ.get("MINI_ORK_RUN_DIR"))
    )


# Mirrors lib/mo_otel.sh:27-29
def mo_otel_buf() -> str:
    """Return the JSONL buffer path: `${MINI_ORK_RUN_DIR}/.otel-spans.jsonl`.

    Bash fallback for unset/empty `MINI_ORK_RUN_DIR` is `${VAR:-/}` →
    `/` (so the resulting path is `/.otel-spans.jsonl`). The port
    reproduces this with `or "/"` to cover both unset and empty-string
    cases. The result is purely a string; no filesystem access happens
    here, so callers that don't intend to write can still call this for
    the parity case (g).
    """
    run_dir = os.environ.get("MINI_ORK_RUN_DIR") or "/"
    return os.path.join(run_dir, ".otel-spans.jsonl")


# Mirrors lib/mo_otel.sh:_mo_otel_now_ms (gdate → python3 → date+%%s*1000)
def _now_ms() -> int:
    """Portable millisecond timestamp.

    Same rationale as `lib/mo_node_events.sh:_mo_now_ms` (also ported at
    `mini_ork.ported.mo_node_events._now_ms`): BSD `date` (macOS default)
    does NOT honor `%3N` — it silently emits the literal "N" instead of
    failing, which poisons arithmetic. Prefer GNU `gdate` (coreutils),
    fall back to `time.time_ns()`, then to `seconds*1000` as a
    last-resort 1s-resolution fallback. The leading underscore is dropped
    to match Python idioms; the bash function name `_mo_otel_now_ms` is
    preserved in the docstring for grep parity.
    """
    try:
        gdate = subprocess.run(
            ["gdate", "+%s%3N"], capture_output=True, text=True
        )
    except (FileNotFoundError, OSError):
        # gdate absent on Linux CI — the spawn raises; fall through to time_ns().
        gdate = None
    if gdate is not None and gdate.returncode == 0 and gdate.stdout.strip():
        try:
            return int(gdate.stdout.strip())
        except ValueError:
            pass
    try:
        return time.time_ns() // 1_000_000
    except AttributeError:  # pragma: no cover (Py<3.7)
        return int(time.time() * 1000)


# Mirrors lib/mo_otel.sh:44-47
def mo_otel_emit(json_line: str) -> int:
    """Append one raw JSON event line to the buffer. No-op when disabled.

    Bash semantics:
        mo_otel_enabled || return 0
        printf '%s\n' "${1:?json required}" >> "$(mo_otel_buf)" 2>/dev/null || true
    The bash `${1:?json required}` aborts with the phrase "json required"
    on stderr if $1 is unset/empty. The port mirrors that phrase + the
    silent-no-op-on-disabled + the IO-error-swallow (best-effort
    observability contract: always returns 0).
    """
    if not mo_otel_enabled():
        return 0
    if not json_line:
        print("mo_otel_emit: json required", file=sys.stderr)
        return 0
    try:
        with open(mo_otel_buf(), "a") as f:
            f.write(json_line + "\n")
    except OSError:
        pass
    return 0


# Mirrors lib/mo_otel.sh:51-55
def mo_otel_root_begin(run_id: str) -> int:
    """Record run start. Call once, after `MINI_ORK_RUN_DIR` is exported.

    Bash semantics:
        mo_otel_enabled || return 0
        local run_id="${1:?task_run_id required}"
        mo_otel_emit "{"type":"root_begin","task_run_id":"${run_id}","start_ms":<ms>}"
    Note: `enabled` is checked BEFORE the `:-` required-arg guard — that
    matters because a disabled caller passes no args and the bash would
    not fire the required-arg error. The port preserves this order.
    """
    if not mo_otel_enabled():
        return 0
    if not run_id:
        print("mo_otel_root_begin: task_run_id required", file=sys.stderr)
        return 0
    payload = {
        "type": "root_begin",
        "task_run_id": run_id,
        "start_ms": _now_ms(),
    }
    return mo_otel_emit(json.dumps(payload, separators=(",", ":")))


# Mirrors lib/mo_otel.sh:59-64
def mo_otel_root_end(rc: str | int = "0") -> int:
    """Record run end. Maps a shell rc to span status.

    Bash semantics:
        local rc="${1:-0}" status="success"
        [ "$rc" = "0" ] || status="failure"
    `rc` defaults to "0" (a string, matching bash). status="success" iff
    rc == "0", else "failure". end_ms is captured at emit time and is
    NOT deterministic across subprocess invocations — the parity test
    compares it within a 1500ms tolerance window.
    """
    if not mo_otel_enabled():
        return 0
    status = "success" if str(rc) == "0" else "failure"
    payload = {
        "type": "root_end",
        "end_ms": _now_ms(),
        "status": status,
    }
    return mo_otel_emit(json.dumps(payload, separators=(",", ":")))


# Mirrors lib/mo_otel.sh:68-72
def mo_otel_agent(
    node_id: str,
    node_type: str = "",
    start_ms: int = 0,
    end_ms: int = 0,
    verdict: str = "",
) -> int:
    """Record one agent (workflow node) span.

    Bash semantics:
        local node_id="${1:?}" node_type="${2:-}" start_ms="${3:-0}" end_ms="${4:-0}" verdict="${5:-}"
    Only `node_id` is required. Other args default to their bash
    defaults. start_ms/end_ms are explicit caller args (deterministic
    across subprocess invocations) and the parity test compares them
    EXACTLY — only the internally-timestamped root_begin/root_end fields
    need a tolerance window.
    """
    if not mo_otel_enabled():
        return 0
    if not node_id:
        print("mo_otel_agent: node_id required", file=sys.stderr)
        return 0
    payload = {
        "type": "agent",
        "node_id": node_id,
        "node_type": node_type,
        "start_ms": int(start_ms),
        "end_ms": int(end_ms),
        "verdict": verdict,
    }
    return mo_otel_emit(json.dumps(payload, separators=(",", ":")))


# Mirrors lib/mo_otel.sh:77-88
def mo_otel_flush() -> int:
    """Flush the buffer through `mini_ork.otel_export --from-jsonl`.

    Bash semantics:
        mo_otel_enabled || return 0
        [ -s "$buf" ] || return 0
        local root="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
        local -a flags=(--from-jsonl "$buf")
        [ -n "${MINI_ORK_DB:-}" ] && [ -f "${MINI_ORK_DB:-}" ] && flags+=(--db "$MINI_ORK_DB")
        [ "${MO_OTEL_DRY_RUN:-0}" = "1" ] && flags+=(--dry-run)
        (cd "$root" && python3 -m mini_ork.otel_export "${flags[@]}") || true
        return 0
    On a successful live POST the exporter renames the buffer to
    *.sent; on any failure the JSONL stays in place for resync. The
    subshell's `|| true` swallows subprocess errors — the port matches
    that with a bare `except` around `subprocess.run`. Output is NOT
    captured (no `capture_output=True`) so the dry-run OTLP payload
    inherits the caller's stdout, mirroring the bash subshell's
    pass-through behavior.
    """
    if not mo_otel_enabled():
        return 0
    buf = mo_otel_buf()
    try:
        if os.path.getsize(buf) == 0:
            return 0
    except OSError:
        return 0
    env_root = os.environ.get("MINI_ORK_ROOT")
    if env_root:
        root = env_root
    else:
        # Mirror bash `dirname ${BASH_SOURCE[0]}/..` — this file lives at
        # `mini_ork/ported/mo_otel.py`, so parents[2] is the repo root.
        root = str(Path(__file__).resolve().parents[2])
    flags = ["--from-jsonl", buf]
    db = os.environ.get("MINI_ORK_DB")
    if db and os.path.isfile(db):
        flags += ["--db", db]
    if os.environ.get("MO_OTEL_DRY_RUN", "0") == "1":
        flags.append("--dry-run")
    try:
        subprocess.run(
            ["python3", "-m", "mini_ork.otel_export", *flags],
            cwd=root,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        pass
    return 0