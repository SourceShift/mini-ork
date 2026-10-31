"""Lane conformance: does a lane, through its real harness, keep the dispatch contract?

``lane_health`` asks a gateway whether it accepts a key — one token over HTTP,
no harness involved. A lane can pass that and still be unusable: a CLI that
ignores the pinned cwd, cannot use the tools a node is granted, wraps its answer
in prose the parser rejects, stops reporting usage so cost accounting reads
zero, or writes into a tree it was only asked to read. Each of those has only
ever surfaced as a failed run.

A conformance probe is one real dispatch through ``dispatch_model`` — the argv,
env, cwd pin and default tool grants a node gets — on a task with an
unguessable answer: a 128-bit nonce is written to ``conformance-probe.txt`` in
a fresh scratch directory, and the lane is asked to read it and reply
``{"token": "<nonce>"}``.

==================  ==========================================================
check               passes when
==================  ==========================================================
``answered``        the dispatch returned rc 0 with text
``cwd_and_tools``   the nonce came back: the harness ran in the pinned cwd and
                    could read a file there
``json_contract``   the reply is a JSON object whose ``token`` is the nonce,
                    by the same rule the planner shape check applies
``usage_reported``  input + output tokens > 0
``read_only``       the scratch directory holds only the probe file afterwards
==================  ==========================================================

The dispatch is written to ``llm_calls`` like any other (it costs money), and
the verdict is appended to the audit chain as ``conformance_probe`` on target
``lane:<name>``, together with the harness identity it ran under. ``passed``
answers "has the harness this lane runs NOW passed?" — a CLI upgrade needs a
fresh probe — and ``MO_REQUIRE_CONFORMANCE=1`` makes the lane preflight refuse
a lane that has not.
"""
from __future__ import annotations

import json
import os
import secrets
import shutil
import tempfile
import time
from collections.abc import Callable, Mapping
from pathlib import Path

from ..stores import audit_chain
from . import harness_identity
from .models import DispatchRequest, DispatchResult
from .predicates import _slice_first_json, _strip_code_fence

PROBE_FILE = "conformance-probe.txt"
EVENT = "conformance_probe"
CHECKS = ("answered", "cwd_and_tools", "json_contract", "usage_reported", "read_only")

Dispatch = Callable[[DispatchRequest, "str | os.PathLike | None"], DispatchResult]


def _prompt() -> str:
    return (
        "This is a conformance check of your harness. Read the file "
        f"{PROBE_FILE} in your current working directory. Reply with ONLY a JSON "
        'object of the form {"token": "<the exact contents of that file>"} with '
        "no prose and no code fence. Do not create, modify or delete any file."
    )


def _reply_object(text: str) -> dict | None:
    body = _strip_code_fence(text.strip())
    for candidate in (body, _slice_first_json(body)):
        if not candidate:
            continue
        try:
            value = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    return None


def _default_dispatch(request: DispatchRequest, root) -> DispatchResult:
    from .providers import dispatch_model

    return dispatch_model(request, root)


def probe(lane: str, *, root: str | os.PathLike | None = None, db: str | None = None,
          timeout_s: float = 180.0, dispatch: Dispatch | None = None) -> dict:
    """Run one conformance probe on ``lane`` and, when ``db`` exists, record it."""
    run = dispatch or _default_dispatch
    nonce = secrets.token_hex(16)
    scratch = tempfile.mkdtemp(prefix="mo-conformance-")
    try:
        Path(scratch, PROBE_FILE).write_text(nonce + "\n", encoding="utf-8")
        request = DispatchRequest(
            model=lane, prompt=_prompt(), timeout_s=float(timeout_s), max_turns=4,
            cwd=scratch,
            # The probe is how a lane earns conformance, so the preflight gate
            # that demands it must not refuse the probe itself.
            env={"MO_TARGET_CWD": scratch, "MO_REQUIRE_CONFORMANCE": "0"},
        )
        started = time.monotonic()
        result = run(request, root)
        duration_ms = int((time.monotonic() - started) * 1000)
        leftovers = sorted(name for name in os.listdir(scratch) if name != PROBE_FILE)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

    text = result.text or ""
    reply = _reply_object(text) if text.strip() else None
    usage = result.usage
    checks = {
        "answered": bool(result.ok and text.strip()),
        "cwd_and_tools": nonce in text,
        "json_contract": reply is not None and str(reply.get("token", "")).strip() == nonce,
        "usage_reported": (usage.input_tokens + usage.output_tokens) > 0,
        "read_only": not leftovers,
    }
    failures = [name for name in CHECKS if not checks[name]]
    verdict = {
        "lane": lane,
        "ok": not failures,
        "checks": checks,
        "failures": failures,
        "harness": harness_identity.identity(lane, root) or {},
        "duration_ms": duration_ms,
        "cost_usd": float(result.cost_usd or 0.0),
        # The tail: a harness prints its own chatter first and the reason last.
        "error": (result.error or "").strip()[-300:],
        "leftovers": leftovers[:10],
    }
    if db and os.path.isfile(db):
        _record(db, verdict, result)
    return verdict


def _record(db: str, verdict: dict, result: DispatchResult) -> None:
    from .llm_dispatch import redact_secrets, write_llm_calls_row
    from .providers import provider_for_model

    lane = verdict["lane"]
    usage = result.usage
    write_llm_calls_row(
        db, provider_for_model(lane), lane, "default", "mini-ork:conformance", "conformance",
        "success" if result.ok else "failed", verdict["duration_ms"],
        f"{verdict['cost_usd']:.6f}", redact_secrets(verdict["error"]),
        usage.input_tokens, usage.output_tokens,
        json.dumps({"harness": verdict["harness"], "conformance": True}),
        usage.cached_input_tokens, usage.cache_creation_tokens,
        thinking_tokens=usage.thinking_tokens,
    )
    verdict["audit"] = audit_chain.record(
        db, EVENT, "conformance", f"lane:{lane}",
        {key: verdict[key] for key in ("ok", "checks", "failures", "harness",
                                       "duration_ms", "cost_usd")},
    )


def latest(db: str, lane: str) -> dict | None:
    """The newest conformance verdict on record for ``lane``, or ``None``."""
    import sqlite3

    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        row = con.execute(
            "SELECT audit_id FROM audit_log WHERE event_type=? AND target=? "
            "ORDER BY audit_id DESC LIMIT 1", (EVENT, f"lane:{lane}"),
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        con.close()
    return audit_chain.find_row(db, int(row[0])) if row else None


def passed(lane: str, *, db: str, root: str | os.PathLike | None = None,
           environment: Mapping[str, str] | None = None) -> tuple[bool, str]:
    """``(ok, reason)``: has the harness ``lane`` runs now passed a probe?"""
    record = latest(db, lane)
    hint = f"run `mini-ork providers conform {lane}`"
    if record is None or not isinstance(record.get("event"), Mapping):
        return False, f"{lane}: no conformance probe on record — {hint}"
    event = record["event"]
    if not event.get("ok"):
        failed = ", ".join(event.get("failures") or []) or "unknown"
        return False, f"{lane}: last conformance probe failed ({failed}) — {hint}"
    probed = event.get("harness") or {}
    current = harness_identity.identity(lane, root, environment=environment) or {}
    if (probed.get("harness"), probed.get("version")) != (current.get("harness"),
                                                          current.get("version")):
        return False, (f"{lane}: harness changed since its conformance probe "
                       f"({probed.get('version') or '?'} -> {current.get('version') or '?'})"
                       f" — {hint}")
    return True, "conformant"


__all__ = ["CHECKS", "EVENT", "PROBE_FILE", "latest", "passed", "probe"]
