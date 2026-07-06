"""Parity gate: mini_ork.ported.mini_ork_execute helper layer vs bin/mini-ork-execute.

bin/mini-ork-execute is a CLI (runs setup at top level), so we EXTRACT each pure
helper's definition by name, source that in isolation, and compare its output to
the port. Covers the deterministic helper layer (reward/lane/chain/finish-reason/
code-region); the orchestration core (_dispatch_node + DAG loop) is a separate,
harsh-critic-gated increment.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.ported import mini_ork_execute as ex  # noqa: E402

BIN = REPO / "bin" / "mini-ork-execute"
_SRC = BIN.read_text().splitlines()


def _extract(name: str) -> str:
    """Pull `<name>() { ... }` (closing brace at column 0) from the CLI."""
    start = None
    for i, ln in enumerate(_SRC):
        if re.match(rf"^{re.escape(name)}\(\) *\{{", ln):
            start = i
            break
    assert start is not None, f"function {name} not found"
    body = [_SRC[start]]
    for ln in _SRC[start + 1:]:
        body.append(ln)
        if ln == "}":
            break
    return "\n".join(body)


def _call(name, *args, env=None):
    fn = _extract(name)
    script = f'{fn}\n{name} "$@"'
    return subprocess.run(["bash", "-c", script, "_", *map(str, args)],
                          capture_output=True, text=True,
                          env={**os.environ, **(env or {})}).stdout.strip()


def test_reward_from_status_parity():
    cases = [("", "approve"), ("", "needs_revision"), ("published", ""), ("failed", ""),
             ("reviewing", ""), ("success", "pass"), ("", "ESCALATE"), ("PUBLISHED", ""),
             ("weird", "weird")]
    for status, verdict in cases:
        rb = _call("_mo_reward_from_status", status, verdict)
        rp = ex.reward_from_status(status, verdict)
        assert rb == rp, f"({status!r},{verdict!r}): bash={rb!r} py={rp!r}"


def test_dispatch_chain_parity():
    env = {"MO_FALLBACK_CODING": "minimax,codex,sonnet", "MO_FALLBACK_REVIEW": "opus,kimi,sonnet"}
    for nt, lead in [("implementer", "minimax"), ("implementer", "glm"), ("reviewer", "opus"),
                     ("reviewer", "sonnet"), ("verifier", "kimi"), ("unknown_role", "codex"),
                     ("planner", "codex")]:
        rb = _call("_mo_dispatch_chain", nt, lead, env=env)
        rp = ex.dispatch_chain(nt, lead)
        assert rb == rp, f"({nt},{lead}): bash={rb!r} py={rp!r}"


def test_learning_static_lane_parity():
    env = {"MO_FRONTIER_LANE": "opus_lens", "MO_CHEAP_LANE": "kimi_lens"}
    for nt, lane in [("reviewer", "reviewer"), ("researcher", "researcher"),
                     ("implementer", "implementer"), ("planner", "planner"),
                     ("researcher", "glm_lens"), ("reviewer", "custom_lane")]:
        rb = _call("_mo_learning_static_lane", nt, lane, env=env)
        rp = ex.learning_static_lane(nt, lane)
        assert rb == rp, f"({nt},{lane}): bash={rb!r} py={rp!r}"


def test_finish_reason_parity():
    for rc, text in [(124, ""), (43, ""), (1, "lane_fuse_open here"),
                     (1, "cost_circuit_open spent"), (2, "generic error"), (0, "")]:
        rb = _call("_mo_finish_reason_for_failure", rc, text)
        rp = ex.finish_reason_for_failure(rc, text)
        assert rb == rp, f"({rc},{text!r}): bash={rb!r} py={rp!r}"


def test_infer_code_region_parity(tmp_path):
    import json
    payloads = [
        json.dumps({"files_written": ["src/foo.py", "src/bar.py"]}),
        json.dumps({"files_written": ["README.md"]}),
        json.dumps({"files_written": []}),
        json.dumps({"files_written": "[\"lib/x.sh\"]"}),          # json-string form
        json.dumps({"files_written": ["./pkg/mod.py"]}),
        json.dumps({"other": 1}),
        "not json",
    ]
    # run both with MINI_ORK_RUN_DIR unset + a shared cwd so relative paths match
    env = {k: v for k, v in os.environ.items() if k not in ("MINI_ORK_RUN_DIR", "RUN_DIR")}
    for p in payloads:
        rb = subprocess.run(["bash", "-c", f'{_extract("_mo_infer_trace_code_region")}\n'
                             '_mo_infer_trace_code_region "$1"', "_", p],
                            capture_output=True, text=True, cwd=tmp_path, env=env).stdout.strip()
        old = dict(os.environ); os.environ.clear(); os.environ.update(env)
        cwd = os.getcwd(); os.chdir(tmp_path)
        try:
            rp = ex.infer_trace_code_region(p)
        finally:
            os.chdir(cwd); os.environ.clear(); os.environ.update(old)
        assert rb == rp, f"{p!r}: bash={rb!r} py={rp!r}"
