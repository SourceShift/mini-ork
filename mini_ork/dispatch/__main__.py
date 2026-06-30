"""CLI entrypoint for the Python dispatch layer (Phase-0 wiring, ADR-001).

    printf '%s' "$prompt" | python3 -m mini_ork.dispatch <model> --out <file>

Reads the prompt from STDIN (never argv — E2BIG-proof), dispatches via
mini_ork.dispatch.dispatch_model, writes the assistant text to --out (or
stdout), persists an llm_calls row when MINI_ORK_DB is set, and exits with the
provider's exit code so a bash caller (lib/llm-dispatch.sh MO_DISPATCH_BACKEND)
sees a faithful rc.

Usable standalone today; it is also the bridge a future MO_DISPATCH_BACKEND=
python switch in lib/llm-dispatch.sh will delegate to (that live-path flip,
with its sidecar-contract integration, is a separate slice).
"""

from __future__ import annotations

import argparse
import os
import sys

from .models import DispatchRequest
from .providers import dispatch_model, provider_for_model
from .telemetry import persist_call


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mini_ork.dispatch", add_help=True)
    parser.add_argument("model", help="lane name, e.g. codex / opus / glm")
    parser.add_argument("--out", default="", help="write assistant text here (default: stdout)")
    parser.add_argument("--feature", default=os.environ.get("MO_LANE_FEATURE", "mini-ork:dispatch"))
    parser.add_argument("--tier", default=os.environ.get("MO_LANE_TIER", "default"))
    parser.add_argument("--actor", default=os.environ.get("MO_LANE_ACTOR", ""))
    parser.add_argument("--timeout", type=float, default=float(os.environ.get("MO_NODE_TIMEOUT_S", "1500")))
    args = parser.parse_args(argv)

    prompt = sys.stdin.read()
    request = DispatchRequest(model=args.model, prompt=prompt, timeout_s=args.timeout)
    result = dispatch_model(request)

    # Emit the assistant body to the caller (out-file or stdout).
    if args.out:
        try:
            with open(args.out, "w", encoding="utf-8") as fh:
                fh.write(result.text)
        except OSError as exc:
            sys.stderr.write(f"[mini_ork.dispatch] could not write {args.out}: {exc}\n")
            return 127
    else:
        sys.stdout.write(result.text)

    # Persist telemetry (best-effort; never changes the exit code).
    db = os.environ.get("MINI_ORK_DB", "")
    if db:
        try:
            persist_call(
                db,
                result,
                provider=provider_for_model(args.model),
                feature_name=args.feature,
                tier=args.tier,
                actor=args.actor or None,
                run_id=os.environ.get("MINI_ORK_RUN_ID") or None,
                traceparent=os.environ.get("MO_TRACEPARENT") or None,
            )
        except Exception as exc:  # telemetry must never break dispatch
            sys.stderr.write(f"[mini_ork.dispatch] telemetry write failed: {exc}\n")

    status = "ok" if result.ok else f"FAIL rc={result.rc}"
    sys.stderr.write(
        f"[mini_ork.dispatch] {args.model} {status} "
        f"in={result.usage.input_tokens} out={result.usage.output_tokens} "
        f"cost=${result.cost_usd:.6f} {result.duration_ms}ms\n"
    )
    if not result.ok and result.error:
        sys.stderr.write(result.error.rstrip() + "\n")
    return result.rc


if __name__ == "__main__":
    raise SystemExit(main())
