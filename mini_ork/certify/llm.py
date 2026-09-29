"""Default model dispatch for the certify oracle.

The oracle is repo-agnostic; model invocation is intentionally narrow so the
test surface stays small and sidecar files never leak into the long-running
verifier process. Anything more elaborate (lane routing, telemetry) lives in
`mini_ork.dispatch` and is reached via the in-process entrypoint here.

`mo_llm_dispatch` reports spend only through its `.cost` sidecar (the
`llm_calls` row is written by the CLI wrapper, which this in-process path does
not go through), so the sidecar is read here and summed. A certificate reports
what the verdict cost from `spent()`, never from a table this path never wrote.
"""
from __future__ import annotations

import os
import shutil
import tempfile

from mini_ork.context import scoped_environ
from mini_ork.dispatch.llm_dispatch import mo_llm_dispatch

# The code-writing lane: probes and invariants are pytest code, so the default is a
# code lane, not an analysis lane. Override per install with MO_CERTIFY_MODEL.
DEFAULT_MODEL = "minimax"

_spend = {"usd": 0.0, "calls": 0}


def spent() -> dict:
    """Cumulative spend of every `default_dispatch` call in this process."""
    return dict(_spend)


def reset_spend() -> None:
    _spend["usd"], _spend["calls"] = 0.0, 0


def default_dispatch(prompt: str) -> str:
    """Invoke the worker lane and return the text body. Empty string on failure.

    The oracle already treats empty output as "could not build a probe" →
    UNVERIFIED (see `oracle.judge`). Swallowing exceptions here is the contract:
    a dispatch crash must not propagate and break a long-running verify loop.
    """
    model = os.environ.get("MO_CERTIFY_MODEL", DEFAULT_MODEL)
    fd, out_file = tempfile.mkstemp(prefix="certify-oracle-", suffix=".out")
    # Agentic lanes have file tools and run in MO_TARGET_CWD (else the process cwd).
    # Measured: while writing a probe, a lane recreated the package under test in the
    # caller's cwd to try its test. `certify` runs inside the user's own repo, so every
    # call gets a throwaway directory — the model can scribble; the repo stays untouched.
    scratch = tempfile.mkdtemp(prefix="certify-lane-")
    try:
        os.close(fd)
        with scoped_environ({"MO_TARGET_CWD": scratch}):
            rc = mo_llm_dispatch(model, prompt, out_file)
        _spend["calls"] += 1
        try:
            with open(out_file + ".cost", encoding="utf-8") as f:
                _spend["usd"] += float(f.read().strip() or 0)
        except (OSError, ValueError):
            pass
        if rc != 0:
            return ""
        try:
            with open(out_file, encoding="utf-8") as f:
                return f.read()
        except OSError:
            return ""
    except Exception:
        return ""
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
        for suffix in ("", ".cost", ".model", ".tokens", ".err.log"):
            try:
                os.remove(out_file + suffix)
            except OSError:
                pass


__all__ = ["DEFAULT_MODEL", "default_dispatch", "reset_spend", "spent"]
